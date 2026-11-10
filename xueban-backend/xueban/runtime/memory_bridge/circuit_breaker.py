"""Circuit breaker for ExternalMemoryBridge (Story 2.9, AC5 / NFR13).

Design:
  - Granularity: (agent_id, operation) — shared across sessions for the same agent.
  - Trigger: asyncio.wait_for timeout > 5 seconds.
  - Short-circuit: 60 seconds after first trip.
  - Recovery: After 60 s, the next call is allowed through (probe); if it succeeds,
    the breaker resets; if it times out again, the 60 s window restarts.

Concurrency model (Round 4 P3 follow-ups, code-review-2026-04-29 §H-A/H-B):
  - Per-key ``asyncio.Lock`` serialises state transitions and the probe-acquisition
    handshake. The lock is held only across short, non-awaiting state mutations,
    never across the protected ``coro`` itself.
  - State predicates are split:
      * ``peek_open()``    — pure read, no side effects (safe in tests / dashboards).
      * ``try_acquire_probe()`` — atomic CAS-style: returns True for at most one
        concurrent caller after the open window has expired; the loser is
        short-circuited just like in the OPEN state.
  - ``trip()`` is idempotent: if the breaker was already OPEN, no second
    ``circuit_break`` SSE event is emitted (only the *first* trip per open
    window fires the event, with the larger of the recorded ``elapsed_ms``).
  - ``CircuitBreaker._state`` lazy-insertion is now wrapped in a registry-level
    lock so the dict mutation cannot race the per-key lock-creation.

SSE events emitted (via sse_emitter callback):
  - agent.memory_bridge_circuit_break  — on first trip per open window.
  - agent.memory_bridge_circuit_recover — on successful probe after open window.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple, TypeVar

logger = logging.getLogger("xueban.memory_bridge.circuit_breaker")

TIMEOUT_SECONDS = 5.0
OPEN_WINDOW_SECONDS = 60.0

T = TypeVar("T")

# State constants
_CLOSED = "closed"
_OPEN = "open"
_HALF_OPEN = "half_open"  # probing after window expires


class CircuitBreakerState:
    """Per (agent_id, operation) circuit breaker state.

    All mutating methods are pure (no awaits, no I/O). Concurrency is enforced
    by the owning :class:`CircuitBreaker` via a per-key ``asyncio.Lock``.
    """

    def __init__(self) -> None:
        self.state: str = _CLOSED
        self.opened_at: Optional[float] = None  # monotonic time
        self.elapsed_ms: Optional[float] = None  # last timeout duration
        # Lock serialises state transitions for this key. Created lazily by
        # CircuitBreaker so it always binds to the running event loop.
        self._lock: Optional[asyncio.Lock] = None

    # ---- helpers ---------------------------------------------------------

    def lock(self) -> asyncio.Lock:
        """Lazily create the per-key lock bound to the current loop."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # ---- pure mutations (caller holds lock) ------------------------------

    def trip(self, elapsed_ms: float) -> bool:
        """Idempotent trip — returns True only on the first CLOSED→OPEN transition.

        Subsequent calls while already OPEN keep the original ``opened_at`` so
        the 60s window is anchored to the first trip; ``elapsed_ms`` is updated
        only when the new measurement is larger (keeps the worst-case visible).
        Returns False so callers know not to emit a duplicate SSE event.
        """
        if self.state == _OPEN:
            if elapsed_ms is not None and (
                self.elapsed_ms is None or elapsed_ms > self.elapsed_ms
            ):
                self.elapsed_ms = elapsed_ms
            return False
        self.state = _OPEN
        self.opened_at = time.monotonic()
        self.elapsed_ms = elapsed_ms
        return True

    def reset(self) -> None:
        self.state = _CLOSED
        self.opened_at = None
        self.elapsed_ms = None

    # ---- predicates ------------------------------------------------------

    def peek_open(self) -> bool:
        """True iff requests *should* be short-circuited right now (no mutation).

        Note: callers that are about to *act* on this answer must use
        :meth:`CircuitBreaker.try_acquire_probe` instead — this method is for
        observation only (tests, metrics).
        """
        if self.state == _CLOSED or self.state == _HALF_OPEN:
            return False
        if self.state == _OPEN:
            if self.opened_at is None:
                return True
            return (time.monotonic() - self.opened_at) < OPEN_WINDOW_SECONDS
        return False

    # Backward-compat alias (read-only; never mutates state). Existing tests
    # call ``state.is_open()`` and expect a pure read.
    def is_open(self) -> bool:
        return self.peek_open()


class CircuitBreaker:
    """Registry of per-(agent_id, operation) breaker states.

    Thread-safety / asyncio-safety:
      The ``_states`` registry is guarded by ``_registry_lock`` for lazy
      insertion. Every state transition (trip/reset/probe acquisition) holds
      ``state.lock()``. We never hold a lock across the protected coroutine.
    """

    def __init__(
        self,
        sse_emitter: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> None:
        self._states: Dict[Tuple[str, str], CircuitBreakerState] = {}
        self._sse_emitter = sse_emitter
        # Guards _states dict mutation only. Created lazily so that callers
        # constructing a CircuitBreaker outside an event loop don't fail.
        self._registry_lock: Optional[asyncio.Lock] = None

    def _get_registry_lock(self) -> asyncio.Lock:
        if self._registry_lock is None:
            self._registry_lock = asyncio.Lock()
        return self._registry_lock

    def _state(self, agent_id: str, operation: str) -> CircuitBreakerState:
        """Get-or-create state for a key.

        NOTE: Sync helper for test code / observers. Inside async code with
        possible races, use :meth:`_state_async` instead.
        """
        key = (agent_id, operation)
        if key not in self._states:
            self._states[key] = CircuitBreakerState()
        return self._states[key]

    async def _state_async(
        self, agent_id: str, operation: str
    ) -> CircuitBreakerState:
        """Race-free get-or-create state for a key (async path)."""
        key = (agent_id, operation)
        # Fast path — no lock if already present.
        existing = self._states.get(key)
        if existing is not None:
            return existing
        async with self._get_registry_lock():
            existing = self._states.get(key)
            if existing is None:
                existing = CircuitBreakerState()
                self._states[key] = existing
            return existing

    def is_open(self, agent_id: str, operation: str) -> bool:
        """Read-only observation of breaker openness (no probe consumed)."""
        return self._state(agent_id, operation).peek_open()

    async def try_acquire_probe(
        self, agent_id: str, operation: str
    ) -> Tuple[bool, CircuitBreakerState]:
        """Atomically decide whether *this* caller may proceed.

        Returns (allowed, state):
          * allowed=True  → caller should run the protected coroutine. State
            is either CLOSED, or HALF_OPEN with this caller designated as the
            single probe.
          * allowed=False → caller should short-circuit (state is OPEN and the
            window has not expired, or HALF_OPEN already taken by another
            concurrent caller).
        """
        state = await self._state_async(agent_id, operation)
        async with state.lock():
            if state.state == _CLOSED:
                return True, state
            if state.state == _OPEN:
                if state.opened_at is None:
                    return False, state
                if (time.monotonic() - state.opened_at) < OPEN_WINDOW_SECONDS:
                    return False, state
                # Window expired → designate this caller as the probe.
                state.state = _HALF_OPEN
                return True, state
            # HALF_OPEN: another caller is already probing — short-circuit
            # this concurrent caller until the probe resolves.
            return False, state

    def _emit(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self._sse_emitter is not None:
            try:
                self._sse_emitter(event_type, payload)
            except Exception:
                logger.exception("SSE emitter raised in circuit breaker")

    async def call(
        self,
        agent_id: str,
        operation: str,
        coro: Awaitable[T],
    ) -> Tuple[Optional[T], bool]:
        """Execute *coro* under circuit breaker protection.

        Returns:
            (result, timed_out) — if timed_out is True, result is None and the
            breaker has been tripped (or was already open).
        """
        allowed, state = await self.try_acquire_probe(agent_id, operation)

        if not allowed:
            # Short-circuit without scheduling the coroutine. Callers pass an
            # already-constructed coroutine object — close it explicitly so we
            # don't leak it (Python emits a "coroutine was never awaited"
            # RuntimeWarning otherwise).
            if hasattr(coro, "close"):
                try:
                    coro.close()
                except Exception:
                    logger.debug("CircuitBreaker: failed to close short-circuited coro")
            logger.debug(
                "CircuitBreaker: short-circuit agent=%s op=%s", agent_id, operation
            )
            return None, True

        was_probe = state.state == _HALF_OPEN
        start = time.monotonic()
        try:
            result = await asyncio.wait_for(coro, timeout=TIMEOUT_SECONDS)
            # Success — if we were the probe, recover.
            if was_probe:
                async with state.lock():
                    if state.state == _HALF_OPEN:
                        logger.info(
                            "CircuitBreaker: recovered agent=%s op=%s",
                            agent_id,
                            operation,
                        )
                        state.reset()
                        emit_recover = True
                    else:
                        emit_recover = False
                if emit_recover:
                    self._emit(
                        "agent.memory_bridge_circuit_recover",
                        {"agent_id": agent_id, "operation": operation},
                    )
            return result, False

        except asyncio.TimeoutError:
            elapsed_ms = (time.monotonic() - start) * 1000
            async with state.lock():
                first_trip = state.trip(elapsed_ms)
            if first_trip:
                logger.warning(
                    "CircuitBreaker: TRIP agent=%s op=%s elapsed_ms=%.1f",
                    agent_id,
                    operation,
                    elapsed_ms,
                )
                self._emit(
                    "agent.memory_bridge_circuit_break",
                    {
                        "agent_id": agent_id,
                        "operation": operation,
                        "elapsed_ms": elapsed_ms,
                    },
                )
            else:
                logger.debug(
                    "CircuitBreaker: re-trip suppressed agent=%s op=%s",
                    agent_id,
                    operation,
                )
            return None, True
