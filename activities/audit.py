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
from activities.stream import stream_query

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


def assemble_audit_prompt(brief: str, constraints: str, round_result: dict, diff: str) -> str:
    contract = (PROMPTS / "supervisor.md").read_text()
    claims = "\n".join(f"- {c}" for c in round_result.get("claims", [])) or "(no claims)"
    files = "\n".join(f"- {f}" for f in round_result.get("files", [])) or "(no files)"
    gates = "\n".join(
        f"- {g['name']}: {g['status']} (exit {g['exit_code']})" for g in round_result.get("gate_results", [])
    ) or "(no gates)"
    truncated = "\n[diff truncated by engine]" if len(diff) >= DIFF_CAP else ""
    return (
        f"{contract}\n\n# Feature brief\n\n{brief.strip()}\n\n"
        f"# Constraints (binding)\n\n{constraints.strip() or '(none)'}\n\n"
        f"# Executor claims\n\n{claims}\n\n"
        f"# Write set (from git status)\n\n{files}\n\n"
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
        allowed_tools=["Read", "Glob", "Grep"],  # read-only by construction
    ), log_path)
    return parse_verdict(text)


@activity.defn
async def audit(run_dir: str, round_result: dict, round_no: int = 1) -> dict:
    """Audit one round's output. Returns the verdict packet."""
    run = Path(run_dir)
    brief = (run / "brief.md").read_text()
    constraints = (run / "constraints.md").read_text() if (run / "constraints.md").exists() else ""
    worktree = round_result["worktree"]
    diff = (await _git("diff", "HEAD", cwd=worktree))[:DIFF_CAP]
    prompt = assemble_audit_prompt(brief, constraints, round_result, diff)
    verdict = await run_supervisor(prompt, worktree, str(run / "logs" / f"r{round_no}-audit.log"))
    verdict["diff_chars"] = len(diff)
    return verdict
