"""tests/test_builder_service.py — BuilderService generate / instantiate / list_kits (AC5)"""
from __future__ import annotations

import pytest

from xueban.runtime.contracts import WorkflowDefinition
from xueban.runtime.contracts_builder import AgentBlueprint, RoleProfile
from xueban.runtime.builder_service import (
    BuilderService,
    GenerateBlueprintRequest,
    InstantiateBlueprintRequest,
    SmokeRunBlueprintRequest,
    PublishBlueprintRequest,
)


@pytest.fixture
def svc() -> BuilderService:
    return BuilderService()


@pytest.fixture
def simple_blueprint() -> AgentBlueprint:
    return AgentBlueprint(
        name="research-agent",
        goal="Research the latest AI trends",
        mode="single",
        role_profiles=[RoleProfile(name="researcher", executor_provider="anthropic")],
    )


@pytest.fixture
def team_blueprint() -> AgentBlueprint:
    return AgentBlueprint(
        name="review-team",
        goal="Review and approve documents",
        mode="team",
        role_profiles=[
            RoleProfile(name="writer", executor_provider="anthropic"),
            RoleProfile(name="reviewer", executor_provider="anthropic"),
        ],
    )


# ---------------------------------------------------------------------------
# generate_blueprint
# ---------------------------------------------------------------------------


def test_generate_single_mode(svc: BuilderService):
    req = GenerateBlueprintRequest(goal="Write a summary", mode="single")
    resp = svc.generate_blueprint(req)

    assert resp.blueprint.mode == "single"
    assert len(resp.blueprint.role_profiles) == 1
    assert resp.blueprint.goal == "Write a summary"
    assert "confidence" in resp.meta
    assert resp.meta["suggested_next_step"] == "instantiate_blueprint"


def test_generate_team_mode(svc: BuilderService):
    req = GenerateBlueprintRequest(goal="Plan and execute", mode="team", desired_output="report")
    resp = svc.generate_blueprint(req)

    assert resp.blueprint.mode == "team"
    assert len(resp.blueprint.role_profiles) == 2


def test_generate_missing_inputs_listed(svc: BuilderService):
    req = GenerateBlueprintRequest(goal="test")
    resp = svc.generate_blueprint(req)
    assert "audience" in resp.meta["missing_inputs"]
    assert "desired_output" in resp.meta["missing_inputs"]


def test_generate_knowledge_sources(svc: BuilderService):
    req = GenerateBlueprintRequest(
        goal="Research AI",
        knowledge_sources=["https://example.com/paper"],
    )
    resp = svc.generate_blueprint(req)
    assert len(resp.blueprint.knowledge_bindings) == 1
    assert resp.blueprint.knowledge_bindings[0].source_type == "url"


# ---------------------------------------------------------------------------
# instantiate_blueprint — core AC2 requirement
# ---------------------------------------------------------------------------


def test_instantiate_produces_valid_workflow_definition(svc: BuilderService, simple_blueprint: AgentBlueprint):
    req = InstantiateBlueprintRequest(blueprint=simple_blueprint)
    resp = svc.instantiate_blueprint(req)

    # Must pass WorkflowDefinition.model_validate
    wf = WorkflowDefinition.model_validate(resp.workflow_definition)
    assert wf.workflow_id == simple_blueprint.blueprint_id
    assert wf.name == simple_blueprint.name
    assert len(wf.nodes) == 1


def test_instantiate_team_blueprint_multi_node(svc: BuilderService, team_blueprint: AgentBlueprint):
    req = InstantiateBlueprintRequest(blueprint=team_blueprint)
    resp = svc.instantiate_blueprint(req)

    wf = WorkflowDefinition.model_validate(resp.workflow_definition)
    assert len(wf.nodes) == 2
    # final edge leads to END
    assert any(e.to_id == "END" for e in wf.edges)


def test_instantiate_returns_template_spec(svc: BuilderService, simple_blueprint: AgentBlueprint):
    req = InstantiateBlueprintRequest(blueprint=simple_blueprint)
    resp = svc.instantiate_blueprint(req)

    assert "template_id" in resp.template_spec
    assert resp.template_spec["template_id"] == simple_blueprint.blueprint_id


def test_instantiate_can_spawn_adds_builtin_tool(svc: BuilderService):
    manager = RoleProfile(
        name="boss",
        executor_provider="anthropic",
        sub_agents=[RoleProfile(name="worker", executor_provider="anthropic")],
    )
    bp = AgentBlueprint(
        name="team",
        goal="manage work",
        mode="team",
        role_profiles=[manager, RoleProfile(name="worker", executor_provider="anthropic")],
    )
    req = InstantiateBlueprintRequest(blueprint=bp)
    resp = svc.instantiate_blueprint(req)

    wf = WorkflowDefinition.model_validate(resp.workflow_definition)
    manager_node = next(n for n in wf.nodes if n.id == manager.role_id)
    assert "builtin:spawn_task" in manager_node.config.get("tool_refs", [])


def test_instantiate_warnings_for_pending_features(svc: BuilderService):
    from xueban.runtime.contracts_builder import KnowledgeBinding, EvalProfile, PublishProfile
    bp = AgentBlueprint(
        name="full",
        goal="everything",
        mode="single",
        role_profiles=[RoleProfile(name="a", executor_provider="anthropic")],
        knowledge_bindings=[KnowledgeBinding(source_type="url", source_ref="https://x.com")],
        eval_profile=EvalProfile(smoke_eval_enabled=True),
        publish_profile=PublishProfile(target="template", visibility="team"),
    )
    req = InstantiateBlueprintRequest(blueprint=bp)
    resp = svc.instantiate_blueprint(req)

    assert any("knowledge_bindings" in w for w in resp.warnings)
    assert any("smoke_eval" in w for w in resp.warnings)
    # Patch 11: publish_profile warning removed — publish pipeline is now implemented (Story 8.6)


# ---------------------------------------------------------------------------
# list_kits
# ---------------------------------------------------------------------------


def test_list_kits_returns_stable_list(svc: BuilderService):
    kits = svc.list_kits()
    assert len(kits) == 4
    kit_ids = {k.kit_id for k in kits}
    assert "research" in kit_ids
    assert "knowledge_assistant" in kit_ids
    assert "review_approval" in kit_ids
    assert "persona_npc" in kit_ids


def test_list_kits_stable_on_repeated_calls(svc: BuilderService):
    first = svc.list_kits()
    second = svc.list_kits()
    assert [k.kit_id for k in first] == [k.kit_id for k in second]


# ---------------------------------------------------------------------------
# smoke_run — Story 8.5 comprehensive checks
# ---------------------------------------------------------------------------


def test_smoke_run_well_formed_blueprint_passes(svc: BuilderService, simple_blueprint: AgentBlueprint):
    req = SmokeRunBlueprintRequest(blueprint=simple_blueprint)
    resp = svc.smoke_run_blueprint(req)

    assert resp.status == "passed"
    assert resp.primary_blocker is None
    assert resp.summary != ""
    assert isinstance(resp.checks, list)
    assert len(resp.checks) == 6


def test_smoke_run_check_ids_present(svc: BuilderService, simple_blueprint: AgentBlueprint):
    req = SmokeRunBlueprintRequest(blueprint=simple_blueprint)
    resp = svc.smoke_run_blueprint(req)

    check_ids = {c.check_id for c in resp.checks}
    assert "role_init" in check_ids
    assert "tools_available" in check_ids
    assert "knowledge_accessible" in check_ids
    assert "min_task_loop" in check_ids
    assert "citation_check" in check_ids


def test_smoke_run_check_envelope_fields(svc: BuilderService, simple_blueprint: AgentBlueprint):
    req = SmokeRunBlueprintRequest(blueprint=simple_blueprint)
    resp = svc.smoke_run_blueprint(req)

    for check in resp.checks:
        assert check.check_id
        assert check.label
        assert check.status in ("passed", "failed", "warning", "skipped")
        assert check.reason


def test_smoke_run_short_goal_fails_goal_clarity(svc: BuilderService):
    from xueban.runtime.contracts_builder import RoleProfile as RP, AgentBlueprint as BP
    bp = BP(name="test", goal="No", mode="single", role_profiles=[RP(name="agent")])
    req = SmokeRunBlueprintRequest(blueprint=bp)
    resp = svc.smoke_run_blueprint(req)

    min_task = next(c for c in resp.checks if c.check_id == "min_task_loop")
    assert min_task.status == "failed"
    assert min_task.failure_category == "goal_clarity"
    assert resp.status == "failed"
    assert resp.primary_blocker is not None
    assert resp.recommended_fix is not None


def test_smoke_run_disabled_tool_fails_tool_permission(svc: BuilderService):
    from xueban.runtime.contracts_builder import (
        RoleProfile as RP, AgentBlueprint as BP, ToolPolicy as TP
    )
    role = RP(name="agent", description="Searches the web", tools=["builtin:web_search"])
    policy = TP(tool_id="builtin:web_search", visibility="disabled")
    bp = BP(
        name="test",
        goal="Research the latest trends in AI systems",
        mode="single",
        role_profiles=[role],
        tool_policies=[policy],
    )
    req = SmokeRunBlueprintRequest(blueprint=bp)
    resp = svc.smoke_run_blueprint(req)

    tools_check = next(c for c in resp.checks if c.check_id == "tools_available")
    assert tools_check.status == "failed"
    assert tools_check.failure_category == "tool_permission"
    assert tools_check.target_ref == "tool_registry"


def test_smoke_run_unspecified_knowledge_passes_with_note(svc: BuilderService):
    from xueban.runtime.contracts_builder import (
        RoleProfile as RP, AgentBlueprint as BP, KnowledgeBinding as KB
    )
    bp = BP(
        name="test",
        goal="Summarise document contents for review",
        mode="single",
        role_profiles=[RP(name="agent")],
        knowledge_bindings=[KB(source_type="unspecified")],
    )
    req = SmokeRunBlueprintRequest(blueprint=bp)
    resp = svc.smoke_run_blueprint(req)

    knowledge_check = next(c for c in resp.checks if c.check_id == "knowledge_accessible")
    assert knowledge_check.status == "passed"
    assert "暂不绑定知识" in knowledge_check.reason or "不要求" in knowledge_check.reason


def test_smoke_run_real_knowledge_binding_passes(svc: BuilderService):
    from xueban.runtime.contracts_builder import (
        RoleProfile as RP, AgentBlueprint as BP, KnowledgeBinding as KB
    )
    bp = BP(
        name="test",
        goal="Answer questions about our product documentation",
        mode="single",
        role_profiles=[RP(name="agent")],
        knowledge_bindings=[KB(source_type="url", source_ref="https://docs.example.com")],
    )
    req = SmokeRunBlueprintRequest(blueprint=bp)
    resp = svc.smoke_run_blueprint(req)

    knowledge_check = next(c for c in resp.checks if c.check_id == "knowledge_accessible")
    assert knowledge_check.status == "passed"


def test_smoke_run_citation_required_triggers_check(svc: BuilderService):
    from xueban.runtime.contracts_builder import (
        RoleProfile as RP, AgentBlueprint as BP, KnowledgeBinding as KB
    )
    bp = BP(
        name="test",
        goal="Write a report with citations from our knowledge base",
        mode="single",
        role_profiles=[RP(name="agent")],
        knowledge_bindings=[KB(source_type="url", source_ref="https://docs.example.com", citation_required=True)],
    )
    req = SmokeRunBlueprintRequest(blueprint=bp)
    resp = svc.smoke_run_blueprint(req)

    citation_check = next(c for c in resp.checks if c.check_id == "citation_check")
    assert citation_check.status in ("passed", "warning")
    assert citation_check.status != "skipped"


def test_smoke_run_no_citation_required_skips_check(svc: BuilderService, simple_blueprint: AgentBlueprint):
    req = SmokeRunBlueprintRequest(blueprint=simple_blueprint)
    resp = svc.smoke_run_blueprint(req)

    citation_check = next(c for c in resp.checks if c.check_id == "citation_check")
    assert citation_check.status == "skipped"


def test_smoke_run_primary_blocker_is_first_failed_check(svc: BuilderService):
    from xueban.runtime.contracts_builder import RoleProfile as RP, AgentBlueprint as BP
    bp = BP(name="test", goal="No", mode="single", role_profiles=[RP(name="agent")])
    req = SmokeRunBlueprintRequest(blueprint=bp)
    resp = svc.smoke_run_blueprint(req)

    failed = [c for c in resp.checks if c.status == "failed"]
    assert resp.primary_blocker == failed[0].check_id


def test_smoke_run_translation_returns_user_friendly_fix(svc: BuilderService):
    from xueban.runtime.contracts_builder import RoleProfile as RP, AgentBlueprint as BP
    bp = BP(name="test", goal="No", mode="single", role_profiles=[RP(name="agent")])
    req = SmokeRunBlueprintRequest(blueprint=bp)
    resp = svc.smoke_run_blueprint(req)

    assert resp.recommended_fix
    # Must NOT contain raw Python/infra jargon
    assert "traceback" not in resp.recommended_fix.lower()
    assert "422" not in resp.recommended_fix
    assert "HTTP" not in resp.recommended_fix


def test_smoke_run_warning_status_aggregated_correctly(svc: BuilderService):
    from xueban.runtime.contracts_builder import (
        RoleProfile as RP, AgentBlueprint as BP, ToolPolicy as TP
    )
    role = RP(name="agent", description="Uses web search to answer", tools=["builtin:web_search"])
    bp = BP(
        name="test",
        goal="Search for information about recent AI research",
        mode="single",
        role_profiles=[role],
        tool_policies=[],  # no explicit policy → warning
    )
    req = SmokeRunBlueprintRequest(blueprint=bp)
    resp = svc.smoke_run_blueprint(req)

    tools_check = next(c for c in resp.checks if c.check_id == "tools_available")
    assert tools_check.status == "warning"
    assert resp.status in ("passed", "warning")


def test_publish_returns_structured_result(svc: BuilderService, simple_blueprint: AgentBlueprint, tmp_path):
    """Story 8.6: publish_blueprint now performs real backfill (not a placeholder)."""
    req = PublishBlueprintRequest(blueprint=simple_blueprint)
    from unittest.mock import patch
    with (
        patch("xueban.runtime.builder_service._CUSTOM_TEMPLATE_DIR", tmp_path / "templates/custom"),
        patch("xueban.runtime.builder_service._WORKFLOW_DIR", tmp_path / ".xueban/workflows"),
    ):
        resp = svc.publish_blueprint(req)

    assert resp.publish_status == "published"
    assert resp.template_id.startswith("bldr-")
    assert resp.workflow_id  # non-empty
    assert resp.links.templates == "/templates"
