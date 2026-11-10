"""Story 2-11: ACP Server — unit tests for ACPConnectionManager + REST API.

Covers:
- authenticate(): valid auth, invalid key, timeout, wrong type, bad JSON
- handshake(): valid manifest, timeout fallback, wrong type fallback, ack sent
- handle_session(): heartbeat, task_complete/error decrements, timeout → offline, cleanup
- send_task() / cancel_task(): dispatch, unknown session
- REST: GET /api/acp/status, GET /api/acp/sessions, POST /api/acp/task (404)
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import xueban.runtime.acp.server as _server_module
import xueban.runtime.acp.registry as _registry_module
from xueban.api.acp_server import router as acp_router
from xueban.runtime.acp.registry import AgentCapabilityManifest, AgentRegistry
from xueban.runtime.acp.server import ACPConnectionManager, AgentSession


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset module-level singletons to prevent state leakage between tests.

    Always restore _registry to a live AgentRegistry (not None) so that
    other test modules running after this file aren't left with a broken
    get_registry() call.
    """
    _server_module._manager = None
    _registry_module._registry = AgentRegistry()
    yield
    _server_module._manager = None
    _registry_module._registry = AgentRegistry()


@pytest.fixture()
def manager() -> ACPConnectionManager:
    return ACPConnectionManager()


@pytest.fixture()
def test_client() -> TestClient:
    app = FastAPI()
    app.include_router(acp_router)
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Mock WebSocket
# ---------------------------------------------------------------------------


class MockWebSocket:
    """Minimal WebSocket mock for ACPConnectionManager tests.

    Raises an Exception (simulating disconnect) when the message queue is
    exhausted, so handle_session exits cleanly without real timeouts.
    """

    def __init__(self, messages: Optional[List[str]] = None) -> None:
        self._messages = list(messages or [])
        self._idx = 0
        self.sent: List[str] = []
        self.closed = False
        self.close_code: Optional[int] = None
        self.client = ("127.0.0.1", 12345)

    async def accept(self) -> None:
        pass

    async def receive_text(self) -> str:
        if self._idx >= len(self._messages):
            raise Exception("mock: no more messages (disconnected)")
        msg = self._messages[self._idx]
        self._idx += 1
        return msg

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        self.closed = True
        self.close_code = code

    def sent_types(self) -> List[str]:
        return [json.loads(m).get("type") for m in self.sent]


class HangingWebSocket(MockWebSocket):
    """WebSocket that never delivers a message (simulates idle / timeout)."""

    async def receive_text(self) -> str:
        await asyncio.sleep(1000)
        return ""  # pragma: no cover


def _make_session(
    agent_id: str = "claude-code-abc123",
    session_id: str = "sess-test-001",
    workspace_id: str = "ws-lab",
) -> AgentSession:
    return AgentSession(
        session_id=session_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        api_key_hash="deadbeef",
        connected_at=datetime.now(timezone.utc),
        agent_hint="claude-code",
    )


# ---------------------------------------------------------------------------
# authenticate()
# ---------------------------------------------------------------------------


class TestAuthenticate:
    @pytest.mark.asyncio
    async def test_valid_auth_returns_session(self, manager):
        auth = json.dumps({
            "type": "auth",
            "api_key": "sf-testkey",
            "workspace_id": "ws-1",
            "agent_hint": "claude-code",
        })
        ws = MockWebSocket(messages=[auth])
        session = await manager.authenticate(ws)
        assert session is not None
        assert session.workspace_id == "ws-1"
        assert session.agent_hint == "claude-code"
        assert "claude-code" in session.agent_id
        assert "auth_ack" in ws.sent_types()

    @pytest.mark.asyncio
    async def test_auth_ack_fields(self, manager):
        auth = json.dumps({"type": "auth", "api_key": "sf-key", "workspace_id": "lab-ws"})
        ws = MockWebSocket(messages=[auth])
        session = await manager.authenticate(ws)
        assert session is not None
        ack_raw = next(m for m in ws.sent if json.loads(m).get("type") == "auth_ack")
        ack = json.loads(ack_raw)
        assert ack["session_id"] == session.session_id
        assert ack["workspace"] == "lab-ws"
        assert "Xueban ACP Server" in ack["message"]

    @pytest.mark.asyncio
    async def test_invalid_api_key_sends_auth_error(self, manager):
        auth = json.dumps({"type": "auth", "api_key": "", "workspace_id": ""})
        ws = MockWebSocket(messages=[auth])
        session = await manager.authenticate(ws)
        assert session is None
        assert "auth_error" in ws.sent_types()

    @pytest.mark.asyncio
    async def test_wrong_message_type_returns_none(self, manager):
        wrong = json.dumps({"type": "capability_response", "manifest": {}})
        ws = MockWebSocket(messages=[wrong])
        session = await manager.authenticate(ws)
        assert session is None
        assert "auth_ack" not in ws.sent_types()

    @pytest.mark.asyncio
    async def test_auth_timeout_returns_none(self, manager):
        ws = HangingWebSocket()
        session = await manager.authenticate(ws, timeout=0.05)
        assert session is None

    @pytest.mark.asyncio
    async def test_auth_invalid_json_returns_none(self, manager):
        ws = MockWebSocket(messages=["not-valid-json{{{"])
        session = await manager.authenticate(ws)
        assert session is None


# ---------------------------------------------------------------------------
# handshake()
# ---------------------------------------------------------------------------


class TestHandshake:
    @pytest.mark.asyncio
    async def test_valid_manifest_fields_parsed(self, manager):
        cap_response = json.dumps({
            "type": "capability_response",
            "manifest": {
                "agent_id": "claude-code-ext",
                "display_name": "Claude Code (local)",
                "version": "1.2.0",
                "tools": [
                    {"name": "shell", "description": "Run commands"},
                    {"name": "fs", "description": "File system"},
                ],
                "streaming": True,
                "max_concurrency": 2,
            },
        })
        ws = MockWebSocket(messages=[cap_response])
        session = _make_session()
        manifest = await manager.handshake(ws, session)
        assert manifest.agent_id == "claude-code-ext"
        assert manifest.display_name == "Claude Code (local)"
        assert manifest.version == "1.2.0"
        assert len(manifest.tools) == 2
        assert manifest.streaming is True
        assert manifest.max_concurrency == 2

    @pytest.mark.asyncio
    async def test_capability_request_sent_first(self, manager):
        cap_response = json.dumps({
            "type": "capability_response",
            "manifest": {"agent_id": "a1", "display_name": "A"},
        })
        ws = MockWebSocket(messages=[cap_response])
        await manager.handshake(ws, _make_session())
        assert ws.sent_types()[0] == "capability_request"

    @pytest.mark.asyncio
    async def test_capability_ack_sent_on_success(self, manager):
        cap_response = json.dumps({
            "type": "capability_response",
            "manifest": {"agent_id": "a1", "display_name": "A"},
        })
        ws = MockWebSocket(messages=[cap_response])
        await manager.handshake(ws, _make_session())
        assert "capability_ack" in ws.sent_types()

    @pytest.mark.asyncio
    async def test_timeout_returns_default_legacy(self, manager):
        ws = HangingWebSocket()
        session = _make_session(agent_id="ext-fallback")
        manifest = await manager.handshake(ws, session, timeout=0.05)
        assert manifest.agent_id == session.agent_id
        assert any(t.name == "shell" for t in manifest.tools)

    @pytest.mark.asyncio
    async def test_wrong_type_returns_default_legacy(self, manager):
        wrong = json.dumps({"type": "auth", "api_key": "foo"})
        ws = MockWebSocket(messages=[wrong])
        session = _make_session(agent_id="ext-wrong-type")
        manifest = await manager.handshake(ws, session)
        assert manifest.agent_id == session.agent_id


# ---------------------------------------------------------------------------
# handle_session() — message routing
# ---------------------------------------------------------------------------


class TestHandleSession:
    @pytest.mark.asyncio
    async def test_heartbeat_sends_ack_and_updates_registry(self, manager):
        reg = AgentRegistry()
        manifest = AgentCapabilityManifest(agent_id="ext-hb", display_name="HB")
        await reg.register(manifest, is_native=False)
        session = _make_session(agent_id="ext-hb", session_id="sess-hb")
        heartbeat = json.dumps({"type": "heartbeat", "active_tasks": 3})
        ws = MockWebSocket(messages=[heartbeat])
        with patch("xueban.runtime.acp.server.get_registry", return_value=reg):
            await manager.handle_session(ws, session)
        assert "heartbeat_ack" in ws.sent_types()
        entry = await reg.get("ext-hb")
        assert entry is not None
        assert entry.active_tasks == 3

    @pytest.mark.asyncio
    async def test_task_complete_decrements_task_counter(self, manager):
        reg = AgentRegistry()
        manifest = AgentCapabilityManifest(
            agent_id="ext-tc", display_name="TC", max_concurrency=3
        )
        await reg.register(manifest, is_native=False)
        await reg.increment_task("ext-tc")
        session = _make_session(agent_id="ext-tc", session_id="sess-tc")
        task_complete = json.dumps({
            "type": "task_complete",
            "task_id": "task-abc",
            "result": {"status": "success"},
        })
        ws = MockWebSocket(messages=[task_complete])
        with patch("xueban.runtime.acp.server.get_registry", return_value=reg):
            await manager.handle_session(ws, session)
        entry = await reg.get("ext-tc")
        assert entry.active_tasks == 0

    @pytest.mark.asyncio
    async def test_task_error_decrements_task_counter(self, manager):
        reg = AgentRegistry()
        manifest = AgentCapabilityManifest(agent_id="ext-te", display_name="TE")
        await reg.register(manifest, is_native=False)
        await reg.increment_task("ext-te")
        session = _make_session(agent_id="ext-te", session_id="sess-te")
        error_msg = json.dumps({
            "type": "task_error",
            "task_id": "task-xyz",
            "error": {"code": "EXEC_ERROR"},
        })
        ws = MockWebSocket(messages=[error_msg])
        with patch("xueban.runtime.acp.server.get_registry", return_value=reg):
            await manager.handle_session(ws, session)
        entry = await reg.get("ext-te")
        assert entry.active_tasks == 0

    @pytest.mark.asyncio
    async def test_timeout_marks_agent_offline(self, manager):
        reg = AgentRegistry()
        manifest = AgentCapabilityManifest(agent_id="ext-to", display_name="TO")
        await reg.register(manifest, is_native=False)
        session = _make_session(agent_id="ext-to", session_id="sess-to")
        with patch(
            "xueban.runtime.acp.server.asyncio.wait_for",
            side_effect=asyncio.TimeoutError,
        ):
            with patch("xueban.runtime.acp.server.get_registry", return_value=reg):
                await manager.handle_session(HangingWebSocket(), session)
        entry = await reg.get("ext-to")
        assert entry.status == "offline"

    @pytest.mark.asyncio
    async def test_session_removed_from_dict_after_disconnect(self, manager):
        reg = AgentRegistry()
        manifest = AgentCapabilityManifest(agent_id="ext-disc", display_name="D")
        await reg.register(manifest, is_native=False)
        session = _make_session(agent_id="ext-disc", session_id="sess-disc")
        heartbeat = json.dumps({"type": "heartbeat", "active_tasks": 0})
        ws = MockWebSocket(messages=[heartbeat])
        with patch("xueban.runtime.acp.server.get_registry", return_value=reg):
            await manager.handle_session(ws, session)
        assert "sess-disc" not in manager._sessions
        assert "sess-disc" not in manager._session_agents

    @pytest.mark.asyncio
    async def test_disconnect_marks_agent_offline(self, manager):
        reg = AgentRegistry()
        manifest = AgentCapabilityManifest(agent_id="ext-offline", display_name="O")
        await reg.register(manifest, is_native=False)
        session = _make_session(agent_id="ext-offline", session_id="sess-offline")
        ws = MockWebSocket(messages=[])  # immediately "disconnects"
        with patch("xueban.runtime.acp.server.get_registry", return_value=reg):
            await manager.handle_session(ws, session)
        entry = await reg.get("ext-offline")
        assert entry.status == "offline"

    @pytest.mark.asyncio
    async def test_task_cancelled_decrements_task_counter(self, manager):
        reg = AgentRegistry()
        manifest = AgentCapabilityManifest(agent_id="ext-tc-cancel", display_name="TC Cancel")
        await reg.register(manifest, is_native=False)
        await reg.increment_task("ext-tc-cancel")
        session = _make_session(agent_id="ext-tc-cancel", session_id="sess-tc-cancel")
        cancelled_msg = json.dumps({"type": "task_cancelled", "task_id": "task-cancel-123"})
        ws = MockWebSocket(messages=[cancelled_msg])
        with patch("xueban.runtime.acp.server.get_registry", return_value=reg):
            await manager.handle_session(ws, session)
        entry = await reg.get("ext-tc-cancel")
        assert entry.active_tasks == 0


# ---------------------------------------------------------------------------
# send_task() / cancel_task()
# ---------------------------------------------------------------------------


class TestSendCancelTask:
    @pytest.mark.asyncio
    async def test_send_task_dispatches_task_message(self, manager):
        ws = MockWebSocket()
        reg = AgentRegistry()
        manifest = AgentCapabilityManifest(agent_id="ext-send", display_name="S")
        await reg.register(manifest)
        session = _make_session(agent_id="ext-send", session_id="sess-send-001")
        # Simulate session registered via handle_session
        async with manager._lock:
            manager._sessions["sess-send-001"] = ws
            manager._session_agents["sess-send-001"] = "ext-send"
        with patch("xueban.runtime.acp.server.get_registry", return_value=reg):
            result = await manager.send_task(
                session_id="sess-send-001",
                task_id="t-001",
                instruction="Run tests",
                context={"workspace_id": "ws-lab"},
                timeout_seconds=120,
            )
        assert result is True
        task_msgs = [
            json.loads(m) for m in ws.sent if json.loads(m).get("type") == "task"
        ]
        assert len(task_msgs) == 1
        assert task_msgs[0]["task_id"] == "t-001"
        assert task_msgs[0]["instruction"] == "Run tests"
        assert task_msgs[0]["timeout_seconds"] == 120

    @pytest.mark.asyncio
    async def test_send_task_increments_task_counter_by_agent_id(self, manager):
        """Task counter must be incremented by agent_id (not session_id)."""
        ws = MockWebSocket()
        reg = AgentRegistry()
        manifest = AgentCapabilityManifest(agent_id="ext-incr", display_name="I")
        await reg.register(manifest)
        session = _make_session(agent_id="ext-incr", session_id="sess-incr-001")
        async with manager._lock:
            manager._sessions["sess-incr-001"] = ws
            manager._session_agents["sess-incr-001"] = "ext-incr"
        with patch("xueban.runtime.acp.server.get_registry", return_value=reg):
            await manager.send_task(
                session_id="sess-incr-001",
                task_id="t-incr",
                instruction="ping",
            )
        entry = await reg.get("ext-incr")
        assert entry.active_tasks == 1

    @pytest.mark.asyncio
    async def test_send_task_returns_false_for_unknown_session(self, manager):
        reg = AgentRegistry()
        with patch("xueban.runtime.acp.server.get_registry", return_value=reg):
            result = await manager.send_task(
                session_id="nonexistent",
                task_id="t-000",
                instruction="ping",
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_task_sends_task_cancel_message(self, manager):
        ws = MockWebSocket()
        async with manager._lock:
            manager._sessions["sess-cancel-001"] = ws
        result = await manager.cancel_task("sess-cancel-001", "task-to-cancel")
        assert result is True
        cancel_msgs = [
            json.loads(m) for m in ws.sent if json.loads(m).get("type") == "task_cancel"
        ]
        assert len(cancel_msgs) == 1
        assert cancel_msgs[0]["task_id"] == "task-to-cancel"

    @pytest.mark.asyncio
    async def test_cancel_task_returns_false_for_unknown_session(self, manager):
        result = await manager.cancel_task("sess-unknown", "task-nope")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_task_still_dispatches_when_agent_id_mapping_absent(self, manager):
        """Task message is still sent even when _session_agents has no entry.

        The counter increment is skipped (and a warning logged), but the
        WebSocket dispatch must still succeed so the agent can execute.
        """
        ws = MockWebSocket()
        reg = AgentRegistry()
        # Seed _sessions but deliberately omit _session_agents entry
        async with manager._lock:
            manager._sessions["sess-no-mapping"] = ws
        with patch("xueban.runtime.acp.server.get_registry", return_value=reg):
            result = await manager.send_task(
                session_id="sess-no-mapping",
                task_id="t-no-map",
                instruction="ping",
            )
        assert result is True
        task_msgs = [json.loads(m) for m in ws.sent if json.loads(m).get("type") == "task"]
        assert len(task_msgs) == 1


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------


class TestACPRestEndpoints:
    @pytest.fixture(autouse=True)
    def _patch_registry_manager(self):
        """Ensure get_registry resolves to a live object in both acp_server.py and server.py.

        Both modules import get_registry from xueban.runtime.acp.registry, so we
        must patch the name as seen from EACH importing module.
        """
        from xueban.runtime.acp.registry import AgentRegistry
        fresh_registry = AgentRegistry()
        with (
            patch("xueban.api.acp_server.get_registry", return_value=fresh_registry),
            patch("xueban.runtime.acp.server.get_registry", return_value=fresh_registry),
        ):
            yield

    def test_status_returns_running(self, test_client):
        resp = test_client.get("/api/acp/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["server"] == "running"
        assert data["connected_agents"] == 0
        assert data["online_agents"] == 0

    def test_sessions_empty_initially(self, test_client):
        resp = test_client.get("/api/acp/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"] == []
        assert data["meta"]["total"] == 0

    def test_dispatch_task_unknown_session_returns_404(self, test_client):
        resp = test_client.post(
            "/api/acp/task",
            json={
                "session_id": "sess-does-not-exist",
                "instruction": "echo hello",
            },
        )
        assert resp.status_code == 404
        body = resp.json()
        assert body["detail"]["error"]["code"] == "SESSION_NOT_FOUND"

    def test_dispatch_task_response_shape(self, test_client):
        """POST /api/acp/task to a known session returns task_id + status."""
        real_manager = _server_module.get_manager()
        ws = MockWebSocket()
        # Direct dict mutation — avoids event-loop coupling issues in sync test context
        real_manager._sessions["sess-rest-001"] = ws
        real_manager._session_agents["sess-rest-001"] = "ext-rest-agent"
        resp = test_client.post(
            "/api/acp/task",
            json={
                "session_id": "sess-rest-001",
                "instruction": "echo hello from REST",
            },
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["session_id"] == "sess-rest-001"
        assert data["status"] == "dispatched"
        assert data["task_id"].startswith("task-")
