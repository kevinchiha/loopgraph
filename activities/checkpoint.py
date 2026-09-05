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


def build_commit_message(round_no: int, summary: str, files: list[str], item_no: int = 1) -> str:
    subject = summary.strip().splitlines()[0][:72] if summary.strip() else "verified write set"
    body = "\n".join(f"- {f}" for f in files)
    return f"loopgraph: item {item_no} round {round_no} accept — {subject}\n\nFiles:\n{body}\n"


async def _ignored(worktree: str, files: list[str]) -> set[str]:
    """Paths matched by .gitignore never belong in a commit (reproducible artifacts)."""
    try:
        out = await _git("check-ignore", *files, cwd=worktree)
        return set(out.split())
    except RuntimeError:  # exit 1: nothing ignored
        return set()


async def _already_committed(worktree: str, message: str) -> str | None:
    """The sha if HEAD is this exact checkpoint, else None.

    Temporal retries this activity, and it is not idempotent after the commit: on
    a second attempt the tree is clean, so every declared file looks missing and
    the old code reported committed:false. The workflow then parked an item whose
    commit was already on the branch, and the merge card lied about what was in it.
    """
    head_msg = await _git("log", "-1", "--format=%B", cwd=worktree)
    if head_msg.strip() != message.strip():
        return None
    return (await _git("rev-parse", "HEAD", cwd=worktree)).strip()


async def checkpoint_write_set(worktree: str, files: list[str], gates: list[dict], message: str) -> dict:
    if not files:
        return {"committed": False, "reason": "empty write set"}

    done = await _already_committed(worktree, message)
    if done:
        return {"committed": True, "commit": done, "files": files, "leftovers": [],
                "dropped_ignored": [], "note": "already committed by an earlier attempt"}

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

    status_files = parse_porcelain(await _git(
        "status", "--porcelain", "-z", "--untracked-files=all", cwd=worktree))
    missing = [f for f in files if f not in status_files]
    if missing:
        return {"committed": False, "reason": f"declared files not in git status: {missing}"}
    leftovers = [f for f in status_files if f not in files]

    await _git("add", "--", *files, cwd=worktree)
    check = await _run_one({"name": "cached-check", "cmd": "git diff --cached --check",
                            "green_exit": 0, "timeout": 60}, worktree)
    if check["status"] == "red":
        # Unstage before giving up. `git commit` takes the whole index, so one bad
        # file left staged by item 1 made every later item's checkpoint fail on a
        # file that item never touched.
        await _git("reset", "-q", cwd=worktree)
        return {"committed": False, "reason": "git diff --cached --check failed",
                "detail": check["output_tail"]}

    await _git("-c", "user.email=engine@loopgraph.local", "-c", "user.name=loopgraph",
               "commit", "-qm", message, cwd=worktree)
    commit = (await _git("rev-parse", "HEAD", cwd=worktree)).strip()
    return {"committed": True, "commit": commit, "files": files, "leftovers": leftovers,
            "dropped_ignored": sorted(ignored)}


@activity.defn
async def checkpoint(run_dir: str, worktree: str, files: list[str], round_no: int,
                     summary: str, item_no: int = 1) -> dict:
    activity.heartbeat("checkpoint start")
    gates = load_gates(str(Path(run_dir) / "gates.yaml"))
    return await checkpoint_write_set(worktree, files, gates, build_commit_message(round_no, summary, files, item_no))


async def merge_branch(target_repo: str, base_branch: str, branch: str) -> dict:
    """Local merge of an accepted branch into its base. Runs ONLY after an explicit
    owner A-card. Never pushes. Refuses a dirty target repo."""
    if not base_branch:
        return {"merged": False, "reason": "unknown base branch (detached HEAD at round start)"}
    dirty = (await _git("status", "--porcelain", cwd=target_repo)).strip()
    if dirty:
        return {"merged": False, "reason": "target repo has uncommitted changes"}
    was_on = (await _git("branch", "--show-current", cwd=target_repo)).strip()
    await _git("checkout", "-q", base_branch, cwd=target_repo)
    try:
        await _git("-c", "user.email=engine@loopgraph.local", "-c", "user.name=loopgraph",
                   "merge", "--no-ff", "-m", f"loopgraph: merge {branch} (owner-approved)",
                   branch, cwd=target_repo)
    except RuntimeError as e:
        # A conflict used to raise straight out of the activity, leaving the owner's
        # repo checked out on base with conflict markers in their files and
        # MERGE_HEAD set, while the ledger still said merge-ready. Put it back.
        for undo in (("merge", "--abort"), ("checkout", "-q", was_on or base_branch)):
            try:
                await _git(*undo, cwd=target_repo)
            except RuntimeError:
                pass
        return {"merged": False, "base": base_branch,
                "reason": f"merge failed and was rolled back: {str(e)[:300]}",
                "restored_branch": was_on or base_branch}
    commit = (await _git("rev-parse", "HEAD", cwd=target_repo)).strip()
    return {"merged": True, "base": base_branch, "commit": commit, "pushed": False}


@activity.defn
async def merge(target_repo: str, base_branch: str, branch: str) -> dict:
    return await merge_branch(target_repo, base_branch, branch)


@activity.defn
async def discard(target_repo: str, branch: str) -> dict:
    """Delete the run's branch after the owner chooses C.

    "C — discard the run" used to delete nothing, so the label was a lie and the
    commits stayed reachable. Uses -D, not -d: the whole point is that this work
    was refused, so git refusing to drop unmerged commits is not helpful here."""
    try:
        await _git("branch", "-D", branch, cwd=target_repo)
        return {"discarded": True, "branch": branch}
    except RuntimeError as e:
        return {"discarded": False, "branch": branch, "reason": str(e)[:200]}
