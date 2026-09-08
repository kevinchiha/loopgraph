import asyncio
import sys
from types import ModuleType, SimpleNamespace

import activities.stream as stream


def test_stream_heartbeats_while_model_is_silent(monkeypatch, tmp_path):
    class TextBlock:
        def __init__(self, text):
            self.text = text

    class ToolUseBlock:
        pass

    class ToolResultBlock:
        pass

    async def query(**_kwargs):
        await asyncio.sleep(0.025)
        yield SimpleNamespace(content=[TextBlock("finished")])

    sdk = ModuleType("claude_agent_sdk")
    sdk.AssistantMessage = object
    sdk.TextBlock = TextBlock
    sdk.ToolUseBlock = ToolUseBlock
    sdk.ToolResultBlock = ToolResultBlock
    sdk.query = query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)

    heartbeats = []
    monkeypatch.setattr(stream.activity, "heartbeat", heartbeats.append)
    monkeypatch.setattr(stream, "STREAM_HEARTBEAT_SECONDS", 0.005)

    result = asyncio.run(stream.stream_query("prompt", object(), str(tmp_path / "run.log")))

    assert result == "finished"
    assert heartbeats.count("claude waiting") >= 2
    assert heartbeats[-1] == "claude streaming"


def test_stream_stops_periodic_heartbeat_after_query(monkeypatch, tmp_path):
    class TextBlock:
        def __init__(self, text):
            self.text = text

    async def query(**_kwargs):
        yield SimpleNamespace(content=[TextBlock("done")])

    sdk = ModuleType("claude_agent_sdk")
    sdk.AssistantMessage = object
    sdk.TextBlock = TextBlock
    sdk.ToolUseBlock = type("ToolUseBlock", (), {})
    sdk.ToolResultBlock = type("ToolResultBlock", (), {})
    sdk.query = query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)

    heartbeats = []
    monkeypatch.setattr(stream.activity, "heartbeat", heartbeats.append)
    monkeypatch.setattr(stream, "STREAM_HEARTBEAT_SECONDS", 0.001)

    async def run_and_wait():
        result = await stream.stream_query("prompt", object(), str(tmp_path / "run.log"))
        count = len(heartbeats)
        await asyncio.sleep(0.01)
        return result, count

    result, count = asyncio.run(run_and_wait())

    assert result == "done"
    assert len(heartbeats) == count
