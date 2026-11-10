"""Story 12.4 — Workspace API endpoint tests.

Covers:
  AC1: GET /api/workspaces empty → auto-creates default workspace "我的工作区"
  AC2: POST /api/workspaces creates successfully (201)
  AC3: GET /api/workspaces/{id} returns workspace details
  AC4: PATCH /api/workspaces/{id} updates name
  AC5: DELETE /api/workspaces/{id} → 404 on next GET
  AC6: GET /api/workspaces agent_count / team_count fields present (may be 0)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from xueban.server import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    import xueban.api.workspaces as ws_mod

    monkeypatch.setattr(ws_mod, "_WORKSPACES_DIR", tmp_path / "workspaces")
    monkeypatch.setattr(ws_mod, "_AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(ws_mod, "_TEAMS_DIR", tmp_path / "teams")
    return TestClient(app)


class TestDefaultWorkspace:
    def test_empty_list_returns_verbatim_empty(self, client):
        """AC1 (post-3bc1755): no auto-created default — empty stays empty."""
        r = client.get("/api/workspaces")
        assert r.status_code == 200
        body = r.json()
        assert body["meta"]["total"] == 0
        assert body["data"] == []

    def test_default_only_created_once(self, client):
        """Auto-create is idempotent — calling twice returns same workspace."""
        r1 = client.get("/api/workspaces")
        r2 = client.get("/api/workspaces")
        assert r1.status_code == 200
        assert r2.status_code == 200
        ids1 = [w["workspace_id"] for w in r1.json()["data"]]
        ids2 = [w["workspace_id"] for w in r2.json()["data"]]
        assert ids1 == ids2


class TestCreateWorkspace:
    def test_create_returns_201(self, client):
        """AC2: create workspace."""
        r = client.post(
            "/api/workspaces",
            json={"name": "Project Alpha", "color": "#22c55e"},
        )
        assert r.status_code == 201
        body = r.json()
        assert body["data"]["name"] == "Project Alpha"
        assert body["data"]["color"] == "#22c55e"
        assert body["data"]["workspace_id"].startswith("ws-")
        assert body["meta"]["created"] is True

    def test_create_uses_default_color(self, client):
        r = client.post("/api/workspaces", json={"name": "No Color"})
        assert r.status_code == 201
        assert r.json()["data"]["color"] == "#6366f1"

    def test_create_requires_name(self, client):
        r = client.post("/api/workspaces", json={"name": "", "color": "#fff"})
        assert r.status_code == 422

    def test_create_name_missing(self, client):
        r = client.post("/api/workspaces", json={"color": "#fff"})
        assert r.status_code == 422


class TestGetWorkspace:
    def test_get_single(self, client):
        """AC3: GET /api/workspaces/{id} returns workspace."""
        create_r = client.post(
            "/api/workspaces",
            json={"name": "My WS", "color": "#3b82f6"},
        )
        ws_id = create_r.json()["data"]["workspace_id"]

        r = client.get(f"/api/workspaces/{ws_id}")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["workspace_id"] == ws_id
        assert data["name"] == "My WS"

    def test_get_not_found(self, client):
        r = client.get("/api/workspaces/ws-doesnotexist")
        assert r.status_code == 404
        assert r.json()["detail"]["error"]["code"] == "WORKSPACE_NOT_FOUND"


class TestPatchWorkspace:
    def test_patch_name(self, client):
        """AC4: PATCH updates workspace name."""
        create_r = client.post(
            "/api/workspaces",
            json={"name": "Old Name", "color": "#6366f1"},
        )
        ws_id = create_r.json()["data"]["workspace_id"]

        r = client.patch(f"/api/workspaces/{ws_id}", json={"name": "New Name"})
        assert r.status_code == 200
        assert r.json()["data"]["name"] == "New Name"

    def test_patch_color(self, client):
        create_r = client.post("/api/workspaces", json={"name": "WS"})
        ws_id = create_r.json()["data"]["workspace_id"]

        r = client.patch(f"/api/workspaces/{ws_id}", json={"color": "#ef4444"})
        assert r.status_code == 200
        assert r.json()["data"]["color"] == "#ef4444"

    def test_patch_not_found(self, client):
        r = client.patch("/api/workspaces/ws-ghost", json={"name": "X"})
        assert r.status_code == 404


class TestDeleteWorkspace:
    def test_delete_then_404(self, client):
        """AC5: DELETE then GET returns 404."""
        create_r = client.post("/api/workspaces", json={"name": "Temp"})
        ws_id = create_r.json()["data"]["workspace_id"]

        del_r = client.delete(f"/api/workspaces/{ws_id}")
        assert del_r.status_code == 204

        get_r = client.get(f"/api/workspaces/{ws_id}")
        assert get_r.status_code == 404

    def test_delete_not_found(self, client):
        r = client.delete("/api/workspaces/ws-nope")
        assert r.status_code == 404


class TestCountFields:
    def test_agent_count_and_team_count_present(self, client):
        """AC6: list response includes agent_count and team_count fields (can be 0)."""
        create_r = client.post("/api/workspaces", json={"name": "Count Fields"})
        assert create_r.status_code == 201
        r = client.get("/api/workspaces")
        assert r.status_code == 200
        ws = r.json()["data"][0]
        assert "agent_count" in ws
        assert "team_count" in ws
        assert isinstance(ws["agent_count"], int)
        assert isinstance(ws["team_count"], int)

    def test_single_get_has_count_fields(self, client):
        create_r = client.post("/api/workspaces", json={"name": "Count Test"})
        ws_id = create_r.json()["data"]["workspace_id"]

        r = client.get(f"/api/workspaces/{ws_id}")
        assert r.status_code == 200
        data = r.json()["data"]
        assert "agent_count" in data
        assert "team_count" in data
