"""Story 12-3 — Team workflow + policy endpoint tests.

Covers:
  - GET  /api/teams/{id}/workflow  → empty {nodes:[], edges:[]}
  - PUT  /api/teams/{id}/workflow  → persists; GET returns saved data
  - GET  /api/teams/{id}/policy   → empty {}
  - PUT  /api/teams/{id}/policy   → persists; GET returns saved data
  - 404 for non-existent team_id on all four endpoints
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from xueban.server import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    import json

    import xueban.api.agents as agents_mod
    import xueban.api.teams as teams_mod

    monkeypatch.setattr(teams_mod, "_TEAMS_DIR", tmp_path / "teams")
    # D2 write-time validation rejects teams referencing non-existent agents —
    # seed the ids these tests use into an isolated agent store.
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(agents_mod, "_AGENTS_DIR", agents_dir)
    for aid in ("agent-a", "agent-b"):
        (agents_dir / f"{aid}.json").write_text(
            json.dumps({"agent_id": aid, "name": aid}), encoding="utf-8"
        )
    return TestClient(app)


@pytest.fixture
def team_id(client):
    r = client.post("/api/teams", json={
        "name": "Workflow Test Team",
        "agent_ids": ["agent-a", "agent-b"],
    })
    assert r.status_code == 201
    return r.json()["data"]["team_id"]


# ---------------------------------------------------------------------------
# Workflow tests
# ---------------------------------------------------------------------------

class TestTeamWorkflow:
    def test_get_workflow_empty(self, client, team_id):
        r = client.get(f"/api/teams/{team_id}/workflow")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["nodes"] == []
        assert data["edges"] == []

    def test_put_workflow_persists(self, client, team_id):
        workflow = {
            "nodes": [
                {
                    "id": "node-1",
                    "type": "agentTask",
                    "position": {"x": 100, "y": 200},
                    "data": {"agentId": "agent-a", "name": "Alice", "soul": "Helpful"},
                }
            ],
            "edges": [
                {
                    "id": "edge-1",
                    "source": "node-1",
                    "target": "node-2",
                    "data": {"mode": "direct"},
                    "label": "直接传递",
                }
            ],
        }
        workflow["nodes"].append({
            "id": "node-2",
            "type": "agentTask",
            "position": {"x": 300, "y": 200},
            "data": {"agentId": "agent-b", "name": "Bob", "soul": "Creative"},
        })
        r = client.put(f"/api/teams/{team_id}/workflow", json=workflow)
        assert r.status_code == 200
        assert r.json()["data"]["nodes"][0]["id"] == "node-1"

    def test_get_workflow_after_put(self, client, team_id):
        workflow = {
            "nodes": [
                {
                    "id": "node-99",
                    "type": "agentTask",
                    "position": {"x": 50, "y": 50},
                    "data": {"agentId": "agent-b", "name": "Bob", "soul": "Creative"},
                }
            ],
            "edges": [],
        }
        client.put(f"/api/teams/{team_id}/workflow", json=workflow)

        r = client.get(f"/api/teams/{team_id}/workflow")
        assert r.status_code == 200
        data = r.json()["data"]
        assert len(data["nodes"]) == 1
        assert data["nodes"][0]["id"] == "node-99"
        assert data["edges"] == []

    def test_put_workflow_overwrites(self, client, team_id):
        wf1 = {"nodes": [{"id": "n1", "type": "agentTask", "position": {"x": 0, "y": 0}, "data": {"agentId": "agent-a", "name": "A", "soul": "x"}}], "edges": []}
        wf2 = {"nodes": [{"id": "n2", "type": "agentTask", "position": {"x": 10, "y": 10}, "data": {"agentId": "agent-b", "name": "B", "soul": "y"}}], "edges": []}
        client.put(f"/api/teams/{team_id}/workflow", json=wf1)
        client.put(f"/api/teams/{team_id}/workflow", json=wf2)

        r = client.get(f"/api/teams/{team_id}/workflow")
        data = r.json()["data"]
        assert len(data["nodes"]) == 1
        assert data["nodes"][0]["id"] == "n2"

    def test_get_workflow_not_found(self, client):
        r = client.get("/api/teams/team-nonexistent/workflow")
        assert r.status_code == 404
        assert r.json()["detail"]["error"]["code"] == "TEAM_NOT_FOUND"

    def test_put_workflow_not_found(self, client):
        r = client.put("/api/teams/team-ghost/workflow", json={"nodes": [], "edges": []})
        assert r.status_code == 404
        assert r.json()["detail"]["error"]["code"] == "TEAM_NOT_FOUND"


# ---------------------------------------------------------------------------
# Policy tests
# ---------------------------------------------------------------------------

class TestTeamPolicy:
    def test_get_policy_empty(self, client, team_id):
        r = client.get(f"/api/teams/{team_id}/policy")
        assert r.status_code == 200
        assert r.json()["data"] == {}

    def test_put_policy_persists(self, client, team_id):
        matrix = {
            "Alice": {"Alice": "permit", "Bob": "deny"},
            "Bob": {"Alice": "warn", "Bob": "permit"},
        }
        r = client.put(f"/api/teams/{team_id}/policy", json={"matrix": matrix})
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["Alice"]["Bob"] == "deny"
        assert data["Bob"]["Alice"] == "warn"

    def test_get_policy_after_put(self, client, team_id):
        matrix = {
            "X": {"X": "permit", "Y": "deny"},
            "Y": {"X": "warn", "Y": "permit"},
        }
        client.put(f"/api/teams/{team_id}/policy", json={"matrix": matrix})

        r = client.get(f"/api/teams/{team_id}/policy")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["X"]["Y"] == "deny"
        assert data["Y"]["X"] == "warn"

    def test_put_policy_overwrites(self, client, team_id):
        m1 = {"A": {"A": "permit", "B": "deny"}, "B": {"A": "deny", "B": "permit"}}
        m2 = {"A": {"A": "warn"}}
        client.put(f"/api/teams/{team_id}/policy", json={"matrix": m1})
        client.put(f"/api/teams/{team_id}/policy", json={"matrix": m2})

        r = client.get(f"/api/teams/{team_id}/policy")
        data = r.json()["data"]
        assert data == {"A": {"A": "warn"}}

    def test_put_policy_empty_matrix(self, client, team_id):
        r = client.put(f"/api/teams/{team_id}/policy", json={"matrix": {}})
        assert r.status_code == 200
        assert r.json()["data"] == {}

    def test_get_policy_not_found(self, client):
        r = client.get("/api/teams/team-nonexistent/policy")
        assert r.status_code == 404
        assert r.json()["detail"]["error"]["code"] == "TEAM_NOT_FOUND"

    def test_put_policy_not_found(self, client):
        r = client.put("/api/teams/team-ghost/policy", json={"matrix": {}})
        assert r.status_code == 404
        assert r.json()["detail"]["error"]["code"] == "TEAM_NOT_FOUND"
