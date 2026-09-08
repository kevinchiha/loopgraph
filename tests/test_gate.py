import asyncio
import pathlib
import subprocess

import pytest

from activities.gate import _run_one, load_gates

ROOT = pathlib.Path(__file__).resolve().parent.parent


def write(tmp_path, text):
    p = tmp_path / "gates.yaml"
    p.write_text(text)
    return str(p)


def test_load_valid_defaults(tmp_path):
    gates = load_gates(write(tmp_path, '- name: t\n  cmd: "true"\n'))
    assert gates[0]["green_exit"] == 0
    assert gates[0]["timeout"] == 600


def test_load_custom_exit_and_timeout(tmp_path):
    gates = load_gates(write(tmp_path, '- name: t\n  cmd: "x"\n  green_when: "exit 2"\n  timeout: 5\n'))
    assert gates[0]["green_exit"] == 2
    assert gates[0]["timeout"] == 5


@pytest.mark.parametrize("text", [
    "",                                  # empty
    "name: no-list\n",                   # not a list
    "- name: t\n",                       # missing cmd
    '- cmd: "true"\n',                   # missing name
    '- name: t\n  cmd: "x"\n  green_when: "contains ok"\n',  # unsupported matcher
])
def test_load_rejects_malformed(tmp_path, text):
    with pytest.raises(ValueError):
        load_gates(write(tmp_path, text))


def test_run_one_green(tmp_path):
    r = asyncio.run(_run_one({"name": "ok", "cmd": "true", "green_exit": 0, "timeout": 5}, str(tmp_path)))
    assert r["status"] == "green" and r["exit_code"] == 0


def test_run_one_red_on_wrong_exit(tmp_path):
    r = asyncio.run(_run_one({"name": "no", "cmd": "false", "green_exit": 0, "timeout": 5}, str(tmp_path)))
    assert r["status"] == "red" and r["exit_code"] == 1


def test_run_one_red_on_timeout(tmp_path):
    r = asyncio.run(_run_one({"name": "slow", "cmd": "sleep 5", "green_exit": 0, "timeout": 1}, str(tmp_path)))
    assert r["status"] == "red" and r["exit_code"] is None and "timeout" in r["note"]


def test_output_tail_bounded(tmp_path):
    r = asyncio.run(_run_one({"name": "loud", "cmd": "yes | head -c 8000", "green_exit": 0, "timeout": 5}, str(tmp_path)))
    assert r["status"] == "green" and len(r["output_tail"]) == 4000


# ---------- the shipped scope gate ----------
#
# `runs/example-hello/check-write-set.sh` is the template the skill and the README
# both tell you to copy, so a defect in it is a defect in every run made from it.
# It shipped for weeks under `#!/bin/sh` while using `read -d`, which is a bash
# extension: under dash the loop died on its first line, the body never ran, and
# the gate printed "write set in scope" and exited 0 whatever had changed. Five
# separate executor rounds noticed and wrote it into a constraints file; nobody
# changed the shebang, because nothing here was watching. A gate that cannot go
# red reads exactly like a gate that passed.
#
# Which of these tests actually guards that regression depends on the machine,
# and it is not the one you would expect. The four behavioural tests run the
# script through its own shebang, so on a host whose /bin/sh is bash — this one,
# and most developer machines — they pass the broken version happily. The bug
# only shows where the run actually happens, in the worker container, where
# /bin/sh is dash. So `test_scope_gate_is_bash_not_sh` is the guard, and it is a
# one-line string comparison on purpose: it is the only check here that fails on
# a host that cannot reproduce the failure. The behavioural four are still worth
# their runtime — they cover the parsing the comments below describe, including
# the filename with a space that once made this gate red forever.

SCOPE_GATE = ROOT / "runs" / "example-hello" / "check-write-set.sh"


def _repo_with(tmp_path, *names):
    """A git repo holding `names` as untracked files, and the gate copied in."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for n in names:
        (tmp_path / n).write_text("x\n")
    return tmp_path


def _run_gate(cwd):
    return subprocess.run([str(SCOPE_GATE)], cwd=cwd, capture_output=True, text=True)


def test_scope_gate_is_bash_not_sh():
    """The regression guard. `read -d` is a bashism; under the container's dash
    the loop never runs and the gate exits 0 whatever changed. Nothing else in
    this file catches that on a host whose /bin/sh is bash."""
    assert SCOPE_GATE.read_text().splitlines()[0] == "#!/bin/bash"


def test_scope_gate_green_on_a_clean_tree(tmp_path):
    out = _run_gate(_repo_with(tmp_path))
    assert out.returncode == 0, out.stderr
    assert "write set in scope" in out.stdout


def test_scope_gate_green_when_only_the_write_set_changed(tmp_path):
    out = _run_gate(_repo_with(tmp_path, "cli.py", "test_cli.py"))
    assert out.returncode == 0, out.stderr


def test_scope_gate_goes_red_and_names_the_file(tmp_path):
    """The direction that matters. A gate proven only green proves nothing."""
    out = _run_gate(_repo_with(tmp_path, "cli.py", "somewhere_else.py"))
    assert out.returncode != 0, f"gate passed an out-of-scope file: {out.stdout!r}"
    assert "somewhere_else.py" in out.stdout


def test_scope_gate_survives_a_filename_with_a_space(tmp_path):
    out = _run_gate(_repo_with(tmp_path, "two words.py"))
    assert out.returncode != 0
    assert "two words.py" in out.stdout
