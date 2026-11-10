"""Agent State API — Story 9.4 AC3.

Endpoints:
  GET    /state/{agent_id}                    — get current AgentState
  PATCH  /state/{agent_id}                    — update state_fields (optimistic lock)
  POST   /state/{agent_id}/snapshot           — create snapshot
  GET    /state/{agent_id}/snapshots          — list snapshots
  POST   /state/{agent_id}/restore/{snapshot_id} — restore from snapshot
  POST   /state/{agent_id}/reset              — reset state
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from xueban.runtime.state_service import (
    AgentState,
    SnapshotNotFound,
    StateConflict,
    StateService,
    get_service,
    set_service,
)
from xueban.runtime.errors import XuebanError

router = APIRouter(tags=["state"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class PatchStateRequest(BaseModel):
    version: int
    role_profile_ref: Optional[str] = None
    memory_profile_ref: Optional[str] = None
    state_fields: Optional[Dict[str, Any]] = None
    session_summary: Optional[str] = None
    recent_artifacts: Optional[List[str]] = None
    pending_tasks: Optional[List[str]] = None


def _envelope(data: Any, **meta: Any) -> Dict[str, Any]:
    return {"data": data, "meta": meta}


def _state_to_dict(state: AgentState) -> Dict[str, Any]:
    return state.model_dump(mode="json")


def _handle_conflict(exc: StateConflict) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"error": {
            "code": "STATE_CONFLICT",
            "message": exc.message,
            "details": {"expected": exc.expected, "got": exc.got},
        }},
    )


def _handle_xueban(exc: XuebanError) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/state/{agent_id}")
async def get_agent_state(agent_id: str) -> Dict[str, Any]:
    svc = get_service()
    try:
        state = svc.get_state(agent_id)
    except XuebanError as exc:
        raise _handle_xueban(exc)
    if state is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "AGENT_STATE_NOT_FOUND", "message": f"No state for agent {agent_id!r}"}},
        )
    return _envelope(_state_to_dict(state))


@router.patch("/state/{agent_id}")
async def patch_agent_state(agent_id: str, body: PatchStateRequest) -> Dict[str, Any]:
    svc = get_service()
    patch: Dict[str, Any] = {"version": body.version}
    if body.role_profile_ref is not None:
        patch["role_profile_ref"] = body.role_profile_ref
    if body.memory_profile_ref is not None:
        patch["memory_profile_ref"] = body.memory_profile_ref
    if body.state_fields is not None:
        patch["state_fields"] = body.state_fields
    if body.session_summary is not None:
        patch["session_summary"] = body.session_summary
    if body.recent_artifacts is not None:
        patch["recent_artifacts"] = body.recent_artifacts
    if body.pending_tasks is not None:
        patch["pending_tasks"] = body.pending_tasks

    try:
        state = svc.update_state(agent_id, patch)
    except StateConflict as exc:
        raise _handle_conflict(exc)
    except XuebanError as exc:
        raise _handle_xueban(exc)
    return _envelope(_state_to_dict(state))


@router.post("/state/{agent_id}/snapshot")
async def create_snapshot(agent_id: str) -> Dict[str, Any]:
    svc = get_service()
    try:
        snapshot_id = svc.snapshot_state(agent_id)
    except XuebanError as exc:
        raise _handle_xueban(exc)
    return _envelope({"snapshot_id": snapshot_id})


@router.get("/state/{agent_id}/snapshots")
async def list_snapshots(agent_id: str) -> Dict[str, Any]:
    svc = get_service()
    try:
        snapshots = svc.list_snapshots(agent_id)
    except XuebanError as exc:
        raise _handle_xueban(exc)
    return _envelope(snapshots)


@router.post("/state/{agent_id}/restore/{snapshot_id}")
async def restore_snapshot(agent_id: str, snapshot_id: str) -> Dict[str, Any]:
    svc = get_service()
    try:
        state = svc.restore_state(agent_id, snapshot_id)
    except SnapshotNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
        )
    except XuebanError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
        )
    return _envelope(_state_to_dict(state))


@router.post("/state/{agent_id}/reset")
async def reset_agent_state(agent_id: str) -> Dict[str, Any]:
    svc = get_service()
    try:
        state = svc.reset_state(agent_id)
    except XuebanError as exc:
        raise _handle_xueban(exc)
    return _envelope(_state_to_dict(state))
