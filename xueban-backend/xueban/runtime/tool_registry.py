"""Tool Registry — Story 8.4b (AC2, AC3, AC5).

In-memory + file-backed store for MCP Provider registrations and tool schema caches.
Schema files live under _data/tool_schemas/{provider_id}.json.
TTL defaults to 1 hour (SF_TOOL_SCHEMA_TTL env var overrides).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from xueban.runtime.tool_credentials import (
    ToolCredentialError,
    decrypt_env,
    encrypt_env,
)

_SCHEMA_CACHE_DIR = Path("_data/tool_schemas")
_SCHEMA_TTL_SECONDS = int(os.environ.get("SF_TOOL_SCHEMA_TTL", "3600"))

# In-memory provider store: provider_id → raw dict (encrypted env stored as token)
_providers: Dict[str, Dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Built-in tool catalogue (static)
# ---------------------------------------------------------------------------

BUILTIN_TOOLS: List[Dict[str, Any]] = [
    {
        "tool_id": "builtin:web_search",
        "name": "web_search",
        "type": "builtin",
        "description": "Search the open web. Returns ranked snippets with URLs.",
        "version": "1.0",
        "icon": "🌐",
        "credentials_required": False,
    },
    {
        "tool_id": "builtin:web_fetch",
        "name": "web_fetch",
        "type": "builtin",
        "description": "Fetch a specific URL; returns extracted text and metadata.",
        "version": "1.0",
        "icon": "📥",
        "credentials_required": False,
    },
    {
        "tool_id": "builtin:code_executor",
        "name": "code_executor",
        "type": "builtin",
        "description": "Execute Python or JavaScript in a sandboxed environment.",
        "version": "1.0",
        "icon": "💻",
        "credentials_required": False,
    },
    {
        "tool_id": "builtin:calculator",
        "name": "calculator",
        "type": "builtin",
        "description": "Perform mathematical calculations.",
        "version": "1.0",
        "icon": "🧮",
        "credentials_required": False,
    },
    {
        "tool_id": "builtin:spawn_task",
        "name": "spawn_task",
        "type": "builtin",
        "description": "Delegate sub-tasks to worker agents (boss roles only).",
        "version": "1.0",
        "icon": "🔀",
        "credentials_required": False,
        "boss_only": True,
    },
    # --- WorkBuddy 平台能力镜像 (2026-11-07) ---
    # Names match the Node ConversationRuntime builtins 1:1
    # (server/src/lib/tools/builtin/). credentials_required reflects whether a
    # real provider call needs env credentials; tools still return a
    # structured stub when unconfigured.
    {
        "tool_id": "builtin:send_email",
        "name": "send_email",
        "type": "builtin",
        "description": "Send an email via Resend or a generic mail bridge (RESEND_API_KEY / SF_MAIL_API_BASE).",
        "version": "1.0",
        "icon": "✉️",
        "credentials_required": True,
    },
    {
        "tool_id": "builtin:im_read",
        "name": "im_read",
        "type": "builtin",
        "description": "Read chat/messaging data via the IM bridge (SF_IM_API_BASE): list conversations, read history.",
        "version": "1.0",
        "icon": "💬",
        "credentials_required": True,
    },
    {
        "tool_id": "builtin:im_send",
        "name": "im_send",
        "type": "builtin",
        "description": "Send a chat message via the IM bridge (SF_IM_API_BASE).",
        "version": "1.0",
        "icon": "📨",
        "credentials_required": True,
    },
    {
        "tool_id": "builtin:github_request",
        "name": "github_request",
        "type": "builtin",
        "description": "Read from the GitHub REST API (GET): repos, issues, pull requests, search.",
        "version": "1.0",
        "icon": "🐙",
        "credentials_required": False,
    },
    {
        "tool_id": "builtin:github_write",
        "name": "github_write",
        "type": "builtin",
        "description": "Write to the GitHub REST API (POST/PUT/PATCH/DELETE): issues, comments, PRs. Requires GITHUB_TOKEN.",
        "version": "1.0",
        "icon": "✍️",
        "credentials_required": True,
    },
    {
        "tool_id": "builtin:image_generate",
        "name": "image_generate",
        "type": "builtin",
        "description": "Generate images from a text prompt (SF_IMAGE_API_BASE, OpenAI-compatible).",
        "version": "1.0",
        "icon": "🎨",
        "credentials_required": True,
    },
    {
        "tool_id": "builtin:video_generate",
        "name": "video_generate",
        "type": "builtin",
        "description": "Generate a short video from a text prompt (SF_VIDEO_API_BASE).",
        "version": "1.0",
        "icon": "🎬",
        "credentials_required": True,
    },
    {
        "tool_id": "builtin:generate_3d",
        "name": "generate_3d",
        "type": "builtin",
        "description": "Generate a 3D model from a text prompt (SF_3D_API_BASE).",
        "version": "1.0",
        "icon": "🧊",
        "credentials_required": True,
    },
]


# ---------------------------------------------------------------------------
# File persistence helpers
# ---------------------------------------------------------------------------


def _provider_path(provider_id: str) -> Path:
    _SCHEMA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _SCHEMA_CACHE_DIR / f"{provider_id}.json"


def _write_provider(provider: Dict[str, Any]) -> None:
    path = _provider_path(provider["provider_id"])
    # float timestamps are JSON-native; avoid default=str which serializes them as strings
    # and causes TypeError when reading back (time.time() - "1714123456.789")
    path.write_text(json.dumps(provider), encoding="utf-8")


def _delete_provider_file(provider_id: str) -> None:
    path = _provider_path(provider_id)
    if path.exists():
        path.unlink()


def _load_providers_from_disk() -> None:
    """Populate in-memory store from persisted JSON files on startup."""
    if not _SCHEMA_CACHE_DIR.exists():
        return
    for path in _SCHEMA_CACHE_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if "provider_id" in data:
                _providers[data["provider_id"]] = data
        except Exception:
            pass


def _public_view(p: Dict[str, Any]) -> Dict[str, Any]:
    """Return a provider dict safe for API response (no encrypted payload)."""
    view = {k: v for k, v in p.items() if k != "env_encrypted"}
    # Rebuild env_masked from env_keys
    view["env_masked"] = {k: "***" for k in view.pop("env_keys", [])}
    return view


# Load on module import (happens once per process).
_load_providers_from_disk()


# ---------------------------------------------------------------------------
# Provider CRUD
# ---------------------------------------------------------------------------


def list_providers() -> List[Dict[str, Any]]:
    return [_public_view(p) for p in _providers.values()]


def get_provider(provider_id: str) -> Optional[Dict[str, Any]]:
    p = _providers.get(provider_id)
    return _public_view(p) if p else None


class ProviderNotFoundError(Exception):
    pass


class ProviderRegistrationError(Exception):
    pass


def register_provider(
    name: str,
    transport_type: str,
    command: Optional[List[str]],
    server_url: Optional[str],
    env: Dict[str, str],
    description: str = "",
) -> Dict[str, Any]:
    """Create a new MCP Provider record. Returns the public view (no credentials)."""
    if transport_type not in ("stdio", "http", "sse"):
        raise ProviderRegistrationError(
            f"transport_type must be 'stdio', 'http', or 'sse'; got {transport_type!r}"
        )
    if transport_type == "stdio" and not command:
        raise ProviderRegistrationError("stdio transport requires a non-empty command list")
    if transport_type in ("http", "sse") and not server_url:
        raise ProviderRegistrationError(f"{transport_type} transport requires server_url")

    try:
        env_encrypted = encrypt_env(env) if env else ""
    except ToolCredentialError as exc:
        raise ProviderRegistrationError(f"Credential encryption failed: {exc}") from exc

    provider_id = str(uuid.uuid4())
    provider: Dict[str, Any] = {
        "provider_id": provider_id,
        "name": name,
        "transport_type": transport_type,
        "command": list(command) if command else [],
        "server_url": server_url or "",
        "description": description,
        "env_encrypted": env_encrypted,
        "env_keys": list(env.keys()),
        "schema_cache": [],
        "schema_fetched_at": None,
        "status": "registered",
        "last_test_result": None,
    }
    _providers[provider_id] = provider
    _write_provider(provider)
    return _public_view(provider)


def delete_provider(provider_id: str) -> bool:
    """Delete a provider. Returns True if it existed."""
    if provider_id not in _providers:
        return False
    del _providers[provider_id]
    _delete_provider_file(provider_id)
    return True


def get_provider_tool_schemas(provider_id: str) -> List[Dict[str, Any]]:
    """Return cached tool schemas (empty list if not fetched or TTL expired)."""
    p = _providers.get(provider_id)
    if not p:
        return []
    fetched_at = p.get("schema_fetched_at")
    if fetched_at is not None:
        # Defensive cast: old files may have serialized the float as a string
        try:
            fetched_at = float(fetched_at)
        except (TypeError, ValueError):
            fetched_at = 0
        if (time.time() - fetched_at) > _SCHEMA_TTL_SECONDS:
            return []
    return list(p.get("schema_cache", []))


# ---------------------------------------------------------------------------
# MCP connection test
# ---------------------------------------------------------------------------


def _build_mcp_config(provider: Dict[str, Any]):
    """Build a McpTransportConfig directly from provider dict (avoids URI round-trip
    which breaks stdio commands containing spaces in the path).

    Returns (config, error_message). error_message is '' on success.
    """
    try:
        from xueban.runtime.mcp.transport import McpTransportConfig
    except ImportError as exc:
        return None, f"MCP client unavailable: {exc}"

    transport_type = provider["transport_type"]
    if transport_type == "stdio":
        cmd = provider.get("command", [])
        if not cmd:
            return None, "stdio transport has no command"
        # Build directly — do NOT join+split which corrupts paths containing spaces
        return McpTransportConfig(kind="stdio", command=list(cmd)), ""
    if transport_type in ("http", "sse"):
        url = provider.get("server_url", "")
        if not url:
            return None, f"{transport_type} transport has no server_url"
        return McpTransportConfig(kind="http", url=url), ""
    return None, f"Unknown transport_type: {transport_type!r}"


async def test_provider_connection(provider_id: str) -> Dict[str, Any]:
    """Connect to the MCP server, fetch tool list, update cache. Returns result dict."""
    p = _providers.get(provider_id)
    if not p:
        raise ProviderNotFoundError(provider_id)

    config, err = _build_mcp_config(p)
    if err or config is None:
        result = {"success": False, "message": err or "Failed to build transport config", "tool_count": 0}
        _update_test_result(p, result, tools=[])
        return result

    try:
        from xueban.runtime.mcp.client import McpClient
    except ImportError as exc:
        result = {"success": False, "message": f"MCP client unavailable: {exc}", "tool_count": 0}
        _update_test_result(p, result, tools=[])
        return result

    try:
        client = McpClient(config)

        try:
            try:
                await asyncio.wait_for(client.connect(), timeout=10.0)
            except asyncio.TimeoutError:
                result = {"success": False, "message": "Connection timeout (10 s)", "tool_count": 0}
                _update_test_result(p, result, tools=[])
                return result

            if client._session is None:
                raise RuntimeError("MCP session not initialized after connect()")

            raw_list = await asyncio.wait_for(client._session.list_tools(), timeout=5.0)
            tools = [
                {
                    "tool_id": f"mcp:{provider_id}:{t.name}",
                    "name": t.name,
                    "description": getattr(t, "description", "") or "",
                    "input_schema": (getattr(t, "inputSchema", None) or {}).get(
                        "properties", {}
                    ),
                    "type": "mcp",
                    "provider_id": provider_id,
                    "provider_name": p["name"],
                }
                for t in raw_list.tools
            ]

            result = {
                "success": True,
                "message": f"Connected — discovered {len(tools)} tools",
                "tool_count": len(tools),
            }
            _update_test_result(p, result, tools=tools)
            return result

        except asyncio.TimeoutError:
            result = {"success": False, "message": "Operation timeout", "tool_count": 0}
            _update_test_result(p, result, tools=[])
            return result
        except Exception as exc:
            result = {"success": False, "message": str(exc), "tool_count": 0}
            _update_test_result(p, result, tools=[])
            return result
        finally:
            # Always close the client regardless of success/failure path
            await client.close()

    except Exception as exc:
        result = {"success": False, "message": str(exc), "tool_count": 0}
        _update_test_result(p, result, tools=[])
        return result


def _update_test_result(
    p: Dict[str, Any], result: Dict[str, Any], tools: List[Dict[str, Any]]
) -> None:
    now = time.time()
    p["last_test_result"] = {**result, "tested_at": now}
    p["status"] = "connected" if result["success"] else "error"
    if result["success"]:
        p["schema_cache"] = tools
        p["schema_fetched_at"] = now
    _write_provider(p)
