import threading
import urllib.request

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


def get(url):
    return urllib.request.urlopen(url, timeout=5)


def test_page_serves(server):
    body = get(server + "/").read().decode()
    assert "loopgraph" in body and "/api/runs" in body


def test_logs_endpoint(server):
    import json
    import re

    from activities.stream import LOG_RE, log_name
    d = json.loads(get(server + "/api/logs?dir=2026-01-01-demo").read())
    name = log_name(1, 1, "executor")
    assert "hello" in d["logs"][name]
    assert re.match(LOG_RE, name), "the page would drop this file and render nothing"


def test_logs_reject_traversal(server):
    with pytest.raises(Exception):
        get(server + "/api/logs?dir=../..")


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
