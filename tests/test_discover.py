"""The detector pass.

Detectors are shell one-liners, so every test here runs a real process: what is
being pinned is how their output is captured, not how a mock behaves.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import subprocess
import time

import pytest
import yaml
from temporalio.testing import ActivityEnvironment

from activities.discover import LINES_SHOWN, STDERR_TAIL, STDOUT_CAP, discover, run_detector

TOKEN = "ab12cd"


def _git(wt, *args):
    subprocess.run(["git", *args], cwd=wt, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "PATH": "/usr/bin:/bin"})


@pytest.fixture
def repo(tmp_path):
    """The target repo a run works on: one commit, one file."""
    target = tmp_path / "target"
    target.mkdir()
    _git(target, "init", "-q", "-b", "main")
    (target / "cli.py").write_text("x = 1\n")
    _git(target, "add", "-A")
    _git(target, "commit", "-qm", "base")
    return target


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "runs" / "sweep-me"
    d.mkdir(parents=True)
    (d / "brief.md").write_text("# Brief\n\nRemove what nothing calls.\n")
    return d


def _head(repo):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()


def _pass(run_dir, repo, detectors=None):
    """One detector pass. `detectors` writes run.yaml first; omit it to re-run
    the detectors already written."""
    if detectors is not None:
        (run_dir / "run.yaml").write_text(
            yaml.safe_dump({"sweep": {"yield_floor": 0, "detectors": detectors}}))
    return asyncio.run(discover(str(run_dir), str(repo), TOKEN, _head(repo)))


def _detector(cmd, name="d", timeout=20):
    return {"name": name, "cmd": cmd, "timeout": timeout}


def test_counts_non_blank_stdout_lines_only(run_dir, repo):
    r = _pass(run_dir, repo, [{"name": "dead", "cmd": r"printf 'a\n\n  \nb\n'"}])
    assert r["complete"] and r["total"] == 2
    d = r["detectors"][0]
    assert d["count"] == 2 and d["lines"] == ["a", "b"]
    assert d["exit_code"] == 0 and d["note"] == "" and d["name"] == "dead"


def test_stderr_is_never_a_candidate(run_dir, repo):
    """A detector that warns on stderr must not have the warning counted as a
    candidate, and the warning must still reach the owner."""
    r = _pass(run_dir, repo, [{"name": "noisy", "cmd": "echo warn >&2; echo x"}])
    d = r["detectors"][0]
    assert r["total"] == 1 and d["count"] == 1 and d["lines"] == ["x"]
    assert d["stderr_tail"] == "warn\n"


def test_a_failing_detector_counts_zero_and_marks_the_pass_incomplete(run_dir, repo):
    r = _pass(run_dir, repo, [{"name": "broken", "cmd": "echo x; exit 3"},
                              {"name": "green", "cmd": r"printf 'a\nb\n'"}])
    broken, green = r["detectors"]
    assert broken["count"] == 0 and broken["exit_code"] == 3
    assert broken["note"] == "exit 3"
    assert green["count"] == 2
    assert r["complete"] is False
    assert r["total"] == 2, "a failed detector's output must not reach the total"


def test_a_failing_detector_keeps_its_lines_and_its_stderr(run_dir, repo):
    """AC-14: the pass entry is where the owner reads why a detector failed, so
    what it printed before it died is kept even though it counts as nothing."""
    r = _pass(run_dir, repo, [{"name": "broken", "cmd": "echo a; echo b; echo boom >&2; exit 3"}])
    d = r["detectors"][0]
    assert d["count"] == 0 and d["lines"] == ["a", "b"]
    assert d["stderr_tail"] == "boom\n"

    # 5000 characters of stderr, of which exactly the last 2000 are kept.
    loud = asyncio.run(run_detector(_detector("yes x | head -c 4996 >&2; echo END >&2"), "/tmp"))
    assert len(loud["stderr_tail"]) == STDERR_TAIL
    assert loud["stderr_tail"].endswith("END\n"), "kept the start of stderr, not the tail"


def test_a_timeout_is_noted_and_the_group_is_killed(tmp_path):
    marker = tmp_path / "child-still-alive"
    started = time.time()
    r = asyncio.run(run_detector(
        _detector(f"(sleep 2; touch {marker}) & sleep 30", timeout=1), str(tmp_path)))
    assert r["note"] == "timeout after 1s" and r["exit_code"] is None and r["count"] == 0
    assert time.time() - started < 10, "it waited for the detector instead of killing it"
    for _ in range(25):
        if marker.exists():
            break
        subprocess.run(["sleep", "0.1"])
    assert not marker.exists(), "the detector's child outlived the detector"


def test_a_child_holding_the_pipe_does_not_hang_the_detector(tmp_path):
    """A backgrounded child inherits stdout, so EOF never comes. Pinned for gates
    by test_review_fixes; a detector reads the same pipes and must not hang
    either, or Temporal kills the activity on both of its attempts."""
    started = time.time()
    r = asyncio.run(run_detector(
        _detector("sh -c 'echo a; echo b; sleep 30 &'"), str(tmp_path)))
    assert time.time() - started < 5, "it waited for an EOF that was never coming"
    assert r["exit_code"] == 0 and r["count"] == 2 and r["lines"] == ["a", "b"]


def test_lines_are_capped_at_sixty_but_the_count_is_not(tmp_path):
    r = asyncio.run(run_detector(_detector("seq 1 100"), str(tmp_path)))
    assert r["count"] == 100
    assert len(r["lines"]) == LINES_SHOWN
    assert r["lines"][0] == "1" and r["lines"][-1] == "60"


def test_stdout_past_the_cap_is_cut_and_said_so(tmp_path):
    """Reading has to continue past the cap. Stopping at 1 MiB leaves the pipe
    full, the detector blocks on write, and a detector that would have exited 0
    is reported as a timeout instead."""
    r = asyncio.run(run_detector(_detector("yes | head -c 1500000"), str(tmp_path)))
    assert r["exit_code"] == 0
    assert r["note"] == "stdout cut at 1 MiB"
    assert r["count"] == STDOUT_CAP // 2, "yes prints two bytes a line"


def test_detectors_run_in_the_worktree_after_a_reset(run_dir, repo):
    """AC-13: a parked item leaves uncommitted changes in the shared worktree,
    and a detector that measured those would count junk as yield."""
    detectors = [{"name": "files", "cmd": "ls"}, {"name": "where", "cmd": "pwd"}]
    _pass(run_dir, repo, detectors)  # the first pass makes the worktree
    worktree = run_dir / "worktrees" / TOKEN
    (worktree / "junk.py").write_text("left behind by a parked item\n")

    r = _pass(run_dir, repo)
    assert r["detectors"][0]["lines"] == ["cli.py"], "the parked item's junk was counted"
    assert r["detectors"][1]["lines"] == [str(worktree)]


def test_discover_heartbeats_before_the_first_detector(run_dir, repo):
    """`git worktree add` on a large repo can outlast Temporal's three-minute
    heartbeat window, and nothing heartbeated between the activity starting and
    the first detector. A pass killed there is retried from the top, into the
    same slow setup again."""
    (run_dir / "run.yaml").write_text(
        yaml.safe_dump({"sweep": {"yield_floor": 0,
                                  "detectors": [_detector("echo x", name="dead")]}}))
    beats: list[str] = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *details: beats.append(details[0])
    asyncio.run(env.run(discover, str(run_dir), str(repo), TOKEN, _head(repo)))
    assert beats[:3] == ["setting up the worktree", "reading the detectors",
                         "detector dead"]


def test_discover_is_registered():
    import worker
    listed = re.search(r"activities=\[(.*?)\]", inspect.getsource(worker.main), re.S)
    names = [n.strip() for n in listed.group(1).replace("\n", " ").split(",")]
    assert "discover" in names, names
