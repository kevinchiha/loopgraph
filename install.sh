#!/usr/bin/env bash
# Set up the loopgraph engine on this machine. Safe to re-run: it backs up an
# existing .env and never overwrites credentials you already have.
#
#   ./install.sh          ask about everything
#   ./install.sh --yes    take every default, skip Telegram (for scripted installs)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
YES=0
case "${1:-}" in -y|--yes) YES=1 ;; esac

bold(){ printf '\033[1m%s\033[0m\n' "$*"; }
say(){  printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn(){ printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die(){  printf '\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

ask(){  # ask <prompt> <default>
  local ans=""
  if [ "$YES" = 1 ] || [ ! -t 0 ]; then printf '%s' "$2"; return; fi
  read -r -p "$1 [$2]: " ans </dev/tty || true
  printf '%s' "${ans:-$2}"
}
ask_secret(){  # ask_secret <prompt> <current>   — never echoes the current value
  local ans="" hint="blank to skip"
  [ -n "$2" ] && hint="Enter keeps the one already in .env"
  if [ "$YES" = 1 ] || [ ! -t 0 ]; then printf '%s' "$2"; return; fi
  read -r -s -p "$1 ($hint): " ans </dev/tty; echo >/dev/tty
  printf '%s' "${ans:-$2}"
}
confirm(){  # confirm <prompt>   (default no)
  local ans=""
  if [ "$YES" = 1 ] || [ ! -t 0 ]; then return 1; fi
  read -r -p "$1 [y/N]: " ans </dev/tty || true
  case "$ans" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# ---------------------------------------------------------------- 1. checks ---
say "Checking what's installed"
need(){ command -v "$1" >/dev/null 2>&1 || MISSING="$MISSING $1"; }
MISSING=""
need docker; need git; need python3
[ -n "$MISSING" ] && die "missing:$MISSING — install those first, then re-run."
docker compose version >/dev/null 2>&1 \
  || die "'docker compose' not available. Install the Compose v2 plugin."
# This compose file uses long-form env_file entries (path:/required:), which
# Compose only understands from 2.24. Older versions fail with a parse error that
# says nothing about the version, so check it here instead.
COMPOSE_V="$(docker compose version --short 2>/dev/null | tr -d 'v')"
if [ -n "$COMPOSE_V" ]; then
  printf '2.24\n%s\n' "$COMPOSE_V" | sort -V -C \
    || die "Docker Compose $COMPOSE_V is too old; this needs 2.24 or newer (long-form env_file)."
fi
python3 - <<'PY' || die "python 3.13+ required for the host venv (the container brings its own)."
import sys; sys.exit(0 if sys.version_info >= (3, 13) else 1)
PY
echo "  docker, compose, git, python $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])') — ok"

# Does docker need sudo here? The skill and the README both need to know.
if docker info >/dev/null 2>&1; then
  DOCKER="docker"
elif sudo -n docker info >/dev/null 2>&1 || sudo docker info >/dev/null 2>&1; then
  DOCKER="sudo docker"
  echo "  docker needs sudo on this machine"
else
  die "cannot talk to docker, with or without sudo. Is the daemon running?"
fi

# ------------------------------------------------------------- 2. questions ---
# Re-running must not wipe a working setup. Read what is already in .env and use
# it as the default for every question, so pressing Enter keeps what you had.
# Before this, the credential questions defaulted to empty and a re-run that
# accepted the defaults blanked the model credential, then restarted the stack.
old_env(){ [ -f "$ROOT/.env" ] && sed -n "s/^$1=//p" "$ROOT/.env" | head -1 || true; }
PREV_BASE="$(old_env ANTHROPIC_BASE_URL)"
PREV_TOK="$(old_env ANTHROPIC_AUTH_TOKEN)"
PREV_KEY="$(old_env ANTHROPIC_API_KEY)"
PREV_MODEL="$(old_env ANTHROPIC_MODEL)"
PREV_PROJECTS="$(old_env LOOPGRAPH_PROJECTS_DIR)"
[ -f "$ROOT/.env" ] && say "Found an existing .env — press Enter at any prompt to keep what it has"

say "Where your code lives"
cat <<'TXT'
The worker can only reach ONE directory tree, mounted into the container as
/projects. Every repo you want the engine to work on has to live under it.
TXT
PROJECTS="$(ask "Your projects directory" "${PREV_PROJECTS:-$HOME/projects}")"
PROJECTS="${PROJECTS/#\~/$HOME}"
[ -d "$PROJECTS" ] || { mkdir -p "$PROJECTS"; echo "  created $PROJECTS"; }

say "How the engine talks to Claude"
cat <<'TXT'
  1) CLIProxyAPI with a Claude subscription  (recommended)
     Runs locally, signs in with your existing subscription, speaks the Anthropic
     API. A run can spend three executor rounds plus an audit pass, so metered
     billing adds up fast. https://github.com/router-for-me/CLIProxyAPI
  2) A plain Anthropic API key
     Simpler to start, billed per token.
TXT
ROUTE="$(ask "Route (1 or 2)" "$([ -n "$PREV_KEY" ] && echo 2 || echo 1)")"
MODEL="$(ask "Model id" "${PREV_MODEL:-claude-opus-5}")"
AUTH_LINES=""
if [ "$ROUTE" = "2" ]; then
  KEY="$(ask_secret "  Anthropic API key" "$PREV_KEY")"
  AUTH_LINES="ANTHROPIC_API_KEY=$KEY"
else
  BASE="$(ask "CLIProxyAPI base url" "${PREV_BASE:-http://127.0.0.1:8317}")"
  TOK="$(ask_secret "  CLIProxyAPI local api-key" "$PREV_TOK")"
  AUTH_LINES="ANTHROPIC_BASE_URL=$BASE
ANTHROPIC_AUTH_TOKEN=$TOK"
fi

# -------------------------------------------------------------- 3. telegram ---
TELEGRAM_ENV="$HOME/.config/loopgraph-telegram.env"
HAVE_TELEGRAM=0
BOT_NAME=""
say "Telegram (required)"
cat <<'TXT'
A run stops and asks you: before it merges anything, and any time the auditor hits
a call only you can make. The engine will not start without a way to reach you,
because a run nobody is told about just waits, silently, for as long as you happen
not to look. It takes two minutes to set up.
TXT
if [ -s "$TELEGRAM_ENV" ]; then
  echo "  already configured at $TELEGRAM_ENV — leaving it alone"
  HAVE_TELEGRAM=1
  BOT_NAME="$(sed -n 's/^# *Bot: *//p' "$TELEGRAM_ENV" | head -1)"
elif [ "$YES" = 1 ] || [ ! -t 0 ]; then
  warn "Non-interactive install: skipping the Telegram step."
  warn "The worker will refuse to start until you configure it. Re-run ./install.sh"
  warn "interactively, or write TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID into"
  warn "$TELEGRAM_ENV (mode 600) yourself."
else
  cat <<'TXT'

  1. In Telegram, message @BotFather and send /newbot
  2. Give it a name, then a username ending in "bot"
  3. It replies with a token like 8123456789:AA...
  4. Open your new bot's chat and press Start (it cannot message you first)
TXT
  read -r -s -p "  Paste the token (hidden): " BOT_TOKEN </dev/tty; echo
  if [ -n "$BOT_TOKEN" ]; then
    BOT_NAME="$(BOT_TOKEN="$BOT_TOKEN" python3 - <<'PY'
import json, os, urllib.request
try:
    with urllib.request.urlopen(
        f"https://api.telegram.org/bot{os.environ['BOT_TOKEN']}/getMe", timeout=15) as r:
        print("@" + json.load(r)["result"]["username"])
except Exception:
    print("")
PY
)"
    [ -n "$BOT_NAME" ] || die "Telegram rejected that token. Check it and re-run."
    echo "  bot is $BOT_NAME"
    CHAT_ID="$(BOT_TOKEN="$BOT_TOKEN" python3 - <<'PY'
import json, os, urllib.request
try:
    with urllib.request.urlopen(
        f"https://api.telegram.org/bot{os.environ['BOT_TOKEN']}/getUpdates", timeout=20) as r:
        for u in reversed(json.load(r)["result"]):
            m = u.get("message") or u.get("edited_message") or {}
            if m.get("chat", {}).get("type") == "private":
                print(m["chat"]["id"]); break
except Exception:
    pass
PY
)"
    if [ -z "$CHAT_ID" ]; then
      warn "No message from you yet. Press Start in $BOT_NAME's chat, then:"
      read -r -p "  press Enter once you have..." _ </dev/tty || true
      CHAT_ID="$(BOT_TOKEN="$BOT_TOKEN" python3 - <<'PY'
import json, os, urllib.request
try:
    with urllib.request.urlopen(
        f"https://api.telegram.org/bot{os.environ['BOT_TOKEN']}/getUpdates", timeout=20) as r:
        for u in reversed(json.load(r)["result"]):
            m = u.get("message") or u.get("edited_message") or {}
            if m.get("chat", {}).get("type") == "private":
                print(m["chat"]["id"]); break
except Exception:
    pass
PY
)"
    fi
    if [ -n "$CHAT_ID" ]; then
      mkdir -p "$(dirname "$TELEGRAM_ENV")"
      ( umask 077
        printf '# loopgraph decision cards.\n# Bot: %s\nTELEGRAM_BOT_TOKEN=%s\nTELEGRAM_CHAT_ID=%s\n' \
          "$BOT_NAME" "$BOT_TOKEN" "$CHAT_ID" > "$TELEGRAM_ENV" )
      chmod 600 "$TELEGRAM_ENV"
      HAVE_TELEGRAM=1
      echo "  wrote $TELEGRAM_ENV (mode 600)"
    else
      die "No chat id: the bot has never heard from you. Press Start in $BOT_NAME's chat, then re-run ./install.sh."
    fi
    unset BOT_TOKEN
  else
    die "No token given. The engine needs a way to reach you; re-run ./install.sh when you have one."
  fi
fi

# ------------------------------------------------------------------ 4. .env ---
say "Writing .env"
if [ -f "$ROOT/.env" ]; then
  BACKUP="$ROOT/.env.backup.$(date +%Y%m%d%H%M%S)"
  ( umask 077; cp "$ROOT/.env" "$BACKUP" )
  chmod 600 "$BACKUP"
  echo "  backed up your existing .env (mode 600)"
fi
( umask 077
  cat > "$ROOT/.env" <<EOF
# Written by install.sh. Never commit this file.
$AUTH_LINES
ANTHROPIC_MODEL=$MODEL

LOOPGRAPH_PROJECTS_DIR=$PROJECTS
LOOPGRAPH_NPM_CACHE=$HOME/.npm
LOOPGRAPH_UID=$(id -u)
LOOPGRAPH_DOCKER=$DOCKER

LOOPGRAPH_TELEGRAM_ENV=$TELEGRAM_ENV
LOOPGRAPH_TELEGRAM_BOT=$BOT_NAME
EOF
)
chmod 600 "$ROOT/.env"
mkdir -p "$HOME/.npm"
echo "  $ROOT/.env (mode 600)"

# ------------------------------------------------------------------- 5. venv ---
say "Host python environment (for the lg command)"
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  if command -v uv >/dev/null 2>&1; then
    (cd "$ROOT" && uv sync --quiet)
  else
    python3 -m venv "$ROOT/.venv"
    "$ROOT/.venv/bin/pip" install --quiet --upgrade pip
    # Same set as the container minus claude-agent-sdk, which only runs in there.
    "$ROOT/.venv/bin/pip" install --quiet temporalio pyyaml httpx langgraph pytest
  fi
fi
echo "  $ROOT/.venv"

say "Installing the lg command"
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/lg" <<EOF
#!/bin/sh
exec "$ROOT/.venv/bin/python" "$ROOT/lg" "\$@"
EOF
chmod +x "$HOME/.local/bin/lg"
echo "  $HOME/.local/bin/lg"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) warn "$HOME/.local/bin is not on your PATH. Add this to your shell rc:"
     echo '      export PATH="$HOME/.local/bin:$PATH"' ;;
esac

# ------------------------------------------------------------------ 6. skill ---
SKILL_SRC="$ROOT/skills/loopgraph"
SKILL_DST="$HOME/.claude/skills/loopgraph"
if [ -d "$SKILL_SRC" ]; then
  say "Claude Code skill"
  if [ -e "$SKILL_DST" ] || [ -L "$SKILL_DST" ]; then
    echo "  $SKILL_DST already exists — leaving it alone"
  elif [ "$YES" = 1 ] || confirm "Link the loopgraph skill into ~/.claude/skills?"; then
    mkdir -p "$(dirname "$SKILL_DST")"
    ln -s "$SKILL_SRC" "$SKILL_DST"
    echo "  linked $SKILL_DST -> $SKILL_SRC (git pull keeps it current)"
  fi
fi

# ------------------------------------------------------------------- 7. up ----
if [ "$HAVE_TELEGRAM" = 0 ]; then
  say "Not starting the stack"
  warn "Telegram is not configured, so the worker would refuse to start."
  warn "Configure it, then: cd $ROOT && $DOCKER compose up -d --build"
  exit 0
fi

say "Starting the stack"
(cd "$ROOT" && $DOCKER compose up -d --build)
printf '  waiting for the worker'
for _ in $(seq 60); do
  if (cd "$ROOT" && $DOCKER compose logs worker 2>/dev/null | grep -q "worker up on task queue"); then
    printf ' up\n'; break
  fi
  printf '.'; sleep 2
done
(cd "$ROOT" && $DOCKER compose logs worker 2>/dev/null | grep -q "worker up on task queue") \
  || { (cd "$ROOT" && $DOCKER compose logs worker | tail -20); die "worker did not come up (log above)"; }

# --------------------------------------------------------------- 8. example ---
EXAMPLE="$PROJECTS/loopgraph-example"
if [ ! -d "$EXAMPLE" ]; then
  say "Creating the example target repo"
  mkdir -p "$EXAMPLE"
  cat > "$EXAMPLE/cli.py" <<'EOF'
import argparse


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--version", action="store_true")
    args = p.parse_args()
    if args.version:
        print("example 0.1")


if __name__ == "__main__":
    main()
EOF
  cat > "$EXAMPLE/test_cli.py" <<'EOF'
import subprocess
import sys


def test_version():
    out = subprocess.run([sys.executable, "cli.py", "--version"],
                         capture_output=True, text=True)
    assert out.stdout.strip() == "example 0.1"
EOF
  cat > "$EXAMPLE/.gitignore" <<'EOF'
# Gates that run pytest write these into the worktree. Without this, a scope gate
# reading `git status --porcelain` reports them as out-of-scope changes and no
# write set can ever be green.
__pycache__/
.pytest_cache/
*.pyc
EOF
  git -C "$EXAMPLE" init -q
  git -C "$EXAMPLE" add -A
  git -C "$EXAMPLE" -c user.email=you@example.com -c user.name=you \
      commit -qm "example target repo for loopgraph"
  echo "  $EXAMPLE"
fi

say "Done"
bold "Try it:"
cat <<EOF
  lg where                                        # paths and ports on this machine
  lg start runs/example-hello /projects/loopgraph-example
EOF
echo "  Cards land in $BOT_NAME on Telegram. From a terminal: lg approve <workflow-id> A"
echo "  Watch a run: lg ui   (dashboard on http://localhost:8400; it is not a"
echo "               compose service, so nothing has started it yet)"
echo "  Temporal's own UI: http://localhost:8233"
