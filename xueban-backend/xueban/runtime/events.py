"""运行时事件常量定义 + 事件 payload 模型 + RunEventBus (Story 2.6 / AR50, Story 4.1)。"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Deque, Dict, List, Literal, Optional, Set, Tuple

from pydantic import BaseModel, Field

# ---------- Approval Gate 事件 ----------
APPROVAL_PENDING = "approval.pending"
APPROVAL_APPROVED = "approval.approved"
APPROVAL_REJECTED = "approval.rejected"
APPROVAL_TIMEOUT = "approval.timeout"

# ---------- Node 生命周期事件 (Story 4.1 AC2, Story 4.4 NODE_RETRIED) ----------
NODE_STARTED = "node.started"
NODE_SUCCEEDED = "node.succeeded"
NODE_FAILED = "node.failed"
NODE_REJECTED = "node.rejected"
NODE_RETRIED = "node.retried"        # Story 4.4: emitted on each retry attempt
NODE_INVALIDATED = "node.invalidated"

# ---------- Policy 事件 ----------
POLICY_VIOLATION = "policy.violation"
POLICY_UPDATED = "policy.updated"

# ---------- Run 级事件 ----------
RUN_COMPLETED = "run.completed"
RUN_RESUMED = "run.resumed"
RUN_RECONFIGURED = "run.reconfigured"  # Story 4.6

# ---------- 其他 ----------
HANDOFF_TRIGGERED = "handoff.triggered"
CHECKPOINT_SAVED = "checkpoint.saved"
GAP_DETECTED = "agent.gap_detected"


# ---------------------------------------------------------------------------
# Agent event type constants (Story 2.6 / AC1)
# ---------------------------------------------------------------------------

class AgentEventType:
    """Canonical event type strings for the agent.* namespace."""

    DISPATCHED         = "agent.dispatched"
    THINKING           = "agent.thinking"
    TOOL_CALLED        = "agent.tool_called"
    TOOL_RESULT        = "agent.tool_result"
    COMPLETED          = "agent.completed"
    FAILED             = "agent.failed"
    REJECTED           = "agent.rejected"
    OUTPUT             = "agent.output"
    DEGRADED           = "agent.degraded"
    APPROVAL_REQUESTED = "agent.approval_requested"

    ALL: frozenset = frozenset({
        DISPATCHED, THINKING, TOOL_CALLED, TOOL_RESULT,
        COMPLETED, FAILED, REJECTED, OUTPUT, DEGRADED, APPROVAL_REQUESTED,
    })


# ---------------------------------------------------------------------------
# RunEventBus — per-run fan-out with Last-Event-ID replay (Story 2.6 / AC2)
# ---------------------------------------------------------------------------

_RING_BUFFER_MAX = 1000  # AC1: ring buffer 最近 1000 事件供断线重连补齐


class RunEventBus:
    """Asyncio fan-out event bus with per-run sequence numbers.

    Each run keeps a bounded ring buffer (maxlen=1000); subscribers can
    reconnect from any seq via last_seq (SSE Last-Event-ID reconnection).
    """

    def __init__(self) -> None:
        self._store: Dict[str, Deque[Tuple[int, Any]]] = {}
        self._seq: Dict[str, int] = {}
        self._notifiers: Dict[str, asyncio.Event] = {}
        self._closed: Set[str] = set()

    def publish(self, run_id: str, event: Any) -> int:
        """Append *event* to run's ring buffer; wake subscribers. Returns seq."""
        if run_id not in self._store:
            self._store[run_id] = deque(maxlen=_RING_BUFFER_MAX)
            self._seq[run_id] = 0
        # Never overwrite an existing notifier (subscribe may already be waiting on it)
        if run_id not in self._notifiers:
            self._notifiers[run_id] = asyncio.Event()

        seq = self._seq[run_id]
        self._seq[run_id] = seq + 1
        self._store[run_id].append((seq, event))
        self._notifiers[run_id].set()
        return seq

    def close_run(self, run_id: str) -> None:
        """Signal that no more events will be published for *run_id*."""
        self._closed.add(run_id)
        notifier = self._notifiers.get(run_id)
        if notifier is not None:
            notifier.set()

    async def subscribe(
        self,
        run_id: str,
        last_seq: Optional[int] = None,
    ) -> AsyncIterator[Tuple[int, Any]]:
        """Yield (seq, event) from *run_id*, starting after *last_seq*.

        Blocks until new events arrive. Terminates after close_run().
        """
        cursor: int = 0 if last_seq is None else last_seq + 1

        while True:
            buffer = self._store.get(run_id, [])
            pending = [(s, e) for s, e in buffer if s >= cursor]

            if pending:
                for seq, evt in pending:
                    cursor = seq + 1
                    yield seq, evt
                continue

            if run_id in self._closed:
                return

            notifier = self._notifiers.get(run_id)
            if notifier is None:
                self._notifiers[run_id] = asyncio.Event()
                notifier = self._notifiers[run_id]

            notifier.clear()
            buffer = self._store.get(run_id, [])
            if any(s >= cursor for s, _ in buffer):
                continue
            if run_id in self._closed:
                return

            await notifier.wait()

    def publish_node_event(
        self,
        run_id: str,
        event_type: str,
        node_id: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Convenience wrapper: publish a node lifecycle event dict (Story 4.1 AC2)."""
        evt = {
            "type": event_type,
            "run_id": run_id,
            "node_id": node_id,
            **(payload or {}),
        }
        return self.publish(run_id, evt)

    def purge_run(self, run_id: str) -> None:
        """Remove all state for a finished run to free memory."""
        self._store.pop(run_id, None)
        self._seq.pop(run_id, None)
        self._notifiers.pop(run_id, None)
        self._closed.discard(run_id)

    def get_events(self, run_id: str, from_seq: int = 0) -> List[Tuple[int, Any]]:
        """Return buffered (seq, event) pairs for replay / testing."""
        return [(s, e) for s, e in self._store.get(run_id, []) if s >= from_seq]

    def latest_seq(self, run_id: str) -> Optional[int]:
        buf = self._store.get(run_id)
        return buf[-1][0] if buf else None


# ---------------------------------------------------------------------------
# SSE formatting helpers
# ---------------------------------------------------------------------------

_SSE_REDACT_KEYS = frozenset({
    "private_key", "api_key", "password", "authorization",
    "secret", "secret_key", "access_token", "refresh_token",
    "token", "credential", "credentials",
})


def _redact_sse_payload(obj: Any) -> Any:
    """Recursively redact sensitive keys from SSE payloads before sending to clients."""
    if isinstance(obj, dict):
        return {
            k: "[REDACTED]" if k.lower() in _SSE_REDACT_KEYS else _redact_sse_payload(v)
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [_redact_sse_payload(item) for item in obj]
    return obj


def format_sse_event(seq: int, event: Any) -> str:
    """Encode an event as an SSE chunk (id / event / data lines).

    Accepts Pydantic models (AgentEvent) or plain dicts (node lifecycle events).
    """
    if isinstance(event, dict):
        event_type = event.get("type", "message")
        payload = event
    else:
        event_type = getattr(event, "type", "message")
        payload = event.model_dump(mode="json", exclude_none=True) if hasattr(event, "model_dump") else {}
    data = json.dumps(_redact_sse_payload(payload), ensure_ascii=False)
    return f"id: {seq}\nevent: {event_type}\ndata: {data}\n\n"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PolicyViolationEvent(BaseModel):
    type: str = POLICY_VIOLATION
    sender: str
    receiver: str
    reason: str
    node_id: str
    timestamp: datetime = Field(default_factory=_utc_now)


class GapChoice(BaseModel):
    id: str
    label: str
    action: str


class AgentGapDetectedEvent(BaseModel):
    type: Literal["agent.gap_detected"] = GAP_DETECTED
    run_id: str
    node_id: str
    gap_type: str
    description: str
    choices: List[GapChoice]
    timestamp: datetime = Field(default_factory=_utc_now)
