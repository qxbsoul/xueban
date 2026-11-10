"""tests/test_knowledge_assistant_policy.py — Story 10.2 (AC6)

覆盖 3 路径 Policy 规则触发（命中 / 拒答 / 升级）：
  - hit_count=0 时 policy 禁止 Answerer 发言（no_source_response 路径）
  - confidence < 0.5 时触发 human_handoff_event（escalation 路径）
  - 命中路径 Blueprint 中 Answerer citation_required=True

说明：
  Policy 规则在 MVP 中以 Blueprint.metadata.policy_rules 字典形式声明，
  运行时执行引擎（Epic 1 / Epic 8）在实际推理时解析这些规则。
  本测试文件验证 Blueprint 的 Policy 声明正确，保证规则完整性（静态合同测试）。
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from xueban.runtime.kits.knowledge_assistant_kit import (
    KnowledgeAssistantGoalInputs,
    create_knowledge_assistant_blueprint,
)
from xueban.runtime.contracts_builder import AgentBlueprint


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_bp(
    knowledge_source: str = "none",
    citation_required: bool = True,
    low_confidence_strategy: str = "escalate_human",
    escalation_keywords: List[str] | None = None,
    assistant_name: str = "Test Assistant",
    confidence_threshold: float = 0.5,
) -> AgentBlueprint:
    """创建用于 Policy 测试的 Blueprint 辅助函数。"""
    inputs = KnowledgeAssistantGoalInputs(
        knowledge_source=knowledge_source,  # type: ignore[arg-type]
        citation_required=citation_required,
        low_confidence_strategy=low_confidence_strategy,  # type: ignore[arg-type]
        escalation_keywords=escalation_keywords or [],
        assistant_name=assistant_name,
        confidence_threshold=confidence_threshold,
    )
    return create_knowledge_assistant_blueprint(inputs)


def _get_policy_rules(bp: AgentBlueprint) -> Dict[str, Any]:
    """从 Blueprint.metadata 中提取 policy_rules 字典。"""
    return bp.metadata.get("policy_rules", {})


def _get_role(bp: AgentBlueprint, role_id: str):
    """从 Blueprint 中按 role_id 查找 RoleProfile。"""
    return next((r for r in bp.role_profiles if r.role_id == role_id), None)


# ---------------------------------------------------------------------------
# 拒答路径 — hit_count=0 时 Answerer 禁止发言
# ---------------------------------------------------------------------------


class TestNoSourcePolicy:
    """hit_count=0 时 Answerer 必须被 Policy 禁止发言，返回 no_source_response。"""

    def test_no_source_rule_exists_in_metadata(self):
        """Blueprint.metadata.policy_rules 中存在 no_source_rule。"""
        bp = _build_bp()
        rules = _get_policy_rules(bp)
        assert "no_source_rule" in rules, (
            f"no_source_rule 未在 policy_rules 中声明，实际 keys={list(rules.keys())}"
        )

    def test_no_source_rule_condition(self):
        """no_source_rule.condition 必须包含 hit_count 逻辑。"""
        bp = _build_bp()
        rules = _get_policy_rules(bp)
        rule = rules["no_source_rule"]
        condition = rule.get("condition", "")
        assert "hit_count" in condition, (
            f"no_source_rule.condition 未包含 hit_count，实际={condition!r}"
        )

    def test_no_source_rule_action_is_reject(self):
        """no_source_rule.action 必须是 'reject'（禁止 Answerer 发言）。"""
        bp = _build_bp()
        rules = _get_policy_rules(bp)
        rule = rules["no_source_rule"]
        assert rule.get("action") == "reject", (
            f"no_source_rule.action 应为 'reject'，实际={rule.get('action')!r}"
        )

    def test_no_source_rule_response_template(self):
        """no_source_rule 声明返回 no_source_response 模板。"""
        bp = _build_bp()
        rules = _get_policy_rules(bp)
        rule = rules["no_source_rule"]
        assert rule.get("response_template") == "no_source_response", (
            f"no_source_rule.response_template 应为 'no_source_response'，实际={rule.get('response_template')!r}"
        )

    def test_no_source_rule_targets_answerer(self):
        """no_source_rule 作用目标是 Answerer 角色。"""
        bp = _build_bp()
        rules = _get_policy_rules(bp)
        rule = rules["no_source_rule"]
        assert rule.get("target_role") == "answerer", (
            f"no_source_rule.target_role 应为 'answerer'，实际={rule.get('target_role')!r}"
        )

    def test_answerer_constraints_mention_no_fabrication(self):
        """Answerer.constraints 中声明禁止编造。"""
        bp = _build_bp()
        answerer = _get_role(bp, "answerer")
        assert answerer is not None
        constraints_str = " ".join(answerer.constraints).lower()
        assert "禁止" in constraints_str or "no_source_response" in constraints_str, (
            f"Answerer.constraints 未声明禁止编造：{answerer.constraints}"
        )

    def test_retriever_failure_rule_exists(self):
        """Retriever 故障时有 retriever_failure_rule（不崩溃整个助手）。"""
        bp = _build_bp()
        rules = _get_policy_rules(bp)
        assert "retriever_failure_rule" in rules, (
            f"retriever_failure_rule 未在 policy_rules 中声明，实际 keys={list(rules.keys())}"
        )


# ---------------------------------------------------------------------------
# 升级路径 — confidence < 0.5 时触发 human_handoff_event
# ---------------------------------------------------------------------------


class TestLowConfidenceEscalationPolicy:
    """confidence < threshold 时触发 Escalation，产出 human_handoff_event。"""

    def test_low_confidence_rule_exists(self):
        """Blueprint.metadata.policy_rules 中存在 low_confidence_rule。"""
        bp = _build_bp()
        rules = _get_policy_rules(bp)
        assert "low_confidence_rule" in rules, (
            f"low_confidence_rule 未在 policy_rules 中声明，实际 keys={list(rules.keys())}"
        )

    def test_low_confidence_rule_condition_contains_threshold(self):
        """low_confidence_rule.condition 包含置信度阈值比较。"""
        bp = _build_bp(confidence_threshold=0.5)
        rules = _get_policy_rules(bp)
        rule = rules["low_confidence_rule"]
        condition = rule.get("condition", "")
        assert "confidence" in condition, (
            f"low_confidence_rule.condition 未包含 confidence，实际={condition!r}"
        )
        assert "0.5" in condition, (
            f"low_confidence_rule.condition 未包含阈值 0.5，实际={condition!r}"
        )

    def test_low_confidence_rule_action_is_escalate(self):
        """low_confidence_rule.action 必须是 'escalate'。"""
        bp = _build_bp()
        rules = _get_policy_rules(bp)
        rule = rules["low_confidence_rule"]
        assert rule.get("action") == "escalate", (
            f"low_confidence_rule.action 应为 'escalate'，实际={rule.get('action')!r}"
        )

    def test_low_confidence_rule_event_is_human_handoff(self):
        """low_confidence_rule 声明触发 human_handoff_event。"""
        bp = _build_bp()
        rules = _get_policy_rules(bp)
        rule = rules["low_confidence_rule"]
        assert rule.get("event") == "human_handoff_event", (
            f"low_confidence_rule.event 应为 'human_handoff_event'，实际={rule.get('event')!r}"
        )

    def test_low_confidence_rule_targets_escalation(self):
        """low_confidence_rule 作用目标是 Escalation 角色。"""
        bp = _build_bp()
        rules = _get_policy_rules(bp)
        rule = rules["low_confidence_rule"]
        assert rule.get("target_role") == "escalation", (
            f"low_confidence_rule.target_role 应为 'escalation'，实际={rule.get('target_role')!r}"
        )

    def test_custom_threshold_propagated_to_rule(self):
        """自定义 confidence_threshold 正确传播到 policy_rules 和 Answerer.metadata。"""
        bp = _build_bp(confidence_threshold=0.7)
        rules = _get_policy_rules(bp)
        rule = rules["low_confidence_rule"]
        assert "0.7" in rule.get("condition", ""), (
            f"自定义阈值 0.7 未在 low_confidence_rule.condition 中，实际={rule.get('condition')!r}"
        )
        answerer = _get_role(bp, "answerer")
        assert answerer is not None
        assert answerer.metadata.get("confidence_threshold") == 0.7

    def test_escalation_role_approval_gate_event(self):
        """Escalation 角色 metadata 中 approval_gate_event='human_handoff_event'。"""
        bp = _build_bp()
        escalation = _get_role(bp, "escalation")
        assert escalation is not None
        assert escalation.metadata.get("approval_gate_event") == "human_handoff_event", (
            f"Escalation.metadata.approval_gate_event 应为 'human_handoff_event'，"
            f"实际={escalation.metadata.get('approval_gate_event')!r}"
        )

    def test_escalation_strategy_propagated(self):
        """low_confidence_strategy 正确传播到 Escalation 角色 metadata。"""
        for strategy in ("escalate_human", "escalate_review", "reject_with_message"):
            bp = _build_bp(low_confidence_strategy=strategy)
            escalation = _get_role(bp, "escalation")
            assert escalation is not None
            assert escalation.metadata.get("escalation_strategy") == strategy, (
                f"strategy={strategy!r} 未正确传播到 Escalation.metadata"
            )


# ---------------------------------------------------------------------------
# 命中路径 — Answerer citation_required=True
# ---------------------------------------------------------------------------


class TestHitPathCitationPolicy:
    """命中路径 Blueprint 中 Answerer 角色 citation_required=True。"""

    def test_answerer_citation_required_true_in_metadata(self):
        """citation_required=True 时 Answerer.metadata.citation_required=True。"""
        bp = _build_bp(citation_required=True)
        answerer = _get_role(bp, "answerer")
        assert answerer is not None
        assert answerer.metadata.get("citation_required") is True, (
            f"Answerer.metadata.citation_required 应为 True，"
            f"实际={answerer.metadata.get('citation_required')!r}"
        )

    def test_answerer_citation_in_constraints(self):
        """citation_required=True 时 Answerer.constraints 中有引用相关声明。"""
        bp = _build_bp(citation_required=True)
        answerer = _get_role(bp, "answerer")
        assert answerer is not None
        constraints_str = " ".join(answerer.constraints)
        assert "citation_required=True" in constraints_str, (
            f"Answerer.constraints 未包含 'citation_required=True'，实际={answerer.constraints}"
        )

    def test_answerer_has_citation_service_tool(self):
        """命中路径 Answerer 必须有 citation_service 工具。"""
        bp = _build_bp(citation_required=True)
        answerer = _get_role(bp, "answerer")
        assert answerer is not None
        assert "citation_service" in answerer.tools, (
            f"Answerer.tools 未包含 'citation_service'，实际={answerer.tools}"
        )

    def test_blueprint_citation_required_in_metadata(self):
        """Blueprint.metadata.citation_required=True。"""
        bp = _build_bp(citation_required=True)
        assert bp.metadata.get("citation_required") is True

    def test_knowledge_binding_citation_required_with_pack(self):
        """绑定知识包时 KnowledgeBinding.citation_required 与 goal_inputs 对齐。"""
        bp = _build_bp(
            knowledge_source="existing_pack",
            citation_required=True,
            escalation_keywords=[],
        )
        # 注意：没有 pack_id 时不添加 binding
        bp_with_pack = _build_bp.__wrapped__ if hasattr(_build_bp, "__wrapped__") else None
        # 直接用 inputs
        from xueban.runtime.kits.knowledge_assistant_kit import (
            KnowledgeAssistantGoalInputs,
            create_knowledge_assistant_blueprint,
        )
        inputs = KnowledgeAssistantGoalInputs(
            knowledge_source="existing_pack",
            citation_required=True,
            low_confidence_strategy="escalate_human",
            escalation_keywords=[],
            assistant_name="Test",
            pack_id="pack-999",
        )
        bp2 = create_knowledge_assistant_blueprint(inputs)
        assert len(bp2.knowledge_bindings) == 1
        assert bp2.knowledge_bindings[0].citation_required is True


# ---------------------------------------------------------------------------
# escalation_keywords 规则
# ---------------------------------------------------------------------------


class TestEscalationKeywordsPolicy:
    """escalation_keywords 命中时强制引用，不允许引用缺失。"""

    def test_escalation_keywords_rule_exists(self):
        """有关键词时 escalation_keywords_rule 在 policy_rules 中。"""
        bp = _build_bp(escalation_keywords=["合规", "法律"])
        rules = _get_policy_rules(bp)
        assert "escalation_keywords_rule" in rules

    def test_escalation_keywords_rule_action_is_force_citation(self):
        """escalation_keywords_rule.action 是 'force_citation'。"""
        bp = _build_bp(escalation_keywords=["合规"])
        rules = _get_policy_rules(bp)
        rule = rules["escalation_keywords_rule"]
        assert rule.get("action") == "force_citation"

    def test_escalation_keywords_in_rule(self):
        """关键词列表正确传播到 escalation_keywords_rule.keywords。"""
        kws = ["合规", "法律", "投诉"]
        bp = _build_bp(escalation_keywords=kws)
        rules = _get_policy_rules(bp)
        rule = rules["escalation_keywords_rule"]
        assert rule.get("keywords") == kws

    def test_empty_keywords_rule_still_present(self):
        """关键词为空时规则仍然存在（关键词为空 = 不触发关键词强制引用）。"""
        bp = _build_bp(escalation_keywords=[])
        rules = _get_policy_rules(bp)
        assert "escalation_keywords_rule" in rules
        rule = rules["escalation_keywords_rule"]
        assert rule.get("keywords") == []

    def test_escalation_keywords_propagated_to_escalation_role(self):
        """关键词列表正确传播到 Escalation 角色 metadata。"""
        kws = ["VIP", "重要客户"]
        bp = _build_bp(escalation_keywords=kws)
        escalation = _get_role(bp, "escalation")
        assert escalation is not None
        assert escalation.metadata.get("escalation_keywords") == kws
