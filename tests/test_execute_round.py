import pytest

from activities.execute_round import assemble_prompt, parse_final_json, parse_porcelain


def test_assemble_prompt_has_all_parts():
    p = assemble_prompt("BRIEF", "C1\nC2", "ITEM")
    assert "BRIEF" in p and "C1\nC2" in p and "ITEM" in p
    assert "Output contract" in p  # executor contract rides along


def test_assemble_prompt_empty_constraints():
    assert "(none yet)" in assemble_prompt("B", "", "I")


def test_parse_final_json_last_block_wins():
    text = 'noise ```json\n{"a": 1}\n``` more ```json\n{"claims": ["x"], "summary": "s"}\n```'
    assert parse_final_json(text)["claims"] == ["x"]


def test_parse_final_json_missing_block():
    with pytest.raises(ValueError):
        parse_final_json("no fences here")


def test_parse_final_json_bad_json():
    with pytest.raises(ValueError):
        parse_final_json("```json\n{not json}\n```")


def test_parse_porcelain():
    assert parse_porcelain(" M cli.py\nA  new.py\n?? untracked.py\n") == ["cli.py", "new.py", "untracked.py"]


def test_parse_porcelain_rename():
    assert parse_porcelain('R  old.py -> new.py\n') == ["new.py"]
