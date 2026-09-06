"""A scripted stand-in for the `workflow` module, shared by the driven tests.

`workflows.run` reaches Temporal through one module-level name, `workflow`, so
swapping that name for this object runs a real workflow method to completion in
a plain event loop and records what it actually scheduled. Source pins can only
say what the file reads like; these say what the run would do. The suite has no
`WorkflowEnvironment` on purpose: it would put a downloaded test-server binary in
the gate's path.

Every answer is scripted by activity name, and a script repeats its last entry
once the run outlasts it, so a test spells out only the calls it cares about.

A run driven to the end reads `held`, never `merge-ready`: the fake answers every
card with `B` and `_owner_card` writes `held` over `merge-ready` when the owner
keeps the branch. So assert on `cards`, on `ledger["reason"]` and on
`ledger["sweep"]["ended"]`, never on the final status.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

# AC-1's defaults: what `load_run_config` returns for a run with no run.yaml.
DEFAULT_CONFIG = {"convergence": {"enabled": True, "every_items": 5, "net_lines": 400},
                  "sweep": None}

GREEN_ROUND = {
    "status": "green", "attempts": 1, "claims": ["did the thing"],
    "files": ["a.py"], "summary": "did the thing", "worktree": "/app/runs/x/worktrees/ab12cd",
    "branch": "lg-x-ab12cd", "base_branch": "main", "self_committed": False,
    "candidates": [],
}

ACCEPT = {"verdict": "accept", "reasons": ["it does what it says"], "directive": {}}

COMMITTED = {"committed": True, "commit": "c1", "files": ["a.py"],
             "leftovers": [], "dropped_ignored": [], "added": 2, "deleted": 0, "net": 2}

START = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)


class _Script:
    """One answer per call, with the last entry repeating for the rest of the run.

    A run takes more rounds and more passes than a test wants to spell out, and a
    list that ran out would fail on the call after the interesting one rather
    than on the thing the test is about.
    """

    def __init__(self, entries, default) -> None:
        self._entries = list(entries) if entries else [default]
        self._i = 0

    def next(self):
        entry = self._entries[min(self._i, len(self._entries) - 1)]
        self._i += 1
        return dict(entry) if isinstance(entry, dict) else entry


class ScriptedWorkflow:
    """The `workflow` module, answering by activity name from a script."""

    MAX_WAITS = 10  # a card that never accepts an answer fails instead of hanging

    def __init__(self, config=None, rounds=None, verdicts=None, checkpoints=None,
                 passes=None, clock=None, items=None, fails=None) -> None:
        self._config = config if config is not None else DEFAULT_CONFIG
        self._rounds = _Script(rounds, GREEN_ROUND)
        self._verdicts = _Script(verdicts, ACCEPT)
        self._checkpoints = _Script(checkpoints, COMMITTED)
        self._passes = _Script(passes, {})
        self._clock = _Script(clock, START)
        self._items = list(items) if items is not None else ["the item"]
        self._fails = fails or {}
        self._waits = 0
        self.run = None                            # the LoopGraphRun being driven
        self.scheduled: list[tuple] = []           # (name, args, kwargs), in order
        self.cards: list[list] = []                # the args of every send_card
        self.awaiting_when_sent: list[dict] = []   # the ledger at each send_card
        self.logger = logging.getLogger("scripted-workflow")

    def info(self):
        return SimpleNamespace(workflow_id="run-x-ab12cd")

    def now(self) -> datetime:
        return self._clock.next()

    async def execute_activity(self, fn, args=None, **kwargs):
        name = fn.__name__
        self.scheduled.append((name, list(args or []), dict(kwargs)))
        if name in self._fails:
            raise self._fails[name]
        if name == "load_run_config":
            return dict(self._config)
        if name == "run_baseline":
            return "base1234"
        if name == "load_work_items":
            return list(self._items)
        if name == "execute_round":
            return self._rounds.next()
        if name == "audit":
            return self._verdicts.next()
        if name == "checkpoint":
            return self._checkpoints.next()
        if name == "discover":
            return self._passes.next()
        if name == "telegram_configured":
            return True
        if name == "send_card":
            self.cards.append(list(args or []))
            self.awaiting_when_sent.append(dict((self.run._ledger.get("awaiting") or {})
                                                if self.run else {}))
        return {}

    async def wait_condition(self, predicate):
        """The dispatcher signalling the waiting run. Always `B`: keep the branch,
        which is the one answer that changes nothing outside the run."""
        self._waits += 1
        assert self._waits <= self.MAX_WAITS, "a card never accepted the answer"
        self.run.decide("B")
        assert predicate()


def _swap(fake, wf, coro):
    """Run one coroutine with `workflows.run.workflow` swapped for the fake."""
    import workflows.run as run_module

    fake.run = wf
    original = run_module.workflow
    run_module.workflow = fake
    try:
        return asyncio.run(coro)
    finally:
        run_module.workflow = original


def drive(fake, run_dir: str = "runs/x", target_repo: str = "/projects/x") -> dict:
    """Run a whole LoopGraphRun against the fake and hand back its ledger."""
    import workflows.run as run_module

    wf = run_module.LoopGraphRun()
    return _swap(fake, wf, wf.run(run_dir, target_repo))


def drive_item(fake, kind: str = "brief", work_item: str = "the item") -> tuple:
    """Run `_run_item` as item 1 of a one-item ledger. Returns (outcome, run).

    `run` fills the ledger before it calls this, so a test that starts here has
    to do the same: `_run_item` counts the items for the auditor's `item_total`.
    """
    import workflows.run as run_module

    wf = run_module.LoopGraphRun()
    wf._ledger["items"] = [{"n": 1, "item": work_item, "status": "running", "kind": kind}]
    if kind == "sweep":
        wf._ledger["sweep"] = {"passes": [], "ended": None}
    outcome = _swap(fake, wf, wf._run_item("runs/x", "/projects/x", work_item, 1, None, kind))
    return outcome, wf
