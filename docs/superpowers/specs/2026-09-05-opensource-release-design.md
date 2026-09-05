# Design: public release of the loopgraph engine

*2026-09-05*

## Goal

Publish this repo on GitHub so a stranger can clone it, run one command, and get
a working engine. Today it runs on exactly one machine.

Success is one test, run at the end: clone the repo into a temp directory as if I
were a new user, run `install.sh`, and drive the shipped example run all the way
to a merge decision. Nothing short of that counts as ready.

## What blocks a stranger today

1. `docker-compose.yml` hardcodes three `/home/you` paths.
2. The `Dockerfile` creates its worker user with no uid control. It lands on 1000
   and matches the owner by luck. On a machine where the human is uid 1001, the
   agent writes files its owner cannot edit.
3. `.env.example` assumes a local CLIProxyAPI on port 8317.
4. Approving a run requires a Telegram bot. A new user cannot finish a first run
   without talking to BotFather.
5. The `lg` command on PATH is a two-line wrapper outside the repo with both
   paths baked in.
6. The skill that makes the engine usable day to day lives in the owner's home
   directory and is not shipped.
7. `runs/` is tracked and holds real client work.
8. No README, no LICENSE, no install path of any kind.

## 1. Runs stay here; only examples are published

`runs/` keeps living in the engine repo. Git stops tracking the owner's real runs.

`.gitignore` gains an ignore-everything-then-allow-examples rule:

```
runs/*
!runs/m1-demo/
!runs/m2-toy/
!runs/m3-accept/
!runs/m3-planted-lie/
!runs/m4-cards/
!runs/example-hello/
```

Safe by default is the point. A new run is invisible to git without anyone
deciding anything or remembering a rule. Only the named examples are tracked.

Untrack, do not delete: `git rm -r --cached` the four dated runs
(`2026-09-04-macwebsite-backtotop`, `2026-09-04-toy-count`,
`2026-09-05-microbits-fact-corrections`, `2026-09-05-microbits-ideas-sharpen`).
The files stay on disk and keep working locally.

**Ignoring them is not enough by itself.** All four are already in commit
`7b55745`, and `.gitignore` never touches history. The microbits brief names a
real client, and its two executor and audit logs are 57KB of an agent reasoning
about that client's sales report. Both would ship inside the published history and
survive in clones and caches after any later delete.

The repo has no remote and two commits, so the fix costs nothing: squash to a
single fresh initial commit before the first push. This is a release gate, not a
nice-to-have. A test enforces the result (see Testing).

`HANDOVER_PROMPT.md` still goes. It is a note to an agent about the author's PC.

Add `runs/example-hello/`: brief, constraints, gates and a scope check that run
against a throwaway git repo `install.sh` creates. This is what the README
quickstart points at and what the end-to-end test drives.

`SPEC.md` stays. Replace "the owner" with "the owner" and leave the doctrine alone.

## 2. Configuration

One `.env` at the repo root, already gitignored, holds everything. Compose reads
it for `${VAR}` substitution *and* mounts it into the worker, so there is one file
to edit and no generated compose file.

Verified: Compose substitutes variables inside `env_file:` paths and volume
mounts, and `${VAR:?message}` aborts with that message when the variable is
missing. That is the whole mechanism.

`docker-compose.yml` changes:

| now | becomes |
|---|---|
| `/home/you/.config/loopgraph-telegram.env` | `${LOOPGRAPH_TELEGRAM_ENV:?run ./install.sh}` |
| `/home/you/projects:/projects` | `${LOOPGRAPH_PROJECTS_DIR:?run ./install.sh}:/projects` |
| `/home/you/.npm:/home/worker/.npm` | `${LOOPGRAPH_NPM_CACHE}:/home/worker/.npm` |

The telegram entry goes back to `required: false`, because a user who skips
Telegram must still be able to start the worker. That reverses a change made
earlier today, so the fail-fast behaviour it bought is preserved a different way:
`install.sh` writes `LOOPGRAPH_REQUIRE_TELEGRAM=1` when the user configures a bot,
and `worker.py` refuses to start if that flag is set and the credentials are
missing. Same protection against a run stalling on a card that never arrives,
without blocking someone who never wanted cards.

`Dockerfile` takes `ARG LOOPGRAPH_UID=1000` and passes it to `useradd -u`.
Compose passes the caller's real uid as a build arg.

## 3. Executor credentials

No code change. `.env.example` and the README document two routes and recommend
the first:

- **CLIProxyAPI plus a Claude subscription.** Install CLIProxyAPI, sign in, set
  `ANTHROPIC_BASE_URL` to the local proxy and `ANTHROPIC_AUTH_TOKEN` to its local
  key. Recommended, and the README says why: a run spends up to three executor
  rounds plus an audit pass, so metered billing adds up fast.
- **A plain Anthropic API key.** Set `ANTHROPIC_API_KEY`, leave
  `ANTHROPIC_BASE_URL` unset. Simpler, costs per token.

`install.sh` asks which one and writes the right lines.

## 4. Approval without Telegram

Both decision points in `workflows/run.py` (`_ask_owner` and `_owner_card`) send a
card and then wait on the `wait_decision` activity, which long-polls Telegram.

Add a second route in: a Temporal signal, `decide(letter)`, and an
`lg approve <workflow-id> <letter>` command that sends it. The workflow waits on
whichever arrives first, the button tap or the command, and cancels the loser.

When Telegram is not configured, skip `send_card` entirely, log the exact
`lg approve` line to type, and wait on the signal alone. A first run then needs no
bot.

Configured means both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are present in
the worker environment. The check belongs in an activity, not the workflow, since
workflow code must stay deterministic.

Merge cards stay buttons-only for free text (`accept_text=False`). The signal is
an explicit letter, so it is safe on both card kinds.

The README recommends Telegram anyway, plainly: waiting at a terminal for a
40-minute run to reach a decision is worse than a phone buzzing.

## 5. `install.sh`

Interactive, idempotent, refuses to guess. In order:

1. Check `docker`, `docker compose`, `python3` (>=3.13) and `git`. Report what is
   missing and stop.
2. Detect whether docker needs `sudo` (try `docker info`, fall back to
   `sudo docker info`) and write the result to `.env` as `LOOPGRAPH_DOCKER`, since
   `lg where` reports it and the skill reads it from there.
3. Ask where the user's projects live. Default `~/projects`. This is the only tree
   the worker can reach, so the README calls that out.
4. Ask which credential route (section 3) and collect the values.
5. Offer the Telegram step: explain BotFather in three lines, take a token, resolve
   the chat id from `getUpdates` after the user presses Start, and write
   `~/.config/loopgraph-telegram.env` at mode 600. Skipping is a first-class
   choice, not a failure, and prints how to add it later.
6. Write `.env` (mode 600) and back up any existing one.
7. Install the `lg` wrapper into `~/.local/bin`, generated with this checkout's
   path. Warn if `~/.local/bin` is not on PATH.
8. Offer to link the skill (section 9).
9. `docker compose up -d --build`, wait for the worker, then run the example.

Never echoes a token. Never puts one on a command line.

## 6. `INSTALL_WITH_AGENT.md`

A short document aimed at Claude Code and similar. It states what to collect from
the user, tells the agent to run `install.sh` rather than improvise, and lists the
three failures worth recognising: no PATH entry for `~/.local/bin`, docker needing
sudo, and a projects directory outside the mounted tree. It stays thin on purpose;
the script is what does the work, and two documents that drift apart are worse
than one.

## 7. README

Sections, in order:

1. What it is, in four sentences. Temporal owns the graph, LangGraph owns the loop
   inside a round, gates are code that must exit 0, and a separate audit agent
   checks the work against the diff.
2. Why you would want it: the agent cannot mark its own homework, and the run is
   durable, so a crash resumes rather than restarts.
3. Quickstart: clone, `./install.sh`, run the example, approve it.
4. How a run is defined: `brief.md`, `constraints.md`, `gates.yaml`, the write set
   and the scope gate.
5. Watching a run: the dashboard on 8400, the Temporal UI on 8233, `lg tail`.
6. Approving: Telegram cards or `lg approve`.
7. Using it from a coding agent: the skill.
8. Honest limits. One run per target repo at a time. Target repos must live under
   the mounted projects tree. The executor runs with permissions bypassed inside
   a container, so read what a brief allows before you start it. Linux and Docker
   only; nobody has tried it on macOS.

## 8. LICENSE

MIT.

## 9. Ship the skill

`skills/loopgraph/SKILL.md` moves into the repo. `install.sh` symlinks it to
`~/.claude/skills/loopgraph`, so `git pull` updates it. If that path already
exists the script stops and asks rather than clobbering.

The shipped copy must not hardcode anything. Add `lg where`, which prints the
engine root, the projects directory, the docker command (with or without sudo),
the dashboard ports, and whether Telegram is configured (and which bot). The skill
asks `lg where` instead of assuming, so one shipped file works everywhere.

Rewrite from the current text: drop the owner's name from the description and
body, drop `~/projects/loopgraph`, drop the hardcoded `sudo`, name whatever bot
the user configured rather than the author's, point the scope-gate example at
`runs/example-hello/check-write-set.sh`, and add the `lg approve` route to the
cards section.

Also add an `AGENTS.md` at the repo root, for agents working *on* loopgraph. That
is a different audience from the skill, which is for agents that *use* it.

## Testing

- The 53 existing tests keep passing.
- New unit tests: the Telegram-configured check, signal-versus-activity race
  resolution, and `lg where` output.
- New: a publish-safety test over `git ls-files` that fails when any tracked file
  matches a credential shape (bot token, `sk-` key, webhook URL), contains a
  literal `/home/` path, or sits under `runs/` without being on the example
  allowlist. Scope: source, config and the skill. `docs/architecture.*` and
  `SPEC.md` are exempt from the path rule only after a check confirms they hold
  none.
- The same test run against the full history (`git log --all --name-only`) once,
  as the release gate, to prove the squash actually removed the client runs.
- The end-to-end clone-and-install run described under Goal. Manual, and it gates
  the release.

## Out of scope

Approval buttons in the web dashboard (`ui.py` is read-only today, and `lg approve`
covers the need). Publishing to PyPI. macOS support. CI.
