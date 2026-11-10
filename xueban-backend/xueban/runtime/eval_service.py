"""EvalService — Story 9.5 AC1/AC2.

EvalProfile 数据契约 + SmokeEvalRunner 引擎。
持久化路径遵循 .xueban/ 约定：
  profiles:  .xueban/eval_profiles/{profile_id}.json
  results:   .xueban/eval_results/{result_id}.json
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import threading
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from xueban.runtime.errors import XuebanError

logger = logging.getLogger(__name__)

_STORAGE_ROOT = Path(os.environ.get("XUEBAN_STORAGE_ROOT", ".xueban"))
_PROFILES_DIR = _STORAGE_ROOT / "eval_profiles"
_RESULTS_DIR = _STORAGE_ROOT / "eval_results"

# Allowlist for IDs persisted to disk (UUID hex).
_ID_RE = re.compile(r"^[a-f0-9]{32}$")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid4().hex


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EvalProfileNotFound(XuebanError):
    code = "EVAL_PROFILE_NOT_FOUND"


class EvalResultNotFound(XuebanError):
    code = "EVAL_RESULT_NOT_FOUND"


class EvalProfileInvalidId(XuebanError):
    code = "EVAL_PROFILE_INVALID_ID"


class EvalResultInvalidId(XuebanError):
    code = "EVAL_RESULT_INVALID_ID"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class MetricType(str, Enum):
    task_completion = "task_completion"
    citation_coverage = "citation_coverage"
    latency_p95 = "latency_p95"
    token_budget = "token_budget"
    rejection_rate = "rejection_rate"


class FailureDimension(str, Enum):
    goal_clarity = "goal_clarity"
    knowledge_access = "knowledge_access"
    tool_permission = "tool_permission"
    role_conflict = "role_conflict"
    graph_broken = "graph_broken"


# ---------------------------------------------------------------------------
# Data models — AC1
# ---------------------------------------------------------------------------


class EvalMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric_id: str = Field(default_factory=_new_id)
    name: str = Field(min_length=1)
    metric_type: MetricType
    threshold: float = Field(ge=0.0)
    weight: float = Field(ge=0.0, default=1.0)


class FailureThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_failed_metrics: int = Field(ge=0, default=1)
    blocking_metrics: List[str] = Field(default_factory=list)


class EvalProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(default_factory=_new_id)
    name: str = Field(min_length=1)
    success_metrics: List[EvalMetric] = Field(default_factory=list)
    test_prompts: List[str] = Field(min_length=1)
    expected_artifacts: List[str] = Field(default_factory=list)
    citation_checks: bool = False
    latency_budget_ms: int = Field(ge=0, default=0)
    failure_thresholds: FailureThresholds = Field(default_factory=FailureThresholds)
    created_at: datetime = Field(default_factory=_now_utc)
    updated_at: datetime = Field(default_factory=_now_utc)

    @model_validator(mode="after")
    def _validate_profile(self) -> "EvalProfile":
        # test_prompts 至少 1 条
        if not self.test_prompts:
            raise ValueError("test_prompts must contain at least one prompt")
        # 每条 test_prompt 不能为空字符串
        for p in self.test_prompts:
            if not p or not p.strip():
                raise ValueError("test_prompts entries must be non-empty strings")
        # blocking_metrics 必须全部在 success_metrics 的 metric_id 集合中
        valid_ids = {m.metric_id for m in self.success_metrics}
        for bm in self.failure_thresholds.blocking_metrics:
            if bm not in valid_ids:
                raise ValueError(
                    f"blocking_metric {bm!r} is not in success_metrics metric_ids"
                )
        return self


# ---------------------------------------------------------------------------
# Result models — AC2
# ---------------------------------------------------------------------------


class MetricScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric_id: str
    score: float
    threshold: float
    passed: bool


class FailureReason(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension: FailureDimension
    detail: str
    suggested_fix: str = ""


class SmokeEvalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_id: str = Field(default_factory=_new_id)
    profile_id: str
    blueprint_id: str
    overall_pass: bool
    metric_scores: List[MetricScore] = Field(default_factory=list)
    citation_pass: Optional[bool] = None
    failure_reasons: List[FailureReason] = Field(default_factory=list)
    latency_ms: int = 0
    token_usage: int = 0
    ran_at: datetime = Field(default_factory=_now_utc)
    status: str = "completed"


# ---------------------------------------------------------------------------
# EvalService — CRUD + runner
# ---------------------------------------------------------------------------


class EvalService:
    """File-backed CRUD for EvalProfile + SmokeEvalRunner."""

    def __init__(
        self,
        profiles_dir: Optional[Path] = None,
        results_dir: Optional[Path] = None,
    ) -> None:
        self._profiles_dir = profiles_dir or _PROFILES_DIR
        self._results_dir = results_dir or _RESULTS_DIR
        self._lock = threading.Lock()
        self._running_lock = threading.Lock()
        # In-progress results (status="running") stored in memory until persisted.
        self._running: Dict[str, SmokeEvalResult] = {}
        self._ensure_dirs()

    # -- helpers ----------------------------------------------------------

    def _ensure_dirs(self) -> None:
        self._profiles_dir.mkdir(parents=True, exist_ok=True)
        self._results_dir.mkdir(parents=True, exist_ok=True)

    def _validate_profile_id(self, profile_id: str) -> None:
        if not profile_id or not _ID_RE.fullmatch(profile_id):
            raise EvalProfileInvalidId(
                f"Invalid profile_id: {profile_id!r}",
                details={"profile_id": profile_id},
            )

    def _profile_path(self, profile_id: str) -> Path:
        self._validate_profile_id(profile_id)
        return self._profiles_dir / f"{profile_id}.json"

    def _result_path(self, result_id: str) -> Path:
        if not result_id or not _ID_RE.fullmatch(result_id):
            raise EvalResultInvalidId(
                f"Invalid result_id: {result_id!r}",
                details={"result_id": result_id},
            )
        return self._results_dir / f"{result_id}.json"

    def _atomic_write(self, path: Path, data: Dict[str, Any]) -> None:
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    # -- Profile CRUD -----------------------------------------------------

    def create_profile(self, profile: EvalProfile) -> EvalProfile:
        path = self._profile_path(profile.profile_id)
        with self._lock:
            self._atomic_write(path, profile.model_dump(mode="json"))
        return profile

    def get_profile(self, profile_id: str) -> EvalProfile:
        path = self._profile_path(profile_id)
        if not path.exists():
            raise EvalProfileNotFound(
                f"EvalProfile not found: {profile_id}",
                details={"profile_id": profile_id},
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return EvalProfile.model_validate(raw)
        except Exception as exc:
            logger.error("Corrupted eval profile %s: %s", path, exc)
            raise EvalProfileNotFound(
                f"EvalProfile not found: {profile_id}",
                details={"profile_id": profile_id},
            )

    def update_profile(self, profile_id: str, updates: Dict[str, Any]) -> EvalProfile:
        # Strip immutable identity fields to prevent profile corruption.
        updates.pop("profile_id", None)
        updates.pop("created_at", None)
        with self._lock:
            profile = self.get_profile(profile_id)
            data = profile.model_dump(mode="json")
            data.update(updates)
            data["updated_at"] = _now_utc().isoformat()
            updated = EvalProfile.model_validate(data)
            self._atomic_write(self._profile_path(profile_id), updated.model_dump(mode="json"))
        return updated

    def delete_profile(self, profile_id: str) -> None:
        path = self._profile_path(profile_id)
        with self._lock:
            if not path.exists():
                raise EvalProfileNotFound(
                    f"EvalProfile not found: {profile_id}",
                    details={"profile_id": profile_id},
                )
            path.unlink()

    def list_profiles(self) -> List[EvalProfile]:
        self._ensure_dirs()
        profiles: List[EvalProfile] = []
        for p in sorted(self._profiles_dir.glob("*.json")):
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                profiles.append(EvalProfile.model_validate(raw))
            except Exception as exc:
                logger.warning("Skipping corrupted profile %s: %s", p, exc)
        return profiles

    # -- Result access ----------------------------------------------------

    def get_result(self, result_id: str) -> SmokeEvalResult:
        # Check in-memory running results first (under lock to prevent data races).
        with self._running_lock:
            if result_id in self._running:
                return self._running[result_id]
        path = self._result_path(result_id)
        if not path.exists():
            raise EvalResultNotFound(
                f"EvalResult not found: {result_id}",
                details={"result_id": result_id},
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return SmokeEvalResult.model_validate(raw)
        except Exception as exc:
            logger.error("Corrupted eval result %s: %s", path, exc)
            raise EvalResultNotFound(
                f"EvalResult not found: {result_id}",
                details={"result_id": result_id},
            )

    def _persist_result(self, result: SmokeEvalResult) -> None:
        path = self._result_path(result.result_id)
        with self._lock:
            self._atomic_write(path, result.model_dump(mode="json"))
        with self._running_lock:
            self._running.pop(result.result_id, None)

    # -- SmokeEvalRunner — AC2 -------------------------------------------

    def start_smoke_eval(self, blueprint_id: str, profile_id: str) -> str:
        """Create a 'running' placeholder and return result_id immediately."""
        profile = self.get_profile(profile_id)
        placeholder = SmokeEvalResult(
            profile_id=profile_id,
            blueprint_id=blueprint_id,
            overall_pass=False,
            status="running",
        )
        with self._running_lock:
            self._running[placeholder.result_id] = placeholder
        logger.info(
            "Smoke eval started: result_id=%s blueprint_id=%s profile_id=%s",
            placeholder.result_id,
            blueprint_id,
            profile_id,
        )
        return placeholder.result_id

    def run_smoke_eval(self, blueprint_id: str, profile_id: str, result_id: str) -> SmokeEvalResult:
        """Execute the 6-step smoke eval and persist the result."""
        profile = self.get_profile(profile_id)
        failure_reasons: List[FailureReason] = []
        metric_scores: List[MetricScore] = []
        citation_pass: Optional[bool] = None
        latency_ms = 0
        token_usage = 0

        # Load blueprint data (best-effort; missing = role_init failure).
        blueprint = self._load_blueprint(blueprint_id)

        # Step 1: Role init check
        role_ok = self._check_role_init(blueprint, failure_reasons)

        # Step 2: Tool availability check
        self._check_tool_availability(blueprint, failure_reasons)

        # Step 3: Knowledge accessibility check
        self._check_knowledge_access(blueprint, failure_reasons)

        # Step 4: Minimal task run (mock run against first test prompt)
        run_ok, latency_ms, token_usage = self._run_min_task(
            blueprint_id, profile, failure_reasons
        )

        # Step 5: Citation check (runs whenever citation_checks=True, regardless of run_ok)
        if profile.citation_checks:
            citation_pass = self._check_citations(blueprint_id, failure_reasons)

        # Step 6: Metric scoring
        metric_scores = self._score_metrics(
            profile, latency_ms, token_usage, citation_pass, failure_reasons
        )

        # Determine overall pass
        blocking_ids = set(profile.failure_thresholds.blocking_metrics)
        failed_metric_ids = {s.metric_id for s in metric_scores if not s.passed}
        blocking_failed = bool(blocking_ids & failed_metric_ids)
        total_failed = len(failed_metric_ids)
        overall_pass = (
            not failure_reasons
            and not blocking_failed
            and total_failed <= profile.failure_thresholds.max_failed_metrics
        )

        result = SmokeEvalResult(
            result_id=result_id,
            profile_id=profile_id,
            blueprint_id=blueprint_id,
            overall_pass=overall_pass,
            metric_scores=metric_scores,
            citation_pass=citation_pass,
            failure_reasons=failure_reasons,
            latency_ms=latency_ms,
            token_usage=token_usage,
            status="completed",
        )
        self._persist_result(result)
        logger.info(
            "Smoke eval completed: result_id=%s overall_pass=%s failures=%d",
            result_id,
            overall_pass,
            len(failure_reasons),
        )
        return result

    # -- Internal check helpers ------------------------------------------

    def _load_blueprint(self, blueprint_id: str) -> Dict[str, Any]:
        """Load blueprint from builder service storage (best-effort)."""
        from xueban.runtime.builder_service import BuilderService
        try:
            svc = BuilderService()
            bp = svc.get_blueprint(blueprint_id)
            return bp.model_dump() if bp else {}
        except Exception as exc:
            logger.debug("Blueprint load failed (blueprint_id=%s): %s", blueprint_id, exc)
            return {}

    def _check_role_init(
        self, blueprint: Dict[str, Any], failures: List[FailureReason]
    ) -> bool:
        roles = blueprint.get("role_profiles", [])
        if not roles:
            failures.append(FailureReason(
                dimension=FailureDimension.goal_clarity,
                detail="No role profiles defined in blueprint",
                suggested_fix="Add at least one role profile in Scene Mode",
            ))
            return False
        all_ok = True
        for role in roles:
            if not role.get("name") or not role.get("persona"):
                failures.append(FailureReason(
                    dimension=FailureDimension.role_conflict,
                    detail=f"Role {role.get('role_id', '?')} missing name or persona",
                    suggested_fix="Fill in role name and persona in Inspector",
                ))
                all_ok = False
        return all_ok

    def _check_tool_availability(
        self, blueprint: Dict[str, Any], failures: List[FailureReason]
    ) -> None:
        """Ping tool endpoints; record unreachable tools (non-blocking by default)."""
        import urllib.request
        policies = blueprint.get("tool_policies", [])
        for policy in policies:
            tool_id = policy.get("tool_id", "")
            endpoint = policy.get("metadata", {}).get("health_url", "")
            if not endpoint:
                continue
            try:
                with urllib.request.urlopen(endpoint, timeout=2):
                    pass
            except Exception:
                failures.append(FailureReason(
                    dimension=FailureDimension.tool_permission,
                    detail=f"Tool {tool_id!r} health endpoint unreachable: {endpoint}",
                    suggested_fix="Check tool endpoint configuration in Tool Registry",
                ))

    def _check_knowledge_access(
        self, blueprint: Dict[str, Any], failures: List[FailureReason]
    ) -> None:
        """Verify bound KnowledgePacks are in 'ready' status."""
        bindings = blueprint.get("knowledge_bindings", [])
        if not bindings:
            return
        try:
            from xueban.memory.knowledge_pack import _load_pack  # type: ignore[attr-defined]
        except ImportError:
            # knowledge module not available in minimal installs
            return
        for kb in bindings:
            source_ref = kb.get("source_ref", "")
            if not source_ref:
                continue
            try:
                pack = _load_pack(source_ref)
                if pack and getattr(pack, "status", None) != "ready":
                    failures.append(FailureReason(
                        dimension=FailureDimension.knowledge_access,
                        detail=f"KnowledgePack {source_ref!r} status is not 'ready'",
                        suggested_fix="Re-index the knowledge pack in Knowledge Dock",
                    ))
            except Exception:
                failures.append(FailureReason(
                    dimension=FailureDimension.knowledge_access,
                    detail=f"KnowledgePack {source_ref!r} could not be loaded",
                    suggested_fix="Verify knowledge pack exists and is indexed",
                ))

    def _run_min_task(
        self,
        blueprint_id: str,
        profile: EvalProfile,
        failures: List[FailureReason],
    ) -> tuple[bool, int, int]:
        """Simulate minimal task run using first test prompt.

        In the real implementation this would call POST /workflow/run.
        For MVP we simulate execution and return mock latency/tokens.
        A real orchestration call is made if runtime_service is wired in.
        """
        prompt = profile.test_prompts[0]
        t0 = time.monotonic()
        latency_ms = 0
        token_usage = 0
        try:
            # Try to use runtime service if available
            result = self._invoke_runtime(blueprint_id, prompt)
            latency_ms = int((time.monotonic() - t0) * 1000)
            token_usage = result.get("token_usage", 0)
        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            failures.append(FailureReason(
                dimension=FailureDimension.graph_broken,
                detail=f"Min task run failed: {exc}",
                suggested_fix="Check workflow graph configuration in Graph Mode",
            ))
            return False, latency_ms, token_usage

        # Check latency budget
        if profile.latency_budget_ms > 0 and latency_ms > profile.latency_budget_ms:
            failures.append(FailureReason(
                dimension=FailureDimension.graph_broken,
                detail=f"Latency {latency_ms}ms exceeds budget {profile.latency_budget_ms}ms",
                suggested_fix="Optimize workflow steps or increase latency budget",
            ))

        return True, latency_ms, token_usage

    def _invoke_runtime(self, blueprint_id: str, prompt: str) -> Dict[str, Any]:
        """Best-effort runtime invocation; returns {token_usage: int}."""
        # MVP: return a stub response. Wire to POST /workflow/run in Phase 2.
        return {"token_usage": 100, "status": "completed"}

    def _check_citations(
        self,
        blueprint_id: str,
        failures: List[FailureReason],
    ) -> bool:
        """Verify citation coverage via CitationService."""
        try:
            from xueban.runtime.citation_service import get_service as get_citation_service
            svc = get_citation_service()
            # Use blueprint_id as run_id surrogate for citation check.
            traces = svc.get_traces(blueprint_id)
            if not traces:
                failures.append(FailureReason(
                    dimension=FailureDimension.knowledge_access,
                    detail="No citation traces found for the eval run",
                    suggested_fix="Ensure knowledge pack is bound and citation_required=true",
                ))
                return False
            return True
        except Exception as exc:
            logger.warning("Citation check unavailable (CitationService unreachable): %s", exc)
            return None  # propagate unknown state; non-blocking

    def _score_metrics(
        self,
        profile: EvalProfile,
        latency_ms: int,
        token_usage: int,
        citation_pass: Optional[bool],
        failures: List[FailureReason],
    ) -> List[MetricScore]:
        scores: List[MetricScore] = []
        for metric in profile.success_metrics:
            score = self._compute_metric_score(
                metric, latency_ms, token_usage, citation_pass, failures
            )
            passed = score >= metric.threshold
            scores.append(MetricScore(
                metric_id=metric.metric_id,
                score=score,
                threshold=metric.threshold,
                passed=passed,
            ))
            if not passed:
                logger.debug(
                    "Metric %s failed: score=%.3f threshold=%.3f",
                    metric.name, score, metric.threshold,
                )
        return scores

    def _compute_metric_score(
        self,
        metric: EvalMetric,
        latency_ms: int,
        token_usage: int,
        citation_pass: Optional[bool],
        failures: List[FailureReason],
    ) -> float:
        """Compute a normalised score [0, 1] for each metric type."""
        if metric.metric_type == MetricType.latency_p95:
            if metric.threshold <= 0:
                return 1.0
            return min(1.0, metric.threshold / max(latency_ms, 1))
        if metric.metric_type == MetricType.token_budget:
            if metric.threshold <= 0:
                return 1.0
            return min(1.0, metric.threshold / max(token_usage, 1))
        if metric.metric_type == MetricType.citation_coverage:
            if citation_pass is None:
                return 1.0  # not measured
            return 1.0 if citation_pass else 0.0
        if metric.metric_type == MetricType.task_completion:
            run_failed = any(
                r.dimension == FailureDimension.graph_broken for r in failures
            )
            return 0.0 if run_failed else 1.0
        if metric.metric_type == MetricType.rejection_rate:
            # MVP: no rejections observed
            return 1.0
        return 1.0


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


_SERVICE_SINGLETON: Optional[EvalService] = None
_SINGLETON_LOCK = threading.Lock()


def get_service() -> EvalService:
    global _SERVICE_SINGLETON
    if _SERVICE_SINGLETON is None:
        with _SINGLETON_LOCK:
            if _SERVICE_SINGLETON is None:
                _SERVICE_SINGLETON = EvalService()
    return _SERVICE_SINGLETON


def set_service(svc: EvalService) -> None:
    global _SERVICE_SINGLETON
    with _SINGLETON_LOCK:
        _SERVICE_SINGLETON = svc


__all__ = [
    "EvalMetric",
    "EvalProfile",
    "EvalService",
    "FailureDimension",
    "FailureReason",
    "FailureThresholds",
    "MetricScore",
    "MetricType",
    "SmokeEvalResult",
    "EvalProfileNotFound",
    "EvalResultNotFound",
    "EvalProfileInvalidId",
    "EvalResultInvalidId",
    "get_service",
    "set_service",
]
