"""Workspace CRUD API — Story 12.4

Endpoints:
  POST   /api/workspaces                — create Workspace
  GET    /api/workspaces                — list all (with agent_count, team_count)
  GET    /api/workspaces/{workspace_id} — get single workspace
  PATCH  /api/workspaces/{workspace_id} — update name / color
  DELETE /api/workspaces/{workspace_id} — delete workspace

All success responses use {data, meta} envelope.
Errors raise HTTP exceptions with {error: {code, message}} body.

No default workspace (2026-06-01, A design): GET /api/workspaces returns the
existing list verbatim — empty when none exist. There is NO auto-created
"我的工作区"; the frontend treats "no workspace selected" as the Xueban root
(shows no agents). Agents/teams always belong to an explicit workspace, and
creation forces a workspace choice (existing/new) rather than silently
defaulting (which previously orphaned agents under workspace_id="default").
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Storage paths
# ---------------------------------------------------------------------------

_WORKSPACES_DIR = Path(__file__).resolve().parents[2] / ".xueban" / "workspaces"
_AGENTS_DIR = Path(__file__).resolve().parents[2] / ".xueban" / "agents"
_TEAMS_DIR = Path(__file__).resolve().parents[2] / ".xueban" / "teams"
_WS_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _validate_id(workspace_id: str) -> None:
    if not _WS_ID_RE.match(workspace_id):
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "INVALID_WORKSPACE_ID", "message": "Invalid workspace_id format"}},
        )


def _workspaces_dir() -> Path:
    _WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    return _WORKSPACES_DIR


def _ws_path(workspace_id: str) -> Path:
    root = _workspaces_dir().resolve()
    resolved = (root / f"{workspace_id}.json").resolve()
    if root not in resolved.parents:
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "INVALID_WORKSPACE_ID", "message": "Invalid workspace_id"}},
        )
    return resolved


def _save_workspace(record: Dict[str, Any]) -> None:
    target = _ws_path(record["workspace_id"])
    content = json.dumps(record, default=str).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-")
    try:
        os.write(fd, content)
        os.fsync(fd)
        os.close(fd)
        os.replace(tmp_name, str(target))
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            Path(tmp_name).unlink()
        except OSError:
            pass
        raise


def _load_workspace(workspace_id: str) -> Optional[Dict[str, Any]]:
    p = _ws_path(workspace_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _list_workspaces_raw() -> List[Dict[str, Any]]:
    d = _workspaces_dir()
    records: List[Dict[str, Any]] = []
    for p in sorted(d.glob("*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
            records.append(rec)
        except (json.JSONDecodeError, OSError):
            pass
    return records


def _delete_workspace_file(workspace_id: str) -> None:
    p = _ws_path(workspace_id)
    if p.exists():
        p.unlink()


# ---------------------------------------------------------------------------
# Count helpers (agent_count / team_count)
# ---------------------------------------------------------------------------

def _count_for_workspace(directory: Path, workspace_id: str) -> int:
    """Count JSON files in directory where workspace_id field matches."""
    if not directory.exists():
        return 0
    count = 0
    for p in directory.glob("*.json"):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
            if rec.get("workspace_id") == workspace_id:
                count += 1
        except (json.JSONDecodeError, OSError):
            pass
    return count


def _enrich(record: Dict[str, Any]) -> Dict[str, Any]:
    """Add agent_count and team_count to a workspace record."""
    ws_id = record["workspace_id"]
    return {
        **record,
        "agent_count": _count_for_workspace(_AGENTS_DIR, ws_id),
        "team_count": _count_for_workspace(_TEAMS_DIR, ws_id),
    }


# ---------------------------------------------------------------------------
# Default workspace creation (AC6)
# ---------------------------------------------------------------------------

def _create_default_workspace() -> Dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    workspace_id = f"ws-{uuid4().hex[:12]}"
    record: Dict[str, Any] = {
        "workspace_id": workspace_id,
        "name": "我的工作区",
        "color": "#6366f1",
        "owner_id": "local",
        "created_at": now,
        "updated_at": now,
    }
    _save_workspace(record)
    return record


# ---------------------------------------------------------------------------
# Router + helpers
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


def _envelope(data: Any, **meta: Any) -> Dict[str, Any]:
    return {"data": data, "meta": meta}


def _not_found(workspace_id: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "error": {
                "code": "WORKSPACE_NOT_FOUND",
                "message": f"Workspace {workspace_id!r} not found",
            }
        },
    )


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class CreateWorkspaceRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    color: str = Field("#6366f1", max_length=20)
    owner_id: str = Field("local", max_length=80)


class PatchWorkspaceRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    color: Optional[str] = Field(None, max_length=20)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("", status_code=201)
async def create_workspace(body: CreateWorkspaceRequest) -> Dict[str, Any]:
    """Create a new Workspace."""
    now = datetime.now(timezone.utc).isoformat()
    workspace_id = f"ws-{uuid4().hex[:12]}"
    record: Dict[str, Any] = {
        "workspace_id": workspace_id,
        "name": body.name,
        "color": body.color,
        "owner_id": body.owner_id,
        "created_at": now,
        "updated_at": now,
    }
    _save_workspace(record)
    return _envelope(_enrich(record), created=True)


@router.get("")
async def list_workspaces() -> Dict[str, Any]:
    """List all Workspaces. Returns empty list when none exist."""
    records = _list_workspaces_raw()
    enriched = [_enrich(r) for r in records]
    return _envelope(enriched, total=len(enriched))


@router.get("/{workspace_id}")
async def get_workspace(workspace_id: str) -> Dict[str, Any]:
    """Get a single Workspace by ID."""
    _validate_id(workspace_id)
    record = _load_workspace(workspace_id)
    if record is None:
        raise _not_found(workspace_id)
    return _envelope(_enrich(record))


@router.patch("/{workspace_id}")
async def patch_workspace(workspace_id: str, body: PatchWorkspaceRequest) -> Dict[str, Any]:
    """Update workspace name and/or color."""
    _validate_id(workspace_id)
    record = _load_workspace(workspace_id)
    if record is None:
        raise _not_found(workspace_id)

    if body.name is not None:
        record["name"] = body.name
    if body.color is not None:
        record["color"] = body.color

    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_workspace(record)
    return _envelope(_enrich(record))


@router.delete("/{workspace_id}", status_code=204)
async def delete_workspace(workspace_id: str) -> None:
    """Delete a Workspace."""
    _validate_id(workspace_id)
    record = _load_workspace(workspace_id)
    if record is None:
        raise _not_found(workspace_id)
    _delete_workspace_file(workspace_id)
