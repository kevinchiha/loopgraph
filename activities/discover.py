"""The detector pass: run a sweep's detectors and count what they print.

A sweep run has no fixed item list. It takes its work from detectors — shell
one-liners named in `run.yaml` that print one candidate per line — and it ends
when they stop reporting. The number the run converges on comes from here and
nowhere else, which is why a detector that fails contributes nothing rather than
a zero the run would read as progress.

Only stdout is counted. A linter that warns on stderr would otherwise have its
warning turned into a candidate, and the executor would be sent to remove
something that was never on the list. Stderr is still captured, bounded to a
tail, because a broken detector with no diagnostic anywhere in the run leaves the
owner with nothing to fix.

Candidates are grouped by path here rather than in the workflow because a
detector's stdout runs to 1 MiB and everything the workflow is handed lands in
Temporal's history for the life of the run. The activity does the grouping and
returns at most GROUP_LINES_CAP sample lines per detector; the counts are exact
whatever the samples cost.

The runner is `gate.py`'s shape, not its code: `_run_one` merges stderr into
stdout, and a detector's list is read from the front rather than the tail.
"""

from __future__ import annotations

import asyncio
import re

from temporalio import activity

from activities.config import read_run_config
from activities.execute_round import ensure_worktree, reset_to_checkpoint, run_paths
from activities.gate import _drain, _kill_group

STDOUT_CAP = 1_048_576   # bytes of detector stdout kept, from the front
STDERR_TAIL = 2000       # characters of detector stderr kept, from the end
LINES_SHOWN = 60         # sample lines per group, and the detector's own head; trim_pass keeps fewer
GROUP_LINES_CAP = 1200   # sample lines kept per detector across all of its groups

# The path is the front of a candidate line, so it ends at the first `:` or space:
# that covers a bare path and the `path:line: message` a grep-shaped detector prints.
_PATH_END = re.compile(r"[:\s]")


def count_candidates(data: bytes) -> tuple[int, list[str], list[str]]:
    """Detector stdout → (candidates, the first LINES_SHOWN of them, all of them).

    A candidate is a line that is not blank once stripped; the lines themselves
    are handed on exactly as printed, because a detector prints paths and a path
    that has been tidied names no file. The whole list comes back as well because
    the groups are counted over every candidate, not over the sample.
    """
    lines = [line for line in data.decode(errors="replace").splitlines() if line.strip()]
    return len(lines), lines[:LINES_SHOWN], lines


def group_key(line: str, prefixes: list[str] | None) -> str:
    """The name of the group a candidate line belongs to.

    Leading whitespace goes first, or a detector that indents its output would
    put every line under `(root)`. One leading `./` is removed, so a detector
    that prints `./src/a.py` groups with one that prints `src/a.py`.

    Without `prefixes` the group is the first path segment, not a fixed depth of
    two: depth two splits `app/core.py` from `app/text.py` on a small repo and
    does nothing at all on one whose code sits under `src/`. That layout is what
    `prefixes` is for. They are compared verbatim, with no globbing and no
    tidying, so `app` matches `apple.py` and `src/lib/` does not match `src/lib`.
    A path no prefix matches goes to `(other)` rather than being dropped: dropping
    it would let a typo in `groups` quietly shrink the run's scope while the
    detector's count still held those lines.
    """
    path = _PATH_END.split(line.lstrip(), 1)[0]
    if path.startswith("./"):
        path = path[2:]
    if prefixes is not None:
        return next((p for p in prefixes if path.startswith(p)), "(other)")
    head, slash, _ = path.partition("/")
    return head + slash if slash else "(root)"


def group_candidates(lines: list[str], prefixes: list[str] | None) -> list[dict]:
    """Candidate lines → `[{"name", "count", "lines"}]`, sorted by name.

    Given the whole captured stdout, not the LINES_SHOWN quoted back in the entry:
    the group counts have to sum to the detector's count, and a directory whose
    candidates all sit past line 60 has to exist as a group for an item to land on.

    Samples are filled in name order and stop at the first group that would take
    the total past GROUP_LINES_CAP; that group and every group after it keep
    `lines: []` with their count exact. Stopping rather than skipping ahead to a
    smaller group keeps which groups carry a sample a question of their names
    alone. A group with no sample still names its count, and the item text sends
    the executor back to the detector's own command for its lines.
    """
    groups: dict[str, dict] = {}
    for line in lines:
        group = groups.setdefault(group_key(line, prefixes), {"count": 0, "lines": []})
        group["count"] += 1
        if len(group["lines"]) < LINES_SHOWN:
            group["lines"].append(line)
    out, kept, full = [], 0, False
    for name in sorted(groups):
        sample = groups[name]["lines"]
        if full or kept + len(sample) > GROUP_LINES_CAP:
            full, sample = True, []
        else:
            kept += len(sample)
        out.append({"name": name, "count": groups[name]["count"], "lines": sample})
    return out


async def _head(stream, keep: int, buf: bytearray) -> None:
    """Read a detector's stdout into `buf`, keeping only the FIRST `keep` bytes.

    The opposite end from `_drain`, which keeps a gate's tail: a detector's list
    is read from the front. One byte past `keep` is kept as the witness that the
    output was cut, since a detector that printed exactly the cap and one that
    printed ten times it are otherwise the same buffer.

    Reading does not stop at the cap. Stopping there leaves the pipe full, the
    detector blocks on its next write, and a detector that would have exited 0
    is reported as a timeout.

    The buffer belongs to the caller so that cancelling this keeps what it has
    already read, the same reason `_drain` takes one.
    """
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return
        if len(buf) <= keep:
            buf.extend(chunk[:keep + 1 - len(buf)])


async def run_detector(det: dict, cwd: str, heartbeat=None,
                       prefixes: list[str] | None = None) -> dict:
    """One detector, run through the shell in `cwd`, its candidates grouped by path."""
    # bash with pipefail, and the exec form rather than a prepended `set -o
    # pipefail` line, both for the reasons spelled out in gate.py's _run_one. A
    # detector is not the round loop's bound, but two shell runners with
    # different rules is a difference nobody remembers. The cost is that a grep
    # matching nothing now exits 1 and marks the pass incomplete; the fix is
    # `{ grep ... || [ $? = 1 ]; }` and the skill teaches it.
    proc = await asyncio.create_subprocess_exec(
        "/bin/bash", "-o", "pipefail", "-c", det["cmd"],
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        # Its own pipe, drained from the start. A second pipe nobody reads fills
        # and blocks the detector exactly as a full stdout pipe does, and sending
        # stderr to DEVNULL instead left a broken detector with no diagnostic
        # anywhere in the run.
        stderr=asyncio.subprocess.PIPE,
        # Own process group, so a timeout can kill what the detector forked.
        start_new_session=True,
    )
    out, err = bytearray(), bytearray()
    readers = {asyncio.create_task(_head(proc.stdout, STDOUT_CAP, out)),
               asyncio.create_task(_drain(proc.stderr, STDERR_TAIL * 4, err))}
    # The timeout governs the PROCESS, not the pipes, and the poll keeps the
    # activity heartbeating through a long detector — both for the reasons
    # spelled out in gate.py's _run_one.
    waiter = asyncio.create_task(proc.wait())
    step = min(20, det["timeout"])
    elapsed = 0
    while True:
        done, _ = await asyncio.wait({waiter}, timeout=step)
        if done:
            exit_code, note = proc.returncode, ""
            break
        elapsed += step
        if heartbeat:
            heartbeat(f"detector {det['name']} running ({elapsed}s)")
        if elapsed >= det["timeout"]:
            _kill_group(proc)
            exit_code, note = None, f"timeout after {det['timeout']}s"
            break
    # Let the last of the output arrive, then stop waiting. A child that outlived
    # the shell holds both pipes open and EOF never comes; the process is already
    # finished, so whatever it wrote is in the buffers by now. One budget covers
    # both readers, because they hang together.
    _, pending = await asyncio.wait(readers, timeout=2)
    for reader in pending:
        reader.cancel()

    count, lines, candidates = count_candidates(bytes(out[:STDOUT_CAP]))
    if exit_code is None:
        pass  # note already says it timed out
    elif exit_code != 0:
        note = f"exit {exit_code}"
    elif len(out) > STDOUT_CAP:
        note = "stdout cut at 1 MiB"
    if exit_code != 0:
        # A detector that died proves nothing about the yield. What it printed is
        # kept for the owner to read, but the run must not converge on it, and its
        # groups would send items to corners of the repo on the same broken evidence.
        count, candidates = 0, []
    return {
        "name": det["name"],
        "cmd": det["cmd"],
        "exit_code": exit_code,
        "count": count,
        "lines": lines,
        "note": note,
        "stderr_tail": bytes(err).decode(errors="replace")[-STDERR_TAIL:],
        "groups": group_candidates(candidates, prefixes),
    }


@activity.defn
async def discover(run_dir: str, target_repo: str, run_token: str, base_commit: str) -> dict:
    """One pass of the run's detectors over the run's worktree.

    The workflow gives this activity a start_to_close_timeout that covers the
    detectors' own timeouts; nothing here reads a timeout of its own.
    """
    worktree, branch = run_paths(run_dir, run_token)
    hb = activity.heartbeat if activity.in_activity() else None
    # The setup heartbeats: `git worktree add` on a large repo can outlast the
    # three-minute heartbeat window on its own, and a pass killed there is
    # retried straight back into the same slow clone. Nothing else here spoke
    # until the first detector was already running.
    if hb:
        hb("setting up the worktree")
    await ensure_worktree(target_repo, worktree, branch)
    # Detectors measure the committed state. A parked item leaves its rejected
    # changes uncommitted in the shared worktree, and a pass that read those
    # would count junk as yield.
    await reset_to_checkpoint(worktree, base_commit)
    if hb:
        hb("reading the detectors")
    # Read as a plain function: this is already inside an activity, and going
    # through Temporal for a file the worker can open would be a round trip for
    # nothing.
    sweep = read_run_config(run_dir)["sweep"]
    results = []
    for det in sweep["detectors"]:
        if hb:
            hb(f"detector {det['name']}")
        results.append(await run_detector(det, worktree, heartbeat=hb,
                                          prefixes=sweep["groups"]))
    return {
        "complete": all(r["exit_code"] == 0 for r in results),
        "total": sum(r["count"] for r in results if r["exit_code"] == 0),
        "detectors": results,
    }
