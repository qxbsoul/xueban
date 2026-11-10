"""API-level tests for the Regression Gate endpoints — Story 9-6.

Covers:
  POST /regression/{blueprint_id}/run   — no baseline → null data + hint
  POST /regression/{blueprint_id}/run   — with baseline → gate blocked
  GET  /regression/{blueprint_id}/baselines
  POST /regression/{blueprint_id}/baselines → 201
"""
from __future__ import annotations

from pathlib import Path
from typing import Generator

import pytest
from fastapi.testclient import TestClient

import xueban.runtime.regression_service as _reg_mod
from xueban.runtime.regression_service import RegressionService


@pytest.fixture(autouse=True)
def isolated_regression_service(tmp_path: Path) -> Generator[RegressionService, None, None]:
    """Redirect the module-level singleton to tmp_path for test isolation."""
    original_dir = _reg_mod._BASELINES_DIR
    _reg_mod._BASELINES_DIR = tmp_path / "regression_baselines"
    # Patch the module-level singleton to use tmp baselines dir
    _reg_mod.regression_service = RegressionService()
    # Also patch the regression api module so it picks up the new singleton
    import xueban.api.regression as _api_mod
    _api_mod.regression_service = _reg_mod.regression_service
    yield _reg_mod.regression_service
    _reg_mod._BASELINES_DIR = original_dir
    _reg_mod.regression_service = RegressionService()
    _api_mod.regression_service = _reg_mod.regression_service


@pytest.fixture(scope="module")
def client() -> TestClient:
    from xueban.server import app
    return TestClient(app)


# ---------------------------------------------------------------------------
# POST /regression/{blueprint_id}/run
# ---------------------------------------------------------------------------


def test_run_no_baseline_returns_null(client: TestClient) -> None:
    """When no baseline exists, data is null and meta contains no_baseline hint."""
    resp = client.post(
        "/regression/bp-new-xyz/run",
        json={"current_metrics": {"accuracy": 0.9}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] is None
    assert body["meta"]["message"] == "no_baseline"


def test_run_with_baseline_blocked(
    client: TestClient,
    isolated_regression_service: RegressionService,
) -> None:
    """With a baseline, if current score drops >10% the gate is blocked."""
    # Save baseline with accuracy=1.0
    isolated_regression_service.save_baseline(
        blueprint_id="bp-block-test",
        result_id="base-001",
        eval_profile_id="ep",
        metrics_snapshot={"accuracy": 1.0},
        overall_pass=True,
    )
    resp = client.post(
        "/regression/bp-block-test/run",
        json={"current_metrics": {"accuracy": 0.8}, "current_result_id": "run-001"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] is not None
    gate = body["data"]["gate_result"]
    assert gate["status"] == "blocked"
    assert "accuracy" in gate["blocking_metrics"]


def test_run_with_baseline_passed(
    client: TestClient,
    isolated_regression_service: RegressionService,
) -> None:
    """With a stable current score the gate passes."""
    isolated_regression_service.save_baseline(
        blueprint_id="bp-pass-test",
        result_id="base-002",
        eval_profile_id="ep",
        metrics_snapshot={"f1": 0.9},
        overall_pass=True,
    )
    resp = client.post(
        "/regression/bp-pass-test/run",
        json={"current_metrics": {"f1": 0.9}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["gate_result"]["status"] == "passed"


# ---------------------------------------------------------------------------
# POST /regression/{blueprint_id}/baselines
# ---------------------------------------------------------------------------


def test_save_baseline_201(client: TestClient) -> None:
    """Saving a baseline returns HTTP 201 and the baseline data."""
    resp = client.post(
        "/regression/bp-save-test/baselines",
        json={
            "eval_profile_id": "ep-1",
            "metrics_snapshot": {"precision": 0.88},
            "overall_pass": True,
            "notes": "initial baseline",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["data"]["blueprint_id"] == "bp-save-test"
    assert body["data"]["metrics_snapshot"] == {"precision": 0.88}
    assert body["data"]["baseline_id"] is not None


# ---------------------------------------------------------------------------
# GET /regression/{blueprint_id}/baselines
# ---------------------------------------------------------------------------


def test_list_baselines(
    client: TestClient,
    isolated_regression_service: RegressionService,
) -> None:
    """GET returns a list of baselines with total in meta."""
    isolated_regression_service.save_baseline(
        blueprint_id="bp-list-api",
        result_id="r1",
        eval_profile_id="ep",
        metrics_snapshot={"score": 0.7},
        overall_pass=True,
    )
    isolated_regression_service.save_baseline(
        blueprint_id="bp-list-api",
        result_id="r2",
        eval_profile_id="ep",
        metrics_snapshot={"score": 0.75},
        overall_pass=True,
    )
    resp = client.get("/regression/bp-list-api/baselines")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["data"], list)
    assert len(body["data"]) == 2
    assert body["meta"]["total"] == 2


def test_list_baselines_empty(client: TestClient) -> None:
    """GET returns empty list when no baselines exist."""
    resp = client.get("/regression/bp-never-seen/baselines")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == []
    assert body["meta"]["total"] == 0


# ---------------------------------------------------------------------------
# GET /regression/{blueprint_id}/reports/{report_id}
# ---------------------------------------------------------------------------


def test_get_report_returns_404(client: TestClient) -> None:
    """Reports are not persisted yet (Phase 2); endpoint always returns 404."""
    resp = client.get("/regression/bp-x/reports/report-123")
    assert resp.status_code == 404
    body = resp.json()
    assert body["detail"]["error"]["code"] == "REPORT_NOT_FOUND"
