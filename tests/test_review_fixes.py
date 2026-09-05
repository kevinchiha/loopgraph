"""Regression pins for the defects the full-repo review confirmed.

Each test names the failure it prevents, because the point of a review is only
banked once something fails when the bug comes back.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from activities.audit import diff_including_new_files
from activities.execute_round import reset_to_checkpoint
from activities.stream import LOG_GLOB, LOG_RE, log_name

ROOT = Path(__file__).resolve().parent.parent


def _git(wt, *args):
    subprocess.run(["git", *args], cwd=wt, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "PATH": "/usr/bin:/bin"})


@pytest.fixture
def worktree(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "cli.py").write_text("x = 1\n")
    (tmp_path / ".gitignore").write_text("ignored/\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    return tmp_path


# ---------- a parked item must not leak into the next item ----------

def test_a_round_starts_from_the_last_checkpoint(worktree):
    """The failure: rounds restart at 1 for each work item, so a reset that only
    ran on a redo left a PARKED item's gate-red edits in the shared worktree, and
    the next item's write set, audit diff and commit swept them up."""
    (worktree / "cli.py").write_text("rejected work\n")
    (worktree / "junk.py").write_text("also rejected\n")
    asyncio.run(reset_to_checkpoint(str(worktree)))
    assert (worktree / "cli.py").read_text() == "x = 1\n"
    assert not (worktree / "junk.py").exists()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=worktree,
                            capture_output=True, text=True).stdout
    assert status == "", "the next item must start from a clean tree"


def test_the_reset_keeps_earlier_checkpoints(worktree):
    (worktree / "item1.py").write_text("done\n")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "loopgraph: item 1 accept")
    (worktree / "cli.py").write_text("rejected\n")
    asyncio.run(reset_to_checkpoint(str(worktree)))
    assert (worktree / "item1.py").exists(), "a committed item must survive"


def test_the_reset_leaves_ignored_files_alone(worktree):
    """A node_modules or a venv must survive, or every round pays to rebuild it."""
    (worktree / "ignored").mkdir()
    (worktree / "ignored" / "big.bin").write_text("expensive\n")
    asyncio.run(reset_to_checkpoint(str(worktree)))
    assert (worktree / "ignored" / "big.bin").exists()


# ---------- the auditor must be shown the files it is judging ----------

def test_a_new_file_appears_in_the_diff_the_auditor_reads(worktree):
    """The failure: `git diff HEAD` reports tracked changes only, so a brief that
    said "add parsers/csv.py" produced an EMPTY diff. The auditor either rejected
    work it was never shown or accepted it sight unseen, and the checkpoint
    committed the file either way."""
    (worktree / "new_module.py").write_text("def added():\n    return 1\n")
    (worktree / "cli.py").write_text("x = 2\n")
    diff = asyncio.run(diff_including_new_files(str(worktree)))
    assert "new_module.py" in diff, "the new file is invisible to the auditor"
    assert "def added" in diff, "its contents are invisible too"
    assert "cli.py" in diff, "the tracked edit is still there"


def test_reading_the_diff_leaves_the_index_as_it_found_it(worktree):
    """The checkpoint stages the declared write set itself; a leftover
    intent-to-add entry would change what it commits."""
    (worktree / "new_module.py").write_text("y = 2\n")
    before = subprocess.run(["git", "status", "--porcelain"], cwd=worktree,
                            capture_output=True, text=True).stdout
    asyncio.run(diff_including_new_files(str(worktree)))
    after = subprocess.run(["git", "status", "--porcelain"], cwd=worktree,
                           capture_output=True, text=True).stdout
    assert after == before == "?? new_module.py\n"


def test_a_clean_tree_still_produces_an_empty_diff(worktree):
    assert asyncio.run(diff_including_new_files(str(worktree))).strip() == ""


# ---------- the log names, and everything that reads them ----------

@pytest.mark.parametrize("item,rnd,role", [(1, 1, "executor"), (2, 3, "audit"), (11, 2, "executor")])
def test_every_reader_matches_the_names_the_writers_produce(item, rnd, role):
    """The failure: adding the item number to log filenames silently broke
    `lg tail` and the dashboard, which each kept their own copy of the old shape.
    Neither found a log again, and the UI test passed because its fixture wrote
    the stale name."""
    import fnmatch
    name = log_name(item, rnd, role)
    assert re.match(LOG_RE, name), f"the dashboard regex does not match {name}"
    assert fnmatch.fnmatch(name, LOG_GLOB), f"lg tail's glob does not match {name}"


def test_the_old_log_names_still_render():
    assert re.match(LOG_RE, "r1-executor.log"), "runs from before the queue must still show"


def test_the_dashboard_ships_a_real_pattern():
    import ui
    html = ui.page_html()
    assert "__LOG_RE__" not in html, "the placeholder was never substituted"
    assert re.search(r"new RegExp\('(.+?)'\)", html)


# ---------- every gate example in the docs must actually parse ----------

DOCS = ["README.md", "SPEC.md", "skills/loopgraph/SKILL.md", "INSTALL_WITH_AGENT.md", "AGENTS.md"]


@pytest.mark.parametrize("doc", DOCS)
def test_yaml_examples_in_the_docs_parse(doc):
    """The failure: the documented gates.yaml used a one-line `- name: x cmd: y`
    form that crashes yaml.safe_load. Copying it out of the README gave a run that
    died before the first gate, with no card and no explanation."""
    text = (ROOT / doc).read_text()
    for i, block in enumerate(re.findall(r"```ya?ml\n(.*?)```", text, re.S)):
        try:
            yaml.safe_load(block)
        except yaml.YAMLError as e:
            pytest.fail(f"{doc} yaml block {i + 1} does not parse: {e}")


def test_the_gate_loader_accepts_the_readme_example(tmp_path):
    from activities.gate import load_gates
    block = re.search(r"```yaml\n(- name: tests.*?)```", (ROOT / "README.md").read_text(), re.S)
    assert block, "the README no longer shows a gates.yaml example"
    p = tmp_path / "gates.yaml"
    p.write_text(block.group(1))
    gates = load_gates(str(p))
    assert [g["name"] for g in gates] == ["tests", "build", "scope"]
    assert all(g["timeout"] for g in gates)


# ---------- the worker must be able to start ----------

def test_every_registered_activity_is_decorated():
    """Adding a helper just above an activity is enough to steal its
    @activity.defn, and the only symptom is the worker dying at startup with
    "Activity <name> missing attributes". Cheap to check, expensive to debug."""
    import worker
    from temporalio.worker import Worker  # noqa: F401  (import proves the dep is there)
    import inspect
    src = inspect.getsource(worker.main)
    listed = re.search(r"activities=\[(.*?)\]", src, re.S)
    assert listed, "worker.main no longer passes an activities list"
    names = [n.strip() for n in listed.group(1).replace("\n", " ").split(",") if n.strip()]
    assert len(names) >= 8, f"expected the full activity set, got {names}"
    for n in names:
        fn = getattr(worker, n)
        assert hasattr(fn, "__temporal_activity_definition"), \
            f"{n} is registered on the worker but is not decorated with @activity.defn"


def test_the_workflow_class_registers_its_signal():
    """`lg approve` is useless if the signal handler loses its decorator."""
    from workflows.run import LoopGraphRun
    assert hasattr(LoopGraphRun.decide, "__temporal_signal_definition")
