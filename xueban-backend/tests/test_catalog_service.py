"""tests/test_catalog_service.py — CatalogService 单元测试 (Story 8.7 AC8)

覆盖：
  - register_published_app 写入 + summary 返回字段
  - list_apps 过滤（kit_type / 关键词）+ 分页
  - get_app 返回脱敏 blueprint_snapshot（不含 system_prompt / credentials_ref）
  - fork_app 生成新 blueprint_id + metadata.forked_from + 不修改原条目
  - 失败路径：app_id 不存在 → CatalogAppNotFound；snapshot 损坏 → CatalogBlueprintInvalid
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from xueban.runtime.catalog_service import (
    CatalogAppNotFound,
    CatalogBlueprintInvalid,
    CatalogService,
    RegisterPublishedAppRequest,
    redact_blueprint_snapshot,
)
from xueban.runtime.contracts_builder import (
    AgentBlueprint,
    KnowledgeBinding,
    PermissionRule,
    RoleProfile,
    ToolPolicy,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_blueprint(name: str = "Test Catalog Agent", goal: str = "Help test the catalog flow") -> AgentBlueprint:
    return AgentBlueprint(
        name=name,
        goal=goal,
        audience="developers",
        mode="single",
        role_profiles=[
            RoleProfile(
                name="planner",
                description="plans things",
                executor_kind="api",
                executor_provider="anthropic",
                metadata={"system_prompt": "SECRET — do not leak", "harmless": "ok"},
            )
        ],
        tool_policies=[
            ToolPolicy(
                tool_id="builtin:web_search",
                provider_id="prov-test",
                credentials_ref="prov-test",
                visibility="enabled",
                permission_rules=[PermissionRule(permission="allow", arg_pattern="")],
                default_permission="allow",
            )
        ],
        knowledge_bindings=[KnowledgeBinding(source_type="url", source_ref="https://example.com")],
        metadata={"api_key": "should-be-stripped", "author": "alice"},
    )


def _make_service(tmp_path: Path) -> CatalogService:
    return CatalogService(storage_dir=tmp_path / "catalog")


# ---------------------------------------------------------------------------
# register_published_app
# ---------------------------------------------------------------------------


def test_register_published_app_persists_and_returns_summary(tmp_path: Path):
    svc = _make_service(tmp_path)
    bp = _make_blueprint()
    req = RegisterPublishedAppRequest(
        blueprint=bp,
        template_id="bldr-aaaa1111",
        workflow_id="wf123",
        author="alice",
        kit_type="research",
    )

    summary = svc.register_published_app(req)

    assert summary.app_id.startswith("app-")
    assert summary.name == bp.name
    assert summary.goal == bp.goal
    assert summary.kit_type == "research"
    assert summary.author == "alice"
    assert summary.template_id == "bldr-aaaa1111"
    assert summary.workflow_id == "wf123"
    assert summary.fork_count == 0
    assert summary.published_at  # ISO timestamp present

    # File persisted
    persisted = list((tmp_path / "catalog").glob("*.json"))
    assert len(persisted) == 1


def test_register_unknown_kit_type_falls_back_to_custom(tmp_path: Path):
    svc = _make_service(tmp_path)
    req = RegisterPublishedAppRequest(blueprint=_make_blueprint(), kit_type="something_weird")
    summary = svc.register_published_app(req)
    assert summary.kit_type == "custom"


# ---------------------------------------------------------------------------
# list_apps — filter + pagination
# ---------------------------------------------------------------------------


def test_list_apps_returns_envelope_shape(tmp_path: Path):
    svc = _make_service(tmp_path)
    svc.register_published_app(
        RegisterPublishedAppRequest(blueprint=_make_blueprint("alpha", "Plan a research"), kit_type="research")
    )

    result = svc.list_apps()
    assert "apps" in result
    assert "total" in result
    assert "page" in result
    assert "page_size" in result
    assert result["total"] == 1
    assert len(result["apps"]) == 1


def test_list_apps_filters_by_kit_type(tmp_path: Path):
    svc = _make_service(tmp_path)
    svc.register_published_app(
        RegisterPublishedAppRequest(blueprint=_make_blueprint("a", "research goal"), kit_type="research")
    )
    svc.register_published_app(
        RegisterPublishedAppRequest(blueprint=_make_blueprint("b", "review goal"), kit_type="review_approval")
    )

    research = svc.list_apps(kit_type="research")
    review = svc.list_apps(kit_type="review_approval")
    everything = svc.list_apps(kit_type="all")

    assert research["total"] == 1
    assert review["total"] == 1
    assert everything["total"] == 2


def test_list_apps_keyword_searches_name_and_goal(tmp_path: Path):
    svc = _make_service(tmp_path)
    svc.register_published_app(
        RegisterPublishedAppRequest(blueprint=_make_blueprint("Newsroom", "find breaking news"), kit_type="research")
    )
    svc.register_published_app(
        RegisterPublishedAppRequest(blueprint=_make_blueprint("Reviewer", "approve docs"), kit_type="review_approval")
    )

    by_name = svc.list_apps(q="newsroom")
    by_goal = svc.list_apps(q="approve")
    none_match = svc.list_apps(q="absolutely-nothing-matches-this-string")

    assert by_name["total"] == 1
    assert by_name["apps"][0]["name"] == "Newsroom"
    assert by_goal["total"] == 1
    assert by_goal["apps"][0]["name"] == "Reviewer"
    assert none_match["total"] == 0
    assert none_match["apps"] == []


def test_list_apps_combines_kit_type_and_keyword(tmp_path: Path):
    svc = _make_service(tmp_path)
    svc.register_published_app(
        RegisterPublishedAppRequest(blueprint=_make_blueprint("Newsroom", "find breaking news"), kit_type="research")
    )
    svc.register_published_app(
        RegisterPublishedAppRequest(blueprint=_make_blueprint("News Reviewer", "review news drafts"), kit_type="review_approval")
    )

    result = svc.list_apps(kit_type="research", q="news")
    assert result["total"] == 1
    assert result["apps"][0]["name"] == "Newsroom"


def test_list_apps_pagination_window(tmp_path: Path):
    svc = _make_service(tmp_path)
    for i in range(5):
        svc.register_published_app(
            RegisterPublishedAppRequest(
                blueprint=_make_blueprint(f"Agent {i}", f"goal {i}"),
                kit_type="custom",
            )
        )

    page1 = svc.list_apps(page=1, page_size=2)
    page2 = svc.list_apps(page=2, page_size=2)
    page3 = svc.list_apps(page=3, page_size=2)

    assert page1["total"] == 5
    assert page1["page"] == 1
    assert page1["page_size"] == 2
    assert len(page1["apps"]) == 2
    assert len(page2["apps"]) == 2
    assert len(page3["apps"]) == 1


# ---------------------------------------------------------------------------
# get_app — sensitive field redaction (AC3)
# ---------------------------------------------------------------------------


def test_get_app_strips_sensitive_fields(tmp_path: Path):
    svc = _make_service(tmp_path)
    summary = svc.register_published_app(
        RegisterPublishedAppRequest(blueprint=_make_blueprint(), kit_type="research")
    )

    detail = svc.get_app(summary.app_id)

    snapshot = detail.blueprint_snapshot
    # Top-level must not include redacted keys
    for k in ("system_prompt", "private_key", "byok", "api_key", "provider_credentials", "credentials"):
        assert k not in snapshot

    # Role metadata redacted
    role0 = snapshot["role_profiles"][0]
    assert "system_prompt" not in role0.get("metadata", {})
    assert role0.get("metadata", {}).get("harmless") == "ok"

    # Tool policy credentials_ref replaced
    tp0 = snapshot["tool_policies"][0]
    assert tp0["credentials_ref"] == "[redacted]"

    # blueprint top-level metadata: api_key removed but author kept
    assert "api_key" not in snapshot.get("metadata", {})
    assert snapshot.get("metadata", {}).get("author") == "alice"


def test_get_app_returns_role_names_and_count(tmp_path: Path):
    svc = _make_service(tmp_path)
    summary = svc.register_published_app(
        RegisterPublishedAppRequest(blueprint=_make_blueprint(), kit_type="research")
    )

    detail = svc.get_app(summary.app_id)
    assert detail.role_count == 1
    assert detail.role_names == ["planner"]


def test_get_unknown_app_raises_not_found(tmp_path: Path):
    svc = _make_service(tmp_path)
    with pytest.raises(CatalogAppNotFound):
        svc.get_app("app-does-not-exist")


# ---------------------------------------------------------------------------
# fork_app (AC6)
# ---------------------------------------------------------------------------


def test_fork_app_creates_new_blueprint_with_new_id(tmp_path: Path):
    svc = _make_service(tmp_path)
    summary = svc.register_published_app(
        RegisterPublishedAppRequest(blueprint=_make_blueprint(), kit_type="research")
    )

    result = svc.fork_app(summary.app_id)

    assert result.blueprint.blueprint_id != summary.blueprint_id
    assert result.blueprint_id == result.blueprint.blueprint_id
    assert result.forked_from == summary.app_id


def test_fork_app_records_metadata_forked_from(tmp_path: Path):
    svc = _make_service(tmp_path)
    summary = svc.register_published_app(
        RegisterPublishedAppRequest(blueprint=_make_blueprint(), kit_type="research")
    )

    result = svc.fork_app(summary.app_id)
    assert result.blueprint.metadata.get("forked_from") == summary.app_id


def test_fork_app_resets_publish_profile_to_none(tmp_path: Path):
    """A fork is a draft; user must explicitly publish it again."""
    svc = _make_service(tmp_path)
    summary = svc.register_published_app(
        RegisterPublishedAppRequest(blueprint=_make_blueprint(), kit_type="research")
    )

    result = svc.fork_app(summary.app_id)
    assert result.blueprint.publish_profile.target == "none"
    assert result.blueprint.publish_profile.visibility == "private"


def test_fork_app_does_not_modify_original_record(tmp_path: Path):
    svc = _make_service(tmp_path)
    summary = svc.register_published_app(
        RegisterPublishedAppRequest(blueprint=_make_blueprint(), kit_type="research")
    )

    svc.fork_app(summary.app_id)

    detail = svc.get_app(summary.app_id)
    assert detail.app_id == summary.app_id
    assert detail.name == summary.name
    # fork_count incremented
    assert detail.fork_count == 1


def test_fork_app_preserves_roles_and_knowledge(tmp_path: Path):
    svc = _make_service(tmp_path)
    summary = svc.register_published_app(
        RegisterPublishedAppRequest(blueprint=_make_blueprint(), kit_type="research")
    )

    result = svc.fork_app(summary.app_id)
    assert len(result.blueprint.role_profiles) == 1
    assert result.blueprint.role_profiles[0].name == "planner"
    assert len(result.blueprint.knowledge_bindings) == 1


def test_fork_app_corrupted_snapshot_raises_blueprint_invalid(tmp_path: Path):
    svc = _make_service(tmp_path)
    summary = svc.register_published_app(
        RegisterPublishedAppRequest(blueprint=_make_blueprint(), kit_type="research")
    )

    # Corrupt the stored record: remove role_profiles entirely (single mode requires ≥1 role)
    record_path = (tmp_path / "catalog" / f"{summary.app_id}.json")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["blueprint_snapshot"]["role_profiles"] = []
    record_path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(CatalogBlueprintInvalid) as exc_info:
        svc.fork_app(summary.app_id)
    assert exc_info.value.code == "CATALOG_BLUEPRINT_INVALID"


def test_fork_unknown_app_raises_not_found(tmp_path: Path):
    svc = _make_service(tmp_path)
    with pytest.raises(CatalogAppNotFound):
        svc.fork_app("app-does-not-exist")


# ---------------------------------------------------------------------------
# redact_blueprint_snapshot — pure-function tests
# ---------------------------------------------------------------------------


def test_redact_strips_top_level_sensitive_keys():
    raw = {
        "blueprint_id": "bp-x",
        "system_prompt": "leak",
        "byok": "leak",
        "api_key": "leak",
        "metadata": {"private_key": "leak", "ok": "kept"},
        "role_profiles": [
            {
                "role_id": "r1",
                "name": "x",
                "system_prompt": "leak",
                "metadata": {"system_prompt": "leak", "ok": "kept"},
                "sub_agents": [
                    {
                        "role_id": "r1-s1",
                        "name": "y",
                        "system_prompt": "leak",
                        "metadata": {"private_key": "leak"},
                        "sub_agents": [],
                    }
                ],
            }
        ],
        "tool_policies": [
            {"tool_id": "t1", "credentials_ref": "secret-id", "metadata": {"api_key": "leak"}}
        ],
    }
    safe = redact_blueprint_snapshot(raw)

    for k in ("system_prompt", "byok", "api_key"):
        assert k not in safe
    assert "private_key" not in safe.get("metadata", {})
    assert safe["metadata"]["ok"] == "kept"

    role = safe["role_profiles"][0]
    assert "system_prompt" not in role
    assert "system_prompt" not in role["metadata"]
    assert role["metadata"]["ok"] == "kept"
    assert "system_prompt" not in role["sub_agents"][0]
    assert "private_key" not in role["sub_agents"][0]["metadata"]

    tp = safe["tool_policies"][0]
    assert tp["credentials_ref"] == "[redacted]"
    assert "api_key" not in tp.get("metadata", {})


def test_redact_scrubs_knowledge_bindings_metadata():
    raw = {
        "blueprint_id": "bp-y",
        "knowledge_bindings": [
            {
                "pack_id": "kb-1",
                "metadata": {"api_key": "leak", "ok": "kept"},
            },
            {
                "pack_id": "kb-2",
                "metadata": {"private_key": "leak", "safe_field": "kept"},
            },
            {"pack_id": "kb-3"},  # no metadata key at all
        ],
    }
    safe = redact_blueprint_snapshot(raw)

    bindings = safe["knowledge_bindings"]
    assert len(bindings) == 3
    assert "api_key" not in bindings[0]["metadata"]
    assert bindings[0]["metadata"]["ok"] == "kept"
    assert "private_key" not in bindings[1]["metadata"]
    assert bindings[1]["metadata"]["safe_field"] == "kept"
    assert bindings[2]["metadata"] == {}


def test_redact_scrubs_knowledge_bindings_top_level_sensitive_keys():
    raw = {
        "blueprint_id": "bp-z",
        "knowledge_bindings": [
            {
                "pack_id": "kb-top",
                "api_key": "top-level-leak",
                "secret": "also-leaked",
                "safe_field": "kept",
                "metadata": {},
            },
        ],
    }
    safe = redact_blueprint_snapshot(raw)

    binding = safe["knowledge_bindings"][0]
    assert "api_key" not in binding
    assert "secret" not in binding
    assert binding["pack_id"] == "kb-top"
    assert binding["safe_field"] == "kept"
