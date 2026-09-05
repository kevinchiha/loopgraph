"""Working out which run a Telegram update is answering.

One bot, one inbox, several runs. Button taps carry an id. Text replies carry
nothing at all, which is why two concurrent runs used to steal each other's
answers: whichever run polled first took whatever was pending.

Every card prints `workflow: run-<slug>-<hex>` in its own text, so a reply that
quotes a card identifies its run for free. That is the whole trick, and it is why
question cards are sent with force_reply.

Pure functions only. The dispatcher does the I/O.
"""

from __future__ import annotations

import re

WF_IN_CARD = re.compile(r"^workflow:\s*(\S+)\s*$", re.MULTILINE)
CB_DATA = re.compile(r"^lg:(?P<key>.+):(?P<letter>[A-Za-z])$")


def wf_from_card(text: str | None) -> str | None:
    """The workflow id a card names, read back out of the card itself."""
    m = WF_IN_CARD.search(text or "")
    return m.group(1) if m else None


def resolve_by_suffix(key: str, open_ids: list[str]) -> str | None:
    """Match a truncated callback key back to a full workflow id.

    callback_data is capped at 64 bytes, so a long run name only carries its tail.
    Exactly one match counts: two runs sharing a tail is ambiguous, and guessing
    which one the owner meant is how a merge lands on the wrong branch.
    """
    hits = [w for w in open_ids if w.endswith(key)]
    return hits[0] if len(hits) == 1 else None


def route_update(update: dict, chat_id: str, open_ids: list[str] | None) -> dict | None:
    """Where one update should go.

    Returns {"wf_id", "value", "callback_id"} to signal a run, or a dict with
    "problem" when the update came from the owner but cannot be placed, or None
    when it is not ours to act on.
    """
    cb = update.get("callback_query")
    if cb:
        m = CB_DATA.match(str(cb.get("data", "")))
        if not m:
            return None
        card = (cb.get("message") or {}).get("text")
        wf = wf_from_card(card) or resolve_by_suffix(m.group("key"), open_ids or [])
        if not wf:
            return {"problem": "tapped a card whose run I could not identify",
                    "callback_id": cb.get("id")}
        return {"wf_id": wf, "value": m.group("letter").upper(), "callback_id": cb.get("id")}

    msg = update.get("message") or update.get("edited_message")
    if not msg or not msg.get("text"):
        return None
    if str(msg.get("chat", {}).get("id")) != str(chat_id):
        return None  # not the owner
    text = msg["text"]
    if text.startswith("/"):
        return None  # a bot command, not an answer

    quoted = (msg.get("reply_to_message") or {}).get("text")
    wf = wf_from_card(quoted)
    if wf:
        return {"wf_id": wf, "value": text[:2000], "callback_id": None}
    if open_ids is None:
        # Not the same as "nothing is running". Saying so, rather than guessing or
        # claiming nothing is waiting, is the difference between a lost answer and
        # an owner who knows to try again.
        return {"problem": "I could not check which runs are open, so I cannot place "
                           "that. Try again in a moment, or answer with "
                           "`lg approve <workflow-id> <letter>`."}
    if len(open_ids) == 1:
        # Unambiguous: only one run could possibly be asking.
        return {"wf_id": open_ids[0], "value": text[:2000], "callback_id": None}
    if not open_ids:
        return {"problem": "no run is waiting for an answer"}
    return {"problem": f"{len(open_ids)} runs are in flight, so I cannot tell which "
                       f"one this answers. Reply to that run's card instead of "
                       f"sending a new message."}
