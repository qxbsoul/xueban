"""xueban-web MCP Server — 网络抓取与搜索工具。

Story 11.3：暴露两个工具给 LLM Agent：
  fetch(url, timeout?)                    — HTTP 抓取，HTML→Markdown，TTL 缓存
  search(query, backend?, max_results?)   — 网络搜索，未配置时优雅降级

启动方式：
  python -m xueban.mcp_servers.web_server
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import httpx
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

try:
    import html2text as _html2text  # type: ignore[import]
    _h2t = _html2text.HTML2Text()
    _h2t.ignore_links = False
    _h2t.ignore_images = True
    _h2t.body_width = 0  # 不折行

    def _to_markdown(html: str) -> str:
        return _h2t.handle(html)

except ImportError:
    import re as _re

    def _to_markdown(html: str) -> str:  # type: ignore[misc]
        text = _re.sub(r"<script[^>]*>.*?</script>", "", html, flags=_re.DOTALL | _re.IGNORECASE)
        text = _re.sub(r"<style[^>]*>.*?</style>", "", text, flags=_re.DOTALL | _re.IGNORECASE)
        text = _re.sub(r"<[^>]+>", " ", text)
        return _re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# 缓存：URL / query → (result_dict, timestamp)
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[dict[str, Any], float]] = {}
CACHE_TTL: float = 3600.0  # 1 小时

# ---------------------------------------------------------------------------
# Server 实例 & 工具定义
# ---------------------------------------------------------------------------

app = Server("xueban-web")

TOOLS: list[types.Tool] = [
    types.Tool(
        name="fetch",
        description=(
            "抓取指定 URL 的内容。HTML 自动转为 Markdown，非 2xx 返回 http_error，"
            "超时返回 timeout，响应缓存 1 小时（cache_hit 标志）。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要抓取的 URL"},
                "timeout": {
                    "type": "integer",
                    "description": "超时秒数（默认 15）",
                    "default": 15,
                },
            },
            "required": ["url"],
        },
    ),
    types.Tool(
        name="search",
        description=(
            "执行网络搜索。需通过 SF_SEARCH_BACKEND + 对应 key 配置后端；"
            "未配置时返回 not_configured（不抛异常）。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "backend": {
                    "type": "string",
                    "description": "搜索后端（serpapi / tavily / duckduckgo），默认读 SF_SEARCH_BACKEND",
                },
                "max_results": {
                    "type": "integer",
                    "description": "最多返回条数（默认 10）",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    ),
]


# ---------------------------------------------------------------------------
# MCP 协议处理器
# ---------------------------------------------------------------------------


@app.list_tools()
async def _handle_list_tools() -> list[types.Tool]:
    return TOOLS


@app.call_tool()
async def _handle_call_tool(
    name: str, arguments: dict[str, Any]
) -> list[types.TextContent]:
    if name == "fetch":
        result = await _fetch(arguments)
    elif name == "search":
        result = await _search(arguments)
    else:
        result = {"status": "error", "error": f"Unknown tool: {name}"}
    return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------


async def _fetch(args: dict[str, Any]) -> dict[str, Any]:
    """AC2 / AC4：URL 抓取，HTML→Markdown，带缓存。"""
    url: str = args["url"]
    timeout: int = args.get("timeout", 15)

    # 缓存命中检查
    if url in _cache:
        cached, ts = _cache[url]
        if time.time() - ts < CACHE_TTL:
            return {**cached, "cache_hit": True}

    start_ms = time.monotonic() * 1000
    try:
        async with httpx.AsyncClient(timeout=float(timeout)) as client:
            resp = await client.get(url, follow_redirects=True)
    except httpx.TimeoutException:
        return {"status": "timeout", "url": url}
    except Exception as exc:
        return {"status": "error", "error": str(exc), "url": url}

    retrieval_time_ms = int(time.monotonic() * 1000 - start_ms)
    content_type: str = resp.headers.get("content-type", "")

    if not (200 <= resp.status_code < 300):
        return {
            "status": "http_error",
            "status_code": resp.status_code,
            "url": url,
            "retrieval_time_ms": retrieval_time_ms,
        }

    raw_text: str = resp.text
    if "html" in content_type.lower():
        content = _to_markdown(raw_text)
    else:
        content = raw_text

    result: dict[str, Any] = {
        "status_code": resp.status_code,
        "content_type": content_type,
        "content": content,
        "size_bytes": len(resp.content),
        "retrieval_time_ms": retrieval_time_ms,
        "cache_hit": False,
    }
    _cache[url] = (result, time.time())
    return result


async def _search(args: dict[str, Any]) -> dict[str, Any]:
    """AC3：网络搜索；未配置后端时优雅降级，不抛异常。"""
    query: str = args["query"]
    backend: str = args.get("backend") or os.getenv("SF_SEARCH_BACKEND", "")
    max_results: int = args.get("max_results", 10)

    cache_key = f"search:{backend}:{query}:{max_results}"
    if cache_key in _cache:
        cached, ts = _cache[cache_key]
        if time.time() - ts < CACHE_TTL:
            return {**cached, "cache_hit": True}

    # 后端路由
    if not backend:
        return {
            "status": "not_configured",
            "message": (
                "No search backend configured. Set SF_SEARCH_BACKEND to one of: "
                "serpapi, tavily, duckduckgo. "
                "For serpapi: set SF_SERPAPI_KEY. "
                "For tavily: set SF_TAVILY_KEY."
            ),
        }

    try:
        result = await _dispatch_search(backend, query, max_results)
    except Exception as exc:
        result = {"status": "error", "error": str(exc), "backend": backend}

    # 仅缓存成功结果，避免错误/限速响应毒化缓存 1 小时
    if result.get("status") != "error":
        _cache[cache_key] = (result, time.time())
    return result


async def _dispatch_search(
    backend: str, query: str, max_results: int
) -> dict[str, Any]:
    """将搜索请求分发给具体后端。"""
    if backend == "serpapi":
        return await _search_serpapi(query, max_results)
    if backend == "tavily":
        return await _search_tavily(query, max_results)
    if backend == "duckduckgo":
        return await _search_duckduckgo(query, max_results)
    return {
        "status": "not_configured",
        "message": f"Unknown backend {backend!r}. Supported: serpapi, tavily, duckduckgo.",
    }


async def _search_serpapi(query: str, max_results: int) -> dict[str, Any]:
    key = os.getenv("SF_SERPAPI_KEY", "")
    if not key:
        return {
            "status": "not_configured",
            "message": "SF_SERPAPI_KEY not set.",
        }
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://serpapi.com/search",
            params={"q": query, "api_key": key, "num": max_results},
            timeout=15,
        )
    if resp.status_code != 200:
        return {"status": "http_error", "status_code": resp.status_code}
    data = resp.json()
    items = data.get("organic_results", [])
    return {
        "status": "success",
        "results": [
            {
                "title": r.get("title", ""),
                "url": r.get("link", ""),
                "snippet": r.get("snippet", ""),
                "rank": i + 1,
            }
            for i, r in enumerate(items[:max_results])
        ],
        "cache_hit": False,
    }


async def _search_tavily(query: str, max_results: int) -> dict[str, Any]:
    key = os.getenv("SF_TAVILY_KEY", "")
    if not key:
        return {"status": "not_configured", "message": "SF_TAVILY_KEY not set."}
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={"query": query, "max_results": max_results, "api_key": key},
            timeout=15,
        )
    if resp.status_code != 200:
        return {"status": "http_error", "status_code": resp.status_code}
    data = resp.json()
    items = data.get("results", [])
    return {
        "status": "success",
        "results": [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", ""),
                "rank": i + 1,
            }
            for i, r in enumerate(items)
        ],
        "cache_hit": False,
    }


async def _search_duckduckgo(query: str, max_results: int) -> dict[str, Any]:
    try:
        from duckduckgo_search import DDGS  # type: ignore[import]
    except ImportError:
        return {
            "status": "not_configured",
            "message": "duckduckgo-search not installed; run: pip install duckduckgo-search",
        }

    def _ddgs_sync() -> list[dict[str, Any]]:
        with DDGS() as ddgs:
            return [
                {
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "snippet": r.get("body", ""),
                    "rank": i + 1,
                }
                for i, r in enumerate(ddgs.text(query, max_results=max_results))
            ]

    results = await asyncio.to_thread(_ddgs_sync)
    return {"status": "success", "results": results, "cache_hit": False}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def main() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
