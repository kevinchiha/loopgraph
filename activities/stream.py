"""Shared Claude streaming: collect the final text AND append a readable live log.

Every executor/supervisor call writes runs/<slug>/logs/i<item>-r<round>-<role>.log
as it streams, so `lg tail runs/<slug>` shows what the nodes are doing right now —
the thing watching Claude Code gave for free. Logs are bounded: head-truncated
past LOG_CAP, so a long run cannot grow a log without limit.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from temporalio import activity

LOG_CAP = 1_000_000  # bytes per log file

# One source of truth for a run's log filenames. Three places used to hardcode the
# shape: the writers here, `lg tail`'s glob, and the dashboard's regex. When the
# item number joined the name, both readers silently stopped matching and nobody
# could watch a run at all. Anything that reads these files uses these.
LOG_GLOB = "*.log"
LOG_RE = r"^(?:i(\d+)-)?r(\d+)-(executor|audit)\.log$"


def log_name(item_no: int, round_no: int, role: str) -> str:
    """e.g. i2-r1-audit.log"""
    return f"i{item_no}-r{round_no}-{role}.log"


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def summarize_tool(name: str, inp: dict) -> str:
    if name in ("Edit", "Write", "Read", "NotebookEdit"):
        return str(inp.get("file_path", ""))
    if name == "Bash":
        return str(inp.get("command", ""))[:120]
    if name in ("Glob", "Grep"):
        return str(inp.get("pattern", ""))[:120]
    return str(inp)[:120]


def append_log(path: str, line: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(line + "\n")
    if p.stat().st_size > LOG_CAP:
        p.write_bytes(b"[... head truncated ...]\n" + p.read_bytes()[-LOG_CAP // 2:])


async def stream_query(prompt: str, options, log_path: str) -> str:
    """Run query(), logging assistant text/tool calls as they stream. Returns all text."""
    from claude_agent_sdk import (AssistantMessage, TextBlock, ToolResultBlock,
                                  ToolUseBlock, query)

    chunks: list[str] = []
    async for msg in query(prompt=prompt, options=options):
        activity.heartbeat("claude streaming")
        content = getattr(msg, "content", None)
        if not content:
            continue
        for b in content:
            if isinstance(b, TextBlock):
                chunks.append(b.text)
                for line in b.text.splitlines():
                    append_log(log_path, f"[{_ts()} assistant] {line}")
            elif isinstance(b, ToolUseBlock):
                append_log(log_path, f"[{_ts()} tool:{b.name}] {summarize_tool(b.name, b.input)}")
            elif isinstance(b, ToolResultBlock):
                append_log(log_path, f"[{_ts()} result] {str(b.content)[:200]}")
    return "\n".join(chunks)
