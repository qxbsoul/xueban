"""tests/test_catalog_api.py — Catalog API endpoint 测试 (Story 8.7 AC8)

覆盖：
  - GET /catalog/apps：envelope + 分页字段稳定
  - GET /catalog/apps/{app_id}：脱敏 (AC3)
  - POST /catalog/apps/{app_id}/fork：成功创建新 Blueprint + metadata.forked_from
  - 失败路径：404 → 错误 envelope，error.code 稳定
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import xueban.api.catalog as catalog_api
import xueban.runtime.catalog_service as catalog_service
from xueban.runtime.catalog_service import (
    CatalogService,
    RegisterPublishedAppRequest,
)
from xueban.runtime.contracts_builder import (
    AgentBlueprint,
    KnowledgeBinding,
    RoleProfile,
    ToolPolicy,
)
from xueban.server import app


client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_catalog(tmp_path: Path):
    """Replace the global catalog singleton with an isolated tmp-backed instance."""
    svc = CatalogService(storage_dir=tmp_path / "catalog")
    catalog_service.set_service(svc)
    try:
        yield svc
    finally:
        catalog_service.set_service(CatalogService())


def _make_blueprint(name: str = "API Test Agent", goal: str = "Test the Catalog API"):
    return AgentBlueprint(
        name=name,
        goal=goal,
        audience="developers",
        mode="single",
        role_profiles=[
            RoleProfile(
                name="agent",
                description="acts",
                executor_kind="api",
                executor_provider="anthropic",
                metadata={"system_prompt": "INTERNAL", "harmless": "ok"},
            )
        ],
        tool_policies=[
            ToolPolicy(
                tool_id="builtin:web_search",
                provider_id="prov-x",
                credentials_ref="prov-x",
                visibility="enabled",
            )
        ],
        knowledge_bindings=[KnowledgeBinding(source_type="url", source_ref="https://example.com")],
        metadata={"api_key": "should-go-away", "author": "alice"},
    )


# ---------------------------------------------------------------------------
# GET /catalog/apps
# ---------------------------------------------------------------------------


def test_list_apps_empty(_isolated_catalog):
    resp = client.get("/catalog/apps")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["apps"] == []
    assert body["meta"]["total"] == 0
    assert body["meta"]["page"] == 1
    assert body["meta"]["page_size"] == 20


def test_list_apps_envelope_shape_with_data(_isolated_catalog):
    _isolated_catalog.register_published_app(
        RegisterPublishedAppRequest(blueprint=_make_blueprint(), kit_type="research")
    )
    resp = client.get("/catalog/apps")
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body
    assert "meta" in body
    assert "apps" in body["data"]
    assert body["meta"]["total"] == 1
    apps = body["data"]["apps"]
    assert len(apps) == 1
    # Stable summary fields on the wire
    expected_keys = {
        "app_id", "name", "goal", "kit_type", "author",
        "published_at", "fork_count", "forked_from",
        "template_id", "workflow_id", "blueprint_id",
    }
    assert expected_keys.issubset(apps[0].keys())


def test_list_apps_filter_by_kit_type(_isolated_catalog):
    _isolated_catalog.register_published_app(
        RegisterPublishedAppRequest(blueprint=_make_blueprint("R", "research goal"), kit_type="research")
    )
    _isolated_catalog.register_published_app(
        RegisterPublishedAppRequest(blueprint=_make_blueprint("V", "review goal"), kit_type="review_approval")
    )

    research = client.get("/catalog/apps?kit_type=research").json()
    assert research["meta"]["total"] == 1
    assert research["data"]["apps"][0]["name"] == "R"


def test_list_apps_keyword_combinable_with_kit_type(_isolated_catalog):
    _isolated_catalog.register_published_app(
        RegisterPublishedAppRequest(blueprint=_make_blueprint("Newsroom", "find breaking news"), kit_type="research")
    )
    _isolated_catalog.register_published_app(
        RegisterPublishedAppRequest(blueprint=_make_blueprint("News Reviewer", "review news drafts"), kit_type="review_approval")
    )

    body = client.get("/catalog/apps?kit_type=research&q=news").json()
    assert body["meta"]["total"] == 1
    assert body["data"]["apps"][0]["name"] == "Newsroom"


def test_list_apps_pagination_stable_meta(_isolated_catalog):
    for i in range(3):
        _isolated_catalog.register_published_app(
            RegisterPublishedAppRequest(
                blueprint=_make_blueprint(f"Agent {i}", f"goal {i}"),
                kit_type="custom",
            )
        )

    page1 = client.get("/catalog/apps?page=1&page_size=2").json()
    page2 = client.get("/catalog/apps?page=2&page_size=2").json()

    assert page1["meta"]["total"] == 3
    assert page1["meta"]["page"] == 1
    assert page1["meta"]["page_size"] == 2
    assert len(page1["data"]["apps"]) == 2

    assert page2["meta"]["page"] == 2
    assert len(page2["data"]["apps"]) == 1


def test_list_apps_invalid_pagination_returns_422(_isolated_catalog):
    # page must be >= 1
    resp = client.get("/catalog/apps?page=0")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /catalog/apps/{app_id}
# ---------------------------------------------------------------------------


def test_get_app_returns_sanitized_detail(_isolated_catalog):
    summary = _isolated_catalog.register_published_app(
        RegisterPublishedAppRequest(blueprint=_make_blueprint(), kit_type="research", author="alice")
    )

    resp = client.get(f"/catalog/apps/{summary.app_id}")
    assert resp.status_code == 200
    body = resp.json()
    detail = body["data"]
    assert detail["app_id"] == summary.app_id
    assert detail["mode"] == "single"
    assert detail["role_count"] == 1
    assert detail["role_names"] == ["agent"]

    snapshot = detail["blueprint_snapshot"]
    # AC3 — sensitive top-level fields stripped
    assert "system_prompt" not in snapshot
    assert "api_key" not in snapshot
    assert "byok" not in snapshot
    # api_key removed from blueprint metadata; author kept
    assert "api_key" not in snapshot.get("metadata", {})
    assert snapshot["metadata"]["author"] == "alice"
    # role metadata redacted
    assert "system_prompt" not in snapshot["role_profiles"][0]["metadata"]
    # tool credentials_ref replaced
    assert snapshot["tool_policies"][0]["credentials_ref"] == "[redacted]"


def test_get_unknown_app_returns_error_envelope(_isolated_catalog):
    resp = client.get("/catalog/apps/app-does-not-exist")
    # XuebanError handler maps to 400 by default
    assert resp.status_code in (400, 404)
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == "CATALOG_APP_NOT_FOUND"


# ---------------------------------------------------------------------------
# POST /catalog/apps/{app_id}/fork
# ---------------------------------------------------------------------------


def test_fork_app_returns_new_blueprint_id_and_records_forked_from(_isolated_catalog):
    summary = _isolated_catalog.register_published_app(
        RegisterPublishedAppRequest(blueprint=_make_blueprint(), kit_type="research")
    )

    resp = client.post(f"/catalog/apps/{summary.app_id}/fork")
    assert resp.status_code == 200
    body = resp.json()
    data = body["data"]
    assert "blueprint_id" in data
    assert data["blueprint_id"] != summary.blueprint_id
    assert data["forked_from"] == summary.app_id
    assert "blueprint" in data
    bp = data["blueprint"]
    assert bp["metadata"]["forked_from"] == summary.app_id
    assert bp["publish_profile"]["target"] == "none"


def test_fork_unknown_app_returns_stable_error(_isolated_catalog):
    resp = client.post("/catalog/apps/app-not-here/fork")
    assert resp.status_code in (400, 404)
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == "CATALOG_APP_NOT_FOUND"


def test_fork_increments_fork_count(_isolated_catalog):
    summary = _isolated_catalog.register_published_app(
        RegisterPublishedAppRequest(blueprint=_make_blueprint(), kit_type="research")
    )

    client.post(f"/catalog/apps/{summary.app_id}/fork")
    client.post(f"/catalog/apps/{summary.app_id}/fork")

    detail = client.get(f"/catalog/apps/{summary.app_id}").json()["data"]
    assert detail["fork_count"] == 2
