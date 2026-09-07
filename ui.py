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
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import version
from activities.stream import LOG_GLOB, LOG_RE
from envfile import read_env

ROOT = Path(__file__).resolve().parent

# The line append_log puts at the top of a log it has cut down to LOG_CAP // 2.
# A copy, because this phase does not touch activities/stream.py, so
# test_the_truncation_marker_matches_the_writer pins it against the writer.
HEAD_MARK = b"[... head truncated ...]\n"

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>loopgraph</title>
<!-- The tab's dot, drawn inline so there is no second request and no /favicon.ico
     404 in the console. runs() recolours it, and this href is FAV_IDLE's string
     character for character, so the page is not one shade on load and another
     4 seconds later. `%23` is the `#`: a raw one ends the URI at the colour. -->
<link id="fav" rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><circle cx='8' cy='8' r='6.5' fill='%237d8590'/></svg>">
<style>
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
  /* The archived toggle, pushed to the far end of the header: it says something
     about the rail rather than about the run on screen, and the reader looks at it
     once a week. The count beside the box is a header span, so it is already the
     same dim 12px as everything else on this line. */
  #showarch { margin-left:auto; display:flex; gap:6px; align-items:center; cursor:pointer;
              user-select:none; }
  main { flex:1; display:flex; min-height:0; }
  #runs { width:320px; flex:none; overflow-y:auto; background:var(--pane); border-right:1px solid var(--line); }
  .run { padding:11px 15px; border-bottom:1px solid var(--line); cursor:pointer; border-left:3px solid transparent; }
  .run:hover { background:#171d29; }
  .run.sel { background:#1a2231; border-left-color:var(--accent); }
  /* A run the owner has taken off the rail, and the whole of hiding and showing it.
     The row is never removed and never gets `hidden`: the reply goes on carrying
     every run, so the toggle needs no second request, and the poll goes on patching
     the row where it stands. A row removed for being archived would be built again
     four seconds later — the run-list bug this page was rewritten to kill — and the
     row the reader has selected is allowed to be a hidden one. */
  #runs:not(.show-archived) .run[data-archived="1"] { display:none; }
  .run .dir { font:600 12px/1.3 ui-monospace,Menlo,monospace; word-break:break-all; }
  .run .meta { margin-top:5px; display:flex; gap:8px; align-items:center; font-size:11px; color:var(--dim); }
  /* The word on a shown archived row, in the purple this page already uses for its
     own annotations beside a run's own words. Not dim, which is what the state
     detail next to it is: `archived` is why the row is there at all with the toggle
     on, and it must not read as more of the detail. The meta line is a flex row
     with a gap, so an empty span on every unarchived row would be an 8px hole in
     every one of them. */
  .run .arch { color:var(--purple); }
  .run .arch:empty { display:none; }
  /* Tabular figures so a duration counting up does not shuffle the line sideways
     under a reader trying to select it. */
  .run .when { margin-top:4px; font-size:11px; color:var(--dim); font-variant-numeric:tabular-nums; }
  .pill { padding:1px 9px; border-radius:20px; font-size:10.5px; font-weight:700; letter-spacing:.3px; text-transform:uppercase; }
  .green { background:rgba(63,185,80,.15); color:#3fb950; }
  .red { background:rgba(248,81,73,.15); color:#f85149; }
  .yellow { background:rgba(210,153,34,.15); color:#d29922; }
  /* A run blocked on its owner, and the one pill on the rail that is asking for
     something. It takes the accent blue the awaiting card and the selected row
     already wear, so the colour means the same thing everywhere on the page. */
  .blue { background:rgba(88,166,255,.15); color:#58a6ff; }
  .gray { background:rgba(125,133,144,.15); color:#8b949e; }
  /* No padding at the top, and the strip below carries it instead. Measured in a
     browser: a sticky offset is taken from the scroll container's PADDING box, so
     16px here held the strip 16px down from the board's own top edge and left a
     band that width above it with the round cards sliding past in full view. The
     suite cannot see it — nothing in this file lays out a page — and neither can
     a scripted browser reading text back. */
  #board { flex:1; overflow-y:auto; padding:0 20px 16px; min-width:0; }
  /* The selected run's name, pinned over the board it names. Three screens into a
     long run the only thing saying which run this is was the highlight in the
     rail, off to the left and behind the reader's eye.
     Sticky is relative to the nearest thing that scrolls, which is #board, and an
     element cannot stick past its own containing block — so the strip is the
     first child of the board's sections and holds for the length of them.
     Opaque and edged because sticky leaves the element in the flow rather than
     lifting it out: the round cards pass underneath, and a see-through strip is
     two lines of text in one place. The padding is the board's own, moved in here
     for the reason above, and the negative side margins put the strip's edges back
     out to the board's — a border stopping 20px short of each side reads as a
     card, not as the top of the board.
     The [hidden] rule above beats this display on !important, which is the whole
     reason it carries one: the strip is hidden until a run is chosen. */
  #strip { position:sticky; top:0; z-index:1; margin:0 -20px 14px; padding:16px 20px 11px;
           background:var(--bg); border-bottom:1px solid var(--line);
           display:flex; gap:10px; align-items:baseline; }
  #strip .dir { font:600 13px/1.3 ui-monospace,Menlo,monospace; word-break:break-all; }
  /* Tabular figures for the rail's own reason: a duration counting up must not
     shuffle the line sideways under a reader trying to read it. */
  #strip .dur { color:var(--dim); font-size:12px; font-variant-numeric:tabular-nums; }
  /* The one control on this page that changes anything, at the far end of the line
     naming the run it acts on. Quiet, and no red on it: the gesture is reversible
     and the same button is what reverses it. */
  #strip .archbtn { margin-left:auto; padding:4px 10px; border-radius:6px;
                    background:var(--panel); border:1px solid var(--line); color:var(--dim);
                    font:12px/1.4 -apple-system,"Segoe UI",system-ui,sans-serif; cursor:pointer; }
  #strip .archbtn:hover { color:var(--fg); border-color:var(--dim); }
  /* What the server said when it refused, in the reader's line of sight rather than
     in a console they do not have open. Empty until something is refused, and an
     empty flex item is still a gap — `.item .detail` is emptied for the same
     reason. */
  #strip .archerr { color:#f85149; font-size:12px; max-width:44ch; }
  #strip .archerr:empty { display:none; }
  .empty { color:var(--dim); text-align:center; padding:40px 0; font-size:13px; }
  /* The state board: what the run is doing, what it is asking, what it is
     working through. The logs come after all three. */
  #state { display:flex; gap:10px; align-items:baseline; flex-wrap:wrap; margin-bottom:14px; }
  #state .reason { color:var(--dim); font-size:12.5px; min-width:0; word-break:break-word;
                   max-width:80ch; }
  #why { color:var(--dim); font-size:12.5px; margin-bottom:16px; }
  #awaiting { background:var(--panel); border:1px solid var(--line);
              border-left:3px solid var(--accent); border-radius:10px;
              padding:13px 16px; margin-bottom:20px; }
  #awaiting h2 { font:700 11px/1.4 ui-monospace,monospace; letter-spacing:1px;
                 text-transform:uppercase; color:var(--accent); margin-bottom:9px; }
  /* The question is the text of the card the owner saw, line breaks and all, and
     the longest prose on the page: run.py puts the whole card here, uncut. */
  #awaiting .q { white-space:pre-wrap; word-break:break-word; margin-bottom:10px;
                 max-width:80ch; }
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
  /* The detector passes, above the items they produced. The lines are the ones
     lg status prints, so they are set in the terminal's own font and the counts
     line up down the column. */
  #sweep { margin-bottom:22px; }
  #sweep .pass { font:12.5px/1.7 ui-monospace,SFMono-Regular,Menlo,monospace;
                 word-break:break-word; }
  #sweep .ended { margin-top:6px; color:var(--dim); font-size:12.5px; }
  #sweep .none { color:var(--dim); font-size:12.5px; }
  #items { margin-bottom:22px; }
  #items .none { color:var(--dim); font-size:12.5px; }
  /* Four columns since the kind word joined the row: number, item, status, kind.
     The detail spans the three past the number, on the line below them. */
  .item { display:grid; grid-template-columns:24px minmax(0,1fr) auto auto; gap:2px 10px;
          align-items:baseline; padding:8px 0; border-bottom:1px solid var(--line); }
  .item .n { color:var(--dim); font:12px ui-monospace,Menlo,monospace; }
  .item .what { min-width:0; word-break:break-word; }
  /* Beside the pill and never inside it: the pill is the item's status, and a
     removal item being the engine's own work is not one. Empty on a brief item,
     which is every item of every run before this phase. */
  .item .kind { color:var(--purple); font:11px ui-monospace,Menlo,monospace; }
  .item .detail { grid-column:2 / 5; color:var(--dim); word-break:break-word;
                  font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace; }
  .item .detail:empty { display:none; }
  /* A card with edges, and the same three the awaiting card and the log panes
     already wear: a run of twelve rounds ran down the board with nothing but the
     gap between them saying where one ended, and a card collapsed to one line has
     not even that. The padding is the awaiting card's — a border is only a
     boundary if the text stops short of it. */
  .round { margin-bottom:22px; background:var(--panel); border:1px solid var(--line);
           border-radius:10px; padding:13px 16px; }
  #sweep > h2, #items > h2 { font-size:11px; font-weight:700; letter-spacing:1.2px;
                             color:var(--dim); text-transform:uppercase; margin-bottom:8px; }
  /* The summary is the whole of a closed card, so it wears what the card's
     heading wore. The gap under it belongs to the fields below it and is only
     there when they are. The triangle is the browser's own, the same one every
     log pane shows, so a card looks like the thing it is. */
  .round > summary { cursor:pointer; user-select:none; font-size:11px; font-weight:700;
                     letter-spacing:1.2px; color:var(--dim); text-transform:uppercase; }
  .round[open] > summary { margin-bottom:8px; }
  /* The separator between the summary's pieces, put in by the stylesheet and not
     written into the text. A round with no verdict yet and no files leaves those
     two spans empty, and an empty span gets no dot in front of it; written into
     the text the poll would have to add and remove the dots itself, on the one
     line this whole change exists to keep short. */
  .round > summary > span:not(:empty) ~ span:not(:empty)::before { content:' · '; }
  /* The verdict is the one word on the card that says whether anything was
     accepted, so it is not dim like the labels around it. */
  .round .verdict { font:700 12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
                    color:var(--accent); margin-bottom:9px; }
  /* What the word means, in the three colours the rail's pills already use:
     accepted, still out or going round again, escalated. Both copies of the word
     take it — the open card's line and the closed card's summary — so one round
     never reads two colours.
     Each colour names `.round .verdict` in front of it, and that is not
     decoration. The rule above is two classes deep, a bare `.good` is one, and
     specificity beats source order — there are no cascade layers here and no
     !important outside the [hidden] rule. So a plain `.good { color:… }` would
     win on the summary span, which has nothing over it, and lose on the card,
     which has that rule: one round in two colours. The accent above stays where
     it is — it is what a word this map has never heard of falls back to. */
  .round .verdict.good, .s-verdict.good { color:#3fb950; }
  .round .verdict.warn, .s-verdict.warn { color:#d29922; }
  .round .verdict.bad,  .s-verdict.bad  { color:#f85149; }
  .round .field { margin-bottom:9px; }
  .round .field b { display:block; margin-bottom:2px; color:var(--dim);
                    font:700 10.5px/1.6 ui-monospace,monospace; letter-spacing:.9px;
                    text-transform:uppercase; }
  /* The files, the directive and the two owner lines are patched in as one string
     with newlines between them — one setText for the lot, so a poll that changes
     nothing touches nothing — and this is what puts each of them back on its own
     line. The reasons are not among them: they are a row each, styled below.
     80ch because a line the width of the board ran to about 151 characters. */
  .round .field span { display:block; white-space:pre-wrap; word-break:break-word;
                       font-size:12.5px; color:var(--fg); max-width:80ch; }
  /* One row per reason, so a line that wrapped can be told from the next reason
     starting: the padding pushes the row in and the negative text-indent pulls its
     first line back out, which leaves every continuation line sitting past where
     the reason itself began. The pair only works as a pair, and a single text node
     could not be given it at all — which is why the reasons stopped being one
     string. The size, the colour and the break are the field rule's above,
     restated because a row is not that span: a supervisor quotes paths, and one
     with nowhere to break spills past the edge of the card. */
  .round .reason { padding-left:2ch; text-indent:-2ch; max-width:80ch;
                   word-break:break-word; font-size:12.5px; color:var(--fg); }
  .round .panels { margin-top:11px; }
  .panels { display:flex; gap:14px; align-items:flex-start; }
  .panel { flex:1; min-width:0; background:var(--panel); border:1px solid var(--line); border-radius:10px;
           overflow:hidden; }
  /* Two panes split the row 50/50 whatever is in them, so the log the reader had
     opened got 516px of a 1046px row at 1440px, beside a closed one showing
     nothing but its own label. Now a closed pane stops flexing while its
     neighbour is open and shrinks to that label, which puts the open one at 914px
     — 930px when it is the wider label that closed. Both closed or both open is
     the 50/50 as before, because then nothing here matches.
     `.panel.log` on both ends and never a bare `.panel`: the diff pane is a
     `.panel` too, it sits alone in #diff with no sibling to take room from, and a
     shrink rule written for a two-pane row has no business reaching it.
     No JavaScript moved for this. A closed pane still asks for nothing. */
  .panels:has(> .panel.log[open]) > .panel.log:not([open]) { flex:0 0 auto; }
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
<header><b>loopgraph</b><span id="ver"></span><span id="hdr">engine dashboard</span>
<!-- The archived runs, and the way back to them. A label around the box so the
     words are part of the target, and the count starts at 0 because runs() writes
     it on the first reply and a blank pair of brackets before then reads as the
     page having failed to load. -->
<label id="showarch"><input type="checkbox"> <span id="archn">archived (0)</span></label></header>
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
// The class per state word. `waiting` is not one the engine writes: no ledger
// carries it and grepping the workflows for it finds nothing. It is what
// run_entry and patchState say for a run whose ledger holds an `awaiting` and
// whose workflow is still open, and it has a colour of its own because a run
// asking the owner for something must not read like a run getting on with it.
const pill = s => ({green:'green',running:'yellow',waiting:'blue',stopped:'red',failed:'red',
  'merge-ready':'green',merged:'green',held:'gray',discarded:'gray',unknown:'gray'}[s]||'gray');
// The tab's dot in the two states it has: dim when nothing wants the reader, and
// the waiting pill's blue when something does, so the tab and the rail say the
// same thing in the same colour. FAV_IDLE is the href the <link> ships with,
// character for character — a drifted copy would show one icon on load and
// another 4 seconds later, for ever, with the suite green, so the equality is
// pinned by test. `%23` is the `#`: a raw one ends the data URI at the colour.
//
// Up here with sel and pill, and above the first `function`, because regions() in
// tests/test_ui.py charges everything after a declaration to that declaration.
const FAV_IDLE = "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><circle cx='8' cy='8' r='6.5' fill='%237d8590'/></svg>";
const FAV_WAIT = "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><circle cx='8' cy='8' r='6.5' fill='%2358a6ff'/></svg>";
// The class a verdict word wears, and through the stylesheet the colour: green
// for accepted, yellow for the supervisor still being out or the round going
// again, red for escalated.
//
// Out here and not inside roundVerdict, which prints whatever word the ledger
// holds and enumerates none. A word missing from this map is not an error and
// gets no branch of its own: it loses its colour and keeps every letter of its
// text. `redo` is in it because a live ledger records one; anything the engine
// grows later reads neutral until someone puts it here.
//
// Up here with sel, pill and the icons, above the first `function`, for the
// reason FAV_IDLE gives: regions() in tests/test_ui.py charges everything after
// a declaration to that declaration, and this mapping is not roundVerdict's.
const VERDICT_CLASS = {accept:'good', 'audit running':'warn', redo:'warn', escalated:'bad'};
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
// A moment as the reader would say it while it is young — minutes under the hour,
// hours under the day — and the date itself once it is neither. A run started this
// morning read `05/09/2026, 09:14:22` before this, which is the same work to read
// as a calendar and tells you nothing you came for.
//
// Floored, unlike duration above, which is rounded: this says the last whole unit
// that has gone by and never one that has not, so a 90-minute-old run is `1h ago`
// rather than `2h ago`, and one started 20 seconds ago is `0m ago`.
//
// A day and over keeps the absolute format it has always had. `31h ago` is a
// number to do arithmetic on; the day it started is the thing worth knowing about
// a run that old.
function ago(start, now) {
  const m = Math.floor((now - start) / 60000);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return start.toLocaleString();
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
                + '<span class="arch"></span></div><div class="when"><span></span><span></span></div>';
  // The closure keeps the reply this row was first built from, and that is safe
  // because neither field it reads ever moves: the id is what the row is found by
  // from here on, and the server works the directory out from the id.
  //
  // The hash goes down here and nowhere else, so the address bar and the board
  // are set by one act: reload, or paste the URL to someone else, and runs()
  // opens this row again. Encoded, because the id is a workflow id or a run
  // directory name and a directory name is whatever the owner typed — a raw
  // space or `%` in a hash is a malformed URL, and what comes back out of it on
  // the next load has to be the name again, character for character.
  //
  // Nothing listens for a hash change, on purpose: the hash is read on load and
  // that is all. Back and forward moving the URL without moving the selection is
  // the accepted cost of there being one path that sets `sel`.
  div.onclick = () => {
    sel = {id: entry.id, dir: entry.dir};
    location.hash = encodeURIComponent(entry.id);
    // patchStrip after buildBoard, which is the only thing that ever makes a
    // #strip for it to write into, and here at all so the strip names the run the
    // moment the reader clicks rather than on whichever of the next four seconds
    // the run list happens to land.
    patchSelected(); buildBoard(sel.id); patchStrip(); poll();
  };
  return div;
}
function patchRunRow(row, entry) {
  const [dir, meta, when] = row.children;
  const [state, detail, arch] = meta.children;
  setText(dir, entry.dir);
  state.className = 'pill ' + pill(entry.state);
  setText(state, entry.state);
  setText(detail, entry.detail || '');
  // Whether the owner has taken this run off the rail, in the two places it is
  // needed: the stamp the stylesheet hides on, and the word the reader sees on the
  // row when the toggle is showing archived runs. Both off the reply's own flag,
  // which /api/runs puts on every row it serves — a row the server never marked is
  // not archived, so a reply from an older dashboard reads as nothing hidden.
  //
  // The stamp is also what the load-time selection skips, and it is read back by
  // patchStrip for the direction the strip's button offers. One mark, three
  // readers, and none of them works out the answer a second time.
  row.dataset.archived = entry.archived ? '1' : '';
  setText(arch, entry.archived ? 'archived' : '');
  // Whether the run is still open, stamped where a poll can read it back rather
  // than inferred from the words above. All three shapes are in these two
  // fields: a start and no close is running, both is finished, and neither is a
  // directory of log files with no workflow behind it at all. patchRoundCard
  // reads it to tell a round being worked on right now from one that was simply
  // never recorded, which nothing else on the board can tell apart.
  row.dataset.live = entry.start_time && !entry.close_time ? '1' : '';
  // Whether Temporal has times for this run at all, which is a different question
  // and one data-live cannot be made to answer: that stamp is '' for a workflow
  // that has finished and '' for a directory of log files with nothing behind it,
  // and the board owes those two different things — a pill and a duration for the
  // first, the name alone for the second. So a second stamp rather than a wider
  // reading of the first, whose shape is pinned by
  // test_only_a_run_that_is_still_open_claims_a_round_is_in_progress in any case.
  row.dataset.times = entry.start_time ? '1' : '';
  // Both times null is a run known only from its log files: it shows no time at
  // all rather than a wrong one. The two live in separate elements because only
  // one of them moves — the duration of a running run counts up on every poll,
  // and a reader with the row's directory selected must not lose it to that.
  // One clock for the row, so how long ago it started and how long it has run are
  // the same instant read twice rather than two Dates a line apart.
  const now = new Date();
  const start = entry.start_time ? new Date(entry.start_time) : null;
  const end = entry.close_time ? new Date(entry.close_time) : now;
  setText(when.firstElementChild, start ? ago(start, now) : '');
  // The absolute timestamp rides along in both of ago's forms: `2h ago` is the
  // reading a glance wants and the one thing it cannot say is which two hours.
  //
  // Assigned straight, where every word in this row goes through setText. A title
  // is an attribute and not a text node: writing it makes no node for a reader's
  // selection to be lost with, which is the whole of what setText is guarding, so
  // there is nothing here for it to save.
  when.firstElementChild.title = start ? start.toLocaleString() : '';
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
  //
  // #strip is first because it is the line that stays while everything under it
  // scrolls away, and the stylesheet can only pin it to the top of what it is
  // inside. Empty, like a run row: every word in it is written by patchStrip off
  // the rail, so nothing a run directory is called reaches the page as markup.
  //
  // The strip's last two children are the archive control and the line the server
  // gets to answer with. The button ships hidden because patchStrip is what shows
  // it, and it is empty for the same reason the three spans are: patchStrip writes
  // `archive` or `unarchive` off the row's own stamp.
  sections.innerHTML =
      '<div id="strip" hidden><span class="dir"></span><span class="pill"></span><span class="dur"></span>'
    + '<button class="archbtn" hidden></button><span class="archerr"></span></div>'
    + '<div id="state" hidden><span class="pill"></span><span class="reason"></span></div>'
    + '<div id="why" hidden></div>'
    + '<section id="awaiting" hidden><h2></h2><div class="q"></div><div class="opts"></div>'
    + '<div class="answer"><span class="lbl">answer with:</span><code class="cmd"></code></div>'
    + '<div class="nocard">no card was sent; the lg approve command is the only way to answer</div>'
    + '</section>'
    + '<section id="sweep" hidden><h2>sweep</h2><div class="rows"></div>'
    + '<div class="ended"></div><div class="none">(no passes yet)</div></section>'
    + '<section id="items" hidden><h2>work items</h2><div class="rows"></div>'
    + '<div class="none">no items yet</div></section>'
    + '<div id="rounds"></div><div id="diff" hidden></div>';
  board.append(sections);
  // One pane, for the run and not for a round: execute_round derives the branch
  // and the worktree from the run token, so every round of a run diffs to the
  // same thing and a copy under each card would be the same patch drawn twice.
  sections.lastElementChild.append(buildDiffPane(id));
  // The dashboard's one gesture that changes anything, wired here on the button
  // this function has just made. Here and not in patchStrip, because a click is
  // not a poll: /api/archive is named in no runs(), poll() or patch… function, so
  // a page nobody touches goes on sending its four GETs and nothing else, which is
  // what checklist item 8 is watching for.
  //
  // Nothing is done to the row before the server has answered. The rail is patched
  // from the next reply and from nothing else, so a run leaves it because the flag
  // is written and never because the page assumed it would be — a refusal then
  // needs nothing put back, which is the failure mode this control could most
  // easily have shipped.
  const btn = sections.querySelector('.archbtn'), err = sections.querySelector('.archerr');
  btn.onclick = async () => {
    // The direction is the word on the button, which patchStrip wrote off the row's
    // own stamp: what the reader was offered is what gets asked for, and the two
    // cannot come apart between the poll and the click. `sel` is read here, with no
    // await above it, so it is the run the strip is naming as the button is
    // pressed.
    const want = btn.textContent === 'archive';
    let r;
    try {
      r = await fetch('/api/archive', {method: 'POST',
                                       headers: {'Content-Type': 'application/json'},
                                       body: JSON.stringify({id: sel.id, archived: want})});
    } catch (e) {
      // The dashboard's own server, the word the header and the diff pane already
      // use for it. A click that does nothing and says nothing is worse than no
      // button at all.
      setText(err, 'server error');
      return;
    }
    if (!r.ok) {
      // The server's sentence, verbatim: AC-18's refusals are written to be read by
      // whoever clicked. The fallback is for a body that is not the JSON this
      // endpoint always answers with — a proxy in the way, or a 500 from something
      // that never got as far as the handler.
      const d = await r.json().catch(() => ({}));
      setText(err, d.error || `archive failed (${r.status})`);
      return;
    }
    setText(err, '');
    // Now rather than on whichever of the next four seconds the run list lands on.
    // runs() is the one path that repatches the rail, so the row is stamped, the
    // count is rewritten and the button's word turns round together.
    runs();
  };
}
// The selected run's name over the board, with the pill and the duration beside
// it — copied off the rail row the run list keeps fresh, never worked out again
// here. The rail stays the one source for those three words, so the strip cannot
// come to disagree with the row it is mirroring, which is the contradiction
// checklist item 4 looks for with the two halves an inch apart.
//
// The guard is the first thing in it and it is not an edge case. `sel` is null
// until something is clicked and #strip does not exist until buildBoard has made
// it, and both are true on every page load and stay true for ever on a dashboard
// with no runs. runs() is one try around the fetch, patchRuns, the guarded first
// click and the header writes, with a single catch that writes `server error`: a
// TypeError thrown from here goes into that catch, the one line that ever selects
// a run never runs, and the page reads `server error` over a board that was never
// built, again every 4 seconds, on a server that answered every request. The
// click handler is the same shape from the other end — a listener that throws
// leaves the board unbuilt and says so nowhere but the console.
//
// No answer command up here, on purpose. user-select:all is what makes that
// command copyable in one gesture, and two copies of it on screen is an invitation
// to paste the wrong one into a shell.
function patchStrip() {
  const strip = document.getElementById('strip');
  if (!sel || !strip) return;
  const row = [...document.getElementById('runs').children].find(r => r.dataset.id === sel.id);
  // The run has left the reply. The strip keeps what it has, the way the board
  // keeps its cards: the last true thing it said beats a name wiped mid-poll.
  if (!row) return;
  strip.hidden = false;
  const [name, word, dur] = strip.children;
  const [rowName, meta, when] = row.children;
  const [rowPill] = meta.children;
  setText(name, rowName.textContent);
  // A run known only from its log files: no workflow behind it, so no state to
  // make a pill out of and no times to make a duration from. The page has neither
  // and must not invent them. Off the row already in hand rather than through
  // runHasTimes, which is the same stamp read by patchState, which has no row.
  const times = row.dataset.times === '1';
  word.hidden = !times;
  dur.hidden = !times;
  // The colour and the word together. A pill given the row's class and not the
  // row's text is a coloured badge reading whatever the run before it left there.
  word.className = rowPill.className;
  setText(word, rowPill.textContent);
  // The row writes ` · 4m 12s`, where the dot holds the duration off the time
  // beside it. Nothing sits to the strip's left for it to hold off.
  setText(dur, when.lastElementChild.textContent.replace(/^ · /, ''));
  // The archive control, shown as soon as there is a row behind the strip — which
  // is every run the reader can select, open or finished. The page does not grey
  // it out on an open run: AC-18 puts that refusal on the server, where it is true
  // whatever the page believes, and showing the sentence the server sends back
  // beats the page guessing at it and being wrong in the direction that costs the
  // owner a click they were entitled to.
  const btn = strip.querySelector('.archbtn');
  btn.hidden = false;
  // The stamp the stylesheet hides on, read back for the word the button offers,
  // so the button and the rail can never say opposite things about one run.
  setText(btn, row.dataset.archived === '1' ? 'unarchive' : 'archive');
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
                + '<span class="pill"></span><span class="kind"></span><span class="detail"></span>';
  return row;
}
function buildPassRow(pass) {
  const row = document.createElement('div');
  row.className = 'pass';
  // Keyed on the pass number the way an item row is keyed on its own: that is how
  // the row is found again on the next poll. Empty on purpose, as those rows are —
  // the line is written by patchSweep, so nothing a detector is called can reach
  // the page as markup.
  row.dataset.pass = pass;
  return row;
}
// status and reason, or the one line saying why there is no state at all.
// `live` is the run's own start and close times, off the rail row's data-live
// stamp, and it is handed in rather than read here so the whole board answers
// one reading of it.
function patchState(d, live) {
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
    // The rail's rule, on the rail's two facts, so the two pills cannot say
    // different words about one run. A ledger holding a question is a run
    // waiting on its owner; a workflow that has closed is asking nobody
    // anything, whatever it left written down. `live` and never the status:
    // an engine failure closes a workflow with its ledger still reading
    // `running` and its question still in it.
    const state = ledger.awaiting && live ? 'waiting' : (ledger.status || 'unknown');
    word.className = 'pill ' + pill(state);
    setText(word, state);
    setText(reason, ledger.reason || '');
    return;
  }
  // The page's three lines for "there is no state". `temporal` says whether the
  // feed is connected, never whether it knows this id, so a feed that never
  // answered is the first of them and nothing else is.
  //
  // The third exists because the strip above now names the run. `no workflow for
  // this run` was printed both for a directory of log files with nothing behind
  // it and for a workflow Temporal knows perfectly well whose ledger query
  // happened to fail this poll — nondeterminism on 7 of the 15 histories on this
  // machine. With the strip carrying that second run's pill and duration, the
  // line under it denied the run named an inch above: the board contradicting
  // itself, for as long as the query kept failing.
  //
  // Told apart by the run's own times off the rail, the way the round cards are
  // told apart by its liveness. Nothing else can: there is no ledger to read,
  // which is the whole case, and `temporal` says the feed answered rather than
  // what it said.
  const feed = d && d.temporal;
  setText(why, !feed ? 'temporal unreachable — logs only'
             : runHasTimes(sel.id) ? 'ledger did not answer this poll; the run is still there'
             : 'no workflow for this run');
}
// What the run is asking. It goes the moment the workflow pops `awaiting`, which
// it does as soon as the owner answers, so the next poll is the whole of AC-6.
//
// And it goes when the run closes, on the same rule the pills follow: run.py's
// blanket handler records `stopped` and hands back the ledger it was holding,
// question and all, so a run whose engine died closes with its card still
// written down. Left to the ledger alone the block would ask the owner to answer
// a run that ended — three lines under a pill saying it had stopped.
//
// `live` is folded into the value rather than tested beside it, so the block is
// still shown or hidden by one `hidden = !a`.
function patchAwaiting(ledger, live) {
  const box = document.getElementById('awaiting');
  const a = live && ledger && ledger.awaiting;
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
// The detector passes, in lg status's words. A brief run's ledger has no `sweep`
// key and the section stays hidden; the section itself is built with the board
// either way, so a poll only ever fills it in or hides it.
function patchSweep(ledger) {
  const box = document.getElementById('sweep');
  const sweep = ledger && ledger.sweep;
  box.hidden = !sweep;
  if (!sweep) return;
  const [, rows, ended, none] = box.children;
  const passes = sweep.passes || [];
  const have = new Map([...rows.children].map(row => [row.dataset.pass, row]));
  let after = null;
  for (const p of passes) {
    const key = String(p.pass);
    let row = have.get(key);
    if (row) {
      have.delete(key);
    } else {
      row = buildPassRow(key);
      // A pass only ever arrives at the end, and it is still placed the way an
      // item row is placed: the rows already on screen do not move, so a line the
      // reader has selected stays selected.
      rows.insertBefore(row, after ? after.nextSibling : rows.firstChild);
    }
    // A non-empty note means there is something to read about that detector: it
    // died, or its output was cut at 1 MiB. Names print only on an incomplete
    // pass, and a pass is incomplete only when a detector died, so what the owner
    // sees here is the tool to go and look at. Both lines are lg status's, word for word,
    // and each is written whole rather than pasted together from a stem and a
    // clause, so the two files cannot word one of them differently.
    const names = (p.detectors || []).filter(d => d.note).map(d => d.name).join(', ');
    // How many candidates the pass found in each directory, summed across its
    // detectors — two tools reporting one directory are one number to read — and
    // sorted by name, which is the order lg status sorts them in and the only one
    // that keeps a directory in the same place from pass to pass. A pass recorded
    // before groups existed sums to nothing and prints the line it always did,
    // rather than empty brackets.
    const counts = new Map();
    for (const d of p.detectors || [])
      for (const g of d.groups || []) counts.set(g.name, (counts.get(g.name) || 0) + g.count);
    const groups = [...counts.keys()].sort().map(name => `${name} ${counts.get(name)}`).join(', ');
    setText(row, groups
      ? (p.complete ? `pass ${p.pass}: ${p.total} candidates (${groups})`
                    : `pass ${p.pass}: ${p.total} candidates (incomplete: ${names}) (${groups})`)
      : (p.complete ? `pass ${p.pass}: ${p.total} candidates`
                    : `pass ${p.pass}: ${p.total} candidates (incomplete: ${names})`));
    after = row;
  }
  for (const row of have.values()) row.remove();
  // Why the sweep stopped, which is only there once it has.
  ended.hidden = !sweep.ended;
  setText(ended, sweep.ended ? 'ended: ' + sweep.ended : '');
  none.hidden = passes.length > 0;
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
  const [n, what, state, kind, detail] = row.children;
  setText(n, String(entry.n));
  setText(what, entry.item || '');
  state.className = 'pill ' + pill(entry.status);
  setText(state, entry.status || '');
  // A removal item and a detector's item are the engine's own work, and nothing
  // else in the row says so. An entry from a run older than `kind` has no key:
  // those runs had brief items and nothing else, which is how lg status reads them
  // too. Written on every poll, the empty word included — a convergence item takes
  // its execution position and the pending items after it move up, so the entry
  // under a row keyed `n` can be a different item from one poll to the next, and a
  // word left unwritten would be the old item's, under the new item's number.
  const word = entry.kind && entry.kind !== 'brief' ? entry.kind : '';
  // A sweep item takes one path group, and which one is all that tells two items
  // of the same detector apart. It goes in the word's own span: a column of its
  // own would be empty on every row of every brief run, and every run before this
  // phase is one of those. An item from before the rotation carries no `group`,
  // and lg status prints the two words in this order too.
  setText(kind, word === 'sweep' && entry.group ? `sweep · ${entry.group}` : word);
  // A done item's commit, cut to the length anyone actually reads, or a parked
  // item's reason, which is the only thing that says why the run moved on.
  setText(detail, entry.status === 'done' ? String(entry.commit || '').slice(0, 10)
                : entry.status === 'parked' ? String(entry.reason || '') : '');
}
// One reply, three sections. patchRounds is not called from here: the log names
// come from the other request and have to be handed down with the run they were
// fetched for, which only poll() holds.
//
// `live` goes to the two sections that say what the run wants from its owner.
// The sweep and the work items are the record of what it did, which a run that
// has closed still has.
function patchBoard(d, live) {
  patchState(d, live);
  patchAwaiting(d && d.ledger, live);
  patchSweep(d && d.ledger);
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
// A <details>, so the card collapses to its summary and the browser keeps the
// state — the log panes are written the same way, and a collapse flag of the
// page's own is one more thing to get out of step with what is on screen.
//
// `open` is decided by the caller and set here, on a node that is not on screen
// yet, and it is never written again. patchRounds hands in whether this is the
// newest round on the board at the moment the card is made; a poll that asked
// again would shut the card the reader opened two seconds ago, which is the
// failure the comments above about selections and open panes are all about. So a
// live run watched through twelve rounds ends with twelve open cards. That is
// what never taking one away costs.
function buildRoundCard(key, open) {
  const card = document.createElement('details');
  card.className = 'round';
  card.dataset.key = key;
  card.open = open;
  // Empty on purpose, exactly as a run row is: every word comes from
  // patchRoundCard, so there is one code path for the text instead of two that
  // have to agree, and nothing a supervisor wrote can reach the page as markup.
  // The summary is three spans rather than one, because its three pieces are
  // three readings off the round and each is written by a setText of its own.
  // The reasons are a box rather than a span: one row per reason, put there by
  // patchReasons. Every other field is still one span holding one string.
  card.innerHTML = '<summary><span class="s-head"></span>'
                 + '<span class="s-verdict"></span><span class="s-files"></span></summary>'
                 + '<div class="verdict"></div>'
                 + '<div class="field"><b>reasons</b><div class="rows"></div></div>'
                 + '<div class="field"><b>files</b><span></span></div>'
                 + '<div class="field"><b>directive</b><span></span></div>'
                 + '<div class="field"><b>owner asked</b><span></span></div>'
                 + '<div class="field"><b>owner replied</b><span></span></div>'
                 + '<div class="panels"></div>';
  return card;
}
function buildReasonRow(i) {
  const row = document.createElement('div');
  row.className = 'reason';
  // Keyed on its place in the list, the way an option row is keyed on its letter:
  // that is how patchReasons finds this row again on the next poll. By index and
  // not by the words, because the supervisor writes the list fresh every round —
  // a re-ordered list is text that changed, never a row that moved.
  row.dataset.i = i;
  // Empty on purpose, exactly as a run row is: the words come from patchReasons,
  // so nothing a supervisor wrote can reach the page as markup. No innerHTML here
  // at all, which is why this builder is one createElement and a class.
  return row;
}
// A labelled block that is not on the card at all when the round has nothing to
// put in it. An empty box under the word `directive` says less than no box.
//
// Never the reasons field: its last element is the box of rows, and the setText
// below would assign textContent to it and wipe every one of them. That field's
// hide is written out in patchRoundCard for this reason.
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
  const [summary, verdict, reasons, files, directive, asked, replied] = card.children;
  // The summary is the whole of the card while it is closed, and its three
  // pieces are written one at a time so a poll that changes the verdict leaves
  // the other two nodes alone. Never the <summary> element itself: setText
  // assigns textContent, and textContent here would take all three spans down
  // with it — innerHTML with the name filed off.
  const [head, summaryWord, summaryFiles] = summary.children;
  const word = roundVerdict(entry);
  // What the word means, held as the class to append so both copies of it are
  // written the same way. A word the map has never heard of appends
  // nothing, which leaves each element its base class and the neutral accent
  // `.round .verdict` sets — no branch here drops the word itself, and none can:
  // the text is written below whatever this reads.
  const tone = VERDICT_CLASS[word] ? ' ' + VERDICT_CLASS[word] : '';
  // Both halves of a round's life, and they are two states rather than one. The
  // first is the card with no row behind it. The second is the row written and
  // the audit still out, which lasts up to 30 minutes — a card that knew only
  // the first would go quiet for half an hour on a round that is still running.
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
  // Two ways of saying one thing, and the summary picks whichever says more.
  // `running` is still both halves of a round's life, but the summary now has a
  // verdict span and through the whole audit window that span already reads
  // `audit running` — appending the suffix as well would collapse the card to
  // `item 1 · round 2 · in progress · audit running · 3 files`, the same news
  // twice on the one line the reader is left with. So the suffix takes the half
  // with no word at all: the executor inside the round, no ledger row, and
  // nothing else on the line saying so.
  setText(head, `item ${item} · round ${round}` + (running && !word ? ' · in progress' : ''));
  summaryWord.className = 's-verdict' + tone;
  setText(summaryWord, word);
  // How much the round touched, for a reader scanning a board of closed cards.
  // The list itself is on the card below and is half of what opening one is for.
  // Nothing rather than `0 files` where there is no entry yet: that is a
  // measurement of work that has not happened, not a round that changed nothing.
  const touched = (entry.files || []).length;
  setText(summaryFiles, touched ? (touched === 1 ? '1 file' : `${touched} files`) : '');
  verdict.className = 'verdict' + tone;
  verdict.hidden = !word;
  setText(verdict, word);
  // A row per reason rather than one string with newlines in it. One string was
  // one setText, and a wrapped line inside it looked exactly like the next reason
  // starting — eight reasons, eleven lines, nothing marking which was which. A
  // text node cannot be styled per line, so the hanging indent that tells them
  // apart needs an element each.
  //
  // What the single setText gave, the diff below keeps: patchReasons finds each
  // row by its index and writes it through setText too, so a poll that changes no
  // reason still replaces no node and the reader's selection lives through it.
  //
  // Read once and used twice: the same list decides whether the field is on the
  // card at all. That hide is patchField's, written out here because this field
  // cannot go through patchField — its last element is the box, not a span, and
  // one setText on it would take every row down. Without it a parked round and a
  // round the executor is still inside both grow a bare REASONS heading over
  // nothing.
  const reasonList = entry.verdict_reasons || [];
  reasons.hidden = !reasonList.length;
  patchReasons(reasons.lastElementChild, reasonList);
  patchField(files, (entry.files || []).join('\\n'));
  patchField(directive, entry.directive || '');
  patchField(asked, entry.owner_question || '');
  patchField(replied, entry.owner_reply || '');
}
// The reasons, one row each, diffed against the rows already on screen — the same
// map patchOptions is written on, and never an empty-and-refill, which would look
// identical and lose the reader's selection every 2 seconds.
function patchReasons(box, reasons) {
  const rows = new Map([...box.children].map(row => [row.dataset.i, row]));
  let after = null;
  for (let i = 0; i < reasons.length; i++) {
    // dataset values are strings whatever they were assigned, so the lookup has
    // to be one too or every row is a miss and gets built again.
    let row = rows.get(String(i));
    if (row) {
      rows.delete(String(i));
    } else {
      row = buildReasonRow(i);
      box.insertBefore(row, after ? after.nextSibling : box.firstChild);
    }
    // String() and not the reason itself. `.join` used to do this without anyone
    // asking it to: verdict_reasons is the audit model's JSON, audit.py fills it
    // in with setdefault and checks no types, and run.py copies it into the
    // ledger as it stands, so an entry that is not a string arrives here as one.
    // setText compares with !==, where a string never equals an object, so an
    // uncoerced reason would fail that test for ever and build a new text node
    // every 2 seconds — the exact churn this whole shape exists to avoid.
    setText(row, String(reasons[i]));
    after = row;
  }
  for (const row of rows.values()) row.remove();
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
// Whether Temporal has times for the run behind `id` — a workflow the feed
// answered for, as against a directory of log files with nothing behind it at
// all. Off the row's own stamp, the way runIsLive reads data-live and for the
// same reason: `sel` holds the reply its row was first built from and never moves
// again, and the rail is what the run list keeps up to date.
function runHasTimes(id) {
  const row = [...document.getElementById('runs').children].find(r => r.dataset.id === id);
  return !!row && row.dataset.times === '1';
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
      // Open only if it is the newest round on the board right now — `keys` is
      // sorted newest-first, so that is keys[0]. Decided once, here, where the
      // card is made: the cards already on screen are the reader's, and a poll
      // that worked this out again would close the one they had opened.
      card = buildRoundCard(key, key === keys[0]);
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
  const hdr = document.getElementById('hdr'), ver = document.getElementById('ver');
  const fav = document.getElementById('fav');
  try {
    const d = await (await fetch('/api/runs')).json();
    const rows = d.runs || [];
    patchRuns(rows);
    // How many runs want the reader, said where they can see it with the tab in
    // the background. Counted off the reply and never off the rail, because the
    // rail is what the reader has chosen to look at: an archived run they have
    // hidden has no row to count, and one they are showing has a row that must
    // not be counted. The reply answers both the same way.
    //
    // `archived` is the server's mark; a row it never marked is not archived, so
    // `!r.archived` is the right reading whether the key is there or not.
    const n = rows.filter(r => r.state === 'waiting' && !r.archived).length;
    const title = n ? `(${n}) loopgraph` : 'loopgraph';
    if (document.title !== title) document.title = title;
    // getAttribute, not .href: the property is the URL the browser parsed and
    // wrote out again, and nothing promises that round trip gives back the string
    // we put in — this URI carries spaces and angle brackets, which a serialiser
    // is free to percent-encode. The attribute is the string itself, so the guard
    // compares what was written with what is about to be, and cannot quietly stop
    // holding on some browser and leave the icon rewritten every 4 seconds.
    const icon = n ? FAV_WAIT : FAV_IDLE;
    if (fav.getAttribute('href') !== icon) fav.setAttribute('href', icon);
    // The hash the reader arrived with, decoded, or nothing.
    //
    // A try of its own, holding this one line. decodeURIComponent throws on `#%`
    // — a hash anyone can type and any link can arrive mangled — and everything
    // here is already inside one try whose catch says `server error`. Left to it,
    // a junk hash skips the only line below that ever selects a run and blames
    // the server for a server that answered, every 4 seconds, for as long as the
    // hash stays. A hash that does not decode reads as no hash instead.
    let want = '';
    try { want = decodeURIComponent(location.hash.slice(1)); } catch(e) { want = ''; }
    // Nothing chosen yet: open the run the hash names, and failing that the first
    // row the reader can see. Through the row's own handler, so there is one path
    // that sets `sel`, builds the board and starts its poll — a second copy of it
    // here would be a poll function calling buildBoard, which is the redraw AC-14
    // forbids, and a board that was never built has no sections for a patch to
    // fill.
    //
    // Found in the list rather than by selector, for patchRuns' reason: an id is
    // a workflow id or a directory name, and a directory name is not ours to
    // paste into CSS. The hash's row is taken archived or not, so a link to an
    // archived run still opens it. The fallback skips the archived while the
    // toggle is off, because archiving the newest finished run is the ordinary
    // case: row zero is then a row nobody can see, and the page would open on the
    // one run the reader deliberately took off the rail with `.sel` on a hidden
    // row. A row the server never marked is not archived, so `!== '1'` is the
    // right reading on a rail where nothing is.
    //
    // `showing` is what makes it "the first row the reader can see" rather than
    // "the first unarchived row", and it is the class the stylesheet hides on
    // rather than the checkbox, so the question asked is exactly the one the CSS
    // answers. Without it a rail whose runs are ALL archived, opened with the
    // toggle on, matches no row at all: nothing is clicked, nothing is selected,
    // and the board reads `select a run` for ever with every run on screen and
    // nothing to say why.
    //
    // The row is worked out here and not inside the `if`, because the guard's
    // shape is pinned by test: an `if` with no call in its condition, and a bare
    // name in front of `.click()`. What it is for is the `!sel` — without it the
    // click fires on every poll, the board is built again and the reader's
    // selection and every log pane they had open go with it.
    const shown = [...document.getElementById('runs').children];
    const showing = document.getElementById('runs').classList.contains('show-archived');
    const target = shown.find(r => r.dataset.id === want)
                || shown.find(r => showing || r.dataset.archived !== '1');
    if (!sel && target) target.click();
    // After the click, which is the only line on the page that ever sets `sel`:
    // above it the first load would find no selection and leave the strip empty
    // for another four seconds. patchStrip returns on its own when there is still
    // nothing selected, which on a rail with no rows is every poll there will be.
    patchStrip();
    setText(hdr, d.temporal ? 'engine dashboard' : 'temporal unreachable — logs only');
    // How many runs are off the rail, on the toggle that brings them back. Counted
    // off the reply for the reason the waiting count above it is: the rail is what
    // the reader is looking at, and with the toggle off the archived rows are
    // precisely the ones not on it. The reply carries them all either way, so the
    // number does not change when the toggle is flipped — which is the point of a
    // label that says how many are hidden.
    //
    // Below the waiting count and not above it: both are the poll's only
    // `.filter(…).length`s, and test_archived_rows_do_not_count_toward_the_title
    // reads the first one it finds.
    const arch = rows.filter(r => r.archived).length;
    setText(document.getElementById('archn'), `archived (${arch})`);
    // Read off the reply the rows came from, so the release and the runs on
    // screen are never one poll apart. A checkout with no tag still gets a word:
    // an empty span beside the name reads as the page having failed to load.
    const v = d.version || {}, here = v.installed || 'untagged';
    setText(ver, v.behind ? here + ' · ' + v.latest + ' available · lg update' : here);
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
      // Read once for the whole board. Two readings a line apart are two
      // answers, and a run can close between them — the pill would then come
      // from one and the round cards from the other, which is the page
      // contradicting itself over the one fact both are reading.
      const live = runIsLive(run.id);
      patchBoard(state, live);
      patchRounds(run.dir, (state && state.ledger && state.ledger.rounds) || [],
                  logs.logs || [], live);
    }
  } catch(e) { /* the board keeps what it has until the next poll */ }
  patchOpenPanes();
}
// The archived toggle, and the whole of what it does. One class on the rail, and
// the stylesheet decides what is on screen: no fetch, no row moved, no row
// removed, so flipping it costs the page nothing and the poll never learns it
// happened. The rows are all there either way — the archived ones are hidden, not
// missing — which is why the count beside the box does not change when it is
// flipped and why the selected run stays selected through it.
const archBox = document.getElementById('showarch').firstElementChild;
archBox.onchange = () => {
  document.getElementById('runs').classList.toggle('show-archived', archBox.checked);
};
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


# ---------- the archive store ----------

# The ids the owner has taken off the rail, as a JSON array of strings. This one
# file is the whole of the dashboard's writing, and POST /api/archive is the one
# request that reaches it.
ARCHIVE_FILE = ".archived.json"

# The most a POST /api/archive body may measure. The body is read by its declared
# Content-Length, so without a cap one request can ask the dashboard to hold an
# arbitrary number of megabytes. Two keys and an id is under a hundred bytes.
ARCHIVE_BODY_CAP = 4096

# Held for the whole of mark_archived's read-apply-replace. make_server returns a
# ThreadingHTTPServer, so every request gets a thread of its own and two archive
# clicks landing together run this on two of them: both would read the same set,
# both would write, the later rename would win and one run would stay on the rail
# with its toggle showing archived. The lock closes that half completely. It
# cannot reach `lg rm`, which is a separate process — re-reading inside it is what
# narrows that half to microseconds.
_ARCHIVE_LOCK = threading.Lock()


class ArchiveUnreadable(Exception):
    """The archive file is on disk and is not a JSON array of strings."""


def _read_archive(path: Path) -> set[str]:
    """The store, strictly: a missing file is empty and anything else broken raises.

    This is the read a WRITE goes through, and it is strict so that mark_archived
    can tell a file that is not there — the first archive on a fresh checkout —
    from one that is there and cannot be understood. read_archived is this plus
    forgiveness, and never the other way round: see mark_archived.
    """
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return set()
    except OSError as e:  # a directory, no permission, a bad symlink
        raise ArchiveUnreadable(str(e)) from e
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise ArchiveUnreadable(str(e)) from e
    if not isinstance(data, list) or not all(isinstance(i, str) for i in data):
        raise ArchiveUnreadable(f"{path.name} is not a list of run ids")
    return set(data)


def read_archived(runs_dir: Path) -> set[str]:
    """The archived ids, forgiving every way the file can be broken.

    /api/runs calls this on every poll of every reader, so a file somebody
    hand-edited into something that does not parse has to cost those rows their
    flag and nothing else. Raising here would turn every poll into a 500 and blank
    the rail while the owner worked out what had happened.
    """
    try:
        return _read_archive(runs_dir / ARCHIVE_FILE)
    except ArchiveUnreadable:
        return set()


def mark_archived(runs_dir: Path, ids: list[str], archived: bool) -> None:
    """Add `ids` to the archive or take them out of it. Whole file, atomically.

    Raises ArchiveUnreadable, having written nothing, when the file is there and
    does not parse. That is why the read here is the strict one and not
    read_archived: chained to the forgiving read, a truncated or half-edited file
    comes back as an empty set and this rewrites it as a one-element list, losing
    every flag the owner had. Reads forgive; the write refuses, and the file stays
    for them to rescue by hand.

    The set is re-read here rather than passed in, and inside the lock, because
    three writers reach this file — this handler on two request threads, and
    `lg rm` in a process of its own. A caller holding a copy across a write would
    put back what another writer had just changed.

    Then: a uniquely named temporary file in the same directory, written, flushed,
    fsynced and renamed over the real one. Same directory so the rename is atomic
    on one filesystem; fsynced because a rename over bytes still sitting in the
    page cache is a file that comes back empty after a power cut; and a fresh name
    per write because two writers interleaving on one fixed `.tmp` path can rename
    each other's half-written bytes, which is the tear the rename exists to
    prevent.
    """
    path = runs_dir / ARCHIVE_FILE
    with _ARCHIVE_LOCK:
        keep = _read_archive(path)
        keep = (keep | set(ids)) if archived else (keep - set(ids))
        fd, tmp = tempfile.mkstemp(dir=runs_dir, prefix=".archived-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(sorted(keep)))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise


def run_entry(wf_id: str, status: str, start_time: datetime | None,
              close_time: datetime | None, ledger: dict | None) -> dict:
    """One row of /api/runs.

    `id` is the key the page selects on and the key /api/run and /api/diff take.
    `dir` is the run directory, which is not the same thing: two workflows of one
    directory write the same log files, so they are two rows sharing one `dir`.

    The times come straight off WorkflowExecution and go out as ISO 8601. A
    running workflow has no close time, and a row built from log files alone has
    neither, so the page shows no time rather than a wrong one.

    `state` is `waiting` on the one run the owner has to do something about, and
    that is read off `awaiting` against the close time rather than off the
    ledger's own status. The status says `running` for the whole of a run's life,
    card up or not, so it cannot tell the two apart; and it says `stopped` on a
    workflow whose engine died holding a card, so the ledger alone would leave
    that run asking for ever.
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
        # Open, so the question is still live and there is something to answer.
        # workflows/run.py's blanket handler records `stopped` and returns the
        # ledger it was holding, `awaiting` and all, so a closed workflow keeps
        # its card in the record — and nothing on this page may claim a run that
        # has finished is asking for something.
        if ledger.get("awaiting") and close_time is None:
            entry["state"] = "waiting"
            entry["detail"] = f"r{len(rounds)} · waiting on you"
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
        # The last round that ran: a round whose executor died before it made a
        # worktree has none to diff, and the pane should show the round before it.
        last = next((r for r in reversed(rounds) if r.get("worktree")), rounds[-1])
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

    def workflow_open(self, wf_id: str) -> bool | None:
        """Whether Temporal says this workflow is still open. None: it cannot say.

        Three answers, not two, and the third is the point. `False` is Temporal
        reporting the workflow closed. `None` is Temporal not reporting anything —
        no client, the describe failed, or an id it has never heard of — and the
        caller must not read it as `False`: archiving is what takes a run off the
        rail, so the one thing that must not happen is a live run being hidden
        while it goes on working and putting cards up.

        Open is _still_running's rule, so a workflow Temporal reports with no
        status counts as open, the way `lg` and the ledger fallback already fall
        it. The ledger is not consulted and cannot be: a failed workflow's ledger
        reads `running` and holds its `awaiting` for ever, and those dead runs are
        exactly the ones archiving exists to clear.
        """
        if not self._client:
            return None
        try:
            return self.call(_still_running(self._client.get_workflow_handle(wf_id)))
        except Exception:
            return None


# What the page is told when nothing is checking, and what a checker says before
# its first read lands. One shape either way, so the header has one path through.
NO_VERSION = {"installed": None, "latest": None, "behind": False}


class VersionChecker:
    """What release this checkout is on and whether origin has a newer one.

    On its own thread, because a request must never wait on git. The remote read
    is a connection to GitHub: hung on a poll it would stall the page every time
    the network did, and every reader watching would hold a connection open
    against the same remote. So the reads happen here and a request only reads
    the answer they left in memory.

    The two reads run at different rates for the same reason. The local one is a
    `git describe` against a checkout the owner may be committing to, so it runs
    every period; the remote one costs a round trip and answers a question whose
    answer changes every few weeks, so it runs rarely.
    """

    def __init__(self, root: Path, remote_timeout: float = 10.0,
                 period: float = 60.0, remote_every: int = 60) -> None:
        self._root = root
        self._remote_timeout = remote_timeout
        self._period, self._remote_every = period, remote_every
        self._state = dict(NO_VERSION)
        self._stop = threading.Event()

    def snapshot(self) -> dict:
        """The last answer read, copied so a caller cannot edit what the thread owns."""
        return dict(self._state)

    def refresh(self, remote: bool) -> None:
        """One wake's worth of reading."""
        self._read(local=True, remote=remote)

    def _read(self, local: bool, remote: bool) -> None:
        # Nothing in here raises. It runs on a thread with nobody to tell, and a
        # remote that cannot be reached is the ordinary state of a laptop rather
        # than a failure: the header keeps the answer it has until a later read
        # replaces it, instead of blinking the update hint out and back.
        state = dict(self._state)
        if local:
            try:
                state["installed"] = version.installed(self._root)["tag"]
            except Exception:
                pass
        if remote:
            try:
                state["latest"] = version.remote_newest(self._root, self._remote_timeout)
            except Exception:
                pass
        state["behind"] = version.is_behind(state["installed"], state["latest"])
        # One reference swapped, never the fields one at a time, so a request that
        # lands mid-read gets the old answer whole rather than a new tag beside a
        # remote that has not been asked yet.
        self._state = state

    def start(self) -> None:
        # The local read happens here, on the caller's thread, so the first page
        # anyone loads already names the release. It is a `git describe`, which
        # costs a millisecond; the remote read is what the thread is for.
        self.refresh(False)
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # The remote alone to begin with: the tag was read a moment ago in start().
        self._read(local=False, remote=True)
        tick = 0
        while not self._stop.wait(self._period):
            tick += 1
            self.refresh(remote=tick % self._remote_every == 0)


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
                feed=None, checker=None) -> ThreadingHTTPServer:
    """The dashboard's server. `feed` stands in for the Temporal connection.

    The handler reads only `connected`, `runs()`, `ledger()` and `workflow_open()`
    off it, so a test can pass a fake and assert the endpoints over HTTP with no
    engine running.

    `checker` is a VersionChecker, and there is no default one: a server built
    without it says nothing about releases and runs no git of its own, which is
    what keeps the suite off the network.
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
                # One read of the store per request, stamped onto every row it
                # names — workflow rows and logs-only rows alike, because the
                # owner archives both and both are keyed on `id`. A row the store
                # does not name is not archived. run_entry is left alone: this is
                # the handler's mark, not Temporal's.
                flags = read_archived(runs_dir)
                for row in wf:
                    row["archived"] = row["id"] in flags
                self._json({"runs": wf, "temporal": bool(feed and feed.connected),
                            "version": checker.snapshot() if checker else NO_VERSION})
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
            elif u.path == "/api/archive":
                # The one path that takes a method other than GET, and it takes
                # only POST. Its own branch rather than the 404 below, so a reader
                # who lands on it is told the path exists and the method does not.
                self._json({"error": "POST to archive a run"}, 405)
            else:
                self.send_error(404)

        def do_POST(self):
            """The dashboard's one write: archive a run, or put it back.

            Everything else here reads, and that is what lets `lg ui` be pointed at
            a repository the owner is working in. This path spends that guarantee
            once, on one file under runs/ — never on git and never on Temporal.

            A refusal is a status with a body every time. A handler that raises
            answers nothing at all: socketserver swallows the exception and closes
            the socket, and the page gets a dropped connection where AC-16 asks for
            a 400 it can show a reason from.
            """
            if urlparse(self.path).path != "/api/archive":
                return self._json({"error": "the dashboard writes nowhere else"}, 405)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if not 0 < length <= ARCHIVE_BODY_CAP:
                return self._json({"error": "body must be a small JSON object"}, 400)
            try:
                req = json.loads(self.rfile.read(length))
            except ValueError:
                req = None
            if not isinstance(req, dict):
                return self._json({"error": "body must be a JSON object"}, 400)

            row_id, archived = req.get("id"), req.get("archived")
            # The str check comes first and is not optional. bad_param is
            # `not value or "/" in value or ".." in value`, written for a value off
            # a query string and never handed anything else: a number makes it
            # raise TypeError, and a list or a dict passes it and raises later on
            # the path join. Either way this method dies before it answers.
            if not isinstance(row_id, str) or bad_param(row_id):
                return self._json({"error": "id must be a run or workflow id"}, 400)
            if not isinstance(archived, bool):
                return self._json({"error": "archived must be true or false"}, 400)

            if archived:
                try:
                    # A row known only from its log files is keyed on the run
                    # directory, so it names no workflow and there is nothing to be
                    # open. run_dirs keys such a row exactly this way.
                    logs_only = (runs_dir / row_id / "logs").is_dir()
                except OSError:  # an id longer than the filesystem allows, the
                    logs_only = False  # same guard /api/log carries
                if not logs_only:
                    # Temporal's answer, never the ledger's: a failed workflow's
                    # ledger reads `running` and holds its `awaiting` for ever, and
                    # that run leaving the rail is what archiving is for.
                    is_open = feed.workflow_open(row_id) if feed else None
                    if is_open:
                        return self._json(
                            {"error": "workflow still open: finish or answer it "
                                      "before archiving"}, 409)
                    if is_open is None:
                        return self._json(
                            {"error": "cannot confirm the workflow is closed; "
                                      "is Temporal up?"}, 409)
            # A request carrying `"archived": false` skips that gate whole and
            # arrives here: putting a run back on the rail hides nothing and needs
            # nothing proved, and every sentence in the gate is written about the
            # opposite gesture. Gated, the owner tidying up on the day Temporal is
            # down could not undo their own click.
            try:
                mark_archived(runs_dir, [row_id], archived)
            except ArchiveUnreadable:
                return self._json({"error": "runs/.archived.json is not a readable "
                                            "list; move it aside and try again"}, 409)
            self._json({"id": row_id, "archived": archived})

        def log_message(self, *a):  # quiet
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), H)


def serve(port: int = 8400, temporal_addr: str | None = "localhost:7233") -> None:
    # start() reads the tag here and leaves the remote to its own thread, so the
    # dashboard is listening as fast as it was before there was a checker.
    checker = VersionChecker(ROOT)
    checker.start()
    srv = make_server(port, ROOT / "runs", temporal_addr, checker=checker)
    print(f"loopgraph ui → http://localhost:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    import sys
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8400)
