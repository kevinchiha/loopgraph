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
lg version                  # what you have, and whether a newer release exists
cd <engine_root> && <docker> compose ps
```

- **If `lg version` prints an `update:` line**, tell the user in one sentence
  what they have and what is available, and that `lg update` gets it. Then carry
  on with the run. Do not run `lg update` unless the user asks: it restarts the
  stack, and it refuses while any run is open.
- The worker **and the dispatcher** must both be `Up`. If not: `<docker> compose
  up -d`, wait ~10s and look again. Nothing in the stack restarts itself, so a
  container that died stays dead. A missing dispatcher is the quiet failure:
  cards still reach the user's phone, and nothing they answer ever comes back.
  A run with a browser.yaml also needs browser Up; see step 1.
- **The target repo must live under `projects_dir`.** That tree is the only thing
  mounted into the container, as `/projects`. If the user's project is somewhere
  else, say so and stop. Do not improvise a mount without asking.

## 1. Write the run dir

**Asked for the shipped example?** ("run the example", "try loopgraph", a first run
after installing.) The run dir already exists. Skip to step 2 with
`runs/example-hello` and `/projects/loopgraph-example`. Do not write a new brief.

`<engine_root>/runs/<YYYY-MM-DD>-<short-slug>/` with these files (the first three
always, `browser.yaml` only for a web app), plus
`run.yaml` for a sweep or to change the convergence defaults:

- `brief.md` — the feature, the checkable done-when, and the write set (the exact
  paths the executor may touch). One screen, no more. If the request is vague,
  pick the smallest honest slice and say which slice you chose.

  **Prove the defect before you describe it.** Open the code that shows the
  problem, not the code you expect to show it, and check the neighbours of every
  file you name. A brief that opens with a problem the repo does not have burns a
  whole item: the executor finds the premise false in its first minute, the
  supervisor returns `plan`, and the item parks having done nothing. That has
  already happened on a Next.js repo, over pages whose "missing" metadata was
  sitting in a sibling `layout.tsx` nobody looked for. Grepping one filename
  pattern is not a survey.

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

  **A decision that is genuinely the user's can stay out of the brief.** Say
  plainly that it is theirs, that nothing in the repo settles it, and that no
  default is to be picked. The executor builds everything the answer will need,
  reports the decision as a blocker, and the supervisor checks whether the repo
  really is silent on it before putting it to the user as a card. So do not guess
  a policy on their behalf to keep a brief tidy, and do not bury the question in
  prose and hope somebody notices. This costs the run nothing: a question does not
  spend a correction round.

  Items must stand alone. No item's done-when may depend on another item landing
  first, because any of them can park and the rest carry on regardless. Keep their
  write sets apart too where the work allows it: two items editing one file is how
  a parked item's leftovers end up in the next item's commit.

  A bullet may wrap onto indented lines. The next heading, or unindented prose,
  ends the list.
- `constraints.md` — `touch` it. The learning edge appends to it between rounds.
- `gates.yaml` — the project's REAL check commands. Read `package.json`,
  `Makefile`, `pyproject.toml` or `Cargo.toml` to find them:
  ```yaml
  - name: tests
    cmd: "pytest -x -q"
    timeout: 600
  - name: build
    cmd: "npm run build"
    timeout: 1800
  - name: scope
    cmd: "/app/runs/<slug>/check-write-set.sh"
    timeout: 60
  ```
  **Paths in gates.yaml resolve INSIDE the container.** The run dir is
  `/app/runs/<slug>` and the target repo is `/projects/<name>`. Never host paths.

  Add a scope gate whenever the write set is enumerable: copy
  `runs/example-hello/check-write-set.sh` and edit the filenames in its `case`.
  Keep its `#!/bin/bash`. The worker container's `/bin/sh` is dash, `read -d` is
  a bash extension, and under dash that script's loop dies on its first line and
  the gate exits 0 whatever changed.

  The engine runs every `cmd` in `gates.yaml`, and every detector `cmd` in
  `run.yaml`, under `bash -o pipefail -c`, so a pipeline is red when any stage
  fails and not only when the last one does: `pytest -q | tee log` is red when
  pytest is. The code you get back is the rightmost failing stage's. That does
  not reach inside a script the gate calls: a `check-write-set.sh` with its own
  pipeline needs its own `set -o pipefail`, and the shipped one has it. Two
  shapes now go red that used to pass. A stage that stops reading early
  (`| head`, `grep -q`) kills its producer with SIGPIPE and the command reports
  exit 141, but only when the producer writes enough. Under a few kilobytes it
  finishes first and the command exits 0; well past the 64 KiB pipe buffer it
  always dies; around the buffer the same command flips between 0 and 141 run to
  run. So a gate you proved green on a small tree can start failing as its
  output grows, or for no visible reason at all. Write the producer's output to
  a file and read the file; use `tail` when the end of the output is what you
  want; and for a yes/no question drop `grep -q` for `grep ... > /dev/null`,
  which reads to the end. And `grep` exits 1 when it matches nothing, so a grep
  stage that may legitimately find nothing needs the no-match test folded in
  with braces: `{ grep -rn TODO src/ || [ $? = 1 ]; } | sort -u`. Without the
  braces `||` binds looser than `|`, the later stages never see grep's output,
  and the command exits 0 whatever grep found.

  Traps worth knowing: Next.js 16 and later have no `next lint`; npm gates in a
  fresh worktree need `npm ci --prefer-offline --no-audit --silent &&` in front,
  and `npm ci` needs a lockfile the repo actually tracks (`git ls-files
  package-lock.json` — a worktree holds tracked files and nothing else); a Python
  project with a `src/` layout needs `pip install -e ".[dev]" -q &&` in front, or
  the tests cannot import the package they are testing; every gate needs a
  timeout.

  **Green gates are not the last check.** After the audit accepts, the engine
  stages the write set and runs `git diff --cached --check` before committing. An
  added line carrying trailing whitespace fails it, and the item parks with every
  gate green and a clean audit. It bites hardest when the write set holds
  generated output, so look at what the generator emits.

- `browser.yaml` — optional, and only for a web app. With it the engine serves
  the worktree with your command, the executor gets a real browser to check its
  own work in, the engine screenshots the pages you list after each round, and
  the auditor judges those screenshots and can open the app itself. Exactly
  these keys, nothing else; a key the engine does not know stops `lg start`:
  ```yaml
  serve:
    cmd: "npm run dev -- --port $LOOPGRAPH_PORT"   # required
    ready_timeout: 60                               # seconds until the port must answer; at most 180
  capture:                                          # at most 10 entries
    - name: home          # letters, digits, _ and -; unique in this file
      path: /             # must start with /
      width: 1280         # pixels; a full-page screenshot at this width
      timeout: 30         # seconds for the page to load; at most 60
  ```
  **The engine picks the port.** There is no `port` key; `cmd` must read
  `$LOOPGRAPH_PORT` and listen on it, on 127.0.0.1. Two runs on the host network
  would otherwise collide, and the second run's browser would be looking at the
  first run's app. `$LOOPGRAPH_APP_URL` is the same thing as a URL. Gates never
  see `$LOOPGRAPH_APP_URL`: the engine re-runs them at commit time with no
  server up, so a gate that needed the app would pass in the round and fail the
  commit. A gate that needs a browser gets `$LOOPGRAPH_BROWSER_WS` and starts
  its own server.

  `cmd` must be a dev server that serves the working tree as it is, so an edit
  shows on the next load: `next dev`, `vite`, `flask --debug run`. A preview of
  a build (`next start`, `vite preview`) shows the tree as it was when the build
  ran, and the executor's edits never reach the auditor's eyes. `npm ci` belongs
  at the front of `cmd` if the worktree needs it, the same as a gate. The serve
  command runs in the worktree, and anything it writes there that the target
  repo does not gitignore joins the round's write set, the same as a gate
  command.

  Ready means `127.0.0.1:$LOOPGRAPH_PORT` accepts a connection. After
  `ready_timeout` the engine gives up, the round goes on without a browser, and
  the auditor is told, with the server's last lines. So prove the command in
  the container first, on the same tracked-files tree the gates were proven on:

  ```bash
  <docker> compose exec -T worker bash -o pipefail -c '
    cd /tmp/gatetest && export LOOPGRAPH_PORT=8765 LOOPGRAPH_APP_URL=http://127.0.0.1:8765
    setsid bash -c "<serve cmd>" & sleep 20
    python -c "import urllib.request as u; print(u.urlopen(\"http://127.0.0.1:8765/\").status)"
    kill -- -$!'
  ```
  A `200` there is what the auditor will get. The probe is Python because the
  worker image has no `curl`. `HTTP Error 404` means the server is up and `/`
  is not a page; `Connection refused` means it never bound the port. The browser
  container must be up: `<docker> compose ps` shows `browser`. If it does not,
  `COMPOSE_PROFILES=browser` goes in the engine's `.env` and the stack comes up
  with `<docker> compose up -d`; `lg start` refuses the run until then. Captures
  land in `runs/<slug>/shots/i<item>-r<round>/<name>.png`, and the engine's own
  browser writes nothing into the worktree.

- **Gate-first, non-negotiable.** Run every gate command yourself, in the order
  gates.yaml lists them and in the same directory, and confirm each exits 0
  BEFORE starting the run. In order matters: a test gate that writes
  `__pycache__/` or `.pytest_cache/` will turn a later scope gate red, and that
  defect is invisible if you only run each gate against a clean tree. The fix is
  a `.gitignore` in the target repo, never a weakened scope gate. A gate that
  cannot pass on an untouched repo will burn eight executor rounds and escalate.
  If one fails on the clean tree, the project is already broken: report that
  instead of starting a run.

  **Exit 0 is half the proof. Make every gate go RED before you trust it.** A
  gate that cannot fail reads exactly like a gate that passed, in your terminal
  and in the ledger both, and it is the one defect this whole step exists to
  catch. So for each gate, break the thing it guards and watch it fail: put a
  file outside the write set in the tree for a scope gate, and confirm the gate
  names that file and exits non-zero; break a test for a test gate. Then undo it
  and confirm green again. Three directions, and skipping the red one has
  already shipped a scope gate that was vacuously green for every run on this
  machine, because `read -d` no-ops under dash and the loop never ran.

  **Prove them on tracked files only, in the container.** A run works in a fresh
  worktree, which holds what git tracks and nothing else. Your checkout also holds
  `node_modules`, a `.venv`, a config file you never committed, and any of them can
  be the only reason a gate passes for you. Build the same tree the run will get:

  ```bash
  <docker> compose exec -T worker bash -o pipefail -c '
    rm -rf /tmp/gatetest && mkdir -p /tmp/gatetest
    git -C /projects/<repo> archive HEAD | tar -x -C /tmp/gatetest
    cd /tmp/gatetest && <each gate command, in order>'
  ```

  That also settles the interpreter question, since it runs where the gates will
  run, and it tells you what a round actually costs in wall-clock time.
  `bash -o pipefail -c` is the shell the engine runs gates and detectors under,
  so what passes there passes in a run.

## 2. Start and watch

```bash
cd <engine_root>
lg start runs/<slug> /projects/<repo-name>
# prints a workflow id like run-<slug>-ab12cd, then FOLLOWS the run and does not return
```

**`lg start` does not exit when the run has started.** It prints the workflow id
and then stays attached for the life of the run. So run it in the background and
read the id out of its log, or give it a `timeout` and let that kill it — the
workflow lives in Temporal and the worker executes it, so nothing stops when the
client goes away.

**Never run it twice.** There is no "already running" guard: a second `lg start`
on the same run dir starts a second workflow, and then two executors work the same
brief in two worktrees and both write to the same `logs/` filenames, because a log
is named `i1-r1-executor.log` with no workflow id in it. A backgrounded first
attempt whose output has not flushed yet looks exactly like one that failed —
that is the trap, and it costs a whole duplicate run. Before starting a second
time, check what is actually open:

```bash
<docker> compose exec -T temporal tctl --address temporal:7233 workflow list --open
```

If you do end up with two, `lg` has no stop command; terminate the one you do not
want by id with `tctl ... workflow terminate --workflow_id <id> --reason <why>`,
and leave its worktree registration alone (see the prune warning in Don'ts).

Open both dashboards as soon as it starts, because that is where the user watches
a run, not in your transcript:

```bash
curl -s -m 2 -o /dev/null localhost:8400 || (nohup lg ui > /tmp/lg-ui.log 2>&1 & sleep 2)
xdg-open http://localhost:8400
xdg-open http://localhost:8233/namespaces/default/workflows    # the new run is the top row
```

**Start the dashboard after Temporal, never before.** `lg ui` binds its Temporal
connection once, at startup. Start it while Temporal is down and it serves every
run as `unknown` with `logs only` beside it, for the whole life of the process,
including runs that start later and are plainly running. It looks like a rail
that has lost its data. Restart the dashboard and the states come back. If you
brought the stack up yourself, bring `lg ui` up last.

On a headless or SSH session skip `xdg-open` and give the two URLs in your reply.

Also useful: `lg status runs/<slug>` for what the run is doing (a workflow id
works too, and `lg status <workflow-id> ledger` still prints the raw JSON), and
`lg tail runs/<slug>` for the live executor and supervisor streams. `lg tail`
never returns, so background it or use the dashboard instead.

## 3. Decisions and outcomes

A run stops at a decision and holds there, changing nothing, until the user
answers. Tell them a decision is coming and how to answer it:

- A card lands in the bot `lg where` names. A merge card always has buttons. A
  question card has them when the supervisor could name the choices and a reply
  box when it could not, so tell the user both are possible. Tell them to **reply
  to the card** rather than sending a new message: that is what tells the engine
  which run they are answering when more than one is in flight.
- From a terminal: `lg approve <workflow-id> A`. Give them this too; it is faster
  when they are already at the machine.
- Whatever they answer is written to `owner-answers.md` in the run dir, and both
  the executor and the auditor read it from there. So the same question is not
  asked twice, and that file is where you read their answer back when you report.
  A question does not spend a correction round; only a redo does.
- A merge card takes a letter and nothing else. If they type an answer instead,
  the engine tells them so and keeps waiting; the run is not stuck.

**Parked items.** An item that cannot go green after eight rounds (`LOOPGRAPH_MAX_ROUNDS` in the
engine's `.env`) is parked and
the run carries on with the rest. The user gets a message straight away, which
needs no answer, and anything they reply is handed to the next item. So a run can
finish `merge-ready` with some items missing: read `items` in the ledger, and
report which ones were parked and why. Do not describe such a run as done.

Runs end as `merge-ready` (waiting on the user, your job is done), `merged`,
`merge-failed` (the merge was refused or rolled back — read `merge.reason`),
`held` or `discarded` (their answer; a discard deletes the branch and its
worktree), `discard-failed` (they chose C and the branch survived — read
`discard.reason` and tell them it is still there), or `stopped`. `stopped` means either every
item was parked or the supervisor said stop, which is the one verdict that ends a
whole run. Read `reason` in the ledger and report it with the red gate's output
tail, verbatim.

## Sweep runs

A sweep has no work items of its own. It runs the project's dead-code detectors,
removes some of what they report, runs them again, and stops when two passes in a
row come back at or under a floor, or when its deadline or its item cap lands
first. The engine decides that from the detectors' output alone; the executor
never gets a vote on when it is finished.

Add `run.yaml` next to `brief.md`. Presence of `sweep:` is what makes the run a
sweep. Leave the block out and the run is a normal brief run with the
`convergence` defaults shown:

```yaml
convergence:
  enabled: true     # every run spends an item removing code after
  every_items: 5    # this many accepted items, or
  net_lines: 400    # this many net lines added, whichever comes first
sweep:
  yield_floor: 3    # end after two passes at or under this
  deadline: 4d      # 30m, 4h or 4d; leave it out for no deadline
  max_items: 40
  groups: [src/, tests/]   # optional; path prefixes each item works in turn
  detectors:
    - name: vulture
      cmd: "vulture src/ --min-confidence 80"
      timeout: 300
    - name: ts-prune
      cmd: "npx ts-prune"
      timeout: 600
```

`enabled: false` switches convergence off for a run that has to add a lot of code,
a migration say. The key is `enabled`, never `off`: YAML reads a bare `off` as a
boolean, and the run refuses to start with a message saying so.

A detector prints one candidate per line to stdout and exits 0. Only stdout is
counted, so a warning on stderr is harmless; a non-zero exit or a timeout makes
the pass incomplete and counts nothing from that detector, and the ledger keeps
the tail of what it wrote to stderr so you can see why. `grep` exits 1 on no
match, and under pipefail so does `grep ... | sort`, so a detector built on grep
needs its no-match test folded in with braces,
`{ grep -rn TODO src/ || [ $? = 1 ]; } | sort -u`, or the sweep ends
`detectors failed:` on the day the repo is clean. The braces matter: `||` binds
looser than `|`, so without them `sort` never sees grep's output and the
detector reports raw grep lines with a green exit. A misspelt key anywhere in
the file stops the run before it starts, with the key named in the reason.

`groups` is optional. Each entry is a path prefix compared verbatim against the
path at the start of every candidate line, which has one leading `./` taken off
first, so write the prefix without one and end it in `/`: `app/` matches
`app/core.py` and nothing outside that directory, while `app` also matches
`apple.py` and `./app/` matches nothing. A candidate no prefix matches goes to a
group called `(other)`. Leave the key out and candidates are grouped by their
top-level directory, with files at the repo root in `(root)`.

Each sweep item takes one group of the chosen detector's candidates, and the next
item takes the next group by name, wrapping round; one pointer carries across
every detector, so a detector whose list always starts in the same corner cannot
keep the run there. Rotation changes nothing about the count the run converges
on: every detector still runs over the whole tree each pass, and every candidate
is still counted, whichever group it sits in.

Prove every detector the way you prove gates, inside the container on a tree that
holds tracked files only: in the same `/tmp/gatetest` tree as step 1, run each
`cmd` and confirm it prints candidates and exits 0. A detector that prints nothing
on the clean tree ends the sweep on its first pass with nothing done.

The brief of a sweep says what "dead" means for this project and what must never
be removed. A sweep brief must not carry a `## Work items` heading; the detectors
are the work list. The run refuses to start if it has one. Every sweep item is
held to net zero lines: the commit is refused if
it adds more lines than it removes, tests included, so a false positive that
needs a whitelist line has to ride along with a real deletion.

## Don'ts

- Never edit the target repo yourself outside a run, and never merge branches.
  Merging is the user's decision, by button or by explicit instruction.
- Never weaken or edit a gate to make a run pass. A defective gate gets reported,
  not patched around. You MAY fix a gate that is wrong on the clean tree, but say
  that you did.
- Never guess credentials or model names. They are already in the engine's `.env`.
- One run at a time per target repo. Worktree branches would collide.
