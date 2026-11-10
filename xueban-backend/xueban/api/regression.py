"""Regression API — Story 9-6.

4 endpoints:
  POST   /regression/{blueprint_id}/run
  GET    /regression/{blueprint_id}/baselines
  POST   /regression/{blueprint_id}/baselines  (201)
  GET    /regression/{blueprint_id}/reports/{report_id}  (404 — Phase 2 persistence)
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from xueban.runtime.regression_service import regression_service, RegressionBaseline

router = APIRouter(prefix="/regression", tags=["regression"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _envelope(data: Any, **meta: Any) -> dict[str, Any]:
    return {"data": data, "meta": meta}


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class SaveBaselineRequest(BaseModel):
    eval_profile_id: str = "default"
    metrics_snapshot: dict[str, float] = {}
    citation_pass: bool | None = None
    overall_pass: bool = True
    notes: str = ""


class RunRegressionRequest(BaseModel):
    current_metrics: dict[str, float] = {}
    current_result_id: str = "manual"
    current_latency_ms: int = 0
    current_tokens: int = 0


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/{blueprint_id}/run")
async def run_regression(blueprint_id: str, body: RunRegressionRequest) -> dict[str, Any]:
    """Compare current metrics against the latest baseline and return a gate result."""
    baseline = regression_service.get_latest_baseline(blueprint_id)
    if baseline is None:
        return _envelope(None, message="no_baseline", hint="首次通过后请保存基线")

    report = regression_service.compare(
        body.current_metrics,
        body.current_result_id,
        body.current_latency_ms,
        body.current_tokens,
        baseline,
    )
    gate = regression_service.gate(report)
    return _envelope(
        {
            "gate_result": gate.model_dump(),
            "report": report.model_dump(),
            "report_id": report.report_id,
            "eval_result_id": body.current_result_id,
        }
    )


@router.get("/{blueprint_id}/baselines")
async def list_baselines(blueprint_id: str) -> dict[str, Any]:
    """Return all saved baselines for a blueprint (newest first, max 10)."""
    baselines = regression_service.list_baselines(blueprint_id)
    return _envelope([b.model_dump() for b in baselines], total=len(baselines))


@router.post("/{blueprint_id}/baselines", status_code=201)
async def save_baseline(blueprint_id: str, body: SaveBaselineRequest) -> dict[str, Any]:
    """Persist a new baseline snapshot for a blueprint."""
    baseline = regression_service.save_baseline(
        blueprint_id=blueprint_id,
        result_id=f"manual-{blueprint_id}-{uuid.uuid4().hex[:8]}",
        eval_profile_id=body.eval_profile_id,
        metrics_snapshot=body.metrics_snapshot,
        citation_pass=body.citation_pass,
        overall_pass=body.overall_pass,
        notes=body.notes,
    )
    return _envelope(baseline.model_dump())


@router.get("/{blueprint_id}/reports/{report_id}")
async def get_report(blueprint_id: str, report_id: str) -> dict[str, Any]:
    """Retrieve a persisted regression report.

    Reports are not yet persisted (Phase 2); this endpoint always returns 404.
    """
    raise HTTPException(
        status_code=404,
        detail={"error": {"code": "REPORT_NOT_FOUND"}},
    )
