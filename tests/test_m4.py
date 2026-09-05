from activities.learn import append_constraint, clean_distilled


def test_append_and_rotate(tmp_path):
    p = str(tmp_path / "constraints.md")
    for i in range(5):
        append_constraint(p, f"lesson {i}", cap=3)
    kept = append_constraint(p, "lesson 5", cap=3)
    assert kept == ["- lesson 3", "- lesson 4", "- lesson 5"]


def test_append_normalizes_and_caps_line(tmp_path):
    p = str(tmp_path / "constraints.md")
    kept = append_constraint(p, "  multi\n  line   waffle " + "x" * 300)
    assert kept[0].startswith("- multi line waffle")
    assert len(kept[0]) <= 202  # 200 chars + "- "


def test_clean_distilled():
    assert clean_distilled("Always run pytest from the worktree root.") == \
        "Always run pytest from the worktree root."
    assert clean_distilled("NONE") is None
    assert clean_distilled("") is None
    assert clean_distilled("too short") is None
