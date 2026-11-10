"""Story 10-2 C1 reverse tests — verify Knowledge Assistant Kit eval
executors derive verdict from blueprint fields and FAIL when the
declarative policy/role/binding configuration is missing or malformed.
"""
from __future__ import annotations

import copy

import pytest

from xueban.runtime.contracts_builder import AgentBlueprint
from xueban.runtime.kits.evals import KitSmokeRunner, SmokeRunOptions
from xueban.runtime.kits.evals.knowledge_assistant_kit_eval import (
    _doc_hit_path,
    _escalation_trigger,
    _no_source_reject,
)
from xueban.runtime.kits.knowledge_assistant_kit import (
    KnowledgeAssistantGoalInputs,
    create_knowledge_assistant_blueprint,
)


def _factory_bp(**kwargs) -> AgentBlueprint:
    inputs = KnowledgeAssistantGoalInputs(
        knowledge_source=kwargs.get("knowledge_source", "existing_pack"),
        pack_id=kwargs.get("pack_id", "kb-1"),
        citation_required=kwargs.get("citation_required", True),
        low_confidence_strategy=kwargs.get("low_confidence_strategy", "escalate_human"),
        escalation_keywords=kwargs.get("escalation_keywords", []),
        assistant_name=kwargs.get("assistant_name", "Reverse Test Assistant"),
        confidence_threshold=kwargs.get("confidence_threshold", 0.5),
    )
    return create_knowledge_assistant_blueprint(inputs)


@pytest.mark.asyncio
async def test_factory_blueprint_passes_all_three_executors() -> None:
    """Sanity: a clean factory-built blueprint passes all three executors."""
    bp = _factory_bp()
    opts = SmokeRunOptions()
    assert (await _doc_hit_path(bp, opts))["passed"] is True
    assert (await _no_source_reject(bp, opts))["passed"] is True
    assert (await _escalation_trigger(bp, opts))["passed"] is True


@pytest.mark.asyncio
async def test_no_source_reject_fails_when_policy_rules_empty() -> None:
    bp = _factory_bp()
    bp.metadata = {**(bp.metadata or {}), "policy_rules": {}}
    out = await _no_source_reject(bp, SmokeRunOptions())
    assert out["passed"] is False
    assert any("no_source_rule" in m for m in out["missing_configs"])


@pytest.mark.asyncio
async def test_no_source_reject_fails_when_template_missing_on_answerer() -> None:
    bp = _factory_bp()
    for role in bp.role_profiles:
        if (role.metadata or {}).get("role_type") == "answerer":
            role.metadata = {
                k: v for k, v in (role.metadata or {}).items() if k != "no_source_response"
            }
    out = await _no_source_reject(bp, SmokeRunOptions())
    assert out["passed"] is False
    assert any("no_source_response" in m for m in out["missing_configs"])


@pytest.mark.asyncio
async def test_no_source_reject_fails_when_template_contains_fabrication_hint() -> None:
    bp = _factory_bp()
    for role in bp.role_profiles:
        if (role.metadata or {}).get("role_type") == "answerer":
            md = dict(role.metadata or {})
            md["no_source_response"] = "据我所知这个问题应该是这样的..."  # fabrication hint
            role.metadata = md
    out = await _no_source_reject(bp, SmokeRunOptions())
    assert out["passed"] is False
    assert out["failed_stage"] == "Answerer"


@pytest.mark.asyncio
async def test_doc_hit_path_fails_when_threshold_zero() -> None:
    bp = _factory_bp()
    md = dict(bp.metadata or {})
    md["confidence_threshold"] = 0.0
    bp.metadata = md
    out = await _doc_hit_path(bp, SmokeRunOptions())
    assert out["passed"] is False
    assert any("confidence_threshold" in m for m in out["missing_configs"])


@pytest.mark.asyncio
async def test_doc_hit_path_fails_when_threshold_missing() -> None:
    bp = _factory_bp()
    md = dict(bp.metadata or {})
    md.pop("confidence_threshold", None)
    bp.metadata = md
    out = await _doc_hit_path(bp, SmokeRunOptions())
    assert out["passed"] is False
    assert any("confidence_threshold" in m for m in out["missing_configs"])


@pytest.mark.asyncio
async def test_doc_hit_path_fails_when_citation_not_required() -> None:
    bp = _factory_bp(citation_required=False)
    # Override factory's auto-fill of citation_required in policy metadata
    md = dict(bp.metadata or {})
    md["citation_required"] = False
    bp.metadata = md
    for kb in bp.knowledge_bindings:
        kb.citation_required = False
    out = await _doc_hit_path(bp, SmokeRunOptions())
    assert out["passed"] is False
    assert any("citation_required" in m for m in out["missing_configs"])


@pytest.mark.asyncio
async def test_doc_hit_path_fails_when_no_bindings() -> None:
    bp = _factory_bp(knowledge_source="none", pack_id=None)
    out = await _doc_hit_path(bp, SmokeRunOptions())
    assert out["passed"] is False
    assert any("KnowledgePack" in m for m in out["missing_configs"])


@pytest.mark.asyncio
async def test_escalation_trigger_fails_when_low_confidence_rule_missing() -> None:
    bp = _factory_bp()
    rules = copy.deepcopy(bp.metadata.get("policy_rules", {}))
    rules.pop("low_confidence_rule", None)
    bp.metadata = {**bp.metadata, "policy_rules": rules}
    out = await _escalation_trigger(bp, SmokeRunOptions())
    assert out["passed"] is False
    assert any("low_confidence_rule" in m for m in out["missing_configs"])


@pytest.mark.asyncio
async def test_escalation_trigger_fails_when_escalation_role_missing() -> None:
    bp = _factory_bp()
    bp.role_profiles = [
        r for r in bp.role_profiles if (r.metadata or {}).get("role_type") != "escalation"
    ]
    out = await _escalation_trigger(bp, SmokeRunOptions())
    assert out["passed"] is False
    assert out["failed_stage"] == "Escalation"
    assert any("escalation" in m.lower() for m in out["missing_configs"])


@pytest.mark.asyncio
async def test_escalation_trigger_fails_when_role_missing_approval_gate_event() -> None:
    bp = _factory_bp()
    for role in bp.role_profiles:
        if (role.metadata or {}).get("role_type") == "escalation":
            md = dict(role.metadata or {})
            md.pop("approval_gate_event", None)
            role.metadata = md
    out = await _escalation_trigger(bp, SmokeRunOptions())
    assert out["passed"] is False
    assert any("approval_gate_event" in m for m in out["missing_configs"])


@pytest.mark.asyncio
async def test_escalation_trigger_fails_when_event_misconfigured() -> None:
    bp = _factory_bp()
    rules = copy.deepcopy(bp.metadata.get("policy_rules", {}))
    rules["low_confidence_rule"]["event"] = "wrong_event"
    bp.metadata = {**bp.metadata, "policy_rules": rules}
    out = await _escalation_trigger(bp, SmokeRunOptions())
    assert out["passed"] is False


@pytest.mark.asyncio
async def test_runner_reports_failure_when_policy_rules_stripped() -> None:
    """End-to-end via KitSmokeRunner: stripping policy_rules → smoke fails."""
    bp = _factory_bp()
    bp.metadata = {**(bp.metadata or {}), "policy_rules": {}}
    runner = KitSmokeRunner()
    report = await runner.run_smoke("knowledge_assistant_kit", bp, SmokeRunOptions())
    by_name = {c.name: c for c in report.case_results}
    assert by_name["no_source_reject"].passed is False
    assert by_name["escalation_trigger"].passed is False
    # doc_hit_path should still pass — it depends on bindings + threshold,
    # which the factory sets independent of policy_rules
    assert by_name["doc_hit_path"].passed is True
