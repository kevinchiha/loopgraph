"""A git repository with a remote, built under tmp_path, for the release tests.

`lg version`, `lg update`, the dashboard's version hint and the release guard all
read git, and the only honest way to test them is against a repository that
really has commits, tags and an origin. So `clone_repo` builds one: a clone on
`main` with a single commit, and a bare repository beside it standing in for
GitHub. Nothing here reaches the network — the "remote" is a directory.

The identity, the config files, the language and the search ceiling come from
environment variables set for the whole test, not from `-c` flags on these
commands, because the code under test spawns its own git. A signing key, a hook,
an `init.defaultBranch` or a translated error message would otherwise decide what
these tests assert, and the run would pass or fail depending on whose machine it
was on.

The fixture is `clone_repo` and not `repo`: tests/test_queue.py already has a
`repo` fixture that hands back a plain Path, and two names with two shapes in one
suite is a trap.
"""

from __future__ import annotations

import os
import subprocess
import types

import pytest


@pytest.fixture
def clone_repo(tmp_path, monkeypatch):
    for name, value in {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "lg",
        "GIT_AUTHOR_EMAIL": "lg@example.invalid",
        "GIT_COMMITTER_NAME": "lg",
        "GIT_COMMITTER_EMAIL": "lg@example.invalid",
        # git ships translations of its own error messages, so a test that reads
        # one is reading the machine's language unless the language is pinned.
        "LC_ALL": "C",
        # Nothing above tmp_path is ours: with TMPDIR inside a checkout, git
        # would walk out of the temporary tree and answer about that repository.
        "GIT_CEILING_DIRECTORIES": str(tmp_path),
    }.items():
        monkeypatch.setenv(name, value)

    root, origin = tmp_path / "clone", tmp_path / "origin.git"

    def git(*args, cwd=None) -> str:
        done = subprocess.run(["git", *args], cwd=cwd or root, check=True,
                              capture_output=True, text=True)
        return done.stdout.strip()

    def commit(message: str, files: dict[str, str]) -> str:
        for name, text in files.items():
            (root / name).write_text(text, encoding="utf-8")
        git("add", "--", *files)
        git("commit", "-m", message)
        return git("rev-parse", "--short", "HEAD")

    def tag(name: str) -> None:
        # Annotated, the way release.sh tags: a lightweight tag has no object of
        # its own, so `<tag>^{commit}` would resolve for the wrong reason.
        git("tag", "-a", name, "-m", name.removeprefix("v"))

    def push() -> None:
        git("push", "origin", "main", "--tags")

    git("init", "-b", "main", str(root), cwd=tmp_path)
    commit("initial", {"README.md": "# clone\n"})
    git("init", "--bare", str(origin), cwd=tmp_path)
    # So the bare repository has a HEAD to name, the way a fresh GitHub repo does.
    git("-C", str(origin), "symbolic-ref", "HEAD", "refs/heads/main")
    git("remote", "add", "origin", str(origin))
    git("push", "-u", "origin", "main")

    return types.SimpleNamespace(root=root, origin=origin, git=git, commit=commit,
                                 tag=tag, push=push)
