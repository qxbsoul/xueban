"""Story 13.3 — POST /builder/blueprints/{blueprint_id}/import-agent 测试。

覆盖：
  - 正常引入：返回带 imported_from 的 RoleProfile
  - catalog_agent_id 不存在 → 404
  - 快照无 role_profiles（损坏）→ 422
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

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


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_catalog_app(app_id: str, role_profiles: list[Dict[str, Any]]) -> CatalogAppDetail:
    """Build a minimal CatalogAppDetail for mocking."""
    snapshot: Dict[str, Any] = {
        "blueprint_id": f"bp-{app_id}",
        "version": "1.0",
        "name": "Test Agent",
        "goal": "Test goal",
        "audience": "Test",
        "mode": "single",
        "role_profiles": role_profiles,
        "tool_policies": [],
        "knowledge_bindings": [],
        "memory_profile": {
            "scope": "session",
            "writeback_target": None,
            "enabled": True,
            "metadata": {},
        },
        "eval_profile": {
            "smoke_eval_enabled": False,
            "eval_criteria": [],
            "regression_gate": False,
            "metadata": {},
        },
        "publish_profile": {
            "target": "none",
            "visibility": "private",
            "publish_ref": "",
            "metadata": {},
        },
        "metadata": {},
    }
    return CatalogAppDetail(
        app_id=app_id,
        name="Test Agent",
        goal="Test goal",
        kit_type="custom",
        author="tester",
        published_at="2026-04-28T00:00:00Z",
        fork_count=0,
        forked_from=None,
        template_id="",
        workflow_id="",
        blueprint_id=f"bp-{app_id}",
        mode="single",
        role_names=["Analyst"],
        role_count=1,
        description="Test goal",
        blueprint_snapshot=snapshot,
    )


def _sample_role(name: str = "Analyst") -> Dict[str, Any]:
    return {
        "role_id": "role-orig",
        "name": name,
        "description": "Test role",
        "persona": "An analyst",
        "responsibilities": ["Analyse data"],
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
    }


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestImportAgentNormal:
    """AC5: 正常引入流程。"""

    def test_returns_role_profile_with_imported_from(self, client: TestClient):
        """引入成功：返回 data 包含新 role_id 和 metadata.imported_from。"""
        catalog_id = "app-aabbccdd1234"
        detail = _make_catalog_app(catalog_id, [_sample_role()])

        mock_svc = CatalogService.__new__(CatalogService)
        mock_svc.get_app = lambda aid: detail  # type: ignore[method-assign]
        set_catalog_service(mock_svc)

        resp = client.post(
            f"/builder/blueprints/bp-draft-001/import-agent",
            json={"catalog_agent_id": catalog_id},
        )
        assert resp.status_code == 200
        body = resp.json()
        role = body["data"]

        # role_id 应以 imported- 开头
        assert role["role_id"].startswith("imported-")
        assert catalog_id[:8] in role["role_id"]

        # metadata.imported_from 保留谱系
        assert role["metadata"]["imported_from"] == catalog_id

        # 原始字段被保留
        assert role["name"] == "Analyst"
        assert role["description"] == "Test role"

    def test_meta_contains_blueprint_id_and_catalog_id(self, client: TestClient):
        """返回 meta 字段包含 blueprint_id 和 catalog_agent_id。"""
        catalog_id = "app-test1234"
        detail = _make_catalog_app(catalog_id, [_sample_role()])

        mock_svc = CatalogService.__new__(CatalogService)
        mock_svc.get_app = lambda aid: detail  # type: ignore[method-assign]
        set_catalog_service(mock_svc)

        resp = client.post(
            "/builder/blueprints/bp-my-draft/import-agent",
            json={"catalog_agent_id": catalog_id},
        )
        assert resp.status_code == 200
        meta = resp.json()["meta"]
        assert meta["blueprint_id"] == "bp-my-draft"
        assert meta["catalog_agent_id"] == catalog_id

    def test_original_metadata_merged(self, client: TestClient):
        """引入后，原始 role.metadata 字段被保留，并追加 imported_from。"""
        catalog_id = "app-meta1234"
        role = _sample_role()
        role["metadata"] = {"custom_key": "custom_val"}
        detail = _make_catalog_app(catalog_id, [role])

        mock_svc = CatalogService.__new__(CatalogService)
        mock_svc.get_app = lambda aid: detail  # type: ignore[method-assign]
        set_catalog_service(mock_svc)

        resp = client.post(
            "/builder/blueprints/bp-x/import-agent",
            json={"catalog_agent_id": catalog_id},
        )
        assert resp.status_code == 200
        meta = resp.json()["data"]["metadata"]
        assert meta["custom_key"] == "custom_val"
        assert meta["imported_from"] == catalog_id


class TestImportAgentNotFound:
    """AC4: Catalog Agent 不存在 → 404。"""

    def test_404_when_app_not_found(self, client: TestClient):
        """get_app 抛 CatalogAppNotFound → HTTP 404。"""
        def _raise(aid: str):
            raise CatalogAppNotFound(f"not found: {aid}", details={"app_id": aid})

        mock_svc = CatalogService.__new__(CatalogService)
        mock_svc.get_app = _raise  # type: ignore[method-assign]
        set_catalog_service(mock_svc)

        resp = client.post(
            "/builder/blueprints/bp-x/import-agent",
            json={"catalog_agent_id": "app-does-not-exist"},
        )
        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert detail["error"]["code"] == "CATALOG_APP_NOT_FOUND"

    def test_404_error_message_contains_agent_id(self, client: TestClient):
        """404 error detail 包含 catalog_agent_id。"""
        bad_id = "app-missing-xyz"

        def _raise(aid: str):
            raise CatalogAppNotFound(f"not found: {aid}", details={"app_id": aid})

        mock_svc = CatalogService.__new__(CatalogService)
        mock_svc.get_app = _raise  # type: ignore[method-assign]
        set_catalog_service(mock_svc)

        resp = client.post(
            "/builder/blueprints/bp-x/import-agent",
            json={"catalog_agent_id": bad_id},
        )
        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert bad_id in str(detail)


class TestImportAgentBrokenSnapshot:
    """AC4: 快照损坏 → 422。"""

    def test_422_when_snapshot_missing(self, client: TestClient):
        """get_app 抛 CatalogSnapshotMissing → HTTP 422。"""
        def _raise(aid: str):
            raise CatalogSnapshotMissing(f"snapshot missing: {aid}", details={"app_id": aid})

        mock_svc = CatalogService.__new__(CatalogService)
        mock_svc.get_app = _raise  # type: ignore[method-assign]
        set_catalog_service(mock_svc)

        resp = client.post(
            "/builder/blueprints/bp-x/import-agent",
            json={"catalog_agent_id": "app-broken"},
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["error"]["code"] == "CATALOG_SNAPSHOT_MISSING"

    def test_422_when_role_profiles_empty(self, client: TestClient):
        """blueprint_snapshot.role_profiles 为空列表 → 422 CATALOG_BLUEPRINT_INVALID。"""
        catalog_id = "app-emptyroles"
        detail = _make_catalog_app(catalog_id, [])  # empty role_profiles
        detail.blueprint_snapshot["role_profiles"] = []

        mock_svc = CatalogService.__new__(CatalogService)
        mock_svc.get_app = lambda aid: detail  # type: ignore[method-assign]
        set_catalog_service(mock_svc)

        resp = client.post(
            "/builder/blueprints/bp-x/import-agent",
            json={"catalog_agent_id": catalog_id},
        )
        assert resp.status_code == 422
        err_detail = resp.json()["detail"]
        assert err_detail["error"]["code"] == "CATALOG_BLUEPRINT_INVALID"

    def test_422_request_body_missing_catalog_id(self, client: TestClient):
        """请求体缺少 catalog_agent_id → 422 (FastAPI validation)。"""
        resp = client.post(
            "/builder/blueprints/bp-x/import-agent",
            json={},
        )
        assert resp.status_code == 422

    def test_422_when_catalog_agent_id_empty_string(self, client: TestClient):
        """Round-1 follow-up M4: 空字符串 catalog_agent_id → 422 (Field min_length=1)。"""
        resp = client.post(
            "/builder/blueprints/bp-x/import-agent",
            json={"catalog_agent_id": ""},
        )
        assert resp.status_code == 422


class TestImportAgentRoleIdUniqueness:
    """Round-1 follow-up H2: 同一 catalog agent 同一秒内连点两次，role_id 必不重复。"""

    def test_role_id_unique_within_same_second(self, client: TestClient):
        """Two consecutive imports of the same catalog agent must produce distinct role_id."""
        catalog_id = "app-collision1234"
        detail = _make_catalog_app(catalog_id, [_sample_role()])

        mock_svc = CatalogService.__new__(CatalogService)
        mock_svc.get_app = lambda aid: detail  # type: ignore[method-assign]
        set_catalog_service(mock_svc)

        ids: set[str] = set()
        # Freeze time.time() so int(time.time()) is identical for both calls,
        # which would have produced a collision under the pre-fix algorithm.
        with patch("xueban.api.builder.time.time", return_value=1714377600.0):
            for _ in range(5):
                resp = client.post(
                    f"/builder/blueprints/bp-x/import-agent",
                    json={"catalog_agent_id": catalog_id},
                )
                assert resp.status_code == 200
                ids.add(resp.json()["data"]["role_id"])

        # All 5 imports share the same int(time.time()) component yet must
        # remain unique thanks to the uuid4 suffix.
        assert len(ids) == 5, f"role_id collision: {ids}"
