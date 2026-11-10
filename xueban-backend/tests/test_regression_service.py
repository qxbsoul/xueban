"""Unit tests for RegressionService — Story 9-6."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from xueban.runtime.regression_service import (
    GateResult,
    RegressionBaseline,
    RegressionReport,
    RegressionService,
)


@pytest.fixture()
def svc(tmp_path: Path) -> RegressionService:
    """Return a RegressionService that writes to tmp_path."""
    import xueban.runtime.regression_service as _mod

    original_dir = _mod._BASELINES_DIR
    _mod._BASELINES_DIR = tmp_path / "regression_baselines"
    yield RegressionService()
    _mod._BASELINES_DIR = original_dir


def _make_baseline(svc: RegressionService, blueprint_id: str = "bp-1", **kwargs) -> RegressionBaseline:
    defaults = dict(
        result_id="result-001",
        eval_profile_id="profile-001",
        metrics_snapshot={"accuracy": 0.9, "f1": 0.85},
        overall_pass=True,
    )
    defaults.update(kwargs)
    return svc.save_baseline(blueprint_id=blueprint_id, **defaults)


# ---------------------------------------------------------------------------
# compare() tests
# ---------------------------------------------------------------------------


def test_compare_critical_when_delta_gt_10(svc: RegressionService) -> None:
    """baseline=1.0, current=0.85 → delta=-15% → status='critical'."""
    baseline = _make_baseline(svc, metrics_snapshot={"accuracy": 1.0})
    report = svc.compare(
        current_metrics={"accuracy": 0.85},
        current_result_id="run-x",
        current_latency_ms=0,
        current_tokens=0,
        baseline=baseline,
    )
    assert len(report.metric_diffs) == 1
    diff = report.metric_diffs[0]
    assert diff.metric_id == "accuracy"
    assert diff.status == "critical"
    assert diff.delta == pytest.approx(-15.0, abs=0.1)
    assert report.overall_status == "blocked"
    assert len(report.blocking_reasons) == 1


def test_compare_regressed_when_delta_between_5_and_10(svc: RegressionService) -> None:
    """baseline=1.0, current=0.93 → delta=-7% → status='regressed'."""
    baseline = _make_baseline(svc, metrics_snapshot={"accuracy": 1.0})
    report = svc.compare({"accuracy": 0.93}, "run-x", 0, 0, baseline)
    diff = report.metric_diffs[0]
    assert diff.status == "regressed"
    assert report.overall_status == "warning"


def test_compare_stable_when_delta_small(svc: RegressionService) -> None:
    """delta=0% → status='stable', overall='passed'."""
    baseline = _make_baseline(svc, metrics_snapshot={"accuracy": 0.9})
    report = svc.compare({"accuracy": 0.9}, "run-x", 0, 0, baseline)
    assert report.metric_diffs[0].status == "stable"
    assert report.overall_status == "passed"


def test_compare_improved_when_delta_positive(svc: RegressionService) -> None:
    """delta=+10% → status='improved', overall='passed'."""
    baseline = _make_baseline(svc, metrics_snapshot={"accuracy": 0.8})
    report = svc.compare({"accuracy": 0.88}, "run-x", 0, 0, baseline)
    diff = report.metric_diffs[0]
    assert diff.status == "improved"
    assert report.overall_status == "passed"


# ---------------------------------------------------------------------------
# gate() tests
# ---------------------------------------------------------------------------


def test_gate_blocked_when_critical(svc: RegressionService) -> None:
    """If any metric is critical, gate returns blocked."""
    baseline = _make_baseline(svc, metrics_snapshot={"accuracy": 1.0})
    report = svc.compare({"accuracy": 0.8}, "run-x", 0, 0, baseline)
    gate = svc.gate(report)
    assert gate.status == "blocked"
    assert "accuracy" in gate.blocking_metrics


def test_gate_warning_when_regressed(svc: RegressionService) -> None:
    """delta=-7% (regressed but not critical) → gate returns warning."""
    baseline = _make_baseline(svc, metrics_snapshot={"accuracy": 1.0})
    report = svc.compare({"accuracy": 0.93}, "run-x", 0, 0, baseline)
    gate = svc.gate(report)
    assert gate.status == "warning"
    assert "accuracy" in gate.warnings
    assert gate.blocking_metrics == []


def test_gate_passed_when_stable(svc: RegressionService) -> None:
    """No regression → gate returns passed."""
    baseline = _make_baseline(svc, metrics_snapshot={"accuracy": 0.9})
    report = svc.compare({"accuracy": 0.9}, "run-x", 0, 0, baseline)
    gate = svc.gate(report)
    assert gate.status == "passed"
    assert gate.blocking_metrics == []
    assert gate.warnings == []


# ---------------------------------------------------------------------------
# Baseline CRUD tests
# ---------------------------------------------------------------------------


def test_save_and_get_baseline(svc: RegressionService) -> None:
    """save_baseline + get_latest_baseline round-trip."""
    saved = _make_baseline(svc, blueprint_id="bp-crud", metrics_snapshot={"f1": 0.75})
    latest = svc.get_latest_baseline("bp-crud")
    assert latest is not None
    assert latest.baseline_id == saved.baseline_id
    assert latest.metrics_snapshot == {"f1": 0.75}


def test_get_latest_returns_none_when_no_baseline(svc: RegressionService) -> None:
    result = svc.get_latest_baseline("bp-missing-xyz")
    assert result is None


def test_list_baselines_returns_all(svc: RegressionService) -> None:
    _make_baseline(svc, blueprint_id="bp-list")
    _make_baseline(svc, blueprint_id="bp-list", metrics_snapshot={"x": 0.5})
    baselines = svc.list_baselines("bp-list")
    assert len(baselines) == 2


def test_baseline_max_10(svc: RegressionService) -> None:
    """Saving 12 baselines keeps only the 10 most recent."""
    for i in range(12):
        # Ensure distinct created_at by varying sleep slightly
        # (use different metric values to distinguish, timestamps from uuid ordering)
        svc.save_baseline(
            blueprint_id="bp-max",
            result_id=f"result-{i}",
            eval_profile_id="ep",
            metrics_snapshot={"score": float(i) / 100},
            overall_pass=True,
        )
    baselines = svc.list_baselines("bp-max")
    assert len(baselines) == 10
