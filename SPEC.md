# Loop-Graph Engine — Temporal + LangGraph + Claude Code

*A concrete replacement for the "open Claude Code, ask for a feature, watch it type" routine.*
*Spec date: 2026-09-04*

> This is the original design document, kept as written. Two things changed in the
> build: decision cards ship on Telegram rather than Discord, and a run can also be
> answered from the terminal with `lg approve`. See the README for how it actually
> works today.

---

## 1. What this replaces

**Old routine:** open Claude Code → describe a feature → babysit the stream →
eyeball the diff → accept. Every step verified by you, in the chair.

**New routine:** write a feature brief → drop it in the queue → the engine
implements, verifies, and audits it → you get a Discord card when it needs a
decision or has a merge ready → you approve the merge. That's the one human step.

You move from *reviewing intermediate output* to *approving the last step* —
exactly the post's doctrine, but as infrastructure instead of Markdown discipline.

---

## 2. The layered architecture

The post's own sentence is the design: **"The loop lives inside a node, the graph
lives between them."**

```
┌─────────────────────────────────────────────────────────────────┐
│ TEMPORAL  (the graph between nodes — durable, survives restarts) │
│                                                                  │
│  LoopGraphRun workflow (one per feature/run)                     │
│   ├── state = the ledger (durable, queryable, survives death)    │
│   ├── timers = executor/supervisor cadences (workflow.sleep)     │
│   ├── signals = owner decision cards (A/B/C replies)             │
│   └── activities:                                                │
│        ┌──────────────────────────────────────────┐              │
│        │ execute_round  →  Claude Code (headless) │              │
│        │   └─ LANGGRAPH (the loop inside the node)│              │
│        │       produce → gate → correct → repeat  │              │
│        │       (capped at 3 corrections per unit) │              │
│        ├──────────────────────────────────────────┤              │
│        │ run_gate       →  deterministic checks   │  ← code node │
│        │   (pytest / ruff / tsc / custom scripts) │   NO model   │
│        ├──────────────────────────────────────────┤              │
│        │ audit          →  Claude Code (headless, │              │
│        │   FRESH context, read-only brief,        │  clean-context│
│        │   verdict: accept | redo | plan | stop)  │  supervisor  │
│        ├──────────────────────────────────────────┤              │
│        │ checkpoint     →  commit verified write  │              │
│        │   sets only, narrow gate re-run first    │              │
│        └──────────────────────────────────────────┘              │
└─────────────────────────────────────────────────────────────────┘
         │                                    │
         ▼                                    ▼
   Discord decision cards              git worktrees per round
   (approve merge / A/B/C)             (isolated write sets)
```

**Why Temporal owns the outside:** durable execution, timers, retries, and
signals are solved problems there. A VPS reboot mid-run is a non-event — the
workflow resumes from its event history. That's longgraph's `ledger.md` +
`/loop` timers, but real.

**Why LangGraph owns the inside:** one round is a small graph — produce →
deterministic gate → bounded correction → repeat-until-green (max 3, then the
failure escalates to the *plan*, per the post's "cap it at three attempts").
LangGraph gives you that inner loop with a checkpointer and clean structure.

**Why Claude Code is the labour:** you already pay for it and trust it. The
engine doesn't replace it — it *supervises* it.

---

## 3. Concept mapping (longgraph → engine)

| longgraph (Markdown)        | Engine primitive                                   |
| --------------------------- | -------------------------------------------------- |
| `ledger.md` scoreboard      | Temporal workflow state + `workflow.query` handler |
| executor on a `/loop` timer | Temporal cron / `workflow.sleep` between fires     |
| `executor.md` frozen prompt | `prompts/executor.md` fed to `claude -p`           |
| supervisor, clean context   | separate headless Claude call, no shared transcript|
| `directives.md` (one-way)   | workflow-internal correction queue (typed)         |
| milestone gate              | `workflow.wait_condition` on audit verdict         |
| owner decision card         | Discord webhook + Temporal signal for the reply    |
| learning edge               | accepted constraints appended to `constraints.md`, injected into every future executor prompt |
| host switch mid-run         | meaningless — workers are containers, not chats    |

---

## 4. Repo layout

```
loopgraph-engine/
├── docker-compose.yml          # temporal server + worker + (optional) temporal UI
├── pyproject.toml              # temporalio, langgraph, anthropic / claude-agent-sdk
├── worker.py                   # registers workflows + activities, runs forever
├── workflows/
│   └── run.py                  # LoopGraphRun: rounds, cadences, gates, signals
├── graphs/
│   └── round_graph.py          # LangGraph: produce → gate → correct (cap 3)
├── activities/
│   ├── execute_round.py        # claude -p in a git worktree, returns diff + log
│   ├── gate.py                 # runs declared check commands, parses exit codes
│   ├── audit.py                # supervisor call, returns verdict packet
│   └── notify.py               # Discord decision cards / merge-ready cards
├── prompts/
│   ├── executor.md             # longgraph's executor contract, lightly edited
│   └── supervisor.md           # longgraph's supervisor contract, verbatim-ish
└── runs/
    └── <date-slug>/
        ├── brief.md            # YOU write this — the feature + done-when
        ├── constraints.md      # the learning edge (grows over runs)
        └── gates.yaml          # exact check commands for this project
```

Borrow the prompt files straight from `levi-qiao/longgraph-skill` —
`skills/loop-graph/templates/executor.md` and `supervisor.md` are already the
right contracts; the engine just enforces what Markdown could only request.

---

## 5. Key implementation decisions

**Executor activity (per round):**
- Spawn a fresh git worktree → `claude -p "$(cat prompts/executor.md)"` with the
  brief + constraints + the round's work item, `--dangerously-skip-permissions`
  *only inside the container*, output as structured JSON (files changed, claims).
- Use the **Claude Agent SDK** (Python) instead of raw `claude -p` if you want
  streaming, session resume, and tool allowlists programmatically.

**Gates are code, never model:**
```yaml
# gates.yaml — write the condition FIRST, so a program can fail it
- name: tests        cmd: "pytest -x -q"                    green_when: exit 0
- name: lint         cmd: "ruff check ."                    green_when: exit 0
- name: scope        cmd: "./scripts/check-write-set.sh"    green_when: exit 0
```
The scope gate enforces the post's `SCOPE fix this file only` line mechanically.

**Supervisor activity (slower cadence, phase-offset):**
- Fresh Claude invocation. Receives: the brief, the *claims*, the *diff*, and
  gate results. Never receives the executor's transcript. Read-only tools.
- Returns `accept | redo | plan | stop` + bounded directive (exact paths, verify
  command, stop condition). Redo targets the **unit, not the batch**.

**Owner cards:** Discord message with A/B/C buttons (or a reply convention);
your reply goes back as a Temporal signal. No reply → run holds at safe
no-change state, same as longgraph's contract.

**Learning edge:** on `accept`, a tiny activity distils the confirmed cause
into one constraint line and appends it to `constraints.md`. Every future
executor prompt includes that file. *This is the part no framework ships — and
it's 20 lines of code.*

---

## 6. Your new daily routine

1. **Write a brief** (5 min, the only real work): what the feature is, and —
   per the post — the *checkable* done-when. `lg new runs/2026-09-05-brief.md`
2. **Start the run:** `lg start runs/2026-09-05-brief.md` → workflow live,
   visible in Temporal UI (localhost:8233).
3. **Walk away.** Executor fires rounds, gates fail/pass, supervisor audits on
   its own cadence, verified slices get checkpoint-committed.
4. **Answer cards when they arrive.** Owner-only calls only: schema changes,
   credentials, spend, lowering a bar. One letter each.
5. **Approve the merge.** The run lands as one reviewed PR. That's your chair
   time: minutes, not the session.

---

## 7. Honest costs & limits

- You now run infrastructure: Temporal server (one container, fine on the
  tower), a worker process, and secrets management for the Anthropic key.
- **You still write the gates.** The engine enforces them mercilessly but
  cannot invent your definition of done. First week is mostly gate-writing.
- Build order (per the post): **gate first**, then executor, then supervisor,
  learning edge last. A graph without a gate is a faster way to produce
  unverified output.
- Keep longgraph-the-skill around: it's the right weight for one-off tasks.
  The engine earns its keep when you have a *queue* of features.

## 8. Suggested first milestone (one weekend)

1. `docker compose up` Temporal + worker; hello-world workflow round-trips.
2. `gate.py` against a real KevBox test command — prove a red gate blocks.
3. `execute_round.py` runs Claude headless in a worktree on a toy brief.
4. `audit.py` overturns one deliberately fake "done" — if the supervisor can't
   catch a planted lie, the run is not trustworthy.
5. Discord card round-trip. Then, and only then, feed it a real feature.
