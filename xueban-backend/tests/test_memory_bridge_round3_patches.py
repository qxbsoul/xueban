"""Round 3 regression tests for ExternalMemoryBridge code-review fixes.

Covers Story 2.9 fixes:
  - C1: empty `candidates` list does not emit ghost SSE event / trajectory write
  - C2: unknown SedimentCandidate fields produce a warning log
  - C3: AgentEventTypeLiteral now accepts the 5 memory-bridge event names
  - H1: circuit breaker closes the short-circuited coroutine (no resource leak)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Tuple

import pytest

from xueban.runtime.contracts import AgentEvent
from xueban.runtime.memory_bridge.bridge import ExternalMemoryBridge
from xueban.runtime.memory_bridge.circuit_breaker import (
    CircuitBreaker,
    OPEN_WINDOW_SECONDS,
)


# ---------------------------------------------------------------------------
# C3 — AgentEventTypeLiteral accepts new memory-bridge event types
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event_type",
    [
        "agent.memory_drink",
        "agent.memory_pour",
        "agent.memory_feedback",
        "agent.memory_bridge_circuit_break",
        "agent.memory_bridge_circuit_recover",
    ],
)
def test_agent_event_accepts_memory_bridge_types(event_type: str) -> None:
    ev = AgentEvent(
        run_id="r-1",
        node_id="n-1",
        agent_id="a-1",
        type=event_type,  # type: ignore[arg-type]
        payload={"foo": "bar"},
    )
    assert ev.type == event_type


# ---------------------------------------------------------------------------
# C1 — empty candidates list does not emit ghost events
# ---------------------------------------------------------------------------


class _StubRiver:
    async def drink(self, *_a, **_k):
        return [], False

    async def pour(self, *_a, **_k):
        return "accepted"


def test_pour_with_empty_candidates_emits_no_event(tmp_path) -> None:
    emitted: List[Tuple[str, Dict[str, Any]]] = []
    bridge = ExternalMemoryBridge(
        river=_StubRiver(),
        agent_card_loader=lambda _aid: {"memory_bridge": {"mode": "two_way"}},
        sse_emitter=lambda et, p: emitted.append((et, p)),
        trajectory_path=tmp_path / "trajectory.jsonl",
    )

    result = asyncio.run(bridge.pour([], agent_id="a-1", session_id="s-1"))

    assert result.is_empty
    assert emitted == []  # no ghost SSE event


# ---------------------------------------------------------------------------
# C2 — unknown SedimentCandidate field is logged
# ---------------------------------------------------------------------------


def test_pour_logs_warning_for_unknown_candidate_fields(tmp_path, caplog) -> None:
    bridge = ExternalMemoryBridge(
        river=_StubRiver(),
        agent_card_loader=lambda _aid: {"memory_bridge": {"mode": "two_way"}},
        sse_emitter=None,
        trajectory_path=tmp_path / "trajectory.jsonl",
    )

    caplog.set_level(logging.WARNING, logger="xueban.memory_bridge")
    asyncio.run(
        bridge.pour(
            [{"content": "hello", "totally_unknown_key": 42}],
            agent_id="a-1",
            session_id="s-1",
        )
    )

    assert any(
        "dropped unknown SedimentCandidate" in rec.getMessage()
        and "totally_unknown_key" in rec.getMessage()
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# H1 — circuit breaker closes the short-circuited coroutine
# ---------------------------------------------------------------------------


def test_circuit_breaker_closes_coroutine_on_short_circuit() -> None:
    """When the breaker is OPEN, the passed-in coroutine must be closed,
    otherwise Python emits a `coroutine was never awaited` RuntimeWarning.

    A closed coroutine has `cr_frame is None` and re-awaiting it raises
    `RuntimeError: cannot reuse already awaited coroutine`.
    """
    import warnings

    breaker = CircuitBreaker(sse_emitter=None)
    state = breaker._state("a-1", "drink")
    state.trip(elapsed_ms=10_000.0)
    assert state.is_open()

    async def _slow():
        await asyncio.sleep(60)

    coro = _slow()
    assert coro.cr_frame is not None  # un-started coro has a frame

    async def _go():
        return await breaker.call("a-1", "drink", coro)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result, timed_out = asyncio.run(_go())

    assert result is None
    assert timed_out is True
    assert coro.cr_frame is None  # close() consumed the frame
    assert not any(
        "coroutine" in str(w.message) and "never awaited" in str(w.message)
        for w in caught
    )


# ---------------------------------------------------------------------------
# H2 — pour_targets non-empty check (Story 2.9 AC2 precondition)
# ---------------------------------------------------------------------------


def test_pour_two_way_with_empty_pour_targets_rejects_all(tmp_path) -> None:
    bridge = ExternalMemoryBridge(
        river=_StubRiver(),
        agent_card_loader=lambda _aid: {
            "memory_bridge": {"mode": "two_way", "pour_targets": []}
        },
        sse_emitter=None,
        trajectory_path=tmp_path / "trajectory.jsonl",
    )

    result = asyncio.run(
        bridge.pour(
            [{"content": "x"}, {"content": "y"}],
            agent_id="a-1",
            session_id="s-1",
        )
    )

    assert result.accepted_count == 0
    assert result.deferred_count == 0
    assert result.rejected_count == 2
    assert all(item.reason == "no_pour_targets" for item in result.rejected)


def test_pour_two_way_with_pour_targets_proceeds(tmp_path) -> None:
    bridge = ExternalMemoryBridge(
        river=_StubRiver(),
        agent_card_loader=lambda _aid: {
            "memory_bridge": {"mode": "two_way", "pour_targets": ["alluvium"]}
        },
        sse_emitter=None,
        trajectory_path=tmp_path / "trajectory.jsonl",
    )

    result = asyncio.run(
        bridge.pour([{"content": "x"}], agent_id="a-1", session_id="s-1"),
    )
    # _StubRiver.pour returns "accepted"
    assert result.accepted_count == 1


# ---------------------------------------------------------------------------
# H4 — hermes update_type uses spec key only ("type"), no sessionUpdate fallback
# ---------------------------------------------------------------------------


def test_hermes_handle_session_update_ignores_legacy_session_update_key(tmp_path) -> None:
    """An update payload that only carries the legacy `sessionUpdate` key
    (not the ACP-spec `type` field) must NOT route to bridge.pour."""
    from xueban.runtime.gateway.hermes import HermesGateway

    bridge = ExternalMemoryBridge(
        river=_StubRiver(),
        agent_card_loader=lambda _aid: {
            "memory_bridge": {"mode": "two_way", "pour_targets": ["alluvium"]}
        },
        sse_emitter=None,
        trajectory_path=tmp_path / "trajectory.jsonl",
    )
    gateway = HermesGateway(bridge=bridge)

    # Only "sessionUpdate" key set — no "type". Should NOT route to pour.
    update = {
        "sessionUpdate": "xueban_memory_proposal",
        "candidates": [{"content": "x"}],
    }
    result = asyncio.run(gateway.handle_session_update(update, "s-1", "a-1"))
    assert result is None  # passed through, not routed
