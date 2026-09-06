"""Forced convergence: the removal item the engine writes for itself."""

from __future__ import annotations

from workflows.run import build_convergence_item


def test_the_convergence_text_is_exact():
    """The whole string, not a substring. This text is the only instruction the
    executor gets for an item nobody wrote by hand, and a reworded line is a
    different item."""
    assert build_convergence_item(["cli.py", "tests/test_cli.py"]) == (
        "Convergence item. No new behaviour, no new files, no new public surface.\n"
        "Net lines for this item must be at or under zero; the engine measures it with\n"
        "git diff --numstat over every staged line, tests included, and refuses the\n"
        "commit if it is not.\n"
        "Look at what the last items added:\n"
        "cli.py\n"
        "tests/test_cli.py\n"
        "Remove dead code, merge duplicates you introduced, delete anything with no consumer.\n"
        "Every gate stays green. If there is genuinely nothing to remove, change no file and\n"
        "say in your claims what you checked and why each thing stays."
    )
