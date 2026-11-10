"""ACP JSON-RPC message models (Story 2.3).

Based on the Agent Client Protocol spec:
https://github.com/zed-industries/agent-client-protocol
(schema/meta.json + schema/schema.json on the `main` branch)

Method names use slashes per ACP spec:
- initialize
- session/new, session/prompt, session/cancel, session/load
- session/update (agent → client notification)
- session/request_permission (agent → client request)

Phase 2 Code Review (2026-04-22): methods migrated from legacy dotted form
(session.new / session.prompt / session.update / session.requestPermission) to
slash form to match Zed's canonical ACP spec. Payloads adjusted for:
  - InitializeRequest: adds protocolVersion, renames capabilities → clientCapabilities
  - NewSessionRequest: uses spec fields cwd + mcpServers + _meta (xueban refs)
  - PromptRequest: prompt is now a ContentBlock array, not a plain string
  - SessionUpdate notification: payload now matches spec's {sessionId, update:{sessionUpdate, ...}}
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


# Latest ACP protocol version targeted by this implementation.
# See https://agentclientprotocol.com/protocol/protocol-version.
ACP_PROTOCOL_VERSION = 1


def _new_id() -> str:
    # Full 32-char hex for negligible collision risk (see Review Finding — msg-id entropy).
    return f"acp-{uuid4().hex}"


class AcpMessage(BaseModel):
    """Base JSON-RPC 2.0 envelope."""

    jsonrpc: str = "2.0"


class AcpRequest(AcpMessage):
    id: str = Field(default_factory=_new_id)
    method: str
    params: Dict[str, Any] = Field(default_factory=dict)


class AcpNotification(AcpMessage):
    """JSON-RPC notification — no id, no response expected."""

    method: str
    params: Dict[str, Any] = Field(default_factory=dict)


class AcpResponse(AcpMessage):
    id: str
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None


# ---------- Concrete message types ----------

class InitializeRequest(AcpRequest):
    """`initialize` method — first call, must be before session.*.

    Spec: params = {protocolVersion, clientCapabilities, clientInfo}
    """

    method: str = "initialize"

    def __init__(
        self,
        client_name: str = "xueban",
        client_version: str = "0.1.0",
        protocol_version: int = ACP_PROTOCOL_VERSION,
        client_capabilities: Optional[Dict[str, Any]] = None,
        **kwargs,
    ):
        # Minimal client capabilities; agent's response reports what it actually supports.
        caps = client_capabilities if client_capabilities is not None else {
            "fs": {"readTextFile": False, "writeTextFile": False},
            "terminal": False,
        }
        params = {
            "protocolVersion": protocol_version,
            "clientInfo": {"name": client_name, "version": client_version},
            "clientCapabilities": caps,
        }
        super().__init__(params=params, **kwargs)


class InitializeResponse(AcpResponse):
    pass


class NewSessionRequest(AcpRequest):
    """`session/new` — spec params: {cwd, mcpServers}.

    Xueban run/node/agent identifiers are stashed in `_meta` which ACP spec
    explicitly reserves for client-attached metadata.
    """

    method: str = "session/new"

    def __init__(
        self,
        run_id: str,
        node_id: str,
        agent_id: str,
        cwd: Optional[str] = None,
        mcp_servers: Optional[List[Dict[str, Any]]] = None,
        provenance: Optional[Dict[str, Any]] = None,
        **kwargs,
    ):
        import os

        # _meta.xueban carries the full dispatch lineage (adoption #2,
        # Hermes-provenance convention): run/node/agent plus the enclosing
        # loop (loop_id / iteration / depth / parent) when dispatched from
        # inside a control.loop body. Derived per call, never persisted —
        # standard ACP clients can safely ignore it.
        meta: Dict[str, Any] = {
            "runId": run_id,
            "nodeId": node_id,
            "agentId": agent_id,
        }
        if isinstance(provenance, dict):
            loop_info = provenance.get("loop")
            if isinstance(loop_info, dict):
                meta["loopId"] = loop_info.get("loop_id")
                meta["iteration"] = loop_info.get("iteration")
                meta["loopDepth"] = loop_info.get("depth")
                meta["parentLoopId"] = loop_info.get("parent_loop_id")

        params: Dict[str, Any] = {
            "cwd": cwd or os.getcwd(),
            "mcpServers": mcp_servers or [],
            "_meta": {"xueban": meta},
        }
        super().__init__(params=params, **kwargs)


# Backwards-compatible alias: legacy code / tests reference SessionNewRequest.
SessionNewRequest = NewSessionRequest


class PromptRequest(AcpRequest):
    """`session/prompt` — params: {sessionId, prompt: [ContentBlock]}.

    A plain-text prompt is wrapped in a single `{type: "text", text: ...}` block.
    The response carries `{stopReason}`.
    """

    method: str = "session/prompt"

    def __init__(
        self,
        session_id: str,
        prompt: str | List[Dict[str, Any]],
        **kwargs,
    ):
        if isinstance(prompt, str):
            blocks: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        else:
            blocks = prompt
        params = {
            "sessionId": session_id,
            "prompt": blocks,
        }
        super().__init__(params=params, **kwargs)


# Backwards-compatible alias.
SessionPromptRequest = PromptRequest


class SessionUpdateNotification(AcpNotification):
    """`session/update` — streamed from agent to client.

    Spec: params = {sessionId, update: {sessionUpdate: <variant>, ...}}
    where sessionUpdate ∈ {
      user_message_chunk, agent_message_chunk, agent_thought_chunk,
      tool_call, tool_call_update, plan, available_commands_update,
      current_mode_update, config_option_update, session_info_update,
    }
    """

    method: str = "session/update"

    @classmethod
    def thinking(cls, session_id: str, text: str) -> "SessionUpdateNotification":
        return cls(params={
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": text},
            },
        })

    @classmethod
    def agent_message(cls, session_id: str, text: str) -> "SessionUpdateNotification":
        return cls(params={
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            },
        })

    @classmethod
    def tool_call(
        cls,
        session_id: str,
        tool_call_id: str,
        title: str,
        kind: str = "other",
        **fields: Any,
    ) -> "SessionUpdateNotification":
        update: Dict[str, Any] = {
            "sessionUpdate": "tool_call",
            "toolCallId": tool_call_id,
            "title": title,
            "kind": kind,
        }
        update.update(fields)
        return cls(params={"sessionId": session_id, "update": update})


class RequestPermissionRequest(AcpRequest):
    """`session/request_permission` — agent → client request (spec requires response).

    params = {sessionId, options: [PermissionOption], toolCall: ToolCall}
    response = {outcome: {outcome: "cancelled"} | {outcome: "selected", optionId}}
    """

    method: str = "session/request_permission"

    @property
    def permission_id(self) -> str:
        # ACP spec 2026-04: there is no top-level permissionId; clients should
        # correlate via the request id. We expose the request id for parity.
        return self.id

    @property
    def session_id(self) -> str:
        return str(self.params.get("sessionId", ""))

    @property
    def tool_call(self) -> Dict[str, Any]:
        val = self.params.get("toolCall")
        return val if isinstance(val, dict) else {}

    @property
    def options(self) -> List[Dict[str, Any]]:
        val = self.params.get("options", [])
        return val if isinstance(val, list) else []


# Backwards-compatible alias: legacy code referenced this as a "Notification",
# but per ACP spec it's actually a request with response.
SessionRequestPermissionNotification = RequestPermissionRequest


class RequestPermissionResponse(AcpResponse):
    """Host's response to session/request_permission.

    result.outcome is either {outcome: "cancelled"} or {outcome: "selected", optionId}.
    """

    @classmethod
    def cancelled(cls, request_id: str) -> "RequestPermissionResponse":
        return cls(id=request_id, result={"outcome": {"outcome": "cancelled"}})

    @classmethod
    def selected(cls, request_id: str, option_id: str) -> "RequestPermissionResponse":
        return cls(
            id=request_id,
            result={"outcome": {"outcome": "selected", "optionId": option_id}},
        )


class SessionCancelNotification(AcpNotification):
    """`session/cancel` — client → agent notification to stop the current turn."""

    method: str = "session/cancel"

    def __init__(self, session_id: str, **kwargs):
        super().__init__(params={"sessionId": session_id}, **kwargs)


# ---------- Legacy names kept for transition ----------
# Only for backwards compatibility with pre-Phase-2 callers; prefer the
# canonical names above. These will be removed after all call sites migrate.
SessionPermissionResultRequest = RequestPermissionResponse  # legacy name
