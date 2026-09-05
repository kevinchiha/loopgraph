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
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from activities.stream import LOG_RE

ROOT = Path(__file__).resolve().parent
LOG_TAIL = 60_000  # bytes per log file served per poll

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
  const LOG_RE = new RegExp('__LOG_RE__');
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

def log_tails(runs_dir: Path, slug: str) -> dict[str, str]:
    logs = {}
    for f in sorted((runs_dir / slug / "logs").glob("*.log")):
        data = f.read_bytes()[-LOG_TAIL:]
        logs[f.name] = data.decode(errors="replace")
    return logs


def run_dirs(runs_dir: Path) -> list[str]:
    return sorted((d.name for d in runs_dir.iterdir() if (d / "logs").is_dir()),
                  key=lambda n: (runs_dir / n).stat().st_mtime, reverse=True)


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

    def call(self, coro, timeout=10):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    async def _runs(self):
        out = []
        async for wf in self._client.list_workflows('WorkflowType = "LoopGraphRun"'):
            entry = {"id": wf.id, "dir": wf.id[4:wf.id.rfind("-")] if wf.id.startswith("run-") else wf.id,
                     "state": wf.status.name.lower() if wf.status else "running", "detail": ""}
            try:
                ledger = await self._client.get_workflow_handle(wf.id).query("ledger")
            except Exception:
                # closed workflows: the ledger IS the workflow's return value
                # (query-by-replay on closed runs can fail decoding in this SDK)
                try:
                    ledger = await self._client.get_workflow_handle(wf.id).result()
                except Exception:
                    ledger = None
            if ledger:
                rounds = ledger.get("rounds", [])
                verdict = rounds[-1].get("verdict", "") if rounds else ""
                entry["state"] = ledger.get("status", entry["state"])
                entry["detail"] = f"r{len(rounds)} {verdict}".strip()
            out.append(entry)
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
    """The dashboard, with the log-name pattern injected from its one owner."""
    return PAGE.replace("__LOG_RE__", LOG_RE)


def make_server(port: int, runs_dir: Path, temporal_addr: str | None = "localhost:7233"):
    feed = TemporalFeed(temporal_addr) if temporal_addr else None

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
                if not slug or "/" in slug or ".." in slug:
                    return self._json({"logs": {}}, 400)
                self._json({"logs": log_tails(runs_dir, slug)})
            elif u.path == "/api/runs":
                wf = feed.runs() if feed else []
                known = {w["dir"] for w in wf}
                for d in run_dirs(runs_dir):
                    if d not in known:
                        wf.append({"id": d, "dir": d, "state": "unknown", "detail": "logs only"})
                self._json({"runs": wf, "temporal": bool(feed and feed._client)})
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
