"""tests/test_knowledge_assistant_kit.py — Story 10.2 (AC6)

覆盖：
  - KnowledgeAssistantGoalInputs 校验（5 字段）
  - 3 角色 Blueprint 结构验证（Retriever / Answerer / Escalation 都在 Blueprint 中）
  - 无知识包绑定时 citation_required=True 的 Blueprint 仍合法
  - 命中路径 Answerer 角色 citation_required=True
  - KNOWLEDGE_ASSISTANT_KIT_DEFINITION 完整注册验证
  - KNOWLEDGE_ASSISTANT_SMOKE_CASES 三路径结构验证
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from xueban.runtime.kits.knowledge_assistant_kit import (
    KNOWLEDGE_ASSISTANT_KIT_DEFINITION,
    KNOWLEDGE_ASSISTANT_SMOKE_CASES,
    KnowledgeAssistantGoalInputs,
    create_knowledge_assistant_blueprint,
)
from xueban.runtime.contracts_builder import AgentBlueprint, RoleProfile


# ---------------------------------------------------------------------------
# KnowledgeAssistantGoalInputs 字段校验
# ---------------------------------------------------------------------------


class TestKnowledgeAssistantGoalInputs:
    """KnowledgeAssistantGoalInputs 5 字段 + 校验逻辑。"""

    def test_default_values(self):
        """默认值：knowledge_source=none, citation_required=True, escalate_human。"""
        inputs = KnowledgeAssistantGoalInputs(assistant_name="Test Assistant")
        assert inputs.knowledge_source == "none"
        assert inputs.citation_required is True
        assert inputs.low_confidence_strategy == "escalate_human"
        assert inputs.escalation_keywords == []
        assert inputs.confidence_threshold == 0.5

    def test_all_fields_explicit(self):
        """所有 5 字段显式赋值正常构造。"""
        inputs = KnowledgeAssistantGoalInputs(
            knowledge_source="existing_pack",
            citation_required=False,
            low_confidence_strategy="reject_with_message",
            escalation_keywords=["法律", "合规", "投诉"],
            assistant_name="HR 知识助手",
            pack_id="pack-abc123",
        )
        assert inputs.knowledge_source == "existing_pack"
        assert inputs.citation_required is False
        assert inputs.low_confidence_strategy == "reject_with_message"
        assert len(inputs.escalation_keywords) == 3
        assert inputs.assistant_name == "HR 知识助手"
        assert inputs.pack_id == "pack-abc123"

    def test_invalid_knowledge_source(self):
        """无效 knowledge_source 值抛出 ValidationError。"""
        with pytest.raises(ValidationError) as exc_info:
            KnowledgeAssistantGoalInputs(
                knowledge_source="ftp",  # 无效值
                assistant_name="Test",
            )
        assert "knowledge_source" in str(exc_info.value).lower() or "ftp" in str(exc_info.value)

    def test_invalid_strategy(self):
        """无效 low_confidence_strategy 值抛出 ValidationError。"""
        with pytest.raises(ValidationError):
            KnowledgeAssistantGoalInputs(
                low_confidence_strategy="ignore",  # 无效值
                assistant_name="Test",
            )

    def test_all_valid_knowledge_sources(self):
        """所有 4 个有效 knowledge_source 值均通过校验。"""
        for source in ("upload", "url", "existing_pack", "none"):
            inputs = KnowledgeAssistantGoalInputs(
                knowledge_source=source,  # type: ignore[arg-type]
                assistant_name="Test",
            )
            assert inputs.knowledge_source == source

    def test_all_valid_strategies(self):
        """所有 3 个有效 low_confidence_strategy 值均通过校验。"""
        for strategy in ("escalate_human", "escalate_review", "reject_with_message"):
            inputs = KnowledgeAssistantGoalInputs(
                low_confidence_strategy=strategy,  # type: ignore[arg-type]
                assistant_name="Test",
            )
            assert inputs.low_confidence_strategy == strategy

    def test_confidence_threshold_range(self):
        """confidence_threshold 必须在 [0.0, 1.0] 范围内。"""
        # 有效边界值
        for threshold in (0.0, 0.5, 1.0):
            inputs = KnowledgeAssistantGoalInputs(
                assistant_name="Test",
                confidence_threshold=threshold,
            )
            assert inputs.confidence_threshold == threshold

        # 越界值
        with pytest.raises(ValidationError):
            KnowledgeAssistantGoalInputs(
                assistant_name="Test",
                confidence_threshold=1.5,
            )
        with pytest.raises(ValidationError):
            KnowledgeAssistantGoalInputs(
                assistant_name="Test",
                confidence_threshold=-0.1,
            )

    def test_assistant_name_required(self):
        """assistant_name 不能为空字符串（min_length=1）。"""
        with pytest.raises(ValidationError):
            KnowledgeAssistantGoalInputs(assistant_name="")

    def test_escalation_keywords_optional(self):
        """escalation_keywords 为空列表时合法。"""
        inputs = KnowledgeAssistantGoalInputs(
            assistant_name="Test",
            escalation_keywords=[],
        )
        assert inputs.escalation_keywords == []


# ---------------------------------------------------------------------------
# create_knowledge_assistant_blueprint — 3 角色 Blueprint 结构
# ---------------------------------------------------------------------------


class TestCreateKnowledgeAssistantBlueprint:
    """3 角色 Blueprint 结构验证。"""

    def _make_default_inputs(self, **overrides) -> KnowledgeAssistantGoalInputs:
        defaults = {
            "knowledge_source": "none",
            "citation_required": True,
            "low_confidence_strategy": "escalate_human",
            "escalation_keywords": [],
            "assistant_name": "Test Assistant",
        }
        defaults.update(overrides)
        return KnowledgeAssistantGoalInputs(**defaults)

    def test_returns_agent_blueprint(self):
        """create_knowledge_assistant_blueprint 返回 AgentBlueprint 实例。"""
        inputs = self._make_default_inputs()
        bp = create_knowledge_assistant_blueprint(inputs)
        assert isinstance(bp, AgentBlueprint)

    def test_has_three_role_profiles(self):
        """Blueprint 必须包含 3 个 RoleProfile：Retriever / Answerer / Escalation。"""
        inputs = self._make_default_inputs()
        bp = create_knowledge_assistant_blueprint(inputs)
        assert len(bp.role_profiles) == 3

    def test_retriever_role_present(self):
        """Retriever 角色存在且 role_id='retriever'。"""
        inputs = self._make_default_inputs()
        bp = create_knowledge_assistant_blueprint(inputs)
        role_ids = [r.role_id for r in bp.role_profiles]
        assert "retriever" in role_ids

    def test_answerer_role_present(self):
        """Answerer 角色存在且 role_id='answerer'。"""
        inputs = self._make_default_inputs()
        bp = create_knowledge_assistant_blueprint(inputs)
        role_ids = [r.role_id for r in bp.role_profiles]
        assert "answerer" in role_ids

    def test_escalation_role_present(self):
        """Escalation 角色存在且 role_id='escalation'。"""
        inputs = self._make_default_inputs()
        bp = create_knowledge_assistant_blueprint(inputs)
        role_ids = [r.role_id for r in bp.role_profiles]
        assert "escalation" in role_ids

    def test_answerer_citation_required_true(self):
        """命中路径 — Answerer 角色 citation_required=True 时 metadata 正确记录。"""
        inputs = self._make_default_inputs(citation_required=True)
        bp = create_knowledge_assistant_blueprint(inputs)
        answerer = next(r for r in bp.role_profiles if r.role_id == "answerer")
        assert answerer.metadata.get("citation_required") is True

    def test_answerer_citation_required_false(self):
        """citation_required=False 时 Answerer metadata 记录 False。"""
        inputs = self._make_default_inputs(citation_required=False)
        bp = create_knowledge_assistant_blueprint(inputs)
        answerer = next(r for r in bp.role_profiles if r.role_id == "answerer")
        assert answerer.metadata.get("citation_required") is False

    def test_escalation_role_has_approval_gate_tool(self):
        """Escalation 角色必须有 approval_gate 工具（复用 Epic 1 机制）。"""
        inputs = self._make_default_inputs()
        bp = create_knowledge_assistant_blueprint(inputs)
        escalation = next(r for r in bp.role_profiles if r.role_id == "escalation")
        assert "approval_gate" in escalation.tools

    def test_escalation_role_can_receive_approvals(self):
        """Escalation 角色 metadata 中 can_receive_approvals=True。"""
        inputs = self._make_default_inputs()
        bp = create_knowledge_assistant_blueprint(inputs)
        escalation = next(r for r in bp.role_profiles if r.role_id == "escalation")
        assert escalation.metadata.get("can_receive_approvals") is True

    def test_retriever_has_knowledge_retrieval_tool(self):
        """Retriever 角色必须有 knowledge_retrieval 工具。"""
        inputs = self._make_default_inputs()
        bp = create_knowledge_assistant_blueprint(inputs)
        retriever = next(r for r in bp.role_profiles if r.role_id == "retriever")
        assert "knowledge_retrieval" in retriever.tools

    def test_no_knowledge_binding_when_source_none(self):
        """knowledge_source='none' 时 Blueprint 不添加 KnowledgeBinding。"""
        inputs = self._make_default_inputs(knowledge_source="none")
        bp = create_knowledge_assistant_blueprint(inputs)
        assert len(bp.knowledge_bindings) == 0

    def test_no_knowledge_binding_citation_required_true_is_still_valid(self):
        """无知识包绑定时 citation_required=True 的 Blueprint 仍合法（AC6 AC1）。

        知识来源为空时退到拒答策略（Policy 层兜底），Blueprint 本身不报错。
        """
        inputs = self._make_default_inputs(
            knowledge_source="none",
            citation_required=True,  # 无来源但 citation_required=True
        )
        # 不应该抛出任何异常
        bp = create_knowledge_assistant_blueprint(inputs)
        assert isinstance(bp, AgentBlueprint)
        assert len(bp.knowledge_bindings) == 0

    def test_knowledge_binding_added_for_existing_pack(self):
        """knowledge_source='existing_pack' 且有 pack_id 时添加 KnowledgeBinding。"""
        inputs = self._make_default_inputs(
            knowledge_source="existing_pack",
            pack_id="pack-12345",
        )
        bp = create_knowledge_assistant_blueprint(inputs)
        assert len(bp.knowledge_bindings) == 1
        kb = bp.knowledge_bindings[0]
        assert kb.source_type == "pack"
        assert kb.source_ref == "pack-12345"
        assert kb.citation_required is True

    def test_eval_profile_smoke_enabled(self):
        """EvalProfile.smoke_eval_enabled=True。"""
        inputs = self._make_default_inputs()
        bp = create_knowledge_assistant_blueprint(inputs)
        assert bp.eval_profile.smoke_eval_enabled is True

    def test_eval_profile_has_three_criteria(self):
        """EvalProfile 包含 3 个检查项（doc_hit_rate / citation_attached_rate / escalation_triggered）。"""
        inputs = self._make_default_inputs()
        bp = create_knowledge_assistant_blueprint(inputs)
        criteria = bp.eval_profile.eval_criteria
        assert len(criteria) == 3
        criteria_str = " ".join(criteria).lower()
        assert "doc_hit_rate" in criteria_str
        assert "citation_attached_rate" in criteria_str
        assert "escalation_triggered" in criteria_str

    def test_blueprint_name_matches_assistant_name(self):
        """Blueprint.name 等于 goal_inputs.assistant_name。"""
        inputs = self._make_default_inputs(assistant_name="产品文档助手")
        bp = create_knowledge_assistant_blueprint(inputs)
        assert bp.name == "产品文档助手"

    def test_blueprint_mode_is_team(self):
        """Knowledge Assistant Blueprint mode='team'（3 角色协作）。"""
        inputs = self._make_default_inputs()
        bp = create_knowledge_assistant_blueprint(inputs)
        assert bp.mode == "team"

    def test_policy_metadata_present(self):
        """Blueprint metadata 中包含 policy_rules 字典。"""
        inputs = self._make_default_inputs()
        bp = create_knowledge_assistant_blueprint(inputs)
        assert "policy_rules" in bp.metadata
        assert "no_source_rule" in bp.metadata["policy_rules"]
        assert "low_confidence_rule" in bp.metadata["policy_rules"]

    def test_escalation_keywords_in_policy_metadata(self):
        """escalation_keywords 在 blueprint metadata 中正确传递。"""
        inputs = self._make_default_inputs(escalation_keywords=["法律", "合规"])
        bp = create_knowledge_assistant_blueprint(inputs)
        assert bp.metadata.get("escalation_keywords") == ["法律", "合规"]


# ---------------------------------------------------------------------------
# KNOWLEDGE_ASSISTANT_KIT_DEFINITION — 注册合同完整性
# ---------------------------------------------------------------------------


class TestKnowledgeAssistantKitDefinition:
    """KNOWLEDGE_ASSISTANT_KIT_DEFINITION 完整注册验证。"""

    def test_kit_id(self):
        assert KNOWLEDGE_ASSISTANT_KIT_DEFINITION.kit_id == "knowledge_assistant_kit"

    def test_display_name(self):
        assert "Knowledge Assistant Kit" in KNOWLEDGE_ASSISTANT_KIT_DEFINITION.display_name

    def test_category(self):
        assert KNOWLEDGE_ASSISTANT_KIT_DEFINITION.category == "knowledge"

    def test_supported_modes_contains_goal_and_scene(self):
        modes = KNOWLEDGE_ASSISTANT_KIT_DEFINITION.supported_modes
        assert "goal" in modes
        assert "scene" in modes

    def test_default_blueprint_is_agent_blueprint(self):
        bp = KNOWLEDGE_ASSISTANT_KIT_DEFINITION.default_blueprint
        assert isinstance(bp, AgentBlueprint)

    def test_default_blueprint_has_three_roles(self):
        bp = KNOWLEDGE_ASSISTANT_KIT_DEFINITION.default_blueprint
        assert len(bp.role_profiles) == 3

    def test_eval_profile_non_empty(self):
        ep = KNOWLEDGE_ASSISTANT_KIT_DEFINITION.default_eval_profile
        assert ep.smoke_eval_enabled is True
        assert len(ep.eval_criteria) > 0

    def test_default_scene_has_three_roles(self):
        scene = KNOWLEDGE_ASSISTANT_KIT_DEFINITION.default_scene
        assert len(scene.root_roles) == 3
        role_names = [r.role_name for r in scene.root_roles]
        assert "Retriever" in role_names
        assert "Answerer" in role_names
        assert "Escalation" in role_names

    def test_icon_is_set(self):
        assert KNOWLEDGE_ASSISTANT_KIT_DEFINITION.icon == "📚"

    def test_recommended_inputs(self):
        inputs = KNOWLEDGE_ASSISTANT_KIT_DEFINITION.recommended_inputs
        assert "knowledge_source" in inputs
        assert "citation_required" in inputs
        assert "low_confidence_strategy" in inputs
        assert "escalation_keywords" in inputs
        assert "assistant_name" in inputs


# ---------------------------------------------------------------------------
# KNOWLEDGE_ASSISTANT_SMOKE_CASES — 三路径
# ---------------------------------------------------------------------------


class TestKnowledgeAssistantSmokeCases:
    """Smoke Run 三路径 case 结构验证。"""

    def test_has_three_cases(self):
        assert len(KNOWLEDGE_ASSISTANT_SMOKE_CASES) == 3

    def test_hit_path_exists(self):
        names = [c["name"] for c in KNOWLEDGE_ASSISTANT_SMOKE_CASES]
        assert "hit_path" in names

    def test_reject_path_exists(self):
        names = [c["name"] for c in KNOWLEDGE_ASSISTANT_SMOKE_CASES]
        assert "reject_path" in names

    def test_escalate_path_exists(self):
        names = [c["name"] for c in KNOWLEDGE_ASSISTANT_SMOKE_CASES]
        assert "escalate_path" in names

    def test_hit_path_expected_is_citation_trace(self):
        hit = next(c for c in KNOWLEDGE_ASSISTANT_SMOKE_CASES if c["name"] == "hit_path")
        assert "citation_trace" in hit["expected"]

    def test_reject_path_expected_is_no_source(self):
        reject = next(c for c in KNOWLEDGE_ASSISTANT_SMOKE_CASES if c["name"] == "reject_path")
        assert "no_source_response" in reject["expected"]

    def test_escalate_path_expected_is_human_handoff(self):
        escalate = next(c for c in KNOWLEDGE_ASSISTANT_SMOKE_CASES if c["name"] == "escalate_path")
        assert "human_handoff_event" in escalate["expected"]

    def test_each_case_has_mock_context(self):
        for case in KNOWLEDGE_ASSISTANT_SMOKE_CASES:
            assert "mock_context" in case
            assert "hit_count" in case["mock_context"]
            assert "confidence" in case["mock_context"]

    def test_reject_path_hit_count_is_zero(self):
        reject = next(c for c in KNOWLEDGE_ASSISTANT_SMOKE_CASES if c["name"] == "reject_path")
        assert reject["mock_context"]["hit_count"] == 0

    def test_escalate_path_confidence_below_threshold(self):
        escalate = next(c for c in KNOWLEDGE_ASSISTANT_SMOKE_CASES if c["name"] == "escalate_path")
        assert escalate["mock_context"]["confidence"] < 0.5

    def test_hit_path_confidence_above_threshold(self):
        hit = next(c for c in KNOWLEDGE_ASSISTANT_SMOKE_CASES if c["name"] == "hit_path")
        assert hit["mock_context"]["confidence"] >= 0.5
