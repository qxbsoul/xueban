"""Tests for Persona / NPC Kit — Story 10.4 (AC5, AC6)

覆盖项：
  1. 3 种 memory_retention 预设值映射
  2. State Fields 初始化验证
  3. enable_relationships=False 时 Blueprint 不含 RelationshipHooks
  4. memory_retention 无效值时校验失败
  5. PERSONA_NPC_KIT_DEFINITION 完整注册合同验证
  6. REGISTRY 中 persona_npc_kit 可查到
  7. _extract_persona_traits 关键词提取
  8. persona_traits 从 personality 提取一致
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from xueban.runtime.kits.persona_npc_kit import (
    MEMORY_RETENTION_PRESETS,
    PERSONA_NPC_KIT_DEFINITION,
    PERSONA_NPC_SMOKE_CASES,
    PersonaNPCGoalInputs,
    RelationshipHook,
    _extract_persona_traits,
    create_persona_npc_blueprint,
)
from xueban.runtime.kits.registry import REGISTRY


# ---------------------------------------------------------------------------
# 1. memory_retention 预设值映射
# ---------------------------------------------------------------------------


class TestMemoryRetentionPresets:
    """验证三档 memory_retention 映射到正确的 MemoryProfile 预设值。"""

    def test_minimal_preset_values(self):
        preset = MEMORY_RETENTION_PRESETS["minimal"]
        assert preset["working_memory_limit"] == 4
        assert preset["episodic_retention_days"] == 7
        assert preset["semantic_retrieval_top_k"] == 3
        assert preset["writeback_policy"] == "key_events_only"

    def test_balanced_preset_values(self):
        preset = MEMORY_RETENTION_PRESETS["balanced"]
        assert preset["working_memory_limit"] == 8
        assert preset["episodic_retention_days"] == 30
        assert preset["semantic_retrieval_top_k"] == 5
        assert preset["writeback_policy"] == "auto"

    def test_rich_preset_values(self):
        """AC6：rich → working_memory_limit=16。"""
        preset = MEMORY_RETENTION_PRESETS["rich"]
        assert preset["working_memory_limit"] == 16
        assert preset["episodic_retention_days"] == 365
        assert preset["semantic_retrieval_top_k"] == 10
        assert preset["writeback_policy"] == "all"

    def test_blueprint_minimal_memory_profile(self):
        inputs = PersonaNPCGoalInputs(
            persona_name="Test",
            personality="冷静、简洁",
            memory_retention="minimal",
        )
        bp = create_persona_npc_blueprint(inputs)
        meta = bp.memory_profile.metadata
        assert meta["working_memory_limit"] == 4
        assert meta["episodic_retention_days"] == 7
        assert meta["writeback_policy"] == "key_events_only"
        assert meta["memory_retention_preset"] == "minimal"

    def test_blueprint_rich_memory_profile(self):
        inputs = PersonaNPCGoalInputs(
            persona_name="Lore",
            personality="博学、记忆超群",
            memory_retention="rich",
        )
        bp = create_persona_npc_blueprint(inputs)
        meta = bp.memory_profile.metadata
        assert meta["working_memory_limit"] == 16
        assert meta["episodic_retention_days"] == 365
        assert meta["semantic_retrieval_top_k"] == 10
        assert meta["writeback_policy"] == "all"

    def test_blueprint_balanced_memory_profile(self):
        inputs = PersonaNPCGoalInputs(
            persona_name="Aria",
            personality="温柔、善解人意",
            memory_retention="balanced",
        )
        bp = create_persona_npc_blueprint(inputs)
        meta = bp.memory_profile.metadata
        assert meta["working_memory_limit"] == 8
        assert meta["writeback_policy"] == "auto"


# ---------------------------------------------------------------------------
# 2. State Fields 初始化验证
# ---------------------------------------------------------------------------


class TestStateFieldsInitialization:
    """验证 Blueprint 包含正确初始化的 State Fields。"""

    def _get_state_fields_dict(self, inputs: PersonaNPCGoalInputs) -> dict:
        bp = create_persona_npc_blueprint(inputs)
        role = bp.role_profiles[0]
        return {sf.name: sf.default for sf in role.state_fields}

    def test_state_fields_count(self):
        inputs = PersonaNPCGoalInputs(persona_name="Kai", personality="活泼")
        bp = create_persona_npc_blueprint(inputs)
        role = bp.role_profiles[0]
        field_names = {sf.name for sf in role.state_fields}
        assert {"mood", "relationship_level", "interaction_count", "last_seen"} == field_names

    def test_mood_default_neutral(self):
        inputs = PersonaNPCGoalInputs(persona_name="Kai", personality="活泼")
        sf_dict = self._get_state_fields_dict(inputs)
        assert sf_dict["mood"] == "neutral"

    def test_relationship_level_default_zero(self):
        inputs = PersonaNPCGoalInputs(persona_name="Kai", personality="活泼")
        sf_dict = self._get_state_fields_dict(inputs)
        assert sf_dict["relationship_level"] == 0

    def test_interaction_count_default_zero(self):
        inputs = PersonaNPCGoalInputs(persona_name="Kai", personality="活泼")
        sf_dict = self._get_state_fields_dict(inputs)
        assert sf_dict["interaction_count"] == 0

    def test_last_seen_is_iso_string(self):
        inputs = PersonaNPCGoalInputs(persona_name="Kai", personality="活泼")
        sf_dict = self._get_state_fields_dict(inputs)
        # last_seen 应是 ISO 8601 格式字符串
        last_seen = sf_dict["last_seen"]
        assert isinstance(last_seen, str)
        assert "T" in last_seen  # ISO 8601 格式

    def test_state_fields_in_blueprint_metadata(self):
        inputs = PersonaNPCGoalInputs(persona_name="Kai", personality="活泼")
        bp = create_persona_npc_blueprint(inputs)
        initial = bp.metadata["initial_state_fields"]
        assert initial["mood"] == "neutral"
        assert initial["relationship_level"] == 0
        assert initial["interaction_count"] == 0
        assert isinstance(initial["last_seen"], str)

    def test_state_field_types(self):
        inputs = PersonaNPCGoalInputs(persona_name="Kai", personality="活泼")
        bp = create_persona_npc_blueprint(inputs)
        role = bp.role_profiles[0]
        type_map = {sf.name: sf.type for sf in role.state_fields}
        assert type_map["mood"] == "string"
        assert type_map["relationship_level"] == "number"
        assert type_map["interaction_count"] == "number"
        assert type_map["last_seen"] == "string"


# ---------------------------------------------------------------------------
# 3. RelationshipHooks 条件注入
# ---------------------------------------------------------------------------


class TestRelationshipHooks:
    """AC6：enable_relationships=False 时 Blueprint 不含 RelationshipHooks。"""

    def test_enable_relationships_true_includes_hooks(self):
        inputs = PersonaNPCGoalInputs(
            persona_name="Luna",
            personality="神秘、深邃",
            enable_relationships=True,
        )
        bp = create_persona_npc_blueprint(inputs)
        hooks = bp.metadata.get("relationship_hooks", [])
        assert len(hooks) >= 2

    def test_enable_relationships_false_no_hooks(self):
        """AC6 核心断言：enable_relationships=False 时 relationship_hooks 为空列表。"""
        inputs = PersonaNPCGoalInputs(
            persona_name="Ghost",
            personality="冷漠、独立",
            enable_relationships=False,
        )
        bp = create_persona_npc_blueprint(inputs)
        hooks = bp.metadata.get("relationship_hooks", [])
        assert hooks == []

    def test_hooks_contain_interaction_count_trigger(self):
        inputs = PersonaNPCGoalInputs(
            persona_name="Luna",
            personality="热情",
            enable_relationships=True,
        )
        bp = create_persona_npc_blueprint(inputs)
        hooks = bp.metadata["relationship_hooks"]
        trigger_types = [h["trigger_type"] for h in hooks]
        assert "interaction_count_reaches" in trigger_types

    def test_hooks_contain_mentioned_name_trigger(self):
        inputs = PersonaNPCGoalInputs(
            persona_name="Luna",
            personality="热情",
            enable_relationships=True,
        )
        bp = create_persona_npc_blueprint(inputs)
        hooks = bp.metadata["relationship_hooks"]
        trigger_types = [h["trigger_type"] for h in hooks]
        assert "mentioned_name" in trigger_types

    def test_hooks_interaction_threshold_is_10(self):
        inputs = PersonaNPCGoalInputs(
            persona_name="Luna",
            personality="热情",
            enable_relationships=True,
        )
        bp = create_persona_npc_blueprint(inputs)
        hooks = bp.metadata["relationship_hooks"]
        count_hook = next(
            (h for h in hooks if h["trigger_type"] == "interaction_count_reaches"), None
        )
        assert count_hook is not None
        assert count_hook["threshold"] == 10

    def test_relationship_hook_model_valid(self):
        hook = RelationshipHook(
            hook_id="test_hook",
            trigger_type="interaction_count_reaches",
            threshold=10,
            effect="relationship_level += 5",
        )
        assert hook.hook_id == "test_hook"
        assert hook.threshold == 10


# ---------------------------------------------------------------------------
# 4. memory_retention 无效值校验
# ---------------------------------------------------------------------------


class TestInputValidation:
    """AC6：无效 memory_retention 触发 ValidationError。"""

    def test_invalid_memory_retention_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            PersonaNPCGoalInputs(
                persona_name="Test",
                personality="随机",
                memory_retention="ultra",  # 无效值
            )
        errors = exc_info.value.errors()
        assert any("memory_retention" in str(e) for e in errors)

    def test_valid_memory_retention_minimal(self):
        inputs = PersonaNPCGoalInputs(
            persona_name="Test",
            personality="随机",
            memory_retention="minimal",
        )
        assert inputs.memory_retention == "minimal"

    def test_valid_memory_retention_balanced_default(self):
        inputs = PersonaNPCGoalInputs(
            persona_name="Test",
            personality="随机",
        )
        assert inputs.memory_retention == "balanced"

    def test_persona_name_required(self):
        with pytest.raises(ValidationError):
            PersonaNPCGoalInputs(persona_name="", personality="随机")

    def test_personality_required(self):
        with pytest.raises(ValidationError):
            PersonaNPCGoalInputs(persona_name="Test", personality="")

    def test_enable_relationships_default_true(self):
        inputs = PersonaNPCGoalInputs(persona_name="Test", personality="随机")
        assert inputs.enable_relationships is True

    def test_backstory_optional_empty_by_default(self):
        inputs = PersonaNPCGoalInputs(persona_name="Test", personality="随机")
        assert inputs.backstory == ""


# ---------------------------------------------------------------------------
# 5. create_persona_npc_blueprint 完整性
# ---------------------------------------------------------------------------


class TestCreatePersonaNPCBlueprint:
    """验证 Blueprint 结构完整性（AC2）。"""

    def setup_method(self):
        self.inputs = PersonaNPCGoalInputs(
            persona_name="Aria",
            personality="温柔、善解人意、略带神秘感",
            backstory="一个旅者，以倾听为使命。",
            memory_retention="balanced",
            enable_relationships=True,
        )
        self.bp = create_persona_npc_blueprint(self.inputs)

    def test_blueprint_name_equals_persona_name(self):
        assert self.bp.name == "Aria"

    def test_blueprint_mode_single(self):
        assert self.bp.mode == "single"

    def test_role_profile_count_is_one(self):
        assert len(self.bp.role_profiles) == 1

    def test_role_title_is_persona_name(self):
        role = self.bp.role_profiles[0]
        assert role.name == "Aria"

    def test_role_persona_contains_personality(self):
        role = self.bp.role_profiles[0]
        assert "温柔" in role.persona or "善解人意" in role.persona

    def test_role_persona_contains_backstory(self):
        role = self.bp.role_profiles[0]
        assert "旅者" in role.persona

    def test_persona_traits_extracted(self):
        role = self.bp.role_profiles[0]
        assert len(role.persona_traits) >= 1
        trait_vals = list(role.persona_traits.values())
        # 至少一个特征词来自 personality
        assert any(t in "温柔善解人意略带神秘感" for t in trait_vals)

    def test_memory_profile_scope_user(self):
        assert self.bp.memory_profile.scope == "user"

    def test_memory_profile_writeback_target(self):
        assert self.bp.memory_profile.writeback_target == "memory"

    def test_eval_profile_smoke_enabled(self):
        assert self.bp.eval_profile.smoke_eval_enabled is True

    def test_eval_profile_criteria_count(self):
        assert len(self.bp.eval_profile.eval_criteria) == 3

    def test_metadata_kit_id(self):
        assert self.bp.metadata["kit_id"] == "persona_npc_kit"


# ---------------------------------------------------------------------------
# 6. _extract_persona_traits 关键词提取
# ---------------------------------------------------------------------------


class TestExtractPersonaTraits:
    """验证 persona_traits 提取逻辑。"""

    def test_chinese_comma_split(self):
        traits = _extract_persona_traits("温柔、善解人意、略带神秘感")
        assert "温柔" in traits.values()
        assert "善解人意" in traits.values()

    def test_english_comma_split(self):
        traits = _extract_persona_traits("calm, rational, humorous")
        assert "calm" in traits.values()

    def test_max_10_traits(self):
        long = "、".join([f"trait{i}" for i in range(20)])
        traits = _extract_persona_traits(long)
        assert len(traits) <= 10

    def test_empty_returns_empty(self):
        traits = _extract_persona_traits("")
        assert traits == {}

    def test_single_trait(self):
        traits = _extract_persona_traits("勇敢")
        assert traits == {"trait_1": "勇敢"}


# ---------------------------------------------------------------------------
# 7. PERSONA_NPC_KIT_DEFINITION 注册合同验证
# ---------------------------------------------------------------------------


class TestPersonaNPCKitDefinition:
    """AC1, AC5：验证 KitDefinition 正确注册。"""

    def test_kit_id(self):
        assert PERSONA_NPC_KIT_DEFINITION.kit_id == "persona_npc_kit"

    def test_display_name_contains_persona(self):
        assert "Persona" in PERSONA_NPC_KIT_DEFINITION.display_name
        assert "NPC" in PERSONA_NPC_KIT_DEFINITION.display_name

    def test_supported_modes(self):
        assert "goal" in PERSONA_NPC_KIT_DEFINITION.supported_modes
        assert "scene" in PERSONA_NPC_KIT_DEFINITION.supported_modes

    def test_default_result_view(self):
        assert PERSONA_NPC_KIT_DEFINITION.default_result_view == "agent_dm_with_state"

    def test_category_persona(self):
        assert PERSONA_NPC_KIT_DEFINITION.category == "persona"

    def test_recommended_inputs_all_five(self):
        inputs = PERSONA_NPC_KIT_DEFINITION.recommended_inputs
        expected = {"persona_name", "personality", "backstory", "memory_retention", "enable_relationships"}
        assert expected == set(inputs)

    def test_icon_not_empty(self):
        assert PERSONA_NPC_KIT_DEFINITION.icon  # 非空

    def test_description_not_empty(self):
        assert len(PERSONA_NPC_KIT_DEFINITION.description) > 10

    def test_default_blueprint_has_role(self):
        assert len(PERSONA_NPC_KIT_DEFINITION.default_blueprint.role_profiles) >= 1

    def test_default_eval_profile_smoke_enabled(self):
        assert PERSONA_NPC_KIT_DEFINITION.default_eval_profile.smoke_eval_enabled is True


# ---------------------------------------------------------------------------
# 8. REGISTRY 注册验证
# ---------------------------------------------------------------------------


class TestRegistryIntegration:
    """AC5：REGISTRY 中 persona_npc_kit 可查到。"""

    def test_persona_npc_kit_in_registry(self):
        kit = REGISTRY.get("persona_npc_kit")
        assert kit is not None
        assert kit.kit_id == "persona_npc_kit"

    def test_list_kits_contains_persona_npc(self):
        kit_ids = [k.kit_id for k in REGISTRY.list_kits()]
        assert "persona_npc_kit" in kit_ids

    def test_list_kits_by_category_persona(self):
        persona_kits = REGISTRY.list_kits(category="persona")
        kit_ids = [k.kit_id for k in persona_kits]
        assert "persona_npc_kit" in kit_ids


# ---------------------------------------------------------------------------
# 9. PERSONA_NPC_SMOKE_CASES 结构验证
# ---------------------------------------------------------------------------


class TestSmokesCasesStructure:
    """验证 Smoke Cases 结构符合 3 轮对话规范。"""

    def test_three_turns(self):
        assert len(PERSONA_NPC_SMOKE_CASES) == 3

    def test_turns_sequential(self):
        turns = [c["turn"] for c in PERSONA_NPC_SMOKE_CASES]
        assert turns == [1, 2, 3]

    def test_all_cases_have_required_fields(self):
        for case in PERSONA_NPC_SMOKE_CASES:
            assert "turn" in case
            assert "name" in case
            assert "input" in case
            assert "expected" in case
            assert "eval_check" in case

    def test_turn_3_has_assertions(self):
        turn_3 = PERSONA_NPC_SMOKE_CASES[2]
        assert "assertions" in turn_3
        assertions = turn_3["assertions"]
        field_names = [a["field"] for a in assertions]
        assert "interaction_count" in field_names

    def test_interaction_count_assertion_value(self):
        """AC6：Smoke Run 第 3 轮 interaction_count == 3（比第 1 轮大 2）。"""
        turn_3 = PERSONA_NPC_SMOKE_CASES[2]
        count_assertion = next(
            a for a in turn_3["assertions"] if a["field"] == "interaction_count"
        )
        assert count_assertion["op"] == "eq"
        assert count_assertion["value"] == 3
