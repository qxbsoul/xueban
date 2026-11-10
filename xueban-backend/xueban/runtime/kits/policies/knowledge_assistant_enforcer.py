"""Knowledge Assistant Policy Enforcer — Story 10.2b.

Runtime enforcement for the 4 policy rules declared by
``knowledge_assistant_kit`` in ``blueprint.metadata['policy_rules']``:

  1. ``no_source_rule``           — empty retrieval → use refusal template
  2. ``low_confidence_rule``      — confidence < threshold → escalate
  3. ``escalation_keywords_rule`` — query contains keyword → require citation
                                    (and optionally escalate)
  4. ``retriever_failure_rule``   — retriever raised → fallback template

Design principles (from story spec):
  * Stateless service — only reads blueprint metadata + runtime input.
  * Closed enum of ``PolicyAction`` kinds — adding a new kind forces the
    caller to handle it explicitly.
  * Fail-closed on malformed metadata — ``PROCEED`` is only returned when
    no rule fired. Missing/garbage rule data degrades gracefully and is
    surfaced via ``PolicyAction.diagnostics``.
  * Observable — every triggered rule produces an event payload that the
    executor can publish on the run event stream as
    ``policy_enforced_event``.

This module is import-free of the rest of the runtime so it can be unit
tested in isolation and wired in by both the live Answerer call chain
(when it exists) and the ``KitSmokeRunner`` eval path.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


# ---------------------------------------------------------------------------
# Public contracts
# ---------------------------------------------------------------------------


class RetrieverError(Exception):
    """Raised by retriever adapters; treated by the enforcer as a fallback
    trigger for ``retriever_failure_rule``. Any ``Exception`` is accepted
    in :class:`EnforcerInput.retriever_error`; this class merely provides a
    convenient default."""


class PolicyActionKind(str, enum.Enum):
    """Closed set of enforcer outcomes."""

    PROCEED = "proceed"
    USE_TEMPLATE = "use_template"
    ESCALATE = "escalate"
    REQUIRE_CITATION = "require_citation"


@dataclass
class PolicyAction:
    """Decision returned from :meth:`KnowledgeAssistantPolicyEnforcer.before_answer`.

    Attributes:
        kind: Which branch the executor should take.
        rule: Rule id that fired (``"no_source_rule"`` etc.). ``None`` when
            ``kind == PROCEED``.
        template: Template *string* (already resolved). Set when
            ``kind == USE_TEMPLATE``.
        template_key: Original key referenced in the rule
            (``"no_source_response"`` etc.). Useful for telemetry.
        reason: Stable reason code for ``ESCALATE`` (e.g. ``"low_confidence"``).
        details: Arbitrary structured detail, surfaced on the
            ``policy_enforced_event``.
        diagnostics: Soft warnings about malformed metadata. Non-fatal.
        events: Event payloads to be emitted on the run event stream.
            Each element is a dict the executor can wrap in its native event
            envelope.
    """

    kind: PolicyActionKind
    rule: Optional[str] = None
    template: Optional[str] = None
    template_key: Optional[str] = None
    reason: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)
    diagnostics: List[str] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def proceed(cls, *, diagnostics: Optional[List[str]] = None) -> "PolicyAction":
        return cls(kind=PolicyActionKind.PROCEED, diagnostics=list(diagnostics or []))


@dataclass
class EnforcerInput:
    """Inputs the enforcer needs to decide.

    Attributes:
        query: User question (raw string).
        retrieved_chunks: Retriever output. Each chunk should expose a
            ``confidence`` numeric field — accepted as either a Mapping or
            an object with attribute access. Empty list ⇒ no source.
        retriever_error: Exception captured by the executor when the
            retriever raised. ``None`` when retrieval succeeded.
        blueprint_metadata: ``AgentBlueprint.metadata`` (the dict produced
            by ``create_knowledge_assistant_blueprint``).
    """

    query: str
    retrieved_chunks: Sequence[Any] = field(default_factory=list)
    retriever_error: Optional[BaseException] = None
    blueprint_metadata: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Enforcer
# ---------------------------------------------------------------------------


_DEFAULT_THRESHOLD = 0.5
_DEFAULT_NO_SOURCE_TEMPLATE = (
    "很抱歉，我没有找到与您问题相关的知识库内容，无法提供答案。请联系人工客服获取帮助。"
)
_DEFAULT_RETRIEVER_FAILURE_TEMPLATE = (
    "抱歉，知识检索暂时不可用，已为您升级至人工跟进。"
)


class KnowledgeAssistantPolicyEnforcer:
    """Stateless enforcer for Knowledge Assistant Kit blueprints.

    Usage::

        action = KnowledgeAssistantPolicyEnforcer().before_answer(
            EnforcerInput(query=q, retrieved_chunks=chunks,
                          blueprint_metadata=blueprint.metadata),
        )
        if action.kind is PolicyActionKind.USE_TEMPLATE:
            reply_with(action.template)
        elif action.kind is PolicyActionKind.ESCALATE:
            handoff(reason=action.reason, details=action.details)
        elif action.kind is PolicyActionKind.REQUIRE_CITATION:
            answer = invoke_llm()
            assert answer.citation_trace, "citation required"
        else:
            answer = invoke_llm()
    """

    # Rule evaluation order. ``retriever_failure`` and ``no_source`` are
    # terminal short-circuits (the LLM should never run). ``low_confidence``
    # also short-circuits to escalation. ``escalation_keywords`` runs last
    # because it's an additive constraint on the answerer (citation required)
    # and may upgrade itself to ``ESCALATE`` if rule.action == "escalate".

    def before_answer(self, inp: EnforcerInput) -> PolicyAction:
        rules = _safe_dict(inp.blueprint_metadata.get("policy_rules"))
        diagnostics: List[str] = []

        # 1. retriever_failure_rule — strongest short-circuit.
        if inp.retriever_error is not None:
            return self._handle_retriever_failure(
                rules.get("retriever_failure_rule"), inp.retriever_error, diagnostics
            )

        # 2. no_source_rule — empty chunks.
        if not inp.retrieved_chunks:
            return self._handle_no_source(rules.get("no_source_rule"), diagnostics)

        # 3. low_confidence_rule.
        threshold = self._resolve_threshold(
            inp.blueprint_metadata, rules.get("low_confidence_rule"), diagnostics
        )
        max_conf = _max_confidence(inp.retrieved_chunks, diagnostics)
        if max_conf is not None and max_conf < threshold:
            rule_cfg = _safe_dict(rules.get("low_confidence_rule"))
            event = rule_cfg.get("event") or "human_handoff_event"
            details = {"max_confidence": max_conf, "threshold": threshold}
            return PolicyAction(
                kind=PolicyActionKind.ESCALATE,
                rule="low_confidence_rule",
                reason="low_confidence",
                details=details,
                diagnostics=diagnostics,
                events=[
                    {
                        "type": "policy_enforced_event",
                        "rule": "low_confidence_rule",
                        "event": event,
                        "details": details,
                    }
                ],
            )

        # 4. escalation_keywords_rule.
        kw_action = self._handle_escalation_keywords(
            inp.query,
            inp.blueprint_metadata,
            rules.get("escalation_keywords_rule"),
            diagnostics,
        )
        if kw_action is not None:
            return kw_action

        return PolicyAction.proceed(diagnostics=diagnostics)

    # ------------------------------------------------------------------
    # Rule handlers
    # ------------------------------------------------------------------

    def _handle_retriever_failure(
        self,
        rule_cfg: Any,
        error: BaseException,
        diagnostics: List[str],
    ) -> PolicyAction:
        cfg = _safe_dict(rule_cfg)
        template_key = (
            cfg.get("fallback_template")
            or cfg.get("response_template")
            or "no_source_response"
        )
        template = (
            cfg.get("template_text")
            if isinstance(cfg.get("template_text"), str)
            else None
        ) or _DEFAULT_RETRIEVER_FAILURE_TEMPLATE
        if not isinstance(rule_cfg, Mapping):
            diagnostics.append("retriever_failure_rule missing or malformed; using defaults")
        details = {"error_type": type(error).__name__, "error_message": str(error)}
        return PolicyAction(
            kind=PolicyActionKind.USE_TEMPLATE,
            rule="retriever_failure_rule",
            template=template,
            template_key=template_key,
            details=details,
            diagnostics=diagnostics,
            events=[
                {
                    "type": "policy_enforced_event",
                    "rule": "retriever_failure_rule",
                    "error": type(error).__name__,
                    "details": details,
                }
            ],
        )

    def _handle_no_source(
        self, rule_cfg: Any, diagnostics: List[str]
    ) -> PolicyAction:
        cfg = _safe_dict(rule_cfg)
        if not cfg:
            diagnostics.append("no_source_rule missing or malformed; using default refusal template")
        template_key = cfg.get("response_template") or "no_source_response"
        template = (
            cfg.get("template_text")
            if isinstance(cfg.get("template_text"), str)
            else None
        ) or _DEFAULT_NO_SOURCE_TEMPLATE
        return PolicyAction(
            kind=PolicyActionKind.USE_TEMPLATE,
            rule="no_source_rule",
            template=template,
            template_key=template_key,
            diagnostics=diagnostics,
            events=[
                {
                    "type": "policy_enforced_event",
                    "rule": "no_source_rule",
                    "template_key": template_key,
                }
            ],
        )

    def _handle_escalation_keywords(
        self,
        query: str,
        metadata: Mapping[str, Any],
        rule_cfg: Any,
        diagnostics: List[str],
    ) -> Optional[PolicyAction]:
        cfg = _safe_dict(rule_cfg)
        # Keywords may live on the rule or at the top level of metadata.
        keywords: Iterable[Any] = (
            cfg.get("keywords")
            if isinstance(cfg.get("keywords"), list)
            else metadata.get("escalation_keywords", [])
        )
        if not isinstance(keywords, list) or not keywords:
            return None  # rule disabled
        clean_keywords = [k for k in keywords if isinstance(k, str) and k]
        if not clean_keywords:
            diagnostics.append("escalation_keywords list contained no usable strings")
            return None
        q_lower = (query or "").lower()
        matched = [k for k in clean_keywords if k.lower() in q_lower]
        if not matched:
            return None

        also_escalate = cfg.get("action") == "escalate"
        details = {"matched_keywords": matched}
        events = [
            {
                "type": "policy_enforced_event",
                "rule": "escalation_keywords_rule",
                "matched_keywords": matched,
                "escalated": bool(also_escalate),
            }
        ]
        if also_escalate:
            return PolicyAction(
                kind=PolicyActionKind.ESCALATE,
                rule="escalation_keywords_rule",
                reason="escalation_keyword",
                details=details,
                diagnostics=diagnostics,
                events=events,
            )
        return PolicyAction(
            kind=PolicyActionKind.REQUIRE_CITATION,
            rule="escalation_keywords_rule",
            details=details,
            diagnostics=diagnostics,
            events=events,
        )

    # ------------------------------------------------------------------
    # Threshold resolution: metadata > rule.threshold > default 0.5.
    # ------------------------------------------------------------------

    def _resolve_threshold(
        self,
        metadata: Mapping[str, Any],
        rule_cfg: Any,
        diagnostics: List[str],
    ) -> float:
        candidate = metadata.get("confidence_threshold")
        if isinstance(candidate, (int, float)) and 0.0 <= float(candidate) <= 1.0:
            return float(candidate)
        if candidate is not None:
            diagnostics.append(
                f"metadata.confidence_threshold invalid: {candidate!r}; falling back"
            )
        cfg = _safe_dict(rule_cfg)
        rule_threshold = cfg.get("threshold")
        if isinstance(rule_threshold, (int, float)) and 0.0 <= float(rule_threshold) <= 1.0:
            return float(rule_threshold)
        if rule_threshold is not None:
            diagnostics.append(
                f"low_confidence_rule.threshold invalid: {rule_threshold!r}; falling back"
            )
        return _DEFAULT_THRESHOLD


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _max_confidence(
    chunks: Sequence[Any], diagnostics: List[str]
) -> Optional[float]:
    values: List[float] = []
    for chunk in chunks:
        conf = None
        if isinstance(chunk, Mapping):
            conf = chunk.get("confidence")
        else:
            conf = getattr(chunk, "confidence", None)
        if isinstance(conf, (int, float)):
            values.append(float(conf))
    if not values:
        diagnostics.append("retrieved_chunks present but no numeric confidence")
        return None
    return max(values)
