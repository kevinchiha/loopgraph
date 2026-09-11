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
LABEL_CAP = 200   # chars per option label. It is a button's text, not an essay.
OPTION_CAP = 6    # choices kept from one question; more than this is a model ignoring its contract
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

HEADER = ("# Owner answers\n\n"
          "Written by the engine each time the owner answers a question card.\n"
          "Nothing else writes this file.\n\n")


def _flat(value, cap: int) -> str:
    """One line, capped. Model text lands in the middle of a prompt and in the
    middle of a card, and a newline in it could open a section of its own."""
    return " ".join(str(value).split())[:cap]


def clean_options(raw) -> dict[str, str]:
    """Whatever a model put in `options`, as the letter-to-label map the engine reads.

    Both contracts ask for that map. A model that wrote a list instead used to
    reach `.items()` and kill whichever activity it landed in: one live audit
    died on it, the item parked, and its accepted work went at the next worktree
    reset. The shape is model output, so it is untrusted exactly the way the text
    is. A list, a bare string or a number is coerced, never fatal and never
    dropped, because ["pay", "stay free"] still tells the owner what they are
    choosing between.

    The letter is not decoration. The button IS the letter, `route.CB_DATA` reads
    one letter back out of a tap, and `format_answer` looks the label up by it,
    so a key that is not a single letter gets replaced by one and folded into the
    label rather than lost. Keys the model got right are kept, because the
    `recommend` line next to them already refers to its choices by letter.

    Labels are collapsed here rather than through `audit.flatten_claim`, the same
    way `clean_candidates` repeats it: audit.py imports this module and never the
    other way round.
    """
    if isinstance(raw, dict):
        pairs = [(str(k), v) for k, v in raw.items()]
    elif isinstance(raw, (list, tuple)):
        pairs = [("", v) for v in raw]
    elif raw is None or not str(raw).strip():
        return {}
    else:
        pairs = [("", raw)]
    pairs = pairs[:OPTION_CAP]
    keys = [k.strip().upper() for k, _ in pairs]
    contract = len(set(keys)) == len(pairs) and all(len(k) == 1 and k.isalpha() for k in keys)
    out = {}
    for i, (key, value) in enumerate(pairs):
        label = _flat(value, LABEL_CAP)
        if not contract and key.strip():
            label = f"{_flat(key, LABEL_CAP)}: {label}".strip(": ")
        out[keys[i] if contract else LETTERS[i]] = label
    return out


def format_answer(question: str, reply: str, options, item_no: int, round_no: int) -> str:
    """One line recording what the owner was asked and what they said back.

    A single letter is expanded with the label of the button it came from,
    because the letter alone is meaningless to anyone who did not send the card.
    """
    q = _flat(question, QUESTION_CAP)
    r = _flat(reply, REPLY_CAP)
    label = clean_options(options).get(r.upper(), "") if len(r) == 1 else ""
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
