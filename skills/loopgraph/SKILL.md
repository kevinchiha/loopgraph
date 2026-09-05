---
name: loopgraph
description: Run a feature or task through the loopgraph engine (supervised, gated, audited agent runs) in any of the user's projects. Use when the user says "run a loop", "loopgraph this", "run it on loopgraph", "use the engine", or wants a feature implemented hands-off with verification, an audit, and a merge-ready decision card. Handles run-dir creation, gate-writing, starting the run, and reporting back. Not for one-off edits the user wants done right now in the chat.
---

# loopgraph — fire an engine run from any project

You write the brief and the gates, start the run, watch it, and report. The engine
runs the work in a container, checks it with code, has a second agent audit it
against the diff, and then stops and asks the user before anything merges.

**Never guess paths.** Run `lg where` first. It prints, for this machine: the
engine root, the projects directory, the docker command (with or without sudo),
the dashboard URLs, and whether Telegram is set up. Everything below refers to
those values.

## 0. Preconditions (check, don't assume)

```bash
lg where                    # engine root, projects dir, docker command
cd <engine_root> && <docker> compose ps
```

- The worker must be `Up`. If not: `<docker> compose up -d`, wait ~10s.
- **The target repo must live under `projects_dir`.** That tree is the only thing
  mounted into the container, as `/projects`. If the user's project is somewhere
  else, say so and stop. Do not improvise a mount without asking.

## 1. Write the run dir

**Asked for the shipped example?** ("run the example", "try loopgraph", a first run
after installing.) The run dir already exists. Skip to step 2 with
`runs/example-hello` and `/projects/loopgraph-example`. Do not write a new brief.

`<engine_root>/runs/<YYYY-MM-DD>-<short-slug>/` with exactly:

- `brief.md` — the feature, the checkable done-when, and the write set (the exact
  paths the executor may touch). One screen, no more. If the request is vague,
  pick the smallest honest slice and say which slice you chose.

  **Split multi-part work into work items.** A `## Work items` heading with one
  bullet per item makes the run do them in order, each with its own rounds, audit
  and commit, all onto one branch:

  ```markdown
  ## Work items

  - Redesign the settings page to match mockups/settings.png
  - Redesign the profile page to match mockups/profile.png
  ```

  One item = one independently verifiable change. Do not split a change and its
  test across two items; they share one verification, so they are one item. Leave
  the heading out for anything small and the whole brief is a single item.

  A bullet may wrap onto indented lines. The next heading, or unindented prose,
  ends the list.
- `constraints.md` — `touch` it. The learning edge appends to it between rounds.
- `gates.yaml` — the project's REAL check commands. Read `package.json`,
  `Makefile`, `pyproject.toml` or `Cargo.toml` to find them:
  ```yaml
  - name: tests  cmd: "pytest -x -q"                      timeout: 600
  - name: build  cmd: "npm run build"                     timeout: 1800
  - name: scope  cmd: "/app/runs/<slug>/check-write-set.sh"  timeout: 60
  ```
  **Paths in gates.yaml resolve INSIDE the container.** The run dir is
  `/app/runs/<slug>` and the target repo is `/projects/<name>`. Never host paths.

  Add a scope gate whenever the write set is enumerable: copy
  `runs/example-hello/check-write-set.sh` and edit the filenames in its `case`.

  Traps worth knowing: Next.js 16 and later have no `next lint`; npm gates in a
  fresh worktree need `npm ci --prefer-offline --no-audit --silent &&` in front;
  every gate needs a timeout.

- **Gate-first, non-negotiable.** Run every gate command yourself, in the order
  gates.yaml lists them and in the same directory, and confirm each exits 0
  BEFORE starting the run. In order matters: a test gate that writes
  `__pycache__/` or `.pytest_cache/` will turn a later scope gate red, and that
  defect is invisible if you only run each gate against a clean tree. The fix is
  a `.gitignore` in the target repo, never a weakened scope gate. Use the closest host
  equivalent where a gate's interpreter only exists in the container. A gate that
  cannot pass on an untouched repo will burn three executor rounds and escalate.
  If one fails on the clean tree, the project is already broken: report that
  instead of starting a run.

## 2. Start and watch

```bash
cd <engine_root>
lg start runs/<slug> /projects/<repo-name>
# prints a workflow id like run-<slug>-ab12cd
```

Open both dashboards as soon as it starts, because that is where the user watches
a run, not in your transcript:

```bash
curl -s -m 2 -o /dev/null localhost:8400 || (nohup lg ui > /tmp/lg-ui.log 2>&1 & sleep 2)
xdg-open http://localhost:8400
xdg-open http://localhost:8233/namespaces/default/workflows    # the new run is the top row
```

On a headless or SSH session skip `xdg-open` and give the two URLs in your reply.

Also useful: `lg status <workflow-id> ledger` for the scoreboard, and
`lg tail runs/<slug>` for the live executor and supervisor streams. `lg tail`
never returns, so background it or use the dashboard instead.

## 3. Decisions and outcomes

A run stops at a decision and holds there, changing nothing, until the user
answers. Tell them a decision is coming and how to answer it:

- A card lands in the bot `lg where` names. Buttons on merge-ready cards,
  plain-text replies on question cards.
- From a terminal: `lg approve <workflow-id> A`. Give them this too; it is faster
  when they are already at the machine.

**Parked items.** An item that cannot go green after three rounds is parked and
the run carries on with the rest. The user gets a message straight away, which
needs no answer, and anything they reply is handed to the next item. So a run can
finish `merge-ready` with some items missing: read `items` in the ledger, and
report which ones were parked and why. Do not describe such a run as done.

Runs end as `merge-ready` (waiting on the user, your job is done), `merged`,
`held` or `discarded` (their answer), or `stopped`. `stopped` means either every
item was parked or the supervisor said stop, which is the one verdict that ends a
whole run. Read `reason` in the ledger and report it with the red gate's output
tail, verbatim.

## Don'ts

- Never edit the target repo yourself outside a run, and never merge branches.
  Merging is the user's decision, by button or by explicit instruction.
- Never weaken or edit a gate to make a run pass. A defective gate gets reported,
  not patched around. You MAY fix a gate that is wrong on the clean tree, but say
  that you did.
- Never guess credentials or model names. They are already in the engine's `.env`.
- One run at a time per target repo. Worktree branches would collide.
