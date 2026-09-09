"""The browser container, the two clients that attach to it, and `browser.yaml`.

The suite runs with no browser container and no network, so what it can check
here is the wiring: compose declares the service behind its profile, the worker
image carries the MCP server and the Python client, and neither of them pulls a
browser down. Everything that needs a real Chromium — a capture on disk, two
sessions that cannot read each other's cookies, an origin the MCP server refuses
to open — is the checklist below, run by hand.

The declaration needs no browser at all: `browser.yaml` is text in, a dict or an
error out, so those are ordinary parser checks.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml

from activities.browser import (BrowserConfigError, load_browser_config,
                                parse_browser_config, read_browser_config)

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
