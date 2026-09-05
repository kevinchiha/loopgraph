"""M1 workflow: run the declared gates; a red gate blocks the run.

Blocking = the workflow fails (ApplicationError) and never completes. The
status query exposes per-gate red/green before and after the failure.
The full LoopGraphRun (rounds, cadences, signals) lands in M3 — same file.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from activities.audit import audit
    from activities.checkpoint import checkpoint, merge
    from activities.execute_round import execute_round
    from activities.gate import run_gates
    from activities.learn import learn
    from activities.notify import send_card, telegram_configured, wait_decision


@workflow.defn
class GateCheckRun:
    def __init__(self) -> None:
        self._results: list[dict] = []

    @workflow.run
    async def run(self, gates_path: str, workdir: str) -> dict:
        self._results = await workflow.execute_activity(
            run_gates,
            args=[gates_path, workdir],
            start_to_close_timeout=timedelta(minutes=30),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        red = [g["name"] for g in self._results if g["status"] == "red"]
        if red:
            raise ApplicationError(f"gates red: {', '.join(red)}", type="GatesRed")
        return {"status": "green", "gates": [g["name"] for g in self._results]}

    @workflow.query
    def status(self) -> dict:
        return {
            "gates": [
                {"name": g["name"], "status": g["status"], "exit_code": g["exit_code"], "note": g["note"]}
                for g in self._results
            ],
            "red": [g["name"] for g in self._results if g["status"] == "red"],
        }


@workflow.defn
class RoundRun:
    """M2: one executor round on a work item. The full LoopGraphRun lands in M3."""

    def __init__(self) -> None:
        self._result: dict | None = None

    @workflow.run
    async def run(self, run_dir: str, target_repo: str, work_item: str, round_no: int = 1) -> dict:
        self._result = await workflow.execute_activity(
            execute_round,
            args=[run_dir, target_repo, work_item, round_no],
            start_to_close_timeout=timedelta(hours=1),
            heartbeat_timeout=timedelta(minutes=3),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
        return self._result

    @workflow.query
    def status(self) -> dict:
        return self._result or {"state": "running"}


MAX_ROUNDS = 3  # initial round + 2 supervisor redos: cap-3 doctrine, then escalate


def _swallow(task: asyncio.Task) -> None:
    """Loser of a decision race. Retrieve its outcome so the loop stays quiet."""
    if not task.cancelled():
        task.exception()


@workflow.defn
class LoopGraphRun:
    """The full run: round → audit → verdict branch. The ledger is workflow state.

    accept  → checkpoint the exact write set → merge-ready
    redo    → bounded correction round carrying the directive (the unit, not the batch)
    plan/stop, an escalated round, or the redo cap → stopped (M4 turns this into a card)
    """

    def __init__(self) -> None:
        self._ledger: dict = {"status": "running", "rounds": [], "checkpoint": None}
        self._target_repo: str = ""
        self._decisions: list[str] = []

    @workflow.run
    async def run(self, run_dir: str, target_repo: str, work_item: str = "") -> dict:
        self._target_repo = target_repo
        directive = None
        for round_no in range(1, MAX_ROUNDS + 1):
            result = await workflow.execute_activity(
                execute_round,
                args=[run_dir, target_repo, work_item, round_no, directive],
                start_to_close_timeout=timedelta(hours=2),  # correction loop may re-run slow gates
                heartbeat_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
            entry = {
                "round": round_no,
                "status": result["status"],
                "attempts": result["attempts"],
                "claims": result["claims"],
                "files": result["files"],
                "worktree": result["worktree"],
                "branch": result["branch"],
                "base_branch": result["base_branch"],
                "directive": directive,
            }
            self._ledger["rounds"].append(entry)
            if result["status"] != "green":
                self._ledger.update(status="stopped",
                                    reason="round escalated: gates red after correction cap")
                break

            verdict = await workflow.execute_activity(
                audit,
                args=[run_dir, result, round_no],
                start_to_close_timeout=timedelta(minutes=30),
                heartbeat_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
            entry["verdict"] = verdict["verdict"]
            entry["verdict_reasons"] = verdict["reasons"]

            if verdict["verdict"] == "accept":
                cp = await workflow.execute_activity(
                    checkpoint,
                    args=[run_dir, result["worktree"], result["files"], round_no, result["summary"]],
                    start_to_close_timeout=timedelta(minutes=45),  # gate re-run may be a full build
                    heartbeat_timeout=timedelta(minutes=3),
                    retry_policy=RetryPolicy(maximum_attempts=2),
                )
                self._ledger["checkpoint"] = cp
                if not cp["committed"]:
                    self._ledger.update(status="stopped",
                                        reason=f"checkpoint refused: {cp['reason']}")
                    break
                self._ledger["learn"] = await workflow.execute_activity(
                    learn,
                    args=[run_dir, result, verdict],
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
                self._ledger.update(status="merge-ready")
                await self._owner_card(run_dir, result, cp)
                break
            if verdict["verdict"] in ("plan", "stop"):
                self._ledger.update(status="stopped",
                                    reason=f"supervisor verdict: {verdict['verdict']}",
                                    reasons=verdict["reasons"])
                break
            if verdict["verdict"] == "ask":
                d = verdict["directive"]
                question = d.get("action", "Supervisor needs an owner decision")
                reply = await self._ask_owner(run_dir, question, verdict.get("options") or {})
                entry["owner_question"] = question
                entry["owner_reply"] = reply
                directive = (
                    f"Context: {d.get('context', '')}\n"
                    f"Owner was asked: {question}\nOwner replied: {reply}\n"
                    f"Verify: {d.get('verify', '')}\nStop: {d.get('stop', '')}"
                )
                continue
            d = verdict["directive"]
            directive = (
                f"Context: {d.get('context', '')}\nAction: {d.get('action', '')}\n"
                f"Verify: {d.get('verify', '')}\nStop: {d.get('stop', '')}"
            )
        else:
            self._ledger.update(status="stopped", reason="redo cap reached")
        return self._ledger

    @workflow.signal
    def decide(self, value: str) -> None:
        """Where `lg approve <workflow-id> <value>` lands. Queued rather than
        overwritten, so an answer that arrives early is not lost."""
        self._decisions.append(str(value))

    def _peek(self, allowed: set[str] | None) -> int | None:
        """Index of the first queued answer this card will accept, or None."""
        for i, raw in enumerate(self._decisions):
            v = raw.strip()
            if v and (allowed is None or v[:1].upper() in allowed):
                return i
        return None

    async def _await_decision(self, run_dir: str, kind: str, summary: str,
                              commit: str | None, options: dict,
                              accept_text: bool) -> str:
        """Hold until the owner answers, by Telegram button or by `lg approve`.
        Whichever lands first wins; the loser is cancelled.

        With no Telegram configured the card is skipped and the signal is the only
        way in, so a first run works without a bot. When a card takes only letters
        (merges), a signal carrying anything else is ignored rather than guessed at.
        """
        wf_id = workflow.info().workflow_id
        allowed = None if accept_text else set(options)
        telegram = await workflow.execute_activity(
            telegram_configured,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        hint = f"lg approve {wf_id} <{'|'.join(options) or 'answer'}>"
        self._ledger["awaiting"] = {"kind": kind, "options": options,
                                    "telegram": telegram, "answer_with": hint}
        if telegram:
            await workflow.execute_activity(
                send_card,
                args=[kind, wf_id, run_dir, summary, commit, options],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        else:
            workflow.logger.warning("no Telegram configured; answer with: %s", hint)

        sig = asyncio.create_task(workflow.wait_condition(lambda: self._peek(allowed) is not None))
        tasks: list[asyncio.Task] = [sig]
        poll: asyncio.Task | None = None
        if telegram:
            poll = asyncio.create_task(workflow.execute_activity(
                wait_decision,
                args=[wf_id, accept_text],
                start_to_close_timeout=timedelta(days=2),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=100),  # durable indefinite wait
            ))
            tasks.append(poll)
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
            t.add_done_callback(_swallow)
        self._ledger.pop("awaiting", None)
        if sig in done:
            value = self._decisions.pop(self._peek(allowed)).strip()
            return value[:1].upper() if allowed else value
        return poll.result()["value"]

    async def _ask_owner(self, run_dir: str, question: str, options: dict) -> str:
        """Supervisor `ask`: a question the owner answers by button, text or signal."""
        return await self._await_decision(run_dir, "decision", question, None,
                                          options, accept_text=True)

    async def _owner_card(self, run_dir: str, result: dict, cp: dict) -> None:
        """Merge-ready: hold at a safe no-change state until the owner decides."""
        letter = await self._await_decision(
            run_dir, "merge-ready", result["summary"], cp["commit"],
            {"A": "merge into " + (result["base_branch"] or "base") + " (local, no push)",
             "B": "keep the branch, don't merge",
             "C": "discard the run"},
            accept_text=False,  # buttons or an explicit letter; stray text never merges
        )
        self._ledger["owner_decision"] = letter
        if letter == "A":
            merged = await workflow.execute_activity(
                merge,
                args=[self._target_repo, result["base_branch"], result["branch"]],
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
            self._ledger["merge"] = merged
            self._ledger.update(status="merged" if merged["merged"] else "merge-failed")
            if not merged["merged"]:
                self._ledger["reason"] = merged["reason"]
        else:
            self._ledger.update(status="held" if letter == "B" else "discarded")

    @workflow.query
    def ledger(self) -> dict:
        return self._ledger
