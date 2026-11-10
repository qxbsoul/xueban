"""Tool Registry API — Story 8.4b (AC2, AC3, AC5).

Endpoints:
  GET  /tools/builtin                    — 静态内置工具列表
  GET  /tools/providers                  — 已注册 MCP Provider 列表
  POST /tools/providers                  — 注册新 Provider（含连接验证）
  GET  /tools/providers/{id}             — 获取单个 Provider
  DELETE /tools/providers/{id}           — 删除 Provider
  GET  /tools/providers/{id}/tools       — 拉取工具 schema 列表
  POST /tools/providers/{id}/test        — 重新测试连接
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from xueban.runtime.tool_registry import (
    BUILTIN_TOOLS,
    ProviderNotFoundError,
    ProviderRegistrationError,
    delete_provider,
    get_provider,
    get_provider_tool_schemas,
    list_providers,
    register_provider,
    test_provider_connection,
)

router = APIRouter(prefix="/tools", tags=["tools"])


def _ok(data: Any, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"data": data, "meta": meta or {}}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class RegisterProviderRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    transport_type: str = Field(..., pattern="^(stdio|http|sse)$")
    command: Optional[List[str]] = None
    server_url: Optional[str] = None
    env: Dict[str, str] = Field(default_factory=dict)
    description: str = ""


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/builtin")
def get_builtin_tools() -> Dict[str, Any]:
    """返回内置工具列表（静态）。"""
    return _ok(
        data={"tools": BUILTIN_TOOLS},
        meta={"count": len(BUILTIN_TOOLS)},
    )


@router.get("/providers")
def get_providers() -> Dict[str, Any]:
    """返回已注册 MCP Provider 列表（凭证已掩码）。"""
    providers = list_providers()
    return _ok(
        data={"providers": providers},
        meta={"count": len(providers)},
    )


@router.post("/providers")
async def create_provider(req: RegisterProviderRequest) -> Dict[str, Any]:
    """注册新 MCP Provider，注册后立即执行连接验证。"""
    try:
        provider = register_provider(
            name=req.name,
            transport_type=req.transport_type,
            command=req.command,
            server_url=req.server_url,
            env=req.env,
            description=req.description,
        )
    except ProviderRegistrationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Attempt connection test immediately after registration.
    # Registration is already persisted — always return 201 so the client knows the
    # provider exists.  A failed connection is surfaced in the `connection` field so
    # the user can fix credentials and retry via POST /tools/providers/{id}/test.
    connection_result: Dict[str, Any]
    try:
        test_result = await test_provider_connection(provider["provider_id"])
        connection_result = {
            "success": test_result["success"],
            "tool_count": test_result.get("tool_count", 0),
            "message": test_result.get("message", ""),
        }
    except Exception as exc:
        connection_result = {"success": False, "message": str(exc), "tool_count": 0}

    # Re-fetch to include updated status & tool count
    updated = get_provider(provider["provider_id"]) or provider
    return _ok(
        data={"provider": updated, "connection": connection_result},
        meta={"test_result": connection_result},
    )


@router.get("/providers/{provider_id}")
def get_single_provider(provider_id: str) -> Dict[str, Any]:
    """返回单个 Provider（凭证已掩码）。"""
    provider = get_provider(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"Provider {provider_id!r} not found")
    return _ok(data={"provider": provider})


@router.delete("/providers/{provider_id}")
def remove_provider(provider_id: str) -> Dict[str, Any]:
    """删除 Provider 及其缓存的工具 schema。"""
    deleted = delete_provider(provider_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Provider {provider_id!r} not found")
    return _ok(data={"deleted": provider_id})


@router.get("/providers/{provider_id}/tools")
def get_provider_tools(provider_id: str) -> Dict[str, Any]:
    """返回已缓存的工具 schema 列表。缓存未建立时返回空列表。"""
    provider = get_provider(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"Provider {provider_id!r} not found")
    tools = get_provider_tool_schemas(provider_id)
    return _ok(
        data={"tools": tools},
        meta={
            "count": len(tools),
            "cached": len(tools) > 0,
            "provider_name": provider.get("name", ""),
        },
    )


@router.post("/providers/{provider_id}/test")
async def test_provider(provider_id: str) -> Dict[str, Any]:
    """重新测试与 MCP 服务器的连接，刷新工具 schema 缓存。"""
    provider = get_provider(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"Provider {provider_id!r} not found")
    try:
        result = await test_provider_connection(provider_id)
    except ProviderNotFoundError:
        raise HTTPException(status_code=404, detail=f"Provider {provider_id!r} not found")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if not result["success"]:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "MCP_CONNECT_FAILED",
                "message": result["message"],
                "provider_id": provider_id,
            },
        )
    return _ok(data=result)
