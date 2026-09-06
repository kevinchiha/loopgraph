"""The rules `lg version`, `lg update` and the dashboard all read releases by.

Most of it is pure functions over text: a tag name, a changelog, two
`pyproject.toml` texts, two `.env.example` texts. The point of keeping those
rules here rather than inside the commands is that a rule the user can hit —
"does this update need a rebuild", "is this key new" — can be tested against the
exact file shapes the repo really produces, and there is only ever one copy of
it.

The last section is the two that do run git, `installed` and `remote_newest`.
Those are tested against a real repository with a real origin, both under
tmp_path, because what they have to get right is git's own output and exit codes.
"""

from __future__ import annotations

import inspect
import re
import subprocess

import pytest

import version
from envfile import parse_env, read_env
from version import (
    RemoteError,
    changelog_between,
    changelog_has,
    deps_changed,
    installed,
    is_behind,
    needs_venv,
    new_settings,
    newest,
    parse_version,
    remote_newest,
    stack_action,
)

CHANGELOG = """\
# Changelog

What changed in each release. `release.sh` renames `## Unreleased` for you.

## Unreleased

- not shipped yet

## 0.5.0 - 2026-09-06

- the fifth thing

## 0.4.0 - 2026-09-01

- the fourth thing

## 0.3.0 - 2026-08-20

- the third thing
"""

PYPROJECT = """\
[project]
name = "loopgraph-engine"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
    "temporalio>=1.15",
    "pyyaml>=6.0",
]
"""


def bumped(text: str, to: str) -> str:
    """What `release.sh` does to pyproject.toml, and nothing else."""
    return text.replace('version = "0.1.0"', f'version = "{to}"')


# --------------------------------------------------------------- tag parsing ---

def test_a_release_tag_is_v_major_minor_patch():
    assert parse_version("v1.2.3") == (1, 2, 3)
    # Leading zeroes are still numbers; nobody writes them, but a tag is text.
    assert parse_version("v01.2.3") == (1, 2, 3)


@pytest.mark.parametrize("tag", ["1.2.3", "v1.2", "v1.2.3-rc1", "release-1"])
def test_any_other_tag_shape_is_not_a_release(tag):
    """`git describe --match 'v*'` also names a hand-made `v1.2.3-rc1`, and the
    callers have to be able to tell that from a release without crashing."""
    assert parse_version(tag) is None


def test_newest_compares_the_numbers_not_the_text():
    """The comparison every string sort gets wrong once the minor reaches ten."""
    assert newest(["v0.9.0", "v0.10.0", "v0.2.0"]) == "v0.10.0"


def test_newest_skips_what_does_not_parse():
    assert newest(["v0.1.0", "release-1", "v1.2.3-rc1"]) == "v0.1.0"


def test_newest_of_nothing_usable_is_none():
    """A fresh remote has no `v*` tag at all, and that is not an error."""
    assert newest([]) is None
    assert newest(["release-1", "main"]) is None


# ----------------------------------------------------------------- changelog ---

def test_changelog_between_slices_by_version_and_never_returns_unreleased():
    """What `lg update` prints after a fast-forward: the notes the user just
    gained, oldest of them last, in the order the file already has them."""
    out = changelog_between(CHANGELOG, "v0.3.0", "v0.5.0")
    assert out == ("## 0.5.0 - 2026-09-06\n"
                   "\n"
                   "- the fifth thing\n"
                   "\n"
                   "## 0.4.0 - 2026-09-01\n"
                   "\n"
                   "- the fourth thing")


def test_changelog_between_with_no_old_tag_starts_at_the_beginning():
    """An untagged checkout has no floor, so everything up to the target counts."""
    out = changelog_between(CHANGELOG, None, "v0.4.0")
    assert "## 0.4.0 - 2026-09-01" in out
    assert "## 0.3.0 - 2026-08-20" in out
    assert "0.5.0" not in out


def test_an_old_tag_that_does_not_parse_is_treated_as_none():
    """`installed` can hand this back a `v1.2.3-rc1`. Printing the whole
    changelog is right; raising in the middle of a finished update is not."""
    assert changelog_between(CHANGELOG, "v1.2.3-rc1", "v0.4.0") == \
        changelog_between(CHANGELOG, None, "v0.4.0")


def test_changelog_between_is_empty_when_nothing_is_newer():
    assert changelog_between(CHANGELOG, "v0.5.0", "v0.5.0") == ""


def test_changelog_between_trims_each_section_to_its_text():
    """It is printed straight after `updated ... → ...`, so a section's trailing
    blank lines would open a hole in the middle of the output."""
    out = changelog_between(CHANGELOG, "v0.3.0", "v0.5.0")
    assert not out.endswith("\n")
    assert "\n\n\n" not in out


def test_changelog_has_needs_the_dated_heading():
    """The release guard asks this of the tag it found, and a heading someone
    started but never dated has no notes under it worth releasing."""
    assert changelog_has(CHANGELOG, "0.4.0")
    assert not changelog_has("## 0.4.0\n\n- undated\n", "0.4.0")
    assert not changelog_has("## Unreleased\n\n- fixed 0.4.0 - the bug\n", "0.4.0")


def test_changelog_has_reads_the_dots_literally():
    assert not changelog_has("## 0x4x0 - 2026-09-06\n", "0.4.0")


# ------------------------------------------------------- what an update needs ---

def test_a_version_only_bump_is_a_restart_with_no_venv_refresh():
    """The shape every single release produces. A path rule would rebuild the
    image and re-sync the venv on every update, docs-only releases included."""
    changed = ["pyproject.toml", "CHANGELOG.md"]
    assert stack_action(changed, PYPROJECT, bumped(PYPROJECT, "0.2.0")) == "restart"
    assert needs_venv(PYPROJECT, bumped(PYPROJECT, "0.2.0")) is False


def test_a_dependency_floor_change_is_a_build_and_a_venv_refresh():
    new = PYPROJECT.replace("temporalio>=1.15", "temporalio>=1.16")
    assert deps_changed(PYPROJECT, new) is True
    assert needs_venv(PYPROJECT, new) is True
    assert stack_action(["pyproject.toml"], PYPROJECT, new) == "build"


def test_a_new_python_floor_is_a_build_and_a_venv_refresh():
    new = PYPROJECT.replace(">=3.13", ">=3.14")
    assert deps_changed(PYPROJECT, new) is True
    assert stack_action(["pyproject.toml"], PYPROJECT, new) == "build"


def test_a_compose_only_change_is_a_recreate():
    """`restart` re-runs the old containers with the configuration they were
    created from, so a compose edit needs `up -d` instead."""
    assert stack_action(["docker-compose.yml"], PYPROJECT, PYPROJECT) == "recreate"


def test_the_dockerfile_in_the_diff_is_a_build():
    assert stack_action(["Dockerfile"], PYPROJECT, PYPROJECT) == "build"


def test_paths_are_compared_whole():
    """A doc that only mentions the Dockerfile is not the Dockerfile."""
    assert stack_action(["docs/Dockerfile.md"], PYPROJECT, PYPROJECT) == "restart"
    assert stack_action(["sub/docker-compose.yml"], PYPROJECT, PYPROJECT) == "restart"


def test_nothing_changed_is_a_restart():
    assert stack_action([], PYPROJECT, PYPROJECT) == "restart"


def test_a_missing_or_broken_pyproject_counts_as_changed():
    """`git show <old>:pyproject.toml` hands back "" when the file was not there,
    and guessing "nothing changed" from text nobody could read is the wrong way
    to be wrong: a needless venv refresh costs seconds, a skipped one breaks lg."""
    assert deps_changed("", PYPROJECT) is True
    assert deps_changed(PYPROJECT, "[project\nname =") is True


# ------------------------------------------------------------- new settings ---

OLD_EXAMPLE = """\
# Copy to .env.
ANTHROPIC_BASE_URL=http://127.0.0.1:8317
ANTHROPIC_AUTH_TOKEN=
# Route 2: comment out the two lines above and use these instead:
# ANTHROPIC_API_KEY=sk-ant-...
LOOPGRAPH_UID=1000
"""


def test_new_settings_ignores_commented_keys_and_keys_the_env_already_has():
    """The reason this diffs the two examples instead of comparing .env against
    the whole example: the example carries the route it is not, and every
    route-1 install would be told it lacks ANTHROPIC_API_KEY forever."""
    new = OLD_EXAMPLE.replace("LOOPGRAPH_UID=1000",
                              "LOOPGRAPH_UID=1000\nLOOPGRAPH_NPM_CACHE=\nLOOPGRAPH_NEW=")
    env = {"LOOPGRAPH_NPM_CACHE": "/tmp/npm"}
    assert new_settings(OLD_EXAMPLE, new, env) == ["LOOPGRAPH_NEW"]


def test_new_settings_keeps_the_new_examples_order():
    """They go into one sentence the user reads left to right, and the example is
    grouped by section, so its order is the order that makes sense to follow."""
    new = OLD_EXAMPLE + "LOOPGRAPH_B=\nLOOPGRAPH_A=\n"
    assert new_settings(OLD_EXAMPLE, new, {}) == ["LOOPGRAPH_B", "LOOPGRAPH_A"]


def test_a_key_that_moves_from_commented_to_uncommented_is_new():
    """A route the release just made the default is a real decision for the user,
    not a line that was always there."""
    new = OLD_EXAMPLE.replace("# ANTHROPIC_API_KEY=sk-ant-...", "ANTHROPIC_API_KEY=")
    assert new_settings(OLD_EXAMPLE, new, {}) == ["ANTHROPIC_API_KEY"]


def test_nothing_is_new_when_the_example_did_not_move():
    assert new_settings(OLD_EXAMPLE, OLD_EXAMPLE, {}) == []


def test_new_settings_delegates_to_the_shared_env_reader():
    """A second copy of the .env rules is exactly the defect envfile.py exists to
    stop, and this one would disagree about `export ` and quotes."""
    assert "parse_env(" in inspect.getsource(version.new_settings)


# ---------------------------------------------------------- the .env parser ---

@pytest.mark.parametrize("line,key,value", [
    ('LOOPGRAPH_PROJECTS_DIR="/srv/code"', "LOOPGRAPH_PROJECTS_DIR", "/srv/code"),
    ("LOOPGRAPH_DOCKER='sudo docker'", "LOOPGRAPH_DOCKER", "sudo docker"),
    ("export LOOPGRAPH_UID=1001", "LOOPGRAPH_UID", "1001"),
    ("ANTHROPIC_MODEL=claude-opus-5  # the dated id", "ANTHROPIC_MODEL", "claude-opus-5"),
])
def test_parse_env_reads_text_the_way_read_env_reads_a_file(tmp_path, line, key, value):
    """The same four shapes `test_reads_the_four_shapes_compose_reads` pins, asked
    of the text parser: what `git show <commit>:.env.example` hands back is a
    string, and it has to mean what the file on disk means."""
    env = tmp_path / ".env"
    env.write_text(line + "\n")
    assert parse_env(line + "\n") == read_env(env)
    assert parse_env(line + "\n")[key] == value


def test_read_env_delegates_to_parse_env():
    """One copy of the rules, the same reason `lg._dotenv` delegates to read_env."""
    assert "parse_env(" in inspect.getsource(read_env)


# ---------------------------------------------------------------- is_behind ---

def test_nothing_is_behind_a_remote_that_said_nothing():
    """A remote we could not reach, or one with no releases yet. Silence is not
    news, and the dashboard must not offer an update it cannot name."""
    assert is_behind("v0.4.0", None) is False
    assert is_behind(None, None) is False
    assert is_behind("v0.4.0", "release-1") is False


def test_an_untagged_checkout_is_behind_any_release():
    assert is_behind(None, "v0.5.0") is True


def test_an_installed_tag_that_does_not_parse_counts_as_untagged():
    """`git describe --match 'v*'` names a hand-made `v1.2.3-rc1` too, and
    comparing a triple with None is a TypeError that takes down `lg version` and
    the dashboard's checker thread."""
    assert is_behind("v1.2.3-rc1", "v0.5.0") is True
    assert is_behind("v1.2.3-rc1", None) is False


def test_behind_is_the_ordering_of_the_two_triples():
    assert is_behind("v0.4.0", "v0.5.0") is True
    assert is_behind("v0.5.0", "v0.5.0") is False
    assert is_behind("v0.10.0", "v0.9.0") is False


# ------------------------------------------------------------- what git says ---

def test_installed_on_an_untagged_clone(clone_repo):
    """Every checkout cloned before the first release looks like this."""
    here = installed(clone_repo.root)
    assert here["tag"] is None
    assert here["ahead"] == 0
    assert re.fullmatch(r"[0-9a-f]{7}", here["commit"])


def test_installed_names_the_nearest_v_tag_and_counts_commits_past_it(clone_repo):
    """`lg version` prints `v0.1.0 +2 commits`, so both halves have to be right,
    and a tag that is not a release must become neither of them."""
    clone_repo.tag("v0.1.0")
    clone_repo.commit("second", {"a.txt": "a\n"})
    clone_repo.commit("third", {"b.txt": "b\n"})
    clone_repo.tag("release-1")
    here = installed(clone_repo.root)
    assert here["tag"] == "v0.1.0"
    assert here["ahead"] == 2


def test_installed_on_a_directory_that_is_not_a_repository(tmp_path, monkeypatch):
    """A tree unpacked from an archive instead of cloned. `lg version` still has
    to print something about it, so nothing here may raise.

    The ceiling is what makes that a fact rather than a machine's opinion: git
    searches upwards for a .git, and a TMPDIR that sits inside a checkout would
    otherwise have it answer about that repository instead of this directory."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    unpacked = tmp_path / "unpacked"
    unpacked.mkdir()
    assert installed(unpacked) == {"tag": None, "commit": None, "ahead": 0}


def test_remote_newest_picks_the_highest_tag_on_origin_not_the_local_one(clone_repo):
    """The release a user is offered has to be one they can actually fetch, and
    the comparison is the one a string sort gets wrong: v0.10.0 beats v0.9.0."""
    clone_repo.tag("v0.9.0")
    clone_repo.commit("second", {"a.txt": "a\n"})
    clone_repo.tag("v0.10.0")
    clone_repo.push()
    clone_repo.commit("third", {"b.txt": "b\n"})
    clone_repo.tag("v1.0.0")  # made here, never pushed: nobody else can fetch it
    assert remote_newest(clone_repo.root, 10) == "v0.10.0"


def test_remote_newest_is_none_when_origin_has_no_tags(clone_repo):
    """A repository that has never released. Silence, not an error."""
    assert remote_newest(clone_repo.root, 10) is None


def test_remote_newest_raises_with_gits_first_stderr_line(clone_repo, tmp_path):
    """An origin nobody can reach: offline, moved, or a URL that never worked.
    The message is printed on one line beside `latest:`, so it has to be one."""
    clone_repo.git("remote", "set-url", "origin", str(tmp_path / "gone.git"))
    with pytest.raises(RemoteError) as raised:
        remote_newest(clone_repo.root, 10)
    assert "does not appear to be a git repository" in str(raised.value)
    assert "\n" not in str(raised.value)


def test_remote_newest_raises_on_timeout(clone_repo, monkeypatch):
    """git against an unreachable host waits for the TCP stack to give up, which
    is minutes. The dashboard's checker gets a message in ten seconds instead."""
    passed = {}

    def times_out(argv, **kwargs):
        passed.update(kwargs)
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(version.subprocess, "run", times_out)
    with pytest.raises(RemoteError, match="timed out after 10s"):
        remote_newest(clone_repo.root, 10)
    assert passed["timeout"] == 10, "the deadline never reached git itself"


def test_remote_newest_names_the_exit_code_when_git_said_nothing(clone_repo, monkeypatch):
    """git usually explains itself, but this message is shown to a person, and an
    empty one would print `latest: unknown ()`."""
    def quiet_failure(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 128, "", "")

    monkeypatch.setattr(version.subprocess, "run", quiet_failure)
    with pytest.raises(RemoteError, match="git ls-remote exited 128"):
        remote_newest(clone_repo.root, 10)
