"""Everything browser-shaped: the run's declaration `browser.yaml`, the port the
engine assigns a run's app, the process that serves it, the captures the engine
takes when a round ends, and the words each model is told about all of it.

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

Chromium is a fact of its own, asked once with `attach_browser` and carried next
to the serve's. A run can have its app up and the container down, and folding
the two together is what told a supervisor its tools were attached to a browser
that was not running.

The prompt blocks are the last thing in here, and they are as much the
deliverable as the PNGs. `execute_round` and `audit` place them and word none of
them: what a model is told about a serve that never came up, or a browser that
did not answer, is the engine's contract with it, not a sentence each activity
makes up for itself.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import socket
from contextlib import suppress
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
MCP_SERVER_NAME = "playwright"
# The two tools that run code the model wrote. The supervisor is there to look,
# so it gets the browser with these two denied by name; the executor keeps them.
SUPERVISOR_DENIED = ["mcp__playwright__browser_evaluate",
                     "mcp__playwright__browser_run_code_unsafe"]
# The engine's own loopback services, kept out of both models' browsers: Temporal,
# the Temporal UI, the dashboard, CLIProxyAPI. The browser port joins them in
# `blocked_origins`, the one place the string is built.
BLOCKED_PORTS = (7233, 8233, 8400, 8317)
ATTACH_TIMEOUT = 10        # seconds for the one-off check that the container is there
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


def shots_dir(run_dir: str, item_no: int, round_no: int) -> str:
    """Where this round's captures go: <run_dir>/shots/i<N>-r<M>.

    The naming is `stream.log_name`'s, so a round's pictures and its transcript
    are findable under one name. Not imported from there: a log filename and a
    directory of PNGs are two different things that happen to be named alike."""
    return str(Path(run_dir) / "shots" / f"i{item_no}-r{round_no}")


def playwright_output_dir(run_dir: str) -> str:
    """Where the MCP server drops what a model asks it to save (AC-13).

    Under the run directory, because the worktree is what the round is judged
    on. This only moves the server's default: a `filename` the model passes
    still resolves against the server's working directory, which is the
    worktree, so `prompts/executor.md` forbids passing one and the write-set
    mismatch reports whatever slips through."""
    return str(Path(run_dir) / "scratch" / "playwright")


def blocked_origins(browser_port: int) -> str:
    """The engine's own loopback services, in both spellings, for the MCP
    server's `--blocked-origins` (AC-16).

    Not `--allowed-origins`: the allow flag aborts every request to an origin it
    does not list, fonts and CDN scripts and XHR included, so the supervisor's
    browser would show a different page from the engine's own captures, which
    filter nothing. A block list of ports no real app runs on needs no matching
    filter on the capture side. Neither flag is a security boundary, and the
    spec's non-goals say so."""
    return ";".join(f"http://127.0.0.1:{port};http://localhost:{port}"
                    for port in (*BLOCKED_PORTS, browser_port))


def mcp_server_entry(output_dir: str, endpoint: str, browser_port: int) -> dict:
    """The `playwright` server entry both models get.

    `--isolated` is not optional and is not the default once `--cdp-endpoint` is
    given: without it the server takes the shared default context, and two
    sessions on one Chromium then read each other's cookies (AC-17). No
    `--headless` and no `--browser` either — the browser is already running in
    its own container and this only attaches to it."""
    return {"type": "stdio",
            "command": "playwright-mcp",
            "args": ["--cdp-endpoint", endpoint, "--isolated",
                     "--blocked-origins", blocked_origins(browser_port),
                     "--output-dir", output_dir]}


def _first_line(err: BaseException) -> str:
    """The part of an exception a prompt block can print.

    Playwright's messages carry a call log dozens of lines long and the blocks
    print this inside a fence. The type's name is the fallback because a timeout
    carries no message at all, and an empty fence tells a model nothing."""
    return str(err).split("\n")[0] or type(err).__name__


@dataclass
class BrowserAttach:
    """Whether the browser container answered, asked once before the captures.

    Its own fact, next to the serve's `ready` (AC-27). A run can have its app up
    and Chromium down, and the two states send a model to do different things."""

    ws: str
    ok: bool
    error: str


async def attach_browser(ws: str) -> BrowserAttach:
    """Attach to the browser container, then let go.

    Nothing raises: a container that did not answer is one more state the round
    reports. The check is deliberately not folded into the serve's `ready`, and
    not left to show up as one identical error per capture, because both of
    those produced a prompt that told a model its tools were attached to a
    browser that was not there."""
    from playwright.async_api import async_playwright

    async def connect() -> None:
        async with async_playwright() as pw:
            chrome = await pw.chromium.connect_over_cdp(ws)
            # Straight back out. Chromium belongs to the stack, so this only
            # drops the connection; the round's real clients attach after.
            await chrome.close()

    try:
        await asyncio.wait_for(connect(), timeout=ATTACH_TIMEOUT)
    except Exception as err:
        return BrowserAttach(ws=ws, ok=False, error=_first_line(err))
    return BrowserAttach(ws=ws, ok=True, error="")


def _shot(entry: dict, png: str | None, error: str | None) -> dict:
    """One row of what `capture_all` returns: what was asked for, and what came
    of it. The audit block renders these in the order the file declared them."""
    return {"name": entry["name"], "path": entry["path"], "width": entry["width"],
            "png": png, "error": error}


async def _capture_one(chrome, entry: dict, app_url: str, shots_dir: str, heartbeat) -> dict:
    """One page, bounded by the entry's own timeout, and never a raise."""
    name = entry["name"]
    # Built from the directory and the name alone, and the name passed NAME_RE
    # when the file was read, so nothing the owner wrote can put this outside
    # the run directory.
    png = str(Path(shots_dir) / f"{name}.png")

    async def shoot() -> None:
        # A context per entry: the viewport is this entry's width, and nothing
        # one page stored reaches the next.
        context = await chrome.new_context(viewport={"width": entry["width"], "height": 720})
        try:
            page = await context.new_page()
            await page.goto(app_url + entry["path"], timeout=entry["timeout"] * 1000)
            await page.screenshot(path=png, full_page=True)
        finally:
            await context.close()

    async def keep_alive() -> None:
        # stream_query's pinger, for its reason: the activity's heartbeat_timeout
        # is three minutes and an entry may take a minute, so the wait has to
        # keep saying something or Temporal decides the worker died.
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            heartbeat(f"capture {name}")

    if heartbeat:
        heartbeat(f"capture {name}")
    beat = asyncio.create_task(keep_alive()) if heartbeat else None
    try:
        # goto and screenshot carry their own timeouts, but the bound has to go
        # round the whole entry: a driver wedged between the two of them would
        # otherwise outlive the seconds the file declared.
        await asyncio.wait_for(shoot(), timeout=entry["timeout"])
    except Exception as err:
        return _shot(entry, png=None, error=_first_line(err))
    finally:
        if beat:
            beat.cancel()
            # Whatever the pinger ended with, the same way stream_query swallows
            # its own: the entry's own result is the answer worth having.
            with suppress(asyncio.CancelledError, Exception):
                await beat
    return _shot(entry, png=png, error=None)


async def capture_all(entries: list[dict], app_url: str, shots_dir: str, endpoint: str,
                      heartbeat=None) -> list[dict]:
    """A full-page PNG per capture entry, in the order the file declared them.

    Never raises and never stops early. The round has already happened by the
    time this runs, so a page that will not load is a finding for the supervisor
    rather than the end of the captures, and a container that has gone away
    since `attach_browser` answered is a reason on every row."""
    from playwright.async_api import async_playwright

    try:
        Path(shots_dir).mkdir(parents=True, exist_ok=True)
    except OSError as err:
        # Kept apart from the reason below rather than folded into it: a run
        # directory nobody can write to is not a browser that did not answer.
        return [_shot(entry, png=None, error=_first_line(err)) for entry in entries]

    shots: list[dict] = []
    try:
        async with async_playwright() as pw:
            chrome = await pw.chromium.connect_over_cdp(endpoint)
            try:
                for entry in entries:
                    shots.append(await _capture_one(chrome, entry, app_url, shots_dir, heartbeat))
            finally:
                # A CDP attachment closes by disconnecting. The container stays
                # up: the supervisor's own session is on the same Chromium.
                await chrome.close()
    except Exception as err:
        # `attach_browser` answered a moment ago, so this is the container going
        # away in between. What was already taken stays; the rest carry the one
        # reason, and the caller gets a list either way.
        reason = f"browser unreachable: {_first_line(err)}"
        shots += [_shot(entry, png=None, error=reason) for entry in entries[len(shots):]]
    return shots


def browser_evidence(serve: Serve | None, attach: BrowserAttach | None, shots: list[dict],
                     config_error: str | None = None) -> dict:
    """Everything the two prompt blocks read, flat, in one dict.

    `ready` is the serve's and `browser_ok` the attach's, and neither is worked
    out from the other (AC-27). Both activities build this the same way, so the
    executor and the supervisor are looking at one set of facts."""
    return {"config_error": config_error,
            "cmd": serve.cmd if serve else None,
            "port": serve.port if serve else None,
            "url": serve.url if serve else None,
            "ready": bool(serve and serve.ready),
            "ready_timeout": serve.ready_timeout if serve else None,
            "output_tail": serve.output_tail if serve else None,
            "browser_ws": attach.ws if attach else None,
            "browser_ok": bool(attach and attach.ok),
            "browser_error": attach.error if attach else None,
            "shots": shots}


# The prompt blocks. The text is the deliverable, so it lives here whole rather
# than being assembled out of sentences at the call site: the two models are told
# different things about one state, and reading the pair side by side is the only
# way to keep that true. `execute_round` and `audit` place them and word nothing.

_EXECUTOR_NOT_READY = """\
# App server (engine check)

This run declares a web app, and the engine tried to serve this worktree with
`{cmd}` before you started. Nothing accepted a connection on 127.0.0.1:{port}
within {ready_timeout}s, so there is no app to open and you have no browser
tools this round. The last lines it printed:

```
{tail}
```

Do not start a server of your own. If the work item is what fixes this, say so
in a claim the supervisor can check against the diff. If the command itself
cannot work on this tree, put that in `blocked`: it is the run's configuration,
and only the owner can change it."""

_EXECUTOR_NO_BROWSER = """\
# App server (engine check)

This run declares a web app, and the engine is serving this worktree with
`{cmd}` on {url}. The browser container did not answer at
{ws}, so you have no browser tools this round:

```
{error}
```

The app is up and `$LOOPGRAPH_APP_URL` points at it. Do not start a server or a
browser of your own. The browser is the engine's, not this tree's: nothing in
the work item fixes it, so do not put it in `blocked`."""

_EXECUTOR_BAD_CONFIG = """\
# App server (engine check)

This run declares a web app in `browser.yaml`, but the file did not pass the
engine's check, so nothing was served and you have no browser tools this round:

```
browser.yaml is invalid: {message}
```

Do not start a server of your own. The file lives in the run directory, not in
this tree, and only the owner can change it. If the work item cannot be checked
without the app, say so in `blocked`."""

_AUDIT_CAPTURES = """\
# Browser (engine check)

The engine served the worktree with `{cmd}` on {url} and took
these captures itself after the round ended, from the app as the executor left
it. Each is a full-page PNG at the width shown. The executor's transcript never
saw them.

{shot_lines}

Read them. Your `playwright` tools are attached to the same app at
{url}, for anything this list missed."""

_AUDIT_CAPTURE_FAILED = """\
A capture that failed means the page did not load for the engine within its
timeout. That is a finding unless the diff explains it."""

_AUDIT_NO_PAGES = """\
# Browser (engine check)

The engine served the worktree with `{cmd}` on {url}.
`browser.yaml` declares no pages to capture, so there are no engine captures
this round. Your `playwright` tools are attached to that app; open what the
diff touches and look."""

_AUDIT_NOT_READY = """\
# Browser (engine check)

The engine could not serve the worktree: `{cmd}` accepted no connection on
127.0.0.1:{port} within {ready_timeout}s. No pages were captured and you have no
browser tools this round. The last lines it printed:

```
{tail}
```

Judge the round on the diff and the gates, and say in `reasons` that the app
did not start. The executor was told the same thing before it began."""

_AUDIT_NO_BROWSER = """\
# Browser (engine check)

The engine served the worktree with `{cmd}` on {url}, but the
browser container did not answer at {ws}, so no pages were captured and you
have no browser tools this round:

```
{error}
```

Judge the round on the diff and the gates, and say in `reasons` that the
browser container was unreachable. That is the engine's setup, not the
executor's work, and not a finding against the round."""

_AUDIT_BAD_CONFIG = """\
# Browser (engine check)

This run declares a web app in `browser.yaml`, but the file did not pass the
engine's check, so nothing was served, no pages were captured and you have no
browser tools this round:

```
browser.yaml is invalid: {message}
```

Judge the round on the diff and the gates, and say in `reasons` that
`browser.yaml` is invalid. The file lives in the run directory; the executor was
told the same thing before it began."""


def _fields(evidence: dict) -> dict:
    """The evidence as the templates name it. `(no output)` because a command
    that printed nothing would otherwise leave an empty fence in the prompt."""
    return {"cmd": evidence["cmd"],
            "port": evidence["port"],
            "url": evidence["url"],
            "ready_timeout": evidence["ready_timeout"],
            "tail": evidence["output_tail"] or "(no output)",
            "ws": evidence["browser_ws"],
            "error": evidence["browser_error"],
            "message": evidence["config_error"]}


def _shot_lines(shots: list[dict]) -> str:
    """One line per capture, in file order, whether it worked or not. A page the
    engine could not load is something the supervisor has to see."""
    return "\n".join(
        f"- {shot['name']}: {shot['path']} at {shot['width']}px -> "
        + (f"capture failed: {shot['error']}" if shot["error"] else shot["png"])
        for shot in shots)


def executor_block(evidence: dict) -> str:
    """What the executor is told about its app, or "" when there is nothing to
    say: the app is up, the browser answered, and the tools are in its options.

    Three states, and what the executor should do differs in each. Only the
    serve failing can ever be the work item's business; the other two are the
    run's configuration and the engine's own stack, and telling it so is what
    keeps a round from spending its `blocked` on something it cannot fix."""
    fields = _fields(evidence)
    if evidence["config_error"]:
        return _EXECUTOR_BAD_CONFIG.format(**fields)
    if not evidence["ready"]:
        return _EXECUTOR_NOT_READY.format(**fields)
    if not evidence["browser_ok"]:
        return _EXECUTOR_NO_BROWSER.format(**fields)
    return ""


def audit_block(evidence: dict) -> str:
    """What the supervisor is told, in every state (AC-12).

    Always something, unlike the executor's: the supervisor never sees the
    executor's transcript, so this block is the only way it learns that pictures
    exist, or that there was nothing to take one of."""
    fields = _fields(evidence)
    if evidence["config_error"]:
        return _AUDIT_BAD_CONFIG.format(**fields)
    if not evidence["ready"]:
        return _AUDIT_NOT_READY.format(**fields)
    if not evidence["browser_ok"]:
        return _AUDIT_NO_BROWSER.format(**fields)
    shots = evidence["shots"]
    if not shots:
        return _AUDIT_NO_PAGES.format(**fields)
    block = _AUDIT_CAPTURES.format(shot_lines=_shot_lines(shots), **fields)
    if any(shot["error"] for shot in shots):
        block += "\n\n" + _AUDIT_CAPTURE_FAILED
    return block
