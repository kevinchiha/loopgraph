import threading
import urllib.request

import pytest

import ui


@pytest.fixture
def server(tmp_path):
    run = tmp_path / "2026-01-01-demo"
    (run / "logs").mkdir(parents=True)
    (run / "logs" / "r1-executor.log").write_text("[10:00:00 assistant] hello\n[10:00:01 tool:Bash] true\n")
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
    d = json.loads(get(server + "/api/logs?dir=2026-01-01-demo").read())
    assert "hello" in d["logs"]["r1-executor.log"]


def test_logs_reject_traversal(server):
    with pytest.raises(Exception):
        get(server + "/api/logs?dir=../..")


def test_runs_endpoint_without_temporal(server):
    import json
    d = json.loads(get(server + "/api/runs").read())
    assert d["temporal"] is False
    assert d["runs"][0]["dir"] == "2026-01-01-demo"
    assert d["runs"][0]["state"] == "unknown"
