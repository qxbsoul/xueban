"""Story 2.4 — McpError 三种错误码触发测试。"""
from __future__ import annotations

import pytest

from xueban.runtime.errors import McpError
from xueban.runtime.executors import McpAgentExecutor
from xueban.runtime.contracts import AgentHandle, AgentTask


# ---------------------------------------------------------------------------
# McpError structure
# ---------------------------------------------------------------------------

class TestMcpErrorStructure:
    def test_code_stored(self):
        exc = McpError(code="MCP_SERVER_UNAVAILABLE", detail="timeout")
        assert exc.code == "MCP_SERVER_UNAVAILABLE"

    def test_detail_stored(self):
        exc = McpError(code="MCP_TOOL_NOT_FOUND", detail="run_agent absent")
        assert exc.detail == "run_agent absent"

    def test_str_includes_code(self):
        exc = McpError(code="MCP_TOOL_ERROR", detail="boom")
        assert "MCP_TOOL_ERROR" in str(exc)

    def test_extra_kwargs_set_as_attributes(self):
        exc = McpError(code="MCP_TOOL_NOT_FOUND", tool="run_agent", available=["search"])
        assert exc.tool == "run_agent"  # type: ignore[attr-defined]
        assert exc.available == ["search"]  # type: ignore[attr-defined]

    def test_empty_detail_allowed(self):
        exc = McpError(code="MCP_SERVER_UNAVAILABLE")
        assert exc.detail == ""


# ---------------------------------------------------------------------------
# MCP_SERVER_UNAVAILABLE — dispatch with bad URI
# ---------------------------------------------------------------------------

class TestMcpServerUnavailable:
    @pytest.mark.asyncio
    async def test_bad_uri_scheme_raises_mcp_server_unavailable(self):
        exc = McpAgentExecutor("test", default_server="")
        task = AgentTask(
            task_id="t1", run_id="r1", node_id="n1", agent_id="a1",
            payload={}, metadata={"server": "ftp://bad-scheme", "tool": "run_agent"},
        )
        with pytest.raises(McpError) as exc_info:
            await exc.dispatch(task)
        assert exc_info.value.code == "MCP_SERVER_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_no_server_raises_mcp_server_unavailable(self):
        exc = McpAgentExecutor("test")  # no default_server
        task = AgentTask(
            task_id="t1", run_id="r1", node_id="n1", agent_id="a1",
            payload={}, metadata={"tool": "run_agent"},
        )
        with pytest.raises(McpError) as exc_info:
            await exc.dispatch(task)
        assert exc_info.value.code == "MCP_SERVER_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_connect_failure_closes_client(self):
        class _FailConnect:
            closed = False

            async def connect(self):
                raise McpError(code="MCP_SERVER_UNAVAILABLE", detail="process refused")

            async def list_tools(self):
                return []

            async def close(self):
                self.closed = True

        stub = _FailConnect()
        exc = McpAgentExecutor("hermes", default_server="stdio://hermes mcp serve")
        exc._make_client = lambda config: stub  # type: ignore[method-assign]
        task = AgentTask(
            task_id="t1", run_id="r1", node_id="n1", agent_id="a1",
            payload={}, metadata={"server": "stdio://hermes mcp serve", "tool": "run_agent"},
        )
        with pytest.raises(McpError):
            await exc.dispatch(task)
        assert stub.closed is True


# ---------------------------------------------------------------------------
# MCP_TOOL_NOT_FOUND
# ---------------------------------------------------------------------------

class TestMcpToolNotFound:
    @pytest.mark.asyncio
    async def test_tool_not_in_list_raises_mcp_tool_not_found(self):
        class _NoToolClient:
            closed = False

            async def connect(self):
                pass

            async def list_tools(self):
                return ["other_tool", "search"]

            async def close(self):
                self.closed = True

        stub = _NoToolClient()
        exc = McpAgentExecutor("hermes", default_server="stdio://hermes mcp serve")
        exc._make_client = lambda config: stub  # type: ignore[method-assign]
        task = AgentTask(
            task_id="t1", run_id="r1", node_id="n1", agent_id="a1",
            payload={},
            metadata={"server": "stdio://hermes mcp serve", "tool": "run_agent"},
        )
        with pytest.raises(McpError) as exc_info:
            await exc.dispatch(task)
        err = exc_info.value
        assert err.code == "MCP_TOOL_NOT_FOUND"
        assert "run_agent" in str(err)


# ---------------------------------------------------------------------------
# MCP_TOOL_ERROR — stream_events with failing call_tool
# ---------------------------------------------------------------------------

class TestMcpToolError:
    @pytest.mark.asyncio
    async def test_call_tool_runtime_error_becomes_agent_failed_with_tool_error(self):
        class _BrokenCallClient:
            closed = False

            async def call_tool(self, name, args):
                raise McpError(code="MCP_TOOL_ERROR", detail="runtime failure")

            async def close(self):
                self.closed = True

        stub = _BrokenCallClient()
        exc = McpAgentExecutor("hermes")
        handle = AgentHandle(
            run_id="r1", node_id="n1", agent_id="a1", status="running",
            metadata={"tool_name": "run_agent", "args": {}, "provider": "hermes"},
        )
        exc._sessions[handle.handle_id] = stub
        events = [e async for e in exc.stream_events(handle)]
        failed = next(e for e in events if e.type == "agent.failed")
        assert failed.payload["code"] == "MCP_TOOL_ERROR"
        assert stub.closed is True

    @pytest.mark.asyncio
    async def test_unexpected_exception_becomes_agent_failed(self):
        class _WeirdClient:
            closed = False

            async def call_tool(self, name, args):
                raise RuntimeError("segfault simulation")

            async def close(self):
                self.closed = True

        exc = McpAgentExecutor("hermes")
        handle = AgentHandle(
            run_id="r1", node_id="n1", agent_id="a1", status="running",
            metadata={"tool_name": "run_agent", "args": {}, "provider": "hermes"},
        )
        exc._sessions[handle.handle_id] = _WeirdClient()
        events = [e async for e in exc.stream_events(handle)]
        failed = next(e for e in events if e.type == "agent.failed")
        assert failed.payload["code"] == "MCP_TOOL_ERROR"
        assert "segfault" in failed.payload["detail"]
