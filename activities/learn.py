"""Learning edge: on accept, distil the confirmed cause into ONE constraint line,
append to constraints.md (bounded, FIFO rotation). Every future executor prompt
includes that file — this is the part no framework ships, and it's ~20 lines.

The model distil is best-effort: garbage/empty/NONE → skip the append, never
block the run. append_constraint is pure file logic and tested.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from temporalio import activity

CONSTRAINT_CAP = 50    # lines kept in constraints.md
LINE_CAP = 200         # chars per constraint


# "Here is the constraint:", "The lesson is:" and friends. A line that is only a
# lead-in is never the constraint itself.
_PREAMBLE = re.compile(
    r"^(here('s| is)|the (constraint|lesson|rule)|based on|i would|sure[,!.]|okay[,!.])\b.*:\s*$",
    re.IGNORECASE)


def append_constraint(path: str, line: str, cap: int = CONSTRAINT_CAP) -> list[str]:
    line = " ".join(line.split())[:LINE_CAP]
    p = Path(path)
    lines = p.read_text().splitlines() if p.exists() else []
    lines = [l for l in lines if l.strip()]
    # removeprefix, not lstrip: lstrip strips a CHARACTER SET, so a constraint
    # that starts with a command-line flag ("--no-cache is required here") lost
    # its dashes and its meaning before being written as a binding rule.
    lines.append(f"- {line.removeprefix('- ')}")
    kept = lines[-cap:]
    p.write_text("\n".join(kept) + "\n")
    return kept


def clean_distilled(text: str) -> str | None:
    """The one constraint sentence out of the model's reply, or None.

    Taking the first line meant any preamble the model wrote ("Here is the
    constraint:") became the stored rule while the real lesson was thrown away,
    and the 12-character floor was far too low to catch it. Take the last
    substantial line instead: a preamble comes first, the answer comes last.
    """
    lines = [" ".join(l.split()) for l in text.strip().splitlines()]
    lines = [l for l in lines if l and not l.startswith(("```", "#"))]
    for line in reversed(lines):
        if line.upper().rstrip(".") == "NONE":
            return None
        if len(line) >= 12 and not _PREAMBLE.match(line):
            return line[:LINE_CAP]
    return None


async def distil_constraint(brief: str, claims: list[str], reasons: list[str]) -> str | None:
    if os.environ.get("LOOPGRAPH_IN_CONTAINER") != "1":
        raise RuntimeError("refusing to run Claude outside the worker container")
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

    prompt = (
        "An accepted engineering round is below. Distil the ONE durable lesson future "
        "executors must know for this repo (a verified cause, a trap, a convention) as a "
        "single imperative constraint line, max 160 chars. If nothing generalizes beyond "
        "this one change, output exactly: NONE. Output only the line.\n\n"
        f"Brief:\n{brief[:1500]}\n\n"
        f"Accepted claims:\n" + "\n".join(f"- {c}" for c in claims) + "\n\n"
        f"Auditor's accept reasons:\n" + "\n".join(f"- {r}" for r in reasons)
    )
    chunks: list[str] = []
    async for msg in query(prompt=prompt, options=ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        # Same trap as the auditor: an empty allowed_tools grants no auto-approval
        # but removes nothing, and bypassPermissions approves everything anyway.
        # This step only distils a sentence; it needs no tools at all.
        tools=[], allowed_tools=[],
        disallowed_tools=["Write", "Edit", "MultiEdit", "NotebookEdit", "Bash",
                          "BashOutput", "KillShell", "Task", "WebFetch", "WebSearch"],
        max_turns=1,
    )):
        activity.heartbeat("distil streaming")
        if isinstance(msg, AssistantMessage):
            chunks.extend(b.text for b in msg.content if isinstance(b, TextBlock))
    return clean_distilled("\n".join(chunks))


@activity.defn
async def learn(run_dir: str, round_result: dict, verdict: dict) -> dict:
    brief = Path(run_dir, "brief.md").read_text()
    line = await distil_constraint(brief, round_result.get("claims", []), verdict.get("reasons", []))
    if line is None:
        return {"appended": None}
    kept = append_constraint(str(Path(run_dir) / "constraints.md"), line)
    return {"appended": line, "constraints_total": len(kept)}
