"""Checkpoint activity: commit only independently verified, gate-green write sets.

Order is mechanical: re-run the narrow gate → stage ONLY the declared set →
`git diff --cached --check` → count the staged lines → commit, unless the
caller's `max_net` says the item grew too much. Never pushes. Never stages
anything the round didn't declare. A refusal is a result, not an exception.

The logic is a plain async function (tested with real tmp git repos, no
Temporal); the @activity.defn wrapper is thin.
"""

from __future__ import annotations

import re
from pathlib import Path

from temporalio import activity

from activities.execute_round import _git, parse_porcelain
from activities.gate import _run_one, load_gates


_NUMSTAT_RE = re.compile(r"^(\d+|-)\t(\d+|-)\t")


def parse_numstat(text: str) -> tuple[int, int]:
    """`git diff --numstat` output → (added, deleted), summed over its records.

    Only lines shaped like a record count. `_git` merges git's stderr into the
    text it returns, so a rename-limit warning ("warning: exhaustive rename
    detection was skipped due to too many files.") lands between the records,
    and an int() over that line would kill the run at the moment of commit, with
    the ledger unwritten. A binary file prints "-" in both columns; it has no
    lines to count, so it counts as none.
    """
    added = deleted = 0
    for line in text.splitlines():
        m = _NUMSTAT_RE.match(line)
        if not m:
            continue
        a, d = m.group(1), m.group(2)
        added += 0 if a == "-" else int(a)
        deleted += 0 if d == "-" else int(d)
    return added, deleted


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
    # Subject line only. Comparing the whole message failed on any repo where git
    # rewrote it — it strips per-line trailing whitespace, and a commit-msg hook
    # appends to it — so the guard never matched and the retry bug came back.
    head_subject = (await _git("log", "-1", "--format=%s", cwd=worktree)).strip()
    if not head_subject or head_subject != message.strip().splitlines()[0].strip():
        return None
    return (await _git("rev-parse", "HEAD", cwd=worktree)).strip()


async def checkpoint_write_set(worktree: str, files: list[str], gates: list[dict], message: str,
                               max_net: int | None = None) -> dict:
    if not files:
        return {"committed": False, "reason": "empty write set"}

    done = await _already_committed(worktree, message)
    if done:
        # Nothing is staged on a retry, so the counts come from the commit itself.
        # Reporting zero here would leave the run's net-lines counter short by
        # one item, and the convergence rule turns on that number.
        added, deleted = parse_numstat(
            await _git("show", "--numstat", "--format=", "HEAD", cwd=worktree))
        return {"committed": True, "commit": done, "files": files, "leftovers": [],
                "dropped_ignored": [], "added": added, "deleted": deleted, "net": added - deleted,
                "note": "already committed by an earlier attempt"}

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

    # A declared file that is on neither the disk nor the index is a deletion the
    # executor staged itself with `git rm`: that empties the path from both the
    # places `git add` looks, so `git add -- dead.py` dies on "did not match any
    # files" — which failed the checkpoint of an accepted round and took the whole
    # run with it. A path that reads as gone is one git has already recorded, so
    # stage only the rest, and stage nothing at all when there is no rest. The
    # cached diff every step below reads already holds those removals.
    indexed = set((await _git("ls-files", "-z", "--", *files, cwd=worktree)).split("\0"))
    # `is_symlink` too: `exists` follows a link, and a dangling one would read
    # as gone when it is a file git still has to stage.
    staging = [f for f in files
               if Path(worktree, f).exists() or Path(worktree, f).is_symlink() or f in indexed]
    if staging:
        await _git("add", "--", *staging, cwd=worktree)
    check = await _run_one({"name": "cached-check", "cmd": "git diff --cached --check",
                            "green_exit": 0, "timeout": 60}, worktree)
    if check["status"] == "red":
        # Unstage before giving up. `git commit` takes the whole index, so one bad
        # file left staged by item 1 made every later item's checkpoint fail on a
        # file that item never touched.
        await _git("reset", "-q", cwd=worktree)
        return {"committed": False, "reason": "git diff --cached --check failed",
                "detail": check["output_tail"]}

    # The staged diff against HEAD is exactly this item's change, and it is already
    # here at the moment of commit. Counted after --check has passed, so a
    # whitespace refusal keeps its own reason and never reads as a cap refusal.
    added, deleted = parse_numstat(await _git("diff", "--numstat", "--cached", cwd=worktree))
    net = added - deleted
    if max_net is not None and net > max_net:
        await _git("reset", "-q", cwd=worktree)  # same reason the whitespace path unstages
        return {"committed": False, "reason": f"net lines +{net} exceed the cap of {max_net}",
                "added": added, "deleted": deleted, "net": net}

    await _git("-c", "user.email=engine@loopgraph.local", "-c", "user.name=loopgraph",
               "commit", "-qm", message, cwd=worktree)
    commit = (await _git("rev-parse", "HEAD", cwd=worktree)).strip()
    return {"committed": True, "commit": commit, "files": files, "leftovers": leftovers,
            "dropped_ignored": sorted(ignored), "added": added, "deleted": deleted, "net": net}


@activity.defn
async def checkpoint(run_dir: str, worktree: str, files: list[str], round_no: int,
                     summary: str, item_no: int = 1, max_net: int | None = None) -> dict:
    activity.heartbeat("checkpoint start")
    gates = load_gates(str(Path(run_dir) / "gates.yaml"))
    return await checkpoint_write_set(worktree, files, gates,
                                      build_commit_message(round_no, summary, files, item_no),
                                      max_net=max_net)


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
async def discard(target_repo: str, branch: str, worktree: str = "") -> dict:
    """Delete the run's branch and its worktree after the owner chooses C.

    "C — discard the run" used to delete nothing, so the label was a lie and the
    commits stayed reachable. The worktree has to go first: git refuses to delete
    a branch that is checked out anywhere, so deleting the branch alone failed
    with "used by worktree at ..." and the run stayed on disk regardless.

    Uses -D, not -d. The whole point is that this work was refused, so git
    declining to drop unmerged commits is not helpful here."""
    removed = None
    if worktree:
        try:
            await _git("worktree", "remove", "--force", worktree, cwd=target_repo)
            removed = worktree
        except RuntimeError:
            pass  # already gone, or never created
    try:
        await _git("worktree", "prune", cwd=target_repo)
    except RuntimeError:
        pass
    try:
        await _git("branch", "-D", branch, cwd=target_repo)
        return {"discarded": True, "branch": branch, "worktree_removed": removed}
    except RuntimeError as e:
        return {"discarded": False, "branch": branch, "reason": str(e)[:300]}
