"""One .env reader, so `lg` and `ui.py` cannot disagree about what the file says.

The failure this guards: a second copy of the rules drifts from the first, and
`lg where` hands the skill a path with its quotes still on while the container
gets the real one.
"""

from __future__ import annotations

import importlib.util
import inspect
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

from envfile import read_env

ROOT = Path(__file__).resolve().parent.parent


def _lg():
    """`lg` has no .py extension, so it loads by path."""
    loader = SourceFileLoader("lg_cli_env", str(ROOT / "lg"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("lg_cli_env", loader))
    loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("line,key,value", [
    ('LOOPGRAPH_PROJECTS_DIR="/srv/code"', "LOOPGRAPH_PROJECTS_DIR", "/srv/code"),
    ("LOOPGRAPH_DOCKER='sudo docker'", "LOOPGRAPH_DOCKER", "sudo docker"),
    ("export LOOPGRAPH_UID=1001", "LOOPGRAPH_UID", "1001"),
    ("ANTHROPIC_MODEL=claude-opus-5  # the dated id", "ANTHROPIC_MODEL", "claude-opus-5"),
])
def test_reads_the_four_shapes_compose_reads(tmp_path, line, key, value):
    """The four shapes `test_lg_reads_env_the_way_compose_does` pins, asked of the
    shared reader directly: a pair of quotes, either kind, the `export` keyword
    and an inline comment all mean here what they mean to compose."""
    env = tmp_path / ".env"
    env.write_text(line + "\n")
    assert read_env(env)[key] == value


def test_absent_file_is_empty(tmp_path):
    """There is no .env until install.sh writes one, and reading it then is not
    an error — `lg where` prints "(unset - run ./install.sh)" off the back of it."""
    assert read_env(tmp_path / ".env") == {}


def test_blank_comment_and_bare_lines_are_skipped(tmp_path):
    """A hand-edited .env picks up blank lines, comments and stray words. None of
    them is a setting, and a bare word has no `=` to split on."""
    env = tmp_path / ".env"
    env.write_text("\n"
                   "# what install.sh wrote this for\n"
                   "   \n"
                   "TODO remember to set the model\n"
                   "LOOPGRAPH_DOCKER=docker\n")
    assert read_env(str(env)) == {"LOOPGRAPH_DOCKER": "docker"}


def test_a_quoted_value_keeps_its_hash(tmp_path):
    """Quotes win over the inline-comment cut. Cutting first would turn a quoted
    value containing ` #` into half of itself plus a dangling quote."""
    env = tmp_path / ".env"
    env.write_text('X="a #b"\n')
    assert read_env(env)["X"] == "a #b"


def test_lg_dotenv_delegates_to_the_shared_reader():
    """Keeping a second copy of the parser in `lg` is the whole defect. The name
    stays, because tests monkeypatch lg.ROOT and call it; the rules do not."""
    src = inspect.getsource(_lg()._dotenv)
    assert "read_env(" in src, "lg still parses .env itself"
