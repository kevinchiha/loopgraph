import asyncio
import inspect
import os
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


def test_the_checkpoint_activity_forwards_max_net_and_the_gate_environment(tmp_path, monkeypatch):
    # A wrapper that takes the argument and drops it leaves every cap dead with a
    # green suite, so the forwarding is pinned on its own. `gate_env` is the same
    # shape of mistake: dropped, the commit-time gates lose the browser endpoint
    # the round's gates had, and only a run with a real web app would ever notice.
    import activities.checkpoint as cp
    seen = {}

    async def recorder(wt, files, gates, message, max_net=None, gate_env=None):
        seen["max_net"] = max_net
        seen["gate_env"] = gate_env
        return {"committed": True}

    monkeypatch.setattr(cp, "checkpoint_write_set", recorder)
    # activity.heartbeat raises outside an activity context and the wrapper calls
    # it unguarded, so calling the activity bare would die before it forwards.
    monkeypatch.setattr(cp.activity, "heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(cp, "load_gates", lambda path: [])
    (tmp_path / "browser.yaml").write_text("serve:\n  cmd: npm run dev\n")
    asyncio.run(cp.checkpoint(str(tmp_path), str(tmp_path), ["base.py"], 1, "s", 1, max_net=0))
    assert seen["max_net"] == 0
    assert seen["gate_env"] == {"LOOPGRAPH_BROWSER_WS": cp.browser.browser_endpoint()}


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


# ---------- the two ways a file can be gone ----------

def _tracked(worktree, name, lines=3):
    """A committed file with `lines` lines in it, for a test that removes it."""
    with open(f"{worktree}/{name}", "w") as f:
        f.write("".join(f"x{i} = {i}\n" for i in range(lines)))
    git(worktree, "add", name)
    git(worktree, "commit", "-qm", f"add {name}")


def _names_and_status(worktree):
    """`git show --name-status HEAD` as (status letter, path) pairs."""
    out = subprocess.run(["git", "show", "--name-status", "--format=", "HEAD"],
                         cwd=worktree, capture_output=True, text=True).stdout
    return sorted(tuple(line.split("\t")) for line in out.splitlines() if line.strip())


def test_a_file_the_executor_removed_with_git_rm_still_commits(worktree):
    """The live bug that killed a sweep run. `git rm` stages the deletion and
    empties the path from both places `git add` looks, so `git add -- helpers.py`
    died on "did not match any files", the checkpoint failed both its attempts,
    and the ActivityError took the whole run with it."""
    _tracked(worktree, "helpers.py")
    git(worktree, "rm", "-q", "helpers.py")
    r = run(worktree, ["helpers.py"])
    assert r["committed"] and (r["added"], r["deleted"], r["net"]) == (0, 3, -3)
    assert r["files"] == ["helpers.py"]
    assert _names_and_status(worktree) == [("D", "helpers.py")]


def test_a_write_set_of_only_removed_files_runs_no_git_add(worktree, monkeypatch):
    """Their removal is already in the index, so there is nothing left to stage,
    and `git add --` with no paths is a hint about `git add .` rather than a
    command."""
    import activities.checkpoint as cp
    real = cp._git
    calls = []

    async def recorder(*args, cwd=None):
        calls.append(args)
        return await real(*args, cwd=cwd)

    monkeypatch.setattr(cp, "_git", recorder)
    _tracked(worktree, "helpers.py")
    git(worktree, "rm", "-q", "helpers.py")
    assert run(worktree, ["helpers.py"])["committed"]
    assert [c for c in calls if c[0] == "add"] == []


def test_a_removed_file_and_a_modified_one_commit_together(worktree):
    """A round that deleted one module and edited another is one write set, and
    the half that still needs staging must still be staged."""
    _tracked(worktree, "helpers.py")
    git(worktree, "rm", "-q", "helpers.py")
    with open(f"{worktree}/base.py", "a") as f:
        f.write("y = 2\n")
    r = run(worktree, ["helpers.py", "base.py"])
    assert r["committed"] and (r["added"], r["deleted"], r["net"]) == (1, 3, -2)
    assert _names_and_status(worktree) == [("D", "helpers.py"), ("M", "base.py")]


def test_a_file_removed_with_plain_rm_still_commits(worktree):
    """The kind of deletion that always worked, pinned so both kinds stay
    covered: the path is gone from the tree but still in the index, so `git add`
    finds it there and stages its removal."""
    _tracked(worktree, "helpers.py")
    os.remove(f"{worktree}/helpers.py")
    r = run(worktree, ["helpers.py"])
    assert r["committed"] and (r["added"], r["deleted"], r["net"]) == (0, 3, -3)
    assert _names_and_status(worktree) == [("D", "helpers.py")]


def test_the_checkpoint_activity_keeps_max_net_on_the_end_of_its_arguments():
    """The workflow passes the activity's arguments positionally and Temporal
    replays a waiting run from recorded history, so an argument that moved would
    replay as a different call. `checkpoint_write_set` is a plain function every
    caller reaches by keyword, so `gate_env` sits after `max_net` there; the
    activity's own list is the one that may never be reordered."""
    from activities.checkpoint import checkpoint
    last = list(inspect.signature(checkpoint).parameters.values())[-1]
    assert last.name == "max_net" and last.default is None
    inner = inspect.signature(checkpoint_write_set).parameters
    assert inner["max_net"].default is None and inner["gate_env"].default is None


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


def test_the_checkpoints_gate_re_run_sees_the_browser_endpoint_and_never_the_app_url(
        worktree, tmp_path_factory):
    """AC-8. The commit-time re-run is the second place the run's gates.yaml
    runs, and no serve is alive here: the round's one was stopped when
    execute_round returned. A gate that reached $LOOPGRAPH_APP_URL would be green
    in the round and red at the commit, and the accepted item would park with its
    work thrown away. So the browser endpoint is handed over and the two app keys
    never are.

    The run directory is its own tmp dir rather than one inside the worktree,
    which git would then report as an untracked leftover of the commit."""
    from temporalio.testing import ActivityEnvironment

    from activities.checkpoint import checkpoint

    run_dir = tmp_path_factory.mktemp("rundir")
    (run_dir / "browser.yaml").write_text("serve:\n  cmd: npm run dev\n")
    (run_dir / "gates.yaml").write_text(
        '- name: env\n'
        '  cmd: test -n "$LOOPGRAPH_BROWSER_WS" && test -z "$LOOPGRAPH_PORT"'
        ' && test -z "$LOOPGRAPH_APP_URL"\n  timeout: 10\n')
    with open(f"{worktree}/base.py", "a") as f:
        f.write("y = 2\n")
    r = asyncio.run(ActivityEnvironment().run(
        checkpoint, str(run_dir), worktree, ["base.py"], 1, "with a web app", 1, None))
    assert r["committed"], r

    # No browser.yaml is a run with no web app, and its gates see none of it.
    (run_dir / "browser.yaml").unlink()
    (run_dir / "gates.yaml").write_text(
        '- name: env\n  cmd: test -z "$LOOPGRAPH_BROWSER_WS"\n  timeout: 10\n')
    with open(f"{worktree}/base.py", "a") as f:
        f.write("z = 3\n")
    r = asyncio.run(ActivityEnvironment().run(
        checkpoint, str(run_dir), worktree, ["base.py"], 2, "with no web app", 1, None))
    assert r["committed"], r
