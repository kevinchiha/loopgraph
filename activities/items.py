"""The run's work queue.

A brief can name several work items. The run does them one at a time, each with
its own rounds, audit and checkpoint commit, all onto one branch. An item that
cannot go green is parked and the run carries on, so one bad item does not throw
away five good ones.

A brief with no work-items section is a single item: the whole brief. That is the
older shape and it still works.
"""

from __future__ import annotations

import re
from pathlib import Path

from temporalio import activity

HEADING = re.compile(r"^#{1,6}\s*work\s*items\s*$", re.IGNORECASE)
BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")
ANY_HEADING = re.compile(r"^#{1,6}\s")


def parse_work_items(brief: str) -> list[str]:
    """Bullets under a `## Work items` heading, in order. Empty list if there is
    no such heading, which means the whole brief is the single item.

    A bullet may wrap: indented lines under it are folded into the same item.
    Items are usually a sentence or two, and a parser that silently cut them at
    the first line would hand the executor half an instruction."""
    items: list[str] = []
    top_indent: int | None = None
    in_section = False
    for line in brief.splitlines():
        if HEADING.match(line):
            in_section = True
            continue
        if not in_section:
            continue
        if ANY_HEADING.match(line):
            break  # the next heading ends the list
        m = BULLET.match(line)
        if m:
            indent = len(line) - len(line.lstrip())
            if items and top_indent is not None and indent > top_indent:
                # A sub-bullet detailing the item above it. Promoting it to its own
                # work item handed the executor a fragment with no context.
                items[-1] = f"{items[-1]} {line.strip()}"
            else:
                top_indent = indent if top_indent is None else min(top_indent, indent)
                items.append(m.group(1).strip())
        elif not line.strip():
            continue  # blank lines inside a list mean nothing
        elif line[:1].isspace() and items:
            items[-1] = f"{items[-1]} {line.strip()}"  # a wrapped bullet
        else:
            break  # unindented prose: the list is over
    return items


@activity.defn
async def load_work_items(run_dir: str) -> list[str]:
    return parse_work_items((Path(run_dir) / "brief.md").read_text())
