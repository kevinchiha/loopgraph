import inspect

from activities.audit import assemble_audit_prompt, audit, parse_verdict


def pkt(v):
    return f'```json\n{{"verdict": "{v}", "reasons": ["r"], "directive": {{}}}}\n```'


def test_parse_verdict_all_valid():
    for v in ("accept", "redo", "plan", "stop"):
        assert parse_verdict(pkt(v))["verdict"] == v


def test_parse_verdict_unknown_is_redo():
    r = parse_verdict(pkt("shipit"))
    assert r["verdict"] == "redo" and r["parse_ok"] is False


def test_parse_verdict_garbage_is_redo():
    r = parse_verdict("no fences, just vibes")
    assert r["verdict"] == "redo" and r["parse_ok"] is False


def test_parse_verdict_missing_key_is_redo():
    r = parse_verdict('```json\n{"reasons": []}\n```')
    assert r["verdict"] == "redo"


def test_assemble_audit_prompt_carries_evidence():
    rr = {"claims": ["c1"], "files": ["a.py"],
          "gate_results": [{"name": "tests", "status": "green", "exit_code": 0}],
          "worktree": "/wt"}
    p = assemble_audit_prompt("BRIEF", "CONS", rr, "diff --git a/a.py")
    for needle in ("BRIEF", "CONS", "- c1", "- a.py", "tests: green", "diff --git a/a.py", "/wt"):
        assert needle in p


def _rr():
    return {"claims": [], "files": [], "gate_results": [], "worktree": "/wt"}


def test_a_sweep_item_reaches_the_auditor_when_it_is_item_1_of_1():
    """The scope block was gated on item_total > 1, which the first sweep item
    never satisfies: it reached the auditor with no item text at all and was
    judged against the whole brief."""
    p = assemble_audit_prompt("BRIEF", "", _rr(), "d", "", "drop the dead flag", 1, 1, "sweep")
    assert "# The work item under audit (item 1, sweep)" in p
    assert "drop the dead flag" in p
    assert "Judge THIS item only" in p


def test_a_convergence_item_gets_its_own_heading():
    p = assemble_audit_prompt("BRIEF", "", _rr(), "d", "", "cut the duplication", 1, 1,
                              "convergence")
    assert "# The work item under audit (item 1, convergence)" in p
    assert "cut the duplication" in p


def test_a_generated_item_past_item_1_gets_one_block_and_no_total():
    """From sweep item 2 on, item_total == item_no, so the item_total branch left
    in play would emit a second block, or head one "of 2" about a run that has no
    total to count towards."""
    for item_no, kind in ((2, "sweep"), (3, "convergence")):
        p = assemble_audit_prompt("BRIEF", "", _rr(), "d", "", "an item", item_no, item_no, kind)
        headings = [l for l in p.splitlines() if l.startswith("# The work item under audit")]
        assert headings == [f"# The work item under audit (item {item_no}, {kind})"]
        assert f"of {item_no}" not in p
        assert "The brief lists others" not in p, "a generated item has no brief list"


def test_a_brief_item_prompt_is_unchanged():
    """kind is appended, so a brief item's prompt is byte-for-byte what it was."""
    for total in (1, 3):
        args = ("BRIEF", "CONS", _rr(), "d", "answers", "the item", 1, total)
        assert assemble_audit_prompt(*args) == assemble_audit_prompt(*args, "brief")
    assert "The work item under audit" not in assemble_audit_prompt(
        "BRIEF", "CONS", _rr(), "d", "", "the only item", 1, 1, "brief")


def test_kind_is_the_last_parameter_of_audit_and_the_prompt():
    """The workflow calls audit positionally; an argument put anywhere but last
    shifts what the auditor is told about the item it is judging."""
    assert list(inspect.signature(audit).parameters) == [
        "run_dir", "round_result", "round_no", "item_no", "work_item", "item_total", "kind"]
    assert list(inspect.signature(assemble_audit_prompt).parameters)[-1] == "kind"


def test_the_audit_activity_hands_the_kind_to_the_prompt():
    """AC-36. Nothing else notices if that argument is dropped: `kind` defaults to
    "brief" at both ends, so every sweep and convergence item would be audited as
    a brief item with the whole suite still green."""
    src = " ".join(inspect.getsource(audit).split())
    assert ("assemble_audit_prompt(brief, constraints, round_result, diff, "
            "read_answers(run_dir), work_item, item_no, item_total, kind)") in src


def _section(prompt: str, heading: str) -> list[str]:
    """The lines of one `## ` section of a contract, heading included."""
    lines = prompt.splitlines()
    body = lines[lines.index(heading) + 1:]
    end = next((i for i, line in enumerate(body) if line.startswith("## ")), len(body))
    section = [heading, *body[:end]]
    while not section[-1].strip():
        section.pop()
    return section


def test_the_supervisor_contract_explains_generated_items():
    """The scope block names the kind, but nothing tells the auditor what that
    kind is worth: that an empty diff can be an accept, that a sweep which
    removed nothing is not, and that net lines is code's job and not its own.
    Short on purpose — a contract nobody finishes is a contract nobody follows."""
    p = assemble_audit_prompt("BRIEF", "", _rr(), "d")
    assert "## Convergence and sweep items" in p
    section = _section(p, "## Convergence and sweep items")
    assert len(section) < 20, "\n".join(section)
    body = "\n".join(section)
    assert "convergence item" in body and "sweep item" in body
