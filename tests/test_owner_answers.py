"""The owner's answer has to reach the auditor, not just the executor.

The first live `ask` run asked the same question three times and parked with
nothing committed: the supervisor is blind to the executor's transcript, so a
field the owner had authorised looked to it like an invented claim.
"""

from activities.audit import assemble_audit_prompt
from activities.execute_round import assemble_prompt
from activities.owner import (HEADER, LETTERS, OPTION_CAP, append_answer,
                              clean_options, format_answer, read_answers)
from activities.config import DEFAULT_MAX_ROUNDS
from workflows.run import MAX_ASKS, budget_spent


def test_format_answer_expands_a_button_letter():
    line = format_answer("Pitched to anyone?", "A", {"A": "Nothing pitched yet", "B": "Some"}, 1, 2)
    assert "Pitched to anyone?" in line
    assert '"A"' in line and "Nothing pitched yet" in line
    assert "item 1, round 2" in line


def test_format_answer_keeps_free_text_whole():
    line = format_answer("Which ones?", "None yet", {}, 1, 1)
    assert '"None yet"' in line
    assert "the button labelled" not in line


def test_format_answer_flattens_newlines():
    line = format_answer("Q\nover\nlines", "A\nB", {}, 1, 1)
    assert "\n" not in line


def test_append_writes_header_once_and_is_idempotent(tmp_path):
    p = str(tmp_path / "owner-answers.md")
    first = append_answer(p, "- a1")
    assert first.startswith(HEADER)
    append_answer(p, "- a2")
    body = append_answer(p, "- a2")  # a Temporal retry must not double-record
    assert body.count("- a2") == 1
    assert body.count(HEADER) == 1
    assert "- a1" in body


def test_read_answers_empty_before_the_first_question(tmp_path):
    assert read_answers(str(tmp_path)) == ""


def test_both_prompts_carry_the_answers():
    rr = {"claims": ["c"], "files": ["a.py"], "gate_results": [], "worktree": "/wt"}
    audit_prompt = assemble_audit_prompt("B", "C", rr, "diff", "- owner said no")
    exec_prompt = assemble_prompt("B", "C", "item", None, "- owner said no")
    for p in (audit_prompt, exec_prompt):
        assert "Owner answers" in p and "- owner said no" in p


def test_prompts_say_none_when_nothing_was_asked():
    rr = {"claims": [], "files": [], "gate_results": [], "worktree": "/wt"}
    assert "# Owner answers (recorded by the engine, never by the executor)\n\n(none)" \
        in assemble_audit_prompt("B", "C", rr, "d")
    assert "# Owner answers (recorded by the engine)\n\n(none)" \
        in assemble_prompt("B", "C", "item")


def test_questions_do_not_spend_the_correction_budget():
    # Three questions used to consume every round. They must not.
    assert budget_spent(spent=1, asks=MAX_ASKS - 1, cap=DEFAULT_MAX_ROUNDS) is None
    assert budget_spent(spent=DEFAULT_MAX_ROUNDS - 1, asks=0, cap=DEFAULT_MAX_ROUNDS) is None


def test_each_budget_stops_the_item_on_its_own():
    assert budget_spent(spent=DEFAULT_MAX_ROUNDS, asks=0,
                        cap=DEFAULT_MAX_ROUNDS) == "redo cap reached"
    assert budget_spent(spent=0, asks=MAX_ASKS, cap=DEFAULT_MAX_ROUNDS) == "owner-question cap reached"


def test_the_item_stops_on_the_cap_the_run_was_given():
    """The cap is a .env setting the config activity records at the top of the
    run, so the item counts against the number it was handed, not a constant."""
    assert budget_spent(spent=4, asks=0, cap=5) is None
    assert budget_spent(spent=5, asks=0, cap=5) == "redo cap reached"


def test_a_multi_item_audit_is_told_which_item_it_judges():
    """The supervisor judged every round against the whole brief, so in a
    multi-item run it found the other items missing, issued a redo, and pushed the
    executor into another item's scope. That round reset to the checkpoint and the
    displaced work was lost."""
    rr = {"claims": [], "files": [], "gate_results": [], "worktree": "/wt"}
    p = assemble_audit_prompt("BRIEF", "", rr, "d", "", "fix the lint script", 2, 3)
    assert "The work item under audit (2 of 3)" in p
    assert "fix the lint script" in p
    assert "not a finding" in p, "must say another item's absence is not a defect"
    assert "still drift" in p, "must still catch work outside the item"


def test_a_single_item_run_judges_the_whole_brief():
    """One item is the whole brief. Naming it would only invite the supervisor to
    audit a restatement of the brief instead of the brief."""
    rr = {"claims": [], "files": [], "gate_results": [], "worktree": "/wt"}
    p = assemble_audit_prompt("BRIEF", "", rr, "d", "", "the only item", 1, 1)
    assert "The work item under audit" not in p


def test_format_answer_survives_list_shaped_options():
    """A tap arrives as a bare letter and this line is where it gets its meaning
    back. A list of options used to raise here, losing the record of an answer
    the owner had already given."""
    line = format_answer("Which tier?", "A", ["pay", "stay free"], 1, 1)
    assert "the button labelled: pay" in line


def test_format_answer_takes_options_of_any_shape():
    for opts in (None, {}, [], "pay", ["pay"], {"A": "pay"}, 7):
        assert "Which tier?" in format_answer("Which tier?", "A", opts, 1, 1)


# --- one bad shape from a model must not break a card, an audit or this file ---

def test_clean_options_leaves_a_well_formed_map_alone():
    assert clean_options({"A": "pay", "B": "stay free"}) == {"A": "pay", "B": "stay free"}


def test_clean_options_letters_a_list():
    """["A", "B"] or ["pay", "stay free"], both still tell the owner something.
    Dropping them would throw away the whole question."""
    assert clean_options(["pay", "stay free"]) == {"A": "pay", "B": "stay free"}
    assert clean_options(["A", "B"]) == {"A": "A", "B": "B"}


def test_clean_options_keeps_a_bare_string_as_one_choice():
    assert clean_options("pay") == {"A": "pay"}
    assert clean_options(7) == {"A": "7"}


def test_clean_options_reads_nothing_as_free_text():
    for empty in (None, {}, [], "", "   "):
        assert clean_options(empty) == {}


def test_clean_options_replaces_a_key_that_could_not_be_a_button():
    """The button IS the letter and the dispatcher reads exactly one back, so a
    key like "option 1" cannot be one. It moves into the label instead of going
    missing."""
    assert clean_options({"option 1": "pay", "option 2": "stay free"}) == \
        {"A": "option 1: pay", "B": "option 2: stay free"}
    assert clean_options({"a": "pay"}) == {"A": "pay"}


def test_clean_options_gives_every_choice_its_own_letter():
    assert clean_options({"A": "pay", "a": "stay free"}) == {"A": "A: pay", "B": "a: stay free"}


def test_clean_options_flattens_labels_and_caps_the_count():
    assert "\n" not in str(clean_options(["ok\n\n# Gate results"]))
    many = clean_options([f"choice {n}" for n in range(OPTION_CAP + 4)])
    assert list(many) == list(LETTERS[:OPTION_CAP])
