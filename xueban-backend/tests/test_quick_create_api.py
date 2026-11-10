"""Quick Create Agent API tests — Story 12.1.

Covers:
  AC1/AC2 — POST /api/agents: creates agent with default Blueprint
  AC2     — Blueprint has default tools (shell/fs/web), llm_provider, max_iterations
  AC3     — GET /api/agents: lists created agents
  AC3     — GET /api/agents/{agent_id}: returns single agent
  AC5     — Empty list when no agents exist
  Delete  — DELETE /api/agents/{agent_id}: removes agent
  Errors  — 404 for unknown agent_id; 422 for missing required fields
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xueban.runtime.defaults import DEFAULT_MCP_SERVERS
from xueban.server import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_agents_dir(tmp_path: Path, monkeypatch):
    import xueban.api.agents as _mod
    monkeypatch.setattr(_mod, "_AGENTS_DIR", tmp_path / "agents")
    yield


# ---------------------------------------------------------------------------
# AC1/AC2 — POST /api/agents
# ---------------------------------------------------------------------------


class TestQuickCreate:
    def test_creates_agent_with_name_and_soul(self):
        resp = client.post("/api/agents", json={"name": "论文复现助手", "soul": "你是严谨的科研助理"})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["source"] == "quick_hire"
        assert "agent_id" in data
        assert data["agent_id"].startswith("agent-")
        assert "blueprint" in data

    def test_blueprint_has_default_tools(self):
        resp = client.post("/api/agents", json={"name": "TestAgent", "soul": "Do stuff"})
        bp = resp.json()["data"]["blueprint"]
        role = bp["role_profiles"][0]
        for tool in DEFAULT_MCP_SERVERS:
            assert tool in role["tools"]

    def test_blueprint_has_default_provider(self):
        resp = client.post("/api/agents", json={"name": "TestAgent", "soul": "Do stuff"})
        bp = resp.json()["data"]["blueprint"]
        role = bp["role_profiles"][0]
        assert role["executor_provider"] == "anthropic"

    def test_blueprint_name_matches_input(self):
        resp = client.post("/api/agents", json={"name": "MyBot", "soul": "Handle tasks"})
        bp = resp.json()["data"]["blueprint"]
        assert bp["name"] == "MyBot"
        assert bp["role_profiles"][0]["name"] == "MyBot"

    def test_blueprint_goal_is_soul(self):
        soul = "你负责处理所有编程任务"
        resp = client.post("/api/agents", json={"name": "Coder", "soul": soul})
        bp = resp.json()["data"]["blueprint"]
        assert bp["goal"] == soul

    def test_returns_422_when_name_missing(self):
        resp = client.post("/api/agents", json={"soul": "Something"})
        assert resp.status_code == 422

    def test_returns_422_when_soul_missing(self):
        resp = client.post("/api/agents", json={"name": "Bot"})
        assert resp.status_code == 422

    def test_returns_422_when_name_empty(self):
        resp = client.post("/api/agents", json={"name": "", "soul": "soul text"})
        assert resp.status_code == 422

    def test_workspace_id_defaults_to_default(self):
        resp = client.post("/api/agents", json={"name": "A", "soul": "B"})
        assert resp.status_code == 200

    def test_workspace_id_custom(self):
        resp = client.post("/api/agents", json={"name": "A", "soul": "B", "workspace_id": "ws-99"})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# avatar_color — 头像色落库（统一头像色字段）
# ---------------------------------------------------------------------------


class TestAvatarColor:
    def test_create_persists_avatar_color(self):
        resp = client.post("/api/agents", json={"name": "Nova", "soul": "x", "avatar_color": "#e11d8f"})
        assert resp.status_code == 200
        assert resp.json()["data"]["avatar_color"] == "#e11d8f"
        # GET 单个也带回字段
        aid = resp.json()["data"]["agent_id"]
        got = client.get(f"/api/agents/{aid}")
        assert got.json()["data"]["avatar_color"] == "#e11d8f"

    def test_create_without_color_is_none(self):
        resp = client.post("/api/agents", json={"name": "Auto", "soul": "x"})
        assert resp.status_code == 200
        assert resp.json()["data"]["avatar_color"] is None

    def test_create_rejects_non_hex(self):
        # 长度合法（≤9）但非 #rrggbb → 命中自定义校验器（400），而非 Pydantic 长度 422
        resp = client.post("/api/agents", json={"name": "Bad", "soul": "x", "avatar_color": "#12xyz"})
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "INVALID_AVATAR_COLOR"

    def test_create_rejects_overlong_color(self):
        # 超长 → Pydantic max_length 拦截（422）
        resp = client.post("/api/agents", json={"name": "Bad2", "soul": "x", "avatar_color": "red; }body{ evil"})
        assert resp.status_code == 422

    def test_patch_sets_and_clears_color(self):
        aid = client.post("/api/agents", json={"name": "Patchy", "soul": "x"}).json()["data"]["agent_id"]
        # 设置
        r1 = client.patch(f"/api/agents/{aid}", json={"avatar_color": "#0891b2"})
        assert r1.status_code == 200
        assert r1.json()["data"]["avatar_color"] == "#0891b2"
        # 清除（"" → None）
        r2 = client.patch(f"/api/agents/{aid}", json={"avatar_color": ""})
        assert r2.status_code == 200
        assert r2.json()["data"]["avatar_color"] is None


# ---------------------------------------------------------------------------
# List and Get
# ---------------------------------------------------------------------------


class TestListAndGet:
    def test_empty_list_when_no_agents(self):
        resp = client.get("/api/agents")
        assert resp.status_code == 200
        assert resp.json()["data"] == []
        assert resp.json()["meta"]["total"] == 0

    def test_list_shows_created_agent(self):
        client.post("/api/agents", json={"name": "ListBot", "soul": "A bot"})
        resp = client.get("/api/agents")
        assert resp.status_code == 200
        names = [a["name"] for a in resp.json()["data"]]
        assert "ListBot" in names

    def test_get_single_agent(self):
        create_resp = client.post("/api/agents", json={"name": "GetBot", "soul": "get me"})
        agent_id = create_resp.json()["data"]["agent_id"]

        resp = client.get(f"/api/agents/{agent_id}")
        assert resp.status_code == 200
        assert resp.json()["data"]["agent_id"] == agent_id
        assert resp.json()["data"]["name"] == "GetBot"

    def test_get_404_for_unknown_id(self):
        resp = client.get("/api/agents/agent-nonexistent")
        assert resp.status_code == 404

    def test_filter_by_workspace_id(self):
        client.post("/api/agents", json={"name": "Ws1Bot", "soul": "s", "workspace_id": "ws-1"})
        client.post("/api/agents", json={"name": "Ws2Bot", "soul": "s", "workspace_id": "ws-2"})
        resp = client.get("/api/agents", params={"workspace_id": "ws-1"})
        data = resp.json()["data"]
        assert len(data) == 1
        assert data[0]["name"] == "Ws1Bot"


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDelete:
    def test_delete_removes_agent(self):
        create_resp = client.post("/api/agents", json={"name": "DelBot", "soul": "gone"})
        agent_id = create_resp.json()["data"]["agent_id"]

        del_resp = client.delete(f"/api/agents/{agent_id}")
        assert del_resp.status_code == 200
        assert del_resp.json()["data"]["deleted"] is True

        # agent should be gone
        get_resp = client.get(f"/api/agents/{agent_id}")
        assert get_resp.status_code == 404

    def test_delete_404_for_unknown(self):
        resp = client.delete("/api/agents/agent-phantom")
        assert resp.status_code == 404
