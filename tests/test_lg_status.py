"""What `lg status` prints, which workflow a run-directory slug names, and how
the command wires the two together.

The summary and the resolution are pure functions over plain data, so they are
tested on dictionaries and lists. The ledgers here are the shapes the engine has
really produced, old ones included: a run that closed before `items` existed
hands back a dictionary without the key, and printing it must not raise. The
command itself runs against a fake client, which is the only way to test the
paths that matter — a run that has not finished, and an id nobody has. The last
section is copy: the help text and the two documents that teach this command.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

import pytest
from temporalio.client import WorkflowExecutionStatus, WorkflowQueryFailedError
from temporalio.service import RPCError, RPCStatusCode

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
    """The shape a run closed before both keys existed hands back. Not a
    verbatim copy of any one run — the real ideas-sharpen ledger does carry
    `rounds` — but two closed runs on this machine have no `items` key at all.
    `ledger["items"]` is a KeyError on them, and a traceback is not an answer
    to `lg status`."""
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


# ---------- the kind word and the sweep section ----------

def test_a_non_brief_item_prints_its_kind(lg):
    """AC-28. A removal item and a detector's item read exactly like the items
    the owner wrote, so nothing says which of them the engine made up. An entry
    from a run older than `kind` has no key at all, and those runs were briefs."""
    out = lg.format_status({"status": "running", "items": [
        {"n": 1, "item": "one", "status": "done", "commit": "3f2a1b0c9d5e",
         "kind": "convergence"},
        {"n": 2, "item": "two", "status": "parked", "reason": "gates red",
         "kind": "sweep"},
        {"n": 3, "item": "three", "status": "pending", "kind": "brief"},
        {"n": 4, "item": "four", "status": "pending"},
    ]}).splitlines()
    assert "  1 done convergence 3f2a1b0c9d" in out
    assert "  2 parked sweep gates red" in out
    assert "  3 pending" in out
    assert "  4 pending" in out


def test_a_sweep_item_prints_its_group_after_the_kind(lg):
    """AC-8. Every sweep item takes one path group now, and two items of the same
    detector read alike apart from that group, so it goes on the line: after the
    kind word, before the commit or the reason. A running item has neither, and
    the line is right-stripped rather than left with a space on the end. An entry
    from a run older than the rotation carries no `group` and prints v1 AC-28's
    line."""
    out = lg.format_status({"status": "running", "items": [
        {"n": 2, "item": "two", "status": "done", "commit": "3f2a1b0c9d5e",
         "kind": "sweep", "group": "app/"},
        {"n": 3, "item": "three", "status": "parked", "reason": "gates red",
         "kind": "sweep", "group": "lib/"},
        {"n": 4, "item": "four", "status": "running", "kind": "sweep", "group": "app/"},
        {"n": 5, "item": "five", "status": "done", "commit": "aa11bb22cc33",
         "kind": "sweep"},
    ]}).splitlines()
    assert "  2 done sweep app/ 3f2a1b0c9d" in out
    assert "  3 parked sweep lib/ gates red" in out
    assert "  4 running sweep app/" in out
    assert "  5 done sweep aa11bb22cc" in out


def test_a_pass_line_carries_the_group_breakdown(lg):
    """AC-8. The count on its own says how much is left; the breakdown says where
    it is, which is what tells the owner whether the rotation is working through
    the repo or circling one directory.

    Sorted by name, not by the order the detectors reported: pass 1's detectors
    are deliberately out of name order and disjoint, so a breakdown built in
    encounter order reads `lib/ 3, app/ 5, scripts/ 2` and fails here. Summed by
    name, because two detectors reporting the same directory are one number to
    read, not two.
    """
    ledger = {"status": "running", "sweep": {"passes": [
        {"pass": 1, "total": 10, "complete": True, "detectors": [
            {"name": "vulture", "note": "", "groups": [{"name": "lib/", "count": 3}]},
            {"name": "ts-prune", "note": "", "groups": [{"name": "app/", "count": 5},
                                                        {"name": "scripts/", "count": 2}]}]},
        {"pass": 2, "total": 12, "complete": True, "detectors": [
            {"name": "vulture", "note": "", "groups": [{"name": "app/", "count": 7}]},
            {"name": "ts-prune", "note": "", "groups": [{"name": "app/", "count": 5}]}]},
        {"pass": 3, "total": 4, "complete": False, "detectors": [
            {"name": "vulture", "note": "", "groups": [{"name": "app/", "count": 4}]},
            {"name": "ts-prune", "note": "timeout after 600s", "groups": []}]},
    ], "ended": None}}
    lines = lg.format_status(ledger).splitlines()
    assert "  pass 1: 10 candidates (app/ 5, lib/ 3, scripts/ 2)" in lines
    assert "  pass 2: 12 candidates (app/ 12)" in lines
    assert "  pass 3: 4 candidates (incomplete: ts-prune) (app/ 4)" in lines


def test_a_pass_without_groups_prints_the_v1_line(lg):
    """AC-8 and AC-11. A pass recorded before this phase carries no `groups` on
    any detector, and a pass whose detectors grouped nothing carries `[]` on each.
    Neither has a breakdown, and empty brackets on the end of the line would say
    less than no brackets at all."""
    ledger = {"status": "running", "sweep": {"passes": [
        {"pass": 1, "total": 12, "complete": True,
         "detectors": [{"name": "vulture", "note": ""}]},
        {"pass": 2, "total": 3, "complete": False,
         "detectors": [{"name": "vulture", "note": "", "groups": []},
                       {"name": "ts-prune", "note": "timeout after 600s", "groups": []}]},
    ], "ended": None}}
    lines = lg.format_status(ledger).splitlines()
    assert "  " + lg.SWEEP_LINES["pass"].format(n=1, k=12) in lines
    assert "  " + lg.SWEEP_LINES["pass_incomplete"].format(n=2, k=3, names="ts-prune") in lines


def test_the_sweep_section_sits_between_items_and_rounds(lg):
    """AC-28, the whole printout. The passes are the only thing that says whether
    a sweep is getting anywhere, so they sit under the items they produced and
    above the rounds that produced them."""
    ledger = {
        "status": "stopped",
        "reason": "item cap 2 reached",
        "items": [
            {"n": 1, "item": "sweep one", "status": "done", "commit": "c1abc23456",
             "kind": "sweep"},
            {"n": 2, "item": "sweep two", "status": "parked", "kind": "sweep",
             "reason": "checkpoint refused: empty write set"},
        ],
        "sweep": {
            "passes": [
                {"pass": 1, "total": 12, "complete": True,
                 "detectors": [{"name": "vulture", "note": ""}]},
                {"pass": 2, "total": 3, "complete": False,
                 "detectors": [{"name": "vulture", "note": ""},
                               {"name": "ts-prune", "note": "timeout after 600s"}]},
            ],
            "ended": "item cap 2 reached",
        },
        "rounds": [{"item_no": 1, "round": 1, "status": "green", "verdict": "accept"}],
    }
    assert lg.format_status(ledger) == "\n".join([
        "status: stopped",
        "reason: item cap 2 reached",
        "",
        "items:",
        "  1 done sweep c1abc23456",
        "  2 parked sweep checkpoint refused: empty write set",
        "",
        "sweep:",
        "  pass 1: 12 candidates",
        "  pass 2: 3 candidates (incomplete: ts-prune)",
        "  ended: item cap 2 reached",
        "",
        "rounds:",
        "  item 1 round 1 accept",
        "",
    ])


def test_the_sweep_section_renders_from_sweep_lines(lg):
    """The dashboard prints these same lines and cannot import them: `lg` has no
    extension, so `ui.py` cannot read this dict and its own copy is checked
    against it instead. Every line here is one of these templates, or the page
    and the terminal go on saying different things about the same pass. Six of
    them since the rotation: a pass whose detectors carry groups prints the
    breakdown, and one from before they existed prints the line it always did."""
    assert set(lg.SWEEP_LINES) == {"pass", "pass_incomplete", "pass_groups",
                                   "pass_incomplete_groups", "ended", "none"}
    ledger = {"status": "running", "sweep": {
        "passes": [{"pass": 1, "total": 12, "complete": True,
                    "detectors": [{"name": "vulture", "note": ""}]},
                   {"pass": 2, "total": 3, "complete": False,
                    "detectors": [{"name": "vulture", "note": "exit 1"},
                                  {"name": "ts-prune", "note": "timeout after 600s"},
                                  {"name": "ruff", "note": ""}]},
                   {"pass": 3, "total": 8, "complete": True,
                    "detectors": [{"name": "vulture", "note": "",
                                   "groups": [{"name": "app/", "count": 5},
                                              {"name": "lib/", "count": 3}]}]},
                   {"pass": 4, "total": 5, "complete": False,
                    "detectors": [{"name": "vulture", "note": "",
                                   "groups": [{"name": "app/", "count": 5}]},
                                  {"name": "ts-prune", "note": "timeout after 600s",
                                   "groups": []}]}],
        "ended": "converged: nothing reported"}}
    lines = lg.format_status(ledger).splitlines()
    assert "  " + lg.SWEEP_LINES["pass"].format(n=1, k=12) in lines
    assert "  " + lg.SWEEP_LINES["pass_incomplete"].format(
        n=2, k=3, names="vulture, ts-prune") in lines, "only the detectors that failed"
    assert "  " + lg.SWEEP_LINES["pass_groups"].format(
        n=3, k=8, groups="app/ 5, lib/ 3") in lines
    assert "  " + lg.SWEEP_LINES["pass_incomplete_groups"].format(
        n=4, k=5, names="ts-prune", groups="app/ 5") in lines
    assert "  " + lg.SWEEP_LINES["ended"].format(
        reason="converged: nothing reported") in lines


def test_no_sweep_key_prints_no_section(lg):
    """A brief run has no `sweep` key and neither has any ledger from before this
    phase. An empty section on either is a heading about nothing."""
    assert "sweep" not in lg.format_status({"status": "merged", "items": [], "rounds": []})


def test_no_passes_yet(lg):
    """The gap between the ledger growing its sweep block and the first pass
    coming back. A bare heading there reads as a sweep that found nothing."""
    out = lg.format_status({"status": "running", "sweep": {"passes": [], "ended": None}})
    assert "sweep:\n  " + lg.SWEEP_LINES["none"] in out


# ---------- which workflow a slug names ----------

def test_an_exact_id_wins_without_a_lookup(lg):
    candidates = [("run-toy-ab12cd", at(1)), ("run-toy-ef34gh", at(2))]
    assert lg.resolve_run_arg("run-toy-ab12cd", candidates) == ("run-toy-ab12cd", 1)


@pytest.mark.parametrize("arg", ["toy", "runs/toy", "runs/toy/"])
def test_bare_slug_and_runs_forms_resolve_the_same(lg, arg):
    assert lg.run_slug(arg) == "toy"
    assert lg.resolve_run_arg(arg, [("run-toy-ab12cd", at(1))]) == ("run-toy-ab12cd", 1)


def test_a_slug_that_prefixes_another_run_matches_only_its_own(lg):
    """The no-dash rule is the inverse of ui.run_entry's id[4:id.rfind("-")].
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


# ---------- the command itself ----------

NO_HANDLER = "Query handler for 'status' expected but not found, known queries: [ledger]"
# What the SDK really raises: the worker replays the closed run's history
# against today's workflow code and diverges. Never a decoding failure.
DRIFTED = RuntimeError("[TMPRL1100] Nondeterminism error: activity type mismatch")

LEDGER = {"status": "merged",
          "items": [{"n": 1, "item": "one", "status": "done", "commit": "3f2a1b9c0d5e"}],
          "rounds": [{"item_no": 1, "round": 1, "status": "green", "verdict": "accept"}]}


def not_found(wf_id: str) -> RPCError:
    """What Temporal answers when the id has never existed."""
    return RPCError(f"workflow not found for ID: {wf_id}", RPCStatusCode.NOT_FOUND, b"")


class FakeHandle:
    """One workflow. `answers` maps a query name to its value or to the exception
    it raises; an unlisted name raises the SDK's own no-handler error, which is
    what a LoopGraphRun really answers to `status`."""

    def __init__(self, wf_id, answers=None, status=WorkflowExecutionStatus.COMPLETED,
                 result=None, missing=False):
        self.id, self.answers, self.status = wf_id, answers or {}, status
        self._result, self.missing = result, missing
        self.queried: list[str] = []
        self.awaited_result = False

    async def query(self, name):
        self.queried.append(name)
        if self.missing:
            raise not_found(self.id)
        answer = self.answers.get(name, WorkflowQueryFailedError(NO_HANDLER))
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def describe(self):
        return SimpleNamespace(status=self.status)

    async def result(self):
        self.awaited_result = True
        if self._result is None:
            raise AssertionError("result() was awaited on a run that has not finished")
        return self._result


class FakeClient:
    """Records every handle asked for and every listing, so a test can prove a
    lookup did not happen."""

    def __init__(self, handles=None, listed=()):
        self.handles = dict(handles or {})
        self.listed = list(listed)
        self.asked_for: list[str] = []
        self.list_queries: list[str] = []

    def get_workflow_handle(self, wf_id):
        self.asked_for.append(wf_id)
        if wf_id not in self.handles:
            self.handles[wf_id] = FakeHandle(wf_id, missing=True)
        return self.handles[wf_id]

    def list_workflows(self, query):
        self.list_queries.append(query)

        async def gen():
            for wf_id, start in self.listed:
                yield SimpleNamespace(id=wf_id, start_time=start)
        return gen()


def run_status(lg, monkeypatch, client, *argv) -> int:
    """`lg status ...` end to end, argparse included, on a fake Temporal."""
    async def fake_client():
        return client
    monkeypatch.setattr(lg, "_client", fake_client)
    monkeypatch.setattr(sys, "argv", ["lg", "status", *argv])
    return lg.main()


def test_a_real_id_prints_a_summary_and_never_lists(lg, monkeypatch, capsys):
    """The id is tried as an id, exactly as before. A run named in full must not
    cost a listing of every workflow on the machine."""
    client = FakeClient({"run-toy-ab12cd": FakeHandle("run-toy-ab12cd", {"ledger": LEDGER})})
    code = run_status(lg, monkeypatch, client, "run-toy-ab12cd")
    out, err = capsys.readouterr()
    assert (code, out, err) == (0, lg.format_status(LEDGER), "")
    assert client.list_queries == []


def test_a_status_query_answer_prints_json(lg, monkeypatch, capsys):
    """GateCheckRun and RoundRun answer `status` and have no ledger, so their
    answer prints as JSON the way it always has."""
    gate = {"red": [], "green": ["tests"]}
    client = FakeClient({"gate-ab12cd": FakeHandle("gate-ab12cd", {"status": gate})})
    assert run_status(lg, monkeypatch, client, "gate-ab12cd") == 0
    assert json.loads(capsys.readouterr().out) == gate


def test_json_flag_prints_the_raw_ledger(lg, monkeypatch, capsys):
    client = FakeClient({"run-toy-ab12cd": FakeHandle("run-toy-ab12cd", {"ledger": LEDGER})})
    assert run_status(lg, monkeypatch, client, "run-toy-ab12cd", "--json") == 0
    assert json.loads(capsys.readouterr().out) == LEDGER


def test_positional_query_still_prints_json(lg, monkeypatch, capsys):
    """`lg status <id> ledger` is what README.md and the skill teach. It keeps
    printing JSON, and it asks only the query it was given."""
    handle = FakeHandle("run-toy-ab12cd", {"ledger": LEDGER})
    assert run_status(lg, monkeypatch, FakeClient({"run-toy-ab12cd": handle}),
                      "run-toy-ab12cd", "ledger") == 0
    assert json.loads(capsys.readouterr().out) == LEDGER
    assert handle.queried == ["ledger"]


def test_an_unknown_id_is_retried_as_a_slug(lg, monkeypatch, capsys):
    """Only Temporal saying the id does not exist turns the argument into a slug."""
    client = FakeClient({"run-toy-ab12cd": FakeHandle("run-toy-ab12cd", {"ledger": LEDGER})},
                        listed=[("run-toy-ab12cd", at(1))])
    code = run_status(lg, monkeypatch, client, "toy")
    out, err = capsys.readouterr()
    assert (code, out, err) == (0, lg.format_status(LEDGER), "")
    assert client.list_queries == ['WorkflowType = "LoopGraphRun"']


def test_runs_form_resolves(lg, monkeypatch, capsys):
    """`runs/toy/` is what tab completion hands the owner."""
    client = FakeClient({"run-toy-ab12cd": FakeHandle("run-toy-ab12cd", {"ledger": LEDGER})},
                        listed=[("run-toy-ab12cd", at(1))])
    assert run_status(lg, monkeypatch, client, "runs/toy/") == 0
    assert capsys.readouterr().out == lg.format_status(LEDGER)


@pytest.mark.parametrize("flag", [[], ["--json"]])
def test_two_matches_use_the_newest_and_say_so_in_both_modes(lg, monkeypatch, capsys, flag):
    """The notice goes to stderr in both modes, so `lg status toy --json | jq`
    still gets nothing but JSON."""
    client = FakeClient({"run-toy-ef34gh": FakeHandle("run-toy-ef34gh", {"ledger": LEDGER})},
                        listed=[("run-toy-ab12cd", at(1)), ("run-toy-ef34gh", at(9))])
    assert run_status(lg, monkeypatch, client, "toy", *flag) == 0
    out, err = capsys.readouterr()
    assert err == "using run-toy-ef34gh (newest of 2 for toy)\n"
    if flag:
        assert json.loads(out) == LEDGER
    else:
        assert out == lg.format_status(LEDGER)


def test_no_match_names_the_prefix_and_exits_1(lg, monkeypatch, capsys):
    """The line names the prefix it looked for, because the usual cause is a
    typo in the slug and the prefix is what shows it."""
    client = FakeClient(listed=[("run-other-ab12cd", at(1))])
    code = run_status(lg, monkeypatch, client, "runs/toy/")
    out, err = capsys.readouterr()
    assert (code, out) == (1, "")
    assert err == "no workflow for toy; looked for ids starting run-toy-\n"


def test_a_closed_run_whose_ledger_query_fails_uses_the_result(lg, monkeypatch, capsys):
    """A query on a closed run replays its history against current workflow
    code; one written by older code diverges and the query fails. The ledger of
    a finished run IS its return value — the same fallback ui._ledger makes."""
    handle = FakeHandle("run-toy-ab12cd", {"ledger": DRIFTED}, result=LEDGER)
    assert run_status(lg, monkeypatch, FakeClient({"run-toy-ab12cd": handle}),
                      "run-toy-ab12cd") == 0
    assert capsys.readouterr().out == lg.format_status(LEDGER)
    assert handle.awaited_result is True


def test_a_running_run_whose_ledger_query_fails_does_not_wait_on_result(lg, monkeypatch, capsys):
    """result() blocks until the workflow ends, so on a run that is waiting on
    its card this would hold the owner's terminal for as long as the card is up."""
    handle = FakeHandle("run-toy-ab12cd", {"ledger": DRIFTED},
                        status=WorkflowExecutionStatus.RUNNING, result=LEDGER)
    code = run_status(lg, monkeypatch, FakeClient({"run-toy-ab12cd": handle}), "run-toy-ab12cd")
    out, err = capsys.readouterr()
    assert (code, out, err) == (
        1, "", "[TMPRL1100] Nondeterminism error: activity type mismatch\n")
    assert handle.awaited_result is False


def test_an_unknown_status_is_treated_as_running(lg, monkeypatch, capsys):
    """temporalio types the status as optional and leaves it None when Temporal
    reports none. Unknown counts as running, the way ui._still_running reads it:
    a skipped fallback prints one line, a wrong one hangs the terminal."""
    handle = FakeHandle("run-toy-ab12cd", {"ledger": DRIFTED}, status=None, result=LEDGER)
    code = run_status(lg, monkeypatch, FakeClient({"run-toy-ab12cd": handle}), "run-toy-ab12cd")
    out, err = capsys.readouterr()
    assert (code, out, err) == (
        1, "", "[TMPRL1100] Nondeterminism error: activity type mismatch\n")
    assert handle.awaited_result is False


def test_an_unanswerable_positional_query_prints_one_line_and_exits_1(lg, monkeypatch, capsys):
    """`lg status <id> status` on a LoopGraphRun, which declares only `ledger`.
    Any query name can be typed, so a wrong one has to answer in one line —
    not the traceback it used to be."""
    handle = FakeHandle("run-toy-ab12cd", {"ledger": LEDGER}, result=LEDGER)
    code = run_status(lg, monkeypatch, FakeClient({"run-toy-ab12cd": handle}),
                      "run-toy-ab12cd", "status")
    out, err = capsys.readouterr()
    assert (code, out, err) == (1, "", NO_HANDLER + "\n")
    assert handle.awaited_result is False


# ---------- what the help and the docs teach ----------

def help_text(lg, monkeypatch, capsys, *argv) -> str:
    """What argparse prints for `lg [...] --help`, at a width this test picks.

    argparse rewraps its output to the terminal, and textwrap breaks on hyphens,
    so in a 40-column window `<workflow-id|run-dir>` arrives as three pieces on
    three lines. The width is pinned here because these assertions are about the
    wording, and a narrow terminal is not a reason for them to go red.
    """
    monkeypatch.setenv("COLUMNS", "100")
    monkeypatch.setattr(sys, "argv", ["lg", *argv, "--help"])
    with pytest.raises(SystemExit):
        lg.main()
    return capsys.readouterr().out


def test_the_status_help_offers_a_run_directory(lg, monkeypatch, capsys):
    """A slug is no use to an owner who has no way of learning it works, and
    `lg status --help` is where they look. The argument is called `arg` there,
    because `workflow_id` reads as a refusal to take anything else — so it has
    to carry a help string naming both forms, and the subcommand line has to
    name both too. That string cannot come from the subcommand line itself:
    argparse keeps a subparser's `help=` on the parent and prints it only under
    `lg --help`."""
    assert "<workflow-id|run-dir>" in help_text(lg, monkeypatch, capsys)
    status = help_text(lg, monkeypatch, capsys, "status")
    assert status.splitlines()[0] == "usage: lg status [-h] [--json] arg [query]"
    assert "workflow_id" not in status
    assert "a workflow id, or a run directory like runs/2026-09-05-feature" in status


@pytest.mark.parametrize("doc", ["README.md", "skills/loopgraph/SKILL.md"])
def test_the_docs_show_the_slug_form(doc):
    """The skill is what agents in other projects read, so a form documented
    nowhere is a form nobody types. The id form stays in both files: it is what
    they taught before, and it still works."""
    text = (ROOT / doc).read_text()
    assert "lg status runs/" in text, f"{doc} never shows the run-directory form"
    assert "lg status <workflow-id> ledger" in text, f"{doc} dropped the id form"
