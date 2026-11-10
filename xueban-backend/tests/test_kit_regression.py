"""Story 10.6 — Regression baseline / verdict tests (AC4, AC7)."""
from __future__ import annotations

from pathlib import Path

import pytest

from xueban.runtime.contracts_builder import AgentBlueprint, RoleProfile
from xueban.runtime.kits.evals.runner import (
    KitSmokeRunner,
    SmokeCaseResult,
    SmokeRunReport,
)
from xueban.runtime.kits.research_kit import (
    ResearchGoalInputs,
    create_research_blueprint,
)


def _bp() -> AgentBlueprint:
    """Real research_kit blueprint — Round 1 closeout: kit executors
    now read role_profiles/metadata, so stub blueprints no longer pass."""
    return create_research_blueprint(
        ResearchGoalInputs(
            research_topic="AI 在医疗领域的应用",
            max_search_rounds=3,
            citation_required=True,
        )
    )


def _build_report(pass_rate: float, latency_ms: float = 100.0) -> SmokeRunReport:
    n = 4
    passed = round(pass_rate * n)
    cases = [
        SmokeCaseResult(name=f"c{i}", passed=i < passed, duration_s=latency_ms / 1000)
        for i in range(n)
    ]
    return SmokeRunReport(
        kit_id="dummy_kit",
        passed=all(c.passed for c in cases),
        case_results=cases,
        summary_metrics={
            "smoke_pass_rate": pass_rate,
            "first_pass_rate": pass_rate,
            "citation_pass_rate": 1.0,
            "avg_latency_ms": latency_ms,
        },
        duration_s=latency_ms / 1000 * n,
        timestamp="2026-04-28T00:00:00Z",
    )


def test_compare_first_run_no_baseline_returns_pass() -> None:
    runner = KitSmokeRunner()
    current = _build_report(1.0)
    result = runner._compare("dummy_kit", current, baseline=None)
    assert result.verdict == "pass"
    assert result.regressions_detected is False


def test_compare_smoke_pass_rate_drop_to_75_percent_warning() -> None:
    runner = KitSmokeRunner()
    baseline = _build_report(1.0)
    current = _build_report(0.75)
    result = runner._compare("dummy_kit", current, baseline)
    assert result.verdict == "warning"
    assert any("smoke 通过率" in r for r in result.reasons)


def test_compare_smoke_pass_rate_drop_to_60_percent_block() -> None:
    runner = KitSmokeRunner()
    baseline = _build_report(1.0)
    current = _build_report(0.60)
    result = runner._compare("dummy_kit", current, baseline)
    assert result.verdict == "block"
    assert result.regressions_detected is True


def test_compare_latency_rise_block() -> None:
    runner = KitSmokeRunner()
    baseline = _build_report(1.0, latency_ms=100.0)
    current = _build_report(1.0, latency_ms=400.0)  # +300%
    result = runner._compare("dummy_kit", current, baseline)
    assert result.verdict == "block"
    assert any("时延" in r for r in result.reasons)


def test_save_and_load_baseline_roundtrip(tmp_path: Path) -> None:
    runner = KitSmokeRunner(baseline_dir=tmp_path)
    report = _build_report(1.0)
    runner.save_baseline(report)
    loaded = runner.load_baseline(report.kit_id)
    assert loaded is not None
    assert loaded.kit_id == report.kit_id
    assert loaded.summary_metrics["smoke_pass_rate"] == 1.0


def test_load_baseline_missing_returns_none(tmp_path: Path) -> None:
    runner = KitSmokeRunner(baseline_dir=tmp_path)
    assert runner.load_baseline("never_saved") is None


@pytest.mark.asyncio
async def test_run_regression_first_time_no_baseline() -> None:
    runner = KitSmokeRunner()
    bp = _bp()
    report = await runner.run_regression("research_kit", bp, baseline=None)
    assert report.verdict == "pass"
    assert report.current is not None
    assert report.current.passed is True
