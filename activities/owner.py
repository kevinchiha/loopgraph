"""What the owner told this run, written where the supervisor can read it.

An `ask` used to reach the executor only, through its directive. The supervisor is
deliberately blind to the executor's transcript, so a field the owner authorised
looked to it like a claim resting on nothing: it asked the same question every
round until the cap, and the run ended with nothing committed. Recording the
exchange in the run directory gives both sides the same evidence.

The button label goes in too. A tap arrives as the bare letter "A", so an executor
told only "Owner replied: A" went reading the previous audit's log to work out what
A had meant, and the supervisor read that, rightly, as an answer the executor had
made up.

The file lives in the run directory. No write set includes it, so the executor
cannot forge a line in it.

Pure helpers are tested; the activity is thin.
"""

from __future__ import annotations

from pathlib import Path

from temporalio import activity

ANSWERS_FILE = "owner-answers.md"
QUESTION_CAP = 500
REPLY_CAP = 800

HEADER = ("# Owner answers\n\n"
          "Written by the engine each time the owner answers a question card.\n"
          "Nothing else writes this file.\n\n")


def format_answer(question: str, reply: str, options: dict | None,
                  item_no: int, round_no: int) -> str:
    """One line recording what the owner was asked and what they said back.

    A single letter is expanded with the label of the button it came from,
    because the letter alone is meaningless to anyone who did not send the card.
    """
    q = " ".join(str(question).split())[:QUESTION_CAP]
    r = " ".join(str(reply).split())[:REPLY_CAP]
    label = (options or {}).get(r.upper(), "") if len(r) == 1 else ""
    said = f'"{r}"' + (f' (the button labelled: {label})' if label else "")
    return f'- item {item_no}, round {round_no}. Asked: "{q}" Owner replied: {said}'


def append_answer(path: str, line: str) -> str:
    """Append one answer, header first on a new file. Returns the whole file.

    Re-appending an identical line is a no-op: Temporal retries activities, and a
    retry after the write would otherwise record the same answer twice.
    """
    p = Path(path)
    body = p.read_text() if p.exists() else HEADER
    if line in body:
        return body
    body = body + line + "\n"
    p.write_text(body)
    return body


def read_answers(run_dir: str) -> str:
    """Everything the owner has answered in this run, or "" before the first one."""
    p = Path(run_dir) / ANSWERS_FILE
    return p.read_text() if p.exists() else ""


@activity.defn
async def record_owner_answer(run_dir: str, question: str, reply: str, options: dict,
                              item_no: int, round_no: int) -> dict:
    line = format_answer(question, reply, options, item_no, round_no)
    append_answer(str(Path(run_dir) / ANSWERS_FILE), line)
    return {"recorded": line}
