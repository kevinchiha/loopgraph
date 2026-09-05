"""Guards for the public release: nothing personal ships, and a run can be
answered without Telegram."""

from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Everything under runs/ is gitignored except these. A run you create must never
# become a tracked file by accident — see .gitignore.
EXAMPLE_RUNS = {"m1-demo", "m2-toy", "m3-accept", "m3-planted-lie", "m4-cards",
                "example-hello"}

SECRET_SHAPES = [
    re.compile(rb"[0-9]{8,12}:[A-Za-z0-9_-]{30,}"),      # telegram bot token
    re.compile(rb"sk-[A-Za-z0-9-]{20,}"),                 # anthropic / openai key
    re.compile(rb"discord(?:app)?\.com/api/webhooks/"),   # webhook url
]
# /home/worker is the container user; /home/you is the placeholder in .env.example.
PERSONAL_PATH = re.compile(rb"/home/(?!worker\b|you\b)[a-z][a-z0-9_-]*")


def tracked() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
    return [ROOT / p for p in out.stdout.decode().split("\0") if p]


@pytest.mark.parametrize("shape", SECRET_SHAPES, ids=["telegram", "api-key", "webhook"])
def test_no_credentials_in_tracked_files(shape):
    hits = [f.relative_to(ROOT) for f in tracked() if shape.search(f.read_bytes())]
    assert not hits, f"credential shape in tracked files: {hits}"


def test_no_personal_paths_in_tracked_files():
    hits = [f.relative_to(ROOT) for f in tracked() if PERSONAL_PATH.search(f.read_bytes())]
    assert not hits, f"someone's home directory is hardcoded in: {hits}"


def test_only_example_runs_are_tracked():
    """Real runs hold the owner's work, sometimes a client's. They stay local."""
    slugs = {f.relative_to(ROOT).parts[1] for f in tracked()
             if f.relative_to(ROOT).parts[0] == "runs"}
    assert slugs <= EXAMPLE_RUNS, f"non-example runs are tracked: {sorted(slugs - EXAMPLE_RUNS)}"


def test_no_run_logs_or_worktrees_tracked():
    parts = {f.relative_to(ROOT).parts for f in tracked()}
    bad = [p for p in parts if p[0] == "runs" and len(p) > 2 and p[2] in ("logs", "worktrees", "target")]
    assert not bad, f"run output is tracked: {bad}"


# ---------- Telegram is optional ----------

def test_telegram_configured_needs_both_values(monkeypatch):
    from activities import notify
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert notify.configured() is False
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    assert notify.configured() is False, "a token with no chat id is not configured"
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    assert notify.configured() is True


def test_worker_refuses_to_start_when_telegram_is_required_but_missing(monkeypatch):
    import worker
    monkeypatch.setenv("LOOPGRAPH_REQUIRE_TELEGRAM", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with pytest.raises(SystemExit) as e:
        worker.check_telegram()
    assert "lg approve" in str(e.value)


def test_worker_starts_when_telegram_is_not_required(monkeypatch):
    import worker
    monkeypatch.setenv("LOOPGRAPH_REQUIRE_TELEGRAM", "0")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    worker.check_telegram()  # no raise


# ---------- the decision queue ----------

def _run():
    from workflows.run import LoopGraphRun
    return LoopGraphRun()


def test_signal_queues_answers_and_letters_are_matched_case_insensitively():
    wf = _run()
    wf.decide("a")
    assert wf._peek({"A", "B", "C"}) == 0


def test_merge_card_ignores_an_answer_that_is_not_one_of_its_letters():
    """A stray `lg approve <id> yes` must never be read as 'merge it'."""
    wf = _run()
    wf.decide("yes please")
    assert wf._peek({"A", "B", "C"}) is None
    wf.decide("B")
    assert wf._peek({"A", "B", "C"}) == 1, "the valid answer is found past the invalid one"


def test_question_card_takes_free_text():
    wf = _run()
    wf.decide("use the second approach")
    assert wf._peek(None) == 0


def test_blank_answers_are_ignored():
    wf = _run()
    wf.decide("   ")
    assert wf._peek(None) is None


# ---------- lg where ----------

def _lg():
    """`lg` has no .py extension, so it needs loading by path."""
    loader = SourceFileLoader("lg_cli", str(ROOT / "lg"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("lg_cli", loader))
    loader.exec_module(mod)
    return mod


def test_lg_where_reads_the_env_file(tmp_path, capsys, monkeypatch):
    import asyncio
    import json
    lg = _lg()
    env = tmp_path / ".env"
    env.write_text("LOOPGRAPH_PROJECTS_DIR=/somewhere/projects\n"
                   "LOOPGRAPH_DOCKER=sudo docker\n"
                   "LOOPGRAPH_TELEGRAM_BOT=@ExampleBot\n")
    monkeypatch.setattr(lg, "ROOT", str(tmp_path))
    asyncio.run(lg.cmd_where(None))
    out = json.loads(capsys.readouterr().out)
    assert out["projects_dir"] == "/somewhere/projects"
    assert out["docker"] == "sudo docker"
    assert out["telegram"]["bot"] == "@ExampleBot"
    assert "lg approve" in out["answer_a_run"]


def test_lg_where_says_what_to_do_before_install(tmp_path, capsys, monkeypatch):
    import asyncio
    import json
    lg = _lg()
    monkeypatch.setattr(lg, "ROOT", str(tmp_path))  # no .env yet
    asyncio.run(lg.cmd_where(None))
    out = json.loads(capsys.readouterr().out)
    assert "install.sh" in out["projects_dir"]
    assert out["telegram"]["configured"] is False
