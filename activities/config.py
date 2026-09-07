"""The run's own knobs: `run.yaml`, parsed once and checked hard.

A run directory may hold a `run.yaml` next to `brief.md` and `gates.yaml`:

    convergence:
      enabled: true
      every_items: 5
      net_lines: 400
    sweep:
      yield_floor: 0
      deadline: 4d
      max_items: 40
      groups: ["app/", "src/lib/"]
      detectors:
        - name: dead code
          cmd: "vulture . | wc -l"
          timeout: 600

Everything is optional. No file at all is a normal run with default convergence.
A `sweep:` block is the mode switch: the run then takes its items from the
detectors instead of the brief. Its `groups` are path prefixes compared verbatim,
so `app` matches `apple.py`; end each one in `/`.

Every mistake in here is an error naming the key, never a default taken quietly.
A `yeild_floor` that silently meant "no floor" is exactly the failure this file
exists to prevent, and the owner would only find out four days later.

Two YAML traps shape the checks. PyYAML follows YAML 1.1, so a bare `off`, `on`,
`yes` or `no` is a boolean even in key position — that is why the convergence
switch is `enabled` and never `off`, and why a non-string key gets its own
message naming the rule. And `bool` is a subclass of `int` in Python, so
`timeout: yes` passes an `isinstance(x, int)` check and would reach the detector
loop as one second.

`read_run_config` is a plain function as well as an activity: `discover` reads
the detectors from inside another activity and must not go through Temporal for
them. Pure helpers are tested; the activity is thin.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from temporalio import activity
from temporalio.exceptions import ApplicationError

from activities.items import HEADING

DEFAULT_CONVERGENCE = {"enabled": True, "every_items": 5, "net_lines": 400}
DEFAULT_MAX_ITEMS = 40
DEFAULT_DETECTOR_TIMEOUT = 600

DEADLINE_RE = re.compile(r"^(\d+)(m|h|d)$")
_UNITS = {"m": 60, "h": 3600, "d": 86400}


def _check_keys(block: dict, allowed: tuple[str, ...], section: str | None = None) -> None:
    """Refuse a key that is not a name first, then one the schema does not know.

    `section` is the block being checked, or None for the top level, which is the
    one section with no name of its own. It used to be named by the same string
    the message shows, so rewording that string would have silently moved every
    top-level message under a section called "the top level".

    Both passes run over the whole block before any value is looked at, so the
    message names the first thing actually wrong rather than a type error further
    down."""
    shown = section or "the top level"
    for key in block:
        if not isinstance(key, str):
            raise ValueError(f"run.yaml: key {key} under {shown} is not a name "
                             "(YAML reads bare off/on/yes/no as booleans)")
    where = f" under {section}" if section else ""
    for key in block:
        if key not in allowed:
            raise ValueError(f"run.yaml: unknown key {key!r}{where}")


def _integer(value, name: str, minimum: int) -> int:
    """An integer of at least `minimum`. A bool is not one, whatever Python says."""
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"run.yaml: {name} must be an integer of at least {minimum}")
    return value


def parse_deadline(text: str) -> int:
    """`30m`, `4h` or `4d` as seconds. Anything else is an error."""
    m = DEADLINE_RE.match(text) if isinstance(text, str) else None
    if not m:
        raise ValueError("run.yaml: sweep.deadline must look like 30m, 4h or 4d")
    return int(m.group(1)) * _UNITS[m.group(2)]


def _convergence(block) -> dict:
    if block is None:
        return dict(DEFAULT_CONVERGENCE)
    if not isinstance(block, dict):
        raise ValueError("run.yaml: convergence must be a mapping")
    _check_keys(block, ("enabled", "every_items", "net_lines"), "convergence")
    enabled = block.get("enabled", DEFAULT_CONVERGENCE["enabled"])
    if not isinstance(enabled, bool):
        raise ValueError("run.yaml: convergence.enabled must be true or false")
    return {
        "enabled": enabled,
        "every_items": _integer(block.get("every_items", DEFAULT_CONVERGENCE["every_items"]),
                                "convergence.every_items", 1),
        "net_lines": _integer(block.get("net_lines", DEFAULT_CONVERGENCE["net_lines"]),
                              "convergence.net_lines", 1),
    }


def _sweep(block) -> dict | None:
    if block is None:
        return None
    if not isinstance(block, dict):
        raise ValueError("run.yaml: sweep must be a mapping")
    _check_keys(block, ("yield_floor", "deadline", "max_items", "detectors", "groups"), "sweep")
    # A missing yield_floor gets the same message as a wrong one: a sweep with no
    # floor has no stopping condition of its own.
    floor = _integer(block.get("yield_floor"), "sweep.yield_floor", 0)
    deadline = block.get("deadline")
    deadline_seconds = None if deadline is None else parse_deadline(deadline)
    max_items = _integer(block.get("max_items", DEFAULT_MAX_ITEMS), "sweep.max_items", 1)
    groups = block.get("groups")
    # Absent means "group by top-level directory"; a wrong one is refused rather
    # than dropped, because a groups that quietly became None would quietly
    # change which corner of the repo each item works. The entries are strings,
    # not anything str() would take: `groups: [yes]` is `[True]`.
    if groups is not None and (not isinstance(groups, list) or not groups
                               or not all(isinstance(g, str) and g for g in groups)):
        raise ValueError("run.yaml: sweep.groups must be a list of path prefixes")
    entries = block.get("detectors")
    if not isinstance(entries, list) or not entries:
        raise ValueError("run.yaml: sweep.detectors must be a non-empty list")
    detectors = []
    for i, d in enumerate(entries):
        section = f"sweep.detectors[{i}]"
        if not isinstance(d, dict):
            raise ValueError(f"run.yaml: {section} needs name and cmd")
        _check_keys(d, ("name", "cmd", "timeout"), section)
        # A string, not anything str() would take: `cmd: yes` is the boolean True
        # and `cmd: 0755` the integer 493, so coercing them handed the shell
        # "True" or "493" to run and the owner a command they never wrote.
        if not all(isinstance(d.get(key), str) and d[key] for key in ("name", "cmd")):
            raise ValueError(f"run.yaml: {section} needs name and cmd")
        detectors.append({
            "name": d["name"],
            "cmd": d["cmd"],
            "timeout": _integer(d.get("timeout", DEFAULT_DETECTOR_TIMEOUT), f"{section}.timeout", 1),
        })
    return {"yield_floor": floor,
            "deadline_seconds": deadline_seconds,
            "max_items": max_items,
            "detectors": detectors,
            "groups": groups}


def parse_run_config(text: str, brief: str = "") -> dict:
    """The whole config, from the text of run.yaml and the text of the brief."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as err:
        # The parser's own message runs to several lines with the position in it;
        # the first line is the part the owner can act on, and this reaches them
        # as one line of a Telegram note.
        raise ValueError("run.yaml: not valid YAML: " + str(err).split("\n")[0]) from err
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("run.yaml: expected a mapping at the top level")
    _check_keys(data, ("convergence", "sweep"))
    convergence = _convergence(data.get("convergence"))
    sweep = _sweep(data.get("sweep"))
    # The workflow reads no disk, so the one place that has both files is here.
    if sweep is not None and any(HEADING.match(line) for line in brief.splitlines()):
        raise ValueError("run.yaml: a sweep run takes its items from the detectors; "
                         "remove the Work items section")
    return {"convergence": convergence, "sweep": sweep}


def _read(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def read_run_config(run_dir: str) -> dict:
    """Parse `<run_dir>/run.yaml`. Either file being absent reads as empty."""
    d = Path(run_dir)
    return parse_run_config(_read(d / "run.yaml"), _read(d / "brief.md"))


@activity.defn
async def load_run_config(run_dir: str) -> dict:
    """The activity behind `read_run_config`, with one thing added: a file the
    checker refuses is refused for good.

    Temporal retries an activity that raises, and asking the same broken file the
    same question three times can only get the same answer. The type is kept so
    the workflow's `config_error_reason` still finds the bare `run.yaml:` line on
    `__cause__`; everything else that can go wrong here is a worker restart or a
    slow disk, which the retry policy is there to cover."""
    try:
        return read_run_config(run_dir)
    except ValueError as e:
        raise ApplicationError(str(e), type="ValueError", non_retryable=True) from e
