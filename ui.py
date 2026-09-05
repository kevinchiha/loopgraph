#!/usr/bin/env python3
"""lg ui — tiny local dashboard for watching loopgraph runs live.

Stdlib HTTP server + the venv's temporalio. Left: runs (from Temporal, plus any
run dir that has logs). Right: the selected run's state, then its rounds — the
status, the question it is waiting on with the command that answers it, the work
items, and one card per round carrying what the supervisor said about it and the
two collapsed log panes that round wrote. An open pane asks for the bytes past
the ones it already has every 2 seconds and appends them; a collapsed one asks
for nothing. A round the executor is still working in has a growing log and no
ledger row yet, and gets a card that says so. Last comes the branch diff, one
pane for the whole run because every round shares one branch, fetched when the
reader opens it and never on a timer. If Temporal is down the page says so and
still tails log files.

Changing PAGE means running the browser checklist in tests/test_ui.py by hand.
That file reads this JavaScript as text and has never seen the page, so how the
page looks, and what a poll does to the text a reader has selected, are outside
every test in it: three defects have shipped past a green suite already.

Run: lg ui [--port 8400]  →  http://localhost:8400
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from activities.stream import LOG_GLOB, LOG_RE
from envfile import read_env

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
  /* The browser's own [hidden] rule is display:none at user-agent weight, which
     every rule below beats. #state is a flex box and the answer command is
     inline-block, so both set the attribute and stayed on screen — a run's last
     status still above the line saying there is no state. One rule here rather
     than a display:none per section, so a section added later is covered too. */
  [hidden] { display:none !important; }
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
  /* Tabular figures so a duration counting up does not shuffle the line sideways
     under a reader trying to select it. */
  .run .when { margin-top:4px; font-size:11px; color:var(--dim); font-variant-numeric:tabular-nums; }
  .pill { padding:1px 9px; border-radius:20px; font-size:10.5px; font-weight:700; letter-spacing:.3px; text-transform:uppercase; }
  .green { background:rgba(63,185,80,.15); color:#3fb950; }
  .red { background:rgba(248,81,73,.15); color:#f85149; }
  .yellow { background:rgba(210,153,34,.15); color:#d29922; }
  .gray { background:rgba(125,133,144,.15); color:#8b949e; }
  #board { flex:1; overflow-y:auto; padding:16px 20px; min-width:0; }
  .empty { color:var(--dim); text-align:center; padding:40px 0; font-size:13px; }
  /* The state board: what the run is doing, what it is asking, what it is
     working through. The logs come after all three. */
  #state { display:flex; gap:10px; align-items:baseline; flex-wrap:wrap; margin-bottom:14px; }
  #state .reason { color:var(--dim); font-size:12.5px; min-width:0; word-break:break-word; }
  #why { color:var(--dim); font-size:12.5px; margin-bottom:16px; }
  #awaiting { background:var(--panel); border:1px solid var(--line);
              border-left:3px solid var(--accent); border-radius:10px;
              padding:13px 16px; margin-bottom:20px; }
  #awaiting h2 { font:700 11px/1.4 ui-monospace,monospace; letter-spacing:1px;
                 text-transform:uppercase; color:var(--accent); margin-bottom:9px; }
  /* The question is the text of the card the owner saw, line breaks and all. */
  #awaiting .q { white-space:pre-wrap; word-break:break-word; margin-bottom:10px; }
  #awaiting .opt { font-size:13px; }
  #awaiting .answer { margin-top:11px; }
  /* Beside the command and never inside it: user-select:all covers the element
     it is set on, so a label in the box would be selected with the command and
     pasted into a shell. */
  #awaiting .lbl { color:var(--dim); font-size:12.5px; margin-right:8px; }
  /* One click selects the whole command, so it can be pasted into a terminal
     without picking the ends off it by hand. */
  #awaiting .cmd { display:inline-block; user-select:all; padding:4px 9px;
                   background:var(--bg); border:1px solid var(--line); border-radius:6px;
                   color:var(--purple); word-break:break-all;
                   font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; }
  #awaiting .nocard { margin-top:9px; color:#d29922; font-size:12.5px; }
  #items { margin-bottom:22px; }
  #items .none { color:var(--dim); font-size:12.5px; }
  .item { display:grid; grid-template-columns:24px minmax(0,1fr) auto; gap:2px 10px;
          align-items:baseline; padding:8px 0; border-bottom:1px solid var(--line); }
  .item .n { color:var(--dim); font:12px ui-monospace,Menlo,monospace; }
  .item .what { min-width:0; word-break:break-word; }
  .item .detail { grid-column:2 / 4; color:var(--dim); word-break:break-word;
                  font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace; }
  .item .detail:empty { display:none; }
  .round { margin-bottom:22px; }
  #items > h2, .round > h2 { font-size:11px; font-weight:700; letter-spacing:1.2px;
                             color:var(--dim); text-transform:uppercase; margin-bottom:8px; }
  /* The verdict is the one word on the card that says whether anything was
     accepted, so it is not dim like the labels around it. */
  .round .verdict { font:700 12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
                    color:var(--accent); margin-bottom:9px; }
  .round .field { margin-bottom:9px; }
  .round .field b { display:block; margin-bottom:2px; color:var(--dim);
                    font:700 10.5px/1.6 ui-monospace,monospace; letter-spacing:.9px;
                    text-transform:uppercase; }
  /* The reasons and the files are patched in as one string with newlines between
     them — one setText for the lot, so a poll that changes nothing touches
     nothing — and this is what puts each of them back on its own line. */
  .round .field span { display:block; white-space:pre-wrap; word-break:break-word;
                       font-size:12.5px; color:var(--fg); }
  .round .panels { margin-top:11px; }
  .panels { display:flex; gap:14px; align-items:flex-start; }
  .panel { flex:1; min-width:0; background:var(--panel); border:1px solid var(--line); border-radius:10px;
           overflow:hidden; }
  .panel > summary { padding:8px 14px; cursor:pointer; user-select:none;
                     font:700 11px/1.4 ui-monospace,monospace; letter-spacing:1px; text-transform:uppercase; }
  .panel[open] > summary { border-bottom:1px solid var(--line); }
  .panel.exec > summary { color:var(--accent); } .panel.audit > summary { color:var(--purple); }
  .panel .body { padding:10px 14px; height:44vh; overflow-y:auto;
                 font:12px/1.65 ui-monospace,SFMono-Regular,Menlo,monospace; white-space:pre-wrap; word-break:break-word; }
  .body .t { color:var(--dim); } .body .tool { color:var(--purple); }
  .body .res { color:var(--dim); } .body .asst { color:var(--fg); }
  /* One diff for the whole run, at the foot of the board. It is a .panel like the
     log panes and is styled with them, and everything below is where it differs. */
  #diff { margin-bottom:22px; }
  .panel.diff > summary { color:#3fb950; }
  /* A log pane is a window onto a file that keeps growing, so it is given a fixed
     44vh whether it is full or not. A diff is a finished thing, and most of them
     are the one line saying the branch is already merged: an empty box that deep
     around it reads as a pane that failed to load. It grows to what it holds. */
  .panel.diff .body { height:auto; max-height:60vh; }
  /* The patch is a <pre>, which comes with a font and a white-space of its own
     that would undo the body's. Wrapped rather than scrolled sideways: a diff of
     a minified file is one line, and a horizontal scrollbar inside a collapsed
     pane inside a scrolling board is three things to get out of. */
  .panel.diff pre { font:inherit; white-space:pre-wrap; word-break:break-word; }
  /* Nothing between the stat and a patch that is not there. On every run the
     owner has approved, that one line in the stat is the whole of the pane. */
  .panel.diff pre:empty { display:none; }
  .panel.diff .stat { color:var(--dim); margin-bottom:10px; }
  .panel.diff .cut { margin-top:10px; color:#d29922; }
  /* Stacked, the cross axis is the width, so a collapsed pane must stretch to it
     or it shrinks to the width of its own label. */
  @media (max-width:900px) { .panels { flex-direction:column; align-items:stretch; }
                             #runs { width:240px; } }
</style></head><body>
<header><b>loopgraph</b><span id="hdr">engine dashboard</span></header>
<main><div id="runs"></div><div id="board"><div class="empty">select a run</div></div></main>
<script>
// Two kinds of function on this page, and the names are the contract.
//
//   build…  makes an element and pours markup into it, once, before anything is
//           on screen. It is the only place innerHTML may appear, and the only
//           element it may be set on is the one the function just created.
//   patch…  changes what is already on screen. It changes text, attributes and
//           classes, adds and removes children, and appends with
//           insertAdjacentHTML. It never assigns innerHTML, because that would
//           throw away every node under the element — including the one holding
//           the reader's selection, and the open pane they were reading — and
//           build them all again. All of them but patchDiff run on every poll;
//           patchDiff runs when its pane is opened. The name says what the
//           function does, not what calls it, and it keeps the polling rules
//           either way: a third name would be a rule that stopped applying.
//
// Two more shapes are innerHTML wearing a hat, and neither of them mentions it:
//
//   emptying a container and filling it back up from build… calls, which is what
//   the run list used to do to itself every 4 seconds; and
//   writing textContent that has not changed, which makes a new text node just
//   the same and takes the reader's selection down with the old one.
//
// So a container is emptied only by the function that made it or by a build…
// function, never by one on the poll path, and every word a poll writes goes
// through setText.
//
// tests/test_ui.py holds the rules. It cannot see a browser, so it checks the
// shape: what the reader actually keeps is checked by hand.
//
// The run the reader has chosen: {id, dir}, or null until the first list lands.
// Both keys, because they are not interchangeable — /api/run and /api/diff take
// the workflow id, /api/logs and /api/log take the run directory, and two
// workflows of one directory share the second and not the first.
let sel = null;
// Injected from activities/stream.py, so the page cannot drift from the writers.
const LOG_RE = new RegExp(__LOG_RE__);
// The role in the filename, and the word the reader sees for it.
const ROLES = [['executor', 'executor'], ['audit', 'supervisor']];
const pill = s => ({green:'green',running:'yellow',stopped:'red',failed:'red','merge-ready':'green',
  merged:'green',held:'gray',discarded:'gray',unknown:'gray'}[s]||'gray');
const esc = t => t.replaceAll('&','&amp;').replaceAll('<','&lt;');
// Assigning textContent builds a NEW text node even when the string is the one
// already there, and the reader's selection lives in the old one. Every text a
// poll writes goes through here, so a row nothing has happened to is not touched.
const setText = (el, t) => { if (el.textContent !== t) el.textContent = t; };
function colorize(t) {
  return esc(t).split('\\n').map(l => {
    if (/^\\[[0-9:]+ tool/.test(l)) return `<span class="tool">${l}</span>`;
    if (/^\\[[0-9:]+ result\\]/.test(l)) return `<span class="res">${l}</span>`;
    return l.replace(/^(\\[[0-9:]+) /, '<span class="t">$1</span> ');
  }).join('\\n');
}
// Hh MMm from an hour up, Mm SSs below it. Rounded rather than floored, so a
// duration does not read a second short for the whole second it is on screen.
function duration(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  const two = n => String(n).padStart(2, '0');
  return s >= 3600 ? `${Math.floor(s / 3600)}h ${two(Math.floor(s / 60) % 60)}m`
                   : `${Math.floor(s / 60)}m ${two(s % 60)}s`;
}
function buildRunRow(entry) {
  const div = document.createElement('div');
  div.className = 'run';
  // The row is keyed on the workflow id and shows the directory. Two workflows of
  // one run directory are two rows reading the same word, and the id is the only
  // thing that tells them apart — so it is an attribute, not text.
  div.dataset.id = entry.id;
  // Empty on purpose. Every word in the row is written by patchRunRow, so there
  // is one code path for the text instead of two that have to agree, and nothing
  // a run directory is called can reach the page as markup.
  div.innerHTML = '<div class="dir"></div><div class="meta"><span class="pill"></span><span></span>'
                + '</div><div class="when"><span></span><span></span></div>';
  // The closure keeps the reply this row was first built from, and that is safe
  // because neither field it reads ever moves: the id is what the row is found by
  // from here on, and the server works the directory out from the id.
  div.onclick = () => { sel = {id: entry.id, dir: entry.dir}; patchSelected(); buildBoard(sel.id); poll(); };
  return div;
}
function patchRunRow(row, entry) {
  const [dir, meta, when] = row.children;
  const [state, detail] = meta.children;
  setText(dir, entry.dir);
  state.className = 'pill ' + pill(entry.state);
  setText(state, entry.state);
  setText(detail, entry.detail || '');
  // Whether the run is still open, stamped where a poll can read it back rather
  // than inferred from the words above. All three shapes are in these two
  // fields: a start and no close is running, both is finished, and neither is a
  // directory of log files with no workflow behind it at all. patchRoundCard
  // reads it to tell a round being worked on right now from one that was simply
  // never recorded, which nothing else on the board can tell apart.
  row.dataset.live = entry.start_time && !entry.close_time ? '1' : '';
  // Both times null is a run known only from its log files: it shows no time at
  // all rather than a wrong one. The two live in separate elements because only
  // one of them moves — the duration of a running run counts up on every poll,
  // and a reader with the row's directory selected must not lose it to that.
  const start = entry.start_time ? new Date(entry.start_time) : null;
  const end = entry.close_time ? new Date(entry.close_time) : new Date();
  setText(when.firstElementChild, start ? start.toLocaleString() : '');
  setText(when.lastElementChild, start ? ' · ' + duration(end - start) : '');
}
// The highlight moves here rather than by refetching /api/runs, which is what it
// used to cost: the reader has clicked, and the row they chose lights up now
// instead of up to 4 seconds later.
function patchSelected() {
  for (const row of document.getElementById('runs').children)
    row.classList.toggle('sel', !!sel && row.dataset.id === sel.id);
}
function patchRuns(entries) {
  const list = document.getElementById('runs');
  // Found in a Map rather than with a selector: an id is a workflow id or a run
  // directory name, and a directory name is whatever the owner typed, which is
  // not ours to paste into CSS.
  const rows = new Map([...list.children].map(row => [row.dataset.id, row]));
  let after = null;  // the last row that was already there, in the server's order
  for (const entry of entries) {
    let row = rows.get(entry.id);
    if (row) {
      rows.delete(entry.id);
    } else {
      row = buildRunRow(entry);
      // Put where the server has it, in front of nothing already on screen. The
      // rows that exist keep their places, so a reader with text selected in one
      // does not have it slide out from under them when a run starts.
      list.insertBefore(row, after ? after.nextSibling : list.firstChild);
    }
    patchRunRow(row, entry);
    after = row;
  }
  // Whatever is left in the map is a run the reply no longer carries.
  for (const row of rows.values()) row.remove();
  patchSelected();
}
// The board is emptied here rather than in poll(), so a card from the run just
// left can never sit under the run just chosen, not even for one interval.
//
// Every section is made here, once, and the patches only ever fill them in or
// hide them. That is what lets a poll be a poll: nothing on the 2-second path
// has to decide whether a part of the page exists yet.
// `id` is handed in rather than read off `sel`, the same way patchRounds is handed
// its directory: the board is built for one run, and the diff pane it carries is
// stamped with that run's workflow id once, here.
function buildBoard(id) {
  const board = document.getElementById('board');
  board.replaceChildren();
  const sections = document.createElement('div');
  // Hidden to begin with, all but the log cards: a section says something about
  // the run's state, and until the first reply lands there is no state to say it
  // from. #rounds is the exception because a run directory with logs and no
  // workflow is a real thing the page has always shown.
  sections.innerHTML =
      '<div id="state" hidden><span class="pill"></span><span class="reason"></span></div>'
    + '<div id="why" hidden></div>'
    + '<section id="awaiting" hidden><h2></h2><div class="q"></div><div class="opts"></div>'
    + '<div class="answer"><span class="lbl">answer with:</span><code class="cmd"></code></div>'
    + '<div class="nocard">no card was sent; the lg approve command is the only way to answer</div>'
    + '</section>'
    + '<section id="items" hidden><h2>work items</h2><div class="rows"></div>'
    + '<div class="none">no items yet</div></section>'
    + '<div id="rounds"></div><div id="diff" hidden></div>';
  board.append(sections);
  // One pane, for the run and not for a round: execute_round derives the branch
  // and the worktree from the run token, so every round of a run diffs to the
  // same thing and a copy under each card would be the same patch drawn twice.
  sections.lastElementChild.append(buildDiffPane(id));
}
function buildOptionRow(letter) {
  const row = document.createElement('div');
  row.className = 'opt';
  // Keyed on the letter, because that is what the owner types back. The label
  // beside it is written by the patch, so an option whose wording changed keeps
  // its row and its place instead of being made again.
  row.dataset.letter = letter;
  return row;
}
function buildItemRow(n) {
  const row = document.createElement('div');
  row.className = 'item';
  row.dataset.n = n;
  // Empty on purpose, exactly as a run row is: every word comes from
  // patchItemRow, so nothing an item is called can reach the page as markup.
  row.innerHTML = '<span class="n"></span><span class="what"></span>'
                + '<span class="pill"></span><span class="detail"></span>';
  return row;
}
// status and reason, or the one line saying why there is no state at all.
function patchState(d) {
  const ledger = d && d.ledger;
  const box = document.getElementById('state');
  const why = document.getElementById('why');
  box.hidden = !ledger;
  why.hidden = !!ledger;
  // The diff is read off the last round's branch, so a run with no ledger has
  // nothing to ask /api/diff for, so the pane is hidden rather than empty.
  document.getElementById('diff').hidden = !ledger;
  if (ledger) {
    const [word, reason] = box.children;
    word.className = 'pill ' + pill(ledger.status);
    setText(word, ledger.status || 'unknown');
    setText(reason, ledger.reason || '');
    return;
  }
  // The page's two lines for "there is no state", and it has no third. `temporal`
  // says whether the feed is connected, never whether it knows this id, so an id
  // Temporal has never heard of is the second line and not the first.
  setText(why, d && d.temporal ? 'no workflow for this run'
                               : 'temporal unreachable — logs only');
}
// What the run is asking. It goes the moment the workflow pops `awaiting`, which
// it does as soon as the owner answers, so the next poll is the whole of AC-6.
function patchAwaiting(ledger) {
  const box = document.getElementById('awaiting');
  const a = ledger && ledger.awaiting;
  box.hidden = !a;
  if (!a) return;
  const [head, q, opts, answer, nocard] = box.children;
  setText(head, 'awaiting: ' + (a.kind || ''));
  // A card that went up before this phase recorded no question. Everything else
  // is still worth reading, so the question is left out rather than drawn as an
  // empty box under the heading.
  q.hidden = !a.question;
  setText(q, a.question || '');
  patchOptions(opts, a.options || {});
  // The label goes with the command: an empty box under the words `answer with:`
  // would be worse than neither.
  answer.hidden = !a.answer_with;
  setText(answer.lastElementChild, a.answer_with || '');
  // No card was sent, so the owner's phone never buzzed and the command above is
  // the only way in. lg status says this sentence too, word for word.
  nocard.hidden = !!a.telegram;
}
function patchOptions(box, options) {
  const rows = new Map([...box.children].map(row => [row.dataset.letter, row]));
  let after = null;
  for (const letter of Object.keys(options)) {
    let row = rows.get(letter);
    if (row) {
      rows.delete(letter);
    } else {
      row = buildOptionRow(letter);
      box.insertBefore(row, after ? after.nextSibling : box.firstChild);
    }
    setText(row, letter + ' — ' + options[letter]);
    after = row;
  }
  for (const row of rows.values()) row.remove();
}
function patchItems(ledger) {
  const box = document.getElementById('items');
  box.hidden = !ledger;
  // A workflow that closed before `items` was a ledger key hands back a
  // dictionary with no `items` at all — two runs on this machine do — and
  // /api/run serves that result untouched. Missing reads as empty, which is what
  // `no items yet` is for; ledger.items.map() would be a TypeError on a run the
  // owner can click today.
  const items = (ledger && ledger.items) || [];
  const [, rows, none] = box.children;
  const have = new Map([...rows.children].map(row => [row.dataset.n, row]));
  let after = null;
  for (const entry of items) {
    const key = String(entry.n);
    let row = have.get(key);
    if (row) {
      have.delete(key);
    } else {
      row = buildItemRow(key);
      rows.insertBefore(row, after ? after.nextSibling : rows.firstChild);
    }
    patchItemRow(row, entry);
    after = row;
  }
  for (const row of have.values()) row.remove();
  none.hidden = items.length > 0;
}
function patchItemRow(row, entry) {
  const [n, what, state, detail] = row.children;
  setText(n, String(entry.n));
  setText(what, entry.item || '');
  state.className = 'pill ' + pill(entry.status);
  setText(state, entry.status || '');
  // A done item's commit, cut to the length anyone actually reads, or a parked
  // item's reason, which is the only thing that says why the run moved on.
  setText(detail, entry.status === 'done' ? String(entry.commit || '').slice(0, 10)
                : entry.status === 'parked' ? String(entry.reason || '') : '');
}
// One reply, three sections. patchRounds is not called from here: the log names
// come from the other request and have to be handed down with the run they were
// fetched for, which only poll() holds.
function patchBoard(d) {
  patchState(d);
  patchAwaiting(d && d.ledger);
  patchItems(d && d.ledger);
}
// A round's key, `<item>-<round>`, and the one place the item-1 default lives.
// Two runs recorded rounds before `item_no` was a ledger key, and both wrote
// their logs in the old shape, r1-executor.log, with no item number either. Read
// with different defaults the ledger round keys undefined-1 while its logs key
// 1-1, and the round draws twice: one card headed `item undefined` with the
// verdict and no panes, one with the panes and a permanent ` · in progress` on a
// run that finished hours ago. lg's format_status reads them as item 1 too.
function roundKey(item, round) { return `${item || 1}-${round}`; }
// Newest first: higher item, then higher round, compared as numbers. As strings
// `1-10` sorts under `1-2`, which put round 10 above round 2 and left it there —
// cards are placed once and never re-sorted, so no later poll moves one back.
function newerFirst(a, b) {
  const [ai, ar] = a.split('-').map(Number), [bi, br] = b.split('-').map(Number);
  return bi - ai || br - ar;
}
// What a round says where its verdict goes, in lg._round_verdict's words.
//
// workflows/run.py appends the round entry BEFORE the audit starts, and the
// audit has half an hour to answer, so a round with no `verdict` key is two
// different pieces of news and they must not read alike: `escalated` means the
// gates stayed red and no audit ever ran, and `green` means the supervisor is
// still out. The gate word `green` itself never goes here — it would read as an
// acceptance nobody has made, on the screen whose job is saying what was
// verified.
//
// Whatever verdict is recorded is printed as it stands. `redo` is one this
// engine writes and a live ledger holds, so a list of the words anyone
// remembered would blank it out.
function roundVerdict(entry) {
  if (entry.verdict) return String(entry.verdict);
  if (entry.status === 'escalated') return 'escalated';
  if (entry.status === 'green') return 'audit running';
  return '';
}
function buildRoundCard(key) {
  const card = document.createElement('div');
  card.className = 'round';
  card.dataset.key = key;
  // Empty on purpose, exactly as a run row is: every word comes from
  // patchRoundCard, so there is one code path for the text instead of two that
  // have to agree, and nothing a supervisor wrote can reach the page as markup.
  card.innerHTML = '<h2></h2><div class="verdict"></div>'
                 + '<div class="field"><b>reasons</b><span></span></div>'
                 + '<div class="field"><b>files</b><span></span></div>'
                 + '<div class="field"><b>directive</b><span></span></div>'
                 + '<div class="field"><b>owner asked</b><span></span></div>'
                 + '<div class="field"><b>owner replied</b><span></span></div>'
                 + '<div class="panels"></div>';
  return card;
}
// A labelled block that is not on the card at all when the round has nothing to
// put in it. An empty box under the word `directive` says less than no box.
function patchField(field, text) {
  field.hidden = !text;
  setText(field.lastElementChild, text);
}
function patchRoundCard(card, row, live) {
  // No ledger entry is a state, not a gap: _run_item appends the round only
  // after execute_round returns, so a round the executor is working in right now
  // has a log file growing and no row at all. An empty entry reads every field
  // below as absent, which is exactly what they are.
  const entry = row.entry || {};
  const [head, verdict, reasons, files, directive, asked, replied] = card.children;
  const word = roundVerdict(entry);
  // Both halves of a round's life, and they are two states rather than one. The
  // first is the card with no row behind it. The second is the row written and
  // the audit still out, which lasts up to 30 minutes — a card that knew only
  // the first would go quiet for half an hour on a round that is still running.
  // The suffix goes when the verdict arrives.
  //
  // And neither half means anything on a run that has closed. Both are read off
  // a ledger the page could not get, so on the logs-only view every round of a
  // run that ended yesterday claimed to be running, three lines under a rail
  // saying `logs only` and a banner saying the page knows nothing. `live` is the
  // run's own start and close times, which say which of the two it is, so the
  // suffix stays on exactly the case it was written for: a workflow still open
  // whose ledger cannot be read.
  const running = live && (!row.entry || word === 'audit running');
  const [item, round] = card.dataset.key.split('-');
  setText(head, `item ${item} · round ${round}` + (running ? ' · in progress' : ''));
  verdict.hidden = !word;
  setText(verdict, word);
  // One string with newlines in it rather than a row per reason: the whole lot
  // is one setText, so a poll that changes nothing replaces nothing, and the
  // stylesheet puts each entry back on its own line.
  patchField(reasons, (entry.verdict_reasons || []).join('\\n'));
  patchField(files, (entry.files || []).join('\\n'));
  patchField(directive, entry.directive || '');
  patchField(asked, entry.owner_question || '');
  patchField(replied, entry.owner_reply || '');
}
function buildLogPane(dir, name, label) {
  // <details> carries "open" itself and fires toggle, so there is no collapse
  // state of ours to get out of step with what the reader can see.
  const pane = document.createElement('details');
  // `log` is what patchOpenPanes selects on. Not `.panel`: the diff pane is one of
  // those too and holds no log name, so a poll that took it in would ask /api/log
  // for `undefined` every 2 seconds for as long as the reader left it open.
  pane.className = 'panel log ' + (label === 'executor' ? 'exec' : 'audit');
  // Everything the pane needs to poll itself: which file, how far into it this
  // pane has read, and whether the last reply said its head had been cut.
  pane.dataset.dir = dir;
  pane.dataset.name = name;
  pane.dataset.offset = '0';
  pane.dataset.cut = '';
  pane.innerHTML = '<summary></summary><div class="body"></div>';
  pane.firstElementChild.textContent = label;
  pane.addEventListener('toggle', () => { if (pane.open) patchOpenPanes(); });
  return pane;
}
// Whether the run behind `id` is still open, off the row the run list keeps up
// to date. Read here and not off `sel`, which holds the reply its row was first
// built from: a run that was running when the reader clicked it closes while
// they watch, and a card must not go on claiming otherwise for as long as the
// tab is open.
function runIsLive(id) {
  const row = [...document.getElementById('runs').children].find(r => r.dataset.id === id);
  return !!row && row.dataset.live === '1';
}
function patchRounds(dir, rounds, names, live) {
  // `dir` is passed in, never read off `sel`: these names were fetched for one
  // run and the panes built from them have to be stamped with that same run,
  // whatever the reader has clicked since.
  const box = document.getElementById('rounds');
  // One row per key, filled from both sides, because neither side alone is the
  // set of rounds a run has had. The ledger has nothing about the round the
  // executor is in right now; the logs have nothing about a verdict.
  const rows = {};
  const slot = key => (rows[key] ||= {entry: null, logs: {}});
  for (const entry of rounds) slot(roundKey(entry.item_no, entry.round)).entry = entry;
  for (const name of names) {
    const m = name.match(LOG_RE);
    if (m) slot(roundKey(m[1], m[2])).logs[m[3]] = name;
  }
  const keys = Object.keys(rows).sort(newerFirst);
  // Cards the reply no longer carries go first, before the empty line goes up or
  // a new card is placed. Done last, the empty branch returned before ever
  // reaching it, so a run whose ledger went unreadable and whose directory went
  // away kept its old cards and had `no rounds yet` appended underneath them —
  // a line denying what was sitting above it. Only keyed cards: the empty line
  // is not a round, and the branch below is what puts it up and takes it down.
  const wanted = new Set(keys);
  for (const card of [...box.children])
    if (card.dataset.key && !wanted.has(card.dataset.key)) card.remove();
  const empty = box.querySelector('.empty');
  if (!keys.length) {
    if (empty) { setText(empty, 'no rounds yet'); return; }
    const line = document.createElement('div');
    line.className = 'empty';
    setText(line, 'no rounds yet');
    box.append(line);
    return;
  }
  if (empty) empty.remove();
  for (const key of keys) {
    let card = [...box.children].find(c => c.dataset.key === key);
    if (!card) {
      card = buildRoundCard(key);
      // Newest first, and put in place rather than re-sorted: the cards already
      // on the board are in order, so putting each new one in front of the first
      // older card moves nothing the reader is looking at.
      box.insertBefore(card,
        [...box.children].find(c => newerFirst(c.dataset.key, key) > 0) || null);
    }
    patchRoundCard(card, rows[key], live);
    // A pane never moves between cards: its key chose its card when it was built
    // and it is added once, so a reader mid-log keeps the log they were reading.
    const panels = card.lastElementChild;
    for (const [role, label] of ROLES) {
      const name = rows[key].logs[role];
      if (name && ![...panels.children].some(p => p.dataset.name === name))
        panels.append(buildLogPane(dir, name, label));
    }
  }
}
// A pane's next offset is only written when its own reply lands. A second pass
// starting before the first has finished would read the offset the first pass
// was still working from, ask for the same bytes and append them a second time,
// and the reader would watch the same lines arrive twice.
let panePoll = false;
async function patchOpenPanes() {
  if (panePoll) return;
  panePoll = true;
  try {
    for (const pane of document.querySelectorAll('.panel.log[open]')) {
      const body = pane.lastElementChild;
      const offset = Number(pane.dataset.offset);
      const cut = pane.dataset.cut === '1';
      // Measured before anything lands: a reader sitting at the bottom is carried
      // along, a reader who scrolled up to read is left exactly where they are.
      const stick = body.scrollHeight - body.scrollTop - body.clientHeight <= 4;
      let d;
      try {
        const r = await fetch('/api/log?dir=' + encodeURIComponent(pane.dataset.dir)
          + '&name=' + encodeURIComponent(pane.dataset.name) + '&offset=' + offset);
        if (!r.ok) continue;  // a file the run has not written yet
        d = await r.json();
      } catch (e) { continue; }  // one pane's bad poll is not the board's problem
      // Two signals say "start again", and the pane needs both.
      //
      // head_truncated turning true means append_log cut the head BELOW the
      // stored offset — it keeps the last 500 KB of a 1 MB file — so the offset
      // still looks valid while every byte behind it has moved. This reply's text
      // is thrown away with the pane's: the offset it starts at was a line ending
      // in the old file and is an arbitrary byte in the new one, so rendering it
      // would open the pane on half a word with nothing above it and no way for
      // the reader to tell why. Asking again from 0 costs one 2-second round trip
      // and shows the rewritten file from its start.
      if (d.head_truncated && !cut) {
        body.replaceChildren();
        pane.dataset.offset = '0';
        pane.dataset.cut = '1';
        continue;
      }
      // The other signal: a reply offset below the one sent means the stored
      // offset was past the end of the file, and the reply already starts at 0.
      if (d.offset < offset) body.replaceChildren();
      // Appending adds nodes and touches none that exist, so a selection survives.
      if (d.text) body.insertAdjacentHTML('beforeend', colorize(d.text));
      // `size` counts bytes; `text` is those bytes decoded, and the two differ on
      // any log holding a non-ASCII character. Only `size` is an offset.
      pane.dataset.offset = d.size;
      pane.dataset.cut = d.head_truncated ? '1' : '';
      if (stick) body.scrollTop = body.scrollHeight;
    }
  } finally { panePoll = false; }
}
// The run's branch diff. One pane, collapsed like the log panes, and fetched on
// the reader's gesture rather than on a timer: the answer is two git commands in
// a repository the owner may be working in this second, and a closed run's branch
// does not change while the tab is open.
function buildDiffPane(id) {
  const pane = document.createElement('details');
  pane.className = 'panel diff';
  // A workflow id, and it has to be one: /api/diff reads the branch off the ledger
  // this id names. The run directory the log panes carry is the other key — two
  // workflows write one directory — and it would name no ledger here.
  pane.dataset.id = id;
  // The cut line is written once, here, and only shown or hidden from now on. It
  // never changes, the same way the awaiting block's no-card sentence never does.
  pane.innerHTML = '<summary></summary><div class="body"><div class="stat"></div>'
                 + '<pre></pre><div class="cut" hidden>patch cut at 200 KB</div></div>';
  pane.firstElementChild.textContent = 'diff';
  pane.addEventListener('toggle', () => { if (pane.open) patchDiff(pane); });
  return pane;
}
// Every opening asks again and the reply replaces what the last one said. Nothing
// polls this, so it runs on the gesture that opened the pane: there is no
// selection inside it to lose that the same gesture did not just uncover.
async function patchDiff(pane) {
  const [stat, patch, cut] = pane.lastElementChild.children;
  // All three, and before the request goes out. Clearing the line alone left the
  // last answer's patch underneath the next one's: open the pane on a 67 KB diff,
  // close it, lose the server, open it again, and the pane read `server error`
  // over 67 KB of patch that was no longer being claimed by anything. A page
  // saying two things at once is the failure this whole phase is about.
  setText(stat, 'loading…');
  setText(patch, '');
  cut.hidden = true;
  let d;
  try {
    // The id the pane was built with, never the selection: a reader who moved on
    // while this was in flight has a board built since, and this pane went with
    // the one they left — writing into it reaches nobody, which is the answer.
    const r = await fetch('/api/diff?id=' + encodeURIComponent(pane.dataset.id));
    // fetch rejects on a broken connection and NOT on a 4xx, so without this a
    // 400 would be read as a diff: `stat` undefined, and a pane rendering blank.
    // The endpoint answers 200 with a line for every failure of its own, so a
    // status that is not 200 is a request this page should never have sent.
    if (!r.ok) throw new Error('/api/diff answered ' + r.status);
    d = await r.json();
  } catch (e) {
    // The dashboard's own server, not the diff. Every way the diff itself can fail
    // comes back 200 with its own line in `stat`, so there is nothing else this
    // can be, and it is the word the header already says for the same condition.
    setText(stat, 'server error');
    return;
  }
  // `stat` is never empty here: diff_payload fills both empty diffs in with the
  // line that tells them apart, so an empty pane would be that endpoint's bug and
  // not a case to guess at. A merged run says so where a real stat would sit.
  setText(stat, d.stat || '');
  setText(patch, d.patch || '');
  cut.hidden = !d.truncated;
}
async function runs() {
  const hdr = document.getElementById('hdr');
  try {
    const d = await (await fetch('/api/runs')).json();
    patchRuns(d.runs || []);
    // Nothing chosen yet: take the first row the way the reader would. Through
    // the row's own handler, so there is one path that sets `sel`, builds the
    // board and starts its poll — a second copy of it here would be a poll
    // function calling buildBoard, which is the redraw AC-14 forbids, and a
    // board that was never built has no sections for a patch to fill.
    const first = document.getElementById('runs').firstElementChild;
    if (!sel && first) first.click();
    setText(hdr, d.temporal ? 'engine dashboard' : 'temporal unreachable — logs only');
  } catch(e) { setText(hdr, 'server error'); }
}
async function poll() {
  // The run this poll is about, held before the await rather than read after it.
  // A reader who picks another run while these names are in flight has already
  // had the board cleared under them, and the names that land belong to the run
  // they left. Putting them on the new board is not a flicker that the next poll
  // clears up: patchRounds only ever adds cards, so a phantom round stays until
  // the reader reselects or reloads, and opening one asks /api/log for a file
  // that is not in this run's directory — a 404 every 2 seconds, for good,
  // behind a pane that can never fill.
  //
  // `sel` is replaced on every click and never edited in place, so comparing the
  // held object with it by identity asks exactly the question: is this still the
  // selection this poll was started for.
  const run = sel;
  if (!run) return;
  try {
    // Both requests go out together, and the board is patched from the pair. The
    // state and the logs are one run's answer, and fetching them a poll apart
    // would put a verdict on screen beside the round before it.
    const [state, logs] = await Promise.all([
      fetch('/api/run?id=' + encodeURIComponent(run.id)).then(r => r.json()),
      fetch('/api/logs?dir=' + encodeURIComponent(run.dir)).then(r => r.json()),
    ]);
    // The ledger's rounds and the log names reach the same patch, because one
    // round is both: a row saying what the supervisor decided, and the two files
    // the round wrote while deciding it. Whether the run is still open goes with
    // them, because a round with no verdict means one thing on a workflow that
    // is running and nothing at all on one that closed yesterday.
    if (run === sel) {
      patchBoard(state);
      patchRounds(run.dir, (state && state.ledger && state.ledger.rounds) || [],
                  logs.logs || [], runIsLive(run.id));
    }
  } catch(e) { /* the board keeps what it has until the next poll */ }
  patchOpenPanes();
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


# The two container paths every run is written in terms of: the run directory is
# mounted at /app/runs and the projects tree at /projects. AGENTS.md pins both.
CONTAINER_RUNS = "/app/runs/"
CONTAINER_PROJECTS = "/projects/"


def resolve_repo(worktree: str, runs_dir: Path,
                 projects_dir: str | None) -> tuple[Path | None, str]:
    """The repository on THIS machine behind a round's recorded `worktree`.

    `worktree` is a container path, `/app/runs/<slug>/worktrees/<token>`, because
    that is what the executor ran in. The same directory is `runs_dir/<slug>/
    worktrees/<token>` here, and it holds a one-line pointer file, `.git`, reading
    `gitdir: /projects/<repo>/.git/worktrees/<token>` — again a container path,
    which `projects_dir` (LOOPGRAPH_PROJECTS_DIR, read from .env per request, not
    at import, so an install after `lg ui` started is picked up) turns into a real
    one.

    That last swap is a prefix swap: `/projects/` off the front, `projects_dir`
    plus a slash on. The slash is the whole of it. `.env.example` ships
    LOOPGRAPH_PROJECTS_DIR=/home/you/projects and install.sh writes what the owner
    typed without normalising it, so no real value ends in one, and dropping it
    would turn /projects/deye into <dir>deye — a directory on no machine, so every
    diff the dashboard drew would answer `repository not found`. It is anchored at
    the front and made once, so a repository whose own path contains a /projects/
    segment keeps it. A value that does end in a slash still works: Path collapses
    the double.

    Returns (path, "") or (None, one line saying why). Every failure here is a run
    the owner can still read — a worktree `discard` removed, a machine with no
    .env yet, a repository since moved — so none of them raises.
    """
    if not worktree.startswith(CONTAINER_RUNS):
        return None, f"worktree is not a container path: {worktree}"
    pointer = runs_dir / worktree[len(CONTAINER_RUNS):] / ".git"
    try:
        text = pointer.read_text(errors="replace")
    except (OSError, ValueError):
        # Gone (discard removes the worktree), unreadable, or a directory: a real
        # clone's .git is one. Either way there is no pointer to follow.
        #
        # ValueError as well as OSError, and both halves are load-bearing. A NUL
        # anywhere in `worktree` makes read_text raise ValueError: embedded null
        # byte, which is not an OSError, and errors="replace" is what keeps a
        # pointer file that is not valid UTF-8 from raising UnicodeDecodeError —
        # also a ValueError, so this catch holds the line if that argument is ever
        # dropped. Either way the caller gets a line, never a traceback.
        return None, f"no worktree pointer at {pointer}"
    line = next((ln for ln in text.splitlines() if ln.strip().startswith("gitdir:")), "")
    # Everything before /.git/worktrees/ is the repository. A line that names no
    # repository — no value, or nothing before the marker — is no more use than a
    # missing one, and saying so beats returning Path("") and reporting the
    # server's own working directory as the repository.
    repo = line.split(":", 1)[1].strip().split("/.git/worktrees/", 1)[0] if line else ""
    if not repo:
        return None, "pointer file has no gitdir line"
    if not projects_dir:
        return None, "LOOPGRAPH_PROJECTS_DIR is not set; run ./install.sh"
    if repo.startswith(CONTAINER_PROJECTS):
        repo = projects_dir + "/" + repo[len(CONTAINER_PROJECTS):]
    path = Path(repo)
    try:
        found = path.is_dir()
    except OSError:  # is_dir() swallows ENOENT and a NUL but re-raises
        found = False  # ENAMETOOLONG, and a pointer file is not a bounded input
    return (path, "") if found else (None, f"repository not found: {path}")


DIFF_CAP = 204_800     # bytes of patch kept
STAT_CAP = 20_480      # bytes of --stat kept
DIFF_TIMEOUT = 20      # seconds, per git command
REF_CAP = 4_096        # rev-parse prints one object name; this is room to spare
ERR_CAP = 4_096        # enough of a failed command's message to name the cause


class _GitTimeout(Exception):
    """A git command outlived DIFF_TIMEOUT and was killed. It gets its own line."""


def _git_read_only(repo: Path, argv: list[str], cap: int) -> tuple[int, bytes, bool, str]:
    """One git command in `repo`, on a deadline, with its output cut at `cap` bytes.

    Returns (exit code, at most `cap` bytes of stdout, whether the cap cut it,
    stderr).

    The cut is what bounds memory, so it has to happen while the command is still
    running. Reading a command to completion and slicing afterwards puts the whole
    diff in the dashboard's heap first, which is the one thing the cap exists to
    prevent: a round that rewrote a lockfile is tens of megabytes, and the owner
    may open it on a laptop. So this reads exactly cap + 1 bytes — the extra byte
    is how it knows there was more — and then kills the command, which by then is
    blocked writing into a pipe nobody is draining.

    The deadline governs the PROCESS, not the read. activities/gate.py carries the
    scar from the other way round: a timeout that waited on the drain was silently
    not enforced, the activity ran past it with no heartbeat, and Temporal killed
    and retried it, running the executor twice. A hang is not an exception, so
    without a timer nothing here ever raises and the browser's fetch never fills.
    Killing the process is also what closes the pipe and lets the read return.

    Killing git alone is not enough, which is gate.py's other half: there,
    signalling the shell left `npm run build`'s children running past the timeout.
    git runs a repository's own external-diff and textconv helpers as children
    that inherit its pipes, so one that backgrounds something holds the
    dashboard's pipe open after git itself has gone — and a dead process cannot be
    killed twice. The read below then waits on that helper for as long as it
    lives. So git gets a session of its own and the deadline kills the whole
    group: 30 seconds against 1 on a repository configured that way.

    stdin is /dev/null so git can never sit waiting on a terminal that is not
    there, and stderr is read only when it is going to be used: a command stopped
    at the cap or at the deadline is being killed and has nothing left to explain.
    """
    proc = subprocess.Popen(argv, cwd=repo, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    # Captured now, while the pid is certainly still ours to ask about. Looked up
    # at kill time instead, os.getpgid would be asking about a pid that proc.wait()
    # may already have reaped and the kernel handed to someone else, and killpg —
    # unlike kill — takes that stranger's whole process group down with it.
    # `reaped` closes the rest of the same window: once waited on, the pid is not
    # signalled at all, whichever thread gets there.
    pgid = os.getpgid(proc.pid)
    expired = threading.Event()
    alive = threading.Lock()
    reaped = False

    def _stop() -> None:
        with alive:
            if reaped:
                return
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass  # already gone, or not ours to signal

    def _expire() -> None:
        expired.set()
        _stop()

    timer = threading.Timer(DIFF_TIMEOUT, _expire)
    timer.start()
    try:
        out = proc.stdout.read(cap + 1)
        truncated = len(out) > cap
        err = b"" if truncated or expired.is_set() else proc.stderr.read(ERR_CAP)
    finally:
        timer.cancel()
        _stop()  # a no-op once it has exited; the cut's stop button otherwise
        proc.stdout.close()
        proc.stderr.close()
        with alive:
            proc.wait()
            reaped = True
    if expired.is_set():
        raise _GitTimeout(" ".join(argv))
    return proc.returncode, out[:cap], truncated, err.decode(errors="replace")


def _checked(repo: Path, argv: list[str], cap: int) -> tuple[bytes, bool]:
    """A git command whose failure is a fault, not an answer. Raises on one.

    A command stopped at the cap exits non-zero too — this is what killed it — so
    the exit code only means anything when the output ran to its end.
    """
    rc, out, truncated, err = _git_read_only(repo, argv, cap)
    if rc != 0 and not truncated:
        raise RuntimeError(err.strip() or f"git exited {rc}")
    return out, truncated


def _reason(line: str) -> dict:
    """The shape /api/diff always answers in, carrying one line instead of a diff."""
    return {"stat": line, "patch": "", "truncated": False}


def branch_diff(repo: Path, base_branch: str, branch: str) -> dict:
    """`git diff <base_branch>...<branch>` in `repo`, cut to size and decoded.

    Two commands, --stat then the patch, each on its own deadline and each cut at
    its own cap. Nothing else runs: no fetch, no checkout, no worktree, no config
    write, no gc. The owner may be working in this repository at the moment a poll
    arrives, and a command that took the index lock would block their own git.

    --no-textconv is what makes that a guarantee rather than a habit. `git diff`
    is not unconditionally read-only: a repository whose config sets
    diff.<driver>.cachetextconv stores each text conversion in git notes the
    first time it makes one, so a plain diff creates objects, writes
    refs/notes/textconv/<driver> and its reflog, and takes a ref lock to do it.
    --no-optional-locks does not stop that and neither does --no-ext-diff. These
    are the owner's repositories and their configuration is not ours to predict,
    so the flag goes on rather than the claim coming off. It costs nothing here:
    the dashboard renders a raw patch and has no use for a conversion.

    The cut is made on bytes and the decode comes after, exactly as log_slice does
    it for /api/log. Cutting the decoded text would measure characters, so a diff
    of files written in Greek or Japanese would arrive at up to four times the
    stated cap; cutting bytes and then decoding strictly would raise on the
    character the cut landed inside, turning every large diff into `diff failed`.
    errors="replace" costs one replacement character at the seam instead, and
    execute_round documents non-ASCII filenames as ordinary here.

    An empty `stat` means both commands ran and found nothing. That is a result,
    not a fault, and it is the state of every run the owner approved: merge_branch
    merges the branch into its base with --no-ff and deletes neither, so the branch
    becomes an ancestor of the base and A...B holds nothing. Which of the two empty
    cases it is depends on the ledger's status, which this does not have, so the
    caller fills that line in.
    """
    try:
        for ref in (base_branch, branch):
            # A ref is a name here, never an option. `git diff --output=<file>...x`
            # writes a file, and a leading `-` is all that separates the two;
            # rev-parse refuses such a value anyway, but a read-only endpoint
            # should not rest its read-onlyness on git's option table.
            if not ref or ref.startswith("-"):
                return _reason(f"branch not found: {ref}")
            rc, _, _, _ = _git_read_only(repo, ["git", "rev-parse", "--verify", ref], REF_CAP)
            if rc != 0:
                return _reason(f"branch not found: {ref}")

        span = f"{base_branch}...{branch}"
        stat, _ = _checked(repo, ["git", "diff", "--no-textconv", "--stat", span], STAT_CAP)
        patch, cut = _checked(repo, ["git", "diff", "--no-textconv", span], DIFF_CAP)
    except _GitTimeout:
        return _reason(f"git took longer than {DIFF_TIMEOUT}s; nothing to show")
    return {"stat": stat.decode(errors="replace"),
            "patch": patch.decode(errors="replace"), "truncated": cut}


def diff_payload(wf_id: str, feed, runs_dir: Path) -> dict:
    """The whole body of /api/diff: one run's branch diff, or a line saying why not.

    Every failure below is a run the owner can still look at — a worktree `discard`
    removed, a repository since moved, a branch they deleted by hand after merging
    — so each answers 200 with its own line in `stat`, rather than a 500 the page
    would render as a blank pane and a console error.

    `worktree`, `branch` and `base_branch` are read off the LAST round of the
    ledger and never off the request. Every round of a run shares one branch and
    one worktree (execute_round derives both from the run token), so this is one
    diff per run and not one per round; and a branch name that arrived in a URL and
    reached a git command would be an injection. The request supplies an id.

    .env is read per request rather than at import, so a machine that installs
    loopgraph while `lg ui` is already running starts resolving repositories
    without a restart.
    """
    try:
        ledger = feed.ledger(wf_id) if feed else None
        if not ledger:
            return _reason("no ledger for this workflow")
        rounds = ledger.get("rounds") or []
        if not rounds:
            return _reason("no rounds yet")
        last = rounds[-1]
        repo, why = resolve_repo(last.get("worktree", ""), runs_dir,
                                 read_env(ROOT / ".env").get("LOOPGRAPH_PROJECTS_DIR"))
        if repo is None:
            return _reason(why)

        base_branch = last.get("base_branch", "")
        out = branch_diff(repo, base_branch, last.get("branch", ""))
        if not out["stat"]:
            # Both commands ran and found nothing. The ledger the handler already
            # holds tells the two empty diffs apart; merge-base, log and status are
            # all forbidden here and none of them is needed.
            out["stat"] = (f"already merged into {base_branch}; the branch adds nothing to it"
                           if ledger.get("status") == "merged"
                           else "no changes on this branch yet")
        return out
    except Exception as e:
        first = next((ln for ln in str(e).splitlines() if ln.strip()), type(e).__name__)
        return _reason(f"diff failed: {first.strip()}")


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
    the page said there were no rounds for every run there has ever been."""
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
            elif u.path == "/api/diff":
                wf_id = parse_qs(u.query).get("id", [""])[0]
                if bad_param(wf_id):
                    return self._json({"error": "id must be a workflow id"}, 400)
                # Nothing else in the query is read, on purpose: the branches come
                # from the ledger, so a branch name in a URL reaches no git command.
                self._json(diff_payload(wf_id, feed, runs_dir))
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
