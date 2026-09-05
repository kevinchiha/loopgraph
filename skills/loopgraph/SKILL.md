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

- **Gate-first, non-negotiable.** Run every gate command yourself, in the order
  gates.yaml lists them and in the same directory, and confirm each exits 0
  BEFORE starting the run. In order matters: a test gate that writes
  `__pycache__/` or `.pytest_cache/` will turn a later scope gate red, and that
  defect is invisible if you only run each gate against a clean tree. The fix is
  a `.gitignore` in the target repo, never a weakened scope gate. A gate that
  cannot pass on an untouched repo will burn three executor rounds and escalate.
  If one fails on the clean tree, the project is already broken: report that
  instead of starting a run.

  **Prove them on tracked files only, in the container.** A run works in a fresh
  worktree, which holds what git tracks and nothing else. Your checkout also holds
  `node_modules`, a `.venv`, a config file you never committed, and any of them can
  be the only reason a gate passes for you. Build the same tree the run will get:

  ```bash
  <docker> compose exec -T worker sh -c '
    rm -rf /tmp/gatetest && mkdir -p /tmp/gatetest
    git -C /projects/<repo> archive HEAD | tar -x -C /tmp/gatetest
    cd /tmp/gatetest && <each gate command, in order>'
  ```

  That also settles the interpreter question, since it runs where the gates will
  run, and it tells you what a round actually costs in wall-clock time.

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

**Parked items.** An item that cannot go green after three rounds is parked and
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

## Don'ts

- Never edit the target repo yourself outside a run, and never merge branches.
  Merging is the user's decision, by button or by explicit instruction.
- Never weaken or edit a gate to make a run pass. A defective gate gets reported,
  not patched around. You MAY fix a gate that is wrong on the clean tree, but say
  that you did.
- Never guess credentials or model names. They are already in the engine's `.env`.
- One run at a time per target repo. Worktree branches would collide.
