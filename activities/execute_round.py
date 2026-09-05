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

from activities.gate import _run_one, load_gates
from activities.stream import stream_query
from graphs.round_graph import run_round

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"

CORRECTION_PREAMBLE = (
    "\n\n# Correction required\n\n"
    "Your previous attempt left these gates RED. Fix the implementation — "
    "never weaken the gate. Red-gate output follows:\n\n"
)

_OUTPUT_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


# ---------- pure helpers (tested without Claude) ----------

def assemble_prompt(brief: str, constraints: str, work_item: str, directive: str | None = None) -> str:
    contract = (PROMPTS / "executor.md").read_text()
    d = f"\n\n# Supervisor directive (binding — this unit only)\n\n{directive.strip()}\n" if directive else ""
    return (
        f"{contract}\n\n# Feature brief\n\n{brief.strip()}\n\n"
        f"# Constraints (learned — binding)\n\n{constraints.strip() or '(none yet)'}\n\n"
        f"# Work item for this round\n\n{work_item.strip()}\n{d}"
    )


def parse_final_json(text: str) -> dict:
    """Extract the LAST ```json fence from executor output (the output contract)."""
    matches = _OUTPUT_RE.findall(text)
    if not matches:
        raise ValueError("executor produced no final ```json block")
    return json.loads(matches[-1])


def parse_porcelain(porcelain: str) -> list[str]:
    """git status --porcelain → list of paths (handles renames, quotes)."""
    files = []
    for line in porcelain.splitlines():
        if not line:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(path.strip('"'))
    return sorted(files)


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
        return  # retry of a round that already made its worktree
    branches = await _git("branch", "--list", branch, cwd=target_repo)
    if branch in branches:
        await _git("worktree", "add", worktree, branch, cwd=target_repo)
    else:
        await _git("worktree", "add", "-b", branch, worktree, cwd=target_repo)


async def run_executor(prompt: str, feedback: str | None, worktree: str, log_path: str) -> dict:
    """One headless Claude produce (or correct) pass inside the worktree."""
    if os.environ.get("LOOPGRAPH_IN_CONTAINER") != "1":
        raise RuntimeError("refusing to run Claude outside the worker container")
    from claude_agent_sdk import ClaudeAgentOptions

    full = prompt if not feedback else prompt + CORRECTION_PREAMBLE + feedback
    text = await stream_query(full, ClaudeAgentOptions(
        cwd=worktree,
        permission_mode="bypassPermissions",
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
    ), log_path)
    try:
        payload = parse_final_json(text)
    except ValueError:
        payload = {"claims": [], "files_changed": [], "summary": "executor violated the output contract"}
    payload["parse_ok"] = "```json" in text
    return payload


@activity.defn
async def execute_round(run_dir: str, target_repo: str, work_item: str, round_no: int = 1,
                        directive: str | None = None) -> dict:
    run = Path(run_dir)
    brief = (run / "brief.md").read_text()
    constraints = (run / "constraints.md").read_text() if (run / "constraints.md").exists() else ""
    prompt = assemble_prompt(brief, constraints, work_item or brief, directive)

    worktree = str(run / "worktrees" / f"r{round_no}")
    base_branch = (await _git("branch", "--show-current", cwd=target_repo)).strip()
    await ensure_worktree(target_repo, worktree, f"lg-{run.name}-r{round_no}")
    log_path = str(run / "logs" / f"r{round_no}-executor.log")

    gates = load_gates(str(run / "gates.yaml"))

    async def exec_fn(p: str, fb: str | None) -> dict:
        return await run_executor(p, fb, worktree, log_path)

    async def gate_fn() -> list[dict]:
        results = []
        for g in gates:
            activity.heartbeat(f"gate {g['name']}")
            results.append(await _run_one(g, worktree, heartbeat=activity.heartbeat))
        return results

    final = await run_round(prompt, exec_fn, gate_fn)

    diff_stat = await _git("diff", "--stat", "HEAD", cwd=worktree)
    files = parse_porcelain(await _git("status", "--porcelain", cwd=worktree))
    return {
        "status": final["status"],           # green | escalated
        "attempts": final["attempt"],
        "claims": final["result"].get("claims", []),
        "summary": final["result"].get("summary", ""),
        "executor_files": final["result"].get("files_changed", []),
        "files": files,
        "diff_stat": diff_stat.strip(),
        "gate_results": final["gate_results"],
        "worktree": worktree,
        "base_branch": base_branch,
        "branch": f"lg-{run.name}-r{round_no}",
    }
