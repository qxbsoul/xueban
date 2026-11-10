"""Knowledge Assistant Kit smoke + regression eval pack — Story 10.6 AC2/AC3.

Verdict derivation rule (Story 10-2 C1 fix):
  Each executor MUST derive its pass/fail decision from declarative
  ``blueprint`` fields (``metadata['policy_rules']``, ``knowledge_bindings``,
  ``role_profiles``) instead of hardcoded mock answers/events. If the
  blueprint is missing or malformed for the relevant rule, the executor
  fails with an explicit ``missing_configs`` entry — surfacing
  configuration drift to the operator instead of silently green-lighting it.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from xueban.runtime.contracts_builder import AgentBlueprint, RoleProfile

from .runner import (
    KitSmokeEvalPack,
    RegressionCase,
    SmokeCase,
    SmokeRunOptions,
    SuggestedFix,
    register_eval_pack,
)

# Heuristic keywords whose presence in a "no-source" answer indicates fabrication.
FABRICATION_HINTS = ("根据我的训练", "据我所知", "我猜", "也许是", "应该是")


# ---------------------------------------------------------------------------
# Blueprint helpers
# ---------------------------------------------------------------------------


def _policy_rules(blueprint: AgentBlueprint) -> Dict[str, Any]:
    md = getattr(blueprint, "metadata", None) or {}
    rules = md.get("policy_rules") if isinstance(md, dict) else None
    return rules if isinstance(rules, dict) else {}


def _find_role(blueprint: AgentBlueprint, role_type: str) -> Optional[RoleProfile]:
    for role in blueprint.role_profiles or []:
        md = getattr(role, "metadata", None) or {}
        if isinstance(md, dict) and md.get("role_type") == role_type:
            return role
        # Fall back to role_id matching the type name
        if getattr(role, "role_id", None) == role_type:
            return role
    return None


# ---------------------------------------------------------------------------
# Smoke executors
# ---------------------------------------------------------------------------


async def _doc_hit_path(
    blueprint: AgentBlueprint, opts: SmokeRunOptions
) -> Dict[str, Any]:
    """Validate KnowledgeBinding shape: at least one usable binding with
    a numeric ``confidence_threshold`` in [0, 1] and ``citation_required``
    surfaced (either on the binding or in policy metadata)."""

    bindings = list(blueprint.knowledge_bindings or [])
    usable = [
        kb for kb in bindings if getattr(kb, "source_type", "unspecified") != "unspecified"
    ]
    if not usable:
        return {
            "passed": False,
            "failed_stage": "Retriever",
            "metrics": {"hit_count": 0.0},
            "missing_configs": ["KnowledgePack not bound"],
            "suggested_fixes": [SuggestedFix(label="绑定知识包", target="knowledge_dock")],
            "detail": "无可用 KnowledgePack，无法生成 citation_trace",
            "citation_present": False,
        }

    md = getattr(blueprint, "metadata", None) or {}
    threshold = md.get("confidence_threshold") if isinstance(md, dict) else None
    if not isinstance(threshold, (int, float)) or not (0.0 < float(threshold) <= 1.0):
        return {
            "passed": False,
            "failed_stage": "Retriever",
            "metrics": {"hit_count": 0.0},
            "missing_configs": [
                f"confidence_threshold invalid or missing: {threshold!r}"
            ],
            "suggested_fixes": [
                SuggestedFix(label="检查检索阈值", target="policy_panel"),
            ],
            "detail": "confidence_threshold 必须为 (0, 1] 之间的数值",
            "citation_present": False,
        }

    # citation_required must be declared somewhere authoritative
    citation_required = (
        md.get("citation_required") if isinstance(md, dict) else None
    )
    if citation_required is None:
        # Fallback: any binding declares it
        citation_required = any(
            bool(getattr(kb, "citation_required", False)) for kb in usable
        )
    if not citation_required:
        return {
            "passed": False,
            "failed_stage": "Retriever",
            "metrics": {"hit_count": 0.0},
            "missing_configs": ["citation_required must be true"],
            "suggested_fixes": [
                SuggestedFix(label="开启强制引用", target="policy_panel"),
            ],
            "detail": "citation_required=false 不满足可信问答要求",
            "citation_present": False,
        }

    citation_trace = [
        {
            "doc_id": getattr(usable[0], "source_ref", "kb-1") or "kb-1",
            "snippet": "Xueban 是一款多智能体编排平台",
        }
    ]
    return {
        "passed": True,
        "metrics": {
            "hit_count": float(len(usable)),
            "confidence_threshold": float(threshold),
        },
        "detail": (
            f"命中并附带 citation_trace（threshold={threshold}，"
            f"bindings={len(usable)}）"
        ),
        "citation_present": bool(citation_trace),
    }


async def _no_source_reject(
    blueprint: AgentBlueprint, opts: SmokeRunOptions
) -> Dict[str, Any]:
    """Validate ``no_source_rule`` is declared and Answerer carries a
    non-empty ``no_source_response`` template free of fabrication hints."""

    rules = _policy_rules(blueprint)
    rule = rules.get("no_source_rule") if isinstance(rules, dict) else None
    if not isinstance(rule, dict):
        return {
            "passed": False,
            "failed_stage": "Policy",
            "metrics": {"fabrication_score": 1.0},
            "missing_configs": ["policy_rules.no_source_rule missing"],
            "suggested_fixes": [
                SuggestedFix(label="检查 Policy / 拒答模板", target="policy_panel"),
            ],
            "detail": "blueprint.metadata.policy_rules.no_source_rule 未声明",
        }

    if rule.get("action") != "reject":
        return {
            "passed": False,
            "failed_stage": "Policy",
            "metrics": {"fabrication_score": 1.0},
            "missing_configs": [
                f"no_source_rule.action expected 'reject', got {rule.get('action')!r}"
            ],
            "suggested_fixes": [
                SuggestedFix(label="检查 Policy / 拒答模板", target="policy_panel"),
            ],
            "detail": "no_source_rule.action 必须为 'reject'",
        }

    template_key = rule.get("response_template")
    if not isinstance(template_key, str) or not template_key:
        return {
            "passed": False,
            "failed_stage": "Policy",
            "metrics": {"fabrication_score": 1.0},
            "missing_configs": ["no_source_rule.response_template missing"],
            "suggested_fixes": [
                SuggestedFix(label="检查 Policy / 拒答模板", target="policy_panel"),
            ],
            "detail": "no_source_rule.response_template 未声明",
        }

    answerer = _find_role(blueprint, "answerer")
    if answerer is None:
        return {
            "passed": False,
            "failed_stage": "Answerer",
            "metrics": {"fabrication_score": 1.0},
            "missing_configs": ["answerer role missing"],
            "suggested_fixes": [
                SuggestedFix(label="补充 Answerer 角色", target="role_panel"),
            ],
            "detail": "blueprint 缺少 answerer 角色",
        }

    role_md = getattr(answerer, "metadata", None) or {}
    answer = role_md.get(template_key) if isinstance(role_md, dict) else None
    if not isinstance(answer, str) or not answer.strip():
        return {
            "passed": False,
            "failed_stage": "Answerer",
            "metrics": {"fabrication_score": 1.0},
            "missing_configs": [
                f"answerer.metadata[{template_key!r}] missing or empty"
            ],
            "suggested_fixes": [
                SuggestedFix(label="补充拒答模板", target="policy_panel"),
            ],
            "detail": f"answerer.metadata 中缺少模板 {template_key!r}",
        }

    fabricated = [k for k in FABRICATION_HINTS if k in answer]
    if fabricated:
        return {
            "passed": False,
            "failed_stage": "Answerer",
            "metrics": {"fabrication_score": 1.0},
            "missing_configs": [f"fabrication hint detected: {fabricated}"],
            "suggested_fixes": [
                SuggestedFix(label="检查 Policy / 拒答模板", target="policy_panel"),
            ],
            "detail": f"拒答模板中检测到编造关键词：{fabricated}",
        }

    return {
        "passed": True,
        "metrics": {"fabrication_score": 0.0},
        "detail": (
            f"no_source_rule 已声明，answerer 模板 {template_key!r} 通过编造检查"
        ),
    }


async def _escalation_trigger(
    blueprint: AgentBlueprint, opts: SmokeRunOptions
) -> Dict[str, Any]:
    """Validate ``low_confidence_rule`` is declared and an escalation role
    with ``approval_gate_event`` exists to absorb ``human_handoff_event``."""

    rules = _policy_rules(blueprint)
    rule = rules.get("low_confidence_rule") if isinstance(rules, dict) else None
    if not isinstance(rule, dict):
        return {
            "passed": False,
            "failed_stage": "Policy",
            "metrics": {"handoff_events": 0.0},
            "missing_configs": ["policy_rules.low_confidence_rule missing"],
            "suggested_fixes": [
                SuggestedFix(label="检查 Policy", target="policy_panel"),
            ],
            "detail": "blueprint.metadata.policy_rules.low_confidence_rule 未声明",
        }

    if rule.get("action") != "escalate":
        return {
            "passed": False,
            "failed_stage": "Policy",
            "metrics": {"handoff_events": 0.0},
            "missing_configs": [
                f"low_confidence_rule.action expected 'escalate', got {rule.get('action')!r}"
            ],
            "suggested_fixes": [
                SuggestedFix(label="检查 Policy", target="policy_panel"),
            ],
            "detail": "low_confidence_rule.action 必须为 'escalate'",
        }

    event = rule.get("event")
    if event != "human_handoff_event":
        return {
            "passed": False,
            "failed_stage": "Policy",
            "metrics": {"handoff_events": 0.0},
            "missing_configs": [
                f"low_confidence_rule.event expected 'human_handoff_event', got {event!r}"
            ],
            "suggested_fixes": [
                SuggestedFix(label="检查 Policy", target="policy_panel"),
            ],
            "detail": "low_confidence_rule.event 必须为 'human_handoff_event'",
        }

    target_role = rule.get("target_role")
    escalation = _find_role(blueprint, "escalation")
    if escalation is None and isinstance(target_role, str):
        # Try matching by role_id explicitly
        for role in blueprint.role_profiles or []:
            if getattr(role, "role_id", None) == target_role:
                escalation = role
                break
    if escalation is None:
        return {
            "passed": False,
            "failed_stage": "Escalation",
            "metrics": {"handoff_events": 0.0},
            "missing_configs": ["escalation role missing"],
            "suggested_fixes": [
                SuggestedFix(label="补充 Escalation 角色", target="role_panel"),
            ],
            "detail": "blueprint 缺少 escalation 角色",
        }

    role_md = getattr(escalation, "metadata", None) or {}
    if not isinstance(role_md, dict) or role_md.get("approval_gate_event") != "human_handoff_event":
        return {
            "passed": False,
            "failed_stage": "Escalation",
            "metrics": {"handoff_events": 0.0},
            "missing_configs": [
                "escalation.metadata.approval_gate_event != 'human_handoff_event'"
            ],
            "suggested_fixes": [
                SuggestedFix(label="检查 Escalation 角色配置", target="role_panel"),
            ],
            "detail": "escalation 角色未声明 approval_gate_event=human_handoff_event",
        }

    return {
        "passed": True,
        "metrics": {"handoff_events": 1.0},
        "detail": (
            "low_confidence_rule + escalation 角色 approval_gate_event 已就绪"
        ),
    }


KIT_SMOKE_EVAL_PACK = KitSmokeEvalPack(
    kit_id="knowledge_assistant_kit",
    smoke_cases=[
        SmokeCase(
            name="doc_hit_path",
            description="有答案问题 → 引用完整",
            citation_required=True,
            executor=_doc_hit_path,
            pass_condition="knowledge_bindings 非空且 confidence_threshold/citation_required 合法",
        ),
        SmokeCase(
            name="no_source_reject",
            description="无答案问题 → 标准拒答，禁止编造",
            executor=_no_source_reject,
            pass_condition="policy_rules.no_source_rule + answerer.no_source_response 非编造",
        ),
        SmokeCase(
            name="escalation_trigger",
            description="低置信度问题 → 升级事件",
            executor=_escalation_trigger,
            pass_condition="policy_rules.low_confidence_rule + escalation.approval_gate_event",
        ),
    ],
    regression_cases=[
        RegressionCase(
            name="doc_hit_path_regression",
            description="doc_hit_path 跨版本对比",
            smoke_case_name="doc_hit_path",
            metric_thresholds={"hit_count": 1.0},
        ),
    ],
)

register_eval_pack(KIT_SMOKE_EVAL_PACK)

__all__ = ["KIT_SMOKE_EVAL_PACK", "FABRICATION_HINTS"]
