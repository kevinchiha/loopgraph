"""Everything browser-shaped. So far: the run's declaration `browser.yaml`, the
port the engine assigns a run's app, and the process that serves it.

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

The serve is the engine's and never the model's. The engine picks the port, so
two runs in one worker cannot land on one socket, hands it over as
$LOOPGRAPH_PORT, runs the command in the worktree in its own process group, and
kills that whole group when the round ends. A gate command is the one party that
gets the browser endpoint without the app URL, which is what `gate_env` is for.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import socket
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from activities.gate import _drain

BROWSER_FILE = "browser.yaml"
CAPTURE_CAP = 10
DEFAULT_READY_TIMEOUT = 60
MAX_READY_TIMEOUT = 180
DEFAULT_WIDTH = 1280
DEFAULT_CAPTURE_TIMEOUT = 30
MAX_CAPTURE_TIMEOUT = 60

# The engine's own names and bounds, rather than the declaration's.
ENDPOINT_ENV = "LOOPGRAPH_BROWSER_WS"
DEFAULT_BROWSER_PORT = 8420
HEARTBEAT_SECONDS = 20     # gate.py's step, well inside the 3-minute heartbeat_timeout
OUTPUT_TAIL_LINES = 30     # of the serve's output, for the prompt blocks to show
_POLL_SECONDS = 0.5        # between one look at the port and the next
# `_drain` keeps a number of bytes rather than lines. 400 a line is generous, and
# a line longer than that costs one of the 30, never the tail.
_TAIL_BYTES = OUTPUT_TAIL_LINES * 400

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


# The ports the engine has handed out and not yet taken back. Module state, so
# it covers one worker process and promises nothing beyond it.
_reserved: set[int] = set()


def browser_port() -> int:
    """The port Chromium's remote debugging endpoint listens on.

    One number for the whole stack: compose starts the container on it and every
    client attaches to it, so the default here and the compose default are the
    same 8420 or a machine that set neither has a browser nobody can find.

    Anything that is not a number reads as unset rather than raising. A round
    must not park over a typo in .env, and compose hands Chromium that same
    string, so the container is not up either and the round says the browser did
    not answer."""
    port = os.environ.get("LOOPGRAPH_BROWSER_PORT", "")
    return int(port) if port.isdigit() else DEFAULT_BROWSER_PORT


def browser_endpoint() -> str:
    """The CDP URL both models and the engine's own capture client attach to."""
    return f"http://127.0.0.1:{browser_port()}"


def _bind_free_port() -> int:
    """A port the OS says is free, given straight back."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def pick_port() -> int:
    """A free port, held until `release_port` gives it back.

    The reservation is what bind-and-release cannot do on its own: between this
    returning and the dev server calling bind the port is free again, so the OS
    can offer the same number to the next serve starting in this worker, and the
    two runs then race for one socket. Two workers picking at the same moment is
    not this case; nothing here reaches across processes."""
    while True:
        port = _bind_free_port()
        if port not in _reserved:
            _reserved.add(port)
            return port


def release_port(port: int) -> None:
    """Hand a port back. A port nobody reserved is not an error: every serve is
    stopped, including one that never started."""
    _reserved.discard(port)


def app_env(port: int) -> dict[str, str]:
    """What the serve command and both models get for this run's app.

    Three keys and nothing else. It is a layer over the environment the worker
    already has, not an environment of its own, and a copy of `os.environ` here
    would hand a model the Telegram token `NO_TELEGRAM` is there to blank."""
    return {"LOOPGRAPH_PORT": str(port),
            "LOOPGRAPH_APP_URL": f"http://127.0.0.1:{port}",
            ENDPOINT_ENV: browser_endpoint()}


def gate_env(run_dir: str) -> dict[str, str] | None:
    """What a gate command gets while the run declares a web app: the browser,
    and never the app (AC-8).

    `checkpoint_write_set` re-runs the same gates.yaml in its own activity with
    no serve alive, so a gate that reached $LOOPGRAPH_APP_URL would be green in
    the round and red at the commit, and the accepted item would park with its
    work thrown away. Completing this dict with the app keys is the mistake.

    Only whether the file is there is asked: a run whose browser.yaml does not
    pass the check still runs its gates. None, for a run with no web app, is
    what `_run_one` takes for no environment of its own."""
    if not (Path(run_dir) / BROWSER_FILE).exists():
        return None
    return {ENDPOINT_ENV: browser_endpoint()}


def _kill_serve(proc) -> None:
    """Kill the serve and everything it started.

    Not `gate._kill_group`, which asks the OS for the group with
    `os.getpgid(proc.pid)`. A gate's shell is still running when its timeout
    fires; a serve's shell often is not. A command that daemonises exits 0 and
    leaves the real server behind it, and once that shell has been reaped there
    is no process left to ask, so the group kill finds nothing and the server
    keeps the port for the next round.

    `start_new_session=True` made the shell the group leader, so its pid is the
    group's number whether or not the shell itself still exists, and the group
    lives as long as any member of it does."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # Nothing is left in the group, which is the ordinary end of a serve
        # whose command exited on its own.
        pass


async def _port_answers(port: int) -> bool:
    """Whether anything accepts a connection on the port, closed again at once.

    This is the whole of what ready means. A dev server that prints "listening"
    before it binds, or prints nothing at all, or daemonises and leaves the shell
    exiting 0 behind it, all answer here and nowhere else."""
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError:
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


def _tail(out: bytearray, note: str = "") -> str:
    """The last lines of what the process wrote, `note` first when there is one."""
    lines = bytes(out).decode(errors="replace").splitlines()[-OUTPUT_TAIL_LINES:]
    return "\n".join(([note] if note else []) + lines)


@dataclass
class Serve:
    """One app server: what was run, where it was reachable, and how it went.

    `output_tail` is the evidence the prompt blocks carry when `ready` is False.
    The process and its drain are held for `stop`, which both activities call in
    a finally whichever way the round ended."""

    cmd: str
    port: int
    url: str
    ready_timeout: int
    ready: bool
    output_tail: str
    _proc: object | None = field(default=None, repr=False)
    _drain: object | None = field(default=None, repr=False)

    async def stop(self) -> None:
        """Kill the group and hand the port back. Safe to call twice, and on a
        serve that never came up."""
        proc, drain = self._proc, self._drain
        # Dropped before the kill, so a second call finds nothing to kill.
        self._proc = self._drain = None
        if proc is not None:
            _kill_serve(proc)
            # The two seconds `_run_one` gives its own drain, for its reason: a
            # child that outlived the shell holds the pipe open and EOF never
            # comes, and what the process wrote is in the buffer by now.
            try:
                await asyncio.wait_for(asyncio.shield(drain), timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                drain.cancel()
        release_port(self.port)


async def start_serve(cmd: str, workdir: str, port: int, ready_timeout: int,
                      heartbeat=None) -> Serve:
    """Serve `workdir` with `cmd` on `port`, and wait for the port to answer.

    Nothing raises out of here. A serve that could not start, or never bound, is
    a Serve with `ready` False and the output as evidence, because a web app the
    engine could not bring up must reach the executor's prompt rather than park
    the item."""
    url = f"http://127.0.0.1:{port}"

    def result(ready: bool, output_tail: str, proc=None, drain=None) -> Serve:
        return Serve(cmd=cmd, port=port, url=url, ready_timeout=ready_timeout,
                     ready=ready, output_tail=output_tail, _proc=proc, _drain=drain)

    try:
        proc = await asyncio.create_subprocess_exec(
            # The shell a gate runs under, for the reason `_run_one` sets out.
            "/bin/bash", "-o", "pipefail", "-c", cmd,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            # Own process group: `npm run dev` forks the real server, and killing
            # the shell alone leaves that child holding the port for the round
            # after this one.
            start_new_session=True,
            env={**os.environ, **app_env(port)},
        )
    except OSError as err:
        # A worktree that is gone is the one that happens. Its first line names
        # the path, and the prompt block prints the tail as one block of text.
        return result(False, str(err).split("\n")[0])

    out = bytearray()
    drain = asyncio.create_task(_drain(proc.stdout, _TAIL_BYTES, out))
    waiter = asyncio.create_task(proc.wait())
    step = min(HEARTBEAT_SECONDS, ready_timeout)
    elapsed = since_beat = 0.0
    ready, note = False, ""
    while True:
        # The port first: a command that binds and exits non-zero in the same
        # breath is still a server that answered.
        if await _port_answers(port):
            ready = True
            break
        # A shell that exited 0 is not a give-up. A command that daemonises does
        # exactly that, and nothing but the port says whether it worked.
        if waiter.done() and proc.returncode:
            note = (f"serve command exited with code {proc.returncode} "
                    "before the port answered")
            break
        if elapsed >= ready_timeout:
            break
        await asyncio.sleep(_POLL_SECONDS)
        elapsed += _POLL_SECONDS
        since_beat += _POLL_SECONDS
        if heartbeat and since_beat >= step:
            since_beat = 0.0
            heartbeat(f"serve waiting ({int(elapsed)}s)")
    waiter.cancel()
    if ready:
        return result(True, _tail(out), proc, drain)
    _kill_serve(proc)
    try:
        await asyncio.wait_for(asyncio.shield(drain), timeout=2)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        drain.cancel()
    return result(False, _tail(out, note))
