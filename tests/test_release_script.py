"""What `./release.sh X.Y.Z` refuses, and what it writes when it does not.

The script is never run from this checkout: it commits, tags and pushes. Every
test copies it into the `clone_repo` fixture's temporary clone and runs that
copy, so `ROOT` — which the script takes from its own location — is the clone,
and the push lands in the bare repository beside it under tmp_path.

The copy is also run from outside the clone, for the same reason: a script that
read the caller's directory would release whichever repository the maintainer
happened to be standing in.

This is a separate file from tests/test_release.py, which is the publishing
guard (no credentials, no personal paths, no real runs tracked) and stays one.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "release.sh"

CHANGELOG = """# Changelog

## Unreleased

- The dashboard says which release you have.
"""

PYPROJECT = """[project]
name = "loopgraph-engine"
version = "0.1.0"
requires-python = ">=3.13"
"""


def _plant(clone_repo, changelog: str = CHANGELOG, pyproject: str = PYPROJECT) -> None:
    """The clone as a maintainer's checkout: the script, a changelog, a version.

    The files are staged by hand rather than through the fixture's `commit`,
    because the mode has to be on the script before git records it: a copy git
    stored as 0644 could not be run at all.
    """
    script = clone_repo.root / "release.sh"
    shutil.copyfile(SCRIPT, script)
    script.chmod(0o755)
    (clone_repo.root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    (clone_repo.root / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    clone_repo.git("add", "release.sh", "CHANGELOG.md", "pyproject.toml")
    clone_repo.git("commit", "-m", "the release script and the files it edits")
    clone_repo.push()


def _release(clone_repo, *args: str, release_tests: str | None = "true"):
    """Run the clone's copy. `release_tests` of None removes RELEASE_TESTS."""
    env = dict(os.environ)
    if release_tests is None:
        env.pop("RELEASE_TESTS", None)
    else:
        env["RELEASE_TESTS"] = release_tests
    return subprocess.run([str(clone_repo.root / "release.sh"), *args],
                          cwd=clone_repo.root.parent, capture_output=True, text=True, env=env)


def _read(clone_repo, name: str) -> str:
    return (clone_repo.root / name).read_text(encoding="utf-8")


def _tags(clone_repo) -> list[str]:
    return clone_repo.git("tag", "-l").split()


# ------------------------------------------------------------------ refusals ---

@pytest.mark.parametrize("given", [["1.2"], ["v1.2.3"], []], ids=["short", "v-prefixed", "none"])
def test_refuses_a_version_that_is_not_x_y_z(clone_repo, given):
    """The argument becomes a tag, a heading and pyproject's version, so a typo
    caught here is a typo that never reaches origin."""
    _plant(clone_repo)
    done = _release(clone_repo, *given)
    assert done.returncode == 1
    arg = given[0] if given else ""
    assert done.stderr.strip() == f"refuse: version must look like X.Y.Z, got '{arg}'"
    assert not _tags(clone_repo)


def test_refuses_off_main(clone_repo):
    """Releases come off main. A tag on a branch nobody merged names a commit
    that is not in the history anyone else has."""
    _plant(clone_repo)
    clone_repo.git("checkout", "-q", "-b", "side")
    done = _release(clone_repo, "0.2.0")
    assert done.returncode == 1
    assert done.stderr.strip() == "refuse: on branch side, not main."


def test_refuses_a_detached_head(clone_repo):
    """`git branch --show-current` says nothing here, and "on branch , not main"
    would tell the maintainer nothing about what is wrong."""
    _plant(clone_repo)
    clone_repo.git("checkout", "-q", "--detach")
    done = _release(clone_repo, "0.2.0")
    assert done.returncode == 1
    assert done.stderr.strip() == "refuse: not on a branch (detached HEAD)."


def test_refuses_uncommitted_changes(clone_repo):
    """Whatever is in the tree is what the tag will mean, so it has to be what
    is committed."""
    _plant(clone_repo)
    (clone_repo.root / "README.md").write_text("# edited\n", encoding="utf-8")
    done = _release(clone_repo, "0.2.0")
    assert done.returncode == 1
    assert done.stderr.strip() == "refuse: uncommitted changes. Commit or stash them first."


def test_refuses_when_main_is_not_at_origin_main(clone_repo):
    """One unpushed commit. Tagging here names a commit nobody can fetch."""
    _plant(clone_repo)
    clone_repo.commit("local only", {"a.txt": "a\n"})
    done = _release(clone_repo, "0.2.0")
    assert done.returncode == 1
    assert done.stderr.strip() == "refuse: main is not at origin/main. Pull or push first."


def test_refuses_a_tag_that_already_exists_locally(clone_repo):
    _plant(clone_repo)
    clone_repo.tag("v0.2.0")
    done = _release(clone_repo, "0.2.0")
    assert done.returncode == 1
    assert done.stderr.strip() == "refuse: tag v0.2.0 already exists locally."


def test_refuses_a_tag_that_already_exists_on_origin(clone_repo):
    """Someone else released it. Nothing local says so, which is what the
    ls-remote check is for."""
    _plant(clone_repo)
    clone_repo.tag("v0.2.0")
    clone_repo.push()
    clone_repo.git("tag", "-d", "v0.2.0")
    done = _release(clone_repo, "0.2.0")
    assert done.returncode == 1
    assert done.stderr.strip() == "refuse: tag v0.2.0 already exists on origin."


def test_refuses_an_empty_unreleased_with_no_version_section(clone_repo):
    """A release with no notes is a release nobody can read. `lg update` prints
    these sections, and an empty one tells a user nothing about what changed."""
    _plant(clone_repo, changelog="# Changelog\n\n## Unreleased\n\n")
    done = _release(clone_repo, "0.2.0")
    assert done.returncode == 1
    assert done.stderr.strip() == (
        "refuse: CHANGELOG.md has nothing under ## Unreleased and no ## 0.2.0 section.")


def test_refuses_when_the_tests_fail_and_writes_nothing(clone_repo):
    """The tests run last, before the first write, so a red suite leaves the
    checkout exactly as it was rather than half-released."""
    _plant(clone_repo)
    head = clone_repo.git("rev-parse", "HEAD")
    done = _release(clone_repo, "0.2.0", release_tests="false")
    assert done.returncode == 1
    assert done.stderr.strip() == "refuse: tests failed; nothing released."
    assert _read(clone_repo, "CHANGELOG.md") == CHANGELOG
    assert _read(clone_repo, "pyproject.toml") == PYPROJECT
    assert not _tags(clone_repo)
    assert clone_repo.git("rev-parse", "HEAD") == head


# ------------------------------------------------------------- what it writes ---

def test_releases_renames_bumps_commits_tags_and_pushes(clone_repo):
    _plant(clone_repo)
    done = _release(clone_repo, "0.2.0")
    assert done.returncode == 0, done.stderr
    assert "released 0.2.0 (tag v0.2.0 pushed)" in done.stdout

    changelog = _read(clone_repo, "CHANGELOG.md")
    assert re.match(r"# Changelog\n\n## Unreleased\n\n## 0\.2\.0 - \d{4}-\d{2}-\d{2}\n", changelog)
    assert 'version = "0.2.0"' in _read(clone_repo, "pyproject.toml")

    assert clone_repo.git("log", "-1", "--format=%s") == "Release 0.2.0"
    touched = clone_repo.git("show", "--name-only", "--format=", "HEAD").split()
    assert touched == ["CHANGELOG.md", "pyproject.toml"]

    assert clone_repo.git("cat-file", "-t", "v0.2.0") == "tag", "the tag is annotated"
    assert clone_repo.git("for-each-ref", "--format=%(contents:subject)",
                          "refs/tags/v0.2.0") == "0.2.0"

    head = clone_repo.git("rev-parse", "HEAD")
    assert clone_repo.git("rev-parse", "main", cwd=clone_repo.origin) == head
    assert clone_repo.git("rev-parse", "refs/tags/v0.2.0^{commit}",
                          cwd=clone_repo.origin) == head


def test_the_intro_survives_a_release_byte_for_byte(clone_repo):
    """The shipped file names `## Unreleased` twice in its intro, above the real
    heading. An unanchored sed or str.replace would rewrite those two mentions on
    the maintainer's very first release."""
    shipped = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    _plant(clone_repo, changelog=shipped)
    before, _ = _split_at_unreleased(shipped)
    assert before.count("`## Unreleased`") == 2, "the fixture is not the shipped file"

    done = _release(clone_repo, "0.2.0")
    assert done.returncode == 0, done.stderr

    after, rest = _split_at_unreleased(_read(clone_repo, "CHANGELOG.md"))
    assert after == before
    assert re.match(r"## Unreleased\n\n## 0\.2\.0 - \d{4}-\d{2}-\d{2}\n", rest)


def _split_at_unreleased(text: str) -> tuple[str, str]:
    """The text above the first whole-line `## Unreleased`, and the rest."""
    lines = text.splitlines(keepends=True)
    at = next(n for n, line in enumerate(lines) if line.rstrip("\n") == "## Unreleased")
    return "".join(lines[:at]), "".join(lines[at:])


def test_leaves_the_changelog_alone_when_the_section_already_exists(clone_repo):
    """The maintainer wrote the dated heading by hand. Renaming Unreleased on top
    of it would produce two 0.2.0 sections."""
    written = ("# Changelog\n\n## Unreleased\n\n- next time.\n\n"
               "## 0.2.0 - 2026-09-01\n\n- this time.\n")
    _plant(clone_repo, changelog=written)
    done = _release(clone_repo, "0.2.0")
    assert done.returncode == 0, done.stderr
    assert _read(clone_repo, "CHANGELOG.md") == written
    assert 'version = "0.2.0"' in _read(clone_repo, "pyproject.toml")
    assert clone_repo.git("log", "-1", "--format=%s") == "Release 0.2.0"


def test_tags_head_without_a_new_commit_when_both_files_are_already_at_the_release(clone_repo):
    """A run that died between the commit and the tag, re-run. `git commit` with
    nothing staged exits 1, and under `set -e` that would kill the script with no
    tag, no push and no reason printed."""
    _plant(clone_repo,
           changelog="# Changelog\n\n## 0.2.0 - 2026-09-01\n\n- shipped.\n",
           pyproject=PYPROJECT.replace("0.1.0", "0.2.0"))
    head = clone_repo.git("rev-parse", "HEAD")

    done = _release(clone_repo, "0.2.0")
    assert done.returncode == 0, done.stderr
    assert "nothing to commit; tagging HEAD" in done.stdout
    assert clone_repo.git("rev-parse", "HEAD") == head, "it committed anyway"
    assert clone_repo.git("rev-parse", "refs/tags/v0.2.0^{commit}",
                          cwd=clone_repo.origin) == head


# ------------------------------------------------------- the default test run ---

def _fake_python(clone_repo, record: Path, exit_code: int) -> None:
    """A .venv/bin/python that writes down how it was called and exits to order.

    Untracked on purpose: `git status --porcelain --untracked-files=no` is what
    the script asks, so planting this does not itself look like a dirty tree.
    """
    fake = clone_repo.root / ".venv" / "bin" / "python"
    fake.parent.mkdir(parents=True)
    fake.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$*" >> {record}\nexit {exit_code}\n',
                    encoding="utf-8")
    fake.chmod(0o755)


def test_runs_the_default_test_command_when_release_tests_is_unset(clone_repo, tmp_path):
    """Nobody sets RELEASE_TESTS for a real release, so the default is the only
    thing standing between a red suite and a pushed tag."""
    _plant(clone_repo)
    record = tmp_path / "argv.txt"
    _fake_python(clone_repo, record, exit_code=0)

    done = _release(clone_repo, "0.2.0", release_tests=None)
    assert done.returncode == 0, done.stderr
    assert record.read_text(encoding="utf-8").strip() == "-m pytest -q"
    assert clone_repo.git("cat-file", "-t", "v0.2.0") == "tag"


def test_the_default_test_command_failing_refuses(clone_repo, tmp_path):
    _plant(clone_repo)
    _fake_python(clone_repo, tmp_path / "argv.txt", exit_code=1)

    done = _release(clone_repo, "0.2.0", release_tests=None)
    assert done.returncode == 1
    assert done.stderr.strip() == "refuse: tests failed; nothing released."
    assert _read(clone_repo, "CHANGELOG.md") == CHANGELOG
    assert _read(clone_repo, "pyproject.toml") == PYPROJECT
    assert not _tags(clone_repo)


def test_the_shipped_script_is_executable():
    """Copied, cloned or downloaded, `./release.sh` has to run without a chmod."""
    assert os.access(SCRIPT, os.X_OK)
