import inspect
import json
import re
import threading
import urllib.error
import urllib.request
from urllib.parse import urlencode

import pytest

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


def get(url):
    return urllib.request.urlopen(url, timeout=5)


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
    import json
    d = json.loads(get(server + "/api/runs").read())
    assert d["temporal"] is False
    assert d["runs"][0]["dir"] == "2026-01-01-demo"
    assert d["runs"][0]["state"] == "unknown"


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
