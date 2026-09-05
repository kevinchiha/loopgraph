"""Sending owner decision cards. Reading replies is dispatcher.py's job.

A card is a sendMessage with an inline keyboard. The answer comes back as a
Temporal signal, because the dispatcher is the only thing that reads Telegram: it
routes each update to the run it names and signals that workflow. Runs used to
poll for themselves, which meant concurrent runs fought over the one getUpdates
connection Telegram allows and stole each other's text replies.

No reply → the run holds at a safe no-change state, waiting on workflow state
rather than on a retrying activity, per SPEC.

Creds come from a file mounted by compose (LOOPGRAPH_TELEGRAM_ENV) and are never
copied. Give loopgraph its own bot: getUpdates consumes an update, so sharing one
with another tool means whoever polls first eats the other's messages.

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
    else:
        # force_reply makes the answer quote this card, and the card names its own
        # workflow, so the dispatcher can route a typed reply with no bookkeeping.
        # Mutually exclusive with an inline keyboard, which is fine: buttons carry
        # the run id themselves.
        body["reply_markup"] = {"force_reply": True}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{API}/bot{token}/sendMessage", json=body)
    if r.status_code >= 300:
        raise RuntimeError(f"telegram sendMessage {r.status_code}: {r.text[:300]}")
    if activity.in_activity():
        activity.heartbeat("card sent")
    return {"sent": True, "kind": kind}
