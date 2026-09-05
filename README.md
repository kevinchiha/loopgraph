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

Linux, Docker with the Compose v2 plugin, Python 3.13+, and a way to reach Claude.
Nobody has tried this on macOS.

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

It collects three answers from you, runs `./install.sh` with them, then proves the
install by driving the example run to a decision. Read
[INSTALL_WITH_AGENT.md](INSTALL_WITH_AGENT.md) if you want to know exactly what it
will do before you let it.

<details>
<summary>No agent to hand?</summary>

```bash
git clone https://github.com/kevinchiha/loopgraph.git && cd loopgraph
./install.sh
```

It asks the same questions in a terminal, and `./install.sh --yes` takes every
default and skips Telegram, for scripted installs. It works. You are just on your
own when your machine disagrees with its assumptions, which is what the agent is
there for.

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

> Run this through loopgraph: add a --json flag to the export command in myapp.

The agent writes the brief and the write set, digs the actual check commands out
of the project's `package.json` or `pyproject.toml`, confirms they pass on a clean
tree before starting, then starts the run and reports back. You hear from it again
when there is a decision to make.

That last step is the point. It refuses to start a run whose gates cannot pass on
an untouched repo, because a broken gate burns all three rounds and then the
report blames the executor for your build.

## Defining a run

The skill writes these for you, but read this section anyway. It is what you are
checking when the agent shows you a brief before starting a run, and a brief you
did not read is a run you cannot judge.

A run is a directory under `runs/`. Three files:

**`brief.md`** — the feature, the checkable done-when, and the write set: the exact
paths the executor may touch. One screen. A vague brief produces vague work.

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

## Approving

A run holds at a decision and changes nothing until you answer.

```bash
lg approve <workflow-id> A     # A merge, B keep the branch, C discard
```

With Telegram configured, the same decision arrives on your phone as a card with
buttons, and a tap does the same thing. Set it up. Watching a terminal for forty
minutes so you can press one key is a bad way to spend an afternoon.

Merge cards take letters only. A stray text reply can never decide a merge.

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

## How it fits together

[SPEC.md](SPEC.md) is the original design doctrine and still the best explanation
of why the pieces are arranged this way. `docs/architecture.html` is an explorable
diagram of the same thing.

## License

MIT. See [LICENSE](LICENSE).
