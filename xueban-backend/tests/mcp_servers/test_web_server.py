"""Story 11.3 — xueban-web MCP Server 测试。

覆盖范围：
- AC1  工具列表（fetch / search）
- AC2  fetch() URL 抓取（mock HTTP）
- AC3  search() 未配置后端 → not_configured
- AC4  响应缓存（TTL 1h，cache_hit 标志）
- AC5  McpClient 集成测试
"""
from __future__ import annotations

import json
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# AC1 — 工具列表
# ---------------------------------------------------------------------------


class TestWebToolList:
    def test_server_exposes_two_tools(self):
        from xueban.mcp_servers import web_server

        names = {t.name for t in web_server.TOOLS}
        assert names == {"fetch", "search"}

    def test_fetch_url_required(self):
        from xueban.mcp_servers import web_server

        fetch_tool = next(t for t in web_server.TOOLS if t.name == "fetch")
        assert "url" in fetch_tool.inputSchema.get("required", [])

    def test_search_query_required(self):
        from xueban.mcp_servers import web_server

        search_tool = next(t for t in web_server.TOOLS if t.name == "search")
        assert "query" in search_tool.inputSchema.get("required", [])


# ---------------------------------------------------------------------------
# AC2 — fetch() URL 抓取
# ---------------------------------------------------------------------------


class TestFetchTool:
    def _make_response(self, status_code=200, content_type="text/html", text="<h1>Hello</h1>",
                       content=b"<h1>Hello</h1>"):
        resp = MagicMock()
        resp.status_code = status_code
        resp.headers = {"content-type": content_type}
        resp.text = text
        resp.content = content
        return resp

    @pytest.mark.asyncio
    async def test_fetch_success_html_converted_to_markdown(self):
        from xueban.mcp_servers import web_server

        mock_resp = self._make_response(
            status_code=200,
            content_type="text/html",
            text="<h1>Hello World</h1><p>Some text.</p>",
            content=b"<h1>Hello World</h1><p>Some text.</p>",
        )

        with patch("xueban.mcp_servers.web_server.httpx") as mock_httpx:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_httpx.AsyncClient.return_value = mock_client

            result = await web_server._fetch({"url": "https://example.com"})

        assert result["status_code"] == 200
        assert result["retrieval_time_ms"] >= 0
        assert result["size_bytes"] > 0
        assert "cache_hit" in result
        # HTML 应被转换，原始标签不应存在于 content 中
        assert "<h1>" not in result["content"]
        assert "Hello World" in result["content"]

    @pytest.mark.asyncio
    async def test_fetch_non_2xx_returns_http_error(self):
        from xueban.mcp_servers import web_server

        mock_resp = self._make_response(status_code=404, content_type="text/html",
                                        text="Not Found", content=b"Not Found")

        with patch("xueban.mcp_servers.web_server.httpx") as mock_httpx:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_httpx.AsyncClient.return_value = mock_client

            result = await web_server._fetch({"url": "https://example.com/missing"})

        assert result["status"] == "http_error"
        assert result["status_code"] == 404

    @pytest.mark.asyncio
    async def test_fetch_timeout_returns_timeout_status(self):
        import httpx as real_httpx
        from xueban.mcp_servers import web_server

        with patch("xueban.mcp_servers.web_server.httpx") as mock_httpx:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(side_effect=real_httpx.TimeoutException("timeout"))
            mock_httpx.AsyncClient.return_value = mock_client
            # Preserve the real exception class so `except httpx.TimeoutException` works
            mock_httpx.TimeoutException = real_httpx.TimeoutException

            result = await web_server._fetch({"url": "https://slow.example.com", "timeout": 1})

        assert result["status"] == "timeout"

    @pytest.mark.asyncio
    async def test_fetch_json_content_type_not_converted(self):
        from xueban.mcp_servers import web_server

        payload = '{"key": "value"}'
        mock_resp = self._make_response(
            status_code=200,
            content_type="application/json",
            text=payload,
            content=payload.encode(),
        )

        with patch("xueban.mcp_servers.web_server.httpx") as mock_httpx:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_httpx.AsyncClient.return_value = mock_client

            result = await web_server._fetch({"url": "https://api.example.com/data"})

        assert result["status_code"] == 200
        assert result["content"] == payload


# ---------------------------------------------------------------------------
# AC3 — search() 未配置后端
# ---------------------------------------------------------------------------


class TestSearchTool:
    @pytest.mark.asyncio
    async def test_search_not_configured_when_no_backend(self):
        import os
        from xueban.mcp_servers import web_server

        with patch.dict(os.environ, {}, clear=False):
            # 确保环境变量未设置
            os.environ.pop("SF_SEARCH_BACKEND", None)
            os.environ.pop("SF_SERPAPI_KEY", None)
            os.environ.pop("SF_TAVILY_KEY", None)

            result = await web_server._search({"query": "test query"})

        assert result["status"] == "not_configured"
        assert "SF_SEARCH_BACKEND" in result.get("message", "") or \
               "not_configured" in result["status"]

    @pytest.mark.asyncio
    async def test_search_not_configured_no_exception(self):
        """search 未配置时不得抛异常（AC3 安全降级）。"""
        import os
        from xueban.mcp_servers import web_server

        os.environ.pop("SF_SEARCH_BACKEND", None)
        os.environ.pop("SF_SERPAPI_KEY", None)

        # 不应抛任何异常
        try:
            result = await web_server._search({"query": "anything"})
            assert "status" in result
        except Exception as exc:
            pytest.fail(f"search raised an exception: {exc}")


# ---------------------------------------------------------------------------
# AC4 — 响应缓存（TTL 1h，cache_hit 标志）
# ---------------------------------------------------------------------------


class TestFetchCache:
    @pytest.mark.asyncio
    async def test_cache_hit_on_second_call(self):
        from xueban.mcp_servers import web_server

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/plain"}
        mock_resp.text = "cached content"
        mock_resp.content = b"cached content"

        call_count = 0

        async def fake_get(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return mock_resp

        with patch("xueban.mcp_servers.web_server.httpx") as mock_httpx:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = fake_get
            mock_httpx.AsyncClient.return_value = mock_client

            url = "https://cached.example.com/unique_url_for_cache_test"

            # 清除旧缓存避免干扰
            web_server._cache.pop(url, None)

            r1 = await web_server._fetch({"url": url})
            r2 = await web_server._fetch({"url": url})

        assert r1.get("cache_hit") is False
        assert r2.get("cache_hit") is True
        assert call_count == 1  # 只发起了一次真实请求

    @pytest.mark.asyncio
    async def test_cache_expired_refetches(self):
        from xueban.mcp_servers import web_server

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/plain"}
        mock_resp.text = "content"
        mock_resp.content = b"content"

        call_count = 0

        async def fake_get(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return mock_resp

        with patch("xueban.mcp_servers.web_server.httpx") as mock_httpx:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = fake_get
            mock_httpx.AsyncClient.return_value = mock_client

            url = "https://expired.example.com/unique_url_for_expire_test"

            # 注入已过期的缓存条目
            web_server._cache[url] = ({"status_code": 200, "content": "old", "cache_hit": False,
                                        "content_type": "text/plain", "size_bytes": 3,
                                        "retrieval_time_ms": 10}, time.time() - 7200)

            r = await web_server._fetch({"url": url})

        assert r.get("cache_hit") is False
        assert call_count == 1  # 缓存过期，重新请求


# ---------------------------------------------------------------------------
# AC5 — McpClient 集成测试
# ---------------------------------------------------------------------------


class TestWebMcpClientIntegration:
    @pytest.mark.asyncio
    async def test_list_tools_via_client(self):
        from xueban.runtime.mcp.client import McpClient
        from xueban.runtime.mcp.transport import McpTransportConfig

        cfg = McpTransportConfig(
            kind="stdio",
            command=[sys.executable, "-m", "xueban.mcp_servers.web_server"],
        )
        client = McpClient(cfg)
        try:
            await client.connect()
            tools = await client.list_tools()
            assert set(tools) == {"fetch", "search"}
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_search_not_configured_via_client(self):
        """未配置后端时 search 通过 McpClient 调用返回 not_configured。"""
        import os
        from xueban.runtime.mcp.client import McpClient
        from xueban.runtime.mcp.transport import McpTransportConfig

        # 传入无搜索 key 的环境
        env_cmd = [
            sys.executable, "-c",
            "import os; "
            "os.environ.pop('SF_SEARCH_BACKEND', None); "
            "os.environ.pop('SF_SERPAPI_KEY', None); "
            "import asyncio; "
            "from xueban.mcp_servers.web_server import main; "
            "asyncio.run(main())",
        ]
        cfg = McpTransportConfig(kind="stdio", command=env_cmd)
        client = McpClient(cfg)
        try:
            await client.connect()
            r = await client.call_tool("search", {"query": "python"})
            res = json.loads(r.content[0].text)
            assert res["status"] == "not_configured"
        finally:
            await client.close()
