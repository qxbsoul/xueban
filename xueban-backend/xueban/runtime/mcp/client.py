"""McpClient — high-level wrapper around the MCP Python SDK (Story 2.4).

Provides connect / list_tools / call_tool / close methods.
The real SDK (mcp package) is imported lazily; ImportError raises McpError so
callers always see the structured error hierarchy.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from xueban.runtime.errors import McpError
from xueban.runtime.mcp.transport import McpTransportConfig


class McpClient:
    """Connects to an MCP server and issues tool calls."""

    def __init__(self, config: McpTransportConfig) -> None:
        self._config = config
        self._session: Any = None
        self._stdio_ctx: Any = None
        self._session_ctx: Any = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open transport and perform SDK initialisation handshake."""
        try:
            from mcp import ClientSession, StdioServerParameters  # type: ignore[import]
        except ImportError as exc:
            raise McpError(
                code="MCP_SERVER_UNAVAILABLE",
                detail="mcp package not installed; run: pip install mcp",
            ) from exc

        if self._config.kind != "stdio":
            raise McpError(
                code="MCP_SERVER_UNAVAILABLE",
                detail=f"MCP transport kind {self._config.kind!r} not supported",
            )

        try:
            from mcp.client.stdio import stdio_client  # type: ignore[import]

            cmd = self._config.command
            params = StdioServerParameters(command=cmd[0], args=cmd[1:] if len(cmd) > 1 else [])
            ctx = stdio_client(params)
            read, write = await ctx.__aenter__()
            self._stdio_ctx = ctx
            session = ClientSession(read, write)
            await session.__aenter__()
            self._session_ctx = session
            self._session = session
            await self._session.initialize()
        except McpError:
            raise
        except Exception as exc:
            await self.close()
            raise McpError(code="MCP_SERVER_UNAVAILABLE", detail=str(exc)) from exc

    async def list_tools(self) -> List[str]:
        """Return tool names exposed by the server."""
        if self._session is None:
            raise McpError(code="MCP_SERVER_UNAVAILABLE", detail="not connected; call connect() first")
        try:
            result = await self._session.list_tools()
            return [t.name for t in result.tools]
        except McpError:
            raise
        except Exception as exc:
            raise McpError(code="MCP_SERVER_UNAVAILABLE", detail=str(exc)) from exc

    async def call_tool(self, name: str, args: Dict[str, Any]) -> Any:
        """Invoke a tool and return the raw result."""
        if self._session is None:
            raise McpError(code="MCP_SERVER_UNAVAILABLE", detail="not connected; call connect() first")
        try:
            return await self._session.call_tool(name, args)
        except McpError:
            raise
        except Exception as exc:
            raise McpError(code="MCP_TOOL_ERROR", detail=str(exc)) from exc

    async def close(self) -> None:
        """Tear down the session and transport."""
        for ctx in (self._session_ctx, self._stdio_ctx):
            if ctx is not None:
                try:
                    await ctx.__aexit__(None, None, None)
                except Exception:
                    pass
        self._session = None
        self._session_ctx = None
        self._stdio_ctx = None
