"""Taking one run's worktrees out of their repositories, and what must survive it.

A run's worktree is registered inside the owner's real repository under a
container path, `/app/runs/<slug>/worktrees/<token>`, because `git worktree add`
ran inside the container. Nothing on the host sits at that path, so from here
every loopgraph worktree of that repository looks stale — and that is the whole
danger. Measured on git 2.39.5 in a repository holding two runs' worktrees:

    git worktree remove --force <host path>       fatal: ... is not a working tree
                                                  exit 128, every time
    git worktree prune -v                         Removing worktrees/tok2
                                                  Removing worktrees/tok1
                                                  exit 0, no .git/worktrees left

`-c gc.worktreePruneExpire=never` changed nothing — a bare prune never consults
that setting — and after it the surviving run's next git command died with
`fatal: not a git repository`. Removing the one recorded path instead exited 0,
left the other run's record and both branches untouched, and exited 128 the
second time.

So the helper names one recorded path at a time and prunes nothing. Every test
here builds the repository that measurement was taken in: real git, real
registrations, both records rewritten the way a containerised run leaves them.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

import pytest

import ui


def container_path(slug: str, token: str) -> str:
    """What a run registers with the repository: the path it had in the container."""
    return f"/app/runs/{slug}/worktrees/{token}"


def branch_of(slug: str, token: str) -> str:
    """A run's branch, shaped like execute_round.run_paths builds it."""
    return f"lg-{slug}-{token}"


@pytest.fixture
def lg():
    """`lg` has no .py extension, so it loads by path, under this file's own name.

    Under its own module name, and fresh per test: these tests replace `_run` on
    the module object they call into, and two files sharing a loader name would
    hand one module to both.
    """
    root = Path(ui.__file__).resolve().parent
    loader = SourceFileLoader("lg_cli_rm", str(root / "lg"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("lg_cli_rm", loader))
    loader.exec_module(mod)
    return mod


@pytest.fixture
def recorded(lg, monkeypatch):
    """Every argv the helper hands `_run`, with the real command still run.

    The door is watched rather than replaced: the git that runs is the same git
    the assertions read the repository back with afterwards, so a test can say
    both what was asked and what it did.
    """
    calls: list[list[str]] = []
    real = lg._run

    def watched(argv, cwd, **rest):
        calls.append(list(argv))
        return real(argv, cwd, **rest)

    monkeypatch.setattr(lg, "_run", watched)
    return calls


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A projects tree, a runs tree, and the two ways a worktree gets registered.

    `container()` plants what a containerised run leaves behind — the
    repository's `worktrees/<token>/gitdir` record and the worktree's own `.git`
    pointer, both written as container paths — and `host()` leaves
    `git worktree add`'s own output alone, which is what a worktree made on this
    machine looks like. Everything lives under tmp_path and nothing under a home
    directory: tests/test_release.py fails the build on one in a tracked file.

    The git identity, config files and language are set for the whole test rather
    than passed as flags, because the code under test spawns its own git: a
    global hook, an `init.defaultBranch` or a translated error message would
    otherwise decide what these assert.
    """
    for name, value in {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "lg",
        "GIT_AUTHOR_EMAIL": "lg@example.invalid",
        "GIT_COMMITTER_NAME": "lg",
        "GIT_COMMITTER_EMAIL": "lg@example.invalid",
        "LC_ALL": "C",
        # Nothing above tmp_path is ours: without a ceiling, git walks out of the
        # temporary tree and answers about this checkout instead.
        "GIT_CEILING_DIRECTORIES": str(tmp_path),
    }.items():
        monkeypatch.setenv(name, value)

    runs, projects = tmp_path / "runs", tmp_path / "projects"
    projects.mkdir()

    def git(repo, *args) -> str:
        done = subprocess.run(["git", *args], cwd=repo, check=True,
                              capture_output=True, text=True)
        return done.stdout

    def repository(name: str = "proj") -> Path:
        path = projects / name
        path.mkdir()
        git(path, "init", "-q", "-b", "main")
        (path / "README.md").write_text("base\n")
        git(path, "add", "--", "README.md")
        git(path, "commit", "-qm", "base")
        return path

    def host(repo: Path, slug: str, token: str) -> Path:
        worktree = runs / slug / "worktrees" / token
        git(repo, "worktree", "add", "-q", "-b", branch_of(slug, token), str(worktree))
        return worktree

    def record(repo: Path, worktree: Path) -> Path:
        """The repository's own record directory for that worktree.

        Asked for rather than assumed, because git names a record after the last
        component of the worktree path and appends a number when two worktrees
        share one. Two runs of `lg round` really do: RoundRun passes no run token
        and execute_round.run_paths falls back to the literal `run`, so both
        register a directory called `run` and the second record is `run1`.
        """
        for found in sorted((repo / ".git" / "worktrees").iterdir()):
            if (found / "gitdir").read_text().strip() == f"{worktree}/.git":
                return found
        raise AssertionError(f"{repo} has no record for {worktree}")

    def container(repo: Path, slug: str, token: str) -> Path:
        worktree = host(repo, slug, token)
        found = record(repo, worktree)
        (found / "gitdir").write_text(f"{container_path(slug, token)}/.git\n")
        (worktree / ".git").write_text(
            f"gitdir: /projects/{repo.name}/.git/worktrees/{found.name}\n")
        return worktree

    def listed(repo: Path) -> list[str]:
        """The paths that repository has registered, as it recorded them."""
        return [line[len("worktree "):]
                for line in git(repo, "worktree", "list", "--porcelain").splitlines()
                if line.startswith("worktree ")]

    return SimpleNamespace(runs=runs, projects=str(projects), git=git,
                           repository=repository, host=host, container=container,
                           listed=listed)


def clean(lg, world, slug: str) -> list[str]:
    return lg.clean_worktrees(slug, str(world.runs), world.projects)


def word_runs(lg) -> list[tuple[str, ...]]:
    """Every run of literal words in a list, a tuple or a call, anywhere in `lg`.

    The whole module, not one function's own source: an argv is an argv wherever
    it is written. Spelled into `_run` as a list, hoisted to a module constant, or
    handed to a small helper that puts the program name on the front — moving it
    changes nothing these guards exist to catch, and it should not turn them red.
    What it must never do is put a forbidden command out of sight.

    Parsed and not searched, so the rule can be written down beside the code that
    keeps it: a comment is not in the tree at all, and a sentence naming a
    subcommand is one string constant rather than the words inside it.
    """
    found = []
    for node in ast.walk(ast.parse(Path(lg.__file__).read_text())):
        if isinstance(node, (ast.List, ast.Tuple)):
            words = node.elts
        elif isinstance(node, ast.Call):
            words = node.args
        else:
            continue
        run = tuple(word.value for word in words
                    if isinstance(word, ast.Constant) and isinstance(word.value, str))
        if run:
            found.append(run)
    return found


def commands_for(lg, subcommand: str) -> set[tuple[str, str]]:
    """Every `<subcommand> <verb>` pair `lg` spells, wherever the argv lives."""
    return {(word, run[i + 1]) for run in word_runs(lg)
            for i, word in enumerate(run[:-1]) if word == subcommand}


def bare_words(lg) -> set[str]:
    """Every argv-shaped string constant in `lg`: one word, no whitespace.

    This is the net under the two above. A command assembled some other way — the
    subcommand held in a variable, glued on with `+`, chosen by a dictionary —
    still has to spell its word somewhere, and a word on its own is the shape an
    argv element has. Sentences explaining a rule have spaces in them and are one
    constant each, so the explanation never trips the guard.
    """
    return {node.value for node in ast.walk(ast.parse(Path(lg.__file__).read_text()))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and node.value and not any(char.isspace() for char in node.value)}


def test_a_container_registered_worktree_is_deregistered(lg, world):
    """The everyday case, and the one that dies the moment the host path becomes
    the argument: git matches on the path it recorded, and no host path equals a
    container one, so `remove --force <host path>` is a 128 every time."""
    repo = world.repository()
    world.container(repo, "doomed", "tok1")

    assert clean(lg, world, "doomed") == []
    assert not (repo / ".git" / "worktrees" / "tok1").exists(), \
        "the repository still holds a record for the run"
    assert container_path("doomed", "tok1") not in world.listed(repo)


def test_a_second_runs_registration_survives(lg, world):
    """The one this file exists for. Two runs of one repository, both registered
    under container paths, so from the host both look stale — which is exactly
    what makes a prune take the live run along with the dead one. Tidying a
    finished run off the rail while its replacement works the same project is the
    ordinary use of `lg rm`, not an exotic race.
    """
    repo = world.repository()
    world.container(repo, "doomed", "tok1")
    world.container(repo, "live", "tok2")

    assert clean(lg, world, "doomed") == []

    assert not (repo / ".git" / "worktrees" / "tok1").exists(), \
        "the run being removed is still registered"
    kept = repo / ".git" / "worktrees" / "tok2"
    for record in ("gitdir", "commondir", "HEAD"):
        assert (kept / record).exists(), f"the live run lost its {record}"
    assert container_path("live", "tok2") in world.listed(repo), \
        "the repository no longer names the live run's worktree"


def test_two_runs_whose_worktrees_share_a_name_are_told_apart(lg, world):
    """AC-29's catastrophe by the one route left open, and the reason the match on
    the recorded path is exact rather than a suffix.

    Two runs of one repository really can have worktree directories with the same
    name. `lg round` gives every one of them the name `run`: RoundRun starts
    execute_round with no run token (`workflows/run.py`) and `run_paths` falls
    back to the literal `run` (`activities/execute_round.py`). So the repository
    holds `/app/runs/aaa-live/worktrees/run` and
    `/app/runs/zzz-doomed/worktrees/run`, and cleaning the second meets the first
    in the listing before its own.

    Matching on anything less than the whole path — `endswith('/worktrees/run')`
    is the obvious wrong turn — takes the live run's registration, leaves the dead
    one's behind, and hands back an empty report saying all is well. The live run
    then dies with `fatal: not a git repository` at its next command. Every other
    test here uses distinct names and none of them can see it, which is the same
    shape as the two-run case above.
    """
    repo = world.repository()
    world.container(repo, "aaa-live", "run")
    world.container(repo, "zzz-doomed", "run")
    live, doomed = container_path("aaa-live", "run"), container_path("zzz-doomed", "run")
    assert world.listed(repo)[1:] == [live, doomed], \
        "the fixture did not leave the live run ahead of the doomed one in git's own order"

    assert clean(lg, world, "zzz-doomed") == []

    assert live in world.listed(repo), \
        "the live run's registration was taken by the removal of a different run"
    assert doomed not in world.listed(repo), "the run being removed is still registered"
    kept = sorted(entry.name for entry in (repo / ".git" / "worktrees").iterdir())
    assert len(kept) == 1, f"one record should be left, and it should be the live run's: {kept}"


def test_a_host_registered_worktree_is_deregistered(lg, world):
    """A worktree made on this machine records the host path, and that is the
    second of the helper's two exact candidates. Git deletes the directory too in
    this case, which is the same directory `lg rm` would have deleted."""
    repo = world.repository()
    worktree = world.host(repo, "doomed", "tok1")
    assert str(worktree) in world.listed(repo), "the fixture registered no host path"

    assert clean(lg, world, "doomed") == []
    assert str(worktree) not in world.listed(repo)
    assert not (repo / ".git" / "worktrees" / "tok1").exists()


def test_an_unregistered_worktree_is_silent(lg, world, recorded):
    """A worktree the repository has no registration for is already out of it, and
    that is the state `lg rm` is trying to reach. A line crying failure over work
    that is done is the same lie in the other direction."""
    repo = world.repository()
    worktree = world.runs / "doomed" / "worktrees" / "tok1"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: /projects/{repo.name}/.git/worktrees/tok1\n")

    assert clean(lg, world, "doomed") == []
    assert [argv for argv in recorded if "remove" in argv] == [], \
        "a remove ran against a path the repository never recorded"


def test_cleaning_the_same_run_twice_reports_nothing_the_second_time(lg, world):
    """Naming a recorded path a second time exits 128 with `is not a working
    tree`. Asking the repository what it has registered before removing anything
    is the whole reason a repeated `lg rm` stays quiet."""
    repo = world.repository()
    world.container(repo, "doomed", "tok1")

    assert clean(lg, world, "doomed") == []
    assert clean(lg, world, "doomed") == [], "the second run of the same command reported a failure"


def test_an_unresolvable_worktree_is_reported_and_skips_nothing(lg, world):
    """AC-24. The unresolvable worktree sorts first on purpose: a loop that gives
    up at the first failure would leave the second one registered and still hand
    back a report that looks like the truth.

    The reason is taken from the resolver rather than typed again here, so what
    this pins is that it reaches the report whole — not the words the resolver
    happens to use this month. The name in front of it is the part AC-24 asks
    for and is this file's to spell.
    """
    repo = world.repository()
    world.container(repo, "doomed", "bb22")
    lost = world.runs / "doomed" / "worktrees" / "aa11"
    lost.mkdir(parents=True)
    (lost / ".git").write_text("gitdir: /projects/gone/.git/worktrees/aa11\n")

    _, why = ui.resolve_repo(container_path("doomed", "aa11"), world.runs, world.projects)
    assert why, "the fixture's lost worktree resolves after all"

    assert clean(lg, world, "doomed") == [f"aa11: {why}"]
    assert container_path("doomed", "bb22") not in world.listed(repo), \
        "the worktree after the unresolvable one was never cleaned"


def test_two_worktrees_stopped_by_the_same_reason_are_still_told_apart(lg, world):
    """AC-24 reports each worktree it could not clean up BY NAME, and the reason
    on its own cannot do that. A run whose .env has gone missing stops on every
    worktree it has, for the same reason each time, so without the name in front
    the report is the identical sentence twice and names neither of them.

    `projects_dir` is None here, which is what a machine with no .env yet hands
    in — the resolver's answer is one fixed sentence with no path in it at all,
    which is the case no other test can distinguish.
    """
    repo = world.repository()
    world.container(repo, "doomed", "aa11")
    world.container(repo, "doomed", "bb22")

    report = lg.clean_worktrees("doomed", str(world.runs), None)

    _, why = ui.resolve_repo(container_path("doomed", "aa11"), world.runs, None)
    assert why, "a missing projects dir resolves after all"
    assert report == [f"aa11: {why}", f"bb22: {why}"]
    assert len(set(report)) == 2, f"two worktrees, one line each, told apart: {report}"
    # And nothing was touched: a repository that could not be resolved is not a
    # repository anything may be removed from.
    for token in ("aa11", "bb22"):
        assert container_path("doomed", token) in world.listed(repo)


def test_every_report_line_names_the_worktree_it_is_about(lg, world, monkeypatch):
    """AC-24, over all three ways a worktree can fail at once: unresolvable, in a
    repository that will not say what it has, and a remove that came back
    non-zero. One run, three worktrees, three lines, each starting with its own
    name — that is the property, whatever the reasons turn out to say."""
    good, mute = world.repository("good"), Path(world.projects) / "mute"
    mute.mkdir()  # a directory that resolves and is not a git repository
    world.container(good, "doomed", "cc33")
    for token, project in (("aa11", "gone"), ("bb22", "mute")):
        worktree = world.runs / "doomed" / "worktrees" / token
        worktree.mkdir(parents=True)
        (worktree / ".git").write_text(f"gitdir: /projects/{project}/.git/worktrees/{token}\n")
    real = lg._run

    def flaky(argv, cwd, **rest):
        if "remove" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "fatal: nope\n")
        return real(argv, cwd, **rest)

    monkeypatch.setattr(lg, "_run", flaky)

    report = lg.clean_worktrees("doomed", str(world.runs), world.projects)
    assert [line.split(":")[0] for line in report] == ["aa11", "bb22", "cc33"], \
        f"a report line does not start with the worktree it is about: {report}"


def test_no_branch_is_ever_deleted(lg, world, recorded):
    """AC-23. `activities/checkpoint.py`'s `discard` deletes the run's branch and
    is right to — the owner refusing a run is a different act from tidying up
    after one. Nothing of that may be carried out to the host, where the branch
    is all that is left of the work."""
    repo = world.repository()
    world.container(repo, "doomed", "tok1")

    assert clean(lg, world, "doomed") == []
    assert branch_of("doomed", "tok1") in world.git(
        repo, "branch", "--format=%(refname:short)").split(), "the run's branch was deleted"
    assert [argv for argv in recorded if "branch" in argv] == []
    # And nowhere in `lg` is there a second branch command to become one. The one
    # that is there reads the current branch for `lg update`'s refusal; anything
    # else appearing beside it is worth a person looking, delete or not.
    assert commands_for(lg, "branch") == {("branch", "--show-current")}


@pytest.mark.parametrize("case", ["missing", "empty"])
def test_no_worktrees_directory_is_an_empty_report(lg, world, recorded, case):
    """A run that never reached an executor round, or one a `discard` already
    cleaned out. There is nothing to de-register, so no command runs."""
    run = world.runs / "doomed"
    run.mkdir(parents=True)
    if case == "empty":
        (run / "worktrees").mkdir()

    assert clean(lg, world, "doomed") == []
    assert recorded == [], f"a command ran with no worktree to clean: {recorded}"


def test_prune_is_never_run(lg, world, recorded):
    """AC-29, from both sides. A prune from the host removes every loopgraph
    registration in the repository, the live run's included, and
    `-c gc.worktreePruneExpire=never` does not stop it: a bare prune never
    consults that setting, so no expiry can be the thing protecting a neighbour.
    The helper therefore has two git commands and no third."""
    repo = world.repository()
    world.container(repo, "doomed", "tok1")
    world.container(repo, "live", "tok2")

    assert clean(lg, world, "doomed") == []
    ran = [argv for argv in recorded if argv[:1] == ["git"]]
    assert ran, "no git command ran at all, so this test proves nothing"
    assert [argv for argv in ran if "prune" in argv] == [], "the helper pruned the repository"
    # And no prune is spelled anywhere in `lg`, reached this run or not: one
    # behind a condition no test happens to take is the whole danger.
    assert "prune" not in bare_words(lg), "`lg` spells a prune somewhere in it"
    assert commands_for(lg, "worktree") == {("worktree", "list"), ("worktree", "remove")}


def test_a_repository_that_cannot_be_listed_is_reported_and_asked_once(lg, world, recorded):
    """A repository whose path is still on this machine but whose git refuses to
    answer. Nothing can be removed from a repository that will not say what it
    has, so every worktree in it is reported and skipped — and it is asked once
    however many of the run's worktrees are inside it."""
    fake = Path(world.projects) / "proj"
    fake.mkdir()
    for token in ("tok1", "tok2"):
        worktree = world.runs / "doomed" / "worktrees" / token
        worktree.mkdir(parents=True)
        (worktree / ".git").write_text(f"gitdir: /projects/proj/.git/worktrees/{token}\n")

    report = clean(lg, world, "doomed")
    assert [line.split(":")[0] for line in report] == ["tok1", "tok2"], \
        f"the report does not name each worktree it could not clean: {report}"
    assert all(str(fake) in line for line in report), \
        f"the report does not name the repository that refused: {report}"
    assert len([argv for argv in recorded if argv[1:3] == ["worktree", "list"]]) == 1, \
        "the repository was asked once per worktree instead of once"
    assert [argv for argv in recorded if "remove" in argv] == []


def test_a_remove_that_fails_is_one_line_and_the_next_worktree_still_runs(lg, world, monkeypatch):
    """Nothing on this machine makes a remove fail after the listing said the path
    is there — that is a race with another process — so the failure is injected at
    the one door the helper goes through. Which is also what proves `_run`'s own
    124 and 127 reach the report as ordinary lines."""
    repo_a, repo_b = world.repository("a"), world.repository("b")
    world.container(repo_a, "doomed", "aa11")
    world.container(repo_b, "doomed", "bb22")
    real = lg._run

    def flaky(argv, cwd, **rest):
        if "remove" in argv and argv[-1].endswith("aa11"):
            return subprocess.CompletedProcess(argv, 1, "", "fatal: nope\nand more\n")
        return real(argv, cwd, **rest)

    monkeypatch.setattr(lg, "_run", flaky)

    assert clean(lg, world, "doomed") == ["aa11: fatal: nope"]
    assert container_path("doomed", "bb22") not in world.listed(repo_b), \
        "the worktree after the failed one was never cleaned"
