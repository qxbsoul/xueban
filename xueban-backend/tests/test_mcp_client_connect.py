"""Story 2.4 — McpTransportConfig + McpClient connect 测试（mock SDK）。"""
from __future__ import annotations

import sys
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xueban.runtime.errors import McpError
from xueban.runtime.mcp.transport import McpTransportConfig
from xueban.runtime.mcp.client import McpClient


# ---------------------------------------------------------------------------
# McpTransportConfig parsing
# ---------------------------------------------------------------------------

class TestMcpTransportConfigParse:
    def test_stdio_command_split(self):
        cfg = McpTransportConfig.parse("stdio://hermes mcp serve")
        assert cfg.kind == "stdio"
        assert cfg.command == ["hermes", "mcp", "serve"]

    def test_stdio_single_word(self):
        cfg = McpTransportConfig.parse("stdio://myserver")
        assert cfg.kind == "stdio"
        assert cfg.command == ["myserver"]

    def test_http_url_stored(self):
        cfg = McpTransportConfig.parse("http://localhost:8080")
        assert cfg.kind == "http"
        assert cfg.url == "http://localhost:8080"

    def test_https_url_stored(self):
        cfg = McpTransportConfig.parse("https://mcp.example.com/api")
        assert cfg.kind == "http"
        assert cfg.url == "https://mcp.example.com/api"

    def test_unknown_scheme_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown MCP server URI scheme"):
            McpTransportConfig.parse("grpc://localhost:9090")

    def test_stdio_empty_command_raises(self):
        with pytest.raises(ValueError):
            McpTransportConfig.parse("stdio://")

    def test_whitespace_stripped(self):
        cfg = McpTransportConfig.parse("  stdio://hermes mcp serve  ")
        assert cfg.command == ["hermes", "mcp", "serve"]


# ---------------------------------------------------------------------------
# McpClient — SDK not installed → McpError
# ---------------------------------------------------------------------------

class TestMcpClientNoSdk:
    @pytest.mark.asyncio
    async def test_connect_raises_mcp_error_if_sdk_missing(self):
        """If mcp package not installed, connect() raises MCP_SERVER_UNAVAILABLE."""
        cfg = McpTransportConfig(kind="stdio", command=["hermes", "mcp", "serve"])
        client = McpClient(cfg)

        # Simulate missing mcp package by removing it from sys.modules
        saved = sys.modules.pop("mcp", None)
        try:
            with patch.dict(sys.modules, {"mcp": None}):  # type: ignore[dict-item]
                with pytest.raises(McpError) as exc_info:
                    await client.connect()
            assert exc_info.value.code == "MCP_SERVER_UNAVAILABLE"
            assert "not installed" in exc_info.value.detail
        finally:
            if saved is not None:
                sys.modules["mcp"] = saved

    @pytest.mark.asyncio
    async def test_list_tools_before_connect_raises(self):
        cfg = McpTransportConfig(kind="stdio", command=["hermes"])
        client = McpClient(cfg)
        with pytest.raises(McpError) as exc_info:
            await client.list_tools()
        assert exc_info.value.code == "MCP_SERVER_UNAVAILABLE"
        assert "not connected" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_call_tool_before_connect_raises(self):
        cfg = McpTransportConfig(kind="stdio", command=["hermes"])
        client = McpClient(cfg)
        with pytest.raises(McpError) as exc_info:
            await client.call_tool("run_agent", {})
        assert exc_info.value.code == "MCP_SERVER_UNAVAILABLE"


# ---------------------------------------------------------------------------
# McpClient — mocked SDK session
# ---------------------------------------------------------------------------

def _make_mock_session(tools: List[str], call_result: Any = "done") -> Any:
    """Build a mock that mimics mcp.ClientSession."""
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.initialize = AsyncMock(return_value=None)

    tool_obj_list = [MagicMock(name=t) for t in tools]
    for obj, name in zip(tool_obj_list, tools):
        obj.name = name
    tools_result = MagicMock()
    tools_result.tools = tool_obj_list
    session.list_tools = AsyncMock(return_value=tools_result)
    session.call_tool = AsyncMock(return_value=call_result)
    return session


class TestMcpClientWithMockedSdk:
    def _make_client_with_session(self, session: Any) -> McpClient:
        cfg = McpTransportConfig(kind="stdio", command=["hermes", "mcp", "serve"])
        client = McpClient(cfg)
        client._session = session
        return client

    @pytest.mark.asyncio
    async def test_list_tools_returns_names(self):
        session = _make_mock_session(["run_agent", "search", "summarize"])
        client = self._make_client_with_session(session)
        tools = await client.list_tools()
        assert tools == ["run_agent", "search", "summarize"]

    @pytest.mark.asyncio
    async def test_call_tool_returns_result(self):
        session = _make_mock_session(["run_agent"], call_result={"answer": "42"})
        client = self._make_client_with_session(session)
        result = await client.call_tool("run_agent", {"prompt": "hello"})
        assert result == {"answer": "42"}
        session.call_tool.assert_called_once_with("run_agent", {"prompt": "hello"})

    @pytest.mark.asyncio
    async def test_close_clears_session(self):
        session = _make_mock_session([])
        client = self._make_client_with_session(session)
        client._session_ctx = session
        client._stdio_ctx = session
        await client.close()
        assert client._session is None
