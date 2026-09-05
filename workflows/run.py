"""M1 workflow: run the declared gates; a red gate blocks the run.

Blocking = the workflow fails (ApplicationError) and never completes. The
status query exposes per-gate red/green before and after the failure.
The full LoopGraphRun (rounds, cadences, signals) lands in M3 — same file.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from activities.audit import audit
    from activities.checkpoint import checkpoint, discard, merge
    from activities.execute_round import execute_round, run_baseline
    from activities.gate import run_gates
    from activities.items import load_work_items
    from activities.learn import learn
    from activities.notify import send_card, telegram_configured
    from activities.owner import record_owner_answer


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
MAX_ASKS = 3    # owner questions per item. Separate budget, see budget_spent.


def budget_spent(spent: int, asks: int) -> str | None:
    """Why this item must stop, or None to carry on.

    Questions and corrections have separate budgets. A question is the engine
    waiting on the owner, not a failed attempt, and charging it as a round meant
    three questions consumed the whole correction budget: the first live `ask`
    run asked the same thing three times and parked with nothing committed."""
    if asks >= MAX_ASKS:
        return "owner-question cap reached"
    if spent >= MAX_ROUNDS:
        return "redo cap reached"
    return None


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
        # The last checkpoint this run committed. Rounds reset to it rather than to
        # HEAD, so a commit the executor made during a failed attempt can never
        # become the baseline.
        self._base_commit: str | None = None

    @workflow.run
    async def run(self, run_dir: str, target_repo: str, work_item: str = "") -> dict:
        """Work through the brief's items, one at a time, onto one branch.

        An item that will not go green is parked and the run carries on, so one
        bad item does not throw away the ones that worked. The owner hears about
        a park immediately and their reply is picked up before the next item.
        """
        self._target_repo = target_repo
        # Capture the starting commit before anything runs, so the very first round
        # already has a baseline to reset to rather than trusting HEAD.
        self._base_commit = await workflow.execute_activity(
            run_baseline,
            args=[target_repo],
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
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
                await self._stopped_note(run_dir, outcome["reason"])
                return self._ledger
            else:
                entry.update(status="parked", reason=outcome["reason"])
                await self._park_note(run_dir, i, len(items), item, outcome["reason"])

            # Anything the owner sent while that item ran is steering for the next
            # one. It is already in workflow state: the dispatcher signalled it.
            notes = self._drain_decisions()
            if notes:
                entry.setdefault("owner_notes", []).extend(notes)
                carried = ("The owner sent this mid-run, after item "
                           f"{i}: {' / '.join(notes)}")

        parked = [e for e in self._ledger["items"] if e["status"] == "parked"]
        if accepted is None:
            self._ledger.update(status="stopped", reason="every work item was parked")
            await self._stopped_note(run_dir, "every work item was parked")
            return self._ledger
        self._ledger.update(status="merge-ready")
        await self._owner_card(run_dir, accepted, checkpoint_result, parked)
        return self._ledger

    def _run_token(self) -> str:
        """The tail of the workflow id, e.g. "ab12cd". It makes this run's branch
        and worktree its own: deriving them from the run-dir name alone meant a
        re-run inherited the previous run's branch, so work the owner discarded
        came back and was merged by the next one."""
        return workflow.info().workflow_id.rsplit("-", 1)[-1][:12]

    async def _run_item(self, run_dir: str, target_repo: str, work_item: str,
                        item_no: int, carried: str | None) -> dict:
        """One work item: rounds until the supervisor accepts, or it is parked.

        Returns accepted (with the result and its checkpoint), parked (the run
        carries on to the next item), or halt (the supervisor said stop, which is
        the one verdict that ends the whole run)."""
        directive = carried
        spent = 0       # executor passes charged to the correction budget
        asks = 0        # owner questions, which are not charged to it
        round_no = 0    # every pass, for the ledger and the log file names
        answered = False  # this pass carries an owner answer, so it is not a redo
        while True:
            round_no += 1
            if not answered:
                spent += 1
            answered = False
            result = await workflow.execute_activity(
                execute_round,
                args=[run_dir, target_repo, work_item, round_no, directive, item_no,
                      self._run_token(), self._base_commit],
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
                args=[run_dir, result, round_no, item_no, work_item,
                      len(self._ledger["items"])],
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
                if cp.get("commit"):
                    self._base_commit = cp["commit"]
                if not cp["committed"]:
                    return {"status": "parked",
                            "reason": f"checkpoint refused: {cp['reason']}"}
                # Best effort, and it runs AFTER the commit. Letting it raise
                # failed the whole workflow over a distilled sentence, throwing
                # away a run whose work was already safely on the branch.
                try:
                    self._ledger["learn"] = await workflow.execute_activity(
                        learn,
                        args=[run_dir, result, verdict],
                        start_to_close_timeout=timedelta(minutes=10),
                        heartbeat_timeout=timedelta(minutes=3),
                        retry_policy=RetryPolicy(maximum_attempts=1),
                    )
                except Exception as e:  # noqa: BLE001 - never block on the learning edge
                    self._ledger["learn"] = {"skipped": str(e)[:200]}
                return {"status": "accepted", "result": result, "checkpoint": cp}

            if verdict["verdict"] == "stop":
                return {"status": "halt",
                        "reason": f"supervisor said stop: {'; '.join(verdict['reasons'])[:300]}"}
            if verdict["verdict"] == "plan":
                return {"status": "parked",
                        "reason": f"supervisor asked to replan: {'; '.join(verdict['reasons'])[:300]}"}
            if verdict["verdict"] == "ask":
                if asks >= MAX_ASKS:
                    return {"status": "parked", "reason": "owner-question cap reached"}
                asks += 1
                d = verdict["directive"]
                question = d.get("action", "Supervisor needs an owner decision")
                options = verdict.get("options") or {}
                reply = await self._ask_owner(run_dir, question, options)
                entry["owner_question"] = question
                entry["owner_reply"] = reply
                # Write it where the AUDITOR can read it. The supervisor never sees
                # the executor's transcript or its directive, so an answer that went
                # only into the directive left every owner-authorised value looking
                # invented, and the same question came back every round.
                await workflow.execute_activity(
                    record_owner_answer,
                    args=[run_dir, question, reply, options, item_no, round_no],
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
                directive = (
                    f"Context: {d.get('context', '')}\n"
                    f"Owner was asked: {question}\nOwner replied: {reply}\n"
                    f"Verify: {d.get('verify', '')}\nStop: {d.get('stop', '')}"
                )
                answered = True
                continue
            over = budget_spent(spent, asks)
            if over:
                return {"status": "parked", "reason": over}
            d = verdict["directive"]
            directive = (
                f"Context: {d.get('context', '')}\nAction: {d.get('action', '')}\n"
                f"Verify: {d.get('verify', '')}\nStop: {d.get('stop', '')}"
            )

    @workflow.signal
    def decide(self, value: str) -> None:
        """Where `lg approve <workflow-id> <value>` lands. Queued rather than
        overwritten, so an answer that arrives early is not lost."""
        self._decisions.append(str(value))

    def _drain_decisions(self) -> list[str]:
        """Everything queued since the last look, as steering notes for the next
        item. Between items there is no card up, so anything that arrived is a
        comment on the run rather than an answer to a question."""
        notes = [d.strip() for d in self._decisions if d.strip()]
        self._decisions = []
        return notes

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
        # question is the card's own text, not a rebuild of it. The ledger said a
        # run was waiting and never what it had asked, so anything reading the run
        # had to guess the question from the kind and the letters.
        self._ledger["awaiting"] = {"kind": kind, "question": summary, "options": options,
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

        # One way in. A tap on the card and `lg approve` both arrive here as the
        # same signal: the dispatcher is the only thing reading Telegram, and it
        # signals the run the update names. Waiting on workflow state rather than
        # on a long-polling activity is also what makes this wait genuinely
        # durable — there is nothing to retry, restart or re-skip.
        while True:
            await workflow.wait_condition(lambda: bool(self._decisions))
            i = self._peek(allowed)
            if i is not None:
                break
            # Something arrived and this card cannot take it. Saying so beats
            # leaving it in the queue: the owner replied, nothing happened, and
            # the run went on waiting with no way to tell why.
            rejected = self._drain_decisions()
            self._ledger.setdefault("ignored_answers", []).extend(rejected)
            await self._note(
                run_dir, "not an answer",
                f"I could not use that: this card takes {' or '.join(sorted(options))} "
                f"and nothing else.\n\nYou sent: {rejected[0][:200]}\n\n"
                f"Tap a button, or run: {hint}", expect_reply=True)
        self._ledger.pop("awaiting", None)
        value = self._decisions.pop(i).strip()
        return value.upper() if allowed else value

    async def _ask_owner(self, run_dir: str, question: str, options: dict) -> str:
        """Supervisor `ask`: a question the owner answers by button, text or signal."""
        return await self._await_decision(run_dir, "decision", question, None,
                                          options, accept_text=True)

    async def _park_note(self, run_dir: str, item_no: int, total: int,
                         item: str, reason: str) -> None:
        """Tell the owner an item was parked. Does not wait: the run has already
        moved on to the next item, and their reply is picked up between items."""
        text = (f"item {item_no} of {total} parked\n\n{item[:600]}\n\n"
                f"why: {reason}\n\nThe run is carrying on with the rest. Reply here "
                f"with anything the next item should know, or wait for the "
                f"merge-ready card at the end.")
        self._ledger.setdefault("parked_notes", []).append({"item_no": item_no, "reason": reason})
        await self._note(run_dir, "parked", text)

    async def _stopped_note(self, run_dir: str, reason: str) -> None:
        """Tell the owner a run ended. A stop used to return silently, so nobody
        was told the run was over, and any items already committed sat on a branch
        nobody knew about."""
        done = [e for e in self._ledger["items"] if e["status"] == "done"]
        parked = [e for e in self._ledger["items"] if e["status"] == "parked"]
        lines = [f"why: {reason}", "",
                 f"{len(done)} item(s) committed, {len(parked)} parked."]
        if done:
            lines.append("")
            lines.append("Committed work is on the branch and was NOT merged. "
                         "Review it, or start a fresh run.")
            lines.extend(f"- item {e['n']}: {str(e['item'])[:100]}" for e in done)
        if parked:
            lines.append("")
            lines.extend(f"- parked item {e['n']}: {e.get('reason', '')}" for e in parked)
        await self._note(run_dir, "run stopped", "\n".join(lines))

    async def _note(self, run_dir: str, kind: str, text: str,
                    expect_reply: bool = False) -> None:
        """A card with no buttons. Does not wait for anything.

        expect_reply drives force_reply. A stopped-run note used to open a reply
        box on the owner's phone for a run that no longer exists."""
        telegram = await workflow.execute_activity(
            telegram_configured,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        if not telegram:
            workflow.logger.warning("%s: %s", kind, text)
            return
        await workflow.execute_activity(
            send_card,
            args=[kind, workflow.info().workflow_id, run_dir, text, None, {}, expect_reply],
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
        elif letter == "C":
            gone = await workflow.execute_activity(
                discard,
                args=[self._target_repo, result["branch"], result.get("worktree", "")],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
            self._ledger["discard"] = gone
            # Do not report a discard that did not happen. That is the same "the
            # label is a lie" problem C had before it deleted anything at all.
            if gone.get("discarded"):
                self._ledger.update(status="discarded")
            else:
                self._ledger.update(status="discard-failed", reason=gone.get("reason", ""))
                await self._note(
                    run_dir, "discard failed",
                    f"You chose C, but the branch is still there.\n\n"
                    f"branch: {result['branch']}\nwhy: {gone.get('reason', '')}\n\n"
                    f"Nothing was merged. Delete it by hand when you can.")
        else:
            self._ledger.update(status="held")

    @workflow.query
    def ledger(self) -> dict:
        return self._ledger
