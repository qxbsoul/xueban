"""Story 2.3 — AcpClient session lifecycle 测试(mock transport)。

Phase 2 Code Review (2026-04-22): 测试已更新到 Zed ACP canonical spec
  - method names 用斜杠(session/new, session/prompt, session/update,
    session/request_permission)
  - `session/update` 的 params 是 `{sessionId, update: {sessionUpdate, ...}}`
  - `session/request_permission` 带 id(是 request,不是 notification)
  - `session/prompt.params.prompt` 是 ContentBlock 数组
  - InitializeRequest 带 protocolVersion + clientCapabilities
  - stream_events 结束条件改为 transport 的 None sentinel(Phase 5)
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List

import pytest

from xueban.runtime.acp.client import AcpClient
from xueban.runtime.acp.transport import AcpSessionTerminated
from xueban.runtime.contracts import AgentHandle


# ---------------------------------------------------------------------------
# Mock transport
# ---------------------------------------------------------------------------

class MockTransport:
    """Minimal mock implementing the AcpTransport interface."""

    def __init__(
        self,
        responses: Dict[str, Dict[str, Any]] | None = None,
        notifications: List[Dict[str, Any]] | None = None,
    ) -> None:
        self._responses = responses or {}
        self._notifications = list(notifications or [])
        self.sent: List[Dict[str, Any]] = []

    async def request(self, msg: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
        self.sent.append(msg)
        method = msg.get("method", "")
        if method in self._responses:
            return self._responses[method]
        return {"jsonrpc": "2.0", "id": msg.get("id", "x"), "result": {}}

    async def send(self, msg: Dict[str, Any]) -> None:
        self.sent.append(msg)

    async def notifications(self) -> AsyncIterator[Dict[str, Any]]:
        for notification in self._notifications:
            yield notification
        # Phase 5 — transport signals end-of-stream with None sentinel so
        # stream_events can exit cleanly on agent-clean-shutdown (no more updates).
        yield None  # type: ignore[misc]

    async def stop(self) -> None:
        pass


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------

class TestAcpClientInitialize:
    @pytest.mark.asyncio
    async def test_initialize_sends_initialize_request(self):
        transport = MockTransport(responses={
            "initialize": {"jsonrpc": "2.0", "id": "1", "result": {"agentCapabilities": {}}}
        })
        client = AcpClient(transport)
        await client.initialize()
        assert len(transport.sent) == 1
        assert transport.sent[0]["method"] == "initialize"

    @pytest.mark.asyncio
    async def test_initialize_payload_has_protocol_version_and_client_capabilities(self):
        transport = MockTransport(responses={
            "initialize": {"jsonrpc": "2.0", "id": "1", "result": {"agentCapabilities": {}}}
        })
        client = AcpClient(transport)
        await client.initialize()
        params = transport.sent[0]["params"]
        # Zed ACP spec required fields
        assert params["protocolVersion"] == 1
        assert "clientInfo" in params and params["clientInfo"]["name"] == "xueban"
        assert "clientCapabilities" in params
        # Legacy field name should NOT appear
        assert "capabilities" not in params

    @pytest.mark.asyncio
    async def test_initialize_stores_agent_capabilities(self):
        transport = MockTransport(responses={
            "initialize": {"jsonrpc": "2.0", "id": "1",
                           "result": {"agentCapabilities": {"loadSession": True}}}
        })
        client = AcpClient(transport)
        await client.initialize()
        assert client.agent_capabilities.get("loadSession") is True


# ---------------------------------------------------------------------------
# start_session
# ---------------------------------------------------------------------------

class TestAcpClientStartSession:
    @pytest.mark.asyncio
    async def test_start_session_sends_session_new_with_slash(self):
        transport = MockTransport(responses={
            "initialize": {"jsonrpc": "2.0", "id": "1", "result": {}},
            "session/new": {"jsonrpc": "2.0", "id": "2", "result": {"sessionId": "sess-abc"}},
        })
        client = AcpClient(transport)
        await client.initialize()
        session_id = await client.start_session("run-1", "node-1", "agent-1")
        assert session_id == "sess-abc"
        assert client.session_id == "sess-abc"
        new_msg = next(m for m in transport.sent if m["method"] == "session/new")
        # Spec fields present
        assert "cwd" in new_msg["params"]
        assert "mcpServers" in new_msg["params"]

    @pytest.mark.asyncio
    async def test_start_session_stashes_xueban_refs_in_meta(self):
        transport = MockTransport(responses={
            "session/new": {"jsonrpc": "2.0", "id": "2", "result": {"sessionId": "sess-1"}},
        })
        client = AcpClient(transport)
        await client.start_session("my-run", "my-node", "my-agent")
        new_msg = next(m for m in transport.sent if m["method"] == "session/new")
        meta = new_msg["params"]["_meta"]["xueban"]
        assert meta["runId"] == "my-run"
        assert meta["nodeId"] == "my-node"
        assert meta["agentId"] == "my-agent"


# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------

class TestAcpClientPrompt:
    @pytest.mark.asyncio
    async def test_prompt_sends_session_prompt_with_content_blocks(self):
        transport = MockTransport(responses={
            "session/new": {"jsonrpc": "2.0", "id": "2", "result": {"sessionId": "sess-1"}},
        })
        client = AcpClient(transport)
        await client.start_session("r", "n", "a")
        await client.prompt("Write an essay")
        prompt_msg = next(m for m in transport.sent if m["method"] == "session/prompt")
        # Spec: prompt is a ContentBlock array
        assert isinstance(prompt_msg["params"]["prompt"], list)
        assert prompt_msg["params"]["prompt"][0]["type"] == "text"
        assert prompt_msg["params"]["prompt"][0]["text"] == "Write an essay"
        assert prompt_msg["params"]["sessionId"] == "sess-1"

    @pytest.mark.asyncio
    async def test_prompt_without_session_raises(self):
        transport = MockTransport()
        client = AcpClient(transport)
        with pytest.raises(RuntimeError, match="not started"):
            await client.prompt("hello")


# ---------------------------------------------------------------------------
# stream_events — discriminator mapping from session/update variants
# ---------------------------------------------------------------------------

class TestAcpClientStreamEvents:
    def _make_handle(self) -> AgentHandle:
        return AgentHandle(
            run_id="r1", node_id="n1", agent_id="a1", status="running",
            metadata={"provider": "hermes"},
        )

    @pytest.mark.asyncio
    async def test_agent_thought_chunk_maps_to_agent_thinking(self):
        notifications = [
            {
                "method": "session/update",
                "params": {
                    "sessionId": "s1",
                    "update": {
                        "sessionUpdate": "agent_thought_chunk",
                        "content": {"type": "text", "text": "thinking..."},
                    },
                },
            },
        ]
        transport = MockTransport(notifications=notifications)
        client = AcpClient(transport)
        events = [e async for e in client.stream_events(self._make_handle())]
        assert len(events) == 1
        assert events[0].type == "agent.thinking"
        assert events[0].payload["update"]["content"]["text"] == "thinking..."

    @pytest.mark.asyncio
    async def test_tool_call_variant_maps_to_agent_tool_called(self):
        notifications = [
            {
                "method": "session/update",
                "params": {
                    "sessionId": "s1",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc-1",
                        "title": "Search",
                        "kind": "search",
                    },
                },
            },
        ]
        transport = MockTransport(notifications=notifications)
        client = AcpClient(transport)
        events = [e async for e in client.stream_events(self._make_handle())]
        assert events[0].type == "agent.tool_called"

    @pytest.mark.asyncio
    async def test_agent_message_chunk_maps_to_agent_output(self):
        notifications = [
            {
                "method": "session/update",
                "params": {
                    "sessionId": "s1",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "hello"},
                    },
                },
            },
        ]
        transport = MockTransport(notifications=notifications)
        client = AcpClient(transport)
        events = [e async for e in client.stream_events(self._make_handle())]
        assert events[0].type == "agent.output"

    @pytest.mark.asyncio
    async def test_request_permission_is_request_with_id_and_emits_approval_requested(self):
        """session/request_permission is a REQUEST (has id); client must respond via transport.send."""
        notifications = [
            {
                "jsonrpc": "2.0",
                "id": "req-perm-1",
                "method": "session/request_permission",
                "params": {
                    "sessionId": "s1",
                    "options": [{"optionId": "approve", "name": "Approve", "kind": "allow_once"}],
                    "toolCall": {"title": "Delete file", "kind": "delete", "toolCallId": "tc-9"},
                },
            },
        ]
        transport = MockTransport(notifications=notifications)
        client = AcpClient(transport)
        events = [e async for e in client.stream_events(self._make_handle())]
        assert events[0].type == "agent.approval_requested"
        assert events[0].payload["requestId"] == "req-perm-1"
        assert events[0].payload["toolCall"]["title"] == "Delete file"
        # Without a bridge installed, client must still send a response (cancelled) so
        # the agent doesn't hang — check the outbound message.
        sent_perms = [m for m in transport.sent if m.get("id") == "req-perm-1"]
        assert len(sent_perms) == 1
        assert sent_perms[0]["result"]["outcome"]["outcome"] == "cancelled"

    @pytest.mark.asyncio
    async def test_request_permission_with_bridge_sends_selected_option(self):
        notifications = [
            {
                "jsonrpc": "2.0", "id": "req-42",
                "method": "session/request_permission",
                "params": {
                    "sessionId": "s1",
                    "options": [{"optionId": "approve"}, {"optionId": "deny"}],
                    "toolCall": {},
                },
            },
        ]
        transport = MockTransport(notifications=notifications)
        client = AcpClient(transport)

        async def _resolver(request_id: str, params: Dict[str, Any]):
            return "approve"

        client.set_approval_resolver(_resolver)
        _ = [e async for e in client.stream_events(self._make_handle())]
        sent = next(m for m in transport.sent if m.get("id") == "req-42")
        assert sent["result"]["outcome"]["outcome"] == "selected"
        assert sent["result"]["outcome"]["optionId"] == "approve"

    @pytest.mark.asyncio
    async def test_session_terminated_emits_agent_failed(self):
        class _TerminatingTransport(MockTransport):
            async def notifications(self):
                raise AcpSessionTerminated(exit_code=1, stderr_tail="crash")
                if False:
                    yield

        client = AcpClient(_TerminatingTransport())
        handle = AgentHandle(run_id="r1", node_id="n1", agent_id="a1", status="running",
                             metadata={"provider": "hermes"})
        events = [e async for e in client.stream_events(handle)]
        assert len(events) == 1
        assert events[0].type == "agent.failed"
        assert events[0].payload["exit_code"] == 1
