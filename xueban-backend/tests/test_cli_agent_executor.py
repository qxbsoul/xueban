"""Story 2.2 — CliAgentExecutor 测试（mock subprocess）。"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xueban.runtime.contracts import (
    AgentCapabilities,
    AgentEvent,
    AgentHandle,
    AgentTask,
    ProviderPreset,
)
from xueban.runtime.executors import CliAgentExecutor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_preset(
    parse_format: str = "stdout-text",
    stdin_format: str = "raw",
    command: list | None = None,
    args_template: list | None = None,
    workspace_template: str | None = None,
) -> ProviderPreset:
    return ProviderPreset(
        command=command or ["echo"],
        args_template=args_template or [],
        stdin_format=stdin_format,
        parse_format=parse_format,
        workspace_template=workspace_template,
        env={},
    )


def _task(agent_id: str = "agent-1", run_id: str = "run-1") -> AgentTask:
    return AgentTask(run_id=run_id, node_id="node-1", agent_id=agent_id, payload={"msg": "hello"})


def _mock_completed(stdout: str = "", returncode: int = 0) -> MagicMock:
    result = MagicMock()
    result.stdout = stdout
    result.stderr = ""
    result.returncode = returncode
    return result


# ---------------------------------------------------------------------------
# dispatch() — sunny path
# ---------------------------------------------------------------------------

class TestCliAgentExecutorDispatch:
    @pytest.mark.asyncio
    async def test_dispatch_returns_agent_handle(self):
        preset = _make_preset()
        exc = CliAgentExecutor("echo", preset)
        with patch("asyncio.to_thread", return_value=_mock_completed("result")):
            handle = await exc.dispatch(_task())
        assert isinstance(handle, AgentHandle)
        assert handle.run_id == "run-1"

    @pytest.mark.asyncio
    async def test_dispatch_status_done_on_success(self):
        preset = _make_preset()
        exc = CliAgentExecutor("test-provider", preset)
        with patch("asyncio.to_thread", return_value=_mock_completed("ok", returncode=0)):
            handle = await exc.dispatch(_task())
        assert handle.status == "done"

    @pytest.mark.asyncio
    async def test_dispatch_status_failed_on_nonzero(self):
        preset = _make_preset()
        exc = CliAgentExecutor("test-provider", preset)
        with patch("asyncio.to_thread", return_value=_mock_completed("err", returncode=1)):
            handle = await exc.dispatch(_task())
        assert handle.status == "failed"

    @pytest.mark.asyncio
    async def test_dispatch_stores_parse_format_in_metadata(self):
        preset = _make_preset(parse_format="jsonl-tail")
        exc = CliAgentExecutor("openclaw", preset)
        with patch("asyncio.to_thread", return_value=_mock_completed("")):
            handle = await exc.dispatch(_task())
        assert handle.metadata["parse_format"] == "jsonl-tail"

    @pytest.mark.asyncio
    async def test_dispatch_command_not_found_returns_degraded_handle(self):
        """Story 2.5: missing binary → degraded handle, not ValueError."""
        preset = _make_preset(command=["nonexistent_binary_12345"])
        exc = CliAgentExecutor("x", preset)
        # shutil.which returns None for any binary not in PATH
        with patch("xueban.runtime.executors.shutil.which", return_value=None):
            handle = await exc.dispatch(_task())
        assert handle.status == "degraded"
        assert handle.metadata.get("_degraded") is True


# ---------------------------------------------------------------------------
# stream_events() — stdout-text
# ---------------------------------------------------------------------------

class TestStreamEventsStdoutText:
    @pytest.mark.asyncio
    async def test_streams_agent_output_event(self):
        preset = _make_preset(parse_format="stdout-text")
        exc = CliAgentExecutor("test", preset)
        handle = AgentHandle(
            run_id="r1", node_id="n1", agent_id="a1", status="done",
            metadata={"stdout": "hello world", "stderr": "", "returncode": 0, "parse_format": "stdout-text"},
        )
        events = [e async for e in exc.stream_events(handle)]
        assert len(events) == 1
        assert events[0].type == "agent.output"
        assert events[0].payload["text"] == "hello world"

    @pytest.mark.asyncio
    async def test_failed_handle_emits_agent_failed(self):
        preset = _make_preset()
        exc = CliAgentExecutor("test", preset)
        handle = AgentHandle(
            run_id="r1", node_id="n1", agent_id="a1", status="failed",
            metadata={"stdout": "", "stderr": "boom", "returncode": 1, "parse_format": "stdout-text"},
        )
        events = [e async for e in exc.stream_events(handle)]
        assert len(events) == 1
        assert events[0].type == "agent.failed"


# ---------------------------------------------------------------------------
# stream_events() — jsonl-tail (OpenClaw)
# ---------------------------------------------------------------------------

class TestStreamEventsJsonlTail:
    @pytest.mark.asyncio
    async def test_parses_jsonl_lines(self):
        lines = [
            json.dumps({"type": "assistant", "content": "hi"}),
            json.dumps({"type": "done", "summary": "complete"}),
        ]
        preset = _make_preset(parse_format="jsonl-tail")
        exc = CliAgentExecutor("openclaw", preset)
        handle = AgentHandle(
            run_id="r1", node_id="n1", agent_id="a1", status="done",
            metadata={"stdout": "\n".join(lines), "stderr": "", "returncode": 0, "parse_format": "jsonl-tail"},
        )
        events = [e async for e in exc.stream_events(handle)]
        assert len(events) == 2
        assert events[0].type == "agent.output"
        assert events[1].type == "agent.completed"

    @pytest.mark.asyncio
    async def test_invalid_json_line_emits_output_event(self):
        preset = _make_preset(parse_format="jsonl-tail")
        exc = CliAgentExecutor("openclaw", preset)
        handle = AgentHandle(
            run_id="r1", node_id="n1", agent_id="a1", status="done",
            metadata={"stdout": "not valid json", "stderr": "", "returncode": 0, "parse_format": "jsonl-tail"},
        )
        events = [e async for e in exc.stream_events(handle)]
        assert len(events) == 1
        assert events[0].type == "agent.output"

    @pytest.mark.asyncio
    async def test_empty_stdout_no_events(self):
        preset = _make_preset(parse_format="jsonl-tail")
        exc = CliAgentExecutor("openclaw", preset)
        handle = AgentHandle(
            run_id="r1", node_id="n1", agent_id="a1", status="done",
            metadata={"stdout": "", "stderr": "", "returncode": 0, "parse_format": "jsonl-tail"},
        )
        events = [e async for e in exc.stream_events(handle)]
        assert events == []


# ---------------------------------------------------------------------------
# capabilities()
# ---------------------------------------------------------------------------

class TestCapabilities:
    def test_jsonl_tail_streaming_is_true(self):
        preset = _make_preset(parse_format="jsonl-tail")
        exc = CliAgentExecutor("openclaw", preset)
        caps = exc.capabilities()
        assert caps.streaming is True

    def test_stdout_text_streaming_is_false(self):
        preset = _make_preset(parse_format="stdout-text")
        exc = CliAgentExecutor("echo", preset)
        caps = exc.capabilities()
        assert caps.streaming is False
