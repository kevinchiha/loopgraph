# loopgraph, for agents

Three different jobs, three different places to look. Find yours first.

**Installing loopgraph for the user** — read
[INSTALL_WITH_AGENT.md](INSTALL_WITH_AGENT.md) and follow it. That is the supported
install path. It tells you what to collect, what to run, and how to prove it
worked. Do not improvise an install out of the README or this file: `install.sh`
handles a dozen machine differences you will not think of, and a hand-rolled setup
produces bug reports nobody can reproduce.

**Running a task through the engine from another project** — use the skill in
`skills/loopgraph/`.

**Changing the engine's own code** — the rest of this file.

## Layout

- `workflows/run.py` — Temporal workflows. The orchestration between rounds.
- `activities/` — everything with a side effect: executor, gates, audit,
  checkpoint, learning, notifications, and the record of what the owner answered.
- `graphs/round_graph.py` — the LangGraph loop inside one round.
- `lg` — the host CLI: `start`, `status`, `approve`, `where`, `version`,
  `update`, `ui`, and `lg rm`, the only one that deletes anything — a run
  directory, its worktree registrations, and its rows off the dashboard's rail.
  It refuses while a workflow of that run is open, or while Temporal cannot be
  asked. No `.py` extension, so tests load it by path.
- `dispatcher.py` — the only process that reads Telegram. Routes each update to
  the run it belongs to (`activities/route.py`) and signals it. Nothing else may
  call `getUpdates`: Telegram allows one poller per bot, and a second one steals
  replies and triggers 409s.
- `ui.py` — the dashboard on port 8400. Everything it serves reads, with one
  exception: `POST /api/archive` writes `runs/.archived.json`, the list of runs
  hidden from the rail. Nothing else takes a method other than GET, and nothing
  in it writes with git or signals a workflow. Change its `PAGE` string and you
  must run the browser checklist in `tests/test_ui.py` by hand: the suite reads
  that JavaScript as text and cannot see the page.
- `version.py` — release tags, changelog slicing and the update rules. `lg` and
  `ui.py` both import it; it is at the root because `lg` cannot be imported.
- `release.sh` — the maintainer's release command. Nothing else writes a tag.
- `CHANGELOG.md` — one section per release, newest first, `## Unreleased` on top.
- `runs/` — run directories. Gitignored except the shipped examples.

## Rules that bite if you ignore them

**Answers arrive as signals, never by polling.** A workflow waits on
`wait_condition` for the `decide` signal. `lg approve` sends it directly; a
Telegram reply reaches it through the dispatcher. Adding a second way in is what
produced four separate bugs: a retry that re-skipped the queue and lost an answer,
an unconfirmed update replayed as a fresh instruction, a poll that destroyed the
updates it did not return, and a stale tap deciding the wrong card.

**The supervisor knows only what `assemble_audit_prompt` hands it.** It never sees
the executor's transcript, its directive, or the workflow's state, and that
isolation is the point. So anything it must weigh has to be built into that
prompt. Two bugs came from forgetting it: an owner's answer reached the executor
alone, so every value the owner authorised looked invented and the same question
came back every round; and the work item was passed for the log filename only, so
a multi-item run judged each round against the whole brief and called the other
items' absence a defect.

**Workflow code must be deterministic.** No environment reads, no clocks, no
randomness, no I/O in `workflows/run.py`. Temporal replays it from history, and a
non-deterministic workflow breaks on replay, not when you write it. Anything that
touches the outside world goes in an activity. `telegram_configured` exists for
exactly this reason.

**An activity that raises must never escape `run`.** `_run_item` parks the item
when the executor round, the audit or the checkpoint dies, `_run_sweep` ends the
sweep when a detector pass dies, and `run` catches everything else as an
`engine failure:` and returns the ledger with a stopped note. Temporal answers
the `ledger` query on a failed workflow from the state at the failure, so a raise
leaves the run reading `running` forever, with no card to the owner.

**Paths in a run's `gates.yaml` are container paths.** `/app/runs/<slug>` for the
run directory, `/projects/<name>` for a target repo. Host paths silently fail.
`lg start` is the one place that checks: `unreachable` refuses a run dir or a
target repo under neither mount before it connects, because `lg` runs on the host
where both mounts are ordinary directories. Nothing checks a gates.yaml.

**Never hardcode a home directory.** `tests/test_release.py` fails the build if you
do. Everything machine-specific comes from `.env`, which `install.sh` writes.

**Nothing under `runs/` gets committed** except the named examples in
`.gitignore`. Real runs hold the user's work, sometimes a client's.

**The yield number comes from `discover` and nowhere else.** A sweep ends when the
detectors stop reporting, never when the executor says it is done; the
`candidates` it reports ride along to the next item and move no count. And a
convergence or sweep item's commit is refused by `checkpoint_write_set` when the
staged diff is above net zero, tests included. Both are code. The prompts only
tell the models the checks exist, so they do not duplicate them.

**The round loop's exit is a gate exit code, never a model's claim.**
`graphs/round_graph.py::route` reads `status` off the gate results, and a round
is done or escalated on nothing else. Where model output does steer the loop,
the audit verdict that drives `_run_item`, the bound is two workflow-local
integers, `spent` and `asks`, that no verdict can extend. An exit code is only
a bound if the shell reports the right one, which is what pipefail is for.
`_run_one` and `run_detector` run every command under `bash -o pipefail -c`,
because `/bin/sh` reports a pipeline's last stage and `pytest -q | tee log` was
green with pytest red. The defect is invisible in review, because the loop lives
behind `add_conditional_edges`, not a `while True` anyone reads. The shipped
scope gate was vacuously green for every run on one machine for weeks, five
rounds wrote it into a constraints file, and nothing in the engine could tell.

## Releasing

Write the user-visible note under `## Unreleased` in `CHANGELOG.md` in the same
commit as the change. `./release.sh X.Y.Z` does the rest: it renames that heading
to the version and date, sets the version in `pyproject.toml`, commits, makes the
annotated tag `vX.Y.Z` and pushes both. It refuses off `main`, with uncommitted
changes, when `main` is not at `origin/main`, when the tag exists, when the
changelog has nothing to say, and when the tests fail.

No agent runs `release.sh`. It pushes.

## Checks

```bash
.venv/bin/python -m pytest -q      # all of it, ~45s
```

`tests/test_release.py` guards publishing: no credentials or personal paths in
tracked files, and no real runs tracked. If it goes red, do not work around it.

The engine cannot test itself end to end in CI. The real check is running
`lg start runs/example-hello /projects/loopgraph-example` and driving it to a
decision.
