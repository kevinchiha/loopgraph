# loopgraph

An agent writes the code. Code decides whether it worked. A second agent, with no
memory of writing it, checks the claim against the diff. Then it stops and asks you.

You still approve the merge. You just stop reviewing the twenty steps before it.

```
brief + gates  →  executor round  →  gates (code, no model)  →  audit (fresh agent)
                       ↑                      │                        │
                       └──── correct ─────────┘                  accept / redo
                            (max 3)                                    │
                                                              decision → you
```

## Why

The usual routine is: open a coding agent, describe a feature, watch it type,
squint at the diff, accept. Every step is verified by you, in the chair.

Two things make that tiring, and both have fixes that are infrastructure rather
than discipline:

**An agent cannot mark its own homework.** So the gates are shell commands that
must exit 0, and the auditor is a separate agent with a fresh context that only
sees the brief and the diff. It never sees the reasoning that produced the code,
which is the whole point. An executor that says "all tests pass" gets checked.

**Long runs die.** So the orchestration is [Temporal](https://temporal.io): the
run's state is durable, and a reboot in the middle resumes rather than restarts.
Waiting two days for you to answer a decision costs nothing.

Inside one round, [LangGraph](https://github.com/langchain-ai/langgraph) runs the
small loop: produce, gate, correct, repeat, capped at three attempts. After that
it escalates to the auditor instead of grinding.

## What you need

Linux, Docker with the Compose v2 plugin, Python 3.13+, a way to reach Claude, and
a Telegram bot. Nobody has tried this on macOS.

The bot is not decoration. A run stops and asks you things, and the engine refuses
to start without a way to reach you, because a run nobody is told about waits
silently for as long as you happen not to look. @BotFather takes two minutes and
the installer does the rest.

For the model, you have two routes:

- **CLIProxyAPI plus a Claude subscription (recommended).**
  [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) runs locally, signs in
  with the subscription you already pay for, and speaks the Anthropic API. A single
  run can spend three executor rounds plus an audit pass, so per-token billing adds
  up faster than you would guess.
- **A plain Anthropic API key.** Simpler to start, metered.

## Install

Installing this means wiring it to your machine: where your repos live, how you
reach Claude, whether Docker needs `sudo` here, which uid the container has to
write as. A coding agent can look all of that up and ask you about the rest, so
that is the supported path.

Paste this into Claude Code, or any coding agent that reads files and runs
commands. It works from an empty directory; the agent clones the repo itself.

```
Install loopgraph from https://github.com/kevinchiha/loopgraph.
Clone it, then follow INSTALL_WITH_AGENT.md in the repo.
```

It asks where your repos live, how you reach Claude, and walks you through
@BotFather. Then it runs `./install.sh` with your answers and proves the install by
driving the example run to a decision. Read
[INSTALL_WITH_AGENT.md](INSTALL_WITH_AGENT.md) if you want to know exactly what it
will do before you let it.

<details>
<summary>No agent to hand?</summary>

```bash
git clone https://github.com/kevinchiha/loopgraph.git && cd loopgraph
./install.sh
```

It asks the same questions in a terminal. `./install.sh --yes` takes every default
for scripted installs, but it cannot do the @BotFather step for you, so it writes
the config and stops short of starting the stack. Either way it works. You are just
on your own when your machine disagrees with its assumptions, which is what the
agent is there for.

</details>

## Using it

The installer links a skill into `~/.claude/skills/`, so your agent already knows
how to drive the engine. You describe the work; it writes the run and starts it.

Start with the example, which ships with the repo:

> Run the example through loopgraph.

The agent checks the stack is up, starts the run, and tells you how to answer the
decision. The run adds a `--hello` flag, gates it, audits it, and stops to ask
whether to merge. Answer it and you have seen the whole engine.

Then point it at something real, in a repo under the tree you mounted:

> Run this through loopgraph: we renamed "customer" to "account" in the database.
> Update every place in the code that still says customer, including the tests, and
> keep everything passing.

> Run this through loopgraph: redesign the app to match the screenshots in
> `mockups/`, one page at a time, and don't break anything they already do.

> Run this through loopgraph: the payments module has no tests. Write them, cover
> the refund and partial-refund paths, and change no behaviour while you do it.

> Run this through loopgraph: upgrade to the new major version of the date library
> and fix everything it breaks.

What those have in common: big, boring, and easy to get almost right. An agent
will tell you it changed all forty places. The gates and the audit are how you
find out it changed thirty-eight and quietly left two, which is the kind of thing
you would have waved through at 6pm on a Friday.

The redesign is the odd one out, and worth being straight about. No gate can tell
you a page looks right. What the gates prove is that it still builds, the tests
still pass, and nothing outside the page you named got touched. You judge the look
yourself, on the branch, when it asks. That is still most of the tedium off your
desk, and you are reviewing a finished thing instead of watching it get made.

"One page at a time" is not a figure of speech. The brief lists work items and the
run does them in order: each gets its own rounds, its own audit, and its own commit
on one branch, so page four starts from page three's verified state. You get one
merge card at the end, not one per page.

An item that will not go green after three rounds is parked and the run carries on.
One page with a broken gate does not throw away the five that worked. You get a
message the moment something is parked, and whatever you reply is handed to the
next item, so you can correct a run in flight without stopping it. The final card
names every parked item and says plainly that merging does not include them.

The agent writes the brief and the write set, digs the real check commands out of
`package.json` or `pyproject.toml`, confirms they pass on a clean tree before
starting, then starts the run and reports back. You hear from it again when there
is a decision to make.

That clean-tree check matters more than it sounds. A gate that cannot pass on an
untouched repo burns all three rounds, and then the report blames the executor for
your build.

Work that is not worth a loop: anything you would have finished while writing the
brief. Ask your agent directly for those.

## Defining a run

The skill writes these for you, but read this section anyway. It is what you are
checking when the agent shows you a brief before starting a run, and a brief you
did not read is a run you cannot judge.

A run is a directory under `runs/`. Three files:

**`brief.md`** — the feature, the checkable done-when, and the write set: the exact
paths the executor may touch. One screen. A vague brief produces vague work.

List the work under a `## Work items` heading and the run does them one at a time:

```markdown
## Work items

- Redesign the settings page to match mockups/settings.png
- Redesign the profile page to match mockups/profile.png
```

Each item gets its own rounds, audit and commit. Leave the heading out and the
whole brief is a single item, which is the right shape for something small.

Size them so one item is one independently verifiable change. Too big and the
audit is reading a diff nobody could check; too small and you pay for a fresh
executor context to move one line.

**`gates.yaml`** — the real check commands for that project.

```yaml
- name: tests  cmd: "pytest -x -q"                        timeout: 600
- name: build  cmd: "npm run build"                       timeout: 1800
- name: scope  cmd: "/app/runs/my-run/check-write-set.sh" timeout: 60
```

Paths here resolve **inside the container**: the run dir is `/app/runs/<slug>` and
your repos are under `/projects`. Every gate needs a timeout.

The scope gate is worth the two minutes. It fails the round if anything outside
the declared write set changed, which is how you keep a "small fix" from touching
nine files. Copy `runs/example-hello/check-write-set.sh` and edit the names in it.

**`constraints.md`** — start it empty. The engine appends what it learns between
rounds, so round three does not repeat round one's mistake.

**Run every gate yourself first, in the order they are listed and in the same
directory.** A gate that cannot pass will burn all three rounds and escalate, and
the report will blame the executor for your broken build.

Order matters more than it looks. A test gate that leaves `__pycache__/` behind
turns a later scope gate red, and you will not see it if you run each gate on its
own against a clean tree. Give the target repo a `.gitignore` that covers its own
build byproducts. Never fix it by loosening the scope gate.

## Watching a run

```bash
lg status <workflow-id> ledger   # the scoreboard
lg tail runs/<slug>              # live executor and audit streams
lg ui                            # dashboard on http://localhost:8400
```

Temporal's own UI is on http://localhost:8233 and shows the workflow history,
which is the place to look when something is stuck rather than slow.

## When it talks to you

Three moments, and they behave differently on purpose.

**The auditor needs a decision only you can make.** Credentials, spend, a schema
change, lowering a bar it was told to hold. The run stops, changes nothing, and
waits however long you take. Your answer goes into the next round for that item.
The auditor is told never to ask what it could verify itself, so this should be
rare; if it starts asking often, your brief is leaving something undecided.

**An item got parked.** No question, no waiting. You are told and the run carries
on with the rest. Anything you reply reaches the executor before the next item
starts, which is how you steer a long run without restarting it.

**Merge-ready at the end.** The work is committed on a branch and nothing has been
merged.

```bash
lg approve <workflow-id> A     # A merge, B keep the branch, C discard
```

The same question is already on your phone with buttons, and a tap does the same
thing. `lg approve` is for when you are at the machine anyway; it is not a
substitute for being told a run needs you.

Merge cards take letters only. A stray text reply can never decide a merge.

So the run carries on by itself exactly when nothing needs your judgment, and the
moment something does, it stops and waits.

## Doing it by hand

Nothing above needs an agent. The skill is a shortcut, not a dependency.

```bash
lg where                                                # paths and ports here
lg start runs/example-hello /projects/loopgraph-example
```

The skill itself is `skills/loopgraph/SKILL.md`. It is worth reading even if you
never use it: it is the shortest description of how to drive this thing properly.

## Limits, honestly

- One run at a time per target repo. Concurrent worktree branches collide.
- Target repos must live under the one directory tree you mount. Nothing outside
  it is reachable, by design.
- The executor runs with permissions bypassed inside the container. It is confined
  to a git worktree and to your projects tree, but read a brief before you start it.
- Three rounds, then it escalates. It does not grind forever, and it does not
  quietly give up either.
- Linux and Docker only.
- A Telegram bot is required, not optional. If you want a different channel, the
  place to add one is `activities/notify.py`, which is about ninety lines.

## How it fits together

[SPEC.md](SPEC.md) is the original design doctrine and still the best explanation
of why the pieces are arranged this way. `docs/architecture.html` is an explorable
diagram of the same thing.

## License

MIT. See [LICENSE](LICENSE).
