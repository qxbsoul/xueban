"""Approvals API — Story 7.7 (Inbox ApprovalGatePanel) — thin FastAPI shim.

This module is now a thin wrapper. All business logic lives in
xueban.runtime.approval.service. The wrapper only owns:
  - the FastAPI router and route decorators
  - request/response Pydantic models
  - HTTP error translation (service exceptions -> HTTPException)

Endpoints (unchanged):
  GET  /api/groups/{group_id}/approvals/pending  — list pending approvals for a group
  POST /api/approvals/{approval_id}/approve      — approve an approval gate
  POST /api/approvals/{approval_id}/reject       — reject via true rejection flow (Story 1.3)
  GET  /api/approvals/events                     — SSE stream of approval lifecycle events

Backward-compat re-exports (used by server.py and tests/test_approvals_api.py):
  _approval_registry, _reverse_registry  — module-state dicts (same instances)
  _get_or_create_approval_id              — registry helper
  set_runtime_service                     — runtime singleton setter
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from xueban.runtime.approval import service as _service
# Re-export module-level state and helpers so legacy callers / tests
# (e.g. tests/test_approvals_api.py) keep working unchanged.
from xueban.runtime.approval.service import (  # noqa: F401
    _approval_registry,
    _reverse_registry,
    _get_or_create_approval_id,
    set_runtime_service,
)

logger = logging.getLogger("xueban.api.approvals")

router = APIRouter(tags=["approvals"])


# ---------------------------------------------------------------------------
# Response models (signatures preserved verbatim)
# ---------------------------------------------------------------------------


class PendingApprovalItem(BaseModel):
    approval_id: str
    run_id: str
    gate_id: str
    submitter_name: str
    submitter_kind: str
    summary: str
    triggered_at: str
    waiting_seconds: int


class PendingApprovalsResponse(BaseModel):
    items: List[PendingApprovalItem]


class ApproveResponse(BaseModel):
    status: str
    run_id: str
    gate_id: str


class RejectRequest(BaseModel):
    reason: Optional[str] = ""


class RejectResponse(BaseModel):
    status: str
    run_id: str
    gate_id: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/api/groups/{group_id}/approvals/pending", response_model=PendingApprovalsResponse)
async def get_group_pending_approvals(group_id: str) -> PendingApprovalsResponse:
    """Return pending ApprovalGate items for a group."""
    items = [PendingApprovalItem(**item) for item in _service.list_pending_for_group(group_id)]
    return PendingApprovalsResponse(items=items)


@router.post("/api/approvals/{approval_id}/approve", response_model=ApproveResponse)
async def approve_approval(approval_id: str) -> ApproveResponse:
    """Approve a pending approval gate."""
    try:
        run_id, gate_id = _service.approve(approval_id)
    except _service.RuntimeUnavailableError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except _service.ApprovalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ApproveResponse(status="approved", run_id=run_id, gate_id=gate_id)


@router.post("/api/approvals/{approval_id}/reject", response_model=RejectResponse)
async def reject_approval(approval_id: str, body: RejectRequest) -> RejectResponse:
    """Reject a pending approval gate via the true rejection flow (Story 1.3)."""
    try:
        run_id, gate_id = await _service.reject(approval_id, body.reason or "")
    except _service.RuntimeUnavailableError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except _service.ApprovalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except _service.RejectionChainError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return RejectResponse(status="rejected", run_id=run_id, gate_id=gate_id)


@router.get("/api/approvals/events")
async def stream_approval_events(request: Request) -> StreamingResponse:
    """SSE stream of approval lifecycle events and run lifecycle events.

    Events emitted:
      approval.pending   — new approval gate awaiting decision
      approval.resolved  — approval gate resolved (includes decision / reason)
      run.started        — run entered active status (running / paused)
      run.completed      — run left active status
    """
    generate = _service.make_event_stream(request)
    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
