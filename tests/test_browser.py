"""The browser container and the two clients that attach to it.

The suite runs with no browser container and no network, so what it can check
here is the wiring: compose declares the service behind its profile, the worker
image carries the MCP server and the Python client, and neither of them pulls a
browser down. Everything that needs a real Chromium — a capture on disk, two
sessions that cannot read each other's cookies, an origin the MCP server refuses
to open — is the checklist below, run by hand.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

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
