"""The only thing that talks to Telegram.

Telegram allows one `getUpdates` per bot at a time, and text replies carry
nothing saying which run they answer. So every run polling for itself gave two
failures at once: concurrent polls fought over the connection (409), and whichever
run polled first swallowed whatever was pending, including another run's answer.

One poller fixes both. It reads each update once, works out which run it belongs
to (activities/route.py), and signals that workflow. Workflows then only ever wait
on a signal, which is what `lg approve` has always used, so there is one way in
rather than two.

Runs for as long as the stack is up. If it is down, cards still go out and nothing
comes back: a run holds at its decision, unchanged, until it is running again.
"""

from __future__ import annotations

import asyncio
import os

import httpx
from temporalio.client import Client

from activities.notify import configured
from activities.route import route_update

API = "https://api.telegram.org"
COMPLETED = "already completed"
OPEN_RUNS = 'WorkflowType = "LoopGraphRun" AND ExecutionStatus = "Running"'


async def open_run_ids(client: Client) -> list[str] | None:
    """Runs that could be waiting on an answer, or None when that could not be
    determined. The difference matters: returning an empty list on an outage made
    the bot tell the owner "no run is waiting" while one was, and drop the
    message. Routing degrades, it does not lie."""
    try:
        return [w.id async for w in client.list_workflows(OPEN_RUNS)]
    except Exception as e:
        print(f"dispatcher: could not list open runs: {e}", flush=True)
        return None


async def deliver(client: Client, hit: dict) -> str:
    """Signal the run. `decide` is the same handler `lg approve` uses."""
    handle = client.get_workflow_handle(hit["wf_id"])
    await handle.signal("decide", hit["value"])
    return f"{hit['value'][:40]} -> {hit['wf_id']}"


def explain_signal_failure(error: str, status: str | None) -> str:
    """The toast for an answer that could not be delivered.

    "that run is not accepting answers" reads like a broken engine. The usual
    cause is a second tap on a card whose run finished on the first one: a button
    stays tappable forever, and Telegram gives no sign the card is spent. Say the
    run is over, and how it ended, so the owner knows their first tap worked."""
    if COMPLETED in error:
        return f"that run already finished ({status})" if status else "that run already finished"
    return "could not reach that run just now, try again"


async def final_status(client: Client, wf_id: str) -> str | None:
    """How a finished run ended, for the toast. Best effort: querying a workflow
    whose history predates a code change raises, and a clearer message is not
    worth turning into a second failure."""
    try:
        ledger = await client.get_workflow_handle(wf_id).query("ledger")
        return str(ledger.get("status") or "") or None
    except Exception:
        return None


async def pump(client: Client, http: httpx.AsyncClient, token: str, chat: str,
               offset: int) -> int:
    r = await http.post(f"{API}/bot{token}/getUpdates",
                        json={"timeout": 25, "offset": offset})
    if r.status_code >= 300:
        print(f"dispatcher: getUpdates {r.status_code}: {r.text[:200]}", flush=True)
        await asyncio.sleep(5)
        return offset
    updates = r.json().get("result", [])
    if not updates:
        return offset
    # Fetch the open runs for every batch. Deciding from the shape of the updates
    # meant a tap on a card older than ~48h — Telegram sends an InaccessibleMessage
    # with no text — routed or was refused depending on whether an unrelated update
    # happened to arrive in the same 25-second batch.
    ids = await open_run_ids(client)
    for u in updates:
        try:
            # Advance first and keep it. Losing this on a later failure re-fetched
            # the whole batch and acted on it again: duplicate decisions, repeated
            # messages to the owner, and every later update in the batch stuck
            # behind the failing one, once every five seconds, forever.
            offset = max(offset, u.get("update_id", offset - 1) + 1)
            await handle(client, http, token, chat, u, ids)
        except Exception as e:  # one bad update must never stop the pump
            print(f"dispatcher: update {u.get('update_id')} failed: {e}", flush=True)
    return offset


async def handle(client: Client, http: httpx.AsyncClient, token: str, chat: str,
                 update: dict, ids: list[str]) -> None:
    hit = route_update(update, chat, ids)
    if not hit:
        return
    if hit.get("problem"):
        print(f"dispatcher: {hit['problem']}", flush=True)
        await http.post(f"{API}/bot{token}/sendMessage",
                        json={"chat_id": chat, "text": f"loopgraph: {hit['problem']}"})
        if hit.get("callback_id"):
            await http.post(f"{API}/bot{token}/answerCallbackQuery",
                            json={"callback_query_id": hit["callback_id"],
                                  "text": "could not place that"})
        return
    try:
        print("dispatcher: " + await deliver(client, hit), flush=True)
        note = f"{hit['value'][:20]} recorded"
        problem = None
    except Exception as e:
        print(f"dispatcher: could not signal {hit['wf_id']}: {e}", flush=True)
        problem = str(e)[:200]
        note = explain_signal_failure(problem, await final_status(client, hit["wf_id"]))
    if hit.get("callback_id"):
        await http.post(f"{API}/bot{token}/answerCallbackQuery",
                        json={"callback_query_id": hit["callback_id"], "text": note})
    elif problem:
        # A typed answer has no toast, so without this the owner's reply would
        # vanish leaving only a line in a container log.
        # The raw exception stays in the log. The owner gets the reason.
        await http.post(f"{API}/bot{token}/sendMessage",
                        json={"chat_id": chat, "text": f"loopgraph: {note}"})


async def main() -> None:
    if not configured():
        raise SystemExit(
            "No Telegram credentials. The dispatcher is the only thing that reads "
            "your replies, so without them no run can ever be answered from your "
            "phone. See README.md, or run ./install.sh.")
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    client = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"))
    offset = 0
    async with httpx.AsyncClient(timeout=40) as http:
        # Say nothing until Telegram has actually answered. Printing the ready
        # line first meant a dead bot token still passed the installer's health
        # check, and the owner was told the stack was fine when no reply could
        # ever reach a run.
        me = await http.post(f"{API}/bot{token}/getMe")
        if me.status_code >= 300:
            raise SystemExit(
                f"Telegram rejected the bot token ({me.status_code}). No reply could "
                f"ever reach a run. Check LOOPGRAPH_TELEGRAM_ENV, or rotate the token "
                f"with @BotFather and re-run ./install.sh.")
        who = me.json().get("result", {}).get("username", "?")
        print(f"dispatcher up, listening for replies on @{who}", flush=True)
        # Start past whatever is already queued: messages sent before this started
        # cannot be answers to a card it has not seen.
        r = await http.post(f"{API}/bot{token}/getUpdates", json={"timeout": 0, "offset": -1})
        if r.status_code < 300:
            backlog = r.json().get("result", [])
            if backlog:
                offset = backlog[-1]["update_id"] + 1
        while True:
            try:
                offset = await pump(client, http, token, chat, offset)
            except Exception as e:  # never let one bad cycle end the process
                print(f"dispatcher: cycle failed: {e}", flush=True)
                await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
