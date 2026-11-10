"""Tests for ExternalMemoryBridge.pour() — Story 2.9 AC2.

Covers:
  - session.update 解析 + candidates 路由
  - river.pour mock 分类结果 → 三桶 (accepted/rejected/deferred) 正确
  - read_only mode: all rejected(mode_not_writable)
  - isolated mode: all rejected(mode_not_writable)
  - two_way: river.pour "accepted" → accepted bucket
  - two_way: river.pour "rejected" → rejected bucket
  - two_way: river.pour "deferred" → deferred bucket
  - SSE 事件 agent.memory_pour 被触发
  - trajectory 事件写入
  - HermesGateway.handle_session_update 路由 xueban_memory_proposal
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest

from xueban.runtime.memory_bridge.bridge import ExternalMemoryBridge
from xueban.runtime.memory_bridge.river_stub import InMemoryRiverStub
from xueban.runtime.memory_bridge.types import PourResult
from xueban.runtime.gateway.hermes import HermesGateway


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bridge(
    river=None,
    mode: str = "two_way",
    sse_emitter=None,
    trajectory_path: Path | None = None,
) -> ExternalMemoryBridge:
    if river is None:
        river = InMemoryRiverStub()
    card = {"memory_bridge": {"mode": mode, "drink_from": [], "pour_targets": ["alluvium"]}}
    loader = lambda _: card  # noqa: E731
    return ExternalMemoryBridge(
        river=river,
        agent_card_loader=loader,
        sse_emitter=sse_emitter,
        trajectory_path=trajectory_path or Path(tempfile.mktemp(suffix=".jsonl")),
    )


def _candidates(n: int = 2) -> List[Dict[str, Any]]:
    return [
        {
            "content": f"memory content {i}",
            "confidence": 0.9,
            "target_layer": "alluvium",
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Tests: mode gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pour_read_only_all_rejected():
    """read_only mode: all candidates → rejected(mode_not_writable); river NOT called."""
    mock_river = AsyncMock()
    mock_river.pour = AsyncMock()

    bridge = _make_bridge(river=mock_river, mode="read_only")
    result = await bridge.pour(_candidates(3), "agent-1", "sess-1")

    mock_river.pour.assert_not_called()
    assert result.rejected_count == 3
    assert result.accepted_count == 0
    assert result.deferred_count == 0
    assert all(r.reason == "mode_not_writable" for r in result.rejected)


@pytest.mark.asyncio
async def test_pour_isolated_all_rejected():
    """isolated mode: all candidates → rejected(mode_not_writable); river NOT called."""
    mock_river = AsyncMock()
    mock_river.pour = AsyncMock()

    bridge = _make_bridge(river=mock_river, mode="isolated")
    result = await bridge.pour(_candidates(2), "agent-1", "sess-1")

    mock_river.pour.assert_not_called()
    assert result.rejected_count == 2
    assert all(r.reason == "mode_not_writable" for r in result.rejected)


# ---------------------------------------------------------------------------
# Tests: two_way bucket classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pour_two_way_accepted():
    """two_way: river.pour returns 'accepted' → accepted bucket."""
    mock_river = AsyncMock()
    mock_river.pour = AsyncMock(return_value="accepted")

    bridge = _make_bridge(river=mock_river, mode="two_way")
    result = await bridge.pour(_candidates(1), "agent-1", "sess-1")

    assert result.accepted_count == 1
    assert result.rejected_count == 0
    assert result.accepted[0].settled_at_layer == "alluvium"


@pytest.mark.asyncio
async def test_pour_two_way_rejected():
    """two_way: river.pour returns 'rejected' → rejected bucket(river_error)."""
    mock_river = AsyncMock()
    mock_river.pour = AsyncMock(return_value="rejected")

    bridge = _make_bridge(river=mock_river, mode="two_way")
    result = await bridge.pour(_candidates(1), "agent-1", "sess-1")

    assert result.rejected_count == 1
    assert result.rejected[0].reason == "river_error"


@pytest.mark.asyncio
async def test_pour_two_way_deferred():
    """two_way: river.pour returns 'deferred' → deferred bucket."""
    mock_river = AsyncMock()
    mock_river.pour = AsyncMock(return_value="deferred")

    bridge = _make_bridge(river=mock_river, mode="two_way")
    result = await bridge.pour(_candidates(1), "agent-1", "sess-1")

    assert result.deferred_count == 1
    assert result.deferred[0].reason == "needs_social_signal"


@pytest.mark.asyncio
async def test_pour_mixed_results():
    """Multiple candidates with mixed river responses → correct bucket counts."""
    responses = ["accepted", "rejected", "deferred", "accepted"]
    call_count = 0

    async def mock_pour(candidate, source_agent_id):
        nonlocal call_count
        resp = responses[call_count]
        call_count += 1
        return resp

    mock_river = MagicMock()
    mock_river.pour = mock_pour

    bridge = _make_bridge(river=mock_river, mode="two_way")
    result = await bridge.pour(_candidates(4), "agent-1", "sess-1")

    assert result.accepted_count == 2
    assert result.rejected_count == 1
    assert result.deferred_count == 1


# ---------------------------------------------------------------------------
# Tests: SSE events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pour_emits_sse_event():
    """pour() must emit agent.memory_pour SSE event with bucket counts."""
    mock_river = AsyncMock()
    mock_river.pour = AsyncMock(return_value="accepted")

    emitted = []
    bridge = _make_bridge(
        river=mock_river,
        mode="two_way",
        sse_emitter=lambda t, p: emitted.append((t, p)),
    )
    await bridge.pour(_candidates(2), "agent-1", "sess-1")

    assert any(t == "agent.memory_pour" for t, _ in emitted)
    payload = next(p for t, p in emitted if t == "agent.memory_pour")
    assert payload["agent_id"] == "agent-1"
    assert payload["accepted_count"] == 2


# ---------------------------------------------------------------------------
# Tests: trajectory write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pour_writes_trajectory_event():
    """pour() must write a memory_pour event to trajectory JSONL."""
    mock_river = AsyncMock()
    mock_river.pour = AsyncMock(return_value="accepted")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tf:
        traj_path = Path(tf.name)

    bridge = _make_bridge(river=mock_river, trajectory_path=traj_path)
    await bridge.pour(_candidates(2), "agent-1", "sess-1")

    lines = traj_path.read_text(encoding="utf-8").strip().splitlines()
    record = json.loads(lines[-1])
    assert record["event"] == "memory_pour"
    assert "candidate_ids" in record
    assert len(record["candidate_ids"]) == 2


# ---------------------------------------------------------------------------
# Tests: HermesGateway routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_routes_memory_proposal():
    """HermesGateway.handle_session_update routes xueban_memory_proposal."""
    mock_river = AsyncMock()
    mock_river.drink = AsyncMock(return_value=[])
    mock_river.pour = AsyncMock(return_value="accepted")

    bridge = _make_bridge(river=mock_river, mode="two_way")
    gateway = HermesGateway(bridge=bridge)

    update = {
        "type": "xueban_memory_proposal",
        "candidates": [
            {"content": "new fact", "confidence": 0.9, "target_layer": "alluvium"}
        ],
    }
    result = await gateway.handle_session_update(update, "sess-1", "agent-1")

    assert isinstance(result, PourResult)
    assert result.accepted_count == 1


@pytest.mark.asyncio
async def test_gateway_ignores_non_memory_update():
    """Non-memory update types should pass through (return None)."""
    bridge = _make_bridge()
    gateway = HermesGateway(bridge=bridge)

    result = await gateway.handle_session_update(
        {"type": "agent_thought_chunk", "content": "thinking…"},
        "sess-1",
        "agent-1",
    )
    assert result is None


@pytest.mark.asyncio
async def test_gateway_handles_missing_candidates_gracefully():
    """Memory proposal without 'candidates' key returns None gracefully."""
    bridge = _make_bridge()
    gateway = HermesGateway(bridge=bridge)

    result = await gateway.handle_session_update(
        {"type": "xueban_memory_proposal"},  # no candidates
        "sess-1",
        "agent-1",
    )
    assert result is None


# Import MagicMock at module level
from unittest.mock import MagicMock  # noqa: E402
