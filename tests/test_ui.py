import ast
import asyncio
import importlib.util
import inspect
import json
import os
import re
import subprocess
import threading
import time
import tracemalloc
import urllib.error
import urllib.request
from datetime import datetime, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from temporalio.client import WorkflowExecutionStatus

import ui


@pytest.fixture
def server(tmp_path):
    run = tmp_path / "2026-01-01-demo"
    (run / "logs").mkdir(parents=True)
    # The name the engine actually writes today. This fixture used to plant the
    # stale r1-executor.log, so the suite stayed green while the dashboard showed
    # "no logs yet" for every real run.
    from activities.stream import log_name
    (run / "logs" / log_name(1, 1, "executor")).write_text(
        "[10:00:00 assistant] hello\n[10:00:01 tool:Bash] true\n")
    srv = ui.make_server(0, tmp_path, temporal_addr=None)  # port 0 = ephemeral
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def logs(tmp_path):
    """The logs directory `server` plants, for tests that add or grow a file."""
    return tmp_path / "2026-01-01-demo" / "logs"


def get(url, timeout=5):
    return urllib.request.urlopen(url, timeout=timeout)


def getjson(url):
    return json.loads(get(url).read())


def status_of(url):
    with pytest.raises(urllib.error.HTTPError) as e:
        get(url)
    return e.value.code


def post(url, body, timeout=5):
    """A POST, as (status, decoded body), whatever the status is.

    Both halves are the point. AC-16 asks for "a 400 with a body, never a dropped
    connection", and a handler that raises before it answers gives urllib no
    status at all: socketserver swallows the exception and closes the socket, so
    the caller gets a RemoteDisconnected out of here rather than a code to assert
    on. `body` is bytes as they are, so a test can send something that is not JSON.
    """
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_page_serves(server):
    body = get(server + "/").read().decode()
    assert "loopgraph" in body and "/api/runs" in body


def test_logs_endpoint(server):
    from activities.stream import LOG_RE, log_name
    d = getjson(server + "/api/logs?dir=2026-01-01-demo")
    name = log_name(1, 1, "executor")
    assert d["logs"] == [name]
    assert re.match(LOG_RE, name), "the page would drop this file and render nothing"


def test_logs_reject_traversal(server):
    with pytest.raises(Exception):
        get(server + "/api/logs?dir=../..")


def test_log_names_uses_the_shared_glob(server, logs):
    """The failure the glob constant exists to prevent: three copies of the
    filename shape, one of them here, and both readers stopped matching at once."""
    from activities.stream import log_name
    (logs / "notes.txt").write_text("not a log\n")
    d = getjson(server + "/api/logs?dir=2026-01-01-demo")
    assert d["logs"] == [log_name(1, 1, "executor")]
    assert "LOG_GLOB" in inspect.getsource(ui.log_names), "a hardcoded '*.log' is the bug"


def test_log_slices_from_the_offset(server, logs):
    from activities.stream import log_name
    name = log_name(1, 1, "executor")
    whole = (logs / name).read_bytes()

    d = getjson(f"{server}/api/log?dir=2026-01-01-demo&name={name}&offset=0")
    assert d["text"] == whole.decode()
    assert d["offset"] == 0
    assert d["size"] == len(whole)

    with (logs / name).open("a") as f:
        f.write("[10:00:02 assistant] more\n")
    d2 = getjson(f"{server}/api/log?dir=2026-01-01-demo&name={name}&offset={d['size']}")
    assert d2["text"] == "[10:00:02 assistant] more\n"
    assert d2["offset"] == d["size"]
    assert d2["size"] == len(whole) + len("[10:00:02 assistant] more\n")


def test_log_restarts_when_the_file_shrank(server, logs):
    from activities.stream import log_name
    name = log_name(1, 1, "executor")
    whole = (logs / name).read_text()
    d = getjson(f"{server}/api/log?dir=2026-01-01-demo&name={name}&offset=99999")
    assert d["offset"] == 0, "a stored offset past the end must restart the pane"
    assert d["text"] == whole


def test_a_head_truncated_file_is_flagged(server, logs):
    """append_log rewrites an over-cap file as the marker plus its last 500 KB.
    A pane whose stored offset was under 500 KB still passes the size check while
    every byte position behind it has moved, so only the flag catches this."""
    from activities.stream import log_name
    name = log_name(2, 1, "audit")
    (logs / name).write_bytes(ui.HEAD_MARK + b"[10:00:00 assistant] tail\n" * 40)
    size = (logs / name).stat().st_size
    offset = size // 2  # below the size on purpose: the other test covers above it
    d = getjson(f"{server}/api/log?dir=2026-01-01-demo&name={name}&offset={offset}")
    assert d["offset"] == offset, "the size signal cannot fire here, so it must not"
    assert d["head_truncated"] is True


class AppendsAtTheSeam:
    """The real file, except that one more line lands in it between log_slice's
    size measurement and its read — which is where a live log actually grows."""

    def __init__(self, path, extra):
        self._path, self._extra, self._f = path, extra, None

    def open(self, mode):
        self._f = self._path.open(mode)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._f.close()

    def read(self, *a):
        return self._f.read(*a)

    def seek(self, *a):
        pos = self._f.seek(*a)
        if a == (0, 2) and self._extra:  # the seek that measures the file
            with self._path.open("ab") as w:
                w.write(self._extra)
            self._extra = b""
        return pos


def test_log_sends_no_more_than_the_size_it_reports(tmp_path):
    """Measuring and reading are two instants, and an executor is appending to
    the log in between. The page stores `size` as its next offset, so bytes past
    that size come round again on the next poll and the reader sees the same
    lines twice."""
    from activities.stream import log_name
    p = tmp_path / log_name(1, 1, "executor")
    p.write_bytes(b"[10:00:00 assistant] first\n")

    d = ui.log_slice(AppendsAtTheSeam(p, b"[10:00:01 assistant] second\n"), 0)
    assert len(d["text"].encode()) == d["size"] - d["offset"], "text ran past the reported size"
    assert "second" not in d["text"], "a line that landed after the measurement went out early"
    # and it is not lost: the next poll starts exactly where `size` said it would
    assert ui.log_slice(p, d["size"])["text"] == "[10:00:01 assistant] second\n"


def test_an_untruncated_file_is_not_flagged(server):
    from activities.stream import log_name
    name = log_name(1, 1, "executor")
    d = getjson(f"{server}/api/log?dir=2026-01-01-demo&name={name}&offset=0")
    assert d["head_truncated"] is False


def test_the_truncation_marker_matches_the_writer():
    """HEAD_MARK is a second copy of a literal append_log writes, and the phase
    does not touch that file. Pin the copy, the way test_review_fixes pins
    LOG_GLOB against log_name, so it cannot drift."""
    import activities.stream
    src = inspect.getsource(activities.stream.append_log)
    escaped = ui.HEAD_MARK.decode().replace("\n", "\\n")  # source writes the newline escaped
    assert escaped in src, f"append_log no longer writes {ui.HEAD_MARK!r}"


def test_the_writer_leaves_the_marker_the_reader_looks_for(tmp_path, monkeypatch):
    """The pin above says the literal still reads the same. This says the bytes
    append_log puts on disk are the bytes log_slice matches, with no fixture
    built out of HEAD_MARK in between. Cap shrunk the way test_visibility does."""
    import activities.stream as s
    from activities.stream import log_name
    monkeypatch.setattr(s, "LOG_CAP", 200)
    p = tmp_path / log_name(1, 1, "executor")
    for i in range(30):
        s.append_log(str(p), f"[10:00:00 assistant] line {i} " + "x" * 30)
    assert ui.log_slice(p, 0)["head_truncated"] is True


def test_log_survives_a_name_longer_than_the_filesystem_allows(server):
    """LOG_RE bounds neither digit run, so is_file() raised ENAMETOOLONG, the
    handler died mid-reply and `lg ui` printed a traceback at a hand-made URL."""
    name = f"i1-r{'9' * 400}-audit.log"
    assert status_of(f"{server}/api/log?dir=2026-01-01-demo&name={name}") == 404


def test_log_rejects_a_name_outside_the_pattern_with_400(server):
    assert status_of(server + "/api/log?dir=2026-01-01-demo&name=notes.log") == 400


def test_log_missing_file_is_404(server):
    from activities.stream import log_name
    name = log_name(9, 9, "audit")
    assert status_of(f"{server}/api/log?dir=2026-01-01-demo&name={name}") == 404


@pytest.mark.parametrize("offset", ["-1", "abc"])
def test_log_bad_offset_is_400(server, offset):
    from activities.stream import log_name
    name = log_name(1, 1, "executor")
    assert status_of(f"{server}/api/log?dir=2026-01-01-demo&name={name}&offset={offset}") == 400


def test_a_blank_offset_reads_as_zero(server, logs):
    """parse_qs drops an empty value, so the handler never sees the key."""
    from activities.stream import log_name
    name = log_name(1, 1, "executor")
    d = getjson(f"{server}/api/log?dir=2026-01-01-demo&name={name}&offset=")
    assert d["offset"] == 0
    assert d["text"] == (logs / name).read_text()


@pytest.mark.parametrize("bad", ["", "a/b", ".."])
@pytest.mark.parametrize("endpoint,param", [("/api/logs", "dir"),
                                            ("/api/log", "dir"),
                                            ("/api/log", "name")])
def test_dir_and_name_reject_empty_slash_and_dotdot(server, endpoint, param, bad):
    from activities.stream import log_name
    q = {"dir": "2026-01-01-demo", "name": log_name(1, 1, "executor"), param: bad}
    assert status_of(f"{server}{endpoint}?{urlencode(q)}") == 400


def test_runs_endpoint_without_temporal(server):
    d = getjson(server + "/api/runs")
    assert d["temporal"] is False
    row = d["runs"][0]
    assert row["dir"] == "2026-01-01-demo"
    assert row["state"] == "unknown"
    assert row["id"] == row["dir"], "a row known only from its logs is keyed on its directory"
    assert row["start_time"] is None
    assert row["close_time"] is None, "no workflow, so no time rather than a wrong one"


# ---------- the release the header reports ----------

# The checker is the one thing on the dashboard that runs git on a timer, and
# `remote_newest` is the one that reaches the network. Every test below hands it
# fakes on ui.version, which is why ui.py calls those two as module attributes.

NO_VERSION = {"installed": None, "latest": None, "behind": False}
THIS_CHECKOUT = {"tag": "v0.1.0", "commit": "abc1234", "ahead": 0}


class Counting:
    """A stand-in for one of version.py's two git readers that counts its calls.

    `hold_at` holds the checker's thread inside the nth call until `release` is
    set. That is what makes a count read from the test's own thread a count of
    what has finished rather than a race with the next tick.

    `args` keeps what each call was given, recorded before the hold so a test can
    read it while the thread is still inside. Without it the fake swallowed its
    arguments and nothing proved the checker's own remote deadline ever reached
    `version.remote_newest` — a checker that passed no timeout would keep every
    one of these tests green and hang the dashboard's thread on a real network.
    """

    def __init__(self, answer, hold_at=None):
        self.answer, self.hold_at, self.calls = answer, hold_at, 0
        self.args: list[tuple] = []
        self.reached, self.release = threading.Event(), threading.Event()

    def __call__(self, *args):
        self.calls += 1
        self.args.append(args)
        if self.calls == self.hold_at:
            self.reached.set()
            self.release.wait(5)
        return self.answer


def patch_version(monkeypatch, local, remote):
    """Both git readers, replaced on the module ui.py reads them off."""
    monkeypatch.setattr(ui.version, "installed", local)
    monkeypatch.setattr(ui.version, "remote_newest", remote)


class FakeChecker:
    """The one member the handler reads, over a canned answer.

    A page request runs no git: it reads what the checker's own thread put in
    memory, so a reader hitting refresh cannot put `git ls-remote` on the wire.
    """

    def __init__(self, snap):
        self.snap = snap

    def snapshot(self):
        return dict(self.snap)


def test_api_runs_reports_a_null_version_with_no_checker(server):
    """Every fixture in this file builds its server without a checker, which is
    what keeps the suite off the network. The field is still on the wire: the
    page reads it on every poll, and a missing key would land the whole poll in
    the catch branch and put `server error` in the header."""
    assert getjson(server + "/api/runs")["version"] == NO_VERSION


def test_api_runs_carries_the_checkers_snapshot(tmp_path):
    snap = {"installed": "v0.1.0", "latest": "v0.2.0", "behind": True}
    srv = ui.make_server(0, tmp_path, temporal_addr=None, checker=FakeChecker(snap))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        d = getjson(f"http://127.0.0.1:{srv.server_address[1]}/api/runs")
    finally:
        srv.shutdown()
    assert d["version"] == snap
    assert d["temporal"] is False, "the version field is the only thing that moved"


def test_the_checker_re_reads_the_tag_without_asking_the_remote(monkeypatch, tmp_path):
    """A refresh with no remote read is the cheap one the thread does every
    minute, and it must stay cheap: `git ls-remote` is a network round trip.

    Counted rather than made to raise, because refresh() swallows exceptions —
    a remote fake that blew up would leave exactly the same snapshot behind.
    """
    local, remote = Counting(THIS_CHECKOUT), Counting("v0.2.0")
    patch_version(monkeypatch, local, remote)

    checker = ui.VersionChecker(tmp_path)
    assert checker.snapshot() == NO_VERSION, "a checker knows nothing until it reads"
    checker.refresh(False)

    assert remote.calls == 0, "a refresh with no remote read went to the network anyway"
    assert local.calls == 1, "the tag was read some number of times other than once"
    assert checker.snapshot() == {"installed": "v0.1.0", "latest": None, "behind": False}
    assert checker.snapshot() is not checker.snapshot(), \
        "a caller holding the dict would see the next refresh half-written"


def test_the_checker_keeps_the_latest_it_has_when_the_remote_fails(monkeypatch, tmp_path):
    """The header is written from this every 4 seconds, so a remote that answered
    once and then went away must not empty it: a laptop off the network would
    watch the update hint blink out and come back all afternoon.
    """
    def gone(root, timeout):
        raise ui.version.RemoteError("could not read from remote repository")

    patch_version(monkeypatch, lambda root: THIS_CHECKOUT, lambda root, timeout: "v0.2.0")
    checker = ui.VersionChecker(tmp_path)
    checker.refresh(True)
    assert checker.snapshot() == {"installed": "v0.1.0", "latest": "v0.2.0", "behind": True}

    monkeypatch.setattr(ui.version, "remote_newest", gone)
    checker.refresh(True)
    assert checker.snapshot() == {"installed": "v0.1.0", "latest": "v0.2.0", "behind": True}


def test_start_publishes_the_tag_before_the_thread_has_read_the_remote(monkeypatch, tmp_path):
    """`serve` starts the checker and then serves, so the first read has to be on
    the caller's thread. `git describe` is a millisecond; the remote read is a
    connection to GitHub, and a header that waited on it would be blank for as
    long as that took, on a page whose only job is to be looked at."""
    remote = Counting("v0.2.0", hold_at=1)
    patch_version(monkeypatch, lambda root: THIS_CHECKOUT, remote)

    checker = ui.VersionChecker(tmp_path)
    checker.start()
    try:
        assert remote.reached.wait(5), "the thread never got as far as the remote"
        assert checker.snapshot() == {"installed": "v0.1.0", "latest": None, "behind": False}
    finally:
        remote.release.set()
        checker.stop()


def test_the_loop_reads_the_tag_every_tick_and_the_remote_every_few(monkeypatch, tmp_path):
    """The two reads cost different things, so they run at different rates: the
    local one every period, the remote one every remote_every-th wake. Held still
    inside the third remote read, the counts are exact — one local read in
    start(), one per tick since, and the remote at the thread's own first read
    and then at ticks 3 and 6.

    The arguments are asserted here too, because the remote read is the only one
    with a deadline and the deadline is the whole reason it is safe to run on
    this thread: `remote_timeout` has to arrive at `version.remote_newest`, not
    stop at the checker."""
    local, remote = Counting(THIS_CHECKOUT), Counting("v0.2.0", hold_at=3)
    patch_version(monkeypatch, local, remote)

    checker = ui.VersionChecker(tmp_path, period=0.01, remote_every=3)
    checker.start()
    try:
        assert remote.reached.wait(5), "the loop never reached its sixth tick"
        assert (local.calls, remote.calls) == (7, 3)
        assert remote.args == [(tmp_path, 10.0)] * 3, \
            "the checker's remote deadline never reached version.remote_newest"
        assert local.args == [(tmp_path,)] * 7
    finally:
        remote.release.set()
        checker.stop()


def test_the_header_carries_the_release_beside_the_name():
    """AC-15 on the page. Its own span, because the poll rewrites #hdr from what
    Temporal said and the release has nothing to do with that."""
    header = re.search(r"<header>(.*?)</header>", ui.page_html(), re.S)
    assert header, "the page has no header"
    inner = header.group(1)
    assert '<span id="ver"></span>' in inner, \
        "the span is not empty, so the page ships text no poll has checked"
    assert inner.index("loopgraph") < inner.index('id="ver"') < inner.index('id="hdr"'), \
        "the release does not sit between the name and the Temporal line"


def test_the_poll_writes_the_update_hint_through_setText():
    """AC-16. The words are what a reader acts on — `lg update` is the command —
    and they go through the guarded setter like every other text on this page, so
    a poll that changes nothing takes nobody's selection with it."""
    src = function_source(ui.page_html(), "runs")
    assert "' available · lg update'" in src, "the header never names the command"
    assert "untagged" in src, "a checkout with no tag leaves a gap beside the name"
    assert re.search(r"setText\(ver,", src), "the release is written some other way"
    assert ".textContent =" not in src, "a poll writes text without checking it changed"


# ---------- the ledger, per workflow id ----------

LEDGER = {"status": "merge-ready", "rounds": [{"verdict": "PASS"}]}
KNOWN = "run-2026-01-01-demo-aaaaaa"
START = datetime(2026, 9, 5, 15, 45, 29, tzinfo=timezone.utc)
CLOSE = datetime(2026, 9, 5, 16, 0, 0, tzinfo=timezone.utc)
# The card a run holds while it waits, in the shape `workflows/run.py` writes
# under `awaiting` — and the shape it leaves behind in a ledger when the run dies
# with the card still up.
AWAITING = {"kind": "decision", "question": "which port?", "options": {"A": "8400"},
            "telegram": False, "answer_with": f"lg approve {KNOWN} <A>"}


class FakeFeed:
    """The four members the handler reads, over canned values.

    `make_server(feed=...)` takes one of these in place of a real TemporalFeed, so
    the endpoints can be asserted over HTTP with no engine running. It cannot stand
    in for the feed's own tests further down: it replaces `ledger`, which is the
    method those exist to exercise.

    `open_states` maps a workflow id to the answer `workflow_open` gives for it —
    True, False or None — and an id that is not in the map answers None, the way a
    real feed answers for an id Temporal has never heard of. `opened` records what
    was asked, because "nothing asked Temporal at all" is what AC-18 promises about
    a run going back on the rail and a returned value cannot show it.
    """

    def __init__(self, connected=True, rows=(), ledgers=None, open_states=None):
        self.connected = connected
        self.rows = [dict(r) for r in rows]
        self.ledgers = dict(ledgers or {})
        self.open_states = dict(open_states or {})
        self.opened = []

    def runs(self):
        return [dict(r) for r in self.rows]

    def ledger(self, wf_id):
        return self.ledgers.get(wf_id)

    def workflow_open(self, wf_id):
        self.opened.append(wf_id)
        return self.open_states.get(wf_id)


@pytest.fixture
def fed(tmp_path):
    """A server whose feed is a fake: Temporal's answers without Temporal."""
    feed = FakeFeed(rows=[ui.run_entry(KNOWN, "completed", START, CLOSE, LEDGER)],
                    ledgers={KNOWN: LEDGER})
    srv = ui.make_server(0, tmp_path, temporal_addr=None, feed=feed)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_run_entry_formats_times_and_keeps_dir():
    done = ui.run_entry(KNOWN, "completed", START, CLOSE, LEDGER)
    assert done["start_time"] == "2026-09-05T15:45:29+00:00"
    assert done["close_time"] == "2026-09-05T16:00:00+00:00"
    assert done["state"] == "merge-ready", "the ledger's status wins over Temporal's"
    assert done["detail"] == "r1 PASS"

    live = ui.run_entry("run-2026-01-01-demo-bbbbbb", "running", START, None, None)
    assert live["close_time"] is None, "a running workflow has not closed"
    assert live["state"] == "running" and live["detail"] == ""

    # Two workflows of one run directory: two rows, two ids, one dir, one set of logs.
    assert done["dir"] == live["dir"] == "2026-01-01-demo"
    assert done["id"] != live["id"]


@pytest.mark.parametrize("recorded", ["running", "held"])
def test_a_waiting_open_workflow_reads_waiting_in_run_entry(recorded):
    """AC-1. A run holding a card is the one row on the rail the owner has to do
    something about, and it read `running` — the same word as the twenty minutes
    it spends letting an executor work, which is what it is doing almost all of
    the time. One word for both is a rail that says nothing.

    The ledger's own status is not the signal and cannot be made into one: it
    reads `running` for the whole of a run's life, card up or not. `awaiting` is
    what the workflow writes when it asks and pops when it is answered, so that
    is what the row is read off.
    """
    ledger = {"status": recorded, "rounds": [{"verdict": "REDO"}], "awaiting": AWAITING}
    row = ui.run_entry(KNOWN, "running", START, None, ledger)
    assert row["state"] == "waiting", "the ledger's own word won over the card it is holding"
    assert row["detail"] == "r1 · waiting on you"

    # An answered card is a key the workflow has popped, and an empty one would
    # be a card asking nothing. Neither is a run waiting on anybody, and the row
    # they get is the row every other run gets.
    for nothing in ({}, None):
        quiet = ui.run_entry(KNOWN, "running", START, None, dict(ledger, awaiting=nothing))
        assert quiet["state"] == recorded
        assert quiet["detail"] == "r1 REDO"


def test_a_closed_workflow_with_a_stale_awaiting_is_not_waiting():
    """The other half of AC-1, and the common case rather than the odd one.

    `workflows/run.py`'s blanket handler records `status="stopped"` and hands
    back the ledger it was holding, `awaiting` and all, so a run whose engine
    died closes with its question still written down. Read off the ledger alone
    the rail would call that run waiting for ever, and the owner would go and
    answer something that ended hours ago.

    The workflow's close time is what settles it, and it is already on the wire
    for AC-3: a start with no close is a run still going.
    """
    ledger = {"status": "stopped", "rounds": [{"verdict": "REDO"}], "awaiting": AWAITING}
    row = ui.run_entry(KNOWN, "completed", START, CLOSE, ledger)
    assert row["state"] == "stopped", "a run that has finished is not asking for anything"
    assert row["detail"] == "r1 REDO", "the detail is the one every other closed run gets"


def test_run_endpoint_without_temporal_is_null_and_200(server):
    assert getjson(f"{server}/api/run?id={KNOWN}") == {"ledger": None, "temporal": False}


def test_run_endpoint_returns_the_feeds_ledger(fed):
    known = getjson(f"{fed}/api/run?id={KNOWN}")
    assert known["ledger"] == LEDGER
    assert known["temporal"] is True

    unknown = getjson(f"{fed}/api/run?id=run-nobody-knows-me-000000")
    assert unknown["ledger"] is None
    assert unknown["temporal"] is True, "an id Temporal never heard of is not Temporal being down"


@pytest.mark.parametrize("bad", ["", "a/b", ".."])
def test_run_endpoint_rejects_bad_ids(server, bad):
    assert status_of(f"{server}/api/run?{urlencode({'id': bad})}") == 400


# ---------- the feed itself, over a stub client ----------

HANGS = object()  # a result() that never comes back, the way a held run's does not


class StubHandle:
    """One workflow's handle. Each call answers with its canned value, or raises it.

    `describes` is what describe() does: a status to report, or an Exception to
    raise. Raising is how Temporal answers for a workflow id it has never heard
    of, and workflow_open has to tell that apart from a workflow it knows to be
    closed — one is a 409 and the other is the archive going through.
    """

    def __init__(self, query, result, status=WorkflowExecutionStatus.COMPLETED,
                 describes=None):
        self._query, self._result, self._status = query, result, status
        self._describes = describes
        self.result_awaited = False
        self.queried = []

    @staticmethod
    async def _answer(canned):
        if isinstance(canned, Exception):
            raise canned
        if canned is HANGS:
            await asyncio.Event().wait()
        return canned

    async def query(self, name):
        # Recorded, not asserted here: _ledger swallows every exception the query
        # raises, so an assert in this coroutine would be eaten and the feed would
        # quietly fall through to result(). The caller asserts on the record.
        self.queried.append(name)
        return await self._answer(self._query)

    async def result(self):
        self.result_awaited = True
        return await self._answer(self._result)

    async def describe(self):
        if isinstance(self._describes, Exception):
            raise self._describes
        return SimpleNamespace(status=self._status)


def stub_feed(monkeypatch, handles, rows=(), connect_fails=False):
    """A real TemporalFeed — its own loop, its own thread, its real call() — over a
    stub client, because the fallback and the two entry points are the feed's own
    code and FakeFeed replaces exactly that.

    `connect_fails` is Temporal down: the feed catches it, keeps no client, and
    every method has to answer from that. Nothing else here can make a feed that
    is not connected, and `workflow_open` answering the wrong thing there is a run
    the owner cannot archive on the one day they are tidying up — which is the day
    Temporal is down.
    """

    class Client:
        @staticmethod
        async def connect(address):
            if connect_fails:
                raise RuntimeError("temporal is down")
            return Client()

        def get_workflow_handle(self, wf_id):
            # An id nobody planted is an id Temporal has never heard of, and a
            # real client raises on all three calls for one. describe() raising
            # is what makes workflow_open answer None rather than "closed", and
            # _ledger swallows it exactly as it swallows the other two.
            return handles.get(wf_id) or StubHandle(KeyError(wf_id), KeyError(wf_id),
                                                    describes=KeyError(wf_id))

        async def list_workflows(self, query):
            for wf in rows:
                yield wf

    monkeypatch.setattr("temporalio.client.Client", Client)
    return ui.TemporalFeed("stub:0")


def test_the_feed_falls_back_from_query_to_result(monkeypatch):
    """Both branches are hot and neither may be optimised away. Of the 15 LoopGraphRun
    workflows on this machine 8 answer query("ledger") and 7 raise [TMPRL1100]
    Nondeterminism, because a query replays the whole history against the workflow
    code registered now. Their ledger is the value the workflow returned."""
    queryable = StubHandle(LEDGER, RuntimeError("result() must not be reached"))
    feed = stub_feed(monkeypatch, {
        "wf-queryable": queryable,
        "wf-replay-diverged": StubHandle(RuntimeError("[TMPRL1100] Nondeterminism error"), LEDGER),
        "wf-neither": StubHandle(RuntimeError("query failed"), RuntimeError("result failed")),
    })
    assert feed.ledger("wf-queryable") == LEDGER
    assert feed.ledger("wf-replay-diverged") == LEDGER
    assert feed.ledger("wf-neither") is None
    assert feed.ledger("wf-never-existed") is None
    # The name is the whole of the query. A stub that ignores it lets `ledger`
    # become `status` with the suite still green, while every queryable run on the
    # live dashboard loses its ledger and falls back to a result that never comes.
    assert queryable.queried == ["ledger"]


@pytest.mark.parametrize("status", [WorkflowExecutionStatus.RUNNING, None],
                         ids=["running", "no status"])
def test_the_feed_never_waits_on_a_running_workflows_result(monkeypatch, status):
    """result() blocks until the workflow finishes, and a run holding its owner's
    card does not finish. Unguarded it burned call()'s whole 10 s timeout, which
    timed the enclosing _runs out too and served an empty run list. Temporal reports
    no status sometimes, and unknown counts as running for the same reason."""
    held = StubHandle(RuntimeError("[TMPRL1100] Nondeterminism error"), HANGS, status=status)
    feed = stub_feed(monkeypatch, {"wf-held": held})
    t0 = time.monotonic()
    assert feed.ledger("wf-held") is None
    assert time.monotonic() - t0 < 2, "it waited on a running workflow's result"
    assert held.result_awaited is False


def test_the_feed_serves_a_ledger_from_both_threads(monkeypatch):
    """_runs is already on the feed's loop, so it awaits the coroutine; a request
    thread calls the sync wrapper. One sync method shared by both deadlocks —
    call() submitted from inside its own loop blocks that loop until the timeout —
    so this fails on the clock rather than on the value."""
    wf = SimpleNamespace(id=KNOWN, status=WorkflowExecutionStatus.COMPLETED,
                         start_time=START, close_time=CLOSE)
    feed = stub_feed(monkeypatch, {KNOWN: StubHandle(LEDGER, RuntimeError("not needed"))},
                     rows=[wf])
    t0 = time.monotonic()
    rows = feed.runs()
    got = []
    t = threading.Thread(target=lambda: got.append(feed.ledger(KNOWN)))
    t.start()
    t.join(5)
    elapsed = time.monotonic() - t0

    assert elapsed < 2, "one sync ledger() shared by both callers deadlocks on the loop thread"
    assert rows == [ui.run_entry(KNOWN, "completed", START, CLOSE, LEDGER)]
    assert rows[0]["detail"] == "r1 PASS", "the run list still carries ledger detail"
    assert got == [LEDGER]


def test_the_injected_pattern_survives_javascript():
    """A JS string literal eats an unknown escape, so a pasted pattern reached
    RegExp with its \\d turned into a bare d and matched no log file, ever."""
    import json
    import re

    from activities.stream import LOG_RE
    html = ui.page_html()
    assert f"new RegExp({json.dumps(LOG_RE)})" in html
    assert "new RegExp('^" not in html, "pattern pasted raw into a JS string"
    # The pattern itself still has to match what stream.py writes.
    assert re.match(LOG_RE, "i2-r1-audit.log")


# ---------- what a poll is allowed to touch ----------
#
# AC-14 is "a poll replaces no DOM node": text stays selected, an open pane stays
# open, a scroll position survives. Half of that can only be seen in a browser, and
# these tests do not pretend otherwise. What they hold is the structure underneath
# it — the page is written so a poll path and a first-render path can be told apart
# by reading the source, and every function that builds markup says so in its name.
#
# Read what follows as tripwires on the shapes this page is written in, not as a
# proof that a poll cannot redraw. They are keyed on names (build…, patch…, runs,
# poll), on the literal words innerHTML and replaceChildren, and on there being
# exactly two intervals. An independent reviewer wrote ten rewrites of the page:
# six left this file green, and the three of those it served to a browser each
# destroyed what AC-14 protects — a board permanently empty, a selection gone
# inside three polls, a text node detached. Four of the six are closed by counting
# the intervals and two by reading one hop past the poll functions. Everything
# below was measured by rewriting the page and running this file over it, the
# NOT list included:
#
#   caught   a poll function — or a plainly-named helper it calls directly — that
#            empties a container and refills it, assigns innerHTML by name,
#            empties an element it fetched from the document, calls a build…
#            function for its side effect, or writes textContent unguarded
#   caught   the same inside a closure, whatever declaration sits above it
#   caught   any of it hung on a timer of its own, because there are two and only
#            two intervals
#   NOT      the same shape two hops out: poll → a() → b() → buildBoard()
#   NOT      `el.textContent = ''` as the emptying primitive inside a build… name,
#            called with its value used so the side-effect rule passes: nothing
#            here reads a textContent write as emptying
#   NOT      innerHTML reached by a computed name: el['inner' + 'HTML'] = markup
#   NOT      a poll that MOVES a node instead of replacing it — list.append(row)
#            on a row already on the page keeps the node and loses the selection
#   NOT      anything about how often a build… function is actually called
#
# The NOT list is not a list of things that do not matter. It is the top of the
# checklist below, and every entry on it reaches the reader as one of two things
# they can see: a highlight that disappears, or a pane that shuts itself. Items 1
# and 2 are how you look for them. What else each test cannot see is written in
# its own docstring.
#
#
# ---------- the browser checklist ----------
#
# Run this by hand whenever PAGE changes. Nothing in this file has ever seen the
# page: it reads the JavaScript as text, so anything about how the page LOOKS is
# outside every check in it. Two defects shipped through a green suite in this
# phase alone. Sections that set `hidden` stayed on screen, because a `display` in
# the page's own stylesheet outranks the browser's built-in `[hidden]` rule — CSS,
# which nothing here parses. And a board said three contradictory things about one
# run at once: a card reading `in progress`, a row reading `unknown`, a line saying
# the page knew nothing. Each half was right on its own.
#
# Ten minutes, in this order — items 1, 2, 6 and 8 are 45 seconds of waiting on
# their own, and an honest number gets a checklist finished rather than dropped
# halfway through. Start the dashboard on a port that is not the owner's 8400 —
# `.venv/bin/python ui.py 8410` — open http://localhost:8410, and
# open the browser's developer tools at the Network tab (F12). Keep an eye on the
# Console tab too: an exception thrown inside a fetch is why a pane goes blank,
# and it is the one failure that leaves no mark on the page. The rail takes a few
# seconds to fill on a machine with a dozen runs, because every row waits on
# Temporal. Pick a run that has rounds.
#
#  1. Select some text in a run row in the left rail. Wait 15 seconds, touching
#     nothing. That is three of the run list's 4-second polls, and you can see them
#     go past as `/api/runs` rows in the network panel.
#     See: the highlight is still there and the row has not moved. If a run in the
#     rail is still open, its duration counts up while you wait; if every run has
#     finished, the network rows are the proof the polls are running at all.
#     If the highlight goes, a poll is making new nodes where it should be writing
#     over the ones on screen — the run list is being rebuilt, not patched.
#
#  2. Open a round's executor log. Scroll up inside it, away from the bottom, and
#     select a line. Wait 10 seconds — five of the board's 2-second polls.
#     See: the pane is still open, still scrolled where you left it, and the line
#     is still highlighted.
#     If the pane shuts or the scroll jumps to the bottom, a poll is replacing the
#     pane's contents rather than adding to the end of them. On a run that is still
#     writing, watch a second longer: new lines arrive at the bottom and do not
#     drag you down to them.
#
#  3. Click a run in the rail whose pill reads `unknown` and whose detail reads
#     `logs only` — a directory with no workflow behind it.
#     See: one line, `no workflow for this run` or `temporal unreachable — logs
#     only`, and then the log cards. Nothing above that line. No status pill, no
#     `answer with:` box, no work items, no diff pane.
#     Anything still showing is a section that set `hidden` and stayed visible,
#     which is the CSS bug: the page needs `[hidden] { display:none !important }`
#     to beat its own rules, and something has got past it.
#
#  4. Read a run's row and its board together, on several runs.
#     See: the pill in the row and the pill on the board say the same word, and
#     nothing says `in progress` on a run whose row shows a duration that has
#     stopped.
#     A page that contradicts itself here is worse than one that says nothing: the
#     reader cannot tell which half to believe.
#     Nothing in items 1 to 3 makes a run that is WAITING, and this item only
#     compares whatever words are already on screen, so serve a pair by hand the
#     way item 10 does: `ui.make_server(8410, Path('runs'), temporal_addr=None,
#     feed=FakeFeed(rows=[...], ledgers={id: {..., "awaiting": {...}}}))`, with one
#     row carrying a `start_time` and no `close_time` — that pair is what
#     patchRunRow turns into `data-live`, which the board's waiting branch reads —
#     and a second row carrying both times and the same stale `awaiting`.
#     See, on the open run: `waiting` in blue in the rail row and `waiting` in blue
#     on the board, with the awaiting block under them.
#     See, on the closed one: the status it recorded — `stopped` on a run the
#     engine died in — and no awaiting block at all. No question, no options, no
#     `lg approve` command. A run that has ended asking to be answered is this
#     item's failure, not item 9's.
#
#  5. Scroll to the foot of the board, past the round cards. The diff pane is
#     there, closed. Open it.
#     See: exactly one `GET /api/diff?id=…` in the network panel, a `stat` naming
#     the files the branch changed, and the patch under it. The pane grows to what
#     it holds and stops short of filling the window, scrolling inside itself. A
#     one-line answer gets a one-line box, not an empty one eleven lines deep.
#     A blank pane is a bug in /api/diff, not in the page: that endpoint answers
#     every failure with a line to show, so an empty pane means it sent none.
#     On a run that was discarded the line is `no worktree pointer at …`, because
#     `lg discard` removed the worktree. That is the right answer, not a failure.
#     If the stat says thousands of changed lines, scroll to the end of the patch:
#     it either stops where the diff stops or says `patch cut at 200 KB`.
#
#  6. Close the diff pane and wait 10 seconds. Open it again.
#     See: no `/api/diff` at all while it is closed, and exactly one more when it
#     opens. The log panes go on polling throughout; the diff never joins them.
#     Then stop the dashboard with the pane closed and open it once more.
#     See: `server error`, on its own. Not `server error` above the patch the last
#     opening put there — a pane that keeps half of an answer it has disowned is
#     the page contradicting itself again, in the one place a reader would read it
#     as current.
#
#  7. Click a run whose rail pill reads `merged` and open its diff.
#     See: `already merged into <base>; the branch adds nothing to it`. That is the
#     state of every run the owner has approved, so it is the diff they will see
#     most often, and a pane that renders blank instead tells them nothing.
#     `branch not found: lg-…` is the same run with its branch deleted by hand
#     after the merge. Also right, but it is not this check — try another.
#
#  8. Leave a log pane open AND the diff pane open, watch the network panel for 10
#     seconds and read every row.
#     See: `/api/runs`, `/api/run`, `/api/logs` and `/api/log` — those four, all
#     GET, and nothing else. Every `/api/log` row names a real log file. A
#     collapsed pane asks for nothing, so the number of `/api/log` rows matches the
#     number of panes you left open.
#     `name=undefined` in a `/api/log` row is the log poll having picked up the
#     diff pane, which is a `.panel` too and holds no log name. Any other method is
#     a dashboard that is no longer read-only. Any other path is a request nobody
#     meant to send. Touch nothing while you watch: the archive button posts, and a
#     `POST /api/archive` in this window means it has reached a timer, which is
#     item 15's failure and the worst one on this page — the dashboard hiding runs
#     nobody asked it to hide.
#
#  9. Only when a run is actually holding a card. There was no such run on this
#     machine the day this was written, so this item goes unrun more often than
#     not — which is the reason it is written down rather than left out. Click the
#     run that is waiting and find the block above the work items.
#     See: the question with its own line breaks, the options, and the `lg approve
#     …` command in a box. One click inside the box selects the whole command and
#     nothing else — not the words `answer with:` beside it, which would go into
#     the shell with it.
#     That box is `display:inline-block`, the same shape as the rule that beat
#     `hidden` in item 3, and the command is the highest-stakes text on the page:
#     it is the only thing here that changes what a run does.
#
# 10. The sweep section, which needs a sweep run, and there was none on this machine
#     the day this was written — so like item 9 this goes unrun more often than not.
#     Until there is one, serve a hand-written ledger instead: `ui.make_server(8410,
#     Path('runs'), temporal_addr=None, feed=FakeFeed(rows=[...], ledgers={id:
#     {"status": "running", "sweep": {"passes": [...], "ended": None}, "items": [...]}}))`,
#     with the FakeFeed from this file and a `kind` on an item or two.
#     See: a `sweep` block above the work items, one line per pass, the failed
#     detectors named in brackets on an incomplete one, the ending reason under them
#     once the run has one, and the word `sweep` or `convergence` beside the status
#     of every item that carries one.
#     Then click a brief run.
#     See: no sweep block at all and not one kind word in the items. Every run before
#     this phase is that run, and a label on every row of them would be this change
#     costing something and giving nothing back.
#
# 11. The group beside a sweep item and the breakdown on a pass, which need a sweep
#     run whose passes carry groups — none on this machine the day this was written.
#     Serve a hand-written ledger as in item 10, with `groups` on each pass's
#     detectors, out of name order across the two detectors: the first
#     `[{"name": "lib/", "count": 3}]`, the second
#     `[{"name": "app/", "count": 5}, {"name": "scripts/", "count": 2}]`; and a
#     `group` on each sweep item, one of them `<b>x</b>`.
#     See: `sweep · app/` beside the item's status, in the one span the kind word
#     had, and `sweep · <b>x</b>` on the other item as those eight characters, not
#     as bold text; and the breakdown in brackets at the end of every pass line,
#     sorted by name whatever order the detectors reported:
#     `pass 1: 10 candidates (app/ 5, lib/ 3, scripts/ 2)`.
#     Then click a brief run.
#     See: neither. No group beside any item and no brackets on any pass line — a
#     brief run has no sweep block, and a sweep recorded before groups existed
#     prints its pass lines exactly as item 10 saw them.
#
# 12. Read the header. This item needs no run at all, so it is the cheap one.
#     See: the release this checkout is on beside the name — `v0.2.0`, or
#     `untagged` on a clone that has no tag yet — as soon as the first
#     `/api/runs` lands, and never a blank gap that fills in a second or two
#     later. A gap is the page waiting on the remote read, which is the whole
#     reason the local one happens before the checker's thread starts. Beside it,
#     `engine dashboard` reads exactly as it did before, in the same dim 12px.
#     Then look at it on a checkout that is behind origin: clone this repository
#     into a temporary directory, check an older tag out in the clone, and run
#     `.venv/bin/python ui.py 8410` from there.
#     See: `v0.1.0 · v0.2.0 available · lg update`, in that same dim style, with
#     `engine dashboard` still untouched next to it. `lg update` is the command
#     the reader is meant to type, so it is the one word here that has to survive
#     being read at a glance.
#
# 13. Click a run in the rail, then look at the address bar and reload.
#     See: the run's id after the `#`, and the same run selected after the reload,
#     with its board built. Paste that URL into a second tab and it opens there
#     too. Then replace the hash with `#%` — a hash that cannot be decoded — and
#     reload, and do it again with `#no-such-run`.
#     See, both times: the top run selected as if there were no hash at all, the
#     header still reading `engine dashboard`, and no `server error`. Watch it for
#     another 10 seconds: `server error` appearing on the third poll is the decode
#     throwing inside the poll's own try, which is the failure this is here for.
#     Nothing in the page listens for a hash change, so pressing Back moves the
#     URL and leaves the selection where it is. That is the accepted cost, not a
#     bug to report.
#
# 14. Read the times in the rail on a run started today and one started last week.
#     See: `14m ago` or `3h ago` on the young one, and the full date on the old
#     one, in the same dim 11px as before with the duration still beside it. Hover
#     either and the full local timestamp comes up as a tooltip.
#     A run started seconds ago reads `0m ago`, which is right — it is the last
#     whole minute that has passed, not a rounding mistake.
#
# 15. Archiving, which is the one thing on this page that changes anything and the
#     only item here that needs a run you are willing to hide. Pick a run that has
#     finished and click `archive` at the right-hand end of the strip.
#     See: the row leaves the rail within a second — not on the next 4-second poll,
#     because the click asks for the list again — the header count goes up by one,
#     and the button turns into `unarchive`. The board stays exactly as it was: the
#     run you archived is still the selected one, still named on the strip, still
#     showing its rounds. That is deliberate, not a bug to report.
#     Now tick `archived (N)` in the header.
#     See: the hidden row comes back where it was, marked `archived` in purple
#     beside its detail, and the count does not change — it counts what the server
#     says is archived, not what is on screen. Untick it and the row goes again.
#     Then click `unarchive` and watch it come back with the toggle off.
#     Now click a run that is still running and press `archive`.
#     See: the server's own sentence in red beside the button —
#     `workflow still open: finish or answer it before archiving` — and the row
#     still on the rail with nothing else changed. A 409 in the console is the
#     browser reporting the status, not an error the page failed to handle. Click
#     another run and the sentence goes with the board it was on.
#     Finally, with every run on the rail archived, reload the page.
#     See: an empty rail, `select a run`, and `archived (N)` in the header saying
#     where they went. Tick the toggle and the top run selects itself within four
#     seconds. A rail that fills up and stays unselected is the load-time fallback
#     skipping archived rows the toggle is showing.

DECLARED = re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(")


def script_of(html):
    """The dashboard's JavaScript: the document stripped off, and the comments too.

    Every <script> block, joined, not the first one and then stop. Reading one
    block left anything in a second invisible to every rule below — a poll
    function that redrew the run list included — and a hole that wide does not
    need anyone to write into it on purpose.

    The rules below are about code. The page explains them in a comment that names
    innerHTML, and saying what the rule is must not read as breaking it — the same
    reason test_the_process_group_is_captured_before_the_pid_can_be_reused reads
    _git_read_only with its comments cut out.
    """
    src = "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", html, re.S))
    assert src.strip(), "the page serves no script"
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("//"))


def regions(html):
    """(name, start, end) per `function NAME(` in the script, each running to the next.

    Not a JavaScript parser, and it does not need to be: this is exactly the
    "nearest preceding declaration" rule the page is written to obey. An arrow or
    a function expression that assigned innerHTML would be charged to whichever
    declaration happens to sit above it, which is the hole
    test_innerhtml_is_only_set_on_a_node_the_same_function_just_created closes
    from the other side, by looking at what is assigned to rather than where, and
    test_a_closure_neither_builds_markup_nor_empties_an_element closes head on.
    """
    src = script_of(html)
    marks = [(m.start(), m.group(1)) for m in DECLARED.finditer(src)]
    ends = [start for start, _ in marks[1:]] + [len(src)]
    return src, [(name, start, end) for (start, name), end in zip(marks, ends)]


def function_source(html, name):
    src, regs = regions(html)
    found = [src[s:e] for n, s, e in regs if n == name]
    assert len(found) == 1, f"expected one `function {name}(`, found {len(found)}"
    return found[0]


def closures(src):
    """The body of every arrow function and function expression written as a block.

    regions() charges these to whichever `function NAME(` declaration sits above
    them, so a closure written under a build… declaration inherits its name and
    every rule keyed on that name. This counts braces instead, which is enough
    for a page with no unbalanced brace inside a string.
    """
    out = []
    for m in re.finditer(r"=>\s*\{|\bfunction\s*\(", src):
        opening = src.index("{", m.start())
        depth = 0
        for i in range(opening, len(src)):
            depth += (src[i] == "{") - (src[i] == "}")
            if depth == 0:
                out.append(src[opening:i + 1])
                break
    return out


def test_every_script_block_is_read_not_only_the_first():
    """The hole in this file's own machinery. script_of stopped at the first
    </script>, so a second block was checked by nothing at all."""
    two = "<p><script>function buildOne() {}</script><b><script>function patchTwo() {}</script>"
    assert "patchTwo" in script_of(two), "the second block was never read"
    assert [n for n, _, _ in regions(two)[1]] == ["buildOne", "patchTwo"], \
        "a function in the second block owns no region, so no rule below can reach it"


def test_closures_are_found_wherever_they_sit():
    """The other hole, and this is the machinery that closes it: a closure written
    under a build… declaration inherits that name from regions(), so the rules
    keyed on the name have to be joined by one that does not read names at all."""
    src = "function buildA() { el.onclick = () => { x.innerHTML = 'no'; }; }"
    assert closures(src) == ["{ x.innerHTML = 'no'; }"]
    assert "buildA" == regions(f"<script>{src}</script>")[1][0][0], \
        "regions still charges the closure to the declaration above it"


def test_innerhtml_lives_only_in_build_functions():
    """The naming contract the rest of the page is written against.

    An assignment to innerHTML throws away every node under the element and makes
    new ones, so a selection inside it dies, an open pane closes and a scroll
    position resets. Doing that once, when the element is created, is how the page
    is drawn; doing it every 2 seconds is the bug AC-14 names. The two are told
    apart here by the name of the function they are in: `build…` runs once per
    element, `patch…` runs on every poll and changes what is already there.

    This proves nothing about how often a build function is called. It proves that
    a poll path which redrew the board would have to be written under a name that
    says it is not one.
    """
    src, regs = regions(ui.page_html())
    assert [n for n, _, _ in regs if n.startswith("build")], "no builder owns the markup"
    for i in (m.start() for m in re.finditer("innerHTML", src)):
        owner = next((n for n, s, e in regs if s <= i < e), None)
        assert owner and owner.startswith("build"), \
            f"innerHTML belongs to {owner}, which is not a build… function"
    for name, s, e in regs:
        if name in ("runs", "poll") or name.startswith("patch"):
            assert "innerHTML" not in src[s:e], f"{name} runs on every poll and assigns innerHTML"
    assert "outerHTML" not in src, "outerHTML is innerHTML with the element thrown in"


def test_innerhtml_is_only_set_on_a_node_the_same_function_just_created():
    """The hole the naming rule leaves, closed from the other side.

    `function buildBoard() { document.getElementById('board').innerHTML = ... }`
    passes the name check and is exactly the redraw AC-14 forbids, once it is
    called from a poll. So the target has to be a node the function made itself:
    markup can be poured into an element that does not exist yet, never into one
    the reader may have a selection in.
    """
    src, regs = regions(ui.page_html())
    setter = re.compile(r"([A-Za-z_$][\w$]*)\.innerHTML\s*=")
    assert len(setter.findall(src)) == src.count("innerHTML"), \
        "an innerHTML that is not a plain `<name>.innerHTML =` assignment"
    for name, s, e in regs:
        body = src[s:e]
        for target in setter.findall(body):
            assert re.search(rf"\b{target}\s*=\s*document\.createElement\(", body), \
                f"{name} sets innerHTML on {target}, which it did not create"


def test_the_page_clears_children_but_never_swaps_them():
    """replaceChildren() with no argument empties an element, which is what a pane
    that has to start again needs. With arguments it is innerHTML by another name:
    every node goes, including the one holding the reader's selection, and no
    check on innerHTML would see it."""
    src = script_of(ui.page_html())
    assert src.count("replaceChildren()") == src.count("replaceChildren("), \
        "replaceChildren(<nodes>) throws away what is already on the page"


def poll_path(src, regs):
    """The names an interval reaches: runs, poll, every patch…, and the plainly
    named functions those call directly.

    One hop, and deliberately not a call graph. `poll() { resetBoard(); }` with
    `function resetBoard() { buildBoard(); }` is a rewrite a reviewer served to a
    browser: the board came back empty every 2 seconds and every rule keyed on the
    caller's name waved it through, because the name that does the damage is
    `resetBoard`. One hop catches that. Two hops — poll → a() → b() → buildBoard()
    — it does not, and nothing here pretends otherwise.

    build… names are left out on purpose. A poll function calling one directly is
    already test_a_poll_never_calls_a_build_function_for_its_side_effect's job,
    and dragging buildRunRow onto the poll path would ban the innerHTML it is
    supposed to have.
    """
    named = {n for n, _, _ in regs}
    direct = {n for n in named if n in ("runs", "poll") or n.startswith("patch")}
    # Read off `direct` and never off the growing set, so the hop count is one
    # whatever order the declarations happen to sit in.
    reached = set(direct)
    for name, s, e in regs:
        if name not in direct:
            continue
        for called in re.findall(r"\b([A-Za-z_$][\w$]*)\s*\(", src[s:e]):
            if called in named and not called.startswith("build"):
                reached.add(called)
    return reached


EMPTIED = re.compile(r"(\w+)\.replaceChildren\(\)")
BUILT = re.compile(r"(\w+)\s*=\s*build\w*\(")
FETCHED = re.compile(r"\b(\w+)\s+(?:=|of)\s+document\.(?:getElementById|querySelector)")
# Every way a node reaches a parent. insertAdjacentHTML is not one of them: it
# takes markup, it only ever adds, and patchOpenPanes is written on it.
INSERTS = ("append", "appendChild", "prepend", "insertBefore", "replaceWith",
           "insertAdjacentElement", "before", "after")


def test_a_function_never_empties_a_container_and_fills_it_back_up():
    """The hole the two innerHTML rules leave, and the one the run list sat in.

        el.replaceChildren();
        for (const r of rows) el.append(buildRunRow(r));

    passes every check above — the emptying is a bare replaceChildren(), the
    markup is inside a build… function, nothing assigns innerHTML — and it does
    exactly what innerHTML does. Every node under `el` is thrown away and made
    again, four seconds at a time, and the reader's selection goes with it. An
    independent reviewer wrote that bypass against the rules as they stood and it
    passed all of them; the run list itself was written that way.

    So emptying a container and putting built nodes back into it are two things
    one function may not both do. Emptying alone stays allowed — a log pane whose
    file was rewritten under it has to start again — and adding alone stays
    allowed, which is how a new round card reaches the board without moving the
    cards already on it.
    """
    src, regs = regions(ui.page_html())
    assert len(EMPTIED.findall(src)) == src.count("replaceChildren("), \
        "a replaceChildren() whose target is not a plain name, so no rule can follow it"
    for name, s, e in regs:
        body = src[s:e]
        built = set(BUILT.findall(body))
        for target in EMPTIED.findall(body):
            for method in INSERTS:
                for arg in re.findall(rf"\b{target}\.{method}\(\s*([^,)]*)", body):
                    arg = arg.strip()
                    assert not (arg.startswith("build") or arg in built), \
                        f"{name} empties {target} and fills it back up with {arg}"


def test_only_a_build_function_empties_an_element_it_looked_up():
    """The same bug read off what a function does rather than off its name.

    An element found with document.getElementById is one the reader is already
    looking at. Emptying it is a first-render act — the board is cleared when
    another run is chosen — so it belongs to a build… function, which the naming
    contract says runs once per element. Any other function that empties one is
    redrawing the page, whatever it does next.

    Two ways out of this, both of them things someone writes by accident rather
    than to get past a test. `replaceChildren()` is the only emptying it knows:
    `el.textContent = ''` empties exactly as thoroughly and matches nothing here.
    And a function renamed to start with `build` is exempt by this rule alone —
    what catches that one is the rule above, which reads no names, and the pinned
    interval count. No single rule in this section covers the shape; three
    partial ones do, and each says which part is its own.

    A pane reached from a node the function already holds is not this: that is
    patchOpenPanes emptying its own body on a truncation signal, which is AC-11.
    """
    src, regs = regions(ui.page_html())
    for name, s, e in regs:
        if name.startswith("build"):
            continue
        body = src[s:e]
        fetched = set(FETCHED.findall(body))
        for target in EMPTIED.findall(body):
            assert target not in fetched, \
                f"{name} empties {target}, which it fetched out of the document"


def test_a_closure_neither_builds_markup_nor_empties_an_element():
    """regions() charges an arrow function to whichever declaration sits above it,
    so a closure under a build… name inherits permission it was never given. The
    two operations the rules govern are banned from closures outright: a click
    handler that has to clear the board calls a build… function that does it."""
    src = script_of(ui.page_html())
    for body in closures(src):
        for banned in ("innerHTML", "replaceChildren"):
            assert banned not in body, f"a closure uses {banned}: {body[:60]}"
    # An arrow with no braces has no body to walk, so it is read off the line.
    for line in src.splitlines():
        if "=>" in line:
            assert "innerHTML" not in line and "replaceChildren" not in line, \
                f"an arrow function redraws part of the page: {line.strip()}"


def test_a_poll_never_calls_a_build_function_for_its_side_effect():
    """The way round the rules above that the page hands out itself.

    buildBoard() is allowed to empty the board because it is a build… function
    and choosing a run is a first-render act. `poll() { buildBoard(); ... }` is
    then a redraw with one extra step, and every check above says yes: the
    emptying is bare, it is inside a build… name, and the poll function assigns
    nothing at all. A reviewer served the version with one plainly-named helper
    in between to a browser — the board came back empty every 2 seconds, with the
    suite green — which is why this reads one hop out and not only the callers.

    A build… function is called for what it returns. On the poll path the value
    has to be used — put in a variable or passed to an insert — so a call written
    for what it does to the document instead stands out on the line.

    Two hops out it is invisible again, and the browser checklist carries that.
    """
    src, regs = regions(ui.page_html())
    reached = poll_path(src, regs)
    for name, s, e in regs:
        if name not in reached:
            continue
        for line in src[s:e].splitlines():
            for m in re.finditer(r"\bbuild\w*\(", line):
                before = line[:m.start()].rstrip()
                assert before.endswith(("=", "(", ",", "return")), \
                    f"{name} calls {m.group()} for what it does, not for what it hands back"


def test_a_poll_writes_text_only_where_the_text_changed():
    """Assigning textContent builds a NEW text node even when the string has not
    changed, and the reader's selection lives in the old one. So a run list that
    finds its rows and writes every field back is still a run list that loses a
    selection every 4 seconds, and no rule about innerHTML would say a word.

    Every text a poll writes goes through one guarded setter instead. A build…
    function writes straight to a node it has just made, where there is nothing
    to lose.

    One hop out as well, because that is where a reviewer put it: patchRunRow
    calling `writeText(el, t)`, and `function writeText(el, t) { el.textContent =
    t; }` sitting quietly beside it. In a browser the selection went inside three
    polls and the text node came away detached, while every rule keyed on
    patchRunRow's own body stayed green.
    """
    src, regs = regions(ui.page_html())
    assert re.search(r"textContent\s*!==", src), "nothing compares the text before writing it"
    reached = poll_path(src, regs)
    for name, s, e in regs:
        if name in reached:
            assert ".textContent =" not in src[s:e], \
                f"{name} runs on every poll and writes text without checking it changed"


def test_the_intervals_are_unchanged():
    """The saving comes from sending less per poll, not from polling less often.

    And these are the only two. Every rule above is keyed on the name of the
    function an interval calls, so a third interval calling anything else is a
    poll path that none of them is looking at: a reviewer wrote four separate
    redraws that way — rows swapped for freshly built ones, the rail emptied with
    textContent and refilled, innerHTML reached as el['inner' + 'HTML'], a build…
    function redrawing the whole rail — and every one of them left the suite green.
    Counting the intervals closes all four at once, and it is the cheapest line in
    this file.
    """
    html = ui.page_html()
    assert "setInterval(runs, 4000)" in html
    assert "setInterval(poll, 2000)" in html
    assert html.count("setInterval(") == 2, \
        "a third interval is a poll path no rule above is watching"


def test_the_page_polls_log_slices():
    html = ui.page_html()
    assert "/api/log?" in html, "the page still asks for whole log files"
    assert "offset=" in html


def test_the_page_declares_the_names_the_later_panes_bind_to():
    src = script_of(ui.page_html())
    for name in ("buildRoundCard", "buildLogPane", "patchRounds", "patchOpenPanes"):
        assert f"function {name}(" in src, f"{name} is the name other panes call"


def test_a_poll_drops_names_that_came_back_for_a_run_the_reader_has_left():
    """The board is cleared when a run is chosen, and a poll already in flight
    fills it back up with the run it was chosen over.

    poll() awaits /api/logs and then patches whatever `sel` says by the time the
    reply lands. Click another run inside that window and the old run's log names
    arrive on the freshly cleared board, where the panes get stamped with the new
    run's directory. The window is 1.4 ms of every 2,000 on this machine, so it
    is rare — and it never heals, because patchRounds only ever adds cards. The
    phantom rounds sit there until the reader reselects or reloads, and opening
    one asks /api/log for a file that is not in that directory: a 404 every 2
    seconds, for ever, behind a pane that can never fill.

    So the run has to be held before the await and carried down, and `sel` may be
    read after the await for one thing only — to ask whether it still says the
    same run.
    """
    src = function_source(ui.page_html(), "poll")
    held = re.search(r"const (\w+) = sel;", src)
    assert held, "poll() reads the global `sel` after its await instead of holding it"
    dir_ = held.group(1)
    after = src[src.index("await"):]
    assert re.findall(r"\bsel\b", after) == ["sel"], \
        "`sel` is read after the await for something other than the run-changed check"
    assert re.search(rf"\b{dir_}\s*[!=]==\s*sel\b|\bsel\s*[!=]==\s*{dir_}\b", after), \
        "nothing checks whether the reader moved on while the names were in flight"
    assert re.search(rf"patchRounds\({dir_}\b", src), "the names are patched in without their run"

    rounds = function_source(ui.page_html(), "patchRounds")
    assert re.match(r"function patchRounds\(\w+,", rounds), \
        "patchRounds is not told which run the names it is given came from"
    assert not re.search(r"\bsel\b", rounds), \
        "patchRounds reads the global instead of the run it was handed"


def test_only_an_open_pane_is_polled():
    """AC-9: a collapsed pane sends no request. <details> carries `open` itself, so
    the selector is the whole of the rule and there is no toggle state to drift."""
    src = function_source(ui.page_html(), "patchOpenPanes")
    assert "[open]" in src, "every pane is polled, collapsed or not"
    assert "createElement('details')" in function_source(ui.page_html(), "buildLogPane")


def guarded_block(src, signal):
    """The `if (…)` naming `signal`, and exactly the statement or block it governs.

    Counted out by braces rather than by a fixed number of lines. A window of a
    few lines reaches past a short block into whatever follows it, so a branch
    that had lost its replaceChildren() borrowed the next branch's and the test
    stayed green.
    """
    lines = src.splitlines()
    i = next((n for n, ln in enumerate(lines)
              if ln.strip().startswith("if (") and re.search(signal, ln)), None)
    assert i is not None, f"nothing in patchOpenPanes tests {signal}"
    out, depth = [], 0
    for ln in lines[i:]:
        out.append(ln)
        depth += ln.count("{") - ln.count("}")
        # A condition with nothing after it governs the line below instead.
        if depth <= 0 and not ln.rstrip().endswith(")"):
            break
    return "\n".join(out)


def test_the_page_replaces_a_pane_on_either_truncation_signal():
    """AC-11's two signals, and the pane needs both, each on its own.

    A reply offset below the one sent means the stored offset was past the end of
    the file. head_truncated turning true means append_log cut the head *below*
    the stored offset — it keeps the last 500 KB of a 1 MB file — so the offset
    still looks valid while every byte position behind it has moved, and no size
    check can catch it.
    """
    src = function_source(ui.page_html(), "patchOpenPanes")
    assert "replaceChildren()" in guarded_block(src, "head_truncated"), \
        "the head-truncation signal does not empty the pane"
    assert "replaceChildren()" in guarded_block(src, r"\.offset\s*<\s*offset"), \
        "a reply offset below the one sent does not empty the pane"


def test_a_head_truncated_pane_asks_again_from_the_start_of_the_new_file():
    """The reply that carries the signal cannot be rendered.

    It begins at the stored offset, which was a line ending in the old file and
    is an arbitrary byte in the rewritten one, so the pane would open on half a
    word with nothing above it and no way for the reader to tell why. The pane
    goes back to 0 instead and pays one 2-second round trip for a file it can
    show whole. The stored flag has to go true in the same breath, or the next
    reply fires the same signal and the pane never renders anything at all.
    """
    block = guarded_block(function_source(ui.page_html(), "patchOpenPanes"), "head_truncated")
    assert re.search(r"\.offset\s*=\s*'0'", block), "the pane keeps an offset into a file that moved"
    assert re.search(r"\.cut\s*=\s*'1'", block), "the signal fires again on the next reply, for ever"
    assert "continue" in block, "this reply's text is rendered from the wrong place anyway"


def test_a_pane_stores_the_byte_size_as_its_next_offset():
    """`size` counts bytes and `text` is a decoded string. They differ on any log
    holding a non-ASCII character — 24,847 against 24,778 on a real one on this
    machine — and only `size` is an offset. Sending text.length back would ask for
    a position inside a line and the reader would see part of it twice."""
    src = function_source(ui.page_html(), "patchOpenPanes")
    assert re.search(r"\.offset\s*=\s*d\.size\b", src), "the pane stores something else"
    assert "text.length" not in script_of(ui.page_html()), "a character count used as an offset"


def test_two_passes_cannot_append_the_same_bytes_twice():
    """A pane's next offset is written only when its own reply lands. A second
    pass starting before the first has finished — the 2-second interval on top of
    the pass that opening a pane kicks off — reads the offset the first was still
    working from, asks for the same bytes and appends them again, and the reader
    watches the same lines arrive twice.

    Every clause below is one way the guard is written and does nothing. A flag
    declared inside the function is false on every call. One raised after the
    first await is raised too late, because that await is where the second pass
    gets in. One cleared anywhere but a finally stays raised for ever the first
    time a pane's poll throws, and no pane is ever polled again.
    """
    src = script_of(ui.page_html())
    fn = function_source(ui.page_html(), "patchOpenPanes")
    held = re.search(r"if \((\w+)\) return;", fn)
    assert held, "two passes can run at once"
    flag = held.group(1)
    top_level = "\n".join(ln for ln in src.splitlines() if not ln.startswith(" "))
    assert re.search(rf"^let {flag} = false;$", top_level, re.M), \
        f"{flag} does not outlive the call, so every pass gets its own false one"
    assert fn.index(f"{flag} = true;") < fn.index("await"), \
        f"{flag} is raised after the await a second pass would arrive during"
    assert re.search(rf"finally \{{\s*{flag} = false;", fn), \
        f"{flag} is not cleared in a finally, so one throw stops every later poll"


def test_a_pane_follows_the_bottom_only_from_the_bottom():
    """AC-13. Someone who scrolled up is reading; do not yank them back."""
    src = function_source(ui.page_html(), "patchOpenPanes")
    assert re.search(r"scrollHeight\s*-\s*\w+\.scrollTop\s*-\s*\w+\.clientHeight\s*<=\s*4", src), \
        "no 4-pixel test of whether the reader was at the bottom"
    line = next(ln for ln in src.splitlines() if "scrollTop =" in ln)
    assert line.strip().startswith("if ("), "the pane scrolls to the bottom unconditionally"


# ---------- the run list ----------


def test_the_page_reads_run_times():
    """AC-3 on the page. The left rail used to show five rows all reading
    example-hello with nothing to tell them apart: no time, no duration, no id.

    Both times null is a run known only from its log files, and it shows no time
    at all rather than a wrong one.
    """
    src = script_of(ui.page_html())
    assert "start_time" in src, "the page never reads when a run started"
    assert "close_time" in src, "the page never reads when a run closed"
    assert "toLocaleString()" in src, "the start time is not shown in the reader's own zone"
    dur = function_source(ui.page_html(), "duration")
    assert "3600" in dur, "no hour boundary, so a two-hour run reads as 127 minutes"
    assert "padStart(2" in dur, "the minutes and seconds are not padded"
    row = function_source(ui.page_html(), "patchRunRow")
    assert "start_time" in row and "close_time" in row, "the row shows no time"
    assert "duration(" in row, "a row shows when its run started and not how long it took"
    assert row.count("setText(") >= 5, "a field of the row is written some other way"


def test_a_row_is_keyed_on_the_workflow_id_and_shows_the_directory():
    """The other half of AC-3, and the reason the two keys may not be swapped.

    Two workflows of one run directory are two rows: same text, same log files,
    different ids. Keyed on the directory they collapse into one row, and whichever
    of the two came second could never be selected at all.
    """
    build = function_source(ui.page_html(), "buildRunRow")
    assert re.search(r"\.dataset\.id\s*=\s*\w+\.id\b", build), \
        "the row is stamped with something other than the workflow id"
    assert re.search(r"sel\s*=\s*\{", build), "a click selects something other than {id, dir}"
    patch = function_source(ui.page_html(), "patchRuns")
    assert "dataset.id" in patch, "patchRuns finds its rows by something other than the id"
    assert "/api/logs?dir=" in function_source(ui.page_html(), "poll"), \
        "the log endpoint takes a run directory, never a workflow id"


def test_the_run_list_is_patched_and_never_rebuilt():
    """AC-14 where the reviewer found it broken: the rail was emptied and every row
    made again every 4 seconds, so a selection in one lasted 13 seconds at most.

    A row that is already there is found and updated. A row for a run the reply no
    longer carries is removed. A new one is inserted where the server put it,
    which moves nothing that is already on screen.
    """
    src = function_source(ui.page_html(), "patchRuns")
    assert "replaceChildren" not in src, "the run list still empties itself"
    assert "insertBefore" in src, "a new row cannot be put in place without moving the others"
    assert ".remove()" in src, "a run that has gone from the reply stays on screen for ever"
    assert "innerHTML" not in src
    runs_fn = function_source(ui.page_html(), "runs")
    assert "patchRuns(" in runs_fn, "the poll does not go through the patch"
    assert "buildRunRow" not in runs_fn, "the poll builds rows itself"


# ---------- the hash, and how young a time is ----------


def block_at(src, i):
    """The `{ … }` starting at the first brace at or after `i`, and where it ends.

    The brace counting `closures` does, out here where a test about one particular
    block can share it. Enough for a page with no unbalanced brace inside a string.
    """
    opening = src.index("{", i)
    depth = 0
    for j in range(opening, len(src)):
        depth += (src[j] == "{") - (src[j] == "}")
        if depth == 0:
            return src[opening:j + 1], j + 1
    raise AssertionError("a block in the page is never closed")


def try_blocks(src):
    """Every `try { … } catch … { … }` in `src`, as (body, handler).

    A `try` reads as one word; which throw lands in which catch does not, and
    nesting is the whole question here — the poll's own try is around everything,
    so `decodeURIComponent` is inside it either way and finding the word `try`
    above the decode proves nothing. The braces are counted instead, so a test can
    ask what else is inside the block that catches a failure.
    """
    out = []
    for m in re.finditer(r"\btry\s*\{", src):
        body, after = block_at(src, m.start())
        assert re.match(r"\s*catch\s*(?:\([^)]*\))?\s*\{", src[after:]), \
            "a `try` in the page has no catch of its own"
        out.append((body, block_at(src, after)[0]))
    return out


def test_a_click_writes_the_row_id_to_the_hash():
    """AC-13. Clicking a run puts its id in the URL, so reloading the page or
    pasting the URL to someone else lands on the same run.

    Written by the one handler that also sets `sel`, so the address bar and the
    board cannot come apart, and off the same entry: the id is the workflow id, or
    the directory name on a run known only from its log files.

    Encoded, because that directory name is whatever the owner typed. A raw space
    or `%` in a hash is a malformed URL, and what comes back out of it on the next
    load has to be the name again, character for character.
    """
    build = function_source(ui.page_html(), "buildRunRow")
    write = re.search(r"location\.hash = encodeURIComponent\((\w+)\.id\)", build)
    assert write, "a click writes no hash, or writes an id that was never encoded"
    picked = re.search(r"sel = \{id: (\w+)\.id", build)
    assert picked and picked.group(1) == write.group(1), \
        "the hash is written off something other than the entry the click selects"


def test_the_load_selection_prefers_the_hash_and_keeps_the_guard():
    """AC-13's other half. On load the page opens the run the hash names, archived
    or not — a deep link to an archived run keeps working — and when the hash names
    no run in the list, or there is no hash, the first row the reader can see.

    Not `firstElementChild`. Archiving the newest finished run is the ordinary
    case and what `lg rm` produces, and row zero is then a `display:none` row: the
    page would open every time on the one run the reader deliberately took off the
    rail, with `.sel` on something nobody can see. A row the server never marked is
    not an archived run, so the stamp reads the same way on a rail where nothing is
    archived.

    "The row the reader can see" is the toggle's question as well as the stamp's,
    so the fallback is two things ORed and this checks the shape of both. Which
    reading of the toggle is the right one — and that it is not inverted, which
    would open the page on a hidden run every time — is
    test_the_load_selection_skips_an_archived_first_row's, with the rest of the
    toggle.

    The row is worked out into a local before the guard because the guard's shape
    is pinned and narrowly: test_the_first_run_is_clicked_only_when_nothing_is_
    chosen_yet matches `if (<no closing paren>) <bare name>.click();`, so the
    lookup cannot be written inline in either half of it. What that guard is for is
    the `!sel`: without it the click fires on every 4-second poll and takes the
    reader's board down with it.
    """
    src = function_source(ui.page_html(), "runs")
    assert src.count(".click()") == 1, \
        "a run is selected in more than one place, so `sel` is set down more than one path"
    guard = re.search(r"if\s*\(([^)]*)\)\s*(\w+)\.click\(\);", src)
    assert guard, "the row is clicked with no `if` in front of it, or not through a bare local"
    cond, held = guard.groups()
    assert re.search(r"!\s*sel\b", cond), "nothing stops the click firing again on every poll"
    # Both halves AND the operator joining them. `!sel` and `target` are each still
    # there in `if (!sel || target)`, and once the rail has a row `target` is
    # truthy on every poll: the click fires every 4 seconds, `sel` is set again,
    # buildBoard runs, and the reader's selection and every log pane they had open
    # go with it — which is word for word what the older guard test says it exists
    # to stop, "with this suite green throughout".
    assert re.search(rf"&&\s*{held}\b|{held}\s*&&", cond), \
        f"the guard is not `nothing chosen yet` AND `a row to click`: `{cond.strip()}`"

    chosen = re.search(rf"const {held} = ([^;]+);", src)
    assert chosen, f"`{held}` is clicked without being worked out into a local first"
    # The two searches and the `||` between them, taken as a whole rather than by
    # splitting on `||`: the fallback carries one of its own now that the toggle is
    # in it, and a split would hand back three pieces of a two-part expression.
    expr = " ".join(chosen.group(1).split())
    pair = re.fullmatch(r"(\w+\.find\(.+?\)) \|\| (\w+\.find\(.+\))", expr)
    assert pair, \
        f"the row is not `the one the hash names, or else the first one showing`: `{expr}`"
    halves = list(pair.groups())

    listed = re.search(r"const (\w+) = \[\.\.\.document\.getElementById\('runs'\)\.children\]", src)
    assert listed, "the rows are not taken off the rail"
    rail = listed.group(1)

    # Found in the list and not by selector, for patchRuns' reason: an id is a
    # workflow id or a directory name, and a directory name is not ours to paste
    # into CSS.
    by_hash = re.fullmatch(rf"{rail}\.find\((\w+) => \1\.dataset\.id === (\w+)\)", halves[0])
    assert by_hash, f"the first choice is not the row whose id the hash names: `{halves[0]}`"
    # `.slice(1)` and not `location.hash` whole: the property keeps the leading `#`,
    # which is in no row's dataset.id, so every deep link falls through to the
    # fallback and AC-13's load half is dead with nothing on screen to show it.
    assert re.search(rf"{by_hash.group(2)} = decodeURIComponent\(location\.hash\.slice\(1\)", src), \
        "the rows are matched against something other than the decoded hash, `#` and all"

    fallback = re.fullmatch(
        rf"{rail}\.find\((\w+) => (\w+) \|\| \1\.dataset\.archived !== '1'\)", halves[1])
    assert fallback, \
        ("the fallback is not `the toggle is showing them, or this row carries no archived "
         f"stamp`: `{halves[1]}`")
    assert re.search(rf"const {fallback.group(2)} = ", src), \
        f"`{fallback.group(2)}` is read out of nowhere rather than worked out in the poll"
    assert "firstElementChild" not in src, \
        "the fallback is the rail's first row, archived or not"


def test_a_junk_hash_reads_as_no_hash():
    """AC-13's failure case, which costs the whole page if it is left to the outer
    catch.

    `runs()` is one try around the fetch, patchRuns, the selection and the header
    writes, with a single catch that writes `server error`. decodeURIComponent
    throws on `#%` — a hash anyone can type and any link can arrive mangled — so an
    unguarded decode skips the only line that ever selects a run, blames the server
    for it, and does it again every 4 seconds for as long as the hash stays. The
    reader gets `server error` on a server that answered every request.

    So the decode has a try of its own, holding nothing else, and a hash that does
    not decode reads as no hash.
    """
    src = function_source(ui.page_html(), "runs")
    assert "decodeURIComponent(" in src, "the hash is read without being decoded"
    caught = [(body, handler) for body, handler in try_blocks(src) if "decodeURIComponent(" in body]
    assert caught, "the decode sits inside no try at all"
    narrow = [(body, handler) for body, handler in caught
              if "fetch(" not in body and "patchRuns(" not in body and ".click()" not in body]
    assert narrow, \
        "the decode is caught no nearer than the poll's own try, so a junk hash costs the poll"
    for body, handler in narrow:
        assert "server error" not in handler, \
            "a hash that does not decode makes the page blame the server"
    held = re.search(r"(\w+) = decodeURIComponent\(", narrow[0][0])
    assert held, "the decoded hash is not held anywhere"
    assert re.search(rf"let {held.group(1)} = '';", src), \
        "the decoded hash does not start out empty, so a throw leaves it holding nothing readable"


AGO_BRANCH = re.compile(r"if \(\s*(\w+)\s*(<=?)\s*(\d+)\s*\)\s*return `\$\{(\w+)\}(\w+) ago`;")


def ago_branches(src):
    """Per unit word, the (tested name, operator, bound, printed name, offset) of its line.

    Keyed on the unit, which is what every question below is about, and carrying
    where the line sits, which the key throws away. Two `if`s that each return read
    in the order they are written and in no other, so a dict of them alone is a
    reading with the order missing.
    """
    return {m.group(5): (m.group(1), m.group(2), int(m.group(3)), m.group(4), m.start())
            for m in AGO_BRANCH.finditer(src)}


def test_ago_reads_minutes_hours_then_absolute():
    """AC-14. Under an hour a run's start time reads in minutes, under a day in
    hours, and older than that it keeps the absolute date it has always shown.

    Pinned off the source, because nothing in this repository can execute
    JavaScript: `35m ago` and `2h ago` are AC-14's illustrations of what the reader
    sees, the numbers in them are computed, and neither string exists anywhere to
    grep for. What is checked instead is what builds them — which value each line
    prints, what that value was floored from, where each branch stops and which one
    is reached first. Those are the places this goes wrong quietly. `<=` at 60 makes
    an hour-old run read `60m ago`; the two names swapped makes a 90-minute run read
    `90h ago`; the difference taken the other way round makes every run on the rail
    read `-1m ago`; an unfloored one reads `2.5166h ago`; and the hours line first
    makes every run under an hour read `0h ago`, because 0 hours is under 24 and the
    minutes line is never reached at all.
    """
    src = function_source(ui.page_html(), "ago")
    params = re.match(r"function ago\((\w+), (\w+)\)", src.strip())
    assert params, "ago no longer takes a start and a now"
    start, now = params.groups()

    def floored(name):
        m = re.search(rf"const {name} = Math\.floor\(([^;]+)\);", src)
        assert m, f"`{name}` is not floored, so it reads with a decimal point in it"
        return " ".join(m.group(1).split())

    branches = ago_branches(src)
    assert set(branches) == {"m", "h"}, \
        f"the relative forms are not `<n>m ago` and `<n>h ago`: {sorted(branches)}"

    tested, op, bound, printed, at_minutes = branches["m"]
    assert printed == tested, "the minutes line prints a value other than the one it tested"
    assert (op, bound) == ("<", 60), \
        f"the minutes branch runs to `{op} {bound}`, so an hour old does not read in hours"
    mins = floored(tested)
    assert re.search(rf"\b{now}\s*-\s*{start}\b", mins), \
        "the minutes are not how long it is since the start"
    assert "60000" in mins or ("1000" in mins and "60" in mins), "the minutes are not minutes"

    tested_h, op_h, bound_h, printed_h, at_hours = branches["h"]
    assert printed_h == tested_h, "the hours line prints a value other than the one it tested"
    assert (op_h, bound_h) == ("<", 24), \
        f"the hours branch runs to `{op_h} {bound_h}`, so a day old never reaches its date"
    hours = floored(tested_h)
    assert re.search(rf"\b{tested}\s*/\s*60\b", hours) or "3600000" in hours, \
        "the hours are not the minutes over 60"

    # Which line is reached first, and not only what each one says. Every bound and
    # every name above is still right with the two swapped over, and the hours line
    # answers first for every run there is: under an hour it is 0 hours, which is
    # under 24, so a run started twenty minutes ago reads `0h ago` and the minutes
    # line below is dead code. The rail then says `0h ago` on everything young.
    assert at_minutes < at_hours, \
        "the hours are tested before the minutes, so nothing young ever reads in minutes"

    assert src.strip().rstrip("}").strip().endswith(f"return {start}.toLocaleString();"), \
        "a run older than a day does not fall through to the date it has always shown"


def test_a_shown_time_carries_its_absolute_form_in_title():
    """AC-14's last line. `2h ago` is the reading a glance wants and the one thing
    it cannot tell you is which 2 hours, so the full timestamp rides along in the
    `title` — on both forms, since hovering a date to be told the date is no worse
    than hovering it for nothing.

    Off the start, never off the clock: a title built from `now` would say the same
    thing on every row on the rail and be wrong about all of them.

    A title is not text, so setText does not cover it and it is written straight.
    That is allowed here where `.textContent =` is not: assigning it makes no text
    node, takes no selection down with it, and the string is the same one four
    seconds later anyway.
    """
    row = function_source(ui.page_html(), "patchRunRow")
    shown = re.search(r"setText\((\w+)\.firstElementChild, (\w+) \? ago\(\2, (\w+)\) : ''\)", row)
    assert shown, "the start time is not written as a relative `ago` off the run's own start"
    when, start, now = shown.groups()
    assert re.search(rf"const {now} = new Date\(\)", row), \
        "`ago` is told a now that is not the clock"
    assert re.search(rf"{when}\.firstElementChild\.title = {start} \? {start}\.toLocaleString\(\) : ''",
                     row), \
        "the shown time carries no absolute form, or one built from something other than the start"


# ---------- the tab: title and favicon ----------


def favicon_link(html):
    """The page's `<link rel="icon">`, as (whole tag, href), from its `<head>`.

    Not `<link[^>]*>`: the href is an inline SVG and carries `>` inside its own
    quotes, so the obvious tag pattern stops in the middle of `<circle` and hands
    back half an attribute. The href is quoted with `"` and contains none, so
    reading to the closing quote is what finds the end of the tag.
    """
    head = re.search(r"<head>(.*?)</head>", html, re.S)
    assert head, "the page has no head"
    tag = re.search(r'<link\s+[^>]*?href="([^"]*)"\s*>', head.group(1), re.S)
    assert tag, "the page ships no <link> with an href in its head"
    return tag.group(0), tag.group(1)


def fav_const(src, name):
    got = re.search(rf'const {name} = "([^"]*)";', src)
    assert got, f"{name} is not a top-level string const in the script"
    return got.group(1)


def test_the_page_ships_a_data_uri_favicon():
    """AC-4. Two things at once, and the second is the quiet one.

    The reader gets a tab icon before the first reply lands, four seconds in. And
    a page with no `<link rel="icon">` at all makes the browser go and ask for
    /favicon.ico by itself, which this server has never had — so the console
    carries a 404 on every load, under which a real one is invisible. The icon is
    a data URI rather than a file for the same reason: a file is a second request
    and a second thing to serve.

    The two consts sit above the first `function` declaration because regions()
    charges everything after a declaration to that declaration, so a const written
    lower down would be read as part of whichever function happened to precede it.
    """
    html = ui.page_html()
    tag, href = favicon_link(html)
    assert 'id="fav"' in tag, "the poll finds the icon by id, and this one has none"
    assert 'rel="icon"' in tag, "a link that is not rel=icon leaves the browser asking for /favicon.ico"
    assert href.startswith("data:image/svg+xml,"), \
        "the icon is a second request rather than an inline URI"

    src = script_of(html)
    fav_const(src, "FAV_IDLE"), fav_const(src, "FAV_WAIT")
    first = DECLARED.search(src)
    assert first, "the script declares no functions, so this file's machinery has moved"
    for name in ("FAV_IDLE", "FAV_WAIT"):
        assert src.index(f"const {name}") < first.start(), \
            f"{name} sits below the first `function`, so regions() charges it to that function"


def test_the_shipped_icon_and_the_idle_icon_are_one_string():
    """The drift nothing but a test can catch.

    The idle URI is written twice — in the tag the page ships and in FAV_IDLE,
    which the poll writes back whenever no run is waiting. Change one and the tab
    shows one icon on load and a different one four seconds later, for ever, with
    the whole suite green. So the two are compared here character for character.

    FAV_WAIT is the same drawing in the waiting pill's blue and nothing else, so
    the tab and the rail say the same thing in the same colour. `%23` is the URI
    encoding of `#`; a raw one would end the data URI at the colour and the
    browser would render nothing.
    """
    html = ui.page_html()
    _, shipped = favicon_link(html)
    src = script_of(html)
    idle, wait = fav_const(src, "FAV_IDLE"), fav_const(src, "FAV_WAIT")
    assert shipped == idle, "the shipped icon and the idle icon have drifted apart"

    fills = [re.findall(r"fill='(%23[0-9a-f]{6})'", u) for u in (idle, wait)]
    assert [len(f) for f in fills] == [1, 1], "an icon does not carry exactly one fill colour"
    for u in (idle, wait):
        assert "#" not in u, "a raw # ends the data URI at the colour, so nothing renders"
    dim, blue = fills[0][0], fills[1][0]
    assert idle.replace(dim, blue) == wait, "the two icons differ somewhere other than the colour"

    rule = re.search(r"\.blue \{([^}]*)\}", html)
    assert rule, "the waiting pill has no rule, so there is no blue to match"
    assert "#" + blue[len("%23"):] in rule.group(1), \
        "the waiting icon is not the colour the waiting pill wears"


def test_the_title_and_favicon_are_written_only_on_change():
    """AC-3's last line and AC-4's. Both are written on the 4-second poll, and
    both are read back before they are written.

    document.title is not an element, so the setText rule that covers every other
    word on this page does not reach it — the guard has to be written by hand and
    pinned here.

    The icon is read back through getAttribute rather than the `.href` property,
    which is the URL the browser parsed and serialised again. This URI carries
    spaces and angle brackets, and nothing promises a serialiser hands those back
    untouched; where one does not, `.href` never equals the string we wrote, the
    guard stops holding, and the icon is replaced every four seconds — the work
    the guard exists to stop, on some browsers and not the one it was tried in.
    """
    src = function_source(ui.page_html(), "runs")
    assert "document.title !==" in src, "the title is written without reading it back"
    assert src.index("document.title !==") < src.index("document.title ="), \
        "the title is assigned before anything compares it"

    assert re.search(r"getAttribute\('href'\) !== \w+", src), \
        "the icon is written without reading the attribute back"
    assert re.search(r"setAttribute\('href', \w+\)", src), "the icon is set some other way"
    assert src.index("getAttribute('href')") < src.index("setAttribute('href'"), \
        "the icon is assigned before anything compares it"
    assert not re.search(r"\.href\s*=", src), \
        "the .href property is the browser's copy of the URL, not the string that was written"

    # Which icon goes with which count, and not merely that both names appear.
    # Swapped, the page is the exact inverse of AC-4 — a blue dot when nothing
    # needs the reader and a dim one when a run does — and every rule above it
    # here still holds, because both consts are still read and still written
    # through the guard. Only the order says which is which.
    icon = re.search(r"const \w+ = (\w+) \? FAV_WAIT : FAV_IDLE;", src)
    assert icon, "the icon a waiting run gets is not FAV_WAIT, or the two are the wrong way round"
    counted = re.search(r"const (\w+) = \w+\.filter\(", src)
    assert counted and icon.group(1) == counted.group(1), \
        "the icon is chosen off something other than the count of waiting runs"


def test_archived_rows_do_not_count_toward_the_title():
    """AC-3. N is counted off the reply, never off the rail.

    The rail is what the reader chose to look at, and it answers the wrong
    question twice over: an archived run they have hidden has no row to count, and
    an archived run they are showing has a row that must not be counted. The reply
    is the same list either way, so the count is taken from it and the filter says
    which rows are in.

    `archived` is the server's mark and Task 8 is what starts writing it. Until
    then no reply carries the key, `!r.archived` reads undefined as not archived,
    and every row counts — which is also the right reading afterwards: a row the
    server never marked is not an archived run. So this pins the filter, not the
    hiding, and the archived run itself is Task 8's to test.
    """
    src = function_source(ui.page_html(), "runs")
    served = re.search(r"patchRuns\((\w+)\)", src)
    assert served, "the poll no longer hands the reply's rows to patchRuns"

    count = re.search(r"const (\w+) = (\w+)\.filter\((\w+) => ([^)]*)\)\.length", src)
    assert count, "the poll never counts the waiting rows"
    held, rows, row, expr = count.groups()
    assert rows == served.group(1), \
        "the count comes from something other than the reply the rows came from"
    # The whole expression, not two substrings of it, because the operator joining
    # them is the half that decides what the tab says and the half a substring
    # cannot see. Swap the `&&` for `||` and both substrings are still there,
    # every row passes — no reply carries `archived`, so `!r.archived` is true on
    # all of them — and the tab reads `(4) loopgraph` with nothing waiting at all.
    # Task 8 comes back to this exact line, which is why it is pinned whole.
    assert " ".join(expr.split()) == f"{row}.state === 'waiting' && !{row}.archived", \
        f"the count is not `state === 'waiting'` AND not archived; it reads `{expr.strip()}`"

    title = re.search(rf"{held} \? `\(\$\{{{held}\}}\) loopgraph` : 'loopgraph'", src)
    assert title, \
        "the tab does not read `(N) loopgraph` off the count, and `loopgraph` when it is zero"


# ---------- the state board ----------


def load_lg():
    """`lg` has no .py extension, so it loads by path, under this file's own name."""
    root = Path(ui.__file__).resolve().parent
    loader = SourceFileLoader("lg_cli_board", str(root / "lg"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("lg_cli_board", loader))
    loader.exec_module(mod)
    return mod


def test_the_board_fetches_the_ledger_by_workflow_id():
    """The board's whole reason for existing. Until now the page asked only for
    log names, so it could say a run was held and never what it was asking."""
    src = function_source(ui.page_html(), "poll")
    assert "/api/run?id=" in src, "the poll never asks for the run's state"
    assert "patchBoard(" in src, "the reply reaches no patch"


def test_the_board_copy_is_pinned():
    """Every line the board says about a run's state, and the one gesture that
    matters: one click on the command selects the whole of it."""
    html = ui.page_html()
    for line in ("temporal unreachable — logs only",
                 "no workflow for this run",
                 "ledger did not answer this poll; the run is still there",
                 "awaiting: ",
                 "no card was sent; the lg approve command is the only way to answer",
                 "no items yet"):
        assert line in html, f"the page no longer says {line!r}"
    assert "user-select:all" in html, "the answer command cannot be selected in one gesture"


def test_the_page_and_lg_status_use_the_same_no_card_line():
    """AC-4 and AC-23 are one sentence written twice, in two files, with no shared
    constant between them: `lg` cannot import a page string and `ui.py` cannot
    import from a file with no extension without loading it.

    So the two are held equal here. The line is taken out of `lg status`'s own
    output rather than typed again — a copy in this test would pin the page to the
    test and let the terminal drift away from both. The two ledgers differ in one
    key, so the line is whatever `format_status` says when no card was sent and
    does not say when one was.
    """
    lg = load_lg()
    awaiting = {"kind": "decision", "question": "which port?", "options": {"A": "8400"},
                "telegram": False, "answer_with": "lg approve run-toy-ab12cd <answer>"}
    silent = set(lg.format_status({"status": "running", "awaiting": awaiting}).splitlines())
    carded = set(lg.format_status(
        {"status": "running", "awaiting": dict(awaiting, telegram=True)}).splitlines())

    only = silent - carded
    assert len(only) == 1, f"telegram false changes more than one line of lg status: {only}"
    # lg indents the line under the awaiting block; the page has no terminal to
    # indent in. The sentence itself is what has to be the same.
    assert only.pop().strip() in ui.page_html(), \
        "the page and lg status word the no-card case differently"


def test_every_section_exists_after_the_board_is_built():
    """The constraint the patches are written against: buildBoard runs on
    selection and makes every section, hidden or not, so a patch never has to
    create one — and a poll therefore never builds part of the page."""
    build = function_source(ui.page_html(), "buildBoard")
    for section in ("state", "why", "awaiting", "sweep", "items", "rounds", "diff"):
        assert f'id="{section}"' in build, f"#{section} is not built with the board"


def test_the_round_cards_leave_the_board_for_their_own_section():
    """The board is seven sections now, and the log cards are one of them. Left
    appending to #board they would land on top of the state sections."""
    src = function_source(ui.page_html(), "patchRounds")
    assert "getElementById('rounds')" in src, "the log cards still go straight onto the board"
    assert "getElementById('board')" not in src


def test_a_ledger_with_no_items_key_reads_as_an_empty_list():
    """/api/run serves a closed workflow's recorded result untouched, and two runs
    on this machine closed before `items` was a ledger key. The key is absent, not
    empty, so `ledger.items.map(...)` is a TypeError on a run the owner can click
    today — and the section must read `no items yet`, not break the board."""
    src = function_source(ui.page_html(), "patchItems")
    assert re.search(r"ledger\.items\s*\)?\s*\|\|\s*\[\]", src), \
        "the items are read with no default for a ledger that carries no items key"
    assert not re.search(r"\.items\s*[.\[]", src), "a call or an index straight on ledger.items"


def test_an_item_row_shows_a_short_commit_or_a_parked_reason():
    """AC-7. A full commit hash is 40 characters of noise beside the item text,
    and a parked item's reason is the only thing that says why it stopped."""
    src = function_source(ui.page_html(), "patchItemRow")
    assert "'done'" in src and "'parked'" in src, "the two statuses with a detail"
    assert re.search(r"slice\(0,\s*10\)", src), "the commit is not cut to 10 characters"
    assert ".reason" in src, "a parked item says nothing about why"


def test_the_items_row_has_a_kind_slot_written_by_the_patch():
    """AC-29 and AC-11. A sweep item and a convergence item are the engine's own
    work, and nothing else in the row says so: same number, same pill, same line of
    text as an item the owner wrote. A brief item gets no word at all — every run
    before this phase is brief items end to end, and a label on every row of every
    one of them is noise — and an entry from one of those runs carries no `kind`
    key, which reads as `brief` here exactly as it does in `lg status`.

    The word goes in a span the builder puts there, so the patch writes text into a
    node that is already on screen rather than markup into a row the reader may
    have a selection in.
    """
    html = ui.page_html()
    assert 'class="kind"' in function_source(html, "buildItemRow"), \
        "the row has no slot for the kind, so a patch would have to write markup to say it"
    src = function_source(html, "patchItemRow")
    assert re.search(r"setText\(kind,", src), "the kind is written some way other than setText"
    assert re.search(r"!==\s*'brief'", src), \
        "a brief item is labelled `brief`, or a missing kind reaches the page as `undefined`"


def test_a_sweep_item_row_shows_its_group_through_settext():
    """AC-9. A sweep item takes one path group, and the row has to say which: two
    items of one detector are otherwise the same number, the same pill and the
    same line of text.

    It goes in the span the kind word already has, because a column of its own
    would be empty on every row of every brief run. And it is written as text like
    everything else a poll writes, so a group named `<b>x</b>` reaches the reader
    as those eight characters — this file cannot see that, and the browser
    checklist's item 11 is where it is looked at.
    """
    src = function_source(ui.page_html(), "patchItemRow")
    assert "entry.group" in src, "the row never reads the item's group"
    assert "sweep · " in src, \
        "the page puts something other than lg status's separator between the two words"
    assert re.search(r"setText\(kind,", src), "the group is written some way other than setText"


def test_pass_rows_carry_the_breakdown_through_settext():
    """AC-9. The other half of the same line in `lg status`: how many candidates a
    pass found in each directory, in brackets on the end of the pass row.

    Sorted by name, as the terminal sorts it — the detectors report in whatever
    order they were run, and a breakdown that follows them moves a directory
    around the line from one pass to the next. The whole line is written with
    setText, so a directory name is text and never markup.
    """
    html = ui.page_html()
    src = function_source(html, "patchSweep")
    assert ".groups" in src, "the pass rows never read the detectors' groups"
    assert ".sort()" in src, \
        "the breakdown keeps the order the detectors reported, which lg status sorts by name"
    assert re.search(r"setText\(row,", src), "the row is written some way other than setText"
    for piece in (" candidates (", ") ("):
        assert piece in html, f"the page no longer says {piece!r}"


def test_the_sweep_copy_is_pinned():
    """Every word the sweep section says that was not read off a ledger, plus the
    two ends of it: the section is made once with the board, and a poll fills it."""
    html = ui.page_html()
    assert 'id="sweep"' in function_source(html, "buildBoard"), \
        "the sweep section is not built with the board, so a poll would have to make it"
    for line in ("(no passes yet)", "candidates", "(incomplete: ", "ended: "):
        assert line in html, f"the page no longer says {line!r}"
    assert "patchSweep(" in function_source(html, "patchBoard"), \
        "the section is built with the board and then filled by nothing"
    src = function_source(html, "patchSweep")
    assert re.search(r"\.hidden\s*=\s*!\w+;", src), \
        "the section's visibility does not follow the ledger's sweep key"


def test_the_page_and_lg_status_share_the_sweep_lines():
    """AC-28 and AC-29 are the same four lines written twice, the way the no-card
    line above is: `lg` cannot import a page string and `ui.py` cannot import from a
    file with no extension without loading it.

    So the templates are read out of `lg` and never typed again here — a copy in
    this test would pin the page to the test and let the terminal drift away from
    both. Every piece outside a `{...}` placeholder is fixed text the page has to
    say, ` candidates (incomplete: ` included: that one is what stops the incomplete
    clause being pasted on as a second string, which is a line the two files could
    then word differently.
    """
    lg = load_lg()
    html = ui.page_html()
    for key, template in lg.SWEEP_LINES.items():
        for piece in re.split(r"\{[^}]*\}", template):
            assert piece in html, \
                f"the page does not say {piece!r}, which lg status's {key} line does"
    assert "', '" in function_source(html, "patchSweep"), \
        "the failed detectors are joined with something other than lg status's separator"


def test_pass_rows_are_keyed_and_built_once():
    """Passes only grow, so the section is never emptied and filled back up. A row
    already on screen is found by its pass number and written over, and a new one is
    inserted where the ledger has it — the map diff the items list is written on,
    for the reason it is written on it: a reader with a line selected keeps it.
    """
    html = ui.page_html()
    src = function_source(html, "patchSweep")
    assert "buildPassRow(" in src, "the rows come from somewhere other than the builder"
    for m in re.finditer(r"\bbuildPassRow\(", src):
        assert src[:m.start()].rstrip().endswith("="), \
            "patchSweep calls buildPassRow for what it does, not for the row it hands back"
    assert "insertBefore" in src, "a new pass row cannot arrive without moving the rows above it"
    assert ".remove()" in src, "a row whose pass the reply no longer carries stays for ever"
    build = function_source(html, "buildPassRow")
    assert re.search(r"dataset\.pass\s*=", build), \
        "a pass row carries no key, so its text could only be matched to it by position"


def test_the_question_is_left_out_when_it_was_never_recorded():
    """A workflow that started before this phase recorded no `question`, and its
    card is still up. Everything else about the block is worth showing, so the
    question is hidden rather than drawn as an empty box under the heading."""
    src = function_source(ui.page_html(), "patchAwaiting")
    assert re.search(r"\.hidden\s*=\s*!\w+\.question", src), \
        "the question element is drawn whether or not there is a question"


def test_the_awaiting_block_goes_when_the_workflow_pops_it():
    """AC-6. The workflow removes `awaiting` the moment the owner answers, so the
    page drops the block on its next poll — 2 seconds at the outside. Nothing
    else is needed for that, and nothing may keep the block alive across a reply
    that no longer carries one."""
    src = function_source(ui.page_html(), "patchAwaiting")
    assert re.search(r"\.hidden\s*=\s*!\w+;", src), \
        "the block's visibility does not follow the ledger's awaiting"


def test_the_waiting_pill_has_its_own_class():
    """AC-2. A word is only a signal if it looks like one: `waiting` in the same
    yellow as `running` is the rail saying what it said before, on a page whose
    job is picking one row out of fifteen.

    `waiting` is not a status the engine writes anywhere — grep the workflows and
    it is not there. It is the word run_entry and patchState compute from
    `awaiting` and the run's own times, so nothing else on the page maps to its
    colour either.

    What this cannot see is the colour itself. Checklist item 4 is where the pill
    is looked at.
    """
    html = ui.page_html()
    table = re.search(r"const pill = \w+ => \(\{(.*?)\}\[", script_of(html), re.S)
    assert table, "the pill map is no longer one object literal, so this cannot read it"
    classes = dict(re.findall(r"'?([\w-]+)'?\s*:\s*'([\w-]+)'", table.group(1)))
    assert classes.get("running") == "yellow", "the map this is written against has moved"
    assert classes.get("waiting") == "blue", "a waiting run wears no class of its own"
    assert [s for s, c in classes.items() if c == "blue"] == ["waiting"], \
        "another state wears the waiting colour, so the rail tells them apart by nothing"
    rule = re.search(r"\.blue \{([^}]*)\}", html)
    assert rule, "the waiting pill has a class and no style, so it reads as plain text"
    assert "background" in rule.group(1) and "color" in rule.group(1), \
        "a pill with only half a colour is not a pill"


def test_the_board_pill_says_waiting_only_on_a_live_run():
    """AC-2's second pill. The rail reads `waiting` off run_entry; the board has
    to reach the same word from the same two facts, or checklist item 4 finds the
    page contradicting itself — a row reading `waiting` above a board reading
    `stopped` is worse than either alone, because the reader cannot tell which
    half to believe.

    Liveness comes off the row's `data-live` stamp and never off the ledger's
    status, for the reason
    test_only_a_run_that_is_still_open_claims_a_round_is_in_progress records: an
    engine failure leaves a closed workflow's ledger reading `running`, so the
    ledger is exactly the thing that cannot answer this question.
    """
    html = ui.page_html()
    src = function_source(html, "patchState")
    args = re.match(r"function patchState\(\w+, (\w+)\)", src)
    assert args, "patchState is not told whether the run is still open"
    guard = re.search(r"const (\w+) = ([^;]*) \? 'waiting' : ([^;]+);", src)
    assert guard, "the board pill never says `waiting`"
    assert re.search(r"\w+\.awaiting\b", guard.group(2)), \
        "the waiting word is not read off the question the ledger is holding"
    assert re.search(rf"\b{args.group(1)}\b", guard.group(2)), \
        "the waiting word is not gated on whether the run is still open"
    assert "status" in guard.group(3), "a run that is not waiting says something other than its status"
    assert "|| 'unknown'" in guard.group(3), "a ledger carrying no status leaves the pill empty"
    word = guard.group(1)
    assert re.search(rf"pill\({word}\)", src) and re.search(rf"setText\(\w+, {word}\)", src), \
        "the pill's colour and the pill's word are read off two different values"

    board = function_source(html, "patchBoard")
    passed = re.match(r"function patchBoard\(\w+, (\w+)\)", board)
    assert passed, "patchBoard is not told whether the run is still open"
    assert re.search(rf"patchState\(\w+, {passed.group(1)}\)", board), \
        "patchBoard patches the state pill without the liveness it was handed"


def test_the_poll_hands_liveness_to_the_board():
    """One reading of the row's stamp, handed to everything on the board that
    answers to it.

    Two reads a line apart are two answers, and a run can close between them: the
    board would then carry a pill from one reading and a round card from the
    other, which is the contradiction checklist item 4 looks for.
    """
    src = function_source(ui.page_html(), "poll")
    held = re.search(r"const (\w+) = runIsLive\(\w+\.id\);", src)
    assert held, "the poll asks whether the run is open without holding the answer"
    live = held.group(1)
    assert src.count("runIsLive(") == 1, \
        "the pills and the round cards read the stamp separately, so they can disagree"
    assert re.search(rf"patchBoard\(\w+, {live}\)", src), \
        "the board is patched without knowing whether its run is still open"
    assert re.search(rf"patchRounds\([^;]*\b{live}\)", src), \
        "the round cards are patched with something other than the poll's own reading"


def test_a_dead_run_stops_asking_to_be_answered():
    """AC-1's second sentence, and the reason it is in the spec at all.

    `workflows/run.py`'s blanket handler records `status="stopped"` and returns
    the ledger with `awaiting` still in it, so a run whose engine died closes with
    its card written down. The pills stop saying `waiting` — and the block three
    lines under them would go on showing the question, the options and an
    `lg approve …` command for a run that ended. That is the page telling the
    owner to answer something nothing is listening to.

    So the card follows the pills. The liveness is folded into the value the hide
    already tests rather than tested beside it, which keeps the single
    `hidden = !<name>` line test_the_awaiting_block_goes_when_the_workflow_pops_it
    is written against.
    """
    html = ui.page_html()
    src = function_source(html, "patchAwaiting")
    args = re.match(r"function patchAwaiting\(\w+, (\w+)\)", src)
    assert args, "patchAwaiting is not told whether the run is still open"
    hide = re.search(r"\.hidden = !(\w+);", src)
    assert hide, "the block's visibility no longer follows one value"
    shown = re.search(rf"const {hide.group(1)} = ([^;]+);", src)
    assert shown, "the value the hide tests is not built in one place"
    assert re.search(rf"\b{args.group(1)}\b", shown.group(1)), \
        "the block is shown on a run that has closed, question and command and all"
    assert f"if (!{hide.group(1)}) return;" in src, \
        "the heading, the question, the options and the command are written anyway"

    board = function_source(html, "patchBoard")
    passed = re.match(r"function patchBoard\(\w+, (\w+)\)", board)
    assert passed, "patchBoard is not told whether the run is still open"
    assert re.search(rf"patchAwaiting\([^)]*\b{passed.group(1)}\)", board), \
        "patchBoard patches the awaiting card without the liveness it was handed"


def test_a_null_ledger_hides_the_state_sections_and_keeps_the_logs():
    """AC-10. With no workflow behind a run directory the page still tails its
    logs, says one line about why there is no state, and shows nothing that would
    have come from a ledger."""
    src = function_source(ui.page_html(), "patchState")
    assert "no workflow for this run" in src and "temporal unreachable — logs only" in src
    assert re.search(r"getElementById\('diff'\)\.hidden", src), \
        "the diff pane stays on a run with no ledger to read a branch from"
    assert "getElementById('rounds')" not in src, "the log cards are hidden with the state"


def test_a_hidden_section_is_really_off_the_page():
    """`hidden` is an attribute, and the rule that acts on it is the browser's own
    `[hidden] { display: none }` — at user-agent weight, which any `display` in
    the page's own stylesheet beats.

    Two selectors here do exactly that. `#state` is a flex box and the answer
    command is inline-block, so both set the attribute, stayed on screen, and cost
    the reader the truth: a poll that lands a null ledger on a run already
    selected drew the last status it had above the line saying there is no state.
    That is reachable without anyone doing anything wrong — feed.ledger() answers
    null whenever the ledger query fails on a running workflow, which is
    nondeterminism on 7 of the 15 histories on this machine.

    One rule beside the reset settles it for every section this page hides, the
    round cards and the diff pane included. It has to carry !important, because the
    page's own rules are the ones it is overruling.
    """
    rule = re.search(r"\[hidden\]\s*\{([^}]*)\}", ui.page_html())
    assert rule, "nothing in the page's own stylesheet hides an element with `hidden`"
    body = rule.group(1).replace(" ", "")
    assert "display:none" in body, "the rule does not take the element out of the layout"
    assert "!important" in body, \
        "a page rule that sets display beats this one, and #state and .cmd both set it"


def test_the_first_run_is_clicked_only_when_nothing_is_chosen_yet():
    """runs() selects the first row by clicking it, which reaches buildBoard two
    hops from an interval — the shape this file's own notes record as invisible to
    every rule in it. So the guard is pinned by hand.

    `!sel` is the whole of it. Without those two characters the click fires on
    every 4-second poll: the board is built again, the reader's selection dies and
    every log pane they had open closes, with this suite green throughout.
    """
    src = function_source(ui.page_html(), "runs")
    guard = re.search(r"if\s*\(([^)]*)\)\s*\w+\.click\(\);", src)
    assert guard, "the first row is clicked with no `if` in front of it"
    assert re.search(r"!\s*sel\b", guard.group(1)), \
        "nothing stops the click firing again on every poll"


def test_the_answer_command_says_what_it_is():
    """AC-4 asks only that the command be selectable in one gesture, and a box of
    monospace text with no label is what that gets you. `lg status` prints
    `answer with:` in front of it; someone reading the page on a phone has no
    terminal to infer it from.

    The words go beside the <code>, never inside it: user-select:all covers the
    element it is set on, so a label within the box would be selected along with
    the command and pasted into a shell.
    """
    html = ui.page_html()
    assert "answer with:" in html, "the command sits in a box with nothing saying what it is"
    build = function_source(html, "buildBoard")
    label = re.search(r"answer with:[^<]*(</\w+>)", build)
    assert label and label.group(1) != "</code>", "the label is inside the <code> and selects with it"


def test_the_recorded_question_keeps_its_own_line_breaks():
    """AC-5. The question is the text of the card the owner saw, location line and
    blank line included. Collapsed to one line it stops being that."""
    html = ui.page_html()
    rule = re.search(r"#awaiting \.q \{([^}]*)\}", html)
    assert rule, "the question has no style of its own"
    assert "pre-wrap" in rule.group(1), "the question's line breaks are collapsed away"


# ---------- the strip over the board ----------


def strip_wiring(html):
    """The locals patchStrip works through: the strip and its three slots, and the
    rail row and the pieces of it they are copied from.

    Read out of the source rather than named here, so the tests below can ask what
    each slot is written FROM rather than only that something was written. A strip
    that took the row's pill class and not the row's pill word is a coloured badge
    reading whatever the run before it left there, and a check that counted the
    writes would pass it.
    """
    src = function_source(html, "patchStrip")
    strip = re.search(r"const (\w+) = document\.getElementById\('strip'\);", src)
    assert strip, "patchStrip never looks the strip up, so it cannot be guarded on one either"
    row = re.search(
        r"const (\w+) = \[\.\.\.document\.getElementById\('runs'\)\.children\]"
        r"\.find\((\w+) => \2\.dataset\.id === sel\.id\);", src)
    assert row, \
        "the strip is filled from something other than the selected run's own row in the rail"
    slots = re.search(rf"const \[(\w+), (\w+), (\w+)\] = {strip.group(1)}\.children;", src)
    assert slots, "the strip's name, pill and duration are not read out as its own three children"
    parts = re.search(rf"const \[(\w+), (\w+), (\w+)\] = {row.group(1)}\.children;", src)
    assert parts, "the row's three lines are not read out as its children"
    pill = re.search(rf"const \[(\w+)\] = {parts.group(2)}\.children;", src)
    assert pill, "the row's pill is not read out of the row's own meta line"
    return {"src": src, "strip": strip.group(1), "row": row.group(1),
            "name": slots.group(1), "word": slots.group(2), "dur": slots.group(3),
            "row_name": parts.group(1), "when": parts.group(3), "row_pill": pill.group(1)}


def test_the_board_builds_a_strip_before_the_state():
    """AC-15. The strip is built with the board, hidden, and ahead of everything
    else on it.

    Built rather than patched into being, for the reason every other section is:
    nothing on the poll path has to decide whether a part of the page exists yet.
    Hidden, because until a run is chosen there is no run for it to name. And
    first, because it is the one line that stays while everything under it scrolls
    away — a strip sitting below the status it is meant to be pinned above is not
    that.

    Every child, in order, and this is the only thing in the file that reads that
    markup: patchStrip binds its slots positionally out of `strip.children` and
    everything else about the strip is read off patchStrip's SOURCE, which is happy
    with a destructure of children that are not there. Three mutations were watched
    in a browser through a `[:3]` version of this. Drop the `archerr` span and the
    first refusal throws a TypeError into a click handler and the strip says
    nothing at all — the silent failure the archive control exists not to be. Drop
    the button and buildBoard throws, then patchStrip throws into runs()'s one
    catch: the header sticks on `server error`, the count freezes and no board is
    ever built. Move the button in front of `.dur` and the duration is written into
    the button and then overwritten by its own label, so the strip reads
    `2026-09-05-alpha merge-ready archive` with the duration nowhere.
    """
    build = function_source(ui.page_html(), "buildBoard")
    order = [m.group(1) for m in re.finditer(r'id="(\w+)"', build)]
    assert order[:2] == ["strip", "state"], \
        f"the board's sections open {order[:2]}, not the strip and then the state"
    strip = re.search(r'<div id="strip"([^>]*)>(.*?)</div>', build, re.S)
    assert strip, "the strip is not a div of its own in the board's markup"
    assert "hidden" in strip.group(1), \
        "the strip is on screen before a run is chosen, naming nothing"
    assert [m.group(1) for m in re.finditer(r'<span class="(\w+)"></span>', strip.group(2))] \
        == ["dir", "pill", "dur", "archerr"], \
        ("the strip's spans are not the name, the pill, the duration and the archive control's "
         "error line, in that order")
    # The button as well, which is no span and which the list above cannot see. Tag
    # and class together and the lot compared whole: this is the position
    # patchStrip's `const [name, word, dur] = strip.children;` is written against,
    # and a child inserted anywhere among the first three silently rebinds them.
    assert re.findall(r'<(\w+) class="([\w-]+)"[^>]*>', strip.group(2)) \
        == [("span", "dir"), ("span", "pill"), ("span", "dur"),
            ("button", "archbtn"), ("span", "archerr")], \
        "the strip's children are not its three words and then the archive control, in that order"


def test_the_strip_mirrors_the_selected_row():
    """AC-15. Every word on the strip is the rail row's own, copied off the row the
    run list keeps fresh — so the rail stays the single source for those words and
    the two can never come to say different things about one run. That is checklist
    item 4's failure with the halves an inch apart instead of a screen.

    The pill moves as a pair. A strip that took the row's class and not its text
    would be a coloured badge reading whatever the run before it left in there.

    A run that has left the reply has no row to copy, and the strip keeps what it
    has rather than blanking: the board does the same with its cards, and the last
    true thing said beats an empty line mid-poll.
    """
    html = ui.page_html()
    w = strip_wiring(html)
    src = w["src"]
    assert f"{w['strip']}.hidden = false;" in src, \
        "the strip is filled and never shown, so it stays hidden for the life of the page"
    assert f"if (!{w['row']}) return;" in src, \
        "a run that has left the reply blanks the strip instead of leaving what it had"
    assert f"setText({w['name']}, {w['row_name']}.textContent);" in src, \
        "the strip's name is written from something other than the row's own"
    assert f"{w['word']}.className = {w['row_pill']}.className;" in src, \
        "the strip's pill does not take the colour the rail has already worked out"
    assert f"setText({w['word']}, {w['row_pill']}.textContent);" in src, \
        "the strip's pill takes the row's colour without the row's word"
    # The row writes ` · 4m 12s`, where the dot holds the duration off the time
    # beside it. The strip has nothing to its left for it to hold off, so a
    # duration copied whole opens every strip on the page with a separator
    # separating nothing.
    assert f"setText({w['dur']}, {w['when']}.lastElementChild.textContent.replace(/^ · /, ''));" \
        in src, "the duration is not the row's own with the row's separator taken off it"
    assert "patchStrip()" in function_source(html, "runs"), \
        "nothing repatches the strip, so it holds the run's first reading for ever"
    assert "patchStrip()" in function_source(html, "buildRunRow"), \
        "a click leaves the strip on the run just left until the run list next lands"


def test_the_strip_does_nothing_before_a_run_is_chosen():
    """AC-15's last sentence, and the line the rest of the page rests on.

    `runs()` is one try around the fetch, patchRuns, the guarded first click and
    the header writes, with a single catch that writes `server error`. Read `sel.id`
    before anything is selected and the TypeError goes straight into that catch:
    the one line that ever selects a run is skipped, and the reader gets `server
    error` over a board that was never built, again every 4 seconds, on a server
    that answered every request. That is not a corner — it is every page load, and
    it is permanent on a dashboard with no runs at all, where there is no first row
    to click either.

    #strip is the same shape from the other end. It does not exist until buildBoard
    has made it, so the click handler patches the strip after that call and the
    poll has to survive there being none; a listener that throws leaves the board
    unbuilt with nothing on screen saying why.

    Hence the two positions, both load-bearing: after the guarded click, which is
    the only line on the page that ever sets `sel`, and after buildBoard, which is
    the only thing that ever makes a strip. The second is also what fills the strip
    the moment the reader clicks rather than up to four seconds later.
    """
    html = ui.page_html()
    src = function_source(html, "patchStrip")
    guard = re.search(r"if \(([^)]*)\) return;", src)
    assert guard, "patchStrip has no early return, so it reads `sel` whatever state the page is in"
    cond = guard.group(1)
    assert re.search(r"!\s*sel\b", cond), \
        "nothing stops patchStrip reading a selection that has not been made yet"
    held = re.search(r"const (\w+) = document\.getElementById\('strip'\);", src)
    assert held and re.search(rf"!\s*{held.group(1)}\b", cond), \
        "nothing stops patchStrip writing into a board that was never built"
    # `||` and not `&&`. ANDed, the function returns only when BOTH are missing,
    # so the first load — nothing selected, no board — falls through to the throw
    # the guard exists to stop.
    assert "||" in cond and "&&" not in cond, \
        f"either half alone has to return, and this returns on neither: `{cond.strip()}`"
    assert not re.search(r"\bsel\s*\.", src[:guard.start()]), \
        "patchStrip reads off `sel` above the line that checks there is one"

    runs_fn = function_source(html, "runs")
    click = re.search(r"\.click\(\);", runs_fn)
    assert click, "runs() no longer selects a run at all"
    # The statement straight after the click, and not merely somewhere below it.
    # `patchStrip()` anywhere later in the text is satisfied by
    # `const firstLoad = !sel; … if (firstLoad) patchStrip();`, which patches the
    # strip on the poll that selects the run and on no poll ever again: measured
    # in a browser, the strip read `42m 03s` beside a rail reading `42m 19s` after
    # 14 seconds, and a run that closes keeps `RUNNING` on the strip for the life
    # of the tab. That is the contradiction the mirroring test above says it
    # prevents, put back by a guard that reads as harmless.
    #
    # Comments are stripped out of the source this reads, so the next line with
    # anything on it is the next statement.
    after = next((ln.strip() for ln in runs_fn[click.end():].splitlines() if ln.strip()), "")
    assert after.startswith("patchStrip();"), \
        ("the poll does not patch the strip on the statement after the one that selects a run: "
         f"`{after}`")

    build = function_source(html, "buildRunRow")
    assert "patchStrip()" in build, "a click never patches the strip"
    # Inside the click handler, and not merely somewhere in the builder above a
    # `buildBoard(` that only ever appears inside it. Moved out into buildRunRow's
    # own body the call runs once, when the ROW is made — before there is a board
    # to write into, and never again when the reader clicks — so a click leaves
    # the strip naming the run they just left for up to 4 seconds. That is
    # behaviour 5's second call site gone, with the comparison below still true.
    handler = [c for c in closures(build) if "buildBoard(" in c]
    assert len(handler) == 1, \
        f"expected one closure in buildRunRow that builds a board, found {len(handler)}"
    assert "patchStrip()" in handler[0], \
        "the strip is patched somewhere other than the click handler, so a click never fills it"
    assert handler[0].index("patchStrip()") > handler[0].index("buildBoard("), \
        "the click patches the strip before buildBoard has made one"


def test_a_logs_only_run_strips_to_its_name():
    """AC-15. A run known only from its log files has no workflow behind it: no
    state to make a pill out of and no times to make a duration from. The page has
    neither and must not invent them, so the strip carries the name alone.

    Off a stamp of its own, because `data-live` cannot answer this and cannot be
    made to: it is '' for a workflow that finished, which owes the strip a pill and
    a duration, and '' for a directory of log files, which owes it neither. The
    regex test_only_a_run_that_is_still_open_claims_a_round_is_in_progress pins
    forbids changing that stamp's shape in any case, which is why there are two.
    """
    html = ui.page_html()
    row = function_source(html, "patchRunRow")
    stamp = re.search(r"\.dataset\.times = (\w+)\.start_time \? '1' : '';", row)
    assert stamp, "the row does not stamp whether Temporal has times for its run"
    live = re.search(r"\.dataset\.live = (\w+)\.start_time", row)
    assert live and live.group(1) == stamp.group(1), \
        "the two stamps are read off different replies"
    w = strip_wiring(html)
    src = w["src"]
    times = re.search(rf"const (\w+) = {w['row']}\.dataset\.times === '1';", src)
    assert times, "the strip reads the stamp some way other than off the row it already holds"
    assert "dataset.live" not in src and "runIsLive(" not in src, \
        "the strip tells a logs-only run from a finished one by liveness, which is '' for both"
    hidden = set(re.findall(rf"(\w+)\.hidden = !{times.group(1)};", src))
    assert hidden == {w["word"], w["dur"]}, \
        f"the pill and the duration are not the two the stamp hides: {sorted(hidden)}"


def test_the_board_says_which_silence_it_means():
    """AC-30. `no workflow for this run` was one line doing two jobs: a directory
    of log files with no workflow behind it, and a workflow Temporal knows
    perfectly well whose ledger query happened to fail this poll — which is
    nondeterminism on 7 of the 15 histories on this machine, not a rare state.

    With the strip above it the second reads as the board denying, in one line, the
    run it named in the line above, pill and duration and all. That is checklist
    item 4's failure — a page contradicting itself, where the reader cannot tell
    which half to believe — and it would sit there for as long as the query kept
    failing.

    So three lines, keyed on the two facts that tell the three cases apart: whether
    the feed answered at all, and whether the run has times on the wire. Which
    sentence goes with which case is the whole of the change, so each is pinned to
    its own branch and not merely to the page.
    """
    html = ui.page_html()
    src = function_source(html, "patchState")
    feed = re.search(r"const (\w+) = (\w+) && \2\.temporal;", src)
    assert feed, "the board no longer holds whether the feed answered at all"
    assert re.search(rf"!{feed.group(1)} \? 'temporal unreachable — logs only'", src), \
        "a feed that never answered says something other than that it is unreachable"
    assert re.search(r"runHasTimes\(sel\.id\) \? "
                     r"'ledger did not answer this poll; the run is still there'", src), \
        "a run Temporal has times for is told its workflow does not exist"
    assert re.search(r": 'no workflow for this run'\);", src), \
        "the run with no workflow behind it is no longer the case that falls through"
    assert "runIsLive(" not in src, \
        "the two silences are told apart by liveness, which is '' for a finished run too"


def test_the_strip_is_sticky_in_css():
    """AC-15's own word: the strip stays visible while the board scrolls. Nothing
    in this file can compute a layout — the suite has never rendered the page — so
    what is held here is the rule that does it, and the two things that make it
    readable once it holds.

    Opaque and edged, because sticky leaves the element in the flow rather than
    taking it out of one: the round cards pass underneath, and a see-through strip
    is two lines of text in the same place. And sticky is relative to the nearest
    thing that scrolls, which is #board — take the overflow off that and the strip
    has nothing to stick inside.

    The last one was measured in a browser and is the reason #board's padding is
    written the way it is. A sticky offset is taken from the scroll container's
    PADDING box, so #board's 16px of padding-top held the strip 16px below the
    board's own top edge and left a band exactly that deep above it, with the work
    items and the round cards sliding through it in full view. Every check in this
    file was green on it, and so is a scripted browser reading text back: the strip
    said the right words in the right place and there was a second line of the
    board's own content overlapping it. So the padding moved into the strip, which
    is what puts the sticky edge on the board's own top.

    Whether the whole of it holds is checklist work, in Task 12.
    """
    html = ui.page_html()
    body = declarations(html, "#strip").replace(" ", "")
    assert "position:sticky" in body, "the strip scrolls off the top with the rest of the board"
    assert "top:0" in body, "the strip sticks at no edge, so it never stops moving"
    assert "background:var(--bg)" in body, \
        "the board's cards read through the strip as they pass under it"
    assert re.search(r"border-bottom:[\d.]+pxsolid", body), \
        "nothing marks where the strip ends and the board it is pinned over begins"
    board = declarations(html, "#board")
    assert "overflow-y:auto" in board.replace(" ", ""), \
        "#board no longer scrolls, so there is nothing for the strip to stick inside"
    pad = re.search(r"padding:\s*([^;]+);", board)
    assert pad and pad.group(1).split()[0] == "0", \
        "#board pads above the strip, so a band of that depth over it carries the cards scrolling by"
    assert re.search(r"padding:[1-9][\d.]*px", body), \
        "the strip took #board's padding away and gave none back, so the board's text starts flush"


# ---------- the rounds ----------


def test_the_rounds_copy_is_pinned():
    """Every word a round card says that was not read off the ledger.

    `no logs yet for this run` is no longer among them. That line lived in
    #rounds when the section held log cards and nothing else; #rounds is where
    every round card lives now, and a run whose ledger has rounds is not empty
    because a log file is missing. One empty case cannot own two sentences, so
    the older one goes and `no rounds yet` stays.
    """
    html = ui.page_html()
    assert re.search(r"`item \$\{\w+\} · round \$\{\w+\}`", html), \
        "the card header no longer reads `item <i> · round <r>`"
    for line in (" · in progress", "no rounds yet", "audit running", "escalated"):
        assert line in html, f"the page no longer says {line!r}"
    for key in ("verdict_reasons", "owner_question", "owner_reply", "directive", "files"):
        assert key in html, f"the page reads no {key} off a round"
    assert "no logs yet for this run" not in html, \
        "two empty-state lines claim #rounds, and only one of them is about rounds"


def test_a_round_without_an_item_number_keys_to_item_one():
    """AC-8, and the only thing keeping two live runs from drawing every round twice.

    `microbits-fact-corrections` and `microbits-ideas-sharpen` both closed before
    the ledger carried `item_no`, and both wrote their logs in the old shape,
    `r1-executor.log`, with no item number either. Read with different defaults
    the ledger round keys `undefined-1` and the log names key `1-1`, so each
    round draws twice: one card headed `item undefined` carrying the verdict and
    no panes, one carrying the panes and a permanent ` · in progress` on a run
    that finished hours ago.

    One helper for both sides is the whole of the fix, and `lg`'s format_status
    defaults the same way, so the page and the terminal cannot disagree.
    """
    html = ui.page_html()
    helper = function_source(html, "roundKey")
    assert re.search(r"\|\|\s*1\b", helper), "a round with no item number keys to `undefined`"
    calls = [c.strip() for c in re.findall(r"roundKey\(([^,]+),", function_source(html, "patchRounds"))]
    assert len(calls) == 2, f"the two sides do not both go through the key helper: {calls}"
    assert any(c.endswith(".item_no") for c in calls), \
        "the ledger round's item_no is keyed without the helper's default"
    assert any(re.fullmatch(r"\w+\[1\]", c) for c in calls), \
        "the log name's LOG_RE item group is keyed without the helper's default"

    lg = load_lg()
    assert "item 1 round 1" in lg.format_status({"rounds": [{"round": 1, "verdict": "accept"}]}), \
        "lg no longer reads a round with no item_no as item 1"


def test_the_page_says_what_lg_says_where_a_round_has_no_verdict():
    """AC-8. `workflows/run.py` appends the round entry BEFORE the audit runs, and
    the audit has half an hour to answer, so a round with no `verdict` key is two
    different pieces of news and they must not read alike.

    `escalated` means the gates stayed red and no audit ever ran. `green` means
    the supervisor is still out. Printing the gate word `green` where a verdict
    belongs would read as an acceptance nobody has made, on a screen whose whole
    job is saying what was actually verified.

    The words are taken out of `lg`'s own helper rather than typed again here: a
    copy in this test would pin the page to the test and let the terminal drift
    away from both.
    """
    lg = load_lg()
    src = function_source(ui.page_html(), "roundVerdict")
    for status in ("escalated", "green"):
        word = lg._round_verdict({"status": status})
        assert f"'{word}'" in src, f"the page does not say {word!r} where lg status does"
    assert re.search(r"green[^\n]*audit running", src), \
        "the gate word green does not map to the words that say the audit is out"

    # `redo` is a real verdict — run-2026-09-05-macweb-metadata-19220a records one
    # — and it is not in the plan's list of four. So the word is printed as it
    # stands, and a page that enumerates verdicts prints a blank where it sits.
    assert lg._round_verdict({"status": "green", "verdict": "redo"}) == "redo"
    assert not re.search(r"'(accept|stop|plan|ask|redo)'", src), \
        "the page enumerates verdict words instead of printing the one it was given"


def test_a_round_says_it_is_in_progress_through_both_halves_of_its_life():
    """The live view the old dashboard gave away for free, and it is two states.

    `_run_item` appends the round entry only after `execute_round` returns, so
    while the executor works there is a log file growing and no ledger row at
    all — the first half. Then the entry is appended and the audit is awaited for
    up to 30 minutes, and only then is `verdict` written — the second half. A
    card that only knew the first would go quiet for half an hour on a round that
    is still very much running.
    """
    src = function_source(ui.page_html(), "patchRoundCard")
    assert "' · in progress'" in src, "no card ever says a round is still running"
    assert re.search(r"!\w+\.entry", src), \
        "a round with no ledger row at all is not read as running"
    assert "audit running" in src, \
        "a round whose audit is still out drops the suffix half an hour early"


def test_a_log_with_no_ledger_row_still_gets_a_card():
    """AC-10 and the in-progress half above, from the other side: the cards are
    the union of the ledger's rounds and the log names, not either one alone. A
    ledger-only view loses every round the executor is still working in; a
    names-only view is what the page did before it could read a verdict."""
    src = function_source(ui.page_html(), "patchRounds")
    assert re.search(r"for \(const \w+ of rounds\)", src), "the ledger's rounds build no cards"
    assert re.search(r"for \(const \w+ of names\)", src), "the log names build no cards"
    assert "LOG_RE" in src, "the log names are grouped by something other than the shared pattern"
    assert ".remove()" in src, "a card whose key left the reply stays on screen for ever"


def test_rounds_are_ordered_by_number_and_not_as_strings():
    """`1-10` sorts under `1-2` as a string, so `item 1 · round 10` sat below
    `item 1 · round 2` — and cards are put in place rather than re-sorted, so no
    later poll ever moves one back.

    The page's own comparison is pulled out and run over a list of keys rather
    than searched for the word `Number`: `ai - bi` carries that word too, passes
    every shape check in this file, and puts the OLDEST round on top, which is
    the bug this test is named after.
    """
    import functools
    html = ui.page_html()
    order = function_source(html, "newerFirst")
    args = re.search(r"function newerFirst\((\w+), (\w+)\)", order)
    binds = re.findall(r"\[(\w+), (\w+)\] = (\w+)\.split\('-'\)\.map\(Number\)", order)
    expr = re.search(r"return ([^;]+);", order)
    assert args and len(binds) == 2 and expr, \
        "newerFirst is no longer two splits and one expression, so this cannot run it"

    def compare(x, y):
        held = {args.group(1): x, args.group(2): y}
        env = {}
        for item, rnd, arg in binds:
            env[item], env[rnd] = (int(n) for n in held[arg].split("-"))
        # `||` is Python's `or` over these integers exactly: 0 falls through to
        # the round, anything else is the answer.
        return eval(expr.group(1).replace("||", "or"), {"__builtins__": {}}, env)

    keys = ["1-1", "1-2", "1-10", "2-1", "10-1", "1-9"]
    assert sorted(keys, key=functools.cmp_to_key(compare)) == \
        ["10-1", "2-1", "1-10", "1-9", "1-2", "1-1"], \
        "the page's own comparison does not put the newest round first"

    src = function_source(html, "patchRounds")
    assert "sort(newerFirst)" in src, "the keys are sorted by something else"
    assert re.search(r"newerFirst\(\w+\.dataset\.key", src), \
        "a new card is placed by a comparison the sort does not use"
    assert not re.search(r"dataset\.key\s*<", src), \
        "the old string comparison is still deciding where a card goes"
    assert "insertBefore" in src, "a new card cannot arrive without moving the ones on screen"


def test_an_empty_reply_takes_the_old_cards_with_it():
    """The early return in the empty branch skipped the removal that used to sit
    below it, so a run whose ledger went unreadable and whose log directory went
    away kept every card it had and got `no rounds yet` appended underneath them
    — one line denying the cards above it. The removal runs first now.

    The empty line itself is left out of the removal. Taken away and made again
    every 2 seconds it would be a new node each poll, which is the churn setText
    exists to avoid.
    """
    src = function_source(ui.page_html(), "patchRounds")
    removal = re.search(r"if \(\w+\.dataset\.key && ![^\n]*\)\s*\w+\.remove\(\);", src)
    assert removal, "a card whose key the reply no longer carries stays on screen for ever"
    assert removal.start() < src.index("if (!keys.length)"), \
        "the removal sits after the empty branch, which returns before reaching it"
    assert removal.start() < src.index("'no rounds yet'"), \
        "the empty line goes up while the cards it denies are still on screen"


def test_only_a_run_that_is_still_open_claims_a_round_is_in_progress():
    """Measured on the logs-only view: `2026-09-05-macweb-metadata` closed
    yesterday after three rounds, and every one of them said ` · in progress` —
    three lines under a rail reading `logs only` and a banner reading `temporal
    unreachable — logs only`. Both halves of the suffix are read off a ledger the
    page could not get, so with no ledger at all both fired on every round there
    has ever been.

    The run's own times settle it, and they are already on the wire (AC-3): a
    start and no close is a workflow still running, both is one that finished,
    and neither is a directory of log files with no workflow behind it. So the
    suffix stays on the case it was written for — a running workflow whose
    ledger query fails, which is nondeterminism on 7 of the 15 histories on this
    machine — and comes off every run that has closed.

    Read off the row and not off `sel`, which holds the reply its row was first
    built from: a run that was running when the reader clicked it closes while
    they are watching, and the card has to stop claiming otherwise.
    """
    html = ui.page_html()
    row = function_source(html, "patchRunRow")
    assert re.search(r"dataset\.live\s*=\s*\w+\.start_time\s*&&\s*!\w+\.close_time", row), \
        "the row does not stamp whether its run is still open"
    live = function_source(html, "runIsLive")
    assert "dataset.live" in live, "the liveness is read from somewhere other than the row"
    assert "sel" not in live, "the liveness comes off the click's own reply, which never moves"
    assert re.search(r"runIsLive\(\w+\.id\)", function_source(html, "poll")), \
        "the poll never asks whether the run it is patching is still open"
    assert re.match(r"function patchRounds\(\w+, \w+, \w+, \w+\)",
                    function_source(html, "patchRounds")), \
        "patchRounds is not told whether the run is still open"
    guard = re.search(r"const \w+ = (\w+) && \(!\w+\.entry",
                      function_source(html, "patchRoundCard"))
    assert guard, "the in-progress suffix is not gated on the run being open"
    assert guard.group(1) == "live", \
        f"the suffix is gated on {guard.group(1)!r} rather than the run's own liveness"


def test_a_round_card_is_a_details_with_a_summary():
    """AC-9. A run of twelve rounds was twelve fully-expanded cards and one very
    long scroll, with nothing between one card and the next saying where the
    boundary was.

    <details> carries `open` itself and brings the triangle and the keyboard with
    it — the log panes are written on it for the same reason, and a collapse
    state of the page's own is one more thing to get out of step with what is on
    screen.

    The summary is three spans and not one string. The head, the verdict word and
    the file count are three separate readings off the round written by three
    setTexts, so a poll that changes the verdict leaves the other two nodes where
    they were.
    """
    html = ui.page_html()
    build = function_source(html, "buildRoundCard")
    assert "createElement('details')" in build, "a round card cannot be collapsed"
    markup = re.search(r"innerHTML = '([^']*)'", build)
    assert markup and markup.group(1).startswith("<summary>"), \
        "the summary is not the card's first child, so a closed card shows nothing at all"
    # In this order, and the order is not decoration. patchRoundCard reads the
    # three spans by position and the stylesheet colours the verdict by class, so
    # swapping two names in this one string leaves every other check in this file
    # green, renders identically today, and paints the file count with the
    # verdict's meaning — `4 files` in green, `accept` in neither.
    assert [m.group(1) for m in re.finditer(r'<span class="(s-[\w-]+)"></span>', build)] \
        == ["s-head", "s-verdict", "s-files"], \
        "the summary's spans are not the head, then the verdict word, then the file count"
    assert "<h2>" not in build, \
        "the card still builds the heading the summary replaced, so the line is drawn twice"
    assert ".round > h2" not in html, \
        "the stylesheet still dresses a heading no round card has"


def test_only_the_newest_card_is_created_open():
    """AC-9's second half, and what makes a live run watchable.

    The keys are sorted newest-first, so the newest round is keys[0] and every
    other card arrives closed. The comparison is made where the card is made and
    nowhere else: a poll that decided it again would shut the card the reader
    opened two seconds earlier, and taking away what the reader is holding is the
    failure four comments in ui.py already record bugs for. So `.open` is written
    once, by the builder, on a node that is not on screen yet, and no patch…
    function writes it at all.

    A live run watched through twelve rounds therefore ends with twelve open
    cards. That is the price of never taking one away, and it is the one chosen.
    """
    html = ui.page_html()
    src, regs = regions(html)
    rounds = function_source(html, "patchRounds")
    keys = re.search(r"const (\w+) = Object\.keys\(\w+\)\.sort\(newerFirst\);", rounds)
    assert keys, "the keys are no longer sorted newest-first, so there is no newest to compare to"
    assert re.search(rf"buildRoundCard\(\w+, \w+ === {keys.group(1)}\[0\]\)", rounds), \
        "a card is not created open exactly when its key is the newest one on the board"
    build = function_source(html, "buildRoundCard")
    took = re.match(r"function buildRoundCard\(\w+, (\w+)\)", build)
    assert took, "buildRoundCard is not told whether the card it is making is the newest one"
    # The flag itself, not merely something. `card.open = true;` keeps the
    # signature, keeps patchRounds working out which key is newest, and passes
    # every other line of this test — while opening all twelve cards of a
    # twelve-round board, which is the one thing AC-9 forbids.
    assert re.search(rf"\w+\.open = {took.group(1)};", build), \
        "the card is opened on something other than the flag the builder was handed"
    for name, s, e in regs:
        if name.startswith("patch"):
            assert not re.search(r"\.open\s*=[^=]", src[s:e]), \
                f"{name} runs on every poll and writes .open, shutting a card the reader opened"


def test_the_summary_carries_verdict_word_and_file_count():
    """AC-10. A closed card is one line, so that line answers the two questions a
    reader scrolling a finished run has: what did this round decide, and how much
    did it touch.

    The word is roundVerdict's own — the same string the open card's verdict line
    gets, so one round never reads two ways. The count is spelled out both ways
    rather than pluralised with an `s`: `1 files` is the kind of thing nobody
    notices in review and everybody notices on a board where most rounds touched
    one file.

    Both go into their own span and never onto the <summary>, which holds all
    three: setText assigns textContent, and textContent on the summary would take
    the spans down with it — innerHTML with the name filed off.
    """
    html = ui.page_html()
    src = function_source(html, "patchRoundCard")
    kids = re.search(r"const \[(\w+),[^\]]*\] = \w+\.children;", src)
    assert kids, "the card's children are no longer read out in order"
    summary = kids.group(1)
    spans = re.search(rf"const \[(\w+), (\w+), (\w+)\] = {summary}\.children;", src)
    assert spans, f"the summary's three spans are not read off {summary}"
    said, counted = spans.group(2), spans.group(3)
    word = re.search(r"const (\w+) = roundVerdict\(", src)
    assert word, "the card no longer asks roundVerdict what the round decided"
    assert re.search(rf"setText\({said}, {word.group(1)}\);", src), \
        "the summary's verdict span is written with something other than the round's own word"
    n = re.search(r"const (\w+) = \(\w+\.files \|\| \[\]\)\.length;", src)
    assert n, "the summary counts something other than the round's own file list"
    # The whole expression rather than the two words in it, the same way the tab's
    # count is pinned: `1 file` and `<n> files` both being present says nothing
    # about which count gets which, and the empty third branch is the one that
    # keeps `0 files` off a round that has touched nothing yet.
    assert re.search(rf"setText\({counted}, {n.group(1)} \? "
                     rf"\({n.group(1)} === 1 \? '1 file' : `\$\{{{n.group(1)}\}} files`\) : ''\);",
                     src), \
        "the count does not read `1 file`, then `<n> files`, then nothing at all at zero"
    assert not re.search(rf"setText\({summary},", src), \
        f"{summary} is the <summary> itself, and writing text on it destroys its three spans"


def test_a_round_with_no_entry_has_a_bare_summary():
    """The half of a round's life the ledger knows nothing about. `_run_item`
    appends the entry only after `execute_round` returns, so while the executor
    works there is a log file growing and no row at all, and that card's summary
    is the head and nothing else.

    Both of the other pieces are read off the entry, so a missing one leaves them
    empty rather than zeroed. `0 files` is a measurement of something that has
    not happened, and a verdict word on a round nobody has judged would be worse.
    """
    html = ui.page_html()
    src = function_source(html, "patchRoundCard")
    empty = re.search(r"const (\w+) = \w+\.entry \|\| \{\};", src)
    assert empty, "a round with no ledger row is not read as an entry with every field absent"
    entry = empty.group(1)
    assert re.search(rf"roundVerdict\({entry}\)", src), \
        "the summary's word comes from somewhere other than the entry"
    assert re.search(rf"\({entry}\.files \|\| \[\]\)\.length", src), \
        "the summary's file count comes from somewhere other than the entry"
    assert "return '';" in function_source(html, "roundVerdict"), \
        "an entry with no verdict and no status hands a word back, so the bare summary is not bare"
    assert "'0 files'" not in html, \
        "a round the executor is still inside is told how much it has not touched yet"


def test_the_summary_says_in_progress_or_a_verdict_never_both():
    """AC-10's second sentence. `roundVerdict` answers `audit running` for the
    whole audit window, and the guard that puts ` · in progress` on the head is
    true for exactly the same half hour — so a card collapsed while the
    supervisor is out read `item 1 · round 2 · in progress · audit running · 3
    files`, which is the same news twice on the one line a closed card gets.

    The word wins because it says more. The suffix is for the half with no word
    at all, the round the executor is still inside. Neither the guard nor any of
    the words it is built from goes away — only the condition the suffix is
    appended under gets narrower, and the whole condition is pinned here because
    either half of it alone is a different page.
    """
    html = ui.page_html()
    src = function_source(html, "patchRoundCard")
    word = re.search(r"const (\w+) = roundVerdict\(", src)
    running = re.search(r"const (\w+) = \w+ && \(!\w+\.entry", src)
    assert word and running, "the verdict word and the in-progress guard are not both read"
    suffix = re.search(r"\(([^()]*) \? ' · in progress' : ''\)", src)
    assert suffix, "the head no longer appends the suffix under a condition of its own"
    assert " ".join(suffix.group(1).split()) == f"{running.group(1)} && !{word.group(1)}", \
        (f"the suffix is appended on `{suffix.group(1).strip()}` rather than on the round "
         "still running AND having no verdict word to show instead")


def card_word_elements(html):
    """(patchRoundCard's source, the verdict line, the summary's word span).

    Both elements read out the way the function reads them, by position, so the
    tests below name whatever the page named. The second read is anchored to the
    first — the summary the spans come off is the card's own first child — so a
    destructuring that moved would fail here rather than quietly match some other
    line that also ends in `.children`.
    """
    src = function_source(html, "patchRoundCard")
    kids = re.search(r"const \[(\w+), (\w+),[^\]]*\] = \w+\.children;", src)
    assert kids, "the card's children are no longer read out in order"
    spans = re.search(rf"const \[\w+, (\w+), \w+\] = {kids.group(1)}\.children;", src)
    assert spans, f"the summary's three spans are not read off {kids.group(1)}"
    return src, kids.group(2), spans.group(1)


def concat_value(expr, holes):
    """The string a `'a' + b` expression builds, given a value for every name in it.

    String literals joined with `+`, which is the whole of what this page
    concatenates onto a class. A piece this cannot read is a failure rather than a
    guess: a rewrite in some other shape stops the test that asked instead of
    quietly passing it.
    """
    out = ""
    for piece in expr.split("+"):
        piece = piece.strip()
        if len(piece) > 1 and piece[0] == piece[-1] == "'":
            out += piece[1:-1]
        else:
            assert piece in holes, \
                f"`{piece}` is neither a string literal nor a value this test can supply"
            out += holes[piece]
    return out


def tone_values(src, tone):
    """What `tone` holds for a verdict the map answers for, and for one it does not.

    Both branches are worked out rather than matched, because the one character
    that matters here can correctly live in either half of the concatenation:
    `'verdict' + ' good'` and `'verdict ' + 'good'` are the same two classes. A
    test that matched the source would pin whichever half the page picked; a test
    that matched neither lets the space go missing altogether, and that is the
    expensive one. `class="verdictgood"` matches no rule in this stylesheet, so a
    recognised verdict loses the colour, the monospace and the margin and renders
    as body text, while an unrecognised one — the single case AC-11 says should
    read neutral — keeps all three. AC-11 inverted, one character, suite green.
    """
    m = re.search(rf"const {tone} = [^;]*?\?([^;]+):([^;]+);", src)
    assert m, f"`{tone}` is not `the map's answer, or nothing`, so its two cases cannot be read"
    hole = "<the map's answer>"
    truthy = re.sub(r"VERDICT_CLASS\[\w+\]", hole, m.group(1))
    return concat_value(truthy, {hole: "good"}), concat_value(m.group(2), {})


def class_assigned(src, element, tone, value):
    """The class string `element` is handed when `tone` holds `value`.

    What the browser receives, built from the source rather than looked for in it.
    """
    m = re.search(rf"{element}\.className = ([^;]+);", src)
    assert m, f"{element} is never given a class of its own"
    return concat_value(m.group(1), {tone: value})


def test_the_verdict_colour_map_lives_outside_roundVerdict():
    """AC-11. The word is coloured by what it means, out of a lookup table rather
    than a branch inside roundVerdict.

    roundVerdict prints whatever the ledger holds and enumerates nothing — that is
    pinned by test_the_page_says_what_lg_says_where_a_round_has_no_verdict, and
    `redo` is the word it was pinned for. A colour map written inside it is
    exactly the enumeration that test forbids, in the one place it cannot see:
    regions() charges every line after a `function` declaration to that
    declaration, and this map's keys are bare identifiers, so a grep for quoted
    words walks straight past `redo:'warn'`. So the position is asserted here, and
    the position is the only thing that asserts it.

    Both copies of the word read the map. The open card's verdict line and the
    closed card's summary span are one round said twice, and a page that coloured
    one of them would contradict itself in colour — the failure the whole phase is
    about, in the one channel a reader trusts without reading.
    """
    html = ui.page_html()
    script = script_of(html)
    decl = re.search(r"const VERDICT_CLASS = \{([^{}]*)\};", script)
    assert decl, "the page carries no verdict colour map"
    first = DECLARED.search(script)
    assert first, "the script declares no functions at all"
    assert decl.start() < first.start(), \
        (f"VERDICT_CLASS sits below `function {first.group(1)}(`, so regions() reads it as "
         "living inside a function and nothing here can tell it from a map inside roundVerdict")
    assert "VERDICT_CLASS" not in function_source(html, "roundVerdict"), \
        "roundVerdict reads the colour map, so it enumerates the words it was written not to"
    pairs = {quoted or bare: cls for quoted, bare, cls
             in re.findall(r"(?:'([^']+)'|([\w$]+))\s*:\s*'([\w-]+)'", decl.group(1))}
    assert pairs == {"accept": "good", "audit running": "warn",
                     "redo": "warn", "escalated": "bad"}, \
        f"the map reads {pairs}, which is not the meaning AC-11 gives each word"

    src, line, span = card_word_elements(html)
    held = re.search(r"const (\w+) = [^;]*VERDICT_CLASS\[(\w+)\]", src)
    assert held, "patchRoundCard never asks the map what colour the word carries"
    word = re.search(r"const (\w+) = roundVerdict\(", src)
    assert word and held.group(2) == word.group(1), \
        "the map is keyed on something other than the word the card is about to print"
    # The class string each element is handed, worked out and not matched. Both
    # copies of the word have to come out as the base class AND the map's answer,
    # two classes with a space between them — which is what makes the stylesheet's
    # `.round .verdict.good` match at all.
    mapped, _ = tone_values(src, held.group(1))
    for el, base in ((line, "verdict"), (span, "s-verdict")):
        got = class_assigned(src, el, held.group(1), mapped)
        assert got.split() == [base, "good"], \
            (f'a round the map answers for leaves {el} with class="{got}", not `{base}` and '
             f"the map's own class as two. A run-together `{base}good` matches no rule in the "
             "stylesheet, so the word the page recognises is the one that loses its colour")


def test_an_unknown_verdict_keeps_its_text_and_the_neutral_colour():
    """AC-11's last clause. The engine writes whatever word the audit model
    returned, and the four in the map are the four anyone has seen — a fifth is a
    word the page has never met, not a fault to swallow.

    So the fallback is nothing at all appended, which leaves the base class alone
    and the neutral accent `.round .verdict` already sets. That colour is the
    fallback by name: deleting it and letting the three new rules carry the whole
    job would leave an unmapped verdict in the body text colour, on the one line of
    the card that is not meant to read like a field.

    Two failures this closes. A word that renders blank because nothing matched —
    so the text is written outside the lookup, never under a branch of it. And a
    span still wearing the previous round's colour, which is what `classList.add`
    buys: the class is assigned whole on every poll, so a round that loses its
    verdict loses the colour with it.
    """
    html = ui.page_html()
    src, line, span = card_word_elements(html)
    held = re.search(r"const (\w+) = [^;]*VERDICT_CLASS\[\w+\]", src)
    assert held, "patchRoundCard never asks the map what colour the word carries"
    # The class string an unmapped word leaves behind: the base class on its own,
    # so the element keeps the styling written for a word with no meaning attached.
    _, unmapped = tone_values(src, held.group(1))
    for el, base in ((line, "verdict"), (span, "s-verdict")):
        got = class_assigned(src, el, held.group(1), unmapped)
        assert got.split() == [base], \
            (f'a verdict this page has never met leaves {el} with class="{got}" rather than '
             f"`{base}` on its own, so it is dressed as one of the words the page does know")
    word = re.search(r"const (\w+) = roundVerdict\(", src)
    assert word, "the card no longer asks roundVerdict what the round decided"
    for el in (line, span):
        assert re.search(rf"setText\({el}, {word.group(1)}\);", src), \
            f"{el} is written with something other than the word roundVerdict handed back"
    assert not re.search(rf"if \([^)]*\b({held.group(1)}|VERDICT_CLASS)\b", src), \
        "the word is printed under a check of its own colour, so an unmapped verdict can be dropped"
    assert "classList" not in src, \
        ("a class is added or removed rather than assigned whole, so a round whose word "
         "changes keeps the colour of the one before it")
    assert "color:var(--accent)" in declarations(html, ".round .verdict").replace(" ", ""), \
        "the verdict line's neutral colour is gone, so an unmapped word has nothing to fall back to"


def css_rules(html):
    """(selector, declarations) for every rule in the page's stylesheet.

    Comments stripped, and the one @media block flattened: the rules inside it come
    back as rules of their own, which is all anything here asks of them. Not a CSS
    parser — it is the same "read the source as text" this whole file does, and the
    stylesheet is what nothing in it could see when a `display` beat `[hidden]`.
    """
    style = re.search(r"<style>(.*?)</style>", html, re.S)
    assert style, "the page serves no stylesheet"
    css = re.sub(r"/\*.*?\*/", "", style.group(1), flags=re.S)
    css = re.sub(r"@media[^{]*\{", "", css)
    return [(sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]


def declarations(html, selector):
    """Everything the stylesheet declares for one selector, from every rule carrying
    it, whether it stands alone or sits in a comma-separated list."""
    return " ".join(body for sel, body in css_rules(html)
                    if selector in [p.strip() for p in sel.split(",")])


def compounds(selector):
    """One selector split into its pieces, on the combinators OUTSIDE any brackets.

    `:has(> .panel.log[open])` carries a combinator of its own, and splitting on
    the string would tear the argument in half and hand back a piece that matches
    nothing anybody wrote. The last piece is the subject — the element the rule
    actually paints — which is the half of a selector these tests keep asking about.
    """
    out, depth, cur = [], 0, ""
    for ch in selector.strip():
        depth += (ch == "(") - (ch == ")")
        if not depth and (ch.isspace() or ch in ">+~"):
            if cur:
                out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def specificity(selector):
    """(ids, classes, types) for one selector, the way the cascade weighs it.

    Specificity beats source order, and that is the whole reason the verdict
    colours are written the way they are: `.round .verdict` is two classes deep,
    so a bare `.good` never reaches the word however far down the sheet it sits.
    Nothing in this file parsed CSS before a `display` beat `[hidden]` on the page
    and nothing here caught it, so the weighing is done rather than assumed.

    Narrow on purpose. A functional pseudo-class takes the specificity of its own
    argument and needs the real algorithm, so one is refused rather than
    mis-counted — no colour rule on this page has any.
    """
    selector = selector.strip()
    assert not re.search(r":[\w-]+\(", selector), \
        f"`{selector}` carries a functional pseudo-class, which this counter cannot weigh"
    ids = selector.count("#")
    classes = (selector.count(".") + selector.count("[")
               + len(re.findall(r"(?<!:):[\w-]+", selector)))
    types = (len(re.findall(r"(?:^|[\s>+~])[a-zA-Z][\w-]*", selector))
             + selector.count("::"))
    return ids, classes, types


def reason_row_declarations(html):
    """The same, for a round's reason rows, wherever the page chose to put them.

    A row of its own rule and a row folded into the selector the other fields use
    are the same page to the reader, so this reads any selector piece ending in
    `.reason` — the state line's own `#state .reason` excepted, which is a
    different element with a different job.
    """
    out = []
    for sel, body in css_rules(html):
        pieces = [p.strip() for p in sel.split(",")]
        if any(p.endswith(".reason") and not p.startswith("#state") for p in pieces):
            out.append(body)
    return " ".join(out)


def test_a_round_card_carries_what_ac8_asks_for():
    """Verdict, every reason, files one per line, the directive when there is one,
    and the owner's question with the answer when the round asked. All of it
    written through setText, so a poll that changes nothing touches no node.

    The reasons stopped being one `\\n`-joined string (AC-5), so this pins what the
    card owes the reader rather than how it is built: each reason reaches the page
    as its own element, made by a builder and written through the guarded setter,
    with no innerHTML anywhere on the way. The fields that still join are pinned
    exactly as before — the files are one string with newlines in it and the
    pre-wrap rule is the only thing putting them back on their own lines, and a
    rewrite of this test that quietly stopped watching them would let a five-file
    list run together into one line with the suite green.
    """
    html = ui.page_html()
    src = function_source(html, "patchRoundCard")
    for field in ("verdict_reasons", "files", "directive", "owner_question", "owner_reply"):
        assert field in src, f"a round card says nothing about {field}"
    assert re.search(r"patchReasons\(\w+\.lastElementChild, \w+\)", src), \
        "the reasons do not reach the page an element at a time"
    rows = function_source(html, "patchReasons")
    assert "buildReasonRow(" in rows, "the rows come from somewhere other than a builder"
    assert re.search(r"setText\(\w+,", rows), "a reason is written without the guarded setter"
    for name in ("patchRoundCard", "patchReasons"):
        assert ".textContent =" not in function_source(html, name), \
            f"{name} runs on every poll and writes text without checking it changed"
    assert "innerHTML" not in function_source(html, "buildReasonRow"), \
        "a reason row is poured out of markup, so a supervisor's `<b>` would arrive bold"

    assert re.search(r"\(\w+\.files \|\| \[\]\)\.join\('\\n'\)", src), \
        "the files no longer reach the page one per line"
    assert "pre-wrap" in declarations(html, ".round .field span"), \
        "the lines the joined fields are patched with are collapsed back into one"


def test_reason_rows_are_keyed_and_diffed_not_rebuilt():
    """AC-5's second sentence: a poll that changes no reason replaces no node.

    One `\\n`-joined string got that for free — one setText, one comparison, and a
    reader's selection survived every poll that changed nothing. A row per reason
    only keeps it if the rows are found again rather than made again, so this is
    the map diff patchOptions and the pass rows are written on: existing rows read
    out by their key, a new one inserted where the list has it, and the ones past
    the end removed. Emptying the box and refilling it would look identical on
    screen and lose the selection every 2 seconds.

    Keyed on the index because the supervisor writes the list fresh each round, so
    a re-ordered list is a text change and never a move.

    Building the map is not the same as reading it, and reading it is where this
    goes wrong. `dataset` hands its values back as strings whatever they were
    assigned, so `rows.get(i)` with a number misses every row it has, and
    patchReasons builds the whole list again on every poll — a new node under the
    reader's selection every 2 seconds, with the map still made, the key still
    stamped, insertBefore and remove still there and every word this test used to
    look for still in the source. `let row = null;` is the same failure written
    shorter. So the lookup is read out by name: the map it reads, the coercion,
    and the variable the branch below tests have to be the one line.
    """
    html = ui.page_html()
    src = function_source(html, "patchReasons")
    made = re.search(r"const (\w+) = new Map\(\[\.\.\.\w+\.children\]", src)
    assert made, "the rows already on screen are not read out before the list is walked"
    held = made.group(1)
    found = re.search(rf"let (\w+) = {held}\.get\(String\(", src)
    assert found, \
        f"nothing reads {held} back with a string key, so every row is a miss and is built again"
    assert re.search(rf"if \({found.group(1)}\)", src), \
        "the row the lookup found is not what decides whether one is built"
    assert re.search(rf"{held}\.delete\(String\(", src), \
        f"a row that was found is not dropped from {held}, so the sweep at the end removes it"
    assert "dataset.i" in src, "a row is matched to its reason by position alone"
    assert "insertBefore" in src, "a new reason cannot arrive without moving the rows around it"
    assert ".remove()" in src, "a reason the round no longer carries stays on screen for ever"
    assert "replaceChildren" not in src, "the box is emptied and filled back up"
    for m in re.finditer(r"\bbuildReasonRow\(", src):
        assert src[:m.start()].rstrip().endswith("="), \
            "patchReasons calls buildReasonRow for what it does, not for the row it hands back"
    build = function_source(html, "buildReasonRow")
    assert re.search(r"dataset\.i\s*=", build), \
        "a reason row carries no key, so its text could only be matched to it by position"


def test_a_reason_is_coerced_before_it_is_written():
    """`.join('\\n')` was doing one thing nobody wrote down: it made a string.

    `verdict_reasons` is the audit model's own JSON. `activities/audit.py` fills it
    in with `pkt.setdefault("reasons", [])` and checks no types, `workflows/run.py`
    copies it into the ledger as it stands, and `/api/run` hands it on, so an entry
    that is not a string arrives here as one. setText compares with `!==`, and a
    string is never equal to an object, so an uncoerced reason would fail that
    comparison for ever: a new text node every 2 seconds, on the one element this
    task exists to hold still, taking the reader's selection with it each time.

    patchOptions, the diff this one is modelled on, coerces by accident — it
    concatenates — so copying it literally is what lands the bug.
    """
    html = ui.page_html()
    src = function_source(html, "patchReasons")
    assert re.search(r"setText\(\w+, String\(", src), \
        "a reason is written without being made a string first"

    card = function_source(html, "patchRoundCard")
    call = re.search(r"patchReasons\((\w+)\.lastElementChild, \w+\)", card)
    assert call, "the reasons box is filled by something other than patchReasons"
    assert not re.search(rf"patchField\(\s*{call.group(1)}\b", card), \
        "the reasons field also goes through patchField, whose setText wipes every row"


def test_a_round_with_no_reasons_hides_the_field():
    """AC-5's last sentence. patchField is what takes an empty field off the card,
    and the reasons no longer go through it — so the hide is written out here, or
    a parked round and a round the executor is still inside both grow a bare
    REASONS heading over nothing. Task 4 makes exactly that round's card the open
    one, so it is the heading the reader would meet first.

    The other fields still get their hide from patchField, which is the reason
    this is the only one written by hand.
    """
    html = ui.page_html()
    src = function_source(html, "patchRoundCard")
    held = re.search(r"const (\w+) = \w+\.verdict_reasons \|\| \[\];", src)
    assert held, "the reasons list is not read once and used for both the hide and the rows"
    hide = re.search(rf"(\w+)\.hidden = !{held.group(1)}\.length", src)
    assert hide, "the reasons field is drawn whether or not the round has any reasons"
    assert re.search(rf"patchReasons\({hide.group(1)}\.lastElementChild, {held.group(1)}\)", src), \
        "the field that is hidden and the box that is filled are not the same field"
    assert re.search(r"\.hidden = !\w+;", function_source(html, "patchField")), \
        "the fields that still go through patchField lost their hide with it"


def test_a_reason_that_wraps_is_indented_past_its_first_line():
    """AC-6, and the whole reason a reason is an element rather than a line.

    Eight reasons rendered as eleven wrapped lines with nothing saying where one
    ended and the next began: a wrapped line read as a new reason. The fix is a
    hanging indent, which only works as a pair — the padding pushes the whole row
    in, the negative text-indent pulls the FIRST line back out, so everything after
    the wrap sits further in than the reason's own start. Either half alone indents
    the block and marks nothing, so the two numbers are checked against each other
    and not merely for being present.

    A single text node cannot be given this at all, which is what the spec rejected
    a CSS-only fix over.
    """
    body = reason_row_declarations(ui.page_html())
    assert body, "the reason rows have no style of their own"
    pad = re.search(r"padding-left:\s*(-?[\d.]+)ch", body)
    indent = re.search(r"text-indent:\s*(-?[\d.]+)ch", body)
    assert pad and indent, "a reason row carries no hanging indent, so a wrap reads as a new reason"
    assert float(pad.group(1)) > 0, "the rows are not indented, so there is nothing to hang from"
    assert float(indent.group(1)) == -float(pad.group(1)), \
        "the first line is not pulled back out by what the row was pushed in, so the whole reason moves together"


def test_a_reason_row_keeps_the_field_typography():
    """A reason row is a field on the card like any other, and it has to read like
    one: 14px reasons beside 12.5px files is one card in two type sizes.

    The break matters more than it looks. A supervisor's reason quotes paths and
    URLs, and an unbroken 200-character one with nowhere to break spills straight
    past the card boundary Task 4 adds. Whether the declarations are restated on
    the rows or the selector the other fields use is widened to reach them is the
    page's business; what lands on the row is not.
    """
    html = ui.page_html()
    body = reason_row_declarations(html)
    field = declarations(html, ".round .field span")
    for piece in ("font-size:12.5px", "word-break:break-word"):
        assert piece.replace(" ", "") in body.replace(" ", ""), \
            f"a reason row does not carry {piece}, which every other field on the card has"
        assert piece.replace(" ", "") in field.replace(" ", ""), \
            f"the fields that still join lost {piece}, so the card is in two type sizes"


def test_a_round_card_has_a_visible_boundary():
    """AC-8. A card's only separator was 22px of empty space between it and the
    next one, and collapsing it to a summary line takes even that away: a closed
    card would be one dim line floating on the board with nothing saying it was a
    card at all.

    The three declarations are `#awaiting`'s and `.panel`'s rather than three new
    numbers — the board already has one shape for a box, and a round card is a
    box — so they are checked against a rule that already carries them. Nothing
    else in this file can look at this: the suite has never rendered the page.
    """
    html = ui.page_html()
    body = declarations(html, ".round").replace(" ", "")
    for piece in ("background:var(--panel)", "border:1pxsolidvar(--line)", "border-radius:10px"):
        assert piece in body, f"a round card carries no {piece}, so it has no edge"
        assert piece in declarations(html, ".panel").replace(" ", ""), \
            f"the panels lost {piece}, so the card is copying a shape nothing else on the board has"
    assert re.search(r"padding:[\d.]+px", body), \
        "the card has no padding, so its text runs into the border it just grew"
    heads = declarations(html, "#sweep > h2").replace(" ", "")
    assert "text-transform:uppercase" in heads, \
        "the sweep and items headings lost their styling along with the round card's h2"
    summary = declarations(html, ".round > summary").replace(" ", "")
    assert "cursor:pointer" in summary, "the summary does not say it can be clicked"
    assert "text-transform:uppercase" in summary, \
        "the summary is not dressed as the heading it replaced"

    # AC-10's read result rests on this rule and nothing else. The `·` inside
    # `item 1 · round 2` is text, but the two separators joining the three spans
    # are generated content, and without them the line renders
    # `item 1 · round 2accept4 files`. Nothing else can catch that: every source
    # check above stays green with the rule deleted, and so does a scripted
    # browser, because innerText and textContent both leave generated content out.
    # A screenshot is the only other witness, which is what the checklist is for.
    seps = [(sel, body) for sel, body in css_rules(html)
            if ".round" in sel and "summary" in sel and "::" in sel]
    assert seps, "nothing separates the summary's three spans, so its words run together"
    sel, body = seps[0]
    assert re.search(r"content:\s*'[^']*·[^']*'", body), \
        "the separator rule generates something other than the board's own `·`"
    # A round with no verdict and no files has two empty spans, and a dot in front
    # of each is `item 1 · round 4 · ·` on the card the reader meets first — the
    # round the executor is still inside, which is the one card that opens.
    #
    # So the guard has to sit on the span RECEIVING the pseudo-element, and asking
    # only whether the selector says `:empty` anywhere does not check that.
    # `span:not(:empty) ~ span::before` keeps the word and loses the rule: the
    # head is never empty, so the sibling half is true for every span after it and
    # the dot comes back in front of both empty ones. Read off the last part of
    # each selector rather than off the selector as a whole, so a rule written any
    # other way that still guards the right span still passes.
    #
    # The comma split is this file's usual "read the source as text", not a CSS
    # parser: a `:not(a, b)` would defeat it, and the page has none.
    guarded = 0
    for piece in sel.split(","):
        # `:not( :empty )` is the same guard spaced out, and the split below reads
        # whitespace as a descendant combinator. Closed up first so it is not.
        piece = re.sub(r"\s*([()])\s*", r"\1", piece.strip())
        if "::" not in piece:
            continue
        subject = re.split(r"[>+~\s]+", piece)[-1]
        assert ":not(:empty)" in subject, \
            f"`{subject}` takes a separator without being asked whether it has anything in it"
        guarded += 1
    assert guarded, "no part of the rule generates a separator at all"


def test_board_prose_is_capped_at_80ch():
    """AC-7. Measured on a 1440px viewport a reason ran 151 characters to the line,
    which is roughly twice what anyone reads without losing their place. The cap
    goes on the four blocks of sentences the board shows and nowhere wider.

    Not on `.round`, `.panels` or `.panel`: a cap there is inherited into the log
    panes and the diff, where it would hold an open pane to about 610px against
    the 900px AC-12 asks for two tasks later — and the suite would never see it,
    because the only thing that can is a browser.
    """
    html = ui.page_html()
    for selector in (".round .field span", "#awaiting .q", "#state .reason"):
        assert "max-width:80ch" in declarations(html, selector).replace(" ", ""), \
            f"{selector} is prose and runs the full width of the board"
    assert "max-width:80ch" in reason_row_declarations(html).replace(" ", ""), \
        "the reason rows are prose and run the full width of the board"
    for wide in (".round", ".panels", ".panel"):
        assert "max-width" not in declarations(html, wide), \
            f"{wide} carries a max-width, which every log and diff pane inside it inherits"


def test_the_verdict_colour_outranks_the_neutral_rule():
    """AC-11's last sentence: one round, one colour, in both places it is printed.

    The two words sit under different rules and only one of them has a competitor.
    `.round .verdict` sets the neutral accent and is two classes deep; the summary
    span has nothing over it at all. So a bare `.good { color:… }` — the obvious
    way to write this — colours the closed card's word and loses on the open
    card's, because specificity beats source order and there are no cascade layers
    or !important on this page outside the `[hidden]` rule. The result is one
    round reading green on one line and accent blue on the next, which is the page
    contradicting itself in the channel a reader believes fastest.

    Nothing else here can see it. Every source check passes on the bare form, and
    a browser reading text back sees the words unchanged either way — the colour
    is the whole of the difference. So the weight is counted rather than eyeballed.

    The three colours are the rail's own pills read back out of the stylesheet,
    not three hexes typed again: green means accepted everywhere on this page or
    it means nothing.
    """
    html = ui.page_html()
    rules = css_rules(html)
    neutral = [i for i, (sel, body) in enumerate(rules)
               if ".round .verdict" in [p.strip() for p in sel.split(",")]
               and "color:" in body.replace(" ", "")]
    assert neutral, "nothing sets the verdict line's neutral colour, so there is nothing to outrank"
    floor = specificity(".round .verdict")
    for tone, pill in (("good", ".green"), ("warn", ".yellow"), ("bad", ".red")):
        want = re.search(r"color:\s*(#[0-9a-fA-F]+)", declarations(html, pill))
        assert want, f"the rail's {pill} pill has no colour of its own for the verdict to match"
        on_line = on_summary = None
        for i, (sel, body) in enumerate(rules):
            got = re.search(r"color:\s*(#[0-9a-fA-F]+)", body)
            if not got or got.group(1).lower() != want.group(1).lower():
                continue
            for piece in [p.strip() for p in sel.split(",")]:
                subject = compounds(piece)[-1]
                if f".{tone}" not in subject:
                    continue
                if ".s-verdict" in subject:
                    on_summary = piece
                elif ".verdict" in subject:
                    weight = specificity(piece)
                    if weight > floor or (weight == floor and i > max(neutral)):
                        on_line = piece
        assert on_line, \
            (f"no rule paints the open card's verdict line {want.group(1)} and outranks "
             f"`.round .verdict` {floor}, so the word stays accent blue there while the summary "
             f"turns {tone} — one round in two colours")
        assert on_summary, \
            (f"no rule names `.s-verdict` and paints it {want.group(1)}, so the closed card's "
             f"word carries the meaning of `{tone}` only for as long as nothing else on the "
             "page claims that class name")


def test_an_open_pane_takes_the_closed_panes_width():
    """AC-12. A row of two panes split it 50/50 whatever was in them, so opening
    one log gave the reader half a row — 516px of 1046 on a 1440px viewport —
    beside a closed pane showing nothing but its own label. One rule, no
    JavaScript: while a sibling log pane is open a closed one stops flexing and
    shrinks to that label, which took the open one to 914px when this went in.

    Scoped to `.panel.log` at both ends. The diff pane is a `.panel` too, holds no
    log name and sits alone in #diff — buildLogPane's own comment records what
    happened the last time a selector written for a log pane reached it, which was
    `/api/log?name=undefined` every 2 seconds for as long as it was open. Checked
    on this rule's selector and not on the stylesheet as a whole: six shipped rules
    begin `.panel.diff` on purpose.

    The second half is what the rule alone does not buy. A `max-width` anywhere
    inside a panel caps the text at a width the pane no longer has, and the reader
    gets a wider box holding the same narrow column — the widening undone, with
    every check in this file green. AC-7's cap is real prose and stops outside the
    panes; this is the guard that keeps it there whatever gets added later.

    The measurement itself — at least 900px on a 1440px viewport — belongs to a
    browser and to the checklist. Nothing in this file has ever laid out a page,
    so every number in this docstring was read off one and none of it is asserted
    below.
    """
    html = ui.page_html()
    found = [(sel, body) for sel, body in css_rules(html) if ":has(" in sel and ".log" in sel]
    assert len(found) == 1, \
        f"expected one rule making room for an open log pane, found {len(found)}"
    sel, body = found[0]
    assert re.search(r"flex:\s*0\s+0\s+auto", body), \
        f"`{sel}` sets {body.strip()!r} rather than stopping the closed pane from flexing"
    pieces = compounds(sel)
    subject = pieces[-1]
    assert ".log" in subject and ":not([open])" in subject, \
        f"the rule paints `{subject}` rather than a log pane that is closed"
    assert any(p.startswith(".panels") for p in pieces[:-1]), \
        f"`{sel}` is not scoped to a row of panes, so a lone pane shrinks to its label"
    arg = re.search(r":has\(([^()]*)\)", sel)
    assert arg, "the rule asks nothing about what the row already has open"
    assert ".log" in arg.group(1) and "[open]" in arg.group(1), \
        (f"the closed pane shrinks on `{arg.group(1).strip()}` rather than on a log pane "
         "beside it being open, so both-closed is no longer the 50/50 it was")
    # The one rule that must never reach the diff pane, read compound by compound:
    # every part of it that names a panel names a log pane too, argument included.
    for piece in pieces + compounds(arg.group(1)):
        if re.search(r"\.panel\b", piece):
            assert ".log" in piece, \
                f"`{piece}` matches every panel, and the diff pane is one of them"
    assert "flex:1" in declarations(html, ".panel").replace(" ", ""), \
        "the panes no longer share the row, so two open ones are not the 50/50 they were"

    for rule_sel, rule_body in css_rules(html):
        if "max-width" not in rule_body:
            continue
        for piece in [p.strip() for p in rule_sel.split(",")]:
            assert not re.search(r"\.panels?\b", piece), \
                (f"`{piece}` caps a width inside a log pane, so the room this rule makes "
                 "reaches the box and never the text in it")


# ---------- the diff pane ----------


def test_the_diff_is_fetched_by_workflow_id():
    """AC-15. The two keys are not interchangeable and this is the page's end of
    the split: `/api/diff` takes a workflow id, `/api/log` takes a run directory,
    and two workflows of one run directory share the second and not the first.
    Sending `dir` here would draw one run's branch on the other's board.

    The id is the one the pane was built with rather than whatever `sel` says when
    the reply lands: a reader who picks another run mid-request has a new board,
    and this pane must not write a stale diff into it.
    """
    html = ui.page_html()
    assert "/api/diff?id=" in html, "the page never asks for the diff"
    assert "patch cut at 200 KB" in html, "a patch stopped at the cap says nothing about it"
    src = function_source(html, "patchDiff")
    assert "dataset.id" in src, "the pane asks for something other than the id it holds"
    assert not re.search(r"\bsel\b", src), "the pane reads the global selection instead"
    assert "dir" not in src, "a run directory reaches the endpoint that takes an id"


def test_the_diff_pane_is_opened_not_polled():
    """AC-9. The diff is two git commands against the owner's own repository, so
    it goes out when the reader asks for it and never on a timer. A collapsed pane
    sends nothing; each opening sends one request.

    The log panes are the other half: `patchOpenPanes` runs every 2 seconds over
    every open pane, so its selector has to name the panes that poll rather than
    every `.panel` on the board — the diff pane is one of those, and it carries no
    log name to ask about.
    """
    html = ui.page_html()
    src, regs = regions(html)
    asking = {n for n, s, e in regs if "/api/diff" in src[s:e]}
    assert asking == {"patchDiff"}, f"the diff is fetched from {asking}"
    callers = {n for n, s, e in regs if n != "patchDiff" and "patchDiff(" in src[s:e]}
    assert callers == {"buildDiffPane"}, f"patchDiff is called from {callers}"

    build = function_source(html, "buildDiffPane")
    assert "createElement('details')" in build, "the pane cannot collapse itself"
    assert re.search(r"addEventListener\('toggle'.*\.open\).*patchDiff\(", build), \
        "the pane does not fetch when it is opened"

    polled = function_source(html, "patchOpenPanes")
    assert "/api/log?" in polled and "/api/diff" not in polled
    assert ".panel[open]" not in polled, \
        "the poll's selector takes in the diff pane, which has no log to ask for"


# ---------- the host repository behind a run's worktree ----------


@pytest.fixture
def wt(tmp_path):
    """A run's worktree pointer, and the repository on this machine it points at.

    The pointer holds the one line every live worktree holds today, the `/app/runs`
    worktree is the container path the ledger records, and `<tmp_path>/projects`
    stands in for LOOPGRAPH_PROJECTS_DIR. tmp_path and never a real directory:
    test_release.py fails the build on a home directory in a tracked file.
    """
    runs_dir = tmp_path / "runs"
    pointer = runs_dir / "x" / "worktrees" / "ab12cd" / ".git"
    pointer.parent.mkdir(parents=True)
    pointer.write_text("gitdir: /projects/proj/.git/worktrees/ab12cd\n")
    projects = tmp_path / "projects"
    (projects / "proj").mkdir(parents=True)
    return SimpleNamespace(runs_dir=runs_dir, pointer=pointer, projects=str(projects),
                           worktree="/app/runs/x/worktrees/ab12cd", repo=projects / "proj")


def test_a_pointer_resolves_to_the_host_repository(wt):
    assert ui.resolve_repo(wt.worktree, wt.runs_dir, wt.projects) == (wt.repo, "")


def test_the_projects_prefix_swap_keeps_the_separator(wt):
    """The separator is the whole trap. .env.example ships
    LOOPGRAPH_PROJECTS_DIR=/home/you/projects and install.sh writes what the owner
    typed without normalising it, so no real value carries a slash of its own.
    Swapping the bare value in for `/projects/` turns /projects/deye into
    <dir>deye, which exists on no machine, so every diff on the dashboard would
    answer `repository not found`."""
    assert not wt.projects.endswith("/"), "the shipped shape has no trailing slash"
    path, err = ui.resolve_repo(wt.worktree, wt.runs_dir, wt.projects)
    assert err == ""
    assert str(path) == wt.projects + "/proj"
    assert str(path) != wt.projects + "proj", "the swap ate the separator"


def test_a_projects_dir_with_a_trailing_slash_resolves_the_same(wt):
    assert ui.resolve_repo(wt.worktree, wt.runs_dir, wt.projects + "/") == (wt.repo, "")


def test_a_nested_projects_segment_is_not_rewritten(wt):
    """The swap is anchored at the start and made once. A whole-string replace
    would rewrite the second `/projects/` too and land nowhere."""
    wt.pointer.write_text("gitdir: /projects/deye/projects/x/.git/worktrees/ab12cd\n")
    (Path(wt.projects) / "deye" / "projects" / "x").mkdir(parents=True)
    path, err = ui.resolve_repo(wt.worktree, wt.runs_dir, wt.projects)
    assert (path, err) == (Path(wt.projects) / "deye" / "projects" / "x", "")


@pytest.mark.parametrize("case", ["host path", "discarded worktree", "no gitdir line",
                                  "projects dir unset", "projects dir empty",
                                  "repository gone"])
def test_each_resolve_failure_returns_its_line(wt, tmp_path, case):
    """None of these is an exception. Each is a run the owner can still look at,
    with one line saying why there is no diff."""
    worktree, projects = wt.worktree, wt.projects
    if case == "host path":  # the ledger records the container path, not this one
        worktree = str(wt.pointer.parent)
        want = f"worktree is not a container path: {worktree}"
    elif case == "discarded worktree":  # `lg discard` removed it, pointer and all
        worktree = "/app/runs/x/worktrees/gone"
        want = f"no worktree pointer at {wt.runs_dir / 'x' / 'worktrees' / 'gone' / '.git'}"
    elif case == "no gitdir line":
        wt.pointer.write_text("ref: refs/heads/main\n")
        want = "pointer file has no gitdir line"
    elif case == "projects dir unset":  # no .env yet, or the key is missing from it
        projects = None
        want = "LOOPGRAPH_PROJECTS_DIR is not set; run ./install.sh"
    elif case == "projects dir empty":  # `LOOPGRAPH_PROJECTS_DIR=` reads as ""
        projects = ""
        want = "LOOPGRAPH_PROJECTS_DIR is not set; run ./install.sh"
    else:
        projects = str(tmp_path / "elsewhere")
        want = f"repository not found: {tmp_path / 'elsewhere' / 'proj'}"

    assert ui.resolve_repo(worktree, wt.runs_dir, projects) == (None, want)


def test_a_gitdir_line_naming_no_repository_is_refused(wt):
    """Path("") is PosixPath("."), and "." is a directory on every machine. Without
    the emptiness guard resolve_repo hands back the dashboard's own working
    directory as "the repository", and /api/diff runs git in the loopgraph
    checkout instead of the owner's."""
    wt.pointer.write_text("gitdir:\n")
    assert ui.resolve_repo(wt.worktree, wt.runs_dir, wt.projects) == (
        None, "pointer file has no gitdir line")

    # The other half of the same guard: a value that names a repository but no
    # worktree under it still resolves to that repository, never to ".".
    wt.pointer.write_text("gitdir: /projects/proj\n")
    path, err = ui.resolve_repo(wt.worktree, wt.runs_dir, wt.projects)
    assert (path, err) == (wt.repo, "")
    assert path != Path("."), "the server's own directory is not a run's repository"


def test_a_pointer_that_is_not_utf8_still_answers(wt):
    """A pointer file is bytes on disk, and nothing guarantees they decode. Without
    errors="replace" this raises UnicodeDecodeError, which is not an OSError, so it
    goes straight past the handler and `lg ui` prints a traceback."""
    wt.pointer.write_bytes(b"gitdir: /projects/proj/.git/worktrees/\xff\xfe\n")
    assert ui.resolve_repo(wt.worktree, wt.runs_dir, wt.projects) == (wt.repo, "")


def test_a_worktree_holding_a_nul_byte_answers_instead_of_raising(wt):
    """read_text on a path with a NUL raises ValueError, not OSError: the one input
    shape that escaped the reason line and reached the socket as a traceback."""
    worktree = "/app/runs/x\x00y/worktrees/ab12cd"
    path, err = ui.resolve_repo(worktree, wt.runs_dir, wt.projects)
    assert path is None
    assert err.startswith("no worktree pointer at ")


def test_a_repository_path_too_long_for_the_filesystem_answers(wt):
    """Path.is_dir() swallows ENOENT and a NUL, but NOT ENAMETOOLONG — it re-raises
    that one. A pointer file is not a bounded input, so the guard around it is live
    code, not decoration."""
    wt.pointer.write_text(f"gitdir: /projects/{'p' * 5000}/.git/worktrees/ab12cd\n")
    path, err = ui.resolve_repo(wt.worktree, wt.runs_dir, wt.projects)
    assert path is None
    assert err.startswith("repository not found: ")


# ---------- the diff of a run's branch ----------

DIFF_ID = "run-x-ab12cd"
BASE = "main"
BRANCH = "lg-x-ab12cd"
WORKTREE = "/app/runs/x/worktrees/ab12cd"  # the container path the `wt` fixture holds

# The caller's git, with none of the caller's configuration: a global hook, an
# init template or a default branch name would otherwise decide what these assert.
GIT_ENV = {**os.environ,
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
           "GIT_AUTHOR_NAME": "lg", "GIT_AUTHOR_EMAIL": "lg@example.invalid",
           "GIT_COMMITTER_NAME": "lg", "GIT_COMMITTER_EMAIL": "lg@example.invalid"}


def git(repo, *args) -> str:
    done = subprocess.run(["git", *args], cwd=repo, env=GIT_ENV, check=True,
                          capture_output=True, text=True)
    return done.stdout


def commit(repo, name, text, message):
    (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", "--", name)
    git(repo, "commit", "-qm", message)


def ledger_for(status="merge-ready", **last_round):
    """A ledger shaped like the engine's.

    Two rounds, because the handler must read the LAST one: a run's worktree and
    branches are recorded per round, and round 1 of a real ledger predates them.
    """
    last = {"worktree": WORKTREE, "branch": BRANCH, "base_branch": BASE, "verdict": "PASS"}
    last.update(last_round)
    return {"status": status, "rounds": [{"verdict": "REDO"}, last]}


@pytest.fixture
def diffing(tmp_path, monkeypatch, wt):
    """Everything /api/diff needs, wired the way a request wires it.

    A real repository at the far end of `wt`'s pointer file, a .env naming the
    projects tree, and a feed holding one run's ledger — so the pointer, the
    prefix swap and the two git commands are exercised together rather than
    separately mocked.
    """
    assert wt.worktree == WORKTREE, "the ledger and the pointer fixture must agree"
    git(wt.repo, "init", "-q", "-b", BASE)
    commit(wt.repo, "README.md", "base\n", "base")
    git(wt.repo, "checkout", "-q", "-b", BRANCH)
    commit(wt.repo, "feature.txt", "hello from the branch\n", "add the feature")
    git(wt.repo, "checkout", "-q", BASE)

    (tmp_path / ".env").write_text(f"LOOPGRAPH_PROJECTS_DIR={wt.projects}\n")
    monkeypatch.setattr(ui, "ROOT", tmp_path)

    feed = FakeFeed(ledgers={DIFF_ID: ledger_for()})
    srv = ui.make_server(0, wt.runs_dir, temporal_addr=None, feed=feed)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield SimpleNamespace(url=f"http://127.0.0.1:{srv.server_address[1]}",
                          repo=wt.repo, feed=feed, wt=wt, env=tmp_path / ".env")
    srv.shutdown()


def test_diff_names_the_changed_file_and_carries_the_patch(diffing):
    d = getjson(f"{diffing.url}/api/diff?id={DIFF_ID}")
    assert "feature.txt" in d["stat"]
    assert "+hello from the branch" in d["patch"]
    assert d["truncated"] is False


def test_diff_without_temporal_is_200_with_a_reason(server):
    r = get(f"{server}/api/diff?id={KNOWN}")
    assert r.status == 200
    assert json.loads(r.read()) == {"stat": "no ledger for this workflow",
                                    "patch": "", "truncated": False}


@pytest.mark.parametrize("case", ["no rounds", "host path", "discarded worktree",
                                  "no gitdir line", "projects dir unset",
                                  "repository gone", "branch gone", "base gone"])
def test_each_failure_is_a_200_with_its_line(diffing, tmp_path, case):
    """None of these is a 500 and none of them is a traceback. Each is a run the
    owner can still open, with one line in `stat` saying why there is no diff."""
    wt, ledger = diffing.wt, ledger_for()
    if case == "no rounds":
        ledger["rounds"] = []
        want = "no rounds yet"
    elif case == "host path":  # the ledger records the container path, not this one
        ledger["rounds"][-1]["worktree"] = str(wt.pointer.parent)
        want = f"worktree is not a container path: {wt.pointer.parent}"
    elif case == "discarded worktree":  # `lg discard` removed it, pointer and all
        ledger["rounds"][-1]["worktree"] = "/app/runs/x/worktrees/gone"
        want = f"no worktree pointer at {wt.runs_dir / 'x' / 'worktrees' / 'gone' / '.git'}"
    elif case == "no gitdir line":
        wt.pointer.write_text("ref: refs/heads/main\n")
        want = "pointer file has no gitdir line"
    elif case == "projects dir unset":  # no .env on this machine yet
        diffing.env.unlink()
        want = "LOOPGRAPH_PROJECTS_DIR is not set; run ./install.sh"
    elif case == "repository gone":  # the repository moved after the run
        diffing.env.write_text(f"LOOPGRAPH_PROJECTS_DIR={tmp_path / 'elsewhere'}\n")
        want = f"repository not found: {tmp_path / 'elsewhere' / 'proj'}"
    elif case == "branch gone":  # the owner deleted it after merging by hand
        ledger["rounds"][-1]["branch"] = "lg-x-deleted"
        want = "branch not found: lg-x-deleted"
    else:
        ledger["rounds"][-1]["base_branch"] = "trunk"
        want = "branch not found: trunk"
    diffing.feed.ledgers[DIFF_ID] = ledger

    r = get(f"{diffing.url}/api/diff?id={DIFF_ID}")
    assert r.status == 200
    assert json.loads(r.read()) == {"stat": want, "patch": "", "truncated": False}


def test_a_large_patch_is_cut_at_the_cap(diffing):
    """300 KB of a three-byte character, so bytes and characters are far apart.
    Cutting characters lets three times the cap through; cutting bytes and then
    decoding strictly raises on the character the cut lands inside, which would
    turn every large diff into `diff failed`."""
    git(diffing.repo, "checkout", "-q", BRANCH)
    commit(diffing.repo, "big.txt", "€" * 100_000 + "\n", "a large non-ascii file")
    git(diffing.repo, "checkout", "-q", BASE)

    d = getjson(f"{diffing.url}/api/diff?id={DIFF_ID}")
    assert d["truncated"] is True
    # For a patch body that is otherwise valid UTF-8, errors="replace" turns the
    # one or two bytes of the character the cut landed inside into a single
    # U+FFFD, which re-encodes to three, so the decoded patch can measure two
    # bytes over the cut. Two, not two hundred thousand. It is not a general
    # bound and does not claim to be: a latin-1 file, which git diffs as text,
    # makes every invalid byte its own U+FFFD and can treble the decoded length.
    # AC-18 caps the raw bytes, and the raw bytes are capped exactly.
    assert len(d["patch"].encode()) <= ui.DIFF_CAP + 2
    assert ui.DIFF_CAP == 204_800 and ui.STAT_CAP == 20_480


def test_the_cap_bounds_what_is_read_not_only_what_is_returned(diffing):
    """The cap exists to keep a huge diff out of the dashboard's memory. Applied
    after git has run to completion it does nothing about that: the 16 MB is
    already in the process by the time the slice happens."""
    git(diffing.repo, "checkout", "-q", BRANCH)
    commit(diffing.repo, "huge.txt", ("x" * 79 + "\n") * 200_000, "16 MB of diff")
    git(diffing.repo, "checkout", "-q", BASE)

    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        d = getjson(f"{diffing.url}/api/diff?id={DIFF_ID}")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert d["truncated"] is True
    assert peak < 4_000_000, f"the whole diff was read before it was cut ({peak} bytes)"


def test_a_git_command_past_its_deadline_answers_instead_of_hanging(diffing, tmp_path,
                                                                    monkeypatch):
    """gate.py carries the scar: a deadline that governed the drain instead of the
    process was silently not enforced. A hang is not an exception, so nothing else
    catches it and the browser's fetch never fills.

    The slow thing is an external-diff driver rather than a textconv one, because
    --no-textconv means a repository's textconv never runs here at all — and a
    test that hangs git through a path the endpoint has closed would prove
    nothing."""
    assert ui.DIFF_TIMEOUT == 20, "the line the owner reads says 20s"
    slow = tmp_path / "slow-external-diff"
    slow.write_text("#!/bin/sh\nsleep 5\n")
    slow.chmod(0o755)
    git(diffing.repo, "config", "diff.slow.command", str(slow))
    (diffing.repo / ".gitattributes").write_text("feature.txt diff=slow\n")
    monkeypatch.setattr(ui, "DIFF_TIMEOUT", 1)

    t0 = time.monotonic()
    d = getjson(f"{diffing.url}/api/diff?id={DIFF_ID}")
    assert time.monotonic() - t0 < 4, "the deadline did not govern git itself"
    assert d == {"stat": "git took longer than 1s; nothing to show",
                 "patch": "", "truncated": False}


def test_the_process_group_is_captured_before_the_pid_can_be_reused():
    """The timer thread can fire after proc.wait() has reaped the pid, and the
    kernel may already have handed that pid to someone else. os.getpgid on it then
    names a stranger's process group, and killpg — unlike kill — takes that whole
    group down.

    There is no reaching that window from a test: it is microseconds wide and
    needs PID wraparound inside it. So pin the shape, the way this file pins
    LOG_GLOB and HEAD_MARK. The group id is read once, before anything can wait on
    the process, and nothing is signalled after the wait."""
    # Code only: the docstring and the comments beside it say `proc.wait()` too.
    code = "\n".join(ln for ln in inspect.getsource(ui._git_read_only).split('"""')[2].splitlines()
                     if not ln.strip().startswith("#"))
    assert "os.killpg(os.getpgid(" not in code, "the kill looks up a pid that may be reused"
    assert code.index("os.getpgid(") < code.index("os.killpg("), "looked up at kill time"
    assert code.index("os.getpgid(") < code.index(".wait()"), "looked up after the reap"
    assert "if reaped:" in code, "a signal can still land after proc.wait()"


def git_dir_state(repo):
    """Every path under .git with its size and mtime, measured without git.

    Running git to find out whether git wrote something is not a measurement: a
    plain `git status` refreshes the index and writes it back.
    """
    return sorted((str(p.relative_to(repo)), p.lstat().st_size, p.lstat().st_mtime_ns)
                  for p in (repo / ".git").rglob("*"))


def test_a_repository_that_caches_its_text_conversion_is_still_not_written_to(
        diffing, tmp_path):
    """git diff is not unconditionally read-only, and the owner's repositories are
    not ours to configure.

    A repository whose config sets diff.<driver>.cachetextconv stores each
    conversion in git notes on the first diff that needs one: new objects,
    refs/notes/textconv/<driver>, its reflog, and a ref lock taken to write them.
    --no-optional-locks does not stop it and neither does --no-ext-diff.
    --no-textconv does, and the dashboard shows a raw patch, so it has no use for
    a repository's text conversion in the first place.
    """
    conv = tmp_path / "textconv"
    conv.write_text('#!/bin/sh\ncat "$1"\n')
    conv.chmod(0o755)
    git(diffing.repo, "config", "diff.tc.textconv", str(conv))
    git(diffing.repo, "config", "diff.tc.cachetextconv", "true")
    (diffing.repo / ".gitattributes").write_text("feature.txt diff=tc\n")

    before = git_dir_state(diffing.repo)
    d = getjson(f"{diffing.url}/api/diff?id={DIFF_ID}")
    assert "feature.txt" in d["stat"], "the diff still has to work"
    assert git_dir_state(diffing.repo) == before, "the diff wrote to the repository"

    # Both commands carry the flag, and only the patch command can be shown to
    # need it: today's git does not convert text to count lines for --stat, so
    # dropping the flag there breaks nothing this test can see. Which commands
    # convert is git's business, it has changed before, and the flag costs
    # nothing — so pin it rather than let it be tidied off the half that looks
    # unused.
    assert inspect.getsource(ui.branch_diff).count('"--no-textconv"') == 2


def test_a_helper_git_left_behind_cannot_pin_the_request(diffing, tmp_path, monkeypatch):
    """A deadline that kills git alone is no deadline at all once git has exited.

    git runs a repository's own external-diff driver with git's own stdout, so a
    driver that leaves a child running holds the dashboard's pipe open long after
    git is gone, and killing a process that has already exited does nothing —
    gate.py's scar, where killing the shell left `npm run build`'s children alive
    past the timeout. Killing the whole process group is what ends it: 30 seconds
    against 1 on this repository."""
    driver = tmp_path / "external-diff"
    driver.write_text("#!/bin/sh\nsleep 30 &\necho 'external diff output'\n")
    driver.chmod(0o755)
    git(diffing.repo, "config", "diff.stray.command", str(driver))
    (diffing.repo / ".gitattributes").write_text("feature.txt diff=stray\n")
    monkeypatch.setattr(ui, "DIFF_TIMEOUT", 1)

    t0 = time.monotonic()
    d = json.loads(get(f"{diffing.url}/api/diff?id={DIFF_ID}", timeout=45).read())
    assert time.monotonic() - t0 < 10, "a child git left behind pinned the request thread"
    assert d["patch"] == "" and d["truncated"] is False


def test_a_merged_branch_says_it_is_merged(diffing):
    """merge_branch merges the run's branch into its base with --no-ff and deletes
    neither, so the branch becomes an ancestor of the base and the three-dot diff
    holds nothing. That is every run the owner approved, and it is the state they
    come back to look at, so an empty pane there would be the common case."""
    git(diffing.repo, "merge", "-q", "--no-ff", "-m", "merge the run", BRANCH)
    diffing.feed.ledgers[DIFF_ID] = ledger_for(status="merged")

    r = get(f"{diffing.url}/api/diff?id={DIFF_ID}")
    assert r.status == 200
    assert json.loads(r.read()) == {
        "stat": "already merged into main; the branch adds nothing to it",
        "patch": "", "truncated": False}


def test_a_branch_with_no_commits_says_so(diffing):
    """The same empty diff, from the other end: a run whose branch is still where
    it started. The ledger's status is what tells the two apart, never a third
    git command."""
    git(diffing.repo, "branch", "-f", BRANCH, BASE)
    diffing.feed.ledgers[DIFF_ID] = ledger_for(status="running")

    assert getjson(f"{diffing.url}/api/diff?id={DIFF_ID}") == {
        "stat": "no changes on this branch yet", "patch": "", "truncated": False}


def test_extra_request_parameters_are_ignored(diffing):
    """The request supplies a workflow id and nothing else. A branch name that
    arrives in a URL and reaches a git command is an injection."""
    plain = get(f"{diffing.url}/api/diff?id={DIFF_ID}").read()
    spiked = get(f"{diffing.url}/api/diff?id={DIFF_ID}&branch=main&base=x").read()
    assert spiked == plain


def test_the_diff_leaves_the_repository_alone(diffing):
    """The owner may be working in this repository while the dashboard polls it.
    Only the refs are asserted: an unrelated git call refreshes the index, which
    would make a working-tree assertion flaky rather than true."""
    def refs():
        return git(diffing.repo, "symbolic-ref", "HEAD"), git(diffing.repo, "show-ref")

    before = refs()
    getjson(f"{diffing.url}/api/diff?id={DIFF_ID}")
    assert refs() == before


@pytest.mark.parametrize("bad", ["", "a/b", ".."])
def test_diff_rejects_bad_ids(server, bad):
    assert status_of(f"{server}/api/diff?{urlencode({'id': bad})}") == 400


# ---------- archiving: the one write the dashboard makes ----------

# The two sentences the page shows the owner when the server refuses (AC-18), and
# the one it shows when the store itself is the problem (AC-16). Copy, so they are
# written down once and asserted whole.
STILL_OPEN = "workflow still open: finish or answer it before archiving"
CANNOT_SAY = "cannot confirm the workflow is closed; is Temporal up?"
UNREADABLE = "runs/.archived.json is not a readable list; move it aside and try again"

# The run archiving exists to clear off the rail: an engine that died holding a
# card. workflows/run.py's blanket handler records the ledger it was holding, so
# this one reads `running` and holds an `awaiting` for ever, and the only thing
# that knows the workflow is over is Temporal. A gate that asked the ledger would
# refuse to archive this run — which is the run the owner is trying to archive.
STALE = {"status": "running", "rounds": [{"verdict": "REDO"}], "awaiting": AWAITING}


def dead_run_feed(open_states):
    """A feed serving that run, with `workflow_open` answering as the test asks."""
    return FakeFeed(rows=[ui.run_entry(KNOWN, "failed", START, CLOSE, STALE)],
                    ledgers={KNOWN: STALE}, open_states=open_states)


@pytest.fixture
def archiving(tmp_path):
    """Dashboards over one empty runs directory, each with the feed a test asks for.

    `make(feed)` starts one and returns its URL — more than one, because a restart
    over the same directory is what AC-16 promises survives, and because "no feed
    at all" is one of the conditions AC-18 refuses. `store` is the file every write
    lands in, so a test can read the bytes back and see that a refusal changed none
    of them.
    """
    started = []

    def make(feed=None):
        srv = ui.make_server(0, tmp_path, temporal_addr=None, feed=feed)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    yield SimpleNamespace(make=make, runs=tmp_path, store=tmp_path / ".archived.json")
    for srv in started:
        srv.shutdown()


def flags_of(url):
    """/api/runs' archived marks, keyed on the row id. KeyError if a row has none."""
    return {r["id"]: r["archived"] for r in getjson(url + "/api/runs")["runs"]}


def test_archiving_a_closed_run_round_trips(archiving):
    """AC-16, over the wire, on the run the feature is for. Its ledger says
    `running` and holds a question nobody can answer any more; Temporal says the
    workflow is closed. Temporal is what the server asks."""
    url = archiving.make(dead_run_feed({KNOWN: False}))
    assert flags_of(url) == {KNOWN: False}, "a run nobody archived is not archived"

    assert post(url + "/api/archive", {"id": KNOWN, "archived": True}) == \
        (200, {"id": KNOWN, "archived": True})
    assert flags_of(url) == {KNOWN: True}
    assert json.loads(archiving.store.read_text()) == [KNOWN]
    assert list(archiving.runs.iterdir()) == [archiving.store], \
        "the write left its temporary file behind in the runs directory"

    assert post(url + "/api/archive", {"id": KNOWN, "archived": False}) == \
        (200, {"id": KNOWN, "archived": False})
    assert flags_of(url) == {KNOWN: False}
    assert json.loads(archiving.store.read_text()) == []


def test_the_archive_survives_a_restart(archiving):
    """The flag is a file, not a dictionary in the process: `lg ui` is restarted
    every time the owner updates, and a rail that forgot what they had hidden
    would be the whole feature undone by a restart."""
    feed = dead_run_feed({KNOWN: False})
    first = archiving.make(feed)
    # Started before anything is archived. A server that read the store once and
    # kept it would answer for this one out of a set that has nothing in it, and a
    # second reader with the page open would go on showing an archived run.
    watching = archiving.make(feed)
    assert post(first + "/api/archive", {"id": KNOWN, "archived": True})[0] == 200
    assert flags_of(watching) == {KNOWN: True}

    assert flags_of(archiving.make(feed)) == {KNOWN: True}, "the flag died with the process"


@pytest.mark.parametrize("body", [
    b"", b"not json at all", b"[]", b'"a string"',
    {"archived": True},
    {"id": KNOWN},
    {"id": "a/b", "archived": True},
    {"id": "../secrets", "archived": True},
    {"id": "", "archived": True},
    {"id": KNOWN, "archived": "yes"},
    {"id": KNOWN, "archived": 1},
    {"id": 7, "archived": True},
    {"id": [KNOWN], "archived": True},
    {"id": {"id": KNOWN}, "archived": True},
], ids=["empty", "not json", "a list", "a bare string", "no id", "no archived",
        "id with a slash", "id with ..", "empty id", "archived is a word",
        "archived is a number", "id is a number", "id is a list", "id is an object"])
def test_bad_archive_bodies_are_400_and_change_nothing(archiving, body):
    """AC-16. Every one of these answers 400 WITH a body — the assertion is on the
    status, so a handler that raises before it answers fails here rather than
    passing: socketserver swallows the exception and the client gets a dropped
    connection, which is not a 400.

    The three non-string ids are the ones that could only fail that way.
    `bad_param` is `not value or "/" in value or ".." in value`, written for a
    value off a query string and never handed anything but one: a number makes
    `"/" in value` raise TypeError, and a list or an object sails through it and
    raises later on the path join. So the type check comes first and is not
    decoration.
    """
    archiving.store.write_text('["kept-by-hand"]')
    before = archiving.store.read_bytes()
    url = archiving.make(dead_run_feed({KNOWN: False}))

    code, answer = post(url + "/api/archive", body)
    assert code == 400, f"{body!r} was not refused with a 400"
    assert "error" in answer, "a 400 with no body tells the page nothing to show"
    assert archiving.store.read_bytes() == before, "a refused request wrote to the store"


def test_an_archive_body_over_the_cap_is_refused(archiving):
    """A body is read by Content-Length, so the cap is what stops one request
    putting an arbitrary number of megabytes in the dashboard's memory."""
    archiving.store.write_text('["kept-by-hand"]')
    url = archiving.make(dead_run_feed({KNOWN: False}))
    code, answer = post(url + "/api/archive", {"id": KNOWN, "archived": True,
                                               "pad": "x" * 5000})
    assert code == 400 and "error" in answer
    assert json.loads(archiving.store.read_text()) == ["kept-by-hand"]


@pytest.mark.parametrize("states,want", [
    ({KNOWN: True}, STILL_OPEN),
    ({}, CANNOT_SAY),
    (None, CANNOT_SAY),
], ids=["temporal says open", "temporal never heard of it", "no feed at all"])
def test_an_open_or_unknowable_workflow_is_409(archiving, states, want):
    """AC-18, on the server rather than in the page: archiving a run that is still
    working hides it while it goes on writing, and the owner would not see the card
    it puts up. Refusing what cannot be verified is the spec's decision — a feed
    that is down cannot tell a finished run from a running one, and guessing wrong
    costs the owner the run they were watching.

    The ledger here reads `running` and holds an `awaiting` in every case, so a
    gate keyed on it would refuse all three for the wrong reason and this test
    would pass while the run it is written about — the dead one above — stayed on
    the rail for ever.
    """
    archiving.store.write_text('["kept-by-hand"]')
    before = archiving.store.read_bytes()
    url = archiving.make(dead_run_feed(states) if states is not None else None)

    assert post(url + "/api/archive", {"id": KNOWN, "archived": True}) == \
        (409, {"error": want})
    assert archiving.store.read_bytes() == before, "a refused archive wrote the flag anyway"


@pytest.mark.parametrize("states", [{KNOWN: True}, {}, None],
                         ids=["temporal says open", "temporal never heard of it",
                              "no feed at all"])
def test_unarchiving_is_never_gated(archiving, states):
    """AC-18's other half. The refusal is one-directional: putting a run back on
    the rail hides nothing and needs nothing proved, and a message about finishing
    the workflow first is nonsense in that direction. The owner is most likely to
    be tidying up on the day Temporal is down, and that is exactly when a gated
    un-archive would strand every run they had hidden.
    """
    archiving.store.write_text(json.dumps([KNOWN, "kept-by-hand"]))
    feed = dead_run_feed(states) if states is not None else None
    url = archiving.make(feed)

    assert post(url + "/api/archive", {"id": KNOWN, "archived": False}) == \
        (200, {"id": KNOWN, "archived": False})
    assert json.loads(archiving.store.read_text()) == ["kept-by-hand"], \
        "the flag was not cleared, or another run's flag went with it"
    if feed is not None:
        assert feed.opened == [], "Temporal was asked about a run coming back on the rail"


def test_a_logs_only_row_archives_without_temporal(server, tmp_path):
    """A row known only from its log files is keyed on the run directory, which
    names no workflow: there is nothing to ask Temporal about and nothing to be
    open. Its neighbour in this test is the same server refusing a workflow-keyed
    id, so what makes the difference is the directory and not the missing feed."""
    assert post(server + "/api/archive", {"id": "2026-01-01-demo", "archived": True}) == \
        (200, {"id": "2026-01-01-demo", "archived": True})
    assert flags_of(server) == {"2026-01-01-demo": True}

    assert post(server + "/api/archive", {"id": KNOWN, "archived": True}) == \
        (409, {"error": CANNOT_SAY})
    assert ui.read_archived(tmp_path) == {"2026-01-01-demo"}


def test_get_archive_is_405_and_other_posts_are_405(server, tmp_path):
    """AC-19. /api/archive is the one path that takes anything but a GET, and it
    takes only POST. Everything else the dashboard serves stays read-only, which
    is what lets it be pointed at a repository the owner is working in.

    Every path here, and paths the dashboard serves nothing at all on, because the
    edit this is written against is a second one smuggled into do_POST's guard —
    `... != "/api/archive" and self.path != "/api/wipe"`. Read off the source that
    looks like the guard it replaced. What it cannot do is refuse a POST to the
    path it added, and the body below is a logs-only id that needs no Temporal, so
    anything let through here writes the store rather than stopping at a 409.
    """
    assert status_of(server + "/api/archive") == 405

    body = {"id": "2026-01-01-demo", "archived": True}
    for path in ["/", "/api/runs", "/api/run", "/api/logs", "/api/log", "/api/diff",
                 "/api/wipe", "/api/archive/", "/api/archives", "/nothing/here"]:
        code, answer = post(server + path, body)
        assert code == 405, f"POST {path} answered {code} rather than refusing the method"
        assert "error" in answer, f"POST {path} was refused with no body"
        assert not (tmp_path / ".archived.json").exists(), \
            f"POST {path} reached the archive writer"


def test_mark_archived_rereads_before_writing(tmp_path):
    """The lost update, from the half no lock can reach: `lg rm` writes this same
    file from another process entirely. A writer that applied its change to a set
    it read a moment ago would put back the ids that arrived in between and lose
    the ones it never saw."""
    ui.mark_archived(tmp_path, ["one"], True)
    store = tmp_path / ".archived.json"
    store.write_text(json.dumps(["one", "written-by-lg-rm"]))

    ui.mark_archived(tmp_path, ["two"], True)
    assert json.loads(store.read_text()) == ["one", "two", "written-by-lg-rm"], \
        "the second write was applied to a stale copy, or the list is not sorted"


def test_two_archive_writers_do_not_lose_a_flag(tmp_path, monkeypatch):
    """make_server returns a ThreadingHTTPServer, so two archive clicks landing
    together run mark_archived on two threads of one process. Unserialised, both
    read the same set, both write, the later rename wins and one run stays on the
    rail with its toggle showing archived.

    The window is microseconds wide in real use, so it is held open here: the
    first writer is stopped holding what it has just read until the second has had
    time to reach the same place. With the lock the second cannot get there and
    blocks; without it, it reads a set with nothing in it and writes one flag over
    the other. Held after the read and not before it — stopped before it, the
    first writer would go on to read what the second had already written and both
    flags would survive with no lock at all.
    """
    read = ui._read_archive
    entered, go, calls = threading.Event(), threading.Event(), []

    def held(path):
        calls.append(path)
        first = len(calls) == 1
        got = read(path)
        if first:
            entered.set()
            go.wait(5)
        return got

    monkeypatch.setattr(ui, "_read_archive", held)
    first = threading.Thread(target=ui.mark_archived, args=(tmp_path, ["one"], True))
    first.start()
    assert entered.wait(5), "the first writer never reached the read"

    second = threading.Thread(target=ui.mark_archived, args=(tmp_path, ["two"], True))
    second.start()
    time.sleep(0.2)  # long enough for an unserialised second writer to read and write
    go.set()
    first.join(5)
    second.join(5)

    assert ui.read_archived(tmp_path) == {"one", "two"}, \
        "two writers landing together lost a flag between them"


def test_the_store_is_written_by_rename(tmp_path):
    """AC-16's durability, pinned on the source: the failure it prevents is a
    crash or a power loss between two writes, and nothing here can stage one.

    Four things, in order and all inside the lock. The read, because the set on
    disk is not the set this process last saw. A temporary file with a name of its
    own, in the runs directory — the same directory so the rename is atomic on one
    filesystem, and unique because two writers interleaving on one fixed `.tmp`
    path can rename each other's half-written bytes, which is the exact tear the
    rename is here to prevent. The fsync, because a rename over a file whose bytes
    are still in the page cache is a file that comes back empty after a power cut.
    And os.replace, which is what makes a reader see the list whole, as it was
    before or as it is after.
    """
    body = inspect.getsource(ui.mark_archived).split('"""')[2]
    code = [ln for ln in body.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]
    order = ["with _ARCHIVE_LOCK:", "_read_archive(", "mkstemp(", "os.fsync(",
             "os.replace("]
    at = {}
    for i, line in enumerate(code):
        for needle in order:
            if needle in line and needle not in at:
                at[needle] = i
    assert set(at) == set(order), \
        f"mark_archived does not do all of {sorted(set(order) - set(at))}"
    assert [at[n] for n in order] == sorted(at[n] for n in order), \
        "the read, the temporary file, the fsync and the rename are out of order"

    def indent(line):
        return len(line) - len(line.lstrip())

    for needle in order[1:]:
        assert indent(code[at[needle]]) > indent(code[at[order[0]]]), \
            f"`{needle}` runs outside the module lock, so two threads can interleave there"

    joined = "\n".join(code)
    assert "dir=runs_dir" in joined, \
        "the temporary file is not made in the runs directory, so the rename crosses filesystems"
    assert not re.search(r"path\.(open|write_text|write_bytes)\(|open\(path", joined), \
        "the store is written in place, so a reader can see half a list"


@pytest.mark.parametrize("junk", [b"\x00\x01 not json {[", b'{"id": true}',
                                  b'["ok", 7]', b"", b"[1, 2, 3]"],
                         ids=["garbage", "an object", "a list with a number",
                              "empty", "numbers"])
def test_a_corrupt_archive_file_reads_as_empty(server, tmp_path, junk):
    """Reads forgive. /api/runs calls this on every poll, so a file somebody
    hand-edited into something that does not parse must cost those rows their
    flag — not turn every poll into a 500 and blank the rail while the owner works
    out what happened."""
    (tmp_path / ".archived.json").write_bytes(junk)
    assert ui.read_archived(tmp_path) == set()

    r = get(server + "/api/runs")
    assert r.status == 200
    assert [row["archived"] for row in json.loads(r.read())["runs"]] == [False]


def test_a_corrupt_archive_file_is_never_overwritten(archiving):
    """And writes refuse. This is the pair to the test above and the reason
    mark_archived does not read through read_archived: chained that way, a
    truncated or half-edited file reads as an empty set and the next archive click
    rewrites it as a one-element list, losing every flag the owner had — the exact
    loss AC-16 promises cannot happen.

    Both directions refuse, because it is the write that refuses and not the
    openness gate: the file is the owner's to rescue by hand either way.
    """
    broken = b'["run-a", "run-b"] and then the disk filled up'
    archiving.store.write_bytes(broken)
    url = archiving.make(dead_run_feed({KNOWN: False}))

    assert post(url + "/api/archive", {"id": KNOWN, "archived": True}) == \
        (409, {"error": UNREADABLE})
    assert archiving.store.read_bytes() == broken, "the flags in it are gone for good"

    assert post(url + "/api/archive", {"id": "run-a", "archived": False}) == \
        (409, {"error": UNREADABLE})
    assert archiving.store.read_bytes() == broken
    assert list(archiving.runs.iterdir()) == [archiving.store], \
        "a refused write left a temporary file behind"


def test_workflow_open_reports_three_states(monkeypatch):
    """The feed's own answer to AC-18's question, over a stub client. Three
    states, not two: `False` is Temporal saying the workflow is closed and `None`
    is Temporal not saying anything, and the server turns one into an archive and
    the other into a refusal.

    Unknown status counts as open, the way _still_running already falls it for
    `lg` and for the ledger fallback. The two mistakes are not equal — reading a
    live run as closed hides a run that is still writing to the page.
    """
    running = StubHandle(LEDGER, HANGS, status=WorkflowExecutionStatus.RUNNING)
    quiet = StubHandle(LEDGER, HANGS, status=None)
    done = StubHandle(LEDGER, LEDGER, status=WorkflowExecutionStatus.COMPLETED)
    feed = stub_feed(monkeypatch, {"wf-running": running, "wf-no-status": quiet,
                                   "wf-done": done})
    assert feed.workflow_open("wf-running") is True
    assert feed.workflow_open("wf-no-status") is True, \
        "a workflow Temporal reports with no status was read as finished"
    assert feed.workflow_open("wf-done") is False
    assert feed.workflow_open("wf-never-existed") is None, \
        "an id Temporal has never heard of came back as an answer"

    down = stub_feed(monkeypatch, {"wf-done": done}, connect_fails=True)
    assert down.connected is False
    assert down.workflow_open("wf-done") is None, \
        "a feed with no client answered for a workflow it cannot have asked about"


# ---------- archiving: the rail, the toggle and the strip's control ----------

# The page half of AC-17. Nothing here executes JavaScript, so what these hold is
# the source: which class the stylesheet hides on, which flag the stamp is written
# from, which word the button offers and which id it posts. The hiding itself, the
# toggle and one refusal on screen were watched in a browser while this was
# written, and that is checklist item 15 — the only place any of it can be seen.


def archive_closure(html):
    """The click handler behind the strip's archive button.

    Found by the path it posts to rather than by the name above it: regions()
    charges a closure to whichever `function NAME(` sits over it, and this one's
    name is buildBoard's. That it is a closure at all — a gesture, reachable from
    no interval — is the point, and test_the_archive_post_is_gesture_only is where
    that is asserted rather than assumed.
    """
    handlers = [c for c in closures(script_of(html)) if "/api/archive" in c]
    assert len(handlers) == 1, \
        f"expected one closure that posts to /api/archive, found {len(handlers)}"
    return handlers[0]


def test_the_load_selection_skips_an_archived_first_row():
    """AC-13 and AC-17 meeting on one line. With no hash the page opens the first
    row the reader can SEE, and row zero is a `display:none` row whenever the newest
    finished run has been archived — the ordinary case, and what `lg rm` leaves
    behind.

    Two ways to get this wrong, and the obvious one is not the dangerous one.
    Skipping every archived row unconditionally is right while the toggle is off
    and matches nothing at all when it is on and every run is archived: no row is
    clicked, `sel` stays null, and the board reads `select a run` for ever under a
    rail full of runs, with no error anywhere to say why. Inverting the toggle read
    is the other, and it opens the page on a hidden run every time, `.sel` on
    something nobody can see — which is what the skip exists to prevent.

    So the fallback asks the stylesheet's own question off the stylesheet's own
    class, and this pins which way round it is. The whole `A || B` shape is
    test_the_load_selection_prefers_the_hash_and_keeps_the_guard's; this is the
    half that arrived with the toggle.
    """
    src = function_source(ui.page_html(), "runs")
    showing = re.search(r"const (\w+) = document\.getElementById\('runs'\)"
                        r"\.classList\.contains\('show-archived'\);", src)
    assert showing, \
        ("the poll never asks whether the toggle is showing archived rows, asks it inverted, "
         "or asks the checkbox instead of the class the stylesheet actually hides on")
    rail = re.search(r"const (\w+) = \[\.\.\.document\.getElementById\('runs'\)\.children\];", src)
    assert rail, "the rows are not taken off the rail"
    # The operator as much as the two halves. `&&` reads "showing AND not archived",
    # which selects nothing whenever the toggle is off — every load, on every
    # dashboard — and both halves are still there for a substring to find.
    assert re.search(rf"\|\| {rail.group(1)}\.find\((\w+) => {showing.group(1)} "
                     rf"\|\| \1\.dataset\.archived !== '1'\);", " ".join(src.split())), \
        "the fallback is not `the toggle is showing them, OR this row is not archived`"


def test_a_row_is_stamped_and_worded_archived():
    """AC-17's mark, in the two forms the page needs it in: the attribute the
    stylesheet hides on, and the word the reader sees on the row once the toggle is
    showing them. `archived (3)` in the header with three unmarked rows underneath
    it would be the page hiding something and not saying which.

    Both off the reply's own flag and neither worked out twice. The stamp is read
    back by three different readers — the stylesheet, the load-time selection and
    the strip's button — so a row that carries the word and not the stamp is a run
    that reads archived and never leaves the rail.
    """
    html = ui.page_html()
    build = function_source(html, "buildRunRow")
    meta = re.search(r'<div class="meta">(.*?)</div>', build, re.S)
    assert meta, "the row's meta line is no longer a div of its own"
    assert re.findall(r'<span(?: class="(\w*)")?></span>', meta.group(1)) \
        == ["pill", "", "arch"], \
        "the row's meta line is not the pill, the detail and an empty archived slot"

    patch = function_source(html, "patchRunRow")
    entry = re.match(r"function patchRunRow\((\w+), (\w+)\)", patch)
    assert entry, "patchRunRow is not the (row, entry) pair the rest of this reads"
    row, served = entry.groups()
    parts = re.search(rf"const \[\w+, (\w+), \w+\] = {row}\.children;", patch)
    assert parts, "the row's three lines are not read out as its children"
    word = re.search(rf"const \[\w+, \w+, (\w+)\] = {parts.group(1)}\.children;", patch)
    assert word, "the archived word is not read out as the third child of the meta line"

    # The stamp's value and the selector's, tied together: they are one agreement
    # written in two languages, and `data-archived="yes"` in the stylesheet against
    # `'1'` here is a rule that matches nothing, with every other check green and
    # every archived run still on the rail.
    hide = [sel for sel, _ in css_rules(html) if ".show-archived" in sel]
    assert len(hide) == 1, "the stylesheet does not hide archived rows in exactly one rule"
    stamped = re.search(r"\[data-archived=\"(\w+)\"\]", hide[0])
    assert stamped, f"the hiding rule is not keyed on a data-archived value: `{hide[0]}`"
    assert f"{row}.dataset.archived = {served}.archived ? '{stamped.group(1)}' : '';" in patch, \
        "the row is not stamped with the value the stylesheet hides on, or not off the reply's flag"
    assert f"setText({word.group(1)}, {served}.archived ? 'archived' : '');" in patch, \
        "the row says nothing when it is archived, says it the wrong way round, or writes it " \
        "straight into the node instead of through setText"


def test_archived_rows_hide_behind_the_toggle():
    """AC-17. An archived run is off the rail; the header's toggle brings the whole
    set back and takes it away again.

    Hiding is one CSS rule off the stamp, and that is the whole of it. Not `hidden`
    and not a row removed: the reply goes on carrying every run, so the toggle needs
    no second request and the poll goes on patching a hidden row where it stands —
    a row removed for being archived would be built again four seconds later, which
    is the run-list bug this page was rewritten to kill.

    So the handler is one line and this pins the whole of it. A fetch in there is a
    request nobody meant to send; an `add` where a `toggle` belongs is a toggle that
    only goes one way; `!checked` is the page showing archived runs when the box is
    clear and hiding them when it is ticked, with every other check in this file
    green and nothing on screen to say which way round it is meant to be.
    """
    html = ui.page_html()
    hide = [(sel, body) for sel, body in css_rules(html) if ".show-archived" in sel]
    assert len(hide) == 1, "the stylesheet does not hide archived rows in exactly one rule"
    sel, body = hide[0]
    assert " ".join(sel.split()) == '#runs:not(.show-archived) .run[data-archived="1"]', \
        f"the hiding rule is not `an archived row on a rail that is not showing them`: `{sel}`"
    assert "display:none" in body.replace(" ", ""), \
        "the rule leaves the row in the layout, so an archived run keeps its space on the rail"

    label = re.search(r'<label id="showarch">(.*?)</label>', html, re.S)
    assert label, "the header carries no archived toggle"
    assert '<input type="checkbox">' in label.group(1), "the toggle is not a checkbox"
    assert '<span id="archn">' in label.group(1), \
        "the count is not a span of the toggle's own, so runs() has nothing to write it into"

    src = script_of(html)
    box = re.search(r"const (\w+) = document\.getElementById\('showarch'\)\.firstElementChild;",
                    src)
    assert box, "nothing on the page reaches the toggle's checkbox"
    assert re.search(rf"{box.group(1)}\.onchange = \(\) => \{{", src), \
        "the checkbox's change is not what flips the rail"
    handler = [c for c in closures(src) if "show-archived" in c]
    assert len(handler) == 1, \
        f"expected one closure that touches the rail's class, found {len(handler)}"
    assert " ".join(handler[0].split()) == \
        ("{ document.getElementById('runs').classList.toggle('show-archived', "
         f"{box.group(1)}.checked); }}"), \
        f"the change handler does more or less than toggle the class: `{handler[0]}`"


def test_the_toggle_counts_from_the_reply():
    """AC-17's `archived (N)`. Counted off the reply, never off the rail.

    The rail is what the reader is looking at, and it answers the wrong question
    both ways round: with the toggle off the archived rows are hidden, and with it
    on they are indistinguishable from the rest without reading a stamp back out of
    the DOM. The reply carries every run and its flag either way, so the number is
    the same whichever way the toggle is set — which is what makes it a label worth
    reading before you flip it.

    Written through setText like every other word a poll puts on the page, and off
    the same rows patchRuns was handed, so the count and the rail are never one
    poll apart.
    """
    src = function_source(ui.page_html(), "runs")
    served = re.search(r"patchRuns\((\w+)\)", src)
    assert served, "the poll no longer hands the reply's rows to patchRuns"
    count = re.search(r"const (\w+) = (\w+)\.filter\((\w+) => \3\.archived\)\.length;", src)
    assert count, "nothing counts the archived rows off the reply's own flags"
    held, rows, _ = count.groups()
    assert rows == served.group(1), \
        "the count comes from something other than the reply the rows came from"
    assert f"setText(document.getElementById('archn'), `archived (${{{held}}})`);" in src, \
        "the toggle's label is not `archived (N)` off that count, written through setText"
    # Below the waiting count, which is the other `.filter(…).length` in this
    # function and the one test_archived_rows_do_not_count_toward_the_title finds
    # with a re.search that takes the first.
    waiting = re.search(r"const \w+ = \w+\.filter\(\w+ => \w+\.state === 'waiting'", src)
    assert waiting and waiting.start() < count.start(), \
        "the archived count sits above the waiting count, where the tab's own test reads it"


def test_the_strip_carries_the_archive_control():
    """AC-17's gesture. The button lives on the strip, which is the one line on the
    board that names the run it would act on, and it says which direction it goes
    in.

    The word comes off the same stamp the stylesheet hides on, so the button and
    the rail cannot come to disagree about one run: a button reading `archive` on a
    run that is already off the rail is a click that does nothing and looks broken.

    Shown as soon as there is a row behind the strip, open run or not. AC-18 puts
    the refusal on the server, where it is true whatever the page believes — the
    page greying the button out would be it guessing, and guessing wrong costs the
    owner a click they were entitled to. The refusal they get instead is
    test_a_refusal_reaches_the_error_span's.
    """
    html = ui.page_html()
    w = strip_wiring(html)
    src = w["src"]
    btn = re.search(r"const (\w+) = " + w["strip"] + r"\.querySelector\('\.archbtn'\);", src)
    assert btn, "patchStrip never reaches the strip's archive button"
    assert f"{btn.group(1)}.hidden = false;" in src, \
        "the button is labelled and never shown, so it stays hidden for the life of the board"
    assert src.index(f"if (!{w['row']}) return;") < src.index(f"{btn.group(1)}.hidden = false"), \
        "the button is shown above the guard, on a strip with no row behind it"
    assert f"setText({btn.group(1)}, {w['row']}.dataset.archived === '1' " \
           "? 'unarchive' : 'archive');" in src, \
        "the button's word is not the row's own stamp read back, or it is the wrong way round"

    # The direction is the word the reader was offered, and the id is the row's own.
    # `sel.dir` is the other key on that object and two workflows of one run
    # directory share it: posted instead of the id it archives a row that may not
    # exist and leaves the one they clicked on the rail.
    handler = archive_closure(html)
    want = re.search(rf"const (\w+) = (\w+)\.textContent === 'archive';", handler)
    assert want, "the direction is not read off the word on the button"
    assert "JSON.stringify({id: sel.id, archived: " + want.group(1) + "})" in handler, \
        "the request does not carry the selected run's id and the direction the button offered"


def test_the_archive_post_is_gesture_only():
    """AC-19 from the page's end, and checklist item 8's guarantee: a dashboard
    nobody is touching sends four GETs and nothing else.

    /api/archive is named in one closure and in no function an interval can reach.
    Written into patchStrip it would post on every 4-second poll — the page
    archiving runs by itself, which is the worst thing in this phase that could
    reach a reader — and every naming rule in this file would wave it through,
    because patchStrip is a `patch…` function doing exactly what its name says.

    The count of non-GET requests is pinned at one for the same reason: this is the
    only write the dashboard makes, and a second one added later would be a second
    thing to think about wherever this endpoint is reasoned about.
    """
    src, regs = regions(ui.page_html())
    reached = poll_path(src, regs)
    hits = [m.start() for m in re.finditer("/api/archive", src)]
    assert hits, "the page never posts to /api/archive at all"
    for i in hits:
        owner = next((n for n, s, e in regs if s <= i < e), None)
        assert owner not in reached, \
            f"/api/archive is written into {owner}, which an interval reaches"

    handler = archive_closure(ui.page_html())
    assert "method: 'POST'" in handler, "the archive request is not a POST"
    assert src.count("method:") == 1, \
        "the page makes a request with a method of its own somewhere else as well"
    assert re.search(r"(\w+)\.onclick = async \(\) => \{", function_source(ui.page_html(),
                                                                          "buildBoard")), \
        "the archive request is not hung on a click at all"
    assert "onclick()" not in src, "something calls a click handler rather than waiting for one"


def test_a_refusal_reaches_the_error_span(archiving):
    """AC-18's sentence, on screen. The server refuses an open run with a line
    written to be read by whoever clicked, and the page shows that line rather than
    a status number, a console message or nothing at all.

    A control that fails silently is worse than no control: the reader clicks,
    the run stays where it is, and the page they are looking at says the same thing
    it said before. So every way the request can fail writes something — the
    server's own words when it answered, and the page's `server error` when it did
    not.

    The last half is the cross-check the source alone cannot make. The page reads
    one key out of the refusal's body; this asks a real dashboard for a real
    refusal and looks for that key in it, so the two halves of this feature cannot
    drift apart with both files' own tests green.
    """
    html = ui.page_html()
    handler = archive_closure(html)
    reply = re.search(r"(\w+) = await fetch\('/api/archive'", handler)
    assert reply, "the reply is not held anywhere, so nothing can read a status off it"
    assert f"if (!{reply.group(1)}.ok)" in handler, \
        "nothing branches on the status, so a refusal reads as a success and the rail repolls"

    body = re.search(r"const (\w+) = await \w+\.json\(\)", handler)
    assert body, "the refusal's body is never read"
    shown = re.search(rf"setText\((\w+), {body.group(1)}\.(\w+) \|\| ([^;]+)\);", handler)
    assert shown, "the body's own words are not written into anything, or not through setText"
    # What is written when the body carries no words of its own — a proxy in the
    # way, a 500 that never reached the handler. Read past the `||`, because a
    # pattern that stops at it is happy with `d.error || ''`: the middle of this
    # closure's three failure paths then writes an empty line, and the reader gets
    # a click that does nothing, which is the whole thing this test says it is
    # about. A literal with something in it, whatever the wording.
    assert re.fullmatch(r"""[`'"][^`'"]*\w[^`'"]*[`'"]""", shown.group(3).strip()), \
        (f"the fallback for a refusal with no `{shown.group(2)}` in it is not a line with words "
         f"in it: `{shown.group(3).strip()}`")
    assert re.search(rf"\b{shown.group(1)} = \w+\.querySelector\('\.archerr'\);?", script_of(html)), \
        f"`{shown.group(1)}` is not the strip's error line"
    # The catch's own block, counted out rather than searched for past the word
    # `catch`: everything below it in this closure writes the error line too, so a
    # substring is satisfied by a catch that has been emptied — which is the page
    # saying nothing at all on the one failure that leaves no status to read.
    caught = [h for _, h in try_blocks(handler) if "fetch('/api/archive'" in _]
    assert len(caught) == 1, "the archive request sits inside no try of its own"
    assert re.search(rf"setText\({shown.group(1)}, '[^']+'\);", caught[0]), \
        "a request that never reached the server leaves the reader with nothing on screen"
    assert f"setText({shown.group(1)}, '');" in handler, \
        "a refusal's words stay on the strip after the next click succeeds"

    key = shown.group(2)
    url = archiving.make(dead_run_feed({KNOWN: True}))
    code, refusal = post(url + "/api/archive", {"id": KNOWN, "archived": True})
    assert code == 409, "the server no longer refuses an open run, so there is nothing to show"
    assert refusal.get(key), \
        f"the page shows `{key}` and the server's refusal carries {list(refusal)}"
    assert refusal[key] == STILL_OPEN, "the refusal is not the sentence AC-18 asks for"


def test_agents_md_no_longer_calls_the_dashboard_read_only():
    """AC-32. This commit is what makes the old wording false, so it is the commit
    that corrects it: the next agent to read that line is about to edit ui.py, and
    a rule that is no longer true about the file being edited is worse than none.

    The rest of the entry is still there. The `PAGE` obligation is the one thing
    in that list that has caught defects the suite cannot see.
    """
    agents = (Path(ui.__file__).resolve().parent / "AGENTS.md").read_text()
    entry = re.search(r"^- `ui\.py` —.*?(?=\n- |\n\n)", agents, re.S | re.M)
    assert entry, "AGENTS.md's layout list no longer has a `ui.py` entry"
    line = " ".join(entry.group(0).split())

    assert "read-only" not in line, f"AGENTS.md still calls ui.py read-only: {line}"
    assert "POST /api/archive" in line, "the one write the dashboard makes is not named"
    assert "8400" in line, "the port went with the rewrite"
    assert "`PAGE`" in line and "browser checklist" in line, \
        "the obligation to run the checklist by hand went with the rewrite"


def handler_method(src, name):
    """One `do_…` method of the handler, out of ui.py's text.

    The handler is a class inside make_server's closure, so there is no object to
    hand inspect.getsource and the method is found by its `def` line and ends
    where the indentation comes back.
    """
    lines = src.splitlines()
    start = next(i for i, ln in enumerate(lines) if re.match(rf"\s*def {name}\(", ln))
    depth = len(lines[start]) - len(lines[start].lstrip())
    out = [lines[start]]
    for ln in lines[start + 1:]:
        if ln.strip() and len(ln) - len(ln.lstrip()) <= depth:
            break
        out.append(ln)
    return "\n".join(out)


def test_the_dashboard_has_no_write_methods():
    """AC-27, read off the source: the handler's methods, and every git invocation
    naming its subcommand right next to the program, so this can see them all.

    Narrowed, not dropped. The dashboard spends its read-only guarantee exactly
    once, on `POST /api/archive`, and everything this test used to forbid it still
    forbids — a second write path, a PUT or a DELETE, a git command that writes.

    What it reads about `do_POST` is one property: the only URL path it names is
    `/api/archive`. A second write path cannot be added without naming itself, so
    both smuggles fail here —
    `... != "/api/archive" and self.path != "/api/wipe"` and
    `self.path not in ("/api/archive", "/api/wipe")`.

    It reads nothing about the shape. Binding the path to a local first, or
    inverting to `if path == "/api/archive": … else: 405`, are ordinary ways to
    write that guard and stay green: an earlier version of this test pinned the
    operator and the statement's position, which failed both of them and left a
    contributor with a red test about code that was fine. The guarantee is held by
    test_get_archive_is_405_and_other_posts_are_405, which POSTs to nine paths it
    must refuse and to one the dashboard serves nothing on at all. That is what
    caught a smuggle moved ahead of the guard, where nothing about the condition
    had changed and this test saw nothing. A false red here is loud and gets
    fixed; a false green there ships the hole. So the behaviour is the guard and
    this is the tripwire beside it.

    Both files, because the dashboard process runs git in two of them: the diff
    pane here, and the version checker's reads through version.py. A guard that
    read only this file would leave the checker's git unwatched, and the checker
    is the half with a remote in it. `lg rm`'s git commands run from `lg`, which
    this does not scan, and they stay there.
    """
    src = Path(ui.__file__).read_text()
    assert re.findall(r"def (do_\w+)\(", src) == ["do_GET", "do_POST"], \
        "the dashboard answers a method other than GET and POST"

    # Parsed, not searched. A path named in a comment is not a second endpoint,
    # two adjacent literals are the one string they compile to, and a docstring is
    # one constant rather than the words in it.
    method = handler_method(src, "do_POST").splitlines()
    pad = len(method[0]) - len(method[0].lstrip())
    tree = ast.parse("\n".join(ln[pad:] if len(ln) > pad else ln.lstrip() for ln in method))
    constants = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)]
    named = [c for c in constants if isinstance(c, str) and c.startswith("/")]
    assert named == ["/api/archive"], \
        f"do_POST names {named} — a URL path beside /api/archive is a second write path"
    assert 405 in constants, "do_POST refuses no method with a 405"

    for path, expected in [(Path(ui.__file__), {"rev-parse", "diff"}),
                           (Path(ui.version.__file__),
                            {"describe", "rev-parse", "rev-list", "ls-remote"})]:
        text = path.read_text()
        subcommands = re.findall(r'"git",\s*"([a-z][a-z-]*)"', text)
        assert set(subcommands) == expected, f"{path.name} runs a git command nobody expected"
        assert text.count('"git"') == len(subcommands), \
            f"{path.name} has a git invocation whose subcommand is not a literal beside it"
        assert not {"push", "commit", "checkout", "merge", "reset"} & set(subcommands), \
            f"{path.name} writes with git from a process that only reads"
