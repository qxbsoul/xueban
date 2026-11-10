"""D3 — workspace_id normalization + list filtering.

Conservative tightening of the cross-workspace leak: a record with NO (or
empty) workspace_id is normalized to the "unassigned" sentinel instead of
acting as a wildcard that matches every workspace filter.

  * `normalize_workspace_id` backfills missing/blank → "unassigned".
  * `list_teams(?workspace_id=X)` no longer returns legacy (no-workspace)
    records for an arbitrary X; they surface only under ?workspace_id=unassigned
    (and in the unfiltered list).
  * Records with an explicit workspace_id are unaffected.

All storage monkeypatched to tmp_path.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from xueban.server import app
from xueban.api import team_validation as tv  # noqa: F401  (ensures import OK)


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

    def write_team(team_id: str, **fields):
        rec = {"team_id": team_id, "name": team_id, "agent_ids": [], **fields}
        (teams_dir / f"{team_id}.json").write_text(json.dumps(rec), encoding="utf-8")

    def write_agent(agent_id: str, **fields):
        rec = {"agent_id": agent_id, "name": agent_id, **fields}
        (agents_dir / f"{agent_id}.json").write_text(json.dumps(rec), encoding="utf-8")

    return TestClient(app), write_team, write_agent


# ---------------------------------------------------------------------------
# normalize_workspace_id helper
# ---------------------------------------------------------------------------


class TestNormalizeHelper:
    def test_missing_becomes_unassigned(self):
        from xueban.api.teams import normalize_workspace_id, UNASSIGNED_WORKSPACE
        assert normalize_workspace_id(None) == UNASSIGNED_WORKSPACE
        assert normalize_workspace_id("") == UNASSIGNED_WORKSPACE
        assert normalize_workspace_id("   ") == UNASSIGNED_WORKSPACE

    def test_explicit_preserved(self):
        from xueban.api.teams import normalize_workspace_id
        assert normalize_workspace_id("ws-abc") == "ws-abc"
        assert normalize_workspace_id("default") == "default"


# ---------------------------------------------------------------------------
# list_teams no longer leaks legacy records across workspaces
# ---------------------------------------------------------------------------


class TestListTeamsFiltering:
    def test_legacy_record_not_leaked_to_arbitrary_workspace(self, env):
        client, write_team, _ = env
        write_team("team-legacy")  # no workspace_id
        write_team("team-ws1", workspace_id="ws-1")

        r = client.get("/api/teams?workspace_id=ws-1")
        ids = [t["team_id"] for t in r.json()["data"]]
        assert "team-ws1" in ids
        assert "team-legacy" not in ids  # previously leaked here

    def test_legacy_record_surfaces_under_unassigned(self, env):
        client, write_team, _ = env
        write_team("team-legacy")  # no workspace_id
        r = client.get("/api/teams?workspace_id=unassigned")
        ids = [t["team_id"] for t in r.json()["data"]]
        assert "team-legacy" in ids

    def test_explicit_record_matches_its_workspace(self, env):
        client, write_team, _ = env
        write_team("team-a", workspace_id="ws-1")
        write_team("team-b", workspace_id="ws-2")
        r = client.get("/api/teams?workspace_id=ws-1")
        ids = [t["team_id"] for t in r.json()["data"]]
        assert ids == ["team-a"]

    def test_unfiltered_returns_all(self, env):
        client, write_team, _ = env
        write_team("team-legacy")
        write_team("team-a", workspace_id="ws-1")
        r = client.get("/api/teams")
        ids = sorted(t["team_id"] for t in r.json()["data"])
        assert ids == ["team-a", "team-legacy"]


class TestListAgentsFiltering:
    def test_legacy_agent_surfaces_under_unassigned(self, env):
        client, _, write_agent = env
        write_agent("agent-legacy")  # no workspace_id
        write_agent("agent-ws1", workspace_id="ws-1")

        r = client.get("/api/agents?workspace_id=unassigned")
        ids = [a["agent_id"] for a in r.json()["data"]]
        assert "agent-legacy" in ids
        assert "agent-ws1" not in ids

    def test_legacy_agent_not_in_arbitrary_workspace(self, env):
        client, _, write_agent = env
        write_agent("agent-legacy")
        r = client.get("/api/agents?workspace_id=ws-1")
        ids = [a["agent_id"] for a in r.json()["data"]]
        assert "agent-legacy" not in ids
