# Installing loopgraph with a coding agent

Paste this into Claude Code (or any coding agent) from inside a clone of this repo.

---

Install the loopgraph engine in this repository. `./install.sh` does the actual
work. Your job is to gather what it needs, run it, and read the failures properly.

**Collect from me first, in one message, and do not guess any of it:**

1. Which directory holds my code repositories. The worker mounts exactly one tree
   and can reach nothing outside it, so every repo I want the engine to work on
   must live under it. Default `~/projects`.
2. How I reach Claude: a local CLIProxyAPI with a Claude subscription
   (recommended, ask for the base URL and its local api-key), or a plain Anthropic
   API key. Tell me a run can spend three executor rounds plus an audit pass, so
   metered billing is the expensive choice.
3. Whether I want Telegram decision cards. Explain the trade honestly: without it
   I answer runs with `lg approve <workflow-id> A` in a terminal; with it the same
   question arrives on my phone with buttons, which matters when a run takes forty
   minutes to need me. If I say yes, walk me through @BotFather and remind me to
   press Start in the new bot's chat, because a bot cannot message me first.

**Then run `./install.sh`** and answer its prompts with what I gave you. Do not
reimplement what it does. If it fails, fix the cause and run it again; it is safe
to re-run and it backs up an existing `.env`.

**Three failures worth recognising:**

- `~/.local/bin` missing from PATH. The install works but `lg` is not found. Tell
  me the exact line to add to my shell rc, and use the full path meanwhile.
- Docker needing `sudo`. The script detects this and records it in `.env` as
  `LOOPGRAPH_DOCKER`. Use that value, do not assume plain `docker`.
- A projects directory outside the mounted tree. If I later ask for a run against
  a repo somewhere else, it will not work. Say so and stop rather than improvising
  a mount.

**Verify before you tell me it worked.** Not "the script exited 0":

```bash
lg where                                                # paths, ports, Telegram
<docker> compose ps                                     # worker must be Up
lg start runs/example-hello /projects/loopgraph-example
```

That last one runs the real thing end to end and stops at a decision. Tell me how
to answer it, then report what the ledger says.
