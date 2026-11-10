"""Runtime policy enforcers for Kit blueprints.

This package implements the **runtime side** of declarative policy rules
that are recorded in ``AgentBlueprint.metadata['policy_rules']`` by Kit
factories (e.g. ``knowledge_assistant_kit``). Story 10.2b introduced
:class:`KnowledgeAssistantPolicyEnforcer` to close the gap where rules
were documented in metadata but never consulted at runtime.
"""

from .knowledge_assistant_enforcer import (
    EnforcerInput,
    KnowledgeAssistantPolicyEnforcer,
    PolicyAction,
    PolicyActionKind,
    RetrieverError,
)

__all__ = [
    "EnforcerInput",
    "KnowledgeAssistantPolicyEnforcer",
    "PolicyAction",
    "PolicyActionKind",
    "RetrieverError",
]
