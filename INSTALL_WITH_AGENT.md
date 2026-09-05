# Installing loopgraph

This is the supported install path. Open this repo in Claude Code, or any coding
agent that reads files and runs commands, and say:

> Install loopgraph. Follow INSTALL_WITH_AGENT.md.

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

**Whether they want Telegram decision cards.** Give the trade honestly. Without
it, they answer a waiting run with `lg approve <workflow-id> A` in a terminal.
With it, the same question arrives on their phone as a card with buttons, which
matters when a run takes forty minutes to reach the point of needing them. If yes,
walk them through @BotFather: `/newbot`, pick a name, pick a username ending in
`bot`, and then **press Start in the new bot's chat**, because a bot cannot send
the first message. The script takes the token and finds the chat id itself.

## 2. Run the script

```bash
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
cause is `LOOPGRAPH_REQUIRE_TELEGRAM=1` with credentials that did not get written,
which fails at startup on purpose so a run never stalls on a card that was never
going to arrive.

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
