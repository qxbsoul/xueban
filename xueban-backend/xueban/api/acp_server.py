"""ACP Server API — Story 2.11.

Exposes:
  WebSocket  /acp                — main ACP Server endpoint for external agents
  GET        /api/acp/status     — server health + connected agent count
  GET        /api/acp/sessions   — list active sessions (admin)
  POST       /api/acp/task       — dispatch task to a connected agent session

External agents connect to ws://localhost:8765/acp (or wss:// in production),
send auth + capability_response, then receive task messages and send back streams.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel

from xueban.runtime.acp.registry import get_registry
from xueban.runtime.acp.server import AgentSession, get_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["acp-server"])

# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


@router.websocket("/acp")
async def acp_endpoint(websocket: WebSocket) -> None:
    """Main ACP Server WebSocket endpoint.

    Query params (optional):
      api_key      — agent API key (can also be sent in first auth message)
      workspace_id — target workspace
    """
    manager = get_manager()
    registry = get_registry()

    await websocket.accept()
    logger.info("ACP: new connection from %s", websocket.client)

    # Step 1: authenticate
    session = await manager.authenticate(websocket)
    if session is None:
        await websocket.close(code=4001)
        return

    # Step 2: capability handshake
    try:
        manifest = await manager.handshake(websocket, session)
    except Exception as exc:
        logger.warning("ACP: handshake failed for session=%s: %s", session.session_id, exc)
        await websocket.close(code=4002)
        return

    # Step 3: register in AgentRegistry
    await registry.register(manifest, is_native=False)

    logger.info(
        "ACP: agent registered — agent_id=%s display=%s tools=%s",
        manifest.agent_id,
        manifest.display_name,
        [t.name for t in manifest.tools],
    )

    # Step 4: run session loop (heartbeats + task results)
    try:
        await manager.handle_session(websocket, session)
    except WebSocketDisconnect:
        pass
    finally:
        await registry.mark_offline(manifest.agent_id)
        logger.info("ACP: agent_id=%s disconnected", manifest.agent_id)


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------


@router.get("/api/acp/status")
async def acp_status() -> Dict[str, Any]:
    """Health check — returns server status and connected agent count."""
    manager = get_manager()
    registry = get_registry()
    online_entries = await registry.list_online()
    return {
        "server": "running",
        "connected_agents": manager.connected_count,
        "online_agents": len(online_entries),
        "online_agent_ids": [e.manifest.agent_id for e in online_entries],
    }


@router.get("/api/acp/sessions")
async def list_sessions() -> Dict[str, Any]:
    """List active ACP sessions (for admin/debug)."""
    manager = get_manager()
    registry = get_registry()
    entries = await registry.list_online()
    return {
        "data": [
            {
                "agent_id": e.manifest.agent_id,
                "display_name": e.manifest.display_name,
                "status": e.status,
                "active_tasks": e.active_tasks,
                "connected_at": e.connected_at.isoformat(),
                "last_heartbeat": e.last_heartbeat.isoformat(),
            }
            for e in entries
        ],
        "meta": {"total": len(entries)},
    }


class DispatchTaskRequest(BaseModel):
    session_id: str
    instruction: str
    context: Optional[Dict[str, Any]] = None
    timeout_seconds: int = 300


@router.post("/api/acp/task")
async def dispatch_task(body: DispatchTaskRequest) -> Dict[str, Any]:
    """Dispatch a task to an active agent session (for Orchestrator integration)."""
    from uuid import uuid4
    manager = get_manager()
    task_id = f"task-{uuid4().hex[:12]}"
    ok = await manager.send_task(
        session_id=body.session_id,
        task_id=task_id,
        instruction=body.instruction,
        context=body.context,
        timeout_seconds=body.timeout_seconds,
    )
    if not ok:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "SESSION_NOT_FOUND", "message": f"Session '{body.session_id}' not active"}},
        )
    return {"data": {"task_id": task_id, "session_id": body.session_id, "status": "dispatched"}}
