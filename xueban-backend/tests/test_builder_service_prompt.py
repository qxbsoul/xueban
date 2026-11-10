"""tests/test_builder_service_prompt.py — T1 follow-up

Verifies that ``builder_service._build_workflow_definition`` uses the central
``SystemPromptBuilder`` instead of the legacy one-liner that dropped
responsibilities / constraints from the RoleProfile contract.

Before T1 follow-up this test FAILED because the emitted prompt was simply
``"You are Researcher. Investigates topics."`` — no responsibilities, no
constraints. After the fix the prompt is composed by SystemPromptBuilder and
contains all RoleProfile sections.
"""
from __future__ import annotations

import pytest

from xueban.runtime.builder_service import (
    _blueprint_to_template_spec,
    _build_workflow_definition,
)
from xueban.runtime.contracts import WorkflowDefinition
from xueban.runtime.contracts_builder import AgentBlueprint, RoleProfile


@pytest.fixture
def rich_blueprint() -> AgentBlueprint:
    """A blueprint whose role carries every field the legacy one-liner dropped."""
    return AgentBlueprint(
        name="researcher-team",
        goal="Investigate latent semantic indexing techniques.",
        mode="single",
        role_profiles=[
            RoleProfile(
                name="Researcher",
                description="Investigates topics and gathers credible sources.",
                responsibilities=["Gather sources", "Summarise findings"],
                constraints=["No speculation", "Cite every claim"],
                executor_provider="anthropic",
            )
        ],
    )


def _instantiate(blueprint: AgentBlueprint) -> WorkflowDefinition:
    """Exercise the same code path BuilderService.instantiate_blueprint uses."""
    template_spec = _blueprint_to_template_spec(blueprint)
    return _build_workflow_definition(blueprint, template_spec)


def _agent_prompt(wf: WorkflowDefinition) -> str:
    node = wf.nodes[0]
    # WorkflowDefinition.nodes may be pydantic models; cope with either shape.
    cfg = getattr(node, "config", None) or node["config"]  # type: ignore[index]
    if hasattr(cfg, "model_dump"):
        cfg = cfg.model_dump()
    prompt = cfg.get("prompt") if isinstance(cfg, dict) else None
    assert isinstance(prompt, str) and prompt, "agent node must carry a non-empty prompt"
    return prompt


def test_workflow_node_prompt_includes_full_role_contract(rich_blueprint: AgentBlueprint):
    """Regression: responsibilities + constraints must reach the agent node."""
    wf = _instantiate(rich_blueprint)
    prompt = _agent_prompt(wf)

    # Identity (kept from the legacy one-liner).
    assert "Researcher" in prompt
    assert "Investigates topics" in prompt

    # Sections that the legacy one-liner dropped — the whole point of this test.
    assert "Gather sources" in prompt, "responsibilities must be rendered"
    assert "No speculation" in prompt, "constraints must be rendered"

    # The composed prompt should carry the SystemPromptBuilder section headers,
    # which proves the new builder ran (not just an inlined string concat).
    assert "Responsibilities:" in prompt
    assert "Constraints:" in prompt


def test_prompt_is_not_one_liner(rich_blueprint: AgentBlueprint):
    """The legacy one-liner had no newlines; the builder always emits sections."""
    wf = _instantiate(rich_blueprint)
    prompt = _agent_prompt(wf)
    assert "\n" in prompt, "SystemPromptBuilder must produce a multi-section prompt"


def test_role_without_responsibilities_still_renders_identity():
    """Backward-compat: minimal RoleProfile still produces a usable prompt."""
    blueprint = AgentBlueprint(
        name="minimal",
        goal="Do a thing.",
        mode="single",
        role_profiles=[
            RoleProfile(
                name="Helper",
                description="Helps the user.",
                executor_provider="anthropic",
            )
        ],
    )
    wf = _instantiate(blueprint)
    prompt = _agent_prompt(wf)
    assert "Helper" in prompt
    assert "Helps the user" in prompt
    # No spurious empty section headers when responsibilities/constraints are blank.
    assert "Responsibilities:" not in prompt
    assert "Constraints:" not in prompt
