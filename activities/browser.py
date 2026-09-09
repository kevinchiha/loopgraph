"""Everything browser-shaped. So far: the run's declaration, `browser.yaml`.

A run directory may hold a `browser.yaml` beside `brief.md` and `gates.yaml`,
and having one at all is what tells the engine this run has a web app:

    serve:
      cmd: npm run dev -- --port $LOOPGRAPH_PORT
      ready_timeout: 90
    capture:
      - name: home
        path: /
      - name: settings
        path: /settings
        width: 375
        timeout: 20

`serve` is the only required block. The engine picks the port, starts the command
in the worktree, waits for the socket, and takes a PNG of each capture entry when
the round ends; both models see the same app through the browser container.

Every mistake in here is an error naming the key, never a default taken quietly.
`lg start` prints this text to whoever wrote the file, and a `serve.port` that
silently meant nothing would put two runs on one port.

The two YAML traps are the ones `activities/config.py` describes at length, and
the checks here have the same shape as its own: PyYAML follows YAML 1.1, so a
bare `on`, `off`, `yes` or `no` is a boolean even in key position, and `bool` is
a subclass of `int` in Python, so `width: yes` would pass an isinstance check as
one pixel. The code is not shared because every message here names browser.yaml.

The activities read the file through `read_browser_config`, which hands the error
back as a value. A file edited into a broken state after `lg start` has to reach
the executor's prompt as evidence rather than park the item.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

BROWSER_FILE = "browser.yaml"
CAPTURE_CAP = 10
DEFAULT_READY_TIMEOUT = 60
MAX_READY_TIMEOUT = 180
DEFAULT_WIDTH = 1280
DEFAULT_CAPTURE_TIMEOUT = 30
MAX_CAPTURE_TIMEOUT = 60

# A capture's name becomes <run_dir>/shots/i<N>-r<M>/<name>.png, so it has to be
# a filename and nothing else. This is what keeps `../x` out of that path.
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass
class BrowserConfigError:
    """A file the checker refused, carried as a value rather than raised.

    The round and the audit branch on it and never `except` it: a browser.yaml
    that does not pass is one more state the prompt blocks render, next to a
    serve that never came up and a browser container that did not answer."""

    message: str


def _check_names(block: dict, section: str | None) -> None:
    """Refuse a key that is not a string, showing it as YAML read it.

    `section` is the block being checked, or None for the top level, which is the
    one section with no name of its own."""
    shown = section or "the top level"
    for key in block:
        if not isinstance(key, str):
            raise ValueError(f"browser.yaml: key {key} under {shown} is not a name "
                             "(YAML reads bare off/on/yes/no as booleans)")


def _check_unknown(block: dict, allowed: tuple[str, ...], section: str | None = None) -> None:
    where = f" under {section}" if section else ""
    for key in block:
        if key not in allowed:
            raise ValueError(f"browser.yaml: unknown key {key!r}{where}")


def _check_keys(block: dict, allowed: tuple[str, ...], section: str | None = None) -> None:
    """Both passes over the whole block before any value is looked at, so the
    message names the first thing actually wrong rather than a type error further
    down. The two are separate functions because `serve` puts its own check for
    `port` between them."""
    _check_names(block, section)
    _check_unknown(block, allowed, section)


def _integer(value, name: str, minimum: int) -> int:
    """An integer of at least `minimum`. A bool is not one, whatever Python says."""
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"browser.yaml: {name} must be an integer of at least {minimum}")
    return value


def _seconds(value, name: str, maximum: int) -> int:
    """A timeout, 1 to `maximum`. Both bounds are in the message because the cap
    is the engine's rather than the owner's: what a valid file may cost the round
    has to fit inside the activity budget."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"browser.yaml: {name} must be an integer from 1 to {maximum}")
    return value


def _serve(block) -> dict:
    if not isinstance(block, dict):
        raise ValueError("browser.yaml: serve is required and must be a mapping")
    _check_names(block, "serve")
    # Ahead of the unknown-key pass, which would call this a spelling mistake.
    # The engine assigns the port so two runs in one worker never land on the same
    # socket, so a port written here could not be honoured either way.
    if "port" in block:
        raise ValueError("browser.yaml: serve.port is not a key: the engine assigns "
                         "the port; use $LOOPGRAPH_PORT in serve.cmd")
    _check_unknown(block, ("cmd", "ready_timeout"), "serve")
    cmd = block.get("cmd")
    # A string, not anything str() would take: `cmd: yes` is the boolean True, and
    # coercing it would hand the shell "True" to run.
    if not isinstance(cmd, str) or not cmd:
        raise ValueError("browser.yaml: serve.cmd must be a non-empty string")
    return {"cmd": cmd,
            "ready_timeout": _seconds(block.get("ready_timeout", DEFAULT_READY_TIMEOUT),
                                      "serve.ready_timeout", MAX_READY_TIMEOUT)}


def _capture(block) -> list[dict]:
    # No captures is the ordinary case: most runs want the app served for the
    # models and nothing on disk.
    if block is None:
        return []
    if not isinstance(block, list):
        raise ValueError("browser.yaml: capture must be a list")
    if len(block) > CAPTURE_CAP:
        raise ValueError(f"browser.yaml: capture holds {len(block)} entries; "
                         f"the cap is {CAPTURE_CAP}")
    entries: list[dict] = []
    for i, entry in enumerate(block):
        section = f"capture[{i}]"
        if not isinstance(entry, dict):
            raise ValueError(f"browser.yaml: {section} must be a mapping")
        _check_keys(entry, ("name", "path", "width", "timeout"), section)
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"browser.yaml: {section}.name must be a non-empty string")
        # fullmatch, not match: `$` also matches before a trailing newline, and a
        # name is about to be a filename.
        if not NAME_RE.fullmatch(name):
            raise ValueError(f"browser.yaml: {section}.name {name!r} may only use "
                             "letters, digits, _ and -")
        # The names share one directory, so a repeat would overwrite the earlier
        # shot and the round would report a picture it does not have.
        for j, seen in enumerate(entries):
            if seen["name"] == name:
                raise ValueError(f"browser.yaml: {section}.name {name!r} repeats capture[{j}]")
        path = entry.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError(f"browser.yaml: {section}.path must be a string starting with /")
        entries.append({
            "name": name,
            "path": path,
            "width": _integer(entry.get("width", DEFAULT_WIDTH), f"{section}.width", 1),
            "timeout": _seconds(entry.get("timeout", DEFAULT_CAPTURE_TIMEOUT),
                                f"{section}.timeout", MAX_CAPTURE_TIMEOUT),
        })
    return entries


def parse_browser_config(text: str) -> dict:
    """The whole declaration, from the text of browser.yaml.

    No I/O here. The host `lg` and the activities both go through
    `load_browser_config`, which is the one place a path is opened."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as err:
        # The parser's own message runs to several lines with the position in it;
        # the first line is the part the owner can act on, and this reaches them
        # as one line of a Telegram note.
        raise ValueError("browser.yaml: not valid YAML: " + str(err).split("\n")[0]) from err
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("browser.yaml: expected a mapping at the top level")
    _check_keys(data, ("serve", "capture"))
    return {"serve": _serve(data.get("serve")), "capture": _capture(data.get("capture"))}


def load_browser_config(path: str) -> dict:
    """Read and check the file at `path`. Raises ValueError on anything wrong,
    because `lg start` refuses the run and prints the text to a person."""
    return parse_browser_config(Path(path).read_text())


def read_browser_config(run_dir: str) -> dict | BrowserConfigError | None:
    """`<run_dir>/browser.yaml` as the activities want it, and never a raise.

    None when there is no file, which is a run with no web app; the config when
    the file is valid; the message as a `BrowserConfigError` when it is not, so a
    file edited after `lg start` reaches the prompt as evidence rather than
    parking the item.

    The filesystem gets the same treatment as the parser. A directory in the
    file's place, a file nobody may read, a disk that has gone away: raising any
    of those out of an activity would have Temporal retry it and park the item,
    which is the whole thing this return type exists to avoid. Opening the file
    is also the test for whether it is there, so there is no window between a
    look and a read for a deletion to fall into."""
    try:
        return load_browser_config(str(Path(run_dir) / BROWSER_FILE))
    except FileNotFoundError:
        return None
    except ValueError as e:
        return BrowserConfigError(str(e))
    except OSError as e:
        # The prefix the checker's own messages carry, so whatever prints this
        # one prints it the same way.
        return BrowserConfigError(f"browser.yaml: {e}")
