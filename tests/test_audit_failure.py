"""An audit that never returns a verdict must not take the whole run with it.

Every other way a round can fail parks the item and tells the owner: gates that
stay red, a checkpoint that refuses, a `learn` step that blows up. The audit call
was the one that could not. When the supervisor blew its Temporal deadline twice,
the ActivityError came straight out of `_run_item`, out of `run`, and failed the
workflow. Nothing wrote the ledger on the way out, so the `ledger` query — which
Temporal still answers for a closed workflow — went on returning `running` for a
run that had been dead for half an hour, and no card ever reached the owner.

Which deadline blew is the whole diagnosis, so the reason has to name it. A
HEARTBEAT timeout means the model session went quiet: `stream_query` heartbeats
on every streamed message, so silence is the only thing that stops the pings.
START_TO_CLOSE means the opposite — the auditor talked the entire time and never
closed with a verdict packet. Those want different fixes.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

import pytest
from temporalio.exceptions import ActivityError, ApplicationError, TimeoutType
from temporalio.exceptions import TimeoutError as TemporalTimeoutError

ROOT = Path(__file__).resolve().parent.parent


def _lg():
    """`lg` has no .py extension, so it loads by path."""
    loader = SourceFileLoader("lg_cli_audit", str(ROOT / "lg"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("lg_cli_audit", loader))
    loader.exec_module(mod)
    return mod


def timed_out(kind: TimeoutType) -> ActivityError:
    """The exception Temporal really raises out of `execute_activity` when an
    activity runs out of time: an ActivityError whose message says nothing, with
    the informative half on `__cause__`."""
    err = ActivityError(
        "Activity task failed", scheduled_event_id=1, started_event_id=2,
        identity="worker", activity_type="audit", activity_id="1", retry_state=None)
    err.__cause__ = TemporalTimeoutError(
        "activity timeout", type=kind, last_heartbeat_details=[])
    return err


GREEN_ROUND = {
    "status": "green", "attempts": 1, "claims": ["did the thing"],
    "files": ["a.py"], "summary": "did the thing", "worktree": "/app/runs/x/worktrees/ab12cd",
    "branch": "lg-x-ab12cd", "base_branch": "main", "self_committed": False,
}


class _FakeWorkflow:
    """Stands in for the `workflow` module inside `workflows.run`.

    Same trick as tests/test_visibility.py: every Temporal call goes through that
    one module-level name, so replacing it runs the real method in a plain event
    loop. `fails` names the activity that should raise and what it raises with.
    """

    def __init__(self, fails=None, items=None) -> None:
        self._fails = fails or {}
        self._items = items if items is not None else ["the only item"]
        self.cards: list[list] = []      # the args of every send_card
        self.logger = logging.getLogger("fake-workflow")

    def info(self):
        return SimpleNamespace(workflow_id="run-x-ab12cd")

    async def execute_activity(self, fn, args=None, **kwargs):
        name = fn.__name__
        if name in self._fails:
            raise self._fails[name]
        if name == "run_baseline":
            return "base1234"
        if name == "load_work_items":
            return list(self._items)
        if name == "execute_round":
            return dict(GREEN_ROUND)
        if name == "telegram_configured":
            return True
        if name == "send_card":
            self.cards.append(list(args or []))
        return {"sent": True}

    async def wait_condition(self, predicate):  # no card in these tests waits
        raise AssertionError("a failed audit must not hold the run at a card")


def _drive(fails, items=None, method="run"):
    """Run one LoopGraphRun method against the fake, restoring the module after.

    `run` needs no setup: it loads its own items through the fake. `_run_item` is
    called mid-run, so the items it counts have to be there already.
    """
    import workflows.run as run_module

    names = items or ["the only item"]
    wf = run_module.LoopGraphRun()
    fake = _FakeWorkflow(fails, names)
    original = run_module.workflow
    run_module.workflow = fake
    try:
        if method == "run":
            return asyncio.run(wf.run("runs/x", "/projects/x", names[0])), fake
        wf._ledger["items"] = [{"n": n, "item": it, "status": "pending"}
                               for n, it in enumerate(names, start=1)]
        return asyncio.run(wf._run_item("runs/x", "/projects/x", names[0], 1, None)), wf
    finally:
        run_module.workflow = original


# ---------- the item is parked, not the run killed ----------

@pytest.mark.parametrize("kind", [TimeoutType.START_TO_CLOSE, TimeoutType.HEARTBEAT])
def test_an_audit_that_runs_out_of_time_parks_the_item(kind):
    outcome, _ = _drive({"audit": timed_out(kind)}, method="_run_item")
    assert outcome["status"] == "parked"
    assert outcome["reason"].startswith("audit failed:")


def test_the_park_reason_names_which_deadline_blew():
    """Stall and runaway are opposite bugs and this string is what tells them
    apart, on the card and in `lg status`. "audit failed" alone sends the next
    person hunting the wrong one."""
    stalled, _ = _drive({"audit": timed_out(TimeoutType.HEARTBEAT)}, method="_run_item")
    ran_away, _ = _drive({"audit": timed_out(TimeoutType.START_TO_CLOSE)},
                         method="_run_item")
    assert "HEARTBEAT" in stalled["reason"]
    assert "START_TO_CLOSE" in ran_away["reason"]
    assert stalled["reason"] != ran_away["reason"]


def test_an_audit_that_raises_its_own_error_parks_too():
    """Not only timeouts. A missing brief.md or an SDK that refuses to start is
    the same news to the owner: this item could not be judged."""
    err = ActivityError("Activity task failed", scheduled_event_id=1, started_event_id=2,
                        identity="w", activity_type="audit", activity_id="1", retry_state=None)
    err.__cause__ = ApplicationError("brief.md is missing", type="FileNotFoundError")
    outcome, _ = _drive({"audit": err}, method="_run_item")
    assert outcome["status"] == "parked"
    assert "brief.md is missing" in outcome["reason"]


def test_the_round_stops_claiming_the_audit_is_still_running():
    """`lg status` reads a green round with no verdict as "audit running". After
    the audit has died that line is a lie, and it is the line the owner checks.

    The whole printout, because the two halves carry different halves of the
    news: the round line says the audit failed, the item line underneath says
    which deadline blew."""
    ledger, _ = _drive({"audit": timed_out(TimeoutType.START_TO_CLOSE)})
    out = _lg().format_status(ledger)
    assert "audit running" not in out
    assert "  item 1 round 1 audit failed" in out.splitlines()
    assert "START_TO_CLOSE" in out


# ---------- what the owner is left holding ----------

def test_the_ledger_stops_saying_running():
    """The reported symptom. Temporal answers the `ledger` query on a closed
    workflow, so a run that failed out of `_run_item` kept answering `running`
    to `lg status` and to the dashboard's panel, while the run list beside it
    read `failed` off Temporal. Two answers, same page."""
    ledger, _ = _drive({"audit": timed_out(TimeoutType.START_TO_CLOSE)})
    assert ledger["status"] == "stopped"
    assert ledger["items"][0]["status"] == "parked"
    assert "audit failed" in ledger["items"][0]["reason"]


def test_the_owner_gets_a_card():
    """A dead run that says nothing is the part that cost the afternoon."""
    _, fake = _drive({"audit": timed_out(TimeoutType.HEARTBEAT)})
    assert fake.cards, "nothing was sent to the owner"
    assert "audit failed" in " ".join(str(c) for c in fake.cards)


def test_one_bad_audit_does_not_park_the_items_after_it():
    """The audit fails on every item here, so all three park and the run stops
    having tried all three. An exception stopped at the first."""
    ledger, _ = _drive({"audit": timed_out(TimeoutType.START_TO_CLOSE)},
                       items=["one", "two", "three"])
    assert [e["status"] for e in ledger["items"]] == ["parked"] * 3
    assert ledger["reason"] == "every work item was parked"


# ---------- the reason string itself ----------

def test_audit_failure_reason_digs_out_the_cause():
    from workflows.run import audit_failure_reason

    reason = audit_failure_reason(timed_out(TimeoutType.HEARTBEAT))
    assert "Activity task failed" in reason
    assert "HEARTBEAT" in reason


def test_audit_failure_reason_is_one_bounded_line():
    """It lands in a Telegram card and in the ledger. A traceback in either is
    the same unreadable wall the park reasons already avoid."""
    from workflows.run import audit_failure_reason

    err = ActivityError("boom", scheduled_event_id=1, started_event_id=2, identity="w",
                        activity_type="audit", activity_id="1", retry_state=None)
    err.__cause__ = ApplicationError("line one\nline two\n" + "x" * 900)
    reason = audit_failure_reason(err)
    assert "\n" not in reason
    assert len(reason) <= 300


def test_audit_failure_reason_survives_a_bare_exception():
    from workflows.run import audit_failure_reason

    assert "RuntimeError" in audit_failure_reason(RuntimeError("no worker"))
