import asyncio
import subprocess

import pytest

from activities.checkpoint import build_commit_message, checkpoint_write_set

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


def run(wt, files, gates=GREEN):
    return asyncio.run(checkpoint_write_set(wt, files, gates, build_commit_message(1, "did things", files)))


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
