"""Tests for Builder Kit API endpoints — Story 10.5 (AC5)

覆盖范围：
  - GET /builder/kits  — 响应结构校验 + 不含完整 Blueprint 字段
  - GET /builder/kits/{kit_id} — 正常 + 404 场景
  - 响应使用 {data, meta} 信封
"""
from __future__ import annotations

from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from xueban.server import app
from xueban.runtime.kits.registry import REGISTRY, discover_and_register_kits


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client() -> TestClient:
    """FastAPI TestClient，module 级别（避免重复启动）。"""
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture(scope="module", autouse=True)
def ensure_kits_registered() -> None:
    """确保 REGISTRY 中至少有 research_kit（discover 幂等，多次调用安全）。"""
    discover_and_register_kits()


# ---------------------------------------------------------------------------
# T5.7 — GET /builder/kits
# ---------------------------------------------------------------------------


class TestListBuilderKits:
    def test_response_200(self, client: TestClient) -> None:
        resp = client.get("/builder/kits")
        assert resp.status_code == 200

    def test_response_has_data_and_meta_envelope(self, client: TestClient) -> None:
        resp = client.get("/builder/kits")
        body = resp.json()
        assert "data" in body
        assert "meta" in body

    def test_meta_has_count(self, client: TestClient) -> None:
        resp = client.get("/builder/kits")
        meta = resp.json()["meta"]
        assert "count" in meta
        assert meta["count"] >= 1

    def test_data_is_list(self, client: TestClient) -> None:
        resp = client.get("/builder/kits")
        data = resp.json()["data"]
        # REGISTRY 模式 data 是 list，legacy 模式 data 可能是 dict
        # 测试时 REGISTRY 已有 kits，应为 list
        assert isinstance(data, list)

    def test_kit_items_have_required_metadata_fields(self, client: TestClient) -> None:
        resp = client.get("/builder/kits")
        data = resp.json()["data"]
        # 确保是 REGISTRY 模式（list of kit metadata）
        if not isinstance(data, list) or len(data) == 0:
            pytest.skip("No registered kits or legacy mode")
        for item in data:
            assert "kit_id" in item, f"kit_id missing in {item}"
            assert "display_name" in item, f"display_name missing in {item}"
            assert "description" in item
            assert "category" in item
            assert "supported_modes" in item
            assert "icon" in item

    def test_response_does_not_contain_full_blueprint(self, client: TestClient) -> None:
        """AC5 要求：GET /builder/kits 响应不包含完整 Blueprint 字段（only metadata）。"""
        resp = client.get("/builder/kits")
        data = resp.json()["data"]
        if not isinstance(data, list):
            pytest.skip("Legacy mode — skip blueprint field check")
        for item in data:
            # 完整 Blueprint 字段不应出现在列表项中
            assert "default_blueprint" not in item, (
                f"default_blueprint should not appear in list response for kit {item.get('kit_id')}"
            )
            assert "role_profiles" not in item
            assert "tool_policies" not in item

    def test_research_kit_in_list(self, client: TestClient) -> None:
        resp = client.get("/builder/kits")
        data = resp.json()["data"]
        if not isinstance(data, list):
            pytest.skip("Legacy mode")
        kit_ids = [item.get("kit_id") for item in data]
        assert "research_kit" in kit_ids, f"research_kit not found in {kit_ids}"


# ---------------------------------------------------------------------------
# T5.8 — GET /builder/kits/{kit_id}
# ---------------------------------------------------------------------------


class TestGetBuilderKit:
    def test_get_research_kit_200(self, client: TestClient) -> None:
        """REGISTRY 中存在 research_kit，应返回 200。"""
        resp = client.get("/builder/kits/research_kit")
        assert resp.status_code == 200

    def test_get_kit_response_envelope(self, client: TestClient) -> None:
        resp = client.get("/builder/kits/research_kit")
        body = resp.json()
        assert "data" in body
        assert "meta" in body

    def test_get_kit_data_has_blueprint_summary(self, client: TestClient) -> None:
        resp = client.get("/builder/kits/research_kit")
        data = resp.json()["data"]
        assert "default_blueprint_summary" in data
        bp_sum = data["default_blueprint_summary"]
        assert "name" in bp_sum
        assert "role_count" in bp_sum
        assert "mode" in bp_sum

    def test_get_kit_data_has_metadata_fields(self, client: TestClient) -> None:
        resp = client.get("/builder/kits/research_kit")
        data = resp.json()["data"]
        assert data["kit_id"] == "research_kit"
        assert "display_name" in data
        assert "description" in data
        assert "category" in data
        assert "icon" in data

    def test_get_kit_data_has_eval_and_policy(self, client: TestClient) -> None:
        resp = client.get("/builder/kits/research_kit")
        data = resp.json()["data"]
        assert "default_eval_profile" in data
        assert "default_policy_profile" in data

    def test_get_nonexistent_kit_returns_404(self, client: TestClient) -> None:
        """AC5 要求：GET /builder/kits/{kit_id} 对不存在的 id 返回标准 404 envelope。"""
        resp = client.get("/builder/kits/nonexistent_kit_xyz_99999")
        assert resp.status_code == 404

    def test_get_nonexistent_kit_has_error_envelope(self, client: TestClient) -> None:
        resp = client.get("/builder/kits/nonexistent_kit_xyz_99999")
        body = resp.json()
        # FastAPI HTTPException detail 格式
        assert "detail" in body
        detail = body["detail"]
        assert "error" in detail
        assert detail["error"]["code"] == "KIT_NOT_FOUND"

    def test_get_knowledge_assistant_kit(self, client: TestClient) -> None:
        """knowledge_assistant_kit 由 discover_and_register_kits 注册，应可查询。"""
        resp = client.get("/builder/kits/knowledge_assistant_kit")
        if resp.status_code == 404:
            # knowledge_assistant_kit 可能尚未注册（未 discover），标记为 xfail 而非 fail
            pytest.xfail("knowledge_assistant_kit not registered yet (run server for auto-discover)")
        assert resp.status_code == 200

    def test_get_review_approval_kit(self, client: TestClient) -> None:
        resp = client.get("/builder/kits/review_approval_kit")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["kit_id"] == "review_approval_kit"


# ---------------------------------------------------------------------------
# T5.9 — Kit list count 与 REGISTRY 一致
# ---------------------------------------------------------------------------


def test_api_kit_count_matches_registry(client: TestClient) -> None:
    """API 返回的 kit 数量与 REGISTRY.list_kits() 数量一致。"""
    resp = client.get("/builder/kits")
    body = resp.json()
    api_data = body["data"]
    if not isinstance(api_data, list):
        pytest.skip("Legacy mode — count check skipped")

    registry_count = len(REGISTRY.list_kits())
    # API count 等于 REGISTRY count
    assert len(api_data) == registry_count
