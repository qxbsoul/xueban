"""Story 2.4 — McpAgentExecutor dispatch + stream_events 测试。"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xueban.runtime.contracts import AgentCapabilities, AgentEvent, AgentHandle, AgentTask
from xueban.runtime.errors import McpError
from xueban.runtime.executors import McpAgentExecutor, ExecutorRegistry
from xueban.runtime.mcp.transport import McpTransportConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task(metadata: Dict[str, Any] | None = None) -> AgentTask:
    return AgentTask(
        task_id="t1", run_id="r1", node_id="n1", agent_id="a1",
        payload={"prompt": "analyse data"},
        metadata=metadata or {"server": "stdio://hermes mcp serve", "tool": "run_agent"},
    )


class _StubMcpClient:
    """Minimal mock implementing McpClient interface."""

    def __init__(self, tools: List[str] | None = None, result: Any = "done") -> None:
        self._tools = tools or ["run_agent"]
        self._result = result
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def list_tools(self) -> List[str]:
        return self._tools

    async def call_tool(self, name: str, args: Dict[str, Any]) -> Any:
        return self._result

    async def close(self) -> None:
        self.closed = True


class _CrashOnCallClient(_StubMcpClient):
    async def call_tool(self, name: str, args: Dict[str, Any]) -> Any:
        raise McpError(code="MCP_TOOL_ERROR", detail="execution exploded")


def _executor_with_stub_client(stub: _StubMcpClient) -> McpAgentExecutor:
    exc = McpAgentExecutor(provider="hermes", default_server="stdio://hermes mcp serve")
    exc._make_client = lambda config: stub  # type: ignore[method-assign]
    return exc


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

class TestMcpAgentExecutorCapabilities:
    def test_capabilities(self):
        exc = McpAgentExecutor("hermes", "stdio://hermes mcp serve")
        caps = exc.capabilities()
        assert caps.streaming is False
        assert caps.approval_required is False
        assert caps.session_resume is False
        assert caps.tool_calls is True

    def test_kind_is_mcp(self):
        exc = McpAgentExecutor("generic")
        assert exc.kind == "mcp"

    def test_provider_stored(self):
        exc = McpAgentExecutor("hermes", "stdio://hermes mcp serve")
        assert exc.provider == "hermes"


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

class TestMcpAgentExecutorDispatch:
    @pytest.mark.asyncio
    async def test_dispatch_returns_handle(self):
        stub = _StubMcpClient()
        exc = _executor_with_stub_client(stub)
        handle = await exc.dispatch(_make_task())
        assert isinstance(handle, AgentHandle)
        assert handle.run_id == "r1"
        assert handle.status == "running"

    @pytest.mark.asyncio
    async def test_dispatch_connects_client(self):
        stub = _StubMcpClient()
        exc = _executor_with_stub_client(stub)
        await exc.dispatch(_make_task())
        assert stub.connected is True

    @pytest.mark.asyncio
    async def test_dispatch_stores_client_in_session_registry(self):
        """Chunk B review 2026-04-22: live client lives in executor._sessions
        keyed by handle_id, not in AgentHandle.metadata (which must stay JSON-serializable).
        """
        stub = _StubMcpClient()
        exc = _executor_with_stub_client(stub)
        handle = await exc.dispatch(_make_task())
        assert exc._sessions[handle.handle_id] is stub
        assert handle.metadata["tool_name"] == "run_agent"
        # metadata must NOT contain the live client object
        assert "_mcp_client" not in handle.metadata

    @pytest.mark.asyncio
    async def test_dispatch_tool_not_found_raises_mcp_error(self):
        stub = _StubMcpClient(tools=["other_tool"])
        exc = _executor_with_stub_client(stub)
        with pytest.raises(McpError) as exc_info:
            await exc.dispatch(_make_task({"server": "stdio://hermes mcp serve", "tool": "run_agent"}))
        assert exc_info.value.code == "MCP_TOOL_NOT_FOUND"
        assert stub.closed is True  # client closed on error

    @pytest.mark.asyncio
    async def test_dispatch_no_server_raises_mcp_error(self):
        exc = McpAgentExecutor("generic")  # no default_server
        with pytest.raises(McpError) as exc_info:
            await exc.dispatch(_make_task({"tool": "run_agent"}))
        assert exc_info.value.code == "MCP_SERVER_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_dispatch_bad_uri_raises_mcp_error(self):
        stub = _StubMcpClient()
        exc = _executor_with_stub_client(stub)
        with pytest.raises(McpError) as exc_info:
            await exc.dispatch(_make_task({"server": "grpc://bad", "tool": "run_agent"}))
        assert exc_info.value.code == "MCP_SERVER_UNAVAILABLE"


# ---------------------------------------------------------------------------
# stream_events
# ---------------------------------------------------------------------------

class TestMcpAgentExecutorStreamEvents:
    def _handle_with_session(
        self, executor: McpAgentExecutor, client: Any, tool_name: str = "run_agent",
    ) -> AgentHandle:
        """Build a handle and inject its matching client into the executor's session registry."""
        handle = AgentHandle(
            run_id="r1", node_id="n1", agent_id="a1", status="running",
            metadata={
                "tool_name": tool_name,
                "args": {"prompt": "hello"},
                "provider": executor.provider,
            },
        )
        executor._sessions[handle.handle_id] = client
        return handle

    @pytest.mark.asyncio
    async def test_stream_events_emits_tool_called_then_completed(self):
        stub = _StubMcpClient(result={"answer": "42"})
        exc = McpAgentExecutor("hermes")
        events = [e async for e in exc.stream_events(self._handle_with_session(exc, stub))]
        types = [e.type for e in events]
        assert "agent.tool_called" in types
        assert "agent.tool_result" in types
        assert "agent.completed" in types

    @pytest.mark.asyncio
    async def test_stream_events_tool_called_payload(self):
        stub = _StubMcpClient()
        exc = McpAgentExecutor("hermes")
        events = [e async for e in exc.stream_events(self._handle_with_session(exc, stub))]
        called = next(e for e in events if e.type == "agent.tool_called")
        assert called.payload["tool"] == "run_agent"
        assert called.payload["args"] == {"prompt": "hello"}

    @pytest.mark.asyncio
    async def test_stream_events_closes_client(self):
        stub = _StubMcpClient()
        exc = McpAgentExecutor("hermes")
        handle = self._handle_with_session(exc, stub)
        [e async for e in exc.stream_events(handle)]
        assert stub.closed is True
        # And the registry entry is cleaned up
        assert handle.handle_id not in exc._sessions

    @pytest.mark.asyncio
    async def test_stream_events_no_session_emits_failed(self):
        """Chunk B review 2026-04-22: handle without matching session → agent.failed."""
        exc = McpAgentExecutor("hermes")
        handle = AgentHandle(
            run_id="r1", node_id="n1", agent_id="a1", status="running",
            metadata={"tool_name": "run_agent", "args": {}, "provider": "hermes"},
        )
        events = [e async for e in exc.stream_events(handle)]
        assert len(events) == 1
        assert events[0].type == "agent.failed"
        assert "no MCP session" in events[0].payload["error"]

    @pytest.mark.asyncio
    async def test_stream_events_tool_error_emits_failed(self):
        stub = _CrashOnCallClient()
        exc = McpAgentExecutor("hermes")
        events = [e async for e in exc.stream_events(self._handle_with_session(exc, stub))]
        failed = next(e for e in events if e.type == "agent.failed")
        assert failed.payload["code"] == "MCP_TOOL_ERROR"


# ---------------------------------------------------------------------------
# ExecutorRegistry auto-registration
# ---------------------------------------------------------------------------

class TestMcpExecutorRegistration:
    def test_generic_mcp_auto_registered(self):
        reg = ExecutorRegistry()
        pairs = reg.list_agent_executors()
        assert ("mcp", "generic") in pairs

    def test_hermes_mcp_auto_registered(self):
        reg = ExecutorRegistry()
        assert ("mcp", "hermes") in reg.list_agent_executors()
