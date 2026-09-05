"""The owner's answer has to reach the auditor, not just the executor.

The first live `ask` run asked the same question three times and parked with
nothing committed: the supervisor is blind to the executor's transcript, so a
field the owner had authorised looked to it like an invented claim.
"""

from activities.audit import assemble_audit_prompt
from activities.execute_round import assemble_prompt
from activities.owner import HEADER, append_answer, format_answer, read_answers
from workflows.run import MAX_ASKS, MAX_ROUNDS, budget_spent


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
    assert budget_spent(spent=1, asks=MAX_ASKS - 1) is None
    assert budget_spent(spent=MAX_ROUNDS - 1, asks=0) is None


def test_each_budget_stops_the_item_on_its_own():
    assert budget_spent(spent=MAX_ROUNDS, asks=0) == "redo cap reached"
    assert budget_spent(spent=0, asks=MAX_ASKS) == "owner-question cap reached"
