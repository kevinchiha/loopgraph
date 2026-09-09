"""`lg start` refusing a path the worker cannot reach, before Temporal sees it.

The engine runs in a container. Two host directories are mounted into it: the
engine root, as `/app`, and the one projects tree named by
LOOPGRAPH_PROJECTS_DIR, as `/projects`. Nothing else exists in there.

A run started with a host path for its repository — `/home/you/projects/thing`
rather than `/projects/thing` — reached Temporal, started, and died in
`run_baseline` on the first git command with a bare

    FileNotFoundError: [Errno 2] No such file or directory: '/home/you/projects/thing'

carried to the owner's phone as `engine failure:` several seconds later, naming
neither the cause nor the fix. `lg` knows both before it connects: it runs on the
host, where the mounted trees are ordinary directories it can look at.
"""

from __future__ import annotations

import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

import ui

ROOT = Path(ui.__file__).resolve().parent


@pytest.fixture
def lg():
    """`lg` has no .py extension, so it loads by path, under this file's own name."""
    loader = SourceFileLoader("lg_cli_start", str(ROOT / "lg"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("lg_cli_start", loader))
    loader.exec_module(mod)
    return mod


@pytest.fixture
def machine(tmp_path):
    """A host with an engine root and a projects tree holding one repository."""
    (tmp_path / "engine" / "runs" / "2026-09-09-toy").mkdir(parents=True)
    (tmp_path / "projects" / "investigator").mkdir(parents=True)
    return tmp_path


# ---------- the check itself ----------

def test_a_host_path_under_the_projects_tree_names_the_path_to_use(lg, machine):
    """The reported failure. The owner typed the path their shell tab-completed,
    which is the only path they have ever needed for that repository, and the
    container has no such directory. `lg` can do the translation itself, so the
    refusal hands it over rather than restating the rule and stopping."""
    why = lg.unreachable("target repo", str(machine / "projects" / "investigator"),
                         str(machine / "projects"), str(machine / "engine"))
    assert why == (
        f"target repo {machine}/projects/investigator is a path on this machine, and "
        f"the engine runs in a container that cannot see it. Use "
        f"/projects/investigator instead.")


def test_a_host_path_outside_the_projects_tree_says_what_is_mounted(lg, machine):
    """No translation exists: that directory is in no mounted tree, so there is
    no container path for it and saying `use /projects/...` would be a lie. The
    owner has to move the repository or re-point the mount, and the refusal says
    which tree it has to end up under."""
    why = lg.unreachable("target repo", "/opt/src/investigator",
                         str(machine / "projects"), str(machine / "engine"))
    assert why == (
        f"target repo /opt/src/investigator is a path on this machine, and the engine "
        f"runs in a container that cannot see it. Only {machine}/projects is mounted, "
        f"as /projects, so the repo has to live under it.")


def test_a_host_path_with_no_projects_dir_set_points_at_the_installer(lg, machine):
    """Before the first `./install.sh` there is no .env and no mount to name."""
    why = lg.unreachable("target repo", "/opt/src/investigator", None,
                         str(machine / "engine"))
    assert why == (
        "target repo /opt/src/investigator is a path on this machine, and the engine "
        "runs in a container that cannot see it. Target repos are named "
        "/projects/<name>; LOOPGRAPH_PROJECTS_DIR is not set, so run ./install.sh.")


def test_a_repo_that_is_there_passes(lg, machine):
    """The correct call, and the one every doc teaches."""
    assert lg.unreachable("target repo", "/projects/investigator",
                          str(machine / "projects"), str(machine / "engine")) == ""


def test_a_container_path_for_a_repo_that_is_not_there_names_where_it_looked(lg, machine):
    """A typo in the name is the same FileNotFoundError from the same git command,
    so it gets caught in the same place. The host path is in the message because
    that is the one the owner can go and look at."""
    why = lg.unreachable("target repo", "/projects/investigatr",
                         str(machine / "projects"), str(machine / "engine"))
    assert why == (f"target repo /projects/investigatr is not on this machine: "
                   f"no directory {machine}/projects/investigatr.")


def test_a_run_dir_under_app_resolves_against_the_engine_root(lg, machine):
    """/app is the engine repo, bind-mounted. The run directory a run is started
    with lives under it, and `lg` is running inside that same checkout."""
    engine = str(machine / "engine")
    assert lg.unreachable("run dir", "/app/runs/2026-09-09-toy", None, engine) == ""
    assert lg.unreachable("run dir", "/app/runs/2026-09-09-typo", None, engine) == (
        f"run dir /app/runs/2026-09-09-typo is not on this machine: "
        f"no directory {engine}/runs/2026-09-09-typo.")


def test_a_projects_dir_written_with_a_trailing_slash_still_matches(lg, machine):
    """install.sh writes what the owner typed without normalising it, and
    ui.resolve_repo already carries the same warning about that slash."""
    why = lg.unreachable("target repo", str(machine / "projects" / "investigator"),
                         str(machine / "projects") + "/", str(machine / "engine"))
    assert "Use /projects/investigator instead." in why


def test_a_sibling_tree_with_the_same_prefix_is_not_the_projects_tree(lg, machine):
    """`<dir>projects-old` starts with `<dir>projects` and is a different tree.
    Suggesting `/projects/-old/thing` for it would send the owner somewhere that
    does not exist on either side of the mount."""
    why = lg.unreachable("target repo", str(machine / "projects-old" / "thing"),
                         str(machine / "projects"), str(machine / "engine"))
    assert "Use /projects/" not in why
    assert "so the repo has to live under it." in why


def test_a_relative_path_is_a_host_path_too(lg, machine):
    """`lg` rewrites `runs/<slug>` and nothing else, so every other relative path
    reaches the worker as typed and resolves against the container's cwd."""
    why = lg.unreachable("target repo", "../investigator", str(machine / "projects"),
                         str(machine / "engine"))
    assert why.startswith("target repo ../investigator is a path on this machine")


# ---------- the command ----------

def run_start(lg, monkeypatch, machine, *argv) -> int:
    """`lg start ...` end to end, argparse included, on a machine with no Temporal.

    `_client` raises rather than returning a fake: every test here is about a run
    that must never be started, and a connection attempt is the failure.
    """
    async def no_client():
        raise AssertionError("lg start connected to Temporal with a bad path")
    monkeypatch.setattr(lg, "_client", no_client)
    monkeypatch.setattr(lg, "ROOT", str(machine / "engine"))
    (machine / "engine" / ".env").write_text(
        f"LOOPGRAPH_PROJECTS_DIR={machine}/projects\n")
    monkeypatch.setattr(sys, "argv", ["lg", "start", *argv])
    return lg.main()


def test_the_command_refuses_a_host_repo_and_never_connects(lg, monkeypatch, machine,
                                                            capsys):
    """The whole point: nothing reaches Temporal, so there is no workflow to read,
    no card, and nothing to clean up. Exit 1, and the reason on stderr, because
    stdout of a start is the workflow id and the ledger."""
    code = run_start(lg, monkeypatch, machine, "runs/2026-09-09-toy",
                     f"{machine}/projects/investigator")
    out = capsys.readouterr()
    assert code == 1
    assert out.out == ""
    assert out.err.strip() == (
        f"target repo {machine}/projects/investigator is a path on this machine, and "
        f"the engine runs in a container that cannot see it. Use /projects/investigator "
        f"instead.")


def test_the_command_checks_the_run_dir_the_same_way(lg, monkeypatch, machine, capsys):
    """A mistyped slug dies in the same activity for the same reason. The run
    directory is checked first because a run named wrong is not a run at all."""
    code = run_start(lg, monkeypatch, machine, "runs/2026-09-09-typo",
                     "/projects/investigator")
    assert code == 1
    assert capsys.readouterr().err.strip().startswith(
        "run dir /app/runs/2026-09-09-typo is not on this machine")


def test_an_omitted_repo_is_checked_where_it_defaults_to(lg, monkeypatch, machine,
                                                         capsys):
    """With no repository argument the run works in `<run dir>/target`, and
    nothing creates that directory: run_baseline is the first thing to touch it."""
    code = run_start(lg, monkeypatch, machine, "runs/2026-09-09-toy")
    assert code == 1
    assert capsys.readouterr().err.strip().startswith(
        "target repo /app/runs/2026-09-09-toy/target is not on this machine")


def test_a_good_pair_gets_past_the_check_to_the_connection(lg, monkeypatch, machine):
    """The check is not allowed to become the thing that stops a correct run: the
    fake `_client` is the proof it was reached."""
    with pytest.raises(AssertionError, match="connected to Temporal"):
        run_start(lg, monkeypatch, machine, "runs/2026-09-09-toy",
                  "/projects/investigator")
