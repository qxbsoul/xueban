"""Citation API — Story 9.2 AC3.

Routes:
  GET /citations/{run_id}            — list citation traces (?node_id= filter)
  GET /citations/{run_id}/export     — structured citation report (JSON)

Success responses use the shared `{data, meta}` envelope. Errors raise
`XuebanError` (CITATION_NOT_FOUND maps to 404 in the global handler).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import APIRouter, Query

from xueban.runtime.citation_service import (
    CitationNotFound,
    CitationService,
    get_service,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _envelope(data: Any, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "data": data,
        "meta": {
            "trace_id": f"trace-{uuid4().hex[:12]}",
            "timestamp": _now(),
            **(meta or {}),
        },
    }


router = APIRouter(prefix="/citations", tags=["citations"])


def _resolve_service() -> CitationService:
    return get_service()


@router.get("/{run_id}")
async def list_citations(
    run_id: str,
    node_id: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    svc = _resolve_service()
    # Use require=True to atomically check + load within the service lock,
    # eliminating the TOCTOU window from a separate has_traces() + get_traces() call.
    traces = svc.get_traces(run_id, node_id=node_id, require=True)
    # citation_missing is True whenever the effective result set is empty.
    run_has_no_traces = len(traces) == 0
    return _envelope(
        data={
            "run_id": run_id,
            "traces": [t.model_dump(mode="json") for t in traces],
            "citation_missing": run_has_no_traces,
        },
        meta={"node_id": node_id, "total": len(traces)},
    )


@router.get("/{run_id}/export")
async def export_citations(run_id: str) -> Dict[str, Any]:
    svc = _resolve_service()
    # require=True raises CitationNotFound if no trace file exists.
    svc.get_traces(run_id, require=True)
    return _envelope(svc.export_traces(run_id))


__all__ = ["router"]
