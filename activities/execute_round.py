"""Executor round activity: fresh git worktree → headless Claude → structured result.

The LangGraph produce→gate→correct loop runs INSIDE this activity (the loop lives
inside the node). Gates run as plain function calls here; the workflow-level gate
activity (gate.py) is for milestone checks.

Safety: bypassPermissions is set only when LOOPGRAPH_IN_CONTAINER=1 (the worker
container); anywhere else the activity refuses to run Claude.

Idempotency (Temporal retries): worktree creation reuses an existing path/branch;
a retried activity simply re-runs the round in the same worktree.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

from temporalio import activity

from activities import browser
from activities.gate import _run_one, load_gates
from activities.owner import read_answers
from activities.stream import log_name, stream_query
from graphs.round_graph import run_round

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"

CORRECTION_PREAMBLE = (
    "\n\n# Correction required\n\n"
    "Your previous attempt left these gates RED. Fix the implementation — "
    "never weaken the gate. Red-gate output follows:\n\n"
)

_OUTPUT_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)

# The worker holds the bot credentials because send_card runs here, and the SDK
# subprocess inherits this process's environment. Both nodes have Bash and the
# container is on the host network, so an executor that decided to message the
# owner directly could. It must not be able to: the supervisor is the only path
# to the owner, and a node that could message them could also answer its own
# card. options.env overrides what is inherited.
NO_TELEGRAM = {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}


# ---------- pure helpers (tested without Claude) ----------

def assemble_prompt(brief: str, constraints: str, work_item: str, directive: str | None = None,
                    owner_answers: str = "") -> str:
    contract = (PROMPTS / "executor.md").read_text()
    d = f"\n\n# Supervisor directive (binding — this unit only)\n\n{directive.strip()}\n" if directive else ""
    return (
        f"{contract}\n\n# Feature brief\n\n{brief.strip()}\n\n"
        f"# Constraints (learned — binding)\n\n{constraints.strip() or '(none yet)'}\n\n"
        # Quote these rather than reconstructing them: a directive carries a
        # button tap as the bare letter, and an executor guessing what the
        # letter meant is an executor making the owner's answer up.
        f"# Owner answers (recorded by the engine)\n\n{owner_answers.strip() or '(none)'}\n\n"
        f"# Work item for this round\n\n{work_item.strip()}\n{d}"
    )


def parse_final_json(text: str) -> dict:
    """Extract the LAST ```json fence from executor output (the output contract)."""
    matches = _OUTPUT_RE.findall(text)
    if not matches:
        raise ValueError("executor produced no final ```json block")
    return json.loads(matches[-1])


CANDIDATE_CAP = 200  # chars per candidate


def clean_candidates(raw) -> list[str]:
    """The things an executor found by hand, one line each, no repeats.

    A sweep pastes these raw into the next item's text, which lands in the
    executor prompt and in the auditor's scope block, above the engine's own
    sections. A newline in one could open a "# Gate results" of its own, which is
    the bug flatten_claim fixed for claims; audit.py imports from this module and
    never the other way round, so the collapse is repeated here rather than
    imported. Cleaning happens before anything stores a candidate, so nothing
    downstream ever holds a raw one.
    """
    if not isinstance(raw, list):
        return []
    out = []
    for c in raw:
        if not isinstance(c, str):
            continue
        line = " ".join(c.split())[:CANDIDATE_CAP]
        if line and line not in out:
            out.append(line)
    return out


def parse_porcelain(porcelain: str) -> list[str]:
    """`git status --porcelain -z --untracked-files=all` → the paths that changed.

    Two things force the flags. Without `-z`, git C-quotes any path that is not
    plain ASCII ("caf\303\251.txt"), and stripping the quotes leaves the escapes
    behind, so the path names no file on disk and the checkpoint dies on it.
    Without `--untracked-files=all`, a new directory collapses to one `dir/` entry,
    so the write set, the commit message and the audit all say one path while
    `git add -- dir/` commits everything underneath it.

    Records are NUL-terminated. A rename is two records, new name then old; only
    the new name is part of the write set.
    """
    files = []
    parts = [p for p in porcelain.split("\0") if p]
    i = 0
    while i < len(parts):
        rec = parts[i]
        if len(rec) < 4:
            i += 1
            continue
        files.append(rec[3:])
        # R and C records are followed by the source path in its own record.
        if rec[0] in ("R", "C") or rec[1] in ("R", "C"):
            i += 1
        i += 1
    return sorted(files)


def run_paths(run_dir: str, run_token: str) -> tuple[str, str]:
    """The worktree and the branch a run works in.

    Every activity that touches the run's checkout derives these two strings, and
    deriving them twice is a rename away from a sweep whose detectors read one
    worktree while its executor writes another.
    """
    run = Path(run_dir)
    worktree = str(run / "worktrees" / (run_token or "run"))
    branch = f"lg-{run.name}-{run_token}" if run_token else f"lg-{run.name}"
    return worktree, branch


# ---------- container-side effects ----------

async def _git(*args: str, cwd: str | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    text = out.decode(errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed ({proc.returncode}): {text[-2000:]}")
    return text


async def ensure_worktree(target_repo: str, worktree: str, branch: str) -> None:
    if os.path.isdir(worktree):
        # It has to actually BE a worktree. Returning on isdir alone meant a plain
        # directory left at that path was treated as one, and the reset that runs
        # next would `git reset --hard` and `git clean -fd` whatever repository
        # encloses it — inside the container, the bind-mounted engine repo.
        if not os.path.exists(os.path.join(worktree, ".git")):
            raise RuntimeError(
                f"{worktree} exists but is not a git worktree. Remove it and start "
                f"the run again; refusing to reset whatever repo contains it.")
        return  # retry of a round that already made its worktree
    branches = await _git("branch", "--list", branch, cwd=target_repo)
    if branch in branches:
        await _git("worktree", "add", worktree, branch, cwd=target_repo)
    else:
        await _git("worktree", "add", "-b", branch, worktree, cwd=target_repo)


@activity.defn
async def run_baseline(target_repo: str) -> str:
    """The commit a run starts from, captured once before the first round.

    reset_to_checkpoint fell back to HEAD whenever base_commit was None, and the
    workflow had no base_commit until its first accepted checkpoint. So on a
    single-item brief — the common case — a Temporal retry adopted the commit the
    executor had made during the failed attempt as the baseline, which is the
    exact failure the reset exists to prevent.
    """
    return (await _git("rev-parse", "HEAD", cwd=target_repo)).strip()


async def reset_to_checkpoint(worktree: str, base_commit: str | None = None) -> None:
    """Start a round from the last checkpoint, dropping anything uncommitted.

    Rounds restart at 1 for each work item, so resetting only on a redo left a
    PARKED item's gate-red changes lying in the shared worktree, where the next
    item swept them into its write set, its audit diff and its commit. Committed
    checkpoints are untouched, and `clean -fd` leaves ignored files alone, so a
    node_modules or a venv survives.

    `base_commit` is the checkpoint the workflow recorded. Passing it matters on a
    Temporal retry: the executor may have committed during the attempt that
    failed, and resetting to HEAD would adopt that commit as the new baseline,
    where nothing would ever undo it and no auditor would ever see it.
    """
    await _git("reset", "-q", "--hard", base_commit or "HEAD", cwd=worktree)
    await _git("clean", "-qfd", cwd=worktree)


async def undo_self_commit(worktree: str, start_head: str) -> bool:
    """Put an executor's own commit back into the working tree.

    An executor that commits hides its work: `git status --porcelain` comes back
    clean, the write set is empty, and the checkpoint refuses a round that
    actually passed every gate. The prompt forbids committing; this is what
    happens when it does anyway. A mixed reset keeps every change and drops the
    commit, so the engine still commits only what the audit accepted, and the
    audit still sees a diff to read.
    """
    if (await _git("rev-parse", "HEAD", cwd=worktree)).strip() == start_head:
        return False
    await _git("reset", "-q", start_head, cwd=worktree)
    return True


async def run_executor(prompt: str, feedback: str | None, worktree: str, log_path: str,
                       env: dict[str, str] | None = None,
                       mcp_servers: dict | None = None) -> dict:
    """One headless Claude produce (or correct) pass inside the worktree.

    `env` is the run's app environment when it has one, laid over the blanked bot
    credentials and never the other way round: `app_env` carries none of those
    keys, so only the order says the executor cannot get the token back.
    `mcp_servers` is the `playwright` entry, and it arrives only when the app came
    up and the browser container answered."""
    if os.environ.get("LOOPGRAPH_IN_CONTAINER") != "1":
        raise RuntimeError("refusing to run Claude outside the worker container")
    from claude_agent_sdk import ClaudeAgentOptions

    full = prompt if not feedback else prompt + CORRECTION_PREAMBLE + feedback
    text = await stream_query(full, ClaudeAgentOptions(
        cwd=worktree,
        permission_mode="bypassPermissions",
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        env=NO_TELEGRAM | (env or {}),
        mcp_servers=mcp_servers or {},
        # With or without a browser: the worktree is the target repo, and its own
        # .mcp.json would otherwise hand this executor servers nobody declared.
        strict_mcp_config=True,
    ), log_path)
    try:
        payload = parse_final_json(text)
    except ValueError:
        payload = {"claims": [], "files_changed": [], "blocked": [],
                   "summary": "executor violated the output contract"}
    payload["parse_ok"] = "```json" in text
    return payload


@activity.defn
async def execute_round(run_dir: str, target_repo: str, work_item: str, round_no: int = 1,
                        directive: str | None = None, item_no: int = 1,
                        run_token: str = "", base_commit: str | None = None) -> dict:
    """One round on one work item.

    All items in a run share one branch and one worktree, so item 2 starts from
    item 1's checkpoint and the owner gets a single branch to merge at the end.

    `run_token` makes that branch unique to this run. Deriving it from the run-dir
    name alone meant re-running the same run dir inherited the old branch, so work
    the owner had explicitly discarded came back and was merged by the next run.

    `base_commit` is the last checkpoint the WORKFLOW recorded. Resetting to
    whatever HEAD happened to be re-baselined onto a commit the executor made
    during a failed attempt, which then never got undone and never got audited.
    """
    run = Path(run_dir)
    brief = (run / "brief.md").read_text()
    constraints = (run / "constraints.md").read_text() if (run / "constraints.md").exists() else ""
    prompt = assemble_prompt(brief, constraints, work_item or brief, directive,
                             read_answers(run_dir))

    worktree, branch = run_paths(run_dir, run_token)
    base_branch = (await _git("branch", "--show-current", cwd=target_repo)).strip()
    await ensure_worktree(target_repo, worktree, branch)
    await reset_to_checkpoint(worktree, base_commit)
    (run / "logs").mkdir(parents=True, exist_ok=True)
    log_path = str(run / "logs" / log_name(item_no, round_no, "executor"))

    gates = load_gates(str(run / "gates.yaml"))

    # A run with a browser.yaml has a web app. The file is read once, and an
    # unreadable one is an error value that reaches the executor's prompt rather
    # than a raise that parks the item. Gates get the browser endpoint whether or
    # not the file passed the check, and never the app's own keys (AC-8).
    cfg = browser.read_browser_config(run_dir)
    config_error = cfg.message if isinstance(cfg, browser.BrowserConfigError) else None
    gates_env = browser.gate_env(run_dir)
    app, servers = None, None

    async def exec_fn(p: str, fb: str | None) -> dict:
        return await run_executor(p, fb, worktree, log_path, env=app, mcp_servers=servers)

    async def gate_fn() -> list[dict]:
        results = []
        for g in gates:
            activity.heartbeat(f"gate {g['name']}")
            results.append(await _run_one(g, worktree, heartbeat=activity.heartbeat,
                                          env=gates_env))
        return results

    start_head = (await _git("rev-parse", "HEAD", cwd=worktree)).strip()

    # The last two statements before the try that stops it, and deliberately so:
    # anything that raises between them and the finally leaks the process group
    # and the reserved port, and Temporal's retry then starts a second server
    # beside the one nobody can reach. The serve reads the tree as the reset left
    # it, which is the checkpoint the round starts from.
    port = serve = None
    if isinstance(cfg, dict):
        port = browser.pick_port()
        serve = await browser.start_serve(cfg["serve"]["cmd"], worktree, port,
                                          cfg["serve"]["ready_timeout"],
                                          heartbeat=activity.heartbeat)
    try:
        evidence = None
        if config_error:
            evidence = browser.browser_evidence(None, None, [], config_error=config_error)
        elif serve is not None:
            app = browser.app_env(port)
            # Asked once, and kept apart from the serve's own `ready`: a run can
            # have its app up and Chromium down, and a prompt that folded the two
            # together told a model its tools were attached to a browser that was
            # not there (AC-27). Nothing to attach to when the app never came up.
            attach = None
            if serve.ready:
                attach = await browser.attach_browser(browser.browser_endpoint())
            evidence = browser.browser_evidence(serve, attach, [])
            if serve.ready and attach.ok:
                output_dir = browser.make_output_dir(run_dir)
                servers = {browser.MCP_SERVER_NAME: browser.mcp_server_entry(
                    output_dir, browser.browser_endpoint(), browser.browser_port())}
        if evidence:
            # Joined the way every other section of the prompt is, and before the
            # loop, so every attempt of the round carries it. Empty when the app
            # is up and the browser answered: the tools are in the options and the
            # contract has already said what they are for.
            block = browser.executor_block(evidence)
            if block:
                prompt += "\n\n" + block
        final = await run_round(prompt, exec_fn, gate_fn)
    finally:
        if serve is not None:
            await serve.stop()

    self_committed = await undo_self_commit(worktree, start_head)

    diff_stat = await _git("diff", "--stat", "HEAD", cwd=worktree)
    files = parse_porcelain(await _git(
        "status", "--porcelain", "-z", "--untracked-files=all", cwd=worktree))
    return {
        "status": final["status"],           # green | escalated
        "attempts": final["attempt"],
        "claims": final["result"].get("claims", []),
        "summary": final["result"].get("summary", ""),
        "executor_files": final["result"].get("files_changed", []),
        # What the executor says only the owner can settle. It has no channel of
        # its own and must not have one: the supervisor reads these and decides
        # which, if any, become a card.
        "blocked": final["result"].get("blocked", []) or [],
        # Extra work the executor spotted while doing this item. The sweep hands
        # them to the next item; the fallback payload run_executor builds when the
        # JSON block is missing has no such key, and .get covers it.
        "candidates": clean_candidates(final["result"].get("candidates")),
        "files": files,
        "diff_stat": diff_stat.strip(),
        "gate_results": final["gate_results"],
        "worktree": worktree,
        "base_branch": base_branch,
        "branch": branch,
        "item_no": item_no,
        "self_committed": self_committed,
    }
