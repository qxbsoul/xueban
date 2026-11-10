"""Unit tests for KnowledgeAssistantPolicyEnforcer (Story 10.2b).

Covers each of the 4 policy rules' pass / trigger paths plus boundary
conditions: confidence_threshold exact match, empty escalation keywords,
malformed rule metadata, retriever failure, and multi-rule priority.
"""
from __future__ import annotations

from typing import Any, Dict

import pytest

from xueban.runtime.kits.knowledge_assistant_kit import (
    KnowledgeAssistantGoalInputs,
    create_knowledge_assistant_blueprint,
)
from xueban.runtime.kits.policies import (
    EnforcerInput,
    KnowledgeAssistantPolicyEnforcer,
    PolicyAction,
    PolicyActionKind,
    RetrieverError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _metadata(
    *,
    threshold: float = 0.5,
    keywords: Any = None,
    keyword_action: str | None = None,
) -> Dict[str, Any]:
    """Build a metadata dict shaped like the kit produces."""
    if keywords is None:
        keywords = ["退款", "投诉", "lawsuit"]
    rules: Dict[str, Any] = {
        "no_source_rule": {
            "action": "reject",
            "response_template": "no_source_response",
        },
        "low_confidence_rule": {
            "action": "escalate",
            "event": "human_handoff_event",
            "threshold": threshold,
        },
        "escalation_keywords_rule": {
            "keywords": list(keywords),
            "action": "force_citation",
        },
        "retriever_failure_rule": {
            "action": "reject",
            "response_template": "no_source_response",
        },
    }
    if keyword_action is not None:
        rules["escalation_keywords_rule"]["action"] = keyword_action
    return {
        "policy_rules": rules,
        "confidence_threshold": threshold,
        "escalation_keywords": list(keywords),
    }


@pytest.fixture()
def enforcer() -> KnowledgeAssistantPolicyEnforcer:
    return KnowledgeAssistantPolicyEnforcer()


# ---------------------------------------------------------------------------
# no_source_rule
# ---------------------------------------------------------------------------


def test_no_source_rule_triggers_on_empty_chunks(enforcer):
    action = enforcer.before_answer(
        EnforcerInput(query="how do I reset?", retrieved_chunks=[],
                      blueprint_metadata=_metadata())
    )
    assert action.kind is PolicyActionKind.USE_TEMPLATE
    assert action.rule == "no_source_rule"
    assert action.template_key == "no_source_response"
    assert action.template  # non-empty
    assert action.events and action.events[0]["rule"] == "no_source_rule"


def test_no_source_rule_does_not_trigger_when_chunks_present(enforcer):
    action = enforcer.before_answer(
        EnforcerInput(
            query="how do I reset?",
            retrieved_chunks=[{"confidence": 0.9, "text": "Click reset."}],
            blueprint_metadata=_metadata(),
        )
    )
    assert action.kind is PolicyActionKind.PROCEED


# ---------------------------------------------------------------------------
# low_confidence_rule
# ---------------------------------------------------------------------------


def test_low_confidence_rule_escalates_below_threshold(enforcer):
    action = enforcer.before_answer(
        EnforcerInput(
            query="something obscure",
            retrieved_chunks=[{"confidence": 0.42}, {"confidence": 0.30}],
            blueprint_metadata=_metadata(threshold=0.5),
        )
    )
    assert action.kind is PolicyActionKind.ESCALATE
    assert action.reason == "low_confidence"
    assert action.details["max_confidence"] == pytest.approx(0.42)
    assert action.details["threshold"] == 0.5
    assert action.events[0]["event"] == "human_handoff_event"


def test_low_confidence_rule_boundary_exact_threshold_proceeds(enforcer):
    # At exact threshold, rule says < threshold, so it should NOT trigger.
    action = enforcer.before_answer(
        EnforcerInput(
            query="boundary test",
            retrieved_chunks=[{"confidence": 0.5}],
            blueprint_metadata=_metadata(threshold=0.5),
        )
    )
    assert action.kind is PolicyActionKind.PROCEED


def test_low_confidence_rule_threshold_priority_metadata_over_rule(enforcer):
    # metadata.confidence_threshold (0.8) should override rule.threshold (0.1).
    md = _metadata(threshold=0.1)
    md["confidence_threshold"] = 0.8
    action = enforcer.before_answer(
        EnforcerInput(
            query="q",
            retrieved_chunks=[{"confidence": 0.5}],
            blueprint_metadata=md,
        )
    )
    assert action.kind is PolicyActionKind.ESCALATE


# ---------------------------------------------------------------------------
# escalation_keywords_rule
# ---------------------------------------------------------------------------


def test_escalation_keywords_require_citation_on_match(enforcer):
    action = enforcer.before_answer(
        EnforcerInput(
            query="我要退款!!!",
            retrieved_chunks=[{"confidence": 0.95}],
            blueprint_metadata=_metadata(),
        )
    )
    assert action.kind is PolicyActionKind.REQUIRE_CITATION
    assert "退款" in action.details["matched_keywords"]


def test_escalation_keywords_case_insensitive(enforcer):
    action = enforcer.before_answer(
        EnforcerInput(
            query="Filing a LAWSUIT now",
            retrieved_chunks=[{"confidence": 0.9}],
            blueprint_metadata=_metadata(),
        )
    )
    assert action.kind is PolicyActionKind.REQUIRE_CITATION
    assert "lawsuit" in action.details["matched_keywords"]


def test_escalation_keywords_action_escalate_upgrades(enforcer):
    md = _metadata(keyword_action="escalate")
    action = enforcer.before_answer(
        EnforcerInput(
            query="我要投诉",
            retrieved_chunks=[{"confidence": 0.9}],
            blueprint_metadata=md,
        )
    )
    assert action.kind is PolicyActionKind.ESCALATE
    assert action.reason == "escalation_keyword"


def test_escalation_keywords_empty_list_disables_rule(enforcer):
    md = _metadata(keywords=[])
    action = enforcer.before_answer(
        EnforcerInput(
            query="我要退款",
            retrieved_chunks=[{"confidence": 0.9}],
            blueprint_metadata=md,
        )
    )
    assert action.kind is PolicyActionKind.PROCEED


def test_escalation_keywords_no_match_proceeds(enforcer):
    action = enforcer.before_answer(
        EnforcerInput(
            query="how do I reset my password",
            retrieved_chunks=[{"confidence": 0.9}],
            blueprint_metadata=_metadata(),
        )
    )
    assert action.kind is PolicyActionKind.PROCEED


# ---------------------------------------------------------------------------
# retriever_failure_rule
# ---------------------------------------------------------------------------


def test_retriever_failure_returns_template(enforcer):
    err = RetrieverError("upstream 503")
    action = enforcer.before_answer(
        EnforcerInput(
            query="anything",
            retrieved_chunks=[],  # would also trigger no_source, but failure wins
            retriever_error=err,
            blueprint_metadata=_metadata(),
        )
    )
    assert action.kind is PolicyActionKind.USE_TEMPLATE
    assert action.rule == "retriever_failure_rule"
    assert action.details["error_type"] == "RetrieverError"
    assert action.details["error_message"] == "upstream 503"
    assert action.events[0]["error"] == "RetrieverError"


def test_retriever_failure_accepts_any_exception(enforcer):
    action = enforcer.before_answer(
        EnforcerInput(
            query="q",
            retriever_error=RuntimeError("boom"),
            blueprint_metadata=_metadata(),
        )
    )
    assert action.kind is PolicyActionKind.USE_TEMPLATE
    assert action.rule == "retriever_failure_rule"


# ---------------------------------------------------------------------------
# Priority + malformed metadata
# ---------------------------------------------------------------------------


def test_priority_retriever_failure_beats_low_confidence(enforcer):
    action = enforcer.before_answer(
        EnforcerInput(
            query="退款",
            retrieved_chunks=[{"confidence": 0.1}],
            retriever_error=RetrieverError("x"),
            blueprint_metadata=_metadata(),
        )
    )
    assert action.rule == "retriever_failure_rule"


def test_priority_no_source_beats_keywords(enforcer):
    action = enforcer.before_answer(
        EnforcerInput(
            query="我要退款",
            retrieved_chunks=[],
            blueprint_metadata=_metadata(),
        )
    )
    assert action.rule == "no_source_rule"


def test_malformed_policy_rules_proceeds_with_diagnostics(enforcer):
    # policy_rules is a string, not a dict -> safely ignored.
    action = enforcer.before_answer(
        EnforcerInput(
            query="hi",
            retrieved_chunks=[{"confidence": 0.9}],
            blueprint_metadata={"policy_rules": "garbage"},
        )
    )
    assert action.kind is PolicyActionKind.PROCEED


def test_malformed_no_source_rule_still_uses_default_template(enforcer):
    action = enforcer.before_answer(
        EnforcerInput(
            query="hi",
            retrieved_chunks=[],
            blueprint_metadata={"policy_rules": {"no_source_rule": "not-a-dict"}},
        )
    )
    assert action.kind is PolicyActionKind.USE_TEMPLATE
    assert action.template  # default refusal template was used
    assert any("no_source_rule" in d for d in action.diagnostics)


def test_invalid_threshold_falls_back_to_default(enforcer):
    md = _metadata(threshold=0.5)
    md["confidence_threshold"] = "not-a-number"
    md["policy_rules"]["low_confidence_rule"]["threshold"] = "also-bad"
    # max_conf 0.4 < default 0.5 -> escalate
    action = enforcer.before_answer(
        EnforcerInput(
            query="q",
            retrieved_chunks=[{"confidence": 0.4}],
            blueprint_metadata=md,
        )
    )
    assert action.kind is PolicyActionKind.ESCALATE
    assert action.details["threshold"] == 0.5
    assert action.diagnostics  # surfaced warnings


# ---------------------------------------------------------------------------
# Integration with real kit blueprint
# ---------------------------------------------------------------------------


def test_integrates_with_real_knowledge_assistant_blueprint(enforcer):
    bp = create_knowledge_assistant_blueprint(
        KnowledgeAssistantGoalInputs(
            knowledge_source_label="FAQ",
            escalation_keywords=["退款", "投诉"],
            confidence_threshold=0.6,
        )
    )
    # Empty chunks -> no_source_rule
    a1 = enforcer.before_answer(
        EnforcerInput(query="hello", retrieved_chunks=[],
                      blueprint_metadata=bp.metadata)
    )
    assert a1.rule == "no_source_rule"

    # Below threshold -> escalate
    a2 = enforcer.before_answer(
        EnforcerInput(query="hello",
                      retrieved_chunks=[{"confidence": 0.3}],
                      blueprint_metadata=bp.metadata)
    )
    assert a2.kind is PolicyActionKind.ESCALATE
    assert a2.details["threshold"] == 0.6

    # Keyword match -> require citation
    a3 = enforcer.before_answer(
        EnforcerInput(query="我要退款",
                      retrieved_chunks=[{"confidence": 0.9}],
                      blueprint_metadata=bp.metadata)
    )
    assert a3.kind is PolicyActionKind.REQUIRE_CITATION


def test_chunks_with_attribute_access_supported(enforcer):
    class Chunk:
        def __init__(self, c):
            self.confidence = c

    action = enforcer.before_answer(
        EnforcerInput(
            query="q",
            retrieved_chunks=[Chunk(0.2), Chunk(0.4)],
            blueprint_metadata=_metadata(threshold=0.5),
        )
    )
    assert action.kind is PolicyActionKind.ESCALATE
    assert action.details["max_confidence"] == pytest.approx(0.4)
