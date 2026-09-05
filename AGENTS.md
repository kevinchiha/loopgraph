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
- `lg` — the host CLI. No `.py` extension, so tests load it by path.
- `dispatcher.py` — the only process that reads Telegram. Routes each update to
  the run it belongs to (`activities/route.py`) and signals it. Nothing else may
  call `getUpdates`: Telegram allows one poller per bot, and a second one steals
  replies and triggers 409s.
- `ui.py` — read-only dashboard on port 8400. Change its `PAGE` string and you
  must run the browser checklist in `tests/test_ui.py` by hand: the suite reads
  that JavaScript as text and cannot see the page.
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

**Paths in a run's `gates.yaml` are container paths.** `/app/runs/<slug>` for the
run directory, `/projects/<name>` for a target repo. Host paths silently fail.

**Never hardcode a home directory.** `tests/test_release.py` fails the build if you
do. Everything machine-specific comes from `.env`, which `install.sh` writes.

**Nothing under `runs/` gets committed** except the named examples in
`.gitignore`. Real runs hold the user's work, sometimes a client's.

## Checks

```bash
.venv/bin/python -m pytest -q      # all of it, ~4s
```

`tests/test_release.py` guards publishing: no credentials or personal paths in
tracked files, and no real runs tracked. If it goes red, do not work around it.

The engine cannot test itself end to end in CI. The real check is running
`lg start runs/example-hello /projects/loopgraph-example` and driving it to a
decision.
