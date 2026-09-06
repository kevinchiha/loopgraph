"""Forced convergence, and the run.yaml the workflow reads before anything else.

The tests that drive a run swap the `workflow` module for the ScriptedWorkflow in
`tests/workflow_fake.py`, so they say what a run would actually do rather than
what the file reads like. A driven run ends `held`, not `merge-ready`: the fake
answers the merge card with `B`.
"""

from __future__ import annotations

import pytest
from temporalio.exceptions import ActivityError, ApplicationError

from workflow_fake import GREEN_ROUND, ScriptedWorkflow, drive, drive_item
from workflows.run import build_convergence_item

BAD_YAML = "run.yaml: unknown key 'yeild_floor' under sweep"


def _names(fake) -> list[str]:
    """The activity names the run scheduled, in order."""
    return [name for name, _args, _kwargs in fake.scheduled]


def _call(fake, name: str) -> tuple:
    """The first scheduled call with that name: (name, args, kwargs)."""
    return next(c for c in fake.scheduled if c[0] == name)


def _config_failure(message: str = BAD_YAML) -> ActivityError:
    """What Temporal really raises when the config activity raises ValueError: an
    ActivityError whose own message says nothing, with the useful half on
    `__cause__`."""
    err = ActivityError("Activity task failed", scheduled_event_id=1, started_event_id=2,
                        identity="worker", activity_type="load_run_config",
                        activity_id="1", retry_state=None)
    err.__cause__ = ApplicationError(message, type="ValueError")
    return err


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


# ---------- run.yaml is read before anything else happens ----------

def test_config_is_the_first_activity_and_runs_once():
    """AC-3. Reading the config after the baseline would mean a misspelt knob is
    found only once the run has started work, and a retried config activity would
    ask the same broken file three times for the same answer."""
    fake = ScriptedWorkflow()
    drive(fake)
    assert _names(fake)[:2] == ["load_run_config", "run_baseline"]
    assert fake.scheduled[0][2]["retry_policy"].maximum_attempts == 1


def test_a_bad_run_yaml_stops_the_run_before_anything_runs():
    """AC-3. A `yeild_floor` that quietly took the default is the failure the file
    exists to prevent, and the owner would only find out four days later."""
    fake = ScriptedWorkflow(fails={"load_run_config": _config_failure()})
    ledger = drive(fake)
    assert ledger["status"] == "stopped"
    assert ledger["reason"] == BAD_YAML
    assert _names(fake) == ["load_run_config", "telegram_configured", "send_card"]
    kind, _wf_id, _run_dir, text = fake.cards[0][:4]
    assert kind == "run stopped"
    assert text.startswith("why: run.yaml:")
    assert "item " not in text, "no item ran, so there is nowhere to speak from"


def test_config_error_reason_finds_the_run_yaml_line():
    """The ActivityError on top says "Activity task failed", which is no help to
    anyone editing a file. The line naming the key is on `__cause__`."""
    from workflows.run import config_error_reason

    assert config_error_reason(_config_failure()) == BAD_YAML
    # Nothing in the chain names run.yaml, so the owner gets the whole chain, the
    # way a failed audit is reported.
    assert "RuntimeError" in config_error_reason(RuntimeError("no worker"))


# ---------- every item knows what kind of item it is ----------

def test_every_item_entry_is_born_with_a_kind():
    """AC-11. `lg status` and the dashboard read the kind off the entry, and an
    entry that only gets one when it finishes has none while it is running."""
    ledger = drive(ScriptedWorkflow(items=["one", "two", "three"]))
    assert [e["kind"] for e in ledger["items"]] == ["brief"] * 3


@pytest.mark.parametrize("kind, max_net", [("sweep", 0), ("convergence", 0), ("brief", None)])
def test_run_item_passes_kind_to_the_audit_and_the_cap_to_the_checkpoint(kind, max_net):
    """AC-7 and AC-36. The supervisor knows only what the prompt hands it, so an
    item it is not told is a removal item gets judged as a feature. The cap is the
    workflow's to set: checkpoint stays ignorant of item kinds."""
    fake = ScriptedWorkflow()
    drive_item(fake, kind=kind)
    assert _call(fake, "audit")[1][6] == kind
    assert _call(fake, "checkpoint")[1][6] == max_net


def test_a_committed_checkpoint_records_net_on_the_round():
    """AC-7. Convergence fires on lines added since the last removal item, and the
    round entry is where that number is kept."""
    _outcome, wf = drive_item(ScriptedWorkflow())
    assert wf._ledger["rounds"][-1]["net"] == 2


# ---------- the one item allowed to finish having changed nothing ----------

def test_a_convergence_item_with_nothing_to_remove_skips_checkpoint_and_learn():
    """AC-10. An accepted convergence item that wrote no file is the answer, not a
    refusal: there was genuinely nothing left to remove. Sending it to checkpoint
    would park it on `empty write set` and end a clean run as a failed one."""
    fake = ScriptedWorkflow(rounds=[dict(GREEN_ROUND, files=[])])
    outcome, _wf = drive_item(fake, kind="convergence")
    assert outcome["status"] == "accepted"
    assert outcome["checkpoint"] is None
    assert outcome["result"]["files"] == []
    assert "checkpoint" not in _names(fake)
    assert "learn" not in _names(fake)


@pytest.mark.parametrize("kind", ["brief", "sweep"])
def test_an_empty_brief_or_sweep_item_still_parks_at_checkpoint(kind):
    """Only a convergence item may finish empty. A brief item that wrote no file
    did not do its job, and a sweep item that removed nothing left every candidate
    where it was."""
    fake = ScriptedWorkflow(rounds=[dict(GREEN_ROUND, files=[])],
                            checkpoints=[{"committed": False, "reason": "empty write set"}])
    outcome, _wf = drive_item(fake, kind=kind)
    assert outcome == {"status": "parked", "reason": "checkpoint refused: empty write set"}
    assert "checkpoint" in _names(fake)


def test_a_net_cap_refusal_parks_with_the_checkpoint_reason():
    """AC-6. The cap is checked inside checkpoint, and the workflow parks on it
    through the refusal path it already had, so the owner reads the real numbers
    rather than "checkpoint refused"."""
    refusal = {"committed": False, "reason": "net lines +37 exceed the cap of 0",
               "added": 40, "deleted": 3, "net": 37}
    outcome, _wf = drive_item(ScriptedWorkflow(checkpoints=[refusal]), kind="convergence")
    assert outcome["status"] == "parked"
    assert outcome["reason"] == "checkpoint refused: net lines +37 exceed the cap of 0"


def test_a_driven_run_ends_held():
    """Why every driven test reads the cards and never the final status: the fake
    answers the merge card with `B`, and `_owner_card` writes `held` over
    `merge-ready` when the owner keeps the branch."""
    fake = ScriptedWorkflow()
    ledger = drive(fake)
    assert [c[0] for c in fake.cards] == ["merge-ready"]
    assert ledger["status"] == "held"
