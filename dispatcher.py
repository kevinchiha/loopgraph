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
OPEN_RUNS = 'WorkflowType = "LoopGraphRun" AND ExecutionStatus = "Running"'


async def open_run_ids(client: Client) -> list[str]:
    """Runs that could be waiting on an answer. Only used when an update cannot
    identify its own run."""
    try:
        return [w.id async for w in client.list_workflows(OPEN_RUNS)]
    except Exception:  # visibility can lag or be unavailable; routing degrades, not dies
        return []


async def deliver(client: Client, hit: dict) -> str:
    """Signal the run. `decide` is the same handler `lg approve` uses."""
    handle = client.get_workflow_handle(hit["wf_id"])
    await handle.signal("decide", hit["value"])
    return f"{hit['value'][:40]} -> {hit['wf_id']}"


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
    ids = await open_run_ids(client) if any(
        not (u.get("callback_query") or {}).get("message") for u in updates) else []
    for u in updates:
        offset = max(offset, u["update_id"] + 1)
        try:
            hit = route_update(u, chat, ids)
        except Exception as e:  # a malformed update must never stop the pump
            print(f"dispatcher: could not read update {u.get('update_id')}: {e}", flush=True)
            continue
        if not hit:
            continue
        if hit.get("problem"):
            print(f"dispatcher: {hit['problem']}", flush=True)
            await http.post(f"{API}/bot{token}/sendMessage",
                            json={"chat_id": chat, "text": f"loopgraph: {hit['problem']}"})
            if hit.get("callback_id"):
                await http.post(f"{API}/bot{token}/answerCallbackQuery",
                                json={"callback_query_id": hit["callback_id"],
                                      "text": "could not place that"})
            continue
        try:
            print("dispatcher: " + await deliver(client, hit), flush=True)
            note = f"{hit['value'][:20]} recorded"
        except Exception as e:
            print(f"dispatcher: could not signal {hit['wf_id']}: {e}", flush=True)
            note = "that run is not accepting answers"
        if hit.get("callback_id"):
            await http.post(f"{API}/bot{token}/answerCallbackQuery",
                            json={"callback_query_id": hit["callback_id"], "text": note})
    return offset


async def main() -> None:
    if not configured():
        raise SystemExit(
            "No Telegram credentials. The dispatcher is the only thing that reads "
            "your replies, so without them no run can ever be answered from your "
            "phone. See README.md, or run ./install.sh.")
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    client = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"))
    print("dispatcher up, listening for replies", flush=True)
    offset = 0
    async with httpx.AsyncClient(timeout=40) as http:
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
