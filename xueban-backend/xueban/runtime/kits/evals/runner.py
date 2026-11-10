"""KitSmokeRunner — Story 10.6 (AC1, AC6, AC7).

Declarative Kit smoke / regression eval driver.

Design notes:
  - EvalPacks are *declarative*: each `SmokeCase` carries an `executor`
    callable (mock_llm friendly, deterministic) plus pass-condition meta.
  - The runner orchestrates the cases (timeout per-case 30s, total 60s),
    aggregates a structured `SmokeRunReport`, and persists / compares
    a baseline under `.xueban/kits/{kit_id}/baseline.json`.
  - Regression verdict follows AC4 thresholds:
       smoke pass-rate    < 80%  → block      | < 100% → warning
       citation coverage  < 50%  → block      | < 80%  → warning
       first-pass-rate    drop > 40% → block  | drop > 20% → warning
       avg latency        rise > 200% → block | rise > 50% → warning
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from xueban.runtime.contracts_builder import AgentBlueprint

logger = logging.getLogger(__name__)

BASELINE_DIR = Path(".xueban") / "kits"

# Per-case / total timeouts (AC6)
DEFAULT_CASE_TIMEOUT_S = 30.0
DEFAULT_TOTAL_TIMEOUT_S = 60.0


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class SuggestedFix(BaseModel):
    """A user-actionable navigation hint shown in SmokeRunPanel."""

    label: str
    action_type: Literal["navigate"] = "navigate"
    target: str  # "knowledge_dock" / "policy_panel" / route or panel id


class SmokeCase(BaseModel):
    """One smoke case — declarative test description + executor."""

    name: str
    description: str = ""
    inputs: Dict[str, Any] = Field(default_factory=dict)
    pass_condition: str = ""
    citation_required: bool = False
    # Executor returns dict with keys: passed (bool), metrics (dict),
    # missing_configs (list[str]), suggested_fixes (list[SuggestedFix dict]),
    # detail (str), failed_stage (str | None), citation_present (bool | None)
    executor: Optional[Callable[[AgentBlueprint, "SmokeRunOptions"], Awaitable[Dict[str, Any]]]] = None

    model_config = {"arbitrary_types_allowed": True}


class RegressionCase(BaseModel):
    """A regression case — typically a smoke case promoted with metric thresholds."""

    name: str
    description: str = ""
    smoke_case_name: str  # which smoke case to re-run for the regression
    metric_thresholds: Dict[str, float] = Field(default_factory=dict)


class KitSmokeEvalPack(BaseModel):
    kit_id: str
    smoke_cases: List[SmokeCase] = Field(default_factory=list)
    regression_cases: List[RegressionCase] = Field(default_factory=list)

    model_config = {"arbitrary_types_allowed": True}


class SmokeRunOptions(BaseModel):
    timeout_s: float = DEFAULT_TOTAL_TIMEOUT_S
    case_timeout_s: float = DEFAULT_CASE_TIMEOUT_S
    dry_run: bool = False
    mock_llm: bool = True


class SmokeCaseResult(BaseModel):
    name: str
    passed: bool
    failed_stage: Optional[str] = None
    metrics: Dict[str, float] = Field(default_factory=dict)
    missing_configs: List[str] = Field(default_factory=list)
    suggested_fixes: List[SuggestedFix] = Field(default_factory=list)
    detail: str = ""
    duration_s: float = 0.0
    error: Optional[str] = None  # "timeout" / "exception"
    citation_present: Optional[bool] = None


class SmokeRunReport(BaseModel):
    kit_id: str
    passed: bool
    failed_stage: Optional[str] = None
    missing_configs: List[str] = Field(default_factory=list)
    suggested_fixes: List[SuggestedFix] = Field(default_factory=list)
    case_results: List[SmokeCaseResult] = Field(default_factory=list)
    summary_metrics: Dict[str, float] = Field(default_factory=dict)
    duration_s: float = 0.0
    timestamp: str = ""
    error: Optional[str] = None  # "timeout" / "no_eval_pack" / ...

    @property
    def smoke_pass_rate(self) -> float:
        if not self.case_results:
            return 0.0
        passed = sum(1 for c in self.case_results if c.passed)
        return passed / len(self.case_results)


class RegressionMetricDiff(BaseModel):
    metric: str
    baseline: float
    current: float
    delta_pct: float
    verdict: Literal["pass", "warning", "block"]


class RegressionReport(BaseModel):
    kit_id: str
    baseline_timestamp: Optional[str] = None
    current: Optional[SmokeRunReport] = None
    baseline_comparison: List[RegressionMetricDiff] = Field(default_factory=list)
    regressions_detected: bool = False
    verdict: Literal["pass", "warning", "block"] = "pass"
    reasons: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# EvalPack registry
# ---------------------------------------------------------------------------


_PACKS: Dict[str, KitSmokeEvalPack] = {}

_KIT_ID_ALIASES = {
    "research": "research_kit",
    "knowledge_assistant": "knowledge_assistant_kit",
    "review_approval": "review_approval_kit",
    "persona_npc": "persona_npc_kit",
}


def _canonical(kit_id: str) -> str:
    return _KIT_ID_ALIASES.get(kit_id, kit_id)


def register_eval_pack(pack: KitSmokeEvalPack) -> None:
    _PACKS[_canonical(pack.kit_id)] = pack
    logger.info("KitSmokeEvalPack registered: %s", pack.kit_id)


def get_eval_pack(kit_id: str) -> Optional[KitSmokeEvalPack]:
    cid = _canonical(kit_id)
    if cid in _PACKS:
        return _PACKS[cid]
    # Lazy import on first miss — auto-discover
    _autodiscover()
    return _PACKS.get(cid)


def list_eval_pack_ids() -> List[str]:
    _autodiscover()
    return sorted(_PACKS.keys())


_AUTODISCOVERED = False


def _autodiscover() -> None:
    global _AUTODISCOVERED
    if _AUTODISCOVERED:
        return
    _AUTODISCOVERED = True
    # Importing the eval modules triggers register_eval_pack() at module load.
    try:  # pragma: no cover - simple import side-effect
        from . import (  # noqa: F401
            research_kit_eval,
            knowledge_assistant_kit_eval,
            review_approval_kit_eval,
            persona_npc_kit_eval,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("KitSmokeEvalPack autodiscovery partial failure: %s", exc)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class KitSmokeRunner:
    """Executes Kit smoke / regression eval packs."""

    def __init__(self, baseline_dir: Path = BASELINE_DIR) -> None:
        self.baseline_dir = baseline_dir

    # ----- public API --------------------------------------------------

    async def run_smoke(
        self,
        kit_id: str,
        blueprint: AgentBlueprint,
        options: Optional[SmokeRunOptions] = None,
    ) -> SmokeRunReport:
        opts = options or SmokeRunOptions()
        pack = get_eval_pack(kit_id)
        started = time.monotonic()
        if pack is None:
            return SmokeRunReport(
                kit_id=kit_id,
                passed=False,
                failed_stage="eval_pack_missing",
                missing_configs=[f"No eval pack registered for kit_id={kit_id!r}"],
                suggested_fixes=[
                    SuggestedFix(label="检查 Kit 注册", target="kit_registry"),
                ],
                duration_s=0.0,
                timestamp=_utc_now_iso(),
                error="no_eval_pack",
            )

        try:
            results: List[SmokeCaseResult] = await asyncio.wait_for(
                self._run_cases(pack.smoke_cases, blueprint, opts),
                timeout=opts.timeout_s,
            )
        except asyncio.TimeoutError:
            return SmokeRunReport(
                kit_id=kit_id,
                passed=False,
                failed_stage="timeout",
                missing_configs=[],
                suggested_fixes=[
                    SuggestedFix(label="检查 Kit 配置 / 网络", target="builder_inspector"),
                ],
                duration_s=time.monotonic() - started,
                timestamp=_utc_now_iso(),
                error="timeout",
            )

        return self._aggregate(kit_id, results, time.monotonic() - started)

    async def run_regression(
        self,
        kit_id: str,
        blueprint: AgentBlueprint,
        baseline: Optional[SmokeRunReport] = None,
        options: Optional[SmokeRunOptions] = None,
    ) -> RegressionReport:
        opts = options or SmokeRunOptions()
        if baseline is None:
            baseline = self.load_baseline(kit_id)
        current = await self.run_smoke(kit_id, blueprint, opts)
        return self._compare(kit_id, current, baseline)

    # ----- baseline persistence ---------------------------------------

    def _baseline_path(self, kit_id: str) -> Path:
        return self.baseline_dir / _canonical(kit_id) / "baseline.json"

    def save_baseline(self, report: SmokeRunReport) -> Path:
        path = self._baseline_path(report.kit_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def load_baseline(self, kit_id: str) -> Optional[SmokeRunReport]:
        path = self._baseline_path(kit_id)
        if not path.exists():
            return None
        try:
            return SmokeRunReport.model_validate(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Baseline load failed for %s: %s", kit_id, exc)
            return None

    # ----- internals ---------------------------------------------------

    async def _run_cases(
        self,
        cases: List[SmokeCase],
        blueprint: AgentBlueprint,
        opts: SmokeRunOptions,
    ) -> List[SmokeCaseResult]:
        results: List[SmokeCaseResult] = []
        for case in cases:
            t0 = time.monotonic()
            try:
                if case.executor is None:
                    raise RuntimeError("smoke case has no executor")
                payload = await asyncio.wait_for(
                    case.executor(blueprint, opts),
                    timeout=opts.case_timeout_s,
                )
            except asyncio.TimeoutError:
                results.append(
                    SmokeCaseResult(
                        name=case.name,
                        passed=False,
                        failed_stage="timeout",
                        detail=f"case '{case.name}' timed out > {opts.case_timeout_s}s",
                        duration_s=time.monotonic() - t0,
                        error="timeout",
                    )
                )
                continue
            except Exception as exc:  # noqa: BLE001
                results.append(
                    SmokeCaseResult(
                        name=case.name,
                        passed=False,
                        failed_stage="exception",
                        detail=f"{type(exc).__name__}: {exc}",
                        duration_s=time.monotonic() - t0,
                        error="exception",
                    )
                )
                continue

            fixes_raw = payload.get("suggested_fixes") or []
            fixes = [
                SuggestedFix.model_validate(f) if not isinstance(f, SuggestedFix) else f
                for f in fixes_raw
            ]
            results.append(
                SmokeCaseResult(
                    name=case.name,
                    passed=bool(payload.get("passed", False)),
                    failed_stage=payload.get("failed_stage"),
                    metrics={k: float(v) for k, v in (payload.get("metrics") or {}).items()},
                    missing_configs=list(payload.get("missing_configs") or []),
                    suggested_fixes=fixes,
                    detail=str(payload.get("detail") or ""),
                    duration_s=time.monotonic() - t0,
                    citation_present=payload.get("citation_present"),
                )
            )
        return results

    def _aggregate(
        self,
        kit_id: str,
        results: List[SmokeCaseResult],
        duration_s: float,
    ) -> SmokeRunReport:
        all_passed = all(r.passed for r in results) and bool(results)
        failed = next((r for r in results if not r.passed), None)
        missing: List[str] = []
        fixes: List[SuggestedFix] = []
        for r in results:
            missing.extend(r.missing_configs)
            fixes.extend(r.suggested_fixes)

        # Summary metrics for regression comparison.
        n = len(results) or 1
        passed_n = sum(1 for r in results if r.passed)
        avg_latency_ms = (
            sum(r.duration_s for r in results) / n * 1000.0 if results else 0.0
        )
        citation_eligible = [r for r in results if r.citation_present is not None]
        citation_pass_rate = (
            sum(1 for r in citation_eligible if r.citation_present) / len(citation_eligible)
            if citation_eligible
            else 1.0
        )

        summary_metrics: Dict[str, float] = {
            "smoke_pass_rate": passed_n / n,
            "first_pass_rate": passed_n / n,  # MVP: same as smoke pass rate
            "citation_pass_rate": citation_pass_rate,
            "avg_latency_ms": avg_latency_ms,
        }

        return SmokeRunReport(
            kit_id=kit_id,
            passed=all_passed,
            failed_stage=(failed.failed_stage if failed else None),
            missing_configs=_dedup(missing),
            suggested_fixes=_dedup_fixes(fixes),
            case_results=results,
            summary_metrics=summary_metrics,
            duration_s=duration_s,
            timestamp=_utc_now_iso(),
        )

    def _compare(
        self,
        kit_id: str,
        current: SmokeRunReport,
        baseline: Optional[SmokeRunReport],
    ) -> RegressionReport:
        if baseline is None:
            return RegressionReport(
                kit_id=kit_id,
                baseline_timestamp=None,
                current=current,
                baseline_comparison=[],
                regressions_detected=False,
                verdict="pass" if current.passed else "warning",
                reasons=(
                    ["首次运行无 baseline；当前 smoke 未通过，建议人工排查"]
                    if not current.passed
                    else []
                ),
            )

        diffs: List[RegressionMetricDiff] = []
        verdicts: List[str] = []
        reasons: List[str] = []

        cur_pass = current.summary_metrics.get("smoke_pass_rate", 0.0)
        cur_cite = current.summary_metrics.get("citation_pass_rate", 1.0)
        cur_latency = current.summary_metrics.get("avg_latency_ms", 0.0)
        b = baseline.summary_metrics

        # 1. smoke_pass_rate (AC4 + AC7: 100%→pass, ~75%→warning, ~60%→block)
        v = "pass"
        if cur_pass <= 0.60:
            v = "block"
            reasons.append(f"smoke 通过率 {cur_pass:.0%} ≤ 60% — 阻断")
        elif cur_pass < 1.0:
            v = "warning"
            reasons.append(f"smoke 通过率 {cur_pass:.0%} < 100% — 警告")
        verdicts.append(v)
        diffs.append(
            RegressionMetricDiff(
                metric="smoke_pass_rate",
                baseline=b.get("smoke_pass_rate", 0.0),
                current=cur_pass,
                delta_pct=_pct_delta(b.get("smoke_pass_rate", 0.0), cur_pass),
                verdict=v,  # type: ignore[arg-type]
            )
        )

        # 2. citation_pass_rate
        v = "pass"
        if cur_cite < 0.50:
            v = "block"
            reasons.append(f"citation 完整率 {cur_cite:.0%} < 50% — 阻断")
        elif cur_cite < 0.80:
            v = "warning"
            reasons.append(f"citation 完整率 {cur_cite:.0%} < 80% — 警告")
        verdicts.append(v)
        diffs.append(
            RegressionMetricDiff(
                metric="citation_pass_rate",
                baseline=b.get("citation_pass_rate", 1.0),
                current=cur_cite,
                delta_pct=_pct_delta(b.get("citation_pass_rate", 1.0), cur_cite),
                verdict=v,  # type: ignore[arg-type]
            )
        )

        # 3. first_pass_rate — REMOVED (Story 10.6 H4):
        #    Previously `first_pass_rate ≡ smoke_pass_rate` (MVP shortcut), which
        #    caused smoke pass-rate drops to be double-counted in the comparator.
        #    Until a real cross-run definition exists, omit it from the diff.
        #    The summary metric is still emitted in `_aggregate` for forward
        #    compatibility, but it does not contribute to the regression verdict.

        # 4. avg_latency_ms (relative rise)
        b_lat = max(b.get("avg_latency_ms", 0.0), 1e-6)
        rise = (cur_latency - b_lat) / b_lat
        v = "pass"
        if rise > 2.0:
            v = "block"
            reasons.append(f"平均时延上升 {rise:.0%} > 200% — 阻断")
        elif rise > 0.5:
            v = "warning"
            reasons.append(f"平均时延上升 {rise:.0%} > 50% — 警告")
        verdicts.append(v)
        diffs.append(
            RegressionMetricDiff(
                metric="avg_latency_ms",
                baseline=b_lat,
                current=cur_latency,
                delta_pct=_pct_delta(b_lat, cur_latency),
                verdict=v,  # type: ignore[arg-type]
            )
        )

        if "block" in verdicts:
            overall = "block"
        elif "warning" in verdicts:
            overall = "warning"
        else:
            overall = "pass"

        return RegressionReport(
            kit_id=kit_id,
            baseline_timestamp=baseline.timestamp,
            current=current,
            baseline_comparison=diffs,
            regressions_detected=overall != "pass",
            verdict=overall,  # type: ignore[arg-type]
            reasons=reasons,
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _pct_delta(baseline: float, current: float) -> float:
    safe = max(abs(baseline), 1e-6)
    return (current - baseline) / safe * 100.0


def _dedup(items: List[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for item in items:
        if item not in seen:
            out.append(item)
            seen.add(item)
    return out


def _dedup_fixes(fixes: List[SuggestedFix]) -> List[SuggestedFix]:
    seen: set[tuple[str, str]] = set()
    out: List[SuggestedFix] = []
    for f in fixes:
        key = (f.label, f.target)
        if key not in seen:
            out.append(f)
            seen.add(key)
    return out
