import asyncio
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
    # errors="replace" turns the one or two bytes of a character the cut landed
    # inside into a single U+FFFD, which re-encodes to three, so the decoded patch
    # can measure two bytes over the cut. Two, not two hundred thousand.
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
    catches it and the browser's fetch never fills."""
    assert ui.DIFF_TIMEOUT == 20, "the line the owner reads says 20s"
    slow = tmp_path / "slow-textconv"
    slow.write_text("#!/bin/sh\nsleep 5\n")
    slow.chmod(0o755)
    git(diffing.repo, "config", "diff.slow.textconv", str(slow))
    (diffing.repo / ".gitattributes").write_text("feature.txt diff=slow\n")
    monkeypatch.setattr(ui, "DIFF_TIMEOUT", 1)

    t0 = time.monotonic()
    d = getjson(f"{diffing.url}/api/diff?id={DIFF_ID}")
    assert time.monotonic() - t0 < 4, "the deadline did not govern git itself"
    assert d == {"stat": "git took longer than 1s; nothing to show",
                 "patch": "", "truncated": False}


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
