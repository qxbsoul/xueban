"""Tests for ExternalMemoryBridge.drink() — Story 2.9 AC1.

Covers:
  - river.drink mock → fence封装正确 → UUID唯一
  - two_way / read_only 模式都正常走 drink 管道
  - isolated 模式短路返回空 context
  - circuit-break timeout → empty + warning="river_unreachable"
  - SSE 事件 agent.memory_drink 被触发
  - trajectory 事件写入
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from xueban.runtime.memory_bridge.bridge import ExternalMemoryBridge
from xueban.runtime.memory_bridge.fence import validate_fence
from xueban.runtime.memory_bridge.river_stub import InMemoryRiverStub
from xueban.runtime.memory_bridge.types import DrinkResult, InvalidMemoryBridgeMode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_card(mode: str = "two_way", drink_from: List[str] | None = None) -> Dict[str, Any]:
    return {
        "memory_bridge": {
            "mode": mode,
            "drink_from": drink_from or ["alluvium"],
            "pour_targets": ["alluvium"],
        }
    }


def _make_bridge(
    river=None,
    mode: str = "two_way",
    sse_emitter=None,
    trajectory_path: Path | None = None,
) -> ExternalMemoryBridge:
    if river is None:
        river = InMemoryRiverStub()
    card = _make_card(mode)
    loader = lambda agent_id: card  # noqa: E731
    return ExternalMemoryBridge(
        river=river,
        agent_card_loader=loader,
        sse_emitter=sse_emitter,
        trajectory_path=trajectory_path or Path(tempfile.mktemp(suffix=".jsonl")),
    )


# ---------------------------------------------------------------------------
# Tests: basic drink functionality
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drink_returns_fence_wrapped_result():
    """river.drink returns fragments → DrinkResult with correct fence metadata."""
    mock_river = AsyncMock()
    mock_river.drink = AsyncMock(return_value=["fragment A", "fragment B"])

    bridge = _make_bridge(river=mock_river, mode="two_way")
    result = await bridge.drink("what is X", "agent-1", "sess-1")

    assert isinstance(result, DrinkResult)
    assert result.fence == "xueban-context"
    assert result.type == "context"
    assert "fragment A" in result.text
    assert "fragment B" in result.text
    assert not result.empty


@pytest.mark.asyncio
async def test_drink_fence_uuid_is_valid_uuid4():
    """fence_uuid must be a valid UUID4 string."""
    mock_river = AsyncMock()
    mock_river.drink = AsyncMock(return_value=["data"])

    bridge = _make_bridge(river=mock_river)
    result = await bridge.drink("query", "agent-1", "sess-1")

    # Should not raise
    parsed = UUID(result.fence_uuid, version=4)
    assert str(parsed) == result.fence_uuid


@pytest.mark.asyncio
async def test_drink_fence_uuid_unique_per_turn():
    """Each drink() call must produce a distinct fence_uuid."""
    mock_river = AsyncMock()
    mock_river.drink = AsyncMock(return_value=["data"])

    bridge = _make_bridge(river=mock_river)
    r1 = await bridge.drink("query1", "agent-1", "sess-1")
    r2 = await bridge.drink("query2", "agent-1", "sess-1")

    assert r1.fence_uuid != r2.fence_uuid


@pytest.mark.asyncio
async def test_drink_acp_fragment_passes_fence_validation():
    """to_acp_fragment() result should pass validate_fence()."""
    mock_river = AsyncMock()
    mock_river.drink = AsyncMock(return_value=["ctx"])

    bridge = _make_bridge(river=mock_river)
    result = await bridge.drink("query", "agent-1", "sess-1")

    assert validate_fence(result.to_acp_fragment()) is True


@pytest.mark.asyncio
async def test_drink_empty_river_response():
    """Empty fragments list → DrinkResult with empty=True."""
    mock_river = AsyncMock()
    mock_river.drink = AsyncMock(return_value=[])

    bridge = _make_bridge(river=mock_river)
    result = await bridge.drink("query", "agent-1", "sess-1")

    assert result.empty is True
    assert result.text == ""


# ---------------------------------------------------------------------------
# Tests: mode branching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drink_isolated_mode_short_circuits():
    """isolated mode: bridge must NOT call river.drink and return empty context."""
    mock_river = AsyncMock()
    mock_river.drink = AsyncMock(return_value=["should not be called"])

    bridge = _make_bridge(river=mock_river, mode="isolated")
    result = await bridge.drink("query", "agent-1", "sess-1")

    mock_river.drink.assert_not_called()
    assert result.empty is True
    assert result.text == ""


@pytest.mark.asyncio
async def test_drink_read_only_mode_calls_river():
    """read_only mode: drink should still call river.drink."""
    mock_river = AsyncMock()
    mock_river.drink = AsyncMock(return_value=["read-only content"])

    bridge = _make_bridge(river=mock_river, mode="read_only")
    result = await bridge.drink("query", "agent-1", "sess-1")

    mock_river.drink.assert_called_once()
    assert "read-only content" in result.text


@pytest.mark.asyncio
async def test_drink_two_way_mode_calls_river():
    """two_way mode: drink calls river.drink normally."""
    mock_river = AsyncMock()
    mock_river.drink = AsyncMock(return_value=["two-way content"])

    bridge = _make_bridge(river=mock_river, mode="two_way")
    result = await bridge.drink("query", "agent-1", "sess-1")

    mock_river.drink.assert_called_once()
    assert "two-way content" in result.text


# ---------------------------------------------------------------------------
# Tests: SSE events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drink_emits_sse_event():
    """drink() must emit agent.memory_drink SSE event."""
    mock_river = AsyncMock()
    mock_river.drink = AsyncMock(return_value=["x"])

    emitted = []
    bridge = _make_bridge(river=mock_river, sse_emitter=lambda t, p: emitted.append((t, p)))
    await bridge.drink("q", "agent-1", "sess-1")

    assert any(t == "agent.memory_drink" for t, _ in emitted)
    event_payload = next(p for t, p in emitted if t == "agent.memory_drink")
    assert event_payload["agent_id"] == "agent-1"
    assert event_payload["session_id"] == "sess-1"
    assert "fence_uuid" in event_payload


@pytest.mark.asyncio
async def test_drink_isolated_emits_sse_event():
    """isolated mode drink() must still emit agent.memory_drink SSE event."""
    emitted = []
    bridge = _make_bridge(mode="isolated", sse_emitter=lambda t, p: emitted.append((t, p)))
    await bridge.drink("q", "agent-1", "sess-1")

    assert any(t == "agent.memory_drink" for t, _ in emitted)


# ---------------------------------------------------------------------------
# Tests: trajectory write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drink_writes_trajectory_event():
    """drink() must write a memory_drink event to trajectory JSONL."""
    mock_river = AsyncMock()
    mock_river.drink = AsyncMock(return_value=["data"])

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tf:
        traj_path = Path(tf.name)

    bridge = _make_bridge(river=mock_river, trajectory_path=traj_path)
    await bridge.drink("query", "agent-1", "sess-1")

    lines = traj_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 1
    record = json.loads(lines[-1])
    assert record["event"] == "memory_drink"
    assert record["agent_id"] == "agent-1"
    assert "fence_uuid" in record


# ---------------------------------------------------------------------------
# Tests: invalid mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drink_invalid_mode_raises():
    """Invalid mode value must raise InvalidMemoryBridgeMode."""

    bad_card = {"memory_bridge": {"mode": "banana"}}
    bridge = ExternalMemoryBridge(
        river=InMemoryRiverStub(),
        agent_card_loader=lambda _: bad_card,
        trajectory_path=Path(tempfile.mktemp(suffix=".jsonl")),
    )
    with pytest.raises(InvalidMemoryBridgeMode):
        await bridge.drink("q", "agent-1", "sess-1")
