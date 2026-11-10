"""Tests for A2A Bridge (xueban/api/a2a_bridge.py).

Covers:
  - GET /.well-known/agent.json  → valid AgentCard JSON
  - POST /a2a/  a2a_sendMessage  → returns Task with id + status
  - POST /a2a/  a2a_getTask      → poll task by id
  - POST /a2a/  a2a_cancelTask   → cancel a working task
  - JSON-RPC error paths (invalid method, bad params, bad jsonrpc version)
  - Auth guard (when A2A_REQUIRE_AUTH=true)
  - Legacy method names (tasks/send, tasks/get)
  - A2AClient unit test (mocked HTTP)
"""

from __future__ import annotations

import json
import os
import unittest
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Force bridge enabled before importing the module so routes are registered
# ---------------------------------------------------------------------------
os.environ.setdefault("A2A_BRIDGE_ENABLED", "true")

from xueban.api.a2a_bridge import (  # noqa: E402
    A2AClient,
    A2AClientError,
    A2AMessage,
    A2APart,
    A2ATask,
    AgentCard,
    _build_agent_card,
    _check_inbound_auth,
    _get_task,
    _new_task,
    _rpc_err,
    _rpc_ok,
    _tasks,
)


# ---------------------------------------------------------------------------
# FastAPI TestClient fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch):
    """Create an isolated TestClient with A2A bridge mounted.

    The module-level A2A_BRIDGE_ENABLED constant freezes at first import —
    if another test (e.g. tests/api/test_chat_completions.py) imported
    xueban.server earlier in the session, the env var set at the top of
    this file arrives too late. Force the flag on the module object so the
    handlers are enabled regardless of import order.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import xueban.api.a2a_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "A2A_BRIDGE_ENABLED", True)
    app = FastAPI()
    app.include_router(bridge_mod.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def clear_tasks():
    """Reset in-memory task store before each test."""
    _tasks.clear()
    yield
    _tasks.clear()


# ---------------------------------------------------------------------------
# Helper: craft a minimal valid JSON-RPC request body
# ---------------------------------------------------------------------------

def _rpc_body(method: str, params: Dict[str, Any], rpc_id: Any = "test-1") -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}


def _valid_message() -> Dict[str, Any]:
    return {
        "messageId": "msg-abc123",
        "role": "user",
        "parts": [{"type": "text", "text": "Hello Xueban from A2A!"}],
    }


# ===========================================================================
# 1. AgentCard endpoint
# ===========================================================================


class TestAgentCardEndpoint:
    def test_returns_200(self, client):
        resp = client.get("/.well-known/agent.json")
        assert resp.status_code == 200

    def test_content_type_json(self, client):
        resp = client.get("/.well-known/agent.json")
        assert "application/json" in resp.headers.get("content-type", "")

    def test_required_fields_present(self, client):
        data = client.get("/.well-known/agent.json").json()
        assert "name" in data
        assert "description" in data
        assert "url" in data
        assert "skills" in data
        assert "capabilities" in data
        assert "version" in data

    def test_skills_non_empty(self, client):
        data = client.get("/.well-known/agent.json").json()
        assert len(data["skills"]) >= 1
        skill = data["skills"][0]
        assert "id" in skill
        assert "name" in skill
        assert "description" in skill

    def test_protocol_version_present(self, client):
        data = client.get("/.well-known/agent.json").json()
        assert "protocolVersion" in data
        assert data["protocolVersion"] == "1.0"

    def test_capabilities_has_expected_keys(self, client):
        data = client.get("/.well-known/agent.json").json()
        caps = data["capabilities"]
        assert "streaming" in caps
        assert "pushNotifications" in caps


# ===========================================================================
# 2. a2a_sendMessage
# ===========================================================================


class TestSendMessage:
    def test_returns_task_id(self, client):
        body = _rpc_body("a2a_sendMessage", {"message": _valid_message()})
        resp = client.post("/a2a/", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data
        assert "id" in data["result"]
        assert data["result"]["id"].startswith("a2a-")

    def test_returns_submitted_or_completed_state(self, client):
        body = _rpc_body("a2a_sendMessage", {"message": _valid_message()})
        resp = client.post("/a2a/", json=body)
        task = resp.json()["result"]
        assert task["status"]["state"] in ("submitted", "working", "completed", "failed")

    def test_missing_message_returns_error(self, client):
        body = _rpc_body("a2a_sendMessage", {})
        resp = client.post("/a2a/", json=body)
        data = resp.json()
        assert "error" in data
        assert data["error"]["code"] == -32602

    def test_invalid_message_format_returns_error(self, client):
        body = _rpc_body("a2a_sendMessage", {"message": "not-an-object"})
        resp = client.post("/a2a/", json=body)
        data = resp.json()
        assert "error" in data

    def test_task_stored_in_memory(self, client):
        body = _rpc_body("a2a_sendMessage", {"message": _valid_message()})
        resp = client.post("/a2a/", json=body)
        task_id = resp.json()["result"]["id"]
        assert task_id in _tasks

    def test_history_contains_message(self, client):
        body = _rpc_body("a2a_sendMessage", {"message": _valid_message()})
        resp = client.post("/a2a/", json=body)
        task = resp.json()["result"]
        assert len(task["history"]) >= 1
        assert task["history"][0]["role"] == "user"

    def test_context_id_propagated(self, client):
        msg = _valid_message()
        msg["contextId"] = "ctx-test-999"
        body = _rpc_body("a2a_sendMessage", {"message": msg, "contextId": "ctx-test-999"})
        resp = client.post("/a2a/", json=body)
        task = resp.json()["result"]
        assert task["contextId"] == "ctx-test-999"

    def test_legacy_tasks_send_method(self, client):
        """Ensure legacy method name 'tasks/send' is accepted."""
        body = _rpc_body("tasks/send", {"message": _valid_message()})
        resp = client.post("/a2a/", json=body)
        assert resp.status_code == 200
        assert "result" in resp.json()


# ===========================================================================
# 3. a2a_getTask
# ===========================================================================


class TestGetTask:
    def test_get_existing_task(self, client):
        # Create a task first
        send_body = _rpc_body("a2a_sendMessage", {"message": _valid_message()})
        task_id = client.post("/a2a/", json=send_body).json()["result"]["id"]

        get_body = _rpc_body("a2a_getTask", {"taskId": task_id})
        resp = client.post("/a2a/", json=get_body)
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data
        assert data["result"]["id"] == task_id

    def test_get_nonexistent_task(self, client):
        body = _rpc_body("a2a_getTask", {"taskId": "nonexistent-task-xyz"})
        resp = client.post("/a2a/", json=body)
        data = resp.json()
        assert "error" in data
        assert data["error"]["code"] == -32001

    def test_missing_task_id_returns_error(self, client):
        body = _rpc_body("a2a_getTask", {})
        resp = client.post("/a2a/", json=body)
        data = resp.json()
        assert "error" in data
        assert data["error"]["code"] == -32602

    def test_legacy_tasks_get_method(self, client):
        """Ensure legacy method name 'tasks/get' is accepted."""
        send_body = _rpc_body("a2a_sendMessage", {"message": _valid_message()})
        task_id = client.post("/a2a/", json=send_body).json()["result"]["id"]

        get_body = _rpc_body("tasks/get", {"taskId": task_id})
        resp = client.post("/a2a/", json=get_body)
        assert resp.status_code == 200
        assert resp.json()["result"]["id"] == task_id


# ===========================================================================
# 4. a2a_cancelTask
# ===========================================================================


class TestCancelTask:
    def _create_working_task(self) -> str:
        msg = A2AMessage(parts=[A2APart(type="text", text="cancel me")])
        task = _new_task(msg)
        task.status.state = "working"
        return task.id

    def test_cancel_working_task(self, client):
        task_id = self._create_working_task()
        body = _rpc_body("a2a_cancelTask", {"taskId": task_id})
        resp = client.post("/a2a/", json=body)
        assert resp.status_code == 200
        task = resp.json()["result"]
        assert task["status"]["state"] == "canceled"

    def test_cancel_already_completed_returns_error(self, client):
        msg = A2AMessage(parts=[A2APart(type="text", text="done")])
        task = _new_task(msg)
        task.status.state = "completed"

        body = _rpc_body("a2a_cancelTask", {"taskId": task.id})
        resp = client.post("/a2a/", json=body)
        data = resp.json()
        assert "error" in data
        assert data["error"]["code"] == -32002

    def test_cancel_nonexistent_task(self, client):
        body = _rpc_body("a2a_cancelTask", {"taskId": "ghost-task"})
        resp = client.post("/a2a/", json=body)
        assert "error" in resp.json()
        assert resp.json()["error"]["code"] == -32001


# ===========================================================================
# 5. JSON-RPC error paths
# ===========================================================================


class TestJsonRpcErrors:
    def test_wrong_jsonrpc_version(self, client):
        body = {"jsonrpc": "1.0", "id": 1, "method": "a2a_getTask", "params": {}}
        resp = client.post("/a2a/", json=body)
        data = resp.json()
        assert "error" in data
        assert data["error"]["code"] == -32600

    def test_unknown_method(self, client):
        body = _rpc_body("unknownMethod", {})
        resp = client.post("/a2a/", json=body)
        data = resp.json()
        assert "error" in data
        assert data["error"]["code"] == -32601

    def test_non_json_body(self, client):
        resp = client.post("/a2a/", content=b"not json", headers={"Content-Type": "application/json"})
        data = resp.json()
        assert "error" in data
        assert data["error"]["code"] in (-32700, -32600)

    def test_array_body_rejected(self, client):
        resp = client.post("/a2a/", json=[{"method": "a2a_getTask"}])
        data = resp.json()
        assert "error" in data

    def test_missing_method_field(self, client):
        body = {"jsonrpc": "2.0", "id": 1, "params": {}}
        resp = client.post("/a2a/", json=body)
        data = resp.json()
        assert "error" in data

    def test_params_must_be_object(self, client):
        body = {"jsonrpc": "2.0", "id": 1, "method": "a2a_getTask", "params": ["not", "an", "object"]}
        resp = client.post("/a2a/", json=body)
        data = resp.json()
        assert "error" in data
        assert data["error"]["code"] == -32602


# ===========================================================================
# 6. Auth guard
# ===========================================================================


class TestAuthGuard:
    def test_no_auth_when_disabled(self, client):
        """When A2A_REQUIRE_AUTH is not set, requests pass through."""
        with patch.dict(os.environ, {"A2A_REQUIRE_AUTH": "false", "A2A_API_KEY": ""}):
            from xueban.api import a2a_bridge
            original = a2a_bridge._A2A_REQUIRE_AUTH
            a2a_bridge._A2A_REQUIRE_AUTH = False
            try:
                body = _rpc_body("a2a_sendMessage", {"message": _valid_message()})
                resp = client.post("/a2a/", json=body)
                assert resp.status_code == 200
            finally:
                a2a_bridge._A2A_REQUIRE_AUTH = original

    def test_auth_required_no_token_rejected(self, client):
        """When auth is required and no token provided, 401 is returned."""
        from xueban.api import a2a_bridge
        orig_auth = a2a_bridge._A2A_REQUIRE_AUTH
        orig_key = a2a_bridge._A2A_API_KEY
        a2a_bridge._A2A_REQUIRE_AUTH = True
        a2a_bridge._A2A_API_KEY = "secret-token"
        try:
            body = _rpc_body("a2a_sendMessage", {"message": _valid_message()})
            resp = client.post("/a2a/", json=body)
            assert resp.status_code == 401
        finally:
            a2a_bridge._A2A_REQUIRE_AUTH = orig_auth
            a2a_bridge._A2A_API_KEY = orig_key

    def test_auth_required_valid_bearer_accepted(self, client):
        """When auth is required and a valid Bearer token is provided, request passes."""
        from xueban.api import a2a_bridge
        orig_auth = a2a_bridge._A2A_REQUIRE_AUTH
        orig_key = a2a_bridge._A2A_API_KEY
        a2a_bridge._A2A_REQUIRE_AUTH = True
        a2a_bridge._A2A_API_KEY = "secret-token"
        try:
            body = _rpc_body("a2a_sendMessage", {"message": _valid_message()})
            resp = client.post(
                "/a2a/", json=body,
                headers={"Authorization": "Bearer secret-token"},
            )
            assert resp.status_code == 200
        finally:
            a2a_bridge._A2A_REQUIRE_AUTH = orig_auth
            a2a_bridge._A2A_API_KEY = orig_key


# ===========================================================================
# 7. AgentCard builder unit tests
# ===========================================================================


class TestAgentCardBuilder:
    def test_build_returns_agent_card_instance(self):
        card = _build_agent_card()
        assert isinstance(card, AgentCard)

    def test_card_has_three_default_skills(self):
        card = _build_agent_card()
        assert len(card.skills) == 3

    def test_card_url_uses_sf_base_url(self):
        with patch.dict(os.environ, {"SF_BASE_URL": "https://example.com"}):
            from xueban.api import a2a_bridge
            orig = a2a_bridge._SF_BASE_URL
            a2a_bridge._SF_BASE_URL = "https://example.com"
            try:
                card = _build_agent_card()
                assert "example.com" in card.url
            finally:
                a2a_bridge._SF_BASE_URL = orig

    def test_no_security_by_default(self):
        card = _build_agent_card()
        assert card.securitySchemes is None
        assert card.security is None

    def test_security_added_when_auth_enabled(self):
        from xueban.api import a2a_bridge
        orig_auth = a2a_bridge._A2A_REQUIRE_AUTH
        orig_key = a2a_bridge._A2A_API_KEY
        a2a_bridge._A2A_REQUIRE_AUTH = True
        a2a_bridge._A2A_API_KEY = "my-key"
        try:
            card = _build_agent_card()
            assert card.securitySchemes is not None
            assert "apiKey" in card.securitySchemes
        finally:
            a2a_bridge._A2A_REQUIRE_AUTH = orig_auth
            a2a_bridge._A2A_API_KEY = orig_key


# ===========================================================================
# 8. A2AClient (outbound) — mocked HTTP
# ===========================================================================


class TestA2AClient:
    @pytest.mark.asyncio
    async def test_send_message_returns_task(self):
        """send_message should call remote /a2a/ and return Task dict."""
        fake_task = {
            "id": "a2a-remote-xyz",
            "status": {"state": "submitted", "timestamp": "2026-05-04T00:00:00+00:00"},
            "artifacts": [],
            "history": [],
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"jsonrpc": "2.0", "id": "x", "result": fake_task}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            client = A2AClient("https://remote.example.com/a2a/")
            result = await client.send_message("Hello remote agent!")

        assert result["id"] == "a2a-remote-xyz"
        assert result["status"]["state"] == "submitted"

    @pytest.mark.asyncio
    async def test_send_message_raises_on_http_error(self):
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            client = A2AClient("https://remote.example.com/a2a/")
            with pytest.raises(A2AClientError, match="HTTP 500"):
                await client.send_message("test")

    @pytest.mark.asyncio
    async def test_send_message_raises_on_rpc_error(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": "x",
            "error": {"code": -32601, "message": "Method not found"},
        }

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            client = A2AClient("https://remote.example.com/a2a/", api_key="my-key")
            with pytest.raises(A2AClientError, match="Method not found"):
                await client.send_message("test")

    @pytest.mark.asyncio
    async def test_get_task(self):
        fake_task = {
            "id": "a2a-remote-xyz",
            "status": {"state": "completed", "timestamp": "2026-05-04T00:00:00+00:00"},
            "artifacts": [],
            "history": [],
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"jsonrpc": "2.0", "id": "x", "result": fake_task}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            client = A2AClient("https://remote.example.com/a2a/")
            result = await client.get_task("a2a-remote-xyz")

        assert result["status"]["state"] == "completed"

    @pytest.mark.asyncio
    async def test_bearer_token_sent_in_header(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "jsonrpc": "2.0", "id": "x",
            "result": {"id": "t1", "status": {"state": "submitted", "timestamp": ""}, "artifacts": [], "history": []},
        }
        captured_headers = {}

        async def _capture_post(url, json=None, headers=None, **kwargs):
            captured_headers.update(headers or {})
            return mock_response

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_http = AsyncMock()
            mock_http.post = _capture_post
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            client = A2AClient("https://remote.example.com/a2a/", api_key="super-secret")
            await client.send_message("test bearer")

        assert captured_headers.get("Authorization") == "Bearer super-secret"


# ===========================================================================
# 9. Data format conformance — verify output matches A2A spec shape
# ===========================================================================


class TestDataFormatConformance:
    def test_task_id_format(self, client):
        body = _rpc_body("a2a_sendMessage", {"message": _valid_message()})
        task = client.post("/a2a/", json=body).json()["result"]
        assert isinstance(task["id"], str)
        assert len(task["id"]) > 4

    def test_task_status_has_state_and_timestamp(self, client):
        body = _rpc_body("a2a_sendMessage", {"message": _valid_message()})
        task = client.post("/a2a/", json=body).json()["result"]
        status = task["status"]
        assert "state" in status
        assert "timestamp" in status
        assert isinstance(status["timestamp"], str)

    def test_jsonrpc_response_envelope(self, client):
        body = _rpc_body("a2a_sendMessage", {"message": _valid_message()}, rpc_id="my-custom-id")
        resp = client.post("/a2a/", json=body).json()
        assert resp.get("jsonrpc") == "2.0"
        assert resp.get("id") == "my-custom-id"
        assert "result" in resp

    def test_message_parts_preserved_in_history(self, client):
        msg = _valid_message()
        msg["parts"][0]["text"] = "A2A spec compliance test payload"
        body = _rpc_body("a2a_sendMessage", {"message": msg})
        task = client.post("/a2a/", json=body).json()["result"]
        first_user_msg = task["history"][0]
        assert first_user_msg["parts"][0]["text"] == "A2A spec compliance test payload"

    def test_agent_card_skills_have_required_fields(self, client):
        data = client.get("/.well-known/agent.json").json()
        for skill in data["skills"]:
            assert "id" in skill, f"Skill missing 'id': {skill}"
            assert "name" in skill, f"Skill missing 'name': {skill}"
            assert "description" in skill, f"Skill missing 'description': {skill}"
