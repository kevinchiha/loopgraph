# Changelog

What changed for people using loopgraph, newest first. Write your notes under
`## Unreleased` in the same commit as the change; `./release.sh X.Y.Z` renames that
heading to the version and date and opens a fresh `## Unreleased` above it.

## Unreleased

## 0.5.0 - 2026-09-09

- `lg update` now keeps the Claude Code skill current instead of leaving it to you.
  `~/.claude/skills/loopgraph` is meant to be a symlink into your checkout, and one
  that is follows every update by itself — but a link left behind by a checkout you
  moved, or one nobody ever made, went stale silently and stayed stale through every
  update after it. That is what made people ask their agent to go and copy the skill
  by hand. `lg update` now re-points a link that has come loose and prints what it
  did, with a reminder to restart your agent session so it loads. A directory you
  copied there yourself is never overwritten: it is named, along with what to remove,
  because your edits are not ours to delete. `./install.sh` repairs the same link on
  a re-run rather than shrugging at whatever is in the way.

## 0.4.0 - 2026-09-09

- Gates and detectors now run under bash with `pipefail`, so a pipeline is red when
  any stage fails. Before, only the last stage counted, and a gate like
  `pytest -q | tee log` could not fail. Two shapes go red that used to pass: a
  `| head` or `grep -q` after a chatty command, which now reports exit 141 because
  the command before it dies of SIGPIPE, and a `grep` that matches nothing, which
  exits 1; wrap a grep stage that may legitimately find nothing as
  `{ grep ... || [ $? = 1 ]; }`, braces included, because `||` binds looser than
  `|`. A sweep whose first pass reports nothing no longer says `converged`; it
  says no item ran and names the detectors as the thing to check, because a clean
  repo and detectors that match nothing look the same. The shipped scope gate
  template sets `pipefail` itself.

## 0.3.2 - 2026-09-09

- `lg start` refuses a path the container cannot reach, instead of starting a run
  that dies on it. The engine only ever sees two host directories: this checkout,
  as `/app`, and your projects tree, as `/projects`. Giving it the path your shell
  tab-completes — `/home/you/projects/thing` rather than `/projects/thing` — started
  a workflow that died seconds later on `FileNotFoundError`, reported to your phone
  as `engine failure:` with no hint of what was wrong. The refusal now happens
  before Temporal is contacted, and where the translation exists it hands it over:
  "Use /projects/thing instead." A mistyped repository name or run slug is the same
  error from the same git command, so it is caught in the same place.

## 0.3.1 - 2026-09-08

- A round is no longer killed for thinking. The executor and the supervisor tell
  Temporal they are still alive every 30 seconds for as long as a model call runs,
  where before the only thing that said so was the model producing output. More
  than three minutes of quiet reasoning blew the heartbeat deadline, so the round
  was killed, retried, killed again, and half an hour of finished implementation
  went with it each time, with nothing wrong with the work. The trade: a session
  that is genuinely stuck now runs to its outer limit, two hours for an executor
  round and half an hour for an audit, instead of dying in three minutes. And
  `HEARTBEAT` in a park reason has stopped meaning a quiet model. It now means the
  worker itself went away.

## 0.3.0 - 2026-09-08

- The dashboard says `waiting` on a run that is blocked on you, in the left rail and
  on the board, where it used to say `running`. `running` is also what it says while
  an executor works, which is most of a run's life, so the one run you had to answer
  looked like every other one. A run that has finished never says `waiting`, and its
  question, its options and its `lg approve` command come off the board with it: a
  run whose engine died keeps its last card in the record, and the page was still
  asking you to answer a run that was over.
- The browser tab says how many runs are waiting on you: `(2) loopgraph` with two of
  them, `loopgraph` with none. Its icon is a dot that turns blue at the same moment,
  so a dashboard left open in a background tab tells you a run needs you without you
  going and looking.
- You can tell one of the supervisor's reasons from the next. Each one starts at the
  left and its wrapped lines are indented under it, so eight reasons read as eight
  things and not as eleven lines running together — a reason that wrapped used to
  look exactly like a new reason starting.
- Sentences on the board stop at about 80 characters instead of running the full
  width of the window: the reasons, the rest of a round's fields, the question a run
  is asking and the line beside its status. The logs and the diff are untouched and
  still get the whole width.
- Round cards fold up. Each one is a box with an edge now, and a closed one is a
  single line — `ITEM 2 · ROUND 3 · ACCEPT · 4 FILES`, in the small heading type the
  board's other labels use — so a run of twelve rounds is twelve lines to read down
  instead of a page to scroll to the end of. The newest round is the one that opens;
  the rest you open and close yourself, and no poll ever changes what you left.
- The verdict word is coloured by what it means: green for `accept`, yellow while the
  supervisor is still out and on a `redo`, red on `escalated`. A round shows that word
  twice, once folded and once open, and both take the same colour, so you can read a
  column of folded cards and see which rounds went wrong without reading a word. A
  verdict the dashboard has never met keeps its plain colour and every letter of its
  text.
- Opening one of a round's two log panes gives it the room the closed one is not
  using — about 930px of a 1440px window instead of 516. Open both and they go back
  to sharing the row.
- The run you are reading is named at the top of the board, with its status and how
  long it has been going, and it stays there while you scroll. Three screens into a
  long run the only thing telling you which run it was is the highlight in the rail,
  off to the left where you are not looking. A run known only from its log files gets
  its name and nothing else, because there is no status or duration to give it.
- The board no longer says `no workflow for this run` about a run that plainly has
  one. That line was covering two different things: a directory of log files with
  nothing behind it, and a run Temporal knows perfectly well whose state just did not
  come back this time. The second now says `ledger did not answer this poll; the run
  is still there` and keeps its status, instead of denying the run named right above
  it.
- A run has an address. Clicking one puts its id in the URL, so reloading the page
  comes back to the run you were reading instead of the top of the rail, and the URL
  is something you can bookmark or send to someone else. A hash naming a run that is
  not there, or one that is not a valid URL at all, opens the top run as before.
- Times in the rail read as `14m ago` and `3h ago` for the first day, and go back to
  the date after that. Hover one for the full timestamp, whichever way it is showing.
- A run can be taken off the rail. The run you are reading has `archive` at the end
  of the line naming it at the top of the board; click it and the run goes there and
  then. `archived (3)` in the header says how many you have hidden — tick it and they
  come back, each one marked `archived`, with the same button now offering to put one
  back. The dashboard has also stopped opening on a run you have archived: with no
  run in the URL it picks the first one you can actually see, which is the point of
  hiding the newest finished run in the first place.
- Nothing is hidden until the dashboard has said yes. It will not hide a run that is
  still working, or one it cannot ask Temporal about — a run that vanished mid-round
  is a run you stopped watching without meaning to — and when it refuses you get the
  reason in red beside the button and the run stays where it was. Putting a run back
  is never refused, Temporal up or down, which is when you are most likely to be
  tidying up. The list of hidden runs lives in `runs/.archived.json` and survives a
  restart. This is the only thing the dashboard writes; everything else it serves
  still only reads.
- `lg rm <run>` takes a run away for good. It deletes the run directory — the logs,
  both agents' transcripts, the gates, the throwaway worktrees — and takes that run's
  rows off the dashboard's rail at the same time, so a finished run goes in one move
  instead of being hidden now and cleared up later. It refuses while any workflow of
  that run is still open, saying which one and whether it is running or waiting on
  you, and it refuses while Temporal is down, because then it cannot tell. Otherwise
  it prints what is about to go and asks you to type the run's name back before it
  touches anything; `--yes` skips that question and nothing else. It will not delete
  anything outside `runs/`, whatever you point it at. And if the directory will not
  delete — a read-only disk, a file something else has open — it says so and leaves
  the run on the dashboard, instead of reporting a removal that did not happen.
  There is no undo.
- Taking a run away unhooks its worktrees from your project repositories one at a
  time, each named by the exact path its repository wrote down. A second run working
  the same project keeps its worktree and goes on running, and no branch is deleted
  anywhere — the work is still there to look at. A worktree whose repository has
  moved off this machine is named in a line saying so, and stops nothing else.
- Documented `ANTHROPIC_DEFAULT_HAIKU_MODEL` for people whose proxy serves no
  Claude models. Nothing in the engine changed; headless runs only ever ask for
  `ANTHROPIC_MODEL`, so this is the escape hatch if one ever asks for more.
- **Fixed: the example scope gate passed everything.** `runs/example-hello/check-write-set.sh`
  is the file the README and the skill both tell you to copy when you want a run kept
  inside its write set. It said `#!/bin/sh` while using `read -d`, which only bash has,
  so under the worker container's shell its loop died on the first line, the check never
  ran, and it printed `write set in scope` and passed whatever had changed. Every run
  built from it had a scope gate that could not fail, and read green in the ledger for
  it. The engine's own learning edge had found this five times over and written it into
  a constraints file; nothing acted on it, because a constraint is advice to one run.
  The script now says `#!/bin/bash`, and the suite fails if that line ever changes back.
- The skill and the README now tell you to prove a gate can go **red** before you trust
  it, rather than only that it exits 0. A gate that cannot fail reads exactly like a
  gate that passed, in your terminal and in the ledger both, which is how the one above
  survived. They also warn that `lg start` follows the run instead of exiting, so a
  backgrounded one that has not printed yet looks just like a failure and a second go
  gives you two runs on one repository; and that `lg ui` binds to Temporal once when it
  starts, so a dashboard opened before Temporal shows every run as `unknown` until you
  restart it.

## 0.2.0 - 2026-09-07

- Sweep runs: a `run.yaml` with detectors makes the engine remove what they report,
  one path group per item, until two passes in a row come back at or under the
  floor, or the deadline or the item cap lands first.
- Convergence items: once a run has added enough code, the engine inserts an item
  that only removes; its commit is refused if it adds more lines than it removes.
- `lg version` says which release you have and whether origin has a newer one.
- `lg update` moves a clean `main` to the newest release, refreshes the host venv
  when the dependencies changed, and restarts the worker and dispatcher. It refuses
  while a run is open or Temporal is down.
- The dashboard header shows the installed release and points at `lg update` when a
  newer one exists.
- `release.sh` for maintainers: checks, changelog, version bump, tag, push.
