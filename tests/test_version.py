"""The rules `lg version`, `lg update` and the dashboard all read releases by.

Most of it is pure functions over text: a tag name, a changelog, two
`pyproject.toml` texts, two `.env.example` texts. The point of keeping those
rules here rather than inside the commands is that a rule the user can hit —
"does this update need a rebuild", "is this key new" — can be tested against the
exact file shapes the repo really produces, and there is only ever one copy of
it.

Then the two that do run git, `installed` and `remote_newest`. Those are tested
against a real repository with a real origin, both under tmp_path, because what
they have to get right is git's own output and exit codes.

The last sections are the commands themselves — `lg version`, which turns those
two into the lines a person reads, and `lg update`, from its refusals through the
venv refresh to the stack restart — and then what the README, the skill and the
installer tell people about them.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import re
import shlex
import subprocess
import types
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest
from temporalio.service import RPCError, RPCStatusCode

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


WRAPPED_BULLET = """\
# Changelog

## 0.5.0 - 2026-09-06

- the fifth thing, written long enough that it wrapped and the second line reads
## 0.3.0 - which is where the trouble starts

## 0.4.0 - 2026-09-01

- the fourth thing
"""


def test_a_wrapped_bullet_beginning_with_a_heading_is_read_as_one_anyway():
    """The trade-off this module accepts, written down so nobody fixes it by
    accident. The section rule is one line long — the line starts with `## ` —
    and that is what makes it a rule a maintainer can hold in mind while writing
    the file. The price is here: a bullet whose text wraps onto a line beginning
    `## 0.3.0 - ` opens a 0.3.0 section right there, so the rest of that bullet
    leaves the release it belongs to and is skipped along with the old section.

    Writing it any other way means a parser that has to know what a list is, and
    a changelog nobody can predict by looking at it. Keep the wrap out of column
    zero instead.
    """
    out = changelog_between(WRAPPED_BULLET, "v0.3.0", "v0.5.0")
    assert out == ("## 0.5.0 - 2026-09-06\n"
                   "\n"
                   "- the fifth thing, written long enough that it wrapped and "
                   "the second line reads\n"
                   "\n"
                   "## 0.4.0 - 2026-09-01\n"
                   "\n"
                   "- the fourth thing")
    assert "which is where the trouble starts" not in out
    assert changelog_has(WRAPPED_BULLET, "0.3.0"), "the wrapped line is a section"


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


def test_a_project_that_is_not_a_table_counts_as_changed_too():
    """`project = 5` parses: it is a number where a table was expected, and
    asking a number for its keys is an AttributeError, not a refusal. It counts
    the way an unreadable file counts, because it is one."""
    assert deps_changed(PYPROJECT, "project = 5\n") is True
    assert deps_changed("project = 5\n", PYPROJECT) is True


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


# --------------------------------------------------------------- lg version ---

def _lg():
    """`lg` has no .py extension, so it needs loading by path.

    Under its own module name, not the one tests/test_release.py uses: two
    loaders sharing a name would hand one module object to both files.
    """
    path = Path(__file__).resolve().parent.parent / "lg"
    loader = SourceFileLoader("lg_cli_version", str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("lg_cli_version", loader))
    loader.exec_module(mod)
    return mod


def _version(root, monkeypatch, capsys, as_json=False) -> tuple[int, str, str]:
    """`lg version` against a checkout under tmp_path, as (code, stdout, stderr)."""
    lg = _lg()
    monkeypatch.setattr(lg, "ROOT", str(root))
    code = asyncio.run(lg.cmd_version(types.SimpleNamespace(json=as_json)))
    said = capsys.readouterr()
    return code, said.out, said.err


def _release_on_origin_only(clone_repo, tag: str) -> None:
    """A release origin has and this checkout does not: a clone nobody fetched."""
    clone_repo.tag(tag)
    clone_repo.push()
    clone_repo.git("tag", "-d", tag)


@pytest.mark.parametrize("info,line", [
    ({"tag": "v0.4.0", "commit": "abc1234", "ahead": 0}, "v0.4.0"),
    ({"tag": "v0.4.0", "commit": "abc1234", "ahead": 3}, "v0.4.0 +3 commits (abc1234)"),
    # `+1 commits` reads wrong and is meant to: the spec fixes the word, and this
    # same text is what `lg update` prints as the release it moved you off.
    ({"tag": "v0.4.0", "commit": "abc1234", "ahead": 1}, "v0.4.0 +1 commits (abc1234)"),
    ({"tag": None, "commit": "abc1234", "ahead": 0}, "untagged (abc1234)"),
    ({"tag": None, "commit": None, "ahead": 0}, "untagged (not a git checkout)"),
])
def test_installed_line_names_every_shape_a_checkout_can_be_in(info, line):
    assert _lg().installed_line(info) == line


def test_lg_version_on_a_tagged_clone_that_matches_origin(clone_repo, monkeypatch, capsys):
    """The everyday case: two lines, and nothing telling the user to update."""
    clone_repo.tag("v0.1.0")
    clone_repo.push()
    code, out, err = _version(clone_repo.root, monkeypatch, capsys)
    assert code == 0
    assert out.splitlines() == ["installed: v0.1.0", "latest: v0.1.0 (up to date)"]
    # `lg version | head -1` has to stay stable, so nothing goes to stderr.
    assert err == ""


def test_lg_version_says_run_lg_update_when_origin_is_ahead(clone_repo, monkeypatch, capsys):
    clone_repo.tag("v0.1.0")
    clone_repo.push()
    _release_on_origin_only(clone_repo, "v0.2.0")
    code, out, _ = _version(clone_repo.root, monkeypatch, capsys)
    assert code == 0
    assert out.splitlines() == ["installed: v0.1.0", "latest: v0.2.0", "update: run lg update"]


def test_lg_version_on_an_untagged_clone_with_an_origin_release(clone_repo, monkeypatch, capsys):
    """Every checkout cloned before the first release, once one exists."""
    _release_on_origin_only(clone_repo, "v0.1.0")
    code, out, _ = _version(clone_repo.root, monkeypatch, capsys)
    lines = out.splitlines()
    assert code == 0
    assert re.fullmatch(r"installed: untagged \([0-9a-f]{7}\)", lines[0])
    assert lines[1:] == ["latest: v0.1.0", "update: run lg update"]


def test_lg_version_when_origin_has_no_tags(clone_repo, monkeypatch, capsys):
    """A remote that has never released is silence, not news: no update line."""
    code, out, _ = _version(clone_repo.root, monkeypatch, capsys)
    lines = out.splitlines()
    assert code == 0
    assert lines[1:] == ["latest: none (origin has no releases yet)"]


def test_lg_version_when_origin_is_unreachable_exits_0_and_says_why(
        clone_repo, tmp_path, monkeypatch, capsys):
    """Offline, or an origin that moved. Asking what you have installed still has
    to answer, and a script wrapping the command must not start failing."""
    clone_repo.git("remote", "set-url", "origin", str(tmp_path / "gone.git"))
    code, out, err = _version(clone_repo.root, monkeypatch, capsys)
    lines = out.splitlines()
    assert code == 0
    assert lines[0].startswith("installed: ")
    assert lines[1].startswith("latest: unknown (could not reach origin: ")
    assert len(lines) == 2
    assert err == ""


def test_lg_version_outside_a_git_checkout_exits_0(tmp_path, monkeypatch, capsys):
    """A tree unpacked from an archive rather than cloned.

    The ceiling is what keeps git inside tmp_path: without it, a TMPDIR under a
    checkout would have git answer about that repository and ask its real origin.
    """
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    unpacked = tmp_path / "unpacked"
    unpacked.mkdir()
    code, out, _ = _version(unpacked, monkeypatch, capsys)
    lines = out.splitlines()
    assert code == 0
    assert lines[0] == "installed: untagged (not a git checkout)"
    assert lines[1].startswith("latest: unknown (")


def test_lg_version_json_carries_the_six_keys(clone_repo, monkeypatch, capsys):
    clone_repo.tag("v0.1.0")
    clone_repo.push()
    code, out, _ = _version(clone_repo.root, monkeypatch, capsys, as_json=True)
    said = json.loads(out)
    assert code == 0
    assert set(said) == {"installed", "commit", "ahead", "latest", "behind", "error"}
    assert said["installed"] == "v0.1.0"
    assert said["latest"] == "v0.1.0"
    assert said["ahead"] == 0
    assert re.fullmatch(r"[0-9a-f]{7}", said["commit"])
    assert said["behind"] is False
    assert said["error"] is None

    _release_on_origin_only(clone_repo, "v0.2.0")
    code, out, _ = _version(clone_repo.root, monkeypatch, capsys, as_json=True)
    said = json.loads(out)
    assert code == 0
    assert (said["installed"], said["latest"], said["behind"]) == ("v0.1.0", "v0.2.0", True)
    assert said["error"] is None


def test_lg_version_json_carries_the_reason_when_origin_is_unreachable(
        clone_repo, tmp_path, monkeypatch, capsys):
    """Whatever reads this has to tell "no release yet" from "no answer"."""
    clone_repo.tag("v0.1.0")
    clone_repo.git("remote", "set-url", "origin", str(tmp_path / "gone.git"))
    with pytest.raises(RemoteError) as raised:
        remote_newest(clone_repo.root, 10)
    code, out, _ = _version(clone_repo.root, monkeypatch, capsys, as_json=True)
    said = json.loads(out)
    assert code == 0
    assert said["error"] == str(raised.value)
    assert said["latest"] is None
    assert said["behind"] is False


def test_lg_version_json_on_a_hand_made_tag_that_does_not_parse(
        clone_repo, monkeypatch, capsys):
    """`--match 'v*'` names a hand-made v0.2.0-rc1 too. It is reported as it is
    and counted as no release, so the user is offered the real one."""
    _release_on_origin_only(clone_repo, "v0.2.0")
    clone_repo.tag("v0.2.0-rc1")
    code, out, _ = _version(clone_repo.root, monkeypatch, capsys, as_json=True)
    said = json.loads(out)
    assert code == 0
    assert said["installed"] == "v0.2.0-rc1"
    assert said["latest"] == "v0.2.0"
    assert said["behind"] is True


# ---------------------------------------------------------------- lg update ---

def fake_run(table=None):
    """A stand-in for lg._run: git runs for real, nothing else runs at all.

    Every call lands on `.calls` as (argv, cwd, timeout, stream), which is how a
    test proves what the update did and, more to the point, what it did not. An
    argv listed in `.table` is answered with the CompletedProcess sitting there;
    a git argv nobody listed reaches the real runner, because git's own exit
    codes are what these tests are about; anything else comes back exit 0 with no
    output. That last default is the safety rail: no test can reach docker, uv or
    pip by accident, and a `compose ps` naming no service reads as a stack that
    is not running, which is the truth on a machine mid-test.
    """
    def run(argv, cwd, timeout=None, stream=False):
        run.calls.append((list(argv), cwd, timeout, stream))
        answer = run.table.get(tuple(argv))
        if answer is not None:
            return answer
        if argv[0] == "git":
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
        return subprocess.CompletedProcess(argv, 0, "", "")
    run.table = dict(table or {})
    run.calls = []
    return run


class ListingClient:
    """Temporal as lg update reads it: one listing of what is still running.

    The shape tests/test_lg_status.py's FakeClient has — list_workflows hands
    back an async generator — narrowed to the single call this command makes.
    `error` is raised from inside that generator rather than from the call, which
    is where a real RPCError surfaces: the listing sends no request to the server
    until something iterates it.
    """

    def __init__(self, workflows=(), error=None):
        self.workflows, self.error = list(workflows), error
        self.queries: list[str] = []

    def list_workflows(self, query):
        self.queries.append(query)

        async def gen():
            if self.error is not None:
                raise self.error
            for workflow in self.workflows:
                yield workflow
        return gen()


def _temporal(lg, monkeypatch, answer) -> None:
    """What Temporal does when lg update asks: hand back this client, raise this
    exception, or, for None, never answer at all. Nothing here opens a socket."""
    async def connect():
        if answer is None:
            await asyncio.Event().wait()
        if isinstance(answer, Exception):
            raise answer
        return answer
    connect.faked = True
    monkeypatch.setattr(lg, "_client", connect)


def _update(lg, root, monkeypatch, capsys, runner=None, home=None) -> tuple[int, str, str]:
    """`lg update` against a checkout under tmp_path, as (code, stdout, stderr).

    Temporal is fenced off unless the test called _temporal: a check that stopped
    running in the right order would otherwise dial the machine's real
    localhost:7233 instead of failing. The wait between log polls goes the same
    way, as a no-op: the poll is sixty tries two seconds apart, and no test has
    two minutes to spend proving it.

    HOME is fenced for the same reason and it matters more: the update writes a
    symlink under ~/.claude/skills, so a test left pointing at the real home
    would re-point the skill link of whoever is running the suite. `home` is
    only for the tests that then read what was written there.
    """
    home = Path(home) if home is not None else root.parent / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    if not getattr(lg._client, "faked", False):
        async def never_asked():
            raise AssertionError("lg update reached Temporal with no fake client installed")
        monkeypatch.setattr(lg, "_client", never_asked)

    async def no_wait(seconds):
        pass
    monkeypatch.setattr(lg, "_sleep", no_wait)
    monkeypatch.setattr(lg, "ROOT", str(root))
    monkeypatch.setattr(lg, "_run", runner if runner is not None else fake_run())
    code = asyncio.run(lg.cmd_update(types.SimpleNamespace()))
    said = capsys.readouterr()
    return code, said.out, said.err


def _release_on_origin(clone_repo, tag: str, files: dict[str, str]) -> None:
    """A release origin has that this checkout is one commit behind.

    The commit and its tag are made here, pushed, and then taken back off the
    clone: what a user's checkout looks like the morning after a release lands.
    """
    was = clone_repo.git("rev-parse", "HEAD")
    clone_repo.commit(f"release {tag}", files)
    clone_repo.tag(tag)
    clone_repo.push()
    clone_repo.git("reset", "--hard", was)
    clone_repo.git("tag", "-d", tag)


RELEASE_NOTES = """\
# Changelog

## 0.2.0 - 2026-09-07

- the newest thing

## 0.1.0 - 2026-09-01

- the first thing
"""


def test_run_turns_a_hung_command_into_a_failed_result(tmp_path):
    """subprocess raises here, and a traceback is not an answer: 124, the code a
    shell reports a timeout as, with the reason where the caller reads stderr."""
    done = _lg()._run(["sleep", "5"], str(tmp_path), timeout=0.05)
    assert (done.returncode, done.stdout, done.stderr) == (124, "", "timed out after 0.05s")


def test_run_turns_a_missing_binary_into_a_failed_result(tmp_path):
    """A machine with no uv, no pip or no docker on the path."""
    done = _lg()._run(["lg-no-such-program"], str(tmp_path))
    assert done.returncode == 127
    assert "lg-no-such-program" in done.stderr


def test_update_refuses_uncommitted_changes_and_touches_nothing(
        clone_repo, monkeypatch, capsys):
    was = clone_repo.git("rev-parse", "HEAD")
    (clone_repo.root / "README.md").write_text("# edited\n", encoding="utf-8")
    code, out, err = _update(_lg(), clone_repo.root, monkeypatch, capsys)
    assert code == 1
    assert out == ""
    assert err == (f"refuse: uncommitted changes in {clone_repo.root}. "
                   "Commit or stash them, then run lg update again.\n")
    assert clone_repo.git("rev-parse", "HEAD") == was


def test_a_dirty_tree_refuses_before_it_asks_origin_anything(
        clone_repo, monkeypatch, capsys):
    """The order matters: a refusal the user can fix in a second must not cost
    them 60 seconds waiting on a network they may not have."""
    (clone_repo.root / "README.md").write_text("# edited\n", encoding="utf-8")
    runner = fake_run()
    code, _, _ = _update(_lg(), clone_repo.root, monkeypatch, capsys, runner)
    assert code == 1
    assert not [argv for argv, _, _, _ in runner.calls if argv[:2] == ["git", "fetch"]]


def test_update_refuses_off_main_and_on_a_detached_head(clone_repo, monkeypatch, capsys):
    lg = _lg()
    clone_repo.git("checkout", "-b", "wip")
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys)
    assert (code, out) == (1, "")
    assert err == "refuse: on branch wip, not main. lg update only moves main.\n"

    clone_repo.git("checkout", "--detach", "HEAD")
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys)
    assert (code, out) == (1, "")
    assert err == "refuse: not on a branch (detached HEAD). lg update only moves main.\n"


def test_an_off_main_branch_refuses_before_it_asks_origin_anything(
        clone_repo, monkeypatch, capsys):
    clone_repo.git("checkout", "-b", "wip")
    runner = fake_run()
    code, _, _ = _update(_lg(), clone_repo.root, monkeypatch, capsys, runner)
    assert code == 1
    assert not [argv for argv, _, _, _ in runner.calls if argv[:2] == ["git", "fetch"]]


def test_update_refuses_when_origin_is_unreachable(clone_repo, tmp_path, monkeypatch, capsys):
    """Offline, or an origin that moved: git's own first line says which."""
    clone_repo.git("remote", "set-url", "origin", str(tmp_path / "gone.git"))
    code, out, err = _update(_lg(), clone_repo.root, monkeypatch, capsys)
    assert (code, out) == (1, "")
    assert err.startswith("refuse: could not reach origin: ")
    assert len(err.splitlines()) == 1


def test_update_refuses_when_the_fetch_hangs(clone_repo, monkeypatch, capsys):
    """A host that swallows packets keeps git waiting until TCP gives up, so the
    fetch carries a deadline and the timeout reads as the reason it refused."""
    hung = subprocess.CompletedProcess(["git", "fetch", "--tags", "--quiet", "origin"],
                                       124, "", "timed out after 60s")
    runner = fake_run({("git", "fetch", "--tags", "--quiet", "origin"): hung})
    code, out, err = _update(_lg(), clone_repo.root, monkeypatch, capsys, runner)
    assert (code, out) == (1, "")
    assert err == "refuse: could not reach origin: timed out after 60s\n"
    deadlines = [t for argv, _, t, _ in runner.calls if argv[:2] == ["git", "fetch"]]
    assert deadlines == [_lg().FETCH_TIMEOUT]


def test_a_fetch_that_failed_silently_still_names_a_reason(clone_repo, monkeypatch, capsys):
    """`refuse: could not reach origin: ` with nothing after the colon is not an
    answer, so the exit code stands in when git said nothing."""
    quiet = subprocess.CompletedProcess(["git", "fetch", "--tags", "--quiet", "origin"],
                                        128, "", "")
    runner = fake_run({("git", "fetch", "--tags", "--quiet", "origin"): quiet})
    code, _, err = _update(_lg(), clone_repo.root, monkeypatch, capsys, runner)
    assert code == 1
    assert err == "refuse: could not reach origin: git fetch exited 128\n"


def test_update_refuses_when_origin_has_no_releases(clone_repo, monkeypatch, capsys):
    code, out, err = _update(_lg(), clone_repo.root, monkeypatch, capsys)
    assert (code, out) == (1, "")
    assert err == "refuse: origin has no releases yet.\n"


def test_already_on_the_newest_tag_never_reaches_temporal(clone_repo, monkeypatch, capsys):
    """No _temporal here on purpose: asking about a checkout that is already
    there would hit _update's fence and fail."""
    lg = _lg()
    clone_repo.tag("v0.1.0")
    clone_repo.push()
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys)
    assert (code, out, err) == (0, "already on v0.1.0\n", "")


def test_already_past_the_newest_tag_reports_how_far(clone_repo, monkeypatch, capsys):
    """A maintainer's checkout, mid-phase. It is not offered a move backwards,
    and it does not reach Temporal either: _update's fence proves that."""
    lg = _lg()
    clone_repo.tag("v0.1.0")
    clone_repo.push()
    clone_repo.commit("after the release", {"one.txt": "1\n"})
    clone_repo.commit("and another", {"two.txt": "2\n"})
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys)
    assert (code, out, err) == (0, "already past v0.1.0 (HEAD is 2 commits ahead)\n", "")


def test_update_refuses_when_temporal_cannot_be_reached_or_hangs(
        clone_repo, monkeypatch, capsys):
    lg = _lg()
    clone_repo.tag("v0.1.0")
    clone_repo.push()
    _release_on_origin(clone_repo, "v0.2.0", {"CHANGELOG.md": RELEASE_NOTES})
    cannot_reach = (f"refuse: cannot reach Temporal at {lg.ADDRESS}. "
                    "Start the stack so lg update can check for open runs.\n")
    was = clone_repo.git("rev-parse", "HEAD")

    _temporal(lg, monkeypatch, OSError("[Errno 111] Connection refused"))
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys)
    assert (code, out, err) == (1, "", cannot_reach)

    # A server that accepts the connection and then says nothing: the deadline is
    # the only thing that ends this, and the user reads the same line either way.
    _temporal(lg, monkeypatch, None)
    monkeypatch.setattr(lg, "TEMPORAL_TIMEOUT", 0.05)
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys)
    assert (code, out, err) == (1, "", cannot_reach)
    assert clone_repo.git("rev-parse", "HEAD") == was


def test_update_refuses_when_the_workflow_listing_fails(clone_repo, monkeypatch, capsys):
    """A server on the wrong namespace, or with a visibility store that is down.
    The connect succeeds and the listing dies, so the check has to cover both."""
    lg = _lg()
    clone_repo.tag("v0.1.0")
    clone_repo.push()
    _release_on_origin(clone_repo, "v0.2.0", {"CHANGELOG.md": RELEASE_NOTES})
    broken = RPCError("namespace not found", RPCStatusCode.NOT_FOUND, b"")
    _temporal(lg, monkeypatch, ListingClient(error=broken))
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys)
    assert (code, out) == (1, "")
    assert err == (f"refuse: cannot reach Temporal at {lg.ADDRESS}. "
                   "Start the stack so lg update can check for open runs.\n")


def test_update_refuses_with_one_line_per_open_run(clone_repo, monkeypatch, capsys):
    """Restarting the stack under a waiting run loses the answer it is waiting
    for, so the user is told which runs to finish and how to read each one."""
    lg = _lg()
    clone_repo.tag("v0.1.0")
    clone_repo.push()
    _release_on_origin(clone_repo, "v0.2.0", {"CHANGELOG.md": RELEASE_NOTES})
    was = clone_repo.git("rev-parse", "HEAD")
    client = ListingClient([types.SimpleNamespace(id="run-toy-ab12cd"),
                            types.SimpleNamespace(id="gate-ef34gh")])
    _temporal(lg, monkeypatch, client)
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys)
    assert (code, out) == (1, "")
    assert err.splitlines() == [
        "refuse: 2 open run(s). Answer or finish them first:",
        "  run-toy-ab12cd    lg status run-toy-ab12cd",
        "  gate-ef34gh    lg status gate-ef34gh",
    ]
    # Every type, not only LoopGraphRun: a GateCheckRun replays too.
    assert client.queries == ['ExecutionStatus = "Running"']
    assert clone_repo.git("rev-parse", "HEAD") == was


def test_update_fast_forwards_and_prints_the_changelog_slice(
        clone_repo, monkeypatch, capsys):
    lg = _lg()
    clone_repo.tag("v0.1.0")
    clone_repo.push()
    _release_on_origin(clone_repo, "v0.2.0", {"CHANGELOG.md": RELEASE_NOTES})
    _temporal(lg, monkeypatch, ListingClient())
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys)
    assert (code, err) == (0, "")
    # The first four lines and no more: what follows the changelog is what the
    # update does next, and this test is about the move and the notes.
    assert out.splitlines()[:4] == ["updated v0.1.0 → v0.2.0",
                                    "## 0.2.0 - 2026-09-07",
                                    "",
                                    "- the newest thing"]
    assert "the first thing" not in out
    assert clone_repo.git("rev-parse", "HEAD") == clone_repo.git("rev-parse", "v0.2.0^{commit}")


def test_update_says_when_the_tag_has_no_changelog_entry(clone_repo, monkeypatch, capsys):
    """A release made before the repo had a CHANGELOG.md at all, or one that
    forgot to write its section. The update happened, and still says so."""
    lg = _lg()
    clone_repo.tag("v0.1.0")
    clone_repo.push()
    _release_on_origin(clone_repo, "v0.2.0", {"README.md": "# clone, released\n"})
    _temporal(lg, monkeypatch, ListingClient())
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys)
    assert (code, err) == (0, "")
    assert out.splitlines()[:2] == ["updated v0.1.0 → v0.2.0",
                                    "(no changelog entry for v0.2.0)"]


def test_update_refuses_a_diverged_main_and_moves_nothing(clone_repo, monkeypatch, capsys):
    """Local commits on main that origin does not have. --ff-only is what makes
    this a refusal instead of a merge commit nobody asked for."""
    lg = _lg()
    clone_repo.tag("v0.1.0")
    clone_repo.push()
    _release_on_origin(clone_repo, "v0.2.0", {"CHANGELOG.md": RELEASE_NOTES})
    clone_repo.commit("my own work", {"mine.txt": "mine\n"})
    was = clone_repo.git("rev-parse", "HEAD")
    _temporal(lg, monkeypatch, ListingClient())
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys)
    assert (code, out) == (1, "")
    assert err == "refuse: main and v0.2.0 have diverged; git status will show why.\n"
    assert clone_repo.git("rev-parse", "HEAD") == was


def test_update_never_runs_a_git_command_that_could_lose_work(
        clone_repo, monkeypatch, capsys):
    """AC-14, read off the recording: a user's own commits are never moved."""
    lg = _lg()
    clone_repo.tag("v0.1.0")
    clone_repo.push()
    _release_on_origin(clone_repo, "v0.2.0", {"CHANGELOG.md": RELEASE_NOTES})
    _temporal(lg, monkeypatch, ListingClient())
    runner = fake_run()
    code, _, _ = _update(lg, clone_repo.root, monkeypatch, capsys, runner)
    assert code == 0
    git = [argv for argv, _, _, _ in runner.calls if argv[0] == "git"]
    assert git
    assert not [argv for argv in git if argv[1] in ("checkout", "reset", "stash", "pull")]
    assert all("--ff-only" in argv for argv in git if argv[1] == "merge")


# ---------------------------------------------- lg update: the skill on disk ---

# `~/.claude/skills/loopgraph` is how an agent reads this checkout's SKILL.md.
# install.sh makes it a symlink, and a symlink follows every update by itself.
# These are the states a machine ends up in when it is anything else.

def _skill_checkout(tmp_path):
    """A checkout with a skill to link, and a home with nothing linked yet."""
    root = tmp_path / "engine"
    (root / "skills" / "loopgraph").mkdir(parents=True)
    (root / "skills" / "loopgraph" / "SKILL.md").write_text("# new\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    return root, home


def _skill_dst(home):
    return home / ".claude" / "skills" / "loopgraph"


def test_relink_skill_says_nothing_when_the_link_already_points_here(tmp_path):
    """The install.sh default. A symlink is already current the moment the merge
    lands, so an update that announced it every time would be noise."""
    root, home = _skill_checkout(tmp_path)
    dst = _skill_dst(home)
    dst.parent.mkdir(parents=True)
    dst.symlink_to(root / "skills" / "loopgraph")
    assert _lg().relink_skill(str(root), str(home)) is None


def test_relink_skill_links_a_home_that_never_had_one(tmp_path):
    """Someone who declined the installer's offer, or installed before the skill
    existed. Nothing is at the destination, and neither are its parents."""
    root, home = _skill_checkout(tmp_path)
    dst = _skill_dst(home)
    said = _lg().relink_skill(str(root), str(home))
    assert said == (f"linked {dst} -> {root / 'skills' / 'loopgraph'}\n"
                    "  restart your agent session to pick up the skill")
    assert dst.is_symlink()
    assert dst.resolve() == (root / "skills" / "loopgraph").resolve()


def test_relink_skill_repoints_a_link_into_another_checkout(tmp_path):
    """A symlink left over from an older clone. It resolves, it reads, and every
    word in it is the version that other checkout is on."""
    root, home = _skill_checkout(tmp_path)
    stale = tmp_path / "old-engine" / "skills" / "loopgraph"
    stale.mkdir(parents=True)
    (stale / "SKILL.md").write_text("# old\n", encoding="utf-8")
    dst = _skill_dst(home)
    dst.parent.mkdir(parents=True)
    dst.symlink_to(stale)
    said = _lg().relink_skill(str(root), str(home))
    assert said == (f"re-pointed {dst} -> {root / 'skills' / 'loopgraph'}\n"
                    "  restart your agent session to pick up the skill")
    assert (dst / "SKILL.md").read_text(encoding="utf-8") == "# new\n"
    # The other checkout is somebody's working tree, not ours to delete.
    assert (stale / "SKILL.md").read_text(encoding="utf-8") == "# old\n"


def test_relink_skill_repoints_a_link_whose_target_is_gone(tmp_path):
    """A checkout that was moved or deleted. The link is still a link, so
    replacing it costs nothing, but os.path.exists answers False for it."""
    root, home = _skill_checkout(tmp_path)
    dst = _skill_dst(home)
    dst.parent.mkdir(parents=True)
    dst.symlink_to(tmp_path / "deleted" / "skills" / "loopgraph")
    said = _lg().relink_skill(str(root), str(home))
    assert said.startswith(f"re-pointed {dst} -> ")
    assert (dst / "SKILL.md").read_text(encoding="utf-8") == "# new\n"


def test_relink_skill_refuses_to_touch_a_copied_directory(tmp_path):
    """A copy someone made by hand, or an agent wrote when asked to install the
    skill. It may have edits in it, so this says what to do and writes nothing."""
    root, home = _skill_checkout(tmp_path)
    dst = _skill_dst(home)
    dst.mkdir(parents=True)
    (dst / "SKILL.md").write_text("# copied, and edited\n", encoding="utf-8")
    said = _lg().relink_skill(str(root), str(home))
    assert said == (f"{dst} is a copy, not a link to {root / 'skills' / 'loopgraph'}\n"
                    "  it did not follow this update; remove it and run lg update again")
    assert not dst.is_symlink()
    assert (dst / "SKILL.md").read_text(encoding="utf-8") == "# copied, and edited\n"


def test_relink_skill_does_nothing_when_the_checkout_has_no_skill(tmp_path):
    """A tree with no skills/loopgraph in it. Linking the destination at a path
    that is not there would leave the machine worse off than untouched."""
    root, home = tmp_path / "engine", tmp_path / "home"
    root.mkdir()
    home.mkdir()
    assert _lg().relink_skill(str(root), str(home)) is None
    assert not _skill_dst(home).exists()


def test_update_repoints_a_stale_skill_link_after_the_merge(
        clone_repo, monkeypatch, capsys, tmp_path):
    """The whole point, end to end: the user runs one command and the skill their
    agent reads is the one this release shipped."""
    lg = _lg()
    (clone_repo.root / "skills" / "loopgraph").mkdir(parents=True)
    clone_repo.commit("a skill", {"skills/loopgraph/SKILL.md": "# shipped\n"})
    clone_repo.tag("v0.1.0")
    clone_repo.push()
    _release_on_origin(clone_repo, "v0.2.0", {"CHANGELOG.md": RELEASE_NOTES})
    home = tmp_path / "home"
    dst = _skill_dst(home)
    dst.parent.mkdir(parents=True)
    dst.symlink_to(tmp_path / "somewhere-else")
    _temporal(lg, monkeypatch, ListingClient())
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys, home=home)
    assert (code, err) == (0, "")
    assert f"re-pointed {dst} -> {clone_repo.root / 'skills' / 'loopgraph'}" in out
    assert (dst / "SKILL.md").read_text(encoding="utf-8") == "# shipped\n"


def test_update_says_nothing_about_a_skill_link_that_is_already_right(
        clone_repo, monkeypatch, capsys, tmp_path):
    lg = _lg()
    (clone_repo.root / "skills" / "loopgraph").mkdir(parents=True)
    clone_repo.commit("a skill", {"skills/loopgraph/SKILL.md": "# shipped\n"})
    clone_repo.tag("v0.1.0")
    clone_repo.push()
    _release_on_origin(clone_repo, "v0.2.0", {"CHANGELOG.md": RELEASE_NOTES})
    home = tmp_path / "home"
    dst = _skill_dst(home)
    dst.parent.mkdir(parents=True)
    dst.symlink_to(clone_repo.root / "skills" / "loopgraph")
    _temporal(lg, monkeypatch, ListingClient())
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys, home=home)
    assert (code, err) == (0, "")
    assert "skills/loopgraph" not in out


# ------------------------------------- lg update: the venv, the .env, the stack ---

# Both banners the poll waits for, in one text: worker.py prints the first and
# dispatcher.py the second, and `compose logs <service>` shows only that
# service's, so one string answers either poll.
BANNERS = "worker up on task queue\ndispatcher up\n"

# The three shapes a release commit leaves pyproject.toml in: a floor that
# moved, the same dependencies under a new version line, and a floor that moved
# beside the one dependency the host is never asked to install.
NEW_DEPS = bumped(PYPROJECT.replace("temporalio>=1.15", "temporalio>=1.16"), "0.2.0")
SAME_DEPS = bumped(PYPROJECT, "0.2.0")
WITH_SDK = NEW_DEPS.replace('"pyyaml>=6.0",', '"pyyaml>=6.0",\n    "claude-agent-sdk>=0.1",')


def _ps(*services: str, code: int = 0, stderr: str = "", docker: str = "docker") -> dict:
    """What `compose ps --services --status running` answers, as a table entry.

    `docker` is what LOOPGRAPH_DOCKER puts in front of every compose command, and
    the key has to carry it: a table entry spelled `docker ...` does not match a
    `sudo docker ...` argv, and the update would read the miss as a stack that is
    not running rather than as the test's own mistake.
    """
    argv = [*docker.split(), "compose", "ps", "--services", "--status", "running"]
    listed = "".join(f"{s}\n" for s in services)
    return {tuple(argv): subprocess.CompletedProcess(argv, code, listed, stderr)}


def stack_run(table=None, logs: str = BANNERS, poll_code: int = 0, poll_stderr: str = ""):
    """fake_run, plus the one docker read whose argv a test cannot spell out.

    Every log poll carries the timestamp the update took a moment earlier, so no
    table can name it: any `--since` poll is answered with `logs`, which by
    default holds both banners and ends the poll on its first try. `poll_code`
    and `poll_stderr` are how a test makes that read fail instead. The closing
    `--tail` read is not a poll and stays the table's to answer.

    Nothing here spells `docker`: the polls are recognised by `logs` and
    `--since`, which is true of a `sudo docker` argv as well.
    """
    inner = fake_run(table)

    def run(argv, cwd, timeout=None, stream=False):
        done = inner(argv, cwd, timeout, stream)
        if "logs" in argv and "--since" in argv:
            return subprocess.CompletedProcess(argv, poll_code, logs, poll_stderr)
        return done

    run.table, run.calls = inner.table, inner.calls
    return run


def _ran(runner, *words: str) -> list[list[str]]:
    """Every argv the update ran that holds all of these words, in order."""
    return [argv for argv, _, _, _ in runner.calls if all(w in argv for w in words)]


def _behind_a_release(lg, clone_repo, monkeypatch, before: dict, after: dict) -> None:
    """A checkout on v0.1.0 holding `before`, one release behind a v0.2.0 that
    changed it to `after`. Temporal answers here too: every test in this section
    is about what happens after the open-run check, so all of them get past it.
    """
    clone_repo.commit("before the release", before)
    clone_repo.tag("v0.1.0")
    clone_repo.push()
    _release_on_origin(clone_repo, "v0.2.0", after)
    _temporal(lg, monkeypatch, ListingClient())


def test_update_refreshes_the_venv_with_uv_when_a_dependency_floor_changed(
        clone_repo, monkeypatch, capsys):
    lg = _lg()
    _behind_a_release(lg, clone_repo, monkeypatch,
                      {"pyproject.toml": PYPROJECT},
                      {"pyproject.toml": NEW_DEPS})
    monkeypatch.setattr(lg.shutil, "which", lambda name: "/usr/bin/uv")
    runner = stack_run(_ps("worker", "dispatcher"))
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys, runner)
    assert (code, err) == (0, "")
    assert out.splitlines()[-1] == "now on v0.2.0"
    assert [(argv, cwd) for argv, cwd, _, _ in runner.calls if argv[0] == "uv"] == [
        (["uv", "sync"], str(clone_repo.root))]


def test_a_version_only_bump_skips_the_venv_and_restarts(clone_repo, monkeypatch, capsys):
    """release.sh rewrites the version line of every release, so pyproject.toml
    is in every update's diff. What was installed is what decides, not the path."""
    lg = _lg()
    _behind_a_release(lg, clone_repo, monkeypatch,
                      {"pyproject.toml": PYPROJECT},
                      {"pyproject.toml": SAME_DEPS})
    runner = stack_run(_ps("worker", "dispatcher"))
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys, runner)
    assert (code, err) == (0, "")
    assert out.splitlines()[-1] == "now on v0.2.0"
    assert not [argv for argv, _, _, _ in runner.calls if argv[0] == "uv" or "pip" in argv]
    assert _ran(runner, "compose", "restart") == [
        ["docker", "compose", "restart", "worker", "dispatcher"]]


def test_the_venv_falls_back_to_python_m_pip_without_claude_agent_sdk(
        clone_repo, monkeypatch, capsys):
    """A machine with no uv. `.venv/bin/pip` is not used: a venv uv built has no
    pip script. claude-agent-sdk is dropped because the host never runs the
    executor — the worker container does, and it has its own install."""
    lg = _lg()
    _behind_a_release(lg, clone_repo, monkeypatch,
                      {"pyproject.toml": PYPROJECT},
                      {"pyproject.toml": WITH_SDK})
    monkeypatch.setattr(lg.shutil, "which", lambda name: None)
    runner = stack_run(_ps("worker", "dispatcher"))
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys, runner)
    assert (code, err) == (0, "")
    installed_with = _ran(runner, "pip")
    assert len(installed_with) == 1
    assert installed_with[0] == [str(clone_repo.root / ".venv" / "bin" / "python"),
                                 "-m", "pip", "install", "--quiet", "--upgrade",
                                 "temporalio>=1.16", "pyyaml>=6.0", "pytest"]


def test_a_missing_pip_is_a_venv_refresh_failure_not_a_traceback(
        clone_repo, monkeypatch, capsys):
    lg = _lg()
    _behind_a_release(lg, clone_repo, monkeypatch,
                      {"pyproject.toml": PYPROJECT},
                      {"pyproject.toml": NEW_DEPS})
    monkeypatch.setattr(lg.shutil, "which", lambda name: None)
    python = str(clone_repo.root / ".venv" / "bin" / "python")
    argv = [python, "-m", "pip", "install", "--quiet", "--upgrade",
            "temporalio>=1.16", "pyyaml>=6.0", "pytest"]
    missing = f"[Errno 2] No such file or directory: '{python}'"
    runner = stack_run({tuple(argv): subprocess.CompletedProcess(argv, 127, "", missing)})
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys, runner)
    assert (code, err) == (1, f"venv refresh failed: {missing}\n")


def test_a_venv_refresh_failure_exits_1_after_the_code_moved(
        clone_repo, monkeypatch, capsys):
    """The merge already happened, and saying so matters: the checkout is on the
    new release with an old venv, and the stack must not be restarted onto it."""
    lg = _lg()
    _behind_a_release(lg, clone_repo, monkeypatch,
                      {"pyproject.toml": PYPROJECT},
                      {"pyproject.toml": NEW_DEPS})
    monkeypatch.setattr(lg.shutil, "which", lambda name: "/usr/bin/uv")
    argv = ["uv", "sync"]
    runner = stack_run({("uv", "sync"): subprocess.CompletedProcess(
        argv, 1, "", "\nerror: no solution found for temporalio>=1.16\n")})
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys, runner)
    assert (code, err) == (1, "venv refresh failed: error: no solution found "
                              "for temporalio>=1.16\n")
    assert out.splitlines()[0] == "updated v0.1.0 → v0.2.0"
    assert clone_repo.git("rev-parse", "HEAD") == clone_repo.git("rev-parse", "v0.2.0^{commit}")
    assert not _ran(runner, "compose")


def test_a_hung_venv_refresh_gives_up_on_the_stack_timeout(clone_repo, monkeypatch, capsys):
    """A resolver waiting on an index that stopped answering. Without a deadline
    on it the update sits there for good with the checkout already moved, which
    is the one place `lg update` must never stop without saying anything."""
    lg = _lg()
    _behind_a_release(lg, clone_repo, monkeypatch,
                      {"pyproject.toml": PYPROJECT},
                      {"pyproject.toml": NEW_DEPS})
    monkeypatch.setattr(lg.shutil, "which", lambda name: "/usr/bin/uv")
    argv = ["uv", "sync"]
    runner = stack_run({("uv", "sync"): subprocess.CompletedProcess(
        argv, 124, "", "timed out after 1800s")})
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys, runner)
    assert (code, err) == (1, "venv refresh failed: timed out after 1800s\n")
    assert [(a, timeout) for a, _, timeout, _ in runner.calls if a[0] == "uv"] == [
        (["uv", "sync"], lg.STACK_TIMEOUT)]
    assert not _ran(runner, "compose")


def test_the_pip_fallback_carries_the_same_deadline(clone_repo, monkeypatch, capsys):
    """The other half of the refresh, on a machine with no uv. pip resolves over
    the network too, and a hang there is the same hang."""
    lg = _lg()
    _behind_a_release(lg, clone_repo, monkeypatch,
                      {"pyproject.toml": PYPROJECT},
                      {"pyproject.toml": NEW_DEPS})
    monkeypatch.setattr(lg.shutil, "which", lambda name: None)
    runner = stack_run(_ps("worker", "dispatcher"))
    code, _, err = _update(lg, clone_repo.root, monkeypatch, capsys, runner)
    assert (code, err) == (0, "")
    assert [timeout for a, _, timeout, _ in runner.calls if "pip" in a] == [lg.STACK_TIMEOUT]


def test_the_venv_refresh_runs_before_the_settings_message(clone_repo, monkeypatch, capsys):
    """Order, read off the exit code: install.sh only builds a venv when there is
    none, so stopping at the settings message first would leave the host lg
    missing a dependency it now imports."""
    lg = _lg()
    _behind_a_release(lg, clone_repo, monkeypatch,
                      {"pyproject.toml": PYPROJECT,
                       ".env.example": "LOOPGRAPH_OLD=1\n"},
                      {"pyproject.toml": NEW_DEPS,
                       ".env.example": "LOOPGRAPH_OLD=1\nLOOPGRAPH_NEW=\n"})
    monkeypatch.setattr(lg.shutil, "which", lambda name: "/usr/bin/uv")
    argv = ["uv", "sync"]
    runner = stack_run({("uv", "sync"): subprocess.CompletedProcess(argv, 1, "", "boom\n")})
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys, runner)
    assert (code, err) == (1, "venv refresh failed: boom\n")
    assert "new settings" not in out


def test_new_settings_exit_2_and_never_reach_docker(clone_repo, monkeypatch, capsys):
    lg = _lg()
    _behind_a_release(lg, clone_repo, monkeypatch,
                      {"pyproject.toml": PYPROJECT,
                       ".env.example": "LOOPGRAPH_OLD=1\n"},
                      {"pyproject.toml": PYPROJECT,
                       ".env.example": "LOOPGRAPH_OLD=1\nLOOPGRAPH_NEW=\n"})
    (clone_repo.root / ".env").write_text("LOOPGRAPH_DOCKER=sudo docker\n", encoding="utf-8")
    runner = stack_run(_ps("worker", "dispatcher"))
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys, runner)
    assert (code, err) == (2, "")
    assert out.splitlines() == [
        "updated v0.1.0 → v0.2.0",
        "(no changelog entry for v0.2.0)",
        f"new settings since v0.1.0: LOOPGRAPH_NEW. Add them to .env (see .env.example "
        f"for what each means), then start the stack: cd {clone_repo.root} "
        "&& sudo docker compose up -d",
    ]
    assert not _ran(runner, "compose")


def test_commented_out_example_keys_are_not_new_settings(clone_repo, monkeypatch, capsys):
    """The example carries the route the user did not take as commented lines.
    Nagging about those would mean nagging on every single update."""
    lg = _lg()
    _behind_a_release(lg, clone_repo, monkeypatch,
                      {"pyproject.toml": PYPROJECT,
                       ".env.example": "LOOPGRAPH_OLD=1\n"},
                      {"pyproject.toml": PYPROJECT,
                       ".env.example": "LOOPGRAPH_OLD=1\n# ANTHROPIC_API_KEY=sk-ant-...\n"})
    runner = stack_run(_ps("worker", "dispatcher"))
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys, runner)
    assert (code, err) == (0, "")
    assert out.splitlines()[-1] == "now on v0.2.0"


def test_a_key_that_moves_from_commented_to_uncommented_is_a_new_setting(
        clone_repo, monkeypatch, capsys):
    """The release just turned that key into a decision the user has to make."""
    lg = _lg()
    _behind_a_release(lg, clone_repo, monkeypatch,
                      {"pyproject.toml": PYPROJECT,
                       ".env.example": "# LOOPGRAPH_NEW=\n"},
                      {"pyproject.toml": PYPROJECT,
                       ".env.example": "LOOPGRAPH_NEW=\n"})
    runner = stack_run(_ps("worker", "dispatcher"))
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys, runner)
    assert code == 2
    assert out.splitlines()[-1].startswith("new settings since v0.1.0: LOOPGRAPH_NEW.")


@pytest.mark.parametrize("changed,tail", [
    ({"ui.py": "# released\n"}, ""),
    ({"Dockerfile": "FROM python:3.13-slim\n"}, " --build"),
])
def test_a_stack_that_is_not_running_prints_the_start_command(
        clone_repo, monkeypatch, capsys, changed, tail):
    """Nothing is restarted, because nothing is up. The user gets the one command
    that starts it, with the --build a changed image needs."""
    lg = _lg()
    _behind_a_release(lg, clone_repo, monkeypatch,
                      {"pyproject.toml": PYPROJECT},
                      {"pyproject.toml": PYPROJECT, **changed})
    runner = stack_run(_ps("temporal"))
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys, runner)
    assert (code, err) == (0, "")
    assert out.splitlines()[-1] == (
        f"the stack is not running; start it with: cd {clone_repo.root} "
        f"&& docker compose up -d{tail}")
    assert _ran(runner, "compose") == [
        ["docker", "compose", "ps", "--services", "--status", "running"]]


def test_a_failing_compose_ps_is_compose_failed_not_a_stopped_stack(
        clone_repo, monkeypatch, capsys):
    """docker-compose.yml guards every required key with ${VAR:?}, so a missing
    one fails every subcommand with empty stdout. Reading that as "not running"
    would leave a worker on the old code and say nothing was up."""
    lg = _lg()
    _behind_a_release(lg, clone_repo, monkeypatch,
                      {"pyproject.toml": PYPROJECT},
                      {"pyproject.toml": PYPROJECT,
                       "ui.py": "# released\n"})
    runner = stack_run(_ps(code=1, stderr="\nrequired variable LOOPGRAPH_UID is missing\n"))
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys, runner)
    assert (code, err) == (1, "compose failed: required variable LOOPGRAPH_UID is missing\n")
    assert "not running" not in out
    assert not _ran(runner, "compose", "restart")


@pytest.mark.parametrize("changed,expected", [
    ({"ui.py": "# released\n"}, ["restart", "worker", "dispatcher"]),
    ({"docker-compose.yml": "services: {}\n"}, ["up", "-d", "worker", "dispatcher"]),
    ({"Dockerfile": "FROM python:3.13-slim\n"}, ["up", "-d", "--build", "worker", "dispatcher"]),
])
def test_restart_recreate_and_build_pick_the_compose_command(
        clone_repo, monkeypatch, capsys, changed, expected):
    """A restart re-runs a container with the configuration it was created from,
    which is exactly what a compose change needs it not to do."""
    lg = _lg()
    _behind_a_release(lg, clone_repo, monkeypatch,
                      {"pyproject.toml": PYPROJECT},
                      {"pyproject.toml": PYPROJECT, **changed})
    runner = stack_run(_ps("worker", "dispatcher"))
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys, runner)
    assert (code, err) == (0, "")
    assert out.splitlines()[-1] == "now on v0.2.0"
    assert _ran(runner, "compose", expected[0], "worker") == [["docker", "compose", *expected]]


def test_the_stack_command_is_streamed_and_the_reads_are_not(
        clone_repo, monkeypatch, capsys):
    """A --build takes minutes and a sudo docker may ask for a password, so that
    one command gets the terminal. ps and the polls are read, so they are not."""
    lg = _lg()
    _behind_a_release(lg, clone_repo, monkeypatch,
                      {"pyproject.toml": PYPROJECT},
                      {"pyproject.toml": PYPROJECT,
                       "ui.py": "# released\n"})
    runner = stack_run(_ps("worker", "dispatcher"))
    code, _, err = _update(lg, clone_repo.root, monkeypatch, capsys, runner)
    assert (code, err) == (0, "")
    compose = [(argv, timeout, stream) for argv, _, timeout, stream in runner.calls
               if "compose" in argv]
    assert [(t, s) for argv, t, s in compose if "restart" in argv] == [(1800.0, True)]
    assert [(t, s) for argv, t, s in compose if "restart" not in argv] == [(60.0, False)] * 3


@pytest.mark.parametrize("said,line", [
    ("", "compose failed: exit 1"),
    ("Error response from daemon: no such image\n", "compose failed: Error response "
                                                   "from daemon: no such image"),
])
def test_a_failing_stack_command_is_compose_failed(
        clone_repo, monkeypatch, capsys, said, line):
    """A streamed command has already put its own output on the terminal, so an
    empty stderr is the ordinary case and the exit code is what is left to say."""
    lg = _lg()
    _behind_a_release(lg, clone_repo, monkeypatch,
                      {"pyproject.toml": PYPROJECT},
                      {"pyproject.toml": PYPROJECT,
                       "ui.py": "# released\n"})
    argv = ["docker", "compose", "restart", "worker", "dispatcher"]
    table = _ps("worker", "dispatcher")
    table[tuple(argv)] = subprocess.CompletedProcess(argv, 1, "", said)
    runner = stack_run(table)
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys, runner)
    assert (code, err) == (1, f"{line}\n")
    assert not _ran(runner, "logs")


def test_a_log_poll_that_fails_is_compose_failed_too(clone_repo, monkeypatch, capsys):
    """The restart went through and the read after it is what broke — a daemon
    that went away mid-update, or a socket the user's group no longer opens.
    Treating that as "not up yet" would spend two minutes on it and then blame
    the worker for a silence that was never the worker's."""
    lg = _lg()
    _behind_a_release(lg, clone_repo, monkeypatch,
                      {"pyproject.toml": PYPROJECT},
                      {"pyproject.toml": PYPROJECT,
                       "ui.py": "# released\n"})
    runner = stack_run(_ps("worker", "dispatcher"), logs="", poll_code=1,
                       poll_stderr="\nerror during connect: dial unix docker.sock\n")
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys, runner)
    assert (code, err) == (
        1, "compose failed: error during connect: dial unix docker.sock\n")
    assert len(_ran(runner, "logs", "--since")) == 1, "it polled on after the failure"
    assert not _ran(runner, "logs", "--tail"), "the closing log belongs to the timeout"


def test_the_docker_prefix_leads_every_compose_command_the_update_runs(
        clone_repo, monkeypatch, capsys):
    """`LOOPGRAPH_DOCKER=sudo docker` is what install.sh writes for a user who is
    not in the docker group, and every compose command has to carry it: one that
    did not would prompt for nothing, fail on the socket, and read as a stack
    that is not running."""
    lg = _lg()
    _behind_a_release(lg, clone_repo, monkeypatch,
                      {"pyproject.toml": PYPROJECT},
                      {"pyproject.toml": PYPROJECT,
                       "ui.py": "# released\n"})
    (clone_repo.root / ".env").write_text("LOOPGRAPH_DOCKER=sudo docker\n", encoding="utf-8")
    runner = stack_run(_ps("worker", "dispatcher", docker="sudo docker"))
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys, runner)
    assert (code, err) == (0, "")
    assert out.splitlines()[-1] == "now on v0.2.0"
    compose = _ran(runner, "compose")
    assert compose, "no compose command ran at all"
    assert all(argv[:3] == ["sudo", "docker", "compose"] for argv in compose), compose
    assert _ran(runner, "compose", "ps") == [
        ["sudo", "docker", "compose", "ps", "--services", "--status", "running"]]
    assert _ran(runner, "compose", "restart") == [
        ["sudo", "docker", "compose", "restart", "worker", "dispatcher"]]
    assert [argv[-1] for argv in _ran(runner, "logs", "--since")] == ["worker", "dispatcher"]


def test_now_on_the_tag_once_both_banners_appear(clone_repo, monkeypatch, capsys):
    """Each service is polled until its own banner shows and not once after, so
    a worker that came up first is not asked again while the dispatcher starts."""
    lg = _lg()
    _behind_a_release(lg, clone_repo, monkeypatch,
                      {"pyproject.toml": PYPROJECT},
                      {"pyproject.toml": PYPROJECT,
                       "ui.py": "# released\n"})
    inner = fake_run(_ps("worker", "dispatcher"))
    polled: dict[str, int] = {}
    banner = {"worker": "worker up on task queue", "dispatcher": "dispatcher up"}

    def runner(argv, cwd, timeout=None, stream=False):
        done = inner(argv, cwd, timeout, stream)
        if "logs" in argv and "--since" in argv:
            service = argv[-1]
            polled[service] = polled.get(service, 0) + 1
            up = polled[service] >= (2 if service == "worker" else 3)
            return subprocess.CompletedProcess(argv, 0, banner[service] if up else "", "")
        return done

    runner.table, runner.calls = inner.table, inner.calls
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys, runner)
    assert (code, err) == (0, "")
    assert out.splitlines()[-1] == "now on v0.2.0"
    assert polled == {"worker": 2, "dispatcher": 3}
    since = _ran(runner, "logs", "worker")[0]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", since[since.index("--since") + 1])


def test_the_poll_times_out_naming_the_service_that_never_came_up(
        clone_repo, monkeypatch, capsys):
    """Sixty tries two seconds apart. The closing log has no --since: when the
    restart did not take, that window is empty and "log above" sits over nothing."""
    lg = _lg()
    _behind_a_release(lg, clone_repo, monkeypatch,
                      {"pyproject.toml": PYPROJECT},
                      {"pyproject.toml": PYPROJECT,
                       "ui.py": "# released\n"})
    tail = ["docker", "compose", "logs", "--tail", "20", "worker", "dispatcher"]
    table = _ps("worker", "dispatcher")
    table[tuple(tail)] = subprocess.CompletedProcess(
        tail, 0, "dispatcher  | Traceback (most recent call last)\n", "")
    inner = fake_run(table)

    def runner(argv, cwd, timeout=None, stream=False):
        done = inner(argv, cwd, timeout, stream)
        if "logs" in argv and "--since" in argv:
            said = "worker up on task queue\n" if argv[-1] == "worker" else ""
            return subprocess.CompletedProcess(argv, 0, said, "")
        return done

    runner.table, runner.calls = inner.table, inner.calls
    code, out, err = _update(lg, clone_repo.root, monkeypatch, capsys, runner)
    assert (code, err) == (
        1, "the dispatcher did not come up after the update (log above)\n")
    assert len(_ran(runner, "logs", "--since", "dispatcher")) == 60
    assert len(_ran(runner, "logs", "--since", "worker")) == 1
    assert _ran(runner, "logs", "--tail") == [tail]
    assert out.splitlines()[-1] == "dispatcher  | Traceback (most recent call last)"


# --------------------------------- what the docs and the installer teach ---

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()
AGENTS = (ROOT / "AGENTS.md").read_text()
SKILL = (ROOT / "skills/loopgraph/SKILL.md").read_text()
INSTALL = (ROOT / "install.sh").read_text()


def _section(text: str, heading: str) -> str:
    """One `##` section of a markdown file, heading line included.

    Whole lines only: `## Unreleased` and `lg update` both appear inside prose
    in these files, and a substring search would find those instead.
    """
    lines = text.splitlines()
    start = lines.index(heading)
    rest = lines[start + 1:]
    end = next((i for i, ln in enumerate(rest) if ln.startswith("## ")), len(rest))
    return "\n".join(lines[start:start + 1 + end])


@pytest.fixture
def skill_step_0() -> str:
    return _section(SKILL, "## 0. Preconditions (check, don't assume)")


def test_the_skill_runs_lg_version_beside_lg_where(skill_step_0):
    """The skill is what an agent in another project reads before it starts a
    run, so `lg version` has to sit in the same block it already runs, not in a
    paragraph underneath it. And the agent is told not to act on the answer:
    `lg update` restarts the stack, which is the user's call and never a step
    on the way to a run."""
    blocks = [b for b in skill_step_0.split("```") if "lg where" in b]
    assert len(blocks) == 1, "step 0 should have one block with lg where in it"
    assert "lg version" in blocks[0]
    assert "Do not run" in skill_step_0
    assert "lg update" in skill_step_0


def test_the_readme_has_an_updating_section_between_install_and_using_it():
    """Order, because a reader who has just installed reads down. And the
    rollback has to name the rebuild: a rollback that only restarts leaves the
    container on the newer image, which is the failure nobody would connect to
    the checkout they moved."""
    headings = [ln for ln in README.splitlines() if ln.startswith("## ")]
    assert headings.index("## Install") + 1 == headings.index("## Updating")
    assert headings.index("## Updating") + 1 == headings.index("## Using it")

    section = _section(README, "## Updating")
    for said in ("lg version", "lg update", "git checkout v",
                 "up -d --build worker dispatcher", "git checkout main"):
        assert said in section, f"the Updating section never says {said}"
    assert "compose restart" not in section


def test_agents_md_lists_the_three_new_files_and_has_a_releasing_section():
    """An agent changing the engine finds files through this list, and a file
    nobody lists is a file somebody re-invents. The releasing rule is the one
    that matters most: release.sh pushes, and an agent must not run it."""
    layout = _section(AGENTS, "## Layout")
    for name in ("`version.py`", "`release.sh`", "`CHANGELOG.md`"):
        assert f"- {name} —" in layout, f"Layout never lists {name}"

    releasing = _section(AGENTS, "## Releasing")
    assert "./release.sh X.Y.Z" in releasing
    assert "## Unreleased" in releasing
    assert "No agent runs `release.sh`." in releasing


def test_the_installers_closing_text_names_lg_version_under_lg_where():
    """The last thing install.sh prints is the only instruction some users ever
    read, and it is a heredoc, so the line is pinned whole: a comment shifted
    out of its column is a ragged block in front of a first-time user."""
    line = ("  lg version                                      "
            "# what you have, and whether a newer release exists")
    lines = INSTALL.splitlines()
    assert line in lines, "install.sh's Try it block never names lg version"
    before = lines[lines.index(line) - 1]
    assert before.startswith("  lg where")
    assert before.index("#") == line.index("#")


# ------------------------------- install.sh: the skill link it leaves behind ---

def _install_skill_step(root, home, yes="1", answer=1):
    """Section 6 of install.sh, run for real, as (code, stdout).

    The section is cut out of the shipped file rather than retyped, so what runs
    here is the text a user downloads. What it leans on from the rest of the
    script is stubbed above it: the two printers, the prompt, and the two paths.
    `answer` is what confirm returns, so 0 is a user saying yes.
    """
    body = INSTALL.split("6. skill ---")[1].split("7. up ----")[0]
    script = (
        # bash with the same flags install.sh sets on itself: under `set -e` a
        # test that ran the section in a laxer shell than the user's would pass
        # on a line that aborts a real install.
        "set -euo pipefail\n"
        f"YES={yes}\n"
        f"ROOT={shlex.quote(str(root))}\n"
        f"HOME={shlex.quote(str(home))}\n"
        "say() { printf '%s\\n' \"$*\"; }\n"
        "warn() { printf '%s\\n' \"$*\"; }\n"
        f"confirm() {{ return {answer}; }}\n"
        + body)
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert done.stderr == "", done.stderr
    return done.returncode, done.stdout


@pytest.fixture
def installable(tmp_path):
    """A checkout with a skill in it, and an empty home to install into."""
    root, home = tmp_path / "engine", tmp_path / "home"
    (root / "skills" / "loopgraph").mkdir(parents=True)
    (root / "skills" / "loopgraph" / "SKILL.md").write_text("# shipped\n", encoding="utf-8")
    home.mkdir()
    return root, home


def test_install_links_the_skill_into_a_home_that_has_none(installable):
    root, home = installable
    code, out = _install_skill_step(root, home)
    dst = home / ".claude" / "skills" / "loopgraph"
    assert code == 0
    assert dst.is_symlink()
    assert dst.resolve() == (root / "skills" / "loopgraph").resolve()
    assert "linked" in out


def test_install_repoints_a_link_into_another_checkout(installable):
    """The bug this section had: it asked whether anything was at the
    destination, never whether it pointed here, so a link left over from an
    older clone survived every re-run and every update after it."""
    root, home = installable
    stale = root.parent / "old-engine" / "skills" / "loopgraph"
    stale.mkdir(parents=True)
    (stale / "SKILL.md").write_text("# old\n", encoding="utf-8")
    dst = home / ".claude" / "skills" / "loopgraph"
    dst.parent.mkdir(parents=True)
    dst.symlink_to(stale)
    code, out = _install_skill_step(root, home)
    assert code == 0
    assert (dst / "SKILL.md").read_text(encoding="utf-8") == "# shipped\n"
    assert (stale / "SKILL.md").read_text(encoding="utf-8") == "# old\n"
    assert "re-pointed" in out


def test_install_leaves_a_link_that_is_already_right_alone(installable):
    root, home = installable
    dst = home / ".claude" / "skills" / "loopgraph"
    dst.parent.mkdir(parents=True)
    dst.symlink_to(root / "skills" / "loopgraph")
    was = dst.readlink()
    code, out = _install_skill_step(root, home)
    assert code == 0
    assert dst.readlink() == was
    assert "already linked" in out


def test_install_names_a_copied_directory_and_writes_nothing(installable):
    """A copy someone made by hand. The installer says why it will go stale and
    what to do, and leaves whatever the user wrote in it where it is."""
    root, home = installable
    dst = home / ".claude" / "skills" / "loopgraph"
    dst.mkdir(parents=True)
    (dst / "SKILL.md").write_text("# copied, and edited\n", encoding="utf-8")
    code, out = _install_skill_step(root, home)
    assert code == 0
    assert not dst.is_symlink()
    assert (dst / "SKILL.md").read_text(encoding="utf-8") == "# copied, and edited\n"
    assert "copy" in out and "Remove it" in out
