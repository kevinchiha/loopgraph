"""Supervisor activity: clean-context audit, read-only tools, verdict packet.

Never sees the executor's transcript — only the brief, constraints, claims,
write set, diff, and gate results. Gate re-runs happen in CODE (checkpoint.py),
never by the model. Read/Glob/Grep only: the auditor cannot modify anything.

Pure parsing helpers are tested without Claude; the SDK call is thin.
"""

from __future__ import annotations

import os
from pathlib import Path

from temporalio import activity

from activities.execute_round import _git, parse_final_json
from activities.stream import log_name, stream_query

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"

DIFF_CAP = 12_000  # chars of unified diff handed to the auditor; keeps prompts bounded
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
    pkt.setdefault("options", {})
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


def assemble_audit_prompt(brief: str, constraints: str, round_result: dict, diff: str) -> str:
    contract = (PROMPTS / "supervisor.md").read_text()
    claims = "\n".join(f"- {flatten_claim(c)}" for c in round_result.get("claims", [])) or "(no claims)"
    files = "\n".join(f"- {f}" for f in round_result.get("files", [])) or "(no files)"
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
    violation = ""
    if round_result.get("self_committed"):
        violation = ("\n\n# Executor red line broken (engine check)\n\n"
                     "This executor committed its own work, which its contract "
                     "forbids. The engine put the changes back in the working tree "
                     "so this round could be judged at all. Treat it as a signal "
                     "about how closely the rest of the contract was followed, and "
                     "check the claims harder than usual.")
    invented, undeclared = declared_vs_actual(round_result)
    mismatch = ""
    if invented or undeclared:
        mismatch = ("\n\n# Write-set mismatch (engine check)\n\n"
                    f"Claimed changed but git did not see: {invented or 'none'}\n"
                    f"Changed but never declared: {undeclared or 'none'}\n"
                    "The executor's contract requires these to agree. Treat a "
                    "mismatch as a finding, not a formatting quirk.")
    truncated = "\n[diff truncated by engine]" if len(diff) >= DIFF_CAP else ""
    return (
        f"{contract}\n\n# Feature brief\n\n{brief.strip()}\n\n"
        f"# Constraints (binding)\n\n{constraints.strip() or '(none)'}\n\n"
        f"# Executor claims\n\n{claims}\n\n"
        f"# Write set (from git status)\n\n{files}{mismatch}{violation}\n\n"
        f"# Gate results\n\n{gates}\n\n"
        f"# Unified diff (capped at {DIFF_CAP} chars)\n\n```diff\n{diff}{truncated}\n```\n\n"
        f"# Worktree (read-only spot checks)\n\n{round_result.get('worktree', '')}\n"
    )


# ---------- container-side effects ----------

async def run_supervisor(prompt: str, worktree: str, log_path: str) -> dict:
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
        disallowed_tools=["Write", "Edit", "MultiEdit", "NotebookEdit", "Bash",
                          "BashOutput", "KillShell", "Task", "WebFetch", "WebSearch"],
        # Load nothing from disk. cwd is the worktree the executor just wrote to,
        # so without this the CLI reads .claude/settings.json and CLAUDE.md from
        # the tree under audit: the audited party could plant a hook that runs
        # shell during its own audit, and instructions telling the auditor to
        # accept. The executor still loads its project's settings; it is the one
        # being supervised, not the supervisor.
        setting_sources=[],
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
        await _git("reset", "-q", "--", *untracked, cwd=worktree)


@activity.defn
async def audit(run_dir: str, round_result: dict, round_no: int = 1, item_no: int = 1) -> dict:
    """Audit one round's output. Returns the verdict packet."""
    run = Path(run_dir)
    brief = (run / "brief.md").read_text()
    constraints = (run / "constraints.md").read_text() if (run / "constraints.md").exists() else ""
    worktree = round_result["worktree"]
    diff = (await diff_including_new_files(worktree))[:DIFF_CAP]
    prompt = assemble_audit_prompt(brief, constraints, round_result, diff)
    verdict = await run_supervisor(prompt, worktree, str(run / "logs" / log_name(item_no, round_no, "audit")))
    verdict["diff_chars"] = len(diff)
    return verdict
