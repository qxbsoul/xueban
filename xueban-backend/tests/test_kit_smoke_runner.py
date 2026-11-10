"""Story 10.6 — KitSmokeRunner tests (AC1, AC6, AC7)."""
from __future__ import annotations

import asyncio

import pytest

from xueban.runtime.contracts_builder import AgentBlueprint, RoleProfile
from xueban.runtime.kits.evals import (
    KitSmokeRunner,
    SmokeRunOptions,
    SmokeRunReport,
)
from xueban.runtime.kits.evals.runner import (
    KitSmokeEvalPack,
    SmokeCase,
    register_eval_pack,
)
from xueban.runtime.kits.research_kit import (
    ResearchGoalInputs,
    create_research_blueprint,
)


def _make_blueprint() -> AgentBlueprint:
    """Realistic research_kit blueprint via factory — required since
    Round 1 closeout: kit eval executors now actually read role_profiles
    and metadata, a stub solo blueprint would fail every executor."""
    return create_research_blueprint(
        ResearchGoalInputs(
            research_topic="AI 在医疗领域的应用",
            max_search_rounds=3,
            citation_required=True,
        )
    )


@pytest.mark.asyncio
async def test_run_smoke_returns_structured_report() -> None:
    runner = KitSmokeRunner()
    bp = _make_blueprint()
    report = await runner.run_smoke("research_kit", bp, SmokeRunOptions(mock_llm=True))
    assert isinstance(report, SmokeRunReport)
    assert report.kit_id == "research_kit"
    assert len(report.case_results) >= 2
    assert all(c.duration_s >= 0 for c in report.case_results)
    assert report.passed is True


@pytest.mark.asyncio
async def test_run_smoke_unknown_kit_returns_error_report() -> None:
    runner = KitSmokeRunner()
    bp = _make_blueprint()
    report = await runner.run_smoke("not_a_real_kit", bp, SmokeRunOptions())
    assert report.passed is False
    assert report.error == "no_eval_pack"
    assert report.failed_stage == "eval_pack_missing"


@pytest.mark.asyncio
async def test_run_smoke_total_timeout() -> None:
    """If a case sleeps longer than the per-case timeout it must error out
    with `error="timeout"` and the overall report must reflect that."""
    runner = KitSmokeRunner()
    bp = _make_blueprint()

    async def _slow(_bp, _opts):  # pragma: no cover - exercised via runner
        await asyncio.sleep(2.0)
        return {"passed": True}

    register_eval_pack(
        KitSmokeEvalPack(
            kit_id="slow_test_kit",
            smoke_cases=[SmokeCase(name="slow_case", executor=_slow)],
        )
    )
    report = await runner.run_smoke(
        "slow_test_kit",
        bp,
        SmokeRunOptions(case_timeout_s=0.1, timeout_s=5.0),
    )
    assert report.passed is False
    assert report.case_results[0].error == "timeout"


@pytest.mark.asyncio
async def test_run_smoke_total_timeout_truncated() -> None:
    """Overall timeout aborts the run with passed=False, error='timeout'."""
    runner = KitSmokeRunner()
    bp = _make_blueprint()

    async def _slow(_bp, _opts):
        await asyncio.sleep(5.0)
        return {"passed": True}

    register_eval_pack(
        KitSmokeEvalPack(
            kit_id="hang_test_kit",
            smoke_cases=[
                SmokeCase(name="hang", executor=_slow),
                SmokeCase(name="hang2", executor=_slow),
            ],
        )
    )
    report = await runner.run_smoke(
        "hang_test_kit",
        bp,
        SmokeRunOptions(case_timeout_s=10.0, timeout_s=0.1),
    )
    assert report.passed is False
    assert report.error == "timeout"


@pytest.mark.asyncio
async def test_run_smoke_alias_kit_id() -> None:
    """Short alias like 'research' should resolve to 'research_kit'."""
    runner = KitSmokeRunner()
    bp = _make_blueprint()
    report = await runner.run_smoke("research", bp, SmokeRunOptions())
    assert report.passed is True


@pytest.mark.asyncio
async def test_baseline_auto_save_first_run_then_compare(tmp_path) -> None:
    """Story 10.6 M2: first successful smoke → save_baseline → next run compares.

    This exercises the runner-level idempotent baseline lifecycle that the
    publish handler now wires up:
      1. load_baseline returns None (clean slate, isolated tmp dir)
      2. run_regression returns baseline_timestamp=None on first run
      3. save_baseline persists; load_baseline now resolves
      4. second run_regression now has baseline_comparison populated
    """
    runner = KitSmokeRunner(baseline_dir=tmp_path)
    bp = _make_blueprint()

    # Use an isolated test kit to decouple from real EvalPack semantics
    # (sibling agents own the per-kit executors).
    kit_id = "baseline_lifecycle_test_kit"

    async def _always_pass(_bp, _opts):
        return {"passed": True, "metrics": {"x": 1.0}, "citation_present": True}

    register_eval_pack(
        KitSmokeEvalPack(
            kit_id=kit_id,
            smoke_cases=[SmokeCase(name="ok_case", executor=_always_pass)],
        )
    )

    # Step 1: no baseline yet
    assert runner.load_baseline(kit_id) is None

    # Step 2: first regression run — no baseline_timestamp
    report1 = await runner.run_regression(
        kit_id, bp, options=SmokeRunOptions(mock_llm=True)
    )
    assert report1.baseline_timestamp is None
    assert report1.current is not None
    assert report1.current.passed is True
    # publish handler would call this branch — simulate it
    saved_path = runner.save_baseline(report1.current)
    assert saved_path.exists()

    # Step 3: idempotent re-save (overwrite) does not raise
    runner.save_baseline(report1.current)

    # Step 4: now load_baseline resolves
    loaded = runner.load_baseline(kit_id)
    assert loaded is not None
    assert loaded.kit_id == report1.current.kit_id

    # Step 5: second run_regression now compares against baseline
    report2 = await runner.run_regression(
        kit_id, bp, options=SmokeRunOptions(mock_llm=True)
    )
    assert report2.baseline_timestamp is not None
    assert len(report2.baseline_comparison) > 0
    # H4 fix: first_pass_rate should NOT appear as a comparator metric
    metrics_compared = {d.metric for d in report2.baseline_comparison}
    assert "first_pass_rate" not in metrics_compared
    assert "smoke_pass_rate" in metrics_compared
