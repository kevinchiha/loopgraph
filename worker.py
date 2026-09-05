"""Temporal worker: registers workflows + activities, runs forever."""

from __future__ import annotations

import asyncio
import os

from temporalio.client import Client
from temporalio.worker import Worker

from activities.audit import audit
from activities.checkpoint import checkpoint, discard, merge
from activities.execute_round import execute_round, run_baseline
from activities.gate import run_gates
from activities.items import load_work_items
from activities.learn import learn
from activities.notify import configured as telegram_ok
from activities.notify import send_card, telegram_configured
from activities.owner import record_owner_answer
from workflows.run import GateCheckRun, LoopGraphRun, RoundRun

TASK_QUEUE = "loopgraph"


def check_telegram() -> None:
    """Refuse to start without a way to reach the owner.

    Any run can stop mid-flight and ask a question only the owner can answer, and
    every run ends by asking whether to merge. Without a notification channel that
    is a run nobody is watching: it waits, silently, for as long as the owner
    happens not to look. Failing here beats failing when the first card is due."""
    if not telegram_ok():
        raise SystemExit(
            "No Telegram credentials. The engine will not start without a way to "
            "reach you: runs stop and ask questions, and one that nobody sees just "
            "waits.\n"
            "Fix: point LOOPGRAPH_TELEGRAM_ENV in .env at a file holding "
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID. `./install.sh` sets this up, "
            "and README.md has the @BotFather steps."
        )


async def main() -> None:
    client = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"))
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[GateCheckRun, RoundRun, LoopGraphRun],
        activities=[run_gates, execute_round, run_baseline, audit, checkpoint, merge,
                    discard, learn,
                    load_work_items, send_card, telegram_configured,
                    record_owner_answer],
    )
    print(f"worker up on task queue {TASK_QUEUE!r}", flush=True)
    await worker.run()


if __name__ == "__main__":
    check_telegram()
    asyncio.run(main())
