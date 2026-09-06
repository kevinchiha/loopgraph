"""What a release is, and what moving to one costs, shared by `lg` and `ui.py`.

Here for the reason `envfile.py` is here: `lg` has no `.py` extension, so
nothing can import from it, and both the command and the dashboard need the same
answer to "is there a newer release than this checkout".

Most of it is a function over text — a tag name, the changelog, two
`pyproject.toml` texts, two `.env.example` texts — so the awkward cases can be
tested against the file shapes the repo really produces without a repository to
run them in. The two that do read git, `installed` and `remote_newest`, take the
checkout to read as an argument and run every command with `cwd=root`, so
nothing in here decides where the checkout is.
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from collections.abc import Iterable

from envfile import parse_env

# A release tag and nothing else. `git describe --match 'v*'` happily names a
# hand-made `v1.2.3-rc1`, and every caller has to be able to tell that apart.
_TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")

# A changelog section heading, as CHANGELOG.md and release.sh write it. The
# version's dots are escaped: `0x4x0` is not `0.4.0`.
_SECTION = re.compile(r"## (\d+\.\d+\.\d+) - ")


def parse_version(tag: str) -> tuple[int, int, int] | None:
    m = _TAG.fullmatch(tag)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def newest(tags: Iterable[str]) -> str | None:
    """The highest release among these tags, or None when none of them is one.

    Sorted by the parsed triple, never by the text: v0.10.0 comes after v0.9.0.
    """
    releases = [(v, t) for t in tags if (v := parse_version(t)) is not None]
    return max(releases)[1] if releases else None


def _sections(text: str) -> list[tuple[str | None, list[str]]]:
    """The changelog cut into its level-two sections, in file order.

    Each is the version its heading names, or None for `## Unreleased`, and the
    lines from the heading to the one before the next `## `. Anything above the
    first heading belongs to no section. Only the heading line is ever matched,
    so a bullet that quotes `## 0.3.0` stays body text.
    """
    out: list[tuple[str | None, list[str]]] = []
    for line in text.splitlines():
        if line.startswith("## "):
            m = _SECTION.match(line)
            out.append((m[1] if m else None, [line]))
        elif out:
            out[-1][1].append(line)
    return out


def changelog_between(text: str, old: str | None, new: str) -> str:
    """The notes a move from `old` to `new` gains, headings included.

    Both are tag names, as `installed` and `remote_newest` hand them over;
    `old` is None on a checkout with no tag, and an `old` that is not a release
    tag counts the same way, so a `v1.2.3-rc1` prints everything instead of
    raising in the middle of a finished update. `Unreleased` is never returned:
    it names work that has not shipped to anyone.
    """
    ceiling = parse_version(new)
    if ceiling is None:
        return ""
    floor = parse_version(old) if old else None
    wanted = []
    for released, lines in _sections(text):
        if released is None:
            continue
        here = parse_version(f"v{released}")
        if here is None or here > ceiling or (floor is not None and here <= floor):
            continue
        while lines and not lines[-1].strip():
            lines.pop()
        wanted.append("\n".join(lines))
    return "\n\n".join(wanted)


def changelog_has(text: str, version: str) -> bool:
    """Whether the changelog already has a dated section for this bare version.

    The bare version as the heading and pyproject.toml write it — `0.4.0`, no
    `v` — which is what both callers hold.
    """
    return any(v == version for v, _ in _sections(text))


def _installs(text: str) -> tuple[list, str] | None:
    """The two fields of a pyproject.toml that change what gets installed.

    None when there is nothing to read: an unparsable text, or the empty string
    `git show <commit>:pyproject.toml` returns when the file was not there.
    """
    if not text.strip():
        return None
    try:
        project = tomllib.loads(text).get("project", {})
    except tomllib.TOMLDecodeError:
        return None
    return (project.get("dependencies", []), project.get("requires-python", ""))


def deps_changed(old_pyproject: str, new_pyproject: str) -> bool:
    """Whether the update changes what has to be installed.

    Content, not the path list, on purpose: release.sh rewrites the `version = `
    line in every release commit, so pyproject.toml is in the diff of every
    update and a path rule would rebuild and re-sync every time, docs-only
    releases included. A text nobody could read counts as changed — a needless
    refresh costs seconds, a skipped one leaves `lg` unable to import.
    """
    old, new = _installs(old_pyproject), _installs(new_pyproject)
    return old is None or new is None or old != new


def needs_venv(old_pyproject: str, new_pyproject: str) -> bool:
    """The host venv needs a refresh exactly when the dependencies moved."""
    return deps_changed(old_pyproject, new_pyproject)


def stack_action(changed: list[str], old_pyproject: str, new_pyproject: str) -> str:
    """What the running stack needs after this update: build, recreate, restart.

    The container's code is the bind-mounted checkout, so most updates are a
    restart. A compose change needs `up -d` instead, because `restart` re-runs
    the old containers with the configuration they were created from. Paths
    compare whole: `docs/Dockerfile.md` is not the Dockerfile.
    """
    if "Dockerfile" in changed or deps_changed(old_pyproject, new_pyproject):
        return "build"
    if "docker-compose.yml" in changed:
        return "recreate"
    return "restart"


def new_settings(old_example: str, new_example: str, env: dict[str, str]) -> list[str]:
    """The keys this update introduced that the user's .env does not answer yet.

    The two examples are diffed against each other rather than .env against the
    whole example: the example carries the route the user did not take as
    commented-out lines, and install.sh writes keys the example never lists, so
    comparing against the whole file would nag every install on every update. A
    key that was commented in the old example and is uncommented in the new one
    is new, and rightly: the release just made it a decision for the user.
    """
    old = parse_env(old_example)
    return [k for k in parse_env(new_example) if k not in old and k not in env]


def is_behind(installed: str | None, latest: str | None) -> bool:
    """Whether `latest` is a release worth moving to from `installed`.

    An unreadable `installed` counts as no tag at all, so a checkout sitting on
    a hand-made `v1.2.3-rc1` is offered the release instead of crashing the
    caller: a triple compared with None is a TypeError that would take down
    `lg version` and the dashboard's checker thread.
    """
    there = parse_version(latest) if latest else None
    if there is None:
        return False
    here = parse_version(installed) if installed else None
    return here is None or there > here


class RemoteError(Exception):
    """Why the remote could not be asked what it has, in one line for a person."""


def _git(root: str | os.PathLike, argv: list[str],
         timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """One git command in `root`, which never raises on a non-zero exit.

    check=True is deliberately absent, because every caller below has its own
    answer to a failure: a checkout with no tag is not an error, a directory that
    is not a checkout is not one either, and the remote read wants git's own
    message rather than a CalledProcessError nobody would want to read.
    """
    return subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=timeout)


def installed(root: str | os.PathLike) -> dict:
    """What this checkout is: its nearest release tag, its commit, its distance.

    `ahead` is the commits HEAD has past that tag, which is what tells a checkout
    sitting on a release apart from one someone has been committing to since.

    Nothing here raises. `lg version` prints this and the dashboard's header
    shows it, and neither has anywhere useful to put an exception; a tree
    unpacked from an archive rather than cloned answers None to both names.
    """
    described = _git(root, ["git", "describe", "--tags", "--abbrev=0", "--match", "v*"])
    tag = described.stdout.strip() if described.returncode == 0 else None
    head = _git(root, ["git", "rev-parse", "--short", "HEAD"])
    commit = head.stdout.strip() if head.returncode == 0 else None
    ahead = 0
    if tag is not None:
        counted = _git(root, ["git", "rev-list", "--count", f"{tag}..HEAD"])
        if counted.returncode == 0:
            ahead = int(counted.stdout)
    return {"tag": tag, "commit": commit, "ahead": ahead}


def remote_newest(root: str | os.PathLike, timeout: float) -> str | None:
    """The highest release tag origin has, or None when it has none yet.

    ls-remote rather than the GitHub API: it needs no token and no rate limit,
    and it answers from the same remote the update would fetch from. It is also
    the only thing in this module that touches the network, which is why it is
    the only one with a deadline — a host that swallows packets keeps git waiting
    until the TCP stack gives up, and one caller is a dashboard thread.
    """
    try:
        listed = _git(root, ["git", "ls-remote", "--tags", "--refs", "origin"], timeout)
    except subprocess.TimeoutExpired:
        raise RemoteError(f"timed out after {timeout:g}s") from None
    if listed.returncode != 0:
        said = next((ln.strip() for ln in listed.stderr.splitlines() if ln.strip()), "")
        raise RemoteError(said or f"git ls-remote exited {listed.returncode}")
    tags = []
    for line in listed.stdout.splitlines():
        # <hash><tab><ref> per line, and --refs has already dropped the peeled
        # `^{}` entry an annotated tag would otherwise add beside its own.
        columns = line.split()
        if len(columns) > 1:
            tags.append(columns[1].removeprefix("refs/tags/"))
    return newest(tags)
