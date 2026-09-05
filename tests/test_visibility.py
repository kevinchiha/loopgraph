import asyncio
import logging
from types import SimpleNamespace

from activities.notify import build_card_text, build_keyboard, cb_key, location_line
from activities.route import route_update, wf_from_card
from activities.stream import append_log, summarize_tool

CARD = "loopgraph: decision\nrun: runs/x\nworkflow: run-x\n\npick one"


def _method(name: str) -> str:
    """The source of one LoopGraphRun method, for the pins that read the file."""
    import inspect

    from workflows.run import LoopGraphRun
    return inspect.getsource(getattr(LoopGraphRun, name))


def _args(src: str) -> list[str]:
    """The elements of the first `args=[...]` in a piece of workflow source."""
    return [a.strip() for a in src.split("args=[")[1].split("]")[0].split(",")]


def _flat(src: str) -> str:
    """One line, single-spaced, so a pin does not depend on where a call wraps."""
    return " ".join(src.split())


# --- routing a reply back to the run that asked ---

def test_a_button_tap_names_its_run():
    u = {"update_id": 1, "callback_query": {"id": "cb1", "data": f"lg:{cb_key('run-x')}:A",
                                            "message": {"text": CARD}}}
    assert route_update(u, "42", []) == {"wf_id": "run-x", "value": "A", "callback_id": "cb1"}


def test_a_reply_that_quotes_a_card_names_its_run():
    u = {"update_id": 1, "message": {"text": "yes, use postgres", "chat": {"id": 42},
                                     "reply_to_message": {"text": CARD}}}
    assert route_update(u, "42", ["run-a", "run-b"]) == \
        {"wf_id": "run-x", "value": "yes, use postgres", "callback_id": None}


def test_a_bare_reply_goes_to_the_only_run_in_flight():
    u = {"update_id": 1, "message": {"text": "use postgres", "chat": {"id": 42}}}
    assert route_update(u, "42", ["run-only"])["wf_id"] == "run-only"


def test_a_bare_reply_is_refused_when_two_runs_could_have_asked():
    """The bug this replaces: whichever run polled first took whatever was
    pending, so one run swallowed another run's answer and that run waited
    forever. Refusing and saying so beats guessing."""
    u = {"update_id": 1, "message": {"text": "use postgres", "chat": {"id": 42}}}
    out = route_update(u, "42", ["run-a", "run-b"])
    assert "wf_id" not in out and "cannot tell which" in out["problem"]


def test_other_chats_and_commands_are_ignored():
    for u in ({"update_id": 1, "message": {"text": "intruder", "chat": {"id": 999}}},
              {"update_id": 2, "message": {"text": "/start", "chat": {"id": 42}}}):
        assert route_update(u, "42", ["run-a"]) is None


def test_the_card_carries_the_id_the_routing_reads_back():
    """These two have to agree or a reply can never be placed."""
    text = build_card_text("merge-ready", "run-y-ab12cd", "runs/y", "did things", None,
                           {"A": "merge"})
    assert wf_from_card(text) == "run-y-ab12cd"


def test_card_text_without_commit_and_capped():
    t = build_card_text("stopped", "run-y", "runs/m4", "x" * 5000, None, {"A": "close"})
    assert "commit" not in t and len(t) <= 4000


def test_keyboard_callback_data():
    kb = build_keyboard("run-x-123", {"A": "merge", "B": "keep"})
    row = kb["inline_keyboard"][0]
    assert row[0] == {"text": "A", "callback_data": "lg:run-x-123:A"}
    assert row[1]["callback_data"] == "lg:run-x-123:B"


# --- where in a run a card speaks from ---

def test_location_line_with_a_round():
    assert location_line(2, 3, 2) == "item 2 of 3 · round 2"  # U+00B7 between


def test_location_line_between_items():
    assert location_line(2, 3) == "item 2 of 3"


def test_a_card_whose_summary_starts_with_a_location_still_routes():
    """The line goes in the summary, under the headers, so routing still reads
    the id back out of the card."""
    text = build_card_text("decision", "run-y-ab12cd", "runs/y",
                           location_line(2, 3, 2) + "\n\nq", None, {"A": "yes"})
    assert wf_from_card(text) == "run-y-ab12cd"


# --- what a waiting run records about the card it is holding ---

def test_the_ledger_records_the_question_it_sent():
    """The ledger said a run was awaiting a decision but never what it had asked,
    so the page could only name the kind and the letters. The question has to be
    the one the card carries: anything reassembled elsewhere can drift from it."""
    import inspect

    from workflows.run import LoopGraphRun
    src = inspect.getsource(LoopGraphRun._await_decision)
    awaiting = src.split('self._ledger["awaiting"] = {')[1].split("}")[0]
    assert '"question": summary' in awaiting, "the question is not recorded"
    card = src.split("args=[kind, wf_id, run_dir,")[1].split("]")[0]
    assert card.strip().startswith("summary,"), "the card no longer sends that same string"


# --- stream log ---

def test_summarize_tool():
    assert summarize_tool("Edit", {"file_path": "a.py"}) == "a.py"
    assert summarize_tool("Bash", {"command": "npm run build"}) == "npm run build"
    assert len(summarize_tool("Other", {"x": "y" * 500})) == 120


def test_append_log_bounded(tmp_path):
    import activities.stream as s
    p = str(tmp_path / "x.log")
    cap = s.LOG_CAP
    s.LOG_CAP = 200  # shrink for the test
    try:
        for i in range(30):
            append_log(p, f"line {i} " + "x" * 30)
        data = open(p).read()
        assert data.startswith("[... head truncated ...]")
        assert "line 29" in data
    finally:
        s.LOG_CAP = cap


# --- driving a card method with no Temporal server ---

class _FakeWorkflow:
    """Stands in for the `workflow` module inside `workflows.run`.

    Every Temporal call a card method makes goes through that one module-level
    name, so replacing it runs the real method to completion in a plain event
    loop and records what it actually scheduled. Source pins can only say what
    the file reads like; these say what the owner would receive.

    The suite has no `WorkflowEnvironment` on purpose: it would put a downloaded
    test-server binary in the gate's path.
    """

    MAX_WAITS = 5  # a card that never accepts an answer fails instead of hanging

    def __init__(self, run, answer: str, telegram: bool = True) -> None:
        self._run = run
        self._answer = answer
        self._waits = 0
        self.telegram = telegram
        self.scheduled: list[str] = []      # activity names, in order
        self.cards: list[list] = []         # the args of every send_card
        self.awaiting_when_sent: list[dict] = []
        self.logger = logging.getLogger("fake-workflow")

    def info(self):
        return SimpleNamespace(workflow_id="run-x-ab12cd")

    async def execute_activity(self, fn, args=None, **kwargs):
        self.scheduled.append(fn.__name__)
        if fn.__name__ == "telegram_configured":
            return self.telegram
        if fn.__name__ == "send_card":
            self.cards.append(list(args or []))
            # what the ledger claimed at the moment the card went out
            self.awaiting_when_sent.append(dict(self._run._ledger.get("awaiting") or {}))
        return {"sent": True}

    async def wait_condition(self, predicate):
        self._waits += 1
        assert self._waits <= self.MAX_WAITS, "the card never accepted the answer"
        self._run.decide(self._answer)  # the dispatcher signals the waiting run
        assert predicate()

    @property
    def text(self) -> str:
        """The summary of the last card sent: send_card's fourth argument."""
        return self.cards[-1][3]


def _drive(monkeypatch, method: str, *args, answer: str = "A", items: int = 3):
    """Run one LoopGraphRun card method against the fake and hand back both."""
    import workflows.run as run_module
    wf = run_module.LoopGraphRun()
    wf._ledger["items"] = [{"n": n, "item": f"work item {n}", "status": "pending"}
                           for n in range(1, items + 1)]
    fake = _FakeWorkflow(wf, answer)
    monkeypatch.setattr(run_module, "workflow", fake)
    asyncio.run(getattr(wf, method)(*args))
    return wf, fake


def _merge_result():
    return {"summary": "did the thing", "base_branch": "main",
            "branch": "lg-run-ab12cd", "worktree": ""}


# --- every card says where in the run it speaks from ---

def test_every_card_text_starts_with_the_location_line(monkeypatch):
    """AC-20. Pinning the six-element `args=[...]` constrains the argument count
    and says nothing about what `summary` holds, so without this the whole
    visible half of AC-20 could be dropped with the suite green."""
    _, ask = _drive(monkeypatch, "_ask_owner", "runs/x", "which database?", {}, 2, 3, 2,
                    answer="postgres")
    assert ask.text.startswith("item 2 of 3 · round 2\n\nwhich database?")

    _, park = _drive(monkeypatch, "_park_note", "runs/x", 2, 3, "the item", "gates red")
    assert park.text.startswith("item 2 of 3 parked\n\nthe item")

    _, stop = _drive(monkeypatch, "_stopped_note", "runs/x", "supervisor said stop", 2, 3)
    assert stop.text.startswith("item 2 of 3\n\nwhy: supervisor said stop")

    _, merge = _drive(monkeypatch, "_owner_card", "runs/x", _merge_result(),
                      {"commit": "abc1234567"}, [], answer="B")
    assert merge.text.startswith("item 3 of 3\n\ndid the thing")


def test_only_the_decision_card_carries_a_round_number(monkeypatch):
    """A card sent between items or at the end has no round to name, so it gets
    the short form."""
    _, ask = _drive(monkeypatch, "_ask_owner", "runs/x", "q", {}, 1, 3, 2, answer="yes")
    assert ask.text.startswith("item 1 of 3 · round 2")
    for fake in (_drive(monkeypatch, "_park_note", "runs/x", 1, 3, "i", "r")[1],
                 _drive(monkeypatch, "_stopped_note", "runs/x", "r", 1, 3)[1],
                 _drive(monkeypatch, "_owner_card", "runs/x", _merge_result(),
                        {"commit": "abc"}, [], answer="B")[1]):
        assert "round" not in fake.text.splitlines()[0]


def test_the_merge_card_carries_the_parked_list_under_its_location(monkeypatch):
    """What the owner saw is one string: the location, then the merge summary
    with its parked list. The page prints it as is."""
    parked = [{"n": 2, "item": "the second thing", "reason": "gates red"}]
    _, merge = _drive(monkeypatch, "_owner_card", "runs/x", _merge_result(),
                      {"commit": "abc1234567"}, parked, answer="B")
    assert merge.text.startswith("item 3 of 3\n\ndid the thing")
    assert "Parked, NOT in this branch:" in merge.text
    assert "- item 2: the second thing (gates red)" in merge.text


def test_the_ledger_records_the_very_string_the_card_carried(monkeypatch):
    """AC-5, driven rather than read.

    `test_the_ledger_records_the_question_it_sent` above is a source pin: it
    checks the ledger statement says `"question": summary` and that `summary` is
    still send_card's fourth argument. Rebinding `summary` between those two
    lines keeps both assertions true while the ledger holds the pre-edit text and
    the owner's phone shows the post-edit one — the page would then print a
    question nobody was ever asked. Only running the method catches that.
    """
    _, ask = _drive(monkeypatch, "_ask_owner", "runs/x", "which database?", {}, 2, 3, 2,
                    answer="postgres")
    assert ask.awaiting_when_sent[0]["question"] == ask.text

    _, merge = _drive(monkeypatch, "_owner_card", "runs/x", _merge_result(),
                      {"commit": "abc1234567"},
                      [{"n": 2, "item": "second", "reason": "gates red"}], answer="B")
    assert merge.awaiting_when_sent[0]["question"] == merge.text


def test_a_card_with_a_location_still_routes_a_reply_back(monkeypatch):
    """End to end through the real builder: what the workflow sends, sent, still
    names its own run so a quoted reply can be placed."""
    _, ask = _drive(monkeypatch, "_ask_owner", "runs/x", "which database?", {}, 2, 3, 2,
                    answer="postgres")
    kind, wf_id, run_dir, summary, commit, options = ask.cards[0]
    assert wf_from_card(build_card_text(kind, wf_id, run_dir, summary, commit,
                                        options)) == "run-x-ab12cd"


# --- the shape a live run replays against ---

def test_activity_argument_counts_are_pinned():
    """A waiting run replays this workflow from recorded history: the same
    activities, in the same order, with the same argument counts. Prefixing a
    location line must not add, drop or reorder one (AC-34)."""
    src = _method("_await_decision")
    assert src.index("telegram_configured") < src.index("send_card")
    assert _args(src) == ["kind", "wf_id", "run_dir", "summary", "commit", "options"]
    note = _method("_note")
    assert note.index("telegram_configured") < note.index("send_card")
    assert len(_args(note)) == 7


def test_owner_question_stays_bare():
    """AC-22. The location line is card furniture; `owner-answers.md` and the
    round entry keep the supervisor's question exactly as it was asked."""
    src = _method("_run_item")
    assert 'entry["owner_question"] = question' in src
    assert _args(src.split("record_owner_answer")[1])[:2] == ["run_dir", "question"]


def test_the_workflow_module_reads_no_clock_env_or_disk():
    """AC-35. Temporal replays this module from history, so anything the outside
    world can change under it belongs in an activity."""
    import inspect

    import workflows.run
    src = inspect.getsource(workflows.run)
    for banned in ("import os", "import random", "datetime.now", "time.time", "open("):
        assert banned not in src, f"{banned} in workflow code breaks replay"


def test_the_all_parked_note_names_the_last_item():
    """AC-20's `item 3 of 3` case: nothing was accepted, so the run speaks from
    the item it finished on. A halt speaks from the item it stopped on."""
    src = _method("run")
    all_parked = _flat(src.split("if accepted is None:")[1])
    assert ('_stopped_note(run_dir, "every work item was parked", '
            'len(items), len(items))') in all_parked
    halt = _flat(src.split('elif outcome["status"] == "halt":')[1].split("else:")[0])
    assert '_stopped_note(run_dir, outcome["reason"], i, len(items))' in halt


def test_a_long_question_is_cut_on_the_card_and_kept_whole_in_the_ledger(monkeypatch):
    """The location line pushes the tail of a very long question past
    build_card_text's 1500-character cut. That cut stays where it is: the card is
    the alert, and the whole question reaches the ledger, which is what the page
    and `lg status` print."""
    _, ask = _drive(monkeypatch, "_ask_owner", "runs/x", "x" * 2000, {}, 2, 3, 2,
                    answer="ok")
    assert ask.awaiting_when_sent[0]["question"].count("x") == 2000
    card = build_card_text(*ask.cards[0])
    assert "item 2 of 3 · round 2" in card, "the line the cut is paying for"
    assert card.count("x") < 2000, "the card is capped, as it was before"
