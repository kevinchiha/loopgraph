# Gap report — ui-state-first

Adversarial gap-hunt over `docs/superpowers/specs/2026-09-05-ui-state-first-design.md` and
`docs/superpowers/plans/2026-09-05-ui-state-first.md`, 2026-09-05.

Seven hunters over spec, plan and the real code; one refuter per unique gap, a second for anything
still rated critical. Refuters default to refuted when uncertain.

- 7 of 7 hunters returned
- 60 raw gaps -> 44 unique (7 kind conflicts, 1 bidirectional contradictions folded in)
- confirmed 23, disputed 0, refuted 21, unverified 0

## Confirmed — folded into spec and plan (23)

### gap-3 — minor

**Claim.** 'No verdict means escalated' is false. A round entry is written to the ledger before the audit runs, so throughout the audit the entry has status 'green' and no verdict - and the page and lg status will print the word 'green' where the supervisor's verdict goes, which reads as an acceptance that has not happened.

**Touches:** AC-8, AC-23, task-4, task-12, fn:format_status, fn:_run_item, file:workflows/run.py  
**Found by:** coverage, page

**Evidence offered**

```
workflows/run.py:229-245 appends the round entry (status = result['status'], i.e. green or escalated per graphs/round_graph.py:84-85); workflows/run.py:250-258 only then runs the audit activity - start_to_close_timeout=timedelta(minutes=30) - and assigns entry['verdict']. Spec AC-8 (lines 68-70) asserts 'A round with no verdict key (its gates stayed red, so no audit ran) shows its status word, escalated'; AC-23 (lines 157-158) says the same for lg status. Plan Task 4's test intent covers only test_a_round_without_a_verdict_prints_its_status_word - escalated. Task 12 behaviour 3 restricts the ' · in progress' marker to a card built from a log name 'before its ledger entry exists', so the audit-in-flight round - which does have an entry - gets no marker at all. That window is the single most interesting moment on a live-run dashboard.
---
`workflows/run.py:245` `self._ledger["rounds"].append(entry)`, then `workflows/run.py:250-254` awaits `audit` with `start_to_close_timeout=timedelta(minutes=30)`, and only `workflows/run.py:258` sets `entry["verdict"]`. Spec AC-8: "A round with no `verdict` key (its gates stayed red, so no audit ran) shows its `status` word, `escalated`" — the parenthetical is wrong about when the key is missing. Plan Task 12 Behaviour 2 and Constraints ("`_run_item` appends the round entry after `execute_round` returns") repeat the same assumption. `lg status` (AC-23, Task 4 copy block) has the identical defect.
```

**Verdict 1 (facts) — stands**

The mechanical core of the claim checks out, but the consequence is much smaller than "critical", and two of the four documents the hunter cites as sharing the defect actually state the rule correctly.

What is true:
- The ordering is exactly as described. `workflows/run.py:245` appends the round entry, `:246-248` returns early when the status is not "green", `:250-257` awaits the `audit` activity (start_to_close 30 minutes, up to 2 attempts), and only `:258` sets `entry["verdict"]`. Because `entry` is the same dict that was appended, the ledger really does hold, for the length of the audit, a round with `status == "green"` and no `verdict` key.
- `graphs/round_graph.py:84-85` and `activities/execute_round.py:256` confirm the status is only ever "green" or "escalated", so AC-8's appositive — "A round with no `verdict` key (its gates stayed red, so no audit ran) shows its `status` word, `escalated`" — asserts an invariant that does not hold. During the audit the status word is "green". That single clause in the spec (line 69-70), and the "(escalated)" example in plan Task 12 Behaviour 2 (line 855), are wrong as explanations.
- Task 12's `· in progress` marker is indeed pinned to a card built from a log name "before its ledger entry exists", so a round under audit gets no marker.

Why this is minor, not critical:
1. The normative instruction is not wrong, only its gloss. Both AC-8 ("shows its `status` word ... in the verdict's place") and AC-23 line 155 ("or its `status` word when it has no verdict") tell the implementer to print the status word, which is unambiguous and correct for both cases. Task 4's pinned copy block says the same thing correctly ("verdict word, or the round's status word when it has no verdict"); only the test-intent name carries the "escalated" example. So the hunter's "AC-23 says the same" and "Task 4's test intent covers only ..." overstate it — nothing here blocks or misdirects a build, and no pinned copy or test contradicts reality.
2. "Reads as an acceptance that has not happened" is an over-read. The verdict vocabulary is accept / stop / plan / ask (run.py:261-297); "green" is the gate word and is not one of them.
3. Handling the hunter did not look at: while the audit is in flight the same page simultaneously shows the ledger `status` as "running" (run.py:120, Task 11 Behaviour 1) and the item row as "running" (run.py:161, AC-7, Task 11 Behaviour 3), and the round card carries its supervisor log pane, which is growing (AC-9, Task 12 Behaviour 3). The dashboard never claims the run is finished.
4. The window is short. On a live run (runs/2026-09-05-microbits-order-v3/logs), the executor log stops at 13:58:55 and the audit log stops at 14:00:09; round 2 is 14:05:52 to 14:07:31. So roughly 1-2 minutes per round, not a persistent misleading state.

The fix is a one-clause correction to AC-8 (and the "(escalated)" gloss in Task 12), plus, if wanted, widening the `· in progress` marker to "entry present but no `verdict` key". Worth doing; not critical.

_Opened:_ Opened and read in full: /home/kevin/projects/loopgraph/workflows/run.py lines 120-340 (init of the ledger, the item loop setting `status = "running"`, and all of `_run_item` including the append at :245, the early return at :246-248, the audit call at :250-257 and the verdict assignment at :258-259); /home/kevin/projects/loopgraph/graphs/round_graph.py lines 60-86 (`run_round` forcing "escalated"); grep of `escalated` across the repo, which surfaced /home/kevin/projects/loopgraph/activities/execute_round.py:256 (`"status": final["status"],  # green | escalated`) and graphs/round_graph.py:26; the whole spec /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md (AC-7 lines 62-65, AC-8 lines 66-70, AC-9 lines 71-74, AC-23 lines 152-158 — note the verdict-word sentence is at line 155, not the 157-158 the gap cites); plan /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md Task 4 (lines 267-345, including the pinned copy block "verdict word, or the round's status word when it has no verdict" and the test intent at line 331), Task 11 (lines 764-830, `patchState` and `patchItems`), and Task 12 (lines 831-885, including the pinned copy "` · in progress` appended while the round has logs but no ledger entry", Behaviour 2 at line 855 and the Constraints paragraph). Timing evidence: `ls -l` on /home/kevin/projects/loopgraph/runs/2026-09-05-microbits-order-v3/logs — i1-r1-executor 13:58:55, i1-r1-audit 14:00:09; i1-r2-executor 14:05:52, i1-r2-audit 14:07:31.

_Severity corrected to:_ minor

---

### gap-4 — minor

**Claim.** For a merged run the three-dot diff is empty, and AC-17's reason list has no line for it, so the diff pane goes blank with no explanation on exactly the runs an owner reviews after merging.

**Touches:** AC-15, AC-17, task-8, task-13, endpoint:/api/diff, fn:merge_branch  
**Found by:** coverage

**Evidence offered**

```
AC-15 (spec:106-112) pins `git diff <base_branch>...<branch>`. activities/checkpoint.py:112-135 (merge_branch) checks out base_branch and merges the run branch into it, which makes the branch an ancestor of base, so merge-base(base, branch) is the branch tip and A...B is empty by definition. AC-17 (spec:119-123) enumerates eight failure cases and promises 'a one-line explanation in stat' for each; 'already merged, nothing left to diff' is not among them, and Task 8's nine verbatim reason strings do not include it. Task 13 behaviour 1 shows stat then patch under the Task 8 constraint 'show it as is. No special-casing', so the owner sees an empty pane indistinguishable from a broken endpoint. Live: run-2026-09-05-microbits-order-v3-ae4a5b has ledger status 'merged'.
```

**Verdict 1 (facts) — stands**

The mechanism checks out on every cited document and file, so the gap stands, but its severity is over-rated and its live example no longer shows the symptom.

What holds:
1. AC-15 (spec:106-112) really does pin `git diff --stat <base_branch>...<branch>` and `git diff <base_branch>...<branch>`, with `base_branch` and `branch` taken from the ledger's last round. `activities/execute_round.py:229` records `base_branch` as a branch *name* (`git branch --show-current`), not a pinned commit, so the name follows the branch forward.
2. `merge_branch` (activities/checkpoint.py:108-137) checks out `base_branch` and runs `git merge --no-ff <branch>`, so the run branch becomes an ancestor of base and the three-dot diff is empty by definition. I reproduced it in a throwaway repo in the scratchpad: after `--no-ff`, `git diff --stat main...feat` printed nothing while both branches still passed `git rev-parse --verify`.
3. Neither `merge_branch` nor `workflows/run.py:493-501` (choice A) deletes the branch or the worktree — only `discard` (checkpoint.py:143-169, choice C) does — so in the engine's own flow a merged run keeps both branches resolvable, and `/api/diff` reaches the diff step and returns an empty `stat` and empty `patch`.
4. AC-17 (spec:119-123) enumerates exactly the eight failure cases the hunter quotes; "already merged" is not among them, and Task 8's nine verbatim reason strings (plan:586-596) do not include it. Task 13's constraint (plan:911) is "show it as is. No special-casing", and Task 13 behaviour 1 renders `stat` then `patch` in a `pre`, so both being empty yields a blank pane. Task 11 (plan:794-798) hides `#diff` only when `ledger` is null, so a merged run's pane is shown. `ui.py` has no `diff` string in it at all today, so nothing pre-existing handles the case. Grepping the spec and plan for "merge", "empty", "no changes" turns up nothing covering it.

Why minor rather than important:
- The plan does not fail the spec here. AC-15 and AC-17 are delivered exactly as written; an empty diff of a merged branch is the correct result of the command AC-15 pins, not one of AC-17's eight failure modes. This is a case the spec never contemplated, and the whole fix is one more reason string plus an "empty stat" check.
- The diff's designed moment is the merge-ready card (spec:231 puts file-change detail on the page instead of the card), and there the branch is unmerged and the diff is full. Only post-merge browsing is affected, and the change is then visible in the target repo's own history.
- The cited live evidence does not currently show the symptom. For run ...-ae4a5b the merge commit is in `/home/kevin/projects/microbits-opportunities` (`846ed5d loopgraph: merge lg-2026-09-05-microbits-order-v3-ae4a5b (owner-approved)`, engine@loopgraph.local in the reflog), but the branch itself is gone (`git branch -a` shows only `main`; `git rev-parse --verify lg-2026-09-05-microbits-order-v3-ae4a5b` fails) and `.git/worktrees` is empty, while the run's pointer file still reads `gitdir: /projects/microbits-opportunities/.git/worktrees/ae4a5b`. So that run resolves the repo fine and then trips reason 8, "branch not found: <name>" — an explanation, not a blank pane. Both merged runs on this box ended that way, which suggests the blank window is short-lived in practice.

Severity is minor: a real, owner-visible blank with a one-line fix, on a read-only dashboard, in a state the spec never promised to cover.

_Opened:_ Opened: /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md (lines 1-70 goal and AC-1..AC-9, 95-135 covering AC-13..AC-19, and re-read 119-124 for AC-17 verbatim; grepped it for "diff", "merge", "empty"). /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md (Task 8 in full, lines 561-644, including the nine reason strings at 586-596; Task 11, 764-835; Task 13 in full, 886-925; grepped for "merge", "empty"). /home/kevin/projects/loopgraph/activities/checkpoint.py:100-169 (checkpoint, merge_branch, merge, discard). /home/kevin/projects/loopgraph/workflows/run.py:475-530 (_owner_card, the A/B/C branches). Grepped ui.py for "diff" (no hits) and the tree for `branch -D` / `worktree remove` (only discard). Ran read-only: `git -C /home/kevin/projects/microbits-opportunities branch -a` (only `main`), `rev-parse --verify lg-2026-09-05-microbits-order-v3-ae4a5b` (fatal), `worktree list`, `log --oneline -5`, `tail .git/logs/HEAD`; `cat runs/2026-09-05-microbits-order-v3/worktrees/ae4a5b/.git` (`gitdir: /projects/microbits-opportunities/.git/worktrees/ae4a5b`); `grep LOOPGRAPH_PROJECTS_DIR .env` (=/home/kevin/projects). Built a throwaway repo under the scratchpad to confirm that after `git merge --no-ff feat`, `git diff --stat main...feat` prints nothing while both branches still rev-parse. Nothing in the project was written or changed.

_Severity corrected to:_ minor

---

### gap-5 — minor

**Claim.** The head-truncation signal only catches offsets past the new size. append_log keeps the LAST half of the cap, so a stored offset below 500 KB still looks valid and the pane silently appends bytes from the wrong place in a rewritten file.

**Touches:** AC-11, task-6, task-9, fn:log_slice, fn:append_log, endpoint:/api/log, file:activities/stream.py  
**Found by:** coverage

**Evidence offered**

```
activities/stream.py:16,50-51: LOG_CAP is 1,000,000 and on overflow the file becomes `[... head truncated ...]\n` plus `p.read_bytes()[-LOG_CAP // 2:]`, so size drops from >1 MB to ~500 KB. AC-11 (spec:82-89) and Task 6 behaviour 4 detect truncation only via 'offset > size'. Task 9 behaviour 2 makes it reachable: 'Reopening resumes from the stored offset with its text intact' - a pane closed at offset 300 KB, whose file then grows past 1 MB and is truncated to 500 KB, reopens with 300 KB <= 500 KB, gets a mid-file slice, and appends it under stale text with no replace signal. The endpoint could detect it (the marker is written at byte 0), but neither the spec nor Task 6 mentions it.
```

**Verdict 1 (facts) — stands**

Every citation checks out, and nothing elsewhere closes the hole.

What the code actually does: `append_log` (activities/stream.py:50-51) fires when the file passes LOG_CAP (1,000,000 bytes) and rewrites it as the 25-byte marker `[... head truncated ...]\n` plus the last 500,000 bytes. So the file shrinks from just over 1 MB to about 500,025 bytes, and every byte position in it now points at different text than before.

What the documents actually say: AC-11 (spec lines 82-89) defines the only recovery signal as "when the requested offset is past the current size ... the reply starts from 0 with the whole file". Task 6 behaviour 4 says the same in one line: "If `offset > size`, the slice starts at 0", and Task 6's constraints assert this is the mechanism ("A reply `offset` lower than the one requested is how the page learns the file was head-truncated"). Task 9 behaviour 3 keys the page's replace-vs-append choice purely on that reply offset. Task 9 behaviour 2 supplies the reachable path, verbatim: "Reopening resumes from the stored offset with its text intact."

So the miss is real. A pane opened early, read to (say) 300,000 bytes, then closed; the run keeps writing, the file crosses 1 MB and is head-truncated to ~500,025; the reader reopens the same pane. 300,000 is not greater than 500,025, so no signal fires, and the endpoint hands back bytes [300,000:500,025] of the rewritten file - text from roughly 800 KB into the old file, arriving mid-line, appended straight under stale text with a silent 500 KB hole at the seam. The same hole reopens for any stored offset once a truncated file regrows past it, so an open pane is not immune either, only unlikely to be caught out. The marker at byte 0 would make this detectable, and neither AC-11 nor Task 6 asks for it. Today's ui.py does not cover it: it has no offsets at all, it sends the last 60 KB of every log every poll (LOG_TAIL, ui.py:24,124-129). Grepping the whole tree for "truncat" turns up only the writer, the diff cap (a different thing), and the two lines quoted above.

Why I dropped it from important to minor rather than leaving it or refuting it. The trigger needs a single log file above 1 MB. Across the 36 log files in runs/ the largest is 29,446 bytes and none exceeds 500 KB - the threshold is about 34x larger than anything this engine has yet produced. On top of that the reader has to close and reopen that same pane across the crossing without reselecting the run (Task 9 behaviour 6 clears the board on reselect, which resets every offset). And when it does fire, the damage is confined to a read-only viewer: the appended chunk still ends at the file's true end, so the newest lines - the ones the owner reads to decide - are correct; what is wrong is one garbled line at the join and a missing middle, cured by reselecting the run. No workflow state, no ledger key, no card and no decision is affected. That is a genuine defect in a stated acceptance criterion with a cosmetic worst case, which reads as minor rather than important.

_Opened:_ Opened in full: /home/kevin/projects/loopgraph/activities/stream.py (all 75 lines; LOG_CAP at line 16, the truncation at lines 50-51), /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md (all 308 lines, AC-11 at lines 82-89), /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md - global constraints (lines 28-73), Task 6 (lines 418-486, including behaviour 4 and the constraint at line 463), Task 9 (lines 644-715, behaviours 1-6), plus the task-heading index. Also read /home/kevin/projects/loopgraph/ui.py lines 110-140 and 220-245 (today's LOG_TAIL/log_tails path and the /api/logs branch) to confirm no existing truncation handling. Ran `grep -rn "truncat"` over all .py and .md outside runs/ - only stream.py's writer, tests/test_visibility.py:79 (asserts the marker on the write side), the two spec/plan lines under test, and the unrelated diff caps in activities/audit.py and Task 8. Ran `find runs -name "*.log" -printf "%s %p\n" | sort -rn` - 36 files, largest 29,446 bytes, zero above 500 KB.

_Severity corrected to:_ minor

---

### gap-6 — minor

**Claim.** AC-2 is claimed in full by Task 6, which only guards dir and name. The id half lands in Tasks 7 and 8, and neither names AC-2 on its Delivers line, so no task is accountable for the criterion as written.

**Touches:** AC-2, task-6, task-7, task-8, endpoint:/api/run, endpoint:/api/diff, task-1, AC-19, fn:read_env, decision:one-shared-env-reader, fn:bad_param, endpoint:/api/log  
**Found by:** coverage, endpoints, purefns

**Evidence offered**

```
Spec AC-2 (lines 36-38): 'Every endpoint that takes id, dir or name answers HTTP 400 when the value is empty or contains / or ..'. Plan Task 6 Delivers: 'AC-2, AC-11, AC-12'; its behaviour 1 says 'bad_param guards dir on /api/logs and dir and name on /api/log. Task 7 and Task 8 apply it to id.' Task 7 Delivers is 'AC-1, AC-3'; Task 8 Delivers is 'AC-15, AC-16, AC-17, AC-18, AC-37'. The behaviour and test intents do exist in Tasks 7 and 8 (test_run_endpoint_rejects_bad_ids, test_diff_rejects_bad_ids), but the coverage line does not, so a reviewer checking Delivers lines sees AC-2 closed at Task 6 while half of it is still open.
---
plan:80 (Task 1 Delivers: AC-19) with Files at plan:81-84 listing envfile.py, lg, tests/test_envfile.py; plan:566 (Task 8 Delivers: AC-15, AC-16, AC-17, AC-18, AC-37); spec AC-19 at spec:128-134; the decision it protects at spec:258-260. Task 1's only ui-facing test intent is absent - all five (plan:116-122) exercise read_env and lg._dotenv.
---
AC-2 covers 'every endpoint that takes id, dir or name' plus the offset rule (spec l.36-38). Task 6 Delivers AC-2 (plan l.424) but its behaviour 1 says 'bad_param guards dir on /api/logs and dir and name on /api/log. Task 7 and Task 8 apply it to id' (plan l.447-448). Task 7's Delivers is AC-1, AC-3 (l.492) and Task 8's is AC-15..AC-18, AC-37 (l.566), although both apply bad_param in their behaviour (l.530, l.601). Separately, Task 6 behaviour 3 says offset 'defaults to 0 when absent; anything that is not a string of digits is 400' (plan l.452-453) — I checked parse_qs, which drops blank values by default, so `?dir=d&name=n&offset=` parses with no offset key at all and reads as absent, i.e. 0 rather than 400. Either add AC-2 to Task 7 and Task 8's Delivers lines, or say in Task 6 that a blank offset is the absent case.
```

**Verdict 1 (facts) — stands**

Every quotation in the gap checks out, so it is not a misread or a misquote. AC-2 does cover "every endpoint that takes id, dir or name" (spec lines 36-38); the only Delivers line in the whole plan that names AC-2 is Task 6's (plan:423); Task 6's own behaviour 1 (plan:447-448) says it guards only dir and name and hands id to Tasks 7 and 8; Task 7 Delivers is AC-1, AC-3 (plan:491) and Task 8 Delivers is AC-15..AC-18, AC-37 (plan:566). So the narrow factual observation stands: an AC-to-task tally built from Delivers lines maps AC-2 to Task 6 alone, and Task 6 openly does half of it.

But the claim's conclusion — "no task is accountable for the criterion as written" — is contradicted by the plan itself, and by the hunter's own evidence. Task 7 behaviour 4 says `bad_param(id)` → 400 for /api/run (plan:530) with test intent `test_run_endpoint_rejects_bad_ids` (plan:552). Task 8 behaviour 1 says the same for /api/diff (plan:601) with `test_diff_rejects_bad_ids` (plan:633). Both name `bad_param` on their Consumes line (plan:498, 573). Task 6 points at both tasks by name. The plan's header says the plan states contracts, behaviour, constraints and test intents; the id half of AC-2 is present in all three of those places for both endpoints. Nothing is at risk of going unbuilt — this is an index entry missing from a summary line, not a hole in the work.

The plan's Delivers convention is also looser than the gap assumes. AC-11 and AC-12 appear bare on Task 6 (plan:423) and qualified on Task 9 as "(page side)" (plan:650); AC-15, AC-18 and AC-37 appear bare on both Task 8 (plan:566) and Task 12 (plan:891). So a bare AC on one Delivers line already does not mean "closed here", and a reviewer cannot treat these lines as an exhaustive map anyway. The consistent fix the plan's own habit suggests is "AC-2 (id)" on Tasks 7 and 8, matching Task 9's "(page side)".

The AC-19 strand in the touches list is the same shape and the same size: Task 1 Delivers AC-19 (plan:79) and its five test intents (plan:116-122) all exercise `read_env` and `lg._dotenv`, while the ui.py half AC-19 requires (spec:128-134) lands in Task 8 behaviour 4 ("reads LOOPGRAPH_PROJECTS_DIR with read_env(ROOT / '.env') per request", plan:605) with AC-19 absent from Task 8's Delivers. Covered in behaviour, unlabelled on the summary line.

The offset sub-claim is technically correct and practically empty. I ran `parse_qs('dir=d&name=n&offset=')` and confirmed it returns `{'dir': ['d'], 'name': ['n']}` — the blank key is dropped, so `.get("offset", ["0"])[0]` yields "0" and the request reads as absent rather than 400. But this contradicts nothing in the plan: Task 6 behaviour 3 says "defaults to 0 when absent; anything that is not a string of digits is 400" (plan:452-453), and under parse_qs a blank value never reaches the digit check. It grazes only the literal reading of the spec sentence, and the same dropping already makes `?dir=` a 400 in today's ui.py via the `not slug` branch (ui.py:230-232), so the pattern is established and harmless.

Verdict: the labelling defect is real and worth one line of cleanup, but it carries no build risk and no ambiguity for the implementer. Important overstates it; minor is right.

_Opened:_ Opened: /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md lines 25-60 (AC-1 through AC-6, AC-2 verbatim at 36-38), lines 120-134 (AC-18, AC-19) and lines 250-270 (the decisions list including "One shared .env reader"). Opened /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md lines 1-70 (header, goal, architecture, global constraints), 70-130 (Task 1 in full: Delivers AC-19 at :79, Files at :81-84, read_env contract, six behaviours, five test intents at :116-122), 415-480 (Task 6 in full: Delivers at :423, bad_param/log_names/log_slice contracts, behaviour 1 at :447-448 and behaviour 3 at :452-453, seven test intents), 483-545 (Task 7: Delivers at :491, Consumes bad_param at :498, behaviour 4 at :530, test intent test_run_endpoint_rejects_bad_ids at :552), 555-660 (Task 8: Delivers at :566, Consumes at :573, behaviour 1 at :601 and behaviour 4 at :605, test_diff_rejects_bad_ids at :633; plus the start of Task 9 and its Delivers at :650). Grepped the plan for every "AC-2" hit (one: line 423), every "**Delivers:" line (13, at :79 :136 :188 :272 :350 :423 :491 :566 :650 :720 :769 :836 :891), and every bad_param mention (:434 :447 :498 :530 :573 :601). Grepped AGENTS.md and docs/superpowers/ for a stated Delivers convention: none exists outside the plan. Read /home/kevin/projects/loopgraph/ui.py lines 218-250 (the current do_GET, including the /api/logs empty-or-traversal check) and line 19 (the parse_qs import). Ran python3 -c "from urllib.parse import parse_qs; print(parse_qs('dir=d&name=n&offset='))" — output {'dir': ['d'], 'name': ['n']}, blank value dropped, and the .get default yields "0".

_Severity corrected to:_ minor

---

### gap-4 — minor

**Claim.** The no-card line that Task 4 adds to format_status exists, by the plan's own admission, so that lg status and the dashboard say the same thing - and nothing tests that they do. Task 4 has no test intent for the `telegram: false` branch at all, and Task 11's test only asserts the page contains its five copy lines. The two verbatim copies of a 62-character sentence sit in different tasks with no shared constant and no cross-check, which is exactly how they drift.

**Touches:** task-4, fn:format_status, AC-23, AC-4  
**Found by:** purefns

**Evidence offered**

```
plan:298 (lg status copy) and plan:790 (page copy), both 'no card was sent; the lg approve command is the only way to answer'; the stated reason at plan:323-325 'it is added so lg status and the page (Task 11) say the same thing'; Task 4's five format_status test intents at plan:327-331 cover status/reason, question present, question absent, no awaiting, empty items, and a verdict-less round - none sets telegram false; Task 11's test intent at plan:817.
```

**Verdict 1 (facts) — stands**

Every fact the gap asserts checks out against the plan and the spec.

What is true:
- The sentence "no card was sent; the lg approve command is the only way to answer" appears twice in the plan, verbatim and independently: plan:298 inside Task 4's pinned `lg status` copy block (annotated "only when telegram is false", and the block states the notes after the arrow are not printed), and plan:790 inside Task 11's pinned page copy block. Two files, two tasks, no shared string.
- The stated reason is quoted correctly. plan:323-324 reads: "The `awaiting` no-card line is not required by AC-23; it is added so `lg status` and the page (Task 11) say the same thing."
- Task 4's test intents (plan:327-336) are exactly the five `format_status` cases the hunter lists plus five resolver cases. None sets `telegram` to false, and none names the no-card line. Task 5 (plan:400-410), which wires `format_status` into `cmd_status`, does not cover it either — I checked, because that was the obvious place it might already be handled.
- Task 11's only copy assertion is plan:817, `test_the_board_copy_is_pinned` — "page contains each of the five copy lines". That pins the page's string in isolation; nothing compares it to the CLI's.
- No shared home for the string exists in the plan. The one module both `lg` and `ui.py` import is `envfile.py` (Task 1, plan:76-120), and it is a dotenv parser with no copy constants.

I also checked the spec. AC-4 (spec:49-53) requires the no-card wording on the page. AC-23 (spec:152-157) lists what `lg status` prints and does not include it, so the plan's admission is accurate. AC-25 (spec:162-166) enumerates the exact five `format_status` cases the plan mirrors, so the plan is faithful to the spec — the untested branch is behaviour the plan added beyond it.

Why I dropped it from important to minor:
- The behaviour itself is fully specified. The copy is pinned character-for-character at plan:298 with an unambiguous condition, and plan:62 is a global constraint: "User-facing copy is pinned in the tasks. Do not reword it at implementation time." An implementer building to the plan gets it right; this is not an underspecification.
- The worst realistic outcome is that the line is silently missing from `lg status` (no test catches it) or the two wordings drift later. Neither breaks an acceptance criterion — AC-23 does not ask for the line, and AC-4's page copy is tested. Nothing is wrong in the data, no user flow breaks, no engine contract moves. It is a cosmetic inconsistency in one advisory sentence of one CLI subcommand.
- One small factual slip in the claim: the sentence is 66 characters, not 62. It does not change the substance.

So the gap is real and well-evidenced, but it is a missing test intent for an optional, spec-unrequired advisory line, which is minor rather than important.

_Opened:_ Opened: /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md lines 40-80 (preamble constraints, incl. line 62 copy-pinning rule), 76-120 (Task 1, envfile.py contract), 270-345 (Task 4: format_status copy block at 288-303 with the no-card line at 298, constraints 320-324, test intents 326-336), 345-410 (Task 5: cmd_status wiring and its full test-intent list), 760-840 (Task 11: page copy block with the no-card line at 790, behaviour step 2 at 803, constraints 812-813, test intents 815-818). Opened: /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md lines 40-70 (AC-4 through AC-8) and 148-178 (AC-22 through AC-28, incl. AC-23 at 152-157 and AC-25 at 162-166). Ran: grep -n "no card was sent" over plan and spec (hits at plan:298, plan:790, spec:52); grep -n "telegram" over the plan (207, 219, 222, 298, 803 only — no test intent); grep -rn "say the same|same words|same copy|shared constant|copy is pinned" over plan and spec (only plan:62 and plan:324); grep -rn "telegram" over ./lg and ./ui.py (only lg:184-196, the unrelated `lg where` env report); ls tests/ (no existing copy cross-check file). Measured the sentence length with python3: 66 characters, not the 62 claimed.

_Severity corrected to:_ minor

---

### gap-7 — minor

**Claim.** The spec's headline reason for the slug form - 'The workflow id is printed once, by lg start, and then it is gone, while the run directory name is the thing the owner already knows' - never reaches the surface agents actually read. No task's Files block touches skills/loopgraph/SKILL.md or README.md, so the skill keeps telling every agent in every other project to use `lg status <workflow-id> ledger`. Nothing breaks; the feature just stays undiscoverable outside this repo.

**Touches:** task-5, file:skills/loopgraph/SKILL.md, AC-27, AC-28  
**Found by:** purefns

**Evidence offered**

```
spec:264-268 (the decision); skills/loopgraph/SKILL.md:159; README.md:218 and :281; the Files blocks of Tasks 4 and 5 at plan:274-277 and plan:352-355 list only lg and tests/test_lg_status.py. AGENTS.md:12-13 routes cross-project work to skills/loopgraph/.
```

**Verdict 1 (facts) — stands**

Every citation checks out, and nothing anywhere else covers it.

The spec decision is real and says exactly what the gap quotes. Spec lines 264-268: "**`lg status` takes a workflow id or a run-directory slug.** The owner's choice, replacing an earlier id-only line. The workflow id is printed once, by `lg start`, and then it is gone, while the run directory name is the thing the owner already knows."

The two documents still teach the id-only form. SKILL.md:159 reads "Also useful: `lg status <workflow-id> ledger` for the scoreboard"; README.md:218 and :281 both read `lg status <workflow-id> ledger`. Neither file mentions a slug for `lg status` anywhere - `grep -n slug` over both returns only run-directory paths for `lg start`, `lg tail` and container paths.

No task touches either file. The Files blocks at plan:274-276 (Task 4) and plan:353-354 (Task 5) list only `lg` and `tests/test_lg_status.py`. I listed the Files block of all thirteen tasks: not one names README.md or `skills/loopgraph/SKILL.md`. Grepping the whole plan for "README" and "SKILL.md" returns a single hit, plan:390, and it is a backward-compatibility constraint, not a change: "`lg status <id> ledger` keeps printing JSON: README.md and `skills/loopgraph/SKILL.md` document it." The spec's only hit, line 161, is the same thought inside AC-24. Both documents mention these files purely as a reason not to break them, never as a reason to update them. The plan's Global constraints carry no blanket "update the docs" rule either.

AGENTS.md:12-13 routes as claimed: "**Running a task through the engine from another project** — use the skill in `skills/loopgraph/`."

I checked three ways the gap could already be handled and none of them hold. The spec's Non-goals list (lines 224-246) does not exclude documentation. The spec's Scope list names ui.py, the shared .env reader, workflows/run.py, activities/notify.py, lg and tests - docs are absent by omission, not by a stated decision. And the CLI's own help does not fill the hole: `lg`'s status parser at lg:217-219 declares the positional as `workflow_id` with the help string "query a workflow (tries status, then ledger)", and plan Task 5 keeps that field name (`the Namespace fields cmd_status reads are workflow_id, query, json`) while adding only `--json`. So after this ships, `lg status --help` still says `workflow_id` too. The slug form reaches no reader anywhere.

The repo does treat docs as shippable: tests/test_review_fixes.py:135 parametrises a docs test over README.md, SPEC.md, skills/loopgraph/SKILL.md, INSTALL_WITH_AGENT.md and AGENTS.md. That test only parses YAML blocks, so it will not catch this, but it shows docs are in scope for this project's definition of done.

Severity stays minor, as claimed. AC-27 and plan Task 5 step 2 keep the id path byte-for-byte identical, so nothing in the docs becomes wrong - only incomplete. A run still works if every agent and the owner keep typing ids. The cost is one line in each of two files, and the loss is discoverability, not correctness. I considered raising it to important, since the decision's whole stated purpose is owner ergonomics and the affordance ends up invisible in all three places a person would look (README, skill, `--help`), but an owner who asked for the feature knows it exists, and no behaviour breaks. Minor is right.

_Opened:_ Opened in full or in the cited range: docs/superpowers/specs/2026-09-05-ui-state-first-design.md (Goal/Scope lines 1-30, AC-23 to AC-28 at 150-175, Non-goals 224-246, Decisions 247-305, and lines 262-272 verified with cat -n so the 264-268 citation is exact); docs/superpowers/plans/2026-09-05-ui-state-first.md (Global constraints 28-73, Task 4 at 267-345, Task 5 at 345-418, plus a grep of every "### Task" and "**Files:**" block across all 13 tasks); skills/loopgraph/SKILL.md lines 144-162; README.md lines 210-225 and 275-290; AGENTS.md lines 1-40 with 10-16 verified via cat -n; lg lines 1-12 and the status argparse block at 215-222; tests/test_review_fixes.py lines 128-165. Greps run: "SKILL.md|README" over both spec and plan (one hit each, both backward-compatibility notes); "slug" over README.md and skills/loopgraph/SKILL.md (no lg status slug anywhere); "README|SKILL.md" over tests/ (only the YAML-parse test).

_Severity corrected to:_ minor

---

### gap-ac20-untested — important

**Claim.** AC-20 - the location line on every card - is delivered by Task 3 with no test intent anywhere that checks a card text actually starts with it.

**Touches:** AC-20, task-3, fn:_park_note, fn:_stopped_note, fn:_owner_card  
**Found by:** workflow

**Evidence offered**

```
Task 3's four test intents (plan:232-237) pin the awaiting question key, the six/seven-element args=[...] lists, the bare owner_question, and a determinism grep. Task 2's intents (plan:169-173) test location_line in isolation and that a card starting with one still routes. Nothing binds _ask_owner (workflows/run.py:424), _park_note (:429), _stopped_note (:440) or _owner_card (:479) to the helper. AC-20 also pins two cases with no check at all: 'item 3 of 3' when every item was parked (the run():191 all-parked call) and the round number appearing only on the decision card. Task 3's whole user-visible value can be dropped or half-wired with the suite green, and the existing 199 tests contain nothing about card location.
```

**Verdict 1 (facts) — stands**

Every citation checks out, and nothing in the plan or the existing suite covers AC-20's user-visible half.

What the claim says and what I found:
1. Task 3 is the task that delivers AC-20 (plan:188 "Delivers: AC-5, AC-20, ..."), and its Behaviour section (plan:208-216) spells the location line into all four card texts. So the plan does state the behaviour precisely — this is a testing gap, not a missing-design gap.
2. Task 3's four test intents (plan:230-237) are exactly as quoted: the `"question": summary` source pin, the six/seven-element `args=[...]` pin, the bare `owner_question` pin, and the determinism grep. None mentions `location_line`. I checked the six-element list against the code — `args=[kind, wf_id, run_dir, summary, commit, options]` (workflows/run.py:389) — so pinning it constrains the count only, never what `summary` contains. That intent cannot catch a dropped location line.
3. Task 2's intents (plan:169-173) are the helper's two shapes plus `test_a_card_whose_summary_starts_with_a_location_still_routes`, which builds the summary itself inside the test and only asserts `wf_from_card` still finds the id. It never touches `workflows/run.py`.
4. `grep -n location` over the whole plan returns no test intent outside Task 2; grep over the existing tests returns nothing pinning a card's location prefix. `tests/test_visibility.py` has card-text tests, but only for `build_card_text` (lines 45, 51). `tests/test_review_fixes.py` source-pins `_owner_card` twice (lines 867, 925) for the discard branches, not for its summary.
5. The named functions and line numbers are right: `_ask_owner` at 424, `_park_note` at 429, `_stopped_note` at 440, `_owner_card` at 479, and the all-parked `_stopped_note` call at run.py:191, which is AC-20's "item 3 of 3 when every item was parked" case.
6. Task 3's Verify (plan:239-261) is pytest plus a read-only Temporal replay. The replay proves determinism, not card text. Task 3 carries no manual card observation either, unlike Tasks 9-13, which all end in a browser checklist.

Why it is not refuted: the rule that a plan carries no test bodies does not apply — the plan does carry test intents per task, and pins user-visible copy that way everywhere else (Task 11 `test_the_board_copy_is_pinned`, Task 12 `test_the_rounds_copy_is_pinned`, Task 13 `test_the_diff_is_fetched_by_workflow_id`). Task 3 is the one task whose user-visible copy has no such pin, in a task that explicitly says its test style is source pins (plan:192).

Severity stays important, not critical: the behaviour is written out unambiguously, so an implementer following the plan will most likely write it correctly; the risk is that a wrong or half-wired version (line on one card and not the others, item/total swapped, round number leaking onto a note) ships green and unnoticed. The fix is small and in the task's own idiom — one source-pin intent asserting `location_line(` appears in each of `_ask_owner`, `_park_note`, `_stopped_note`, `_owner_card`, plus the `(len(items), len(items))` all-parked call. Not critical because it breaks nothing structural and cannot make the live engine unsafe; the determinism and argument-count protections Task 3 needs are already pinned.

_Opened:_ Opened in full: /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md (all 927 lines, Tasks 2 and 3 read closely at :132-263). Read spec sections /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md lines 1-45 (scope and test plan), 45-60 (AC-4, AC-5), 125-165 (AC-19 through AC-25), 195-235 (AC-32 through AC-37 and non-goals). Read /home/kevin/projects/loopgraph/workflows/run.py lines 160-200 (`run()`, the `_stopped_note` calls at 174 and 191, `_park_note` at 178, `_owner_card` at 194), 295-315 (`_run_item`'s `_ask_owner` call at 304 and `record_owner_answer`), 364-400 (`_await_decision`, `args=[kind, wf_id, run_dir, summary, commit, options]` at 389), 400-520 (`_ask_owner` 424, `_park_note` 429, `_stopped_note` 440, `_note` 458, `_owner_card` 479). Read /home/kevin/projects/loopgraph/tests/test_visibility.py lines 1-60 and /home/kevin/projects/loopgraph/tests/test_review_fixes.py lines 855-935. Ran `grep -rn "item .* of \|of {total}\|_park_note\|_stopped_note\|_ask_owner\|_owner_card\|build_card_text" tests/` (only hits: test_owner_answers.py:81 audit-prompt text, test_visibility.py build_card_text tests, test_review_fixes.py `_owner_card` discard pins) and `grep -n location` over both the plan and the spec.

_Severity corrected to:_ important

---

### gap-agents-md-restart-rule-absent — minor

**Claim.** The spec twice justifies its central safety decision by citing a rule in AGENTS.md that AGENTS.md does not contain, and no task says how the new workflow code ever reaches the running worker.

**Touches:** decision:replay-not-restart, file:AGENTS.md, task-3, file:worker.py  
**Found by:** workflow

**Evidence offered**

```
spec:245 ('Runs hold cards right now, and AGENTS.md forbids it') and spec:297-298 ('AGENTS.md forbids restarting the stack while runs hold cards'). I read AGENTS.md in full and grepped it for restart, docker compose and stack: no match. AGENTS.md's rules are signals-not-polling, the supervisor's isolation, workflow determinism, container paths, no home directory, nothing under runs/ committed. The restart ban is real in this pipeline's own safety block, but an implementing agent that reads only AGENTS.md - which is what AGENTS.md:15 tells engine changers to do - will not find it. Compounding this: worker.py loads LoopGraphRun at process start (worker.py:20,48), so Task 3's edit is inert until the worker restarts, and no task, global constraint or Verify step names who restarts it, when, or what to do about a run holding a card at that moment. The spec's non-goal 'Rescuing runs already waiting when this ships' (spec:234-236) notes the risk and stops there.
```

**Verdict 1 (facts) — stands**

Both factual halves hold. AGENTS.md genuinely contains no restart rule (read in full; grep for restart/docker compose/stack returns nothing; a repo-wide git grep over tracked files finds the rule in no tracked document at all), yet the spec cites AGENTS.md for it twice, at 244-245 and 297-298. And worker.py really does bind the workflow class at process start (import line 20, registration line 48), so Task 3's edit to workflows/run.py cannot take effect until the worker restarts, and no task, constraint or Verify step names who does that or when.

I lower the severity from important to minor because the consequence the hunter argues for is already prevented somewhere they did not cite. The plan's Global constraints, lines 42-47, state the ban directly and without leaning on AGENTS.md: "The engine is live and holds owner cards. Nothing restarts, stops or `docker compose down`s the stack, and nothing restarts the worker... Replay is proven with `temporalio.worker.Replayer` over fetched history (Task 3), never with a worker restart." Task 3's Verify closes with "The script signals nothing and restarts nothing." The implementing agent's operating document is the plan, not the spec's rationale prose, so the failure mode "an agent reads only AGENTS.md, finds no ban, restarts the worker" is fenced by the document the agent is actually executing. The hunter's own framing concedes the ban "is real in this pipeline's own safety block".

What survives is a documentation-accuracy defect: the spec attributes a real constraint to the wrong file, so anyone auditing the safety argument by opening AGENTS.md finds nothing there. Fixable either by adding the rule to AGENTS.md (where it arguably belongs, since it is a genuine engine rule) or by rewording two spec lines to cite the live-run safety block instead. The second half — nothing says how the code reaches the worker — is a real omission, but the spec names the event and its risk explicitly at 234-236 ("Rescuing runs already waiting when this ships. A worker restart replays workflow code, so a run holding a card from before the change may need answering or terminating by hand. This phase notes the risk and does not migrate anything"), and a plan task that restarted the worker would itself violate the live-run constraint the plan and the safety block both impose. That makes it a deliberately deferred deploy step, not an unnoticed hole.

_Opened:_ Opened in full: /home/kevin/projects/loopgraph/AGENTS.md (76 lines, read end to end) — rules are signals-not-polling (33-38), supervisor isolation (40-47), workflow determinism (49-53), container paths (55-56), no hardcoded home dir (58-59), nothing under runs/ committed (61-62); AGENTS.md:15 is "**Changing the engine's own code** — the rest of this file."; no restart, docker compose or stack rule anywhere.

Opened in full: /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md (308 lines). Confirmed spec:244-245 "**Restarting the worker to prove replay works.** Runs hold cards right now, and `AGENTS.md` forbids it. AC-33 replays exported history with the SDK's replayer instead." and spec:297-298 "**Replay is proven with the replayer, not a worker restart** (auto-resolved). `AGENTS.md` forbids restarting the stack while runs hold cards; exported history is read-only." Also spec:234-236 (the "Rescuing runs already waiting when this ships" non-goal). Note spec:228 and spec:253 cite AGENTS.md for the one-way-in signal rule, and those citations ARE accurate against AGENTS.md:33-38.

Opened in full: /home/kevin/projects/loopgraph/worker.py (60 lines). Line 20 `from workflows.run import GateCheckRun, LoopGraphRun, RoundRun`; line 48 `workflows=[GateCheckRun, RoundRun, LoopGraphRun],` inside Worker(). Both at process start, as claimed.

Opened: /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md — Global constraints (lines 28-70, in particular 42-47 the live-engine/no-restart constraint), Task 2 (lines 160-182), Task 3 in full (lines 184-265, including its Interfaces, Constraints, Test intents and the Replayer Verify script whose closing line is "The script signals nothing and restarts nothing."), and the plan tail through Task 13 (lines 540-927) to confirm the last task is the whole-branch browser check and no task ships code to the worker.

Commands run: grep -rniE "restart|docker compose|docker-compose|\bstack\b" AGENTS.md README.md (AGENTS.md: zero hits; README hits are unrelated prose about reboots and "the stack is up"); git grep -niE "do not restart|don't restart|never restart|restart the worker|docker compose down" (zero hits repo-wide over tracked files); find for other AGENTS.md files (only the root one, three run-worktree copies, and one inside .venv); git status confirming the spec and plan are untracked, which is why git grep does not see the plan's own copy of the rule.

_Severity corrected to:_ minor

---

### gap-1 — important

**Claim.** format_status will crash on the result-fallback ledger of two live runs, because their ledgers have no `items` key at all and neither Task 4's copy block nor AC-25's test list covers an absent key — only an empty one.

**Touches:** task-5, task-4, fn:format_status, AC-25, AC-26, decision:result-fallback-on-closed, AC-7, task-11, artifact:ledger-microbits-fact-corrections, file:ui.py, file:workflows/run.py  
**Found by:** cli, page

**Evidence offered**

```
Live probe: `run-2026-09-05-microbits-fact-corrections-36437d`.result() keys are ['checkpoint','learn','merge','owner_decision','rounds','status'] and `run-2026-09-05-microbits-ideas-sharpen-794631`'s are ['checkpoint','reason','rounds','status'] — no `items`. Both have run directories on disk (runs/2026-09-05-microbits-fact-corrections, runs/2026-09-05-microbits-ideas-sharpen), so the slug is exactly what the owner types per the spec decision at spec:264-268. Both fail the `ledger` query with WorkflowQueryFailedError '[TMPRL1100] Nondeterminism error', so `lg status <slug>` lands on plan:380-382 step 4's handle.result() and hands that dict to format_status (plan:383). Plan:300-306 pins '(none yet)' only 'when items is empty'; plan:317 says missing optional keys print nothing, but items/rounds are not optional in the copy block; AC-25 (spec:163-166) names 'empty items and rounds' and no absent-key shape. Result: KeyError and a traceback on stdout for a command AC-29 says must never show a Temporal traceback.
---
Fetched read-only from the live Temporal on 2026-09-05: `run-2026-09-05-microbits-fact-corrections-36437d` ledger top-level keys are `['checkpoint','learn','merge','owner_decision','rounds','status']` — no `items`. `workflows/run.py:122` only creates `items: []` in a workflow that runs the current code; a closed pre-queue workflow's recorded result does not have it, and `/api/run` serves that recorded result (Task 7 Behaviour 1). Spec AC-7 and plan Task 11 Behaviour 3 both say only "Empty `items` shows `no items yet`". `lg status` (Task 4 copy block, AC-23) has the same hole via `ledger['items']`.
```

**Verdict 1 (facts) — stands**

The gap stands. Every quotation is accurate, every named section exists and says what the hunter says it says, and the input shape it depends on is real on this machine right now — I confirmed it with a read-only probe rather than trusting the claim.

What is true:
1. The ledger shape exists. Commit 3f49df6 ("Work items per run, parking, and Telegram required", 2026-09-05 10:17) is what added `items` to the ledger; its parent initialises `{"status", "rounds", "checkpoint"}` with no `items` at all. Both named run directories were written 00:59-04:05 that morning, hours before that commit, so their recorded results predate the key.
2. My own read-only probe against localhost:7233 reproduces the hunter's numbers exactly: both workflows are COMPLETED, both fail the `ledger` query with `WorkflowQueryFailedError: [TMPRL1100] Nondeterminism error`, and `handle.result()` returns keys `[checkpoint, learn, merge, owner_decision, rounds, status]` and `[checkpoint, reason, rounds, status]`. No `items` in either.
3. The path to `format_status` is exactly as described. `LoopGraphRun` defines only a `ledger` query (workflows/run.py:525-527), no `status` query, so plan Task 5's default `["status", "ledger"]` both fail, step 4's `describe()` guard passes (workflow is closed), `handle.result()` returns the legacy dict, and step 5 sends a `ledger`-or-fallback result to `format_status`.
4. The documents genuinely cover only the empty case. Task 4's copy block pins `(none yet)` "when items is empty"; AC-25 enumerates test shapes and names "empty `items` and `rounds`" with no absent-key shape; AC-7 says "While `items` is empty ... the section says `no items yet`"; Task 11 step 3 says "Empty `items` shows `no items yet`".
5. Nothing upstream normalises the shape. Task 7 Behaviour 1 says the fallback is `handle.result()` served as-is, so `/api/run` hands the legacy dict straight to Task 11's `patchItems`. The page side has no catch-all at all, so the board path is the weaker of the two, not the stronger.

The one thing that could have rescued it — plan:317, "Missing optional keys print nothing rather than `None`" — does not clearly cover `items`. The copy block marks exactly two things conditional (`reason`, and the whole `awaiting` section); `items:` and `rounds:` are drawn unconditionally with an explicit empty-case. So under the copy block `items` is not optional, and an implementer following it literally writes `ledger["items"]`. At best the sentence makes the contract ambiguous, which is itself the gap: this is an under-specified contract for an input shape that provably exists, not a "there is no code for this" complaint.

Why I dropped critical to important:
- The blast radius is legacy-only. Every run started after 2026-09-05 10:17 carries `items`; 2 of the 13 workflows on this Temporal are affected. Nothing live breaks, no data is lost, no design has to change — it is one defensive read in `format_status`, one in `patchItems`, and one added test shape in AC-25.
- Two details in the framing are stretched. AC-29 (spec:182-184) promises no traceback specifically on the slug-matches-no-workflow path, and specifically no *Temporal* traceback; a `KeyError` out of the formatter is neither. And an uncaught traceback lands on stderr, not "stdout" as the evidence says — which matters, because AC-24/AC-30 are the rules about stdout carrying only the summary.
Neither error touches the substance, but together they are what pushed the rating to critical. A visible crash on two of the owner's own runs, on a headline new feature, fixed by a one-line defensive read, is important.

_Opened:_ Opened (all absolute paths):
- /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md — lines 1-100 (AC-1 to AC-10, incl. AC-7 at 62-65), 150-215 (AC-23 to AC-35, incl. AC-25 at 163-166, AC-26 at 167-169, AC-29 at 182-184), 255-280 (the decisions block: "`lg status` takes a workflow id or a run-directory slug" and "falls back to the workflow result on a closed run").
- /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md — lines 280-400 (Task 4's `format_status` signature, the verbatim copy block at 292-308, Behaviour 3 at 317, the AC-25 test-intent list at 327-336; Task 5's steps 1-6 at 373-386 and its constraints at 386-392), 486-531 (Task 7, `/api/run` and `TemporalFeed.ledger`), 764-830 (Task 11, `patchItems` at 805). Also grepped the whole plan and spec for "items", "missing", "absent", "optional", ".get(", "traceback".
- /home/kevin/projects/loopgraph/workflows/run.py — ledger init at :122, the `items` writes at :155-163, the three `return self._ledger` sites (:175, :192, :195), and the query/signal decorators (only `ledger` at :525-527 on LoopGraphRun; the `status` queries at :46 and :75 belong to GateCheckRun and RoundRun).
- /home/kevin/projects/loopgraph/ui.py — whole file; the existing closed-run fallback in `TemporalFeed._runs` at :161-183, which uses `ledger.get("rounds", [])` for the row list.
- /home/kevin/projects/loopgraph/lg — `cmd_status` at :84-95 (today's re-raise).
- /home/kevin/projects/loopgraph/AGENTS.md and .gitignore (for the gated-command rules and runs/ handling).

Commands run (all read-only):
- `git log -S'"items": []' -- workflows/run.py` → single hit, 3f49df6, dated 2026-09-05 10:17:08 +0300.
- `git show 3f49df6~1:workflows/run.py` → ledger init `{"status": "running", "rounds": [], "checkpoint": None}`, no `items`.
- `ls -la --time-style=long-iso` on both run directories → fact-corrections written 01:19-04:05, ideas-sharpen 00:59-01:13, both before that commit.
- A throwaway probe script at /tmp/claude-1000/-home-kevin-projects-loopgraph/14a6376c-1363-475c-83c8-0968ea97d2ae/scratchpad/probe.py, run with the repo venv: connected to localhost:7233, listed `WorkflowType = "LoopGraphRun"` (13 workflows), and for the two named ids called `query("ledger")` and `result()`. Output:
  run-2026-09-05-microbits-fact-corrections-36437d — COMPLETED; query error `WorkflowQueryFailedError: [TMPRL1100] Nondeterminism error: Activity type of scheduled event 'execute_round' does not match...`; result keys ["checkpoint","learn","merge","owner_decision","rounds","status"].
  run-2026-09-05-microbits-ideas-sharpen-794631 — COMPLETED; same query error; result keys ["checkpoint","reason","rounds","status"].
Nothing was written, committed, restarted or stopped.

_Severity corrected to:_ important

---

### gap-2 — important

**Claim.** AC-33's replay proof — the one thing standing between this branch and a stranded live run — cannot be run as written, and its fallback cannot tell a regression from drift that is already there.

**Touches:** task-3, AC-33, decision:replay-not-restart, file:workflows/run.py  
**Found by:** cli

**Evidence offered**

```
Spec:204-207 says to 'fetch the history of a workflow that is waiting on a card' and replay it. Live: `list_workflows('WorkflowType = "LoopGraphRun" AND ExecutionStatus = "Running"')` yields nothing — 0 running workflows, so no run is waiting on a card. Plan:259-261 falls back to 'drop the ExecutionStatus clause and replay the first three' and expects 'no nondeterminism error'. Replaying all 13 live histories against today's UNMODIFIED workflows/run.py: 7 pass, 6 fail with '[TMPRL1100] Nondeterminism error: Activity type of scheduled event ... does not match activity type of activity command run_baseline'. The plan gives no baseline-first step, so after Task 3 an implementer seeing a failure has no way to know whether they caused it. It only appears to pass because list order is newest-first and the newest three happen to be clean.
```

**Verdict 1 (facts) — stands**

Every factual claim checks out, and I reproduced the failure myself against unmodified code.

Quotes are accurate. Spec lines 204-207 do say "fetch the history of a workflow that is waiting on a card (`handle.fetch_history()`, read-only) and replay it ... it raises no non-determinism error." Plan lines 259-261 do say "On `0 running workflows replayed`, drop the `ExecutionStatus` clause and replay the first three." Plan line 185 asserts the premise the whole check rests on: "Runs are waiting on cards right now."

That premise is false. `list_workflows('WorkflowType = "LoopGraphRun" AND ExecutionStatus = "Running"')` returned 0. All 13 LoopGraphRun workflows are status 2 (Completed). No workflow is waiting on a card, so AC-33's check cannot be run as the spec words it, and the plan's Task 3 verify script prints `0 running workflows replayed`.

The fallback is contaminated by pre-existing drift. Replaying all 13 histories against today's untouched `workflows/run.py` (git clean at 7b61c54): 6 pass, 7 fail, every failure `[TMPRL1100] Nondeterminism error: Activity type of scheduled event 'load_work_items'/'execute_round' does not match activity type of activity command 'run_baseline'`. `git log -S run_baseline -- workflows/run.py` shows `run_baseline` entered the workflow in commit e23766d, so histories from runs started before that commit have failed replay ever since — nothing to do with this branch.

The hunter's "newest three happen to be clean" observation holds: indices 0, 1, 2 all pass. So an implementer who follows "replay the first three" literally gets a green light by luck. But the minimal edit to the plan's heredoc — deleting ` AND ExecutionStatus = "Running"` — replays all 13 and yields 7 nondeterminism errors, against a stated expectation of "no nondeterminism error." The plan shows no `limit`/break in the script, so the three-history cap is an extra edit it never spells out.

The baseline is not stable either, which is the sharper version of the hunter's point. They measured 7 pass / 6 fail; hours later I measure 6 pass / 7 fail on the same 13-workflow set. The "what fails today" line moves under you, so without recording it first, a post-change failure is genuinely ambiguous.

I checked the places that could have already handled this and none do. Spec's non-goals cover restarting the worker (244-245) and rescuing waiting runs (234-236), not the zero-running-workflows case. No baseline step appears anywhere in plan or spec (grep for baseline/replay/nondetermin). No test performs a replay — `tests/test_review_fixes.py:881` only source-pins the string `run_baseline`. Nothing in docs records the known-bad histories.

This is not a "no code for this" gap: the plan carries the verification command and its expected output verbatim, so the defect is in text the plan deliberately does state.

Severity corrected down from critical to important. The claim's framing — "the one thing standing between this branch and a stranded live run" — is contradicted by the hunter's own evidence: with 0 running workflows, no run can be stranded today. What actually breaks is the proof, not the product. AC-33's determinism guarantee goes unverified and the implementer hits a false alarm or, worse, learns to wave off nondeterminism errors as "pre-existing" and misses a real one. The fix is two lines in the plan's Verify block: record which histories fail before the change, and cap the fallback at the three the plan already names. Real and worth fixing before Task 3 runs, but not a live-data hazard.

_Opened:_ Opened and read: /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md lines 194-245 and 292-302 (AC-32 through AC-37, non-goals, decisions); /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md lines 180-300 (Task 3 in full, including the Verify heredoc and its Expected paragraph, plus Task 4); /home/kevin/projects/loopgraph/AGENTS.md lines 45-58 (the determinism rule).

Read-only commands run (no signals, no restarts, stack untouched):
- `ss -ltn` — Temporal listening on 127.0.0.1:7233.
- Python client `list_workflows('WorkflowType = "LoopGraphRun" AND ExecutionStatus = "Running"')` → 0 results. Same query without the status clause → 13 results, all status 2 (Completed), ordered newest-first from run-2026-09-05-deye-pending-restore-ad4abd (11:54:36Z) down to run-2026-09-05-microbits-ideas-sharpen-794631 (2026-09-04 21:59Z).
- `Replayer(workflows=[LoopGraphRun]).replay_workflow(...)` over all 13 fetched histories against the working tree at 7b61c54 (git clean, run.py unmodified): "pass 6 fail 7 of 13". Indices 0/1/2 PASS; 4, 7, 8, 9, 10, 11, 12 FAIL. Sample error: `[TMPRL1100] Nondeterminism error: Activity type of scheduled event 'load_work_items' does not match activity type of activity command 'run_baseline'`.
- `git log --oneline -S 'run_baseline' -- workflows/run.py` → e23766d "Close the four remaining bugs and the tests that could not fail".
- `grep -n -i "baseline|replay|Replayer|nondetermin"` across plan, spec and AGENTS.md → no baseline-first step anywhere.
- `grep -rn -i "replay|Replayer|nondetermin|run_baseline" tests/ conftest.py` → only tests/test_review_fixes.py:881, a source-string pin, no replay test.
- `grep -rn -il "nondetermin" docs/ *.md` → only the plan itself.

_Severity corrected to:_ important

---

### gap-3 — minor

**Claim.** Task 5's behaviour list has no terminal case: when the last query fails and it is not `ledger`, nothing says what is printed or what the exit code is — and a constraint deletes today's re-raise without replacing it.

**Touches:** task-5, fn:cmd_status, file:lg, AC-24  
**Found by:** cli

**Evidence offered**

```
plan:371-375 step 2 says only 'Any other failure moves to the next query'; plan:380-382 step 4's return-1 path is conditioned on 'the ledger query fails'. plan:388-389 says 'Today cmd_status re-raises; that goes' (lg:93-94). Concrete live trigger: `lg status run-2026-09-05-deye-pending-restore-ad4abd status` — queries = ['status'], and the live server answers WorkflowQueryFailedError 'Query handler for status expected but not found, known queries: [__enhanced_stack_trace __stack_trace __temporal_workflow_metadata ledger]'. lg:217's own help text advertises those two query names, so this is a form the CLI invites. Output and exit code are undefined; a bare traceback or a silent `return 1` are both consistent with the plan.
```

**Verdict 1 (facts) — stands**

The claim holds. Task 5's behaviour list genuinely has no clause for "the last query failed and it was not `ledger`".

Walking the plan's own steps for `lg status <loopgraph-id> status`:
- Step 1 (plan:369) sets queries = ["status"] because a positional query was given.
- Step 2 (plan:370-372) says a NOT_FOUND RPCError goes to step 3, and "Any other failure moves to the next query". A query-handler-not-found failure is not NOT_FOUND, and there is no next query. The step falls off the end.
- Step 3 (plan:373-376) is entered only on NOT_FOUND, so it does not apply.
- Step 4 (plan:377-379) — the only place in the whole task that says "print the error's one-line message on stderr and return 1" — opens with "When the `ledger` query fails". Here the failing query is `status`, not `ledger`.
- Steps 5 and 6 (plan:380-383) describe output only when there is a result.

So no step says what stdout/stderr carry or what the exit code is. The constraint at plan:388-389 ("No traceback reaches the terminal on the slug-miss path (AC-29). Today `cmd_status` re-raises; that goes.") removes today's fallback without naming a replacement for this path, and the residual `return 1` at lg:95 would then fire silently. The spec is no help: AC-26 (spec:167-169) puts the same condition on the same word — "when the `ledger` query raises ... When both fail it prints the error on stderr and exits 1" — and AC-29 (spec:182-184) covers only the slug-miss path.

The live trigger is real, not hypothetical. `LoopGraphRun` (workflows/run.py:113) declares exactly one query handler, `ledger` (workflows/run.py:525-527); `status` handlers exist only on `GateCheckRun` (run.py:46) and `RoundRun` (run.py:75). Meanwhile lg:217's help string for the subcommand reads "query a workflow (tries status, then ledger)", so the CLI advertises `status` as a query name a user can type against any workflow. The default path is safe (`ledger` is always last), so only the optional positional argument reaches the hole.

I checked the places a "already handled elsewhere" refutation would live and found nothing: `main()` (lg:210-249) has no try/except around `asyncio.run(args.fn(args))`, so an escaping exception is a raw traceback; the plan's Global constraints (plan:29-70) say nothing about CLI error output; no other task in the plan touches `cmd_status` (only Task 4 and Task 5 mention it).

Severity corrected from important to minor. The defect is real and would ship as either a bare traceback or, more likely, a silent exit 1 with no output at all — but the blast radius is one optional CLI argument on a developer-facing command, no acceptance criterion is violated on any branch, both plausible implementations still exit nonzero, and step 4 two lines above already states the exact message-and-return-1 pattern an implementer is likely to copy. That is a wording hole worth closing with one sentence, not a design-level miss.

_Opened:_ Opened and read in full: /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md lines 1-70 (header + Global constraints) and 345-414 (all of Task 5, with line numbers rendered to check the citations); /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md lines 145-195 (AC-23 through AC-31) plus a grep for every traceback/stderr/exit-1 mention across the spec; /home/kevin/projects/loopgraph/lg lines 60-120 (cmd_start and cmd_status, confirming the re-raise sits at lg:92-94 and the trailing `return 1` at lg:95) and lines 200-249 (the status argparse block at lg:217 and all of main(), confirming no top-level exception handler); /home/kevin/projects/loopgraph/workflows/run.py — grepped every `@workflow.query` (lines 46, 75, 525) and every class/`@workflow.defn` (GateCheckRun:28, RoundRun:58, LoopGraphRun:113) and read each handler, confirming LoopGraphRun answers only `ledger`; grep for `cmd_status`/`lg status` across the plan, README.md, skills/loopgraph/SKILL.md and AGENTS.md.

Two citation nits that do not change the verdict: the gap cites "plan:371-375" for step 2's "Any other failure moves to the next query" (that text is on line 372, and 373-375 is step 3), and "plan:380-382" for step 4's return-1 path (step 4 is 377-379; 380-382 is step 5). The quoted words are verbatim and in the steps named; only the line spans slide by about three lines. plan:388-389 and lg:93-94 are exact.

I did not run `lg status` against the live server. The trigger is confirmed statically instead: LoopGraphRun has no `status` query handler, which is precisely the condition that produces the WorkflowQueryFailedError the gap quotes.

_Severity corrected to:_ minor

---

### gap-5 — minor

**Claim.** The result() hang guard is stated against a value that can be None, and inverts this repo's own convention for a None status, so the guard can let the terminal block on a live run holding a card.

**Touches:** task-5, fn:cmd_status, file:ui.py, AC-26  
**Found by:** cli

**Evidence offered**

```
plan:380-382: 'When the ledger query fails and handle.describe().status is not RUNNING, fall back to handle.result()', with plan:386-387 explaining that without the guard a running run hangs the terminal. temporalio 1.32 types WorkflowExecution.status as `WorkflowExecutionStatus | None` and sets it to None when info.status is falsy. ui.py:165 already handles that case the other way — `wf.status.name.lower() if wf.status else 'running'` — so a None status is treated as running by the dashboard and as closed by the plan. The plan also never names the comparison target: lg:18 imports only Client, and temporalio.client.WorkflowExecutionStatus.RUNNING is never mentioned, inviting a `.name != 'RUNNING'` or `!= 1` comparison instead.
```

**Verdict 1 (facts) — stands**

Every fact the gap cites checks out, but the harm it claims does not. The plan really does state the guard as "handle.describe().status is not RUNNING" (plan:377-379 — the hunter's citation of 380-382 points at the --json output rule instead, though the quoted sentence is verbatim). temporalio 1.32.0 as installed really does type that field Optional: WorkflowExecution.status is "WorkflowExecutionStatus | None" (_workflow.py:1279) and is set to None whenever info.status is falsy (_workflow.py:1349), and WorkflowExecutionDescription inherits that path through _from_raw_description (_workflow.py:1415+). ui.py:165 really does default a falsy status to "running", the opposite of what the plan's phrasing implies. WorkflowExecutionStatus is named nowhere in the plan or the spec, and spec AC-26 covers only the closed-workflow fallback, so nothing elsewhere handles the None case. Nothing here is a misread, a nonexistent reference, or already covered.

What fails is the consequence. status is None only when the server reports WORKFLOW_EXECUTION_STATUS_UNSPECIFIED (0) for a workflow that exists, and Temporal's DescribeWorkflowExecution always reports RUNNING for a live run. So no live run holding an owner card can reach the result() fallback through this path; the Optional is defensive typing, not a reachable hang. The second half of the claim — that the plan never names the enum, "inviting" a .name comparison — is close to an implementation-detail complaint, and a .name access on None would raise rather than hang anyway.

What is left is genuine but small: the plan branches on a value the SDK types as optional and never says which way an unknown status falls, while the repo's only precedent (ui.py:165) falls the other way. Worth one clarifying clause in the plan, not an important defect. Severity corrected from important to minor.

_Opened:_ Opened: /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md lines 340-425 (Task 5 header, Delivers, Files, Interfaces, Behaviour steps 1-6, Constraints, Test intents) with line numbers via grep -n; /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md lines 155-185 (AC-23 through AC-30, including AC-26 at line 167) plus greps for "RUNNING", "describe", "hang", "block", "result()"; /home/kevin/projects/loopgraph/ui.py lines 1-40 and 150-190 with line numbers (confirming ui.py:165); /home/kevin/projects/loopgraph/lg lines 1-30 (confirming lg:18 imports only Client) and the current cmd_status at lg:84-95 plus the argparse status block at lg:220; /home/kevin/projects/loopgraph/.venv/lib/python3.13/site-packages/temporalio/client/_workflow.py lines 1260-1300, 1330-1400, 1415-1455 and .venv/.../temporalio-1.32.0.dist-info for the version; greps for "WorkflowExecutionStatus" across the plan, spec, and the client package; grep for "describe" across tests/ (no hits).

Key text confirmed verbatim — plan:377-379: "When the `ledger` query fails and `handle.describe().status` is not `RUNNING`, fall back to `handle.result()`. When that fails too, or the workflow is running, print the error's one-line message on stderr and return 1." plan:386-387: "`handle.result()` blocks until the workflow ends. Without the `describe()` guard a running run whose ledger query failed would hang the terminal for as long as the run waits on its card." ui.py:165: `"state": wf.status.name.lower() if wf.status else "running", "detail": ""`. temporalio _workflow.py:1349: `status=WorkflowExecutionStatus(info.status) if info.status else None,`

_Severity corrected to:_ minor

---

### gap-6 — minor

**Claim.** Nothing updates the copy that describes `lg status`, and a global constraint tells the implementer not to — so the shipped help, docstring, README and skill all keep describing the old command.

**Touches:** task-5, file:lg, file:README.md, file:skills/loopgraph/SKILL.md, decision:slug-or-id  
**Found by:** cli

**Evidence offered**

```
lg:217 help reads 'query a workflow (tries status, then ledger)' and lg:4-6 says 'lg status <workflow-id> queries gate red/green'; the positional is named workflow_id while the spec calls it <arg> (spec:152). README.md:218,281 and skills/loopgraph/SKILL.md:159 document only `lg status <workflow-id> ledger`. No task's Files block lists README.md or SKILL.md, and plan:62 says 'User-facing copy is pinned in the tasks. Do not reword it at implementation time' while Task 5 pins only the two stderr lines (plan:363-367). The spec decision at spec:264-268 makes the slug form the headline — 'the run directory name is the thing the owner already knows' — yet neither the owner nor an agent reading the skill is ever told it exists.
```

**Verdict 1 (facts) — stands**

Every citation in the gap is accurate, and nothing in the spec or plan closes it — but the consequence is smaller than "important".

What I confirmed as true:
- `lg` line 217 really reads `sub.add_parser("status", help="query a workflow (tries status, then ledger)")` with the positional declared as `workflow_id`, and lines 4-6 of the module docstring really say "`lg status <workflow-id>` queries gate red/green". Spec line 152 (AC-23) really writes the argument as `<arg>`, and plan Task 5 (line ~356) explicitly pins the Namespace field back to `workflow_id`, so the usage line printed by `lg status --help` will keep saying `workflow_id` after a slug becomes legal.
- README.md lines 218 and 281 and skills/loopgraph/SKILL.md line 159 are the only three places outside `lg` that mention `lg status`, and all three show only `lg status <workflow-id> ledger`.
- Grepping the whole plan for "README" and "SKILL" returns exactly one hit, plan line 390, and it is a *preservation* constraint inside Task 5, not an edit instruction. No Files block in any of the 13 tasks lists README.md or SKILL.md, and no task touches the argparse help or the module docstring. The plan ends at Task 13 with no documentation task.
- Plan line 62 does say "User-facing copy is pinned in the tasks. Do not reword it at implementation time," and Task 5 pins only the two stderr lines. It also pins `--json` as `action="store_true"` with no `help=` text, so an implementer following the constraint literally ships a flag with a blank description in `--help`.
- Spec lines 264-268 do make the slug form the headline decision, justified by "the run directory name is the thing the owner already knows", and nothing tells the owner or an agent reading the skill that it exists.

Why I dropped it to minor rather than refuting or leaving it important:
- The existing README and skill text is not made *wrong* by this change. AC-24 and the Task 5 constraint at plan:390 exist precisely to keep `lg status <id> ledger` printing JSON as today, so the documented command keeps behaving exactly as documented. The docs are incomplete, not stale-incorrect.
- The `lg` module docstring was already out of date before this phase — it still frames the file as "M1" and says `lg new` / `lg start` "land with M3" although `start` has shipped. Charging this phase for pre-existing rot overstates it.
- No acceptance criterion goes undelivered, nothing breaks, and the plan stays fully executable. The whole cost is one small copy edit: three doc lines, one argparse `help=`, and a `help=` for `--json`.

So: a genuine omission in the plan's coverage, worth adding README.md, skills/loopgraph/SKILL.md and the `status` argparse block to Task 5's Files list with the copy pinned there — but a discoverability shortfall, not an important defect.

_Opened:_ Opened in full or at the cited ranges: /home/kevin/projects/loopgraph/lg (lines 1-30 and 200-240, plus a grep for every "lg status" occurrence in the file); /home/kevin/projects/loopgraph/README.md (lines 210-225, 275-290, and grep -n "lg status" giving only 218 and 281); /home/kevin/projects/loopgraph/skills/loopgraph/SKILL.md (lines 150-170, grep giving only 159); /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md (lines 1-60 Goal/Scope, 145-200 AC-23 through AC-33, 222-250 Non-goals, 255-285 Decisions, plus greps for "README", "SKILL", "help", "scope"); /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md (lines 50-75 Global constraints, 140-170 and 278-320 Task 2/4 copy blocks, 320-400 Task 4 tail and all of Task 5, the tail through Task 13, and greps for "README", "SKILL", "help=", "copy", plus a listing of all 13 task headers); /home/kevin/projects/loopgraph/AGENTS.md (grep for README/skill/document/user-facing rules — only hits are the install note at line 8 and the skill pointer at 12, no docs-sync rule). Also ran a repo-wide grep for "lg status" to confirm README.md, lg, and skills/loopgraph/SKILL.md are the only non-spec/plan files that mention it.

_Severity corrected to:_ minor

---

### gap-ledger-shared-sync-deadlock — critical

**Claim.** Task 7 declares TemporalFeed.ledger as a sync method and then says _runs shares it, but _runs runs on the feed's own event-loop thread where the sync call() deadlocks — following the plan literally empties the run list, a behaviour that ships and works today.

**Touches:** task-7, fn:TemporalFeed.ledger, fn:TemporalFeed.call, fn:TemporalFeed._runs, endpoint:/api/runs, file:ui.py  
**Found by:** endpoints

**Evidence offered**

```
The Interfaces block produces one method, `def ledger(self, wf_id: str) -> dict | None` (plan l.505), and behaviour 1 says it is 'the fallback _runs has today, extracted so _runs and /api/run share it' (plan l.522-523). A sync method on TemporalFeed can only reach the client through call(), which is `asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)` (ui.py:158-159). _runs is a coroutine that already executes on self._loop (ui.py:161-163, driven by runs() at ui.py:189). I measured that shape: submitting from inside the loop thread blocks the loop and raises TimeoutError after the full timeout, and the inner coroutine never runs. With the plan's own 'keep the call(timeout=10) bound' (plan l.541) that is 10 s of dead loop per workflow, and the outer call(self._runs()) times out first, so runs() returns [] (ui.py:190-191) and /api/runs — the left-hand column that works today — goes permanently empty. The plan needs to state two things, an async _ledger(wf_id) coroutine that _runs awaits and a sync ledger() wrapper over call(), not one sync method shared by both.
```

**Verdict 1 (facts) — stands**

Every quote checks out, and the mechanism reproduces.

The document says what the claim says it says. Task 7's Interfaces block declares exactly one ledger-fetching method on the feed, and it is synchronous: `def ledger(self, wf_id: str) -> dict | None` (plan l.506 — the hunter wrote l.505, off by one, harmless). Behaviour 1 (plan l.522-523) reads verbatim "This is the fallback `_runs` has today, extracted so `_runs` and `/api/run` share it." The constraint at plan l.540 reads "Keep the 25-row cap in `_runs` and the `call(timeout=10)` bound" (hunter wrote l.541, again off by one). `run_entry` (plan l.508-509) takes `ledger: dict | None` as an argument, which pins `_runs` as the caller that must fetch a ledger per workflow — so `_runs` really does have to reach the shared thing.

The code says what the claim says it says. `TemporalFeed` (ui.py:137) owns a private event loop on its own thread (ui.py:143-144, 150-156). `call` is `asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)` with `timeout=10` (ui.py:158-159). `_runs` is `async def` and runs on that loop (ui.py:161-163), submitted by `runs()` at ui.py:189, which returns `[]` on any exception (ui.py:188-191). Since the temporalio client is async-only, a sync `ledger()` has no way to reach it except through `call()`.

I reproduced the failure with a stripped-down copy of the same shape (`.venv/bin/python`, scratchpad script): a coroutine on the loop thread calling a sync method that submits through `run_coroutine_threadsafe(...).result(timeout)` blocks the loop, the inner coroutine never runs, `.result()` raises TimeoutError after the full timeout, and the outer submission returns `[]`. `asyncio.run_coroutine_threadsafe` has no same-thread guard, so this is unavoidable, not incidental.

Nothing anywhere resolves it. I grepped the whole plan for `_ledger`, `await`, `coroutine`, `asyncio`, `thread`, `loop thread`, `event loop`: the only hits are in Task 3's replay snippet and Task 4's `lg status` test intents. No async form of `ledger` is named anywhere. Task 8 (plan l.573, l.601) consumes `TemporalFeed.ledger` from an HTTP handler thread, where the sync form is correct — which is exactly why one method cannot serve both callers. The spec (AC-1 l.30-34, AC-26 l.167-168) only says the fallback matches `_runs`'s and says nothing about threads.

The named test intents would not catch it. All of Task 7's checks run through a `FakeFeed` over HTTP (plan l.543-551), and `test_runs_endpoint_without_temporal` takes the `self._client is None` short-circuit at ui.py:186-187. No test intent exercises a real `TemporalFeed._runs` against a live loop, so the deadlock ships silently.

The strongest counter I could build, and why it fails: a plan lists public interfaces, so a private async `_ledger` with a sync wrapper over it is arguably a body detail the plan need not state. But this plan is already operating at that register in this very task — it pins `call(timeout=10)`, the 25-row cap inside `_runs`, and "the handler stops reading `feed._client`". More to the point, the failure is not "no code for this": the stated contract (one sync method, shared by a coroutine and a handler thread) cannot be implemented as written without jamming the loop. Cross-thread reachability is a contract fact, not an implementation body.

One factual overstatement in the claim, which I do not treat as refuting. `/api/runs` does not go literally empty: with `runs()` returning `[]`, ui.py:236-239 still appends a row per log directory as `state: unknown, detail: logs only`. What is lost is every Temporal-derived row (real workflow id, state, detail, and Task 7's new times), plus the loop jams for the timeout per workflow, which also starves `/api/run` and `/api/diff` since both call `feed.ledger()`. The user-visible result is a run list stripped of live state, not a blank one.

Severity: leaving it at critical. It regresses shipping behaviour, jams the shared loop for every other endpoint, and no named test would surface it. I note for the consequence-lens pass that the damage is contingent on an implementer taking the literal reading; someone editing `async def _runs` may notice the blocking call unaided. The fix is one sentence in the plan.

_Opened:_ Opened: /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md — Task 7 in full (l.486-557, Interfaces l.498-519, Behaviour l.521-533, Constraints l.535-540, Test intents l.542-551) and Task 8 (l.570-640, `Consumes: TemporalFeed.ledger` l.573, behaviour 1 l.601); grepped the whole plan for `ledger`, `_ledger`, `await`, `coroutine`, `asyncio`, `thread`, `loop thread`, `event loop` — no async ledger form anywhere. /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md — grepped and read AC-1 (l.30-34), AC-10 (l.75), AC-26 (l.167-168), AC-32 (l.179), plus the resolved-question note at l.278; no threading guidance. /home/kevin/projects/loopgraph/ui.py — `TemporalFeed` (l.137-191): `__init__` and loop thread l.140-156, `call` l.158-159, `async def _runs` l.161-183, `runs()` l.185-191; `make_server` l.208+ and the `/api/runs` branch of `do_GET` l.234-240. /home/kevin/.agents/skills/phase/references/gaphunt.md for the severity enum and the second-refuter rule. Ran a 30-line reproduction under /home/kevin/projects/loopgraph/.venv/bin/python mirroring `call`/`_runs`/`runs` with a 2 s bound: output was "outer failed: TimeoutError / result: [] after 2.0s" and the inner coroutine's print never fired, confirming the loop-thread deadlock and the empty return.

_Severity corrected to:_ critical

**Verdict 2 (consequence) — stands**

The consequence follows. Task 7's only named ledger method is sync (`def ledger(self, wf_id) -> dict | None`, plan l.505) and behaviour 1 says it is the fallback "extracted so `_runs` and `/api/run` share it" (plan l.522-523). `/api/run` is served on an HTTP handler thread, so a sync method there is right; `_runs` is a coroutine that already runs on `self._loop`, so it cannot share the same sync method. A sync method can only reach the temporalio client through `call()`, which is `run_coroutine_threadsafe(...).result(timeout)` (ui.py:158-159), and the plan's own constraint keeps that 10 s bound (l.541).

I reproduced the shape: calling that pattern from inside the loop thread blocks the loop, the inner coroutine cannot start, and the caller raises TimeoutError after the full timeout; the outer `call(self._runs())` in the HTTP thread times out first. `runs()` swallows the exception and returns `[]` (ui.py:188-191). So every poll costs 10 s and returns no workflow rows.

Two reasons this is not a "no code for it" gap and not merely an omitted implementation detail: the plan makes a positive statement about which callers share which method, and that statement is wrong for one of the two callers; and the planned tests cannot catch it, because every Task 7 test intent runs against `FakeFeed` or `feed=None`, never a real `TemporalFeed`, and the Verify line is pytest only. The mistake ships.

The one real counter-argument is that the Interfaces block lists only the public surface (it omits `_runs`, `call`, `_client`), so an implementer is free to add a private async `_ledger` and make `ledger()` a thin wrapper. That is the correct build, and a careful reader may reach it — but the plan's sentence points the other way, and the plan is the thing under test. One sentence fixes it: async `_ledger(wf_id)` that `_runs` awaits, sync `ledger()` wrapping `call(self._ledger(...))` for the HTTP thread.

Severity: critical stands under the rubric's "breaks a shipped behaviour". One correction to the claim's wording, which does not change the severity: the run list does not go literally empty. The handler backfills every run directory from disk as a logs-only row (ui.py ~233-238), so the left column survives but degrades to one "unknown / logs only" row per directory with no state, no round/verdict detail and (after Task 7) null times, workflow-id keying is lost, and every poll stalls 10 s. The state and detail the column shows today are gone, and no live run or owner data is harmed.

_Opened:_ Opened: /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md (Task 7 in full, l.484-560; Task 5 l.350-430 for how the plan handles the same ledger fallback in `lg`); /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md (AC-1 to AC-6 l.25-60, decision log l.260-290); /home/kevin/projects/loopgraph/ui.py (TemporalFeed l.135-191 — `call` at 158-159, `async def _runs` at 161, `runs()` swallowing to `[]` at 188-191 — and the `do_GET` `/api/runs` branch at l.233-239 including the logs-only backfill and `feed._client`).

Ran a read-only reproduction in the scratchpad of the exact shape (persistent loop thread; a sync helper doing `run_coroutine_threadsafe(...).result(t)` called from inside a coroutine already on that loop): output was "outer call raised TimeoutError after 2.0s / inner call raised TimeoutError / outer got None after 2.0s / inner ran" — the inner coroutine only ran after both waits gave up, confirming the loop is blocked for the full timeout and the outer `runs()` call fails first.

_Severity corrected to:_ critical

---

### gap-no-test-for-real-ledger-fallback — important

**Claim.** The query-to-result fallback being extracted in Task 7 ends up with no test at all: every Task 7 and Task 8 test goes through a FakeFeed that replaces ledger() wholesale, so an extraction bug in the one piece that makes closed runs readable is invisible to the suite.

**Touches:** task-7, fn:TemporalFeed.ledger, file:tests/test_ui.py, AC-1  
**Found by:** endpoints

**Evidence offered**

```
Task 7's test fixture is 'a FakeFeed class in tests/test_ui.py with connected, runs(), ledger(id) returning canned values' (plan l.543-544), and all five test intents (l.545-552) assert over it or over feed=None; Task 8 reuses the same fake (l.626-628). The behaviour being extracted lives at ui.py:167-174 and carries a comment explaining why it exists — 'closed workflows: the ledger IS the workflow's return value (query-by-replay on closed runs can fail decoding in this SDK)'. Every finished run in runs/ (2026-09-04-toy-count, 2026-09-05-macweb-metadata and the rest) reads its ledger through that path, and both the run list and the new board now depend on it. Task 7 needs one test intent over TemporalFeed.ledger itself with a stubbed handle: query raises, result answers; both raise, None comes back.
```

**Verdict 1 (facts) — stands**

Every fact the gap cites holds up, and I found no coverage anywhere the hunter did not look.

The code exists as quoted. ui.py lines 166-174 are the query-then-result fallback, with the comment "closed workflows: the ledger IS the workflow's return value (query-by-replay on closed runs can fail decoding in this SDK)". Task 7 does extract exactly this: plan l.522-523 says "`TemporalFeed.ledger`: query `ledger`; on any failure try `handle.result()`; on failure return `None`. This is the fallback `_runs` has today, extracted so `_runs` and `/api/run` share it."

The spec makes it an acceptance criterion, not an incidental detail. AC-1 (spec l.30-35) spells out "The ledger comes from the `ledger` query for a running workflow and from the workflow's result for a closed one, the same fallback `TemporalFeed._runs` uses today." Task 7's Delivers line (plan l.490) claims AC-1.

The test intents cannot reach it. The Task 7 fixture (plan l.543-544) is a FakeFeed with canned `connected`, `runs()`, `ledger(id)`, plus a server built with `feed=FakeFeed(...)`; the five intents (l.545-552) either go through that fake, through `feed=None`, or through the pure `run_entry`. Task 8's fixture (l.628) is the same fake. So the object under test in every intent is the stub that replaces the extracted method, never the method.

Nothing else in the repo or the plan covers it. Grepping tests/ for TemporalFeed returns one hit — `tests/test_ui.py:19`, `ui.make_server(0, tmp_path, temporal_addr=None)`, which is the no-feed path. The only other test file importing ui is `tests/test_review_fixes.py:125`, which asserts over `page_html()`. Later plan tasks (9-13) are page-JavaScript tests.

Two things soften it but do not cancel it. Task 5 tests the twin fallback in `lg status` with a stubbed handle (`test_a_closed_run_whose_ledger_query_fails_uses_the_result`, l.407), which shows the plan's own standard is to fake the client and prove this behaviour — making the ui.py omission inconsistent rather than deliberate. And Task 13's browser checklist (l.918-926) opens the diff on a finished run, which would surface a broken fallback to a human at the end of thirteen tasks. Neither is the suite, and the gate the plan names (`pytest -q`) stays blind to it.

This is a missing test intent, not "there is no code for this" — test intents are exactly what the plan is supposed to carry, and the plan carries them for the sibling function. Severity important stands: the failure mode is every finished run silently going stateless in the list, the board and the diff pane.

_Opened:_ Opened: /home/kevin/projects/loopgraph/ui.py (lines 130-200, plus numbered 160-182 to confirm the fallback sits at 166-174 with the quoted comment); /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md (header and Global constraints l.1-80, Task 5 l.345-418, Task 7 l.486-560, Task 8 l.561-643, Task 13 l.886-925, plus greps for "ledger", "FakeFeed", "manual/browser/checklist" across the file); /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md (the Tests paragraph l.20-24, AC-1 l.30-35, the full AC list, AC-25/AC-26 l.163-172); /home/kevin/projects/loopgraph/tests/test_ui.py (all 69 lines); /home/kevin/projects/loopgraph/tests/test_review_fixes.py l.118-136. Ran: grep for TemporalFeed across the repo (only ui.py defines or uses it) and across tests/ (only the temporal_addr=None line); grep for "import ui" in tests/ (two files, both accounted for); ls runs/ (2026-09-04-toy-count and 2026-09-05-macweb-metadata are present as claimed).

---

### gap-log-names-hardcodes-glob — minor

**Claim.** log_names is specified with a literal '*.log' where activities/stream.py declares LOG_GLOB the single source of truth for these filenames — the exact drift that once made both lg tail and the dashboard find no logs at all.

**Touches:** task-6, fn:log_names, code:LOG_GLOB, file:activities/stream.py  
**Found by:** endpoints

**Evidence offered**

```
Task 6 behaviour 2 says 'the *.log file names under runs_dir/<slug>/logs' (plan l.450) and its Interfaces consume only LOG_RE (plan l.431). stream.py:18-22 says the opposite: 'One source of truth for a run's log filenames. Three places used to hardcode the shape ... When the item number joined the name, both readers silently stopped matching and nobody could watch a run at all. Anything that reads these files uses these.' lg tail already imports LOG_GLOB for its glob (lg:161-164), and tests/test_review_fixes.py:104-117 exists to keep the glob and the regex agreeing. Today's log_tails hardcodes it too (ui.py:126), so this is not a regression — but Task 6 is rewriting that exact line and is the moment to import LOG_GLOB instead.
```

**Verdict 1 (facts) — stands**

Every quote in the claim is accurate and nothing elsewhere covers it.

What the plan actually says. Task 6's Interfaces block (plan line 430) reads "Consumes: `LOG_RE` from `activities.stream` (already imported)" — LOG_RE and nothing else. Behaviour 2 (plan line 449) reads "`log_names`: the `*.log` file names under `runs_dir/<slug>/logs`, sorted, names only." The glob is spelled out as a literal in backticks, and LOG_GLOB is named nowhere in the plan or the spec — `grep -rn "LOG_GLOB" docs/superpowers/` returns nothing. (The hunter's line numbers are each one off, 431 vs 430 and 450 vs 449; the text is quoted correctly.)

What the code actually says. activities/stream.py line 18 opens "One source of truth for a run's log filenames. Three places used to hardcode the shape: the writers here, `lg tail`'s glob, and the dashboard's regex. When the item number joined the name, both readers silently stopped matching and nobody could watch a run at all. Anything that reads these files uses these." LOG_GLOB = "*.log" is line 22, LOG_RE line 23. lg line 161 imports LOG_GLOB and line 164 globs with it. ui.py line 126 — the line Task 6 deletes — hardcodes .glob("*.log"). So the claim's reading of both the document and the code is correct, and log_names is a reader of these files, which the comment says must use the constants.

Not already handled. tests/test_review_fixes.py line 117 asserts fnmatch(log_name(...), LOG_GLOB), so it pins LOG_GLOB, LOG_RE and log_name to each other — but it never touches log_names. A future change to the filename shape that moves LOG_GLOB (say to "*.txt") keeps that test green while a hardcoded "*.log" in log_names finds nothing, which is exactly the direction that once broke lg tail and the dashboard.

Severity stays minor, and I'd resist raising it. Today LOG_GLOB is the string "*.log", so behaviour is identical either way — there is no bug in the shipped design, only a missed chance to remove the last hardcoded copy while the line is being rewritten anyway. The fix is one word in the Interfaces line plus naming LOG_GLOB in behaviour 2. Calling it important would overstate a hygiene point with no present failure.

_Opened:_ Opened: /home/kevin/projects/loopgraph/activities/stream.py lines 1-60 (LOG_GLOB line 22, LOG_RE line 23, source-of-truth comment lines 18-21, log_name lines 26-28); /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md lines 400-480 (Task 6 in full: Interfaces line 430, log_names signature line 435, behaviour 2 line 449, test intents lines 465-475); /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md lines 70-95 (AC-9 through AC-13; AC-12 at line 91 mentions only LOG_RE); /home/kevin/projects/loopgraph/ui.py lines 1-30 (imports LOG_RE only) and 100-150 (log_tails at 124-129, hardcoded glob at 126); /home/kevin/projects/loopgraph/lg lines 150-175 (cmd_tail imports LOG_GLOB at 161, globs with it at 164); /home/kevin/projects/loopgraph/tests/test_review_fixes.py lines 95-125 (import line 20, test_every_reader_matches_the_names_the_writers_produce lines 108-117). Greps run: LOG_GLOB/LOG_RE/*.log/log_names/glob across the plan and spec (LOG_GLOB absent from both), LOG_GLOB across docs/superpowers/ (no hits), and "source of truth"/"log name" across AGENTS.md (no hits — no project rule states this beyond the stream.py comment).

_Severity corrected to:_ minor

---

### gap-diff-empty-after-merge — important

**Claim.** The three-dot diff AC-15 pins returns nothing for a merged run, and the response shape gives the page no way to say so - a merged run's diff pane renders blank, indistinguishable from a failure. That is the terminal state the owner cares about most: the run they approved.

**Touches:** AC-15, AC-17, endpoint:/api/diff, task-8, task-13, fn:merge_branch  
**Found by:** diff

**Evidence offered**

```
AC-15 pins `git diff --stat <base_branch>...<branch>` and `git diff <base_branch>...<branch>` (spec:106-112). activities/checkpoint.py:108-135 merges the branch into base with `--no-ff` and keeps the branch, so merge-base(base, branch) becomes the branch tip and the three-dot diff is empty. Verified two ways: a scratch repo (before merge: ` f.txt | 1 +`; after merge: stat empty, patch 0 bytes, `rev-parse --verify feat` still succeeds so no reason line fires); and live, /home/kevin/projects/deye, where merge-base(main, lg-2026-09-05-deye-pending-restore-ad4abd) == f344ec2 == the branch tip and `git diff main...lg-...-ad4abd` is 0 bytes for run runs/2026-09-05-deye-pending-restore. AC-17 (spec:119-123) and plan:586-598 require every failure to be `stat` = one line, `patch` empty, `truncated` false - byte-identical to a real empty diff - and plan:934 forbids Task 13 from special-casing `stat`. No AC and no reason line covers 'the branch adds nothing to the base'. Neither is there copy for a run before its first accepted checkpoint, which is also an empty diff.
```

**Verdict 1 (facts) — stands**

The core of the gap stands. Every link in the chain checks out against the documents and the running system.

What is true:
1. AC-15 (spec:106-112) does pin the three-dot form, verbatim: `git diff --stat <base_branch>...<branch>` and `git diff <base_branch>...<branch>`, with `base_branch` and `branch` taken from the ledger's last round.
2. `merge_branch` (activities/checkpoint.py:108-135) merges the branch into the base with `--no-ff` and never deletes the branch. Only `discard` (checkpoint.py:145-169, the owner's C choice) removes the branch and worktree. So after choosing A, the branch, the worktree and its `.git` pointer all survive.
3. Because the branch becomes an ancestor of the base, merge-base(base, branch) is the branch tip, so the three-dot diff is empty. Confirmed live in /home/kevin/projects/deye: merge-base(main, lg-2026-09-05-deye-pending-restore-ad4abd) == f344ec2 == the branch tip; `git diff main...<branch>` is 0 bytes and `git diff --stat main...<branch>` is 0 bytes.
4. None of AC-17's eight reason lines fires. Both branches pass `rev-parse --verify`, the pointer file /home/kevin/projects/loopgraph/runs/2026-09-05-deye-pending-restore/worktrees/ad4abd/.git exists and reads `gitdir: /projects/deye/.git/worktrees/ad4abd`, exactly the shape AC-16 expects, so resolve_repo succeeds.
5. The run stays reachable after it closes. ui.py:163-183 falls back to `handle.result()` when the query fails, and workflows/run.py:527 returns `self._ledger`, whose `rounds` entries carry `worktree`, `branch` and `base_branch` (run.py:229-245). So the ledger is not null, `#diff` is not hidden (plan:907-908), and the pane opens.
6. Task 13's behaviour (plan:904-906) shows `stat`, then `patch` in a `pre`, then the cut line. With both strings empty the pane renders nothing, and the constraint at plan:911 forbids special-casing `stat`. I grepped both documents for "empty", "merged", "no changes" and "nothing to show": no AC and no reason line covers "the branch adds nothing to the base". The second case the hunter names is real too - a round recorded before its checkpoint commits gives a branch with no commits and the same blank pane.

Where the hunter overstates, which is why I drop the severity a notch rather than let "critical" stand:
- "The response shape gives the page no way to say so" is wrong. `stat` is free text and is precisely how all nine reason lines arrive; the shape supports the message, the spec just never writes the copy.
- "byte-identical to a real empty diff" is wrong in the opposite direction. Every AC-17 failure carries a one-line explanation in `stat`; a real empty diff carries an empty `stat`. They are distinguishable. The pane is unexplained, not confusable with the listed failures.
- Nothing breaks: the endpoint still returns 200 with a valid shape, no 500, no traceback, no data loss.
- The moment the owner actually needs the diff is the merge-ready card, before the merge. At that point the branch is unmerged and the diff is complete and correct. The blankness appears only after the decision is made.

So this is a missing acceptance criterion and missing copy for two states, one of them a terminal state - a real hole a builder would ship, worth fixing, but not a correctness or safety failure. Important, not critical.

_Opened:_ Opened and read: docs/superpowers/specs/2026-09-05-ui-state-first-design.md lines 80-140 (AC-11 through AC-19, covering the cited AC-15 at 106-112 and AC-17 at 119-123) plus a grep of the whole spec for "diff", "empty", "merged", "no changes"; docs/superpowers/plans/2026-09-05-ui-state-first.md lines 495-545 (Task 7, the feed and ledger contract), 561-620 (Task 8, including the cited reason-copy block at 586-598) and 886-927 (Task 13 in full - note the cited plan:934 is past the end of the file; the "no special-casing" constraint the claim means is at plan:911); activities/checkpoint.py lines 90-169 (merge_branch at 108-135 and discard at 145-169); workflows/run.py lines 228-250 (round entry fields) and 470-527 (the A/B/C owner card and the ledger return); ui.py lines 155-200 (TemporalFeed._runs and its handle.result() fallback) plus a grep confirming /api/diff is not yet implemented, which is expected for a plan.

Ran read-only in /home/kevin/projects/deye: git log --oneline -3 shows 5bbb428 "Merge branch 'lg-2026-09-05-deye-pending-restore-ad4abd'"; git merge-base main lg-2026-09-05-deye-pending-restore-ad4abd and git rev-parse of the branch both give f344ec28c5d5e46ba3dd64e45a5ed1758b514042; git diff main...<branch> pipes 0 bytes and git diff --stat main...<branch> pipes 0 bytes. Read /home/kevin/projects/loopgraph/runs/2026-09-05-deye-pending-restore/worktrees/ad4abd/.git, which holds the single line "gitdir: /projects/deye/.git/worktrees/ad4abd". Wrote nothing and ran no state-changing command.

_Severity corrected to:_ important

---

### gap-cap-after-the-fact — minor

**Claim.** The 200 KB cap is stated as a cut applied after both git commands have run to completion, so nothing bounds how much a single /api/diff request pulls into the dashboard process, and no subprocess timeout is stated anywhere. That defeats the cap's own stated reason for existing.

**Touches:** AC-18, AC-17, decision:patch-cap-200kb, task-8, fn:branch_diff, endpoint:/api/diff  
**Found by:** diff

**Evidence offered**

```
plan:608-610: 'then `git diff --stat <base>...<branch>` and `git diff <base>...<branch>`, `cwd=repo`. Cut `patch` at `DIFF_CAP` bytes and set `truncated`.' The spec's decision for the cap (spec:300) is 'small enough that one request cannot stall the page'. ui.py serves on a ThreadingHTTPServer (ui.py:243) with no request limit, and AC-9 (spec:72-74) says the diff is fetched on every pane open, so repeated opens fan out concurrent unbounded git processes. `stat` is not capped at all. The repo already learned both halves of this lesson elsewhere and Task 8 cites neither: activities/gate.py:97-124 ('The timeout has to govern the PROCESS, not the pipe', after a timeout that was silently not enforced) and activities/stream.py:16 LOG_CAP with a head-truncating writer. Nothing in AC-18, AC-17 or Task 8 states that the read must be bounded, that git must be given a deadline, or that a hang - which is not an exception, so plan:612's catch-all never fires - must still answer 200.
```

**Verdict 1 (facts) — stands**

The factual core survives verification. Task 8 behaviour 3 (plan:608-610) does order the work as "run both git commands, then cut the patch", so nothing in either document bounds the read. No subprocess deadline is stated anywhere: the plan's only timeout is `call(timeout=10)` at plan:540, which bounds the Temporal query, not git. AC-17 (spec:119-123) enumerates eight failure modes that are all error returns, so plan behaviour 5's "any exception -> 200" genuinely never fires on a hang. AC-18 (spec:124) caps `patch` and leaves `stat` uncapped, which is a real inconsistency with the cap's intent. The citations to activities/gate.py:84-124 and activities/stream.py:16 are accurate, and nothing elsewhere in the spec, plan or existing ui.py handles this.

The severity is wrong, though, and the reasoning that inflates it is a misread. The cap's stated reason (spec:299-300) is "small enough that one request cannot stall the page" — a statement about the payload delivered to the browser, which cutting at 204,800 bytes fully achieves. The hunter reinterprets it as a promise to bound the dashboard process's read and then declares it defeated; the document does not say that.

Real-world impact is small. The server binds 127.0.0.1 (ui.py:247), so it is a single-user local dev tool. The commands are `rev-parse --verify` and `diff` on the user's own local repo: no network, and with output on a pipe rather than a tty git spawns no pager, so the usual hang paths do not apply. The worst realistic outcome is one browser fetch that never fills and one thread holding memory; the threading server keeps every other poll alive, and restarting `lg ui` clears it. No data loss, no wrong results, no risk to live runs holding owner cards. The "repeated opens fan out concurrent unbounded git processes" line is technically true but describes one local user rapidly toggling a pane. The repo precedents cited are not comparable: gate.py bounds arbitrary agent-supplied build commands and stream.py bounds durable log files that would otherwise grow without limit.

Worth fixing (a subprocess deadline, a bounded read, a cap on `stat`, and a reason line for a timeout), but as hardening, not as a critical defect.

_Opened:_ Opened in full: docs/superpowers/specs/2026-09-05-ui-state-first-design.md lines 60-90 (AC-9, AC-10), 100-132 (AC-15 through AC-19), 220 (AC-37) and 280-320 (the decisions block, including the 200 KB cap at 299-300); docs/superpowers/plans/2026-09-05-ui-state-first.md lines 30-70 (global constraints), 540-612 (Task 7 tail and all of Task 8) and 885-925 (Task 13, the diff pane); /home/kevin/projects/loopgraph/ui.py lines 230-260 plus a grep for subprocess/timeout/ThreadingHTTPServer across the file; /home/kevin/projects/loopgraph/activities/gate.py lines 84-130; /home/kevin/projects/loopgraph/activities/stream.py lines 1-40. Also grepped both documents for DIFF_CAP, LOG_CAP, timeout, 200 KB, 204800, bound, stall, hang, slow, memory, risk, and grepped the whole repo for `subprocess` to check for an existing bounded git runner Task 8 could inherit (there is none in production code; only gate.py and execute_round.py use asyncio subprocesses, and the rest are tests).

Confirmed exact: plan:608-610 quote matches character for character. spec:299-300 reads "large enough for any real review, small enough that one request cannot stall the page". AC-9 at spec:72-74 says "The diff is fetched each time its pane is opened and is not polled", and plan Task 13 behaviour 1 says "opening again fetches again". AC-18 at spec:124 caps only `patch`.

One citation is slightly off: ThreadingHTTPServer is at ui.py:247, not ui.py:243. The gap does not mention that the same line binds to 127.0.0.1 only, which materially limits the exposure it describes.

_Severity corrected to:_ minor

---

### gap-projects-dir-slash — important

**Claim.** Task 8 drops the trailing slash AC-16 puts in the container-to-host path swap, and states it as a whole-string replace rather than a prefix swap. Written as the plan says, every diff on this machine returns 'repository not found: /home/kevin/projectsdeye'.

**Touches:** AC-16, task-8, fn:resolve_repo, file:.env  
**Found by:** diff

**Evidence offered**

```
AC-16 (spec:116-118): 'with `/projects/` replaced by `<LOOPGRAPH_PROJECTS_DIR>/`'. plan:606: 'with `/projects/` replaced by `projects_dir`'. The live value has no trailing slash - .env:12 `LOOPGRAPH_PROJECTS_DIR=/home/kevin/projects` - and neither does the shipped default (.env.example:23 `/home/you/projects`) nor install.sh:87, which takes the prompt answer with no normalisation. The gitdir lines on disk are `gitdir: /projects/deye/.git/worktrees/ad4abd`, so the plan's rule concatenates to /home/kevin/projectsdeye. AC-16's version survives a trailing slash too (Path collapses the double slash), so the spec text is the correct one. Separately, 'replaced' with no 'first occurrence' or 'prefix' qualifier is str.replace, which would also rewrite a nested `/projects/` segment inside a repo path; neither doc states that the swap is anchored at the start.
```

**Verdict 1 (facts) — stands**

Every citation checks out, and the plan's rule is broken against the plan's own test fixture.

The two documents genuinely disagree. Spec AC-16 (line 117-118) says the repository is the part before `/.git/worktrees/` "with `/projects/` replaced by `<LOOPGRAPH_PROJECTS_DIR>/`" — trailing slash inside the replacement. Plan line 606 says the same swap "with `/projects/` replaced by `projects_dir`" — no trailing slash, and `projects_dir` is the raw string parameter from the signature at plan line 578 (`projects_dir: str | None`), not a Path.

The live value carries no trailing slash, and nothing in the install path adds one: .env line 12 is `LOOPGRAPH_PROJECTS_DIR=/home/kevin/projects`, .env.example line 23 is `/home/you/projects`, and install.sh line 87 takes the answer straight from `ask` with only tilde expansion on line 88 (`PROJECTS="${PROJECTS/#\~/$HOME}"`) — no slash normalisation, and `ask` (lines 18-23) just echoes the default or the typed answer.

The pointer files on disk have exactly the shape both documents describe: `runs/2026-09-05-deye-pending-restore/worktrees/ad4abd/.git` reads `gitdir: /projects/deye/.git/worktrees/ad4abd`. I ran both rules against that string. The plan's rule yields `/home/kevin/projectsdeye`; the spec's yields `/home/kevin/projects/deye`, which exists. So an implementer following plan line 606 literally hits reason 7, whose verbatim copy at plan line 596 is `repository not found: <path>` — producing exactly the message the gap predicts. Every diff on this machine would be a 200 with an empty patch.

The plan also contradicts itself. Its own test fixture (lines 626-627) writes `gitdir: /projects/proj/.git/worktrees/ab12cd` with `LOOPGRAPH_PROJECTS_DIR=<tmp_path>/projects`, so the repo sits at `<tmp_path>/projects/proj`, and the plan's rule resolves to `<tmp_path>/projectsproj`. The first named test, `test_diff_names_the_changed_file_and_carries_the_patch`, cannot pass under the rule the plan states.

The gap's own tie-breaker is right too: AC-16's version is robust to a trailing slash in the env value — `/home/kevin/projects/` gives `/home/kevin/projects//deye`, which `pathlib.Path` collapses to `/home/kevin/projects/deye` (verified, and `is_dir()` is True). The spec text is the one to keep.

Nothing already handles this elsewhere. `resolve_repo` does not exist yet — grep for it across tracked .py files returns nothing, so this is unwritten work and the plan text is the only instruction an implementer has. That is a plan defect, not a "there is no code for this" gap.

Severity stays important, not critical. The defect kills the whole diff feature as written, but the plan's own first test catches it on the first run, so it costs a cycle rather than shipping broken.

The secondary point about `replace` being unanchored is true as stated — neither document says "prefix" or "first occurrence", so a nested `/projects/` segment inside a repo path would also be rewritten — but that exposure is identical in both documents, so it is a note on the spec too, not a plan-vs-spec divergence. It does not change the verdict.

_Opened:_ Opened: docs/superpowers/specs/2026-09-05-ui-state-first-design.md lines 100-130 with line numbers (AC-15 through AC-19; AC-16 at 113-118, the cited phrase on 117-118). docs/superpowers/plans/2026-09-05-ui-state-first.md lines 561-640 (Task 8 header, Delivers, Files, Interfaces with the `resolve_repo` signature at 578, the reason-copy block including `repository not found: <path>` at 596, Behaviour 1-5 with the cited rule at 606, Constraints, Test intents and fixture at 626-627, Verify, Commit), plus a grep of the whole plan for `projects_dir|PROJECTS_DIR|/projects/` and for "Task 8". .env (grep: line 12 `LOOPGRAPH_PROJECTS_DIR=/home/kevin/projects`). .env.example (grep: line 23 `LOOPGRAPH_PROJECTS_DIR=/home/you/projects`). install.sh lines 70-100 (the projects prompt at 87 and tilde expansion at 88), the `ask()` function at lines 18-23, and the .env writer at lines 210-220. On-disk pointer files: catted five `runs/*/worktrees/*/.git` files, all of the form `gitdir: /projects/<repo>/.git/worktrees/<token>` (e.g. `/projects/deye/.git/worktrees/ad4abd`). Grepped all tracked .py files for `resolve_repo` and `/projects/` — no hits; ui.py exists but has neither, so the function is unwritten. Ran two python3 one-liners: applying each rule to the real deye gitdir string (plan rule -> `/home/kevin/projectsdeye`, spec rule -> `/home/kevin/projects/deye`), and confirming `Path('/home/kevin/projects//deye')` collapses to `/home/kevin/projects/deye` and `is_dir()` is True.

_Severity corrected to:_ important

---

### gap-readonly-and-request-params-untested — minor

**Claim.** The two guarantees Task 8 is trusted on - the target repository is never written, and branch names never come from the request - have no behavioural test intent. The only AC-37 check is a grep over ui.py's own source.

**Touches:** AC-15, AC-37, decision:read-only, task-8, file:ui.py  
**Found by:** diff

**Evidence offered**

```
AC-15 (spec:108-110) says base_branch and branch come from the ledger 'never from the request'; AC-37 (spec:220-222) says no endpoint runs a git command other than rev-parse and diff. The six test intents at plan:629-635 cover the happy path, missing Temporal, reasons 2-8, the cap, and bad ids; `test_the_dashboard_has_no_write_methods` asserts only that ui.py 'defines do_GET and no other do_ method' and that 'the only git subcommands in its source are rev-parse and diff'. Nothing sends `/api/diff?id=<wf>&branch=main&base=x` and asserts the extra parameters are ignored, and nothing asserts the fixture repository's HEAD, refs, index and worktree are unchanged after a request. Both are silent-regression shapes: a later task adding a `?branch=` convenience, or a helper that reaches for `git status` or `git worktree`, passes the source grep. AGENTS.md:33-38 makes the read-only boundary load-bearing, and the engine is holding owner cards in /home/kevin/projects/deye and /home/kevin/projects/macwebsite right now.
```

**Verdict 1 (facts) — stands**

Every citation resolves exactly, and nothing elsewhere in the plan closes the hole.

What checks out:
- Spec 106-110 (AC-15) does say the diff runs "read-only in the host's copy of the target repository" and that "base_branch and branch come from the last entry of the ledger's rounds, never from the request".
- Spec 220-222 (AC-37) does say no endpoint writes a file, signals a workflow, or runs a git command other than rev-parse and diff.
- Plan 629-635 lists exactly the six test intents named, and test_the_dashboard_has_no_write_methods is quoted verbatim: it checks that ui.py "defines do_GET and no other do_ method" and that "the only git subcommands in its source are rev-parse and diff". That is a grep over ui.py's own source, nothing more.
- No test intent anywhere sends extra query parameters to /api/diff, and none inspects the fixture repository's state after a request.

Where I looked for it being already handled and did not find it: the plan's constraints preamble (48-49: "ui.py stays read-only (AC-37)" - a constraint, not a check), Task 8 (566, delivers AC-15/16/17/18/37), Task 12 (869, only mentions AC-15's "one diff per run" shape), Task 13 (891, delivers AC-15/AC-37), and the existing tests/test_ui.py. Task 13's browser checklist item 4 does add a check - "no request with a method other than GET" in the network panel - but that watches what the page's own JavaScript sends, not what the server would do if something else sent it, and it says nothing about the repository being written or about request parameters.

So the two specific holes are real: (1) AC-15's "never from the request" has zero test intent of any kind, and (2) AC-37's server-side half rests on a source grep plus a client-side browser observation.

Why minor rather than important:
- AC-37 is not untested, it is weakly tested. The grep plus the browser network check plus the explicit plan constraint at 620 ("Only rev-parse and diff run. No worktree, checkout, fetch or status") are three separate statements of the boundary. The gap's own regression story - a later task adding a ?branch= convenience, or a helper reaching for git status - lives outside this plan: none of Tasks 9-13 touch the /api/diff handler, they only edit the PAGE JavaScript. Nothing inside this branch can break the guarantee without a task explicitly saying so.
- The proposed remedy's second half is partly unsound as written. Asserting the fixture repo's "index and worktree are unchanged" invites a flaky test, because git can rewrite .git/index refreshing cached stat data without any semantic change. A HEAD/refs assertion would hold; an index-mtime one may not.
- The fix is one line appended to an existing test intent (call /api/diff with &branch=/&base= set to a real second branch and assert the patch is still the ledger's), which is cheap but is a strengthening of the intent list rather than a hole that lets the build ship wrong.

_Opened:_ Opened and read: /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md lines 95-130 (AC-14 through AC-19, with AC-15 numbered at 106-110), 215-235 (AC-35, AC-36, AC-37 at 220-222, and the Non-goals list), and 245-265 (the Decisions section, including "Read-only stays read-only" at 252). Grepped the spec for every read-only mention (lines 1, 108, 205, 220, 226, 233, 252, 298).

/home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md: lines 35-70 (the constraints preamble, including 48-49 "ui.py stays read-only (AC-37)"), 563-640 (all of Task 8: contract, the nine reason strings, the five behaviour steps, the four constraints at 617-623, the fixture at 625-628, the six test intents numbered 629-635, verify and commit), and 860-925 (end of Task 12 and all of Task 13, including its browser checklist item 4). Grepped the plan for AC-37 and AC-15 (hits at 48, 566, 869, 891) and for read-only / never-from-the-request / bad_param / query-parsing wording.

/home/kevin/projects/loopgraph/AGENTS.md lines 28-45 with line numbers: line 28 is "ui.py - read-only dashboard on port 8400"; 33-38 is the "Answers arrive as signals, never by polling" rule and its four bugs, which the spec's Decisions section at 252-253 cites as the reason the dashboard stays read-only. The gap's citation of 33-38 is the right rule, though line 28 is the more direct anchor.

/home/kevin/projects/loopgraph/tests/test_ui.py in full (69 lines): five tests today - test_page_serves, test_logs_endpoint, test_logs_reject_traversal, test_runs_endpoint_without_temporal, test_the_injected_pattern_survives_javascript. None touches methods, git, or request parameters. Grepped all of tests/ for do_POST, rev-parse, read-only, no_write: the only rev-parse hits are unrelated fixtures in test_queue.py:135 and test_review_fixes.py:796.

/home/kevin/projects/loopgraph/ui.py grepped for do_, parse_qs, subprocess, git: today it has one do_GET (line 220) and one parse_qs (line 230), no git call at all - Task 8 is what adds the git calls.

_Severity corrected to:_ minor

---

### gap-cap-bytes-vs-characters — minor

**Claim.** AC-18 caps the patch in bytes; the plan's test intent measures characters, so the cap it asserts is not the cap the spec states, and neither document says whether the cut lands on bytes or on decoded text.

**Touches:** AC-18, task-8, fn:branch_diff  
**Found by:** diff

**Evidence offered**

```
AC-18 (spec:124-125): 'capped at 200 KB (204,800 bytes)'. plan:610: 'Cut `patch` at `DIFF_CAP` bytes'. plan:632 asserts `len(patch) <= 204800` on the JSON string, which counts characters, and its fixture is 'a 300 KB file' with no stated encoding, so an all-ASCII fixture makes the two units agree and hides the difference. Any diff containing non-ASCII - which activities/execute_round.py:73-85 documents as routine enough that the porcelain parser was rewritten around it - makes a 204,800-character patch larger than 204,800 bytes. Cutting the raw bytes instead can split a UTF-8 sequence at the boundary, which decodes to a replacement character or raises depending on the decoder; nothing states which side of the decode the cut happens on.
```

**Verdict 1 (facts) — stands**

Every quotation is exact and nothing in either document closes the question.

What is true, checked line by line:
1. Spec line 124 says the patch is "capped at 200 KB (204,800 bytes)" — the unit is bytes.
2. Plan line 610 repeats "Cut `patch` at `DIFF_CAP` bytes", but the thing being cut is a str: plan line 579 declares `branch_diff(...) -> dict  # {"stat", "patch", "truncated"}` and line 583 sends `patch` as a JSON string. So "cut a string at N bytes" is under-specified on its face.
3. Plan line 632 asserts `len(patch) <= 204800`, which on a Python str counts characters, and the fixture is only "a 300 KB file" with no content stated. An all-ASCII fixture makes the two units identical, so the test passes whichever way the implementer cuts.
4. Grepping both documents for decode/utf-8/errors/bytes turns up nothing on this path. The spec mentions bytes exactly once, at AC-18. So neither document says which side of the decode the cut lands on.

The asymmetry inside the plan is what makes this more than pedantry: for the sibling endpoint the plan does state the rule, at line 454 — "`text` is bytes `[offset:size]` decoded with `errors='replace'`". The author wrote the byte-slice-then-decode contract explicitly for `/api/log` and left it out of Task 8, which is the same question about the same kind of data in the same file.

Non-ASCII really is routine here. execute_round.py:76-78 documents git C-quoting "caf\303\251.txt" as something that killed a checkpoint, and audit.py:223-225 sets `core.quotePath=false` so a non-ASCII filename reaches the auditor as itself. Diff bodies carry raw file content, so a patch with multi-byte characters is ordinary, not exotic.

Two ways to be wrong, and how bad each is:
- Cut characters (natural if the implementer uses `subprocess.run(..., text=True)`): the reply can reach roughly 4x the stated cap on a heavily non-ASCII diff. The cap exists so "one request cannot stall the page" (spec:299), so this weakens the guarantee without breaking anything.
- Cut raw bytes: the boundary can land mid-UTF-8-sequence. With `errors="replace"` that is one replacement character, harmless. With a strict decode it raises, and plan line 612 turns any exception into reason 9, so a large non-ASCII diff silently degrades to "diff failed" and the reviewer sees no patch at all.

Why minor and not important: the test intent is weak rather than wrong. A correct byte-cut always satisfies `len(patch) <= 204800`, since characters never exceed bytes in UTF-8, so the assertion never fails a correct implementation — it just fails to catch a character-based one. And ui.py already carries the safe convention in the same file (line 127-128: `f.read_bytes()[-LOG_TAIL:]` then `.decode(errors="replace")`), so an implementer reading around is likely to land on the right answer by imitation. That is a convention, not a stated contract, and it does not cover subprocess output, so the ambiguity is real — but the worst outcome is a degraded diff pane, not corrupt data or a broken run. The hunter's own severity is correct.

_Opened:_ Opened and read in full:
- /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md lines 100-140 (AC-15 through AC-19, incl. AC-18 verbatim at 124-125), 219-223 (AC-36/AC-37), 290-310 (the auto-resolved decision "The patch cap is 200 KB ... small enough that one request cannot stall the page").
- /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md lines 440-472 (Task 6 /api/log, incl. line 454 "bytes `[offset:size]` decoded with `errors='replace'`"), 545-660 (all of Task 8: interfaces at 577-583, behaviour at 605-612, test intents at 628-635), 890-925 (Task 13 diff pane, "patch cut at 200 KB").
- /home/kevin/projects/loopgraph/activities/execute_round.py lines 60-95 (parse_porcelain docstring at 73-85, the C-quoting note at 76-78).
- /home/kevin/projects/loopgraph/activities/audit.py lines 208-237 (diff_including_new_files, core.quotePath=false comment at 223-225) and lines 23, 142, 173, 247 (the existing DIFF_CAP = 12_000, capped in chars, for the auditor prompt — a different cap, not the one under test).
- /home/kevin/projects/loopgraph/ui.py lines 110-140 (log_tails: `f.read_bytes()[-LOG_TAIL:]` then `.decode(errors="replace")`), plus greps for subprocess/text=True/encoding across ui.py (no git call exists yet, only `_run` at line 149).

Greps run: DIFF_CAP / 204800 / "200 KB" / truncated across the repo excluding runs/; decode|utf-8|errors=|bytes across both documents. The spec mentions bytes exactly once, at AC-18; the plan mentions the decode rule only for /api/log at line 454, never for the diff.

_Severity corrected to:_ minor

---

### G2 — important

**Claim.** Task 12 keys a ledger round as `item_no-round` and states the fallback for a *log name* with no item group ("meaning item 1") but states no fallback for a *ledger round* with no `item_no`. Three live run directories are in exactly that state, so each of their rounds renders as two cards: one from the ledger with the verdict and no logs (header `item undefined · round 1`), one from the log names with the panes and no verdict — and, per Task 12 Behaviour 3, the second one carries ` · in progress` forever because its ledger entry "never arrives".

**Touches:** AC-8, task-12, artifact:ledger-microbits-fact-corrections, file:activities/stream.py, code:test_the_old_log_names_still_render  
**Found by:** page

**Evidence offered**

```
Live ledger `run-2026-09-05-microbits-fact-corrections-36437d`: its round's keys are `[attempts, base_branch, branch, claims, directive, files, round, status, verdict, verdict_reasons, worktree]` — no `item_no`. `runs/2026-09-05-microbits-fact-corrections/logs/`, `runs/2026-09-05-microbits-ideas-sharpen/logs/` and `runs/2026-09-04-toy-count/logs/` all hold old-shape `r1-executor.log` / `r1-audit.log`. `activities/stream.py:23` makes the item group optional on purpose and `tests/test_review_fixes.py:120` (`test_the_old_log_names_still_render`) pins that these runs must keep rendering. Plan Task 12 Behaviour 1.
```

**Verdict 1 (facts) — stands**

The claim reads the plan and the code correctly, and the live state it rests on is real.

What the plan actually says. Task 12 Behaviour 1 (plan line 851): "Keys: a ledger round is `item_no-round`; a log name is `i-r` from `LOG_RE`, with a missing item group meaning item `1`." The missing-value fallback is stated for the log-name side only. Nothing in the plan or the spec says what the key is when a ledger round has no `item_no` — `item_no` appears in the plan only at lines 148-212 (the unrelated `location_line` helper), 845 and 851, and in the spec only at AC-8 (line 66) and AC-35 (line 213). Task 7 (plan 486-541) hands the ledger to the page untouched, so nothing normalizes it server-side either.

The live state. I queried the running Temporal instance read-only (list plus a `ledger` query, falling back to `result()`, exactly what `ui.py` does). Of 15 workflows, two have a round with no `item_no` key: `run-2026-09-05-microbits-fact-corrections-36437d` and `run-2026-09-05-microbits-ideas-sharpen-794631`. Both are closed/completed. Every other run's rounds carry `item_no`. `git log -S'"item_no": item_no' -- workflows/run.py` dates the field to 3f49df6 (Sep 5 10:17); those two runs' logs were written at 01:13 and 04:05, before it.

The consequence follows. Both workflow ids map to run directories (`wf.id[4:wf.id.rfind("-")]`) that hold old-shape `r1-executor.log` / `r1-audit.log`, so a single selection feeds `patchRounds` a ledger round keying to `undefined-1` and two log names keying to `1-1`. Two cards: one with the verdict and no panes, headed `item undefined · round 1` (the copy at plan line 847 is `item <i> · round <r>`); one with the panes and, per Behaviour 3, a permanent ` · in progress` — the entry keyed `1-1` never arrives. A completed run reading "in progress" is actively wrong, not just untidy.

One correction to the evidence. The claim says three run directories are in this state. Only two are. `runs/2026-09-04-toy-count/` has no workflow in Temporal at all, so its ledger is null and Task 12 Behaviour 4 covers it (cards from names only) — its logs are old-shape as claimed, but it does not produce the doubled card.

Severity. Real and verifiable, and the fix belongs in the plan (a stated default, symmetric with the log-name rule), so it is not refuted. But the blast radius is the display of two closed historical runs. It does not touch the engine, any live run, any new run, or any data. Critical overstates it; important is right.

_Opened:_ Opened: /home/kevin/projects/loopgraph/activities/stream.py (whole file; confirmed line 22 `LOG_GLOB = "*.log"` and line 23 `LOG_RE = r"^(?:i(\d+)-)?r(\d+)-(executor|audit)\.log$"` — the item group is genuinely optional). /home/kevin/projects/loopgraph/tests/test_review_fixes.py lines 90-180 (confirmed `test_the_old_log_names_still_render` at line 120, asserting `re.match(LOG_RE, "r1-executor.log")`). /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md: Task 12 in full (lines 831-883), Task 11's poll description (779-825), Task 9 in full (644-704), Task 7 in full (486-541), plus a grep for `item_no` / `old|legacy|migrat|default` across the whole plan. /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md: AC-8 through AC-12 (lines 61-95), AC-15 through AC-19 (110-130), AC-33 through AC-37 (200-230), plus greps for `item_no`, `LOG_RE`, `in progress`. /home/kevin/projects/loopgraph/workflows/run.py lines 195-280 (the round entry at 229-243 sets `"item_no": item_no`). /home/kevin/projects/loopgraph/ui.py lines 158-200 (`_runs`, the `wf.id[4:wf.id.rfind("-")]` dir derivation, the ledger query/result fallback).

Commands run (all read-only): `ls runs/` and `ls runs/<slug>/logs/` for the three named runs (all three hold `r1-audit.log` and `r1-executor.log`, no `i`-prefixed names); `stat` on those logs (01:13, 04:05 on Sep 5; 10:00 on Sep 4); `git log -S'"item_no": item_no' -- workflows/run.py` (added in 3f49df6, Sep 5 10:17); and a Python script against `localhost:7233` listing all 15 workflows and printing, per round, whether `item_no` is present. Result: `run-2026-09-05-microbits-fact-corrections-36437d` → `[False]`, `run-2026-09-05-microbits-ideas-sharpen-794631` → `[False]`, every other run `True`, and `2026-09-04-toy-count` has no workflow at all. Nothing was written, committed, or restarted.

_Severity corrected to:_ important

---

### G7 — minor

**Claim.** Two tasks pin two different verbatim empty-state strings for the same container, and no task retires either. After Task 11 moves the round cards into `#rounds`, Task 9's `no logs yet for this run` and Task 12's `no rounds yet` both claim the empty case. The plan's own global constraint says copy is pinned and must not be reworded at implementation time, so the implementer has no basis to choose — and the null-ledger view (AC-10), where there are no rounds *and* possibly no logs, is exactly where both apply.

**Touches:** task-9, task-12, task-11, AC-10, file:ui.py  
**Found by:** page

**Evidence offered**

```
Plan global constraints: "User-facing copy is pinned in the tasks. Do not reword it at implementation time." Task 9 Behaviour 5: "`no logs yet for this run` (existing copy) when no name matches" — the string lives at `ui.py:115` today. Task 12 Copy: "`no rounds yet` when the ledger has no rounds and no log matches". Task 12's test intent `test_the_rounds_copy_is_pinned` asserts the new string is present; nothing asserts the old one is gone, so both can ship and the page can show either.
```

**Verdict 1 (facts) — stands**

The factual core checks out, but the severity argument rests on a misreading of AC-10, and the plan gives the implementer more of a basis to choose than the gap admits.

What is true. Every quote is accurate. The plan's global constraint at line 68 does say "User-facing copy is pinned in the tasks. Do not reword it at implementation time." Task 9 behaviour 5 (line 685) pins `no logs yet for this run`. Task 12's copy block (line 848) pins `no rounds yet` for the empty case. Task 11 (lines 793-794) does move the Task 9 log cards from `#board` into `#rounds`, and after Task 12 that container is the only home for round cards, so there is no second slot where the older string could still belong. No task says the older line goes. So yes: two pinned strings, one empty slot, no explicit retirement.

Why it is smaller than claimed. First, the AC-10 tie is wrong. AC-10 (spec lines 75-78) describes a run directory that has logs and no workflow — "the page still shows the log panes grouped by `LOG_RE`, exactly as a run directory with logs and no workflow is shown today". In that state cards exist, so neither empty string renders. AC-10 pins only the why-line copy (`temporal unreachable — logs only` / `no workflow for this run`), which Task 11 carries; it says nothing about the rounds empty state. The two strings collide only in a narrow state the gap never names: a selected run whose logs directory holds no matching names and whose ledger has no rounds — the first seconds of a run, or a bare run directory viewed with Temporal down.

Second, "the implementer has no basis to choose" overstates it. Task 12's Produces line (lines 844-845) says it writes `patchRounds(rounds, names)`, "extending Task 9's `patchRounds(names)`", and its copy block restates the full copy for the region it now owns: header, ` · in progress`, and the empty line. Task 12 also widens where cards come from (ledger rounds as well as log names), so Task 9's predicate "no name matches" no longer describes an empty container and has to be rewritten anyway. The later, more specific task governing the function it rewrites is the ordinary reading of a plan whose build order says each task sees the finished state of every task before it. The pinned-copy constraint forbids inventing different words, not choosing between two rules the plan itself gives.

Third, the blast radius is one line of text. Nothing in the spec pins `no logs yet for this run` as copy that must survive (I read all 37 ACs), no existing test asserts it — tests/test_ui.py:15 only mentions it inside a comment about a stale fixture — and both strings can sit in the page source without breaking `test_the_rounds_copy_is_pinned`, which only checks the new one is present. The gate stays green either way. The worst outcome is the wrong empty-state sentence in a rare view.

So it survives as a real loose end the plan should close with a half-sentence in Task 12 ("the Task 9 empty line goes"), but it is a copy tidy-up, not an important design hole. Minor.

One thing I noticed while checking, not part of this gap: `no rounds yet` is also pinned at plan line 590 as one of Task 8's `/api/diff` `stat` reasons, so the same literal string now lives on two surfaces. Task 12's test intent says "page contains", so it still binds to `page_html()` and is not weakened — worth knowing if that test is ever written against the whole module source instead.

_Opened:_ Opened: /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md — header and full Global constraints block (lines 1-73, including the pinned-copy rule at line 68 and the existing-tests rule at lines 57-59); Task 8 in full (lines 561-643, whose `stat` reason copy at line 590 is also `no rounds yet`); Task 9 in full (lines 644-714, behaviour 5 at line 685, the naming contract at lines 662-668); Task 10 opening (lines 715-763); Task 11 in full (lines 764-830, the `#rounds` move at lines 793-794, the five-line copy block at lines 787-793); Task 12 in full (lines 831-885, Produces at lines 844-845, copy block at lines 847-848, behaviour 4 at line 866, test intents at lines 871-873). /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md — AC-8, AC-9, AC-10 (lines 66-78), AC-15 to AC-19 (lines 105-134), AC-20 to AC-37 (lines 155-232), plus Non-goals and Decisions. /home/kevin/projects/loopgraph/ui.py — the `poll()` function and the board render, lines 85-125, with the live string at line 115: `</div></div>`).join('') : '<div class="empty">no logs yet for this run</div>';`. /home/kevin/projects/loopgraph/tests/test_ui.py — lines 1-40, the fixture and its comment at line 15; no test asserts either empty string. Grepped the whole tree for `no logs yet` and `no rounds yet` (hits only in ui.py:115, ui.py:203 (a docstring), lg:166 (an unrelated CLI message), tests/test_ui.py:15 (a comment), and the plan and spec lines above), and grepped the plan for supersession words (`replac`, `retire`, `supersede`, `no longer`, `removed`, `extend`) — the only Task 12-over-Task 9 language is line 844-845.

_Severity corrected to:_ minor

---

## Disputed — folded at important, both verdicts recorded (0)

## Unverified — NOT folded, for the human (0)

A refuter that never returned. Nothing here was folded; read it yourself.

## Refuted — recorded so the same ground is not re-hunted (21)

### gap-1 — critical

**Claim.** Task 7's TemporalFeed.ledger falls back to handle.result() with no RUNNING guard, on an endpoint the page polls every 2 seconds. The plan names this exact hazard three tasks earlier and guards against it for lg status, then drops the guard here.

**Touches:** AC-1, task-7, task-5, fn:TemporalFeed.ledger, endpoint:/api/run, file:ui.py, fn:TemporalFeed.call, task-11  
**Found by:** coverage, endpoints, page

**Evidence offered**

```
Plan Task 7 Behaviour 1: 'query ledger; on any failure try handle.result(); on failure return None' - no describe() guard, and no timeout bound is stated for ledger() (the call(timeout=10) bound is named only for _runs). Plan Task 5 Constraints: 'handle.result() blocks until the workflow ends. Without the describe() guard a running run whose ledger query failed would hang the terminal for as long as the run waits on its card.' Task 11 polls /api/run every 2000 ms. ui.py:158 TemporalFeed.call abandons the future after 10 s but never cancels the coroutine, so on a worker outage (Temporal up, worker down - the exact case AC-1 is written for) every poll blocks a request thread and leaks a pending long-poll RPC on the single feed loop thread, forever. If the implementer omits call()'s timeout, the browser request hangs outright.
---
Task 7 behaviour 1: 'query ledger; on any failure try handle.result(); on failure return None' (plan l.522). AC-1 distinguishes the two sources by run state: 'from the ledger query for a running workflow and from the workflow's result for a closed one' (spec l.31-32). Task 5 states the guard explicitly for the same fallback in cmd_status: 'handle.result() blocks until the workflow ends. Without the describe() guard a running run whose ledger query failed would hang the terminal for as long as the run waits on its card' (plan l.385-387), and its behaviour 4 requires describe().status != RUNNING first (plan l.378-379). The dashboard consequence is worse than the CLI's, because Task 11 polls /api/run every 2 seconds: call(timeout=10) returns to the HTTP handler but does not cancel the submitted coroutine, so each failed poll leaves a pending handle.result() long-poll on the single feed loop thread, accumulating ~30/minute for as long as a run waits on its card. runs/2026-09-05-deye-pending-restore and the other live directories are exactly those runs. State the same describe() guard in Task 7 that Task 5 already states.
---
`ui.py:158-159` `asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)` — `concurrent.futures.Future.result(timeout)` raises and leaves the task running. `ui.py:167-174` is the existing unguarded query→result fallback Task 7 Behaviour 1 extracts verbatim. Plan Task 5 Constraints: "`handle.result()` blocks until the workflow ends. Without the `describe()` guard a running run whose ledger query failed would hang the terminal." Both branches are live: on 2026-09-05 `run-2026-09-05-deye-pending-restore-ad4abd` answered the `ledger` query while closed, and `run-2026-09-05-microbits-fact-corrections-36437d` needed the `result()` fallback.
```

**Verdict 1 (facts) — refuted**

The claim's load-bearing sentence misreads the plan. Task 7's own Constraints (plan l.540) say: "Keep the 25-row cap in `_runs` and the `call(timeout=10)` bound; a poll must not hang the page." The hunter asserts "no timeout bound is stated for ledger() (the call(timeout=10) bound is named only for _runs)". That is wrong twice over: the 25-row cap is what is scoped to `_runs`; the `call(timeout=10)` bound is a property of `TemporalFeed.call` (ui.py:158), the single door every sync accessor goes through ("sync callers use call()", ui.py:136), and the produced contract makes `ledger(self, wf_id) -> dict | None` a sync method exactly like `runs()`, so it can only reach the loop thread through `call`. The constraint also states the very consequence the gap says is unaddressed, in those words: "a poll must not hang the page." So "if the implementer omits call()'s timeout, the browser request hangs outright" is refuted by the sentence the implementer is required to honour.

The claimed asymmetry with Task 5 also dissolves once you open the CLI. `cmd_status` (lg:84-95) awaits `handle.query(q)` directly on the client with no bound of any kind — no `call()`, no timeout — so Task 5's `describe()` guard is the only thing that can stop `handle.result()` blocking the terminal. That is precisely why Task 5 states it and Task 7 does not: the two call sites have different mechanisms for the same requirement, and Task 7 states its own.

AC-1 is quoted accurately but read selectively. The full sentence is "The ledger comes from the `ledger` query for a running workflow and from the workflow's result for a closed one, the same fallback `TemporalFeed._runs` uses today" (spec l.30-32). It pins the endpoint to today's try/except at ui.py:166-174, not to a state check. Task 7 Behaviour 1 ("query `ledger`; on any failure try `handle.result()`; on failure return `None`. This is the fallback `_runs` has today, extracted...", plan l.522-524) matches that anchor exactly. Requiring a `describe()` branch would add behaviour the spec does not ask for, not restore behaviour the plan dropped.

The one true residue: `concurrent.futures.Future.result(timeout)` does raise and leave the submitted coroutine running (ui.py:158-159 is quoted correctly). But that is pre-existing behaviour of `_runs`, which the run-list poll already drives every 4000 ms (plan l.60, l.659) with the same query→result fallback for up to 25 workflows per poll — the plan neither introduces nor changes it. And the hunter's specific mechanism is wrong: in the worker-down case AC-1 is written for, the `ledger` query is what hangs, so the 10 s bound fires inside the query and `handle.result()` is never reached, meaning the thing claimed to accumulate ~30/minute is not the thing that would accumulate. Task 11 also defines the graceful outcome of a null ledger with Temporal up ("no workflow for this run", plan l.789-793), so a bounded null is a handled state, not a broken page. If anything survives here it is a minor note about cancelling abandoned coroutines in `call()`, which is out of this plan's scope, not a critical missing guard.

_Opened:_ Opened: /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md — Task 7 in full (l.486-560: header, Delivers AC-1/AC-3, the produced `TemporalFeed`/`run_entry`/`make_server` contract, endpoint shapes, Behaviour 1-5, all four Constraints incl. l.540, Test intents), Task 5 (l.350-400: Behaviour 4 at l.377-379 and the Constraint at l.386-387), Task 11 (l.764-800), the global constraints block (l.54-71, incl. l.60 poll intervals), and grep for every `describe`/`RUNNING`/`result()` mention in the plan (hits only at l.377, 378, 386, 395, 522). /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md — AC-1 through AC-3 (l.20-45). /home/kevin/projects/loopgraph/ui.py — l.130-200: `TemporalFeed.__init__`, `_run`, `call` at l.158-159, `_runs` with the query→result fallback at l.166-174, `runs()` at l.183-189. /home/kevin/projects/loopgraph/lg — `cmd_status` at l.84-95, confirming it awaits with no timeout wrapper at all.

Key texts as they actually read — plan l.540: "Keep the 25-row cap in `_runs` and the `call(timeout=10)` bound; a poll must not hang the page." ui.py:136: "Persistent temporalio client on its own loop thread; sync callers use call()." ui.py:158-159: `def call(self, coro, timeout=10): return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)`. lg:88-94: `for q in queries: try: print(json.dumps(await handle.query(q), indent=2))` — no bound, which is what Task 5's guard exists for.

_Severity corrected to:_ minor

---

### gap-2 — critical

**Claim.** Task 9 cannot pass its own Verify. Its test test_innerhtml_lives_only_in_build_functions asserts that runs() contains no innerHTML, but Task 9 does not touch runs() - Task 10 does. The gate stays red for the whole of Task 9.

**Touches:** AC-14, task-9, task-10, fn:runs, fn:poll, file:ui.py, contract:test_innerhtml_lives_only_in_build_functions  
**Found by:** coverage, page

**Evidence offered**

```
Plan Task 9 test intent: 'for every innerHTML in ui.page_html(), the nearest preceding function <name>( has a name starting with build; the text of runs, poll and every patch function contains none.' Task 9 Files: 'Modify: ui.py (PAGE: the <script> and the styles it needs)' with a behaviour list that rewrites poll() only. ui.py:82 has `el.innerHTML = '';` and ui.py:86 has `div.innerHTML = ...`, both inside `async function runs()` (ui.py:79). Task 9 Verify: '.venv/bin/python -m pytest -q tests/test_ui.py && .venv/bin/python -m pytest -q ... Expected: green.' Task 10's own test intent ('still green with runs() rewritten') shows the author assumed it was already green. Either Task 9 must also rewrite runs(), or the assertion has to be scoped to poll() until Task 10 lands.
---
`ui.py:82` (`el.innerHTML = '';`) and `ui.py:86` (`div.innerHTML = \`<div class="dir">...\``), both inside `async function runs()`. Plan Task 9 Constraints: "`sel` stays a directory string in this task; Task 10 turns it into `{id, dir}`". Plan Task 9 Test intents: "the text of `runs`, `poll` and every `patch` function contains none". Plan Task 10 Test intents: "`test_innerhtml_lives_only_in_build_functions` (Task 9) still green with `runs()` rewritten" — the plan believes it was already green one task earlier. Either the test must be scoped to `poll`/`patch*` at Task 9 and widened at Task 10, or the `runs()` rewrite moves into Task 9 and Task 10 loses its point.
```

**Verdict 1 (facts) — refuted**

The code citations are right but the plan reading is wrong, and the plan already puts runs() inside Task 9's scope.

What the hunter got right: ui.py:82 (`el.innerHTML = '';`) and ui.py:86 (`div.innerHTML = ...`) do sit inside `async function runs()` (ui.py:79), and Task 9's test intent does name `runs`.

What the hunter got wrong. The claim rests on "Task 9 does not touch runs() - Task 10 does". Four separate lines of Task 9 say otherwise, and the hunter quoted none of them:

1. Files (plan:654): "Modify: `ui.py` (`PAGE`: the `<script>` and the styles it needs)". That is the whole script, not poll(). The hunter's paraphrase "a behaviour list that rewrites poll() only" swaps the Files line for the Behaviour list.
2. Interfaces / Produces (plan:658-665): Task 9 states the page-wide naming contract - "A function that assigns `innerHTML` is declared with the `function` keyword and its name starts with `build`" - and names runs() directly: "`runs()` and `poll()` stay the two interval functions". The contract Task 9 says it *produces* is the very rule the test checks, and it covers the whole page, not poll().
3. Behaviour 6 (plan:685-687): "Selecting a run clears the board (`replaceChildren()` on `#board`, in the click handler, not in `poll()`)". The click handler is ui.py:88, inside runs(). So Task 9's own behaviour list edits runs().
4. Constraints (plan:690): "`sel` stays a directory string in this task" - a constraint on runs()' selection code, which only makes sense if runs() is being edited.

So the test intent naming `runs` is not an accident the implementer trips over; it is the instruction that runs() must be innerHTML-free by the end of Task 9. Satisfying it is small and does not steal Task 10's work: swap `el.innerHTML = ''` for `replaceChildren()` and move the row markup at ui.py:86-87 into a `build…` function, exactly as the produced contract allows ("A function that assigns innerHTML ... its name starts with build"). Task 10 then does the actual patch-in-place rewrite - rows keyed by `data-id`, `patchRuns`, start/close times, `sel` becoming `{id, dir}` - which is a different job entirely.

The hunter's strongest evidence, Task 10's "still green with `runs()` rewritten" (plan:753), reads the other way once the above is in view. "Still green" is the natural phrasing for a test that was already green and whose subject is now being rewritten; it is not proof the author forgot. The same phrase appears verbatim at Tasks 11, 12 and 13 (plan:818, 874, 916), where runs() is not being rewritten at all - the plan uses it as boilerplate for "this earlier test must not regress", not as a claim about who cleaned what.

Spec AC-14 (spec:96-102) also survives the split cleanly: it requires that the interval functions never assign innerHTML to an existing element, and the two tasks divide the browser-observable half of it (Task 9 "AC-14 (log panes)", Task 10 "AC-14 (run list)"). The static source assertion is one test that Task 9 introduces and everything after keeps green - which is how the plan uses it.

Residual nit, well below the claimed severity: the requirement to clean runs()' innerHTML lives in Task 9's Files, Produces and test intent, but not in its numbered Behaviour list. An implementer who reads only the Behaviour list could write the test, watch it go red, and be briefly confused. That is a one-line clarity improvement, not "the gate stays red for the whole of Task 9" - the fix is in the task's own scope and the plan states the requirement twice outside the behaviour list. Nowhere close to critical, and it is not the gap as filed, whose load-bearing claim ("Task 9 does not touch runs()") is contradicted by the document.

_Opened:_ Opened in full: /home/kevin/projects/loopgraph/ui.py lines 60-130 (the whole PAGE `<script>`, confirming `async function runs()` at :79, `el.innerHTML = ''` at :82, `div.innerHTML = ...` at :86-87, the click handler at :88, `async function poll()` at :95, `board.innerHTML` at :109, and the `setInterval` line at :117). Grepped every `innerHTML` in ui.py (3 hits: :82, :86, :109).

/home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md - preamble and Global constraints (lines 1-75), Task 9 in full (lines 644-714: Delivers, Files, Interfaces, all six Behaviour items, all four Constraints, all three Test intents, Verify, Commit), Task 10 in full (lines 715-763), and the "still green" lines for Tasks 11/12/13 (818, 874, 916).

/home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md - AC-14 (lines 96-102), plus AC-3, AC-9, AC-11, AC-12, AC-13 for context.

/home/kevin/projects/loopgraph/tests/test_ui.py - listed its 5 existing tests; grepped the whole tests/ tree for `innerHTML`/`innerhtml` (zero hits), confirming `test_innerhtml_lives_only_in_build_functions` is new in Task 9 and is not an existing gate the branch would break.

---

### gap-7 — critical

**Claim.** Task 3's instruction for the parked card, read literally, duplicates a line the card already has: the existing text already begins with exactly the location line.

**Touches:** AC-20, task-3, fn:_park_note, fn:location_line, file:workflows/run.py, task-2, AC-21, AC-33, decision:no-worker-restart, file:docker-compose.yml, file:worker.py  
**Found by:** coverage, purefns, workflow

**Evidence offered**

```
Plan Task 3 Behaviour 3: "_park_note's text starts location_line(item_no, total) + ' parked', then today's text." workflows/run.py:433 already reads `text = (f"item {item_no} of {total} parked\n\n{item[:600]}\n\n" ...)`. Following the instruction as written produces `item 2 of 3 parkeditem 2 of 3 parked` on a card the owner reads on their phone. The intent is plainly 'replace the hand-written first line with the helper', but this is pinned user-facing copy and the Global constraints say not to reword it at implementation time, so the ambiguity belongs in the plan, not in the implementer's judgement.
---
docker-compose.yml:59-64 (worker mounts ./:/app, restart: unless-stopped); Dockerfile:9 copies only pyproject.toml; worker.py:17-18 imports activities.notify at startup; worker.py:45-53 passes no workflow_runner, so the default SandboxedWorkflowRunner applies (verified: passthrough_all_modules=False, passthrough list is stdlib + temporalio only); .venv/.../workflow_sandbox/_importer.py:61-67 seeds each instance's sys.modules with only sys/builtins/__main__, and _runner.py:111-145 builds a new Importer plus `from workflows.run import LoopGraphRun` per workflow instance; _importer.py:329-338 does passthrough via importlib.import_module OUTSIDE the sandbox, returning the host's cached module. A worker is polling the loopgraph queue right now (DescribeTaskQueue: 1 poller, identity 1@omarchyos). Task 3's own Verify (plan:239-256) runs Replayer in the host .venv where both modules load fresh, so it cannot detect this.
---
plan:213 vs workflows/run.py:433 `text = (f"item {item_no} of {total} parked\n\n{item[:600]}\n\n"`. The other three call sites (plan:208, 214-216) prepend to text that has no such prefix, so only this one collides. Task 3 has no test intent over card text, only source pins (plan:230-237), so a duplicated line reaches the owner's phone unchecked.
---
plan:214 says "_park_note's text starts location_line(item_no, total) + ' parked', then today's text". Today's text (workflows/run.py:433) IS f"item {item_no} of {total} parked\n\n{item[:600]}..." - byte-identical to location_line(item_no, total) + ' parked'. Following the sentence as written yields 'item 2 of 3 parked\n\nitem 2 of 3 parked\n\n<item>...'. The plan pins user-facing copy elsewhere and says at plan:62 'Do not reword it at implementation time', but here the pinned copy and the real file collide and the plan never says the existing first line is being replaced rather than prefixed. AC-20 only requires the first line to 'begin with' the location line (spec:143), so both readings pass the AC and the suite.
```

**Verdict 1 (facts) — refuted**

The quotes are accurate, but the conclusion does not follow, and the argument that makes it "critical" rests on a misreading of the plan's own constraint.

What is true: plan:211 reads "_park_note's text starts location_line(item_no, total) + \" parked\", then today's text." Task 2 (plan:161-163) defines location_line without a round as f"item {item_no} of {total}". run.py:433 does begin f"item {item_no} of {total} parked\n\n". So the helper output and the first line of today's text are the same string. That much checks out.

Why it does not stand:

1. The plan writes behaviour 3 differently from its neighbours on purpose, and the hunter compared them without noticing the difference. Behaviours 2, 4 and 5 each name a separator, because each puts new content in front of text that had none: 2 is `location_line(...) + "\n\n" + question`, 4 is "a blank line, then today's lines", 5 is `location_line(...) + "\n\n" + build_merge_summary(...)`. Behaviour 3 names no separator at all. On a card whose every block is separated by \n\n, a prefix with no separator is not a coherent instruction; the sentence describes how the first line is built, not something glued in front of the whole string. The hunter's own literal reading produces "parkeditem", which is the tell that the literal reading is not the reading.

2. The load-bearing part of the claim, that plan:62 traps the implementer, is wrong. plan:62 says "User-facing copy is pinned in the tasks. Do not reword it at implementation time." Task 3 has no "Copy, verbatim" block; those blocks appear only at plan:151 (Task 2), 731, 784, 847 and 901. The parked note's copy is not pinned in Task 3. And the constraint cuts the opposite way from the claim: the replace reading leaves the owner's card byte-identical to today, while the duplicate reading changes user-facing copy, which is exactly what plan:62 forbids.

3. The plan header (plan:4-6) tells the implementer "Read the file you are modifying before writing anything; the plan tells you what must be true, the codebase tells you how to say it." Reading run.py:433, which the task requires, puts the overlap in the same glance.

Supporting details that are correct but do not rescue the claim: Task 3's test intents (plan:230-237) are source pins only, and no existing test asserts the parked card's text (grep over tests/ finds "parked" only in test_queue.py's merge-summary tests and unrelated comments). So nothing would catch a doubled line, but nothing plausibly produces one either.

Two more things weaken the submission. The line citations drift: it cites plan:213 and plan:214 for a sentence that sits at plan:211. And roughly a third of the offered evidence (docker-compose.yml:59-64, worker.py:17-18, the workflow_sandbox importer walkthrough, the live poller) is about whether adding the location_line import needs a worker restart. That is a separate question and contributes nothing to the duplicated-copy claim it is filed under.

Severity if someone still wanted it logged: minor. "Rebuilt from the helper, output unchanged" would read a shade tighter than "then today's text", but that is wording polish, not a defect an implementer walks into.

_Opened:_ Opened: plan lines 1-80 (header and Global constraints, including plan:62), plan 150-260 (all of Task 2 and Task 3: contract, behaviours 1-5 at 204-216, constraints, test intents 230-237, the Replayer verify block); spec lines 128-160 (AC-20, AC-21, AC-22) and 195-215 (AC-32 through AC-35); workflows/run.py 415-450 (_ask_owner, _park_note with its text at line 433, _stopped_note). Ran grep for "Copy, verbatim" across the plan (hits at 151, 731, 784, 847, 901, none inside Task 3) and grep for "parked" across tests/*.py (no test asserts the parked card's text).

_Severity corrected to:_ minor

---

### gap-8 — minor

**Claim.** Task 8's grounding claim about worktree pointers is wrong: most run directories have no pointer at all, so the diff feature is far less available than the plan implies.

**Touches:** AC-16, AC-17, task-8, fn:resolve_repo, decision:diff-via-worktree-pointer  
**Found by:** coverage

**Evidence offered**

```
Plan Task 8 Constraints: 'Every live run's pointer read gitdir: /projects/<repo>/.git/worktrees/<token> on 2026-09-05.' On disk today only 5 of the 16 directories under runs/ have a worktrees/*/.git file, and runs/2026-09-05-microbits-order-v2/worktrees/983d87/.git is absent because that run was discarded (activities/checkpoint.py:144-167 removes the worktree and deletes the branch). Checked against the five most recent real runs: one gives a real diff (deye, 13 files changed), one is missing its pointer, and two fail `git rev-parse --verify <branch>` with 'Needed a single revision' because the branch is gone. Three of five land on a reason line rather than a diff. The behaviour is correct (AC-17 covers reasons 4 and 8); the claim that motivates the design is not, and it sets the wrong expectation for whoever verifies Task 8 against real runs.
```

**Verdict 1 (facts) — refuted**

The gap quotes half a sentence and drops the half that already answers it.

The plan's Task 8 constraint is two sentences, not one: "Every live run's pointer read `gitdir: /projects/<repo>/.git/worktrees/<token>` on 2026-09-05. **A worktree that `discard` removed has no pointer, which is reason 4.**" The hunter quotes only the first and presents the second as its discovery. The plan already names the missing-pointer case, already names its cause (discard), and already routes it to reason 4.

The first sentence is also true as written. It is a claim about the *shape of the line inside a pointer that exists* — it grounds the parsing rule in Behaviour step 2 (`/projects/<path>/.git/worktrees/<token>`) — and it is scoped to "live run", not "every directory under runs/". Every one of the five pointer files on disk today reads exactly that form, all five naming a real project directory. Nothing in the plan or in AC-16 claims every run directory carries a pointer; AC-16 says "the shape every live run directory holds today", which is the same shape claim.

The hunter's 5-of-16 count treats all sixteen directories as live runs. Six of the eleven without a pointer are the committed example/milestone fixtures `.gitignore` names (m1-demo, m2-toy, m3-accept, m3-planted-lie, m4-cards, example-hello); the rest are runs from 2026-09-04 or runs whose worktree was cleaned up. The five directories that do carry a pointer are the five newest real runs.

The hunter's own audit of the five recent runs is wrong too. It says "one gives a real diff (deye), one is missing its pointer, and two fail rev-parse. Three of five land on a reason line." On disk right now: macwebsite's branch `lg-2026-09-05-macweb-metadata-19220a` exists, is a registered worktree, and `git diff --stat main...` returns 2 files changed, 367 insertions, 356 deletions — a real diff the hunter did not count. Meanwhile *three* microbits runs (sequence, running-order, order-v3) hit reason 8, not two, because `microbits-opportunities` has no `lg-*` branches left at all. And deye now returns an empty diff, not "13 files changed": it was merged into main 80 minutes ago (5bbb428), so `main...branch` is empty. Neither the numerator nor the denominator survives checking.

`activities/checkpoint.py:144-167` does say what the hunter says (discard removes the worktree with `--force`, then `branch -D`), but that only confirms the second half of the plan sentence the hunter omitted.

The hunter concedes "the behaviour is correct (AC-17 covers reasons 4 and 8)". What is left is a claim that a true, correctly-scoped sentence with an explicit caveat attached "sets the wrong expectation". It does not.

One thing near this area is real but is not this gap: Task 13's manual checklist step 3 says "Open the diff on a finished run; `stat` names files", and a verifier picking an arbitrary finished run would often land on reason 4 or 8. That is Task 13's browser checklist, not Task 8's grounding claim, and the gap under test names task-8, AC-16, AC-17, fn:resolve_repo and the pointer decision — none of which it lands on.

_Opened:_ Opened, all under /home/kevin/projects/loopgraph:

- docs/superpowers/plans/2026-09-05-ui-state-first.md — header and Global constraints (lines 1-60), Task 8 in full (lines 561-641), the Constraints block re-read verbatim (lines 614-623), Task 13 in full (lines 886-928), and every "diff" line in the file.
- docs/superpowers/specs/2026-09-05-ui-state-first-design.md — Goal/Scope/AC-1..AC-6 (lines 1-60), the diff block AC-15..AC-19 (lines 100-135), and the design-decisions block containing "The diff is found through the worktree's `.git` pointer" (lines 255-270).
- activities/checkpoint.py — merge_branch (lines 80-135) and discard (lines 144-167). discard does `worktree remove --force`, `worktree prune`, `branch -D`; merge_branch deletes neither branch nor worktree.

Read-only shell checks:
- `ls runs/` — 16 directories; six are the committed examples .gitignore names (m1-demo, m2-toy, m3-accept, m3-planted-lie, m4-cards, example-hello), confirmed against .gitignore lines 13-24.
- `find runs -maxdepth 4 -path '*worktrees/*/.git'` — 5 pointers: deye-pending-restore/ad4abd, macweb-metadata/19220a, microbits-order-v3/ae4a5b, microbits-running-order/4eca72, microbits-sequence/d5d29c. Sorted by mtime, these are the five newest real runs.
- `cat` of all five pointers — every one reads exactly `gitdir: /projects/<repo>/.git/worktrees/<token>`: /projects/deye, /projects/macwebsite, /projects/microbits-opportunities x3. The plan's shape claim holds for 5 of 5.
- `git -C /home/kevin/projects/{deye,macwebsite,microbits-opportunities} branch --list` and `worktree list` — deye has lg-2026-09-05-deye-pending-restore-ad4abd; macwebsite has lg-2026-09-05-macweb-metadata-19220a plus a prunable worktree at /app/runs/2026-09-05-macweb-metadata/worktrees/19220a; microbits-opportunities has no lg-* branch and only its main worktree.
- `git -C macwebsite diff --stat main...lg-2026-09-05-macweb-metadata-19220a` — 2 files changed, 367 insertions(+), 356 deletions(-). A real diff the hunter's tally omits.
- `git -C deye diff --stat main...lg-...-ad4abd` — empty; merge-base equals the branch tip; `git log -1 5bbb428` dates the merge commit "Merge branch 'lg-2026-09-05-deye-pending-restore-ad4abd'" to 2026-09-05 15:34, 80 minutes before this check. Pre-merge the branch was 14 files against its merge base.
- `.env` line 12 confirms LOOPGRAPH_PROJECTS_DIR=/home/kevin/projects, and all three named project directories exist.

Nothing written, nothing committed.

---

### gap-5 — minor

**Claim.** awaiting.question is recorded unbounded while the card the owner saw was truncated, and neither format_status nor the page caps what it prints. For a merge-ready card the recorded question is the location line plus the whole executor summary and parked list, so `lg status` will dump that block indented two spaces into the awaiting section. The spec justifies verbatim recording with 'one string is what the owner saw on their phone' - not true, since build_card_text truncates the summary to 1500 characters inside a 4000-character message cap.

**Touches:** fn:build_card_text, AC-5, fn:format_status, task-4, task-3  
**Found by:** purefns

**Evidence offered**

```
activities/notify.py:37 `lines.append(summary.strip()[:1500])` and :40 `return "\n".join(lines)[:4000]`; spec:284-286 (the decision); workflows/run.py:482-490 (_owner_card's summary is build_merge_summary of result['summary']); plan:307-309 (format_status prints the question with every line indented two spaces, no cap); plan:227-228 accepts the merge card's question is the full merge summary.
```

**Verdict 1 (facts) — refuted**

The observation is factually right but it names no defect in the spec or the plan.

What checks out. `activities/notify.py:37` really is `lines.append(summary.strip()[:1500])` and `:40` really is `return "\n".join(lines)[:4000]`. The spec decision at 284-286 really does read "Rejected because one string is what the owner saw on their phone". `workflows/run.py:482-483` really does build the merge card's summary from `build_merge_summary(result["summary"], ...)`. And the executor summaries in this repo's own run logs are 371 to 2692 characters, eleven of eighteen over 1500 - so on a real merge card the phone does show a cut-off summary while the ledger would hold the whole thing. The hunter is not misreading the code.

Why it still falls over. The gap's stated harm is "`lg status` will dump that block indented two spaces into the awaiting section". That is not an unhandled case, it is the specified behaviour, written down twice on purpose: plan:295 spells out `<question>` with every line indented two spaces as part of the verbatim copy block, and Task 11 step 2 (plan:800-802) puts the same text on the page in an element with `white-space: pre-wrap`, which is a web page's way of handling a long block. So the "neither surface caps it" half is a disagreement with a decision the plan already made and stated, not a hole in it. On the CLI, printing the full merge summary is the point of the section - it is the text the owner needs in order to answer.

Nothing depends on the sentence the hunter attacks. AC-5 (spec:55-58) defines the value operationally: "the exact string passed as `summary` to `send_card` for that card", and adds "Nothing in `ui.py` or `lg` reconstructs a question from any other field." AC-4 and AC-23 say the page and `lg status` show "the text of `awaiting.question`" - no fidelity promise to the delivered card. Task 3 reduces it to a one-line edit ("add `"question": summary`") and pins it with `test_the_ledger_records_the_question_it_sent`. No acceptance criterion, constraint or test intent asserts that the ledger string equals what Telegram rendered, so nothing breaks and no implementer is steered wrong. What survives is one over-stated clause in an already-resolved decisions log, plus the same phrasing echoed at plan:225-226 whose operative half ("the page prints it as is") is correct.

Two other weaknesses. Neither cited plan range is right: the format_status question line is at 295, not 307-309 (307-309 is the closing fence, a blank line and the "sections are separated by one blank line" sentence), and the merge-card constraint is at 225-226, not 227-228. And "recorded unbounded" is unremarkable in context: the ledger already carries uncapped `claims` and `verdict_reasons` per round, while `awaiting` is popped the moment the owner answers.

Refuted. If it were not, this is cosmetic rather than minor - a rewording of a rationale sentence, changing no code and no test.

_Opened:_ Opened: /home/kevin/projects/loopgraph/activities/notify.py (lines 1-80, with line numbers - confirmed :37 `summary.strip()[:1500]` and :40 `[:4000]`); /home/kevin/projects/loopgraph/workflows/run.py (85-135 `build_merge_summary`, 230-300 round recording, 364-500 `_await_decision`, `_note`, `_park_note`, `_stopped_note`, `_owner_card`); /home/kevin/projects/loopgraph/activities/execute_round.py:259 (`"summary": final["result"].get("summary", "")`, uncapped); /home/kevin/projects/loopgraph/prompts/executor.md:63 (contract says "one paragraph"); /home/kevin/projects/loopgraph/lg `cmd_status` (84-95, today's JSON dump). Spec /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md: AC-4 through AC-10 (lines 49-79), AC-23 through AC-35 (150-200), and the decisions block 260-305 including the quoted decision at 284-286. Plan /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md: Task 3 (176-240, including the constraint at 225-226 and test intents at 230-236), Task 4 (280-330, the format_status copy block with `<question>` at line 295), Task 11 (764-815, `patchAwaiting` printing question in a `white-space: pre-wrap` element). Empirical check: scanned all 18 `runs/*/logs/*executor*.log` files for `"summary"` values - lengths 371, 1173, 1284, 1317, 1533, 1565, 1573, 1582, 1607, 1693, 1753, 1757, 1776, 1777, 1891, 1985, 2026, 2692, so summaries over the 1500 cap are common. Read-only throughout; nothing written or committed.

---

### gap-6 — minor

**Claim.** Two small contract wrinkles in Task 4's Interfaces block. resolve_run_arg's annotation names `datetime`, which lg never imports - it survives only because lg carries `from __future__ import annotations`, so anything that later resolves the hints (typing.get_type_hints, a doc generator) raises NameError. And Task 7 types the same field as `datetime | None` for logs-only rows while Task 4 compares start times with `>`; benign today because logs-only rows never reach the resolver, but the two blocks disagree on the type.

**Touches:** task-4, fn:resolve_run_arg, task-7  
**Found by:** purefns

**Evidence offered**

```
plan:285 `def resolve_run_arg(arg: str, candidates: list[tuple[str, datetime]])` and plan:322 'Start times are compared with >'; lg:9-19 imports argparse, asyncio, json, os, sys, uuid and temporalio.client.Client only; plan:508-509 `def run_entry(wf_id: str, status: str, start_time: datetime | None, close_time: datetime | None, ...)`.
```

**Verdict 1 (facts) — refuted**

The quotes are accurate but neither half is a defect in the plan.

Half 1 (`datetime` not imported in `lg`). The facts check out: `lg` line 9 is `from __future__ import annotations` and lines 11-18 import only argparse, asyncio, json, os, sys, uuid and `temporalio.client.Client`. But "Task 4's signature names a type the file does not yet import" is a request for an import statement, which is exactly the layer this plan says it leaves out. Its header: "states contracts, behaviour and constraints - it deliberately contains no implementation or test bodies... the plan tells you what must be true, the codebase tells you how to say it." The plan does name imports, but only where a rule hangs on them - Task 3's single permitted import into the deterministic `workflows/run.py`, `from envfile import read_env`, and the AC-34 deviation in Task 5. AC-34 (spec:208-210) freezes the import list of `workflows/run.py` alone; nothing anywhere freezes `lg`'s, so an implementer adding `from datetime import datetime` breaks no stated constraint. The hunter's own second citation shows the omission is uniform rather than a Task 4 wrinkle: `ui.py` also carries `from __future__ import annotations` (ui.py:11) and also imports no datetime, yet Task 7's `run_entry` annotates two datetime parameters and the hunter does not call that a gap. And the harm described is hypothetical here: the repo has no ruff, mypy, pyright, sphinx or pdoc (pyproject.toml declares only pytest under dev), and a repo-wide grep for `get_type_hints` and `__annotations__` outside `.venv` returns nothing, so no NameError is reachable.

Half 2 (the two blocks "disagree on the type") misreads the plan. These are two different functions fed by two different sources, and each annotation matches its own source. Task 4's `candidates` come solely from Temporal - plan:250 uses `c.list_workflows(...)` and plan:396 says the objects "async-yield objects with `.id` and `.start_time`" - and temporalio types that field non-optional (`_workflow.py:1276` `start_time: datetime`, against `:1236` `close_time: datetime | None`), so `list[tuple[str, datetime]]` is exactly right and the `>` comparison at plan:322 is safe by construction. Task 7's `run_entry` is wider on purpose: plan behaviour item 3 says logs-only rows (a directory with logs and no workflow) get "both times `None`", which is why it takes `datetime | None`. A row with no workflow can never appear in a `list_workflows` result, so it cannot reach the resolver - not "benign today", but structurally impossible. Two correct signatures over different domains is not a contract disagreement.

_Opened:_ Opened: /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md lines 1-60 (altitude statement and global constraints), 260-340 (Task 4 in full, including the cited plan:285 signature and plan:322 ">" constraint), 340-400 (Task 5/8 wiring, plan:357, plan:374, plan:396 fake `list_workflows` yielding `.id` and `.start_time`), 480-530 (Task 7 in full, including the cited plan:508-509 `run_entry` signature and behaviour items 2-3), plus greps for every "datetime" and "import" mention across the plan. /home/kevin/projects/loopgraph/lg lines 1-40 and a grep of its imports: line 9 `from __future__ import annotations`, lines 11-18 argparse, asyncio, json, os, sys, uuid, `from temporalio.client import Client`, no datetime - the cited fact holds. /home/kevin/projects/loopgraph/ui.py lines 1-40: line 11 `from __future__ import annotations`, imports asyncio, json, threading, time, http.server, pathlib, urllib.parse, activities.stream, no datetime either. Spec /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md lines 170-195 (AC-27 to AC-31) and 196-220 (AC-32 to AC-37, with AC-34 at 208-210 freezing only `workflows/run.py`'s import list). Tooling: pyproject.toml (dev group is pytest only; no ruff/mypy/pyright config, no ruff.toml/mypy.ini/setup.cfg present); repo-wide grep for get_type_hints/__annotations__/mypy/pyright/pdoc/sphinx outside .venv returns nothing (only SPEC.md prose mentioning ruff as an example gate command). Library typing read from /home/kevin/projects/loopgraph/.venv/lib/python3.13/site-packages/temporalio/client/_workflow.py:1236 and :1276.

---

### gap-replay-check-proves-nothing — critical

**Claim.** Task 3's Replayer Verify cannot run to its stated success condition and cannot tell a Task 3 regression from failures that already exist, so AC-33 - the one criterion protecting live runs - goes unproven.

**Touches:** task-3, AC-33, artifact:replay-script, file:workflows/run.py  
**Found by:** workflow

**Evidence offered**

```
I ran the plan's own query against the live Temporal at localhost:7233 today: 0 LoopGraphRun workflows with ExecutionStatus "Running", 13 total, every one COMPLETED. So the script prints '0 running workflows replayed' and the stated expectation ('each running workflow prints replayed') never fires. The written fallback - 'drop the ExecutionStatus clause and replay the first three' (plan:259-260) - is prose, not a command, and it lands on a booby trap: replaying all 13 histories against UNMODIFIED HEAD (7b61c54) gives 6 OK and 7 NondeterminismError, e.g. "Activity type of scheduled event 'load_work_items' does not match activity type of activity command 'run_baseline'" and "...'execute_round' does not match ... 'record_owner_answer'". Those come from earlier commits (75757b1 added the record_owner_answer call; an earlier one added run_baseline before load_work_items), not from Task 3. I then built a scratchpad copy of workflows/run.py implementing plan behaviours 1-5 and replayed the same 13: identical 6 OK / 7 FAIL. The change is genuinely replay-safe, and the check as written would have said the same thing whether it was or not. The plan needs a baseline run before the edit and a runnable fallback that names which histories are expected to fail. Spec AC-33 (spec:203-205) additionally requires 'the history of a workflow that is waiting on a card' - no such workflow exists.
```

**Verdict 1 (facts) — refuted**

The hunter's raw numbers are all correct, but the conclusion they carry is not. The load-bearing claim is that the check "cannot tell a Task 3 regression from failures that already exist". I ran the plan's fallback exactly as it is written - drop the ExecutionStatus clause, replay the first three (plan:259-260) - against unmodified HEAD (7b61c54). All three replayed clean: run-2026-09-05-deye-pending-restore-ad4abd, run-2026-09-05-macweb-metadata-19220a, run-2026-09-05-microbits-order-v3-ae4a5b. There is no booby trap on the path the plan actually names. The hunter got 7 failures by replaying all 13, which the plan nowhere instructs; list_workflows returns newest-first, so "the first three" are the three newest, and those are green. That green baseline is exactly the discrimination the hunter says is missing: after Task 3, a failure among those three is Task 3's.

Those three histories also cover the code Task 3 touches. Their scheduled-activity counts are telegram_configured x2 + send_card x2 each (the _await_decision and _note pairs AC-33 pins), plus record_owner_answer in two of them and merge in one. So the fallback exercises the changed command sequence, twice per run, not some unrelated corner.

The second complaint, that the fallback is "prose, not a command", rests on there being no code for it. The plan's own header says it "deliberately contains no implementation or test bodies", and the fallback is a one-clause edit of the runnable script printed three lines above it. A gap resting on "there is no code for this" does not stand.

What is genuinely true and survives: with 0 running LoopGraphRun workflows the primary script prints "0 running workflows replayed", so its expectation sentence fires on nothing - but the plan anticipates that literal string and branches to the fallback, which is the plan handling the situation rather than missing it. And spec AC-33 (spec:204-207) does say "the history of a workflow that is waiting on a card", and no such workflow exists today; the completed histories carry the same command sequence and more of it, so AC-33's substance is provable, only its qualifier is stale. That is a wording wart worth a one-line edit, not a criterion going unproven.

"The one criterion protecting live runs" also overstates it. There are zero running LoopGraphRun workflows and nothing is holding a card, so no live run is currently exposed. (Plan line 44's "the engine is live and holds owner cards" is itself stale, but it is a don't-restart safety constraint, not the verification.) The hunter's own scratchpad experiment concluded the change is replay-safe. Severity corrected to minor if the orchestrator keeps the residual.

_Opened:_ Opened in full: /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md (all 928 lines; Task 3 at 182-263, Verify script 239-257, fallback sentence 259-261, global constraints 28-70, plan header 3-6), /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md (all 307 lines; AC-32 to AC-35 at 195-215, AC-33 text re-read at 200-208, non-goals 224-245, decisions 297-298), /home/kevin/projects/loopgraph/AGENTS.md (layout, determinism rule, checks).

Read-only commands run against the live Temporal at localhost:7233 (no signals, no restarts, nothing written):
1. Listed LoopGraphRun workflows: 13 total, all status 2 (COMPLETED), 0 with ExecutionStatus "Running". Confirms the hunter's count.
2. Replayed all 13 against HEAD 7b61c54: 6 OK, 7 NondeterminismError, with the two messages the hunter quotes ("Activity type of scheduled event 'load_work_items' does not match activity type of activity command 'run_baseline'" and "...'execute_round' does not match ... 'record_owner_answer'"). Confirms the hunter's split.
3. Ran the plan's written fallback (ExecutionStatus clause dropped, first three only): "replayed run-2026-09-05-deye-pending-restore-ad4abd / run-2026-09-05-macweb-metadata-19220a / run-2026-09-05-microbits-order-v3-ae4a5b", 3 workflows replayed, no error. This is the check the plan actually specifies, and it is green at HEAD.
4. Counted scheduled activity types in those same three histories: ad4abd {execute_round 2, audit 2, telegram_configured 2, send_card 2, run_baseline 1, load_work_items 1, record_owner_answer 1, checkpoint 1, learn 1}; 19220a {execute_round 3, audit 3, telegram_configured 2, send_card 2, run_baseline 1, load_work_items 1, checkpoint 1, learn 1}; ae4a5b {execute_round 2, audit 2, telegram_configured 2, send_card 2, run_baseline 1, load_work_items 1, record_owner_answer 1, checkpoint 1, learn 1, merge 1}.
5. Listed running workflows of any type: one RaceWf, no LoopGraphRun. Confirms nothing is waiting on a card.

Also checked: .venv/bin/python exists; git log confirms HEAD 7b61c54 and that 75757b1 ("Let the owner's answer reach the auditor") predates it, matching the hunter's attribution of the pre-existing failures to earlier commits.

_Severity corrected to:_ minor

---

### gap-question-materialises-on-replay — important

**Claim.** awaiting.question is workflow state recomputed by replay, not stored data, so a run already waiting on a card WILL show a question the moment the new code runs - and it will be the new text, with a location line the Telegram card the owner is holding does not have.

**Touches:** contract:awaiting.question, AC-4, AC-5, AC-23, task-4, task-11  
**Found by:** workflow

**Evidence offered**

```
_await_decision writes self._ledger['awaiting'] at workflows/run.py:388-389 and the ledger query just returns self._ledger (:525-527). A query against a workflow not in the worker's sticky cache replays the history through the current code; I confirmed the query path works live against run-2026-09-05-deye-pending-restore-ad4abd. This falsifies three doc statements at once: the spec non-goal 'Filling awaiting.question into workflows already waiting. Workflow state cannot be edited from outside. The page and lg status say the question was not recorded' (spec:242-243); AC-4's branch for 'a workflow that started before this change' (spec:53-54); and AC-23's 'question not recorded' (spec:157). It also breaks AC-5's 'Its value is the exact string passed as summary to send_card for that card' (spec:56-57) for exactly those runs. Nothing is corrupted, but Task 11 and Task 4 are being asked to build a branch that will not be reached, and the branch that WILL be reached - a recorded question that does not match the card on the owner's phone - is unspecified.
```

**Verdict 1 (facts) — refuted**

The Temporal mechanics the hunter describes are real, but the conclusion drawn from them is wrong, and the residual behaviour it calls "unspecified" is already written down one non-goal above the one it quotes.

1. The branch it calls dead is the ship-day reality. Workflow code only runs inside the worker process. docker-compose.yml bind-mounts ./ into the worker container, but a Temporal Python worker does not hot-reload workflow classes: the running worker keeps executing the OLD `_await_decision` from memory, including when it replays an evicted workflow. `lg ui` and `lg status` run on the host from the new code (`cmd_ui` at lg:204 imports `ui` directly). So the moment this phase merges, the deployed state is new page plus old worker, and every run currently holding a card answers the `ledger` query with an `awaiting` block that has no `question`. That is exactly the state AC-4's "leave the question out" and AC-23's `question not recorded` describe, and exactly what Task 11's "hidden when the key is absent" and Task 4's `test_awaiting_without_a_question_says_not_recorded` are for. The spec even forbids restarting the worker to change this (spec:244-245). The branch is not merely reachable, it is the only branch those runs can take until someone restarts the worker by hand.

2. The "unspecified" branch is specified. Spec:234-236 is a separate non-goal, "Rescuing runs already waiting when this ships. A worker restart replays workflow code, so a run holding a card from before the change may need answering or terminating by hand. This phase notes the risk and does not migrate anything." That is the post-restart case the hunter says nobody wrote down. The hunter cited only spec:242-243 and missed the adjacent entry. Likewise AC-33 (spec:199-207) exists precisely because the authors know a query replays history through current code; it makes the team replay a waiting run's exported history against the new class before shipping.

3. AC-5 is not falsified. It is a contract on the code: the recorded value is the string that same execution passes as `summary` to `send_card`. Under replay that identity still holds. The mismatch the hunter names is between a replay-time recomputation and a card delivered earlier by different code, which is not what AC-5 says.

4. The residual harm is cosmetic. Per AC-20 and Task 3 the only change to `summary` is a location-line prefix ("item 2 of 3 · round 2") on the first line; AC-33 pins the rest of the strings and the activity call shape. So after a future worker restart, an already-waiting run would show its old question with a correct extra location line prepended, a superset of what the owner's card says, not a contradiction. Spec:242-243's rationale ("workflow state cannot be edited from outside") is literally true, and its predicted consequence is accurate for the entire window the phase ships into. At worst it is one imprecise clause about a scenario the neighbouring non-goal already defers, which is a doc-polish item, not an important gap.

Uncertainty also points the same way: the only way to observe the post-restart behaviour is to restart the worker, which both the spec and the run rules forbid while cards are live.

_Opened:_ Read in full: /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md (all 307 lines, with attention to AC-4/AC-5 at 49-58, AC-20/AC-21 at 138-148, AC-22 at 149-151, AC-23 at 152-158, AC-26/AC-27 at 167-173, AC-32 to AC-35 at 196-215, and the whole non-goals list 224-245 — including 234-236, the non-goal the gap does not cite); /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md Task 3 (182-266), Task 4 (267-344) and Task 11 (764-830); /home/kevin/projects/loopgraph/AGENTS.md in full (note: it carries the determinism rule at lines 49-53 but has no "do not restart the worker" line, so spec:244 misattributes that rule — unrelated to this gap); /home/kevin/projects/loopgraph/workflows/run.py lines 360-420 and 500-527, confirming `self._ledger["awaiting"] = {...}` sits at 388-389 with no `question` key and `ledger()` returns `self._ledger` at 525-527 as claimed; /home/kevin/projects/loopgraph/ui.py lines 160-180, the `query("ledger")` then `.result()` fallback; /home/kevin/projects/loopgraph/lg `cmd_status` (84-95) and `cmd_ui` (204-207), showing the CLI and page run host-side; /home/kevin/projects/loopgraph/docker-compose.yml worker service, showing `./:/app` bind mount with `restart: unless-stopped` and no reload mechanism.

_Severity corrected to:_ minor

---

### gap-ac32-unverified — important

**Claim.** AC-32 is the criterion that stops an activity signature change stranding a waiting run, Task 3 claims to deliver it, and no task in the plan checks it - while Task 2 edits the very file that defines send_card.

**Touches:** AC-32, task-3, task-2, fn:send_card, file:worker.py  
**Found by:** workflow

**Evidence offered**

```
Task 3's Delivers line (plan:188) names AC-32, but its Files block (plan:190-193) lists only workflows/run.py and tests/test_visibility.py - neither worker.py nor activities/notify.py - and none of its test intents (plan:232-237) mentions send_card's parameter list or the worker's registered set. Task 2 does modify activities/notify.py (plan:141), the module holding send_card at activities/notify.py:90-93, and its test intents (plan:169-173) pin nothing about it either. The existing suite has test_every_registered_activity_is_decorated (tests/test_review_fixes.py:162) which only checks decorators, not signatures. AC-32 spells the signature out verbatim (spec:198-199) and the spec's own design decision (spec:254-257) says an argument-list change is 'the change most likely to strand a run that is already waiting' - so the plan states the risk and then verifies nothing against it.
```

**Verdict 1 (facts) — refuted**

The citations mostly hold, but the conclusion does not: AC-32 is handled in a part of the plan the hunter never opened, and the one thing it asks for is belt-and-braces insurance against a change the plan forbids anyone from making.

What is true. Task 3's Delivers line (plan:188) does name AC-32; its Files block (plan:190-192) lists only workflows/run.py and tests/test_visibility.py; its test intents (plan:230-237) name no signature pin; Task 2 does edit activities/notify.py; and no test in the repo pins send_card's full parameter list or the worker's registered set. Line refs drift by one or two in places (Task 2's Modify line is plan:139 not 141; test_every_registered_activity_is_decorated is tests/test_review_fixes.py:164 not 162; AC-32's verbatim signature is spec:197-198 not 198-199), which is cosmetic.

Why it is refuted. The hunter read only the Delivers / Files / test-intent blocks of Tasks 2 and 3 and concluded the plan "states the risk and then verifies nothing." It missed three places that do handle AC-32:

1. Global constraints, plan:42-47, cites AC-32 by number and binds every task on the branch: "No activity changes its name, parameter list, defaults or argument count (AC-32)." A do-not-change criterion belongs at branch scope, not inside one task, so Task 3's Delivers line naming it is redundant labelling rather than an unbacked claim.
2. Task 2's Constraints, plan:165-166: the only edit the branch makes to activities/notify.py is "Pure. No activity decorator, no import beyond what the module has" - one added helper in the pure-helpers section, nowhere near send_card at activities/notify.py:89-92.
3. Task 3's Constraints, plan:219-220: "Activity calls do not move ... Same order, same counts, nothing added," which its test intent test_activity_argument_counts_are_pinned (plan:232-233) does pin.

The spec decision the hunter quotes (spec:254-257) is the design *rejecting* the dangerous change, not accepting it: the location line rides inside the card text precisely so send_card gains no parameter. So no task in this plan proposes anything that could break AC-32, and worker.py is never opened. The suite also has partial existing cover the hunter under-read: tests/test_review_fixes.py:164-179 fails if a registered activity loses its decorator or the list drops below eight names, and tests/test_review_fixes.py:894-895 pins "elif expect_reply:" inside send_card's own source, so the expect_reply parameter cannot silently vanish.

What is left is a regression pin nobody has written - a source assert that send_card still reads (kind, wf_id, run_dir, summary, commit, options, expect_reply=True) and that worker.py registers the same twelve names. Cheap, worth adding, and it would guard a future phase that does touch an activity. But its absence is not a hole this plan opens, so the claimed "important" severity is wrong; corrected to minor.

_Opened:_ Opened in full: docs/superpowers/plans/2026-09-05-ui-state-first.md (head 1-60 incl. Global constraints 28-73, Task 2 at 132-179, Task 3 at 182-265, and a grep of every "### Task"/"Delivers:" line to confirm no later task touches AC-32); docs/superpowers/specs/2026-09-05-ui-state-first-design.md lines 180-270 (AC-29 through AC-37 at 196-210, the Non-goals block, and the Decisions block incl. "The location line travels inside the card's text" at 254-257); activities/notify.py lines 80-100 (send_card decorated at :89, def spanning :90-92, exactly the signature AC-32 names); worker.py in full (activities list registering twelve names incl. send_card and telegram_configured); tests/test_review_fixes.py:150-190 (test_every_registered_activity_is_decorated at :164) and :880-915 (test_a_terminal_note_does_not_open_a_reply_box asserting "elif expect_reply:" in inspect.getsource(notify.send_card) at :894-895); tests/test_release.py:65-105 (worker imports are check_telegram tests only, no signature pin); AGENTS.md grepped for activity/worker/signature rules (one hit, line 52, unrelated). Greps run across tests/ for send_card, inspect.signature, getfullargspec, "activities=[", and across the plan for AC-32/AC-33/worker.py/signature/registered/notify.

_Severity corrected to:_ minor

---

### gap-replay-script-mechanics — minor

**Claim.** The replay script hardcodes the Temporal address against the repo's own convention, builds a Replayer per workflow, and needs an unstated working directory.

**Touches:** artifact:replay-script, task-3, AC-33  
**Found by:** workflow

**Evidence offered**

```
plan:249 uses Client.connect("localhost:7233") while worker.py:44, dispatcher.py:141 and lg:21 all read os.environ TEMPORAL_ADDRESS with that value as the fallback - on a host where the address is overridden, the Verify fails for a reason unrelated to the change. plan:251 constructs Replayer(workflows=[LoopGraphRun]) inside the async for, and temporalio 1.32's Replayer docstring says its default ThreadPoolExecutor 'will be shared across all replay calls and never explicitly shut down' - one leaked pool per workflow. The heredoc also does `from workflows.run import LoopGraphRun`, which only resolves when run with the repo root as the working directory; the plan does not say so. I confirmed the venv can import workflows.run and that the script's `await c.get_workflow_handle(id).fetch_history()` parses correctly (fetch_history is async on the handle, get_workflow_handle is not).
```

**Verdict 1 (facts) — refuted**

All three parts of the claim quote real text, but none of them is a defect that survives looking at the rest of the repo and the rest of the plan. The hunter's own evidence says it ran the script and it works.

1. "Hardcoded address against the repo's own convention." The three readers do exist (worker.py:44 and lg:21 exactly as cited; dispatcher.py is line 145, not the claimed 141). But the harm the hunter describes — "on a host where the address is overridden, the Verify fails" — is a host that does not exist here. TEMPORAL_ADDRESS appears in no .env, no .env.example, and no shell on this box (`env | grep -i temporal` returns nothing); docker-compose publishes Temporal on 127.0.0.1:7233, and the two compose services that set the variable set it for containers only. So `os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")` resolves to exactly the literal the plan wrote. AGENTS.md:58-59 forbids hardcoding a *home directory*, and tests/test_release.py only matches `/home/<name>`; an address is not covered. This is a throwaway diagnostic run once by the builder on this machine, and on this machine it is correct.

2. "One leaked thread pool per workflow." The docstring is quoted verbatim (temporalio 1.32.0, _replayer.py:67-70) and _replayer.py:74-75 confirms a fresh `ThreadPoolExecutor()` per Replayer instance that nothing shuts down. But this is a one-shot script over the handful of currently-running workflows; the pools sit idle and Python joins their threads at interpreter exit. Nothing hangs, nothing fails, no test or acceptance criterion is affected. It is a tidiness nit about a five-line diagnostic, not a hole in the plan's contract.

3. "Needs an unstated working directory." The plan states it, just not in the sentence the hunter looked for. Every single Verify in the document invokes the interpreter by the relative path `.venv/bin/python` (lines 124, 175, 239, 338, 411, 479, 554, 637, 705, 755, 820, 918), which only resolves from the repo root — the same relative command AGENTS.md uses in its own Checks block. Line 822 does `.venv/bin/python -c "import ui; ..."`, an import that likewise only works from the root, and plan:113 says outright that both callers "run with the repo root on `sys.path`". The heredoc's `from workflows.run import LoopGraphRun` inherits that established convention rather than inventing a new unstated one.

Judged against the lens: the citations are real, but each one either misses where the repo already handles it (the address matches on the only host that will run this; the cwd is fixed by the plan's own uniform Verify form) or names something with no consequence (a pool reaped at process exit). Refuted.

_Opened:_ Opened: plan lines 1-60 (preamble, global constraints), 105-125 (Task 1 sys.path note), 200-270 (Task 3 including the replay heredoc at 241-257 and the hardcoded connect at 248), plus every `Verify:` line via grep. Spec lines 185-260, covering AC-32 through AC-37 and the non-goal "Restarting the worker to prove replay works". AGENTS.md lines 50-75 (determinism rule, hardcoded-home rule, Checks block). worker.py:42-46, dispatcher.py:141-146, lg:1-40 (ADDRESS at :21, _dotenv), ui.py grep hits, docker-compose.yml TEMPORAL_ADDRESS/7233 lines, .env.example in full, .env keys with values redacted, tests/test_release.py:29-46 (PERSONAL_PATH regex). Ran: `env | grep -i temporal` (no match), `.venv/bin/pip`/version check (temporalio 1.32.0), `inspect.getdoc(Replayer.__init__)` (docstring matches the quote verbatim), and read .venv/.../temporalio/worker/_replayer.py:45,60-80,259,423 confirming `workflow_task_executor or concurrent.futures.ThreadPoolExecutor()` is built per instance and never shut down.

---

### gap-ac35-omits-owner-fields — minor

**Claim.** AC-35's frozen rounds[] key list, which Task 3 owns, omits owner_question and owner_reply - the two keys AC-8 requires the page to render.

**Touches:** AC-35, AC-8, task-3, task-12  
**Found by:** workflow

**Evidence offered**

```
AC-35 (spec:212-214) lists item_no, round, status, verdict, verdict_reasons, files, directive, worktree, branch, base_branch. AC-8 (spec:69) requires 'owner_question with owner_reply when the round asked the owner', and _run_item writes both at workflows/run.py:305-306. Task 3 delivers AC-35 and Task 12 delivers AC-8, so the key-freeze that is supposed to protect what the dashboard reads leaves out two keys the dashboard reads. A live ledger I queried (run-2026-09-05-deye-pending-restore-ad4abd) has 13 round keys, all from the AC-35 list, because that run never asked - the gap only bites on an ask run.
```

**Verdict 1 (facts) — refuted**

The quotes are accurate but the consequence does not stand, and the thing the hunter says is unprotected is protected in three other places it did not look.

1. AC-35's own scope is "every ledger key the dashboard and `lg` read TODAY". Today's dashboard reads exactly two things off `rounds`: `len(rounds)` and `rounds[-1].get("verdict")` (ui.py:176-179). `lg` reads nothing off `rounds` at all — it prints the whole ledger as JSON (lg:141-143) and pulls only `status` for its exit code. So neither `owner_question` nor `owner_reply` is "a key the dashboard reads today", and their absence from a today-scoped list is not an omission. The list is over-inclusive if anything (it names `files`, `directive`, `worktree`, `branch`, `base_branch`, none of which any reader touches today).

2. The AC-35 list is a subset of the round entry by design, not an attempt at completeness. The entry literal at run.py:229-244 also writes `attempts`, `claims` and `self_committed`, and AC-35 names none of them. A list that already leaves out three keys nobody reads is not "the frozen key list" in the sense the gap needs.

3. The operative protection is elsewhere and it covers both keys. AC-33 (spec:199-204) states the changes to `workflows/run.py` are limited to the contents of the `awaiting` dict, the strings passed as `summary` and `text`, and the parameter lists of `_ask_owner`, `_park_note`, `_stopped_note` and `_owner_card`. Lines 305-306 are none of those, so nothing in this change may touch them. AC-22 (spec:149-151) then names `owner_question` directly and pins its meaning: the bare supervisor question with no location line. AC-35's closing sentence, "`question` is the only key added", is a no-additions rule, not a delete-everything-else rule.

4. Task 3 — the task the gap says owns the problem — already carries the pin. Its behaviour item 2 (plan:210) says `_run_item` "still stores the bare `question` in `entry["owner_question"]`", and its test intent `test_owner_question_stays_bare` (plan:234) asserts the source line `entry["owner_question"] = question` survives.

5. Task 12 — the task that delivers AC-8 — renders both keys. Behaviour item 2 (plan:856) says a ledger card shows "`owner_question` and `owner_reply` when the round asked", and test intent `test_the_rounds_copy_is_pinned` (plan:873) asserts the page text contains `owner_reply`.

So no implementer following the plan can drop, rename or fail to render either key, and no acceptance criterion breaks on an ask run. The most that survives is a cosmetic wish that AC-35's illustrative list were longer — which is not a defect in the spec or the plan.

_Opened:_ Opened, all under /home/kevin/projects/loopgraph:
- docs/superpowers/specs/2026-09-05-ui-state-first-design.md lines 55-90 (AC-5 through AC-12, incl. AC-8 at line 66-71), 140-165 (AC-20 through AC-26, incl. AC-22 at 149-151), 165-215 (AC-26 through AC-35, incl. AC-33 at 199-204 and AC-35 at 211-215), and 215-235 (AC-36, AC-37, Non-goals).
- docs/superpowers/plans/2026-09-05-ui-state-first.md lines 180-250 (Task 3 in full: Delivers AC-5/20/22/32/33/34/35, Behaviour 2 at line 210, test intents at 232-236) and lines 830-890 (Task 12 in full: Delivers AC-8/9/10/14, Behaviour 2 at line 856, test intents at 872-874).
- workflows/run.py lines 225-262 (the round `entry` literal and the verdict writes) and 280-330 (the `ask` branch, incl. `entry["owner_question"] = question` at 305 and `entry["owner_reply"] = reply` at 306). Also grepped every `entry[` write in the file: status/commit at 164-172, verdict/verdict_reasons at 258-259, owner_question/owner_reply at 305-306.
- ui.py — grepped for `rounds`, `branch`, `worktree`, `base_branch`, `ledger`. Only reads: `ledger.get("rounds", [])`, `rounds[-1].get("verdict","")`, `len(rounds)`, `ledger.get("status", ...)` at lines 167-179. No hit for worktree/base_branch anywhere.
- lg — grepped for `rounds`, `worktree`, `base_branch`, `directive`, `verdict`, `files`: zero hits. Reads `ledger.get("status","")` at line 143 and dumps JSON at 142.
- Repo-wide grep for `owner_question|owner_reply` (excluding runs/): only run.py:305-306, spec:68 and 149, plan:210, 234, 856, 873.

---

### gap-4 — important

**Claim.** Task 5 cannot verify its own argparse change: every test intent drives cmd_status through a hand-built Namespace, so `--json` never reaching the parser, or the documented `lg status <id> ledger` form breaking, both stay green.

**Touches:** task-5, file:lg, AC-24  
**Found by:** cli

**Evidence offered**

```
plan:358-360 states the deliverable as 'the status subcommand gains --json (action="store_true")' and names the Namespace fields workflow_id, query, json. plan:393-397's fixture monkeypatches lg._client, and all ten intents at plan:398-409 exercise cmd_status only. plan:411's Verify is pytest. The block that must actually change is lg:217-220. A test constructing argparse.Namespace(workflow_id=..., query=None, json=True) passes whether or not main() was touched — while AC-24 (spec:159-162) requires the positional form README.md:218,281 and SKILL.md:159 document to keep working.
```

**Verdict 1 (facts) — refuted**

Every quotation checks out, but the one sentence the gap rests on is the hunter's own assumption, not something the plan says.

The claim is "every test intent drives cmd_status through a hand-built Namespace, so --json never reaching the parser stays green." The plan never states a test entry point. What it states at plan:393-397 is a fixture that monkeypatches `lg._client`. That fixture works just as well when the test drives the real CLI: `main()` (lg:210-250) calls `p.parse_args()` and then `asyncio.run(args.fn(args))`, and `cmd_status` (lg:84) awaits `_client()` — the monkeypatched object. A test that patches `sys.argv` to `["lg", "status", "x", "--json"]` and calls `lg.main()` satisfies the stated fixture exactly and does fail when the parser is not touched. The gap treats "the plan doesn't spell out the test body" as "the test body must be the weak one", which is the shape of gap the instructions rule out: the plan's own header (plan:3-6) says it deliberately carries no test bodies.

The plan also pushes the other way, which the gap's evidence leaves out. plan:353 names the files to modify as "`lg` (`cmd_status`, the `status` argparse block)" — the parser block is called out as a thing that must change, not just the handler. And two of the ten intents are named at the CLI surface, not the function surface: plan:400 `test_json_flag_prints_the_raw_ledger` and plan:401 `test_positional_query_still_prints_json`. "Flag" is a parser word; an implementer reading "the status subcommand gains --json (action=store_true)" next to a test called "json flag" is being pointed at the parser.

The Interfaces line the gap quotes (plan:358-360) says "The Namespace fields `cmd_status` reads are `workflow_id`, `query`, `json`". That is a contract about what `cmd_status` may read, which the plan needs to say because Task 4's pure functions and Task 5's handler are split. It is not an instruction to build Namespaces in tests.

The one real supporting signal is repo precedent: the only existing lg test, `tests/test_release.py:150-175`, calls `lg.cmd_where(None)` directly rather than going through `main()`, and no test in tests/ touches argparse or `main()` today. So an implementer could write the weak version. But "could be implemented badly" is not a hole in the plan when the plan names the argparse block as a deliverable and names two tests after the CLI forms. At most this is a wording nicety — "the flag and positional tests enter through `main()` with patched argv" would remove all doubt — which is a minor polish note, not an important gap. Default to refuted on that uncertainty.

Everything else in the evidence is accurate: lg:217-220 is the status parser block (`add_parser("status")`, `workflow_id`, `query` nargs="?", `set_defaults(fn=cmd_status)`), spec:159-162 is AC-24 as quoted, README.md:218 and 281 and skills/loopgraph/SKILL.md:159 all document `lg status &lt;workflow-id&gt; ledger`, and plan:390 already carries that as a named constraint on Task 5.

_Opened:_ Opened: /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md lines 1-80 (header, incl. the "no test bodies" note and the global constraint at ~line 60 that tests load `lg` with SourceFileLoader), 267-345 (Task 4) and 345-420 (Task 5, with 391-412 printed line-numbered). /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md lines 140-180 line-numbered over 155-165 (AC-23 through AC-25). /home/kevin/projects/loopgraph/lg lines 190-300 line-numbered over 214-222 (the status parser block at 217-220) plus `cmd_status` at line 84 and the `main()` tail (`args = p.parse_args(); return asyncio.run(args.fn(args))`). /home/kevin/projects/loopgraph/README.md lines 210-224 and 275-285; /home/kevin/projects/loopgraph/skills/loopgraph/SKILL.md lines 155-163. /home/kevin/projects/loopgraph/tests/test_release.py lines 142-175 (the `_lg()` SourceFileLoader helper and the two `lg.cmd_where(None)` tests). Grepped tests/ for `Namespace|lg.main|sys.argv|argparse|parse_args|--help` — no hits; no existing test drives the lg parser. Grepped the plan for `lg status|--json|argparse|main()` to check whether a later task re-verifies the CLI — none after Task 5.

_Severity corrected to:_ minor

---

### gap-7 — minor

**Claim.** The candidate listing behind resolve_run_arg must stay unbounded and untimed to satisfy AC-28/AC-30, and no constraint says so — while the sibling task pins the opposite rule for the same call.

**Touches:** task-5, task-7, fn:resolve_run_arg, AC-28, AC-30  
**Found by:** cli

**Evidence offered**

```
plan:376 step 3 says 'list WorkflowType = "LoopGraphRun", collect (wf.id, wf.start_time)' with no cap, no ORDER BY StartTime DESC and no rpc timeout. Task 7's constraint at plan:540 pins the opposite for the same API: 'Keep the 25-row cap in _runs and the call(timeout=10) bound' (ui.py:181-182, ui.py:158-159). AC-28 (spec:180) requires the newest across every match and plan:322 compares start times with '>', so copying that cap would silently pick the wrong run. Live data already has a 5-way match — five run-example-hello-* workflows — which is the AC-30 case, and a slow or hung listing has no bound on the terminal.
```

**Verdict 1 (facts) — refuted**

The quotes are accurate but the inference is not. plan:540 reads "Keep the 25-row cap in `_runs` and the `call(timeout=10)` bound; a poll must not hang the page" — both names are scoped by the constraint's own words to ui.py: `_runs` is `TemporalFeed._runs` (ui.py:161) and `call` is `TemporalFeed.call` (ui.py:158-159), a helper that does not exist in `lg`, with an explicitly page-poll rationale. Task 5's listing is in `lg`'s `cmd_status`, a different file and process. So the sibling task does not "pin the opposite rule for the same call"; it pins a rule for a different call site, and no reader would carry it into the CLI.

With that gone, the gap reduces to "the plan does not forbid a cap it never asked for." plan:373-376 states the contract fully — collect (wf.id, wf.start_time), hand them to `resolve_run_arg` — and plan:313-316 / AC-31 (spec:188-190) define that function over the whole candidate list, choosing the greatest start time. Absent a stated cap there is no cap.

The "no ORDER BY StartTime DESC" half is a non-defect under this design: the pick is a max over all candidates (plan:322 compares with `>`), so listing order is irrelevant; an ORDER BY only matters if you cap, which the design does not.

The timeout half asks for a convention `lg` does not have anywhere: `_client()` (lg:50-51), `cmd_status`'s query loop (lg:84-96), `cmd_gate`'s `handle.result()` and `cmd_round` all await unbounded today. The plan's Task-5 hang constraint (plan:386-387) targets `handle.result()` on a running run, which blocks by definition — a categorically different exposure from a hypothetical slow RPC. Plan:250 shows the same uncapped, untimed listing shape already used in Task 3's verify script, so the pattern is consistent, not an oversight.

The live-data detail is true but does not bite: the listing returns 13 LoopGraphRun workflows, five of them run-example-hello-*, so AC-30's multi-match case is real, but 13 is far under 25 and no cap exists on the CLI path anyway. Refuted.

_Opened:_ Opened: spec /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md lines 163-200, covering AC-25..AC-31 (AC-28 at 175-181 "Among several matches the newest by `WorkflowExecution.start_time` wins"; AC-30 at 185-187; AC-31 at 188-190 "a pure function in `lg` taking the argument and a list of (workflow id, start time) pairs"). Grepped the whole spec for "cap|timeout|list_workflows|ORDER BY|hang" — the only cap in the spec is the 200 KB patch cap at spec:299; nothing about a listing bound.

Opened plan /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md lines 228-270 (Task 3's verify script, whose plan:250 does an uncapped, untimed `async for wf in c.list_workflows(...)` on the host), 267-330 (Task 4: behaviour 313-316 "Several -> the id with the greatest start time, and the match count"; constraint 322 "Start times are compared with `>`"), 360-410 (Task 5: step 3 at 373-376 exactly as quoted; constraints 385-390 including "handle.result() blocks until the workflow ends. Without the describe() guard a running run whose ledger query failed would hang the terminal"; test intents 395-408), and 500-545 (Task 7 constraints, 540 quoted exactly).

Opened /home/kevin/projects/loopgraph/ui.py lines 140-200: `def call(self, coro, timeout=10)` at 158-159 (a TemporalFeed method), `_runs` at 161, and `if len(out) >= 25: break` at 181-182. Both cited spans exist and say what the gap says, inside ui.py only.

Opened /home/kevin/projects/loopgraph/lg lines 40-115: `_client()` at 50-51 (`Client.connect(ADDRESS)`, no timeout), `cmd_status` at 84-96 (query loop, no timeout, re-raises), `cmd_gate` and `cmd_round` awaiting `handle.result()` unbounded. There is no `call()` helper and no timeout convention anywhere in `lg`.

Ran a read-only live listing: `list_workflows('WorkflowType = "LoopGraphRun"')` returns 13 ids, five of them run-example-hello-23c436/3e352e/a4b453/b4cb62/cbf12e. The 5-way match is real; the total is 13, under the 25 the gap worries about, and that cap lives only in ui.py.

---

### gap-ledger-no-disconnect-guard — critical

**Claim.** Task 7's TemporalFeed.ledger has no guard for a feed that failed to connect, so with Temporal down `lg ui` serves a broken /api/run on every 2-second board poll instead of the 200-with-null-ledger that AC-1 and AC-10 require.

**Touches:** task-7, fn:TemporalFeed.ledger, endpoint:/api/run, AC-1, AC-10, file:ui.py, task-11  
**Found by:** endpoints

**Evidence offered**

```
make_server builds a TemporalFeed whenever temporal_addr is truthy, and the default is 'localhost:7233' (ui.py:208-209). I ran it against a dead address: the feed object is truthy and self._client stays None (ui.py:141, 153-155). Task 7 behaviour 4 then calls feed.ledger(id) because `feed` is truthy (plan l.530), and the behaviour Task 7 states for ledger() — 'query ledger; on any failure try handle.result()' (plan l.522) — raises AttributeError: 'NoneType' object has no attribute 'get_workflow_handle' on its first statement, before any try/except it describes. The sibling runs() carries exactly this guard (`if not self._client: return []`, ui.py:186-187) and the plan does not carry it over. Nothing catches it: the two /api/run test intents use feed=None or a FakeFeed (plan l.547-551), and Task 11's manual check is `ui.serve(8401, None)` (plan l.822-823), which is the feed=None path. AC-1 says the status is still HTTP 200 'so the page works from log files alone' (spec l.33-35) and AC-10 depends on that reply to print 'temporal unreachable — logs only' (spec l.75-78).
```

**Verdict 1 (facts) — refuted**

The hunter's line citations are all accurate, but the claim rests on an implementation detail the plan deliberately does not state, and the behaviour the plan does state already covers the dead-feed case.

1. Plan line 522 (Task 7 behaviour 1) reads: "`TemporalFeed.ledger`: query `ledger`; on any failure try `handle.result()`; on failure return `None`. This is the fallback `_runs` has today, extracted..." A client that never connected is a failure of "query ledger", and "on any failure return `None`" is exactly the contract that makes /api/run answer 200 with a null ledger. The hunter's "raises AttributeError ... before any try/except it describes" is only true for one particular way of writing the function (hoisting `self._client.get_workflow_handle(id)` above the try). The obvious extraction — the one the plan points at, `_runs` at ui.py:165-173, where `self._client.get_workflow_handle(wf.id).query("ledger")` sits *inside* the `try` — catches the AttributeError in the same handler that catches a connection error and returns None. The plan's header (l.3-6) says it "deliberately contains no implementation or test bodies", so "the guard line is not written out" is not a gap.

2. Task 7 delivers AC-1 (plan l.491), and AC-1 (spec l.28-35) states the required outcome in words the implementer must satisfy: "With Temporal unreachable, or an id Temporal does not know, `ledger` is `null` and the status is still HTTP 200, so the page works from log files alone." So the unreachable case is not unspecified anywhere; it is spelled out in the very criterion the task is assigned.

3. The plan also introduces `connected -> bool(self._client)` (l.503-504) and `"temporal": bool(feed and feed.connected)` (l.531), so the disconnected-but-constructed feed is a case the plan is explicitly aware of and reports on the wire; AC-10's `temporal unreachable — logs only` line is driven by that false flag (plan l.797-799), which is reachable only when a feed object exists and is not connected — i.e. the plan does contemplate the exact state the hunter says it ignores.

The one residual, much smaller point is that no test intent builds a feed that exists but is not connected (`test_run_endpoint_without_temporal_is_null_and_200` uses the existing `temporal_addr=None` fixture, and Task 11's manual check `ui.serve(8401, None)` is confirmed the feed=None path since `serve(port, temporal_addr)` at ui.py:250-251). But a FakeFeed with `connected=False` is already in the fixture's remit (l.543-545), and a missing test variant is a minor coverage nit, nowhere near a critical gap. Refuted.

_Opened:_ Opened, in full or in the cited ranges:
- /home/kevin/projects/loopgraph/ui.py (note: the repo root holds ui.py, not src/loopgraph/ui.py) — lines 120-230 covering `TemporalFeed.__init__` (l.140-148, `self._client` set to None), `_run` (l.150-156, connect failure leaves `_client` None and `_err` set), `call` (l.158-159), `_runs` (l.159-183, with the query/result fallback both inside `try` blocks at l.165-173), `runs()` guard at l.185-191, `make_server` at l.208-209; plus `serve` at l.250-253.
- /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md — header l.1-40 (the "no implementation or test bodies" instruction), all of Task 7 l.488-560 (Delivers l.491, interface block l.502-516, behaviour 1-5 l.522-533, constraints l.536-540, test intents l.543-551), Task 11 l.770-825 (behaviour 1 l.796-799, verify l.820-825), and a grep for `connected|unreachable|_client|TemporalFeed` across the whole plan.
- /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md — AC-1 l.28-35 and AC-10 l.75-78.
- /home/kevin/projects/loopgraph/tests/test_ui.py — the `server` fixture (`ui.make_server(0, tmp_path, temporal_addr=None)`) and `test_runs_endpoint_without_temporal`.

_Severity corrected to:_ minor

---

### gap-truncation-protocol — important

**Claim.** The plan's description of append_log is wrong in both directions, and the single reset signal it derives from it — reply offset lower than the requested one — does not fire in the case it was written for.

**Touches:** task-6, AC-11, AC-9, fn:append_log, fn:log_slice, endpoint:/api/log, task-9, file:activities/stream.py  
**Found by:** endpoints

**Evidence offered**

```
Task 6 constraint l.462-463 says 'append_log already caps a file at 1 MB and the first open of a pane fetches it once'. It does neither. append_log writes the line first and only then, when st_size passes LOG_CAP, rewrites the whole file as b'[... head truncated ...]\n' + the last LOG_CAP//2 bytes (activities/stream.py:45-51). I measured it: 1,000,091 bytes become 500,025 — the file loses half its content in one step, so a first open can return just over 1 MB, and the shipped 60 KB-per-file bound (LOG_TAIL, ui.py:24, ui.py:127) is removed with log_tails (plan l.426) with nothing put in its place. The protocol consequence: after a truncation, byte N of the new file is byte N+499,965 of the old one, so `offset > size` (plan l.454, spec l.85-87) detects the rewrite only while the stored offset is above ~500 KB. A pane that was collapsed keeps its offset and its text and resumes from it (AC-9 spec l.72-73, Task 9 behaviour 2 plan l.674-675); if it was closed at offset 40,000, the file then grew past 1 MB and truncated, the page appends bytes [40000:size] of an unrelated region with no reset and shows silently spliced text. AC-11 needs a second signal (a restarted flag, or the page treating any size below its stored offset-at-last-poll as a restart), and Task 6's constraint needs to say what append_log really does.
```

**Verdict 1 (facts) — refuted**

Two of the gap's three assertions misread the sources, and the headline one is false.

1. "append_log already caps a file at 1 MB ... It does neither." The code does cap the file at 1 MB. append_log writes the line, then rewrites whenever st_size passes LOG_CAP, so the file never holds more than 1 MB plus one line. The constraint at plan line 460 is a bound on what a first open can transfer, and the gap's own evidence ("a first open can return just over 1 MB") concedes exactly that bound. The file sitting at 500 KB after a rewrite does not make "capped at 1 MB" wrong.

2. "the first open of a pane fetches it once ... It does neither." That clause is about the pane, not about append_log. The sentence joins two subjects: append_log caps the file, and the pane fetches it once on first open. Task 9 behaviour 2 and 3 (plan 677-682) specify precisely that. The gap reads both clauses as claims about append_log, which the sentence does not make. So "wrong in both directions" is the gap's misreading, not the plan's error.

3. "the single reset signal does not fire in the case it was written for." It fires. The case it was written for is an open pane polling across a truncation (AC-11 spec 82-89, AC-9 spec 71-74). Such a pane stores the last reported size as its next offset, so just before a truncation its offset is at or above 1 MB; the post-rewrite size is 500,025; offset > size, and the reset fires. For the signal to miss on an open pane the file would have to truncate and regrow past ~500 KB inside one 2-second poll, against a largest-ever observed log of 29 KB.

What survives is only the narrower case the gap builds at the end: a pane opened early, left at a low offset, collapsed (AC-9 spec 72, Task 9 behaviour 2 plan 677-678 keep the offset and the text), the file then crossing 1 MB and truncating while it is closed, and the reader reopening it. There the reply offset equals the requested one, the page appends, and the pane shows spliced text without even the "[... head truncated ...]" marker, since that marker sits in the first 25 bytes the slice skips. That hole is real and nothing in the plan or spec closes it.

But it is minor, not important. Every ingredient has to line up: a log 34x larger than any this repo has ever produced, a pane opened then closed at an offset under ~500 KB, a truncation landing in that window, and a reopen. The result is garbled text in a read-only viewer that clears itself on the next page reload or run reselect (Task 9 behaviour 6 clears the board on selection). No state is corrupted and no decision rests on it.

The gap's removed-cap argument also overstates. It says the 60 KB LOG_TAIL is dropped "with nothing put in its place", but the same constraint names the 1 MB file cap as the replacement bound, and panes are collapsed by default so a reader opens one at a time.

Two smaller citation slips: the quoted constraint text is at plan lines 460-461, not 462-463 (the gap cites the next constraint's lines for it), and the byte shift after a rewrite of a 1,000,091-byte file is ~500,066, not the 499,965 given.

Filed as an important defect in the reset protocol, the gap does not stand: the protocol works in the case it was written for, and the document says what the code does. Refuted, with a minor cosmetic edge case worth a line in Task 9 if anyone wants belt and braces.

_Opened:_ Opened, in full or at the cited lines: /home/kevin/projects/loopgraph/activities/stream.py (all 74 lines; append_log at 45-51, LOG_CAP at 16); /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md lines 60-130 (AC-9 at 71-74, AC-11 at 82-89); /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md lines 400-480 (Task 6, with the Files line at 426, log_slice behaviour at 453-455, and the two constraints at 458-463) and lines 640-712 (Task 9, behaviours at 670-687, constraints at 689-696); /home/kevin/projects/loopgraph/ui.py lines 118-135 and the LOG_TAIL definition at line 24 with its use at line 127.

Ran the truncation myself to measure the step: a file at 1,000,000 bytes, one more append, then the rewrite lands at 500,025 bytes (25-byte marker plus the last 500,000). So the half-file rewrite the gap describes is real.

Surveyed the live logs: `find runs -name '*.log' -printf '%s\n' | sort -n | tail` gives a largest file of 29,446 bytes across 36 log files, none over 400 KB, and `grep -rl "head truncated" runs --include='*.log'` returns zero files. Truncation has never fired in this deployment. tests/test_visibility.py:73-82 exercises it only by shrinking LOG_CAP to 200.

Grepped the whole plan and spec for "truncat|shrank|restart|LOG_CAP|append_log": the only reset signal anywhere is `offset > size` (plan 453, 462-463; spec 85-87). No second flag exists.

_Severity corrected to:_ minor

---

### gap-discard-constraint-false-on-disk — important

**Claim.** Task 8's constraint that a discarded worktree leaves no pointer is false on this machine today, so the most common finished-run state lands on the wrong reason line: 'branch not found: lg-...' where the truth is 'this run was discarded'.

**Touches:** AC-17, task-8, artifact:worktree-pointer, fn:discard  
**Found by:** diff

**Evidence offered**

```
plan:621-622: 'A worktree that `discard` removed has no pointer, which is reason 4.' On disk: runs/2026-09-05-microbits-order-v3, -running-order and -sequence all still hold `.git` pointer files (e.g. runs/2026-09-05-microbits-sequence/worktrees/d5d29c/.git -> `gitdir: /projects/microbits-opportunities/.git/worktrees/d5d29c`) with their full working trees beside them, while `git -C /home/kevin/projects/microbits-opportunities branch --list` shows only `main` and `/home/kevin/projects/microbits-opportunities/.git/worktrees/` does not exist. activities/checkpoint.py:143-169 explains how: `worktree remove --force` failures are swallowed by `except RuntimeError: pass` before `worktree prune` and `branch -D` succeed. So resolve_repo succeeds, rev-parse fails, and three of the ten real runs on this box get reason 8. Reason 4 is the one that would never fire.
```

**Verdict 1 (facts) — refuted**

The quotes are accurate but the conclusion drawn from them is contradicted by the hunter's own evidence.

1. The plan sentence is literally true, and true of this box. Plan line 622 says "A worktree that `discard` removed has no pointer, which is reason 4." That is a conditional about worktrees discard actually removed. Four runs on disk are exactly that case: runs/2026-09-05-microbits-order-v2 provably had worktree 983d87 (its audit log records /app/runs/2026-09-05-microbits-order-v2/worktrees/983d87) and that directory, pointer and all, is gone, leaving an empty worktrees/ dir. Same for 2026-09-04-toy-count (worktrees/r1 in its log, now empty), microbits-fact-corrections and microbits-ideas-sharpen. For every one of them /api/diff hits reason 4.

2. So the load-bearing conclusion, "Reason 4 is the one that would never fire", is false. Counting runs on this machine: 2 land on the happy path (deye's lg-...-ad4abd and macwebsite's lg-...-19220a both still exist, so rev-parse succeeds and a real diff comes back), 3 land on reason 8 (the microbits pointer-survives cases), and 4-5 land on reason 4. Reason 4 fires more often than reason 8, not never.

3. "The wrong reason line" is not wrong. Spec AC-17 (lines 119-123) enumerates "either branch failing `git rev-parse --verify`" as one of the eight states that must return 200 with a one-line explanation. When the branch is gone, "branch not found: lg-..." is the truthful, specified answer. Nothing in the spec or plan promises a "this run was discarded" line; the only spec mention of discard (line 237) is an unrelated non-goal about the `discard failed` note. The gap asks for a message the design never committed to.

4. Nothing is missing from Task 8. Its test intents already parametrise test_each_failure_is_a_200_with_its_reason over reasons 2 to 8, so reason 8 is contracted and tested. The gap names no absent contract, behaviour, constraint or test intent.

The checkpoint.py:143-169 reading is fair as far as it goes (the `except RuntimeError: pass` at 159-160 does swallow a failed `worktree remove --force`), and the stale-pointer state of the three microbits runs is real. But that is a `discard` bug in activities/checkpoint.py, outside this spec and plan, and it does not make the plan's constraint false or the diff endpoint's reason list wrong. The most it supports is a wording nit: the sentence would read better as "a worktree discard removed has no pointer; one whose removal failed still does, and shows reason 8." That is not an important gap in the design.

_Opened:_ Opened: /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md lines 560-700 (all of Task 8: reason list 585-594, Behaviour 1-5, Constraints including the cited 621-622, Test intents), with grep confirming 622 is the quoted line. /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md lines 100-130 (AC-15 through AC-19) plus a grep for every "discard" in the spec (one hit, line 237, unrelated). /home/kevin/projects/loopgraph/activities/checkpoint.py lines 100-200, line numbers confirmed by grep: 144 `async def discard`, 157 `worktree remove --force`, 159 `except RuntimeError:`, 162 `worktree prune`, 166 `branch -D` — the swallow the gap describes is there.

Read-only shell: `ls` of every runs/*/worktrees; `cat` of all five surviving .git pointer files (deye/ad4abd, macwebsite/19220a, microbits ae4a5b, 4eca72, d5d29c, each holding `gitdir: /projects/<repo>/.git/worktrees/<token>`); `git -C /home/kevin/projects/microbits-opportunities branch --list` (only `main`), `worktree list` (only the main checkout), `ls .git/worktrees` (no such directory) — the hunter's microbits facts check out. Also `git branch --list` and `ls .git/worktrees` for /home/kevin/projects/deye (branch lg-2026-09-05-deye-pending-restore-ad4abd present) and /home/kevin/projects/macwebsite (lg-2026-09-05-macweb-metadata-19220a present, checked out in worktree 19220a) — two happy-path runs the gap did not count. Grepped run logs for `/app/runs/...worktrees/<token>` to prove the empty-worktrees runs really had worktrees that were removed. Grepped the repo for `worktree remove`/`prune` to confirm checkpoint.py:157/162 is the only removal site.

_Severity corrected to:_ minor

---

### gap-empty-base-branch — important

**Claim.** base_branch can legitimately be the empty string, and Task 8 has no reason line for it. It degrades to 'branch not found: ' with a blank name, and the diff expression it would otherwise build silently means something else.

**Touches:** AC-17, task-8, fn:branch_diff, fn:execute_round  
**Found by:** diff

**Evidence offered**

```
activities/execute_round.py:229 sets `base_branch = (await _git('branch', '--show-current', cwd=target_repo)).strip()`, which is '' when the owner's repo is on a detached HEAD at round start. Two places in the shipped code already guard that exact case and say so: activities/checkpoint.py:112 `if not base_branch: return {'merged': False, 'reason': 'unknown base branch (detached HEAD at round start)'}` and workflows/run.py:486 `'merge into ' + (result['base_branch'] or 'base')`. Task 8 does not. AC-17's eight modes and plan:586-598's nine reasons name nothing for it; the closest, `branch not found: <name>`, renders with an empty name. And the string plan:608-609 builds, `f'{base}...{branch}'`, is `...lg-x` when base is empty, which git reads as `HEAD...lg-x` - a diff against whatever the owner happens to have checked out. Only the rev-parse guard stops that, so the guard is load-bearing in a way neither document states.
```

**Verdict 1 (facts) — refuted**

Every citation the hunter makes is accurate, but the conclusion does not follow, because the plan already orders the guard that stops the dangerous outcome.

What holds up. `activities/execute_round.py:229` really does set `base_branch` from `git branch --show-current`, which is the empty string on a detached HEAD. `activities/checkpoint.py:111-112` really does guard `if not base_branch` with the reason "unknown base branch (detached HEAD at round start)". `workflows/run.py:486` really does write `result["base_branch"] or "base"`. So an empty `base_branch` is a real value that can reach `rounds[-1]` (run.py:238 copies it into the ledger entry unconditionally). I confirmed by shell that `git rev-parse --verify ""` fails, exit 128, "fatal: Needed a single revision", and that a `...HEAD`-shaped diff argument is accepted by git.

Why the gap still fails. The claim's serious half is "the diff expression it would otherwise build silently means something else... Only the rev-parse guard stops that, so the guard is load-bearing in a way neither document states." Both documents state it, in order:
- Spec AC-17 (docs/.../specs/2026-09-05-ui-state-first-design.md:119-123) lists as its eighth 200-with-reason mode "either branch failing `git rev-parse --verify`". That is not a mode the hunter says is missing - it is the mode that covers this exact input.
- Plan Task 8 step 3 (plan:608-610) reads: "`branch_diff`: `git rev-parse --verify` each branch (reason 8), **then** `git diff --stat <base>...<branch>`...". The word "then" fixes the ordering. With an empty base, rev-parse fails first and the handler returns reason 8. The `...lg-x` string is never handed to git, so the "diff against whatever the owner has checked out" outcome the gap warns about cannot occur under the plan as written.

So the gap is not an uncovered mode. It reduces to: the reason line prints as "branch not found: " with a blank name after the colon. That is cosmetic - the endpoint still returns 200, still returns an empty patch, still tells the owner the diff could not be built. Calling an empty branch name "not found" is not even wrong.

The residue is a rationale note, not a contract hole: neither document spells out *why* rev-parse guards specifically against a detached-HEAD empty base. The plan states contracts, behaviour, constraints and test intents on purpose, and "the plan does not explain the motivation behind a behaviour it already specifies" is not a gap. AC-17 and plan step 3 both specify the behaviour.

Two secondary checks. No live run under runs/ has an empty `base_branch` (grep found none), so this is a possible-but-unobserved input. And plan:614 already parametrises `test_each_failure_is_a_200_with_its_reason` over reasons 2 to 8, which includes reason 8, so the guard is under test intent too.

Refuted. If not refuted it would be minor, not important: the blank name in one reason string, with no wrong diff and no 500.

_Opened:_ Opened: /home/kevin/projects/loopgraph/activities/execute_round.py lines 200-270 (confirmed line 229 `base_branch = (await _git("branch", "--show-current", cwd=target_repo)).strip()` and line 269 returning it); /home/kevin/projects/loopgraph/activities/checkpoint.py lines 90-140 (confirmed line 111-112 `if not base_branch: return {"merged": False, "reason": "unknown base branch (detached HEAD at round start)"}`); /home/kevin/projects/loopgraph/workflows/run.py lines 225-250 and 470-500 (confirmed line 238 puts base_branch in every ledger round entry, line 486 `"merge into " + (result["base_branch"] or "base")`); /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md lines 100-130 (AC-15 through AC-19, AC-17 verbatim including its eighth mode "either branch failing `git rev-parse --verify`"); /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md lines 560-640 (all of Task 8: interfaces, the nine reason strings at 586-598, behaviour steps 1-5, constraints, test intents), and re-read 586-598 and 606-610 with line numbers to check the hunter's citations. Ran `grep -rn "base_branch" --include=*.py .` for every use in the shipped code. Ran `grep -rl '"base_branch": ""' runs/` - no live run has an empty base. Ran in a throwaway temp repo: `git rev-parse --verify ""` -> `fatal: Needed a single revision`, exit 128; `git diff --stat "...HEAD"` -> exit 0.

_Severity corrected to:_ minor

---

### G4 — important

**Claim.** The automated half of AC-14 is a lexical naming check that an implementer can satisfy while replacing the DOM on every poll. It constrains where the string `innerHTML` sits in the page source, never who calls what. Nothing forbids `poll()` or `patchBoard()` from calling `buildBoard()` unconditionally every 2 seconds — and build-from-patch is not even suspicious, because Tasks 9 and 12 require `patchRounds` to call `buildRoundCard` and `buildLogPane`. The "nearest preceding `function <name>(`" rule also mis-attributes the page's own dominant idiom: helpers are arrow consts, so `const redraw = () => { board.innerHTML = ... }` placed anywhere after a `build*` declaration passes the test and can be called from the poll. The only check that would actually catch this is the browser observation, which is not in the pytest gate.

**Touches:** AC-14, contract:test_innerhtml_lives_only_in_build_functions, task-9, task-13, file:ui.py  
**Found by:** page

**Evidence offered**

```
`ui.py:69-71` declare `const pill = s => ...` and `const esc = t => ...`; `colorize` is the only `function`-keyword helper. Plan Task 9 Test intents define the check; Task 9 Behaviour 1 and Task 12 Behaviour 1 both require build functions to be called from patch functions. Spec AC-14's own wording ("contains no `innerHTML` assignment inside the polling functions") is a third, different rule from the plan's, and the plan does not flag the difference. No task states a checkable constraint that a `build*` call is guarded by a "this key/row does not exist yet" test.
```

**Verdict 1 (facts) — refuted**

The gap's load-bearing claims are contradicted by plan text the hunter did not quote.

1. "Nothing forbids `poll()` or `patchBoard()` from calling `buildBoard()` unconditionally every 2 seconds" is false. Task 11 Constraints (plan line 810) says verbatim: "`buildBoard()` runs on selection only. Every section exists after it, hidden or not, so patches never create sections." Task 10 Behaviour 3 (line 742) puts the only `buildBoard()` call in the row click handler. Task 11 Interfaces (line 780) enumerates what `patchBoard` calls — `patchState`, `patchAwaiting`, `patchItems`, `patchRounds` — and `buildBoard` is not among them. Task 9 Behaviour 6 adds that the board clear happens "in the click handler, not in `poll()`".

2. "No task states a checkable constraint that a `build*` call is guarded by a 'this key/row does not exist yet' test" is false. Task 9 Interfaces: build functions run "once per element it creates". Task 9 Behaviour 1: `buildRoundCard(key)` "once per new key ... without moving existing cards", `buildLogPane` "once per role present". Task 10 Behaviour 1: "find the row by `data-id` or `buildRunRow` it ... without moving existing rows. Remove rows whose id is absent." Task 12 Behaviour 1: "One card per key, built once ... inserted without moving existing cards." That is exactly the does-not-exist-yet guard the gap says is missing, stated four times.

3. "Spec AC-14's own wording is a third, different rule from the plan's, and the plan does not flag the difference" misreads both. AC-14's automated half is "`ui.page_html()` contains no `innerHTML` assignment inside the polling functions". The plan's test intent (line ~880) has two clauses, and the second one — "the text of `runs`, `poll` and every `patch` function contains none" — is AC-14's rule verbatim in substance. The plan's check is a superset of the spec's, not a divergent rule.

4. "The only check that would actually catch this is the browser observation, which is not in the pytest gate" restates the design rather than exposing a hole. AC-14 says "Two checks" and names the browser one itself. The plan carries it as a Verify step in Task 10 (select text in a row, wait 12 seconds), Task 9 (select text in a pane, wait 6 seconds) and Task 13's four-step browser checklist. The pytest gate (AC-36) was never the whole of AC-14 in either document.

What survives is small and real: the literal "nearest preceding `function <name>(`" algorithm would not flag `const redraw = () => { el.innerHTML = ... }` sitting after a `build*` declaration, and the code claim about ui.py is accurate — line 69 `const pill = s => ...`, line 71 `const esc = t => ...` are arrow consts and `colorize` (line 72) is the only `function`-keyword helper. But Task 9's naming contract (line 662) states in words that "A function that assigns `innerHTML` is declared with the `function` keyword and its name starts with `build`", so an arrow const assigning innerHTML violates a stated contract; the plan carries test intents, not test bodies, and an implementer enforcing the stated contract writes a check that covers it. That residue is a sharpening note on one test intent, not an important gap, and the gap as written rests on three claims that the plan refutes outright.

_Opened:_ Opened, all absolute paths: /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md lines 80-120 (AC-11 through AC-18, AC-14 in full) and grepped AC-36/AC-37 at lines 219-220. /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md lines 600-927 in full — Task 8 tail, Task 9 (Interfaces naming contract, Behaviour 1-6, Constraints, Test intents including `test_innerhtml_lives_only_in_build_functions`), Task 10 (Behaviour 1-4, Constraints, Verify), Task 11 (Interfaces, Behaviour 1-4, Constraints, Verify), Task 12 (Interfaces, Behaviour 1-4, Constraints), Task 13 (Behaviour, Test intents, four-step browser Verify checklist) — plus a grep across the whole plan for `buildBoard|patchBoard|built once|once per|runs on selection` (hits at lines 647, 662, 674, 675, 742, 777, 780, 810, 843, 852, 898). /home/kevin/projects/loopgraph/ui.py lines 55-95 plus a grep for `innerHTML|function |const .*=>`: line 69 `const pill = s => ...`, line 71 `const esc = t => ...`, line 72 `function colorize(t)`, line 79 `async function runs()`, innerHTML assignments at lines 82, 86 and 109. Grepped /home/kevin/projects/loopgraph/tests/ for innerHTML — no existing test, as expected for unbuilt work.

_Severity corrected to:_ minor

---

### G8 — minor

**Claim.** Nothing says the board is built on the path every page load actually takes. Task 10 Behaviour 3 calls `buildBoard()` only from the row click handler, then adds "With nothing selected and rows present, select the first, as today" — and "as today" is `sel = d.runs[0].dir; poll();` with no board build. On first load nobody clicks, so `poll()` runs `patchBoard` against `#state`/`#why`/`#awaiting`/`#items`/`#rounds`/`#diff` that do not exist yet, which Task 11's constraint ("Every section exists after it, hidden or not, so patches never create sections") assumes cannot happen.

**Touches:** task-10, task-11, file:ui.py  
**Found by:** page

**Evidence offered**

```
`ui.py:91` `if (!sel && d.runs.length) { sel = d.runs[0].dir; poll(); }`. Plan Task 10 Behaviour 3; plan Task 11 Constraints and Interfaces. The ordering requirement — `buildBoard()` before the first `patchBoard()`, on both the click path and the auto-select path — is stated for one path only.
```

**Verdict 1 (facts) — refuted**

The code quotes check out, but the central claim — "nothing says the board is built on the path every page load actually takes" — is contradicted by a document the hunter did not open: the spec.

1. Spec AC-14 (line ~99) says, in plain words: "The board is built with `innerHTML` only on first render and when the reader selects another run." First render IS the page-load / auto-select path. So the requirement the hunter says is stated for one path only is stated for both, in the spec, in an acceptance criterion. Plan Task 11 lists "AC-14 (board)" under **Delivers**, so the implementer of the very task whose constraint the hunter says is unprotected is bound by that criterion.

2. The plan text is also weaker evidence than the claim needs. Task 10 Behaviour 3 is one paragraph headed "Selection is by id"; its first sentence defines what selecting does (set `sel`, move the class, `buildBoard()`, `poll()`), and its second sentence — "With nothing selected and rows present, select the first, as today" — says to perform that selection automatically. "as today" names the policy that survives (pick the top row when nothing is chosen), not a code path with the build removed. Reading it as "call `poll()` alone" requires ignoring the sentence it is attached to.

3. Task 11's own constraint points the same way: "`buildBoard()` runs on selection only." Auto-selecting the first run is a selection, so the constraint binds the board build to it rather than exempting it.

4. Task 11's **Verify** exercises the load path with no click described: `python -c "import ui; ui.serve(8401, None)"` on a second port "shows `temporal unreachable — logs only` with log cards and no state sections" — i.e. the board renders after auto-select. A build missing on that path fails the task's own verify step.

The underlying ordering concern is real enough that an implementer should keep it in mind, but a gap needs the documents to be silent, and they are not: the spec states the first-render build outright and the plan assigns it to Task 11. Refuted.

_Opened:_ Opened, all absolute paths:

/home/kevin/projects/loopgraph/ui.py — read lines 1-200 and re-read 85-115. Line 91 is exactly `if (!sel && d.runs.length) { sel = d.runs[0].dir; poll(); }` and today's `poll()` assigns `board.innerHTML` at line ~109, so the hunter's quotes are accurate and today's auto-select does work because `poll()` itself builds.

/home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md — read the header and Global constraints (lines 1-75), Task 9 (lines 644-714), Task 10 (715-763, Behaviour 3 at lines 741-743), Task 11 (764-830, Constraints at line 810, Delivers line listing "AC-14 (board)", Verify at 819-824), and grepped the whole file for select/buildBoard/first load/page load.

/home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md — grepped for select/first/load/board/section and read lines 90-110. AC-14: "The board is built with `innerHTML` only on first render and when the reader selects another run."

/home/kevin/projects/loopgraph/tests/test_ui.py — listed test names (5 tests; none bind the selection path either way).

---

### gap-conflict-kind-file:workflows/run.py — minor

**Claim.** Hunters disagree about what file:workflows/run.py is: artifact versus code. One of them is reading it wrong, and which one changes what binds to it.

**Touches:** file:workflows/run.py, file:ui.py, file:activities/stream.py  
**Found by:** cli, coverage, diff, page, workflow

**Evidence offered**

```
node file:workflows/run.py reported as kind=artifact by coverage and kind=code by workflow.
---
node file:ui.py reported as kind=artifact by coverage and kind=code by cli.
---
node file:workflows/run.py reported as kind=artifact by coverage and kind=code by cli.
---
node file:ui.py reported as kind=artifact by coverage and kind=code by diff.
---
node file:ui.py reported as kind=artifact by coverage and kind=code by page.
---
node file:workflows/run.py reported as kind=artifact by coverage and kind=code by page.
---
node file:activities/stream.py reported as kind=artifact by coverage and kind=code by page.
```

**Verdict 1 (facts) — refuted**

The "conflict" lives in the gap-hunting harness's own node labels, not in the spec, the plan, or the code. Neither document ever classifies a file as "artifact" or "code" — the word "artifact" appears zero times in both, and every occurrence of "kind" in either document refers to `awaiting.kind` (the decision-card type recorded in the ledger) or to `send_card`'s first parameter. So there is nothing in the cited documents that the claim can be a misreading of, and nothing that "binds" differently depending on which label a hunter attached.

All three named files exist and are treated consistently, as ordinary Python source, everywhere the two documents mention them:
- `workflows/run.py` (25,707 bytes, real Temporal workflow module): spec Scope line 15 and AC-33/AC-34 describe editing its `awaiting` dict and card strings; plan Task 3 lists it under "Modify". AGENTS.md's Layout section also calls it "Temporal workflows".
- `ui.py` (12,037 bytes, the stdlib dashboard): spec Scope line 11 ("the page, its endpoints, and the polling behaviour. Most of the work"); plan Tasks 6, 7, 8, 9, 10, 11, 12, 13 all list it under "Modify".
- `activities/stream.py`: never listed as modified anywhere; the plan touches it only as "Consumes: `LOG_RE` from `activities.stream` (already imported)" (Task 6), which matches `ui.py`'s actual `from activities.stream import LOG_RE` on line 21 and `LOG_RE = r"^(?:i(\d+)-)?r(\d+)-(executor|audit)\.log$"` in `activities/stream.py`.

There is no ambiguity for an implementer to trip over: for each file the plan says exactly whether it is created, modified, or only imported from. The claim also names no concrete consequence — "which one changes what binds to it" points at no acceptance criterion, task, or interface whose meaning would differ. This is harness metadata noise, not a defect in the spec or its plan.

_Opened:_ Opened in full: /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md (all 308 lines), /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md (all 928 lines), /home/kevin/projects/loopgraph/AGENTS.md, /home/kevin/projects/loopgraph/activities/stream.py (all 76 lines), and /home/kevin/projects/loopgraph/ui.py lines 1-40 (the docstring and import block, confirming `from activities.stream import LOG_RE`).

Directory listings confirming the three named nodes exist as real source files: /home/kevin/projects/loopgraph/workflows/run.py (25,707 bytes), /home/kevin/projects/loopgraph/ui.py (12,037 bytes), /home/kevin/projects/loopgraph/activities/stream.py (2,844 bytes).

Greps run over both documents: `grep -rn -i "artifact\|kind="` returned no lines at all (the word "artifact" is absent from both). `grep -rn "\bkind\b"` returned 10 lines, every one of them about `awaiting.kind` or the `send_card(kind, ...)` parameter — spec lines 50, 56, 153, 197, 214 and plan lines 207, 294, 789, 799, 821. `grep -rn "workflows/run.py\|activities/stream.py\|ui\.py"` over both documents returned 46 lines, all treating the files as Python source to modify or import from: spec Scope lines 11-15, spec AC-19/AC-33/AC-34, plan "Modify: `workflows/run.py`" (line 191), plan "Modify: `ui.py`" (lines 426, 494, 569, 653, 723, 772, 839, 894), plan "Consumes: `LOG_RE` from `activities.stream` (already imported)" (Task 6).

---

### gap-conflict-bidi-task-5-task-7 — important

**Claim.** task-5 and task-7 are each reported as contradicting the other, so at least one side of that pair is misread and the real contradiction is unresolved.

**Touches:** task-5, task-7  
**Found by:** merge

**Evidence offered**

```
plan:540 pins the 25-row cap and call(timeout=10) for the same list_workflows call that plan:376 leaves uncapped and untimed, while plan:322 requires the newest of all matches
```

**Verdict 1 (facts) — refuted**

The gap rests on the premise that plan:540 and plan:376 constrain "the same list_workflows call". They do not — they are two different call sites, in two different files, in two different processes, and the plan scopes each one by name.

plan:540 sits inside Task 7, whose Files block (plan:493-495) says "Modify: ui.py (TemporalFeed, make_server, do_GET)". The constraint reads: "Keep the 25-row cap in `_runs` and the `call(timeout=10)` bound; a poll must not hang the page." Both names it pins are ui.py-only and already exist there: `TemporalFeed.call(self, coro, timeout=10)` at ui.py:158 and the `if len(out) >= 25: break` at ui.py:181, inside `async def _runs` at ui.py:161. That call is the dashboard's repeating browser poll, and it fires a per-row `query("ledger")` (with a `handle.result()` fallback) for every workflow it walks — which is exactly why it caps at 25 and bounds the wait. Task 7 changes nothing about the cap; it says leave it alone.

plan:373-376 sits inside Task 5, whose Files block (plan:353-355) says "Modify: `lg` (`cmd_status`, the `status` argparse block)". It is a brand-new, one-shot lookup in the CLI: "list `WorkflowType = "LoopGraphRun"`, collect `(wf.id, wf.start_time)`, call `resolve_run_arg`". `lg` today has no `list_workflows` call at all (grep finds none; `cmd_status` at lg:84-95 only does `get_workflow_handle` + `query`), and it has its own `_client()`, which Task 5's Interfaces block lists as what it consumes. Task 5 never touches ui.py and never reads `TemporalFeed` — the only mention of `TemporalFeed` anywhere near these tasks is plan:320, a note explaining that the no-dash rule is the *inverse* of the id-slicing expression in `_runs`, not a code dependency.

The two calls also want opposite things for good reason. The CLI must see every `run-<slug>-<token>` match to pick the newest (AC-28, spec:179-181; AC-30, spec:185-187). Applying the dashboard's 25-row cap there would break that correctness. The dashboard must stay responsive on a 4-second poll while doing per-row ledger queries. Splitting them is deliberate, not contradictory.

The third citation is also misattributed. plan:322 ("Start times are compared with `>`; the CLI passes `WorkflowExecution.start_time`") is a constraint on **Task 4**, not Task 5 — Task 4 spans plan:267-344, Task 5 starts at plan:345. It is a tie-break rule for the pure function `resolve_run_arg`, which receives an already-collected list from its caller and has no listing behaviour of its own. It does not "require the newest of all matches" in any sense that conflicts with a cap somewhere else in a different file.

So neither side of the alleged pair says what the gap reports. There is no contradiction to resolve, and no unresolved residue: no plan text or spec AC asks the CLI's list to be capped or timed, and nothing asks the dashboard's poll to be uncapped. Refuted on the lens's own terms — the claim misreads both cited passages and misattributes the third to the wrong task.

_Opened:_ Read: /home/kevin/projects/loopgraph/docs/superpowers/plans/2026-09-05-ui-state-first.md — global constraints (28-73), Task 4 heading through end (267-344, incl. cited 315-324), Task 5 heading through behaviour+constraints (345-400, incl. cited 370-382), Task 7 heading through test intents (486-560, incl. cited 535-545); plus `grep -n "^### Task"` for exact task boundaries and `grep -n "TemporalFeed|_runs"` across the whole plan (hits at 13, 320, 488, 494, 502, 522-523, 532, 540, 573).
Read: /home/kevin/projects/loopgraph/docs/superpowers/specs/2026-09-05-ui-state-first-design.md — AC-27 through AC-31 (170-190), plus grep for "25|timeout|list_workflows|newest|cap" across the spec (no cap or timeout stated for the CLI path anywhere).
Read: /home/kevin/projects/loopgraph/ui.py:150-195 — `TemporalFeed.call(self, coro, timeout=10)` at line 158, `async def _runs` at 161 with `async for wf in self._client.list_workflows('WorkflowType = "LoopGraphRun"')` at 163, per-row `query("ledger")` at 167 with `result()` fallback at 173, and `if len(out) >= 25: break` at 181.
Read: /home/kevin/projects/loopgraph/lg:80-140 — `async def cmd_status` at line 84 (only `get_workflow_handle` + `query`, no listing); `grep -n "list_workflows" lg` returns nothing, confirming Task 5's list call is new and separate from ui.py's.

---

## Node and edge graph

121 nodes, 138 edges, merged across the seven hunters.

```json
{
 "nodes": [
  {
   "id": "AC-1",
   "label": "/api/run returns {ledger, temporal}",
   "kind": "ac",
   "facts": [
    "Ledger from the `ledger` query for a running workflow, from the workflow result for a closed one - the same fallback TemporalFeed._runs uses today",
    "Always HTTP 200; null ledger when Temporal is unreachable or the id is unknown",
    "`temporal` means the feed is connected",
    "The ledger comes from the ledger query for a running workflow and from the workflow's result for a closed one",
    "With Temporal unreachable, or an id Temporal does not know, ledger is null and the status is still HTTP 200, so the page works from log files alone"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:30-35"
   ],
   "foundBy": [
    "coverage",
    "endpoints"
   ]
  },
  {
   "id": "AC-2",
   "label": "id/dir/name validation, 400 on empty, / or ..",
   "kind": "ac",
   "facts": [
    "Applies to EVERY endpoint taking id, dir or name",
    "/api/log also 400s when offset is not a non-negative integer",
    "Every endpoint that takes id, dir or name answers 400 when the value is empty or contains / or ..",
    "/api/log also answers 400 when offset is not a non-negative integer"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:36-38"
   ],
   "foundBy": [
    "coverage",
    "endpoints"
   ]
  },
  {
   "id": "AC-3",
   "label": "run rows carry start_time and close_time",
   "kind": "ac",
   "facts": [
    "Read from WorkflowExecution.start_time and .close_time - both fields verified present in temporalio 1.32.0",
    "Logs-only row carries null for both",
    "Two workflows of one run directory are two rows; selection is by workflow id",
    "Read from WorkflowExecution.start_time and close_time; a logs-only run carries null for both",
    "Verified: temporalio 1.32 returns both as tz-aware UTC datetimes or None, so .isoformat() is genuinely UTC ISO 8601",
    "Two workflows of one run directory are two rows; selecting selects by workflow id",
    "Live proof: five run-example-hello-* workflows exist for one directory runs/example-hello"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:39-45",
    ".venv/lib/python3.13/site-packages/temporalio/client/_workflow.py:1312-1348",
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md AC-3",
    "live Temporal list_workflows output"
   ],
   "foundBy": [
    "coverage",
    "endpoints",
    "page"
   ]
  },
  {
   "id": "AC-8",
   "label": "rounds render newest first with verdict or status word",
   "kind": "ac",
   "facts": [
    "Names verdict, verdict_reasons, files, directive, owner_question/owner_reply",
    "Asserts: a round with no `verdict` key means its gates stayed red, so it shows its status word `escalated`",
    "owner_question with owner_reply shown when the round asked the owner",
    "Spec asserts the no-verdict case only happens when gates stayed red ('so no audit ran')",
    "Says the status word in that case is 'escalated'"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:66-70",
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:65-70",
    "specs/2026-09-05-ui-state-first-design.md AC-8"
   ],
   "foundBy": [
    "coverage",
    "workflow",
    "page"
   ]
  },
  {
   "id": "AC-11",
   "label": "/api/log offset slicing",
   "kind": "ac",
   "facts": [
    "Returns {text, offset, size}",
    "Head-truncation is signalled only by `offset > size`: reply starts at 0 so the page replaces instead of appending",
    "Returns {text, offset, size}; size is the file's current length, which the page sends as its next offset",
    "When the requested offset is past the current size (append_log head-truncated the file), the reply starts from 0 with the whole file",
    "A reply offset lower than the one requested is the only stated signal to replace the pane",
    "The replace signal is defined only for 'offset past the current size'"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:82-89",
    "specs/2026-09-05-ui-state-first-design.md AC-11"
   ],
   "foundBy": [
    "coverage",
    "endpoints",
    "page"
   ]
  },
  {
   "id": "AC-14",
   "label": "a poll replaces no DOM node",
   "kind": "ac",
   "facts": [
    "The interval functions runs() and poll() never assign innerHTML to an existing element",
    "Check: ui.page_html() contains no innerHTML assignment inside the polling functions",
    "Two checks: page_html() has no innerHTML assignment inside the polling functions; selection survives three polls in a browser",
    "The second check is a human observation, not part of the pytest gate"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:96-102",
    "specs/2026-09-05-ui-state-first-design.md AC-14"
   ],
   "foundBy": [
    "coverage",
    "page"
   ]
  },
  {
   "id": "AC-15",
   "label": "/api/diff returns stat, patch, truncated",
   "kind": "ac",
   "facts": [
    "git diff --stat <base_branch>...<branch> and git diff <base_branch>...<branch>, read-only in the host copy",
    "base_branch and branch come from ledger rounds[-1], never the request",
    "One diff per run",
    "Pins the commands verbatim: `git diff --stat <base_branch>...<branch>` and `git diff <base_branch>...<branch>` (three dots).",
    "base_branch and branch come from the last entry of rounds, never from the request.",
    "One diff per run, not per round."
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:106-112"
   ],
   "foundBy": [
    "coverage",
    "diff"
   ]
  },
  {
   "id": "AC-16",
   "label": "repo found via the worktree .git pointer",
   "kind": "ac",
   "facts": [
    "Container worktree path /app/runs/<slug>/worktrees/<token>",
    "Pointer holds `gitdir: /projects/<path>/.git/worktrees/<token>`",
    "Verified true on disk for all 5 run directories that still have a worktree",
    "worktree is `/app/runs/<slug>/worktrees/<token>`; the pointer file is `runs/<slug>/worktrees/<token>/.git`.",
    "Pointer holds one line `gitdir: /projects/<path>/.git/worktrees/<token>`.",
    "Repository = the part before `/.git/worktrees/` with `/projects/` replaced by `<LOOPGRAPH_PROJECTS_DIR>/` - the trailing slash is part of the spec text.",
    "Verified against live disk: five run dirs carry pointers of exactly this shape."
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:113-118",
    "runs/2026-09-05-deye-pending-restore/worktrees/ad4abd/.git",
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:114-118"
   ],
   "foundBy": [
    "coverage",
    "diff"
   ]
  },
  {
   "id": "AC-17",
   "label": "every diff failure is a 200 with a one-line reason",
   "kind": "ac",
   "facts": [
    "Enumerates 8 failure cases: no ledger, no rounds, non-container worktree, missing pointer, no gitdir line, projects dir unset, repo gone, branch not found",
    "Never a stack trace, never a 500",
    "Lists eight failure modes; the plan adds a ninth catch-all (`diff failed:`).",
    "Requires `stat` to hold a one-line explanation, `patch` empty, `truncated` false - the same shape a genuinely empty diff produces.",
    "Does not list an empty base_branch, nor a branch that is already merged, nor a subprocess that never returns."
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:119-123"
   ],
   "foundBy": [
    "coverage",
    "diff"
   ]
  },
  {
   "id": "AC-20",
   "label": "the location line on every card",
   "kind": "ac",
   "facts": [
    "`item 2 of 3 \u00b7 round 2` on a decision card; `item 2 of 3` between items and at the end",
    "The first line of the summary handed to send_card begins with the location line",
    "build_card_text's header lines stay first so wf_from_card keeps routing",
    "Decision card while an item runs: 'item 2 of 3 \u00b7 round 2'; cards between items and at the end: 'item 2 of 3' with no round",
    "The parked note names the parked item; the stopped note names the item it stopped on; the merge card names the last item",
    "The four build_card_text header lines stay first so wf_from_card keeps routing",
    "'item 2 of 3 \u00b7 round 2' on a decision card, 'item 2 of 3' on parked, stopped and merge-ready cards",
    "'item 3 of 3' when every item was parked",
    "build_card_text header lines stay first so wf_from_card keeps routing"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:138-145"
   ],
   "foundBy": [
    "coverage",
    "purefns",
    "workflow"
   ]
  },
  {
   "id": "AC-23",
   "label": "lg status prints a readable summary",
   "kind": "ac",
   "facts": [
    "Prints each round as item, round and verdict word, or its `status` word when it has no verdict",
    "Same escalated assumption as AC-8",
    "status and optional reason; awaiting as kind, question, one line per option, answer_with; items as number/status/commit-or-reason; rounds as item/round/verdict-word or status word",
    "A ledger whose awaiting has no question prints 'question not recorded'",
    "Says nothing about a no-card line - the plan adds one on its own initiative",
    "A ledger whose awaiting has no question prints 'question not recorded' in its place",
    "Summary is status, reason, awaiting block, items as number/status/commit-or-reason, rounds as item/round/verdict",
    "GateCheckRun and RoundRun, which answer the status query, print JSON as today"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:152-158"
   ],
   "foundBy": [
    "coverage",
    "purefns",
    "workflow",
    "cli"
   ]
  },
  {
   "id": "AC-32",
   "label": "every worker activity keeps its name and signature",
   "kind": "ac",
   "facts": [
    "send_card stays (kind, wf_id, run_dir, summary, commit, options, expect_reply=True)",
    "No activity added to or removed from worker.py",
    "No activity added to or removed from the worker"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:197-199",
    "activities/notify.py:91-93",
    "worker.py:49"
   ],
   "foundBy": [
    "coverage",
    "workflow"
   ]
  },
  {
   "id": "AC-33",
   "label": "LoopGraphRun schedules the same activities in the same order",
   "kind": "ac",
   "facts": [
    "_await_decision: telegram_configured then send_card with six args (confirmed workflows/run.py:393)",
    "_note: the same pair with seven (confirmed workflows/run.py:474)",
    "Proof is a read-only Replayer run over fetched history",
    "Check named: fetch a waiting workflow's history and replay it with temporalio.worker.Replayer against the new code",
    "Explicitly says 'The running stack is not restarted for this'",
    "Check: fetch the history of a workflow that is waiting on a card and replay it against the new code",
    "The running stack is not restarted for this",
    "Check named by the spec: fetch the history of a workflow that is waiting on a card and replay it with temporalio.worker.Replayer",
    "Live check on 2026-09-05: zero LoopGraphRun workflows are Running, so no workflow is waiting on a card"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:200-207",
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:199-207",
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:199-205",
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:204-207"
   ],
   "foundBy": [
    "coverage",
    "purefns",
    "workflow",
    "cli"
   ]
  },
  {
   "id": "AC-37",
   "label": "the dashboard stays read-only",
   "kind": "ac",
   "facts": [
    "do_GET only; the only git commands are rev-parse and diff",
    "GET only; no endpoint writes a file, signals a workflow, or runs a git command other than rev-parse and diff.",
    "Verified empirically: `git diff A...B` in a scratch repo left `.git/index` mtime untouched and took no lock, so the two named commands really are read-only against a repo the owner is using."
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:220-222"
   ],
   "foundBy": [
    "coverage",
    "diff"
   ]
  },
  {
   "id": "task-1",
   "label": "One .env reader for lg and ui.py",
   "kind": "task",
   "facts": [
    "Creates envfile.read_env(path); lg._dotenv keeps its name as a wrapper",
    "Delivers AC-19",
    "Verified: the installed lg shim execs <ROOT>/lg so sys.path[0] is the repo root, and the root conftest.py puts it on sys.path for tests",
    "Creates envfile.py with read_env(path: str | os.PathLike) -> dict[str, str]; rewires lg._dotenv() to return read_env(os.path.join(ROOT, '.env'))",
    "Its six behaviour rules match lg:27-47 exactly, including the quote-strip-before-comment-cut elif priority and the value strip that makes the quote rule survive the trailing newline",
    "Delivers line claims AC-19, but Files lists only envfile.py, lg and tests/test_envfile.py - ui.py is not touched until Task 8",
    "Constraint 'both run with the repo root on sys.path' checks out: ~/.local/bin/lg is a shell wrapper exec-ing $ROOT/.venv/bin/python $ROOT/lg (install.sh:245-249), and the empty root conftest.py puts the root on sys.path under pytest"
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md Task 1",
    "/home/kevin/.local/bin/lg",
    "conftest.py",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:74-128",
    "lg:25-47",
    "install.sh:245-249",
    "conftest.py (empty, repo root)"
   ],
   "foundBy": [
    "coverage",
    "purefns"
   ]
  },
  {
   "id": "task-2",
   "label": "The location line helper",
   "kind": "task",
   "facts": [
    "location_line(item_no, total, round_no=None) in activities/notify.py",
    "Delivers AC-21",
    "Pure, no activity decorator",
    "Adds location_line(item_no: int, total: int, round_no: int | None = None) -> str to activities/notify.py",
    "Pinned copy 'item 2 of 3 \u00b7 round 2' / 'item 2 of 3' uses U+00B7 in both plan and spec - byte-checked, they agree",
    "Constraint says build_card_text does not change; it does not need to (activities/notify.py:31-40 already puts summary after the four header lines)",
    "Test intents name build_card_text and wf_from_card, both already imported at tests/test_visibility.py:1-2",
    "Adds location_line to activities/notify.py, the module that also holds send_card",
    "Test intents cover location_line shapes and that a card whose summary starts with a location line still routes"
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md Task 2",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:132-178",
    "activities/notify.py:31-40",
    "tests/test_visibility.py:1-2"
   ],
   "foundBy": [
    "coverage",
    "purefns",
    "workflow"
   ]
  },
  {
   "id": "task-3",
   "label": "Workflow records its question and says where it is",
   "kind": "task",
   "facts": [
    "Delivers AC-5, AC-20, AC-22, AC-32, AC-33, AC-34, AC-35",
    "Adds \"question\": summary to the awaiting literal; prefixes card texts with location_line",
    "Replay verify script confirmed executable: 3 real histories replayed clean today, and a scratchpad copy carrying both changes also replayed clean",
    "Adds location_line to the existing `from activities.notify import send_card, telegram_configured` inside workflows/run.py's imports_passed_through() block",
    "Adds \"question\": summary to the awaiting literal; re-signs _ask_owner and _stopped_note",
    "Verify is pytest plus a Replayer script run in the host .venv",
    "Right now zero LoopGraphRun workflows are Running, so the script's documented fallback (drop the ExecutionStatus clause, replay the first three) is the path that will actually execute",
    "Only task touching workflows/run.py",
    "Files: workflows/run.py, tests/test_visibility.py only",
    "Four test intents, all source-text pins: question key, arg counts, bare owner_question, determinism grep",
    "Verify = pytest plus a heredoc Replayer script",
    "Delivers AC-5, AC-20, AC-22, AC-32, AC-33, AC-34, AC-35 (plan:188)",
    "Verify replays live histories with temporalio.worker.Replayer filtered to ExecutionStatus = \"Running\" (plan:241-256)",
    "Fallback instruction on zero running workflows: drop the ExecutionStatus clause and replay the first three (plan:259-261)"
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md Task 3",
    "workflows/run.py:388-396",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:182-263",
    "workflows/run.py:16-24",
    "workflows/run.py:388-389",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:186-263"
   ],
   "foundBy": [
    "coverage",
    "purefns",
    "workflow",
    "cli"
   ]
  },
  {
   "id": "task-4",
   "label": "format_status, run_slug, resolve_run_arg as pure functions",
   "kind": "task",
   "facts": [
    "Delivers AC-25, AC-31 and the formats AC-23/AC-28 name",
    "Copy block pins the summary layout; rounds print the verdict word or the status word",
    "Three pure functions added to lg; new tests in tests/test_lg_status.py loading lg by path as lg_cli_status",
    "The prefix rule (id starts run-<slug>-, remainder non-empty and holding no '-') is correct against every real workflow id: run-2026-09-05-microbits-order-v2-983d87 is not matched by slug 2026-09-05-microbits-order, and five run-example-hello-* ids exercise the newest-wins branch",
    "Test intents cover all six AC-31 cases and all five AC-25 ledger shapes",
    "Real ledgers match the assumed shape: items carry n/item/status/commit, rounds carry item_no/round/status/verdict/verdict_reasons",
    "format_status prints awaiting.question, or 'question not recorded' when the key is absent",
    "Delivers AC-25, AC-31 plus the formats AC-23 and AC-28 name (plan:272)",
    "Copy block pins '(none yet)' for empty items and empty rounds (plan:300-306)",
    "Behaviour 3: missing optional keys print nothing rather than None (plan:317)",
    "resolve_run_arg prefix rule: run-<slug>- with a non-empty remainder holding no '-' (plan:314-316)"
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md Task 4",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:267-341",
    "lg:203-209 (cmd_start id shape at lg:131)",
    "live Temporal ledger query on run-2026-09-05-microbits-order-v3-ae4a5b"
   ],
   "foundBy": [
    "coverage",
    "purefns",
    "workflow",
    "cli"
   ]
  },
  {
   "id": "task-5",
   "label": "lg status takes an id or a slug",
   "kind": "task",
   "facts": [
    "Delivers AC-23, AC-24, AC-26, AC-27, AC-28, AC-29, AC-30",
    "NOT_FOUND detection verified live: handle.query on an unknown id raises temporalio.service.RPCError with status 5",
    "Guards handle.result() behind handle.describe().status != RUNNING",
    "Its NOT_FOUND discrimination is correct against the live server: an unregistered query name on an existing workflow raises WorkflowQueryFailedError, a query on a missing id raises RPCError with RPCStatusCode.NOT_FOUND",
    "RPCError('...', RPCStatusCode.NOT_FOUND, b'') is a constructible signature in temporalio 1.32",
    "Constraint that README.md and skills/loopgraph/SKILL.md document `lg status <id> ledger` is accurate - all three doc mentions use the positional-query form, so the summary change breaks no shipped caller",
    "Delivers AC-23, AC-24, AC-26, AC-27, AC-28, AC-29, AC-30 (plan:350)",
    "Modifies lg (cmd_status + the status argparse block) and tests/test_lg_status.py (plan:352-354)",
    "Step 2 detects a non-existent id by temporalio.service.RPCError with status RPCStatusCode.NOT_FOUND (plan:371-374)",
    "Step 4 falls back to handle.result() only when describe().status is not RUNNING (plan:380-382)",
    "Step 5 routes output: --json or positional query -> json.dumps; ledger or result fallback -> format_status (plan:383)",
    "Step 6 pins stdout to the summary/JSON and every notice to stderr (plan:384)",
    "Constraint: handle.result() blocks until the workflow ends; without a describe() guard a running run whose ledger query failed would hang the terminal for as long as the run waits on its card",
    "Behaviour 4 requires describe().status != RUNNING before falling back to result()",
    "Constraint: handle.result() blocks until the workflow ends, so a describe() guard is required before falling back"
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md Task 5",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:345-414",
    "README.md:218,281",
    "skills/loopgraph/SKILL.md:159",
    "live probe: temporalio 1.32.0",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:377-387",
    "plans/2026-09-05-ui-state-first.md Task 5 Behaviour 4, Constraints"
   ],
   "foundBy": [
    "coverage",
    "purefns",
    "cli",
    "endpoints",
    "page"
   ]
  },
  {
   "id": "task-6",
   "label": "Validate params, list log names, serve log slices",
   "kind": "task",
   "facts": [
    "Delivers AC-2, AC-11, AC-12",
    "Adds bad_param, log_names, log_slice; removes log_tails and LOG_TAIL (used nowhere else)",
    "bad_param is applied only to dir and name here",
    "Removes log_tails and LOG_TAIL from ui.py; adds bad_param, log_names, log_slice",
    "Changes /api/logs from {name: text} to {\"logs\": [name,...]} and adds /api/log?dir=&name=&offset=",
    "Changes the 400 body from {\"logs\": {}} to {\"error\": \"<one line>\"} on every endpoint",
    "Names test_logs_endpoint as the one existing test it changes",
    "log_slice: if offset > size the slice starts at 0",
    "Test intent test_log_restarts_when_the_file_shrank covers only offset-past-size"
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md Task 6",
    "ui.py:124-129",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:418-482",
    "plans/2026-09-05-ui-state-first.md Task 6 Behaviour 4, Test intents"
   ],
   "foundBy": [
    "coverage",
    "endpoints",
    "page"
   ]
  },
  {
   "id": "task-7",
   "label": "/api/runs times, /api/run one ledger",
   "kind": "task",
   "facts": [
    "Delivers AC-1, AC-3",
    "Adds TemporalFeed.ledger, TemporalFeed.connected, run_entry, make_server(feed=)",
    "Keeps the 25-row cap and call(timeout=10) bound for _runs only",
    "Constraint keeps the 25-row cap in _runs and the call(timeout=10) bound so a poll cannot hang (plan:540)",
    "Adds TemporalFeed.ledger(wf_id), TemporalFeed.connected, run_entry(...), make_server(feed=...)",
    "Behaviour 1: query 'ledger'; on any failure try handle.result(); on failure return None; 'extracted so _runs and /api/run share it'",
    "Behaviour 4: /api/run returns {\"ledger\": feed.ledger(id) if feed else None, \"temporal\": bool(feed and feed.connected)}, always 200",
    "Names test_runs_endpoint_without_temporal as the one existing test it changes",
    "Produces TemporalFeed.ledger(wf_id) -> dict | None and make_server(..., feed=None); Task 8 consumes both under those exact names.",
    "TemporalFeed.ledger queries `ledger`, then falls back to handle.result(), then returns None - the fallback ui.py:159-167 already has.",
    "TemporalFeed.ledger: query 'ledger'; on any failure try handle.result(); on failure return None",
    "No describe() guard stated"
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md Task 7",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:486-557",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:486-559",
    "ui.py:158-168",
    "plans/2026-09-05-ui-state-first.md Task 7 Behaviour 1"
   ],
   "foundBy": [
    "coverage",
    "cli",
    "endpoints",
    "diff",
    "page"
   ]
  },
  {
   "id": "task-8",
   "label": "/api/diff",
   "kind": "task",
   "facts": [
    "Delivers AC-15, AC-16, AC-17, AC-18, AC-37",
    "resolve_repo + branch_diff + DIFF_CAP; nine verbatim reason strings",
    "Reads LOOPGRAPH_PROJECTS_DIR per request through read_env(ROOT / \".env\")",
    "Consumes read_env (Task 1) and calls read_env(ROOT / '.env') per request; ui.ROOT exists as a Path at ui.py:23, so the contract holds",
    "Delivers line names AC-15, AC-16, AC-17, AC-18, AC-37 - not AC-19",
    "Consumes bad_param (Task 6) and TemporalFeed.ledger + feed= (Task 7)",
    "Behaviour 1 applies bad_param(id) -> 400 but its Delivers line names AC-15, AC-16, AC-17, AC-18, AC-37 only",
    "Every diff test goes through a FakeFeed, so it never exercises the real TemporalFeed.ledger either",
    "Delivers AC-15, AC-16, AC-17, AC-18, AC-37.",
    "Produces DIFF_CAP = 204_800, resolve_repo(worktree, runs_dir, projects_dir) -> tuple[Path|None, str], branch_diff(repo, base_branch, branch) -> dict.",
    "Consumes read_env (Task 1), bad_param (Task 6), TemporalFeed.ledger and feed= (Task 7); all three names match those tasks' Produces blocks exactly.",
    "Nine verbatim reason lines, one per failure mode."
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md Task 8",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:562-640",
    "ui.py:23",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:560-640",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:561-640"
   ],
   "foundBy": [
    "coverage",
    "purefns",
    "endpoints",
    "diff"
   ]
  },
  {
   "id": "task-9",
   "label": "Log panes that append instead of re-render",
   "kind": "task",
   "facts": [
    "Delivers AC-9, AC-11, AC-12, AC-13, AC-14 (log panes)",
    "Establishes the build/patch JavaScript naming contract Tasks 10-13 bind to",
    "Rewrites poll(); does NOT rewrite runs()",
    "Binds to the /api/log offset protocol: if the reply offset is lower than the one sent, replaceChildren() then append the whole text; otherwise append",
    "A collapsed pane sends nothing; reopening resumes from the stored offset with its text intact",
    "Constraint claims chunk boundaries fall on line ends because append_log writes whole lines",
    "Introduces the innerHTML naming contract and test_innerhtml_lives_only_in_build_functions",
    "Explicitly defers the runs() rewrite: 'sel stays a directory string in this task; Task 10 turns it into {id, dir}'",
    "Pins the copy 'no logs yet for this run'",
    "Verify is pytest tests/test_ui.py && pytest -q plus four browser observations"
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md Task 9",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:644-711",
    "plans/2026-09-05-ui-state-first.md Task 9"
   ],
   "foundBy": [
    "coverage",
    "endpoints",
    "page"
   ]
  },
  {
   "id": "task-10",
   "label": "Run list patched in place, keyed by workflow id",
   "kind": "task",
   "facts": [
    "Delivers AC-3 (page side), AC-14 (run list)",
    "sel becomes {id, dir}; buildRunRow + patchRuns replace the innerHTML rebuild in runs()",
    "Behaviour 3: clicking a row calls buildBoard() then poll()",
    "Behaviour 3 also says 'With nothing selected and rows present, select the first, as today' with no buildBoard()",
    "Test intent asserts Task 9's innerHTML test is 'still green with runs() rewritten'"
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md Task 10",
    "plans/2026-09-05-ui-state-first.md Task 10 Behaviour 3, Test intents"
   ],
   "foundBy": [
    "coverage",
    "page"
   ]
  },
  {
   "id": "task-11",
   "label": "State board: status, why-no-state, awaiting, items",
   "kind": "task",
   "facts": [
    "Delivers AC-4, AC-6, AC-7, AC-10, AC-14 (board)",
    "buildBoard() creates #state #why #awaiting #items #rounds #diff once on selection",
    "patchAwaiting hides the question element when the key is absent",
    "Prints question as recorded, location line included",
    "patchState: with ledger null, #why shows 'temporal unreachable \u2014 logs only' when temporal is false and 'no workflow for this run' when true",
    "Verify uses `import ui; ui.serve(8401, None)` \u2014 temporal_addr=None, so feed is None, the guarded path, never the connect-failed path",
    "poll() fetches /api/run?id= and /api/logs?dir= together every 2000 ms and calls patchBoard(run, names)",
    "patchItems: rows keyed by n; empty items shows 'no items yet'",
    "buildBoard() runs on selection only; every section exists after it"
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md Task 11",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:764-827",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:764-828",
    "plans/2026-09-05-ui-state-first.md Task 11 Interfaces, Behaviour 1-3, Constraints"
   ],
   "foundBy": [
    "coverage",
    "workflow",
    "endpoints",
    "page"
   ]
  },
  {
   "id": "task-12",
   "label": "Rounds from the ledger with their log panes",
   "kind": "task",
   "facts": [
    "Delivers AC-8, AC-9, AC-10, AC-14 (rounds)",
    "Adds the ` \u00b7 in progress` card for a round with logs but NO ledger entry",
    "Re-keys patchRounds to <item_no>-<round>, changing Task 9's signature",
    "Renders owner_question and owner_reply on a round card",
    "A ledger round is keyed item_no-round; a log name is keyed i-r with a missing item group meaning item 1",
    "A round with no verdict key shows its status word (escalated)",
    "' \u00b7 in progress' is appended while a round has logs but no ledger entry",
    "Pins the copy 'no rounds yet'"
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md Task 12",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:831-882",
    "plans/2026-09-05-ui-state-first.md Task 12 Behaviour 1-4, Copy"
   ],
   "foundBy": [
    "coverage",
    "workflow",
    "page"
   ]
  },
  {
   "id": "task-13",
   "label": "The diff pane and the whole-branch check",
   "kind": "task",
   "facts": [
    "Delivers AC-9, AC-14, AC-15, AC-18, AC-36, AC-37",
    "Shows stat, then patch in a pre, then the cut line; Task 8 reasons arrive in stat and are shown as is",
    "Opening #diff fetches /api/diff?id=<sel.id> and shows `stat`, then `patch` in a <pre>, then the cut line when truncated.",
    "Constraint: \"The `stat` text is also where the Task 8 reasons arrive; show it as is. No special-casing.\"",
    "So whatever /api/diff puts in `stat` is the entire explanation the reader gets.",
    "buildDiffPane(id) fetches on each toggle-to-open, never polled",
    "Owns the four-item browser checklist that is the only real enforcement of AC-14"
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md Task 13",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:886-935",
    "plans/2026-09-05-ui-state-first.md Task 13"
   ],
   "foundBy": [
    "coverage",
    "diff",
    "page"
   ]
  },
  {
   "id": "fn:TemporalFeed.ledger",
   "label": "TemporalFeed.ledger(wf_id)",
   "kind": "contract",
   "facts": [
    "Plan: query `ledger`; on any failure try handle.result(); on failure return None",
    "No RUNNING guard and no stated timeout bound",
    "handle.result() long-polls until the workflow ends",
    "Declared as a sync method in Task 7's Interfaces block",
    "Behaviour: query 'ledger'; on any failure try handle.result(); on failure return None",
    "No guard for self._client being None, unlike the sibling runs()",
    "No guard on running vs closed before calling result(), unlike Task 5's cmd_status"
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md Task 7 Behaviour 1",
    "ui.py:161-183",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:502-506",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:522-524"
   ],
   "foundBy": [
    "coverage",
    "endpoints"
   ]
  },
  {
   "id": "fn:TemporalFeed.connected",
   "label": "TemporalFeed.connected property",
   "kind": "contract",
   "facts": [
    "bool(self._client); replaces the handler's read of feed._client",
    "Not in the spec; Task 7 states why"
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md Task 7",
    "ui.py:240"
   ],
   "foundBy": [
    "coverage"
   ]
  },
  {
   "id": "fn:log_slice",
   "label": "log_slice(path, offset) -> {text, offset, size}",
   "kind": "contract",
   "facts": [
    "Restarts at 0 only when offset > size",
    "Decodes with errors=replace",
    "size is the current file length; if offset > size the slice starts at 0",
    "text is bytes[offset:size] decoded with errors='replace'"
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md Task 6",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:454-458"
   ],
   "foundBy": [
    "coverage",
    "endpoints"
   ]
  },
  {
   "id": "fn:append_log",
   "label": "append_log head truncation",
   "kind": "code",
   "facts": [
    "Caps a log at LOG_CAP = 1,000,000 bytes",
    "On overflow rewrites the file as `[... head truncated ...]\\n` plus the LAST 500,000 bytes, so size DROPS to ~500 KB but stays above many stored offsets",
    "Appends the line, then, when st_size > LOG_CAP (1,000,000), rewrites the whole file as b'[... head truncated ...]\\n' + last LOG_CAP//2 bytes",
    "Measured: a 1,000,091-byte file becomes 500,025 bytes in one step \u2014 the file loses half its content, it is not capped at 1 MB",
    "The tail cut is at an arbitrary byte, so the first line after the marker is a partial line"
   ],
   "sources": [
    "activities/stream.py:16,45-51",
    "activities/stream.py:45-51",
    "activities/stream.py:16"
   ],
   "foundBy": [
    "coverage",
    "endpoints"
   ]
  },
  {
   "id": "fn:format_status",
   "label": "format_status(ledger) -> str",
   "kind": "contract",
   "facts": [
    "Prints each round as `item 1 round 1 accept`, or the round's status word when it has no verdict",
    "Test intent covers only the `escalated` case",
    "format_status(ledger: dict) -> str; the plan's copy block is the only specification of its output",
    "Copy block ends 'Sections are separated by one blank line. Output ends with a newline.'",
    "The no-card line 'no card was sent; the lg approve command is the only way to answer' is duplicated verbatim in Task 11's page copy",
    "New pure function in lg produced by Task 4",
    "Consumed by Task 5 on both the ledger-query result and the result() fallback"
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md Task 4",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:283-325",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:786-792",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:283,383"
   ],
   "foundBy": [
    "coverage",
    "purefns",
    "cli"
   ]
  },
  {
   "id": "fn:run_slug",
   "label": "run_slug(arg) -> str",
   "kind": "contract",
   "facts": [
    "Last non-empty / segment",
    "Agrees between Task 4 (produces) and Task 5 (consumes)",
    "Last non-empty '/'-separated segment; behaviour for an argument with no non-empty segment is unstated"
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md Task 4-5",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:284,312"
   ],
   "foundBy": [
    "coverage",
    "cli"
   ]
  },
  {
   "id": "fn:resolve_run_arg",
   "label": "resolve_run_arg(arg, candidates) -> (id|None, count)",
   "kind": "contract",
   "facts": [
    "prefix run-<slug>-, remainder non-empty and holding no `-`",
    "Inverse of wf.id[4:wf.id.rfind('-')] in ui.TemporalFeed._runs - verified: real ids are run-<slug>-<6 hex>",
    "resolve_run_arg(arg: str, candidates: list[tuple[str, datetime]]) -> tuple[str | None, int]",
    "lg imports no datetime; only `from __future__ import annotations` at lg:9 keeps the annotation from raising",
    "Start times compared with '>'",
    "Needs every candidate to pick the newest, so the Temporal listing behind it cannot be capped"
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md Task 4",
    "ui.py:164",
    "lg:133",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:285",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:322",
    "lg:9-19",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:285,313-316,322"
   ],
   "foundBy": [
    "coverage",
    "purefns",
    "cli"
   ]
  },
  {
   "id": "fn:location_line",
   "label": "location_line(item_no, total, round_no=None)",
   "kind": "contract",
   "facts": [
    "Separator is space, U+00B7, space",
    "Signature agrees across Task 2 (produces) and Task 3 (all three call shapes)",
    "New pure function, no decorator, added to the pure-helpers section of a module the worker already has loaded",
    "activities/notify.py is in worker.py's top-level import list, so a running worker holds a stale module object for it",
    "Lives in activities/notify.py, imported into workflow code inside imports_passed_through()"
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md Task 2-3",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:146-162",
    "activities/notify.py:29-40",
    "worker.py:17-18"
   ],
   "foundBy": [
    "coverage",
    "purefns",
    "workflow"
   ]
  },
  {
   "id": "fn:read_env",
   "label": "envfile.read_env(path)",
   "kind": "contract",
   "facts": [
    "Quote-strip branch wins over the inline-comment cut, matching lg._dotenv today",
    "Called with a str from lg and a Path from ui.py; both accepted by the declared signature",
    "read_env(path: str | os.PathLike) -> dict[str, str]; returns {} for an absent file",
    "Called by lg._dotenv() with a str and by ui.py's diff handler with a Path - both forms are accepted by open()"
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md Task 1",
    "lg:25-47",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:90-96",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:611"
   ],
   "foundBy": [
    "coverage",
    "purefns"
   ]
  },
  {
   "id": "fn:resolve_repo",
   "label": "resolve_repo(worktree, runs_dir, projects_dir)",
   "kind": "contract",
   "facts": [
    "Walks /app/runs/<slug>/worktrees/<token>/.git -> gitdir line -> repo path with /projects/ swapped for LOOPGRAPH_PROJECTS_DIR",
    "Verified end to end today on three live run directories",
    "Plan: worktree must start with `/app/runs/`; pointer at runs_dir / <rel> / '.git'; gitdir line parsed; `/projects/` replaced by `projects_dir`.",
    "AC-16 says `/projects/` is replaced by `<LOOPGRAPH_PROJECTS_DIR>/`; the plan drops the trailing slash.",
    "Deviation from AC-16 on runs_dir vs engine root is stated with a reason; the /app/ vs /app/runs/ narrowing is not."
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md Task 8",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:603-607",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:615-617"
   ],
   "foundBy": [
    "coverage",
    "diff"
   ]
  },
  {
   "id": "fn:_park_note",
   "label": "LoopGraphRun._park_note",
   "kind": "code",
   "facts": [
    "Its text ALREADY begins `item {item_no} of {total} parked`",
    "Signature already carries item_no and total",
    "Today's text already begins f\"item {item_no} of {total} parked\\n\\n...\" - the exact string location_line(item_no, total) + \" parked\" produces",
    "Already takes item_no and total",
    "Its first line today is exactly location_line(item_no, total) + ' parked'"
   ],
   "sources": [
    "workflows/run.py:429-438"
   ],
   "foundBy": [
    "coverage",
    "purefns",
    "workflow"
   ]
  },
  {
   "id": "fn:_run_item",
   "label": "LoopGraphRun._run_item",
   "kind": "code",
   "facts": [
    "Appends the round entry to the ledger BEFORE the audit activity runs (line 245)",
    "Sets entry['verdict'] only after audit returns (line 258); audit has a 30-minute start_to_close timeout",
    "Round entry status comes from execute_round: green | escalated"
   ],
   "sources": [
    "workflows/run.py:229-259",
    "graphs/round_graph.py:84-85"
   ],
   "foundBy": [
    "coverage"
   ]
  },
  {
   "id": "fn:merge_branch",
   "label": "activities.checkpoint.merge_branch",
   "kind": "code",
   "facts": [
    "Checks out base_branch in the target repo and merges the run branch into it",
    "Does not delete the branch",
    "`git merge --no-ff -m 'loopgraph: merge <branch> (owner-approved)'` into base_branch; never pushes; the branch is kept, not deleted.",
    "After that merge, merge-base(base, branch) == branch tip, so the three-dot diff AC-15 pins is empty.",
    "Guards `if not base_branch: return {'merged': False, 'reason': 'unknown base branch (detached HEAD at round start)'}` - proof that base_branch can be ''."
   ],
   "sources": [
    "activities/checkpoint.py:112-135",
    "activities/checkpoint.py:108-135"
   ],
   "foundBy": [
    "coverage",
    "diff"
   ]
  },
  {
   "id": "fn:runs",
   "label": "page JavaScript runs() interval function",
   "kind": "code",
   "facts": [
    "Today assigns innerHTML twice: el.innerHTML = '' and div.innerHTML = `...`",
    "Rewritten in Task 10, not in Task 9"
   ],
   "sources": [
    "ui.py:79-94"
   ],
   "foundBy": [
    "coverage"
   ]
  },
  {
   "id": "fn:poll",
   "label": "page JavaScript poll() interval function",
   "kind": "code",
   "facts": [
    "Today rebuilds #board with innerHTML from a name->text dict",
    "Rewritten in Task 9"
   ],
   "sources": [
    "ui.py:95-117"
   ],
   "foundBy": [
    "coverage"
   ]
  },
  {
   "id": "endpoint:/api/run",
   "label": "GET /api/run?id=<wf>",
   "kind": "contract",
   "facts": [
    "New in Task 7; polled every 2000 ms from Task 11 onward",
    "Always 200; {ledger, temporal}",
    "New in Task 7; {\"ledger\": object|null, \"temporal\": bool}, always 200",
    "Polled by the board every 2 seconds (Task 11)",
    "Polled every 2 seconds by poll() once a run is selected (Task 11)"
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md Task 7, Task 11",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:517-518",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:530-531",
    "plans/2026-09-05-ui-state-first.md Task 7, Task 11"
   ],
   "foundBy": [
    "coverage",
    "endpoints",
    "page"
   ]
  },
  {
   "id": "endpoint:/api/diff",
   "label": "GET /api/diff?id=<wf>",
   "kind": "contract",
   "facts": [
    "Fetched only when the pane opens; never polled",
    "Nine verbatim reason strings, all 200",
    "Response has exactly three fields; failure reasons are carried in `stat`, with `patch` empty and `truncated` false (spec AC-17, plan lines 586-598).",
    "Keyed on workflow id only; `worktree`, `branch`, `base_branch` are read from `rounds[-1]` of the ledger, never from the query string (plan line 601-602).",
    "Does not exist in ui.py today: ui.py's do_GET handles only `/`, `/api/logs`, `/api/runs` (ui.py:219-244)."
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md Task 8, Task 13",
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:106-125",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:561-640",
    "ui.py:219-244"
   ],
   "foundBy": [
    "coverage",
    "diff"
   ]
  },
  {
   "id": "endpoint:/api/log",
   "label": "GET /api/log?dir=&name=&offset=",
   "kind": "contract",
   "facts": [
    "400 on a name outside LOG_RE or a non-digit offset; 404 for a matching name that names no file",
    "New in Task 6; 200 {text, offset, size}, 400 on a bad dir/name/offset, 404 when the name matches LOG_RE but no file exists",
    "No content cap; first open of a pane can return the whole file",
    "The page appends text and stores size as its next offset"
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md Task 6",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:440-464",
    "plans/2026-09-05-ui-state-first.md Task 6, Task 9 Behaviour 3"
   ],
   "foundBy": [
    "coverage",
    "endpoints",
    "page"
   ]
  },
  {
   "id": "endpoint:/api/logs",
   "label": "GET /api/logs?dir=<slug>",
   "kind": "contract",
   "facts": [
    "Shape changes from {name: text} to {logs: [name,...]} in Task 6",
    "The served page cannot render logs between Task 6 and Task 9 - stated in Global constraints",
    "Today returns {name: tail-of-60000-bytes}; Task 6 makes it {\"logs\": [name, ...]}",
    "Today's 400 body is {\"logs\": {}} (ui.py:232)"
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md Task 6, Global constraints",
    "ui.py:229-233",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:440"
   ],
   "foundBy": [
    "coverage",
    "endpoints"
   ]
  },
  {
   "id": "file:ui.py",
   "label": "ui.py dashboard",
   "kind": "artifact",
   "facts": [
    "258 lines; stdlib ThreadingHTTPServer, one PAGE string, do_GET only",
    "make_server(port, runs_dir, temporal_addr) today; gains feed= in Task 7",
    "TemporalFeed.call bounds a coroutine with a 10-second future timeout but does not cancel it",
    "TemporalFeed._runs treats a falsy workflow status as running: wf.status.name.lower() if wf.status else 'running'",
    "_runs caps output at 25 rows and every Temporal call is bounded by call(coro, timeout=10)",
    "_runs already carries the ledger-query -> result() fallback that AC-26 copies",
    "make_server constructs TemporalFeed(temporal_addr) whenever temporal_addr is truthy, and the default is 'localhost:7233'",
    "Measured: with Temporal down the feed object is truthy and _client stays None",
    "do_GET is the only do_* method; the page is one PAGE string with __LOG_RE__ injected",
    "Runs on the host only: install.sh:347 and README.md:220 both say `lg ui`; no compose service serves it, so ROOT is the engine checkout and host paths are correct.",
    "make_server(port, runs_dir, temporal_addr) today has no `feed=` parameter and no subprocess import; Task 7 adds feed=, Task 8 adds git.",
    "ROOT = Path(__file__).resolve().parent, so read_env(ROOT / '.env') resolves to the real .env at request time and nothing is hardcoded.",
    "runs() assigns innerHTML twice: el.innerHTML='' (line 82) and div.innerHTML=`...` (line 86)",
    "Helper functions are arrow consts: const pill (line 69), const esc (line 71)",
    "Auto-select on first load: line 91 sets sel and calls poll() with no board build",
    "poll() renders board.innerHTML and the copy 'no logs yet for this run' (lines 109-115)",
    "TemporalFeed.call times the waiter out at 10s via concurrent Future.result(timeout); the coroutine is never cancelled (lines 158-159)",
    "_runs falls back from query('ledger') to handle.result() with no describe() guard (lines 167-174)"
   ],
   "sources": [
    "ui.py:1-258",
    "ui.py:158-191",
    "ui.py:208-209",
    "ui.py:140-156",
    "ui.py:220-242",
    "ui.py:22-24",
    "ui.py:205-244",
    "install.sh:347",
    "README.md:220",
    "/home/kevin/projects/loopgraph/ui.py"
   ],
   "foundBy": [
    "coverage",
    "cli",
    "endpoints",
    "diff",
    "page"
   ]
  },
  {
   "id": "file:workflows/run.py",
   "label": "workflows/run.py",
   "kind": "artifact",
   "facts": [
    "527 lines, deterministic, imports activities inside workflow.unsafe.imports_passed_through()",
    "All 13 LoopGraphRun workflows in Temporal are COMPLETED as of this check - none is currently holding an owner card",
    "527 lines; no `open(`, no `import os`, no `import random`, no datetime.now, no time.time today",
    "_await_decision writes awaiting at 388-389 and schedules send_card with six args at 391-396",
    "_note schedules send_card with seven args at 472-477",
    "_park_note text already begins 'item {item_no} of {total} parked' at 433",
    "_stopped_note(run_dir, reason) is called at 174 and 191",
    "ledger query returns self._ledger at 525-527, so the dict is rebuilt by replay on every cache miss",
    "LoopGraphRun exposes only a ledger query; there is no status query, so the status/ledger order in cmd_status still reaches the ledger",
    "run() returns self._ledger, so the result() fallback yields a real ledger for recent runs",
    "Replaying all 13 live histories against today's unmodified code: 7 pass, 6 fail with [TMPRL1100] Nondeterminism error",
    "Ledger init creates items: [] and rounds: [] (line 122)",
    "items list is filled at lines 155-156 after load_work_items returns",
    "_run_item appends the round entry at line 245, then awaits audit with a 30-minute start_to_close_timeout (lines 250-254), and only sets entry['verdict'] at line 258",
    "A non-green round returns at lines 246-248 before any verdict is written"
   ],
   "sources": [
    "workflows/run.py:1-527",
    "workflows/run.py:112-196",
    "workflows/run.py:525-527",
    "/home/kevin/projects/loopgraph/workflows/run.py"
   ],
   "foundBy": [
    "coverage",
    "workflow",
    "cli",
    "page"
   ]
  },
  {
   "id": "file:activities/stream.py",
   "label": "activities/stream.py",
   "kind": "artifact",
   "facts": [
    "Owns LOG_RE, LOG_GLOB, log_name, append_log",
    "LOG_RE = ^(?:i(\\d+)-)?r(\\d+)-(executor|audit)\\.log$",
    "Owns LOG_CAP, LOG_GLOB, LOG_RE, log_name and append_log",
    "Every executor/supervisor line reaches disk through append_log",
    "LOG_RE = ^(?:i(\\d+)-)?r(\\d+)-(executor|audit)\\.log$ (line 23): the item group is optional",
    "append_log head-truncates past LOG_CAP=1_000_000 by rewriting the file as a 25-byte marker plus the last 500,000 bytes (lines 50-51), which shifts every byte position while leaving the file about 500 KB"
   ],
   "sources": [
    "activities/stream.py:16-51",
    "activities/stream.py:1-51",
    "/home/kevin/projects/loopgraph/activities/stream.py"
   ],
   "foundBy": [
    "coverage",
    "endpoints",
    "page"
   ]
  },
  {
   "id": "decision:read-only",
   "label": "Read-only stays read-only",
   "kind": "decision",
   "facts": [
    "Honoured by the Global constraints, AC-37, Task 8 and Task 13",
    "Matches the AGENTS.md standing rule that an answer reaches a run one way only",
    "Owner-rejected a POST endpoint; AGENTS.md forbids a second way into a run.",
    "The only test the plan gives for it is a source scan of ui.py (do_ methods and git subcommand strings), not a behavioural check against a repository."
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:252-254",
    "AGENTS.md:33-38",
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:252-253",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:634-635"
   ],
   "foundBy": [
    "coverage",
    "diff"
   ]
  },
  {
   "id": "decision:one-diff-per-run",
   "label": "One diff per run, not per round",
   "kind": "decision",
   "facts": [
    "Honoured: Task 12 constraint keeps the diff out of round cards, Task 13 puts it in #diff",
    "Grounded: execute_round derives one branch and one worktree per run token"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:281-283",
    "activities/execute_round.py:226-230"
   ],
   "foundBy": [
    "coverage"
   ]
  },
  {
   "id": "decision:diff-via-worktree-pointer",
   "label": "The diff is found through the worktree .git pointer",
   "kind": "decision",
   "facts": [
    "Honoured by Task 8 resolve_repo",
    "Verified end to end today: pointer -> /home/kevin/projects/deye -> git diff --stat produced 13 files changed"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:261-263"
   ],
   "foundBy": [
    "coverage"
   ]
  },
  {
   "id": "AC-19",
   "label": "AC-19: one shared .env reader for lg and ui.py",
   "kind": "ac",
   "facts": [
    "Two halves: the reader keeps lg._dotenv's behaviour and keeps its name, AND LOOPGRAPH_PROJECTS_DIR reaches ui.py through it",
    "Names three tests that must pass unchanged: test_lg_reads_env_the_way_compose_does, both test_lg_where_*; all three exist and monkeypatch lg.ROOT then call _dotenv()"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:128-134",
    "tests/test_review_fixes.py:630-637",
    "tests/test_release.py:150-175"
   ],
   "foundBy": [
    "purefns"
   ]
  },
  {
   "id": "AC-21",
   "label": "AC-21: a pure location-line function in activities/notify.py",
   "kind": "ac",
   "facts": [
    "Pins the module: activities/notify.py, not a new module",
    "Says tests/test_visibility.py tests both shapes and build_card_text itself does not change",
    "build_card_text itself does not change"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:146-148"
   ],
   "foundBy": [
    "purefns",
    "workflow"
   ]
  },
  {
   "id": "AC-5",
   "label": "AC-5: awaiting.question is the card's summary verbatim",
   "kind": "ac",
   "facts": [
    "Its value is the exact string passed as summary to send_card for that card",
    "Nothing in ui.py or lg reconstructs a question from another field",
    "Recorded in the same statement as kind, options, telegram, answer_with",
    "Nothing reconstructs the question from another field"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:55-58"
   ],
   "foundBy": [
    "purefns",
    "workflow"
   ]
  },
  {
   "id": "fn:_dotenv",
   "label": "lg._dotenv",
   "kind": "code",
   "facts": [
    "Reads os.path.join(ROOT, '.env') at call time; three existing tests monkeypatch lg.ROOT then call it",
    "Quote-strip branch is an elif ahead of the ' #' comment cut - the plan preserves the priority and says why"
   ],
   "sources": [
    "lg:25-47"
   ],
   "foundBy": [
    "purefns"
   ]
  },
  {
   "id": "fn:build_card_text",
   "label": "activities.notify.build_card_text",
   "kind": "code",
   "facts": [
    "Writes four header lines, then a blank, then summary.strip()[:1500], then options; whole message capped at 4000",
    "So the string the owner actually saw is a truncation of the summary that AC-5 records verbatim"
   ],
   "sources": [
    "activities/notify.py:31-40"
   ],
   "foundBy": [
    "purefns"
   ]
  },
  {
   "id": "file:docker-compose.yml",
   "label": "docker-compose.yml",
   "kind": "artifact",
   "facts": [
    "worker service mounts `- ./:/app` and carries `restart: unless-stopped`",
    "dispatcher service mounts `- ./:/app` too",
    "Dockerfile copies only pyproject.toml, so the container's source IS the host working tree, live"
   ],
   "sources": [
    "docker-compose.yml:59-64",
    "docker-compose.yml:85-88",
    "Dockerfile:9"
   ],
   "foundBy": [
    "purefns"
   ]
  },
  {
   "id": "file:worker.py",
   "label": "worker.py",
   "kind": "code",
   "facts": [
    "Imports send_card and telegram_configured from activities.notify at module level, so the process caches that module at startup",
    "Worker(...) passes no workflow_runner, so the default SandboxedWorkflowRunner is used with passthrough_all_modules=False",
    "A worker is polling the loopgraph task queue right now (one poller, identity 1@omarchyos)",
    "Registers 12 activities including send_card and telegram_configured",
    "Loads workflow code at process start, so a workflows/run.py change is inert until the worker restarts",
    "Connects with os.environ TEMPORAL_ADDRESS defaulting to localhost:7233"
   ],
   "sources": [
    "worker.py:17-20",
    "worker.py:45-53",
    "live DescribeTaskQueue on queue 'loopgraph'",
    "worker.py:11-55"
   ],
   "foundBy": [
    "purefns",
    "workflow"
   ]
  },
  {
   "id": "decision:no-worker-restart",
   "label": "Replay is proven with the Replayer, not a worker restart",
   "kind": "decision",
   "facts": [
    "Spec: AGENTS.md forbids restarting the stack while runs hold cards; exported history is read-only",
    "Spec non-goal: 'A worker restart replays workflow code, so a run holding a card from before the change may need answering or terminating by hand'",
    "Both statements assume new code only reaches the worker on restart"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:244-245",
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:234-236",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:42-47"
   ],
   "foundBy": [
    "purefns"
   ]
  },
  {
   "id": "decision:one-shared-env-reader",
   "label": "One shared .env reader",
   "kind": "decision",
   "facts": [
    "Alternative rejected: a second copy of the parser inside ui.py, because the existing reader carries a quote-stripping fix and two copies drift apart"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:258-260"
   ],
   "foundBy": [
    "purefns"
   ]
  },
  {
   "id": "file:skills/loopgraph/SKILL.md",
   "label": "skills/loopgraph/SKILL.md",
   "kind": "artifact",
   "facts": [
    "The agent-facing surface for running the engine from other projects",
    "Only mention of status is `lg status <workflow-id> ledger` for the scoreboard - no slug form, no --json",
    "Line 159 tells agents 'lg status <workflow-id> ledger' and nothing about a slug or --json",
    "No task in the plan updates it"
   ],
   "sources": [
    "skills/loopgraph/SKILL.md:159"
   ],
   "foundBy": [
    "purefns",
    "cli"
   ]
  },
  {
   "id": "AC-22",
   "label": "owner_question and record_owner_answer keep the bare question",
   "kind": "ac",
   "facts": [
    "owner-answers.md reads as it does today"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:149-151"
   ],
   "foundBy": [
    "workflow"
   ]
  },
  {
   "id": "AC-34",
   "label": "workflows/run.py reads no clock, env or random source and does no I/O",
   "kind": "ac",
   "facts": [
    "Import list unchanged",
    "Plan states a reasoned deviation: the notify import gains location_line"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:206-210",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:220-224"
   ],
   "foundBy": [
    "workflow"
   ]
  },
  {
   "id": "AC-35",
   "label": "Every ledger key keeps its name; question is the only key added",
   "kind": "ac",
   "facts": [
    "rounds[] list: item_no, round, status, verdict, verdict_reasons, files, directive, worktree, branch, base_branch",
    "owner_question and owner_reply are not in the list"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:211-215"
   ],
   "foundBy": [
    "workflow"
   ]
  },
  {
   "id": "AC-4",
   "label": "The awaiting block on the page",
   "kind": "ac",
   "facts": [
    "When the block has no question key (a workflow that started before this change) the page shows everything else and leaves the question out",
    "Missing question key: show everything else, leave the question out"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:49-54",
    "specs/2026-09-05-ui-state-first-design.md AC-4"
   ],
   "foundBy": [
    "workflow",
    "page"
   ]
  },
  {
   "id": "file:activities/notify.py",
   "label": "Card builders and the send_card / telegram_configured activities",
   "kind": "code",
   "facts": [
    "build_card_text writes loopgraph:/run:/workflow:/commit: headers first, then summary.strip()[:1500], capped at 4000",
    "send_card signature is (kind, wf_id, run_dir, summary, commit, options, expect_reply=True)",
    "Module imports os and httpx at top level"
   ],
   "sources": [
    "activities/notify.py:31-114"
   ],
   "foundBy": [
    "workflow"
   ]
  },
  {
   "id": "file:activities/owner.py",
   "label": "record_owner_answer and owner-answers.md",
   "kind": "code",
   "facts": [
    "record_owner_answer(run_dir, question, reply, options, item_no, round_no) - six parameters",
    "format_answer collapses whitespace and caps the question at 500 chars, so a leaked location line would show inline in owner-answers.md"
   ],
   "sources": [
    "activities/owner.py:35-75"
   ],
   "foundBy": [
    "workflow"
   ]
  },
  {
   "id": "file:AGENTS.md",
   "label": "Project rules for agents",
   "kind": "artifact",
   "facts": [
    "Contains: answers arrive as signals; supervisor sees only assemble_audit_prompt; workflow code must be deterministic; container paths; no hardcoded home; nothing under runs/ committed",
    "Contains NO rule about restarting the worker, the stack, or docker compose",
    "grep -i 'restart|docker compose|stack' returns nothing"
   ],
   "sources": [
    "AGENTS.md:1-79"
   ],
   "foundBy": [
    "workflow"
   ]
  },
  {
   "id": "fn:_await_decision",
   "label": "LoopGraphRun._await_decision",
   "kind": "code",
   "facts": [
    "Builds the awaiting dict, then schedules telegram_configured, then send_card only when telegram is true",
    "Loops back to wait_condition after a 'not an answer' note, leaving awaiting in place"
   ],
   "sources": [
    "workflows/run.py:364-422"
   ],
   "foundBy": [
    "workflow"
   ]
  },
  {
   "id": "fn:_ask_owner",
   "label": "LoopGraphRun._ask_owner",
   "kind": "code",
   "facts": [
    "Today (run_dir, question, options); plan adds item_no, total, round_no",
    "One call site, workflows/run.py:304"
   ],
   "sources": [
    "workflows/run.py:424-427",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:200-201"
   ],
   "foundBy": [
    "workflow"
   ]
  },
  {
   "id": "fn:_stopped_note",
   "label": "LoopGraphRun._stopped_note",
   "kind": "code",
   "facts": [
    "Today (run_dir, reason); plan adds item_no, total",
    "Two call sites: the halt branch at 174 and the all-parked branch at 191"
   ],
   "sources": [
    "workflows/run.py:440-456",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:202"
   ],
   "foundBy": [
    "workflow"
   ]
  },
  {
   "id": "fn:_owner_card",
   "label": "LoopGraphRun._owner_card",
   "kind": "code",
   "facts": [
    "Builds the merge summary with build_merge_summary and calls _await_decision with letters A/B/C",
    "Two existing tests pin its source text by splitting on 'elif letter == \"C\"'"
   ],
   "sources": [
    "workflows/run.py:479-523",
    "tests/test_review_fixes.py:861-932"
   ],
   "foundBy": [
    "workflow"
   ]
  },
  {
   "id": "fn:send_card",
   "label": "send_card activity",
   "kind": "contract",
   "facts": [
    "(kind, wf_id, run_dir, summary, commit, options, expect_reply=True)",
    "Registered on the worker; changing it is the change the spec names as most likely to strand a waiting run"
   ],
   "sources": [
    "activities/notify.py:90-93",
    "worker.py:51"
   ],
   "foundBy": [
    "workflow"
   ]
  },
  {
   "id": "fn:wf_from_card",
   "label": "wf_from_card - reads the workflow id back out of a card",
   "kind": "code",
   "facts": [
    "Regex ^workflow:\\s*(\\S+)\\s*$ with MULTILINE, searched over the whole card text",
    "Verified: a card whose summary starts 'item 2 of 3 \u00b7 round 2' still routes correctly, for both a quoted text reply and a merge card with buttons"
   ],
   "sources": [
    "activities/route.py:18-25"
   ],
   "foundBy": [
    "workflow"
   ]
  },
  {
   "id": "fn:record_owner_answer",
   "label": "record_owner_answer activity",
   "kind": "contract",
   "facts": [
    "Six positional args from workflows/run.py:313; unchanged by the plan"
   ],
   "sources": [
    "activities/owner.py:70-75",
    "workflows/run.py:311-316"
   ],
   "foundBy": [
    "workflow"
   ]
  },
  {
   "id": "contract:awaiting.question",
   "label": "The new awaiting.question ledger key",
   "kind": "contract",
   "facts": [
    "Written inside _await_decision, so it is recomputed by replay rather than stored",
    "Read by lg format_status (Task 4) and patchAwaiting (Task 11)"
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:204-207",
    "workflows/run.py:388-389"
   ],
   "foundBy": [
    "workflow"
   ]
  },
  {
   "id": "artifact:replay-script",
   "label": "The Replayer heredoc in Task 3's Verify",
   "kind": "artifact",
   "facts": [
    "Lists WorkflowType = \"LoopGraphRun\" AND ExecutionStatus = \"Running\"",
    "Hardcodes localhost:7233",
    "Constructs Replayer inside the loop",
    "Fallback on zero results is prose, not a command"
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:239-261"
   ],
   "foundBy": [
    "workflow"
   ]
  },
  {
   "id": "decision:replay-not-restart",
   "label": "Replay is proven with the replayer, not a worker restart",
   "kind": "decision",
   "facts": [
    "Spec: 'AGENTS.md forbids restarting the stack while runs hold cards; exported history is read-only'",
    "Spec non-goal repeats 'Runs hold cards right now, and AGENTS.md forbids it'",
    "AGENTS.md forbids restarting the stack while runs hold cards; exported history is read-only",
    "Rests on there being a waiting workflow to replay"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:244-245",
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:297-298"
   ],
   "foundBy": [
    "workflow",
    "cli"
   ]
  },
  {
   "id": "decision:location-line-in-card-text",
   "label": "The location line travels inside the card's text, not as a new send_card argument",
   "kind": "decision",
   "facts": [
    "Rationale: changing an activity's argument list is the change most likely to strand a run already waiting when the worker restarts with new code"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:254-257"
   ],
   "foundBy": [
    "workflow"
   ]
  },
  {
   "id": "AC-24",
   "label": "--json and the positional query both still print raw JSON",
   "kind": "ac",
   "facts": [
    "lg status <id> ledger is documented in README.md and the skill and must keep printing json.dumps(result, indent=2)"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:159-162"
   ],
   "foundBy": [
    "cli"
   ]
  },
  {
   "id": "AC-25",
   "label": "The summary is a pure function tested over five ledger shapes",
   "kind": "ac",
   "facts": [
    "Named shapes: full ledger; awaiting without a question; no awaiting; empty items and rounds; a round with no verdict",
    "No shape covers a ledger that lacks items or rounds entirely"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:163-166"
   ],
   "foundBy": [
    "cli"
   ]
  },
  {
   "id": "AC-26",
   "label": "Closed-run ledger-query failure falls back to the workflow result",
   "kind": "ac",
   "facts": [
    "Mirrors the fallback TemporalFeed._runs already carries",
    "When both fail, print the error on stderr and exit 1"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:167-169"
   ],
   "foundBy": [
    "cli"
   ]
  },
  {
   "id": "AC-27",
   "label": "The argument is a workflow id first, slug only on NOT_FOUND",
   "kind": "ac",
   "facts": [
    "Verified against the live server: a query on an unknown id raises RPCError with RPCStatusCode.NOT_FOUND and message 'workflow not found for ID: ...'",
    "The plan names this exception correctly"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:170-173"
   ],
   "foundBy": [
    "cli"
   ]
  },
  {
   "id": "AC-28",
   "label": "Slug resolution, last path segment, prefix run-<slug>- with a token holding no dash",
   "kind": "ac",
   "facts": [
    "Verified against all 13 live LoopGraphRun ids: every slug resolves to its own workflows only",
    "2026-09-05-microbits-order-v2 does not match run-2026-09-05-microbits-order-v3-ae4a5b",
    "Among several matches the newest by WorkflowExecution.start_time wins"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:174-180"
   ],
   "foundBy": [
    "cli"
   ]
  },
  {
   "id": "AC-29",
   "label": "A slug that matches nothing prints one stderr line and exits 1, no traceback",
   "kind": "ac",
   "facts": [
    "Copy: 'no workflow for <slug>; looked for ids starting run-<slug>-'",
    "Plan Task 5 reproduces this line verbatim (plan:364)"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:182-184"
   ],
   "foundBy": [
    "cli"
   ]
  },
  {
   "id": "AC-30",
   "label": "Several matches use the newest and say so on stderr in both modes",
   "kind": "ac",
   "facts": [
    "Copy: 'using run-<slug>-ab12cd (newest of 2 for <slug>)'",
    "Live data already has a 5-way match: five run-example-hello-* workflows"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:185-187"
   ],
   "foundBy": [
    "cli"
   ]
  },
  {
   "id": "AC-31",
   "label": "Resolution is a pure function over (id, start_time) pairs",
   "kind": "ac",
   "facts": [
    "temporalio 1.32 WorkflowExecution.start_time is a non-optional datetime, so the '>' comparison in the plan is safe"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:188-192"
   ],
   "foundBy": [
    "cli"
   ]
  },
  {
   "id": "fn:cmd_status",
   "label": "cmd_status in lg",
   "kind": "code",
   "facts": [
    "Today: queries = [args.query] if args.query else ['status','ledger']; prints json.dumps of the first answer; re-raises on the last failure",
    "Reads args.workflow_id and args.query only"
   ],
   "sources": [
    "lg:84-95"
   ],
   "foundBy": [
    "cli"
   ]
  },
  {
   "id": "file:lg",
   "label": "lg, the host CLI",
   "kind": "code",
   "facts": [
    "Invoked through a generated shell wrapper: exec <root>/.venv/bin/python <root>/lg \"$@\", so sys.path[0] is the repo root",
    "Module docstring still says 'lg status <workflow-id> queries gate red/green'",
    "status subparser help: 'query a workflow (tries status, then ledger)'; positional is named workflow_id",
    "Imports only Client from temporalio.client"
   ],
   "sources": [
    "lg:1-22",
    "lg:217-220",
    "~/.local/bin/lg",
    "install.sh:245-249"
   ],
   "foundBy": [
    "cli"
   ]
  },
  {
   "id": "file:README.md",
   "label": "README.md",
   "kind": "artifact",
   "facts": [
    "Documents only 'lg status <workflow-id> ledger' at lines 218 and 281",
    "No task in the plan updates it"
   ],
   "sources": [
    "README.md:218,281"
   ],
   "foundBy": [
    "cli"
   ]
  },
  {
   "id": "decision:slug-after-not-found",
   "label": "Slug resolution runs only after Temporal says the id does not exist",
   "kind": "decision",
   "facts": [
    "Auto-resolved in the spec; rejected the alternative of deciding by the run- prefix shape",
    "Verified implementable: an unknown id raises RPCError/NOT_FOUND from handle.query"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:270-271"
   ],
   "foundBy": [
    "cli"
   ]
  },
  {
   "id": "decision:result-fallback-on-closed",
   "label": "lg status falls back to the workflow result on a closed run",
   "kind": "decision",
   "facts": [
    "Auto-resolved; the reason given is that the ledger query can fail on closed runs",
    "Live: 6 of 13 closed LoopGraphRun workflows fail the ledger query with a nondeterminism error, so this is the majority path for historical runs, not an edge case"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:272-274"
   ],
   "foundBy": [
    "cli"
   ]
  },
  {
   "id": "AC-9",
   "label": "Log and diff panes collapsed by default; a collapsed pane sends no request",
   "kind": "ac",
   "facts": [
    "An open log pane polls /api/log with its offset every 2 seconds and stops when closed",
    "Implies a pane keeps an offset across a close/reopen cycle",
    "A collapsed pane sends no request; the diff is fetched on open and never polled"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:72-74",
    "specs/2026-09-05-ui-state-first-design.md AC-9"
   ],
   "foundBy": [
    "endpoints",
    "page"
   ]
  },
  {
   "id": "AC-10",
   "label": "Null ledger still shows log panes plus one reason line",
   "kind": "ac",
   "facts": [
    "Requires /api/run to answer with a null ledger, not to fail, when Temporal is unreachable",
    "Reason lines: 'temporal unreachable \u2014 logs only' or 'no workflow for this run'",
    "Copy: 'temporal unreachable \u2014 logs only' or 'no workflow for this run'"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:75-78",
    "specs/2026-09-05-ui-state-first-design.md AC-10"
   ],
   "foundBy": [
    "endpoints",
    "page"
   ]
  },
  {
   "id": "AC-12",
   "label": "/api/logs returns sorted names only",
   "kind": "ac",
   "facts": [
    "{\"logs\": [name, ...]}, no content",
    "The page groups names with the LOG_RE that page_html() injects; test_logs_endpoint changes to assert names only"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:90-93"
   ],
   "foundBy": [
    "endpoints"
   ]
  },
  {
   "id": "endpoint:/api/runs",
   "label": "GET /api/runs",
   "kind": "contract",
   "facts": [
    "Shipped and working today; rows are {id, dir, state, detail} plus logs-only rows",
    "Task 7 adds start_time and close_time and swaps feed._client for feed.connected",
    "temporal is bool(feed and feed._client) today (ui.py:240)"
   ],
   "sources": [
    "ui.py:234-240",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:516-517"
   ],
   "foundBy": [
    "endpoints"
   ]
  },
  {
   "id": "fn:TemporalFeed.runs",
   "label": "TemporalFeed.runs() (shipped)",
   "kind": "code",
   "facts": [
    "Guards the disconnected case explicitly: `if not self._client: return []`",
    "Wraps the whole thing in call(timeout=10) and swallows every exception"
   ],
   "sources": [
    "ui.py:185-191"
   ],
   "foundBy": [
    "endpoints"
   ]
  },
  {
   "id": "fn:TemporalFeed._runs",
   "label": "TemporalFeed._runs() (shipped, async, runs on the feed's loop thread)",
   "kind": "code",
   "facts": [
    "Holds the query->result fallback Task 7 wants to extract",
    "Caps at 25 rows",
    "Derives dir as wf.id[4:wf.id.rfind('-')]"
   ],
   "sources": [
    "ui.py:161-183"
   ],
   "foundBy": [
    "endpoints"
   ]
  },
  {
   "id": "fn:TemporalFeed.call",
   "label": "TemporalFeed.call(coro, timeout=10)",
   "kind": "code",
   "facts": [
    "asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)",
    "Measured: called from inside the loop thread it blocks the loop and raises TimeoutError; the coroutine never runs",
    "A TimeoutError does not cancel the submitted coroutine, so a pending handle.result() long-poll stays alive on the loop"
   ],
   "sources": [
    "ui.py:158-159"
   ],
   "foundBy": [
    "endpoints"
   ]
  },
  {
   "id": "fn:log_names",
   "label": "log_names(runs_dir, slug) -> list[str] (planned)",
   "kind": "contract",
   "facts": [
    "Specified as 'the *.log file names under runs_dir/<slug>/logs, sorted'",
    "Task 6's Interfaces consume only LOG_RE from activities.stream, not LOG_GLOB"
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:431-437",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:450-451"
   ],
   "foundBy": [
    "endpoints"
   ]
  },
  {
   "id": "fn:bad_param",
   "label": "bad_param(value) -> bool (planned)",
   "kind": "contract",
   "facts": [
    "True when empty, or containing / or ..",
    "Task 6 applies it to dir and name only; 'Task 7 and Task 8 apply it to id'"
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:435",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:447-448"
   ],
   "foundBy": [
    "endpoints"
   ]
  },
  {
   "id": "fn:run_entry",
   "label": "run_entry(wf_id, status, start_time, close_time, ledger) -> dict (planned)",
   "kind": "contract",
   "facts": [
    "Builds one /api/runs row; state and detail from the ledger as today",
    "Behaviour 3 gives logs-only rows detail 'logs only', which no ledger can supply, so the handler must still build those rows itself; the plan does not say which"
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:508-509",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:526-529"
   ],
   "foundBy": [
    "endpoints"
   ]
  },
  {
   "id": "fn:log_tails",
   "label": "log_tails(runs_dir, slug) (shipped, removed by Task 6)",
   "kind": "code",
   "facts": [
    "Reads each *.log and returns the last LOG_TAIL = 60,000 bytes as text",
    "So today no single /api/logs reply carries more than 60 KB per file"
   ],
   "sources": [
    "ui.py:124-129",
    "ui.py:24"
   ],
   "foundBy": [
    "endpoints"
   ]
  },
  {
   "id": "code:LOG_GLOB",
   "label": "LOG_GLOB / LOG_RE in activities/stream.py",
   "kind": "code",
   "facts": [
    "Declared as 'One source of truth for a run's log filenames ... Anything that reads these files uses these'",
    "The comment records the regression: three places hardcoded the shape, the item number joined the name, and both lg tail and the dashboard silently found no logs",
    "test_the_glob_and_the_regex_agree exists to hold the two together"
   ],
   "sources": [
    "activities/stream.py:18-23",
    "tests/test_review_fixes.py:104-117"
   ],
   "foundBy": [
    "endpoints"
   ]
  },
  {
   "id": "code:test_logs_endpoint",
   "label": "tests/test_ui.py::test_logs_endpoint",
   "kind": "code",
   "facts": [
    "Today asserts 'hello' in d['logs'][name] and re.match(LOG_RE, name)",
    "Task 6 tells it to assert d['logs'] == [log_name(1, 1, 'executor')] and that the name matches LOG_RE \u2014 matches the fixture, which writes exactly that one file"
   ],
   "sources": [
    "tests/test_ui.py:34-42",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:466-468"
   ],
   "foundBy": [
    "endpoints"
   ]
  },
  {
   "id": "code:test_runs_endpoint_without_temporal",
   "label": "tests/test_ui.py::test_runs_endpoint_without_temporal",
   "kind": "code",
   "facts": [
    "Today asserts temporal is False, dir == '2026-01-01-demo', state == 'unknown'",
    "Task 7 tells it to keep those three and add start_time is None, close_time is None, id == dir",
    "Its fixture passes temporal_addr=None, so feed is None \u2014 it exercises the guarded path only"
   ],
   "sources": [
    "tests/test_ui.py:50-55",
    "tests/test_ui.py:19",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:545-546"
   ],
   "foundBy": [
    "endpoints"
   ]
  },
  {
   "id": "code:test_logs_reject_traversal",
   "label": "tests/test_ui.py::test_logs_reject_traversal",
   "kind": "code",
   "facts": [
    "urlopen('/api/logs?dir=../..') must raise; still passes with the {\"error\": ...} body since urlopen raises on any 400"
   ],
   "sources": [
    "tests/test_ui.py:45-47"
   ],
   "foundBy": [
    "endpoints"
   ]
  },
  {
   "id": "decision:endpoints-key-on-workflow-id",
   "label": "Endpoints key on the workflow id, not the run directory",
   "kind": "decision",
   "facts": [
    "Auto-resolved in the spec: /api/run and /api/diff take id; logs still go by dir because both workflows of a directory write the same log files",
    "Checked across the plan: architecture (l.12-13), global constraints (l.50-52), Task 6 (l.447), Task 7 (l.517), Task 10 (l.728), Task 11 (l.780), Task 13 (l.905) \u2014 consistent everywhere, never inverted",
    "The one deliberate blur is stated: a logs-only row gets id == dir (Task 7 behaviour 3)"
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:276-279",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:50-52"
   ],
   "foundBy": [
    "endpoints"
   ]
  },
  {
   "id": "AC-18",
   "label": "AC-18: patch capped at 200 KB",
   "kind": "ac",
   "facts": [
    "\"capped at 200 KB (204,800 bytes). Past that it is cut and `truncated` is true.\" - the unit is bytes.",
    "Plan step 3 cuts only after both git commands have run to completion.",
    "Plan test intent asserts `len(patch) <= 204800`, which on a decoded str counts characters."
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:124-125",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:608-610",
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:632"
   ],
   "foundBy": [
    "diff"
   ]
  },
  {
   "id": "fn:branch_diff",
   "label": "branch_diff(repo, base_branch, branch) -> {stat, patch, truncated}",
   "kind": "contract",
   "facts": [
    "`git rev-parse --verify` each branch, then two `git diff` runs with cwd=repo.",
    "No subprocess timeout stated, no bound on how much git may emit before the cut, no cap on `stat`.",
    "Repo precedent it does not follow: activities/gate.py:97-124 (\"The timeout has to govern the PROCESS, not the pipe\") and activities/stream.py:16 LOG_CAP."
   ],
   "sources": [
    "docs/superpowers/plans/2026-09-05-ui-state-first.md:608-610",
    "activities/gate.py:97-134",
    "activities/stream.py:16"
   ],
   "foundBy": [
    "diff"
   ]
  },
  {
   "id": "fn:execute_round",
   "label": "execute_round - where worktree, branch and base_branch are born",
   "kind": "code",
   "facts": [
    "worktree = str(run / 'worktrees' / token) where run_dir is the container path, so `/app/runs/<slug>/worktrees/<token>` (execute_round.py:226-228).",
    "branch = f'lg-{run.name}-{run_token}'; base_branch = `git branch --show-current` in the target repo, which is the empty string on a detached HEAD (execute_round.py:229).",
    "All three land on every round entry, appended before the audit runs, so they exist even for a round that never produced a commit (workflows/run.py:229-245)."
   ],
   "sources": [
    "activities/execute_round.py:220-230",
    "workflows/run.py:229-245"
   ],
   "foundBy": [
    "diff"
   ]
  },
  {
   "id": "fn:discard",
   "label": "discard - the C card's cleanup",
   "kind": "code",
   "facts": [
    "`git worktree remove --force`, then `git worktree prune`, then `git branch -D`; the first two swallow RuntimeError with `pass`.",
    "On this machine the branch and the repo's .git/worktrees admin dir are gone for microbits-opportunities while three engine-side pointer files survive."
   ],
   "sources": [
    "activities/checkpoint.py:143-169"
   ],
   "foundBy": [
    "diff"
   ]
  },
  {
   "id": "decision:patch-cap-200kb",
   "label": "decision: the patch cap is 200 KB",
   "kind": "decision",
   "facts": [
    "Stated rationale: \"large enough for any real review, small enough that one request cannot stall the page\".",
    "The plan's step 3 satisfies the first half and not the second."
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:300"
   ],
   "foundBy": [
    "diff"
   ]
  },
  {
   "id": "decision:diff-through-worktree-pointer",
   "label": "decision: the diff is found through the worktree's .git pointer",
   "kind": "decision",
   "facts": [
    "Chosen over recording the host repo path in the ledger, \"because the ledger holds container paths by design, and the pointer file is already on disk and already names the project\".",
    "Holds up against live disk; the container-to-host hop is the only mapping and it comes from .env."
   ],
   "sources": [
    "docs/superpowers/specs/2026-09-05-ui-state-first-design.md:262-264"
   ],
   "foundBy": [
    "diff"
   ]
  },
  {
   "id": "file:.env",
   "label": ".env - LOOPGRAPH_PROJECTS_DIR",
   "kind": "artifact",
   "facts": [
    "Live value on this machine: `LOOPGRAPH_PROJECTS_DIR=/home/kevin/projects` - no trailing slash.",
    ".env.example ships `LOOPGRAPH_PROJECTS_DIR=/home/you/projects` - also no trailing slash.",
    "install.sh:87 takes the value from a prompt defaulting to $HOME/projects and does no trailing-slash normalisation.",
    "docker-compose.yml mounts ${LOOPGRAPH_PROJECTS_DIR}:/projects, which is the mapping AC-16 inverts."
   ],
   "sources": [
    ".env:12",
    ".env.example:23",
    "install.sh:82-89",
    "docker-compose.yml:66-68"
   ],
   "foundBy": [
    "diff"
   ]
  },
  {
   "id": "artifact:worktree-pointer",
   "label": "runs/<slug>/worktrees/<token>/.git pointer file",
   "kind": "artifact",
   "facts": [
    "Five live pointers, every one of the shape `gitdir: /projects/<repo>/.git/worktrees/<token>`, exactly as AC-16 says.",
    "runs/2026-09-05-microbits-{order-v3,running-order,sequence} still hold pointers into /projects/microbits-opportunities, whose `.git/worktrees/` directory no longer exists and whose lg-* branches are all gone."
   ],
   "sources": [
    "runs/2026-09-05-deye-pending-restore/worktrees/ad4abd/.git",
    "runs/2026-09-05-microbits-sequence/worktrees/d5d29c/.git"
   ],
   "foundBy": [
    "diff"
   ]
  },
  {
   "id": "AC-7",
   "label": "items list with n, item, status, commit[:10], reason; 'no items yet' when empty",
   "kind": "ac",
   "facts": [
    "Only the empty-list case is specified; the absent-key case is not"
   ],
   "sources": [
    "specs/2026-09-05-ui-state-first-design.md AC-7"
   ],
   "foundBy": [
    "page"
   ]
  },
  {
   "id": "artifact:ledger-microbits-fact-corrections",
   "label": "Live ledger of run-2026-09-05-microbits-fact-corrections-36437d",
   "kind": "artifact",
   "facts": [
    "Fetched read-only from the live Temporal on 2026-09-05 via handle.result()",
    "Top-level keys: checkpoint, learn, merge, owner_decision, rounds, status \u2014 there is NO items key at all",
    "Its one round entry has keys attempts, base_branch, branch, claims, directive, files, round, status, verdict, verdict_reasons, worktree \u2014 there is NO item_no key",
    "Its run directory runs/2026-09-05-microbits-fact-corrections/logs holds old-shape names r1-executor.log and r1-audit.log"
   ],
   "sources": [
    "live Temporal query, 2026-09-05",
    "runs/2026-09-05-microbits-fact-corrections/logs/"
   ],
   "foundBy": [
    "page"
   ]
  },
  {
   "id": "code:test_the_old_log_names_still_render",
   "label": "Test pinning that pre-queue log names must still render",
   "kind": "code",
   "facts": [
    "Asserts LOG_RE matches r1-executor.log, 'runs from before the queue must still show'"
   ],
   "sources": [
    "tests/test_review_fixes.py:120"
   ],
   "foundBy": [
    "page"
   ]
  },
  {
   "id": "contract:test_innerhtml_lives_only_in_build_functions",
   "label": "The automated half of AC-14",
   "kind": "contract",
   "facts": [
    "For every innerHTML in ui.page_html(), the nearest preceding 'function <name>(' must start with build",
    "The text of runs, poll and every patch function must contain none"
   ],
   "sources": [
    "plans/2026-09-05-ui-state-first.md Task 9 Test intents"
   ],
   "foundBy": [
    "page"
   ]
  }
 ],
 "edges": [
  {
   "from": "task-7",
   "to": "AC-1",
   "relation": "delivers",
   "evidence": "Task 7 Delivers line names AC-1; behaviour 4 gives the {ledger, temporal} shape and the always-200 rule\n---\nplan l.492 Delivers: AC-1, AC-3"
  },
  {
   "from": "task-7",
   "to": "AC-3",
   "relation": "delivers",
   "evidence": "Task 7 run_entry formats start_time/close_time; both fields confirmed on temporalio 1.32.0 WorkflowExecution\n---\nplan l.492; run_entry carries start_time/close_time (l.508-509, l.526-529)"
  },
  {
   "from": "task-6",
   "to": "AC-2",
   "relation": "delivers",
   "evidence": "Task 6 Delivers names AC-2 and defines bad_param, but its behaviour 1 applies it only to dir and name\n---\nplan l.424 Delivers: AC-2, AC-11, AC-12 \u2014 but its behaviour list covers dir and name only (l.447-448)"
  },
  {
   "from": "task-7",
   "to": "fn:TemporalFeed.ledger",
   "relation": "delivers",
   "evidence": "Task 7 Interfaces declares def ledger(self, wf_id) -> dict | None"
  },
  {
   "from": "task-8",
   "to": "fn:TemporalFeed.ledger",
   "relation": "binds_to",
   "evidence": "Task 8 Interfaces: 'Consumes: TemporalFeed.ledger and feed= (Task 7)'; behaviour 1 calls feed.ledger(id)"
  },
  {
   "from": "endpoint:/api/run",
   "to": "fn:TemporalFeed.ledger",
   "relation": "binds_to",
   "evidence": "Task 7 behaviour 4: {\"ledger\": feed.ledger(id) if feed else None}"
  },
  {
   "from": "fn:TemporalFeed.ledger",
   "to": "task-5",
   "relation": "contradicts",
   "evidence": "Task 5 Constraints require a describe() guard before handle.result() because it blocks; Task 7 behaviour 1 calls handle.result() with no guard on an endpoint polled every 2 s"
  },
  {
   "from": "task-9",
   "to": "AC-14",
   "relation": "delivers",
   "evidence": "Task 9 Delivers AC-14 (log panes) and owns the test test_innerhtml_lives_only_in_build_functions"
  },
  {
   "from": "task-9",
   "to": "fn:runs",
   "relation": "missing",
   "evidence": "Task 9 Files list only rewrites poll() and its styles; runs() still holds two innerHTML assignments at ui.py:82 and ui.py:86"
  },
  {
   "from": "task-10",
   "to": "fn:runs",
   "relation": "delivers",
   "evidence": "Task 10 Produces buildRunRow/patchRuns and rewrites runs(); its test intent says the Task 9 innerHTML test is 'still green with runs() rewritten'"
  },
  {
   "from": "task-10",
   "to": "task-9",
   "relation": "precedes",
   "evidence": "Ordering is inverted: Task 9's own assertion about runs() can only hold after Task 10 rewrites runs()\n---\nTask 10's runs() rewrite is what makes Task 9's own test green, so the stated build order is inverted for that test"
  },
  {
   "from": "task-12",
   "to": "AC-8",
   "relation": "delivers",
   "evidence": "Task 12 behaviour 2: verdict as a word, or the round's status word (escalated) when there is no verdict key\n---\nplan:836 Delivers line; behaviour 2 renders owner_question and owner_reply"
  },
  {
   "from": "task-4",
   "to": "AC-23",
   "relation": "delivers",
   "evidence": "Task 4 copy block: 'item 1 round 1 accept - verdict word, or the round's status word when it has no verdict'"
  },
  {
   "from": "AC-8",
   "to": "fn:_run_item",
   "relation": "contradicts",
   "evidence": "AC-8 equates 'no verdict' with 'gates stayed red, so escalated'; workflows/run.py:245 appends the entry with status 'green' before the audit at :250 sets the verdict"
  },
  {
   "from": "fn:format_status",
   "to": "fn:_run_item",
   "relation": "contradicts",
   "evidence": "format_status prints the round's status word in the verdict slot, so an audit-in-flight round prints 'green' as if the supervisor had accepted it"
  },
  {
   "from": "task-12",
   "to": "fn:_run_item",
   "relation": "binds_to",
   "evidence": "Task 12 constraint cites _run_item appending the round entry after execute_round returns, but only covers the case where NO ledger entry exists yet"
  },
  {
   "from": "task-8",
   "to": "AC-17",
   "relation": "delivers",
   "evidence": "Task 8 lists nine verbatim reason strings, one per failure, all HTTP 200\n---\nplan:586-598 nine verbatim reason lines; behaviour 5 makes any exception a 200."
  },
  {
   "from": "AC-17",
   "to": "fn:merge_branch",
   "relation": "missing",
   "evidence": "merge_branch merges the run branch into base_branch, which makes git diff base...branch empty; AC-17's eight reasons have no entry for an already-merged run"
  },
  {
   "from": "task-13",
   "to": "endpoint:/api/diff",
   "relation": "binds_to",
   "evidence": "Task 13 behaviour 1 shows stat then patch then the cut line, with the Task 8 constraint 'show it as is. No special-casing'"
  },
  {
   "from": "task-6",
   "to": "fn:log_slice",
   "relation": "delivers",
   "evidence": "Task 6 Interfaces declares log_slice(path, offset) -> {text, offset, size}"
  },
  {
   "from": "fn:log_slice",
   "to": "fn:append_log",
   "relation": "breaks",
   "evidence": "append_log rewrites a 1 MB log as marker + last 500 KB; a stored offset below 500 KB then satisfies offset <= size and log_slice returns a mid-file slice with no restart signal"
  },
  {
   "from": "task-9",
   "to": "fn:log_slice",
   "relation": "binds_to",
   "evidence": "Task 9 behaviour 3: 'If the reply's offset is lower than the one sent, replaceChildren() then append the whole text'; behaviour 2: 'Reopening resumes from the stored offset with its text intact'"
  },
  {
   "from": "task-3",
   "to": "AC-20",
   "relation": "delivers",
   "evidence": "Task 3 behaviour 2-5 prefixes each card's text with location_line\n---\nplan:188; behaviours 2-5 cover _ask_owner, _park_note, _stopped_note, _owner_card"
  },
  {
   "from": "task-3",
   "to": "fn:_park_note",
   "relation": "contradicts",
   "evidence": "Task 3 behaviour 3 says the text starts with location_line(item_no, total) + ' parked', 'then today's text' - and today's text already starts with exactly that string (workflows/run.py:433)\n---\nplan:213 'starts location_line(item_no, total) + \" parked\", then today's text' vs workflows/run.py:433 whose text already opens with exactly that string"
  },
  {
   "from": "task-2",
   "to": "fn:location_line",
   "relation": "delivers",
   "evidence": "Task 2 Interfaces declares the signature and pins the U+00B7 separator"
  },
  {
   "from": "task-3",
   "to": "fn:location_line",
   "relation": "binds_to",
   "evidence": "Task 3 Interfaces: 'Consumes: location_line (Task 2)'; three call shapes all match the declared signature\n---\nplan:196 'Consumes: location_line (Task 2)'; plan:221-223 adds it to the existing activities.notify import inside imports_passed_through()\n---\nplan:220-224 - the notify import gains location_line, stated as a reasoned deviation from AC-34"
  },
  {
   "from": "task-1",
   "to": "fn:read_env",
   "relation": "delivers",
   "evidence": "Task 1 Interfaces declares read_env(path: str | os.PathLike) -> dict[str, str]"
  },
  {
   "from": "task-8",
   "to": "fn:read_env",
   "relation": "binds_to",
   "evidence": "Task 8 behaviour 4 reads LOOPGRAPH_PROJECTS_DIR with read_env(ROOT / '.env') per request\n---\nplan:611 'reads LOOPGRAPH_PROJECTS_DIR with read_env(ROOT / \".env\") per request'; ui.ROOT exists at ui.py:23"
  },
  {
   "from": "task-8",
   "to": "fn:resolve_repo",
   "relation": "delivers",
   "evidence": "Task 8 Interfaces declares resolve_repo(worktree, runs_dir, projects_dir) -> tuple[Path | None, str]"
  },
  {
   "from": "task-4",
   "to": "fn:resolve_run_arg",
   "relation": "delivers",
   "evidence": "Task 4 Interfaces; the no-dash remainder rule is the inverse of ui.py:164"
  },
  {
   "from": "task-5",
   "to": "fn:resolve_run_arg",
   "relation": "binds_to",
   "evidence": "Task 5 Interfaces: 'Consumes: format_status, run_slug, resolve_run_arg (Task 4)'; spelling and casing agree"
  },
  {
   "from": "task-3",
   "to": "AC-33",
   "relation": "delivers",
   "evidence": "Verified live: a scratchpad copy of workflows/run.py carrying both Task 3 changes replayed run-2026-09-05-deye-pending-restore-ad4abd's real history with no non-determinism error\n---\nplan:239-256 replay script; run in the host .venv where both modules load fresh, so it cannot reproduce the worker's stale-passthrough state\n---\nplan:239-261 Verify, the Replayer heredoc\n---\nplan:188 Delivers line; plan:241-256 Verify runs the Replayer over fetched history"
  },
  {
   "from": "task-3",
   "to": "AC-32",
   "relation": "delivers",
   "evidence": "Task 3 Delivers names AC-32, but its test intents pin only the workflow-side args=[...] counts, not send_card's signature or worker.py's activity list"
  },
  {
   "from": "task-11",
   "to": "endpoint:/api/run",
   "relation": "binds_to",
   "evidence": "Task 11 Interfaces: poll() fetches /api/run?id= and /api/logs?dir= together every 2000 ms\n---\nTask 11 Interfaces: poll() fetches /api/run?id= every 2000 ms"
  },
  {
   "from": "task-8",
   "to": "decision:diff-via-worktree-pointer",
   "relation": "delivers",
   "evidence": "resolve_repo implements the pointer walk; verified working today against three live run directories"
  },
  {
   "from": "task-1",
   "to": "AC-19",
   "relation": "delivers",
   "evidence": "plan:80 'Delivers: AC-19' - but the task's Files block (plan:81-84) lists envfile.py, lg and tests/test_envfile.py only"
  },
  {
   "from": "task-2",
   "to": "AC-21",
   "relation": "delivers",
   "evidence": "plan:136 'Delivers: AC-21'; the pinned signature and copy match spec:146-148\n---\nplan:136 Delivers line"
  },
  {
   "from": "task-4",
   "to": "AC-25",
   "relation": "delivers",
   "evidence": "plan:272; its five format_status test intents (plan:327-331) match the five ledger shapes AC-25 names at spec:164-166\n---\nplan:272 Delivers line; test intents at plan:327-331 cover the five spec shapes"
  },
  {
   "from": "task-4",
   "to": "AC-31",
   "relation": "delivers",
   "evidence": "plan:272; its six resolver test intents (plan:332-336) match the six cases AC-31 names at spec:190-192\n---\nplan:272; test intents at plan:332-336 cover the six spec shapes"
  },
  {
   "from": "task-2",
   "to": "task-3",
   "relation": "precedes",
   "evidence": "plan build order is sequential (plan:25-27); Task 3 imports what Task 2 adds\n---\nplan:196 'Consumes: location_line (Task 2)'"
  },
  {
   "from": "task-1",
   "to": "task-8",
   "relation": "precedes",
   "evidence": "plan:573 'Consumes: read_env (Task 1)'"
  },
  {
   "from": "fn:location_line",
   "to": "file:worker.py",
   "relation": "breaks",
   "evidence": "worker.py:17-18 preloads activities.notify, so the sandbox's passthrough import hands workflows/run.py the stale module object with no location_line attribute"
  },
  {
   "from": "file:docker-compose.yml",
   "to": "decision:no-worker-restart",
   "relation": "contradicts",
   "evidence": "docker-compose.yml:60 mounts `- ./:/app` into the live worker, so an edit to workflows/run.py reaches the worker's disk with no restart; the sandbox re-imports that module per workflow instance"
  },
  {
   "from": "task-8",
   "to": "AC-19",
   "relation": "missing",
   "evidence": "plan:566 Delivers names AC-15/16/17/18/37; nothing in Task 8 claims or verifies AC-19's 'reaches ui.py through one shared reader' half"
  },
  {
   "from": "task-5",
   "to": "AC-23",
   "relation": "delivers",
   "evidence": "plan:350; rule 5 at plan:381-383 routes a ledger result through format_status and a status-query result to JSON\n---\nplan:350 Delivers line names AC-23; step 5 (plan:383) routes a ledger result to format_status"
  },
  {
   "from": "fn:format_status",
   "to": "AC-23",
   "relation": "contradicts",
   "evidence": "plan:298 adds a no-card line AC-23 does not ask for; plan:323-325 admits it and says it exists so lg status and Task 11's page say the same thing, but no test on either side checks that they do"
  },
  {
   "from": "fn:build_card_text",
   "to": "AC-5",
   "relation": "contradicts",
   "evidence": "activities/notify.py:37 sends summary.strip()[:1500] inside a 4000-char cap, so the verbatim summary recorded as awaiting.question is not what the owner saw, which spec:284-286 claims as the reason for recording it"
  },
  {
   "from": "file:skills/loopgraph/SKILL.md",
   "to": "task-5",
   "relation": "missing",
   "evidence": "SKILL.md:159 and README.md:218,281 document only `lg status <workflow-id> ledger`; no task's Files block touches either doc to add the slug form or --json"
  },
  {
   "from": "fn:resolve_run_arg",
   "to": "fn:_dotenv",
   "relation": "precedes",
   "evidence": "both land in lg; Task 1 edits _dotenv, Task 4 adds the resolver, Task 5 rewrites cmd_status - three sequential edits to one file, order stated at plan:25-27"
  },
  {
   "from": "task-3",
   "to": "AC-5",
   "relation": "delivers",
   "evidence": "plan:188 Delivers line; behaviour 1 adds \"question\": summary to the awaiting literal"
  },
  {
   "from": "task-3",
   "to": "AC-22",
   "relation": "delivers",
   "evidence": "plan:210 - _run_item still stores the bare question and hands it to record_owner_answer"
  },
  {
   "from": "fn:location_line",
   "to": "file:activities/notify.py",
   "relation": "binds_to",
   "evidence": "plan:141 Task 2 modifies activities/notify.py, the same module that defines send_card at activities/notify.py:90"
  },
  {
   "from": "fn:wf_from_card",
   "to": "file:activities/notify.py",
   "relation": "binds_to",
   "evidence": "Verified live: build_card_text with a summary starting 'item 2 of 3 \u00b7 round 2' still yields the right id from wf_from_card, for a quoted reply and a merge card. No gap here."
  },
  {
   "from": "fn:_ask_owner",
   "to": "fn:record_owner_answer",
   "relation": "precedes",
   "evidence": "workflows/run.py:304-316 - reply comes back, then the bare question goes to record_owner_answer; AC-22 holds under the plan"
  },
  {
   "from": "artifact:replay-script",
   "to": "AC-33",
   "relation": "breaks",
   "evidence": "Live query 2026-09-05: 0 LoopGraphRun workflows with ExecutionStatus Running, 13 total, all COMPLETED. The script's success condition never fires."
  },
  {
   "from": "artifact:replay-script",
   "to": "file:workflows/run.py",
   "relation": "contradicts",
   "evidence": "7 of 13 stored histories already fail NondeterminismError against unmodified HEAD (7b61c54); the script offers no baseline so the failures read as Task 3's fault"
  },
  {
   "from": "decision:replay-not-restart",
   "to": "file:AGENTS.md",
   "relation": "missing",
   "evidence": "Spec cites 'AGENTS.md forbids it' twice (spec:245, spec:298); grep -i 'restart|docker compose|stack' over AGENTS.md returns nothing"
  },
  {
   "from": "task-3",
   "to": "AC-32",
   "relation": "missing",
   "evidence": "plan:188 claims AC-32 but plan:190-193 lists neither worker.py nor activities/notify.py, and none of the four test intents at plan:232-237 pins send_card's signature or the worker's activity set"
  },
  {
   "from": "task-3",
   "to": "AC-20",
   "relation": "missing",
   "evidence": "None of the four test intents at plan:232-237 asserts that any card text starts with the location line, nor covers the (len(items), len(items)) all-parked case AC-20 pins"
  },
  {
   "from": "contract:awaiting.question",
   "to": "AC-4",
   "relation": "contradicts",
   "evidence": "awaiting is written inside _await_decision (workflows/run.py:388) and rebuilt by replay, so after the worker runs new code a run that was already waiting DOES get a question key - AC-4's 'no question key' branch is unreachable for it"
  },
  {
   "from": "contract:awaiting.question",
   "to": "AC-23",
   "relation": "contradicts",
   "evidence": "Same replay mechanism makes 'question not recorded' (spec:157) unreachable for a LoopGraphRun ledger once the new code is live"
  },
  {
   "from": "task-4",
   "to": "contract:awaiting.question",
   "relation": "binds_to",
   "evidence": "plan:295 format_status copy block prints <question> or 'question not recorded'"
  },
  {
   "from": "task-11",
   "to": "contract:awaiting.question",
   "relation": "binds_to",
   "evidence": "plan:803-805 patchAwaiting shows the question element, hidden when the key is absent"
  },
  {
   "from": "AC-8",
   "to": "AC-35",
   "relation": "contradicts",
   "evidence": "AC-8 (spec:69) requires the page to render owner_question and owner_reply, but AC-35's rounds[] key list (spec:212-214) does not name them, so the key-freeze that Task 3 owns does not protect them"
  },
  {
   "from": "task-3",
   "to": "file:worker.py",
   "relation": "missing",
   "evidence": "A workflows/run.py change is inert until the worker process restarts (worker.py loads the class at start); no task, constraint or verify step says how or when the new workflow code goes live"
  },
  {
   "from": "decision:location-line-in-card-text",
   "to": "fn:send_card",
   "relation": "binds_to",
   "evidence": "spec:254-257 - the whole design choice exists to avoid touching send_card's argument list; empirically confirmed replay-neutral (6 OK / 7 pre-existing FAIL, identical before and after a patched copy of the plan's behaviours 1-5)"
  },
  {
   "from": "task-5",
   "to": "AC-24",
   "relation": "delivers",
   "evidence": "plan:350; step 5 sends --json and a positional query to json.dumps(result, indent=2)"
  },
  {
   "from": "task-5",
   "to": "AC-26",
   "relation": "delivers",
   "evidence": "plan:380-382 step 4 describes the describe()-guarded result() fallback"
  },
  {
   "from": "task-5",
   "to": "AC-27",
   "relation": "delivers",
   "evidence": "plan:371-374 step 2 tries the argument as an id first and only branches on RPCStatusCode.NOT_FOUND"
  },
  {
   "from": "task-5",
   "to": "AC-29",
   "relation": "delivers",
   "evidence": "plan:364 reproduces the spec's stderr line verbatim"
  },
  {
   "from": "task-5",
   "to": "AC-30",
   "relation": "delivers",
   "evidence": "plan:365 reproduces the spec's 'newest of N' line verbatim; step 3 prints it before the mode branch so it holds in --json too"
  },
  {
   "from": "task-4",
   "to": "task-5",
   "relation": "precedes",
   "evidence": "plan:357 Task 5 Consumes format_status, run_slug, resolve_run_arg from Task 4; build order is sequential (plan:25-27)"
  },
  {
   "from": "task-5",
   "to": "fn:cmd_status",
   "relation": "binds_to",
   "evidence": "plan:352-353 Files: Modify lg (cmd_status, the status argparse block)"
  },
  {
   "from": "task-5",
   "to": "fn:format_status",
   "relation": "binds_to",
   "evidence": "plan:383 a result from ledger or the result fallback prints format_status(result)"
  },
  {
   "from": "decision:result-fallback-on-closed",
   "to": "fn:format_status",
   "relation": "breaks",
   "evidence": "Live: run-2026-09-05-microbits-fact-corrections-36437d result keys are ['checkpoint','learn','merge','owner_decision','rounds','status'] and run-2026-09-05-microbits-ideas-sharpen-794631's are ['checkpoint','reason','rounds','status'] \u2014 neither has an items key, which plan:300-306 assumes"
  },
  {
   "from": "task-5",
   "to": "fn:cmd_status",
   "relation": "missing",
   "evidence": "plan:371-375 sends a non-final query failure to the next query and plan:380-382 handles only the ledger query's failure; nothing states the outcome when the last query fails and is not ledger, while plan:388-389 removes today's re-raise (lg:93-94)"
  },
  {
   "from": "file:ui.py",
   "to": "task-5",
   "relation": "contradicts",
   "evidence": "ui.py:165 treats a falsy workflow status as running; plan:380 treats 'not RUNNING' as closed and awaits handle.result(), and temporalio 1.32 types WorkflowExecution.status as WorkflowExecutionStatus | None"
  },
  {
   "from": "task-5",
   "to": "task-7",
   "relation": "contradicts",
   "evidence": "plan:540 pins the 25-row cap and call(timeout=10) for the same list_workflows call that plan:376 leaves uncapped and untimed, while plan:322 requires the newest of all matches"
  },
  {
   "from": "task-5",
   "to": "file:README.md",
   "relation": "missing",
   "evidence": "README.md:218,281 document only 'lg status <workflow-id> ledger'; no task's Files block lists README.md, and plan:62 forbids rewording copy not pinned in a task"
  },
  {
   "from": "task-5",
   "to": "file:skills/loopgraph/SKILL.md",
   "relation": "missing",
   "evidence": "SKILL.md:159 documents only 'lg status <workflow-id> ledger'; no task's Files block lists it"
  },
  {
   "from": "task-5",
   "to": "file:lg",
   "relation": "contradicts",
   "evidence": "lg:217 subparser help 'query a workflow (tries status, then ledger)' and lg:4-6 docstring 'queries gate red/green' both describe behaviour Task 5 replaces, and no task changes them"
  },
  {
   "from": "decision:replay-not-restart",
   "to": "file:workflows/run.py",
   "relation": "contradicts",
   "evidence": "Zero LoopGraphRun workflows are Running on 2026-09-05, and 6 of the 13 closed histories already fail Replayer against today's unmodified workflows/run.py with [TMPRL1100] Nondeterminism error"
  },
  {
   "from": "file:workflows/run.py",
   "to": "AC-23",
   "relation": "binds_to",
   "evidence": "LoopGraphRun defines only a ledger query (workflows/run.py:525), confirmed live: 'Query handler for status expected but not found, known queries: [__enhanced_stack_trace __stack_trace __temporal_workflow_metadata ledger]', so the status-then-ledger order still reaches the summary"
  },
  {
   "from": "fn:resolve_run_arg",
   "to": "AC-28",
   "relation": "delivers",
   "evidence": "Simulated against all 13 live workflow ids: every run-directory slug resolves to its own workflows only, and runs/<slug>/ resolves the same as the bare slug"
  },
  {
   "from": "task-6",
   "to": "AC-11",
   "relation": "delivers",
   "evidence": "plan l.424; the offset protocol is Task 6 behaviour 3-4 (l.452-458)\n---\nTask 6 Behaviour 4 implements the offset/size reply"
  },
  {
   "from": "task-6",
   "to": "AC-12",
   "relation": "delivers",
   "evidence": "plan l.424; log_names + the {\"logs\": [names]} wire form (l.440, l.450)"
  },
  {
   "from": "task-6",
   "to": "task-7",
   "relation": "precedes",
   "evidence": "Task 7 Consumes: bad_param (Task 6) \u2014 plan l.497"
  },
  {
   "from": "task-6",
   "to": "task-9",
   "relation": "precedes",
   "evidence": "Task 9 consumes /api/logs and /api/log (plan l.657); between Task 6 and Task 9 the served page cannot render logs (plan l.63-64)"
  },
  {
   "from": "task-7",
   "to": "task-8",
   "relation": "precedes",
   "evidence": "Task 8 Consumes TemporalFeed.ledger and feed= (plan l.573)\n---\nTask 8 Interfaces consume TemporalFeed.ledger and feed=, produced at plan:512-522; sequential build order at plan:25-27."
  },
  {
   "from": "task-7",
   "to": "task-11",
   "relation": "precedes",
   "evidence": "Task 11 Consumes /api/run (plan l.776)"
  },
  {
   "from": "fn:TemporalFeed.ledger",
   "to": "AC-1",
   "relation": "contradicts",
   "evidence": "AC-1 splits the source by run state ('the ledger query for a running workflow and the workflow's result for a closed one', spec l.31-32); plan l.522 collapses it to 'on any failure try handle.result()'"
  },
  {
   "from": "fn:TemporalFeed.ledger",
   "to": "AC-10",
   "relation": "breaks",
   "evidence": "With temporal_addr set and Temporal down, feed is truthy and _client is None (measured); the planned body raises AttributeError before any query, so /api/run cannot answer 200 with a null ledger and the page never reaches its 'temporal unreachable \u2014 logs only' line"
  },
  {
   "from": "fn:TemporalFeed.ledger",
   "to": "fn:TemporalFeed.runs",
   "relation": "missing",
   "evidence": "runs() guards with `if not self._client: return []` (ui.py:186-187); the planned ledger() has no equivalent guard"
  },
  {
   "from": "fn:TemporalFeed.ledger",
   "to": "fn:TemporalFeed.call",
   "relation": "contradicts",
   "evidence": "ledger() is declared sync (plan l.505) so it must go through call(); _runs already runs on the loop thread, where call() blocks the loop and raises TimeoutError (measured)"
  },
  {
   "from": "task-7",
   "to": "task-5",
   "relation": "contradicts",
   "evidence": "Task 5 constraint requires a describe() guard before handle.result() because it blocks for as long as a run waits on its card (plan l.385-387); Task 7 states the same fallback with no guard (plan l.522) and polls it every 2 seconds\n---\nTask 5 requires a describe() guard before handle.result(); Task 7 Behaviour 1 states the same fallback with no guard, and the page calls it 30 times a minute"
  },
  {
   "from": "endpoint:/api/log",
   "to": "fn:append_log",
   "relation": "binds_to",
   "evidence": "AC-11's reset case is named as 'append_log head-truncated the file' (spec l.85-87); Task 6 constraint l.462-463 claims append_log 'caps a file at 1 MB'"
  },
  {
   "from": "task-9",
   "to": "endpoint:/api/log",
   "relation": "binds_to",
   "evidence": "plan l.464-465 'A reply offset lower than the one requested is how the page learns the file was head-truncated (Task 9 binds to this)'; Task 9 behaviour 3 (l.678-681)\n---\nTask 9 Behaviour 3 replaces the pane only when the reply offset is lower than the one sent"
  },
  {
   "from": "AC-9",
   "to": "endpoint:/api/log",
   "relation": "binds_to",
   "evidence": "a collapsed pane sends no request and reopening resumes from the stored offset (spec l.72-74, plan l.674-675), which is the case the offset>size detector misses"
  },
  {
   "from": "task-7",
   "to": "AC-2",
   "relation": "missing",
   "evidence": "Task 7 behaviour 4 applies bad_param(id) (plan l.530) but its Delivers line names AC-1 and AC-3 only (l.492)"
  },
  {
   "from": "task-8",
   "to": "AC-2",
   "relation": "missing",
   "evidence": "Task 8 behaviour 1 applies bad_param(id) (plan l.601) but its Delivers line names AC-15..AC-18 and AC-37 only (l.566)"
  },
  {
   "from": "task-7",
   "to": "code:test_runs_endpoint_without_temporal",
   "relation": "binds_to",
   "evidence": "plan l.545-546 states exactly what it asserts afterwards; its fixture uses temporal_addr=None (tests/test_ui.py:19), so it never covers the connect-failed feed"
  },
  {
   "from": "task-6",
   "to": "code:test_logs_endpoint",
   "relation": "binds_to",
   "evidence": "plan l.466-468 states the new assertion and that the content assertion goes; it matches the fixture's single file (tests/test_ui.py:16-18)"
  },
  {
   "from": "task-6",
   "to": "code:test_logs_reject_traversal",
   "relation": "binds_to",
   "evidence": "plan l.460-461 argues the {\"error\": ...} body keeps it green; correct, urlopen raises on any 400"
  },
  {
   "from": "fn:log_names",
   "to": "code:LOG_GLOB",
   "relation": "missing",
   "evidence": "plan l.450 specifies the literal '*.log'; stream.py:18-22 declares LOG_GLOB the single source of truth for anything that reads these files"
  },
  {
   "from": "task-6",
   "to": "fn:log_tails",
   "relation": "breaks",
   "evidence": "plan l.426 removes log_tails and LOG_TAIL, so the shipped 60 KB-per-file bound (ui.py:24, ui.py:127) disappears with them"
  },
  {
   "from": "task-7",
   "to": "fn:run_entry",
   "relation": "missing",
   "evidence": "run_entry takes a ledger and derives detail from it (plan l.526-528), but logs-only rows need detail 'logs only' with no ledger (l.529); which side builds them is unstated"
  },
  {
   "from": "task-8",
   "to": "AC-15",
   "relation": "delivers",
   "evidence": "plan:561-566 Delivers line names AC-15; behaviour 1 and 3 implement the ledger-sourced branches and the two git commands."
  },
  {
   "from": "task-8",
   "to": "AC-16",
   "relation": "delivers",
   "evidence": "plan:603-607 resolve_repo walks worktree -> pointer -> gitdir -> host repo."
  },
  {
   "from": "task-8",
   "to": "AC-18",
   "relation": "delivers",
   "evidence": "plan:576 DIFF_CAP = 204_800; behaviour 3 cuts patch and sets truncated."
  },
  {
   "from": "task-8",
   "to": "AC-37",
   "relation": "delivers",
   "evidence": "plan:620 'Only rev-parse and diff run'; test intent test_the_dashboard_has_no_write_methods."
  },
  {
   "from": "task-13",
   "to": "AC-15",
   "relation": "delivers",
   "evidence": "plan:891 Delivers AC-15 (page side); behaviour 1 fetches /api/diff?id=<sel.id>."
  },
  {
   "from": "task-8",
   "to": "task-13",
   "relation": "precedes",
   "evidence": "Task 13 Interfaces: 'Consumes: /api/diff (Task 8)' (plan:897)."
  },
  {
   "from": "task-8",
   "to": "fn:resolve_repo",
   "relation": "binds_to",
   "evidence": "plan:578 signature; plan:603-607 behaviour."
  },
  {
   "from": "task-8",
   "to": "fn:branch_diff",
   "relation": "binds_to",
   "evidence": "plan:579 signature; plan:608-610 behaviour."
  },
  {
   "from": "endpoint:/api/diff",
   "to": "fn:execute_round",
   "relation": "binds_to",
   "evidence": "worktree, branch and base_branch on rounds[-1] are exactly the keys execute_round returns (execute_round.py:268-270) and workflows/run.py:236-238 copies onto every round entry."
  },
  {
   "from": "endpoint:/api/diff",
   "to": "file:.env",
   "relation": "binds_to",
   "evidence": "plan:611 'reads LOOPGRAPH_PROJECTS_DIR with read_env(ROOT / \".env\") per request'; .env:12 holds the value."
  },
  {
   "from": "fn:merge_branch",
   "to": "AC-15",
   "relation": "contradicts",
   "evidence": "merge --no-ff into base makes branch an ancestor of base, so merge-base(base, branch) == branch tip and the three-dot diff AC-15 pins returns nothing. Verified live: /home/kevin/projects/deye merge-base(main, lg-...-ad4abd) == f344ec2 == the branch tip, and `git diff main...lg-...-ad4abd` is 0 bytes."
  },
  {
   "from": "task-8",
   "to": "AC-16",
   "relation": "contradicts",
   "evidence": "AC-16 replaces `/projects/` with `<LOOPGRAPH_PROJECTS_DIR>/`; plan:606 replaces it with `projects_dir`, dropping the slash. With .env:12 = /home/kevin/projects that yields /home/kevin/projectsdeye."
  },
  {
   "from": "task-8",
   "to": "decision:patch-cap-200kb",
   "relation": "contradicts",
   "evidence": "The decision's stated purpose is that 'one request cannot stall the page'; plan:608-610 runs both git commands to completion and cuts afterwards, with no timeout and no cap on stat."
  },
  {
   "from": "fn:discard",
   "to": "task-8",
   "relation": "contradicts",
   "evidence": "plan:621-622 asserts 'A worktree that discard removed has no pointer, which is reason 4'; on disk today three run dirs keep their pointer while the branch and /home/kevin/projects/microbits-opportunities/.git/worktrees are gone, so those land on reason 8."
  },
  {
   "from": "task-8",
   "to": "decision:read-only",
   "relation": "missing",
   "evidence": "AC-37 is claimed but the only test intent (plan:634-635) greps ui.py's source; nothing asserts the target repository is byte-identical after a /api/diff request."
  },
  {
   "from": "task-8",
   "to": "AC-15",
   "relation": "missing",
   "evidence": "AC-15's 'never from the request' has no test intent; the six intents at plan:629-635 never send a branch/base/dir query parameter and assert it is ignored."
  },
  {
   "from": "fn:execute_round",
   "to": "AC-17",
   "relation": "missing",
   "evidence": "execute_round.py:229 can record base_branch == '' (detached HEAD), a state checkpoint.py:112 and workflows/run.py:486 both guard against; AC-17's eight modes and the plan's nine reasons name no case for it."
  },
  {
   "from": "task-8",
   "to": "artifact:worktree-pointer",
   "relation": "binds_to",
   "evidence": "plan:604 pointer path runs_dir / <worktree relative to /app/runs/> / '.git'; five such files exist and all match plan:621's stated shape."
  },
  {
   "from": "task-11",
   "to": "AC-7",
   "relation": "delivers",
   "evidence": "Task 11 Delivers line names AC-4, AC-6, AC-7, AC-10, AC-14"
  },
  {
   "from": "task-11",
   "to": "artifact:ledger-microbits-fact-corrections",
   "relation": "breaks",
   "evidence": "patchItems is specified only for a present-but-empty items list; this live ledger has no items key, so ledger.items is undefined in the page's JavaScript"
  },
  {
   "from": "task-12",
   "to": "artifact:ledger-microbits-fact-corrections",
   "relation": "breaks",
   "evidence": "Task 12 Behaviour 1 keys a ledger round as item_no-round; this round has no item_no, so it keys 'undefined-1' while its logs key '1-1'"
  },
  {
   "from": "task-12",
   "to": "code:test_the_old_log_names_still_render",
   "relation": "contradicts",
   "evidence": "The test pins that pre-queue runs must still render; Task 12 splits exactly those runs into a verdict-only card and a logs-only card"
  },
  {
   "from": "task-9",
   "to": "contract:test_innerhtml_lives_only_in_build_functions",
   "relation": "delivers",
   "evidence": "Task 9 Test intents introduce the test as its own verification"
  },
  {
   "from": "contract:test_innerhtml_lives_only_in_build_functions",
   "to": "file:ui.py",
   "relation": "breaks",
   "evidence": "ui.py:82 and ui.py:86 put innerHTML inside runs(), which Task 9 explicitly leaves to Task 10"
  },
  {
   "from": "contract:test_innerhtml_lives_only_in_build_functions",
   "to": "AC-14",
   "relation": "missing",
   "evidence": "The test is lexical; nothing forbids poll() or a patch* function from calling buildBoard()/buildRoundCard() unconditionally, which is the DOM replacement AC-14 exists to stop"
  },
  {
   "from": "task-13",
   "to": "AC-14",
   "relation": "delivers",
   "evidence": "Task 13's browser checklist items 1 and 2 are the only check that actually observes surviving selection"
  },
  {
   "from": "file:workflows/run.py",
   "to": "AC-8",
   "relation": "contradicts",
   "evidence": "run.py:245 appends the round entry before the 30-minute audit at run.py:250, so a round with no verdict key is routinely a green round mid-audit, not only the escalated case AC-8 describes"
  },
  {
   "from": "task-12",
   "to": "file:workflows/run.py",
   "relation": "binds_to",
   "evidence": "Task 12's ' \u00b7 in progress' marker is keyed on 'has logs but no ledger entry', a window that closes at run.py:245 while the audit log is still growing"
  },
  {
   "from": "task-7",
   "to": "endpoint:/api/run",
   "relation": "delivers",
   "evidence": "Task 7 Behaviour 4 defines /api/run over TemporalFeed.ledger"
  },
  {
   "from": "task-9",
   "to": "task-12",
   "relation": "contradicts",
   "evidence": "Task 9 pins 'no logs yet for this run' and Task 12 pins 'no rounds yet' for the same empty #rounds container, and no task retires the first"
  },
  {
   "from": "task-10",
   "to": "task-11",
   "relation": "precedes",
   "evidence": "buildBoard() must create #state/#why/#awaiting/#items/#rounds/#diff before the first patchBoard, but Task 10 Behaviour 3 calls it only on the click path, not on the auto-select-first path at ui.py:91"
  },
  {
   "from": "file:activities/stream.py",
   "to": "AC-11",
   "relation": "missing",
   "evidence": "append_log's head truncation shifts content while leaving the file ~500 KB, so a stored offset below the new size yields misaligned bytes that the lower-offset signal never fires for"
  }
 ]
}
```
