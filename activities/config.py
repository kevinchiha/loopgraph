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
      detectors:
        - name: dead code
          cmd: "vulture . | wc -l"
          timeout: 600

Everything is optional. No file at all is a normal run with default convergence.
A `sweep:` block is the mode switch: the run then takes its items from the
detectors instead of the brief.

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

from activities.items import HEADING

DEFAULT_CONVERGENCE = {"enabled": True, "every_items": 5, "net_lines": 400}
DEFAULT_MAX_ITEMS = 40
DEFAULT_DETECTOR_TIMEOUT = 600

DEADLINE_RE = re.compile(r"^(\d+)(m|h|d)$")
_UNITS = {"m": 60, "h": 3600, "d": 86400}

_TOP_LEVEL = "the top level"


def _check_keys(block: dict, allowed: tuple[str, ...], section: str) -> None:
    """Refuse a key that is not a name first, then one the schema does not know.

    Both passes run over the whole block before any value is looked at, so the
    message names the first thing actually wrong rather than a type error further
    down."""
    for key in block:
        if not isinstance(key, str):
            raise ValueError(f"run.yaml: key {key} under {section} is not a name "
                             "(YAML reads bare off/on/yes/no as booleans)")
    where = "" if section == _TOP_LEVEL else f" under {section}"
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
    _check_keys(block, ("yield_floor", "deadline", "max_items", "detectors"), "sweep")
    # A missing yield_floor gets the same message as a wrong one: a sweep with no
    # floor has no stopping condition of its own.
    floor = _integer(block.get("yield_floor"), "sweep.yield_floor", 0)
    deadline = block.get("deadline")
    deadline_seconds = None if deadline is None else parse_deadline(deadline)
    max_items = _integer(block.get("max_items", DEFAULT_MAX_ITEMS), "sweep.max_items", 1)
    entries = block.get("detectors")
    if not isinstance(entries, list) or not entries:
        raise ValueError("run.yaml: sweep.detectors must be a non-empty list")
    detectors = []
    for i, d in enumerate(entries):
        section = f"sweep.detectors[{i}]"
        if not isinstance(d, dict):
            raise ValueError(f"run.yaml: {section} needs name and cmd")
        _check_keys(d, ("name", "cmd", "timeout"), section)
        if not d.get("name") or not d.get("cmd"):
            raise ValueError(f"run.yaml: {section} needs name and cmd")
        detectors.append({
            "name": str(d["name"]),
            "cmd": str(d["cmd"]),
            "timeout": _integer(d.get("timeout", DEFAULT_DETECTOR_TIMEOUT), f"{section}.timeout", 1),
        })
    return {"yield_floor": floor,
            "deadline_seconds": deadline_seconds,
            "max_items": max_items,
            "detectors": detectors}


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
    _check_keys(data, ("convergence", "sweep"), _TOP_LEVEL)
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
    return read_run_config(run_dir)
