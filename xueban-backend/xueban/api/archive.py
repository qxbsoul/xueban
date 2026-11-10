"""Trajectory archive endpoint (Story 4.8).

Cursor-paginated list of completed runs with search / window filter +
`badges` (rejections / approvals / aborted) aggregated from RunResult.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field


class RunBadges(BaseModel):
    rejections: int = 0
    approvals: int = 0
    aborted: bool = False


class ArchiveRunSummary(BaseModel):
    run_id: str
    workflow_id: str = ""
    template: str = ""
    intent: str = ""
    status: str = ""
    duration_ms: Optional[int] = None
    tokens_in: int = 0
    tokens_out: int = 0
    completed_at: Optional[datetime] = None
    badges: RunBadges = Field(default_factory=RunBadges)


@dataclass
class ArchiveService:
    runtime_service: Any = None

    def list_runs(
        self,
        search: str = "",
        window: str = "all",
        after_cursor: Optional[str] = None,
        limit: int = 30,
    ) -> Dict[str, Any]:
        limit = max(1, min(limit, 100))
        runs = self._collect()

        # Window filter (on completed_at)
        cutoff: Optional[datetime] = None
        now = datetime.now(timezone.utc)
        if window == "24h":
            cutoff = now - timedelta(hours=24)
        elif window == "7d":
            cutoff = now - timedelta(days=7)
        elif window == "30d":
            cutoff = now - timedelta(days=30)

        if cutoff is not None:
            runs = [
                r for r in runs
                if r.completed_at is None or r.completed_at >= cutoff
            ]

        # Search filter (intent/workflow/run_id contains)
        if search:
            needle = search.lower()
            runs = [
                r for r in runs
                if needle in (r.intent or "").lower()
                or needle in (r.workflow_id or "").lower()
                or needle in r.run_id.lower()
            ]

        # Sort desc by completed_at (running runs go last)
        runs.sort(key=lambda r: r.completed_at or datetime.fromtimestamp(0, tz=timezone.utc), reverse=True)

        # Cursor pagination
        start = 0
        if after_cursor:
            for i, r in enumerate(runs):
                if r.run_id == after_cursor:
                    start = i + 1
                    break

        page = runs[start:start + limit]
        next_cursor = page[-1].run_id if len(page) == limit and (start + limit) < len(runs) else None

        return {
            "data": {
                "runs": [r.model_dump(mode="json") for r in page],
                "next_cursor": next_cursor,
                "total_count": len(runs),
            },
            "meta": {"window": window, "search": search},
        }

    # ---- helpers ----
    def _collect(self) -> List[ArchiveRunSummary]:
        svc = self.runtime_service
        if svc is None:
            return []
        results: List[ArchiveRunSummary] = []
        for run_id, run_result in getattr(svc, "_runs", {}).items():
            run_rec = run_result.run
            rejections = sum(1 for s in run_result.steps if s.status in ("failed",))
            # Approval heuristic: count steps with node_type / kind hinting approval_gate
            approvals = 0
            for s in run_result.steps:
                kind = (s.metadata or {}).get("node_type") or ""
                if "approval" in str(kind).lower():
                    approvals += 1
            duration_ms: Optional[int] = None
            if run_rec.ended_at and run_rec.started_at:
                duration_ms = int((run_rec.ended_at - run_rec.started_at).total_seconds() * 1000)
            intent = str((run_rec.metadata or {}).get("intent") or (run_rec.metadata or {}).get("goal") or "")
            template = str((run_rec.metadata or {}).get("template") or run_rec.workflow_id)
            results.append(ArchiveRunSummary(
                run_id=run_id,
                workflow_id=run_rec.workflow_id,
                template=template,
                intent=intent,
                status=str(run_rec.status),
                duration_ms=duration_ms,
                completed_at=run_rec.ended_at,
                badges=RunBadges(
                    rejections=rejections,
                    approvals=approvals,
                    aborted=run_rec.status in ("cancelled",),
                ),
            ))
        return results


_SERVICE_SINGLETON: Optional[ArchiveService] = None


def get_service() -> ArchiveService:
    global _SERVICE_SINGLETON
    if _SERVICE_SINGLETON is None:
        _SERVICE_SINGLETON = ArchiveService()
    return _SERVICE_SINGLETON


def set_service(svc: ArchiveService) -> None:
    global _SERVICE_SINGLETON
    _SERVICE_SINGLETON = svc


router = APIRouter(tags=["archive"])


@router.get("/archive/runs")
async def list_archive_runs(
    search: str = Query(""),
    window: str = Query("all"),
    after_cursor: Optional[str] = Query(None),
    limit: int = Query(30, ge=1, le=100),
):
    if window not in {"24h", "7d", "30d", "all"}:
        raise HTTPException(status_code=422, detail="invalid window")
    svc = get_service()
    return svc.list_runs(search=search, window=window, after_cursor=after_cursor, limit=limit)
