"""Story 2.5 — ShadowSoul binary 缺失时降级测试（agent.degraded 事件 + fallback_chain）。"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from xueban.runtime.contracts import AgentEvent, AgentHandle, AgentTask
from xueban.runtime.executors import CliAgentExecutor
from xueban.runtime.preset_loader import load_presets, resolve_preset


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task() -> AgentTask:
    return AgentTask(
        task_id="t1", run_id="r1", node_id="n1", agent_id="soul-1",
        payload={"prompt": "analyse the codebase"},
        metadata={},
    )


def _make_shadowsoul_executor() -> CliAgentExecutor:
    preset = resolve_preset("shadowsoul", {})
    return CliAgentExecutor(provider="shadowsoul", preset=preset)


# ---------------------------------------------------------------------------
# Binary missing → dispatch returns degraded handle
# ---------------------------------------------------------------------------

class TestShadowSoulDegradedDispatch:
    @pytest.mark.asyncio
    async def test_missing_binary_returns_degraded_handle(self):
        executor = _make_shadowsoul_executor()
        with patch("xueban.runtime.executors.shutil.which", return_value=None):
            handle = await executor.dispatch(_make_task())
        assert handle.status == "degraded"
        assert handle.metadata.get("_degraded") is True

    @pytest.mark.asyncio
    async def test_degraded_handle_contains_reason(self):
        executor = _make_shadowsoul_executor()
        with patch("xueban.runtime.executors.shutil.which", return_value=None):
            handle = await executor.dispatch(_make_task())
        reason = handle.metadata.get("_degraded_reason", "")
        assert "shadow" in reason.lower() or "not found" in reason.lower()

    @pytest.mark.asyncio
    async def test_degraded_handle_contains_provider(self):
        executor = _make_shadowsoul_executor()
        with patch("xueban.runtime.executors.shutil.which", return_value=None):
            handle = await executor.dispatch(_make_task())
        assert handle.metadata.get("_provider") == "shadowsoul"


# ---------------------------------------------------------------------------
# Binary missing → stream_events emits agent.degraded
# ---------------------------------------------------------------------------

class TestShadowSoulDegradedStreamEvents:
    @pytest.mark.asyncio
    async def test_degraded_handle_emits_agent_degraded_event(self):
        executor = _make_shadowsoul_executor()
        with patch("xueban.runtime.executors.shutil.which", return_value=None):
            handle = await executor.dispatch(_make_task())
        events = [e async for e in executor.stream_events(handle)]
        assert len(events) == 1
        assert events[0].type == "agent.degraded"

    @pytest.mark.asyncio
    async def test_degraded_event_has_fallback_chain(self):
        executor = _make_shadowsoul_executor()
        with patch("xueban.runtime.executors.shutil.which", return_value=None):
            handle = await executor.dispatch(_make_task())
        events = [e async for e in executor.stream_events(handle)]
        payload = events[0].payload
        assert "fallback_chain" in payload
        assert "api:claude" in payload["fallback_chain"]

    @pytest.mark.asyncio
    async def test_degraded_event_payload_has_reason(self):
        executor = _make_shadowsoul_executor()
        with patch("xueban.runtime.executors.shutil.which", return_value=None):
            handle = await executor.dispatch(_make_task())
        events = [e async for e in executor.stream_events(handle)]
        assert "reason" in events[0].payload

    @pytest.mark.asyncio
    async def test_no_degraded_when_binary_present(self):
        executor = _make_shadowsoul_executor()
        # Simulate binary found but immediately exits non-zero (no actual run)
        with patch("xueban.runtime.executors.shutil.which", return_value="/usr/bin/shadow"), \
             patch("xueban.runtime.executors.asyncio.to_thread") as mock_run:
            import subprocess as _sp
            mock_result = type("R", (), {
                "stdout": "", "stderr": "error", "returncode": 1
            })()
            mock_run.return_value = mock_result
            handle = await executor.dispatch(_make_task())
        assert handle.status != "degraded"
        assert not handle.metadata.get("_degraded")
