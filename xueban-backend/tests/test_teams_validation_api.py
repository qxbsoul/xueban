"""D1+D2 — Team write-time validation, endpoint-level (TestClient).

Verifies the teams API rejects (4xx, not 500):
  D1: workflow PUT with a sequential cycle / dangling edge endpoint / self-loop
  D2: team create / patch / workflow referencing agents that don't exist

All storage is monkeypatched to tmp_path — never touches real .xueban.
Backward-compat: a pre-existing team record with a cyclic workflow still loads
via GET (validation only fires on write).
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from xueban.server import app


@pytest.fixture
def env(tmp_path, monkeypatch):
    import xueban.api.teams as teams_mod
    import xueban.api.agents as agents_mod

    teams_dir = tmp_path / "teams"
    agents_dir = tmp_path / "agents"
    teams_dir.mkdir(parents=True, exist_ok=True)
    agents_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(teams_mod, "_TEAMS_DIR", teams_dir)
    monkeypatch.setattr(agents_mod, "_AGENTS_DIR", agents_dir)

    def seed_agent(aid: str) -> None:
        (agents_dir / f"{aid}.json").write_text(
            json.dumps({"agent_id": aid, "name": aid}), encoding="utf-8"
        )

    client = TestClient(app)
    return client, teams_dir, agents_dir, seed_agent


def _node(node_id: str, agent_id: str = "") -> dict:
    return {"id": node_id, "data": {"agentId": agent_id}}


def _edge(src: str, dst: str, mode: str = "direct") -> dict:
    return {"source": src, "target": dst, "data": {"mode": mode}}


# ---------------------------------------------------------------------------
# D2 — agent existence on create / patch
# ---------------------------------------------------------------------------


class TestCreateAgentExistence:
    def test_create_with_missing_agent_rejected(self, env):
        client, _, _, _ = env
        r = client.post("/api/teams", json={"name": "T", "agent_ids": ["ghost"]})
        assert r.status_code == 422
        assert r.json()["detail"]["error"]["code"] == "AGENT_NOT_FOUND"
        assert "ghost" in r.json()["detail"]["error"]["message"]

    def test_create_with_existing_agent_ok(self, env):
        client, _, _, seed = env
        seed("agent-real")
        r = client.post("/api/teams", json={"name": "T", "agent_ids": ["agent-real"]})
        assert r.status_code == 201

    def test_create_partial_missing_reports_only_missing(self, env):
        client, _, _, seed = env
        seed("agent-ok")
        r = client.post(
            "/api/teams", json={"name": "T", "agent_ids": ["agent-ok", "agent-bad"]}
        )
        assert r.status_code == 422
        msg = r.json()["detail"]["error"]["message"]
        assert "agent-bad" in msg
        assert "agent-ok" not in msg


class TestPatchAgentExistence:
    def test_patch_add_missing_agent_rejected(self, env):
        client, _, _, seed = env
        seed("agent-a")
        tid = client.post(
            "/api/teams", json={"name": "T", "agent_ids": ["agent-a"]}
        ).json()["data"]["team_id"]
        r = client.patch(f"/api/teams/{tid}", json={"add_agent_ids": ["ghost"]})
        assert r.status_code == 422
        assert r.json()["detail"]["error"]["code"] == "AGENT_NOT_FOUND"

    def test_patch_add_existing_ok(self, env):
        client, _, _, seed = env
        seed("agent-a")
        seed("agent-b")
        tid = client.post(
            "/api/teams", json={"name": "T", "agent_ids": ["agent-a"]}
        ).json()["data"]["team_id"]
        r = client.patch(f"/api/teams/{tid}", json={"add_agent_ids": ["agent-b"]})
        assert r.status_code == 200

    def test_patch_remove_does_not_require_existence(self, env):
        """Removing members never validates existence (lets you clean up
        teams that reference already-deleted agents)."""
        client, teams_dir, _, seed = env
        seed("agent-a")
        tid = client.post(
            "/api/teams", json={"name": "T", "agent_ids": ["agent-a"]}
        ).json()["data"]["team_id"]
        r = client.patch(f"/api/teams/{tid}", json={"remove_agent_ids": ["agent-a"]})
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# D1 — workflow DAG structure on PUT /workflow
# ---------------------------------------------------------------------------


class TestWorkflowValidation:
    def _make_team(self, env, agent_ids):
        client, _, _, seed = env
        for aid in agent_ids:
            if aid:
                seed(aid)
        return client.post(
            "/api/teams", json={"name": "T", "agent_ids": [a for a in agent_ids if a]}
        ).json()["data"]["team_id"]

    def test_valid_workflow_accepted(self, env):
        client, _, _, _ = env
        tid = self._make_team(env, ["agent-1", "agent-2"])
        wf = {
            "nodes": [_node("n1", "agent-1"), _node("n2", "agent-2")],
            "edges": [_edge("n1", "n2")],
        }
        r = client.put(f"/api/teams/{tid}/workflow", json=wf)
        assert r.status_code == 200

    def test_cycle_rejected(self, env):
        client, _, _, _ = env
        tid = self._make_team(env, ["agent-1", "agent-2"])
        wf = {
            "nodes": [_node("n1", "agent-1"), _node("n2", "agent-2")],
            "edges": [_edge("n1", "n2"), _edge("n2", "n1")],
        }
        r = client.put(f"/api/teams/{tid}/workflow", json=wf)
        assert r.status_code == 422
        assert r.json()["detail"]["error"]["code"] == "INVALID_DAG"

    def test_dangling_edge_rejected(self, env):
        client, _, _, _ = env
        tid = self._make_team(env, ["agent-1"])
        wf = {
            "nodes": [_node("n1", "agent-1")],
            "edges": [_edge("n1", "missing-node")],
        }
        r = client.put(f"/api/teams/{tid}/workflow", json=wf)
        assert r.status_code == 422
        assert r.json()["detail"]["error"]["code"] == "INVALID_DAG"

    def test_self_loop_rejected(self, env):
        client, _, _, _ = env
        tid = self._make_team(env, ["agent-1"])
        wf = {"nodes": [_node("n1", "agent-1")], "edges": [_edge("n1", "n1")]}
        r = client.put(f"/api/teams/{tid}/workflow", json=wf)
        assert r.status_code == 422

    def test_conditional_back_edge_accepted(self, env):
        client, _, _, _ = env
        tid = self._make_team(env, ["agent-1", "agent-2"])
        wf = {
            "nodes": [_node("n1", "agent-1"), _node("n2", "agent-2")],
            "edges": [
                _edge("n1", "n2", "direct"),
                _edge("n2", "n1", "conditional"),
            ],
        }
        r = client.put(f"/api/teams/{tid}/workflow", json=wf)
        assert r.status_code == 200

    def test_workflow_node_missing_agent_rejected(self, env):
        client, _, _, seed = env
        seed("agent-1")
        tid = client.post(
            "/api/teams", json={"name": "T", "agent_ids": ["agent-1"]}
        ).json()["data"]["team_id"]
        wf = {
            "nodes": [_node("n1", "agent-1"), _node("n2", "agent-ghost")],
            "edges": [_edge("n1", "n2")],
        }
        r = client.put(f"/api/teams/{tid}/workflow", json=wf)
        assert r.status_code == 422
        assert r.json()["detail"]["error"]["code"] == "AGENT_NOT_FOUND"

    def test_coordinator_empty_agentid_allowed(self, env):
        """A coordinator node with empty agentId must NOT be rejected by D2."""
        client, _, _, _ = env
        tid = self._make_team(env, ["agent-1"])
        wf = {
            "nodes": [_node("coordinator", ""), _node("n1", "agent-1")],
            "edges": [_edge("coordinator", "n1")],
        }
        r = client.put(f"/api/teams/{tid}/workflow", json=wf)
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Backward compat — legacy cyclic team still readable via GET
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    def test_legacy_cyclic_workflow_still_loads(self, env):
        """A team file written directly to disk with a cyclic workflow (the
        kind that exists in production today) must still GET fine — validation
        is write-time only."""
        client, teams_dir, _, _ = env
        legacy = {
            "team_id": "team-legacy01",
            "name": "Legacy",
            "agent_ids": ["agent-x"],
            "workflow": {
                "nodes": [_node("a", "agent-x"), _node("b", "")],
                "edges": [_edge("a", "b"), _edge("b", "a")],  # cycle!
            },
        }
        (teams_dir / "team-legacy01.json").write_text(
            json.dumps(legacy), encoding="utf-8"
        )
        r = client.get("/api/teams/team-legacy01")
        assert r.status_code == 200
        rw = client.get("/api/teams/team-legacy01/workflow")
        assert rw.status_code == 200
        assert len(rw.json()["data"]["edges"]) == 2
