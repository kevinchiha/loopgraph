import inspect
import re

import pytest

from activities.config import (DEFAULT_CONVERGENCE, parse_deadline, parse_run_config,
                               read_run_config)

DEFAULTS = {"convergence": {"enabled": True, "every_items": 5, "net_lines": 400}, "sweep": None}

SWEEP = ("sweep:\n"
         "  yield_floor: 3\n"
         "  detectors:\n"
         "    - name: dead\n"
         "      cmd: vulture .\n")

# The heading is loose on purpose: two spaces and no capital still means a brief
# that names its own items, which a sweep may not do.
WORK_ITEMS_BRIEF = "# Brief\n\n### work  items\n\n- rip out the old client\n"
WORK_ITEMS_MESSAGE = ("run.yaml: a sweep run takes its items from the detectors; "
                      "remove the Work items section")

TOP_LEVEL_LIST = "- one\n- two\n"
BARE_OFF = "convergence:\n  off: true\n"
NOT_YAML = "sweep:\n  detectors:\n  - name: a\n   cmd: x\n"

UNKNOWN_KEYS = [
    ("yeild_floor: 3\n", "run.yaml: unknown key 'yeild_floor'"),
    ("convergence:\n  every_item: 2\n", "run.yaml: unknown key 'every_item' under convergence"),
    (SWEEP + "  deadlines: 4d\n", "run.yaml: unknown key 'deadlines' under sweep"),
    (SWEEP + "      green_exit: 0\n", "run.yaml: unknown key 'green_exit' under sweep.detectors[0]"),
]

BAD_CONVERGENCE = [
    ("convergence: 5\n", "run.yaml: convergence must be a mapping"),
    ('convergence:\n  enabled: "yes"\n', "run.yaml: convergence.enabled must be true or false"),
    ("convergence:\n  every_items: 0\n", "run.yaml: convergence.every_items must be an integer of at least 1"),
    ("convergence:\n  every_items: '5'\n", "run.yaml: convergence.every_items must be an integer of at least 1"),
    ("convergence:\n  every_items: true\n", "run.yaml: convergence.every_items must be an integer of at least 1"),
    ("convergence:\n  net_lines: 0\n", "run.yaml: convergence.net_lines must be an integer of at least 1"),
]

BAD_SWEEP = [
    ("sweep: 5\n", "run.yaml: sweep must be a mapping"),
    ("sweep:\n  yield_floor: yes\n  detectors:\n    - name: a\n      cmd: x\n",
     "run.yaml: sweep.yield_floor must be an integer of at least 0"),
    ("sweep:\n  yield_floor: 0\n  max_items: true\n  detectors:\n    - name: a\n      cmd: x\n",
     "run.yaml: sweep.max_items must be an integer of at least 1"),
    ("sweep:\n  yield_floor: 0\n  detectors:\n    - name: a\n      cmd: x\n      timeout: yes\n",
     "run.yaml: sweep.detectors[0].timeout must be an integer of at least 1"),
]

NEEDS_A_FLOOR_AND_A_DETECTOR = [
    ("sweep:\n  detectors:\n    - name: a\n      cmd: x\n",
     "run.yaml: sweep.yield_floor must be an integer of at least 0"),
    ("sweep:\n  yield_floor: 0\n", "run.yaml: sweep.detectors must be a non-empty list"),
    ("sweep:\n  yield_floor: 0\n  detectors: []\n", "run.yaml: sweep.detectors must be a non-empty list"),
    ("sweep:\n  yield_floor: 0\n  detectors:\n    - name: a\n", "run.yaml: sweep.detectors[0] needs name and cmd"),
]

BAD_DEADLINE = ("sweep:\n  yield_floor: 0\n  deadline: 4w\n  detectors:\n    - name: a\n      cmd: x\n",
                "run.yaml: sweep.deadline must look like 30m, 4h or 4d")


def refused(text, message, brief=""):
    with pytest.raises(ValueError) as err:
        parse_run_config(text, brief)
    assert str(err.value) == message


# --- the shape a run directory without a run.yaml still gets ---

def test_no_file_and_an_empty_file_give_the_defaults(tmp_path):
    assert read_run_config(str(tmp_path)) == DEFAULTS  # no run.yaml at all
    for text in ("", "\n", "# nothing but a comment\n", "convergence:\nsweep:\n"):
        assert parse_run_config(text) == DEFAULTS, text
    # Handing back the module constant itself would let one run's counters edit
    # the defaults every later run reads.
    assert parse_run_config("")["convergence"] is not DEFAULT_CONVERGENCE


def test_a_partial_convergence_block_takes_defaults_for_the_rest():
    assert parse_run_config("convergence:\n  every_items: 2\n")["convergence"] == {
        "enabled": True, "every_items": 2, "net_lines": 400}
    assert parse_run_config("convergence:\n  enabled: false\n")["convergence"] == {
        "enabled": False, "every_items": 5, "net_lines": 400}


# --- a knob the engine does not know is an error, not a default ---

@pytest.mark.parametrize("text,message", UNKNOWN_KEYS)
def test_an_unknown_key_is_named_at_every_level(text, message):
    refused(text, message)


@pytest.mark.parametrize("text,message", BAD_CONVERGENCE)
def test_bad_convergence_values_are_refused(text, message):
    refused(text, message)


@pytest.mark.parametrize("text,message", BAD_SWEEP)
def test_bad_sweep_values_are_refused(text, message):
    """A bare `yes` is a YAML boolean and a bool is an int, so an isinstance check
    alone would pass `timeout: yes` to the detector poll as 1 second."""
    refused(text, message)


def test_a_top_level_list_is_refused():
    refused(TOP_LEVEL_LIST, "run.yaml: expected a mapping at the top level")


def test_a_bare_off_key_is_named_as_a_boolean():
    """Under the unknown-key rule alone this reads as `unknown key 'False'`, which
    names a key the owner never wrote."""
    refused(BARE_OFF, "run.yaml: key False under convergence is not a name "
                      "(YAML reads bare off/on/yes/no as booleans)")
    refused("on: true\n", "run.yaml: key True under the top level is not a name "
                          "(YAML reads bare off/on/yes/no as booleans)")


def test_a_file_that_is_not_yaml_is_refused_with_the_prefix():
    """A mis-indented file is the likeliest way a hand-written config goes wrong.
    Without the prefix the owner's note would begin `why: ActivityError:`."""
    with pytest.raises(ValueError) as err:
        parse_run_config(NOT_YAML)
    message = str(err.value)
    assert message.startswith("run.yaml: not valid YAML: ")
    assert "\n" not in message
    assert len(message) > len("run.yaml: not valid YAML: ")


# --- sweeps ---

def test_a_sweep_block_parses_with_its_defaults():
    cfg = parse_run_config(SWEEP)
    assert set(cfg) == {"convergence", "sweep"}
    assert cfg["convergence"] == DEFAULT_CONVERGENCE
    assert cfg["sweep"] == {
        "yield_floor": 3, "deadline_seconds": None, "max_items": 40,
        "detectors": [{"name": "dead", "cmd": "vulture .", "timeout": 600}]}


def test_deadline_units():
    assert parse_deadline("30m") == 1800
    assert parse_deadline("4h") == 14400
    assert parse_deadline("4d") == 345600
    for bad in ("4", "4 days", "4w", "-4d", "d"):
        with pytest.raises(ValueError):
            parse_deadline(bad)
    refused(*BAD_DEADLINE)
    sweep = parse_run_config(SWEEP.replace("  yield_floor: 3\n", "  yield_floor: 3\n  deadline: 4h\n"))["sweep"]
    assert sweep["deadline_seconds"] == 14400


@pytest.mark.parametrize("text,message", NEEDS_A_FLOOR_AND_A_DETECTOR)
def test_a_sweep_needs_a_floor_and_at_least_one_detector(text, message):
    refused(text, message)


def test_a_sweep_brief_with_work_items_is_refused():
    refused(SWEEP, WORK_ITEMS_MESSAGE, brief=WORK_ITEMS_BRIEF)


def test_a_brief_run_may_keep_its_work_items():
    assert parse_run_config("", WORK_ITEMS_BRIEF) == DEFAULTS
    assert parse_run_config("convergence:\n  every_items: 2\n", WORK_ITEMS_BRIEF)["sweep"] is None


def test_every_error_starts_with_run_yaml():
    """Task 6 puts this message straight into the owner's note and the ledger's
    stop reason, so the prefix is what tells them which file to go and fix."""
    fixtures = [(text, "") for text, _ in
                UNKNOWN_KEYS + BAD_CONVERGENCE + BAD_SWEEP + NEEDS_A_FLOOR_AND_A_DETECTOR]
    fixtures += [(TOP_LEVEL_LIST, ""), (BARE_OFF, ""), (NOT_YAML, ""), (BAD_DEADLINE[0], ""),
                 (SWEEP, WORK_ITEMS_BRIEF)]
    for text, brief in fixtures:
        with pytest.raises(ValueError) as err:
            parse_run_config(text, brief)
        assert str(err.value).startswith("run.yaml: "), text


def test_read_run_config_reads_both_files_from_the_run_dir(tmp_path):
    (tmp_path / "run.yaml").write_text(SWEEP)
    assert read_run_config(str(tmp_path))["sweep"]["yield_floor"] == 3  # no brief.md yet
    (tmp_path / "brief.md").write_text("# Brief\n\nRemove what nothing calls.\n")
    assert read_run_config(str(tmp_path))["sweep"]["detectors"][0]["cmd"] == "vulture ."
    (tmp_path / "brief.md").write_text(WORK_ITEMS_BRIEF)
    with pytest.raises(ValueError) as err:
        read_run_config(str(tmp_path))
    assert str(err.value) == WORK_ITEMS_MESSAGE


def test_load_run_config_is_registered():
    import worker
    listed = re.search(r"activities=\[(.*?)\]", inspect.getsource(worker.main), re.S)
    names = [n.strip() for n in listed.group(1).replace("\n", " ").split(",")]
    assert "load_run_config" in names, names
