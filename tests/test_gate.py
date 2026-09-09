import asyncio
import os
import pathlib
import subprocess

import pytest

from activities.gate import _run_one, load_gates
from graphs.round_graph import run_round

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
    # `|| true` because the gate now runs under pipefail: head closes the pipe,
    # yes dies of SIGPIPE, and the pipeline reports 141.
    # `test_a_stage_that_stops_reading_early_is_red_with_141` pins that.
    r = asyncio.run(_run_one({"name": "loud", "cmd": "yes | head -c 8000 || true", "green_exit": 0, "timeout": 5}, str(tmp_path)))
    assert r["status"] == "green" and len(r["output_tail"]) == 4000


# ---------- the shell the gate runs under ----------


def test_a_pipeline_is_red_when_an_early_stage_fails(tmp_path):
    """The defect this file's runner shipped with. A POSIX shell reports a
    pipeline's exit as its last stage's, so `pytest -q | tee log` exited 0 with
    pytest red, and gate exit codes are the only thing the round loop bounds
    itself on."""
    r = asyncio.run(_run_one({"name": "pipe", "cmd": "false | true", "green_exit": 0, "timeout": 5}, str(tmp_path)))
    assert r["status"] == "red" and r["exit_code"] == 1


def test_the_exit_code_is_the_rightmost_failing_stage(tmp_path):
    """Which failure the executor is handed when two stages fail. Under pipefail
    bash reports the last one that failed, not the first, and the skill states
    that rule. The trailing `| true` does two jobs: it exits 0 under /bin/sh, so
    this is red on the old runner, and it puts a passing stage after both failing
    ones, so a shell reporting the first failure would say 3 and be caught."""
    cmd = 'sh -c "exit 3" | sh -c "exit 4" | true'
    r = asyncio.run(_run_one({"name": "twice", "cmd": cmd, "green_exit": 0, "timeout": 5}, str(tmp_path)))
    assert r["status"] == "red" and r["exit_code"] == 4


def test_the_gate_runs_under_bash(tmp_path):
    """A gate written against bash gets bash. The old runner used /bin/sh, which
    is dash in the worker container. The scope gate below hit the same trap from
    the other side, through its own `#!/bin/sh` shebang: the runner's shell never
    reached inside a script it called, then or now."""
    r = asyncio.run(_run_one({"name": "shell", "cmd": 'echo "$0"', "green_exit": 0, "timeout": 5}, str(tmp_path)))
    assert r["status"] == "green"
    assert r["output_tail"].strip() == "/bin/bash"


def test_the_command_is_passed_as_written(tmp_path):
    """Nothing is prepended to the command string. A `set -o pipefail` line in
    front of it would move every line number bash prints in its own errors by
    one, and that text lands in output_tail. A shebang, a blank line and a
    comment are just comments to `-c`, and the reported cmd still matches
    gates.yaml character for character."""
    cmd = "#!/bin/sh\n\n# a comment\nfalse | true"
    r = asyncio.run(_run_one({"name": "written", "cmd": cmd, "green_exit": 0, "timeout": 5}, str(tmp_path)))
    assert r["status"] == "red" and r["exit_code"] == 1
    assert r["cmd"] == cmd


def test_a_red_pipeline_reaches_the_executor_as_written(tmp_path):
    """What the executor reads about a newly-red pipeline: its own command, and
    nothing about the shell it ran under. `_why` prints `command: {cmd}` into the
    correction feedback and `lg status` shows the owner the same string, so a
    wrapper leaking into it would send the executor after a fault in loopgraph."""
    seen = []

    async def exec_fn(prompt, feedback):
        seen.append(feedback)
        return {"claims": [], "files_changed": [], "summary": "x"}

    async def gate_fn():
        return [await _run_one({"name": "pipe", "cmd": "false | true", "green_exit": 0, "timeout": 5}, str(tmp_path))]

    asyncio.run(run_round("do the thing", exec_fn, gate_fn, max_attempts=2))
    correction = seen[1]
    assert "GATE pipe FAILED (exit 1)" in correction
    assert "command: false | true" in correction
    assert "pipefail" not in correction and "/bin/bash" not in correction


def test_a_stage_that_stops_reading_early_is_red_with_141(tmp_path):
    """What pipefail costs, pinned so nobody meets it as a mystery. head closes
    the pipe, yes dies of SIGPIPE, and the pipeline reports 141. The runner masks
    no exit code: it cannot tell a truncated producer from a tool that crashed,
    so the number and the command go to the executor and the gate's author
    decides."""
    r = asyncio.run(_run_one({"name": "cut", "cmd": "yes | head -c 8000", "green_exit": 0, "timeout": 5}, str(tmp_path)))
    assert r["status"] == "red" and r["exit_code"] == 141


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
# and it is not the one you would expect. The behavioural tests run the
# script through its own shebang, so on a host whose /bin/sh is bash — this one,
# and most developer machines — they pass the broken version happily. The bug
# only shows where the run actually happens, in the worker container, where
# /bin/sh is dash. So `test_scope_gate_is_bash_not_sh` is the guard, and it is a
# one-line string comparison on purpose: it is the only check here that fails on
# a host that cannot reproduce the failure. The behavioural ones are still worth
# their runtime — they cover the parsing the comments below describe, including
# the filename with a space that once made this gate red forever.

SCOPE_GATE = ROOT / "runs" / "example-hello" / "check-write-set.sh"


def _repo_with(tmp_path, *names):
    """A git repo holding `names` as untracked files, and the gate copied in."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for n in names:
        (tmp_path / n).write_text("x\n")
    return tmp_path


def _run_gate(cwd, env=None):
    """`env=None` inherits this process's environment, which is what every
    caller but the git-failure test wants."""
    return subprocess.run([str(SCOPE_GATE)], cwd=cwd, capture_output=True, text=True, env=env)


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


def test_scope_gate_goes_red_when_git_status_fails(tmp_path):
    """The gate has to be red when it could not look. `|| bad=1` reads the exit
    of the `{ ... }` block, so without pipefail a failing `git status` hands the
    block no input, the loop never runs, the block exits 0, and the gate prints
    "write set in scope" having checked nothing. GIT_DIR names a path that does
    not exist, so git exits 128 wherever pytest puts tmp_path, and the third
    assertion is what says the red came from git: the first two are satisfied by
    the script failing for any reason at all, a bad shebang included."""
    env = {**os.environ, "GIT_DIR": str(tmp_path / "nowhere")}
    out = _run_gate(_repo_with(tmp_path), env=env)
    assert out.returncode != 0, f"gate passed while git was broken: {out.stdout!r}"
    assert "write set in scope" not in out.stdout
    assert "not a git repository" in out.stderr, out.stderr


# ---------- what the docs say about the shell ----------
#
# Each of the skill's three edits carries at least one row no other edit
# satisfies: `does not reach inside` and `rightmost failing stage` for the
# gates.yaml paragraph, `worker bash -o pipefail -c` and
# `what passes there passes in a run` for the proving snippet, and
# `on the day the repo is clean` for the sweep sentence. A row every edit
# satisfies pins only whichever of them comes first, and the other two could be
# deleted with this test still green.

RED_STEP = "Exit 0 is half the proof. Make every gate go RED before you trust it."


@pytest.mark.parametrize("doc, phrase", [
    ("skills/loopgraph/SKILL.md", "bash -o pipefail -c"),
    ("skills/loopgraph/SKILL.md", "does not reach inside"),
    ("skills/loopgraph/SKILL.md", "set -o pipefail"),
    ("skills/loopgraph/SKILL.md", "rightmost failing stage"),
    ("skills/loopgraph/SKILL.md", "141"),
    ("skills/loopgraph/SKILL.md", "worker bash -o pipefail -c"),
    ("skills/loopgraph/SKILL.md", "what passes there passes in a run"),
    ("skills/loopgraph/SKILL.md", "on the day the repo is clean"),
    # The closing `; }` is part of the row. `|| [ $? = 1 ]` on its own is what
    # the braceless form contains, and that form detaches the rest of the
    # pipeline, so the bare row would pin the broken idiom as happily as the fix.
    ("skills/loopgraph/SKILL.md", "|| [ $? = 1 ]; }"),
    ("skills/loopgraph/SKILL.md", RED_STEP),
    ("AGENTS.md", "never a model's claim"),
    ("AGENTS.md", "`spent` and `asks`"),
    ("AGENTS.md", "pipefail"),
    ("AGENTS.md", "add_conditional_edges"),
])
def test_the_docs_say_what_the_shell_reports(doc, phrase):
    """AC-8 and AC-9. The shell decides what a pipeline's exit code is, and the
    two documents someone reads before writing a gate are the only place that
    fact reaches them: a rule that lives in `activities/gate.py` alone is a rule
    the person writing the gate never sees. Every document is compared with its
    whitespace collapsed, because each of these sentences wraps and the rule is
    the wording, not the line breaks.

    `RED_STEP` is the one row that was green before this phase too. pipefail
    fixes a lost pipeline exit code and nothing else, so a gate that is
    vacuously green for any other reason is still caught by breaking it and
    watching it go red, and that step must not be dropped as fixed."""
    text = " ".join((ROOT / doc).read_text().split())
    assert phrase in text, f"{doc} never says {phrase!r}"
