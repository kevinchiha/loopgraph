"""M1 workflow: run the declared gates; a red gate blocks the run.

Blocking = the workflow fails (ApplicationError) and never completes. The
status query exposes per-gate red/green before and after the failure.
The full LoopGraphRun (rounds, cadences, signals) lands in M3 — same file.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from activities.audit import audit
    from activities.checkpoint import checkpoint, discard, merge
    from activities.config import DEFAULT_MAX_ROUNDS, load_run_config
    from activities.discover import discover
    from activities.execute_round import execute_round, run_baseline
    from activities.gate import run_gates
    from activities.items import load_work_items
    from activities.learn import learn
    from activities.notify import location_line, send_card, telegram_configured
    from activities.owner import clean_options, record_owner_answer


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


MAX_ASKS = 3  # owner questions per item. Separate budget, see budget_spent.


def budget_spent(spent: int, asks: int, cap: int) -> str | None:
    """Why this item must stop, or None to carry on.

    Questions and corrections have separate budgets. A question is the engine
    waiting on the owner, not a failed attempt, and charging it as a round meant
    three questions consumed the whole correction budget: the first live `ask`
    run asked the same thing three times and parked with nothing committed.

    `cap` is the round budget the config activity read from .env at the top of
    the run. It is passed in rather than read here because this module replays
    from history and must see the same number the run started with."""
    if asks >= MAX_ASKS:
        return "owner-question cap reached"
    if spent >= cap:
        return "redo cap reached"
    return None


AUDIT_REASON_CAP = 300  # chars of failure text on the card and in the ledger


def audit_failure_reason(e: BaseException) -> str:
    """Why the audit activity died, in one line the owner can act on.

    ActivityError's own message is "Activity task failed" and says nothing; what
    actually happened is on `__cause__`. For a timeout that is the deadline's
    name, and the name is the whole diagnosis. HEARTBEAT means the pings
    themselves stopped. `stream_query` now pings every 30 seconds for the whole
    call whatever the model is doing, so a model reasoning in silence no longer
    trips this one; what does is the worker going away, or its event loop held
    by something synchronous. START_TO_CLOSE is the other one: the auditor ran
    the entire half hour and never closed with a verdict packet. Opposite bugs
    wanting opposite fixes, so the string has to say which one happened.
    """
    parts: list[str] = []
    cur: BaseException | None = e
    while cur is not None and len(parts) < 4:  # bounded: __cause__ chains can cycle
        name = type(cur).__name__
        # Temporal's TimeoutError carries the deadline on `.type`; an
        # ApplicationError's `.type` is a plain string with no `.name`.
        deadline = getattr(getattr(cur, "type", None), "name", "")
        text = " ".join(str(cur).split())
        if deadline:
            parts.append(f"{name} {deadline}")
        elif text:
            parts.append(f"{name}: {text}")
        else:
            parts.append(name)
        cur = cur.__cause__
    return " <- ".join(parts)[:AUDIT_REASON_CAP]


def config_error_reason(e: BaseException) -> str:
    """Why the run cannot start, in the words run.yaml's own checker used.

    The ActivityError on top says "Activity task failed", which is no help to
    anyone about to edit a file; the line naming the offending key is on
    `__cause__`, wrapped as an ApplicationError. Anything else that can go wrong
    scheduling the activity has no such line, so it is reported whole.
    """
    cur: BaseException | None = e
    seen = 0
    while cur is not None and seen < 4:  # bounded: __cause__ chains can cycle
        # Temporal keeps the raw message on `.message`; str() prepends the type
        # the activity raised, which would push `run.yaml:` off the front.
        text = " ".join((getattr(cur, "message", None) or str(cur)).split())
        if text.startswith("run.yaml:"):
            # Capped like the audit reason: this goes on a card and into the
            # ledger, and a YAML parser's own line can run to thousands of
            # characters.
            return text[:AUDIT_REASON_CAP]
        cur = cur.__cause__
        seen += 1
    return audit_failure_reason(e)


def build_merge_summary(summary: str, total: int, parked: list[dict],
                        ended: str | None = None) -> str:
    """The merge card's text when some items did not make it.

    Says plainly what merging does and does not include, because the one thing
    the owner must not think is that a green card means everything got done. A
    sweep also says which condition stopped it: converged, out of items and out
    of time all arrive as the same merge-ready card otherwise.
    """
    text = summary
    if parked:
        lines = "\n".join(f"- item {e['n']}: {str(e['item'])[:120]} ({e['reason']})" for e in parked)
        kept = total - len(parked)
        text = (f"{summary}\n\nParked, NOT in this branch:\n{lines}\n\n"
                f"Merging takes the {kept} item(s) that passed. The parked ones need "
                f"another run.")
    # `is not None`, not truthiness: an ending reason that came out empty is a
    # bug the owner should see on the card, not one that vanishes off it.
    return f"{text}\n\nsweep ended: {ended}" if ended is not None else text


def build_convergence_item(files: list[str]) -> str:
    """The removal item the engine writes for itself once a run has added enough.

    Nobody wrote this item by hand, so the text is the whole instruction: what
    the executor may not do, how the cap is measured, which files the last
    accepted items touched, and what to do when there is genuinely nothing to
    remove. It says "net lines", not "net production lines", because the engine
    counts every staged line: a text that excluded tests would invite the
    executor to buy its removals back with new ones.
    """
    return "\n".join([
        "Convergence item. No new behaviour, no new files, no new public surface.",
        "Net lines for this item must be at or under zero; the engine measures it with",
        "git diff --numstat over every staged line, tests included, and refuses the",
        "commit if it is not.",
        "Look at what the last items added:",
        *files,
        "Remove dead code, merge duplicates you introduced, delete anything with no consumer.",
        "Every gate stays green. If there is genuinely nothing to remove, change no file and",
        "say in your claims what you checked and why each thing stays.",
    ])


def build_sweep_item(detector: dict, pass_no: int, extras: list[str], group: int) -> str:
    """One sweep item: one of a detector's path groups, and what to do about it.

    `group` indexes `detector_groups(detector)`. An item takes one group so its
    write set sits in one part of the repo: an item over the whole list picks
    whatever the detector printed first, and a run of them edits the same files
    over and over while the rest of the repo goes unworked.

    The counts and the sample all come from the detector entry, so they cannot
    disagree. They are different numbers on purpose: a detector reports
    everything it found and hands over only the first few, and an executor told
    the sample was the list would think the job was nearly done. The other groups
    are named but not sampled, so the executor can see the shape of the pass
    without reading it as this item's work. `extras` are things an earlier item
    found by hand; they get worked, but no detector counts them, so the text says
    they do not move the run's end.
    """
    groups = detector_groups(detector)
    chosen = groups[group]
    lines = chosen["lines"]
    takes = (f"groups by path; this item takes group `{chosen['name']}` "
             f"({chosen['count']} candidates).")
    if chosen["count"] and not lines:
        # Past the sample cap: `discover` kept this group's count and none of its
        # lines. The command is the only way back to them, and an item that just
        # printed nothing would hide every candidate in the group.
        sample = [takes,
                  f"No sample was kept for this group; run `{detector['cmd']}` from the "
                  "repo root and take the lines that fall in it:"]
    else:
        sample = [f"{takes} The first",
                  f"{len(lines)} follow, one per line, printed from the repo root:",
                  *lines]
    others = [f"{g['name']}: {g['count']} candidates"
              for i, g in enumerate(groups) if i != group]
    return "\n".join([
        f"Sweep item. Detector `{detector['name']}` (pass {pass_no}) reported "
        f"{detector['count']} candidates across {len(groups)}",
        *sample,
        "The other groups this pass, for the picture only; later items take them:",
        *(others or ["(none)"]),
        "Take the largest safe family of these that shares one behaviour claim, one write set",
        "and one gate. Remove or merge it. Leave the rest for a later item. Net lines for",
        "this item must be at or under zero; the engine measures it and refuses the commit",
        "if it is not. Every gate stays green.",
        "Also reported by an earlier item, not by a detector (work them if they are real; they do",
        "not count toward the run's end):",
        *(extras or ["(none)"]),
        "Register anything you find by hand under `candidates` in your output.",
    ])


EXTRAS_CAP = 20    # candidates an earlier item found, carried to the next one
STALL_LIMIT = 3    # sweep items parked in a row before the run gives up
LEDGER_LINES = 10  # candidate lines per detector the ledger keeps


def trim_pass(found: dict, pass_no: int) -> dict:
    """A `discover` result cut down to what the ledger can afford to carry.

    The ledger is not a log: it is the `ledger` query's whole reply on every
    dashboard poll, and the workflow's return value, and Temporal caps a payload
    at 2 MB. Storing the result whole was measured at 1,569 KB for a 40-item
    sweep over three detectors and 1,941 KB over four, because every pass kept
    60 candidate lines and a 2000-character stderr tail per detector.

    Ten lines is enough for the owner to see what a pass was looking at, and the
    executor still gets the chosen group's whole sample: this returns a copy and
    leaves `found` alone, which is what the item text is built from. The stderr
    tail is kept only where the note says there is something to read, which is
    the same test `lg status` and the dashboard use to decide whether to name the
    detector at all. `cmd` stays whatever the note says: it is one short string,
    and an owner reading a failed pass wants to see which command it was.

    A group keeps its name and its count and never its sample: the sample is the
    detector's own lines split up again, and the names and counts are all the
    per-pass breakdown reads.
    """
    detectors = []
    for d in found["detectors"]:
        kept = {"name": d["name"], "cmd": d["cmd"], "exit_code": d["exit_code"],
                "count": d["count"], "note": d["note"],
                "lines": d["lines"][:LEDGER_LINES]}
        if d["note"]:
            kept["stderr_tail"] = d["stderr_tail"]
        # The one guarded read of `groups` outside `detector_groups`: a pass
        # recorded before this phase has to keep the exact shape it had, and
        # writing `groups: []` there would change every one of them for a key
        # that prints nothing.
        if "groups" in d:
            kept["groups"] = [{"name": g["name"], "count": g["count"]} for g in d["groups"]]
        detectors.append(kept)
    return {"pass": pass_no, "complete": found["complete"], "total": found["total"],
            "detectors": detectors}


def sweep_end_reason(passes: list[dict], sweep: dict, elapsed: float, items_run: int,
                     parked_streak: int) -> str | None:
    """Why this sweep is over, or None to build another item.

    The order is the whole of it. Two conditions can land on the same pass, and
    a fixed order is what stops the same run reporting a different ending
    depending on which check happened to run first. Detector failure comes
    before everything else, because a zero from a detector that died is not a
    zero: the run would converge on a number the repo never had.

    A pass that reports nothing before any item has run comes next, ahead of
    convergence, because `converged` claims the run did work and this one did
    none. A clean repo and detectors that match nothing hand back the same zero,
    so the reason says both readings and names the detectors as the thing to
    check.
    """
    latest = passes[-1]
    if latest["total"] == 0 and not latest["complete"]:
        failed = ", ".join(d["name"] for d in latest["detectors"] if d["note"])
        return f"detectors failed: {failed}"
    if latest["complete"] and latest["total"] == 0 and items_run == 0:
        return ("nothing reported, no item ran: a clean repo and detectors that match "
                "nothing look the same, so check the detectors")
    if latest["complete"] and latest["total"] == 0:
        return "converged: nothing reported"
    floor = sweep["yield_floor"]
    # Complete passes only: an incomplete one is a count nobody can trust.
    counted = [p["total"] for p in passes if p["complete"]]
    if len(counted) >= 2 and counted[-2] <= floor and counted[-1] <= floor:
        return f"converged: two passes at or under {floor} ({counted[-2]}, {counted[-1]})"
    # `is not None`, because 0 is a deadline that has already passed and
    # truthiness would read it as a run with no deadline at all.
    if sweep["deadline_seconds"] is not None and elapsed >= sweep["deadline_seconds"]:
        return f"deadline reached after {items_run} items"
    if items_run >= sweep["max_items"]:
        return f"item cap {sweep['max_items']} reached"
    if parked_streak >= STALL_LIMIT:
        return f"sweep stalled: {STALL_LIMIT} consecutive items parked"
    return None


def pick_detector(entries: list[dict], last_used: int | None) -> int | None:
    """Which detector the next sweep item comes from, or None if none reported.

    Round-robin from the one after the detector the last item used, wrapping,
    skipping any that reported nothing this pass. A detector's list is often the
    same corner of the repo every pass, and always taking the first would pin the
    run there while the others go unworked. A detector on 0 has nothing to hand
    over, so taking it would build an item with an empty sample.
    """
    start = 0 if last_used is None else last_used + 1
    order = [(start + step) % len(entries) for step in range(len(entries))]
    return next((i for i in order if entries[i]["count"]), None)


def pick_group(groups: list[dict], last: str | None) -> int | None:
    """Which of that detector's groups the next item takes, or None if it has none.

    The next name along, wrapping to the front when there is none. `groups` is
    name-sorted (`discover` sorts it), so "the first name after `last`" is the
    next group. The pointer is a name rather than an index because the set of
    groups changes from pass to pass and from detector to detector: an index
    would point at a different group the moment a directory emptied. A name that
    has left the list still lands on the next name after it, which is what one
    pointer shared across detectors needs.

    The loop never asks about a detector with no groups: `pick_detector` skips a
    detector on 0, and a counted detector has at least one group. None is the
    answer for anyone who asks anyway.
    """
    if not groups:
        return None
    if last is None:
        return 0
    return next((i for i, g in enumerate(groups) if g["name"] > last), 0)


def detector_groups(entry: dict) -> list[dict]:
    """A detector entry's groups, with a pass that predates them read as one group.

    Every read that builds an item goes through here; `trim_pass` checks for the
    key itself, on purpose, so an old pass trims to the key set it had. A run
    that started before groups existed has no `groups` in its history, and
    Temporal replays that history through today's code: raising would fail the
    workflow task and leave the run stuck with nothing to do about it. Reading
    the whole detector as one group named `(all)` builds the item v1 built.
    """
    if "groups" in entry:
        return entry["groups"]
    return [{"name": "(all)", "count": entry["count"], "lines": entry["lines"]}]


def merge_extras(extras: list[str], candidates: list[str], cap: int = EXTRAS_CAP) -> list[str]:
    """What the next sweep item carries: the extras it was handed, plus whatever
    this item found by hand that is not on the list already.

    A candidate that is already there keeps the position it first had. Appending
    it again would let a thing every executor notices walk the older ones off the
    front, and nobody would ever work them. The list is trimmed from the front to
    `cap` because it rides in the item text, which lands in the executor's prompt
    and in the auditor's scope block: an unbounded one pushes the detector's own
    candidates out of both.
    """
    merged = list(extras)
    for candidate in candidates:
        if candidate not in merged:
            merged.append(candidate)
    return merged[-cap:]


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
        # run.yaml, read once before anything else runs. The convergence knobs and
        # the sweep block both live here.
        self._config: dict = {}
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

        A sweep run has no brief items and takes its work from the detectors
        instead; `_run_sweep` is that half of the loop.
        """
        # Where a sweep's deadline counts from, and the only clock this module
        # may read: Temporal replays `workflow.now()` to the same instant, so the
        # run that resumes after a worker restart is not suddenly four days old.
        start = workflow.now()
        self._target_repo = target_repo
        # run.yaml first, and once. A knob the schema does not know is an error,
        # not a default taken quietly, and finding it after the baseline would
        # mean the run had already started work on a config nobody could trust.
        # The retry policy is run_baseline's: the activity refuses a bad file
        # itself and is never asked twice about it, so what is left for the
        # policy to cover is a worker restart or a slow disk, which used to end
        # the run for good on an `ActivityError` nobody could act on.
        try:
            self._config = await workflow.execute_activity(
                load_run_config,
                args=[run_dir],
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except Exception as e:  # noqa: BLE001 - a broken run.yaml ends the run, cleanly
            reason = config_error_reason(e)
            self._ledger.update(status="stopped", reason=reason)
            await self._stopped_note(run_dir, reason, None, None)
            return self._ledger
        # Every activity from here on, in an item or between them. A dead one
        # stops the run and says so. The run returns its ledger rather than
        # raising because Temporal answers the `ledger` query on a failed
        # workflow by replaying the state at the failure: a raise leaves
        # `lg status`, the dashboard and the owner all believing the run is
        # still going, with no card to tell them otherwise.
        try:
            # Capture the starting commit before anything runs, so the very first round
            # already has a baseline to reset to rather than trusting HEAD.
            self._base_commit = await workflow.execute_activity(
                run_baseline,
                args=[target_repo],
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            if self._config["sweep"] is not None:
                # The detectors are the item source, so there is no brief list to
                # load and no convergence item to inject: a sweep item is already
                # held to net zero, and a removal pass would be the same item twice.
                return await self._run_sweep(run_dir, target_repo, self._config["sweep"],
                                             start)
            items = await workflow.execute_activity(
                load_work_items,
                args=[run_dir],
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            if not items:
                items = [work_item or ""]  # no work-items section: the whole brief, one item
            # The kind is set when the entry is made, not when it finishes: an entry
            # with no kind while it runs is one the dashboard cannot label.
            self._ledger["items"] = [{"n": i, "item": it, "status": "pending", "kind": "brief"}
                                     for i, it in enumerate(items, start=1)]

            carried: str | None = None   # an owner reply, handed to the next item
            accepted: dict | None = None  # last accepted round result, for the final card
            checkpoint_result: dict | None = None

            conv = self._config["convergence"]
            queue = list(items)          # the brief's items, taken from the front
            n = 0                        # items executed, and the number the next one gets
            items_since = 0              # accepted brief items since the last removal pass
            net_since = 0                # net lines those items committed
            touched: list[str] = []      # every path they wrote

            while True:
                # Before the next brief item AND after the last one, which is the same
                # branch: a five-item brief that added 600 lines needs its removal pass
                # after item 5, or the rule skips exactly the run it exists for.
                if conv["enabled"] and (items_since >= conv["every_items"]
                                        or net_since >= conv["net_lines"]):
                    kind = "convergence"
                    item = build_convergence_item(sorted(set(touched)))
                    entry = {"n": n + 1, "item": item, "status": "pending", "kind": kind}
                    # It takes the position it runs in, and the pending items after it
                    # move up. Appending it with the next free number would put
                    # `item 6 of 6` on a card mid-run, which reads as the run going
                    # backwards; the dashboard keys rows on `n` and re-patches their
                    # text, so renumbering pending rows loses nothing.
                    self._ledger["items"].insert(n, entry)
                    for later in self._ledger["items"][n + 1:]:
                        later["n"] += 1
                elif not queue:
                    break
                else:
                    kind = "brief"
                    item = queue.pop(0)
                    entry = self._ledger["items"][n]
                total = len(self._ledger["items"])
                n += 1
                entry["status"] = "running"
                outcome = await self._run_item(run_dir, target_repo, item, n, carried, kind)
                if kind == "brief":
                    # Cleared only after a brief item. The park card tells the owner
                    # to reply with anything the next item should know; clearing after
                    # every item spent that reply on the convergence item the engine
                    # injects — its own item, about the run's own growth — and the
                    # brief item the note was written for never saw it.
                    carried = None
                if outcome["status"] == "accepted":
                    cp = outcome["checkpoint"]
                    if cp is None:
                        # A convergence item the auditor accepted with an empty write
                        # set: there was nothing left to remove. It committed nothing,
                        # so the merge card must go on naming the last real checkpoint.
                        entry.update(status="done", commit=None, note="nothing to remove")
                    else:
                        entry["status"] = "done"
                        entry["commit"] = cp.get("commit")
                        accepted, checkpoint_result = outcome["result"], cp
                        if kind == "brief":
                            items_since += 1
                            net_since += cp.get("net", 0)
                            touched.extend(cp["files"])
                elif outcome["status"] == "halt":
                    entry.update(status="parked", reason=outcome["reason"])
                    self._ledger.update(status="stopped", reason=outcome["reason"])
                    await self._stopped_note(run_dir, outcome["reason"], n, total)
                    return self._ledger
                else:
                    entry.update(status="parked", reason=outcome["reason"])
                    await self._park_note(run_dir, n, total, item, outcome["reason"])
                if kind == "convergence":
                    # The pass happened, whatever came of it. Carrying the counters on
                    # would inject the next one immediately and every item after it.
                    items_since, net_since, touched = 0, 0, []

                # Anything the owner sent while that item ran is steering for the next
                # one. It is already in workflow state: the dispatcher signalled it.
                notes = self._drain_decisions()
                if notes:
                    entry.setdefault("owner_notes", []).extend(notes)
                    fresh = ("The owner sent this mid-run, after item "
                             f"{n}: {' / '.join(notes)}")
                    # Joined, not overwritten: a note kept across a convergence item
                    # and a note sent during it are both steering for the same next
                    # brief item, and dropping either loses an owner's answer.
                    carried = f"{carried} / {fresh}" if carried else fresh

            parked = [e for e in self._ledger["items"] if e["status"] == "parked"]
            if accepted is None:
                self._ledger.update(status="stopped", reason="every work item was parked")
                # Nothing was accepted, so there is no item the run stopped "on": it
                # ran out at the last one, and that is where the note speaks from. The
                # count is read again here because an injected item changed it.
                total = len(self._ledger["items"])
                await self._stopped_note(run_dir, "every work item was parked", total, total)
                return self._ledger
            self._ledger.update(status="merge-ready")
            await self._owner_card(run_dir, accepted, checkpoint_result, parked)
            return self._ledger
        except Exception as e:  # noqa: BLE001 - the run ends, and reports it
            reason = "engine failure: " + audit_failure_reason(e)
            self._ledger.update(status="stopped", reason=reason)
            if "sweep" in self._ledger and self._ledger["sweep"].get("ended") is None:
                # Why a sweep ended is read off this one key by `lg status`,
                # the dashboard and the merge card. A sweep that had already
                # ended keeps its own reason: a merge card that died is not why
                # the detectors stopped reporting.
                self._ledger["sweep"]["ended"] = reason
            try:
                await self._stopped_note(run_dir, reason, None, None)
            except Exception:  # noqa: BLE001 - a dead sender must not unwrite the ledger
                workflow.logger.warning("no stopped note went out: %s", reason)
            return self._ledger

    async def _run_sweep(self, run_dir: str, target_repo: str, sweep: dict,
                         start: datetime) -> dict:
        """The other item source: a detector pass, one item, another pass.

        A pass runs before the first item so the run has a baseline to measure
        against, and again after every item, because the detectors are the only
        thing allowed to say the work is finished. The executor may add work —
        what it found by hand rides on the next item as `extras` — but nothing it
        reports moves a total or an end condition. An agent deciding when its own
        job is done is exactly what the engine exists to prevent.
        """
        self._ledger["sweep"] = {"passes": [], "ended": None, "last_group": None}
        # Every detector's own timeout, plus 20 seconds each because the runner
        # polls in 20-second steps and can only kill on a step boundary (a
        # detector with `timeout: 30` dies at 40), plus two minutes for the
        # worktree and the reset to the last checkpoint. The bare sum would kill
        # the pass while its last detector was still being killed, and report
        # the whole thing as a Temporal failure rather than a timed-out detector.
        pass_timeout = timedelta(
            seconds=sum(d["timeout"] + 20 for d in sweep["detectors"]) + 120)

        carried: str | None = None    # an owner reply, handed to the next item
        accepted: dict | None = None  # last accepted round result, for the final card
        checkpoint_result: dict | None = None
        extras: list[str] = []        # candidates earlier items found by hand
        items_run = 0
        parked_streak = 0             # parked in a row; STALL_LIMIT ends the run
        used: int | None = None       # index of the detector the last item came from
        pass_no = 0

        while True:
            pass_no += 1
            try:
                found = await workflow.execute_activity(
                    discover,
                    args=[run_dir, target_repo, self._run_token(), self._base_commit],
                    start_to_close_timeout=pass_timeout,
                    heartbeat_timeout=timedelta(minutes=3),
                    retry_policy=RetryPolicy(maximum_attempts=2),
                )
            # A pass that failed both attempts ends the sweep the way a pass whose
            # detectors all died does, and for the same reason: there is no number
            # to build an item from. Letting it raise killed the workflow with the
            # ledger unwritten, so the `ledger` query went on saying `running` and
            # no card went out — the failure the audit call already closed inside
            # an item. Nothing is appended to `passes`: a pass that never returned
            # measured nothing.
            except Exception as e:  # noqa: BLE001 - the sweep ends, the run reports
                reason = f"discover failed: {audit_failure_reason(e)}"
                break
            # `found` stays a local: the item text below quotes one group's whole
            # sample and names the other groups, and the ledger keeps ten lines
            # per detector.
            self._ledger["sweep"]["passes"].append(trim_pass(found, pass_no))
            elapsed = (workflow.now() - start).total_seconds()
            reason = sweep_end_reason(self._ledger["sweep"]["passes"], sweep, elapsed,
                                      items_run, parked_streak)
            if reason:
                break

            # A pass with nothing to hand over has already ended the run above,
            # so there is always a detector left for `pick_detector` to find, and
            # a detector that counted something has a group for `pick_group`.
            entries = found["detectors"]
            used = pick_detector(entries, used)
            groups = detector_groups(entries[used])
            picked = pick_group(groups, self._ledger["sweep"]["last_group"])
            item = build_sweep_item(entries[used], pass_no, extras, picked)
            # The pointer moves as the item is built, not when it is accepted, so
            # a group whose item parked is not handed straight back to the next
            # one; and it lives in the ledger, so a rotation stuck on one group is
            # something `lg status` and the dashboard can show.
            self._ledger["sweep"]["last_group"] = groups[picked]["name"]
            entry = {"n": items_run + 1, "item": item, "status": "running",
                     "kind": "sweep", "group": groups[picked]["name"]}
            self._ledger["items"].append(entry)
            items_run += 1
            outcome = await self._run_item(run_dir, target_repo, item, items_run,
                                           carried, "sweep")
            carried = None
            if outcome["status"] == "accepted":
                cp = outcome["checkpoint"]
                entry.update(status="done", commit=cp.get("commit"), net=cp.get("net"))
                accepted, checkpoint_result = outcome["result"], cp
                parked_streak = 0
            elif outcome["status"] == "halt":
                entry.update(status="parked", reason=outcome["reason"])
                self._ledger.update(status="stopped", reason=outcome["reason"])
                # Not one of the reasons `sweep_end_reason` returns, but it is
                # still why the sweep ended, and that is the one key
                # `lg status` reads.
                self._ledger["sweep"]["ended"] = outcome["reason"]
                await self._stopped_note(run_dir, outcome["reason"], items_run, None)
                return self._ledger
            else:
                entry.update(status="parked", reason=outcome["reason"])
                parked_streak += 1
                await self._park_note(run_dir, items_run, None, item, outcome["reason"])
            # A parked item read the code too, so its candidates count. They are
            # text for the next item and nothing else: counting them would hand
            # the executor the number the run ends on.
            extras = merge_extras(extras, outcome["result"]["candidates"])

            # Anything the owner sent while that item ran is steering for the
            # next one. It is already in workflow state: the dispatcher signalled
            # it.
            notes = self._drain_decisions()
            if notes:
                entry.setdefault("owner_notes", []).extend(notes)
                carried = ("The owner sent this mid-run, after item "
                           f"{items_run}: {' / '.join(notes)}")

        self._ledger["sweep"]["ended"] = reason
        self._ledger["reason"] = reason
        parked = [e for e in self._ledger["items"] if e["status"] == "parked"]
        if accepted is None:
            self._ledger.update(status="stopped")
            # A sweep that converged on its baseline pass ran no item and has
            # nowhere to speak from; `item 0 of 0` is a wrong answer to where the
            # run is.
            if items_run:
                await self._stopped_note(run_dir, reason, items_run, None)
            else:
                await self._stopped_note(run_dir, reason, None, None)
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
                        item_no: int, carried: str | None, kind: str = "brief") -> dict:
        """One work item: rounds until the supervisor accepts, or it is parked.

        Returns accepted (with the result and its checkpoint), parked (the run
        carries on to the next item), or halt (the supervisor said stop, which is
        the one verdict that ends the whole run). Every one of them carries the
        round result, because a sweep harvests the executor's candidates off a
        parked item too: it read the code either way.

        `kind` says what sort of item this is. The auditor is told, because the
        supervisor sees only what the prompt hands it and would otherwise judge a
        removal item as a feature. The checkpoint is told as a number: a sweep or
        convergence item may not grow the repo, and the engine measures that
        rather than asking."""
        directive = carried
        max_net = 0 if kind in ("sweep", "convergence") else None
        spent = 0       # executor passes charged to the correction budget
        asks = 0        # owner questions, which are not charged to it
        round_no = 0    # every pass, for the ledger and the log file names
        answered = False  # this pass carries an owner answer, so it is not a redo
        while True:
            round_no += 1
            if not answered:
                spent += 1
            answered = False
            try:
                result = await workflow.execute_activity(
                    execute_round,
                    args=[run_dir, target_repo, work_item, round_no, directive, item_no,
                          self._run_token(), self._base_commit],
                    # correction loop may re-run slow gates
                    start_to_close_timeout=timedelta(hours=2),
                    heartbeat_timeout=timedelta(minutes=3),
                    retry_policy=RetryPolicy(maximum_attempts=2),
                )
            # An executor round that never returned is the same news as one whose
            # gates stayed red: nothing to judge, nothing to commit. It gets a round
            # entry all the same, or the round `lg status` and the dashboard were
            # watching disappears from under them.
            except Exception as e:  # noqa: BLE001 - the item is lost, the run is not
                why = audit_failure_reason(e)
                self._ledger["rounds"].append({
                    "item_no": item_no, "round": round_no, "status": "failed",
                    "attempts": 0, "claims": [], "files": [], "directive": directive,
                    "verdict": "executor failed", "verdict_reasons": [why]})
                # A sweep reads `candidates` off every parked item, so the stand-in
                # result has to carry the key even with nothing to put in it.
                return {"status": "parked",
                        "result": {"status": "failed", "claims": [], "files": [],
                                   "candidates": []},
                        "reason": f"executor failed: {why}"}
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
                return {"status": "parked", "result": result,
                        "reason": "gates red after the correction cap"}

            try:
                verdict = await workflow.execute_activity(
                    audit,
                    args=[run_dir, result, round_no, item_no, work_item,
                          len(self._ledger["items"]), kind],
                    start_to_close_timeout=timedelta(minutes=30),
                    heartbeat_timeout=timedelta(minutes=3),
                    retry_policy=RetryPolicy(maximum_attempts=2),
                )
            # An auditor that cannot produce a verdict parks the item, the way a
            # red gate and a refused checkpoint already do. Letting it raise was
            # the one failure in this loop that killed the whole run: the ledger
            # was never written on the way out, so the `ledger` query — which
            # Temporal still answers for a closed workflow — went on saying
            # `running` long after the run was dead, and no card ever went out.
            except Exception as e:  # noqa: BLE001 - the item is lost, the run is not
                why = audit_failure_reason(e)
                # Without this the round keeps reading as `audit running` in
                # `lg status`, which is the line the owner checks first.
                entry["verdict"] = "audit failed"
                entry["verdict_reasons"] = [why]
                return {"status": "parked", "result": result,
                        "reason": f"audit failed: {why}"}
            entry["verdict"] = verdict["verdict"]
            entry["verdict_reasons"] = verdict["reasons"]

            if verdict["verdict"] == "accept":
                if kind == "convergence" and not result["files"]:
                    # The one item allowed to finish having changed nothing: the
                    # auditor looked and agreed there was nothing left to remove.
                    # Checkpoint would park it on `empty write set`, which would
                    # end a clean run as a failed one.
                    return {"status": "accepted", "result": result, "checkpoint": None}
                try:
                    cp = await workflow.execute_activity(
                        checkpoint,
                        args=[run_dir, result["worktree"], result["files"], round_no,
                              result["summary"], item_no, max_net],
                        # gate re-run may be a full build
                        start_to_close_timeout=timedelta(minutes=45),
                        heartbeat_timeout=timedelta(minutes=3),
                        retry_policy=RetryPolicy(maximum_attempts=2),
                    )
                # A checkpoint that died is not a checkpoint that refused, and the
                # verdict stays `accept` because that is what happened: the auditor
                # passed the round and the commit is what was lost. A live run died
                # here on a `git rm`'d path the checkpoint could not stage.
                except Exception as e:  # noqa: BLE001 - the item is lost, the run is not
                    why = audit_failure_reason(e)
                    entry["checkpoint_failed"] = why
                    return {"status": "parked", "result": result,
                            "reason": f"checkpoint failed: {why}"}
                self._ledger["checkpoint"] = cp
                if cp.get("commit"):
                    self._base_commit = cp["commit"]
                if not cp["committed"]:
                    return {"status": "parked", "result": result,
                            "reason": f"checkpoint refused: {cp['reason']}"}
                # Convergence fires on lines added since the last removal item, so
                # the number has to survive the round it was measured in.
                entry["net"] = cp["net"]
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
                return {"status": "halt", "result": result,
                        "reason": f"supervisor said stop: {'; '.join(verdict['reasons'])[:300]}"}
            if verdict["verdict"] == "plan":
                return {"status": "parked", "result": result,
                        "reason": f"supervisor asked to replan: {'; '.join(verdict['reasons'])[:300]}"}
            if verdict["verdict"] == "ask":
                if asks >= MAX_ASKS:
                    return {"status": "parked", "result": result,
                            "reason": "owner-question cap reached"}
                asks += 1
                d = verdict["directive"]
                question = d.get("action", "Supervisor needs an owner decision")
                # Normalised where the packet is parsed too. A run already in
                # flight replays the verdict its history recorded, so an upgraded
                # worker still gets the old shape handed back to it here.
                options = clean_options(verdict.get("options"))
                # A sweep counts nothing towards a total: it builds its items
                # as it goes, and `item 7 of 7` would say the run was on its
                # last one every time it asked. The auditor still gets the
                # integer above, which is a list length and not a total.
                total = None if "sweep" in self._ledger else len(self._ledger["items"])
                reply = await self._ask_owner(run_dir, question, options, item_no,
                                              total, round_no)
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
            over = budget_spent(spent, asks,
                                self._config.get("max_rounds", DEFAULT_MAX_ROUNDS))
            if over:
                return {"status": "parked", "result": result, "reason": over}
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
        # Every card the engine sends comes through here, and below this line the
        # options are a letter map: the allowed set, the `lg approve` hint, the
        # ledger the page reads, and the card itself all assume one.
        options = clean_options(options)
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

    async def _ask_owner(self, run_dir: str, question: str, options: dict,
                         item_no: int, total: int | None, round_no: int) -> str:
        """Supervisor `ask`: a question the owner answers by button, text or signal.

        The location line is prefixed here, not inside _await_decision, so the
        string the ledger records and the string the card carries stay one object
        and cannot drift. `question` itself is left alone: the round entry and
        `owner-answers.md` keep the supervisor's words with no card furniture.

        The line costs a question over ~1480 characters its tail, because
        build_card_text cuts the summary at 1500. That cut stays where it is: the
        card is the alert, and the whole question goes to the ledger, which is
        what the page and `lg status` read.
        """
        summary = location_line(item_no, total, round_no) + "\n\n" + question
        return await self._await_decision(run_dir, "decision", summary, None,
                                          options, accept_text=True)

    async def _park_note(self, run_dir: str, item_no: int, total: int | None,
                         item: str, reason: str) -> None:
        """Tell the owner an item was parked. Does not wait: the run has already
        moved on to the next item, and their reply is picked up between items."""
        text = (f"{location_line(item_no, total)} parked\n\n{item[:600]}\n\n"
                f"why: {reason}\n\nThe run is carrying on with the rest. Reply here "
                f"with anything the next item should know, or wait for the "
                f"merge-ready card at the end.")
        self._ledger.setdefault("parked_notes", []).append({"item_no": item_no, "reason": reason})
        await self._note(run_dir, "parked", text)

    async def _stopped_note(self, run_dir: str, reason: str,
                            item_no: int | None, total: int | None) -> None:
        """Tell the owner a run ended. A stop used to return silently, so nobody
        was told the run was over, and any items already committed sat on a branch
        nobody knew about.

        A run that ended before any item ran has nowhere to speak from and gets no
        location line: `item 0 of 0` is a wrong answer to where the run is."""
        done = [e for e in self._ledger["items"] if e["status"] == "done"]
        parked = [e for e in self._ledger["items"] if e["status"] == "parked"]
        where = [location_line(item_no, total), ""] if item_no is not None else []
        lines = [*where,
                 f"why: {reason}", "",
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
        total = len(self._ledger["items"])
        # A sweep has no total to count towards and stops on a condition rather
        # than on a list running out, so its card says which condition: converged,
        # out of items and out of time arrive as this same card otherwise.
        is_sweep = "sweep" in self._ledger
        # The last item is where the run finished, so that is where this speaks
        # from. Location line plus merge summary, parked list included, is exactly
        # what the owner saw, and the page prints it back as it is.
        where = location_line(total, None if is_sweep else total)
        summary = where + "\n\n" + build_merge_summary(
            result["summary"], total, parked or [],
            ended=self._ledger["sweep"]["ended"] if is_sweep else None)
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
