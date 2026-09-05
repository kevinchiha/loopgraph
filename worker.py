"""Temporal worker: registers workflows + activities, runs forever."""

from __future__ import annotations

import asyncio
import os

from temporalio.client import Client
from temporalio.worker import Worker

from activities.audit import audit
from activities.checkpoint import checkpoint, merge
from activities.execute_round import execute_round
from activities.gate import run_gates
from activities.learn import learn
from activities.notify import configured as telegram_ok
from activities.notify import send_card, telegram_configured, wait_decision
from workflows.run import GateCheckRun, LoopGraphRun, RoundRun

TASK_QUEUE = "loopgraph"


def check_telegram() -> None:
    """Fail at startup, not when a card is due. Without this, a run reaches
    merge-ready and then waits forever on a card that was never going to arrive."""
    if os.environ.get("LOOPGRAPH_REQUIRE_TELEGRAM") == "1" and not telegram_ok():
        raise SystemExit(
            "LOOPGRAPH_REQUIRE_TELEGRAM=1 but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID "
            "are not set. Check that LOOPGRAPH_TELEGRAM_ENV in .env points at your "
            "credentials file, or set LOOPGRAPH_REQUIRE_TELEGRAM=0 to answer runs "
            "with `lg approve` instead."
        )


async def main() -> None:
    client = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"))
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[GateCheckRun, RoundRun, LoopGraphRun],
        activities=[run_gates, execute_round, audit, checkpoint, merge, learn,
                    send_card, wait_decision, telegram_configured],
    )
    print(f"worker up on task queue {TASK_QUEUE!r}", flush=True)
    await worker.run()


if __name__ == "__main__":
    check_telegram()
    asyncio.run(main())
