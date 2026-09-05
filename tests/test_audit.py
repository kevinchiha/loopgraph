from activities.audit import assemble_audit_prompt, parse_verdict


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
