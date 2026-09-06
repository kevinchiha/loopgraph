"""Forced convergence, and the run.yaml the workflow reads before anything else.

The tests that drive a run swap the `workflow` module for the ScriptedWorkflow in
`tests/workflow_fake.py`, so they say what a run would actually do rather than
what the file reads like. A driven run ends `held`, not `merge-ready`: the fake
answers the merge card with `B`.
"""

from __future__ import annotations

import pytest
from temporalio.exceptions import ActivityError, ApplicationError

from workflow_fake import (COMMITTED, DEFAULT_CONFIG, GREEN_ROUND, ScriptedWorkflow,
                           drive, drive_item)
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
    assert outcome["status"] == "parked"
    assert outcome["reason"] == "checkpoint refused: empty write set"
    # Every outcome carries its round result: a sweep harvests the executor's
    # candidates off a parked item as well as an accepted one.
    assert outcome["result"]["files"] == []
    # And nothing else: a parked outcome that grew a `checkpoint` key would be
    # read by `run` as an item that committed something.
    assert set(outcome) == {"status", "reason", "result"}
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


# ---------- the engine spends an item removing what the last ones added ----------

def _config(**knobs) -> dict:
    """The defaults with the convergence knobs a test cares about changed."""
    return {"convergence": {**DEFAULT_CONFIG["convergence"], **knobs}, "sweep": None}


def _cp(files: list[str], net: int = 2, commit: str = "c1") -> dict:
    """A committed checkpoint that wrote those files."""
    return dict(COMMITTED, files=files, net=net, commit=commit)


def _kinds(ledger: dict) -> list[str]:
    """What kind of item each ledger entry is, in execution order."""
    return [e["kind"] for e in ledger["items"]]


EMPTY_REFUSAL = {"committed": False, "reason": "empty write set"}

CAP_REFUSAL = {"committed": False, "reason": "net lines +37 exceed the cap of 0",
               "added": 40, "deleted": 3, "net": 37}


def test_five_accepted_items_run_six():
    """AC-8 and AC-9. Five accepted items trip `every_items`, and the item the
    engine writes for itself lists everything those five wrote, sorted, so the
    executor is looking at the run's own growth and not at the repo."""
    fake = ScriptedWorkflow(items=["one", "two", "three", "four", "five"],
                            checkpoints=[_cp(["e.py"]), _cp(["d.py"]), _cp(["c.py"]),
                                         _cp(["b.py"]), _cp(["a.py"])])
    ledger = drive(fake)
    assert _kinds(ledger) == ["brief"] * 5 + ["convergence"]
    assert [e["n"] for e in ledger["items"]] == [1, 2, 3, 4, 5, 6]
    assert ledger["items"][5]["item"] == build_convergence_item(
        ["a.py", "b.py", "c.py", "d.py", "e.py"])


def test_net_lines_trigger_between_items():
    """AC-8. On a short brief the counter that fires is the size of what it added:
    300 then 150 crosses 400 with a brief item still to come, and the removal pass
    goes in before it."""
    fake = ScriptedWorkflow(config=_config(every_items=99, net_lines=400),
                            items=["one", "two", "three"],
                            checkpoints=[_cp(["a.py"], net=300), _cp(["b.py"], net=150)])
    ledger = drive(fake)
    assert _kinds(ledger) == ["brief", "brief", "convergence", "brief"]
    # The brief item after it moved up, so `n` is still execution order.
    assert [e["n"] for e in ledger["items"]] == [1, 2, 3, 4]
    assert ledger["items"][3]["item"] == "three"


def test_a_parked_brief_item_moves_no_counter():
    """AC-8. A parked item committed nothing, so it added none of the lines the
    removal pass exists to take back out."""
    fake = ScriptedWorkflow(config=_config(every_items=2),
                            items=["one", "two", "three", "four"],
                            checkpoints=[_cp(["a.py"]), EMPTY_REFUSAL, _cp(["b.py"])])
    ledger = drive(fake)
    assert ledger["items"][1]["status"] == "parked"
    assert _kinds(ledger) == ["brief", "brief", "brief", "convergence", "brief"]
    assert ledger["items"][3]["item"] == build_convergence_item(["a.py", "b.py"])


def test_counters_and_touched_reset_after_a_convergence_item():
    """AC-8. Both counters and the write set start again after a removal pass, or
    the next one fires an item early and is handed files that were already
    cleaned."""
    fake = ScriptedWorkflow(config=_config(every_items=2),
                            items=["one", "two", "three", "four"],
                            checkpoints=[_cp(["a.py"]), _cp(["b.py"]), _cp(["gone.py"]),
                                         _cp(["c.py"]), _cp(["d.py"])])
    ledger = drive(fake)
    assert _kinds(ledger) == ["brief", "brief", "convergence",
                              "brief", "brief", "convergence"]
    assert [e["n"] for e in ledger["items"]] == [1, 2, 3, 4, 5, 6]
    assert ledger["items"][5]["item"] == build_convergence_item(["c.py", "d.py"])


def test_a_parked_convergence_item_resets_the_counters_too():
    """AC-8. The removal pass happened, whatever came of it. Counting on from
    where it left off would inject the next one straight away and every item after
    it would be a convergence item."""
    fake = ScriptedWorkflow(config=_config(every_items=2), items=["one", "two", "three"],
                            checkpoints=[_cp(["a.py"]), _cp(["b.py"]), CAP_REFUSAL,
                                         _cp(["c.py"])])
    ledger = drive(fake)
    assert _kinds(ledger) == ["brief", "brief", "convergence", "brief"]
    assert ledger["items"][2]["status"] == "parked"
    assert ledger["items"][2]["reason"] == \
        "checkpoint refused: net lines +37 exceed the cap of 0"


class _NoteDuringItem(ScriptedWorkflow):
    """A run with the owner sending one note while a chosen item is executing.

    That is how a reply really arrives: the dispatcher signals `decide` at
    whatever moment the owner types, and the run picks it up between items. The
    scripts here give every item one round, so counting `execute_round` calls
    counts items.
    """

    def __init__(self, during_item: int, note: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._during_item, self._note = during_item, note
        self._items_seen = 0

    async def execute_activity(self, fn, args=None, **kwargs):
        result = await super().execute_activity(fn, args=args, **kwargs)
        if fn.__name__ == "execute_round":
            self._items_seen += 1
            if self._items_seen == self._during_item:
                self.run.decide(self._note)
        return result


def test_a_note_that_lands_before_an_injection_reaches_the_next_brief_item():
    """AC-8. The park card tells the owner to reply with anything the next item
    should know, and the convergence item is not the item they were answering:
    it is the engine's own. Clearing the note after it spent the reply there and
    the brief item it was written for never heard a word of it."""
    note = "the API key is in .env.local"
    fake = _NoteDuringItem(2, note, config=_config(every_items=2),
                           items=["one", "two", "three"])
    ledger = drive(fake)
    assert _kinds(ledger) == ["brief", "brief", "convergence", "brief"]
    directives = [args[4] for name, args, _kwargs in fake.scheduled
                  if name == "execute_round"]
    assert note in (directives[2] or ""), "the convergence item hears it too"
    assert directives[3] == f"The owner sent this mid-run, after item 2: {note}"


def test_enabled_false_injects_nothing():
    """The escape hatch. A migration that legitimately adds 600 lines needs a way
    out that is not editing the engine."""
    ledger = drive(ScriptedWorkflow(config=_config(enabled=False),
                                    items=["one", "two", "three", "four", "five", "six"]))
    assert _kinds(ledger) == ["brief"] * 6


def test_nothing_to_remove_leaves_the_card_on_the_last_real_commit():
    """AC-10. The convergence item changed no file, so the commit the owner is
    offered has to be the last one that exists. Writing its `None` over the card's
    commit would offer a merge of nothing."""
    fake = ScriptedWorkflow(config=_config(every_items=1), items=["one"],
                            rounds=[GREEN_ROUND, dict(GREEN_ROUND, files=[])],
                            checkpoints=[_cp(["a.py"], commit="c1")])
    ledger = drive(fake)
    conv = ledger["items"][1]
    assert conv["kind"] == "convergence"
    assert (conv["status"], conv["commit"]) == ("done", None)
    assert conv["note"] == "nothing to remove"
    assert [c[0] for c in fake.cards] == ["merge-ready"]
    assert fake.cards[0][4] == "c1", "the merge card must name the last real commit"


def test_the_location_line_counts_the_injected_item():
    """AC-9. The injected item is in `items`, so every card after it says four.
    A note still saying "of 3" would be counting a list nobody has."""
    fake = ScriptedWorkflow(config=_config(every_items=2), items=["one", "two", "three"],
                            checkpoints=[_cp(["a.py"]), _cp(["b.py"]), _cp(["c.py"]),
                                         EMPTY_REFUSAL])
    ledger = drive(fake)
    assert _kinds(ledger) == ["brief", "brief", "convergence", "brief"]
    parked = next(c for c in fake.cards if c[0] == "parked")
    assert parked[3].startswith("item 4 of 4 parked")


def test_a_convergence_item_can_be_the_last_item():
    """The trigger is the counters, not the position: a five-item brief that added
    enough gets its removal pass after item 5, or the rule would skip exactly the
    run it exists for."""
    fake = ScriptedWorkflow(items=["one", "two", "three", "four", "five"])
    ledger = drive(fake)
    assert _kinds(ledger)[-1] == "convergence"
    assert _names(fake).count("execute_round") == 6, "the sixth item is the last one"
    assert [c[0] for c in fake.cards] == ["merge-ready"]


def test_an_all_parked_run_speaks_from_its_last_item():
    """AC-33, driven. Nothing was accepted, so the run ran out at the last item
    rather than stopping on one, and that is where the note speaks from."""
    fake = ScriptedWorkflow(items=["one", "two", "three"], checkpoints=[EMPTY_REFUSAL])
    ledger = drive(fake)
    assert ledger["reason"] == "every work item was parked"
    stopped = next(c for c in fake.cards if c[0] == "run stopped")
    assert stopped[3].startswith("item 3 of 3")
