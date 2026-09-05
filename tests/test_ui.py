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


# ---------- the ledger, per workflow id ----------

LEDGER = {"status": "merge-ready", "rounds": [{"verdict": "PASS"}]}
KNOWN = "run-2026-01-01-demo-aaaaaa"
START = datetime(2026, 9, 5, 15, 45, 29, tzinfo=timezone.utc)
CLOSE = datetime(2026, 9, 5, 16, 0, 0, tzinfo=timezone.utc)


class FakeFeed:
    """The three members the handler reads, over canned values.

    `make_server(feed=...)` takes one of these in place of a real TemporalFeed, so
    the endpoints can be asserted over HTTP with no engine running. It cannot stand
    in for the feed's own tests further down: it replaces `ledger`, which is the
    method those exist to exercise.
    """

    def __init__(self, connected=True, rows=(), ledgers=None):
        self.connected = connected
        self.rows = [dict(r) for r in rows]
        self.ledgers = dict(ledgers or {})

    def runs(self):
        return [dict(r) for r in self.rows]

    def ledger(self, wf_id):
        return self.ledgers.get(wf_id)


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
    """One workflow's handle. Each call answers with its canned value, or raises it."""

    def __init__(self, query, result, status=WorkflowExecutionStatus.COMPLETED):
        self._query, self._result, self._status = query, result, status
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
        return SimpleNamespace(status=self._status)


def stub_feed(monkeypatch, handles, rows=()):
    """A real TemporalFeed — its own loop, its own thread, its real call() — over a
    stub client, because the fallback and the two entry points are the feed's own
    code and FakeFeed replaces exactly that."""

    class Client:
        @staticmethod
        async def connect(address):
            return Client()

        def get_workflow_handle(self, wf_id):
            return handles.get(wf_id) or StubHandle(KeyError(wf_id), KeyError(wf_id))

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
# The NOT list is Task 16's browser checklist, not a list of things that do not
# matter. What each test cannot see is written in its own docstring.

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

    Two hops out it is invisible again, and Task 16's checklist carries that.
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
    for section in ("state", "why", "awaiting", "items", "rounds", "diff"):
        assert f'id="{section}"' in build, f"#{section} is not built with the board"


def test_the_round_cards_leave_the_board_for_their_own_section():
    """The board is six sections now, and the log cards are one of them. Left
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
    ones Tasks 15 and 16 add included. It has to carry !important, because the
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


def test_the_dashboard_has_no_write_methods():
    """AC-38, read off the source: one handler method, and every git invocation
    naming its subcommand right next to the program, so this can see them all."""
    src = Path(ui.__file__).read_text()
    assert re.findall(r"def (do_\w+)\(", src) == ["do_GET"]
    subcommands = re.findall(r'"git",\s*"([a-z][a-z-]*)"', src)
    assert set(subcommands) == {"rev-parse", "diff"}
    assert src.count('"git"') == len(subcommands), \
        "a git invocation whose subcommand is not a literal beside it"
