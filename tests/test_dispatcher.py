"""The dispatcher: every owner decision goes through it, and it had no tests.

Its pure matcher is covered in test_visibility.py. This covers the part that turns
an update into a Temporal signal, which is where the failures actually were.
"""

from __future__ import annotations

import asyncio

import pytest

import dispatcher

CARD = "loopgraph: merge-ready\nrun: runs/x\nworkflow: run-x-ab12cd\n\nsummary here"


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self.status_code = status
        self._payload = payload or {"ok": True, "result": []}
        self.text = "fake"

    def json(self):
        return self._payload


class FakeHttp:
    """Records every Telegram call, and can be told to fail on a chosen one."""

    def __init__(self, batches=None, fail_on=None):
        self.calls = []
        self.batches = list(batches or [])
        self.fail_on = fail_on or set()

    async def post(self, url, json=None):
        name = url.rsplit("/", 1)[-1]
        self.calls.append((name, json))
        if name in self.fail_on:
            raise RuntimeError(f"telegram {name} exploded")
        if name == "getUpdates":
            return FakeResponse({"ok": True, "result": self.batches.pop(0) if self.batches else []})
        return FakeResponse()


class FakeHandle:
    def __init__(self, wf_id, sink, explode=False):
        self.id, self.sink, self.explode = wf_id, sink, explode

    async def signal(self, name, value):
        if self.explode:
            raise RuntimeError("workflow already completed")
        self.sink.append((self.id, name, value))


class FakeClient:
    def __init__(self, open_ids=(), explode=False):
        self.signals = []
        self._open = list(open_ids)
        self.explode = explode

    def get_workflow_handle(self, wf_id):
        return FakeHandle(wf_id, self.signals, self.explode)

    def list_workflows(self, query):
        async def gen():
            for w in self._open:
                yield type("W", (), {"id": w})()
        return gen()


def tap(update_id=1, letter="A", card=CARD, cb_id="cb1"):
    from activities.notify import cb_key
    msg = {"text": card} if card is not None else {}
    return {"update_id": update_id,
            "callback_query": {"id": cb_id, "data": f"lg:{cb_key('run-x-ab12cd')}:{letter}",
                               "message": msg}}


def typed(update_id=1, text="use postgres", quote=CARD):
    m = {"text": text, "chat": {"id": 42}}
    if quote:
        m["reply_to_message"] = {"text": quote}
    return {"update_id": update_id, "message": m}


def run_pump(http, client, offset=0):
    return asyncio.run(dispatcher.pump(client, http, "tok", "42", offset))


# ---------- the happy path ----------

def test_a_tap_becomes_a_signal_on_the_run_the_card_names():
    http, client = FakeHttp([[tap()]]), FakeClient()
    assert run_pump(http, client) == 2
    assert client.signals == [("run-x-ab12cd", "decide", "A")]
    assert any(c[0] == "answerCallbackQuery" for c in http.calls), "no toast for the owner"


def test_a_typed_reply_quoting_a_card_reaches_that_run():
    http, client = FakeHttp([[typed()]]), FakeClient(open_ids=["run-other", "run-x-ab12cd"])
    run_pump(http, client)
    assert client.signals == [("run-x-ab12cd", "decide", "use postgres")]


def test_a_bare_message_with_two_runs_open_is_refused_out_loud():
    """It must never guess: guessing is how one run's answer merges another."""
    http = FakeHttp([[typed(quote=None)]])
    client = FakeClient(open_ids=["run-a", "run-b"])
    run_pump(http, client)
    assert client.signals == []
    said = [c[1]["text"] for c in http.calls if c[0] == "sendMessage"]
    assert said and "cannot tell which" in said[0]


# ---------- the failures that used to bite ----------

def test_a_tap_on_a_card_too_old_to_carry_its_text_still_routes():
    """Telegram sends an InaccessibleMessage for a card older than ~48h: `message`
    is present with no `text`. Keying the open-run lookup off `message` meant the
    suffix fallback written for this case never ran, and whether the tap worked
    depended on what else arrived in the same batch."""
    http = FakeHttp([[tap(card=None)]])
    client = FakeClient(open_ids=["run-x-ab12cd"])
    run_pump(http, client)
    assert client.signals == [("run-x-ab12cd", "decide", "A")]


def test_one_failed_send_does_not_replay_the_whole_batch():
    """The bug: offset lived in a local only returned on the happy path, so a
    transient POST failure re-fetched the batch and acted on it again — duplicate
    decisions and repeated messages, every five seconds, forever."""
    http = FakeHttp([[tap(1, "A"), tap(2, "B", cb_id="cb2")]],
                    fail_on={"answerCallbackQuery"})
    client = FakeClient()
    offset = run_pump(http, client)
    assert offset == 3, "progress was lost, so the batch will be replayed"
    assert [s[2] for s in client.signals] == ["A", "B"], "a failure stopped later updates"


def test_a_signal_that_cannot_be_delivered_tells_the_owner():
    """A typed answer has no toast, so without this the reply vanished leaving
    only a line in a container log. It says why, not just that it failed."""
    http, client = FakeHttp([[typed()]]), FakeClient(explode=True)
    run_pump(http, client)
    said = [c[1]["text"] for c in http.calls if c[0] == "sendMessage"]
    assert said and "that run already finished" in said[0]


def test_an_update_with_no_id_does_not_wedge_the_pump():
    http = FakeHttp([[{"message": {"text": "hi", "chat": {"id": 42}}}, tap(9)]])
    client = FakeClient(open_ids=["run-x-ab12cd"])
    offset = run_pump(http, client, offset=5)
    assert offset == 10, "a malformed update stopped the batch"
    assert ("run-x-ab12cd", "decide", "A") in client.signals


def test_a_telegram_outage_does_not_advance_past_unread_updates():
    http = FakeHttp([], fail_on=set())
    http.batches = []

    class Down(FakeHttp):
        async def post(self, url, json=None):
            if url.endswith("getUpdates"):
                return FakeResponse(status=502)
            return FakeResponse()

    assert run_pump(Down(), FakeClient(), offset=7) == 7


def test_a_visibility_outage_degrades_rather_than_dies():
    """open_run_ids swallows its errors, so routing still works for anything that
    names its own run."""
    class Blind(FakeClient):
        def list_workflows(self, query):
            raise RuntimeError("visibility is down")

    http, client = FakeHttp([[tap()]]), Blind()
    run_pump(http, client)
    assert client.signals == [("run-x-ab12cd", "decide", "A")]


def test_an_outage_says_so_rather_than_claiming_nothing_is_running():
    """Returning [] on a visibility outage made the bot tell the owner "no run is
    waiting" while one was, and drop the answer. Not knowing is a third state."""
    class Blind(FakeClient):
        def list_workflows(self, query):
            raise RuntimeError("visibility is down")

    http, client = FakeHttp([[typed(quote=None)]]), Blind()
    run_pump(http, client)
    assert client.signals == []
    said = [c[1]["text"] for c in http.calls if c[0] == "sendMessage"]
    assert said and "could not check which runs are open" in said[0]
    assert "no run is waiting" not in said[0]


def test_no_open_runs_still_says_no_open_runs():
    http, client = FakeHttp([[typed(quote=None)]]), FakeClient(open_ids=[])
    run_pump(http, client)
    said = [c[1]["text"] for c in http.calls if c[0] == "sendMessage"]
    assert said and "no run is waiting" in said[0]


def test_a_second_tap_says_the_run_finished():
    """A button stays tappable after the run it decided has ended. The old toast,
    "that run is not accepting answers", read like a broken engine."""
    err = "workflow execution already completed"
    assert dispatcher.explain_signal_failure(err, "discarded") == "that run already finished (discarded)"
    assert dispatcher.explain_signal_failure(err, None) == "that run already finished"


def test_other_signal_failures_ask_for_a_retry():
    note = dispatcher.explain_signal_failure("connection refused", None)
    assert "try again" in note and "finished" not in note
