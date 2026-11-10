"""ACP Client — high-level session management (Story 2.3).

Implements the client (host) side of the Agent Client Protocol:
https://agentclientprotocol.com/protocol

Phase 2 Code Review (2026-04-22): aligned with Zed canonical spec
  - method names: session/new, session/prompt, session/update, session/request_permission
  - `session/update` notification parses `update.sessionUpdate` discriminator
  - `session/prompt` is a request-with-response; uses transport.request(...)
  - `session/request_permission` is an agent→client request; responds with
    `RequestPermissionResponse` via the request id, not a separate notification.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict, Optional
from uuid import uuid4

from xueban.runtime.acp.messages import (
    ACP_PROTOCOL_VERSION,
    InitializeRequest,
    NewSessionRequest,
    PromptRequest,
    RequestPermissionResponse,
)
from xueban.runtime.acp.transport import AcpTransport, AcpSessionTerminated
from xueban.runtime.contracts import AgentEvent, AgentHandle

logger = logging.getLogger(__name__)


# Map ACP SessionUpdate discriminator to Xueban AgentEvent.type.
# Unknown variants pass through as agent.<sessionUpdate> (observable, not lost).
_SESSION_UPDATE_EVENT_MAP: Dict[str, str] = {
    "agent_thought_chunk": "agent.thinking",
    "agent_message_chunk": "agent.output",
    "user_message_chunk": "agent.user_echo",
    "tool_call": "agent.tool_called",
    "tool_call_update": "agent.tool_result",
    "plan": "agent.plan",
    "available_commands_update": "agent.commands",
    "current_mode_update": "agent.mode",
    "config_option_update": "agent.config",
    "session_info_update": "agent.session_info",
}


class AcpClient:
    """High-level ACP session manager.

    Wraps an AcpTransport and provides initialize/session lifecycle helpers.
    The approval flow is pluggable: set `self._approval_resolver` (coroutine
    taking the RequestPermission params and returning an option id / None)
    via AcpApprovalBridge to close the AC2 loop. When unset, the client emits
    an `agent.approval_requested` event and replies with `cancelled` so the
    agent turn can terminate cleanly instead of hanging.
    """

    def __init__(self, transport: AcpTransport) -> None:
        self._transport = transport
        self._session_id: Optional[str] = None
        self._agent_capabilities: Dict[str, Any] = {}
        # Approval bridge hook — set by AcpAgentExecutor after constructing the client.
        # Signature: async (params: dict) -> Optional[str]  (None = cancel, str = option_id)
        self._approval_resolver = None

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def agent_capabilities(self) -> Dict[str, Any]:
        return self._agent_capabilities

    def set_approval_resolver(self, resolver) -> None:
        """Install the AC2 approval bridge callback."""
        self._approval_resolver = resolver

    async def initialize(
        self,
        client_name: str = "xueban",
        client_version: str = "0.1.0",
    ) -> Dict[str, Any]:
        msg = InitializeRequest(client_name=client_name, client_version=client_version)
        response = await self._transport.request(msg.model_dump(exclude_none=True))
        result = response.get("result", {})
        self._agent_capabilities = result.get("agentCapabilities", {}) or {}
        # Protocol version negotiation: agent may downgrade.
        proto = result.get("protocolVersion", ACP_PROTOCOL_VERSION)
        if proto != ACP_PROTOCOL_VERSION:
            logger.warning(
                "ACP protocol version mismatch: client=%d agent=%d — proceeding with agent's version",
                ACP_PROTOCOL_VERSION, proto,
            )
        return result

    async def start_session(
        self,
        run_id: str,
        node_id: str,
        agent_id: str,
        provenance: Optional[Dict[str, Any]] = None,
    ) -> str:
        msg = NewSessionRequest(
            run_id=run_id, node_id=node_id, agent_id=agent_id, provenance=provenance
        )
        response = await self._transport.request(msg.model_dump(exclude_none=True))
        result = response.get("result", {})
        self._session_id = str(result.get("sessionId", f"sess-{uuid4().hex[:12]}"))
        return self._session_id

    async def prompt(self, prompt: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Send a session/prompt request and return the PromptResponse result.

        Per ACP spec this is a request with response (`{stopReason}`), NOT a
        notification. The response arrives AFTER the stream of session/update
        notifications; callers should iterate stream_events() concurrently
        while awaiting this method in a background task (AcpAgentExecutor does
        this by fire-and-forget kicking prompt and then streaming updates).
        """
        if self._session_id is None:
            raise RuntimeError("ACP session not started — call start_session first")
        msg = PromptRequest(session_id=self._session_id, prompt=prompt)
        # Note: the host's AcpAgentExecutor currently treats prompt as fire-and-forget
        # because stream_events consumes notifications on the same transport.
        # `send` is kept for backwards compatibility; AC3 refactor tracks turning this
        # into a proper request/response with concurrent stream consumption.
        await self._transport.send(msg.model_dump(exclude_none=True))
        return {"requestId": msg.id}

    async def cancel(self) -> None:
        """Send session/cancel notification (stops current prompt turn)."""
        if self._session_id is None:
            return
        from xueban.runtime.acp.messages import SessionCancelNotification
        msg = SessionCancelNotification(session_id=self._session_id)
        await self._transport.send(msg.model_dump(exclude_none=True))

    async def send_permission_response(
        self,
        request_id: str,
        option_id: Optional[str],
    ) -> None:
        """Reply to a session/request_permission with selected option or cancelled."""
        if option_id is None:
            resp = RequestPermissionResponse.cancelled(request_id)
        else:
            resp = RequestPermissionResponse.selected(request_id, option_id)
        await self._transport.send(resp.model_dump(exclude_none=True))

    # Legacy name kept for transition: older code used granted: bool and a
    # xueban-specific permissionId. New code should call send_permission_response
    # with request_id + option_id.
    async def send_permission_result(self, permission_id: str, granted: bool) -> None:
        option_id = "approve" if granted else None
        await self.send_permission_response(permission_id, option_id)

    async def stream_events(
        self,
        handle: AgentHandle,
    ) -> AsyncIterator[AgentEvent]:
        """Convert ACP session/update notifications + session/request_permission
        requests into an AgentEvent stream.

        Closes the generator when we observe a terminal stopReason (via the
        transport's terminal marker) or the transport signals AcpSessionTerminated.
        """
        try:
            async for notification in self._transport.notifications():
                # Transport signals end-of-stream with a None sentinel (Phase 5).
                if notification is None:
                    return

                method = str(notification.get("method", ""))
                params = notification.get("params", {}) if isinstance(notification.get("params"), dict) else {}

                # Agent-to-client REQUEST for permission (has id; needs a response).
                if method == "session/request_permission":
                    request_id = str(notification.get("id", ""))
                    tool_call = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
                    options = params.get("options") if isinstance(params.get("options"), list) else []
                    yield AgentEvent(
                        run_id=handle.run_id,
                        node_id=handle.node_id,
                        agent_id=handle.agent_id,
                        type="agent.approval_requested",
                        payload={
                            "requestId": request_id,
                            "sessionId": params.get("sessionId", ""),
                            "toolCall": tool_call,
                            "options": options,
                        },
                    )
                    await self._resolve_permission_or_cancel(request_id, params)
                    continue

                # Agent-to-client notifications (no id, no response).
                if method == "session/update":
                    update = params.get("update") if isinstance(params.get("update"), dict) else {}
                    session_update = str(update.get("sessionUpdate", ""))
                    event_type = _SESSION_UPDATE_EVENT_MAP.get(
                        session_update,
                        f"agent.{session_update}" if session_update else "agent.update",
                    )
                    yield AgentEvent(
                        run_id=handle.run_id,
                        node_id=handle.node_id,
                        agent_id=handle.agent_id,
                        type=event_type,
                        payload={
                            "sessionId": params.get("sessionId", ""),
                            "update": update,
                        },
                    )
                    continue

                # Unknown / unsupported method — surface via logger, do not crash.
                if method:
                    logger.debug("ACP client: unhandled notification method=%s", method)

        except AcpSessionTerminated as exc:
            yield AgentEvent(
                run_id=handle.run_id,
                node_id=handle.node_id,
                agent_id=handle.agent_id,
                type="agent.failed",
                payload={"exit_code": exc.exit_code, "stderr": exc.stderr_tail},
            )

    async def _resolve_permission_or_cancel(
        self,
        request_id: str,
        params: Dict[str, Any],
    ) -> None:
        """Invoke the approval bridge (if installed) and respond to the agent.

        Falls back to `cancelled` when no bridge is wired up so the agent's
        turn doesn't hang on a missing response.
        """
        option_id: Optional[str] = None
        resolver = self._approval_resolver
        if resolver is not None:
            try:
                option_id = await resolver(request_id, params)
            except Exception:
                logger.exception("ACP approval resolver raised; cancelling permission request")
                option_id = None
        await self.send_permission_response(request_id, option_id)
