"""An activity that raises must never take the run with it.

A dead audit and a dead detector pass were closed in earlier phases. A live sweep
run then died on the two that were left: the checkpoint refused a path the
executor had already `git rm`'d, the ActivityError came out of `_run_item`, out
of `run`, and failed the workflow. Nothing wrote the ledger on the way out, so
the `ledger` query — which Temporal answers on a failed workflow by replaying the
state at the failure — went on saying `running`, `lg status` and the dashboard
both believed it, and no card ever reached the owner.

So a dead executor round or a dead checkpoint parks its item and the run carries
on, and any other dead activity stops the run with a reason and a note. The
ledger comes back either way and the workflow never raises.

A run driven to the end here reads `held`, not `merge-ready`: the fake answers
every card with `B`. Assert on the cards.
"""

from __future__ import annotations

import asyncio

import pytest

from workflow_fake import (COMMITTED, DEFAULT_CONFIG, GREEN_ROUND, ScriptedWorkflow,
                           drive, drive_item)

SWEEP = {"yield_floor": 0, "deadline_seconds": None, "max_items": 40,
         "detectors": [{"name": "vulture", "cmd": "vulture .", "timeout": 600}]}


def _sweep_config() -> dict:
    return {"convergence": dict(DEFAULT_CONFIG["convergence"]), "sweep": dict(SWEEP)}


def _pass(count: int) -> dict:
    """A `discover` result over one detector that counted `count`."""
    det = {"name": "vulture", "cmd": "vulture .", "exit_code": 0, "count": count,
           "lines": [f"a{i}.py:{i}: unused" for i in range(1, min(count, 3) + 1)],
           "note": "", "stderr_tail": ""}
    return {"complete": True, "total": count, "detectors": [det]}


def _boom() -> RuntimeError:
    """What a Temporal activity failure looks like from inside the workflow, cut
    to the part these tests read back off the card."""
    return RuntimeError("boom")


class _DiesOnce(ScriptedWorkflow):
    """A fake that kills one activity on its first call and answers the rest.

    `fails` kills every call, and what these tests need is a run that carries on
    past the failure to the item after it.
    """

    def __init__(self, activity_name: str, **kw) -> None:
        super().__init__(**kw)
        self._dying = activity_name
        self._died = False

    async def execute_activity(self, fn, args=None, **kwargs):
        result = await super().execute_activity(fn, args=args, **kwargs)
        if fn.__name__ == self._dying and not self._died:
            self._died = True
            raise _boom()
        return result


def _cards(fake) -> list[str]:
    """The kind of every card sent, in order."""
    return [c[0] for c in fake.cards]


# ---------- a dead checkpoint parks the item ----------

def test_a_dead_checkpoint_parks_the_item_and_the_run_carries_on():
    """The live failure. Item 1's work was already accepted and its commit is
    lost, but items 2 onward are not, and the owner still gets a merge card for
    what did land."""
    fake = _DiesOnce("checkpoint", items=["one", "two"])
    ledger = drive(fake)
    first, second = ledger["items"]
    assert first["status"] == "parked"
    assert first["reason"].startswith("checkpoint failed:")
    assert "RuntimeError: boom" in first["reason"]
    assert second["status"] == "done" and second["commit"] == "c1"
    assert _cards(fake) == ["parked", "merge-ready"]
    assert ledger["owner_decision"] == "B", "the run reached the merge card"


def test_the_round_keeps_the_verdict_it_got_and_names_the_dead_checkpoint():
    """The auditor did accept, so the round entry says accept: rewriting that
    would blame the supervisor for a failure it had no part in. What the entry
    gains is the reason the accepted work never made it to a commit."""
    fake = _DiesOnce("checkpoint")
    ledger = drive(fake)
    entry = ledger["rounds"][0]
    assert entry["verdict"] == "accept"
    assert "RuntimeError: boom" in entry["checkpoint_failed"]


def test_a_dead_checkpoint_in_a_sweep_parks_the_item_and_its_candidates_ride_on():
    """A sweep harvests the executor's candidates off a parked item too: it read
    the code either way. The placeholder a park returns has to carry them, or the
    next item loses the work this one found by hand."""
    fake = _DiesOnce("checkpoint", config=_sweep_config(),
                     passes=[_pass(5), _pass(5), _pass(0)],
                     rounds=[dict(GREEN_ROUND, candidates=["x", "y"]), GREEN_ROUND])
    ledger = drive(fake)
    first, second = ledger["items"]
    assert first["status"] == "parked"
    assert first["reason"].startswith("checkpoint failed:")
    assert [line for line in second["item"].splitlines() if line in ("x", "y")] == ["x", "y"]
    assert [p["pass"] for p in ledger["sweep"]["passes"]] == [1, 2, 3]
    assert ledger["sweep"]["ended"] == "converged: nothing reported"


# ---------- a dead executor round parks the item ----------

def test_a_dead_executor_round_parks_the_item_and_the_run_carries_on():
    """No result to audit, so there is nothing to judge and nothing to commit —
    the same news to the owner as a round whose gates stayed red."""
    fake = _DiesOnce("execute_round", items=["one", "two"])
    ledger = drive(fake)
    first, second = ledger["items"]
    assert first["status"] == "parked"
    assert first["reason"].startswith("executor failed:")
    assert second["status"] == "done"
    assert _cards(fake) == ["parked", "merge-ready"]


def test_a_dead_executor_round_still_writes_its_round_entry():
    """A round that ran and left no entry is a round `lg status`, the dashboard
    and the merge card cannot see at all."""
    fake = _DiesOnce("execute_round")
    ledger = drive(fake)
    entry = ledger["rounds"][0]
    assert entry["status"] == "failed"
    assert (entry["item_no"], entry["round"], entry["attempts"]) == (1, 1, 0)
    assert (entry["claims"], entry["files"]) == ([], [])
    assert entry["verdict"] == "executor failed"
    assert "RuntimeError: boom" in entry["verdict_reasons"][0]


def test_no_audit_runs_for_a_round_that_never_produced_one():
    """There is no write set to judge. Scheduling the audit anyway would ask the
    supervisor about the previous round's work."""
    fake = _DiesOnce("execute_round")
    drive(fake)
    assert "audit" not in [name for name, _args, _kwargs in fake.scheduled]


# ---------- anything else stops the run, with a note ----------

def test_a_dead_activity_stops_the_run_instead_of_killing_the_workflow():
    """`run_baseline` here, but it stands for every activity outside an item:
    the work items, the merge, the cards. The ledger comes back rather than the
    exception, because Temporal answers the `ledger` query on a failed workflow
    from the state at the failure — a raise leaves `lg status`, the dashboard and
    the owner all believing the run is still alive."""
    fake = ScriptedWorkflow(fails={"run_baseline": _boom()})
    ledger = drive(fake)
    assert ledger["status"] == "stopped"
    assert ledger["reason"].startswith("engine failure:")
    assert "RuntimeError: boom" in ledger["reason"]
    assert _cards(fake) == ["run stopped"]
    assert fake.cards[0][3].startswith("why: engine failure:")


def test_a_broken_run_yaml_keeps_its_own_reason():
    """The config failure is reported in run.yaml's own words and has its own
    catch, above the one this phase added. Wrapping it as an engine failure would
    hide the line naming the key the owner has to edit."""
    fake = ScriptedWorkflow(fails={"load_run_config": _boom()})
    ledger = drive(fake)
    assert ledger["status"] == "stopped"
    assert not ledger["reason"].startswith("engine failure:")


def test_an_activity_that_dies_mid_sweep_ends_the_sweep_with_the_same_reason():
    """`lg status`, the dashboard and the merge card all read why a sweep ended
    off one key. A sweep still running when the engine broke has no ending of
    its own, and leaving that key empty would print as a sweep still deciding.
    The break here is the park note: the item's checkpoint is refused, the note
    about it dies, and the sweep had not ended."""
    refused = {"committed": False, "reason": "net lines +3 exceed the cap of 0"}
    fake = ScriptedWorkflow(config=_sweep_config(), passes=[_pass(5)], checkpoints=[refused],
                            fails={"send_card": _boom()})
    ledger = drive(fake)
    assert ledger["status"] == "stopped"
    assert ledger["reason"].startswith("engine failure:")
    assert ledger["items"][0]["status"] == "parked"
    assert ledger["sweep"]["ended"] == ledger["reason"]


def test_a_sweep_that_already_ended_keeps_its_own_reason_when_the_card_dies():
    """The detectors stopped reporting, and then the merge card died. The sweep's
    ending is the detectors' verdict, not the sender's, and `lg status` prints
    it on its own line: overwriting it would say the engine broke the sweep."""
    fake = ScriptedWorkflow(config=_sweep_config(), passes=[_pass(5), _pass(0)],
                            fails={"send_card": _boom()})
    ledger = drive(fake)
    assert ledger["status"] == "stopped"
    assert ledger["reason"].startswith("engine failure:")
    assert ledger["sweep"]["ended"] == "converged: nothing reported"


def test_a_dead_card_sender_does_not_turn_a_written_ledger_into_a_raised_one():
    """The stopped note is best effort and runs last. Letting it raise would undo
    the whole point: the ledger is written, and then the workflow fails anyway
    and the query answers `running` off the state before it."""
    fake = ScriptedWorkflow(fails={"telegram_configured": _boom()})
    ledger = drive(fake)
    assert ledger["status"] == "stopped"
    assert ledger["reason"].startswith("engine failure:")
    assert fake.cards == [], "the sender is dead; nothing went out"


# ---------- a cancellation is not a failure ----------

def test_a_cancelled_run_is_not_swallowed_as_an_engine_failure():
    """Temporal cancels a workflow by raising `asyncio.CancelledError` inside it,
    and that is a BaseException on purpose: catching it would leave the run
    reporting itself stopped while the worker went on waiting for it to unwind."""
    fake = ScriptedWorkflow(fails={"run_baseline": asyncio.CancelledError()})
    with pytest.raises(asyncio.CancelledError):
        drive(fake)


def test_a_cancelled_item_is_not_swallowed_as_a_park():
    fake = ScriptedWorkflow(fails={"checkpoint": asyncio.CancelledError()},
                            checkpoints=[COMMITTED])
    with pytest.raises(asyncio.CancelledError):
        drive_item(fake)
