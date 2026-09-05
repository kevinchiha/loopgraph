# UI state-first dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`. This plan
> states contracts, behaviour and constraints — **it deliberately contains no implementation or
> test bodies**. Read the file you are modifying before writing anything; the plan tells you what
> must be true, the codebase tells you how to say it.

**Goal:** Opening `lg ui` shows what a run is doing, what it is asking, and what it changed, without
reading a wall of log text; Telegram cards and `lg status` say where in the run they speak from.

**Architecture:** `ui.py` stays a stdlib `ThreadingHTTPServer` serving one HTML page from the
`PAGE` string plus JSON endpoints. Two endpoints (`/api/run`, `/api/diff`) key on the workflow id
and read the ledger through `TemporalFeed`; two (`/api/logs`, `/api/log`) key on the run directory
and read files. The page's JavaScript builds each DOM region once and then patches text and
attributes on every poll. `workflows/run.py` records the question it asked in the ledger and puts a
location line at the top of every card's text; a pure helper in `activities/notify.py` formats that
line; `lg status` gains a readable summary and a run-directory lookup. A new `envfile.py` holds the
one `.env` parser both `lg` and `ui.py` use.

**Tech stack:** Python 3.13, `temporalio` 1.32 (client, `Replayer`), stdlib `http.server`, plain
JavaScript inside a Python string, `git` on the host, pytest.

**Spec:** `docs/superpowers/specs/2026-09-05-ui-state-first-design.md` — cite its criteria as `AC-n`.

**Build order:** sequential. One branch, one task at a time, in the order below. Each task sees the
finished state of every task before it. A task that consumes an earlier task's output names it.

## Global constraints

- The gate is `.venv/bin/python -m pytest -q`. It is green today at 199 tests. AGENTS.md says
  about 4 seconds; with the engine live it took 19 seconds on 2026-09-05. Every task's **Verify**
  is that command, or that command plus a narrower `-k` or file argument. A red gate is never
  worked around by deleting or weakening a test.
- `tests/test_release.py` fails the build when a tracked file contains `/home/<name>` (anything
  but `/home/worker` or `/home/you`) or a credential shape. The host's projects directory reaches
  `ui.py` from `.env` at request time through the shared reader (Task 1). No literal host path
  anywhere, tests included: tests use `tmp_path`.
- Nothing under `runs/` is committed except the six example directories `.gitignore` names. Stage
  files by name; never `git add -A` or `git add .`.
- `workflows/run.py` stays deterministic: no clock, no environment read, no randomness, no I/O.
  Temporal replays it from history. Task 4 says the one import it may add and why.
- The engine is live and holds owner cards. Nothing restarts, stops or `docker compose down`s the
  stack, and nothing restarts the worker. No activity changes its name, parameter list, defaults
  or argument count (AC-33). `LoopGraphRun` schedules the same activities in the same order with
  the same argument counts (AC-34). Every ledger key keeps its name and meaning; `question` is the
  only key added (AC-36). Replay is proven with `temporalio.worker.Replayer` over fetched
  history (Tasks 3 and 4), never with a worker restart, and it is proven as a before-and-after
  comparison per workflow id, never as a pass count: 7 of the 13 histories on this machine
  already fail on drift older than this phase.
- The live worker never sees this branch. `worker.py` imports `LoopGraphRun` and registers it at
  process start, so every change to `workflows/run.py` is inert until the worker restarts. No
  task restarts it; the owner does that when no run is holding a card. This is deliberate, it is
  a spec non-goal, and a reviewer should not "fix" it by adding a restart step.
- The restart ban is this phase's rule and the pipeline's, not `AGENTS.md`'s. `AGENTS.md` says
  nothing about restarting the stack, so do not go there to confirm it; the two constraints
  above are the whole of it.
- `ui.py` stays read-only (AC-38): `do_GET` only, no other `do_*` method, no file writes, no
  workflow signals, and the only git commands are `rev-parse --verify` and `diff`.
- Keying is split on purpose: `/api/run` and `/api/diff` take `id` (a workflow id); `/api/logs`
  and `/api/log` take `dir` (a run-directory slug). Two workflows of one run directory are two
  ids writing the same log files. Never swap them.
- Existing tests change only where a task says so, and the task says what they assert afterwards:
  `test_logs_endpoint` (Task 8), `test_runs_endpoint_without_temporal` (Task 9). These pass
  unchanged: `test_the_injected_pattern_survives_javascript`, `test_the_dashboard_ships_a_real_pattern`
  (which needs `new RegExp("…")` to survive in the page), `test_lg_reads_env_the_way_compose_does`,
  both `test_lg_where_*`, `test_the_card_carries_the_id_the_routing_reads_back`.
- `lg` has no `.py` extension. Tests load it with `SourceFileLoader`, the way
  `tests/test_release.py._lg()` does, each test file under its own module name.
- Poll intervals stay 4000 ms for the run list and 2000 ms for the board. The saving comes from
  sending less per poll, not polling less.
- User-facing copy is pinned in the tasks. Do not reword it at implementation time.
- Between Task 8 and Task 12 the served page cannot render logs: `/api/logs` changes shape under the
  old JavaScript. That is expected on the branch. Browser checks begin at Task 12.
- Never create a `CLAUDE.md` in this repo. Never put AI attribution in a commit message: no
  `Co-Authored-By: Claude`, no "Generated with" line. Commit messages are one plain imperative
  sentence, like the existing history.
- Every commit ends with this trailer, after a blank line, and nothing else after it:
  `Claude-Session: https://claude.ai/code/session_01GMcH1Wo5maFWxRVE4cqY1d`
  This is a session link, not an authorship note, and it is the one exception to the rule above.

---

### Task 1: One `.env` reader for `lg` and `ui.py`

`lg` cannot be imported by `ui.py` (no `.py` extension), so the parser moves to a module both can
import. `lg._dotenv()` keeps its name because two test files monkeypatch `lg.ROOT` and call it.

**Delivers:** AC-19

**Files:**
- Create: `envfile.py`
- Modify: `lg` (`_dotenv` only)
- Test: `tests/test_envfile.py` (new)

**Interfaces:**
- Consumes: nothing.
- Produces:

```python
# envfile.py
def read_env(path: str | os.PathLike) -> dict[str, str]
```

`lg._dotenv()` keeps its signature and returns `read_env(os.path.join(ROOT, ".env"))`, reading
`ROOT` at call time.

**Behaviour:**
1. Absent file → `{}`.
2. Skip blank lines, lines starting with `#`, and lines without `=`.
3. Split on the first `=`. Strip the key, drop a leading `export `, strip again.
4. Strip the value. If it is at least two characters and starts and ends with the same quote
   character (`"` or `'`), remove that pair and keep everything inside, `#` included.
5. Otherwise, if the value contains ` #`, cut from there and strip.
6. Later keys overwrite earlier ones.

**Constraints:**
- This is a move, not a rewrite: the rules above are exactly what `lg._dotenv()` does today. The
  quote-stripping branch takes priority over the comment cut, because the existing `elif` does,
  and the last time that drifted `lg where` handed the skill a quoted path.
- `read_env` is pure apart from reading the named file. No `os.environ`, no defaults.
- `lg` imports it as `from envfile import read_env`; `ui.py` will do the same in Task 10. Both run
  with the repo root on `sys.path` (script directory for `lg`, root `conftest.py` for tests).

**Test intents:**
- `test_reads_the_four_shapes_compose_reads` — parametrised over the same four lines
  `test_lg_reads_env_the_way_compose_does` uses, against `read_env` directly.
- `test_absent_file_is_empty`
- `test_blank_comment_and_bare_lines_are_skipped`
- `test_a_quoted_value_keeps_its_hash` — `X="a #b"` → `a #b`; the quote rule wins over the cut.
- `test_lg_dotenv_delegates_to_the_shared_reader` — `inspect.getsource(lg._dotenv)` contains
  `read_env(`; loads `lg` by path under the module name `lg_cli_env`.

**Verify:** `.venv/bin/python -m pytest -q`
Expected: 199 old tests plus the new ones pass; `test_lg_reads_env_the_way_compose_does` and both
`test_lg_where_*` untouched and green.

**Commit:** `Read .env through one shared parser`

---

### Task 2: The location line

A pure helper that says where in a run a card speaks from. It is the only place the format lives.

**Delivers:** AC-21

**Files:**
- Modify: `activities/notify.py` (add one function in the pure-helpers section)
- Test: `tests/test_visibility.py` (new tests next to the card-text tests)

**Interfaces:**
- Consumes: nothing.
- Produces:

```python
# activities/notify.py
def location_line(item_no: int, total: int, round_no: int | None = None) -> str
```

Copy, verbatim:

```
item 2 of 3 · round 2
item 2 of 3
```

The separator is a space, U+00B7 middle dot, space.

**Behaviour:**
1. With a round number: `item {item_no} of {total} · round {round_no}`.
2. Without one: `item {item_no} of {total}`.

**Constraints:**
- Pure. No activity decorator, no import beyond what the module has. Task 4 imports it into
  workflow code, so it must not pull in anything the workflow sandbox would reject.
- `build_card_text` does not change (AC-21). The header lines it writes stay first.

**Test intents:**
- `test_location_line_with_a_round`
- `test_location_line_between_items`
- `test_a_card_whose_summary_starts_with_a_location_still_routes` — `build_card_text` with a
  summary of `location_line(2, 3, 2) + "\n\n" + "q"`, then `wf_from_card` returns the id.

**Verify:** `.venv/bin/python -m pytest -q tests/test_visibility.py && .venv/bin/python -m pytest -q`
Expected: green.

**Commit:** `Add the location line cards will carry`

---

### Task 3: The workflow records the question it asked

One line inside `_await_decision`'s `awaiting` literal, and the replay baseline the rest of the
branch is measured against. The baseline has to be recorded here, before the first edit to
`workflows/run.py`, because afterwards there is nothing left to compare with.

**Delivers:** AC-5, AC-36

**Files:**
- Modify: `workflows/run.py` (`_await_decision` only)
- Test: `tests/test_visibility.py` (source pins, the style `tests/test_review_fixes.py` uses)

**Interfaces:**
- Consumes: nothing.
- Produces: no signature change. One key added to the `awaiting` literal: `"question": summary`.

**Behaviour:**
1. `awaiting.question` is the exact `summary` string `_await_decision` passes to `send_card`,
   recorded in the same statement as `kind`, `options`, `telegram`, `answer_with`.
2. Nothing else in the dict, and no other ledger key, changes (AC-36).

**Constraints:**
- Activity calls do not move: `_await_decision` runs `telegram_configured` then `send_card` with
  six arguments. Same order, same count, nothing added.
- This edit is inert until the worker restarts — `worker.py` binds `LoopGraphRun` at process
  start. Do not restart it and do not add a deploy step; the owner does that when no run is
  holding a card.

**Test intents:**
- `test_the_ledger_records_the_question_it_sent` — the `awaiting` statement in
  `inspect.getsource(LoopGraphRun._await_decision)` contains `"question": summary`.

**Verify:** record the baseline **before touching `workflows/run.py`**, then make the edit, then
run pytest and the same script again into a second file:

```bash
.venv/bin/python - <<'EOF' > /tmp/replay-before.txt
import asyncio
from temporalio.client import Client
from temporalio.worker import Replayer
from workflows.run import LoopGraphRun
async def main():
    c = await Client.connect("localhost:7233")
    async for wf in c.list_workflows('WorkflowType = "LoopGraphRun"'):
        h = await c.get_workflow_handle(wf.id).fetch_history()
        try:
            await Replayer(workflows=[LoopGraphRun]).replay_workflow(h)
            print("PASS", wf.id)
        except Exception as e:
            print("FAIL", wf.id, str(e)[:120])
asyncio.run(main())
EOF
```

```bash
comm -23 <(grep ^PASS /tmp/replay-before.txt | sort) <(grep ^PASS /tmp/replay-after.txt | sort)
```

Expected: `.venv/bin/python -m pytest -q` green, and `comm` prints nothing — no history that
replayed clean before the edit fails after it. Do not expect a clean sweep and do not compare
counts: on 2026-09-05 six of the thirteen histories replayed and seven already failed on
`Activity type of scheduled event 'load_work_items' does not match ... 'run_baseline'`, drift
that entered in commit e23766d long before this phase. Do not filter the listing on
`ExecutionStatus = "Running"` — no `LoopGraphRun` is running, so that listing replays nothing.
The script only lists and fetches: it signals nothing, starts nothing, restarts nothing. Keep
`/tmp/replay-before.txt`; Task 4 compares against the same file.

**Commit:** `Record the question a run asked in its ledger`

---

### Task 4: Every card says where in the run it speaks from

The location line from Task 2 reaches all four card texts. This is the change most likely to
strand a waiting run, so it ends with the same replay comparison, against the baseline Task 3
recorded before either edit.

**Delivers:** AC-20, AC-22, AC-33, AC-34, AC-35

**Files:**
- Modify: `workflows/run.py`
- Test: `tests/test_visibility.py`

**Interfaces:**
- Consumes: `location_line` (Task 2).
- Produces the two signatures below. `_await_decision`, `_note`, `_park_note` and `_owner_card`
  keep theirs.

```python
async def _ask_owner(self, run_dir: str, question: str, options: dict,
                     item_no: int, total: int, round_no: int) -> str
async def _stopped_note(self, run_dir: str, reason: str, item_no: int, total: int) -> None
```

**Behaviour:**
1. `_ask_owner` passes `location_line(item_no, total, round_no) + "\n\n" + question`;
   `_run_item` calls it with `item_no`, `len(self._ledger["items"])`, `round_no`, and still
   stores the bare `question` in `entry["owner_question"]` and hands the bare `question` to
   `record_owner_answer`.
2. `_park_note`'s text starts `location_line(item_no, total) + " parked"`, then today's text.
3. `_stopped_note`'s text starts with `location_line(item_no, total)`, a blank line, then today's
   lines. `run()` passes `(i, len(items))` on a halt and `(len(items), len(items))` when every
   item was parked.
4. `_owner_card`'s summary is `location_line(total, total) + "\n\n" + build_merge_summary(...)`,
   `total` being `len(self._ledger["items"])`.

**Constraints:**
- Activity calls do not move: `_await_decision` runs `telegram_configured` then `send_card` with
  six arguments; `_note` the same pair with seven. Same order, same counts, nothing added.
- Deviation from AC-35's literal "import list unchanged": the existing
  `from activities.notify import send_card, telegram_configured` gains `location_line`. It is a
  pure string function from a module already passed through the sandbox, so the determinism
  AC-35 protects holds. Nothing else is imported.
- A merge card's `question` is the location line plus the merge summary, parked list included.
  That is what the owner saw, and the page prints it as is.
- No location line on the `not an answer` and `discard failed` notes (spec non-goal).
- Inert until the worker restarts, as in Task 3. The first card the owner sees carrying
  `item N of M` is the real confirmation of AC-20, and it comes after this branch, not during it.

**Test intents:**
- `test_activity_argument_counts_are_pinned` — the six-element `args=[...]` in `_await_decision`
  and the seven-element one in `_note`, as they read today.
- `test_owner_question_stays_bare` — `_run_item` source has `entry["owner_question"] = question`
  and passes `question` to `record_owner_answer`.
- `test_the_workflow_module_reads_no_clock_env_or_disk` — `inspect.getsource(workflows.run)`
  has none of `import os`, `import random`, `datetime.now`, `time.time`, `open(`.
- `test_every_card_text_starts_with_the_location_line` — the source of `_ask_owner`,
  `_park_note`, `_stopped_note` and `_owner_card` each builds the string it sends from
  `location_line(`. Pinning the six-element `args=[...]` constrains the argument count and says
  nothing about what `summary` holds, so without this the whole visible half of AC-20 can be
  dropped with the suite green.
- `test_only_the_decision_card_carries_a_round_number` — `_ask_owner` passes three arguments to
  `location_line`; the other three pass two.
- `test_the_all_parked_note_names_the_last_item` — `run()`'s all-parked branch calls
  `_stopped_note` with `(len(items), len(items))`, AC-20's `item 3 of 3` case.

**Verify:** `.venv/bin/python -m pytest -q`, then Task 3's replay script into
`/tmp/replay-after.txt` and its `comm` line against `/tmp/replay-before.txt`.
Expected: green, and `comm` prints nothing.

**Commit:** `Say where in the run each card is speaking from`

---

### Task 5: `lg status` summary and slug resolution, as pure functions

The readable summary and the run-directory lookup, with no Temporal in sight, so they can be tested
on plain dictionaries and lists.

**Delivers:** AC-25, AC-31 (and the formats AC-23 and AC-28 name)

**Files:**
- Modify: `lg`
- Test: `tests/test_lg_status.py` (new; loads `lg` by path under the module name `lg_cli_status`)

**Interfaces:**
- Consumes: nothing.
- Produces:

```python
def format_status(ledger: dict) -> str
def run_slug(arg: str) -> str
def resolve_run_arg(arg: str, candidates: list[tuple[str, datetime]]) -> tuple[str | None, int]
```

Summary copy, verbatim (angle brackets are values; the notes after `←` are not printed):

```
status: <status>
reason: <reason>                 ← only when the ledger has `reason`

awaiting: <kind>                 ← whole section absent when there is no `awaiting`
  <question>                     ← every line of it indented two spaces; or: question not recorded
  A — <label>                    ← one line per option, in dict order
  answer with: <answer_with>
  no card was sent; the lg approve command is the only way to answer   ← only when telegram is false

items:
  1 done 3f2a1b9c0d              ← n, status, then commit[:10] for done, reason for parked, nothing else
  (none yet)                     ← when items is empty OR the key is absent

rounds:
  item 1 round 1 accept          ← the verdict word
  item 1 round 2 escalated       ← no verdict and status escalated: the gates stayed red
  item 2 round 1 audit running   ← no verdict and status green: the audit has not come back
  (none yet)                     ← when rounds is empty OR the key is absent
```

Sections are separated by one blank line. Output ends with a newline.

**Behaviour:**
1. `run_slug` returns the last non-empty `/`-separated segment: `runs/x/` → `x`, `x` → `x`.
2. `resolve_run_arg`: if `arg` equals a candidate id, return `(arg, 1)`. Otherwise
   `prefix = f"run-{run_slug(arg)}-"`; a candidate matches when its id starts with `prefix` and
   the remainder is non-empty and holds no `-`. No match → `(None, 0)`. Several → the id with the
   greatest start time, and the match count.
3. `format_status` follows the copy block. Every key is optional and an absent key never
   raises: a missing `items` or `rounds` reads as an empty list, a missing `status` or `reason`
   prints nothing rather than `None`.
4. A round with no `verdict` prints `escalated` when its `status` is `escalated` and
   `audit running` when its `status` is `green`. The bare word `green` never appears where a
   verdict goes: it is the gate word, and the verdicts are `accept`, `stop`, `plan`, `ask`.

**Constraints:**
- The no-`-` rule is the inverse of `wf.id[4:wf.id.rfind("-")]` in `ui.TemporalFeed._runs`. It is
  what stops `run-foo-` matching `run-foo-bar-ab12cd`.
- Start times are compared with `>`; the CLI passes `WorkflowExecution.start_time`.
- `format_status` is handed real ledgers that predate half the keys it reads. Two closed runs on
  this machine fail the `ledger` query and hand back a recorded result with no `items` key at all
  (`items` entered the ledger in commit 3f49df6; both runs are older), so `ledger["items"]` is a
  `KeyError` and a traceback on a command AC-29 forbids one on. Use `.get` with a default.
- The `awaiting` no-card line is AC-23's, word for word the same string the page pins in Task 14.
  Two verbatim copies of one sentence in two files is how copy drifts, so Task 14 carries a test
  that compares them.
- A round entry exists before its verdict does: `_run_item` appends the entry, then runs the
  audit for up to 30 minutes, then sets `verdict`. So `status: green` with no `verdict` is the
  ordinary mid-audit state of a live run, not a broken record.

**Test intents:**
- `test_full_ledger_prints_every_section` — items, rounds with verdicts, awaiting with a question.
- `test_awaiting_without_a_question_says_not_recorded`
- `test_no_awaiting_prints_no_awaiting_section`
- `test_empty_items_and_rounds_say_none_yet`
- `test_a_ledger_with_no_items_or_rounds_keys_says_none_yet` — the fixture is a real recorded
  result, `{"status", "rounds", "checkpoint", "reason"}`; it must print, not raise.
- `test_a_red_gate_round_prints_escalated` — no `verdict`, `status` `escalated`.
- `test_a_round_still_being_audited_says_audit_running` — no `verdict`, `status` `green`; the
  word `green` appears nowhere in the round line.
- `test_no_card_says_lg_approve_is_the_only_way` — `awaiting.telegram` false prints the line;
  true omits it.
- `test_an_exact_id_wins_without_a_lookup`
- `test_bare_slug_and_runs_forms_resolve_the_same` — `x`, `runs/x`, `runs/x/`.
- `test_a_slug_that_prefixes_another_run_matches_only_its_own`
- `test_two_matches_pick_the_later_start_and_report_two`
- `test_no_match_is_none_and_zero`

**Verify:** `.venv/bin/python -m pytest -q tests/test_lg_status.py && .venv/bin/python -m pytest -q`
Expected: green.

**Commit:** `Format a ledger and resolve a run slug without touching Temporal`

---

### Task 6: `lg status` takes an id or a slug, prints a summary or JSON

Wires Task 5 into `cmd_status`. The argument is tried as a workflow id first, exactly as today;
only a "workflow not found" answer from Temporal turns it into a slug lookup.

**Delivers:** AC-23, AC-24, AC-26, AC-27, AC-28, AC-29, AC-30

**Files:**
- Modify: `lg` (`cmd_status`, the `status` argparse block)
- Test: `tests/test_lg_status.py`

**Interfaces:**
- Consumes: `format_status`, `run_slug`, `resolve_run_arg` (Task 5); `_client()`.
- Produces: the `status` subcommand gains `--json` (`action="store_true"`). The Namespace fields
  `cmd_status` reads are `workflow_id`, `query`, `json`.

stderr copy, verbatim:

```
no workflow for <slug>; looked for ids starting run-<slug>-
using run-<slug>-ab12cd (newest of 2 for <slug>)
```

**Behaviour:**
1. Queries are `[args.query]` when given, else `["status", "ledger"]`, as today.
2. Try each query on `get_workflow_handle(args.workflow_id)`. A `temporalio.service.RPCError`
   whose `status` is `RPCStatusCode.NOT_FOUND` means the id does not exist: go to step 3. Any
   other failure moves to the next query.
3. Slug path: list `WorkflowType = "LoopGraphRun"`, collect `(wf.id, wf.start_time)`, call
   `resolve_run_arg`. No id → print the first stderr line, return 1. More than one candidate →
   print the second stderr line. Then repeat step 2 on the chosen id (a NOT_FOUND here is a plain
   error, not a second lookup).
4. When the `ledger` query fails, fall back to `handle.result()` only when
   `handle.describe().status` is a real status that is not `RUNNING`. A status of `None` counts
   as running and skips the fallback.
5. Terminal failure, whichever query it was: when the last query in the list fails and no
   fallback answers, print the error's first line on stderr and return 1. This covers
   `lg status <id> status` against a `LoopGraphRun`, which declares only a `ledger` query handler
   and answers `Query handler for status expected but not found`. The positional `query` takes
   any name and today's help text teaches `status`, so this is reached by typing, not by
   accident.
6. Output: `--json` or a positional `query` → `json.dumps(result, indent=2)`. Otherwise a result
   from the `status` query prints as JSON (GateCheckRun, RoundRun have no ledger) and a result
   from `ledger` or the result fallback prints `format_status(result)`.
7. stdout carries only the summary or the JSON; every notice goes to stderr.

**Constraints:**
- `handle.result()` blocks until the workflow ends. Without the `describe()` guard a running run
  whose ledger query failed would hang the terminal for as long as the run waits on its card.
- `temporalio` 1.32 types `WorkflowExecution.status` as `WorkflowExecutionStatus | None` and sets
  it to `None` for an unspecified status, so the guard must say which way `None` falls. It falls
  the way `ui.py:165` already falls it — unknown means running — because a wrongly-skipped
  fallback prints an error and a wrongly-taken one hangs the terminal. Import
  `WorkflowExecutionStatus` from `temporalio.client` and compare against the enum member; never
  `.name` (which raises on `None`) and never an integer.
- No traceback reaches the terminal on any path, not just the slug miss (AC-29, AC-26). Today
  `cmd_status` re-raises when the last query fails; that goes, and behaviour 5 replaces it. A
  bare `return 1` with nothing on stderr is just as wrong as a traceback.
- `lg status <id> ledger` keeps printing JSON: README.md and `skills/loopgraph/SKILL.md` document
  it, and Task 7 keeps that line in both.

**Test intents:**
(fixture: monkeypatch `lg._client` with an async function returning a fake whose
`get_workflow_handle(id)` yields a handle with `query`, `result`, `describe`, and whose
`list_workflows(q)` async-yields objects with `.id` and `.start_time`; the fake records calls;
NOT_FOUND is `RPCError("workflow not found for ID: x", RPCStatusCode.NOT_FOUND, b"")`)
- `test_a_real_id_prints_a_summary_and_never_lists` — zero `list_workflows` calls.
- `test_a_status_query_answer_prints_json`
- `test_json_flag_prints_the_raw_ledger`
- `test_positional_query_still_prints_json`
- `test_an_unknown_id_is_retried_as_a_slug` — one candidate, summary on stdout, empty stderr.
- `test_runs_form_resolves` — `runs/<slug>/`.
- `test_two_matches_use_the_newest_and_say_so_in_both_modes` — stderr line exact; stdout parses as
  JSON under `--json`.
- `test_no_match_names_the_prefix_and_exits_1` — stderr line exact, stdout empty, return 1.
- `test_a_closed_run_whose_ledger_query_fails_uses_the_result`
- `test_a_running_run_whose_ledger_query_fails_does_not_wait_on_result` — `result` never awaited,
  return 1.
- `test_an_unknown_status_is_treated_as_running` — `describe()` returns a status of `None`;
  `result` is never awaited and the command returns 1.
- `test_an_unanswerable_positional_query_prints_one_line_and_exits_1` — `lg status <id> status`
  where the handle raises `Query handler for status expected but not found`: stdout empty, one
  line on stderr, no traceback, return 1.

**Verify:** `.venv/bin/python -m pytest -q tests/test_lg_status.py && .venv/bin/python -m pytest -q`
Expected: green.

**Commit:** `Let lg status take a run directory and print something readable`

---

### Task 7: Tell the owner and the skill that a slug works

The slug form is the spec's headline choice, and after Task 6 nothing on the surface mentions it:
`lg status --help` still calls the argument `workflow_id`, and the README and the cross-project
skill still show only `lg status <workflow-id> ledger`. This task is copy only — no behaviour.

**Delivers:** AC-32

**Files:**
- Modify: `lg` (the module docstring, the `status` parser's `help` and its positional `metavar`,
  and a `help=` for the `--json` Task 6 added)
- Modify: `README.md` (two lines)
- Modify: `skills/loopgraph/SKILL.md` (one line)

**Interfaces:**
- Consumes: the `status` argparse block as Task 6 leaves it.
- Produces: no new names. The Namespace field stays `workflow_id`, so `cmd_status` is untouched;
  only the `metavar` the usage line prints changes.

Copy, verbatim. `lg`'s `status` parser:

```
help="read a run: lg status <workflow-id|run-dir> [ledger]"
metavar="arg"
--json help="print the raw query result instead of the summary"
```

`lg`'s module docstring line, replacing "`lg status <workflow-id>` queries gate red/green":

```
`lg status <workflow-id|run-dir>` prints what a run is doing.
```

`README.md`, both places, replacing `lg status <workflow-id> ledger`:

```
lg status runs/<slug>             # what the run is doing
lg status <workflow-id> ledger    # the raw ledger, as before
```

`skills/loopgraph/SKILL.md`, replacing the `lg status <workflow-id> ledger` clause:

```
Also useful: `lg status runs/<slug>` for what the run is doing (a workflow id works too, and
`lg status <workflow-id> ledger` still prints the raw JSON), and
```

**Behaviour:**
1. Every `lg status <workflow-id> ledger` line that exists today keeps a copy in the same block,
   so nothing an agent copied from these files stops working (AC-24, AC-27).
2. The slug example uses the `runs/<slug>` form, because that is what the owner has in their
   shell history from `lg start`, and Task 5's `run_slug` accepts it as well as the bare slug.
3. No code path changes. `argparse` prints `metavar` in the usage line and leaves `dest` alone.

**Constraints:**
- No home directory in any of these lines. `tests/test_release.py` fails the build on one, and
  README and SKILL.md are tracked files.
- `SKILL.md` is read by agents in other projects (`AGENTS.md` routes cross-project work there),
  which is the whole reason this task exists. Keep the sentence one clause long; it sits in a
  paragraph of two other tips.
- Do not touch the `lg tail` or container-path lines near these; they are correct.

**Test intents:**
- `test_the_status_help_offers_a_run_directory` — build `lg`'s parser, format the `status`
  subparser's help, assert it names `run-dir` and that the usage line says `arg`, not
  `workflow_id`.
- `test_the_docs_show_the_slug_form` — parametrised over `README.md` and
  `skills/loopgraph/SKILL.md`: each contains `lg status runs/` and still contains
  `lg status <workflow-id> ledger`.

**Verify:** `.venv/bin/python -m pytest -q`
Expected: green, `test_no_personal_paths_in_tracked_files` included. Then `lg status --help` by
hand: the usage line reads `lg status [-h] [--json] arg [query]`.

**Commit:** `Say in the docs that lg status takes a run directory`

---

### Task 8: Validate parameters, list log names, serve log slices

The two directory-keyed endpoints. `/api/logs` stops sending content; `/api/log` sends the bytes
past an offset so the page can append instead of re-render.

**Delivers:** AC-2, AC-11, AC-12

**Files:**
- Modify: `ui.py` (remove `log_tails` and `LOG_TAIL`; add the helpers below; the `do_GET` branches)
- Test: `tests/test_ui.py`

**Interfaces:**
- Consumes: `LOG_RE` and `LOG_GLOB` from `activities.stream` (`LOG_RE` is already imported;
  add `LOG_GLOB`).
- Produces:

```python
def bad_param(value: str) -> bool               # True when empty, or containing "/" or ".."
def log_names(runs_dir: Path, slug: str) -> list[str]
def log_slice(path: Path, offset: int) -> dict  # {"text", "offset", "size", "head_truncated"}
HEAD_MARK = b"[... head truncated ...]\n"
```

```
GET /api/logs?dir=<slug>                    -> 200 {"logs": [<name>, ...]}
GET /api/log?dir=<slug>&name=<file>&offset=<n> -> 200 {"text", "offset", "size", "head_truncated"}
any validation failure                      -> 400 {"error": "<one line>"}
name matches LOG_RE but names no file       -> 404 {"error": "<one line>"}
```

**Behaviour:**
1. `bad_param` guards `dir` on `/api/logs` and `dir` and `name` on `/api/log`. Task 9 and Task 11
   apply it to `id`.
2. `log_names`: the file names under `runs_dir/<slug>/logs` matching `LOG_GLOB`, sorted, names
   only. A missing directory gives `[]`.
3. `/api/log`: `name` must `re.match(LOG_RE)`, else 400. `offset` is `0` when the parameter is
   absent and when it is present with an empty value; a value that is not a string of digits is
   400. The file must exist, else 404.
4. `log_slice`: `size` is the current file length. If `offset > size`, the slice starts at 0.
   `text` is bytes `[offset:size]` decoded with `errors="replace"`; `offset` in the reply is
   where `text` starts; `size` is what the page sends next time.
5. `head_truncated` is `True` when the file's first `len(HEAD_MARK)` bytes are `HEAD_MARK`. It
   is read from the file, not inferred from the offsets, and it is returned on every reply
   including the first.

**Constraints:**
- The 400 body was `{"logs": {}}`; it becomes `{"error": ...}` on every endpoint so the page has
  one shape to check. `test_logs_reject_traversal` still passes: `urlopen` raises on 400.
- No content cap on `/api/log`. `append_log` already caps a file at 1 MB and the first open of a
  pane fetches it once; after that only new bytes travel.
- Two signals, not one, and the page needs both (Task 12 binds to them). A reply `offset` lower
  than the one requested catches a stored offset that ended up past the new size.
  `head_truncated` catches the other half: `append_log` rewrites an over-cap file as `HEAD_MARK`
  plus its last 500,000 bytes, so a pane whose stored offset was under 500 KB still passes the
  size check while every byte position in the file has moved. Without the flag that pane appends
  text from roughly 800 KB into the old file straight under its stale text, mid-line, with a
  silent 500 KB hole at the seam and nothing to show it happened.
- `HEAD_MARK` is a second copy of a string `activities/stream.py` writes as a literal inside
  `append_log`, and the spec's scope keeps this phase out of that file. A copy that can drift is
  the same shape of bug as the glob below, so pin it with a test rather than leaving it loose.
- Glob through `LOG_GLOB`, never a literal `"*.log"`. `activities/stream.py` says why in its own
  comment: three places once hardcoded the filename shape, and when the item number joined the
  name both readers stopped matching and nobody could watch a run at all. `lg tail` already
  imports the constant; `ui.py`'s copy is the last hardcoded one and this task deletes the line
  it lives on.

**Test intents:**
- `test_logs_endpoint` **changes**: asserts `d["logs"] == [log_name(1, 1, "executor")]` and that
  the name matches `LOG_RE`; the old `"hello" in d["logs"][name]` assertion goes, since content no
  longer travels here.
- `test_log_slices_from_the_offset` — offset 0 returns the whole file and `size == len(bytes)`;
  offset `size` after an append returns only the new line.
- `test_log_restarts_when_the_file_shrank` — request offset past the size; reply `offset == 0`
  and `text` is the whole file.
- `test_a_head_truncated_file_is_flagged` — a file written as `HEAD_MARK` plus a tail, requested
  at an offset well inside it: reply `offset` equals the request (no size signal fires) and
  `head_truncated` is true. The fixture must use an offset *below* the file size, because an
  offset above it is the case the previous test already covers.
- `test_an_untruncated_file_is_not_flagged` — `head_truncated` false at offset 0.
- `test_the_truncation_marker_matches_the_writer` — `HEAD_MARK`'s text appears verbatim in
  `inspect.getsource(activities.stream.append_log)`, the way `tests/test_review_fixes.py` pins
  `LOG_GLOB` against `log_name`.
- `test_log_names_uses_the_shared_glob` — a file whose name matches `LOG_RE` is listed and a
  `.txt` neighbour is not; `inspect.getsource(ui.log_names)` contains `LOG_GLOB`.
- `test_log_rejects_a_name_outside_the_pattern_with_400`
- `test_log_missing_file_is_404`
- `test_log_bad_offset_is_400` — `offset=-1` and `offset=abc`.
- `test_a_blank_offset_reads_as_zero` — `&offset=` with no value returns the whole file, because
  `parse_qs` drops empty values and the handler never sees the key.
- `test_dir_and_name_reject_empty_slash_and_dotdot` — parametrised over `/api/logs` and
  `/api/log`.

**Verify:** `.venv/bin/python -m pytest -q tests/test_ui.py && .venv/bin/python -m pytest -q`
Expected: green. The served page no longer renders logs from here until Task 12; that is expected.

**Commit:** `Serve log names and offset slices instead of whole files`

---

### Task 9: `/api/runs` carries times, `/api/run` returns one ledger

The two things the page needs to key on workflow id. `TemporalFeed` learns to fetch one ledger, and
`make_server` accepts a feed object so the endpoints can be tested without Temporal.

**Delivers:** AC-1, AC-2 (the `id` half), AC-3

**Files:**
- Modify: `ui.py` (`TemporalFeed`, `make_server`, `do_GET`)
- Test: `tests/test_ui.py`

**Interfaces:**
- Consumes: `bad_param` (Task 8).
- Produces:

```python
class TemporalFeed:
    @property
    def connected(self) -> bool                  # bool(self._client)
    def runs(self) -> list[dict]
    def ledger(self, wf_id: str) -> dict | None      # HTTP handler threads only
    async def _ledger(self, wf_id: str) -> dict | None   # the feed's own loop only

def run_entry(wf_id: str, status: str, start_time: datetime | None,
              close_time: datetime | None, ledger: dict | None) -> dict

def make_server(port: int, runs_dir: Path, temporal_addr: str | None = "localhost:7233",
                feed=None) -> ThreadingHTTPServer
```

```
GET /api/runs        -> 200 {"runs": [{"id", "dir", "state", "detail", "start_time", "close_time"}, ...],
                            "temporal": bool}
GET /api/run?id=<wf> -> 200 {"ledger": <object|null>, "temporal": bool}
```

**Behaviour:**
1. The query-then-result fallback is extracted **once, as a coroutine**: `async _ledger(wf_id)`
   awaits the `ledger` query, and on any failure awaits `handle.result()`, and on failure returns
   `None`. An unknown id returns `None` the same way. This is exactly the fallback `_runs` has
   today (`ui.py:166-174`).
2. The two callers reach it differently and that difference is the point. `_runs` is a coroutine
   already running on the feed's loop, so it `await`s `_ledger` directly. The HTTP handler is on
   a request thread, so it calls the sync `ledger(wf_id)`, which is a thin wrapper returning
   `self.call(self._ledger(wf_id))` — `None` when there is no client, and `None` on any
   exception, so a poll never raises into the handler.
3. `run_entry` builds one `/api/runs` row: `dir` as today (`wf.id[4:wf.id.rfind("-")]` when the
   id starts with `run-`), `start_time`/`close_time` as `datetime.isoformat()` or `None`, `state`
   and `detail` from the ledger as today.
4. Logs-only rows (a directory with logs and no workflow) get `id == dir`, `state` `unknown`,
   `detail` `logs only`, both times `None`.
5. `/api/run`: `bad_param(id)` → 400. Otherwise `{"ledger": feed.ledger(id) if feed else None,
   "temporal": bool(feed and feed.connected)}`, always 200.
6. `make_server(feed=...)` uses the given object instead of constructing a `TemporalFeed`. The
   handler reads only `connected`, `runs()`, `ledger()` from it.

**Constraints:**
- `temporal` means "the feed is connected", the same as on `/api/runs`. An unknown id with
  Temporal up is `{"ledger": null, "temporal": true}`; the page (Task 14) tells the two apart.
- `feed=` and `connected` are not in the spec. They exist so `/api/run`, `/api/runs` and
  `/api/diff` can be asserted over HTTP with a fake; the handler stops reading `feed._client`.
- Keep the 25-row cap in `_runs` and the `call(timeout=10)` bound; a poll must not hang the page.
- Never let `_runs` call the sync `ledger()`. `call()` is
  `asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)`, which has no same-thread
  guard: submitted from inside the loop thread it blocks the loop, the coroutine can never start,
  and it raises `TimeoutError` after the full 10 seconds. `_runs` runs on that loop. One sync
  method shared by both callers therefore burns 10 dead seconds per workflow, the outer
  `call(self._runs())` times out first, `runs()` swallows it and returns `[]`, and the run column
  that works today degrades to one `unknown / logs only` row per directory. Two entry points over
  one coroutine is the shape that works.

**Test intents:**
(fixture: a `FakeFeed` class in `tests/test_ui.py` with `connected`, `runs()`, `ledger(id)`
returning canned values; a second server fixture built with `feed=FakeFeed(...)`)
- `test_runs_endpoint_without_temporal` **changes**: keeps its three assertions and adds
  `start_time is None`, `close_time is None`, `id == dir`.
- `test_run_entry_formats_times_and_keeps_dir` — ISO strings, `close_time` `None` while running,
  two ids of one directory give two rows with the same `dir`.
- `test_run_endpoint_without_temporal_is_null_and_200`
- `test_run_endpoint_returns_the_feeds_ledger` — known id → ledger, `temporal` true; unknown id →
  `null`, `temporal` true.
- `test_run_endpoint_rejects_bad_ids` — empty, `/`, `..` → 400.
- `test_the_feed_falls_back_from_query_to_result` — over a real `TemporalFeed` with a stubbed
  `_client` (not `FakeFeed`, which replaces the method under test): the query raises and
  `result()` answers → that dict; both raise → `None`. The `FakeFeed` fixture above cannot reach
  this at all, and it is the one piece that makes every closed run readable.
- `test_the_feed_serves_a_ledger_from_both_threads` — the same stubbed `TemporalFeed`:
  `feed.runs()` returns rows carrying ledger detail, and `feed.ledger(id)` called from a plain
  thread returns the same dict, both within a second. A build that shares one sync method
  between the two callers fails this on the timeout rather than on the value.

**Verify:** `.venv/bin/python -m pytest -q tests/test_ui.py && .venv/bin/python -m pytest -q`
Expected: green.

**Commit:** `Give the dashboard one ledger per workflow id and run times`

---

### Task 10: Find the host repository from a run's worktree pointer

Half of the diff, and the half with no git in it: turning the container path the ledger records
into the directory on this machine that holds the repository. Split from the endpoint because it
is a pure path function with its own failure vocabulary.

**Delivers:** AC-16, AC-19 (the `ui.py` half)

**Files:**
- Modify: `ui.py`
- Test: `tests/test_ui.py`

**Interfaces:**
- Consumes: `read_env` (Task 1).
- Produces:

```python
def resolve_repo(worktree: str, runs_dir: Path, projects_dir: str | None) -> tuple[Path | None, str]
```

The second element is empty on success and one of these lines on failure, verbatim:

```
worktree is not a container path: <worktree>
no worktree pointer at <path>
pointer file has no gitdir line
LOOPGRAPH_PROJECTS_DIR is not set; run ./install.sh
repository not found: <path>
```

**Behaviour:**
1. `worktree` must start with `/app/runs/`, else `not a container path`.
2. The pointer file is `runs_dir / <worktree relative to /app/runs/> / ".git"`; absent gives
   `no worktree pointer`.
3. Its `gitdir:` line, missing gives `pointer file has no gitdir line`, reads
   `/projects/<path>/.git/worktrees/<token>`.
4. The repository is the part before `/.git/worktrees/` with its leading `/projects/` replaced by
   `projects_dir + "/"` — that prefix, once, anchored at position 0. `projects_dir` of `None`
   gives `LOOPGRAPH_PROJECTS_DIR is not set`; a directory that is not there gives
   `repository not found`.
5. Callers read `LOOPGRAPH_PROJECTS_DIR` with `read_env(ROOT / ".env")` per request, not at
   import, so an install after `lg ui` started is picked up and a test can point `ui.ROOT` at
   `tmp_path`.

**Constraints:**
- The trailing slash on the replacement is not cosmetic. `.env.example` ships
  `LOOPGRAPH_PROJECTS_DIR=/home/you/projects` and `install.sh` writes whatever the user typed
  without normalising, so no real value carries a slash of its own — the one on this machine does
  not. Swapping `/projects/` for the bare value turns `/projects/deye` into `<dir>deye`, and every
  diff then answers `repository not found`. Swapping in `<value>/` also survives a value that does
  end in a slash, because `pathlib.Path` collapses the double.
- Anchor the swap at the start and do it once. A plain whole-string replace would also rewrite a
  `/projects/` segment nested inside a repository path.
- Deviation from AC-16's wording "replace `/app/` with the engine root": the pointer is found
  under `runs_dir`, which the server already holds. In production that is `ROOT / "runs"`, the
  same file; in tests `tmp_path` stands in.
- Every live run's pointer read `gitdir: /projects/<repo>/.git/worktrees/<token>` on 2026-09-05.
  A worktree that `discard` removed has no pointer, which is `no worktree pointer`.

**Test intents:**
(fixture: `runs_dir/x/worktrees/ab12cd/.git` reading `gitdir:
/projects/proj/.git/worktrees/ab12cd`, and `<tmp_path>/projects/proj` as a real directory)
- `test_a_pointer_resolves_to_the_host_repository`
- `test_the_projects_prefix_swap_keeps_the_separator` — `projects_dir` with no trailing slash,
  the shipped shape, resolves to `<dir>/proj` and not `<dir>proj`.
- `test_a_projects_dir_with_a_trailing_slash_resolves_the_same`
- `test_each_resolve_failure_returns_its_line` — parametrised over the five lines above.

**Verify:** `.venv/bin/python -m pytest -q tests/test_ui.py && .venv/bin/python -m pytest -q`
Expected: green, `test_no_personal_paths_in_tracked_files` included.

**Commit:** `Find a run's repository on this machine from its worktree pointer`

---

### Task 11: `/api/diff`

The branch diff itself, run read-only in the repository Task 10 found. Two git commands, both on a
deadline, both cut on bytes, and a `stat` line for every outcome including the empty one.

**Delivers:** AC-2 (the `id` half), AC-15, AC-17, AC-18, AC-38

**Files:**
- Modify: `ui.py`
- Test: `tests/test_ui.py`

**Interfaces:**
- Consumes: `bad_param` (Task 8), `TemporalFeed.ledger` and `feed=` (Task 9), `resolve_repo` and
  `read_env(ROOT / ".env")` (Task 10).
- Produces:

```python
DIFF_CAP = 204_800     # bytes of patch
STAT_CAP = 20_480      # bytes of stat
DIFF_TIMEOUT = 20      # seconds per git command
def branch_diff(repo: Path, base_branch: str, branch: str) -> dict   # {"stat", "patch", "truncated"}
```

```
GET /api/diff?id=<wf> -> 200 {"stat": str, "patch": str, "truncated": bool}
```

Copy in `stat`, verbatim, with `patch` empty and `truncated` false. Task 10's five lines arrive
here unchanged; these are this task's own:

```
no ledger for this workflow
no rounds yet
branch not found: <name>
git took longer than 20s; nothing to show
already merged into <base_branch>; the branch adds nothing to it
no changes on this branch yet
diff failed: <first line of the error>
```

**Behaviour:**
1. `bad_param(id)` → 400. No feed, or `feed.ledger(id)` is `None` → `no ledger for this
   workflow`. Empty `rounds` → `no rounds yet`. Otherwise `worktree`, `branch` and `base_branch`
   come from `rounds[-1]`, never from the request.
2. `resolve_repo` on that `worktree`; a non-empty second element is the answer, as is.
3. `branch_diff`: `git rev-parse --verify` each branch (`branch not found`), then `git diff
   --stat <base>...<branch>` and `git diff <base>...<branch>`, `cwd=repo`, each as an argv list
   with no shell and each with `timeout=DIFF_TIMEOUT` (`git took longer than 20s` when it
   expires). Capture both as raw bytes; cut `stat` at `STAT_CAP` and `patch` at `DIFF_CAP`
   **before** decoding; decode each with `errors="replace"`; set `truncated` when the patch was
   cut.
4. An empty diff is a result, not a fault: when both commands ran and `stat` is empty, `stat`
   carries `already merged into <base_branch>` when the ledger's `status` is `merged`, and
   `no changes on this branch yet` otherwise.
5. Any exception anywhere → 200 with `diff failed`. Never a 500, never a traceback in the body.

**Constraints:**
- A merged run's three-dot diff is empty by construction, not broken: `merge_branch` merges the
  branch into its base with `--no-ff` and deletes neither, so the branch becomes an ancestor of
  the base and `A...B` holds nothing. That is the state of every run the owner approved, and the
  one they come back to look at, so behaviour 4 is what stops the pane going blank. Tell the two
  empty cases apart from the ledger the handler already holds; do not reach for `merge-base`,
  `log` or `status`, which AC-38 forbids and which are not needed.
- Only `rev-parse` and `diff` run. Nothing in the request picks a branch: `branch` and
  `base_branch` come from `rounds[-1]`, and extra query parameters are ignored, not honoured
  (AC-15).
- Give git the deadline, not the pipe. `activities/gate.py` carries the scar: a timeout that
  governed the drain instead of the process was silently not enforced and the activity ran twice.
  A hang is not an exception, so without `timeout=DIFF_TIMEOUT` behaviour 5 never fires and the
  browser fetch never fills.
- Cut bytes, then decode, exactly as `log_slice` does for `/api/log`. Cutting decoded text
  measures characters, so a heavily non-ASCII patch arrives at several times the stated cap;
  cutting bytes and then decoding strictly would raise instead, turning a large diff into `diff
  failed`. Diff bodies carry file content, and `activities/execute_round.py` documents non-ASCII
  filenames as ordinary here.

**Test intents:**
(fixture: a temp git repo with `main` and a branch `lg-x-ab12cd` adding a file; Task 10's pointer
and `.env` fixture; `monkeypatch.setattr(ui, "ROOT", tmp_path)`; a `FakeFeed` whose ledger's last
round names that worktree and both branches)
- `test_diff_names_the_changed_file_and_carries_the_patch`
- `test_diff_without_temporal_is_200_with_a_reason`
- `test_each_failure_is_a_200_with_its_line` — parametrised over `no rounds yet`, the five Task 10
  lines and `branch not found`.
- `test_a_large_patch_is_cut_at_the_cap` — a 300 KB file whose content is **not** all ASCII
  (repeat a multi-byte character), so bytes and characters differ and the assertion tells the two
  cuts apart; assert `len(patch.encode()) <= 204800` and `truncated` true.
- `test_a_merged_branch_says_it_is_merged` — the fixture repo with the branch merged into `main`
  and a ledger whose `status` is `merged`; HTTP 200, `patch` empty.
- `test_a_branch_with_no_commits_says_so` — the branch pointed at `main`, ledger `status`
  `running`.
- `test_extra_request_parameters_are_ignored` — `/api/diff?id=<wf>&branch=main&base=x` returns
  byte-for-byte what `?id=<wf>` alone returns.
- `test_the_diff_leaves_the_repository_alone` — the fixture repo's `HEAD` symbolic ref and both
  branch tips are identical before and after the request. Do not assert over the index or the
  working tree: an unrelated `git` call refreshes the index and makes that flaky.
- `test_diff_rejects_bad_ids`
- `test_the_dashboard_has_no_write_methods` — `ui.py` defines `do_GET` and no other `do_` method;
  the only `git` subcommands in its source are `rev-parse` and `diff`.

**Verify:** `.venv/bin/python -m pytest -q tests/test_ui.py && .venv/bin/python -m pytest -q`
Expected: green.

**Commit:** `Show a run's branch diff from the host copy of its repository`

---

### Task 12: Log panes that append instead of re-render

The first page task, and the one the whole polling story rests on. `poll()` stops rebuilding the
board; it fetches names, builds a card once per round it has not seen, and appends new bytes to the
panes that are open. From here the page renders logs again.

**Delivers:** AC-9 (log panes), AC-11 (page side), AC-12 (page side), AC-13, AC-14 (log panes)

**Files:**
- Modify: `ui.py` (`PAGE`: the `<script>` and the styles it needs)
- Test: `tests/test_ui.py`

**Interfaces:**
- Consumes: `/api/logs`, `/api/log` (Task 8); the injected `__LOG_RE__`.
- Produces the JavaScript naming contract every later page task obeys and the Python test binds to:
  - `runs()` and `poll()` stay the two interval functions: `setInterval(runs, 4000)` and
    `setInterval(poll, 2000)` appear verbatim.
  - A function that assigns `innerHTML` is declared with the `function` keyword and its name
    starts with `build`. It runs once per element it creates.
  - Functions that run on every poll are named `patch…`. They change `textContent`, attributes and
    classes, add or remove child elements, and may call `insertAdjacentHTML('beforeend', …)` to
    append and `replaceChildren()` to clear. They never assign `innerHTML`.
  - `buildLogPane(dir, name, label)` returns a collapsed pane element that remembers `dir`,
    `name`, its `offset`, and whether it is open. `patchRounds(names)` groups names into cards.
    `patchOpenPanes()` polls every open pane. Tasks 14 and 15 bind to all three.

**Behaviour:**
1. `poll()`: return when nothing is selected; fetch `/api/logs?dir=<sel>`; call
   `patchRounds(names)` then `patchOpenPanes()`. `patchRounds` groups names with the injected
   `LOG_RE` into keys `item <i> · round <r>` (or `round <r>` when the name has no item), calls
   `buildRoundCard(key)` once per new key, inserted so that keys sort newest first without moving
   existing cards, and inside each card calls `buildLogPane` once per role present, labelled
   `executor` for `executor` and `supervisor` for `audit`.
2. A collapsed pane sends nothing. Opening it starts polling from its stored offset (0 the first
   time); closing it stops. Reopening resumes from the stored offset with its text intact.
3. `patchOpenPanes()`: for each open pane fetch `/api/log?dir=&name=&offset=`. Replace the
   pane's contents (`replaceChildren()`, then append the whole text) when either signal fires:
   the reply's `offset` is lower than the one sent, or the reply's `head_truncated` is true while
   the pane's stored flag was false. Otherwise append `text`. Store `size` as the next offset and
   `head_truncated` as the pane's flag, on every reply including the first. Appends go through
   the existing `colorize()` via `insertAdjacentHTML('beforeend', …)`.
4. Stick rule: before appending, `stick = scrollHeight - scrollTop - clientHeight <= 4` (also
   true when the pane is empty); after appending, scroll to the bottom only if `stick`.
5. `no logs yet for this run` (existing copy) when no name matches. This line is temporary:
   Task 15 takes ownership of the empty state for `#rounds` and replaces it with `no rounds yet`.
   Do not leave both in the page.
6. Selecting a run clears the board (`replaceChildren()` on `#board`, in the click handler, not
   in `poll()`) before the first poll, so cards never belong to two runs at once.

**Constraints:**
- `sel` stays a directory string in this task; Task 13 turns it into `{id, dir}`.
- Prefer a `<details>` element per pane: native collapse, an `open` property and a `toggle`
  event, so there is no custom toggle state to get wrong.
- `insertAdjacentHTML('beforeend')` adds nodes and touches none that exist, so a text selection
  survives it; that is why it is allowed where `innerHTML` is not. Chunk boundaries fall on line
  ends because `append_log` writes whole lines.
- `new RegExp(<json>)` must survive: two existing tests assert on it.

**Test intents:**
- `test_innerhtml_lives_only_in_build_functions` — for every `innerHTML` in `ui.page_html()`,
  the nearest preceding `function <name>(` has a name starting with `build`; the text of `runs`,
  `poll` and every `patch` function contains none.
- `test_the_intervals_are_unchanged` — both `setInterval(...)` strings present verbatim.
- `test_the_page_polls_log_slices` — the page contains `/api/log?` and `offset=`.
- `test_the_page_replaces_a_pane_on_either_truncation_signal` — the page's `patchOpenPanes`
  source names both `head_truncated` and a comparison of the reply offset against the requested
  one, and calls `replaceChildren` under each.

**Verify:** `.venv/bin/python -m pytest -q tests/test_ui.py && .venv/bin/python -m pytest -q`, then a
manual check: run `lg ui`, select a run, open an executor pane, select some text, wait 6 seconds;
the selection is still there. Scroll up; wait; the position holds. Scroll to the bottom; new lines
keep it pinned. Close the pane; the browser's network panel shows no `/api/log` requests.
Expected: green, and the four manual observations.

**Commit:** `Append to open log panes instead of redrawing the board`

---

### Task 13: The run list is patched in place and keyed by workflow id

Rows stop being rebuilt every 4 seconds; each row is created once, then its text is updated. Two
workflows of one directory are two rows.

**Delivers:** AC-3 (page side), AC-14 (run list)

**Files:**
- Modify: `ui.py` (`PAGE`: `runs()` and helpers)
- Test: `tests/test_ui.py`

**Interfaces:**
- Consumes: `/api/runs` rows with `start_time`/`close_time` (Task 9); the naming contract (Task 12).
- Produces: `sel` becomes `{id, dir}` or `null`; `poll()` reads `sel.dir` for `/api/logs`.
  `buildRunRow(entry)` creates a row carrying `data-id`; `patchRuns(data)` updates rows.

Copy, verbatim: start time via the browser's `Date.toLocaleString()`; duration `Hh MMm` at an hour
or more, `Mm SSs` under it; a logs-only row shows no time text at all.

**Behaviour:**
1. `runs()` fetches `/api/runs` and calls `patchRuns`. For each entry, find the row by `data-id` or
   `buildRunRow` it, inserted at its position in the server order without moving existing rows.
   Update the directory text, the pill class and text, the detail, the time line. Remove rows
   whose id is absent from the reply.
2. Time line: `start_time` formatted, then the duration: to `close_time` when present, to now
   when the run is running. Recomputed on each poll (text only). Both `null` → empty.
3. Selection is by id: clicking a row sets `sel = {id, dir}`, moves the `sel` class, calls
   `buildBoard()` (Task 14 fills it; in this task it may only clear the board) and `poll()`.
   With nothing selected and rows present, select the first, as today.
4. Header text as today: `engine dashboard` or `temporal unreachable — logs only`.

**Constraints:**
- Do not sort or move existing rows. A reader with text selected in a row keeps it.
- The row shows `dir`; the id is an attribute. Two rows may show the same directory text, which
  is the point of AC-3.

**Test intents:**
- `test_the_page_reads_run_times` — page contains `start_time` and `close_time`.
- `test_innerhtml_lives_only_in_build_functions` (Task 12) still green with `runs()` rewritten.

**Verify:** `.venv/bin/python -m pytest -q tests/test_ui.py && .venv/bin/python -m pytest -q`, then a
manual check in `lg ui`: select text in a run row, wait 12 seconds (three polls); the selection
survives; a running run's duration ticks up; a finished run's does not.
Expected: green plus the manual observations.

**Commit:** `Patch the run list in place and select runs by workflow id`

---

### Task 14: The state board: status, why-no-state, awaiting, items

The board starts answering the question the page is opened for. Built once on selection, patched
every 2 seconds from `/api/run`.

**Delivers:** AC-4, AC-6, AC-7, AC-10 (reason line and hidden sections), AC-14 (board)

**Files:**
- Modify: `ui.py` (`PAGE`)
- Test: `tests/test_ui.py`

**Interfaces:**
- Consumes: `/api/run` (Task 9), `sel.id`/`sel.dir` (Task 13), the naming contract (Task 12).
- Produces: `buildBoard()` renders the fixed sections `#state`, `#why`, `#awaiting`, `#items`,
  `#rounds`, `#diff` (Task 16 fills it). The Task 12 log cards move from `#board` into `#rounds`
  in this task; Task 15 re-keys them and adds the ledger fields. `poll()` fetches `/api/run?id=`
  and `/api/logs?dir=` together and calls `patchBoard(run, names)`, which calls `patchState`,
  `patchAwaiting`, `patchItems` and Task 12's `patchRounds(names)`, the last now targeting
  `#rounds` instead of `#board`.

Copy, verbatim:

```
temporal unreachable — logs only
no workflow for this run
awaiting: <kind>
no card was sent; the lg approve command is the only way to answer
no items yet
```

**Behaviour:**
1. `patchState`: with a ledger, show `status` and, when present, `reason`. With `ledger` null,
   `#why` shows the first line when `temporal` is false, the second when true; `#awaiting`,
   `#items` and `#diff` are hidden (`hidden` attribute); `#rounds` stays visible and shows the
   log cards, which is the "log panes grouped by `LOG_RE`" AC-10 asks for.
2. `patchAwaiting`: hidden when the ledger has no `awaiting`. Otherwise `awaiting: <kind>`; the
   `question` text in an element with `white-space: pre-wrap`, hidden when the key is absent; one
   row per option `A — label` (rows keyed by letter, added and removed as they change);
   `answer_with` in its own `<code>` element styled `user-select: all`, so one click selects the
   whole command; the no-card line when `telegram` is false.
3. `patchItems`: rows keyed by `n`: `n`, the `item` text, `status`, then `commit` cut to 10
   characters for `done` or `reason` for `parked`. `no items yet` when `items` is empty **and**
   when the ledger has no `items` key at all — `/api/run` serves a closed workflow's recorded
   result untouched, and two runs on this machine closed before `items` existed, so
   `ledger.items.map(...)` is a TypeError on a real run the owner can click today.
4. The `awaiting` block disappears on the first poll after the workflow pops it (AC-6): that is
   step 2's "hidden when absent" and nothing else.

**Constraints:**
- `buildBoard()` runs on selection only. Every section exists after it, hidden or not, so patches
  never create sections.
- The two `#why` lines are the only place the page says why there is no state. Do not add a third.
- `question` is printed as recorded, location line included (AC-5); nothing reconstructs it.

**Test intents:**
- `test_the_board_fetches_the_ledger_by_workflow_id` — page contains `/api/run?id=`.
- `test_the_board_copy_is_pinned` — page contains each of the five copy lines and `user-select`.
- `test_the_page_and_lg_status_use_the_same_no_card_line` — load `lg` by path, call
  `format_status` on a ledger whose `awaiting.telegram` is false, take the no-card line out of
  its output, and assert that exact string is in `ui.page_html()`. The sentence is 62 characters
  written twice in two files with no shared constant; this is what keeps them equal.
- `test_innerhtml_lives_only_in_build_functions` (Task 12) still green.

**Verify:** `.venv/bin/python -m pytest -q tests/test_ui.py && .venv/bin/python -m pytest -q`, then:
`lg ui` on a run with a card up shows kind, question, options and the command; clicking the command
selects all of it. `.venv/bin/python -c "import ui; ui.serve(8401, None)"` on a second port shows
`temporal unreachable — logs only` with log cards and no state sections. When the owner next answers
a card, the block is gone within 2 seconds.
Expected: green plus those observations.

**Commit:** `Show a run's status, question and items before its logs`

---

### Task 15: Rounds from the ledger, with their log panes attached

Rounds render newest first from the ledger, and the log panes from Task 12 move inside them. A round
that is running right now has logs but no ledger entry yet, and still gets a card.

**Delivers:** AC-8, AC-9 (per-round panes), AC-10 (log cards without a ledger), AC-14 (rounds)

**Files:**
- Modify: `ui.py` (`PAGE`)
- Test: `tests/test_ui.py`

**Interfaces:**
- Consumes: `buildLogPane`, the grouping by `LOG_RE` (Task 12); `patchBoard` (Task 14).
- Produces: `patchRounds(rounds, names)`, extending Task 12's `patchRounds(names)`; round cards
  keyed `<item_no>-<round>` inside `#rounds`, replacing Task 12's header-string keys.

Copy, verbatim: card header `item <i> · round <r>`, with ` · in progress` appended while the
round has not finished; the verdict slot carries the verdict word, or `escalated`, or
`audit running`; `no rounds yet` when the ledger has no rounds and no log matches.

**Behaviour:**
1. Keys: a ledger round is `item_no-round`; a log name is `i-r` from `LOG_RE`. The **same**
   default applies on both sides: a missing item number means item `1`, whether it is a log name
   with no item group or a ledger round with no `item_no` key. One card per key, built once,
   newest first (higher item, then higher round), inserted without moving existing cards. Cards
   whose key vanishes are removed.
2. A card from the ledger shows: header; the verdict slot; each `verdict_reasons` entry on its
   own line; `files` one per line; `directive` when not null; `owner_question` and `owner_reply`
   when the round asked. All patched as text. The verdict slot is the `verdict` word when the key
   is there; with no `verdict` key it is `escalated` when `status` is `escalated` and
   `audit running` when `status` is `green` (AC-8). The word `green` never appears there.
3. Log panes: for each name whose key matches a card, `buildLogPane` inside that card once,
   collapsed. ` · in progress` is appended while the round has not finished, which is two states,
   not one: a card built from a log name whose ledger entry does not exist yet, and a ledger
   round with `status` `green` and no `verdict` — the audit, which can run for half an hour. The
   suffix goes when the verdict arrives.
4. With `ledger` null, cards come from names only: header and panes, no verdict fields. `#rounds`
   stays visible (Task 14). That is the logs-only view.

**Constraints:**
- Addition to the spec: the `in progress` card, and it has to cover both halves of a round's
  life. `_run_item` appends the round entry only after `execute_round` returns, so while the
  executor works the ledger has no row at all and its log grows — that is the first half. Then
  the entry is appended, `audit` is awaited for up to 30 minutes, and only then is `verdict` set,
  so a ledger round with `status` `green` and no `verdict` is a live round mid-audit — the second
  half. Without both, the live view the dashboard gives today disappears, and a mid-audit round
  prints its gate word where its verdict belongs.
- Two real runs recorded rounds with no `item_no` (`microbits-fact-corrections` and
  `microbits-ideas-sharpen`, both closed before commit 3f49df6 added the key), and both have
  old-shape `r1-executor.log` / `r1-audit.log` on disk. Without the shared item-1 default the
  ledger round keys `undefined-1` and the log names key `1-1`, and each round draws twice: one
  card headed `item undefined · round 1` with the verdict and no panes, one with the panes and a
  permanent ` · in progress` on a run that finished hours ago.
  `tests/test_review_fixes.py::test_the_old_log_names_still_render` exists to keep these runs
  rendering; do not regress them.
- `no rounds yet` replaces Task 12's `no logs yet for this run`. After this task `#rounds` is the
  only home for round cards, so the older line has no slot left and is deleted from the page —
  the pinned-copy rule forbids rewording either string, not choosing which of the two survives.
- Panes never move between cards. A key decides its card at creation.
- The diff is not under any round (AC-15): one branch per run, one diff, Task 16.

**Test intents:**
- `test_the_rounds_copy_is_pinned` — page contains `in progress`, `no rounds yet`,
  `audit running`, `verdict_reasons`, `owner_reply`, and no longer contains
  `no logs yet for this run`.
- `test_a_round_without_an_item_number_keys_to_item_one` — the page's key helper source applies
  the same fallback to a ledger round's `item_no` as to `LOG_RE`'s item group.
- `test_innerhtml_lives_only_in_build_functions` (Task 12) still green.

**Verify:** `.venv/bin/python -m pytest -q tests/test_ui.py && .venv/bin/python -m pytest -q`, then in
`lg ui`: a finished multi-item run shows its rounds newest first with verdicts and reasons; a round
with red gates shows `escalated`; a running run shows an `in progress` card whose executor pane
grows; selecting `run-2026-09-05-microbits-fact-corrections-36437d` shows one card per round, headed
`item 1 · round 1`, with both its panes and its verdict, and no card saying `item undefined` and
none stuck on `in progress`; on the Temporal-less port the same run shows log cards only.
Expected: green plus those observations.

**Commit:** `Render rounds from the ledger with their logs inside`

---

### Task 16: The diff pane, and the whole-branch check

One collapsed pane per run that fetches the branch diff each time it opens. Then the browser
checklist that AC-14 asks for, run once over the finished page.

**Delivers:** AC-9, AC-14, AC-15, AC-18, AC-37, AC-38

**Files:**
- Modify: `ui.py` (`PAGE`)
- Test: `tests/test_ui.py`

**Interfaces:**
- Consumes: `/api/diff` (Task 11); `#diff` from `buildBoard` (Task 14).
- Produces: `buildDiffPane(id)`; the pane fetches on each `toggle` to open and is not polled.

Copy, verbatim: pane label `diff`; `patch cut at 200 KB` when `truncated` is true.

**Behaviour:**
1. `#diff` holds one collapsed pane. Opening it fetches `/api/diff?id=<sel.id>` and shows `stat`,
   then `patch` in a `pre`, then the cut line when truncated. Closing shows nothing new; opening
   again fetches again.
2. `poll()` does not touch the diff pane. With `ledger` null `#diff` is hidden, alongside
   `#awaiting` and `#items` (Task 14).

**Constraints:**
- The `stat` text is also where Task 11's reason lines arrive; show it as is. No special-casing,
  and none is needed: after Task 11 `stat` is never empty, so an empty diff explains itself in
  the slot a real stat occupies. A pane that renders blank is a bug in Task 11, not a case to
  handle here.

**Test intents:**
- `test_the_diff_is_fetched_by_workflow_id` — page contains `/api/diff?id=` and
  `patch cut at 200 KB`.
- `test_innerhtml_lives_only_in_build_functions` (Task 12) still green.

**Verify:** `.venv/bin/python -m pytest -q` green (AC-37), then the browser checklist in `lg ui`:
1. Select text in a run row; wait 12 seconds; the selection survives.
2. Open a log pane on a running run; select text; wait 6 seconds; the selection survives.
3. Open the diff on a finished run; `stat` names files; open it again and the network panel shows
   a second `/api/diff` request and no request while it is closed.
4. Open the diff on a merged run (`run-2026-09-05-microbits-order-v3-ae4a5b`, ledger status
   `merged`): the pane says `already merged into <base>; the branch adds nothing to it` rather
   than rendering blank.
5. The network panel over 10 seconds shows only `/api/runs`, `/api/run`, `/api/logs` and
   `/api/log` for open panes; nothing else, and no request with a method other than GET.
Expected: all five hold.

**Commit:** `Show the branch diff on demand`
