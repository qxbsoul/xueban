"""Story 10.6 — Eval pack contents tests (AC2, AC3, AC7)."""
from __future__ import annotations

import pytest

from xueban.runtime.contracts_builder import (
    AgentBlueprint,
    KnowledgeBinding,
    RoleProfile,
)
from xueban.runtime.kits.evals import KitSmokeRunner, SmokeRunOptions, get_eval_pack
from xueban.runtime.kits.evals.knowledge_assistant_kit_eval import (
    FABRICATION_HINTS,
)
from xueban.runtime.kits.research_kit import (
    ResearchGoalInputs,
    create_research_blueprint,
)
from xueban.runtime.kits.knowledge_assistant_kit import (
    KnowledgeAssistantGoalInputs,
    create_knowledge_assistant_blueprint,
)


def _ka_bp(*, with_pack: bool) -> AgentBlueprint:
    """Build a Knowledge Assistant blueprint via the canonical factory."""
    inputs = KnowledgeAssistantGoalInputs(
        knowledge_source="existing_pack" if with_pack else "none",
        pack_id="kb-1" if with_pack else None,
        citation_required=True,
        low_confidence_strategy="escalate_human",
        escalation_keywords=[],
        assistant_name="Test Assistant",
    )
    return create_knowledge_assistant_blueprint(inputs)


def _research_bp(**overrides) -> AgentBlueprint:
    inputs = ResearchGoalInputs(
        research_topic=overrides.pop("research_topic", "AI 在医疗领域的应用"),
        output_format=overrides.pop("output_format", "report"),
        freshness=overrides.pop("freshness", "any"),
        citation_required=overrides.pop("citation_required", True),
        max_search_rounds=overrides.pop("max_search_rounds", 2),
    )
    return create_research_blueprint(inputs)


def _bp(knowledge: bool = False) -> AgentBlueprint:
    bindings = (
        [KnowledgeBinding(source_type="pack", source_ref="kb-1", citation_required=True)]
        if knowledge
        else []
    )
    return AgentBlueprint(
        name="kit-test",
        goal="evaluate kit smoke pack content thoroughly",
        mode="single",
        role_profiles=[RoleProfile(name="agent", description="solo")],
        knowledge_bindings=bindings,
    )


def test_each_pack_registered_with_required_cases() -> None:
    expected = {
        "research_kit": (2, 1),
        "knowledge_assistant_kit": (3, 1),
        "review_approval_kit": (3, 1),
        "persona_npc_kit": (3, 1),
    }
    for kit_id, (smoke_n, regression_n) in expected.items():
        pack = get_eval_pack(kit_id)
        assert pack is not None, f"missing pack for {kit_id}"
        assert len(pack.smoke_cases) == smoke_n
        assert len(pack.regression_cases) == regression_n


@pytest.mark.asyncio
async def test_research_kit_min_loop_passes_with_5_fields() -> None:
    runner = KitSmokeRunner()
    report = await runner.run_smoke("research_kit", _research_bp(), SmokeRunOptions())
    by_name = {c.name: c for c in report.case_results}
    assert by_name["research_min_loop"].passed is True
    assert by_name["research_min_loop"].metrics.get("fields_present") == 5.0


# ---------------------------------------------------------------------------
# Reverse / negative tests for Research Kit (Round-5 review C1 contract).
# These prove the executors actually read blueprint fields rather than
# returning canned pass verdicts.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_research_min_loop_fails_when_writer_missing() -> None:
    bp = _research_bp()
    bp.role_profiles = [
        r for r in bp.role_profiles
        if (r.metadata or {}).get("role_type") != "report_writer"
    ]
    runner = KitSmokeRunner()
    report = await runner.run_smoke("research_kit", bp, SmokeRunOptions())
    by_name = {c.name: c for c in report.case_results}
    res = by_name["research_min_loop"]
    assert res.passed is False
    assert any("report_writer" in m for m in res.missing_configs)


@pytest.mark.asyncio
async def test_research_min_loop_fails_when_role_profiles_empty() -> None:
    bp = _research_bp()
    bp.role_profiles = []
    runner = KitSmokeRunner()
    report = await runner.run_smoke("research_kit", bp, SmokeRunOptions())
    by_name = {c.name: c for c in report.case_results}
    res = by_name["research_min_loop"]
    assert res.passed is False
    assert res.failed_stage == "blueprint"


@pytest.mark.asyncio
async def test_research_min_loop_fails_with_solo_blueprint() -> None:
    runner = KitSmokeRunner()
    report = await runner.run_smoke("research_kit", _bp(), SmokeRunOptions())
    by_name = {c.name: c for c in report.case_results}
    assert by_name["research_min_loop"].passed is False


@pytest.mark.asyncio
async def test_research_min_loop_fails_with_invalid_max_search_rounds() -> None:
    bp = _research_bp()
    bp.metadata["max_search_rounds"] = 99  # outside 1..5
    runner = KitSmokeRunner()
    report = await runner.run_smoke("research_kit", bp, SmokeRunOptions())
    by_name = {c.name: c for c in report.case_results}
    res = by_name["research_min_loop"]
    assert res.passed is False
    assert any("max_search_rounds" in m for m in res.missing_configs)


@pytest.mark.asyncio
async def test_citation_integrity_fails_without_researcher() -> None:
    bp = _research_bp(citation_required=True)
    bp.knowledge_bindings = [
        KnowledgeBinding(source_type="pack", source_ref="kb-1", citation_required=True)
    ]
    bp.role_profiles = [
        r for r in bp.role_profiles
        if (r.metadata or {}).get("role_type") != "researcher"
    ]
    runner = KitSmokeRunner()
    report = await runner.run_smoke("research_kit", bp, SmokeRunOptions())
    by_name = {c.name: c for c in report.case_results}
    res = by_name["citation_integrity"]
    assert res.passed is False
    assert res.failed_stage == "citation_check"
    assert any("Researcher" in m for m in res.missing_configs)


@pytest.mark.asyncio
async def test_citation_integrity_fails_when_researcher_lacks_citation_capability() -> None:
    bp = _research_bp(citation_required=True)
    bp.knowledge_bindings = [
        KnowledgeBinding(source_type="pack", source_ref="kb-1", citation_required=True)
    ]
    for r in bp.role_profiles:
        if (r.metadata or {}).get("role_type") == "researcher":
            r.tools = []
            r.metadata = {**(r.metadata or {}), "citation_required": False}
    runner = KitSmokeRunner()
    report = await runner.run_smoke("research_kit", bp, SmokeRunOptions())
    by_name = {c.name: c for c in report.case_results}
    res = by_name["citation_integrity"]
    assert res.passed is False
    assert any("引用" in m or "citation" in m.lower() for m in res.missing_configs)


@pytest.mark.asyncio
async def test_knowledge_assistant_no_source_reject_no_fabrication() -> None:
    runner = KitSmokeRunner()
    report = await runner.run_smoke(
        "knowledge_assistant_kit", _ka_bp(with_pack=True), SmokeRunOptions()
    )
    by_name = {c.name: c for c in report.case_results}
    assert by_name["no_source_reject"].passed is True
    # Sanity: fabrication keyword list non-empty (used by the executor)
    assert FABRICATION_HINTS


@pytest.mark.asyncio
async def test_knowledge_assistant_doc_hit_path_requires_pack() -> None:
    runner = KitSmokeRunner()
    # No knowledge binding → doc_hit_path should fail with KnowledgePack missing
    report = await runner.run_smoke(
        "knowledge_assistant_kit", _ka_bp(with_pack=False), SmokeRunOptions()
    )
    by_name = {c.name: c for c in report.case_results}
    assert by_name["doc_hit_path"].passed is False
    assert any("KnowledgePack" in m for m in by_name["doc_hit_path"].missing_configs)
    assert any(
        f.target == "knowledge_dock" for f in by_name["doc_hit_path"].suggested_fixes
    )


@pytest.mark.asyncio
async def test_review_approval_happy_path_passes() -> None:
    """Round-1 fix (C1/C2): use real Kit blueprint + verify all 3 cases pass."""
    from xueban.runtime.kits.review_approval_kit import (
        ReviewApprovalGoalInputs,
        create_review_approval_blueprint,
    )

    bp = create_review_approval_blueprint(
        ReviewApprovalGoalInputs(approval_levels="review_then_approve")
    )
    runner = KitSmokeRunner()
    report = await runner.run_smoke("review_approval_kit", bp, SmokeRunOptions())
    by_name = {c.name: c for c in report.case_results}
    assert by_name["happy_path"].passed is True, by_name["happy_path"].detail
    assert by_name["approval_visible"].passed is True, by_name["approval_visible"].detail
    assert by_name["reject_rework"].passed is True, by_name["reject_rework"].detail


@pytest.mark.asyncio
async def test_review_approval_minimal_bp_fails_all_three() -> None:
    """Reverse test: stub blueprint without Kit roles must FAIL every case."""
    runner = KitSmokeRunner()
    report = await runner.run_smoke("review_approval_kit", _bp(), SmokeRunOptions())
    by_name = {c.name: c for c in report.case_results}
    assert by_name["happy_path"].passed is False
    assert by_name["reject_rework"].passed is False
    assert by_name["approval_visible"].passed is False


@pytest.mark.asyncio
async def test_review_approval_reverse_remove_approver_fails() -> None:
    """Remove the Approver role → happy_path & reject_rework must FAIL."""
    from xueban.runtime.kits.review_approval_kit import (
        ReviewApprovalGoalInputs,
        create_review_approval_blueprint,
    )

    bp = create_review_approval_blueprint(
        ReviewApprovalGoalInputs(approval_levels="review_then_approve")
    )
    bp.role_profiles = [
        r
        for r in bp.role_profiles
        if (r.metadata or {}).get("role_type") != "approver"
    ]
    runner = KitSmokeRunner()
    report = await runner.run_smoke("review_approval_kit", bp, SmokeRunOptions())
    by_name = {c.name: c for c in report.case_results}
    assert by_name["happy_path"].passed is False
    assert by_name["reject_rework"].passed is False
    assert any(
        "approver" in m.lower() for m in by_name["happy_path"].missing_configs
    )


@pytest.mark.asyncio
async def test_review_approval_reverse_zero_max_rounds_fails_reject_rework() -> None:
    """Force retry_policy.max_rounds=0 → reject_rework must FAIL."""
    from xueban.runtime.kits.review_approval_kit import (
        ReviewApprovalGoalInputs,
        create_review_approval_blueprint,
    )

    bp = create_review_approval_blueprint(
        ReviewApprovalGoalInputs(approval_levels="review_then_approve")
    )
    bp.metadata["retry_policy"] = {"max_rounds": 0, "on_exceed": "escalated"}
    bp.metadata["max_reject_rounds"] = 0
    runner = KitSmokeRunner()
    report = await runner.run_smoke("review_approval_kit", bp, SmokeRunOptions())
    by_name = {c.name: c for c in report.case_results}
    assert by_name["reject_rework"].passed is False
    assert by_name["reject_rework"].failed_stage == "retry_policy"


@pytest.mark.asyncio
async def test_review_approval_reverse_no_gate_nodes_fails_visibility() -> None:
    """Remove approval_gate_nodes → approval_visible must FAIL."""
    from xueban.runtime.kits.review_approval_kit import (
        ReviewApprovalGoalInputs,
        create_review_approval_blueprint,
    )

    bp = create_review_approval_blueprint(
        ReviewApprovalGoalInputs(approval_levels="review_then_approve")
    )
    bp.metadata["approval_gate_nodes"] = []
    runner = KitSmokeRunner()
    report = await runner.run_smoke("review_approval_kit", bp, SmokeRunOptions())
    by_name = {c.name: c for c in report.case_results}
    assert by_name["approval_visible"].passed is False
    assert by_name["approval_visible"].failed_stage == "approval_gate_nodes"


@pytest.mark.asyncio
async def test_persona_npc_state_update_three_turns() -> None:
    """Use the real persona_npc_kit factory blueprint — executors now require
    persona_traits / state_fields / memory_retention to be present on the
    blueprint."""
    from xueban.runtime.kits.persona_npc_kit import (
        PersonaNPCGoalInputs,
        create_persona_npc_blueprint,
    )

    bp = create_persona_npc_blueprint(
        PersonaNPCGoalInputs(
            persona_name="Aria",
            personality="温柔、善解人意、神秘",
            memory_retention="balanced",
        )
    )
    runner = KitSmokeRunner()
    report = await runner.run_smoke("persona_npc_kit", bp, SmokeRunOptions())
    by_name = {c.name: c for c in report.case_results}
    assert by_name["state_update"].metrics.get("interaction_count") == 3.0
    assert by_name["memory_cross_turn"].passed is True
    assert by_name["persona_tone_stability"].passed is True
