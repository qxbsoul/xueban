"""Policy observability aggregator (Story 4.9).

GET /policy/stats?window=24h|7d|30d|all → {summary, heatmap, examples}
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field


Stage = Literal["intent", "plan", "review", "execute", "deliver"]
_STAGE_ORDER: List[Stage] = ["intent", "plan", "review", "execute", "deliver"]


class PolicyStatsSummary(BaseModel):
    total_rejections: int = 0
    total_runs: int = 0
    rejection_rate_pct: float = 0.0
    top_policy: Dict[str, Any] = Field(default_factory=lambda: {"name": "", "count": 0})
    top_stage: Dict[str, Any] = Field(default_factory=lambda: {"name": "", "count": 0})
    recovered_rate_pct: float = 0.0
    median_loops: float = 0.0


class HeatmapRow(BaseModel):
    policy: str
    counts: Dict[str, int] = Field(default_factory=dict)


class RejectExample(BaseModel):
    run_id: str
    stage: str
    timestamp: Optional[datetime] = None
    reason: str = ""
    outcome: Literal["retry_ok", "aborted", "pending"] = "pending"


class PolicyStats(BaseModel):
    summary: PolicyStatsSummary
    heatmap: List[HeatmapRow] = Field(default_factory=list)
    examples: Dict[str, List[RejectExample]] = Field(default_factory=dict)


_TTL_SECONDS = 15 * 60


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


@dataclass
class PolicyObsAggregator:
    event_bus: Any = None
    runtime_service: Any = None
    cache: Dict[str, _CacheEntry] = field(default_factory=dict)

    def clear_cache(self) -> None:
        self.cache.clear()

    def aggregate(self, window: str = "7d") -> PolicyStats:
        cached = self.cache.get(window)
        if cached is not None and time.time() < cached.expires_at:
            return cached.value

        cutoff: Optional[datetime] = None
        now = datetime.now(timezone.utc)
        if window == "24h":
            cutoff = now - timedelta(hours=24)
        elif window == "7d":
            cutoff = now - timedelta(days=7)
        elif window == "30d":
            cutoff = now - timedelta(days=30)

        violations = self._collect_violations(cutoff)
        total_runs = self._count_runs(cutoff)

        policy_count: Dict[str, int] = {}
        stage_count: Dict[str, int] = {}
        heatmap_map: Dict[str, Dict[str, int]] = {}
        examples: Dict[str, List[RejectExample]] = {}
        loop_counts: List[int] = []
        recovered = 0

        for v in violations:
            policy = v.get("policy") or v.get("sender") or "unknown"
            stage = str(v.get("stage") or "review").lower()
            if stage not in _STAGE_ORDER:
                stage = "review"
            policy_count[policy] = policy_count.get(policy, 0) + 1
            stage_count[stage] = stage_count.get(stage, 0) + 1
            row = heatmap_map.setdefault(policy, {s: 0 for s in _STAGE_ORDER})
            row[stage] = row.get(stage, 0) + 1
            examples.setdefault(policy, []).append(RejectExample(
                run_id=v.get("run_id", ""),
                stage=stage,
                timestamp=v.get("timestamp"),
                reason=v.get("reason", ""),
                outcome=v.get("outcome", "pending"),
            ))
            if v.get("outcome") == "retry_ok":
                recovered += 1
            loops = v.get("loops")
            if isinstance(loops, (int, float)):
                loop_counts.append(int(loops))

        total_rejections = len(violations)
        rate = (total_rejections / total_runs * 100.0) if total_runs else 0.0
        recovered_rate = (recovered / total_rejections * 100.0) if total_rejections else 0.0
        median_loops = statistics.median(loop_counts) if loop_counts else 0.0

        top_policy_name = max(policy_count, key=policy_count.get) if policy_count else ""
        top_stage_name = max(stage_count, key=stage_count.get) if stage_count else ""

        summary = PolicyStatsSummary(
            total_rejections=total_rejections,
            total_runs=total_runs,
            rejection_rate_pct=round(rate, 1),
            top_policy={"name": top_policy_name, "count": policy_count.get(top_policy_name, 0)},
            top_stage={"name": top_stage_name, "count": stage_count.get(top_stage_name, 0)},
            recovered_rate_pct=round(recovered_rate, 1),
            median_loops=float(median_loops),
        )

        heatmap = [
            HeatmapRow(policy=name, counts=row)
            for name, row in heatmap_map.items()
        ]
        # Cap examples to latest 5 per policy
        examples = {p: ex[-5:] for p, ex in examples.items()}

        stats = PolicyStats(summary=summary, heatmap=heatmap, examples=examples)
        self.cache[window] = _CacheEntry(value=stats, expires_at=time.time() + _TTL_SECONDS)
        return stats

    def _collect_violations(self, cutoff: Optional[datetime]) -> List[Dict[str, Any]]:
        bus = self.event_bus
        out: List[Dict[str, Any]] = []
        if bus is None:
            return out
        store = getattr(bus, "_store", {})
        for run_id, buffer in store.items():
            for _, evt in buffer:
                evt_dict = evt if isinstance(evt, dict) else getattr(evt, "__dict__", {})
                evt_type = evt_dict.get("type") or evt_dict.get("event")
                if evt_type not in ("policy.violation", "node.rejected"):
                    continue
                ts_raw = evt_dict.get("timestamp")
                ts: Optional[datetime] = None
                if isinstance(ts_raw, datetime):
                    ts = ts_raw
                elif isinstance(ts_raw, str):
                    try:
                        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    except Exception:
                        ts = None
                if cutoff is not None and ts is not None and ts < cutoff:
                    continue
                out.append({
                    "run_id": evt_dict.get("run_id") or run_id,
                    "policy": evt_dict.get("policy") or evt_dict.get("sender"),
                    "stage": evt_dict.get("stage"),
                    "reason": evt_dict.get("reason", ""),
                    "timestamp": ts,
                    "outcome": evt_dict.get("outcome"),
                    "loops": evt_dict.get("loops"),
                })
        return out

    def _count_runs(self, cutoff: Optional[datetime]) -> int:
        svc = self.runtime_service
        if svc is None:
            return 0
        try:
            runs = svc.list_runs()
        except Exception:
            return 0
        if cutoff is None:
            return len(runs)
        return sum(1 for r in runs if (r.started_at or datetime.fromtimestamp(0, tz=timezone.utc)) >= cutoff)


_AGG_SINGLETON: Optional[PolicyObsAggregator] = None


def get_aggregator() -> PolicyObsAggregator:
    global _AGG_SINGLETON
    if _AGG_SINGLETON is None:
        _AGG_SINGLETON = PolicyObsAggregator()
    return _AGG_SINGLETON


def set_aggregator(agg: PolicyObsAggregator) -> None:
    global _AGG_SINGLETON
    _AGG_SINGLETON = agg


router = APIRouter(tags=["policy-observability"])


@router.get("/policy/stats")
async def get_policy_stats(window: str = Query("7d")):
    if window not in {"24h", "7d", "30d", "all"}:
        raise HTTPException(status_code=422, detail="window must be 24h|7d|30d|all")
    agg = get_aggregator()
    stats = agg.aggregate(window)
    return {"data": stats.model_dump(mode="json"), "meta": {"window": window}}
