from activities.notify import accept_hit, build_card_text, build_keyboard, extract_reply
from activities.stream import append_log, summarize_tool


# --- notify: replies (buttons + free text) ---

def test_extract_reply_button():
    updates = [{"update_id": 1, "callback_query": {"id": "cb1", "data": "lg:run-x:A"}}]
    assert extract_reply(updates, "run-x", "42") == ("button", "A", "cb1")


def test_extract_reply_free_text_from_owner_chat():
    updates = [{"update_id": 1, "message": {"text": "yes, use postgres", "chat": {"id": 42}}}]
    assert extract_reply(updates, "run-x", "42") == ("text", "yes, use postgres", None)


def test_extract_reply_ignores_other_chats_and_commands():
    updates = [
        {"update_id": 1, "message": {"text": "intruder", "chat": {"id": 999}}},
        {"update_id": 2, "message": {"text": "/start", "chat": {"id": 42}}},
    ]
    assert extract_reply(updates, "run-x", "42") is None


def test_extract_reply_button_beats_text():
    updates = [
        {"update_id": 1, "message": {"text": "hello", "chat": {"id": 42}}},
        {"update_id": 2, "callback_query": {"id": "cb9", "data": "lg:run-x:B"}},
    ]
    assert extract_reply(updates, "run-x", "42") == ("button", "B", "cb9")


def test_accept_hit_stray_text_never_decides_merge():
    text_hit = ("text", "Blue", None)
    btn_hit = ("button", "A", "cb1")
    assert accept_hit(text_hit, accept_text=True)
    assert not accept_hit(text_hit, accept_text=False)   # merge cards ignore free text
    assert accept_hit(btn_hit, accept_text=False)
    assert not accept_hit(None, accept_text=True)


def test_card_text_without_commit_and_capped():
    t = build_card_text("stopped", "run-y", "runs/m4", "x" * 5000, None, {"A": "close"})
    assert "commit" not in t and len(t) <= 4000


def test_keyboard_callback_data():
    kb = build_keyboard("run-x-123", {"A": "merge", "B": "keep"})
    row = kb["inline_keyboard"][0]
    assert row[0] == {"text": "A", "callback_data": "lg:run-x-123:A"}
    assert row[1]["callback_data"] == "lg:run-x-123:B"


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
