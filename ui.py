#!/usr/bin/env python3
"""lg ui — tiny local dashboard for watching loopgraph runs live.

Stdlib HTTP server + the venv's temporalio. Left: runs (from Temporal, plus any
run dir that has logs). Right: the selected run's stream logs, polled and
auto-scrolled. If Temporal is down the page still tails log files.

Run: lg ui [--port 8400]  →  http://localhost:8400
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from activities.stream import LOG_GLOB, LOG_RE

ROOT = Path(__file__).resolve().parent

# The line append_log puts at the top of a log it has cut down to LOG_CAP // 2.
# A copy, because this phase does not touch activities/stream.py, so
# test_the_truncation_marker_matches_the_writer pins it against the writer.
HEAD_MARK = b"[... head truncated ...]\n"

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>loopgraph</title><style>
  :root { --bg:#0b0e14; --pane:#11151d; --panel:#151a24; --line:#232a36; --fg:#d6dbe2;
          --dim:#7d8590; --accent:#58a6ff; --purple:#bc8cff; }
  * { box-sizing:border-box; margin:0; }
  body { background:var(--bg); color:var(--fg); height:100vh; display:flex; flex-direction:column;
         font:14px/1.5 -apple-system,"Segoe UI",system-ui,sans-serif; }
  header { padding:12px 18px; border-bottom:1px solid var(--line); display:flex; gap:10px; align-items:baseline; }
  header b { font-size:15px; letter-spacing:.4px; }
  header span { color:var(--dim); font-size:12px; }
  main { flex:1; display:flex; min-height:0; }
  #runs { width:320px; flex:none; overflow-y:auto; background:var(--pane); border-right:1px solid var(--line); }
  .run { padding:11px 15px; border-bottom:1px solid var(--line); cursor:pointer; border-left:3px solid transparent; }
  .run:hover { background:#171d29; }
  .run.sel { background:#1a2231; border-left-color:var(--accent); }
  .run .dir { font:600 12px/1.3 ui-monospace,Menlo,monospace; word-break:break-all; }
  .run .meta { margin-top:5px; display:flex; gap:8px; align-items:center; font-size:11px; color:var(--dim); }
  .pill { padding:1px 9px; border-radius:20px; font-size:10.5px; font-weight:700; letter-spacing:.3px; text-transform:uppercase; }
  .green { background:rgba(63,185,80,.15); color:#3fb950; }
  .red { background:rgba(248,81,73,.15); color:#f85149; }
  .yellow { background:rgba(210,153,34,.15); color:#d29922; }
  .gray { background:rgba(125,133,144,.15); color:#8b949e; }
  #board { flex:1; overflow-y:auto; padding:16px 20px; min-width:0; }
  .empty { color:var(--dim); text-align:center; padding:40px 0; font-size:13px; }
  .round { margin-bottom:22px; }
  .round > h2 { font-size:11px; font-weight:700; letter-spacing:1.2px; color:var(--dim);
                text-transform:uppercase; margin-bottom:8px; }
  .panels { display:flex; gap:14px; align-items:stretch; }
  .panel { flex:1; min-width:0; background:var(--panel); border:1px solid var(--line); border-radius:10px;
           display:flex; flex-direction:column; overflow:hidden; }
  .panel > header { padding:8px 14px; border-bottom:1px solid var(--line); font:700 11px/1.4 ui-monospace,monospace;
                    letter-spacing:1px; text-transform:uppercase; }
  .panel.exec > header { color:var(--accent); } .panel.audit > header { color:var(--purple); }
  .panel .body { padding:10px 14px; height:44vh; overflow-y:auto;
                 font:12px/1.65 ui-monospace,SFMono-Regular,Menlo,monospace; white-space:pre-wrap; word-break:break-word; }
  .body .t { color:var(--dim); } .body .tool { color:var(--purple); }
  .body .res { color:var(--dim); } .body .asst { color:var(--fg); }
  @media (max-width:900px) { .panels { flex-direction:column; } #runs { width:240px; } }
</style></head><body>
<header><b>loopgraph</b><span id="hdr">engine dashboard</span></header>
<main><div id="runs"></div><div id="board"><div class="empty">select a run</div></div></main>
<script>
let sel = null;
const pill = s => ({green:'green',running:'yellow',stopped:'red',failed:'red','merge-ready':'green',
  merged:'green',held:'gray',discarded:'gray',unknown:'gray'}[s]||'gray');
const esc = t => t.replaceAll('&','&amp;').replaceAll('<','&lt;');
function colorize(t) {
  return esc(t).split('\\n').map(l => {
    if (/^\\[[0-9:]+ tool/.test(l)) return `<span class="tool">${l}</span>`;
    if (/^\\[[0-9:]+ result\\]/.test(l)) return `<span class="res">${l}</span>`;
    return l.replace(/^(\\[[0-9:]+) /, '<span class="t">$1</span> ');
  }).join('\\n');
}
async function runs() {
  try {
    const d = await (await fetch('/api/runs')).json();
    const el = document.getElementById('runs'); el.innerHTML = '';
    for (const r of d.runs) {
      const div = document.createElement('div');
      div.className = 'run' + (sel === r.dir ? ' sel' : '');
      div.innerHTML = `<div class="dir">${r.dir}</div><div class="meta">
        <span class="pill ${pill(r.state)}">${r.state}</span><span>${r.detail||''}</span></div>`;
      div.onclick = () => { sel = r.dir; runs(); poll(); };
      el.appendChild(div);
    }
    if (!sel && d.runs.length) { sel = d.runs[0].dir; poll(); }
    document.getElementById('hdr').textContent = d.temporal ? 'engine dashboard' : 'temporal unreachable — logs only';
  } catch(e) { document.getElementById('hdr').textContent = 'server error'; }
}
async function poll() {
  if (!sel) return;
  const d = await (await fetch('/api/logs?dir=' + encodeURIComponent(sel))).json();
  const LOG_RE = new RegExp(__LOG_RE__);
  const rounds = {};
  for (const [name, text] of Object.entries(d.logs)) {
    // Pattern comes from activities/stream.py so it cannot drift from the writers.
    const m = name.match(LOG_RE);
    if (!m) continue;
    const key = m[1] ? `item ${m[1]} · round ${m[2]}` : `round ${m[2]}`;
    (rounds[key] ||= {})[m[3]] = text;
  }
  const board = document.getElementById('board');
  const keys = Object.keys(rounds).sort().reverse();
  board.innerHTML = keys.length ? keys.map(n => `
    <div class="round"><h2>${n}</h2><div class="panels">
      ${rounds[n].executor !== undefined ? `<div class="panel exec"><header>executor</header>
        <div class="body" data-stick>${colorize(rounds[n].executor)}</div></div>` : ''}
      ${rounds[n].audit !== undefined ? `<div class="panel audit"><header>supervisor</header>
        <div class="body" data-stick>${colorize(rounds[n].audit)}</div></div>` : ''}
    </div></div>`).join('') : '<div class="empty">no logs yet for this run</div>';
  document.querySelectorAll('[data-stick]').forEach(el => el.scrollTop = el.scrollHeight);
}
runs(); setInterval(runs, 4000); setInterval(poll, 2000);
</script></body></html>"""


# ---------- data providers ----------

def bad_param(value: str) -> bool:
    """A path segment that arrived over the wire and must stay one segment."""
    return not value or "/" in value or ".." in value


def log_names(runs_dir: Path, slug: str) -> list[str]:
    """The run's log file names, sorted, no content. Missing directory: []."""
    return sorted(f.name for f in (runs_dir / slug / "logs").glob(LOG_GLOB))


def log_slice(path: Path, offset: int) -> dict:
    """The bytes past `offset`, plus the two signals that say "start again".

    `size` is a count of bytes, and it is the number the caller sends back as its
    next `offset`. `text` is those bytes decoded, so its length is not an offset
    and must never be used as one: a log holding any non-ASCII character decodes
    to fewer characters than it measures in bytes, and a slice that starts
    mid-character comes out longer again, because errors="replace" turns each
    stray byte at the seam into a replacement character.

    `text` is exactly `size - offset` bytes, not "the rest of the file". A live
    log grows between the measurement and the read, and bytes past the `size` we
    reported would be sent again on the next poll, under the reader's eyes, as
    duplicate lines.

    `offset` comes back lower than it went in when the stored offset was past the
    end of the file. That misses the other half: append_log rewrites an over-cap
    file as HEAD_MARK plus its last 500 KB, so an offset under 500 KB still looks
    valid while every byte behind it has moved. `head_truncated` is read from the
    file's first bytes on every reply, so the page sees that flag go true and
    replaces its text instead of appending from the middle of a line.
    """
    with path.open("rb") as f:
        head = f.read(len(HEAD_MARK))
        size = f.seek(0, 2)
        if offset > size:
            offset = 0
        f.seek(offset)
        text = f.read(size - offset).decode(errors="replace")
    return {"text": text, "offset": offset, "size": size, "head_truncated": head == HEAD_MARK}


def run_dirs(runs_dir: Path) -> list[str]:
    return sorted((d.name for d in runs_dir.iterdir() if (d / "logs").is_dir()),
                  key=lambda n: (runs_dir / n).stat().st_mtime, reverse=True)


def run_entry(wf_id: str, status: str, start_time: datetime | None,
              close_time: datetime | None, ledger: dict | None) -> dict:
    """One row of /api/runs.

    `id` is the key the page selects on and the key /api/run and /api/diff take.
    `dir` is the run directory, which is not the same thing: two workflows of one
    directory write the same log files, so they are two rows sharing one `dir`.

    The times come straight off WorkflowExecution and go out as ISO 8601. A
    running workflow has no close time, and a row built from log files alone has
    neither, so the page shows no time rather than a wrong one.
    """
    entry = {"id": wf_id,
             "dir": wf_id[4:wf_id.rfind("-")] if wf_id.startswith("run-") else wf_id,
             "state": status,
             "detail": "",
             "start_time": start_time.isoformat() if start_time else None,
             "close_time": close_time.isoformat() if close_time else None}
    if ledger:
        rounds = ledger.get("rounds", [])
        verdict = rounds[-1].get("verdict", "") if rounds else ""
        entry["state"] = ledger.get("status", entry["state"])
        entry["detail"] = f"r{len(rounds)} {verdict}".strip()
    return entry


async def _still_running(handle) -> bool:
    """Whether waiting on this workflow's result would block.

    temporalio types the status optional and leaves it None when Temporal reports
    none, so unknown has to fall one way. It falls the way lg._still_running falls
    it — unknown counts as running — because the two mistakes are not equal:
    skipping the fallback costs one row its detail, and taking it on a run that is
    waiting for its owner holds the poll until call() gives up.
    """
    from temporalio.client import WorkflowExecutionStatus
    status = (await handle.describe()).status
    return status is None or status == WorkflowExecutionStatus.RUNNING


class TemporalFeed:
    """Persistent temporalio client on its own loop thread; sync callers use call()."""

    def __init__(self, address: str):
        self._addr, self._client, self._err = address, None, None
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._run, daemon=True).start()
        for _ in range(50):
            if self._client or self._err:
                break
            time.sleep(0.1)

    def _run(self):
        asyncio.set_event_loop(self._loop)
        try:
            from temporalio.client import Client
            self._client = self._loop.run_until_complete(Client.connect(self._addr))
        except Exception as e:  # temporal down: UI still serves logs
            self._err = str(e)
        self._loop.run_forever()

    @property
    def connected(self) -> bool:
        return bool(self._client)

    def call(self, coro, timeout=10):
        """Run a coroutine on the feed's loop and wait for it. Request threads only.

        run_coroutine_threadsafe has no same-thread guard. Called from inside the
        loop's own thread it blocks that loop, so the coroutine it just submitted
        can never start and the wait runs the full timeout out. Anything already on
        the loop — _runs is — awaits the coroutine instead.
        """
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    async def _ledger(self, wf_id: str) -> dict | None:
        """One workflow's ledger, on the feed's own loop. Unknown id: None.

        Both branches carry real traffic. A workflow answers the `ledger` query
        while the worker can replay its history; when it cannot, the ledger is the
        value the workflow returned instead.

        That second branch is not a decoding failure, whatever the comment that
        used to sit here said and its copy in lg._answer still says. Read that way
        it sends the next person searching the data converter for a bug that is not
        there. What happens is that a query makes the worker replay the whole
        history against the workflow code registered right now, so a history
        written by older code comes back as WorkflowQueryFailedError, [TMPRL1100]
        Nondeterminism error. It is fixed per workflow id — 7 of the 15 runs on
        this machine — and the share grows every time the engine changes. Neither
        branch may be dropped, and neither costs anything: a failing query answers
        in 0.01-0.03 s and a closed run's result in about none.

        result() on a workflow that has not finished is the exception. It waits for
        the workflow, and a run holding its owner's card does not finish, so
        _still_running is asked first.
        """
        handle = self._client.get_workflow_handle(wf_id)
        try:
            return await handle.query("ledger")
        except Exception:
            pass
        try:
            return None if await _still_running(handle) else await handle.result()
        except Exception:
            return None

    def ledger(self, wf_id: str) -> dict | None:
        """The same ledger, for an HTTP handler thread. Never raises into a poll."""
        if not self._client:
            return None
        try:
            return self.call(self._ledger(wf_id))
        except Exception:
            return None

    async def _runs(self):
        out = []
        async for wf in self._client.list_workflows('WorkflowType = "LoopGraphRun"'):
            out.append(run_entry(wf.id, wf.status.name.lower() if wf.status else "running",
                                 wf.start_time, wf.close_time, await self._ledger(wf.id)))
            if len(out) >= 25:
                break
        return out

    def runs(self) -> list[dict]:
        if not self._client:
            return []
        try:
            return self.call(self._runs())
        except Exception:
            return []


# ---------- server ----------

def page_html() -> str:
    """The dashboard, with the log-name pattern injected from its one owner.

    json.dumps writes the JS string literal, backslashes and all. Pasting the
    pattern between quotes instead looked right and was not: a JS string eats an
    unknown escape, so `\\d` reached RegExp as a bare `d`, the browser built
    /^(?:i(d+)-)?r(d+)-(executor|audit).log$/, every filename failed to match, and
    the log pane said "no logs yet for this run" for every run there has ever
    been."""
    return PAGE.replace("__LOG_RE__", json.dumps(LOG_RE))


def make_server(port: int, runs_dir: Path, temporal_addr: str | None = "localhost:7233",
                feed=None) -> ThreadingHTTPServer:
    """The dashboard's server. `feed` stands in for the Temporal connection.

    The handler reads only `connected`, `runs()` and `ledger()` off it, so a test
    can pass a fake and assert the endpoints over HTTP with no engine running.
    """
    if feed is None and temporal_addr:
        feed = TemporalFeed(temporal_addr)

    class H(BaseHTTPRequestHandler):
        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/":
                body = page_html().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/logs":
                slug = parse_qs(u.query).get("dir", [""])[0]
                if bad_param(slug):
                    return self._json({"error": "dir must be a run directory name"}, 400)
                self._json({"logs": log_names(runs_dir, slug)})
            elif u.path == "/api/log":
                q = parse_qs(u.query)
                slug, name = q.get("dir", [""])[0], q.get("name", [""])[0]
                if bad_param(slug) or bad_param(name):
                    return self._json({"error": "dir and name must be single names"}, 400)
                if not re.match(LOG_RE, name):
                    return self._json({"error": "name is not a log file name"}, 400)
                offset = q.get("offset", ["0"])[0]  # parse_qs drops &offset=, so 0
                if not re.fullmatch(r"[0-9]+", offset):
                    return self._json({"error": "offset must be a whole number"}, 400)
                path = runs_dir / slug / "logs" / name
                try:
                    found = path.is_file()
                except OSError:  # LOG_RE bounds neither digit run: a hand-made
                    found = False  # name can be longer than the filesystem allows
                if not found:
                    return self._json({"error": "no such log file"}, 404)
                self._json(log_slice(path, int(offset)))
            elif u.path == "/api/runs":
                wf = feed.runs() if feed else []
                known = {w["dir"] for w in wf}
                for d in run_dirs(runs_dir):
                    if d not in known:
                        # No workflow, so no id to key on but the directory itself,
                        # and no times: the page shows none rather than a wrong one.
                        wf.append({"id": d, "dir": d, "state": "unknown", "detail": "logs only",
                                   "start_time": None, "close_time": None})
                self._json({"runs": wf, "temporal": bool(feed and feed.connected)})
            elif u.path == "/api/run":
                wf_id = parse_qs(u.query).get("id", [""])[0]
                if bad_param(wf_id):
                    return self._json({"error": "id must be a workflow id"}, 400)
                # An id Temporal does not know is a null ledger with temporal true;
                # `temporal` says the feed is connected, nothing about the id.
                self._json({"ledger": feed.ledger(wf_id) if feed else None,
                            "temporal": bool(feed and feed.connected)})
            else:
                self.send_error(404)

        def log_message(self, *a):  # quiet
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), H)


def serve(port: int = 8400, temporal_addr: str | None = "localhost:7233") -> None:
    srv = make_server(port, ROOT / "runs", temporal_addr)
    print(f"loopgraph ui → http://localhost:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    import sys
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8400)
