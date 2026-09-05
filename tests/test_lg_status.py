"""What `lg status` prints, and which workflow a run-directory slug names.

Both are pure functions over plain data, so they are tested on dictionaries and
lists with no Temporal anywhere. The ledgers here are the shapes the engine has
really produced, old ones included: a run that closed before `items` existed
hands back a dictionary without the key, and printing it must not raise.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _lg():
    """`lg` has no .py extension, so it loads by path."""
    loader = SourceFileLoader("lg_cli_status", str(ROOT / "lg"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("lg_cli_status", loader))
    loader.exec_module(mod)
    return mod


@pytest.fixture
def lg():
    return _lg()


def at(hour: int) -> datetime:
    return datetime(2026, 9, 5, hour, 0, tzinfo=timezone.utc)


# ---------- the summary ----------

def test_full_ledger_prints_every_section(lg):
    """The whole format at once, so a reworded line fails here and not in a
    reviewer's terminal. The question keeps its own line breaks, blank line and
    all: it is the card's text as the owner saw it, not a rebuild of it."""
    ledger = {
        "status": "running",
        "reason": "checkpoint refused: empty write set",
        "awaiting": {
            "kind": "decision",
            "question": "item 2 of 3 · round 1\n\nWhich port should the health check use?",
            "options": {"A": "8400", "B": "9000"},
            "telegram": True,
            "answer_with": "lg approve run-toy-ab12cd <A|B>",
        },
        "items": [
            {"n": 1, "item": "one", "status": "done", "commit": "3f2a1b9c0d5e6f"},
            {"n": 2, "item": "two", "status": "parked",
             "reason": "gates red after the correction cap"},
            {"n": 3, "item": "three", "status": "pending"},
        ],
        "rounds": [
            {"item_no": 1, "round": 1, "status": "green", "verdict": "accept"},
            {"item_no": 2, "round": 1, "status": "green", "verdict": "ask"},
        ],
    }
    assert lg.format_status(ledger) == "\n".join([
        "status: running",
        "reason: checkpoint refused: empty write set",
        "",
        "awaiting: decision",
        "  item 2 of 3 · round 1",
        "",
        "  Which port should the health check use?",
        "  A — 8400",
        "  B — 9000",
        "  answer with: lg approve run-toy-ab12cd <A|B>",
        "",
        "items:",
        "  1 done 3f2a1b9c0d",
        "  2 parked gates red after the correction cap",
        "  3 pending",
        "",
        "rounds:",
        "  item 1 round 1 accept",
        "  item 2 round 1 ask",
        "",
    ])


def test_awaiting_without_a_question_says_not_recorded(lg):
    """Runs that started before `question` was recorded still have a card up.
    The block must say the question is missing rather than print nothing where
    the question goes, which reads as a card with no question."""
    out = lg.format_status({"status": "running", "awaiting": {
        "kind": "merge-ready", "options": {"A": "merge"}, "telegram": True,
        "answer_with": "lg approve run-toy-ab12cd <A>"}})
    assert "  question not recorded" in out.splitlines()


def test_no_awaiting_prints_no_awaiting_section(lg):
    out = lg.format_status({"status": "merged", "items": [], "rounds": []})
    assert "awaiting" not in out
    assert out.startswith("status: merged\n")
    assert "reason" not in out


def test_empty_items_and_rounds_say_none_yet(lg):
    out = lg.format_status({"status": "running", "items": [], "rounds": []})
    assert out == "status: running\n\nitems:\n  (none yet)\n\nrounds:\n  (none yet)\n"


def test_a_ledger_with_no_items_or_rounds_keys_says_none_yet(lg):
    """The real recorded result of run-2026-09-05-microbits-ideas-sharpen-794631,
    which closed before `items` was a ledger key. `ledger["items"]` is a KeyError
    on it, and a traceback is not an answer to `lg status`."""
    out = lg.format_status({"status": "stopped",
                            "reason": "checkpoint refused: empty write set",
                            "checkpoint": {"committed": False, "reason": "empty write set"}})
    assert "items:\n  (none yet)" in out
    assert "rounds:\n  (none yet)" in out


def test_a_red_gate_round_prints_escalated(lg):
    out = lg.format_status({"status": "stopped",
                            "rounds": [{"item_no": 1, "round": 2, "status": "escalated"}]})
    assert "  item 1 round 2 escalated" in out.splitlines()


def test_a_round_still_being_audited_says_audit_running(lg):
    """The entry is appended before the audit runs, so a green round with no
    verdict is the ordinary mid-audit state. Printing `green` where the verdict
    goes would read as an acceptance that has not happened."""
    out = lg.format_status({"status": "running",
                            "rounds": [{"item_no": 2, "round": 1, "status": "green"}]})
    assert "  item 2 round 1 audit running" in out.splitlines()
    assert "green" not in out


def test_a_round_recorded_before_item_no_existed_reads_as_item_1(lg):
    """Two closed runs on this machine have a round with no `item_no`. They were
    single-item runs, so item 1 is what they were — the same default a log name
    with no item number gets."""
    out = lg.format_status({"status": "merged",
                            "rounds": [{"round": 1, "status": "green", "verdict": "accept"}]})
    assert "  item 1 round 1 accept" in out.splitlines()


def test_no_card_says_lg_approve_is_the_only_way(lg):
    """With no Telegram configured nothing was sent to the owner's phone. Saying
    so is the whole difference between a run that is waiting and one that looks
    stuck."""
    line = "  no card was sent; the lg approve command is the only way to answer"
    awaiting = {"kind": "decision", "question": "q?", "options": {},
                "telegram": False, "answer_with": "lg approve run-toy-ab12cd <answer>"}
    assert line in lg.format_status({"status": "running", "awaiting": awaiting}).splitlines()
    awaiting["telegram"] = True
    assert line not in lg.format_status({"status": "running", "awaiting": awaiting})


# ---------- which workflow a slug names ----------

def test_an_exact_id_wins_without_a_lookup(lg):
    candidates = [("run-toy-ab12cd", at(1)), ("run-toy-ef34gh", at(2))]
    assert lg.resolve_run_arg("run-toy-ab12cd", candidates) == ("run-toy-ab12cd", 1)


@pytest.mark.parametrize("arg", ["toy", "runs/toy", "runs/toy/"])
def test_bare_slug_and_runs_forms_resolve_the_same(lg, arg):
    assert lg.run_slug(arg) == "toy"
    assert lg.resolve_run_arg(arg, [("run-toy-ab12cd", at(1))]) == ("run-toy-ab12cd", 1)


def test_a_slug_that_prefixes_another_run_matches_only_its_own(lg):
    """The no-dash rule is the inverse of ui.TemporalFeed's id[4:id.rfind("-")].
    Without it `foo` would claim every run whose slug starts with foo-."""
    candidates = [("run-foo-ab12cd", at(1)), ("run-foo-bar-ef34gh", at(2)),
                  ("run-foo-", at(3))]
    assert lg.resolve_run_arg("foo", candidates) == ("run-foo-ab12cd", 1)


def test_two_matches_pick_the_later_start_and_report_two(lg):
    candidates = [("run-toy-ab12cd", at(1)), ("run-toy-ef34gh", at(9)),
                  ("run-other-ij56kl", at(11))]
    assert lg.resolve_run_arg("runs/toy", candidates) == ("run-toy-ef34gh", 2)


def test_no_match_is_none_and_zero(lg):
    assert lg.resolve_run_arg("nothing", [("run-toy-ab12cd", at(1))]) == (None, 0)
