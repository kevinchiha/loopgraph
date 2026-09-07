"""`lg rm`: what it refuses, what it asks, and what it is allowed to delete.

Two halves. The first is `clean_worktrees`, which takes one run's worktrees out
of their repositories and deletes nothing. The second is the command itself,
which refuses, prompts, deletes and archives — the one thing in this repository
with no undo behind it, so the tests below assert on the tree that is left and
not only on an exit code.

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
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
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


# ---------- the command: what it may be pointed at ----------

CLOSED = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)

# The visibility query every run of the engine is listed by, and the one string
# that must reach Temporal. A query naming any other workflow type matches
# nothing on a real server, so `lg rm` would find no workflow for a run that has
# one — and a run with no workflow is deliberately not a refusal, so an open run
# would be deleted, its worktrees de-registered and `archived 0` reported over
# the top of it. tests/test_lg_status.py pins the same string for `lg status`.
LOOPGRAPH_RUNS = 'WorkflowType = "LoopGraphRun"'


class Workflows:
    """A Temporal holding LoopGraphRun rows, and what each answers to `ledger`.

    A row is `(id, close_time)`, and `None` for the close time is the only thing
    that makes a workflow open — never the ledger, whose `status` reads `running`
    for ever on a run whose engine died. `ledgers` maps an id to the ledger it
    answers with, or to the exception the query raises.

    The listing honours its query the way a server does: ask for a workflow type
    nothing here is, and nothing comes back. Recording the query is not enough on
    its own — every assertion about a refusal would still pass while the command
    quietly saw an empty machine.
    """

    def __init__(self, rows=(), ledgers=None, stall=0):
        self.rows = list(rows)
        self.ledgers = dict(ledgers or {})
        self.stall = stall
        self.listed: list[str] = []
        self.queried: list[tuple[str, str]] = []

    def list_workflows(self, query):
        self.listed.append(query)
        feed = self

        async def rows():
            # A server that accepted the connection and then said nothing. The
            # wait goes here rather than in the connect because that is where a
            # real one goes: nothing is sent until the listing is iterated.
            if feed.stall:
                await asyncio.sleep(feed.stall)
            for wf_id, close_time in (feed.rows if query == LOOPGRAPH_RUNS else []):
                yield SimpleNamespace(id=wf_id, close_time=close_time)
        return rows()

    def get_workflow_handle(self, wf_id):
        feed = self

        class Handle:
            async def query(self, name):
                feed.queried.append((wf_id, name))
                answer = feed.ledgers.get(wf_id, {})
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return Handle()


@pytest.fixture
def rm(lg, world, tmp_path, monkeypatch):
    """`lg rm ...` end to end — argparse, ROOT and .env all under tmp_path.

    ROOT is the temporary tree, so the command's `runs/` is world's runs
    directory and the .env it reads is the one written here. Nothing points at
    the real checkout, because this is the command that deletes things.

    `typed` is what the operator types at the prompt, one string per read, and
    an exception in the list is raised instead. Leaving it out means the command
    must not prompt at all: a stray read fails the test rather than hanging it.
    """
    (tmp_path / ".env").write_text(f"LOOPGRAPH_PROJECTS_DIR={world.projects}\n")
    monkeypatch.setattr(lg, "ROOT", str(tmp_path))
    prompts: list[str] = []

    def go(*argv, client=None, typed=None) -> int:
        async def connect():
            if isinstance(client, Exception):
                raise client
            return Workflows() if client is None else client
        monkeypatch.setattr(lg, "_client", connect)
        answers = [] if typed is None else list(typed)

        def read(prompt=""):
            sys.stdout.write(prompt)
            prompts.append(prompt)
            if typed is None:
                raise AssertionError(f"the command prompted with nothing to answer: {prompt!r}")
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        monkeypatch.setattr("builtins.input", read)
        monkeypatch.setattr(sys, "argv", ["lg", "rm", *argv])
        return lg.main()

    return SimpleNamespace(go=go, prompts=prompts, runs=world.runs, root=tmp_path,
                           address=lg.ADDRESS)


def everything_under(root: Path) -> set[str]:
    """Every path in a tree, relative to it. What must not have moved."""
    return {str(p.relative_to(root)) for p in root.rglob("*")}


@pytest.mark.parametrize("flag", [[], ["--yes"]], ids=["asks", "--yes"])
@pytest.mark.parametrize("arg", [".", "./", "..", "runs/..", "", "a/b"])
def test_a_slug_that_escapes_runs_is_refused(rm, capsys, arg, flag):
    """AC-28, and the only bug in this phase that could take the repository with
    it. `run_slug` validates nothing — it splits on `/`, drops the empties and
    hands back the last part — so `.`, `..` and `` all come through it unchanged,
    and every later stop lets them past: all three directories exist, `run-.-`
    matches no workflow and a run with no workflow is not a refusal, and the
    prompt asks the operator to retype the same `.` they just typed, or to press
    Enter for the empty one.

    So `lg rm .` and `lg rm ./` would rmtree every run directory, `lg rm ..`
    would take the checkout — source, `.git` and all — and `lg rm --yes "$SLUG"`
    with `SLUG` unset would empty `runs/` and exit 0. `--yes` is parametrised for
    exactly that: the flag skips the prompt and nothing else.

    The assertion is on the tree and not only on the exit code, because the
    version of this bug that matters deletes the files and *then* raises. And
    Temporal is never asked: the refusal comes before the listing, so a machine
    with the stack down still refuses rather than refusing for the wrong reason.
    """
    feed = Workflows([("run-keep-ab12cd", CLOSED)])
    (rm.runs / "keep" / "logs").mkdir(parents=True)
    (rm.runs / "other" / "logs").mkdir(parents=True)
    (rm.runs / ".archived.json").write_text('["run-gone-ff00aa"]')
    (rm.root / "beside.txt").write_text("not a run\n")
    before = everything_under(rm.root)

    code = rm.go(arg, *flag, client=feed)

    out, err = capsys.readouterr()
    assert (code, out) == (1, "")
    assert err == f"not a run name: {arg}\n"
    assert everything_under(rm.root) == before, "a refused argument still changed the tree"
    assert feed.listed == [], "the refusal asked Temporal before deciding"


def test_the_slug_passes_the_same_check_a_value_off_the_wire_passes(rm, capsys):
    """AC-28 names `bad_param` and not a check of its own, and this is the case
    only `bad_param` catches: a slug with `..` inside it that is neither `..` nor
    an escape. `weird..name` resolves to a perfectly ordinary child of `runs/`,
    so the shape check and the resolve check both wave it through.

    One guard, one case that needs it. Without this the whole of `bad_param` can
    be deleted from the command and every other test here still passes, which is
    the shape a refusal that has quietly stopped running takes.
    """
    (rm.runs / "weird..name" / "logs").mkdir(parents=True)
    before = everything_under(rm.root)

    assert rm.go("weird..name", "--yes") == 1
    assert capsys.readouterr().err == "not a run name: weird..name\n"
    assert everything_under(rm.root) == before


def test_a_run_directory_that_is_a_symlink_out_of_runs_is_refused(rm, world, capsys):
    """The case the resolve is there for, and the one that reads as a plain run
    name all the way up to the rmtree. `runs/evil` is a perfectly good child of
    `runs/` by its name; it is only a child by its path once the link is
    followed, which is what resolve() does and what nothing before it does."""
    (rm.runs / "keep" / "logs").mkdir(parents=True)
    (rm.runs / "evil").symlink_to(world.projects)
    before = everything_under(Path(world.projects))

    assert rm.go("evil", "--yes") == 1
    assert capsys.readouterr().err == "not a run name: evil\n"
    assert everything_under(Path(world.projects)) == before
    assert (rm.runs / "evil").is_symlink(), "the link itself was removed"


def test_a_missing_run_directory_is_one_line_and_no_lookup(rm, capsys):
    """Archiving a row whose directory was never there is the dashboard's job,
    not this command's, and it is a typo far more often than anything else. The
    listing is not worth a round trip to Temporal to say so."""
    (rm.runs / "keep" / "logs").mkdir(parents=True)
    feed = Workflows([("run-typo-ab12cd", CLOSED)])

    assert rm.go("runs/typo", "--yes", client=feed) == 1
    out, err = capsys.readouterr()
    assert (out, err) == ("", "no run directory runs/typo\n")
    assert feed.listed == [], "a missing directory still asked Temporal"
    assert (rm.runs / "keep").is_dir()


# ---------- the command: what it refuses ----------

def test_rm_refuses_while_a_workflow_is_open_and_names_the_condition(rm, world, capsys):
    """AC-21. Open is Temporal's close time and nothing else, and the line says
    which of the two conditions stopped it, because they need different things
    from the owner: one wants an answer, the other wants waiting.

    Both open workflows are named, not just the first: a run directory can hold
    two, and stopping at the first would leave the owner answering one and
    meeting the other on the next attempt.
    """
    repo = world.repository()
    world.container(repo, "doomed", "tok1")
    (rm.runs / "doomed" / "logs").mkdir(parents=True)
    feed = Workflows(
        [("run-doomed-aa11aa", None), ("run-doomed-bb22bb", None),
         ("run-doomed-cc33cc", CLOSED)],
        {"run-doomed-aa11aa": {"status": "running",
                               "awaiting": {"kind": "decision", "question": "which port?"}},
         "run-doomed-bb22bb": {"status": "running"}})

    assert rm.go("doomed", "--yes", client=feed) == 1

    out, err = capsys.readouterr()
    assert err.splitlines() == [
        "refuse: run-doomed-aa11aa is waiting on you — answer it: "
        "lg approve run-doomed-aa11aa <answer>",
        "refuse: run-doomed-bb22bb is still running",
    ]
    assert out == "", "a refusal printed a deletion plan"
    assert (rm.runs / "doomed" / "logs").is_dir(), "the run directory was deleted anyway"
    assert container_path("doomed", "tok1") in world.listed(repo), \
        "the worktree was de-registered by a command that refused"


def test_a_closed_workflow_whose_ledger_still_says_running_is_removed(rm, capsys):
    """AC-21's other half, and the runs `lg rm` exists for. An activity that
    escapes `run` leaves the workflow closed with its ledger frozen at the
    failure — `status: running`, `awaiting` still set — for ever. Read off the
    ledger, that run can never be removed; read off the close time, it goes.
    """
    (rm.runs / "dead" / "logs").mkdir(parents=True)
    feed = Workflows([("run-dead-aa11aa", CLOSED)],
                     {"run-dead-aa11aa": {"status": "running",
                                          "awaiting": {"kind": "decision"}}})

    assert rm.go("dead", "--yes", client=feed) == 0

    assert capsys.readouterr().err == ""
    assert not (rm.runs / "dead").exists()
    assert json.loads((rm.runs / ".archived.json").read_text()) == ["run-dead-aa11aa"]
    assert feed.queried == [], "a closed workflow's ledger was queried to decide this"


def test_an_open_workflow_whose_ledger_cannot_be_read_is_still_running(rm, capsys):
    """A query replays the whole history against the workflow code registered
    now, so one written by older code fails with a nondeterminism error — 7 of
    the 15 runs on the owner's machine. The workflow is open either way, and a
    line that cannot say which condition still has to say it is open."""
    (rm.runs / "doomed" / "logs").mkdir(parents=True)
    feed = Workflows([("run-doomed-aa11aa", None)],
                     {"run-doomed-aa11aa": RuntimeError("[TMPRL1100] Nondeterminism error")})

    assert rm.go("doomed", "--yes", client=feed) == 1
    assert capsys.readouterr().err == "refuse: run-doomed-aa11aa is still running\n"
    assert (rm.runs / "doomed").is_dir()


def test_rm_refuses_when_temporal_cannot_be_asked(rm, capsys):
    """AC-21. A stack that is down cannot say whether this run is mid-round, and
    a run mid-round has a worktree the executor is writing into. Guessing is the
    one thing a command with no undo may not do."""
    (rm.runs / "doomed" / "logs").mkdir(parents=True)

    assert rm.go("doomed", "--yes", client=RuntimeError("connection refused")) == 1

    out, err = capsys.readouterr()
    assert (out, err) == ("", f"cannot reach Temporal at {rm.address}; "
                              "lg rm cannot prove the run is not mid-round\n")
    assert (rm.runs / "doomed" / "logs").is_dir()


def test_a_temporal_that_never_answers_is_a_refusal_and_not_a_hang(rm, lg, monkeypatch, capsys):
    """The deadline covers the listing and not just the connect. `list_workflows`
    sends no request until it is iterated, so a client that connected proves
    nothing about the server behind it — the same reason `lg update` wraps
    `_open_runs` whole rather than wrapping the connect."""
    monkeypatch.setattr(lg, "TEMPORAL_TIMEOUT", 0.05)
    (rm.runs / "doomed" / "logs").mkdir(parents=True)

    assert rm.go("doomed", "--yes", client=Workflows(stall=5)) == 1
    assert capsys.readouterr().err.startswith("cannot reach Temporal at ")
    assert (rm.runs / "doomed" / "logs").is_dir()


def test_another_runs_workflows_are_neither_archived_nor_a_refusal(rm, capsys):
    """The id shape is `run-<slug>-` plus a token holding no `-`, which is what
    `cmd_start` builds and `resolve_run_arg` inverts. Without the no-dash rule
    `lg rm foo` claims every run whose own slug starts with `foo-`, and both
    halves of that are wrong at once: it refuses because a different, live run is
    open, and if that run were closed it would archive its row while deleting
    nothing of it — a row off the rail with its directory still there."""
    (rm.runs / "foo" / "logs").mkdir(parents=True)
    (rm.runs / "foo-bar" / "logs").mkdir(parents=True)
    feed = Workflows([("run-foo-aa11aa", CLOSED), ("run-foo-bar-bb22bb", None)])

    assert rm.go("foo", "--yes", client=feed) == 0

    assert capsys.readouterr().err == ""
    assert json.loads((rm.runs / ".archived.json").read_text()) == ["run-foo-aa11aa"]
    assert (rm.runs / "foo-bar" / "logs").is_dir(), "the neighbouring run was deleted"


# ---------- the command: the prompt ----------

def test_the_prompt_wants_the_slug_back(rm, world, capsys):
    """AC-22. `y` is what a person types without reading, and this command has no
    undo, so what it asks for is the name of the thing it is about to delete.
    Anything else stops it, and it stops before the first change: the worktree is
    still registered and the directory is still there afterwards."""
    repo = world.repository()
    world.container(repo, "doomed", "tok1")
    (rm.runs / "doomed" / "logs").mkdir(parents=True)
    feed = Workflows([("run-doomed-aa11aa", CLOSED)])

    assert rm.go("doomed", client=feed, typed=["y"]) == 1
    err = capsys.readouterr().err
    assert err == "stopped; nothing was touched\n"
    assert rm.prompts == ["type doomed to delete, or anything else to stop: "]
    assert (rm.runs / "doomed" / "logs").is_dir()
    assert container_path("doomed", "tok1") in world.listed(repo)
    assert not (rm.runs / ".archived.json").exists()

    assert rm.go("doomed", client=feed, typed=["doomed"]) == 0
    assert not (rm.runs / "doomed").exists()
    assert container_path("doomed", "tok1") not in world.listed(repo)


def test_yes_skips_the_prompt_and_nothing_else(rm, capsys):
    """--yes is for a script that has already decided. It answers the question
    and takes no refusal with it: the fixture fails the test if the command reads
    from a stdin nothing is holding."""
    (rm.runs / "doomed" / "logs").mkdir(parents=True)

    assert rm.go("doomed", "--yes", client=Workflows([("run-doomed-aa11aa", CLOSED)])) == 0
    assert rm.prompts == []
    assert not (rm.runs / "doomed").exists()


@pytest.mark.parametrize("raised", [
    EOFError(),
    RuntimeError("input(): lost sys.stdin"),
    ValueError("I/O operation on closed file"),
], ids=["a pipe", "fd 0 closed", "stdin closed under us"])
def test_a_closed_stdin_stops_instead_of_raising(rm, world, capsys, raised):
    """AC-22, and all three ways a prompt can fail to be read at all. `main()`
    has no top-level handler, so any of them reaches the terminal as a traceback
    — which is not an answer to a plain question, and every other refusal in `lg`
    is one line and a 1.

    The three are not interchangeable and only one of them is EOFError. A pipe or
    `< /dev/null` gives EOFError, which is the case everyone writes the test for.
    `lg rm doomed 0<&-` — the shell closing the descriptor, which is what AC-22
    means by stdin closed — leaves `sys.stdin` as None and `input()` raises
    RuntimeError instead; measured against the real command. A stdin closed under
    a running process raises ValueError. Catching EOFError alone passes the first
    of these and gives a traceback on the other two.
    """
    repo = world.repository()
    world.container(repo, "doomed", "tok1")
    (rm.runs / "doomed" / "logs").mkdir(parents=True)

    code = rm.go("doomed", client=Workflows([("run-doomed-aa11aa", CLOSED)]),
                 typed=[raised])

    assert code == 1
    assert capsys.readouterr().err == "stopped; nothing was touched\n"
    assert (rm.runs / "doomed" / "logs").is_dir()
    assert container_path("doomed", "tok1") in world.listed(repo)


def test_it_prints_what_it_is_about_to_delete(rm, world, capsys):
    """AC-22's first half. The operator is being asked to confirm, and a question
    with nothing above it is a question about nothing: the directory, every
    worktree under it and every row that will leave the rail, before the prompt
    that asks about them."""
    repo = world.repository()
    world.container(repo, "doomed", "tok1")
    world.container(repo, "doomed", "tok2")
    (rm.runs / "doomed" / "logs").mkdir(parents=True)
    feed = Workflows([("run-doomed-aa11aa", CLOSED), ("run-doomed-bb22bb", CLOSED)])

    assert rm.go("doomed", client=feed, typed=["doomed"]) == 0

    # The prompt is written without a newline after it, so the summary line
    # afterwards shares its line. The plan is the part above it.
    out = capsys.readouterr().out.splitlines()
    assert out[:5] == [
        f"about to delete {rm.runs / 'doomed'}",
        f"  worktree {rm.runs / 'doomed' / 'worktrees' / 'tok1'}",
        f"  worktree {rm.runs / 'doomed' / 'worktrees' / 'tok2'}",
        "  archive run-doomed-aa11aa",
        "  archive run-doomed-bb22bb",
    ]
    assert out[5].startswith("type doomed to delete, or anything else to stop: ")


# ---------- the command: what it deletes, and in what order ----------

def test_a_removal_de_registers_then_deletes_then_archives(rm, world, capsys, monkeypatch):
    """The whole successful path, and the order it has to happen in.

    The worktrees go first because `clean_worktrees` finds them by reading
    `runs/<slug>/worktrees/`: after the rmtree there is nothing left to read and
    the registration would stay in the owner's repository for ever, unreported.
    The archive write goes last because the two halves have to work together —
    a row taken off the rail by a delete that then failed is a run the owner can
    no longer see and has not lost.
    """
    repo = world.repository()
    world.container(repo, "doomed", "tok1")
    (rm.runs / "doomed" / "logs").mkdir(parents=True)
    still_there = []
    real = ui.mark_archived

    def watched(runs, ids, archived):
        still_there.append((rm.runs / "doomed").exists())
        return real(runs, ids, archived)

    monkeypatch.setattr(ui, "mark_archived", watched)
    feed = Workflows([("run-doomed-aa11aa", CLOSED)])

    assert rm.go("runs/doomed/", "--yes", client=feed) == 0

    # The one string that reaches Temporal, pinned here the way
    # tests/test_lg_status.py pins it for `lg status`. Nothing else in this file
    # would notice it change: a query naming a workflow type nothing is matches
    # nothing, and a run with no workflow is deliberately not a refusal — so a
    # typo here deletes an open run and reports `archived 0 workflow row(s)`.
    assert feed.listed == [LOOPGRAPH_RUNS]
    out, err = capsys.readouterr()
    assert out.splitlines()[-1] == "removed runs/doomed; archived 1 workflow row(s)"
    assert err == ""
    assert container_path("doomed", "tok1") not in world.listed(repo), \
        "the worktree was still registered after the directory went"
    assert not (rm.runs / "doomed").exists()
    assert json.loads((rm.runs / ".archived.json").read_text()) == ["run-doomed-aa11aa"]
    assert still_there == [False], "the archive was written while the directory was still there"


def test_rm_archives_both_workflows_of_a_shared_directory(rm, capsys):
    """AC-20. Two workflows really do share one run directory — a restart, or a
    second `lg start` on the same brief — and the dashboard keys its rows on the
    workflow id. Archiving one leaves the other on the rail, pointing at logs,
    a diff and a ledger that are all gone.

    The id already in the file stays: `mark_archived` re-reads and merges, and a
    write that replaced the list would take every other run the owner had hidden
    off with it."""
    (rm.runs / "doomed" / "logs").mkdir(parents=True)
    (rm.runs / ".archived.json").write_text('["run-older-ff00aa"]')
    feed = Workflows([("run-doomed-aa11aa", CLOSED), ("run-other-zz99zz", CLOSED),
                      ("run-doomed-bb22bb", CLOSED)])

    assert rm.go("doomed", "--yes", client=feed) == 0

    assert capsys.readouterr().out.splitlines()[-1] == \
        "removed runs/doomed; archived 2 workflow row(s)"
    assert json.loads((rm.runs / ".archived.json").read_text()) == [
        "run-doomed-aa11aa", "run-doomed-bb22bb", "run-older-ff00aa"]
    assert not (rm.runs / "doomed").exists()


@pytest.mark.parametrize("before", [None, '[\n  "run-older-ff00aa"\n]\n'],
                         ids=["no file", "pretty-printed"])
def test_rm_deletes_a_logs_only_run_with_nothing_to_archive(rm, capsys, before):
    """A run driven by `lg round`, or one whose workflow history has aged out:
    the directory is real and Temporal has nothing keyed to it. There is nothing
    to take off the rail, so the archive file is not written at all — writing it
    with an empty list would create one on a fresh checkout and reformat the
    owner's own file for nothing.

    Temporal answering with no matching workflow is not a refusal either. It is
    the state a logs-only run is always in."""
    (rm.runs / "doomed" / "logs").mkdir(parents=True)
    if before is not None:
        (rm.runs / ".archived.json").write_text(before)

    assert rm.go("doomed", "--yes", client=Workflows([("run-other-zz99zz", CLOSED)])) == 0

    assert capsys.readouterr().out.splitlines()[-1] == \
        "removed runs/doomed; archived 0 workflow row(s)"
    assert not (rm.runs / "doomed").exists()
    archive = rm.runs / ".archived.json"
    assert (archive.read_text() if archive.exists() else None) == before, \
        "the archive file was rewritten with nothing to record"


def test_an_uncleanable_worktree_still_lets_the_directory_go(rm, world, capsys):
    """AC-24. The repository moved, or .env has not been written yet. The
    registration cannot be taken out from here whatever happens, and leaving the
    run directory on disk as well helps nobody: the owner is told which worktree
    and why, and the thing they asked for is done."""
    lost = rm.runs / "doomed" / "worktrees" / "aa11"
    lost.mkdir(parents=True)
    (lost / ".git").write_text("gitdir: /projects/gone/.git/worktrees/aa11\n")
    (rm.runs / "doomed" / "logs").mkdir(parents=True)
    _, why = ui.resolve_repo(container_path("doomed", "aa11"), rm.runs, world.projects)
    assert why, "the fixture's lost worktree resolves after all"

    assert rm.go("doomed", "--yes", client=Workflows([("run-doomed-aa11aa", CLOSED)])) == 0

    out, err = capsys.readouterr()
    assert err == f"aa11: {why}\n"
    assert out.splitlines()[-1] == "removed runs/doomed; archived 1 workflow row(s)"
    assert not (rm.runs / "doomed").exists()


def test_rm_never_touches_a_branch(rm, world, recorded, capsys):
    """AC-23. `discard` deletes the run's branch because the owner refused the
    work. This is the other act — tidying up after one — and by then the branch
    is all that is left of it. A merged run's branch is history."""
    repo = world.repository()
    world.container(repo, "doomed", "tok1")
    (rm.runs / "doomed" / "logs").mkdir(parents=True)

    assert rm.go("doomed", "--yes", client=Workflows([("run-doomed-aa11aa", CLOSED)])) == 0

    assert branch_of("doomed", "tok1") in world.git(
        repo, "branch", "--format=%(refname:short)").split(), "the run's branch was deleted"
    assert [argv for argv in recorded if "branch" in argv] == []


def test_rm_leaves_a_neighbouring_runs_worktree_alone(rm, world, capsys):
    """AC-29, end to end. Tidying a finished run off the rail while its
    replacement works the same project is the ordinary use of this command, and
    from the host both registrations look stale, because both were made inside
    the container. A prune here takes the live run with the dead one and its next
    git command dies with `fatal: not a git repository`."""
    repo = world.repository()
    world.container(repo, "doomed", "tok1")
    world.container(repo, "live", "tok2")
    for slug in ("doomed", "live"):
        (rm.runs / slug / "logs").mkdir(parents=True)

    assert rm.go("doomed", "--yes", client=Workflows([("run-doomed-aa11aa", CLOSED)])) == 0

    assert container_path("live", "tok2") in world.listed(repo), \
        "the live run's registration went with the removed one"
    assert (repo / ".git" / "worktrees" / "tok2").is_dir()
    assert (rm.runs / "live" / "worktrees" / "tok2").is_dir()
    assert container_path("doomed", "tok1") not in world.listed(repo)


def test_a_directory_that_will_not_delete_is_never_reported_as_removed(rm, capsys):
    """The floor under the rmtree. `runs/` is made read-only, so rmtree empties
    the run and then cannot unlink the directory itself — a real partial delete,
    not an injected one, and exactly what a read-only mount or a stuck filesystem
    leaves behind.

    Two things must not happen then. It must not say `removed runs/doomed`, and
    it must not archive: a row off the rail over logs still on disk is a run the
    owner can neither see nor find. `shutil.rmtree(run, ignore_errors=True)` —
    the obvious thing to reach for when this goes flaky — makes both happen at
    once and exits 0 while it does.
    """
    (rm.runs / "doomed" / "logs").mkdir(parents=True)
    (rm.runs / "doomed" / "logs" / "i1-r1-exec.log").write_text("a transcript\n")
    feed = Workflows([("run-doomed-aa11aa", CLOSED)])
    os.chmod(rm.runs, 0o555)
    try:
        code = rm.go("doomed", "--yes", client=feed)
    finally:
        os.chmod(rm.runs, 0o755)

    out, err = capsys.readouterr()
    assert (rm.runs / "doomed").exists(), \
        "the run directory went after all, so this proves nothing"
    assert code == 1
    assert err.splitlines()[-1].startswith("could not delete runs/doomed: "), err
    assert "removed runs/doomed" not in out, "it claimed a directory that is still there"
    assert not (rm.runs / ".archived.json").exists(), \
        "the row left the rail while its directory was still on disk"


def test_an_unreadable_archive_file_is_a_line_and_still_a_zero(rm, capsys):
    """The directory and the worktrees are already gone by the time the write
    happens, and no exit code puts them back. Telling the owner the rows are
    still on the rail, and which file to move aside, beats a traceback over work
    that is done — and beats a non-zero exit that says the whole command failed.
    """
    (rm.runs / "doomed" / "logs").mkdir(parents=True)
    (rm.runs / ".archived.json").write_text("{}")

    assert rm.go("doomed", "--yes", client=Workflows([("run-doomed-aa11aa", CLOSED)])) == 0

    out, err = capsys.readouterr()
    assert err == ("runs/.archived.json is not a readable list; "
                   "move it aside and archive by hand\n")
    assert out.splitlines()[-1] == "removed runs/doomed; archived 0 workflow row(s)"
    assert not (rm.runs / "doomed").exists()
    assert (rm.runs / ".archived.json").read_text() == "{}", "the unreadable file was overwritten"


# ---------- what the docs teach ----------

def test_the_readme_and_agents_md_document_lg_rm():
    """AC-33. A destructive command nobody wrote down is one the owner meets for
    the first time by running it. The README says what goes and what stops it;
    AGENTS.md's layout line names it beside the other subcommands, because that
    is where the next agent to change `lg` looks for what `lg` does."""
    root = Path(ui.__file__).resolve().parent
    readme = (root / "README.md").read_text()
    listed = [ln for ln in readme.splitlines() if ln.startswith("lg rm ")]
    assert listed, "README's lg command list never shows lg rm"
    assert any("--yes" in ln for ln in listed), "README never shows the --yes flag"
    for missing, said in [("what it de-registers", "worktree"),
                          ("what stops it", "refuses"),
                          ("that Temporal being down stops it", "cannot reach Temporal"),
                          ("what the prompt wants", "type the run's name back")]:
        assert said in readme, f"README does not say {missing}"

    # The whole bullet, not its first line: where the sentence wraps is not
    # something a doc test should have an opinion about.
    lines = (root / "AGENTS.md").read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("- `lg` —"))
    rest = lines[start + 1:]
    end = next((i for i, ln in enumerate(rest) if ln.startswith("- ")), len(rest))
    bullet = "\n".join(lines[start:start + 1 + end])
    assert "lg rm" in bullet, f"AGENTS.md's lg entry does not name lg rm:\n{bullet}"
