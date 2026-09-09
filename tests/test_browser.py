"""The browser container, the two clients that attach to it, and `browser.yaml`.

The suite runs with no browser container and no network, so what it can check
here is the wiring: compose declares the service behind its profile, the worker
image carries the MCP server and the Python client, and neither of them pulls a
browser down. Everything that needs a real Chromium — a capture on disk, two
sessions that cannot read each other's cookies, an origin the MCP server refuses
to open — is the checklist below, run by hand.

The declaration needs no browser at all: `browser.yaml` is text in, a dict or an
error out, so those are ordinary parser checks. Neither does the serve: the app
under test in here is a real local `python -m http.server`, and what those tests
watch is the engine's own half of it — the port it hands out, the shell it runs
the command under, and the process group it kills.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import yaml

from activities import browser
from activities.browser import (DEFAULT_BROWSER_PORT, BrowserConfigError, app_env,
                                browser_endpoint, browser_port, gate_env,
                                load_browser_config, parse_browser_config, pick_port,
                                read_browser_config, release_port, start_serve)

ROOT = Path(__file__).resolve().parent.parent


# ---------- the container and its clients ----------

def test_the_browser_service_is_behind_the_profile_and_on_the_host_network():
    """The image is about 1.5GB and most runs never open a page, so the service
    exists only when COMPOSE_PROFILES asks for it. Host network because the app
    it looks at is served on the worker's own 127.0.0.1."""
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    browser = compose["services"]["browser"]
    assert browser["profiles"] == ["browser"]
    assert browser["network_mode"] == "host"
    assert re.match(r"mcr\.microsoft\.com/playwright:v\d+\.\d+\.\d+-", browser["image"]), \
        f"the browser image is not a pinned Playwright tag: {browser['image']!r}"
    # Same reason the worker has none: postgres and temporal do not come back
    # after a reboot, so a service that does comes back to no stack.
    assert "restart" not in browser


def test_the_worker_image_pins_the_mcp_server_and_never_installs_a_browser():
    """Chromium lives in the browser container and both clients attach to it over
    CDP, so a browser in this image would be a gigabyte nothing opens."""
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert re.search(r"@playwright/mcp@\d+\.\d+\.\d+", dockerfile), \
        "the MCP server is missing or unpinned in the worker image"
    assert "playwright install" not in dockerfile, \
        "the worker image is downloading a browser it never opens"


def test_the_python_client_is_declared_in_pyproject():
    """The container's pip line reads this list, so the floor here is the whole
    of the Python install."""
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    assert any(d.startswith("playwright>=") for d in deps), \
        f"no playwright floor in the dependencies: {deps}"


# ---------- the declaration ----------

MINIMAL = "serve:\n  cmd: npm run dev\n"

FULL = ("serve:\n"
        "  cmd: npm run dev -- --port $LOOPGRAPH_PORT\n"
        "  ready_timeout: 90\n"
        "capture:\n"
        "  - name: home\n"
        "    path: /\n"
        "    width: 375\n"
        "    timeout: 15\n"
        "  - name: team_settings-2\n"
        "    path: /settings/team\n"
        "    width: 1600\n"
        "    timeout: 60\n")

PORT_MESSAGE = ("browser.yaml: serve.port is not a key: the engine assigns the port; "
                "use $LOOPGRAPH_PORT in serve.cmd")

NOT_A_NAME = "(YAML reads bare off/on/yes/no as booleans)"

UNKNOWN_KEYS = [
    ("port: 3000\n" + MINIMAL, "browser.yaml: unknown key 'port'"),
    ("serve:\n  cmd: x\n  redy_timeout: 5\n", "browser.yaml: unknown key 'redy_timeout' under serve"),
    (MINIMAL + "capture: [{name: home, path: /, height: 800}]\n",
     "browser.yaml: unknown key 'height' under capture[0]"),
]

BARE_ON = [
    ("on: true\n" + MINIMAL, f"browser.yaml: key True under the top level is not a name {NOT_A_NAME}"),
    ("serve:\n  cmd: x\n  on: true\n", f"browser.yaml: key True under serve is not a name {NOT_A_NAME}"),
    (MINIMAL + "capture: [{on: true, name: home, path: /}]\n",
     f"browser.yaml: key True under capture[0] is not a name {NOT_A_NAME}"),
]

# The whole-file shapes. `cmd: yes` is the boolean True and `name: yes` the same,
# so neither is coerced with str(): the shell would be handed "True" to run and
# shots/ a file called True.png.
BAD_SHAPES = [
    ("- one\n- two\n", "browser.yaml: expected a mapping at the top level"),
    ("", "browser.yaml: serve is required and must be a mapping"),
    ("capture: []\n", "browser.yaml: serve is required and must be a mapping"),
    ("serve: npm run dev\n", "browser.yaml: serve is required and must be a mapping"),
    ("serve:\n  ready_timeout: 5\n", "browser.yaml: serve.cmd must be a non-empty string"),
    ('serve:\n  cmd: ""\n', "browser.yaml: serve.cmd must be a non-empty string"),
    ("serve:\n  cmd: yes\n", "browser.yaml: serve.cmd must be a non-empty string"),
    (MINIMAL + "capture: home\n", "browser.yaml: capture must be a list"),
    (MINIMAL + "capture: [home]\n", "browser.yaml: capture[0] must be a mapping"),
    (MINIMAL + "capture: [{path: /}]\n", "browser.yaml: capture[0].name must be a non-empty string"),
    (MINIMAL + "capture: [{name: yes, path: /}]\n",
     "browser.yaml: capture[0].name must be a non-empty string"),
    (MINIMAL + "capture: [{name: home}]\n",
     "browser.yaml: capture[0].path must be a string starting with /"),
    (MINIMAL + "capture: [{name: home, path: home}]\n",
     "browser.yaml: capture[0].path must be a string starting with /"),
    (MINIMAL + "capture: [{name: home, path: 5}]\n",
     "browser.yaml: capture[0].path must be a string starting with /"),
]

BAD_NAMES = ["../home", "a b", "home.png", "shots/home"]

# A bool is an int in Python, so an isinstance check alone would take
# `width: yes` as one pixel and `ready_timeout: true` as one second.
BAD_NUMBERS = [
    ("serve:\n  cmd: x\n  ready_timeout: true\n",
     "browser.yaml: serve.ready_timeout must be an integer from 1 to 180"),
    ("serve:\n  cmd: x\n  ready_timeout: '60'\n",
     "browser.yaml: serve.ready_timeout must be an integer from 1 to 180"),
    (MINIMAL + "capture: [{name: home, path: /, width: yes}]\n",
     "browser.yaml: capture[0].width must be an integer of at least 1"),
    (MINIMAL + "capture: [{name: home, path: /, width: 0}]\n",
     "browser.yaml: capture[0].width must be an integer of at least 1"),
    (MINIMAL + "capture: [{name: home, path: /, timeout: yes}]\n",
     "browser.yaml: capture[0].timeout must be an integer from 1 to 60"),
]

# The bound is the engine's, not the owner's: a valid file may cost the audit
# 180 + 10 x 60 = 780s, which is what the 30-minute activity budget is sized on.
PAST_THE_CAP = [
    ("serve:\n  cmd: x\n  ready_timeout: 181\n",
     "browser.yaml: serve.ready_timeout must be an integer from 1 to 180"),
    ("serve:\n  cmd: x\n  ready_timeout: 0\n",
     "browser.yaml: serve.ready_timeout must be an integer from 1 to 180"),
    (MINIMAL + "capture: [{name: home, path: /, timeout: 61}]\n",
     "browser.yaml: capture[0].timeout must be an integer from 1 to 60"),
    (MINIMAL + "capture: [{name: home, path: /, timeout: 0}]\n",
     "browser.yaml: capture[0].timeout must be an integer from 1 to 60"),
]


def refused(text, message):
    with pytest.raises(ValueError) as err:
        parse_browser_config(text)
    assert str(err.value) == message


def numbered(n: int) -> str:
    """A file with `n` valid capture entries, for the cap."""
    return MINIMAL + "capture:\n" + "".join(f"  - {{name: p{i}, path: /p{i}}}\n" for i in range(n))


def test_a_minimal_file_gets_every_default():
    """A dev server and nothing else is the whole of what most runs declare."""
    assert parse_browser_config(MINIMAL) == {
        "serve": {"cmd": "npm run dev", "ready_timeout": 60}, "capture": []}
    # capture: with nothing under it reads as no captures, not as a broken list.
    assert parse_browser_config(MINIMAL + "capture:\n")["capture"] == []
    assert parse_browser_config(MINIMAL + "capture: [{name: home, path: /}]\n")["capture"] == [
        {"name": "home", "path": "/", "width": 1280, "timeout": 30}]


def test_every_key_is_accepted_with_its_value():
    assert parse_browser_config(FULL) == {
        "serve": {"cmd": "npm run dev -- --port $LOOPGRAPH_PORT", "ready_timeout": 90},
        "capture": [{"name": "home", "path": "/", "width": 375, "timeout": 15},
                    {"name": "team_settings-2", "path": "/settings/team",
                     "width": 1600, "timeout": 60}]}


def test_serve_port_is_refused_by_name():
    """The engine picks the port and hands it over as $LOOPGRAPH_PORT. A port
    written here looks honoured and is not, which is how two runs end up on one
    socket, so it is refused ahead of the unknown-key pass rather than reading as
    a key nobody knows."""
    refused("serve:\n  cmd: x\n  port: 3000\n", PORT_MESSAGE)


@pytest.mark.parametrize("text,message", UNKNOWN_KEYS)
def test_an_unknown_key_is_named_with_its_section(text, message):
    refused(text, message)


@pytest.mark.parametrize("text,message", BARE_ON)
def test_a_key_that_is_not_a_name_is_refused_before_the_unknown_key_pass(text, message):
    """Under the unknown-key rule alone a bare `on:` reads as `unknown key True`,
    naming a key the owner never wrote."""
    refused(text, message)


@pytest.mark.parametrize("text,message", BAD_SHAPES)
def test_the_file_has_to_have_the_shape_the_engine_reads(text, message):
    refused(text, message)


def test_a_file_that_is_not_yaml_is_refused_with_the_prefix():
    """A mis-indented file is the likeliest way a hand-written one goes wrong.
    `lg start` prints this to whoever wrote it and the owner's note carries it,
    so it stays one line and says which file to go and fix."""
    with pytest.raises(ValueError) as err:
        parse_browser_config("serve:\n  cmd: x\n   ready_timeout: 5\n")
    message = str(err.value)
    assert message.startswith("browser.yaml: not valid YAML: ")
    assert "\n" not in message
    assert len(message) > len("browser.yaml: not valid YAML: ")


def test_an_eleventh_capture_is_refused():
    """Every entry costs the audit up to a minute; ten is what the budget holds."""
    assert len(parse_browser_config(numbered(10))["capture"]) == 10
    refused(numbered(11), "browser.yaml: capture holds 11 entries; the cap is 10")


def test_a_repeated_name_names_both_entries():
    """The names become filenames in one directory, so the second one would
    overwrite the first and the round would report a shot it does not have."""
    refused(MINIMAL + "capture:\n"
            "  - {name: home, path: /}\n"
            "  - {name: about, path: /about}\n"
            "  - {name: home, path: /home}\n",
            "browser.yaml: capture[2].name 'home' repeats capture[0]")


@pytest.mark.parametrize("name", BAD_NAMES)
def test_a_name_that_is_not_a_filename_is_refused(name):
    """`shots/i2-r1/<name>.png` is where this ends up, so `../home` would write
    outside the run directory and `a b` is a filename nobody can type."""
    refused(MINIMAL + f"capture: [{{name: {name!r}, path: /}}]\n",
            f"browser.yaml: capture[0].name {name!r} may only use letters, digits, _ and -")


def test_a_name_that_ends_in_a_newline_is_refused():
    """`$` matches before a trailing newline as well as at the end, so checking
    the name with match() would let one through into a filename."""
    refused(MINIMAL + 'capture: [{name: "home\\n", path: /}]\n',
            "browser.yaml: capture[0].name 'home\\n' may only use letters, digits, _ and -")


@pytest.mark.parametrize("text,message", BAD_NUMBERS)
def test_a_bool_is_not_an_int(text, message):
    refused(text, message)


@pytest.mark.parametrize("text,message", PAST_THE_CAP)
def test_a_timeout_past_its_cap_is_refused_naming_the_bound(text, message):
    refused(text, message)


def test_the_bounds_themselves_are_allowed():
    assert parse_browser_config("serve:\n  cmd: x\n  ready_timeout: 180\n"
                                )["serve"]["ready_timeout"] == 180
    assert parse_browser_config(MINIMAL + "capture: [{name: home, path: /, timeout: 60}]\n"
                                )["capture"][0]["timeout"] == 60


def test_the_activities_read_a_bad_file_as_a_value_and_never_a_raise(tmp_path):
    """A file edited after `lg start` reaches the round as evidence for the
    prompt, so the activities branch on the error instead of parking the item.
    `lg start` still gets the raise, because it has a person to print it to."""
    assert read_browser_config(str(tmp_path)) is None
    # No exists() check to race against: a file deleted between the look and the
    # read reads as absent, which is what a run with no web app looks like.
    assert read_browser_config(str(tmp_path / "no-such-run")) is None
    path = tmp_path / "browser.yaml"
    path.write_text(MINIMAL)
    assert read_browser_config(str(tmp_path)) == parse_browser_config(MINIMAL)
    path.write_text("serve:\n  cmd: x\n  port: 3000\n")
    bad = read_browser_config(str(tmp_path))
    assert isinstance(bad, BrowserConfigError)
    assert bad.message == PORT_MESSAGE
    with pytest.raises(ValueError) as err:
        load_browser_config(str(path))
    assert str(err.value) == PORT_MESSAGE


def test_a_file_the_engine_cannot_read_is_an_error_value_too(tmp_path):
    """Never raising has to hold for the filesystem as well as for the parser.
    An activity that raised here would be retried by Temporal and park the item,
    which is the outcome the error-as-a-value design is there to avoid.

    A directory in the file's place is the case that reproduces on any machine;
    a mode-000 file and a disk that has gone away arrive the same way."""
    (tmp_path / "browser.yaml").mkdir()
    bad = read_browser_config(str(tmp_path))
    assert isinstance(bad, BrowserConfigError)
    # The prefix every other message carries, and one line, because the prompt
    # block and the owner's note print this one the same way as the rest.
    assert bad.message.startswith("browser.yaml: ")
    assert "\n" not in bad.message


# ---------- the port and the environments ----------


@pytest.fixture
def reservations():
    """Hand back whatever a test reserves.

    `_reserved` is module state that outlives a test, and a number left in it is
    a number `pick_port` will refuse for the rest of the session."""
    held = set(browser._reserved)
    yield browser._reserved
    browser._reserved.clear()
    browser._reserved.update(held)


def test_two_picks_in_a_row_differ_even_when_the_os_offers_the_same_port(
        monkeypatch, reservations):
    """The window the reservation exists for. Between `pick_port` returning and
    the dev server calling bind the port is free again, so the OS can offer the
    same number to the next activity in this worker, which is then two servers
    on one socket. Bind-and-release alone cannot see that."""
    offered = iter([7000, 7000, 7001])
    monkeypatch.setattr(browser, "_bind_free_port", lambda: next(offered))
    assert pick_port() == 7000
    assert pick_port() == 7001


def test_release_port_frees_a_port_for_the_next_pick(monkeypatch, reservations):
    """A serve that has stopped is not holding its port any more, and a worker
    that ran all day would otherwise refuse every number it had ever used."""
    monkeypatch.setattr(browser, "_bind_free_port", lambda: 7000)
    assert pick_port() == 7000
    release_port(7000)
    assert pick_port() == 7000
    # A serve that never started still gets stopped, so this has to be a no-op.
    release_port(65000)


def test_app_env_carries_exactly_the_three_keys():
    """The serve command and both models get these three laid over the worker's
    own environment. A dict of the whole environment here would hand a model the
    Telegram token that `NO_TELEGRAM` is there to blank."""
    env = app_env(4321)
    assert env == {"LOOPGRAPH_PORT": "4321",
                   "LOOPGRAPH_APP_URL": "http://127.0.0.1:4321",
                   "LOOPGRAPH_BROWSER_WS": browser_endpoint()}
    assert "TELEGRAM_BOT_TOKEN" not in env


def test_gate_env_carries_only_the_browser_endpoint(tmp_path):
    """AC-8. A gate gets the browser and never the app: `checkpoint_write_set`
    re-runs the same gates.yaml in its own activity with no serve alive, so a
    gate that reached $LOOPGRAPH_APP_URL would be green in the round and red at
    the commit, and the accepted item would park with its work thrown away.

    Whether the file parses is not the question either. A run that declares a web
    app in a broken file still runs its gates."""
    assert gate_env(str(tmp_path)) is None
    (tmp_path / "browser.yaml").write_text("serve:\n  cmd: x\n  port: 3000\n")
    assert gate_env(str(tmp_path)) == {"LOOPGRAPH_BROWSER_WS": browser_endpoint()}


def test_browser_port_reads_the_env_and_falls_back_to_8420(monkeypatch):
    """One port for the whole stack: compose starts Chromium on it and every
    client attaches to it, so the default here and the compose default are the
    same number or the container is unreachable on a machine that set neither."""
    monkeypatch.setenv("LOOPGRAPH_BROWSER_PORT", "8431")
    assert browser_port() == 8431
    assert browser_endpoint() == "http://127.0.0.1:8431"
    monkeypatch.delenv("LOOPGRAPH_BROWSER_PORT")
    assert browser_port() == DEFAULT_BROWSER_PORT == 8420
    assert browser_endpoint() == "http://127.0.0.1:8420"
    # `LOOPGRAPH_BROWSER_PORT=` with nothing after it is what a hand-written .env
    # looks like, and neither that nor a typo may take a round down: nothing
    # browser-shaped raises out of a round.
    monkeypatch.setenv("LOOPGRAPH_BROWSER_PORT", "")
    assert browser_port() == 8420
    monkeypatch.setenv("LOOPGRAPH_BROWSER_PORT", "no-such-port")
    assert browser_port() == 8420


# ---------- the serve process ----------
#
# Every serve in here is a real local process, so every test goes through
# `serving`: one left behind holds a port and a pytest worker for the rest of
# the session. Nothing is spawned but `python -m http.server`, `sleep` and
# `echo`, and the only address touched is the loopback the serve is on.


@asynccontextmanager
async def serving(cmd, workdir, ready_timeout=5, heartbeat=None):
    """A serve on a picked port, stopped whatever the test does with it."""
    port = pick_port()
    serve = await start_serve(cmd, str(workdir), port, ready_timeout, heartbeat=heartbeat)
    try:
        yield serve
    finally:
        await serve.stop()


async def http_get(url):
    """A GET written down a socket. urllib would read this machine's proxy
    variables; the point here is that the URL the serve reports is the one the
    app answers on."""
    host, port = url.removeprefix("http://").split(":")
    reader, writer = await asyncio.open_connection(host, int(port))
    writer.write(f"GET / HTTP/1.0\r\nHost: {host}:{port}\r\n\r\n".encode())
    await writer.drain()
    answer = await reader.read()
    writer.close()
    await writer.wait_closed()
    return answer


def assert_dead(pid):
    """`kill -0` until the process is gone. SIGKILL to the group is delivered at
    once but the kernel takes a moment to tear the process down, so reading the
    answer immediately is a race."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"pid {pid} outlived the serve it was started from")


def test_the_serve_runs_in_the_worktree(tmp_path):
    """The app under test is the round's worktree, not whatever directory the
    worker happens to be sitting in."""
    async def main():
        async with serving("pwd; exit 1", tmp_path) as serve:
            assert serve.ready is False
            assert str(tmp_path) in serve.output_tail
    asyncio.run(main())


def test_the_serve_runs_under_bash_with_pipefail(tmp_path):
    """The same shell a gate gets. `false | true` exits 0 under a shell without
    pipefail, so a serve run that way would sit out the whole ready_timeout
    waiting for a command that had already failed."""
    async def main():
        started = time.monotonic()
        async with serving("false | true", tmp_path, ready_timeout=5) as serve:
            waited = time.monotonic() - started
            assert "exited with code 1" in serve.output_tail
            assert waited < 3, f"waited {waited:.1f}s, so the pipeline's exit went unread"
        async with serving('echo "$0"; exit 1', tmp_path) as serve:
            assert "/bin/bash" in serve.output_tail
    asyncio.run(main())


def test_a_real_local_server_comes_up_ready(tmp_path):
    """The whole path, against a server that really listens: the command gets
    the port, the socket is what readiness means, the URL is the app's, and
    stopping hands the port back."""
    cmd = f"{sys.executable} -m http.server $LOOPGRAPH_PORT --bind 127.0.0.1"
    async def main():
        async with serving(cmd, tmp_path, ready_timeout=30) as serve:
            assert serve.ready is True, serve.output_tail
            assert serve.url == f"http://127.0.0.1:{serve.port}"
            assert (await http_get(serve.url)).startswith(b"HTTP/1.0 200")
            port = serve.port
        assert port not in browser._reserved
    asyncio.run(main())


def test_the_command_sees_the_app_env(tmp_path):
    """A dev server is told which port to bind through the environment, so a
    command that cannot read it cannot come up on the port the engine picked."""
    async def main():
        async with serving('echo "$LOOPGRAPH_APP_URL"; exit 1', tmp_path) as serve:
            assert serve.url in serve.output_tail
    asyncio.run(main())


def test_a_command_that_never_listens_times_out_with_its_tail(tmp_path):
    """AC-5. The round still runs, with the last of the output as the evidence
    the executor's prompt block carries, and nothing of the serve is left."""
    pid_file = tmp_path / "pid"
    async def main():
        async with serving(f"echo starting; sleep 30 & echo $! > {pid_file}; wait",
                           tmp_path, ready_timeout=1) as serve:
            assert serve.ready is False
            assert "starting" in serve.output_tail
    asyncio.run(main())
    assert_dead(int(pid_file.read_text()))


def test_a_command_that_exits_non_zero_gives_up_before_the_timeout(tmp_path):
    """A command that has failed is not going to bind anything, and waiting out
    a three-minute ready_timeout for it is three minutes of the round's budget.
    A shell that exits 0 is not this case: a dev server that daemonises does
    exactly that, which is why the port is the only readiness signal."""
    async def main():
        started = time.monotonic()
        async with serving("echo boom; exit 3", tmp_path, ready_timeout=30) as serve:
            waited = time.monotonic() - started
            assert serve.ready is False
            assert serve.output_tail.splitlines() == [
                "serve command exited with code 3 before the port answered", "boom"]
            assert waited < 3, f"waited {waited:.1f}s for a command that had already exited"
    asyncio.run(main())


def test_a_shell_that_exits_zero_is_not_a_give_up(tmp_path):
    """The other half of the same rule. A command that daemonises leaves the
    shell exiting 0 with the real server still coming up behind it, so an exit
    code of 0 says nothing and only the port does. The second the server waits
    is the whole test: without it the port answers in the same poll the shell
    exits in, and a give-up on any exit at all would go unnoticed."""
    pid_file = tmp_path / "pid"
    cmd = (f"(sleep 1; exec {sys.executable} -m http.server $LOOPGRAPH_PORT "
           f"--bind 127.0.0.1) & echo $! > {pid_file}; exit 0")
    async def main():
        async with serving(cmd, tmp_path, ready_timeout=30) as serve:
            assert serve.ready is True, serve.output_tail
    asyncio.run(main())
    assert_dead(int(pid_file.read_text()))


def test_a_serve_that_could_not_start_at_all_is_a_result_and_not_a_raise(tmp_path):
    """Nothing browser-shaped raises out of a round. A worktree that is not there
    reaches the prompt as a serve that failed, like every other way one fails."""
    async def main():
        async with serving("echo hi", tmp_path / "gone", ready_timeout=1) as serve:
            assert serve.ready is False
            assert "No such file or directory" in serve.output_tail
            assert "\n" not in serve.output_tail
    asyncio.run(main())


def test_stop_kills_the_whole_group(tmp_path):
    """AC-4. `npm run dev` forks the real server, so killing the shell alone
    leaves it holding the port for the next round. The command here has both
    shapes: a child that outlives the shell, and a shell that becomes the
    server."""
    pid_file = tmp_path / "pid"
    cmd = (f"sleep 300 & echo $! > {pid_file}; "
           f"exec {sys.executable} -m http.server $LOOPGRAPH_PORT --bind 127.0.0.1")
    async def main():
        async with serving(cmd, tmp_path, ready_timeout=30) as serve:
            assert serve.ready is True, serve.output_tail
    asyncio.run(main())
    assert_dead(int(pid_file.read_text()))


def test_the_heartbeat_is_called_while_waiting(tmp_path):
    """AC-9. The activity's heartbeat_timeout is three minutes and a serve may
    wait up to 180 seconds, so a wait that says nothing is a wait Temporal can
    decide is a dead worker."""
    beats = []
    pid_file = tmp_path / "pid"
    async def main():
        async with serving(f"sleep 30 & echo $! > {pid_file}; wait", tmp_path,
                           ready_timeout=2, heartbeat=beats.append) as serve:
            assert serve.ready is False
    asyncio.run(main())
    assert "serve waiting (2s)" in beats, beats
    assert_dead(int(pid_file.read_text()))


def test_stop_is_idempotent(tmp_path):
    """Both activities stop the serve in a finally, and a round that ended badly
    can reach that finally with a serve that never came up or one already
    stopped."""
    cmd = f"{sys.executable} -m http.server $LOOPGRAPH_PORT --bind 127.0.0.1"
    async def main():
        # `serving` has already stopped both of these once by the time the second
        # stop below runs.
        async with serving(cmd, tmp_path, ready_timeout=30) as ready:
            assert ready.ready is True, ready.output_tail
        await ready.stop()
        assert ready.port not in browser._reserved
        async with serving("exit 1", tmp_path, ready_timeout=1) as never:
            assert never.ready is False
        await never.stop()
    asyncio.run(main())


# ---------- the browser-container checklist ----------
#
# Run this by hand whenever the browser wiring changes. The three tests above
# read three files as text. Nothing in this suite has attached to Chromium,
# opened a page or looked at a PNG, and it never will: the gate has to pass with
# no browser container and no network. Six criteria need a real browser, and
# none of them can be faked — a capture on disk, a worktree left clean, an
# origin refused, two sessions that cannot read each other's cookies, the
# service compose declares and the clients the worker image carries.
#
# The order below is the running order rather than the spec's numbering, because
# nothing after item 1 works without the container up and item 3 needs the
# worker of item 2. Both of those touch the owner's live stack, so ask first:
#
#   COMPOSE_PROFILES=browser in .env, then
#   docker compose --profile browser up -d browser
#   docker compose up -d --build worker
#
# Items 1 to 3 were run the day this file was written, against a container and
# an image started by hand rather than the live stack, and each one records what
# it answered. Items 4 to 6 need the engine's own capture and MCP wiring, which
# lands later in this phase; until then they go unrun, and they are written down
# now so that nobody has to work out afterwards what was never checked.
#
#  1. AC-18, the service. With nothing set in the environment:
#       docker compose config --services | grep -c '^browser$'
#     then the same line with COMPOSE_PROFILES=browser in front of it.
#     See: 0, then 1, and the other five services unchanged either way.
#     Bring the service up and ask Chromium who it is:
#       curl -s http://127.0.0.1:8420/json/version
#     See: JSON carrying a webSocketDebuggerUrl of the shape
#     ws://127.0.0.1:8420/devtools/browser/<uuid>. Nothing listening means the
#     glob in the compose command found no chrome, so read the real directory
#     names:
#       docker run --rm mcr.microsoft.com/playwright:v1.63.0-noble ls /ms-playwright
#     Run 2026-09-09. The two config lines answered 0 and 1. The container was
#     started by hand with the compose service's own command:
#       docker run --rm -d --network host --ipc host --init \
#         --name lg-browser-spike mcr.microsoft.com/playwright:v1.63.0-noble \
#         /bin/sh -c 'exec /ms-playwright/chromium-*/chrome-linux*/chrome ...'
#     and answered Chrome/153.0.8010.12 with
#     ws://127.0.0.1:8420/devtools/browser/1a4c30b8-fd94-4fb0-9393-69ad6f347d0f.
#     The glob resolved to /ms-playwright/chromium-1243/chrome-linux64/chrome:
#     the image also holds chromium_headless_shell-1243, which chromium-* does
#     not match, and this tag uses the chrome-linux64 layout.
#
#  2. AC-19, the clients. Rebuild the worker, then ask both of them:
#       docker compose exec -T worker playwright-mcp --version
#       docker compose exec -T worker python -c 'import importlib.metadata as m; print(m.version("playwright"))'
#     See: `Version 0.0.80`, and 1.62.0 or newer. Then prove no browser came
#     with them:
#       docker compose exec -T worker sh -c 'ls /home/worker/.cache /ms-playwright'
#     See: both missing. A browser in this image is a gigabyte nothing opens —
#     Chromium lives in the browser container and both clients attach to it.
#     Run 2026-09-09 against `docker build -t loopgraph-worker-phase-test .` and
#     `docker run --rm` of the three commands: Version 0.0.80, 1.62.0, and both
#     directories absent. The image went from 2.27GB to 2.52GB, which is the MCP
#     server and the Python client and no browser.
#
#  3. AC-17, the cookie jars. Three clients on one Chromium at the same time:
#     two MCP sessions started with the entry the engine hands both models
#     (--cdp-endpoint $LOOPGRAPH_BROWSER_WS --isolated) and the engine's own
#     capture client. Serve something to set a cookie on — `python -m
#     http.server 8477` in any directory does — point all three at it, have each
#     MCP session run `document.cookie = 'mcp_one=1; path=/'` through
#     browser_evaluate, and read document.cookie back in all three.
#     See: each session reads its own cookie and only its own, and the capture
#     client reads the empty string. Anything else means --isolated is not
#     reaching the server and every client is sharing the one default context,
#     where one model's login is the other model's session.
#     Run 2026-09-09 from the host against item 1's container. The two sessions
#     were driven over stdio JSON-RPC — `npx -y @playwright/mcp@0.0.80` with the
#     entry's own flags, initialize, then tools/call — because no agent had been
#     wired to the server yet; the capture client was connect_over_cdp plus
#     new_context. Session one read "mcp_one=1", session two "mcp_two=2", the
#     capture client "". Three connect_over_cdp clients with no MCP in the
#     picture answered the same way.
#
#  4. AC-16, the blocked origins. From the supervisor's browser, navigate to
#     http://127.0.0.1:8400, the owner's dashboard.
#     See: the navigation aborted, net::ERR_BLOCKED_BY_CLIENT. Then point the
#     same session at an app under test that pulls a font off a CDN and list the
#     requests it made.
#     See: the font arrived. The list names the engine's own loopback services
#     and nothing else, so a page that cannot reach a CDN is a list that has
#     grown past what it is for and is now blocking the app the models are meant
#     to be looking at.
#     The first half was run 2026-09-09 with item 3's session: browser_navigate
#     to http://127.0.0.1:8400 answered
#     `net::ERR_BLOCKED_BY_CLIENT at http://127.0.0.1:8400/`. The second half
#     wants an app under test and waits for the engine's wiring.
#
#  5. AC-10, the captures. Run an item whose browser.yaml declares pages, and
#     look in <run_dir>/shots/i<N>-r<M>/ when the round ends.
#     See: one PNG per capture entry, named as the entry named it, the whole
#     page, as wide as it asked for. Open one. A dev server that answered 500
#     screenshots exactly as well as one that worked, so the check is that the
#     picture is the app.
#     Then, in the same run's worktree, `ls shots` — there is no such directory.
#     Every path the engine writes is under the run directory.
#     Needs the engine's capture wiring; unrun as this file is written.
#
#  6. AC-13, the worktree. After an executor round in which the model took a
#     screenshot of its own, in that run's worktree:
#       git status --porcelain
#     See: nothing. The MCP server writes under <run_dir>/scratch/playwright/
#     and the executor prompt tells the model never to pass a filename to a
#     browser tool, because a named file lands in the server's working
#     directory, which is the worktree. If a file is there anyway, the audit
#     prompt's write-set mismatch block names it — that is the net underneath
#     working, and this item has still failed.
#     Needs the engine's MCP wiring; unrun as this file is written.
