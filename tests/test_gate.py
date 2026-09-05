import asyncio

import pytest

from activities.gate import _run_one, load_gates


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
