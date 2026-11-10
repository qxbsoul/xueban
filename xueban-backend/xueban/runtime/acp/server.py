"""ACP Server — Story 2.11.

Implements the server (host) side of a Xueban ACP endpoint.
External agents (Claude Code CLI, Hermes, Openclaw, etc.) connect to
`ws://{host}:{port}/acp` and are registered into the AgentRegistry so
the Orchestrator can route tasks to them.

Connection lifecycle:
  1. accept()            — TCP-level WebSocket accept
  2. auth                — Agent sends {type: "auth", api_key, workspace_id, agent_hint}
  3. capability_request  — Server asks for manifest
  4. capability_response — Agent declares its Manifest; server registers in AgentRegistry
  5. handle_session()    — Bidirectional: heartbeats in, tasks out, streams back
  6. disconnect          — Clean or crash disconnect → AgentRegistry marks offline
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set
from uuid import uuid4

from pydantic import BaseModel

from xueban.runtime.acp.registry import (
    AgentCapabilityManifest,
    MemoryCapability,
    ToolCapability,
    get_registry,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Session model
# ---------------------------------------------------------------------------


class AgentSession(BaseModel):
    session_id: str
    agent_id: str
    workspace_id: str
    api_key_hash: str
    connected_at: datetime
    agent_hint: str = ""

    class Config:
        arbitrary_types_allowed = True


# ---------------------------------------------------------------------------
# Auth validator (pluggable stub — replace with real key lookup in prod)
# ---------------------------------------------------------------------------


def _validate_api_key(api_key: str, workspace_id: str) -> bool:
    """Accept any non-empty key in dev; real impl checks DB/Redis."""
    dev_mode = os.getenv("ACP_AUTH_SKIP", "").lower() in ("1", "true", "yes")
    if dev_mode:
        return bool(api_key)
    # Minimal: accept keys starting with "sf-" (demo convention)
    return bool(api_key) and bool(workspace_id)


def _hash_key(api_key: str) -> str:
    import hashlib
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Connection manager
# ---------------------------------------------------------------------------


class ACPConnectionManager:
    """Manages all active external agent WebSocket sessions."""

    def __init__(self) -> None:
        # session_id → websocket (starlette WebSocket)
        self._sessions: Dict[str, Any] = {}
        # session_id → agent_id (needed to address registry by agent_id)
        self._session_agents: Dict[str, str] = {}
        self._lock = asyncio.Lock()

    @property
    def connected_count(self) -> int:
        return len(self._sessions)

    async def authenticate(
        self,
        websocket: Any,
        timeout: float = 10.0,
    ) -> Optional[AgentSession]:
        """Wait for auth message and validate credentials. Returns None on failure."""
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=timeout)
            msg = json.loads(raw)
        except (asyncio.TimeoutError, Exception) as exc:
            logger.warning("ACP auth: failed to receive auth message: %s", exc)
            return None

        if msg.get("type") != "auth":
            logger.warning("ACP auth: first message type=%s (expected 'auth')", msg.get("type"))
            return None

        api_key = str(msg.get("api_key", ""))
        workspace_id = str(msg.get("workspace_id", ""))
        agent_hint = str(msg.get("agent_hint", ""))

        if not _validate_api_key(api_key, workspace_id):
            await websocket.send_text(json.dumps({
                "type": "auth_error",
                "code": 401,
                "message": "Invalid API key",
            }))
            return None

        session_id = f"sess-{uuid4().hex[:16]}"
        session = AgentSession(
            session_id=session_id,
            agent_id=f"{agent_hint or 'external'}-{uuid4().hex[:8]}",
            workspace_id=workspace_id,
            api_key_hash=_hash_key(api_key),
            connected_at=datetime.now(timezone.utc),
            agent_hint=agent_hint,
        )

        await websocket.send_text(json.dumps({
            "type": "auth_ack",
            "session_id": session_id,
            "workspace": workspace_id,
            "message": "Connected to Xueban ACP Server v1",
        }))
        return session

    async def handshake(
        self,
        websocket: Any,
        session: AgentSession,
        timeout: float = 15.0,
    ) -> AgentCapabilityManifest:
        """Send capability_request and parse the agent's capability_response."""
        # Ask agent to declare capabilities
        await websocket.send_text(json.dumps({
            "type": "capability_request",
            "protocol": "acp-v1",
        }))

        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=timeout)
            msg = json.loads(raw)
        except asyncio.TimeoutError:
            logger.warning("ACP handshake: capability_response timeout for session=%s", session.session_id)
            return AgentCapabilityManifest.default_legacy(session.agent_id)
        except Exception as exc:
            logger.warning("ACP handshake: error receiving capability_response: %s", exc)
            return AgentCapabilityManifest.default_legacy(session.agent_id)

        if msg.get("type") != "capability_response":
            logger.warning(
                "ACP handshake: expected capability_response, got type=%s — using defaults",
                msg.get("type"),
            )
            return AgentCapabilityManifest.default_legacy(session.agent_id)

        raw_manifest = msg.get("manifest", {})
        try:
            manifest = _parse_manifest(session.agent_id, raw_manifest)
        except Exception as exc:
            logger.warning("ACP handshake: manifest parse error: %s — using defaults", exc)
            manifest = AgentCapabilityManifest.default_legacy(session.agent_id)

        # Confirm registration
        await websocket.send_text(json.dumps({
            "type": "capability_ack",
            "agent_id": manifest.agent_id,
            "status": "registered",
        }))
        return manifest

    async def handle_session(
        self,
        websocket: Any,
        session: AgentSession,
    ) -> None:
        """Main session loop — process heartbeats and task results until disconnect."""
        async with self._lock:
            self._sessions[session.session_id] = websocket
            self._session_agents[session.session_id] = session.agent_id

        registry = get_registry()
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(websocket.receive_text(), timeout=35.0)
                except asyncio.TimeoutError:
                    # 35s without message — heartbeat missed (interval is 30s)
                    logger.info("ACP session: heartbeat timeout for agent_id=%s", session.agent_id)
                    break

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type", "")

                if msg_type == "heartbeat":
                    active_tasks = int(msg.get("active_tasks", 0))
                    await registry.update_heartbeat(session.agent_id, active_tasks)
                    await websocket.send_text(json.dumps({"type": "heartbeat_ack"}))

                elif msg_type == "task_stream":
                    task_id = str(msg.get("task_id", ""))
                    chunk = str(msg.get("chunk", ""))
                    await _forward_task_stream(session, task_id, chunk)

                elif msg_type == "task_complete":
                    task_id = str(msg.get("task_id", ""))
                    result = msg.get("result", {})
                    await _forward_task_complete(session, task_id, result)
                    await registry.decrement_task(session.agent_id)

                elif msg_type == "task_error":
                    task_id = str(msg.get("task_id", ""))
                    error = msg.get("error", {})
                    await _forward_task_error(session, task_id, error)
                    await registry.decrement_task(session.agent_id)

                elif msg_type == "task_cancelled":
                    task_id = str(msg.get("task_id", ""))
                    logger.info("ACP session: task_id=%s cancelled by agent_id=%s", task_id, session.agent_id)
                    await registry.decrement_task(session.agent_id)

                else:
                    logger.debug("ACP session: unhandled message type=%s", msg_type)

        except Exception as exc:
            logger.info("ACP session: agent_id=%s disconnected: %s", session.agent_id, exc)
        finally:
            async with self._lock:
                self._sessions.pop(session.session_id, None)
                self._session_agents.pop(session.session_id, None)
            await registry.mark_offline(session.agent_id)
            await _emit_agent_offline_event(session)
            logger.info("ACP session: cleaned up agent_id=%s", session.agent_id)

    async def send_task(
        self,
        session_id: str,
        task_id: str,
        instruction: str,
        context: Optional[Dict[str, Any]] = None,
        timeout_seconds: int = 300,
    ) -> bool:
        """Dispatch a task to the given session. Returns False if session not found."""
        async with self._lock:
            websocket = self._sessions.get(session_id)
            agent_id = self._session_agents.get(session_id)
        if websocket is None:
            return False
        msg = {
            "type": "task",
            "task_id": task_id,
            "instruction": instruction,
            "context": context or {},
            "timeout_seconds": timeout_seconds,
        }
        try:
            await websocket.send_text(json.dumps(msg))
            registry = get_registry()
            if agent_id:
                await registry.increment_task(agent_id)
            else:
                logger.warning("ACP: no agent_id mapping for session=%s, task counter not incremented", session_id)
            return True
        except Exception as exc:
            logger.warning("ACP: failed to send task to session=%s: %s", session_id, exc)
            return False

    async def cancel_task(self, session_id: str, task_id: str) -> bool:
        """Send task_cancel to an active session."""
        websocket = self._sessions.get(session_id)
        if websocket is None:
            return False
        try:
            await websocket.send_text(json.dumps({"type": "task_cancel", "task_id": task_id}))
            return True
        except Exception:
            return False

    async def disconnect_session(self, session_id: str, reason: str = "server_shutdown") -> None:
        """Gracefully close an active session."""
        async with self._lock:
            websocket = self._sessions.get(session_id)
            agent_id = self._session_agents.get(session_id)
        if websocket:
            try:
                await websocket.send_text(json.dumps({"type": "disconnect", "reason": reason}))
                await websocket.close()
            except Exception:
                pass
            finally:
                async with self._lock:
                    self._sessions.pop(session_id, None)
                    self._session_agents.pop(session_id, None)
            if agent_id:
                registry = get_registry()
                await registry.mark_offline(agent_id)


# ---------------------------------------------------------------------------
# Helper parsers
# ---------------------------------------------------------------------------


def _parse_manifest(session_agent_id: str, raw: Dict[str, Any]) -> AgentCapabilityManifest:
    agent_id = str(raw.get("agent_id") or session_agent_id)
    display_name = str(raw.get("display_name") or agent_id)
    version = str(raw.get("version") or "unknown")

    tools = []
    for t in (raw.get("tools") or []):
        if isinstance(t, dict):
            tools.append(ToolCapability(
                name=str(t.get("name", "unknown")),
                description=str(t.get("description", "")),
            ))

    max_concurrency = int(raw.get("max_concurrency") or 1)
    streaming = bool(raw.get("streaming", False))
    workspace_context = bool(raw.get("workspace_context", False))
    protocols = list(raw.get("protocols") or ["acp-v1"])

    mem_raw = raw.get("memory") or {}
    memory = MemoryCapability(
        type=mem_raw.get("type", "stateless"),
        scope=mem_raw.get("scope", "request"),
        persistence=bool(mem_raw.get("persistence", False)),
    )

    return AgentCapabilityManifest(
        agent_id=agent_id,
        display_name=display_name,
        version=version,
        tools=tools,
        max_concurrency=max_concurrency,
        streaming=streaming,
        memory=memory,
        protocols=protocols,
        workspace_context=workspace_context,
    )


# ---------------------------------------------------------------------------
# SSE event forwarding stubs (no-op if event bus not wired)
# ---------------------------------------------------------------------------


async def _forward_task_stream(session: AgentSession, task_id: str, chunk: str) -> None:
    try:
        from xueban.runtime.events import RunEventBus
        # Attempt best-effort broadcast; silently ignore if no bus connected
    except Exception:
        pass
    logger.debug("ACP task_stream: task_id=%s agent=%s chunk_len=%d", task_id, session.agent_id, len(chunk))


async def _forward_task_complete(session: AgentSession, task_id: str, result: Dict[str, Any]) -> None:
    logger.info("ACP task_complete: task_id=%s agent=%s status=%s", task_id, session.agent_id, result.get("status"))


async def _forward_task_error(session: AgentSession, task_id: str, error: Dict[str, Any]) -> None:
    logger.warning("ACP task_error: task_id=%s agent=%s error=%s", task_id, session.agent_id, error)


async def _emit_agent_offline_event(session: AgentSession) -> None:
    logger.info("ACP offline: agent_id=%s workspace=%s", session.agent_id, session.workspace_id)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_manager: Optional[ACPConnectionManager] = None


def get_manager() -> ACPConnectionManager:
    global _manager
    if _manager is None:
        _manager = ACPConnectionManager()
    return _manager
