"""tests/test_builder_contracts.py — Builder 合同字段与跨字段校验 (AC5, Story 8.3b)"""
from __future__ import annotations

import pytest

from xueban.runtime.contracts_builder import (
    AgentBlueprint,
    EvalProfile,
    HandoffRule,
    KnowledgeBinding,
    MemoryProfile,
    PublishProfile,
    RoleProfile,
    StateField,
    ToolPolicy,
)


# ---------------------------------------------------------------------------
# RoleProfile — 层级结构
# ---------------------------------------------------------------------------


def test_role_profile_defaults():
    role = RoleProfile(name="analyst", executor_provider="anthropic")
    assert role.can_spawn_tasks is False
    assert role.sub_agents == []


def test_role_profile_can_spawn_inferred_from_sub_agents():
    child = RoleProfile(name="worker", executor_provider="anthropic")
    manager = RoleProfile(name="manager", executor_provider="anthropic", sub_agents=[child])
    assert manager.can_spawn_tasks is True


def test_role_profile_explicit_can_spawn():
    role = RoleProfile(name="supervisor", executor_provider="anthropic", can_spawn_tasks=True)
    assert role.can_spawn_tasks is True


# ---------------------------------------------------------------------------
# ToolPolicy
# ---------------------------------------------------------------------------


def test_tool_policy_defaults():
    tp = ToolPolicy(tool_id="search")
    assert tp.trust_level == "external"
    assert tp.side_effects == "read_only"
    assert tp.requires_confirmation is False


# ---------------------------------------------------------------------------
# KnowledgeBinding
# ---------------------------------------------------------------------------


def test_knowledge_binding_defaults():
    kb = KnowledgeBinding()
    assert kb.source_type == "unspecified"
    assert kb.citation_required is False


# ---------------------------------------------------------------------------
# PublishProfile — visibility / target 组合校验
# ---------------------------------------------------------------------------


def test_publish_profile_public_visibility_without_target_raises():
    with pytest.raises(ValueError, match="non-none target"):
        PublishProfile(target="none", visibility="public")


def test_publish_profile_valid_combinations():
    p1 = PublishProfile(target="template", visibility="team")
    assert p1.target == "template"

    p2 = PublishProfile(target="none", visibility="private")
    assert p2.target == "none"


# ---------------------------------------------------------------------------
# AgentBlueprint — 顶层校验
# ---------------------------------------------------------------------------


def test_blueprint_missing_role_in_single_mode_raises():
    with pytest.raises(ValueError, match="role_profile"):
        AgentBlueprint(name="test", goal="do something", mode="single", role_profiles=[])


def test_blueprint_single_mode_minimum_valid():
    bp = AgentBlueprint(
        name="test",
        goal="do something",
        mode="single",
        role_profiles=[RoleProfile(name="agent", executor_provider="anthropic")],
    )
    assert bp.mode == "single"
    assert len(bp.role_profiles) == 1


def test_blueprint_team_mode_no_roles_ok():
    bp = AgentBlueprint(
        name="team",
        goal="work together",
        mode="team",
        role_profiles=[
            RoleProfile(name="a", executor_provider="anthropic"),
            RoleProfile(name="b", executor_provider="anthropic"),
        ],
    )
    assert bp.mode == "team"


def test_blueprint_citation_required_without_source_type_raises():
    with pytest.raises(ValueError, match="citation_required"):
        AgentBlueprint(
            name="test",
            goal="g",
            mode="single",
            role_profiles=[RoleProfile(name="a", executor_provider="anthropic")],
            knowledge_bindings=[KnowledgeBinding(citation_required=True, source_type="unspecified")],
        )


def test_blueprint_citation_required_with_source_type_ok():
    bp = AgentBlueprint(
        name="test",
        goal="g",
        mode="single",
        role_profiles=[RoleProfile(name="a", executor_provider="anthropic")],
        knowledge_bindings=[KnowledgeBinding(citation_required=True, source_type="url", source_ref="https://example.com")],
    )
    assert bp.knowledge_bindings[0].citation_required is True


def test_blueprint_auto_fields():
    bp = AgentBlueprint(
        name="x",
        goal="y",
        role_profiles=[RoleProfile(name="r", executor_provider="anthropic")],
    )
    assert bp.blueprint_id.startswith("bp-")
    assert bp.version == "1.0"
    assert isinstance(bp.memory_profile, MemoryProfile)
    assert isinstance(bp.eval_profile, EvalProfile)
    assert isinstance(bp.publish_profile, PublishProfile)


# ---------------------------------------------------------------------------
# Story 8.3b — 新增字段 model_validate 校验
# ---------------------------------------------------------------------------


def test_role_profile_new_fields_defaults():
    """新增字段有正确默认值，向后兼容。"""
    role = RoleProfile(name="agent", executor_provider="anthropic")
    assert role.capabilities == []
    assert role.handoff_rules == []
    assert role.persona_traits == {}
    assert role.state_fields == []


def test_role_profile_capabilities_roundtrip():
    """capabilities 字符串列表可通过 model_validate。"""
    data = {
        "name": "analyst",
        "executor_provider": "anthropic",
        "capabilities": ["撰写报告", "数据分析"],
    }
    role = RoleProfile.model_validate(data)
    assert role.capabilities == ["撰写报告", "数据分析"]


def test_role_profile_handoff_rules_roundtrip():
    """handoff_rules 结构体列表可通过 model_validate。"""
    data = {
        "name": "manager",
        "executor_provider": "anthropic",
        "handoff_rules": [
            {"trigger": "需要代码执行时", "target_role": "role-abc"},
        ],
    }
    role = RoleProfile.model_validate(data)
    assert len(role.handoff_rules) == 1
    assert isinstance(role.handoff_rules[0], HandoffRule)
    assert role.handoff_rules[0].trigger == "需要代码执行时"
    assert role.handoff_rules[0].target_role == "role-abc"


def test_role_profile_persona_traits_roundtrip():
    """persona_traits 字典可通过 model_validate。"""
    data = {
        "name": "npc",
        "executor_provider": "anthropic",
        "persona_traits": {"tone": "friendly", "language": "zh"},
    }
    role = RoleProfile.model_validate(data)
    assert role.persona_traits == {"tone": "friendly", "language": "zh"}


def test_role_profile_state_fields_roundtrip():
    """state_fields 列表可通过 model_validate，boolean default 类型正确。"""
    data = {
        "name": "persona",
        "executor_provider": "anthropic",
        "state_fields": [
            {"name": "friendship_level", "type": "number", "default": 0},
            {"name": "is_active", "type": "boolean", "default": False},
            {"name": "notes", "type": "string", "default": ""},
        ],
    }
    role = RoleProfile.model_validate(data)
    assert len(role.state_fields) == 3
    assert isinstance(role.state_fields[0], StateField)
    assert role.state_fields[0].name == "friendship_level"
    assert role.state_fields[1].type == "boolean"
    assert role.state_fields[1].default is False


def test_role_profile_full_deep_config_model_validate():
    """完整深度配置字段组合可通过 model_validate（Story 8.3b AC6）。"""
    data = {
        "name": "Persona NPC",
        "executor_provider": "anthropic",
        "capabilities": ["角色扮演", "情感反馈"],
        "handoff_rules": [{"trigger": "剧情结束", "target_role": "role-narrator"}],
        "persona_traits": {"tone": "warm", "style": "narrative"},
        "state_fields": [
            {"name": "friendship_level", "type": "number", "default": 0},
            {"name": "is_active", "type": "boolean", "default": True},
        ],
    }
    role = RoleProfile.model_validate(data)
    assert role.capabilities == ["角色扮演", "情感反馈"]
    assert role.persona_traits["style"] == "narrative"
    assert role.state_fields[1].default is True


def test_blueprint_with_deep_config_role():
    """含深度配置字段的 blueprint 整体可通过 model_validate。"""
    bp = AgentBlueprint.model_validate(
        {
            "name": "NPC Team",
            "goal": "沉浸式叙事",
            "mode": "team",
            "role_profiles": [
                {
                    "name": "Persona NPC",
                    "executor_provider": "anthropic",
                    "capabilities": ["角色扮演"],
                    "persona_traits": {"tone": "warm"},
                    "state_fields": [{"name": "mood", "type": "string", "default": "happy"}],
                }
            ],
        }
    )
    role = bp.role_profiles[0]
    assert role.capabilities == ["角色扮演"]
    assert role.state_fields[0].name == "mood"