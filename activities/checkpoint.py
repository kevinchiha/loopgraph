"""Checkpoint activity: commit only independently verified, gate-green write sets.

Order is mechanical: re-run the narrow gate → stage ONLY the declared set →
`git diff --cached --check` → commit. Never pushes. Never stages anything the
round didn't declare. A refusal is a result, not an exception.

The logic is a plain async function (tested with real tmp git repos, no
Temporal); the @activity.defn wrapper is thin.
"""

from __future__ import annotations

from pathlib import Path

from temporalio import activity

from activities.execute_round import _git, parse_porcelain
from activities.gate import _run_one, load_gates


def build_commit_message(round_no: int, summary: str, files: list[str]) -> str:
    subject = summary.strip().splitlines()[0][:72] if summary.strip() else "verified write set"
    body = "\n".join(f"- {f}" for f in files)
    return f"loopgraph: round {round_no} accept — {subject}\n\nFiles:\n{body}\n"


async def _ignored(worktree: str, files: list[str]) -> set[str]:
    """Paths matched by .gitignore never belong in a commit (reproducible artifacts)."""
    try:
        out = await _git("check-ignore", *files, cwd=worktree)
        return set(out.split())
    except RuntimeError:  # exit 1: nothing ignored
        return set()


async def checkpoint_write_set(worktree: str, files: list[str], gates: list[dict], message: str) -> dict:
    if not files:
        return {"committed": False, "reason": "empty write set"}

    hb = activity.heartbeat if activity.in_activity() else None
    gate_results = [await _run_one(g, worktree, heartbeat=hb) for g in gates]
    red = [g["name"] for g in gate_results if g["status"] == "red"]
    if red:
        return {"committed": False, "reason": f"gates red at checkpoint: {', '.join(red)}",
                "gate_results": gate_results}

    ignored = await _ignored(worktree, files)
    files = [f for f in files if f not in ignored]
    if not files:
        return {"committed": False, "reason": "write set entirely ignored paths",
                "dropped_ignored": sorted(ignored)}

    status_files = parse_porcelain(await _git("status", "--porcelain", cwd=worktree))
    missing = [f for f in files if f not in status_files]
    if missing:
        return {"committed": False, "reason": f"declared files not in git status: {missing}"}
    leftovers = [f for f in status_files if f not in files]

    await _git("add", "--", *files, cwd=worktree)
    check = await _run_one({"name": "cached-check", "cmd": "git diff --cached --check",
                            "green_exit": 0, "timeout": 60}, worktree)
    if check["status"] == "red":
        return {"committed": False, "reason": "git diff --cached --check failed",
                "detail": check["output_tail"]}

    await _git("-c", "user.email=engine@loopgraph.local", "-c", "user.name=loopgraph",
               "commit", "-qm", message, cwd=worktree)
    commit = (await _git("rev-parse", "HEAD", cwd=worktree)).strip()
    return {"committed": True, "commit": commit, "files": files, "leftovers": leftovers,
            "dropped_ignored": sorted(ignored)}


@activity.defn
async def checkpoint(run_dir: str, worktree: str, files: list[str], round_no: int, summary: str) -> dict:
    activity.heartbeat("checkpoint start")
    gates = load_gates(str(Path(run_dir) / "gates.yaml"))
    return await checkpoint_write_set(worktree, files, gates, build_commit_message(round_no, summary, files))


async def merge_branch(target_repo: str, base_branch: str, branch: str) -> dict:
    """Local merge of an accepted branch into its base. Runs ONLY after an explicit
    owner A-card. Never pushes. Refuses a dirty target repo."""
    if not base_branch:
        return {"merged": False, "reason": "unknown base branch (detached HEAD at round start)"}
    dirty = (await _git("status", "--porcelain", cwd=target_repo)).strip()
    if dirty:
        return {"merged": False, "reason": "target repo has uncommitted changes"}
    await _git("checkout", "-q", base_branch, cwd=target_repo)
    await _git("-c", "user.email=engine@loopgraph.local", "-c", "user.name=loopgraph",
               "merge", "--no-ff", "-m", f"loopgraph: merge {branch} (owner-approved)", branch,
               cwd=target_repo)
    commit = (await _git("rev-parse", "HEAD", cwd=target_repo)).strip()
    return {"merged": True, "base": base_branch, "commit": commit, "pushed": False}


@activity.defn
async def merge(target_repo: str, base_branch: str, branch: str) -> dict:
    return await merge_branch(target_repo, base_branch, branch)
