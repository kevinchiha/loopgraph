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

from activities.discover import (GROUP_LINES_CAP, LINES_SHOWN, STDERR_TAIL, STDOUT_CAP,
                                 discover, group_candidates, group_key, run_detector)

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
    # `|| true` because the detector now runs under pipefail: head closes the
    # pipe, yes dies of SIGPIPE, and the pipeline reports 141. What this pins is
    # the cap, so the exit code is put back to 0 and left out of the way.
    r = asyncio.run(run_detector(_detector("yes | head -c 1500000 || true"), str(tmp_path)))
    assert r["exit_code"] == 0
    assert r["note"] == "stdout cut at 1 MiB"
    assert r["count"] == STDOUT_CAP // 2, "yes prints two bytes a line"


# ---------- the shell the detector runs under ----------


def test_a_pipeline_that_fails_early_fails_the_detector(tmp_path):
    """The same shell as the gate runner, for the same reason: /bin/sh reports a
    pipeline's exit as its last stage's, so a detector whose real work died
    mid-pipe came back exit 0 and the sweep counted its silence as a clean
    tree."""
    r = asyncio.run(run_detector(_detector("false | true"), str(tmp_path)))
    assert r["exit_code"] == 1 and r["note"] == "exit 1"
    assert r["count"] == 0 and r["groups"] == []
    assert r["cmd"] == "false | true", "the wrapper leaked into the reported command"


def test_the_detector_runs_under_bash(tmp_path):
    """A detector written against bash gets bash. The old runner used /bin/sh,
    which is dash in the worker container."""
    r = asyncio.run(run_detector(_detector('echo "$0"'), str(tmp_path)))
    assert r["exit_code"] == 0 and r["count"] == 1
    assert r["lines"] == ["/bin/bash"]


def test_a_grep_that_matches_nothing_fails_the_detector(tmp_path):
    """What pipefail costs a sweep, pinned so nobody meets it as a mystery. grep
    exits 1 when it matches nothing, and pipefail reports that past the sort, so
    a detector over a clean tree now marks the pass incomplete instead of
    reporting zero. The runner masks no exit code: it cannot tell a grep that
    matched nothing from a tool that died with 1. The next test is the fix."""
    r = asyncio.run(run_detector(_detector("grep -r ZZZ . | sort"), str(tmp_path)))
    assert r["exit_code"] == 1 and r["note"] == "exit 1" and r["count"] == 0


def test_the_grep_idiom_keeps_a_clean_tree_at_zero(tmp_path):
    """The idiom the skill teaches, spelled here character for character so the
    two cannot drift. The braces are not style: `||` binds looser than `|`, so
    the braceless form parses as `grep ... || { [ $? = 1 ] | sort; }`, sort
    never sees grep's lines and the command exits 0 whatever grep found. Not
    `|| true`, which also swallows grep's 2 for a path it could not read."""
    r = asyncio.run(run_detector(
        _detector("{ grep -r ZZZ . || [ $? = 1 ]; } | sort"), str(tmp_path)))
    assert r["exit_code"] == 0 and r["note"] == "" and r["count"] == 0


def test_group_key_defaults_to_the_first_segment():
    """The path is read from the front of the line, so a detector that prints
    `path:line: message` groups the same as one that prints bare paths."""
    assert group_key("app/core.py:18: legacy_sum", None) == "app/"
    assert group_key("./src/lib/a.py", None) == "src/"
    assert group_key("setup.py:3: x", None) == "(root)"
    assert group_key("    app/core.py:18: x", None) == "app/", \
        "an indented detector puts its whole list in (root)"
    assert group_key("./", None) == "(root)"


def test_group_key_with_prefixes_is_verbatim():
    """A prefix is compared as text, with no globbing and no tidying: `app`
    matching `apple.py` is the price of `src/lib/` matching that directory alone."""
    assert group_key("src/lib", ["src/lib/"]) == "(other)"
    assert group_key("apple.py", ["app"]) == "app"
    assert group_key("src/lib/a.py", ["src/", "src/lib/"]) == "src/", "the first listed wins"
    assert group_key("docs/x.md", ["app/"]) == "(other)"


def test_groups_cover_every_counted_line_and_sum_to_the_count(tmp_path):
    """Grouping runs over the whole captured stdout, not the 60 lines quoted back:
    a small directory whose candidates all sit past line 60 is still a group, and
    the counts still add up to the number the run converges on."""
    r = asyncio.run(run_detector(_detector(
        r"seq 1 70 | sed 's|^|app/f|;s|$|.py:1: x|'; "
        r"seq 1 5 | sed 's|^|lib/g|;s|$|.py:1: x|'"), str(tmp_path)))
    assert r["count"] == 75 and not any(line.startswith("lib/") for line in r["lines"])
    groups = r["groups"]
    assert [g["name"] for g in groups] == ["app/", "lib/"], "groups are name-sorted"
    assert [g["count"] for g in groups] == [70, 5]
    assert sum(g["count"] for g in groups) == r["count"]
    assert len(groups[0]["lines"]) == LINES_SHOWN
    assert groups[1]["lines"] == [f"lib/g{i}.py:1: x" for i in range(1, 6)]


def test_a_group_past_the_cap_keeps_its_count():
    """Twenty groups of 59 fill 1,180 of the 1,200 sample lines, so the 30-line
    group next by name does not fit and the 5-line group behind it would have.
    The sample stops at the first group that does not fit and stays stopped, so
    which groups carry lines depends on their names and never on their sizes. The
    counts are exact either way."""
    lines = [f"g{i:02d}/f{j}.py" for i in range(20) for j in range(59)]
    lines += [f"x30/f{j}.py" for j in range(30)]
    lines += [f"y05/f{j}.py" for j in range(5)]
    assert 20 * 59 + 30 > GROUP_LINES_CAP > 20 * 59 + 5, "the small group had room to slip in"
    groups = group_candidates(lines, None)

    assert [g["name"] for g in groups] == [f"g{i:02d}/" for i in range(20)] + ["x30/", "y05/"]
    assert [g["count"] for g in groups] == [59] * 20 + [30, 5]
    assert all(len(g["lines"]) == 59 for g in groups[:20])
    assert groups[0]["lines"] == [f"g00/f{j}.py" for j in range(59)]
    assert sum(len(g["lines"]) for g in groups) == 20 * 59
    assert groups[20]["lines"] == [], "the 30-line group did not fit"
    assert groups[21]["lines"] == [], "a smaller group slipped in behind the one that did not fit"


def test_a_failed_or_silent_detector_has_no_groups(tmp_path):
    """Groups are candidates, and a detector that died or said nothing reported
    none. Its count is already 0; its groups must say the same."""
    broken = asyncio.run(run_detector(_detector("echo app/a.py; exit 1"), str(tmp_path)))
    assert broken["count"] == 0 and broken["groups"] == []
    assert broken["lines"] == ["app/a.py"], "the failed detector's output is still kept"

    slow = asyncio.run(run_detector(
        _detector("echo app/a.py; sleep 30", timeout=1), str(tmp_path)))
    assert slow["exit_code"] is None and slow["groups"] == []

    quiet = asyncio.run(run_detector(_detector("true"), str(tmp_path)))
    assert quiet["count"] == 0 and quiet["groups"] == []


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


def test_discover_hands_the_configured_prefixes_to_every_detector(run_dir, repo):
    """The prefixes come from the sweep block the activity already opens, and a
    path no prefix matches goes to (other) rather than being dropped: a typo in
    `groups` would otherwise shrink the run's scope while the count still held it."""
    (run_dir / "run.yaml").write_text(yaml.safe_dump(
        {"sweep": {"yield_floor": 0, "groups": ["app/"],
                   "detectors": [_detector(r"printf 'app/a.py\nlib/b.py\n'"),
                                 _detector(r"printf 'app/c.py\ndocs/d.md\n'", name="e")]}}))
    r = _pass(run_dir, repo)
    first, second = (d["groups"] for d in r["detectors"])
    assert [(g["name"], g["count"]) for g in first] == [("(other)", 1), ("app/", 1)]
    assert first[1]["lines"] == ["app/a.py"]
    assert [(g["name"], g["count"]) for g in second] == [("(other)", 1), ("app/", 1)]
    assert second[1]["lines"] == ["app/c.py"], "the second detector was grouped by top-level dir"


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
