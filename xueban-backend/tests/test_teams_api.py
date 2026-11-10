"""Story 12.2 — Teams API endpoint tests.

Covers:
  AC1: empty list 200
  AC2/AC3: create team with members
  AC4: list returns created team
  AC5: get single team; patch add/remove; delete
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from xueban.server import app


# Agent ids these tests reference when creating/patching teams. D2 write-time
# validation now rejects teams that reference non-existent agents, so the
# fixture seeds these ids into the (isolated, monkeypatched) agent store.
_SEEDED_AGENT_IDS = [
    "agent-1", "agent-2", "agent-3",
    "a", "a1", "a2", "a3",
    "x", "y",
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    import xueban.api.teams as teams_mod
    import xueban.api.agents as agents_mod

    monkeypatch.setattr(teams_mod, "_TEAMS_DIR", tmp_path / "teams")
    # Isolate + seed the agent store so D2 existence checks pass for the ids
    # these tests use. Never touches the real .xueban/agents.
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(agents_mod, "_AGENTS_DIR", agents_dir)
    for aid in _SEEDED_AGENT_IDS:
        (agents_dir / f"{aid}.json").write_text(
            json.dumps({"agent_id": aid, "name": aid}), encoding="utf-8"
        )
    return TestClient(app)


class TestCreateTeam:
    def test_create_returns_201(self, client):
        r = client.post("/api/teams", json={
            "name": "Research Lab",
            "description": "Paper writers",
            "agent_ids": ["agent-1", "agent-2"],
        })
        assert r.status_code == 201
        body = r.json()
        assert body["data"]["name"] == "Research Lab"
        assert body["data"]["team_id"].startswith("team-")
        assert body["data"]["agent_ids"] == ["agent-1", "agent-2"]
        assert body["meta"]["created"] is True

    def test_create_deduplicates_agent_ids(self, client):
        r = client.post("/api/teams", json={
            "name": "Dedupe",
            "agent_ids": ["a1", "a1", "a2"],
        })
        assert r.status_code == 201
        assert r.json()["data"]["agent_ids"] == ["a1", "a2"]

    def test_create_requires_name(self, client):
        r = client.post("/api/teams", json={
            "name": "",
            "agent_ids": ["a1"],
        })
        assert r.status_code == 422

    def test_create_requires_at_least_one_agent(self, client):
        r = client.post("/api/teams", json={
            "name": "Empty",
            "agent_ids": [],
        })
        assert r.status_code == 422

    def test_create_default_workspace(self, client):
        r = client.post("/api/teams", json={"name": "T", "agent_ids": ["a"]})
        assert r.status_code == 201
        assert r.json()["data"]["workspace_id"] == "default"

    def test_create_custom_workspace(self, client):
        r = client.post("/api/teams", json={
            "name": "T",
            "agent_ids": ["a"],
            "workspace_id": "ws-abc",
        })
        assert r.status_code == 201
        assert r.json()["data"]["workspace_id"] == "ws-abc"


class TestListTeams:
    def test_empty_list(self, client):
        r = client.get("/api/teams")
        assert r.status_code == 200
        body = r.json()
        assert body["data"] == []
        assert body["meta"]["total"] == 0

    def test_list_returns_created(self, client):
        client.post("/api/teams", json={"name": "A", "agent_ids": ["x"]})
        client.post("/api/teams", json={"name": "B", "agent_ids": ["y"]})
        r = client.get("/api/teams")
        assert r.status_code == 200
        assert r.json()["meta"]["total"] == 2

    def test_filter_by_workspace(self, client):
        client.post("/api/teams", json={"name": "A", "agent_ids": ["x"], "workspace_id": "ws1"})
        client.post("/api/teams", json={"name": "B", "agent_ids": ["y"], "workspace_id": "ws2"})
        r = client.get("/api/teams?workspace_id=ws1")
        assert r.status_code == 200
        data = r.json()["data"]
        assert len(data) == 1
        assert data[0]["name"] == "A"


class TestGetTeam:
    def test_get_existing(self, client):
        created = client.post("/api/teams", json={"name": "T", "agent_ids": ["a"]}).json()
        team_id = created["data"]["team_id"]
        r = client.get(f"/api/teams/{team_id}")
        assert r.status_code == 200
        assert r.json()["data"]["team_id"] == team_id

    def test_get_not_found(self, client):
        r = client.get("/api/teams/team-nonexistent")
        assert r.status_code == 404
        assert r.json()["detail"]["error"]["code"] == "TEAM_NOT_FOUND"

    def test_get_invalid_id(self, client):
        r = client.get("/api/teams/../../etc")
        assert r.status_code in (400, 404)


class TestPatchTeam:
    def _create(self, client, name="T", agents=None):
        r = client.post("/api/teams", json={
            "name": name,
            "agent_ids": agents or ["a1"],
        })
        return r.json()["data"]["team_id"]

    def test_rename(self, client):
        tid = self._create(client)
        r = client.patch(f"/api/teams/{tid}", json={"name": "Renamed"})
        assert r.status_code == 200
        assert r.json()["data"]["name"] == "Renamed"

    def test_add_members(self, client):
        tid = self._create(client, agents=["a1"])
        r = client.patch(f"/api/teams/{tid}", json={"add_agent_ids": ["a2", "a3"]})
        assert r.status_code == 200
        assert set(r.json()["data"]["agent_ids"]) == {"a1", "a2", "a3"}

    def test_add_duplicate_member_no_effect(self, client):
        tid = self._create(client, agents=["a1"])
        r = client.patch(f"/api/teams/{tid}", json={"add_agent_ids": ["a1"]})
        assert r.status_code == 200
        assert r.json()["data"]["agent_ids"] == ["a1"]

    def test_remove_members(self, client):
        tid = self._create(client, agents=["a1", "a2", "a3"])
        r = client.patch(f"/api/teams/{tid}", json={"remove_agent_ids": ["a2"]})
        assert r.status_code == 200
        assert r.json()["data"]["agent_ids"] == ["a1", "a3"]

    def test_patch_not_found(self, client):
        r = client.patch("/api/teams/team-doesnotexist", json={"name": "X"})
        assert r.status_code == 404


class TestDeleteTeam:
    def test_delete_returns_204(self, client):
        r0 = client.post("/api/teams", json={"name": "T", "agent_ids": ["a"]})
        tid = r0.json()["data"]["team_id"]
        r = client.delete(f"/api/teams/{tid}")
        assert r.status_code == 204

    def test_delete_then_get_404(self, client):
        r0 = client.post("/api/teams", json={"name": "T", "agent_ids": ["a"]})
        tid = r0.json()["data"]["team_id"]
        client.delete(f"/api/teams/{tid}")
        r = client.get(f"/api/teams/{tid}")
        assert r.status_code == 404

    def test_delete_not_found(self, client):
        r = client.delete("/api/teams/team-ghost")
        assert r.status_code == 404
