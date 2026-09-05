"""The work queue: several items per run, and what happens when one won't pass."""

from __future__ import annotations

from activities.items import parse_work_items
from workflows.run import build_merge_summary


# ---------- reading the queue out of a brief ----------

BRIEF = """# Feature: redesign

Match the screenshots in mockups/.

## Work items

- Redesign the settings page
- Redesign the profile page
2. Redesign the billing page

## Done when

- every page matches its mockup
"""


def test_items_come_from_the_work_items_section_in_order():
    assert parse_work_items(BRIEF) == [
        "Redesign the settings page",
        "Redesign the profile page",
        "Redesign the billing page",
    ]


def test_the_next_heading_ends_the_list():
    """'every page matches its mockup' is a done-when bullet, not a work item."""
    assert "every page matches its mockup" not in parse_work_items(BRIEF)


def test_a_brief_with_no_section_is_one_item():
    """The older shape still works: no section means the whole brief is the item."""
    assert parse_work_items("# Add a --hello flag\n\nDone when tests pass.") == []


def test_the_heading_is_matched_loosely():
    for heading in ("## Work items", "# WORK ITEMS", "### work  items"):
        assert parse_work_items(f"{heading}\n\n- one\n") == ["one"]


def test_a_wrapped_bullet_stays_one_item():
    """Cutting an item at its first line hands the executor half an instruction."""
    brief = ("## Work items\n\n"
             "- Add a --hello flag that prints exactly\n"
             "  `hello from the example`, and a test for it.\n"
             "- Second item\n")
    items = parse_work_items(brief)
    assert items == [
        "Add a --hello flag that prints exactly `hello from the example`, and a test for it.",
        "Second item",
    ]


def test_unindented_prose_ends_the_list():
    brief = "## Work items\n\n- one\n\nThat is the lot.\n\n- not an item\n"
    assert parse_work_items(brief) == ["one"]


def test_the_shipped_example_parses_into_two_items():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    items = parse_work_items((root / "runs/example-hello/brief.md").read_text())
    assert len(items) == 2
    assert "--hello" in items[0] and "--goodbye" in items[1]


def test_bullets_outside_the_section_are_ignored():
    brief = "# Feature\n\n- not an item\n\n## Work items\n\n- an item\n"
    assert parse_work_items(brief) == ["an item"]


# ---------- what the owner is told when something was parked ----------

def test_a_clean_run_says_nothing_about_parking():
    assert build_merge_summary("did the thing", 3, []) == "did the thing"


def test_parked_items_are_named_on_the_merge_card():
    parked = [{"n": 2, "item": "Redesign the profile page",
               "reason": "gates red after the correction cap"}]
    out = build_merge_summary("2 of 3 items done", 3, parked)
    assert "item 2: Redesign the profile page" in out
    assert "gates red after the correction cap" in out


def test_the_card_says_merging_does_not_include_the_parked_ones():
    """The one thing the owner must not conclude is that a green card means done."""
    out = build_merge_summary("done", 5, [{"n": 1, "item": "a", "reason": "r"},
                                          {"n": 4, "item": "b", "reason": "r"}])
    assert "NOT in this branch" in out
    assert "Merging takes the 3 item(s) that passed" in out


def test_long_item_text_is_trimmed_so_the_card_still_fits():
    out = build_merge_summary("done", 2, [{"n": 1, "item": "x" * 500, "reason": "r"}])
    assert "x" * 121 not in out


# ---------- an executor that commits its own work ----------

import asyncio
import subprocess

import pytest

from activities.execute_round import undo_self_commit


def _git(wt, *args):
    subprocess.run(["git", *args], cwd=wt, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "PATH": "/usr/bin:/bin"})


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "cli.py").write_text("x = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def _head(wt):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt, capture_output=True,
                          text=True, check=True).stdout.strip()


def _porcelain(wt):
    return subprocess.run(["git", "status", "--porcelain"], cwd=wt, capture_output=True,
                          text=True, check=True).stdout


def test_a_quiet_executor_is_left_alone(repo):
    start = _head(repo)
    (repo / "cli.py").write_text("x = 2\n")
    assert asyncio.run(undo_self_commit(str(repo), start)) is False
    assert _head(repo) == start
    assert " M cli.py" in _porcelain(repo)


def test_a_self_commit_is_put_back_in_the_working_tree(repo):
    """The failure this prevents: the executor commits, git status comes back
    clean, the write set is empty, and a round that passed every gate is parked."""
    start = _head(repo)
    (repo / "cli.py").write_text("x = 2\n")
    (repo / "new.py").write_text("y = 3\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "the executor committing its own work")
    assert _porcelain(repo) == "", "precondition: a self-commit leaves a clean tree"

    assert asyncio.run(undo_self_commit(str(repo), start)) is True
    assert _head(repo) == start, "the commit is gone"
    status = _porcelain(repo)
    assert " M cli.py" in status and "?? new.py" in status, "the work is not"
    assert (repo / "cli.py").read_text() == "x = 2\n"
    assert (repo / "new.py").read_text() == "y = 3\n"


def test_earlier_checkpoints_survive(repo):
    """Only the executor's own commit is undone, never a previous item's."""
    (repo / "item1.py").write_text("done = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "loopgraph: item 1 accept")
    start = _head(repo)
    (repo / "item2.py").write_text("done = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "executor committing item 2")

    asyncio.run(undo_self_commit(str(repo), start))
    assert _head(repo) == start
    assert (repo / "item1.py").exists(), "item 1's checkpoint is still committed"
    assert "?? item2.py" in _porcelain(repo)
