"""Regression baseline comparison service — Story 9-6.

Provides:
  - RegressionBaseline  — persisted baseline snapshot
  - RegressionReport    — per-run comparison output
  - GateResult          — pass / warning / blocked decision
  - RegressionService   — CRUD + compare + gate logic
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

_BASELINES_DIR = Path(__file__).resolve().parents[2] / ".xueban" / "regression_baselines"
_MAX_BASELINES = 10


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class RegressionBaseline(BaseModel):
    baseline_id: str = Field(default_factory=lambda: str(uuid4()))
    blueprint_id: str
    eval_profile_id: str
    based_on_result_id: str
    metrics_snapshot: dict[str, float] = {}
    citation_pass: bool | None = None
    overall_pass: bool
    created_at: str = Field(default_factory=utc_now)
    notes: str = ""


class MetricDiff(BaseModel):
    metric_id: str
    baseline_score: float
    current_score: float
    delta: float  # percentage change
    status: str  # "improved" | "stable" | "regressed" | "critical"


class CitationDiff(BaseModel):
    baseline_coverage: float
    current_coverage: float
    delta: float


class RegressionReport(BaseModel):
    report_id: str = Field(default_factory=lambda: str(uuid4()))
    blueprint_id: str
    baseline_id: str
    current_result_id: str
    metric_diffs: list[MetricDiff] = []
    citation_diff: CitationDiff | None = None
    current_latency_ms: int = 0
    current_tokens: int = 0
    overall_status: str = "passed"  # "passed" | "warning" | "blocked"
    blocking_reasons: list[str] = []
    created_at: str = Field(default_factory=utc_now)


class GateResult(BaseModel):
    status: str  # "passed" | "warning" | "blocked"
    blocking_metrics: list[str] = []
    warnings: list[str] = []


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class RegressionService:
    """Handles baseline persistence, comparison, and gate decisions."""

    def _blueprints_dir(self, blueprint_id: str) -> Path:
        d = _BASELINES_DIR / blueprint_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ------------------------------------------------------------------
    # Baseline CRUD
    # ------------------------------------------------------------------

    def save_baseline(
        self,
        blueprint_id: str,
        result_id: str,
        eval_profile_id: str,
        metrics_snapshot: dict[str, float],
        citation_pass: bool | None = None,
        overall_pass: bool = True,
        notes: str = "",
    ) -> RegressionBaseline:
        baseline = RegressionBaseline(
            blueprint_id=blueprint_id,
            eval_profile_id=eval_profile_id,
            based_on_result_id=result_id,
            metrics_snapshot=metrics_snapshot,
            citation_pass=citation_pass,
            overall_pass=overall_pass,
            notes=notes,
        )
        d = self._blueprints_dir(blueprint_id)
        all_file = d / "baselines.json"
        baselines: list[dict[str, Any]] = []
        if all_file.exists():
            baselines = json.loads(all_file.read_text(encoding="utf-8"))
        baselines.append(baseline.model_dump())
        # Sort descending by created_at, keep most recent _MAX_BASELINES
        baselines.sort(key=lambda x: x["created_at"], reverse=True)
        baselines = baselines[:_MAX_BASELINES]
        all_file.write_text(json.dumps(baselines), encoding="utf-8")
        return baseline

    def get_latest_baseline(self, blueprint_id: str) -> RegressionBaseline | None:
        d = self._blueprints_dir(blueprint_id)
        all_file = d / "baselines.json"
        if not all_file.exists():
            return None
        baselines: list[dict[str, Any]] = json.loads(all_file.read_text(encoding="utf-8"))
        if not baselines:
            return None
        return RegressionBaseline(**baselines[0])

    def list_baselines(self, blueprint_id: str) -> list[RegressionBaseline]:
        d = self._blueprints_dir(blueprint_id)
        all_file = d / "baselines.json"
        if not all_file.exists():
            return []
        baselines = json.loads(all_file.read_text(encoding="utf-8"))
        return [RegressionBaseline(**b) for b in baselines]

    # ------------------------------------------------------------------
    # Compare
    # ------------------------------------------------------------------

    def compare(
        self,
        current_metrics: dict[str, float],
        current_result_id: str,
        current_latency_ms: int,
        current_tokens: int,
        baseline: RegressionBaseline,
    ) -> RegressionReport:
        diffs: list[MetricDiff] = []
        overall = "passed"
        blocking: list[str] = []

        for metric_id, baseline_score in baseline.metrics_snapshot.items():
            current_score = current_metrics.get(metric_id, 0.0)
            safe_base = max(abs(baseline_score), 0.001)
            delta = (current_score - baseline_score) / safe_base * 100

            if delta <= -10:
                status = "critical"
                overall = "blocked"
                blocking.append(f"{metric_id}: {delta:.1f}%")
            elif delta <= -5:
                status = "regressed"
                if overall == "passed":
                    overall = "warning"
            elif delta >= 5:
                status = "improved"
            else:
                status = "stable"

            diffs.append(
                MetricDiff(
                    metric_id=metric_id,
                    baseline_score=baseline_score,
                    current_score=current_score,
                    delta=delta,
                    status=status,
                )
            )

        return RegressionReport(
            blueprint_id=baseline.blueprint_id,
            baseline_id=baseline.baseline_id,
            current_result_id=current_result_id,
            metric_diffs=diffs,
            current_latency_ms=current_latency_ms,
            current_tokens=current_tokens,
            overall_status=overall,
            blocking_reasons=blocking,
        )

    # ------------------------------------------------------------------
    # Gate
    # ------------------------------------------------------------------

    def gate(self, report: RegressionReport) -> GateResult:
        blocking_metrics = [d.metric_id for d in report.metric_diffs if d.status == "critical"]
        warnings = [d.metric_id for d in report.metric_diffs if d.status == "regressed"]
        return GateResult(
            status=report.overall_status,
            blocking_metrics=blocking_metrics,
            warnings=warnings,
        )


regression_service = RegressionService()
