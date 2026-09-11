"""Supervisor activity: clean-context audit, read-only tools, verdict packet.

Never sees the executor's transcript — only the brief, constraints, any answers
the owner gave, claims, write set, diff, and gate results. Gate re-runs happen in CODE (checkpoint.py),
never by the model. Read/Glob/Grep only: the auditor cannot modify anything.

Pure parsing helpers are tested without Claude; the SDK call is thin.
"""

from __future__ import annotations

import os
from pathlib import Path

from temporalio import activity

from activities import browser
from activities.execute_round import NO_TELEGRAM, _git, parse_final_json
from activities.owner import clean_options, read_answers
from activities.stream import log_name, stream_query

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"

DIFF_CAP = 12_000  # chars of unified diff handed to the auditor; keeps prompts bounded
BLOCKED_CAP = 6      # blockers shown per round; a longer list is the executor stalling
VERDICTS = {"accept", "redo", "plan", "stop", "ask"}


# ---------- pure helpers (tested without Claude) ----------

def parse_verdict(text: str) -> dict:
    """Extract the verdict packet. An unparseable or unknown verdict is a `redo`:
    the conservative default keeps an unclear audit from waving work through."""
    try:
        pkt = parse_final_json(text)
        verdict = pkt["verdict"]
    except (ValueError, KeyError):
        return {"verdict": "redo", "reasons": ["audit produced no parseable verdict packet"],
                "directive": {}, "parse_ok": False}
    if verdict not in VERDICTS:
        return {"verdict": "redo", "reasons": [f"audit returned unknown verdict {verdict!r}"],
                "directive": {}, "parse_ok": False}
    pkt.setdefault("reasons", [])
    pkt.setdefault("directive", {})
    # Not setdefault: the key being present says nothing about its shape, and the
    # card, the buttons and owner-answers.md all read this as letter to label.
    # Normalising it here is what keeps every one of them from having to ask.
    pkt["options"] = clean_options(pkt.get("options"))
    pkt["parse_ok"] = True
    return pkt


CLAIM_CAP = 1200  # chars per claim


def flatten_claim(claim) -> str:
    """One claim, on one line, unable to impersonate the engine.

    Claims come straight out of the executor's JSON with no validation and land in
    the middle of the audit prompt. A claim containing newlines could open its own
    "# Gate results" section, and the auditor has no way to tell engine text from
    executor text. Collapsing the whitespace removes the trick.
    """
    return " ".join(str(claim).split())[:CLAIM_CAP]


def declared_vs_actual(round_result: dict) -> tuple[list[str], list[str]]:
    """(files the executor claimed but git did not see, files git saw but the
    executor never declared).

    executor.md tells the executor its `files_changed` is checked against git
    status. Nothing checked it: the declared list was stored and read by nobody,
    and the auditor was shown only the git-status side, so neither code nor the
    auditor could spot an omission or an invention."""
    declared = {str(f) for f in round_result.get("executor_files", []) or []}
    actual = {str(f) for f in round_result.get("files", []) or []}
    return sorted(declared - actual), sorted(actual - declared)


def format_blocked(entries) -> str:
    """The executor's blocked list, as text the supervisor can judge.

    The executor cannot reach the owner and must not. This is the only way it can
    say a decision is out of its hands, and every field is flattened for the same
    reason claims are: it is untrusted text landing in the middle of a prompt, and
    a newline in it could open a section of its own.
    """
    if not entries:
        return "(none)"
    lines = []
    for e in entries[:BLOCKED_CAP]:
        if not isinstance(e, dict):
            lines.append(f"- {flatten_claim(e)}")
            continue
        # `or {}` only stood in for a falsy value, so a list went to .items() and
        # killed the audit. The guard above checks the entry is a dict and stopped
        # one level short of the fields inside it.
        opts = clean_options(e.get("options"))
        opts_txt = "; ".join(f"{k}: {v}" for k, v in opts.items()) or "(free text)"
        lines.append(
            f"- decision: {flatten_claim(e.get('decision', '(unstated)'))}\n"
            f"  recommends: {flatten_claim(e.get('recommend', '(none)'))}\n"
            f"  options: {opts_txt}\n"
            f"  why now: {flatten_claim(e.get('why_now', '(unstated)'))}")
    if len(entries) > BLOCKED_CAP:
        lines.append(f"- [{len(entries) - BLOCKED_CAP} more, not shown]")
    return "\n".join(lines)


def assemble_audit_prompt(brief: str, constraints: str, round_result: dict, diff: str,
                          owner_answers: str = "", work_item: str = "",
                          item_no: int = 1, item_total: int = 1, kind: str = "brief",
                          *, browser_evidence: dict | None = None) -> str:
    # Keyword-only, and named for what it holds rather than `browser`: the module
    # is imported under that name and a parameter would shadow it here, so
    # `browser.audit_block(...)` would be a lookup on a dict.
    contract = (PROMPTS / "supervisor.md").read_text()
    claims = "\n".join(f"- {flatten_claim(c)}" for c in round_result.get("claims", [])) or "(no claims)"
    # Flattened for the same reason as claims: a path can contain a newline, and
    # the write set is pasted into the same prompt the forging fix was hardening.
    files = "\n".join(f"- {flatten_claim(f)}" for f in round_result.get("files", [])) or "(no files)"
    # The gate's command and its output, not just a name and an exit code: the
    # contract asks the auditor to judge whether a gate exercises the claim, which
    # it cannot do without seeing what the gate ran.
    gate_blocks = []
    for g in round_result.get("gate_results", []):
        head = f"- {g['name']}: {g['status']} (exit {g['exit_code']})"
        if g.get("note"):
            head += f" [{g['note']}]"
        tail = (g.get("output_tail") or "").strip()
        gate_blocks.append(
            f"{head}\n  command: {g.get('cmd', '(not recorded)')}\n"
            f"  output:\n```\n{tail[-1500:] or '(no output)'}\n```"
        )
    gates = "\n".join(gate_blocks) or "(no gates)"
    blocked = format_blocked(round_result.get("blocked") or [])
    violation = ""
    if round_result.get("self_committed"):
        violation = ("\n\n# Executor red line broken (engine check)\n\n"
                     "This executor committed its own work, which its contract "
                     "forbids. The engine put the changes back in the working tree "
                     "so this round could be judged at all. Treat it as a signal "
                     "about how closely the rest of the contract was followed, and "
                     "check the claims harder than usual.")
    # One of the engine's own checks, and it sits with them: what the engine saw
    # of the app it served for this audit, whether that is a list of pictures or
    # the reason there are none. Nothing at all for a run with no browser.yaml,
    # whose prompt is byte-for-byte what it was.
    browser_block = "" if browser_evidence is None else "\n\n" + browser.audit_block(
        browser_evidence)
    invented, undeclared = declared_vs_actual(round_result)
    mismatch = ""
    if invented or undeclared:
        mismatch = ("\n\n# Write-set mismatch (engine check)\n\n"
                    f"Claimed changed but git did not see: {invented or 'none'}\n"
                    f"Changed but never declared: {undeclared or 'none'}\n"
                    "The executor's contract requires these to agree. Treat a "
                    "mismatch as a finding, not a formatting quirk.")
    truncated = "\n[diff truncated by engine]" if len(diff) >= DIFF_CAP else ""
    # Which item this round covers. Without it the supervisor judged every round
    # against the WHOLE brief, so in a multi-item run it found the other items
    # missing and issued a redo that pushed the executor into another item's
    # scope. That round then reset to the checkpoint, and the work the redo
    # displaced was lost.
    scope = ""
    if kind != "brief":
        # An item the engine wrote itself. There is no brief list to point at and
        # no total to count towards: a sweep's first item is item 1 of 1 and every
        # one after it is item n of n, so left to the branch below the first would
        # get no block at all and the rest one headed "of n" about a run whose
        # length nobody knows yet.
        scope = (f"# The work item under audit (item {item_no}, {kind})\n\n"
                 f"{work_item.strip() or '(unnamed item)'}\n\n"
                 "Judge THIS item only. Another item missing from this diff is\n"
                 "not a finding. Work outside this item is still drift.\n\n")
    elif item_total > 1:
        scope = (f"# The work item under audit ({item_no} of {item_total})\n\n"
                 f"{work_item.strip() or '(unnamed item)'}\n\n"
                 "Judge THIS item only. The brief lists others; each gets its own\n"
                 "round, its own audit and its own commit, and some are already\n"
                 "committed or still to come. Another item missing from this diff is\n"
                 "not a finding. Work outside this item is still drift.\n\n")
    return (
        f"{contract}\n\n# Feature brief\n\n{brief.strip()}\n\n"
        f"{scope}"
        f"# Constraints (binding)\n\n{constraints.strip() or '(none)'}\n\n"
        # The owner's own words, written by the engine when they answered a card.
        # Without this the auditor had no way to tell an owner-authorised value
        # from one the executor invented, so it re-asked the same question every
        # round until the cap and the run died with nothing committed.
        f"# Owner answers (recorded by the engine, never by the executor)\n\n"
        f"{owner_answers.strip() or '(none)'}\n\n"
        f"# Executor claims\n\n{claims}\n\n"
        # The executor has no channel to the owner. This is its only way to say a
        # decision is not its to make, and judging these is the supervisor's job:
        # settle what it can settle, card what it cannot.
        f"# Executor says these are the owner's call\n\n{blocked}\n\n"
        f"# Write set (from git status)\n\n{files}{mismatch}{violation}{browser_block}\n\n"
        f"# Gate results\n\n{gates}\n\n"
        f"# Unified diff (capped at {DIFF_CAP} chars)\n\n```diff\n{diff}{truncated}\n```\n\n"
        f"# Worktree (read-only spot checks)\n\n{round_result.get('worktree', '')}\n"
    )


# ---------- container-side effects ----------

async def run_supervisor(prompt: str, worktree: str, log_path: str,
                         env: dict[str, str] | None = None,
                         mcp_servers: dict | None = None) -> dict:
    """The audit pass, with the app's environment and browser tools when the run
    has them. Both arrive only when the app came up and the browser answered."""
    if os.environ.get("LOOPGRAPH_IN_CONTAINER") != "1":
        raise RuntimeError("refusing to run Claude outside the worker container")
    from claude_agent_sdk import ClaudeAgentOptions

    text = await stream_query(prompt, ClaudeAgentOptions(
        cwd=worktree,
        permission_mode="bypassPermissions",
        # allowed_tools only says what is auto-approved, NOT what exists: on its
        # own it left the auditor holding Write, Edit and Bash, auto-approved. It
        # could edit the code it was judging, or push. `tools` sets the base set
        # and `disallowed_tools` denies the rest, and a deny beats bypass.
        tools=["Read", "Glob", "Grep"],
        allowed_tools=["Read", "Glob", "Grep"],
        # The browser adds two tools that run code the supervisor wrote itself,
        # and it is here to look. Denied by name, and only when it has the
        # browser at all: nothing else about the list moves.
        disallowed_tools=["Write", "Edit", "MultiEdit", "NotebookEdit", "Bash",
                          "BashOutput", "KillShell", "Task", "WebFetch", "WebSearch"]
        + (browser.SUPERVISOR_DENIED if mcp_servers else []),
        # Load nothing from disk. cwd is the worktree the executor just wrote to,
        # so without this the CLI reads .claude/settings.json and CLAUDE.md from
        # the tree under audit: the audited party could plant a hook that runs
        # shell during its own audit, and instructions telling the auditor to
        # accept. The executor still loads its project's settings; it is the one
        # being supervised, not the supervisor.
        setting_sources=[],
        # The app's keys go on over the blanked bot credentials and never the
        # other way round, the same order `run_executor` uses.
        env=NO_TELEGRAM | (env or {}),
        mcp_servers=mcp_servers or {},
        # With or without a browser: cwd is the tree under audit, and its own
        # .mcp.json would otherwise hand the supervisor servers nobody declared.
        strict_mcp_config=True,
    ), log_path)
    return parse_verdict(text)


async def diff_including_new_files(worktree: str) -> str:
    """The diff the auditor judges, including files the executor created.

    In a worktree `git diff HEAD` reports tracked changes only, so a brief that
    says "add parsers/csv.py" produced an EMPTY diff: the auditor either rejected
    work it was never shown, or accepted it sight unseen, while the checkpoint
    committed the file anyway. Marking untracked files intent-to-add puts them in
    the diff; the reset afterwards leaves the index exactly as it was found, so the
    scope gate and the checkpoint see what they expect.
    """
    # -z, for the same reason parse_porcelain needs it: without it git C-quotes any
    # path that is not plain ASCII, and handing that quoted string back to
    # `git add` fails with a pathspec error that took the whole run down.
    untracked = [f for f in (await _git(
        "ls-files", "--others", "--exclude-standard", "-z", cwd=worktree)).split("\0") if f]
    # quotePath=false so a non-ASCII filename reaches the auditor as itself rather
    # than as "donn\303\251es.csv", which it would have to decode to judge.
    diff = ("-c", "core.quotePath=false", "diff", "HEAD")
    if not untracked:
        return await _git(*diff, cwd=worktree)
    await _git("add", "--intent-to-add", "--", *untracked, cwd=worktree)
    try:
        return await _git(*diff, cwd=worktree)
    finally:
        # --force-remove, not reset. These paths had NO index entry before this
        # ran, and `git reset -- path` restores the entry from HEAD, so for a file
        # that was `git rm --cached`-ed it un-staged the deletion instead of
        # putting the index back as it was found.
        await _git("update-index", "--force-remove", "--", *untracked, cwd=worktree)


@activity.defn
async def audit(run_dir: str, round_result: dict, round_no: int = 1, item_no: int = 1,
                work_item: str = "", item_total: int = 1, kind: str = "brief") -> dict:
    """Audit one round's output. Returns the verdict packet."""
    run = Path(run_dir)
    brief = (run / "brief.md").read_text()
    constraints = (run / "constraints.md").read_text() if (run / "constraints.md").exists() else ""
    worktree = round_result["worktree"]
    # Ahead of the serve, and it has to stay there: this marks the untracked
    # files it has just listed intent-to-add, and a dev server writing into the
    # same worktree between the two turns that into a pathspec error out of the
    # activity.
    diff = (await diff_including_new_files(worktree))[:DIFF_CAP]

    # The same file the round read, read again here: the audit serves the
    # worktree a second time so the pictures and the supervisor's own browsing
    # are of the app as the executor left it.
    cfg = browser.read_browser_config(run_dir)
    config_error = cfg.message if isinstance(cfg, browser.BrowserConfigError) else None
    app, servers = None, None

    # The last two statements before the try that stops it, for
    # `execute_round`'s reason: anything raising in between leaks the process
    # group and the reserved port.
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
            endpoint = browser.browser_endpoint()
            # Asked once, and kept apart from the serve's own `ready` (AC-27).
            # Nothing to attach to when the app never came up.
            attach = None
            if serve.ready:
                attach = await browser.attach_browser(endpoint)
            shots = []
            if serve.ready and attach.ok:
                # No entries is its own state, and the block says so. Asking for
                # zero captures would spend a Chromium connection on nothing.
                if cfg["capture"]:
                    shots_dir = browser.shots_dir(run_dir, item_no, round_no)
                    shots = await browser.capture_all(cfg["capture"], serve.url, shots_dir,
                                                      endpoint, heartbeat=activity.heartbeat)
                output_dir = browser.make_output_dir(run_dir)
                servers = {browser.MCP_SERVER_NAME: browser.mcp_server_entry(
                    output_dir, endpoint, browser.browser_port())}
            evidence = browser.browser_evidence(serve, attach, shots)
        prompt = assemble_audit_prompt(brief, constraints, round_result, diff,
                                       read_answers(run_dir), work_item, item_no, item_total, kind,
                                       browser_evidence=evidence)
        verdict = await run_supervisor(prompt, worktree,
                                       str(run / "logs" / log_name(item_no, round_no, "audit")),
                                       env=app, mcp_servers=servers)
    finally:
        # However the audit ended, including a supervisor call that died: a dev
        # server left holding the port outlives the run that started it.
        if serve is not None:
            await serve.stop()
    verdict["diff_chars"] = len(diff)
    return verdict
