"""Approval REST business logic (extracted from xueban.api.approvals).

This module holds the framework-agnostic core for the approvals REST surface:
  - In-memory approval registry (approval_id <-> (run_id, gate_id))
  - Runtime service singleton plumbing
  - Approve / reject business operations (event signalling + SSE publish)
  - Group-run resolution helpers
  - SSE generator factory for /api/approvals/events

The FastAPI router in xueban.api.approvals is a thin wrapper that only
handles HTTP concerns (decorators, request parsing, response models) and
delegates everything else here. Module-level state (registries, singleton)
is re-exported by the wrapper for backward compatibility (tests reach into
_approvals_api._approval_registry etc.).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from xueban.runtime.events import _redact_sse_payload

logger = logging.getLogger("xueban.api.approvals")

# ---------------------------------------------------------------------------
# Registry: approval_id → (run_id, node_id) + metadata
# ---------------------------------------------------------------------------

# NOTE: These two dicts are part of the public contract: tests (and the api
# wrapper) re-export them by reference. Do NOT reassign — only mutate in place.
_approval_registry: Dict[str, Dict[str, Any]] = {}  # approval_id → record
_reverse_registry: Dict[Tuple[str, str], str] = {}  # (run_id, node_id) → approval_id


def _get_or_create_approval_id(run_id: str, node_id: str) -> str:
    key = (run_id, node_id)
    if key not in _reverse_registry:
        aid = uuid4().hex[:16]
        _reverse_registry[key] = aid
        _approval_registry[aid] = {
            "run_id": run_id,
            "gate_id": node_id,
            "triggered_at": datetime.now(timezone.utc).isoformat(),
            "submitter_name": node_id,
            "submitter_kind": "acp",
            "summary": "",
        }
    return _reverse_registry[key]


# ---------------------------------------------------------------------------
# Runtime service singleton
# ---------------------------------------------------------------------------

_runtime_service: Any = None


def set_runtime_service(svc: Any) -> None:
    global _runtime_service
    _runtime_service = svc


def _get_runtime_service() -> Any:
    return _runtime_service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _waiting_seconds(triggered_at: str) -> int:
    try:
        t = datetime.fromisoformat(triggered_at.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - t
        return max(0, int(delta.total_seconds()))
    except Exception:
        return 0


def _format_sse(event_type: str, payload: Dict[str, Any]) -> str:
    data = json.dumps(_redact_sse_payload(payload), ensure_ascii=False)
    return f"event: {event_type}\ndata: {data}\n\n"


def _resolve_run_group_id(svc: Any, run_id: str) -> Optional[str]:
    """Return group_id for a given run_id, or None if not determinable."""
    try:
        run_store = getattr(svc, "_run_store", None)
        if run_store is None:
            return None
        runs = run_store.list() if hasattr(run_store, "list") else []
        for run in runs:
            run_dict = run.model_dump() if hasattr(run, "model_dump") else {}
            if run_dict.get("run_id") == run_id:
                return run_dict.get("group_id")
    except Exception:
        return None
    return None


def _resolve_group_run_ids(svc: Any, group_id: str) -> Optional[set]:
    """Return run_ids associated with group_id, or None if group_id not tracked.

    Returns None when the run store is unavailable or when no run record carries
    a `group_id` field (data-model limitation — can't determine group membership).
    Returns an empty set when the field exists but no run belongs to this group
    (correct isolation — show zero approvals for this group).
    """
    try:
        runs = svc.list_runs() if hasattr(svc, "list_runs") else []
        has_group_field = False
        matched: set = set()
        for r in runs:
            run_dict = r.model_dump() if hasattr(r, "model_dump") else {}
            if "group_id" in run_dict:
                has_group_field = True
                if run_dict.get("group_id") == group_id:
                    matched.add(run_dict.get("run_id", ""))
        if not has_group_field:
            return None  # group_id not in data model yet — skip filtering
        return matched
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Business operations
# ---------------------------------------------------------------------------


def list_pending_for_group(group_id: str) -> List[Dict[str, Any]]:
    """Return pending approval items (raw dicts) for a group.

    Returns an empty list when no runtime service is registered. The wrapper
    converts these dicts into PendingApprovalItem Pydantic models.
    """
    svc = _get_runtime_service()
    items: List[Dict[str, Any]] = []

    if svc is None:
        return items

    approval_events: Dict[Tuple[str, str], Any] = getattr(svc, "_approval_events", {})
    group_run_ids: Optional[set] = _resolve_group_run_ids(svc, group_id)

    for (run_id, node_id), evt in list(approval_events.items()):
        if not (hasattr(evt, "is_set") and not evt.is_set()):
            continue
        if group_run_ids is not None and run_id not in group_run_ids:
            continue

        aid = _get_or_create_approval_id(run_id, node_id)
        rec = _approval_registry[aid]

        items.append(
            {
                "approval_id": aid,
                "run_id": run_id,
                "gate_id": node_id,
                "submitter_name": rec.get("submitter_name", node_id),
                "submitter_kind": rec.get("submitter_kind", "acp"),
                "summary": rec.get("summary", ""),
                "triggered_at": rec.get("triggered_at", ""),
                "waiting_seconds": _waiting_seconds(rec.get("triggered_at", "")),
            }
        )

    return items


class ApprovalNotFoundError(Exception):
    """Raised when an approval_id is unknown or already resolved."""


class RuntimeUnavailableError(Exception):
    """Raised when no runtime service has been registered."""


class RejectionChainError(Exception):
    """Raised when RuntimeService.reject() fails with an unexpected exception
    (gate was unblocked but the policy/rejection chain did not fully fire)."""

    def __init__(self, exc_type: str) -> None:
        super().__init__(f"Rejection gate unblocked but policy chain failed: {exc_type}")
        self.exc_type = exc_type


def approve(approval_id: str) -> Tuple[str, str]:
    """Approve a pending approval gate.

    Returns (run_id, gate_id) on success.
    Raises RuntimeUnavailableError or ApprovalNotFoundError as appropriate.
    """
    svc = _get_runtime_service()
    if svc is None:
        raise RuntimeUnavailableError("Runtime service unavailable")

    rec = _approval_registry.get(approval_id)
    if rec is None:
        raise ApprovalNotFoundError(f"Approval not found: {approval_id}")

    run_id = rec["run_id"]
    gate_id = rec["gate_id"]
    key = (run_id, gate_id)

    approval_events: Dict[Tuple[str, str], Any] = getattr(svc, "_approval_events", {})
    approval_decisions: Dict[Tuple[str, str], Any] = getattr(svc, "_approval_decisions", {})

    evt = approval_events.get(key)
    if evt is None or (hasattr(evt, "is_set") and evt.is_set()):
        raise ApprovalNotFoundError(
            f"Approval not found or already resolved: {approval_id}"
        )

    # Signal the waiting _execute_approval_gate coroutine
    approval_decisions[key] = {"decision": "approve", "reason": ""}
    evt.set()

    # Publish SSE event for real-time updates
    event_bus = getattr(svc, "_event_bus", None)
    if event_bus is not None:
        try:
            event_bus.publish(
                run_id,
                {
                    "type": "approval.resolved",
                    "approval_id": approval_id,
                    "run_id": run_id,
                    "gate_id": gate_id,
                    "decision": "approve",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception:
            pass

    logger.info(
        "Approval approved: approval_id=%s run_id=%s gate=%s",
        approval_id, run_id, gate_id,
    )
    return run_id, gate_id


async def reject(approval_id: str, reason: str) -> Tuple[str, str]:
    """Reject a pending approval gate (Story 1.3 真驳回 flow).

    Returns (run_id, gate_id) on success.
    Raises RuntimeUnavailableError, ApprovalNotFoundError, or RejectionChainError.
    """
    svc = _get_runtime_service()
    if svc is None:
        raise RuntimeUnavailableError("Runtime service unavailable")

    rec = _approval_registry.get(approval_id)
    if rec is None:
        raise ApprovalNotFoundError(f"Approval not found: {approval_id}")

    run_id = rec["run_id"]
    gate_id = rec["gate_id"]
    reason = (reason or "").strip()
    key = (run_id, gate_id)

    approval_events: Dict[Tuple[str, str], Any] = getattr(svc, "_approval_events", {})
    approval_decisions: Dict[Tuple[str, str], Any] = getattr(svc, "_approval_decisions", {})

    evt = approval_events.get(key)
    if evt is None or (hasattr(evt, "is_set") and evt.is_set()):
        raise ApprovalNotFoundError(
            f"Approval not found or already resolved: {approval_id}"
        )

    # Step 1: Signal the gate to unblock with 'reject' decision
    approval_decisions[key] = {"decision": "reject", "reason": reason}
    evt.set()

    # Step 2: Trigger true rejection flow via RuntimeService.reject() (Story 1.3)
    try:
        await svc.reject(
            run_id=run_id,
            reviewer_role="human",
            target_node_id=gate_id,
            reason=reason,
        )
    except ValueError as exc:
        # Expected business error (policy violation, unknown run) — gate already unblocked.
        logger.warning(
            "RuntimeService.reject policy/business error: run=%s gate=%s: %s",
            run_id, gate_id, exc,
        )
    except Exception as exc:
        # Unexpected error — full rejection event chain did NOT fire; gate already unblocked.
        logger.critical(
            "RuntimeService.reject UNEXPECTED failure — rejection chain incomplete: "
            "run=%s gate=%s exc=%s: %s",
            run_id, gate_id, type(exc).__name__, exc,
        )
        raise RejectionChainError(type(exc).__name__) from exc

    # Publish SSE resolved event
    event_bus = getattr(svc, "_event_bus", None)
    if event_bus is not None:
        try:
            event_bus.publish(
                run_id,
                {
                    "type": "approval.resolved",
                    "approval_id": approval_id,
                    "run_id": run_id,
                    "gate_id": gate_id,
                    "decision": "reject",
                    "reason": reason,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception:
            pass

    logger.info(
        "Approval rejected: approval_id=%s run_id=%s gate=%s",
        approval_id, run_id, gate_id,
    )
    return run_id, gate_id


# ---------------------------------------------------------------------------
# SSE event stream
# ---------------------------------------------------------------------------

_ACTIVE_RUN_STATUSES = {"running", "paused"}


def make_event_stream(request: Any):
    """Build the SSE async generator used by /api/approvals/events.

    `request` is the Starlette/FastAPI Request; we only call .is_disconnected().
    """
    svc = _get_runtime_service()

    async def generate():
        known_pending: set = set()
        known_active_runs: Dict[str, Optional[str]] = {}  # run_id → group_id
        seq = 0

        while True:
            if await request.is_disconnected():
                break

            if svc is not None:
                # ── approval events ────────────────────────────────────────
                approval_events: Dict[Tuple[str, str], Any] = getattr(svc, "_approval_events", {})
                current = {
                    k for k, v in approval_events.items()
                    if hasattr(v, "is_set") and not v.is_set()
                }

                for key in current - known_pending:
                    run_id, node_id = key
                    aid = _get_or_create_approval_id(run_id, node_id)
                    rec = _approval_registry[aid]
                    group_id = _resolve_run_group_id(svc, run_id)
                    payload = {
                        "type": "approval.pending",
                        "approval_id": aid,
                        "run_id": run_id,
                        "gate_id": node_id,
                        "group_id": group_id,
                        "submitter_name": rec.get("submitter_name", node_id),
                        "submitter_kind": rec.get("submitter_kind", "acp"),
                        "summary": rec.get("summary", ""),
                        "triggered_at": rec.get("triggered_at", ""),
                        "waiting_seconds": _waiting_seconds(rec.get("triggered_at", "")),
                        "seq": seq,
                    }
                    yield _format_sse("approval.pending", payload)
                    seq += 1

                for key in known_pending - current:
                    run_id, node_id = key
                    aid = _reverse_registry.get(key)
                    if aid:
                        group_id = _resolve_run_group_id(svc, run_id)
                        # Include decision/reason so clients can distinguish approve vs reject
                        approval_decisions: Dict[Tuple[str, str], Any] = getattr(
                            svc, "_approval_decisions", {}
                        )
                        decision_rec = approval_decisions.get(key, {})
                        payload = {
                            "type": "approval.resolved",
                            "approval_id": aid,
                            "run_id": run_id,
                            "gate_id": node_id,
                            "group_id": group_id,
                            "decision": decision_rec.get("decision", ""),
                            "reason": decision_rec.get("reason", ""),
                            "seq": seq,
                        }
                        yield _format_sse("approval.resolved", payload)
                        seq += 1
                        # Clean up resolved entries to prevent unbounded memory growth
                        _approval_registry.pop(aid, None)
                        _reverse_registry.pop(key, None)

                known_pending = current

                # ── run lifecycle events ────────────────────────────────────
                try:
                    all_runs = svc.list_runs() if hasattr(svc, "list_runs") else []
                    current_active: Dict[str, Optional[str]] = {}
                    for r in all_runs:
                        run_dict = r.model_dump() if hasattr(r, "model_dump") else {}
                        if run_dict.get("status") in _ACTIVE_RUN_STATUSES:
                            rid = run_dict.get("run_id", "")
                            current_active[rid] = run_dict.get("group_id")  # may be None
                except Exception:
                    current_active = {}

                for run_id, group_id in current_active.items():
                    if run_id not in known_active_runs:
                        yield _format_sse(
                            "run.started",
                            {"type": "run.started", "run_id": run_id, "group_id": group_id, "seq": seq},
                        )
                        seq += 1

                for run_id, group_id in list(known_active_runs.items()):
                    if run_id not in current_active:
                        yield _format_sse(
                            "run.completed",
                            {"type": "run.completed", "run_id": run_id, "group_id": group_id, "seq": seq},
                        )
                        seq += 1

                known_active_runs = current_active

            await asyncio.sleep(1)

    return generate
