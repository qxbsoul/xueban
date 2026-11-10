"""Tests for Story 2.9 Round 4 P3 follow-ups (code-review-2026-04-29).

Covers:
  - H-A: concurrent first-trip race emits exactly one circuit_break event.
  - H-B: peek_open() is pure read; try_acquire_probe() is atomic CAS for the
         single allowed probe slot; concurrent callers in HALF_OPEN are
         short-circuited until the probe resolves.
  - M-A: pour candidates over MAX_POUR_CANDIDATES → overflow rejected with
         reason="over_capacity"; first cap entries still processed.
  - M-D: agent_card_loader exception → fail-closed (drink: empty +
         warning="agent_card_unreadable"; pour: all rejected with
         reason="agent_card_unreadable").
"""
from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

import pytest

from xueban.runtime.memory_bridge.bridge import (
    ExternalMemoryBridge,
    MAX_POUR_CANDIDATES,
)
from xueban.runtime.memory_bridge.circuit_breaker import (
    CircuitBreaker,
    OPEN_WINDOW_SECONDS,
    _CLOSED,
    _HALF_OPEN,
    _OPEN,
)
from xueban.runtime.memory_bridge.river_stub import InMemoryRiverStub


# ---------------------------------------------------------------------------
# H-A: concurrent first-trip race
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_trips_emit_single_circuit_break_event():
    """Two concurrent timeouts → only one circuit_break SSE event."""
    emitted = []
    cb = CircuitBreaker(sse_emitter=lambda t, p: emitted.append((t, p)))

    async def slow():
        await asyncio.sleep(10.0)
        return "never"

    # Launch two concurrent calls — both will time out near simultaneously.
    r1, r2 = await asyncio.gather(
        cb.call("agent-1", "drink", slow()),
        cb.call("agent-1", "drink", slow()),
    )

    assert r1[1] is True and r2[1] is True
    breaks = [e for e in emitted if e[0] == "agent.memory_bridge_circuit_break"]
    # H-A: exactly one trip event despite two concurrent timeouts.
    assert len(breaks) == 1, f"Expected 1 circuit_break event, got {len(breaks)}"
    assert cb._state("agent-1", "drink").state == _OPEN


@pytest.mark.asyncio
async def test_trip_is_idempotent_when_already_open():
    """Manual trip() while already OPEN must not move opened_at backwards."""
    cb = CircuitBreaker()
    state = cb._state("agent-1", "drink")
    async with state.lock():
        first = state.trip(1234.0)
    original_opened_at = state.opened_at
    await asyncio.sleep(0.01)
    async with state.lock():
        second = state.trip(5678.0)
    assert first is True
    assert second is False  # idempotent — no second trip notification
    assert state.opened_at == original_opened_at  # window anchored to first trip
    # elapsed_ms updated to worst-case
    assert state.elapsed_ms == 5678.0


# ---------------------------------------------------------------------------
# H-B: peek_open vs try_acquire_probe split
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_peek_open_does_not_consume_probe_slot():
    """peek_open() must be a pure read — no state mutation."""
    cb = CircuitBreaker()
    state = cb._state("agent-1", "drink")
    # Force into OPEN with expired window.
    async with state.lock():
        state.trip(100.0)
        state.opened_at = time.monotonic() - (OPEN_WINDOW_SECONDS + 1)

    # Multiple peeks return False (window expired) without consuming the probe.
    assert state.peek_open() is False
    assert state.peek_open() is False
    # State stays OPEN until try_acquire_probe is invoked.
    assert state.state == _OPEN

    # Now acquire — first caller wins, becomes HALF_OPEN.
    allowed_1, _ = await cb.try_acquire_probe("agent-1", "drink")
    assert allowed_1 is True
    assert state.state == _HALF_OPEN

    # Concurrent second caller while still HALF_OPEN is denied (probe taken).
    allowed_2, _ = await cb.try_acquire_probe("agent-1", "drink")
    assert allowed_2 is False


@pytest.mark.asyncio
async def test_concurrent_probe_only_one_executes():
    """In HALF_OPEN, only one of N concurrent callers executes the coro."""
    cb = CircuitBreaker()
    state = cb._state("agent-1", "drink")
    async with state.lock():
        state.trip(100.0)
        state.opened_at = time.monotonic() - (OPEN_WINDOW_SECONDS + 1)

    executed = []

    async def probe(label: str):
        executed.append(label)
        await asyncio.sleep(0.05)
        return label

    results = await asyncio.gather(
        cb.call("agent-1", "drink", probe("a")),
        cb.call("agent-1", "drink", probe("b")),
        cb.call("agent-1", "drink", probe("c")),
    )
    timed_out_count = sum(1 for _, t in results if t)
    success_count = sum(1 for _, t in results if not t)
    # Exactly one probe executed; others short-circuited as still-open.
    assert success_count == 1
    assert timed_out_count == 2
    assert len(executed) == 1
    # Probe succeeded → breaker fully reset.
    assert state.state == _CLOSED


# ---------------------------------------------------------------------------
# M-A: pour candidates over capacity
# ---------------------------------------------------------------------------


def _make_bridge(card=None) -> ExternalMemoryBridge:
    if card is None:
        card = {
            "memory_bridge": {
                "mode": "two_way",
                "drink_from": ["alluvium"],
                "pour_targets": ["alluvium"],
            }
        }
    return ExternalMemoryBridge(
        river=InMemoryRiverStub(),
        agent_card_loader=lambda _aid: card,
        trajectory_path=Path(tempfile.mktemp(suffix=".jsonl")),
    )


@pytest.mark.asyncio
async def test_pour_over_cap_rejects_overflow():
    """Submissions beyond MAX_POUR_CANDIDATES are rejected, first cap processed."""
    bridge = _make_bridge()
    over = MAX_POUR_CANDIDATES + 5
    candidates = [
        {"content": f"c-{i}", "confidence": 0.9, "target_layer": "alluvium"}
        for i in range(over)
    ]
    result = await bridge.pour(candidates, "agent-1", "sess-1")

    over_capacity_rejects = [
        r for r in result.rejected if r.reason == "over_capacity"
    ]
    assert len(over_capacity_rejects) == 5
    # First MAX_POUR_CANDIDATES were actually processed (accepted by stub).
    assert result.accepted_count + (
        result.rejected_count - 5
    ) + result.deferred_count == MAX_POUR_CANDIDATES


@pytest.mark.asyncio
async def test_pour_at_cap_no_rejects():
    """Exactly MAX_POUR_CANDIDATES → no over_capacity rejects."""
    bridge = _make_bridge()
    candidates = [
        {"content": f"c-{i}", "confidence": 0.9, "target_layer": "alluvium"}
        for i in range(MAX_POUR_CANDIDATES)
    ]
    result = await bridge.pour(candidates, "agent-1", "sess-1")
    assert all(r.reason != "over_capacity" for r in result.rejected)


# ---------------------------------------------------------------------------
# M-D: fail closed on agent_card loader errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drink_fail_closed_on_loader_error():
    """drink() with raising loader → empty + warning='agent_card_unreadable'."""

    def boom(_aid):
        raise RuntimeError("simulated loader failure (e.g. yaml parse error)")

    emitted = []
    bridge = ExternalMemoryBridge(
        river=InMemoryRiverStub(),
        agent_card_loader=boom,
        sse_emitter=lambda t, p: emitted.append((t, p)),
        trajectory_path=Path(tempfile.mktemp(suffix=".jsonl")),
    )
    result = await bridge.drink("q", "agent-x", "sess-1")
    assert result.empty is True
    assert result.warning == "agent_card_unreadable"
    drink_events = [e for e in emitted if e[0] == "agent.memory_drink"]
    assert drink_events
    assert drink_events[0][1].get("warning") == "agent_card_unreadable"


@pytest.mark.asyncio
async def test_pour_fail_closed_on_loader_error():
    """pour() with raising loader → all rejected with agent_card_unreadable."""

    def boom(_aid):
        raise RuntimeError("simulated loader failure")

    bridge = ExternalMemoryBridge(
        river=InMemoryRiverStub(),
        agent_card_loader=boom,
        trajectory_path=Path(tempfile.mktemp(suffix=".jsonl")),
    )
    candidates = [
        {"content": "c1", "confidence": 0.8, "target_layer": "alluvium"},
        {"content": "c2", "confidence": 0.9, "target_layer": "alluvium"},
    ]
    result = await bridge.pour(candidates, "agent-x", "sess-1")
    assert result.accepted_count == 0
    assert result.deferred_count == 0
    assert result.rejected_count == 2
    assert all(r.reason == "agent_card_unreadable" for r in result.rejected)


@pytest.mark.asyncio
async def test_drink_invalid_mode_still_raises_not_fail_closed():
    """An invalid mode literal in agent-card is still surfaced as an error.

    Distinguishing this from loader exceptions is the whole point of
    AgentCardUnreadable: bad config should be loud, transient I/O failure
    should be safe-by-default.
    """
    from xueban.runtime.memory_bridge.types import InvalidMemoryBridgeMode

    bridge = _make_bridge(card={"memory_bridge": {"mode": "banana"}})
    with pytest.raises(InvalidMemoryBridgeMode):
        await bridge.drink("q", "agent-x", "sess-1")
