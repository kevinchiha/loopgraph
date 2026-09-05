# Installing loopgraph

This is the supported install path. Paste this into Claude Code, or any coding
agent that reads files and runs commands. It works from an empty directory:

> Install loopgraph from https://github.com/kevinchiha/loopgraph.
> Clone it, then follow INSTALL_WITH_AGENT.md in the repo.

Everything below is written for the agent.

---

## Your job

Wire this engine to this machine, then prove it works by running something real.
`./install.sh` does the mechanical work. You gather what it needs, run it, read
failures properly, and verify the result.

**Do not reimplement what the script does.** Not the `.env`, not the `lg` wrapper,
not the compose invocation. It handles machine differences you will not think of
until they bite: docker needing `sudo`, a uid that isn't 1000, `~/.local/bin`
missing from PATH, an existing `.env` worth backing up. A hand-rolled install
produces a setup nobody can reproduce, including you, next week.

## 1. Ask for three things, in one message

Ask all three at once. Do not start work and interrupt them three times.

**Where your code repositories live.** The worker mounts exactly one directory
tree and can reach nothing outside it, so every repo you want the engine to work
on has to live under it. Default `~/projects`. Say this constraint out loud when
you ask, because it decides where they put future projects.

**How you reach Claude.** Two routes:

- CLIProxyAPI with a Claude subscription. Ask for the base URL (usually
  `http://127.0.0.1:8317`) and its local api-key. Recommended.
- A plain Anthropic API key.

Tell them why it matters, in one line: a single run can spend three executor
rounds plus an audit pass, so per-token billing adds up faster than they expect.

**A Telegram bot.** This one is required, and the install will stop without it.
Say why rather than just demanding it: runs stop and ask questions, and the engine
refuses to start without a way to reach them, because a run nobody is told about
waits silently. Walk them through @BotFather: `/newbot`, pick a name, pick a
username ending in `bot`, and then **press Start in the new bot's chat**, because a
bot cannot send the first message. The script takes the token and finds the chat id
itself. If they push back, the honest answer is that `lg approve` can answer a
waiting run from a terminal but cannot tell them there is one.

## 2. Clone if you need to, then run the script

If you are not already inside a checkout, clone it somewhere sensible first. Ask
where if the user has a preference; `~/projects/loopgraph` is a reasonable default,
and note that the engine's own checkout does not have to live under the projects
tree it mounts.

```bash
git clone https://github.com/kevinchiha/loopgraph.git
cd loopgraph
./install.sh
```

Answer its prompts with what they gave you. It is safe to re-run and it backs up
an existing `.env`. If it fails, fix the cause and run it again.

## 3. Failures worth recognising

**`lg: command not found` after a clean install.** `~/.local/bin` is not on their
PATH. Give them the exact line for their shell rc, and use the full path meanwhile.

**Docker needing sudo.** The script detects this and records it in `.env` as
`LOOPGRAPH_DOCKER`. Read that value and use it. Do not assume plain `docker`.

**A projects directory that doesn't cover what they care about.** If they later
ask for a run against a repo outside the mounted tree, it cannot work. Say so and
stop. Do not improvise a mount.

**The worker not coming up.** Read `<docker> compose logs worker`. The most common
cause is missing Telegram credentials, which stops the worker at startup on
purpose. Check that `LOOPGRAPH_TELEGRAM_ENV` in `.env` points at a file that holds
both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

## 4. Prove it, then report

"The script exited 0" is not verification. Run all three:

```bash
lg where                                                # paths, ports, Telegram
<docker> compose ps                                     # worker must be Up
lg start runs/example-hello /projects/loopgraph-example
```

The last one is the real test. It takes a few minutes, implements a `--hello`
flag, gates it, audits it, and then holds at a decision. Tell them how to answer:
`lg approve <workflow-id> A` to merge, or a tap on the Telegram card if they set
one up. Then report what the ledger says.

**If that run escalates instead of reaching a decision**, read the ledger's
`reason` and the executor's claims before blaming the install. An escalation with
a gate defect written up in the claims means a gate is wrong, not that the engine
is broken. Report it with the gate's output, verbatim. Never loosen a gate to make
a run pass.
