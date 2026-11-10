"""Story 13.6 — POST /builder/teams/from-agent 测试。

覆盖：
  - happy: 返回完整 Blueprint，含 anchor=true 的 RoleProfile
  - 404: Catalog Agent 不存在
  - 422: 快照缺失（CatalogSnapshotMissing）
  - 422: snapshot.role_profiles 为空 → CATALOG_BLUEPRINT_INVALID
"""
from __future__ import annotations

from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from xueban.runtime.catalog_service import (
    CatalogAppDetail,
    CatalogAppNotFound,
    CatalogService,
    CatalogSnapshotMissing,
    set_service as set_catalog_service,
)
from xueban.server import app


def _sample_role() -> Dict[str, Any]:
    return {
        "role_id": "role-orig",
        "name": "Paper Reproducer",
        "description": "Reproduces papers",
        "persona": "Methodical scientist",
        "responsibilities": ["Reproduce papers"],
        "constraints": [],
        "tools": [],
        "executor_kind": "api",
        "executor_provider": "anthropic",
        "executor_model": "claude-sonnet-4-6",
        "capabilities": [],
        "handoff_rules": [],
        "persona_traits": {},
        "state_fields": [],
        "can_spawn_tasks": False,
        "sub_agents": [],
        "metadata": {"original": "yes"},
        "collaboration_contract": {
            "scope": "team_member_candidate",
            "accepts_from": ["literature_search"],
            "delivers_to": ["report_writer"],
            "collaboration_style": "push",
        },
    }


def _make_app(app_id: str, role_profiles: list[Dict[str, Any]]) -> CatalogAppDetail:
    snapshot: Dict[str, Any] = {
        "blueprint_id": f"bp-{app_id}",
        "version": "1.0",
        "name": "Paper Reproducer",
        "goal": "Reproduce ML papers",
        "audience": "Researchers",
        "mode": "single",
        "role_profiles": role_profiles,
        "tool_policies": [],
        "knowledge_bindings": [],
        "memory_profile": {"scope": "session", "writeback_target": None, "enabled": True, "metadata": {}},
        "eval_profile": {"smoke_eval_enabled": False, "eval_criteria": [], "regression_gate": False, "metadata": {}},
        "publish_profile": {"target": "none", "visibility": "private", "publish_ref": "", "metadata": {}},
        "metadata": {},
    }
    return CatalogAppDetail(
        app_id=app_id,
        name="Paper Reproducer",
        goal="Reproduce ML papers",
        kit_type="custom",
        author="tester",
        published_at="2026-04-28T00:00:00Z",
        fork_count=0,
        forked_from=None,
        template_id="",
        workflow_id="",
        blueprint_id=f"bp-{app_id}",
        mode="single",
        role_names=["Paper Reproducer"],
        role_count=1,
        description="Reproduce ML papers",
        blueprint_snapshot=snapshot,
    )


@pytest.fixture
def client():
    return TestClient(app)


class TestPromoteFromAgentHappy:
    def test_returns_team_blueprint_with_anchor_role(self, client: TestClient):
        catalog_id = "app-aabbccdd1234"
        detail = _make_app(catalog_id, [_sample_role()])

        mock_svc = CatalogService.__new__(CatalogService)
        mock_svc.get_app = lambda aid: detail  # type: ignore[method-assign]
        set_catalog_service(mock_svc)

        resp = client.post("/builder/teams/from-agent", json={"anchor_agent_id": catalog_id})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        bp = body["data"]

        # 整体 Blueprint
        assert bp["mode"] == "team"
        assert bp["blueprint_id"].startswith("team-from-")
        assert catalog_id[:8] in bp["blueprint_id"]
        assert "Paper Reproducer" in bp["name"]
        assert len(bp["role_profiles"]) == 1

        # anchor RoleProfile
        anchor = bp["role_profiles"][0]
        assert anchor["role_id"].startswith("anchor-")
        assert catalog_id[:8] in anchor["role_id"]
        assert anchor["metadata"]["anchor"] is True
        assert anchor["metadata"]["imported_from"] == catalog_id
        # 原 metadata 字段保留
        assert anchor["metadata"]["original"] == "yes"
        # collaboration_contract 透传
        assert anchor["collaboration_contract"]["delivers_to"] == ["report_writer"]

        # meta — review P12: trace_id must be present
        assert body["meta"]["anchor_agent_id"] == catalog_id
        assert isinstance(body["meta"].get("trace_id"), str) and body["meta"]["trace_id"].startswith("trace-")
        # P3: role_id no longer derived from int(time.time()) — should be uuid suffix
        assert anchor["role_id"] != f"anchor-{catalog_id[:8]}-"  # not an empty suffix
        # blueprint metadata.anchor_role_id matches anchor role
        assert bp["metadata"]["anchor_role_id"] == anchor["role_id"]


class TestPromoteFromAgentInputValidation:
    def test_422_when_anchor_id_invalid_format(self, client: TestClient):
        # Story 13.6 review P10 — anchor_agent_id format gate.
        resp = client.post(
            "/builder/teams/from-agent",
            json={"anchor_agent_id": "../../etc/passwd"},
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"]["code"] == "INVALID_ANCHOR_AGENT_ID"


class TestPromoteFromAgentStandaloneLocked:
    def test_422_when_anchor_declared_standalone(self, client: TestClient):
        # D3-a — server must reject standalone Agents even if a non-UI client
        # bypasses the disabled CatalogCard button.
        catalog_id = "app-standalone1"
        detail = _make_app(catalog_id, [_sample_role()])
        detail.scope_hint = "standalone"

        mock_svc = CatalogService.__new__(CatalogService)
        mock_svc.get_app = lambda aid: detail  # type: ignore[method-assign]
        set_catalog_service(mock_svc)

        resp = client.post("/builder/teams/from-agent", json={"anchor_agent_id": catalog_id})
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"]["code"] == "CATALOG_AGENT_STANDALONE_LOCKED"


class TestPromoteFromAgentNotFound:
    def test_404_when_app_missing(self, client: TestClient):
        def _raise(aid: str):
            raise CatalogAppNotFound(f"not found: {aid}", details={"app_id": aid})

        mock_svc = CatalogService.__new__(CatalogService)
        mock_svc.get_app = _raise  # type: ignore[method-assign]
        set_catalog_service(mock_svc)

        resp = client.post("/builder/teams/from-agent", json={"anchor_agent_id": "app-missing"})
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"]["code"] == "CATALOG_APP_NOT_FOUND"


class TestPromoteFromAgentBrokenSnapshot:
    def test_422_when_snapshot_missing(self, client: TestClient):
        def _raise(aid: str):
            raise CatalogSnapshotMissing(f"snapshot missing: {aid}", details={"app_id": aid})

        mock_svc = CatalogService.__new__(CatalogService)
        mock_svc.get_app = _raise  # type: ignore[method-assign]
        set_catalog_service(mock_svc)

        resp = client.post("/builder/teams/from-agent", json={"anchor_agent_id": "app-broken"})
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"]["code"] == "CATALOG_SNAPSHOT_MISSING"

    def test_422_when_role_profiles_empty(self, client: TestClient):
        catalog_id = "app-emptyroles"
        detail = _make_app(catalog_id, [])
        detail.blueprint_snapshot["role_profiles"] = []

        mock_svc = CatalogService.__new__(CatalogService)
        mock_svc.get_app = lambda aid: detail  # type: ignore[method-assign]
        set_catalog_service(mock_svc)

        resp = client.post("/builder/teams/from-agent", json={"anchor_agent_id": catalog_id})
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"]["code"] == "CATALOG_BLUEPRINT_INVALID"
