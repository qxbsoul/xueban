"""tests/test_tool_registry_api.py — AC5, AC7: Tool Registry API 端点测试"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from xueban.server import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def set_secret_key(monkeypatch):
    monkeypatch.setenv("SF_TOOL_SECRET_KEY", "test-key-tool-registry-api-2026")
    yield


@pytest.fixture(autouse=True)
def clear_registry():
    """Reset the in-memory registry between tests."""
    from xueban.runtime import tool_registry
    tool_registry._providers.clear()
    yield
    tool_registry._providers.clear()


# ---------------------------------------------------------------------------
# GET /tools/builtin
# ---------------------------------------------------------------------------


def test_get_builtin_tools_returns_list():
    resp = client.get("/tools/builtin")
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body
    tools = body["data"]["tools"]
    assert isinstance(tools, list)
    assert len(tools) >= 4
    ids = [t["tool_id"] for t in tools]
    assert "builtin:web_search" in ids
    assert "builtin:web_fetch" in ids
    assert "builtin:code_executor" in ids
    assert "builtin:calculator" in ids


def test_builtin_tools_have_required_fields():
    resp = client.get("/tools/builtin")
    tools = resp.json()["data"]["tools"]
    for tool in tools:
        assert "tool_id" in tool
        assert tool["tool_id"].startswith("builtin:")
        assert "name" in tool
        assert "description" in tool
        assert isinstance(tool["credentials_required"], bool)

    # 基础工具不依赖密钥；平台工具按真实上游要求声明凭证。
    basic_tools = {"builtin:web_search", "builtin:web_fetch", "builtin:code_executor", "builtin:calculator"}
    for tool in tools:
        if tool["tool_id"] in basic_tools:
            assert tool["credentials_required"] is False


# ---------------------------------------------------------------------------
# GET /tools/providers — empty
# ---------------------------------------------------------------------------


def test_get_providers_empty_initially():
    resp = client.get("/tools/providers")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["providers"] == []
    assert body["meta"]["count"] == 0


# ---------------------------------------------------------------------------
# POST /tools/providers — registration (stdio with fake command)
# ---------------------------------------------------------------------------


def test_register_provider_connect_failure_returns_201_with_connection_info():
    """stdio server that doesn't exist → registration succeeds (200) but connection.success=false.

    Patch 4 (code-review): registration is always persisted; connection result is
    returned in the response body rather than raising 422, so the client knows the
    provider_id and can retry via POST /tools/providers/{id}/test.
    """
    payload = {
        "name": "Test Provider",
        "transport_type": "stdio",
        "command": ["nonexistent-mcp-binary-xyz", "--stdio"],
        "env": {"API_KEY": "test-secret"},
        "description": "Test MCP provider",
    }
    resp = client.post("/tools/providers", json=payload)
    # Registration always returns 200; connection outcome is in response body
    assert resp.status_code == 200
    body = resp.json()
    data = body.get("data", {})
    provider = data.get("provider", {})
    connection = data.get("connection", {})
    assert "provider_id" in provider
    # Connection failed but provider is registered
    assert connection.get("success") is False
    # Provider should be fetchable via GET
    provider_id = provider["provider_id"]
    get_resp = client.get(f"/tools/providers/{provider_id}")
    assert get_resp.status_code == 200


def test_register_provider_validates_transport_type():
    payload = {
        "name": "Bad",
        "transport_type": "grpc",  # unsupported
        "env": {},
    }
    resp = client.post("/tools/providers", json=payload)
    assert resp.status_code == 422


def test_register_provider_missing_command_for_stdio():
    payload = {
        "name": "Bad",
        "transport_type": "stdio",
        "command": [],  # empty command → ProviderRegistrationError → 400
        "env": {},
    }
    resp = client.post("/tools/providers", json=payload)
    # register_provider raises ProviderRegistrationError for empty command → HTTP 400
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Credentials mask — never return plaintext
# ---------------------------------------------------------------------------


def test_credentials_never_returned_as_plaintext():
    """Register a provider; neither POST nor GET response must contain the secret value."""
    payload = {
        "name": "Secret Provider",
        "transport_type": "stdio",
        "command": ["nonexistent-binary"],
        "env": {"SECRET_KEY": "super-secret-value-do-not-leak"},
    }
    resp = client.post("/tools/providers", json=payload)
    # Patch 4: registration returns 200; connection.success may be false
    assert resp.status_code == 200
    # POST response itself must not leak the secret
    assert "super-secret-value-do-not-leak" not in resp.text
    provider_id = resp.json()["data"]["provider"]["provider_id"]

    get_resp = client.get(f"/tools/providers/{provider_id}")
    assert get_resp.status_code == 200
    body_text = get_resp.text
    assert "super-secret-value-do-not-leak" not in body_text


# ---------------------------------------------------------------------------
# DELETE /tools/providers/{id}
# ---------------------------------------------------------------------------


def _register_raw(name: str = "test") -> str:
    """Register a provider and return its provider_id (bypassing connection test)."""
    from xueban.runtime.tool_registry import register_provider
    result = register_provider(
        name=name,
        transport_type="stdio",
        command=["echo", "test"],
        server_url=None,
        env={"K": "v"},
        description="",
    )
    return result["provider_id"]


def test_delete_provider_success():
    pid = _register_raw()
    resp = client.delete(f"/tools/providers/{pid}")
    assert resp.status_code == 200
    assert resp.json()["data"]["deleted"] == pid
    # Should be gone
    get_resp = client.get(f"/tools/providers/{pid}")
    assert get_resp.status_code == 404


def test_delete_provider_not_found():
    resp = client.delete("/tools/providers/nonexistent-id")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /tools/providers/{id}/tools — cache empty initially
# ---------------------------------------------------------------------------


def test_get_provider_tools_empty_cache():
    pid = _register_raw()
    resp = client.get(f"/tools/providers/{pid}/tools")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["tools"] == []
    assert body["meta"]["cached"] is False


def test_get_provider_tools_not_found():
    resp = client.get("/tools/providers/does-not-exist/tools")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /tools/providers/{id}/test — re-test connection
# ---------------------------------------------------------------------------


def test_retest_nonexistent_provider():
    resp = client.post("/tools/providers/does-not-exist/test")
    assert resp.status_code == 404


def test_retest_fails_for_invalid_command():
    pid = _register_raw("retest-provider")
    resp = client.post(f"/tools/providers/{pid}/test")
    # echo is a valid command but not an MCP server → connect will fail
    assert resp.status_code == 422
    body = resp.json()
    detail = body.get("detail", {})
    assert detail.get("error") == "MCP_CONNECT_FAILED"


# ---------------------------------------------------------------------------
# GET /tools/providers/{id}
# ---------------------------------------------------------------------------


def test_get_single_provider_returns_masked():
    pid = _register_raw("single-get-test")
    resp = client.get(f"/tools/providers/{pid}")
    assert resp.status_code == 200
    provider = resp.json()["data"]["provider"]
    assert provider["provider_id"] == pid
    assert "env_encrypted" not in provider
    assert provider["env_masked"] == {"K": "***"}


def test_get_single_provider_not_found():
    resp = client.get("/tools/providers/nonexistent")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# credentials_ref indirection — AC3
# ---------------------------------------------------------------------------


def test_credentials_ref_not_inlined_in_policy():
    """ToolPolicy must reference credentials by ref, not inline secret."""
    from xueban.runtime.contracts_builder import ToolPolicy

    policy = ToolPolicy(
        tool_id="mcp:provider-uuid:search_notes",
        provider_id="provider-uuid",
        credentials_ref="provider-uuid",
        visibility="enabled",
        default_permission="ask",
    )
    dumped = policy.model_dump()
    assert dumped["credentials_ref"] == "provider-uuid"
    # No actual secret in the policy object
    assert "SECRET" not in str(dumped)
    assert "encrypted" not in str(dumped).lower()
