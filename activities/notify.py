"""Owner decision cards via Telegram (@LoopGraphBot — loopgraph's own bot).

Card = sendMessage with an inline keyboard; The owner's tap is a callback_query that
the wait_decision activity picks up via getUpdates long-polling. No public URL,
no extra process — Temporal retries make the wait durable. No reply → the run
holds at a safe no-change state (the activity just keeps polling), per SPEC.

Creds come from ~/.config/loopgraph-telegram.env through compose env_file — never
copied. This bot exists only for loopgraph, deliberately not @Kimipaseobot (general
notifications): getUpdates consumes an update, so a shared bot would let loopgraph's
long-poll swallow messages meant for something else.

Builders and the callback matcher are pure and tested.
"""

from __future__ import annotations

import os

import httpx
from temporalio import activity

API = "https://api.telegram.org"


# ---------- pure helpers (tested) ----------

def build_card_text(kind: str, wf_id: str, run_dir: str, summary: str,
                    commit: str | None, options: dict[str, str]) -> str:
    lines = [f"loopgraph: {kind}", f"run: {run_dir}", f"workflow: {wf_id}"]
    if commit:
        lines.append(f"commit: {commit[:10]}")
    lines.append("")
    lines.append(summary.strip()[:1500])
    lines.append("")
    lines.extend(f"{letter} — {label}" for letter, label in options.items())
    return "\n".join(lines)[:4000]  # telegram message cap is 4096


CB_ID_CAP = 48  # Telegram caps callback_data at 64 bytes; "lg::" plus a letter is 5


def cb_key(wf_id: str) -> str:
    """The part of the workflow id that goes in a button.

    callback_data has a hard 64-byte limit, and a run directory named after a real
    feature blows past it: every card with buttons then failed with a 400 and the
    owner got nothing. The tail of the id is the random part, so trimming from the
    left keeps what actually distinguishes one run from another.
    """
    return wf_id[-CB_ID_CAP:]


def build_keyboard(wf_id: str, options: dict[str, str]) -> dict:
    return {"inline_keyboard": [[
        {"text": letter, "callback_data": f"lg:{cb_key(wf_id)}:{letter}"} for letter in options
    ]]}


def extract_reply(updates: list[dict], wf_id: str, chat_id: str,
                  allowed: set[str] | None = None) -> tuple[str, str, str | None, int] | None:
    """Find the owner's reply: a button tap on THIS run's card, or a free-text
    message from the owner chat (answers a supervisor `ask` card).

    Returns (kind, value, callback_id, update_id). A tap is always a deliberate
    answer, so buttons win over stray text within a batch; free text from any
    other chat, and /commands, are ignored.

    `allowed` is the set of letters the CURRENT card offers. Without it, a tap on
    an older card of the same run — still sitting in the chat, still tappable —
    answered whatever card happened to be waiting. The update_id lets the caller
    confirm exactly what it consumed and nothing more.
    """
    prefix = f"lg:{cb_key(wf_id)}:"
    for u in updates:
        cb = u.get("callback_query")
        if cb and str(cb.get("data", "")).startswith(prefix):
            letter = cb["data"][len(prefix):][:1].upper()
            if letter and (allowed is None or letter in allowed):
                return ("button", letter, cb["id"], u["update_id"])
    for u in updates:
        m = u.get("message")
        if (m and m.get("text") and not m["text"].startswith("/")
                and str(m.get("chat", {}).get("id")) == str(chat_id)):
            return ("text", m["text"][:2000], None, u["update_id"])
    return None


def configured() -> bool:
    """Whether cards can be sent. Required: worker.py refuses to start without it,
    because a run stops and asks questions and one nobody is told about just waits.
    `lg approve` answers a waiting run from a terminal, but cannot announce one."""
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def _creds() -> tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set. Point "
            "LOOPGRAPH_TELEGRAM_ENV at a credentials file (see .env.example); "
            "./install.sh sets this up.")
    return token, chat


# ---------- activities ----------

@activity.defn
async def telegram_configured() -> bool:
    """Asked by the workflow before it sends a card. Reading the environment is
    not deterministic, so it cannot live in workflow code."""
    return configured()


@activity.defn
async def send_card(kind: str, wf_id: str, run_dir: str, summary: str,
                    commit: str | None, options: dict[str, str]) -> dict:
    token, chat = _creds()
    body = {
        "chat_id": chat,
        "text": build_card_text(kind, wf_id, run_dir, summary, commit, options),
    }
    if options:  # no options = free-text reply expected, no buttons
        body["reply_markup"] = build_keyboard(wf_id, options)
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{API}/bot{token}/sendMessage", json=body)
    if r.status_code >= 300:
        raise RuntimeError(f"telegram sendMessage {r.status_code}: {r.text[:300]}")
    if activity.in_activity():
        activity.heartbeat("card sent")
    return {"sent": True, "kind": kind}


def accept_hit(hit: tuple | None, accept_text: bool) -> bool:
    """A button tap always counts; free text only counts when the card asked for it
    (merge-ready cards are buttons-only — a stray message must not decide a merge)."""
    return bool(hit) and (hit[0] == "button" or accept_text)


@activity.defn
async def poll_reply(wf_id: str) -> dict:
    """One non-blocking look for an owner reply, for use mid-run.

    A parked item does not stop the run, so this cannot block the way
    wait_decision does. It takes whatever is pending, acknowledges it so the same
    message is not read twice, and returns immediately. No Telegram, no reply,
    empty dict either way."""
    if not configured():
        return {}
    token, chat = _creds()
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(f"{API}/bot{token}/getUpdates", json={"timeout": 0, "offset": 0})
        if r.status_code >= 300:
            return {}
        updates = r.json().get("result", [])
        if not updates:
            return {}
        hit = extract_reply(updates, wf_id, chat)
        # Confirm only up to what was actually used. Acknowledging everything read
        # while returning at most one deleted the rest: a second owner message, or
        # a tap still waiting for another card, was silently destroyed.
        through = hit[3] if hit else None
        if through is None:
            return {}
        await client.post(f"{API}/bot{token}/getUpdates",
                          json={"timeout": 0, "offset": through + 1})
    return {"kind": hit[0], "value": hit[1]}


@activity.defn
async def wait_decision(wf_id: str, accept_text: bool = True,
                        options: list[str] | None = None) -> dict:
    """Long-poll getUpdates until the owner taps a button (or replies by text, if allowed).

    Starts from the current high-water mark: backlog messages (anything sent
    before this wait began) are never answers to this card."""
    token, chat = _creds()
    offset = 0
    # Skip the backlog on the FIRST attempt only. A retry means the worker
    # restarted or Telegram hiccuped, and the owner may well have answered during
    # the gap. Re-skipping to the queue head on every attempt threw that answer
    # away and left the run waiting for a reply it had already been given.
    first_try = activity.info().attempt == 1 if activity.in_activity() else True
    async with httpx.AsyncClient(timeout=40) as client:
        if first_try:
            r = await client.post(f"{API}/bot{token}/getUpdates", json={"timeout": 0, "offset": -1})
            if r.status_code < 300:
                backlog = r.json().get("result", [])
                if backlog:
                    offset = backlog[-1]["update_id"] + 1
        while True:
            r = await client.post(f"{API}/bot{token}/getUpdates",
                                  json={"timeout": 25, "offset": offset})
            if r.status_code >= 300:
                raise RuntimeError(f"telegram getUpdates {r.status_code}: {r.text[:300]}")
            updates = r.json().get("result", [])
            for u in updates:
                offset = max(offset, u["update_id"] + 1)
            if activity.in_activity():
                activity.heartbeat(f"polling, {len(updates)} updates")
            hit = extract_reply(updates, wf_id, chat,
                                None if accept_text else set(options or []))
            if accept_hit(hit, accept_text):
                kind, value, cb_id, _ = hit
                if cb_id:
                    await client.post(f"{API}/bot{token}/answerCallbackQuery",
                                      json={"callback_query_id": cb_id, "text": f"{value} recorded"})
                # Confirm the answer before returning. Telegram only drops an
                # update once a getUpdates asks past it, so without this the tap
                # that decided this card stayed pending and the next poll_reply
                # read it again and fed it to the executor as a fresh instruction
                # the owner never sent.
                await client.post(f"{API}/bot{token}/getUpdates",
                                  json={"timeout": 0, "offset": offset})
                return {"kind": kind, "value": value}
