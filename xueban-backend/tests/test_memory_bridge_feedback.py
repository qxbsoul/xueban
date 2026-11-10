"""Tests for ExternalMemoryBridge.get_feedback() — Story 2.9 AC3.

Covers:
  - 单轮缓存: pour() → 下一轮 get_feedback() 取出并清空
  - 二次取用返回 None（取用即清）
  - 无 pour 时 get_feedback() 返回 None
  - feedback 中 accepted/rejected/deferred 字段正确
  - HermesGateway.build_session_prompt 注入 memory_feedback 字段
  - HermesGateway: 无 pour 时 xueban_envelope 不出现
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest

from xueban.runtime.memory_bridge.bridge import ExternalMemoryBridge
from xueban.runtime.memory_bridge.river_stub import InMemoryRiverStub
from xueban.runtime.memory_bridge.types import MemoryFeedback
from xueban.runtime.gateway.hermes import HermesGateway


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bridge(
    river=None,
    mode: str = "two_way",
    sse_emitter=None,
) -> ExternalMemoryBridge:
    if river is None:
        river = InMemoryRiverStub()
    card = {"memory_bridge": {"mode": mode, "drink_from": [], "pour_targets": ["alluvium"]}}
    loader = lambda _: card  # noqa: E731
    return ExternalMemoryBridge(
        river=river,
        agent_card_loader=loader,
        sse_emitter=sse_emitter,
        trajectory_path=Path(tempfile.mktemp(suffix=".jsonl")),
    )


def _candidates(n: int = 1) -> List[Dict[str, Any]]:
    return [
        {"content": f"memory {i}", "confidence": 0.8, "target_layer": "alluvium"}
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Tests: single-round cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feedback_available_after_pour():
    """get_feedback() returns MemoryFeedback after pour() in the same session."""
    mock_river = AsyncMock()
    mock_river.pour = AsyncMock(return_value="accepted")

    bridge = _make_bridge(river=mock_river)
    await bridge.pour(_candidates(2), "agent-1", "sess-1")

    feedback = bridge.get_feedback("sess-1")
    assert feedback is not None
    assert isinstance(feedback, MemoryFeedback)
    assert len(feedback.accepted) == 2


@pytest.mark.asyncio
async def test_feedback_cleared_after_first_read():
    """get_feedback() clears cache; second call returns None."""
    mock_river = AsyncMock()
    mock_river.pour = AsyncMock(return_value="accepted")

    bridge = _make_bridge(river=mock_river)
    await bridge.pour(_candidates(1), "agent-1", "sess-1")

    first = bridge.get_feedback("sess-1")
    second = bridge.get_feedback("sess-1")

    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_no_pour_means_no_feedback():
    """Without prior pour(), get_feedback() returns None."""
    bridge = _make_bridge()
    assert bridge.get_feedback("sess-never-poured") is None


@pytest.mark.asyncio
async def test_feedback_contains_correct_buckets():
    """Feedback accepted/rejected/deferred match the pour result."""
    responses = ["accepted", "rejected", "deferred"]
    call_idx = 0

    async def mock_pour(candidate, source_agent_id):
        nonlocal call_idx
        r = responses[call_idx]
        call_idx += 1
        return r

    from unittest.mock import MagicMock

    mock_river = MagicMock()
    mock_river.pour = mock_pour

    bridge = _make_bridge(river=mock_river)
    await bridge.pour(_candidates(3), "agent-1", "sess-1")

    feedback = bridge.get_feedback("sess-1")
    assert feedback is not None
    assert len(feedback.accepted) == 1
    assert len(feedback.rejected) == 1
    assert len(feedback.deferred) == 1


@pytest.mark.asyncio
async def test_feedback_session_isolation():
    """Feedback is per-session; different session IDs don't interfere."""
    mock_river = AsyncMock()
    mock_river.pour = AsyncMock(return_value="accepted")

    bridge = _make_bridge(river=mock_river)
    await bridge.pour(_candidates(1), "agent-1", "sess-A")
    await bridge.pour(_candidates(2), "agent-1", "sess-B")

    fb_a = bridge.get_feedback("sess-A")
    fb_b = bridge.get_feedback("sess-B")

    assert fb_a is not None
    assert fb_b is not None
    assert len(fb_a.accepted) == 1
    assert len(fb_b.accepted) == 2


# ---------------------------------------------------------------------------
# Tests: HermesGateway integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_build_prompt_injects_feedback():
    """HermesGateway.build_session_prompt injects feedback when pour happened."""
    mock_river = AsyncMock()
    mock_river.drink = AsyncMock(return_value=[])
    mock_river.pour = AsyncMock(return_value="accepted")

    bridge = _make_bridge(river=mock_river)
    gateway = HermesGateway(bridge=bridge)

    # Simulate pour happening in a previous turn
    await bridge.pour(_candidates(1), "agent-1", "sess-1")

    # Now build the next prompt — feedback should be injected
    payload = await gateway.build_session_prompt("summarize", "sess-1", "agent-1")

    assert "xueban_envelope" in payload
    envelope = payload["xueban_envelope"]
    assert "memory_feedback" in envelope
    fb = envelope["memory_feedback"]
    assert len(fb["accepted"]) == 1


@pytest.mark.asyncio
async def test_gateway_build_prompt_no_feedback_when_no_pour():
    """build_session_prompt without prior pour should NOT include xueban_envelope."""
    mock_river = AsyncMock()
    mock_river.drink = AsyncMock(return_value=[])

    bridge = _make_bridge(river=mock_river)
    gateway = HermesGateway(bridge=bridge)

    payload = await gateway.build_session_prompt("query", "sess-no-pour", "agent-1")

    # No prior pour → no feedback envelope
    assert "xueban_envelope" not in payload


@pytest.mark.asyncio
async def test_gateway_feedback_injected_only_once():
    """After feedback is injected once, the next prompt call should NOT include it."""
    mock_river = AsyncMock()
    mock_river.drink = AsyncMock(return_value=[])
    mock_river.pour = AsyncMock(return_value="accepted")

    bridge = _make_bridge(river=mock_river)
    gateway = HermesGateway(bridge=bridge)

    await bridge.pour(_candidates(1), "agent-1", "sess-1")

    # First prompt: feedback injected
    first_payload = await gateway.build_session_prompt("q1", "sess-1", "agent-1")
    assert "xueban_envelope" in first_payload

    # Second prompt: feedback cleared
    second_payload = await gateway.build_session_prompt("q2", "sess-1", "agent-1")
    assert "xueban_envelope" not in second_payload


@pytest.mark.asyncio
async def test_feedback_emits_sse_event():
    """get_feedback() should emit agent.memory_feedback SSE event."""
    mock_river = AsyncMock()
    mock_river.pour = AsyncMock(return_value="accepted")

    emitted = []
    bridge = _make_bridge(
        river=mock_river,
        sse_emitter=lambda t, p: emitted.append((t, p)),
    )
    await bridge.pour(_candidates(1), "agent-1", "sess-1")
    bridge.get_feedback("sess-1")

    assert any(t == "agent.memory_feedback" for t, _ in emitted)
