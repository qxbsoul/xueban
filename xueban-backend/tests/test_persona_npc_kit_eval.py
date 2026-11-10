"""Reverse tests for persona_npc_kit_eval — Story 10-4 C1 fix.

Verify that each smoke executor *FAILS* when its required blueprint config
is missing / empty / disabled — i.e. the executor genuinely reads the
blueprint instead of returning a constant verdict.
"""
from __future__ import annotations

import pytest

from xueban.runtime.contracts_builder import (
    AgentBlueprint,
    MemoryProfile,
    RoleProfile,
    StateField,
)
from xueban.runtime.kits.evals import (
    KitSmokeRunner,
    SmokeRunOptions,
)
from xueban.runtime.kits.persona_npc_kit import (
    PersonaNPCGoalInputs,
    create_persona_npc_blueprint,
)


def _good_bp() -> AgentBlueprint:
    return create_persona_npc_blueprint(
        PersonaNPCGoalInputs(
            persona_name="Aria",
            personality="温柔、善解人意、神秘",
            memory_retention="balanced",
        )
    )


# ---------------------------------------------------------------------------
# persona_tone_stability — must read role_profiles[0].persona_traits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persona_tone_fails_when_persona_traits_empty() -> None:
    bp = _good_bp()
    # Wipe persona_traits → executor must FAIL.
    bp.role_profiles[0].persona_traits = {}
    bp.metadata = {**(bp.metadata or {}), "persona_traits": {}}

    runner = KitSmokeRunner()
    report = await runner.run_smoke("persona_npc_kit", bp, SmokeRunOptions())
    by_name = {c.name: c for c in report.case_results}
    case = by_name["persona_tone_stability"]
    assert case.passed is False
    assert any("persona_traits" in m for m in case.missing_configs)


@pytest.mark.asyncio
async def test_persona_tone_passes_with_gentle_traits() -> None:
    bp = _good_bp()
    runner = KitSmokeRunner()
    report = await runner.run_smoke("persona_npc_kit", bp, SmokeRunOptions())
    by_name = {c.name: c for c in report.case_results}
    assert by_name["persona_tone_stability"].passed is True


@pytest.mark.asyncio
async def test_persona_tone_distinguishes_cold_persona() -> None:
    """A cold/harsh persona should NOT pass against gentle bot replies — the
    derived keywords differ. This proves the executor is persona-aware."""
    bp = _good_bp()
    bp.role_profiles[0].persona_traits = {
        "trait_1": "冷酷",
        "trait_2": "暴躁",
    }
    runner = KitSmokeRunner()
    report = await runner.run_smoke("persona_npc_kit", bp, SmokeRunOptions())
    by_name = {c.name: c for c in report.case_results}
    case = by_name["persona_tone_stability"]
    # Bot replies are gentle ("请/您/可以") → cold-persona keywords
    # ("不/拒绝/不行/别/冷酷/暴躁") have low coverage → should fail.
    assert case.passed is False, (
        f"cold persona unexpectedly passed against gentle replies: {case.detail}"
    )


# ---------------------------------------------------------------------------
# state_update — must read role_profiles[0].state_fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_update_fails_when_state_fields_missing() -> None:
    bp = _good_bp()
    bp.role_profiles[0].state_fields = []
    # Also clear metadata fallbacks.
    if bp.metadata:
        bp.metadata.pop("initial_state_fields", None)
        bp.metadata.pop("state_fields", None)

    runner = KitSmokeRunner()
    report = await runner.run_smoke("persona_npc_kit", bp, SmokeRunOptions())
    by_name = {c.name: c for c in report.case_results}
    case = by_name["state_update"]
    assert case.passed is False
    assert any("state_fields" in m for m in case.missing_configs)


@pytest.mark.asyncio
async def test_state_update_fails_when_interaction_count_missing() -> None:
    bp = _good_bp()
    # Keep state_fields but drop interaction_count.
    bp.role_profiles[0].state_fields = [
        StateField(name="mood", type="string", default="neutral"),
    ]
    if bp.metadata:
        bp.metadata.pop("initial_state_fields", None)

    runner = KitSmokeRunner()
    report = await runner.run_smoke("persona_npc_kit", bp, SmokeRunOptions())
    by_name = {c.name: c for c in report.case_results}
    case = by_name["state_update"]
    assert case.passed is False
    assert any("interaction_count" in m for m in case.missing_configs)


# ---------------------------------------------------------------------------
# memory_cross_turn — must read memory retention config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_cross_turn_fails_when_retention_none() -> None:
    bp = _good_bp()
    bp.metadata = {**(bp.metadata or {}), "memory_retention": "none", "memory_preset": {}}
    bp.memory_profile = MemoryProfile(
        scope="user",
        writeback_target="memory",
        enabled=False,
        metadata={"working_memory_limit": 0},
    )

    runner = KitSmokeRunner()
    report = await runner.run_smoke("persona_npc_kit", bp, SmokeRunOptions())
    by_name = {c.name: c for c in report.case_results}
    case = by_name["memory_cross_turn"]
    assert case.passed is False
    # Either the explicit 'none' branch or the working_memory_limit==0 branch.
    msg = " ".join(case.missing_configs)
    assert ("none" in msg) or ("working_memory_limit" in msg), (
        f"unexpected missing_configs: {case.missing_configs}"
    )


@pytest.mark.asyncio
async def test_memory_cross_turn_fails_when_working_limit_zero() -> None:
    bp = _good_bp()
    bp.metadata = {
        **(bp.metadata or {}),
        "memory_preset": {"working_memory_limit": 0},
    }
    bp.memory_profile = MemoryProfile(
        scope="user",
        writeback_target="memory",
        enabled=True,
        metadata={"working_memory_limit": 0},
    )
    runner = KitSmokeRunner()
    report = await runner.run_smoke("persona_npc_kit", bp, SmokeRunOptions())
    by_name = {c.name: c for c in report.case_results}
    case = by_name["memory_cross_turn"]
    assert case.passed is False
    assert any("working_memory_limit" in m for m in case.missing_configs)


@pytest.mark.asyncio
async def test_memory_cross_turn_passes_with_balanced_preset() -> None:
    bp = _good_bp()
    runner = KitSmokeRunner()
    report = await runner.run_smoke("persona_npc_kit", bp, SmokeRunOptions())
    by_name = {c.name: c for c in report.case_results}
    case = by_name["memory_cross_turn"]
    assert case.passed is True
    assert case.metrics.get("working_memory_limit", 0) > 0
