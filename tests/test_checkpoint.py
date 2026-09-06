import asyncio
import inspect
import subprocess

import pytest

from activities.checkpoint import build_commit_message, checkpoint_write_set, parse_numstat

GREEN = [{"name": "g", "cmd": "true", "green_exit": 0, "timeout": 10}]
RED = [{"name": "g", "cmd": "false", "green_exit": 0, "timeout": 10}]


def git(wt, *args):
    subprocess.run(["git", *args], cwd=wt, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"})


@pytest.fixture
def worktree(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "base.py").write_text("x = 1\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    return str(tmp_path)


def run(wt, files, gates=GREEN, max_net=None):
    return asyncio.run(checkpoint_write_set(wt, files, gates, build_commit_message(1, "did things", files),
                                            max_net=max_net))


def test_commits_exact_write_set(worktree):
    with open(f"{worktree}/base.py", "a") as f:
        f.write("y = 2\n")
    with open(f"{worktree}/new.py", "w") as f:
        f.write("z = 3\n")
    r = run(worktree, ["base.py", "new.py"])
    assert r["committed"] and r["commit"]
    out = subprocess.run(["git", "show", "--stat", "--format=", "HEAD"],
                         cwd=worktree, capture_output=True, text=True).stdout
    assert "base.py" in out and "new.py" in out


def test_red_gate_refuses(worktree):
    with open(f"{worktree}/base.py", "a") as f:
        f.write("y = 2\n")
    r = run(worktree, ["base.py"], gates=RED)
    assert not r["committed"] and "gates red" in r["reason"]
    # nothing committed
    assert subprocess.run(["git", "diff", "--name-only", "HEAD"], cwd=worktree,
                          capture_output=True, text=True).stdout.strip() == "base.py"


def test_empty_write_set_refuses(worktree):
    assert not run(worktree, [])["committed"]


def test_declared_but_missing_refuses(worktree):
    r = run(worktree, ["ghost.py"])
    assert not r["committed"] and "ghost.py" in r["reason"]


def test_whitespace_error_refuses(worktree):
    with open(f"{worktree}/base.py", "a") as f:
        f.write("y = 2 \n")  # trailing whitespace
    r = run(worktree, ["base.py"])
    assert not r["committed"] and "--check" in r["reason"]


def test_message_format():
    m = build_commit_message(2, "first line\nsecond", ["a.py"])
    assert m.startswith("loopgraph: item 1 round 2 accept — first line\n")
    assert build_commit_message(1, "x", ["a.py"], item_no=3).startswith(
        "loopgraph: item 3 round 1 accept")
    assert "- a.py" in m


def test_ignored_paths_dropped_not_staged(worktree):
    with open(f"{worktree}/.gitignore", "w") as f:
        f.write("*.log\n")
    with open(f"{worktree}/a.py", "w") as f:
        f.write("a = 1\n")
    with open(f"{worktree}/debug.log", "w") as f:
        f.write("noise\n")
    r = run(worktree, [".gitignore", "a.py", "debug.log"])
    assert r["committed"] and r["dropped_ignored"] == ["debug.log"]
    out = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                         cwd=worktree, capture_output=True, text=True).stdout
    assert "a.py" in out and "debug.log" not in out


def test_write_set_all_ignored_refuses(worktree):
    with open(f"{worktree}/.gitignore", "w") as f:
        f.write("*.log\n")
    with open(f"{worktree}/debug.log", "w") as f:
        f.write("noise\n")
    r = run(worktree, ["debug.log"])
    assert not r["committed"] and "ignored" in r["reason"]


def test_parse_numstat_sums_lines_and_counts_binary_as_zero():
    text = "2\t1\tsrc/a.py\n-\t-\tlogo.png\n0\t3\tb.py\n"
    assert parse_numstat(text) == (2, 4)


def test_parse_numstat_ignores_lines_that_are_not_records():
    # _git merges git's stderr into the text it hands back, so a rename-limit
    # warning arrives between the records. An int() over one would kill the run.
    text = ("1\t0\ta.py\n"
            "warning: exhaustive rename detection was skipped due to too many files.\n"
            "warning: you may want to set your diff.renameLimit variable to at least 6 "
            "and retry the command.\n"
            "\n"
            "3\t2\tb.py\n")
    assert parse_numstat(text) == (4, 2)


def test_a_committed_checkpoint_reports_its_line_counts(worktree):
    with open(f"{worktree}/base.py", "w") as f:
        f.write("y = 2\nz = 3\n")
    r = run(worktree, ["base.py"])
    assert r["committed"] and (r["added"], r["deleted"], r["net"]) == (2, 1, 1)


def test_only_the_staged_diff_is_counted(worktree):
    with open(f"{worktree}/other.py", "w") as f:
        f.write("a = 1\nb = 2\n")
    git(worktree, "add", "other.py")
    git(worktree, "commit", "-qm", "other")
    with open(f"{worktree}/other.py", "w") as f:
        f.write("a = 1\n")
    # A modified tracked file nobody declared: git diff HEAD would count its five
    # lines and refuse the item, the staged diff must not see them at all.
    with open(f"{worktree}/base.py", "a") as f:
        f.write("p = 1\nq = 2\nr = 3\ns = 4\nt = 5\n")
    r = run(worktree, ["other.py"], max_net=0)
    assert r["committed"] and r["net"] == -1
    assert r["leftovers"] == ["base.py"]


def test_the_checkpoint_activity_forwards_max_net(tmp_path, monkeypatch):
    # A wrapper that takes the argument and drops it leaves every cap dead with a
    # green suite, so the forwarding is pinned on its own.
    import activities.checkpoint as cp
    seen = {}

    async def recorder(wt, files, gates, message, max_net=None):
        seen["max_net"] = max_net
        return {"committed": True}

    monkeypatch.setattr(cp, "checkpoint_write_set", recorder)
    # activity.heartbeat raises outside an activity context and the wrapper calls
    # it unguarded, so calling the activity bare would die before it forwards.
    monkeypatch.setattr(cp.activity, "heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(cp, "load_gates", lambda path: [])
    asyncio.run(cp.checkpoint(str(tmp_path), str(tmp_path), ["base.py"], 1, "s", 1, max_net=0))
    assert seen["max_net"] == 0


def test_a_retry_reports_the_numbers_the_first_attempt_did(worktree):
    with open(f"{worktree}/base.py", "w") as f:
        f.write("y = 2\nz = 3\n")
    first = run(worktree, ["base.py"])
    second = run(worktree, ["base.py"])
    assert "already committed" in second["note"]
    counts = ("added", "deleted", "net")
    assert [second[k] for k in counts] == [first[k] for k in counts] == [2, 1, 1]


def test_the_cap_refuses_unstages_and_keeps_the_work(worktree):
    with open(f"{worktree}/base.py", "a") as f:
        f.write("y = 2\nz = 3\n")
    r = run(worktree, ["base.py"], max_net=0)
    assert not r["committed"]
    assert r["reason"] == "net lines +2 exceed the cap of 0"
    assert (r["added"], r["deleted"], r["net"]) == (2, 0, 2)
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=worktree,
                            capture_output=True, text=True).stdout.strip()
    assert staged == ""
    dirty = subprocess.run(["git", "diff", "--name-only"], cwd=worktree,
                           capture_output=True, text=True).stdout.strip()
    assert dirty == "base.py"


def test_a_deletion_passes_the_cap(worktree):
    with open(f"{worktree}/base.py", "w") as f:
        f.write("")
    r = run(worktree, ["base.py"], max_net=0)
    assert r["committed"] and r["net"] == -1


def test_net_equal_to_the_cap_passes(worktree):
    with open(f"{worktree}/base.py", "w") as f:
        f.write("x = 2\n")
    r = run(worktree, ["base.py"], max_net=0)
    assert r["committed"] and (r["added"], r["deleted"], r["net"]) == (1, 1, 0)


def test_a_whitespace_refusal_keeps_its_reason_under_a_cap(worktree):
    with open(f"{worktree}/base.py", "a") as f:
        f.write("y = 2 \n")  # trailing whitespace, and net +1 over the cap
    r = run(worktree, ["base.py"], max_net=0)
    assert not r["committed"] and r["reason"] == "git diff --cached --check failed"


def test_max_net_is_the_last_parameter_of_both_functions():
    from activities.checkpoint import checkpoint
    for fn in (checkpoint_write_set, checkpoint):
        last = list(inspect.signature(fn).parameters.values())[-1]
        assert last.name == "max_net" and last.default is None


def test_merge_branch_without_any_git_identity(tmp_path):
    # merge must carry its own -c identity: containers have no git config
    import activities.checkpoint as cp
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.py").write_text("a = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "checkout", "-qb", "feat")
    (repo / "b.py").write_text("b = 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "feat")
    git(repo, "checkout", "-q", "main")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}  # no GIT_* identity at all
    import subprocess as sp
    sp.run(["git", "config", "--global", "user.email"], env=env, capture_output=True)
    r = asyncio.run(cp.merge_branch(str(repo), "main", "feat"))
    assert r["merged"] and not r["pushed"]
    log = sp.run(["git", "log", "--format=%s", "-1"], cwd=repo, capture_output=True, text=True).stdout
    assert "owner-approved" in log


def test_merge_branch_refuses_dirty_repo(worktree):
    with open(f"{worktree}/loose.py", "w") as f:
        f.write("x\n")
    import activities.checkpoint as cp
    r = asyncio.run(cp.merge_branch(worktree, "main", "whatever"))
    assert not r["merged"] and "uncommitted" in r["reason"]
