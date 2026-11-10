"""Tests for CircuitBreaker + ExternalMemoryBridge timeout handling — Story 2.9 AC5.

Covers:
  - 5s 超时 → drink 返回 empty+warning="river_unreachable"
  - 5s 超时 → pour 返回 deferred(river_timeout)
  - 60s 内短路（不调 river）
  - 60s 后自动恢复（probe 成功 → reset）
  - SSE 事件 agent.memory_bridge_circuit_break / agent.memory_bridge_circuit_recover
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from xueban.runtime.memory_bridge.bridge import ExternalMemoryBridge
from xueban.runtime.memory_bridge.circuit_breaker import (
    CircuitBreaker,
    TIMEOUT_SECONDS,
    OPEN_WINDOW_SECONDS,
    _CLOSED,
    _OPEN,
    _HALF_OPEN,
)
from xueban.runtime.memory_bridge.river_stub import InMemoryRiverStub


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
    card = {"memory_bridge": {"mode": mode, "drink_from": ["alluvium"], "pour_targets": ["alluvium"]}}
    loader = lambda _: card  # noqa: E731
    return ExternalMemoryBridge(
        river=river,
        agent_card_loader=loader,
        sse_emitter=sse_emitter,
        trajectory_path=Path(tempfile.mktemp(suffix=".jsonl")),
    )


async def _slow_coro(delay: float = 10.0):
    """A coroutine that sleeps longer than the circuit breaker timeout."""
    await asyncio.sleep(delay)
    return "should not reach here"


# ---------------------------------------------------------------------------
# Unit tests: CircuitBreaker directly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_circuit_breaker_timeout_trips():
    """Timeout should trip the circuit breaker."""
    emitted = []
    cb = CircuitBreaker(sse_emitter=lambda t, p: emitted.append((t, p)))

    result, timed_out = await cb.call("agent-1", "drink", _slow_coro())

    assert timed_out is True
    assert result is None
    assert cb._state("agent-1", "drink").state == _OPEN
    assert any(t == "agent.memory_bridge_circuit_break" for t, _ in emitted)


@pytest.mark.asyncio
async def test_circuit_breaker_short_circuits_within_window():
    """After trip, subsequent calls within 60s should short-circuit."""
    cb = CircuitBreaker()

    # Trip the breaker
    _, _ = await cb.call("agent-1", "drink", _slow_coro())

    # Immediately check — should be open (short-circuit)
    called = []

    async def probe_coro():
        called.append(True)
        return "data"

    result, timed_out = await cb.call("agent-1", "drink", probe_coro())
    assert timed_out is True
    assert len(called) == 0  # coro never executed


@pytest.mark.asyncio
async def test_circuit_breaker_recovers_after_window(monkeypatch):
    """After 60s window, circuit breaker enters HALF_OPEN and recovers on success."""
    cb = CircuitBreaker()

    # Trip the breaker
    _, _ = await cb.call("agent-1", "drink", _slow_coro())
    assert cb._state("agent-1", "drink").state == _OPEN

    # Simulate 60s passing by manipulating opened_at
    state = cb._state("agent-1", "drink")
    state.opened_at = time.monotonic() - (OPEN_WINDOW_SECONDS + 1)

    # Now the breaker should be half-open → allow probe
    emitted = []
    cb._sse_emitter = lambda t, p: emitted.append((t, p))

    async def fast_coro():
        return "recovered data"

    result, timed_out = await cb.call("agent-1", "drink", fast_coro())
    assert timed_out is False
    assert result == "recovered data"
    assert cb._state("agent-1", "drink").state == _CLOSED
    assert any(t == "agent.memory_bridge_circuit_recover" for t, _ in emitted)


@pytest.mark.asyncio
async def test_circuit_breaker_different_agents_independent():
    """Breakers for different (agent_id, operation) pairs are independent."""
    cb = CircuitBreaker()

    # Trip agent-1/drink
    _, _ = await cb.call("agent-1", "drink", _slow_coro())

    # agent-2/drink should still be CLOSED
    called = []

    async def probe():
        called.append(True)
        return "ok"

    result, timed_out = await cb.call("agent-2", "drink", probe())
    assert timed_out is False
    assert len(called) == 1


@pytest.mark.asyncio
async def test_circuit_breaker_drink_pour_independent():
    """Drink and pour breakers for the same agent are independent."""
    cb = CircuitBreaker()

    # Trip agent-1/drink
    _, _ = await cb.call("agent-1", "drink", _slow_coro())

    # agent-1/pour should still be CLOSED
    async def fast_pour():
        return "accepted"

    result, timed_out = await cb.call("agent-1", "pour", fast_pour())
    assert timed_out is False


# ---------------------------------------------------------------------------
# Integration: bridge drink() with timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_drink_timeout_returns_empty_warning():
    """drink() timeout → DrinkResult.empty=True, warning='river_unreachable'."""
    async def slow_drink(*a, **kw):
        await asyncio.sleep(10.0)

    mock_river = AsyncMock()
    mock_river.drink = AsyncMock(side_effect=slow_drink)

    emitted = []
    bridge = _make_bridge(
        river=mock_river, sse_emitter=lambda t, p: emitted.append((t, p))
    )

    result = await bridge.drink("q", "agent-1", "sess-1")

    assert result.empty is True
    assert result.warning == "river_unreachable"
    assert any(t == "agent.memory_bridge_circuit_break" for t, _ in emitted)


@pytest.mark.asyncio
async def test_bridge_pour_timeout_defers_all():
    """pour() timeout → all candidates → deferred(river_timeout)."""
    async def slow_pour(*a, **kw):
        await asyncio.sleep(10.0)

    mock_river = AsyncMock()
    mock_river.pour = AsyncMock(side_effect=slow_pour)

    bridge = _make_bridge(river=mock_river)

    candidates = [
        {"content": "c1", "confidence": 0.8, "target_layer": "alluvium"},
        {"content": "c2", "confidence": 0.9, "target_layer": "alluvium"},
    ]
    result = await bridge.pour(candidates, "agent-1", "sess-1")

    assert result.deferred_count == 2
    assert all(d.reason == "river_timeout" for d in result.deferred)


@pytest.mark.asyncio
async def test_bridge_drink_short_circuits_after_trip():
    """After a trip, subsequent drink() calls short-circuit without calling river."""
    call_count = 0

    async def slow_drink(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(10.0)
        return []

    mock_river = AsyncMock()
    mock_river.drink = AsyncMock(side_effect=slow_drink)

    bridge = _make_bridge(river=mock_river)

    # First call trips the breaker
    r1 = await bridge.drink("q1", "agent-1", "sess-1")
    assert r1.empty is True
    assert call_count == 1  # river was called once (before timeout)

    # Second call should short-circuit
    r2 = await bridge.drink("q2", "agent-1", "sess-1")
    assert r2.empty is True
    assert call_count == 1  # river NOT called again


@pytest.mark.asyncio
async def test_circuit_break_sse_payload():
    """agent.memory_bridge_circuit_break event should contain required fields."""
    emitted = []
    bridge = _make_bridge(sse_emitter=lambda t, p: emitted.append((t, p)))

    async def slow_drink_inner(*a, **kw):
        await asyncio.sleep(10.0)
        return []

    mock_river = AsyncMock()
    mock_river.drink = AsyncMock(side_effect=slow_drink_inner)
    bridge._river = mock_river

    await bridge.drink("q", "agent-1", "sess-1")

    cb_events = [(t, p) for t, p in emitted if t == "agent.memory_bridge_circuit_break"]
    assert len(cb_events) >= 1
    payload = cb_events[0][1]
    assert payload["agent_id"] == "agent-1"
    assert payload["operation"] == "drink"
    assert "elapsed_ms" in payload
