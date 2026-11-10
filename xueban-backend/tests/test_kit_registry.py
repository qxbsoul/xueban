"""Tests for KitRegistry — Story 10.5 (AC5)

覆盖范围：
  - KitRegistry.register() — 正常注册 + 缺字段阻断
  - KitRegistry.get() — 查找存在和不存在的 kit_id
  - KitRegistry.list_kits() — 全量 + 按 category 过滤
  - KitRegistry.get_default_blueprint() — 正常 + 不存在
  - KitRegistry.get_default_eval_profile() — 正常 + 不存在
  - KitValidationError — 缺少 default_eval_profile 抛出正确错误
  - discover_and_register_kits() — 扫描后注册的 Kit 数量 >= 3
"""
from __future__ import annotations

import pytest

from xueban.runtime.contracts_builder import (
    AgentBlueprint,
    EvalProfile,
    RoleProfile,
)
from xueban.runtime.kits.registry import (
    REGISTRY as _GLOBAL_REGISTRY,
    KitDefinition,
    KitRegistry,
    KitValidationError,
    PolicyProfile,
    SceneDefinition,
    discover_and_register_kits,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_minimal_blueprint(name: str = "Test Agent") -> AgentBlueprint:
    """最小有效 AgentBlueprint（1 个角色）。"""
    return AgentBlueprint(
        name=name,
        goal=f"Test goal for {name}",
        mode="single",
        role_profiles=[
            RoleProfile(
                name="test_role",
                description="Test role for kit registry tests",
            )
        ],
    )


def _make_valid_eval_profile() -> EvalProfile:
    """有意义的 EvalProfile（至少有 eval_criteria）。"""
    return EvalProfile(
        smoke_eval_enabled=True,
        eval_criteria=["accuracy", "completeness"],
        regression_gate=False,
    )


def _make_valid_policy_profile(profile_id: str = "test-policy") -> PolicyProfile:
    return PolicyProfile(
        profile_id=profile_id,
        display_name="Test Policy",
        default_tool_permission="ask",
        allow_tool_ids=["builtin:web_search"],
    )


def _make_kit(
    kit_id: str = "test_kit",
    category: str = "research",
    eval_profile: EvalProfile | None = None,
) -> KitDefinition:
    """构造一个完整的 KitDefinition（用于测试注册）。"""
    return KitDefinition(
        kit_id=kit_id,
        display_name=f"Test Kit — {kit_id}",
        description="A test kit for Story 10.5 unit tests.",
        category=category,
        supported_modes=["goal", "scene"],
        default_blueprint=_make_minimal_blueprint(kit_id),
        default_scene=SceneDefinition(scene_id=f"scene-{kit_id}", display_name=kit_id),
        default_policy_profile=_make_valid_policy_profile(f"policy-{kit_id}"),
        default_eval_profile=eval_profile or _make_valid_eval_profile(),
        default_result_view="research_report",
        recommended_inputs=["topic", "format"],
        icon="🧪",
    )


@pytest.fixture()
def fresh_registry() -> KitRegistry:
    """返回一个全新的 KitRegistry 实例（与 REGISTRY 单例隔离）。"""
    return KitRegistry()


# ---------------------------------------------------------------------------
# T5.1 — register / get / list
# ---------------------------------------------------------------------------


def test_register_and_get(fresh_registry: KitRegistry) -> None:
    kit = _make_kit("alpha")
    fresh_registry.register(kit)
    retrieved = fresh_registry.get("alpha")
    assert retrieved is not None
    assert retrieved.kit_id == "alpha"
    assert retrieved.display_name == "Test Kit — alpha"


def test_get_nonexistent_returns_none(fresh_registry: KitRegistry) -> None:
    result = fresh_registry.get("does_not_exist")
    assert result is None


def test_list_kits_all(fresh_registry: KitRegistry) -> None:
    fresh_registry.register(_make_kit("kit1", "research"))
    fresh_registry.register(_make_kit("kit2", "knowledge"))
    fresh_registry.register(_make_kit("kit3", "research"))
    kits = fresh_registry.list_kits()
    assert len(kits) == 3


def test_list_kits_by_category(fresh_registry: KitRegistry) -> None:
    fresh_registry.register(_make_kit("r1", "research"))
    fresh_registry.register(_make_kit("k1", "knowledge"))
    fresh_registry.register(_make_kit("r2", "research"))
    research_kits = fresh_registry.list_kits(category="research")
    assert len(research_kits) == 2
    assert all(k.category == "research" for k in research_kits)
    knowledge_kits = fresh_registry.list_kits(category="knowledge")
    assert len(knowledge_kits) == 1


def test_list_kits_empty_registry(fresh_registry: KitRegistry) -> None:
    assert fresh_registry.list_kits() == []


# ---------------------------------------------------------------------------
# T5.2 — validate（缺字段阻断注册）
# ---------------------------------------------------------------------------


def test_register_raises_when_eval_profile_empty(fresh_registry: KitRegistry) -> None:
    """缺少有意义的 default_eval_profile 时 register() 抛 KitValidationError。"""
    empty_eval = EvalProfile(
        smoke_eval_enabled=False,
        eval_criteria=[],
        regression_gate=False,
    )
    kit = _make_kit("bad_kit", eval_profile=empty_eval)
    with pytest.raises(KitValidationError) as exc_info:
        fresh_registry.register(kit)

    err = exc_info.value
    assert "bad_kit" in err.message
    assert "default_eval_profile" in err.message
    assert err.code == "KIT_VALIDATION_FAILED"


def test_register_raises_error_format(fresh_registry: KitRegistry) -> None:
    """KitValidationError 消息格式符合 AC4 规范。"""
    empty_eval = EvalProfile(smoke_eval_enabled=False, eval_criteria=[], regression_gate=False)
    kit = _make_kit("custom_kit", eval_profile=empty_eval)
    with pytest.raises(KitValidationError) as exc_info:
        fresh_registry.register(kit)

    msg = exc_info.value.message
    # AC4 格式要求：Kit 'X' is missing required field: Y.
    assert "custom_kit" in msg
    assert "missing required field" in msg
    assert "default_eval_profile" in msg
    # Cannot register... 句子
    assert "Cannot register kit" in msg


def test_register_succeeds_with_regression_gate_only(fresh_registry: KitRegistry) -> None:
    """regression_gate=True 即可满足 eval profile 校验（无需 criteria）。"""
    eval_profile = EvalProfile(
        smoke_eval_enabled=False,
        eval_criteria=[],
        regression_gate=True,
    )
    kit = _make_kit("gate_only_kit", eval_profile=eval_profile)
    fresh_registry.register(kit)
    assert fresh_registry.get("gate_only_kit") is not None


def test_register_succeeds_with_smoke_eval_only(fresh_registry: KitRegistry) -> None:
    """smoke_eval_enabled=True 即可满足 eval profile 校验。"""
    eval_profile = EvalProfile(
        smoke_eval_enabled=True,
        eval_criteria=[],
        regression_gate=False,
    )
    kit = _make_kit("smoke_only_kit", eval_profile=eval_profile)
    fresh_registry.register(kit)
    assert fresh_registry.get("smoke_only_kit") is not None


# ---------------------------------------------------------------------------
# T5.3 — get_default_blueprint / get_default_eval_profile
# ---------------------------------------------------------------------------


def test_get_default_blueprint(fresh_registry: KitRegistry) -> None:
    kit = _make_kit("bp_kit")
    fresh_registry.register(kit)
    bp = fresh_registry.get_default_blueprint("bp_kit")
    assert bp.name == "bp_kit"


def test_get_default_blueprint_raises_for_unknown(fresh_registry: KitRegistry) -> None:
    with pytest.raises(KeyError, match="unknown_kit"):
        fresh_registry.get_default_blueprint("unknown_kit")


def test_get_default_eval_profile(fresh_registry: KitRegistry) -> None:
    kit = _make_kit("eval_kit")
    fresh_registry.register(kit)
    ep = fresh_registry.get_default_eval_profile("eval_kit")
    assert ep.smoke_eval_enabled is True
    assert "accuracy" in ep.eval_criteria


def test_get_default_eval_profile_raises_for_unknown(fresh_registry: KitRegistry) -> None:
    with pytest.raises(KeyError, match="missing_kit"):
        fresh_registry.get_default_eval_profile("missing_kit")


# ---------------------------------------------------------------------------
# T5.4 — KitDefinition 字段校验
# ---------------------------------------------------------------------------


def test_kit_definition_invalid_category() -> None:
    with pytest.raises(Exception):
        _make_kit("bad_cat").__class__(
            kit_id="bad_cat",
            display_name="Bad Cat",
            description="test",
            category="invalid_category",
            supported_modes=["goal"],
            default_blueprint=_make_minimal_blueprint(),
            default_scene=SceneDefinition(),
            default_policy_profile=_make_valid_policy_profile(),
            default_eval_profile=_make_valid_eval_profile(),
            default_result_view="research_report",
            recommended_inputs=[],
            icon="🐱",
        )


def test_kit_definition_invalid_result_view() -> None:
    with pytest.raises(Exception):
        KitDefinition(
            kit_id="bad_view",
            display_name="Bad View",
            description="test",
            category="research",
            supported_modes=["goal"],
            default_blueprint=_make_minimal_blueprint(),
            default_scene=SceneDefinition(),
            default_policy_profile=_make_valid_policy_profile(),
            default_eval_profile=_make_valid_eval_profile(),
            default_result_view="totally_invalid",
            recommended_inputs=[],
            icon="📋",
        )


def test_kit_definition_invalid_mode() -> None:
    with pytest.raises(Exception):
        KitDefinition(
            kit_id="bad_mode",
            display_name="Bad Mode",
            description="test",
            category="research",
            supported_modes=["goal", "teleport"],  # invalid
            default_blueprint=_make_minimal_blueprint(),
            default_scene=SceneDefinition(),
            default_policy_profile=_make_valid_policy_profile(),
            default_eval_profile=_make_valid_eval_profile(),
            default_result_view="research_report",
            recommended_inputs=[],
            icon="🚀",
        )


# ---------------------------------------------------------------------------
# T5.5 — metadata_only() / blueprint_summary() 结构
# ---------------------------------------------------------------------------


def test_metadata_only_no_blueprint(fresh_registry: KitRegistry) -> None:
    kit = _make_kit("meta_kit")
    fresh_registry.register(kit)
    meta = kit.metadata_only()
    # 不含 default_blueprint 等大对象
    assert "default_blueprint" not in meta
    assert "default_eval_profile" not in meta
    assert "default_policy_profile" not in meta
    # 含基本元数据
    assert meta["kit_id"] == "meta_kit"
    assert meta["display_name"] == "Test Kit — meta_kit"
    assert meta["category"] == "research"
    assert "icon" in meta
    assert "recommended_inputs" in meta


def test_blueprint_summary_contains_summary(fresh_registry: KitRegistry) -> None:
    kit = _make_kit("summary_kit")
    fresh_registry.register(kit)
    summary = kit.blueprint_summary()
    assert "default_blueprint_summary" in summary
    bp_sum = summary["default_blueprint_summary"]
    assert bp_sum["name"] == "summary_kit"
    assert "role_count" in bp_sum
    # 也应含 eval + policy（用于 GET /builder/kits/{kit_id}）
    assert "default_eval_profile" in summary
    assert "default_policy_profile" in summary


# ---------------------------------------------------------------------------
# T5.6 — discover_and_register_kits() （集成检查）
# ---------------------------------------------------------------------------


def test_discover_and_register_kits_global_registry() -> None:
    """discover_and_register_kits() 调用后 REGISTRY 中至少有 3 个 Kit。

    3 个最小集：research_kit / knowledge_assistant_kit / review_approval_kit
    persona_npc_kit 若已实现则为 4 个。
    """
    # 重置不影响全局 REGISTRY — discover 在全局单例上操作
    discover_and_register_kits()
    all_kits = _GLOBAL_REGISTRY.list_kits()
    kit_ids = [k.kit_id for k in all_kits]
    # 基本三件套（10.1/10.2/10.3）必须存在
    assert "research_kit" in kit_ids, f"research_kit missing from {kit_ids}"
    assert "knowledge_assistant_kit" in kit_ids, f"knowledge_assistant_kit missing from {kit_ids}"
    assert "review_approval_kit" in kit_ids, f"review_approval_kit missing from {kit_ids}"


def test_global_registry_research_kit_complete() -> None:
    """全局 REGISTRY 中的 research_kit 具备完整的三件套。"""
    discover_and_register_kits()
    kit = _GLOBAL_REGISTRY.get("research_kit")
    assert kit is not None
    assert kit.default_blueprint is not None
    assert kit.default_policy_profile is not None
    assert kit.default_eval_profile is not None
    # eval profile 必须有意义
    ep = kit.default_eval_profile
    assert ep.smoke_eval_enabled or ep.eval_criteria or ep.regression_gate
