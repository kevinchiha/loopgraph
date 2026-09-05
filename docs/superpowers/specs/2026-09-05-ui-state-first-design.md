# UI: state-first read-only dashboard

## Goal

Opening `lg ui` tells you what a run is doing, what it is asking you, and what it changed,
without reading a wall of streaming log text; Telegram cards and `lg status` say where in the
run they are speaking from.

## Scope

- `ui.py` — the page, its endpoints, and the polling behaviour. Most of the work.
- A shared `.env` reader in a module both `lg` and `ui.py` import (`lg` has no `.py`
  extension, so `ui.py` cannot import it), so the dashboard finds the host's projects
  directory without a second copy of the parser.
- `workflows/run.py` — record the pending question's text in the ledger, and put a location
  line into the text a card carries. Ledger contents and card text only: no new activity, no
  changed activity call.
- `activities/notify.py` — the pure helper that formats that location line.
- `lg` — `lg status` prints a readable summary, accepts a run-directory slug as well as a
  workflow id, and keeps today's raw output behind `--json`.
- `README.md`, `skills/loopgraph/SKILL.md` and `lg`'s own `--help` — the four lines that
  teach `lg status`, so the slug form reaches the owner and every agent that reads the skill
  from another project.
- Tests: `tests/test_ui.py` changes with the endpoints; `tests/test_visibility.py` gains the
  location-line tests next to the other card-text tests; the `.env` tests already in
  `tests/test_review_fixes.py` and `tests/test_release.py` keep passing untouched; new tests
  cover the status formatter, the slug resolver and the shared `.env` reader.

## Acceptance criteria

**The ledger reaches the page**

- **AC-1** `GET /api/run?id=<workflow-id>` returns `{"ledger": <object|null>, "temporal":
  <bool>}`. The ledger comes from the `ledger` query for a running workflow and from the
  workflow's result for a closed one, the same fallback `TemporalFeed._runs` uses today.
  `temporal` means what it means on `/api/runs`: the feed is connected. With Temporal
  unreachable, or an id Temporal does not know, `ledger` is `null` and the status is still
  HTTP 200, so the page works from log files alone.
- **AC-2** Every endpoint that takes `id`, `dir` or `name` answers HTTP 400 when the value is
  empty or contains `/` or `..`, the way `/api/logs` rejects `dir` today. `/api/log` answers
  400 when `offset` carries a value that is not a string of digits. An `offset` that is absent,
  or present with an empty value (`&offset=`, which `parse_qs` drops before the handler sees
  it), means 0.
- **AC-3** Each entry in `/api/runs` carries `start_time` (ISO 8601, UTC) and `close_time`
  (the same, or `null` while running), read from `WorkflowExecution.start_time` and
  `close_time`. A run known only from its log files carries `null` for both. Each row on the
  page shows the start time and a duration (elapsed for a running workflow, start to close for
  a finished one); a logs-only row shows no time rather than a wrong one. Two workflows of the
  same run directory are two rows, and selecting a row selects by workflow id, not by
  directory.

**What the page shows**

- **AC-4** When the selected run's ledger has an `awaiting` block, the page shows
  `awaiting.kind`, the text of `awaiting.question`, one line per letter in `awaiting.options`
  with its label, and the `awaiting.answer_with` command in its own element so one gesture
  selects the whole command. When `awaiting.telegram` is false the block says no card was sent
  and the command is the only way to answer. When the block has no `question` key (a workflow
  that started before this change) the page shows everything else and leaves the question out.
- **AC-5** `_await_decision` records `awaiting.question` in the same statement that builds
  `awaiting` today (`kind`, `options`, `telegram`, `answer_with`). Its value is the exact
  string passed as `summary` to `send_card` for that card. Nothing in `ui.py` or `lg`
  reconstructs a question from any other field.
- **AC-6** The workflow already pops `awaiting` once the owner answers. The page drops the
  block on its next poll, so within one poll interval (2 seconds) of an answer no question is
  shown.
- **AC-7** The page lists every entry of `items` with its `n`, its `item` text, its `status`
  (`pending`, `running`, `done` or `parked`), the first 10 characters of `commit` for a done
  item, and `reason` for a parked one. While `items` is empty (before `load_work_items` has
  returned) the section says `no items yet`. A ledger with no `items` key at all — the shape a
  workflow that closed before the key existed returns, and what two runs on this machine return
  today — is read as an empty list, not an error.
- **AC-8** The page lists `rounds` newest first. Each shows `item_no` and `round`, `verdict`
  as a word, each entry of `verdict_reasons` on its own line rather than a JSON blob, `files`
  one per line, `directive` when it is not null, and `owner_question` with `owner_reply` when
  the round asked the owner. A round can be missing its `verdict` key for two different
  reasons and the page tells them apart. `status` `escalated` means the gates stayed red and no
  audit ran: the word `escalated` goes in the verdict's place. `status` `green` means the audit
  is still running — the workflow appends the round entry, then starts the audit, then fills the
  verdict in — and the words `audit running` go there instead. The gate word `green` is never
  printed where a verdict belongs. A round recorded before `item_no` existed is shown as item 1,
  the same default a log name with no item number gets.
- **AC-9** The executor log and the supervisor log of each round, and the run's diff, are
  collapsed by default. A collapsed pane sends no request. An open log pane polls `/api/log`
  with its offset every 2 seconds and stops when closed. The diff is fetched each time its
  pane is opened and is not polled.
- **AC-10** When `/api/run` returns a null ledger, the page still shows the log panes grouped
  by `LOG_RE`, exactly as a run directory with logs and no workflow is shown today, plus one
  line saying why there is no state: `temporal unreachable — logs only` or `no workflow for
  this run`. It shows no items, rounds or awaiting sections.

**Polling that does not fight the reader**

- **AC-11** `GET /api/log?dir=<slug>&name=<file>&offset=<n>` returns `{"text": <str>,
  "offset": <int>, "size": <int>, "head_truncated": <bool>}`. `text` is the file from `offset`
  to its end, `offset` in the reply is the position `text` starts at, `size` is the file's
  current length, which the page sends as its next `offset`, and `head_truncated` is true when
  the file now begins with the 25-byte marker `append_log` writes when it cuts a file's head,
  `[... head truncated ...]` and a newline. Two separate signals tell the page to replace a
  pane's text rather than append to it, and it needs both. A reply `offset` lower than the one
  requested means the requested offset was past the current size. `head_truncated` turning true
  on a pane that last saw it false means the cut landed *below* the pane's stored offset —
  `append_log` keeps the last 500 KB of a 1 MB file, so an offset under 500 KB still looks
  valid while every byte position in the file has moved, and no size check can catch it.
  `name` must match `LOG_RE`, else 400. A name that matches but names no file gets 404.
- **AC-12** `GET /api/logs?dir=<slug>` returns `{"logs": [<name>, ...]}`: the run's log file
  names, sorted, and no content. The page groups the names with the `LOG_RE` that
  `page_html()` injects, so `test_the_injected_pattern_survives_javascript` stays as it is and
  `test_logs_endpoint` changes to assert names only.
- **AC-13** After a log pane updates, it is scrolled to the bottom only if it was within 4
  pixels of the bottom before the update. A reader who scrolled up keeps their position.
- **AC-14** A poll whose data is unchanged replaces no DOM node. The interval functions
  (`runs()` and `poll()` today) never assign `innerHTML` to an element that already exists;
  they change text and attributes and add or remove rows for runs and rounds that appeared or
  disappeared. The board is built with `innerHTML` only on first render and when the reader
  selects another run. Two checks: `ui.page_html()` contains no `innerHTML` assignment inside
  the polling functions, and text selected in a log pane or a run row survives three polls in
  a browser.

**The diff**

- **AC-15** `GET /api/diff?id=<workflow-id>` returns `{"stat": <str>, "patch": <str>,
  "truncated": <bool>}`: `git diff --stat <base_branch>...<branch>` and `git diff
  <base_branch>...<branch>`, run read-only in the host's copy of the target repository.
  `base_branch` and `branch` come from the last entry of the ledger's `rounds`, never from the
  request. It is one diff per run: every round of a run shares one branch and one worktree
  (`execute_round` derives both from the run token), so the page shows the diff once, not
  under each round.
- **AC-16** The host repository is found from the last round's recorded `worktree`, a
  container path of the form `/app/runs/<slug>/worktrees/<token>`. Replacing `/app/` with the
  engine root gives the pointer file `runs/<slug>/worktrees/<token>/.git`, which holds one
  line, `gitdir: /projects/<path>/.git/worktrees/<token>` (the shape every live run directory
  holds today). The repository is the part before `/.git/worktrees/` with its leading
  `/projects/` replaced by `<LOOPGRAPH_PROJECTS_DIR>/` — first occurrence only, anchored at the
  start of the string, and with that trailing slash. The slash is the whole point: the shipped
  value has none of its own (`.env.example` writes `/home/you/projects`, and `install.sh` does
  not add one), so dropping it turns `/projects/deye` into `<dir>deye` and every diff on the
  machine answers `repository not found`.
- **AC-17** Each of these answers HTTP 200 with a one-line explanation in `stat`, an empty
  `patch` and `truncated` false, never a stack trace and never a 500: no ledger; a ledger with
  no rounds yet; a `worktree` that does not start with `/app/`; a missing pointer file; a
  pointer file without a `gitdir:` line; `LOOPGRAPH_PROJECTS_DIR` unset or `.env` absent; a
  repository directory that is gone; either branch failing `git rev-parse --verify`; a git
  command that ran past its deadline; a diff that ran and came back empty because the branch is
  already merged into its base; a diff that ran and came back empty because the branch has no
  commits yet. The last two are the ordinary end state, not a fault: `merge_branch` merges the
  run's branch into its base, which makes the branch an ancestor of the base and the three-dot
  diff empty by construction, so every run the owner reviews after approving it lands there.
  The two are told apart by the ledger the handler already holds — `status` `merged` means the
  first, anything else the second — and never by a third git command.
- **AC-18** Both git commands are given a 20-second deadline and are killed when they pass it.
  `stat` is capped at 20 KB (20,480 bytes) and `patch` at 200 KB (204,800 bytes). Both cuts are
  made on the raw bytes, and the result is decoded afterwards with `errors="replace"`, so a
  multi-byte character split at the cut cannot raise and cannot turn a large non-ASCII diff into
  a failure. Past the patch cap `truncated` is true.
- **AC-19** `LOOPGRAPH_PROJECTS_DIR` reaches `ui.py` through one shared `.env` reader that `lg`
  also uses. The reader takes the path of the file to read and keeps `lg._dotenv`'s behaviour:
  it strips a matching pair of single or double quotes, drops a leading `export `, cuts an
  inline ` #` comment from an unquoted value, skips blank lines, `#` lines and lines without
  `=`, and returns `{}` when the file is absent. `lg._dotenv()` keeps its name and reads
  `<lg.ROOT>/.env` through the shared reader, so `test_lg_reads_env_the_way_compose_does`
  (`tests/test_review_fixes.py`) and both `test_lg_where_*` tests (`tests/test_release.py`)
  pass unchanged. No home directory appears in any tracked file, so
  `test_no_personal_paths_in_tracked_files` stays green.

**Telegram and lg**

- **AC-20** The location line reads `item 2 of 3 · round 2` on a decision card sent while an
  item is running, and `item 2 of 3` with no round on the cards sent between items or at the
  end: the parked note names the parked item; the stopped note names the item the run stopped
  on (`item 3 of 3` when every item was parked); the merge-ready card names the last item,
  `item 3 of 3` for a three-item run. The first line of the `summary` handed to `send_card`
  begins with the location line. The header lines `build_card_text` writes (`loopgraph:`,
  `run:`, `workflow:`, `commit:`) stay first and unchanged, so `wf_from_card` keeps routing
  replies and `test_the_card_carries_the_id_the_routing_reads_back` stays green.
- **AC-21** A pure function in `activities/notify.py` builds the line from an item number, a
  total and an optional round number, and `tests/test_visibility.py` tests both shapes.
  `build_card_text` itself does not change.
- **AC-22** The text recorded as `owner_question` on the round entry and passed as `question`
  to `record_owner_answer` stays the bare supervisor question with no location line, so
  `owner-answers.md` reads as it does today.
- **AC-23** `lg status <arg>` on a `LoopGraphRun` prints a readable summary from the ledger:
  the `status` and, when present, `reason`; the `awaiting` block as kind, question, one line
  per option, the `answer_with` command and, when `awaiting.telegram` is false, the same
  no-card line the page shows (AC-4), word for word, so the two surfaces cannot drift; each item
  as number, status and commit or reason; each round as item, round and verdict word, or, when
  it has no verdict, the replacement AC-8 defines: `escalated` for a red-gate round, `audit
  running` for one whose audit has not come back. A ledger whose `awaiting` has no `question`
  prints `question not recorded` in its place. A
  workflow that answers the `status` query instead (`GateCheckRun`, `RoundRun`) prints JSON as
  today.
- **AC-24** `lg status <arg> --json` prints exactly what `lg status` prints today:
  `json.dumps(result, indent=2)` of the first query that answers. The optional positional
  `query` (`lg status <id> ledger`, as README.md and the skill document it) also still prints
  JSON as today.
- **AC-25** The summary is produced by a pure function in `lg` over a ledger dictionary,
  tested (loading `lg` by path as `tests/test_release.py._lg` does) against: items, rounds with
  verdicts and an `awaiting` block with a question; an `awaiting` block without a question; an
  `awaiting` block with `telegram` false; no `awaiting`; empty `items` and `rounds`; a ledger
  carrying neither an `items` nor a `rounds` key, which is what a workflow closed before those
  keys existed hands back; a round with no `verdict` and `status` `escalated`; a round with no
  `verdict` and `status` `green`.
- **AC-26** When the argument is a workflow id and the `ledger` query raises on a closed
  workflow, `lg status` falls back to the workflow's result, as `TemporalFeed._runs` does. A
  workflow that Temporal reports as running, and one whose status Temporal does not report at
  all, both count as running: the fallback is skipped rather than blocking the terminal on a run
  that is waiting for its owner. When the last query tried fails and nothing answers — whichever
  query that was, the default `ledger` or the optional positional one — `lg status` prints the
  error's first line on stderr and exits 1, with no traceback.
- **AC-27** The argument is used as a workflow id first, exactly as today, with no lookup.
  Only when Temporal answers that no such workflow exists does `lg status` treat it as a
  run-directory slug. `lg status run-2026-09-05-deye-pending-restore-ab12cd` therefore behaves
  as it does now.
- **AC-28** As a slug, the argument's last non-empty path segment is used, so
  `2026-09-05-deye-pending-restore`, `runs/2026-09-05-deye-pending-restore` and
  `runs/2026-09-05-deye-pending-restore/` all name the same run. It resolves to the workflow
  whose id is `run-<slug>-<token>`, the shape `cmd_start` builds: the id starts with
  `run-<slug>-` and the remainder holds no `-`. That is the inverse of
  `wf.id[4:wf.id.rfind("-")]` in `ui.TemporalFeed._runs`, so `run-foo-` never matches
  `run-foo-bar-ab12cd`. Among several matches the newest by `WorkflowExecution.start_time`
  wins.
- **AC-29** When a slug matches no workflow, `lg status` prints one line on stderr naming the
  slug and the prefix it tried (`no workflow for 2026-09-05-nothing; looked for ids starting
  run-2026-09-05-nothing-`) and exits 1, with no Temporal traceback.
- **AC-30** When a slug matches more than one workflow, `lg status` uses the newest and prints
  one line on stderr saying so, `using run-<slug>-ab12cd (newest of 2 for <slug>)`, in both
  plain and `--json` mode. stdout carries only the summary or the JSON.
- **AC-31** The resolution is a pure function in `lg` taking the argument and a list of
  (workflow id, start time) pairs and returning the chosen id and the number of candidates, or
  no id. Tests cover: the argument equal to a listed id; a bare slug; `runs/<slug>` and
  `runs/<slug>/`; a slug that is a prefix of another run's slug matching only its own
  workflows; two matches picking the later start time; no match.
- **AC-32** The three places that teach `lg status` say the slug form exists: `lg status
  --help` names the argument as a workflow id or a run directory instead of `workflow_id`, and
  `README.md` and `skills/loopgraph/SKILL.md` show a run-directory slug in their examples. The
  id form they showed before keeps working (AC-24, AC-27), so nothing they taught becomes
  wrong.

**A live run keeps replaying**

- **AC-33** Every activity registered in `worker.py` keeps its name, parameter list and
  defaults. `send_card` stays `(kind, wf_id, run_dir, summary, commit, options,
  expect_reply=True)`. No activity is added to or removed from the worker.
- **AC-34** `LoopGraphRun` schedules the same activities, in the same order, with the same
  argument counts as today. In `_await_decision` that is `telegram_configured` then
  `send_card` with six arguments; in `_note`, `telegram_configured` then `send_card` with
  seven. The changes to `workflows/run.py` are limited to the contents of the `awaiting` dict,
  the strings passed as `summary` and `text`, and the parameter lists of the class's own
  methods (`_ask_owner`, `_park_note`, `_stopped_note`, `_owner_card`). Check: replay every
  `LoopGraphRun` history Temporal holds with `temporalio.worker.Replayer` over fetched history
  (`handle.fetch_history()`, read-only), once before the change and once after, and compare the
  two runs workflow id by workflow id. No history that replayed clean before the change may fail
  after it. A pass count is not the check and neither is "no error": some histories already fail
  on drift that predates this phase — `run_baseline` entered the workflow in commit e23766d, so
  every run started before it fails on its first activity — and the set of histories grows as the
  engine runs. On 2026-09-05 Temporal held 13 `LoopGraphRun` histories, 6 replaying clean and 7
  already failing, and none of them was running, so a check that filters on a running workflow
  finds nothing to replay. The running stack is not restarted for this.
- **AC-35** `workflows/run.py` still reads no clock, environment or random source and does no
  I/O. Its import list is unchanged: `timedelta`, `workflow`, `RetryPolicy`,
  `ApplicationError` and the activities it already imports.
- **AC-36** Every ledger key the dashboard and `lg` read today keeps its name and meaning:
  `status`, `reason`; `items[]` with `n`, `item`, `status`, `commit`, `reason`; `rounds[]`
  with `item_no`, `round`, `status`, `verdict`, `verdict_reasons`, `files`, `directive`,
  `worktree`, `branch`, `base_branch`; `awaiting` with `kind`, `options`, `telegram`,
  `answer_with`. `question` is the only key added.

**Whole-branch**

- **AC-37** `.venv/bin/python -m pytest -q` is green.
- **AC-38** The dashboard stays read-only. It handles `GET` only; any other method gets the
  stdlib handler's error status and reaches no code of ours. No endpoint writes a file,
  signals a workflow, or runs a git command other than `rev-parse` and `diff`.

## Non-goals

- **Answering a run from the browser.** The owner chose to keep the dashboard read-only, and
  `AGENTS.md` has a standing rule that an answer reaches a run one way only, as a signal from
  the dispatcher or from `lg approve`. A second way in has caused four separate bugs.
- **`lg ls` or any run lister.** Offered and not picked. The slug form of `lg status` is a
  lookup on an argument the owner already types, not a listing.
- **File-change detail on the merge card.** Offered and not picked; the diff is on the page
  instead.
- **Starting, stopping or editing runs from the browser.** Out of scope for a read-only page.
- **Getting the new workflow code into the running worker.** `worker.py` imports
  `LoopGraphRun` and registers it at process start, so nothing this phase changes in
  `workflows/run.py` reaches the live engine until the worker is restarted. No task restarts it,
  and the branch is proven by replay alone (AC-34). The owner restarts the worker when they
  choose, at a moment when no run is holding a card.
- **Rescuing runs already waiting when this ships.** A worker restart replays workflow code,
  so a run holding a card from before the change may need answering or terminating by hand.
  This phase notes the risk and does not migrate anything.
- **A location line on the `not an answer` and `discard failed` notes.** They answer the
  owner's last message, not a place in the run, and `_await_decision` would need the item and
  round threaded through for a note nobody asked for.
- **Per-round or per-item diffs** (`git show` of a checkpoint commit). The branch diff is what
  the merge card merges. A per-commit view is another phase.
- **Filling `awaiting.question` into workflows already waiting.** Workflow state cannot be
  edited from outside. The page and `lg status` say the question was not recorded.
- **Restarting the worker to prove replay works.** The engine is live and holds owner cards
  right now, and this phase's own live-run rule — stated in the plan's Global constraints and in
  the pipeline's safety block — forbids restarting the stack or the worker. `AGENTS.md` carries
  no such rule today, so do not go looking for it there. AC-34 replays exported history with the
  SDK's replayer instead.

## Decisions

- **State first, logs second.** Alternative: keep today's two log columns with a thin state
  strip above. Rejected because the state answers the question you open the page for, and the
  logs are the follow-up, not the headline.
- **Read-only stays read-only.** Alternative: a POST endpoint that sends the `decide` signal.
  Rejected by the owner, and it would add the second way in that `AGENTS.md` forbids.
- **The location line travels inside the card's text, not as a new `send_card` argument.**
  Alternative: add a parameter to the activity. Rejected because changing an activity's
  argument list is the change most likely to strand a run that is already waiting when the
  worker restarts with new code.
- **One shared `.env` reader.** Alternative: a second copy of the parser inside `ui.py`.
  Rejected because the existing reader carries a quote-stripping fix whose absence caused a bug
  that was hard to trace, and two copies drift apart.
- **The diff is found through the worktree's `.git` pointer.** Alternative: record the host
  repository path in the ledger. Rejected because the ledger holds container paths by design,
  and the pointer file is already on disk and already names the project.
- **`lg status` takes a workflow id or a run-directory slug.** The owner's choice, replacing
  an earlier id-only line. The workflow id is printed once, by `lg start`, and then it is
  gone, while the run directory name is the thing the owner already knows. A slug that matches
  several workflows takes the newest and says which id it picked, so the owner never guesses
  which run they are reading.
- **Slug resolution runs only after Temporal says the id does not exist** (auto-resolved). A
  real id costs no extra call and behaves exactly as today. Alternative: decide by shape, a
  `run-` prefix meaning id. Rejected because a run directory could carry that prefix.
- **`lg status` falls back to the workflow result on a closed run** (auto-resolved). The slug
  lookup will often land on a closed workflow, and the `ledger` query can fail on closed runs;
  `ui.py` already carries this fallback for that reason.
- **Endpoints key on the workflow id, not the run directory** (auto-resolved). Two runs of
  one directory are two workflows, and AC-3 exists to tell them apart; a `dir` parameter
  cannot. Logs still go by `dir`, because both runs write the same log files.
- **`temporal` in `/api/run` means the feed is connected** (auto-resolved), the same as on
  `/api/runs`. The draft returned false for an unknown id too, which would make the page say
  Temporal was down when it was up.
- **One diff per run, not per round** (auto-resolved). All rounds share one branch, so a
  per-round branch diff does not exist. Alternative, `git show` of each accepted round's
  commit: a different thing from what merges, and another phase.
- **`awaiting.question` is the card's summary string, verbatim** (auto-resolved). Alternative:
  the bare question plus a separate location field. Rejected because one string is what the
  owner saw on their phone, and the page and `lg status` print it as is.
- **End-of-run cards say `item N of N`; a stopped note names the item it stopped on**
  (auto-resolved). The draft said those cards name "the item and the total"; this pins which
  item. One format, one function.
- **`/api/logs` returns a list of names** (auto-resolved). The dict of name to text was the
  content the endpoint no longer sends.
- **`/api/log` answers 404 for a missing file and 400 for a name outside `LOG_RE`**
  (auto-resolved). Honest statuses, and the page only asks for names it was given.
- **The shared reader takes a file path; `lg._dotenv()` stays as a wrapper** (auto-resolved).
  The existing tests monkeypatch `lg.ROOT` and call `lg._dotenv()`; keeping the name keeps
  those tests as they are.
- **Replay is proven with the replayer, not a worker restart** (auto-resolved). The engine is
  live and holds owner cards, and this phase's live-run rule forbids restarting the stack or the
  worker; fetched history is read-only, so the replayer proves the same thing and touches
  nothing. The rule lives in the plan's Global constraints and the pipeline's safety block, not
  in `AGENTS.md`.
- **A replay proof is a before-and-after comparison, not a pass count** (auto-resolved).
  Alternative: replay a few histories and require no error. Rejected because 7 of the 13
  histories on this machine already fail on drift older than this phase, so "no error" cannot be
  met and a count moves on its own as new runs finish.
- **An empty diff explains itself** (auto-resolved). Alternative: leave the pane blank, since
  the diff command succeeded. Rejected because a merged run is the one an owner browses most and
  a blank pane cannot be told apart from a broken endpoint.
- **A verdict-less green round says `audit running`** (auto-resolved). Alternative: print the
  `status` word for every round with no verdict. Rejected because `green` is a gate word, not
  one of the verdict words (`accept`, `stop`, `plan`, `ask`), and reads as an acceptance that
  has not happened.
- **The head-truncation signal rides on the reply, not on the size** (auto-resolved).
  Alternative: keep `offset > size` as the only signal. Rejected because `append_log` keeps the
  last half of the cap, so a stored offset under 500 KB still looks valid after a cut and the
  pane would append bytes from the wrong place with no visible seam.
- **The documentation changes with the command** (auto-resolved). Alternative: leave
  `README.md` and the skill on the id-only form, since it keeps working. Rejected because the
  skill is what every agent in every other project reads, so a slug form nobody is told about
  ships to nobody.
- **Both git commands get a 20-second deadline** (auto-resolved). Alternative: rely on the
  200 KB cut, which happens only after both commands have finished. Rejected because a cut
  applied afterwards bounds what the browser receives and bounds nothing about the request.
- **The patch cap is 200 KB** (auto-resolved: large enough for any real review, small enough
  that one request cannot stall the page).
- **Poll intervals stay as they are**, 2 seconds for logs and 4 for the run list
  (auto-resolved: unchanged from today; the win comes from sending less per poll, not from
  polling less often).

## Open questions

None.
