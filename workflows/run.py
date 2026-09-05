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
    from activities.items import load_work_items
    from activities.learn import learn
    from activities.notify import poll_reply, send_card, telegram_configured, wait_decision


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


def build_merge_summary(summary: str, total: int, parked: list[dict]) -> str:
    """The merge card's text when some items did not make it.

    Says plainly what merging does and does not include, because the one thing
    the owner must not think is that a green card means everything got done."""
    if not parked:
        return summary
    lines = "\n".join(f"- item {e['n']}: {str(e['item'])[:120]} ({e['reason']})" for e in parked)
    kept = total - len(parked)
    return (f"{summary}\n\nParked, NOT in this branch:\n{lines}\n\n"
            f"Merging takes the {kept} item(s) that passed. The parked ones need "
            f"another run.")


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
        self._ledger: dict = {"status": "running", "items": [], "rounds": [], "checkpoint": None}
        self._target_repo: str = ""
        self._decisions: list[str] = []

    @workflow.run
    async def run(self, run_dir: str, target_repo: str, work_item: str = "") -> dict:
        """Work through the brief's items, one at a time, onto one branch.

        An item that will not go green is parked and the run carries on, so one
        bad item does not throw away the ones that worked. The owner hears about
        a park immediately and their reply is picked up before the next item.
        """
        self._target_repo = target_repo
        items = await workflow.execute_activity(
            load_work_items,
            args=[run_dir],
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        if not items:
            items = [work_item or ""]  # no work-items section: the whole brief, one item
        self._ledger["items"] = [{"n": i, "item": it, "status": "pending"}
                                 for i, it in enumerate(items, start=1)]

        carried: str | None = None   # an owner reply, handed to the next item
        accepted: dict | None = None  # last accepted round result, for the final card
        checkpoint_result: dict | None = None

        for i, item in enumerate(items, start=1):
            entry = self._ledger["items"][i - 1]
            entry["status"] = "running"
            outcome = await self._run_item(run_dir, target_repo, item, i, carried)
            carried = None
            if outcome["status"] == "accepted":
                entry["status"] = "done"
                entry["commit"] = outcome["checkpoint"].get("commit")
                accepted, checkpoint_result = outcome["result"], outcome["checkpoint"]
            elif outcome["status"] == "halt":
                entry.update(status="parked", reason=outcome["reason"])
                self._ledger.update(status="stopped", reason=outcome["reason"])
                return self._ledger
            else:
                entry.update(status="parked", reason=outcome["reason"])
                await self._park_note(run_dir, i, len(items), item, outcome["reason"])

            reply = await workflow.execute_activity(
                poll_reply,
                args=[workflow.info().workflow_id],
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
            if reply.get("value"):
                entry.setdefault("owner_notes", []).append(reply["value"])
                carried = f"The owner sent this mid-run, after item {i}: {reply['value']}"

        parked = [e for e in self._ledger["items"] if e["status"] == "parked"]
        if accepted is None:
            self._ledger.update(status="stopped", reason="every work item was parked")
            return self._ledger
        self._ledger.update(status="merge-ready")
        await self._owner_card(run_dir, accepted, checkpoint_result, parked)
        return self._ledger

    async def _run_item(self, run_dir: str, target_repo: str, work_item: str,
                        item_no: int, carried: str | None) -> dict:
        """One work item: rounds until the supervisor accepts, or it is parked.

        Returns accepted (with the result and its checkpoint), parked (the run
        carries on to the next item), or halt (the supervisor said stop, which is
        the one verdict that ends the whole run)."""
        directive = carried
        for round_no in range(1, MAX_ROUNDS + 1):
            result = await workflow.execute_activity(
                execute_round,
                args=[run_dir, target_repo, work_item, round_no, directive, item_no],
                start_to_close_timeout=timedelta(hours=2),  # correction loop may re-run slow gates
                heartbeat_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
            entry = {
                "item_no": item_no,
                "round": round_no,
                "status": result["status"],
                "attempts": result["attempts"],
                "claims": result["claims"],
                "files": result["files"],
                "worktree": result["worktree"],
                "branch": result["branch"],
                "base_branch": result["base_branch"],
                "directive": directive,
                # True when the executor committed its own work and the engine put
                # it back in the working tree. Worth seeing in the ledger: it means
                # the executor ignored a red line in its prompt.
                "self_committed": result.get("self_committed", False),
            }
            self._ledger["rounds"].append(entry)
            if result["status"] != "green":
                return {"status": "parked",
                        "reason": "gates red after the correction cap"}

            verdict = await workflow.execute_activity(
                audit,
                args=[run_dir, result, round_no, item_no],
                start_to_close_timeout=timedelta(minutes=30),
                heartbeat_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
            entry["verdict"] = verdict["verdict"]
            entry["verdict_reasons"] = verdict["reasons"]

            if verdict["verdict"] == "accept":
                cp = await workflow.execute_activity(
                    checkpoint,
                    args=[run_dir, result["worktree"], result["files"], round_no,
                          result["summary"], item_no],
                    start_to_close_timeout=timedelta(minutes=45),  # gate re-run may be a full build
                    heartbeat_timeout=timedelta(minutes=3),
                    retry_policy=RetryPolicy(maximum_attempts=2),
                )
                self._ledger["checkpoint"] = cp
                if not cp["committed"]:
                    return {"status": "parked",
                            "reason": f"checkpoint refused: {cp['reason']}"}
                self._ledger["learn"] = await workflow.execute_activity(
                    learn,
                    args=[run_dir, result, verdict],
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
                return {"status": "accepted", "result": result, "checkpoint": cp}

            if verdict["verdict"] == "stop":
                return {"status": "halt",
                        "reason": f"supervisor said stop: {'; '.join(verdict['reasons'])[:300]}"}
            if verdict["verdict"] == "plan":
                return {"status": "parked",
                        "reason": f"supervisor asked to replan: {'; '.join(verdict['reasons'])[:300]}"}
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
        return {"status": "parked", "reason": "redo cap reached"}

    @workflow.signal
    def decide(self, value: str) -> None:
        """Where `lg approve <workflow-id> <value>` lands. Queued rather than
        overwritten, so an answer that arrives early is not lost."""
        self._decisions.append(str(value))

    def _peek(self, allowed: set[str] | None) -> int | None:
        """Index of the first queued answer this card will accept, or None.

        A letters-only card takes a letter and nothing else. Matching on the first
        character would turn "Abort, do not merge" into A and merge the branch,
        which is the opposite of what the owner typed.
        """
        for i, raw in enumerate(self._decisions):
            v = raw.strip()
            if not v:
                continue
            if allowed is None:
                return i
            if len(v) == 1 and v.upper() in allowed:
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
        # Only an answer sent AFTER this card counts. Anything already queued was
        # meant for an earlier card, or was sent before the owner could have seen
        # this one, and must never decide it.
        if self._decisions:
            self._ledger.setdefault("ignored_answers", []).extend(self._decisions)
            self._decisions = []
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
        # workflow.wait, not asyncio.wait: the Temporal sandbox flags the asyncio one
        # as non-deterministic, and this is the code path that decides a merge.
        done, pending = await workflow.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
            t.add_done_callback(_swallow)
        self._ledger.pop("awaiting", None)
        if sig in done:
            value = self._decisions.pop(self._peek(allowed)).strip()
            return value.upper() if allowed else value
        return poll.result()["value"]

    async def _ask_owner(self, run_dir: str, question: str, options: dict) -> str:
        """Supervisor `ask`: a question the owner answers by button, text or signal."""
        return await self._await_decision(run_dir, "decision", question, None,
                                          options, accept_text=True)

    async def _park_note(self, run_dir: str, item_no: int, total: int,
                         item: str, reason: str) -> None:
        """Tell the owner an item was parked. Does not wait: the run has already
        moved on to the next item, and their reply is picked up between items."""
        wf_id = workflow.info().workflow_id
        text = (f"item {item_no} of {total} parked\n\n{item[:600]}\n\n"
                f"why: {reason}\n\nThe run is carrying on with the rest. Reply here "
                f"with anything the next item should know, or wait for the "
                f"merge-ready card at the end.")
        self._ledger.setdefault("parked_notes", []).append({"item_no": item_no, "reason": reason})
        telegram = await workflow.execute_activity(
            telegram_configured,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        if not telegram:
            workflow.logger.warning("item %d parked: %s", item_no, reason)
            return
        await workflow.execute_activity(
            send_card,
            args=["parked", wf_id, run_dir, text, None, {}],
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

    async def _owner_card(self, run_dir: str, result: dict, cp: dict,
                          parked: list[dict] | None = None) -> None:
        """Merge-ready: hold at a safe no-change state until the owner decides."""
        summary = build_merge_summary(result["summary"], len(self._ledger["items"]),
                                      parked or [])
        letter = await self._await_decision(
            run_dir, "merge-ready", summary, cp["commit"],
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
