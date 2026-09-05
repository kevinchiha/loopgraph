from activities.notify import build_card_text, build_keyboard, cb_key, location_line
from activities.route import route_update, wf_from_card
from activities.stream import append_log, summarize_tool

CARD = "loopgraph: decision\nrun: runs/x\nworkflow: run-x\n\npick one"


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
