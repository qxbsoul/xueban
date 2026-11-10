"""A2A Bridge — Inbound + Outbound A2A protocol adapter for Xueban.

Only mounted when env var A2A_BRIDGE_ENABLED=true.

Inbound (external A2A → Xueban):
  GET  /.well-known/agent.json        — AgentCard discovery
  POST /a2a/                           — JSON-RPC 2.0 dispatch endpoint
        methods: a2a_sendMessage       — receive task, return task object
                 a2a_getTask           — poll task status
                 a2a_cancelTask        — cancel a task

Outbound (Xueban → external A2A):
  A2AClient.send_message()             — call any remote A2A agent

Wire protocol: JSON-RPC 2.0 over HTTP (no SDK dependency).
A2A spec reference: https://a2a-protocol.org/latest/specification/
License: Apache 2.0 (compatible with Xueban MIT).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feature flag — entire router is a no-op when disabled
# ---------------------------------------------------------------------------

A2A_BRIDGE_ENABLED: bool = os.getenv("A2A_BRIDGE_ENABLED", "").lower() in ("1", "true", "yes")

router = APIRouter(tags=["a2a-bridge"])


# ---------------------------------------------------------------------------
# A2A data models (hand-rolled, no SDK)
# ---------------------------------------------------------------------------


class A2ASkill(BaseModel):
    """Subset of AgentSkill required by A2A spec discovery."""

    id: str
    name: str
    description: str
    tags: List[str] = Field(default_factory=list)
    inputModes: List[str] = Field(default_factory=lambda: ["text"])
    outputModes: List[str] = Field(default_factory=lambda: ["text"])


class A2ACapabilities(BaseModel):
    streaming: bool = False
    pushNotifications: bool = False
    stateTransitionHistory: bool = False


class A2AProvider(BaseModel):
    organization: str
    url: str = ""


class A2ASecurityScheme(BaseModel):
    type: str  # "apiKey" | "http" | "oauth2" | "openIdConnect"


class AgentCard(BaseModel):
    """A2A AgentCard — served at /.well-known/agent.json."""

    protocolVersion: str = "1.0"
    name: str
    description: str
    url: str                                   # canonical base URL of this agent
    provider: A2AProvider
    version: str
    capabilities: A2ACapabilities
    skills: List[A2ASkill]
    defaultInputModes: List[str] = Field(default_factory=lambda: ["text"])
    defaultOutputModes: List[str] = Field(default_factory=lambda: ["text"])
    # Optional — only include when auth is required
    securitySchemes: Optional[Dict[str, A2ASecurityScheme]] = None
    security: Optional[List[Dict[str, List[str]]]] = None


class A2APart(BaseModel):
    """Content part inside an A2A Message."""

    type: str = "text"           # "text" | "file" | "data"
    text: Optional[str] = None
    data: Optional[Any] = None


class A2AMessage(BaseModel):
    """A2A Message envelope."""

    messageId: str = Field(default_factory=lambda: uuid4().hex)
    role: str = "user"           # "user" | "agent"
    parts: List[A2APart]
    contextId: Optional[str] = None
    taskId: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class A2ATaskStatus(BaseModel):
    state: str                   # submitted | working | completed | failed | canceled
    message: Optional[A2AMessage] = None
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class A2ATask(BaseModel):
    """A2A Task object — returned from tasks/send."""

    id: str
    contextId: Optional[str] = None
    status: A2ATaskStatus
    artifacts: List[Dict[str, Any]] = Field(default_factory=list)
    history: List[A2AMessage] = Field(default_factory=list)
    metadata: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# In-memory task store (ephemeral — no persistence required for MVP)
# ---------------------------------------------------------------------------

_tasks: Dict[str, A2ATask] = {}


def _new_task(message: A2AMessage, context_id: Optional[str] = None) -> A2ATask:
    task_id = f"a2a-{uuid4().hex[:16]}"
    task = A2ATask(
        id=task_id,
        contextId=context_id or message.contextId,
        status=A2ATaskStatus(state="submitted", message=message),
        history=[message],
    )
    _tasks[task_id] = task
    return task


def _get_task(task_id: str) -> Optional[A2ATask]:
    return _tasks.get(task_id)


# ---------------------------------------------------------------------------
# AgentCard builder — reads env vars so it never hardcodes a URL
# ---------------------------------------------------------------------------

_SF_BASE_URL = os.getenv("SF_BASE_URL", "http://localhost:8000")
_SF_ORG = os.getenv("SF_ORG_NAME", "Xueban")
_SF_ORG_URL = os.getenv("SF_ORG_URL", "https://github.com/Ravenveil/Xueban")
_SF_VERSION = os.getenv("SF_VERSION", "0.3.0")

# Whether inbound A2A requests require an API key
_A2A_REQUIRE_AUTH: bool = os.getenv("A2A_REQUIRE_AUTH", "").lower() in ("1", "true", "yes")
_A2A_API_KEY: str = os.getenv("A2A_API_KEY", "")


def _build_agent_card() -> AgentCard:
    """Construct the AgentCard from current environment config."""
    skills = [
        A2ASkill(
            id="workflow-run",
            name="Run Workflow",
            description="Execute a multi-agent Xueban workflow given a YAML definition.",
            tags=["orchestration", "workflow", "multi-agent"],
        ),
        A2ASkill(
            id="chat",
            name="Chat Session",
            description="Engage in a multi-turn conversation with a Xueban-managed agent.",
            tags=["chat", "conversation"],
        ),
        A2ASkill(
            id="acp-relay",
            name="ACP Relay",
            description="Relay tasks to ACP-connected external agents registered in Xueban.",
            tags=["acp", "relay", "bridge"],
        ),
    ]

    card = AgentCard(
        name="Xueban",
        description=(
            "Multi-agent workflow orchestration platform with ACP/A2A bridging. "
            "Accepts tasks, runs them through configurable agent teams, and returns results."
        ),
        url=f"{_SF_BASE_URL}/a2a/",
        provider=A2AProvider(organization=_SF_ORG, url=_SF_ORG_URL),
        version=_SF_VERSION,
        capabilities=A2ACapabilities(streaming=False, pushNotifications=False),
        skills=skills,
    )

    if _A2A_REQUIRE_AUTH and _A2A_API_KEY:
        card.securitySchemes = {
            "apiKey": A2ASecurityScheme(type="apiKey"),
        }
        card.security = [{"apiKey": []}]

    return card


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 helpers
# ---------------------------------------------------------------------------


def _rpc_ok(request_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_err(request_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": err}


# ---------------------------------------------------------------------------
# Inbound auth guard
# ---------------------------------------------------------------------------


def _check_inbound_auth(request: Request) -> bool:
    """Return True if the request passes auth. Always True when auth disabled."""
    if not _A2A_REQUIRE_AUTH:
        return True
    if not _A2A_API_KEY:
        logger.warning("A2A_REQUIRE_AUTH=true but A2A_API_KEY is not set; blocking request")
        return False
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:] == _A2A_API_KEY
    api_key_header = request.headers.get("X-API-Key", "")
    return api_key_header == _A2A_API_KEY


# ---------------------------------------------------------------------------
# ACP integration — forward A2A task into Xueban internal ACP pipeline
# ---------------------------------------------------------------------------


async def _forward_to_acp(task: A2ATask) -> None:
    """Best-effort: mark task 'working', then try to dispatch via ACP manager.

    If the ACP system is not reachable or has no available agents, task stays
    'working' and caller must poll. For MVP we set completed immediately for
    the echo-back case.  Real implementations should wire to RuntimeService.
    """
    task.status.state = "working"

    # Extract plain text from first message part
    first_message = task.history[0] if task.history else None
    instruction = ""
    if first_message:
        for part in first_message.parts:
            if part.type == "text" and part.text:
                instruction = part.text
                break

    if not instruction:
        task.status = A2ATaskStatus(state="failed")
        task.status.message = A2AMessage(
            role="agent",
            parts=[A2APart(type="text", text="No text instruction found in message.")],
        )
        return

    # Try to find any available ACP session and forward
    try:
        from xueban.runtime.acp.server import get_manager as _acp_mgr
        from xueban.runtime.acp.registry import get_registry as _acp_reg

        manager = _acp_mgr()
        registry = _acp_reg()
        online = await registry.list_online()

        if online:
            # Pick the first available agent and forward
            candidate = online[0]
            # Find a session for this agent — scan manager sessions
            session_id = None
            for sid, aid in manager._session_agents.items():
                if aid == candidate.manifest.agent_id:
                    session_id = sid
                    break

            if session_id:
                dispatched = await manager.send_task(
                    session_id=session_id,
                    task_id=task.id,
                    instruction=instruction,
                    context={"a2a_task_id": task.id, "a2a_context_id": task.contextId},
                )
                if dispatched:
                    logger.info("A2A bridge: forwarded task_id=%s to ACP session=%s", task.id, session_id)
                    return  # task remains 'working' — ACP agent will complete async

        # No ACP agent available — echo back as completed (demo / smoke-test path)
        _complete_task_echo(task, instruction)

    except Exception as exc:
        logger.warning("A2A bridge: ACP forward failed for task_id=%s: %s", task.id, exc)
        _complete_task_echo(task, instruction)


def _complete_task_echo(task: A2ATask, instruction: str) -> None:
    """Fallback: immediately complete with an echo artifact."""
    reply = A2AMessage(
        role="agent",
        parts=[A2APart(
            type="text",
            text=f"[Xueban A2A bridge] Received: {instruction!r}. "
                 "No ACP agent available — echo mode.",
        )],
        taskId=task.id,
        contextId=task.contextId,
    )
    task.status = A2ATaskStatus(state="completed", message=reply)
    task.artifacts = [{"type": "text", "content": reply.parts[0].text}]
    task.history.append(reply)


# ---------------------------------------------------------------------------
# JSON-RPC method handlers
# ---------------------------------------------------------------------------


async def _handle_send_message(
    request_id: Any, params: Dict[str, Any]
) -> Dict[str, Any]:
    """a2a_sendMessage — create a task and begin processing."""
    raw_msg = params.get("message")
    if not raw_msg:
        return _rpc_err(request_id, -32602, "params.message is required")

    try:
        message = A2AMessage.model_validate(raw_msg)
    except Exception as exc:
        return _rpc_err(request_id, -32602, f"Invalid message format: {exc}")

    task = _new_task(message, context_id=params.get("contextId"))
    logger.info("A2A bridge: new task task_id=%s message_id=%s", task.id, message.messageId)

    # Attempt ACP forwarding (non-blocking result update)
    await _forward_to_acp(task)

    return _rpc_ok(request_id, task.model_dump(mode="json"))


async def _handle_get_task(
    request_id: Any, params: Dict[str, Any]
) -> Dict[str, Any]:
    """a2a_getTask — return current task status."""
    task_id = params.get("taskId") or params.get("id")
    if not task_id:
        return _rpc_err(request_id, -32602, "params.taskId is required")

    task = _get_task(str(task_id))
    if task is None:
        return _rpc_err(request_id, -32001, f"Task not found: {task_id}", data={"taskId": task_id})

    return _rpc_ok(request_id, task.model_dump(mode="json"))


async def _handle_cancel_task(
    request_id: Any, params: Dict[str, Any]
) -> Dict[str, Any]:
    """a2a_cancelTask — cancel a pending/working task."""
    task_id = params.get("taskId") or params.get("id")
    if not task_id:
        return _rpc_err(request_id, -32602, "params.taskId is required")

    task = _get_task(str(task_id))
    if task is None:
        return _rpc_err(request_id, -32001, f"Task not found: {task_id}", data={"taskId": task_id})

    if task.status.state in ("completed", "failed", "canceled"):
        return _rpc_err(
            request_id, -32002,
            f"Task is already in terminal state: {task.status.state}",
            data={"taskId": task_id, "state": task.status.state},
        )

    task.status = A2ATaskStatus(state="canceled")
    logger.info("A2A bridge: task_id=%s canceled", task_id)
    return _rpc_ok(request_id, task.model_dump(mode="json"))


# Method dispatch table
_METHOD_HANDLERS = {
    "a2a_sendMessage": _handle_send_message,
    "a2a_getTask": _handle_get_task,
    "a2a_cancelTask": _handle_cancel_task,
    # Legacy names (some clients use these)
    "tasks/send": _handle_send_message,
    "tasks/get": _handle_get_task,
    "tasks/cancel": _handle_cancel_task,
}


# ---------------------------------------------------------------------------
# FastAPI endpoints
# ---------------------------------------------------------------------------


@router.get("/.well-known/agent.json", include_in_schema=A2A_BRIDGE_ENABLED)
async def get_agent_card() -> JSONResponse:
    """A2A discovery endpoint — returns this agent's AgentCard."""
    if not A2A_BRIDGE_ENABLED:
        return JSONResponse({"error": "A2A bridge is disabled"}, status_code=404)
    card = _build_agent_card()
    return JSONResponse(card.model_dump(mode="json", exclude_none=True))


@router.post("/a2a/", include_in_schema=A2A_BRIDGE_ENABLED)
async def a2a_jsonrpc(request: Request) -> JSONResponse:
    """JSON-RPC 2.0 endpoint for A2A protocol methods.

    Accepts: application/json with {jsonrpc, method, params, id}
    Returns: JSON-RPC 2.0 response envelope
    """
    if not A2A_BRIDGE_ENABLED:
        return JSONResponse({"error": "A2A bridge is disabled"}, status_code=404)

    # Auth guard
    if not _check_inbound_auth(request):
        return JSONResponse(
            _rpc_err(None, -32001, "Unauthorized"),
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Parse JSON-RPC envelope
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            _rpc_err(None, -32700, "Parse error: request body must be JSON"),
            status_code=400,
        )

    if not isinstance(body, dict):
        return JSONResponse(
            _rpc_err(None, -32600, "Invalid Request: body must be a JSON object"),
            status_code=400,
        )

    request_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params") or {}

    if body.get("jsonrpc") != "2.0":
        return JSONResponse(
            _rpc_err(request_id, -32600, "Invalid Request: jsonrpc must be '2.0'"),
            status_code=400,
        )

    if not method:
        return JSONResponse(
            _rpc_err(request_id, -32600, "Invalid Request: method is required"),
            status_code=400,
        )

    handler = _METHOD_HANDLERS.get(method)
    if handler is None:
        return JSONResponse(
            _rpc_err(request_id, -32601, f"Method not found: {method}"),
            status_code=404,
        )

    if not isinstance(params, dict):
        return JSONResponse(
            _rpc_err(request_id, -32602, "Invalid params: must be an object"),
            status_code=400,
        )

    try:
        result = await handler(request_id, params)
    except Exception as exc:
        logger.exception("A2A bridge: unhandled error in method=%s", method)
        return JSONResponse(
            _rpc_err(request_id, -32603, f"Internal error: {exc}"),
            status_code=500,
        )

    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Outbound A2A client — call a remote A2A agent
# ---------------------------------------------------------------------------


class A2AClientError(Exception):
    """Raised when an outbound A2A call fails."""


class A2AClient:
    """Minimal outbound A2A client (JSON-RPC 2.0 over HTTP).

    Usage::

        client = A2AClient("https://remote-agent.example.com/a2a/", api_key="...")
        task = await client.send_message("Summarize this document: ...")
        print(task["status"]["state"])

    The api_key is sent as 'Authorization: Bearer <key>' if provided.
    Store keys in .env / environment — never hardcode.
    """

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        timeout: float = 60.0,
    ) -> None:
        self._base_url = base_url.rstrip("/") + "/"
        self._api_key = api_key
        self._timeout = timeout

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    async def _call(self, method: str, params: Dict[str, Any]) -> Any:
        """Send a JSON-RPC 2.0 request and return the 'result' field."""
        rpc_id = uuid4().hex[:12]
        payload = {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            try:
                resp = await http.post(self._base_url, json=payload, headers=self._headers())
            except httpx.RequestError as exc:
                raise A2AClientError(f"HTTP request failed: {exc}") from exc

        if resp.status_code not in (200, 201):
            raise A2AClientError(
                f"Remote returned HTTP {resp.status_code}: {resp.text[:200]}"
            )

        try:
            data = resp.json()
        except Exception as exc:
            raise A2AClientError(f"JSON decode failed: {exc}") from exc

        if "error" in data:
            err = data["error"]
            raise A2AClientError(
                f"JSON-RPC error {err.get('code')}: {err.get('message')}"
            )

        return data.get("result")

    async def fetch_agent_card(self) -> Dict[str, Any]:
        """Fetch the AgentCard from /.well-known/agent.json on the remote agent."""
        well_known_url = self._base_url.replace("/a2a/", "").rstrip("/") + "/.well-known/agent.json"
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            try:
                resp = await http.get(well_known_url, headers=self._headers())
            except httpx.RequestError as exc:
                raise A2AClientError(f"HTTP request failed: {exc}") from exc
        if resp.status_code != 200:
            raise A2AClientError(f"AgentCard fetch failed HTTP {resp.status_code}")
        return resp.json()

    async def send_message(
        self,
        text: str,
        context_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Send a text message to the remote A2A agent and return the Task object."""
        message = {
            "messageId": uuid4().hex,
            "role": "user",
            "parts": [{"type": "text", "text": text}],
        }
        if context_id:
            message["contextId"] = context_id
        if metadata:
            message["metadata"] = metadata

        params: Dict[str, Any] = {"message": message}
        if context_id:
            params["contextId"] = context_id

        return await self._call("a2a_sendMessage", params)

    async def get_task(self, task_id: str) -> Dict[str, Any]:
        """Poll task status from remote agent."""
        return await self._call("a2a_getTask", {"taskId": task_id})

    async def cancel_task(self, task_id: str) -> Dict[str, Any]:
        """Request cancellation of a remote task."""
        return await self._call("a2a_cancelTask", {"taskId": task_id})
