"""Team workflow 版本仓测试 — 版本快照 / 回滚 / 分支（team_workflow_versions）。

覆盖：
  - PUT /workflow 成功保存自动记版本（v1、v2 递增）
  - GET /workflow/versions 只回元数据（不带 workflow 载荷）
  - POST /workflow/restore 回滚到旧版本，且回滚本身再记一个新版本
  - restore 未知版本 → 404 VERSION_NOT_FOUND
  - POST /workflow/branch 复制为新 team 变体（默认名「· 分支」后缀），
    分支版本仓记下初始版本并注明来源
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from xueban.server import app


def _workflow(agent_id: str, edge_mode: str = "direct") -> dict:
    return {
        "nodes": [
            {
                "id": f"n-{agent_id}",
                "type": "agentTask",
                "position": {"x": 0, "y": 0},
                "data": {"agentId": agent_id, "name": agent_id, "soul": ""},
            }
        ],
        "edges": [],
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    import xueban.api.teams as teams_mod
    import xueban.api.agents as agents_mod
    from xueban.runtime import team_workflow_versions as versions_mod

    monkeypatch.setattr(teams_mod, "_TEAMS_DIR", tmp_path / "teams")
    monkeypatch.setattr(versions_mod, "_VERSIONS_DIR", tmp_path / "team_workflow_versions")
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(agents_mod, "_AGENTS_DIR", agents_dir)
    for aid in ("agent-1", "agent-2"):
        (agents_dir / f"{aid}.json").write_text(
            json.dumps({"agent_id": aid, "name": aid}), encoding="utf-8"
        )
    return TestClient(app)


@pytest.fixture
def team_id(client) -> str:
    r = client.post("/api/teams", json={"name": "版本小队", "agent_ids": ["agent-1", "agent-2"]})
    assert r.status_code == 201
    return r.json()["data"]["team_id"]


class TestAutoSnapshot:
    def test_put_workflow_records_versions_incrementally(self, client, team_id):
        assert client.put(f"/api/teams/{team_id}/workflow", json=_workflow("agent-1")).status_code == 200
        assert client.put(f"/api/teams/{team_id}/workflow", json=_workflow("agent-2")).status_code == 200

        r = client.get(f"/api/teams/{team_id}/workflow/versions")
        assert r.status_code == 200
        versions = r.json()["data"]
        assert [v["version"] for v in versions] == [1, 2]
        assert all(v["saved_at"] for v in versions)
        # 元数据列表不带 workflow 载荷
        assert all("workflow" not in v for v in versions)
        assert r.json()["meta"]["total"] == 2

    def test_versions_empty_for_team_without_saves(self, client, team_id):
        r = client.get(f"/api/teams/{team_id}/workflow/versions")
        assert r.status_code == 200
        assert r.json()["data"] == []

    def test_versions_404_for_unknown_team(self, client):
        r = client.get("/api/teams/team-nope/workflow/versions")
        assert r.status_code == 404


class TestRestore:
    def test_restore_rewrites_workflow_and_appends_version(self, client, team_id):
        client.put(f"/api/teams/{team_id}/workflow", json=_workflow("agent-1"))
        client.put(f"/api/teams/{team_id}/workflow", json=_workflow("agent-2"))

        r = client.post(f"/api/teams/{team_id}/workflow/restore", json={"version": 1})
        assert r.status_code == 200
        restored = r.json()["data"]
        assert restored["nodes"][0]["data"]["agentId"] == "agent-1"

        # 回滚写回 team 本体
        current = client.get(f"/api/teams/{team_id}/workflow").json()["data"]
        assert current["nodes"][0]["data"]["agentId"] == "agent-1"

        # 回滚本身再记一版（append-only 历史）
        versions = client.get(f"/api/teams/{team_id}/workflow/versions").json()["data"]
        assert [v["version"] for v in versions] == [1, 2, 3]
        assert versions[-1]["summary"] == "restored from v1"

    def test_restore_unknown_version_404(self, client, team_id):
        client.put(f"/api/teams/{team_id}/workflow", json=_workflow("agent-1"))
        r = client.post(f"/api/teams/{team_id}/workflow/restore", json={"version": 99})
        assert r.status_code == 404
        assert r.json()["detail"]["error"]["code"] == "VERSION_NOT_FOUND"

    def test_restore_unknown_team_404(self, client):
        r = client.post("/api/teams/team-nope/workflow/restore", json={"version": 1})
        assert r.status_code == 404


class TestBranch:
    def test_branch_copies_workflow_with_default_name(self, client, team_id):
        client.put(f"/api/teams/{team_id}/workflow", json=_workflow("agent-1"))

        r = client.post(f"/api/teams/{team_id}/workflow/branch", json={})
        assert r.status_code == 201
        branch = r.json()["data"]
        assert branch["team_id"] != team_id
        assert branch["name"] == "版本小队 · 分支"
        assert branch["agent_ids"] == ["agent-1", "agent-2"]
        assert branch["workflow"]["nodes"][0]["data"]["agentId"] == "agent-1"
        assert r.json()["meta"]["branched_from"] == team_id

        # 分支版本仓记下初始版本并注明来源
        b_versions = client.get(f"/api/teams/{branch['team_id']}/workflow/versions").json()["data"]
        assert len(b_versions) == 1
        assert b_versions[0]["summary"] == f"branched from {team_id} v1"

        # 原 team 版本仓不受影响
        versions = client.get(f"/api/teams/{team_id}/workflow/versions").json()["data"]
        assert [v["version"] for v in versions] == [1]

    def test_branch_with_custom_name(self, client, team_id):
        r = client.post(f"/api/teams/{team_id}/workflow/branch", json={"name": "实验变体"})
        assert r.status_code == 201
        assert r.json()["data"]["name"] == "实验变体"

    def test_branch_unknown_team_404(self, client):
        r = client.post("/api/teams/team-nope/workflow/branch", json={})
        assert r.status_code == 404
