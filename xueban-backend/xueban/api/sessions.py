"""Sessions API — Story 11.5

Endpoints:
  POST   /api/sessions/{session_id}/checkpoint   — manual checkpoint
  GET    /api/sessions/{session_id}/context      — get workspace context
  GET    /api/sessions/{session_id}/report        — get task report (if complete)
  DELETE /api/sessions/{session_id}/context      — clear context (restart)
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from xueban.runtime.workspace_context import (
    AgentWorkspaceContext,
    CheckpointSummary,
    get_store,
)

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

# Module-level report cache with LRU eviction (prevents unbounded memory growth)
_REPORT_CACHE_MAX_SIZE = 1000
_report_cache: "OrderedDict[str, Any]" = OrderedDict()


def set_report(agent_id: str, session_id: str, report: Any) -> None:
    key = f"{agent_id}:{session_id}"
    _report_cache[key] = report
    _report_cache.move_to_end(key)
    while len(_report_cache) > _REPORT_CACHE_MAX_SIZE:
        _report_cache.popitem(last=False)


def _get_report(agent_id: str, session_id: str) -> Optional[Any]:
    return _report_cache.get(f"{agent_id}:{session_id}")


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------


class CheckpointRequest(BaseModel):
    agent_id: str
    summary: Optional[str] = None


class ContextResponse(BaseModel):
    agent_id: str
    session_id: str
    task_description: str
    working_dir: str
    steps_taken: int
    segments: int
    last_updated: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/{session_id}/checkpoint", summary="手动创建检查点")
async def create_checkpoint(session_id: str, body: CheckpointRequest) -> Dict[str, Any]:
    store = get_store()
    ctx = store.load(body.agent_id, session_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail="Session context not found")

    segment_idx = len(ctx.checkpoint_summaries)
    summary = body.summary or f"手动检查点 — 段 {segment_idx + 1}，共 {len(ctx.completed_steps)} 步"
    ctx.add_checkpoint_summary(segment_idx, summary)
    store.save(ctx)
    return {"checkpoint_index": segment_idx, "summary": summary, "steps_taken": len(ctx.completed_steps)}


@router.get("/{session_id}/context", summary="获取工作区上下文")
async def get_context(session_id: str, agent_id: str) -> Dict[str, Any]:
    store = get_store()
    ctx = store.load(agent_id, session_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail="Session context not found")
    return {
        "agent_id": ctx.agent_id,
        "session_id": ctx.session_id,
        "task_description": ctx.task_description,
        "working_dir": ctx.working_dir,
        "steps_taken": len(ctx.completed_steps),
        "segments": len(ctx.checkpoint_summaries),
        "last_updated": ctx.last_updated.isoformat(),
        "recent_steps": [
            {
                "index": s.index,
                "tool_called": s.tool_called,
                "description": s.description,
                "result_summary": s.result_summary[:300],
                "status": s.status,
            }
            for s in ctx.completed_steps[-10:]
        ],
        "checkpoint_summaries": [
            {"segment_index": c.segment_index, "summary": c.summary, "steps": c.steps_in_segment}
            for c in ctx.checkpoint_summaries
        ],
    }


@router.get("/{session_id}/report", summary="获取结构化复现报告")
async def get_report(session_id: str, agent_id: str) -> Dict[str, Any]:
    report = _get_report(agent_id, session_id)
    if report is None:
        # Check if context exists (task in progress)
        store = get_store()
        ctx = store.load(agent_id, session_id)
        if ctx is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return {
            "status": "in_progress",
            "steps_taken": len(ctx.completed_steps),
            "segments": len(ctx.checkpoint_summaries),
        }
    if hasattr(report, "model_dump"):
        return report.model_dump()
    return dict(report)


@router.delete("/{session_id}/context", summary="清空工作区上下文，从头开始")
async def delete_context(session_id: str, agent_id: str) -> Dict[str, Any]:
    store = get_store()
    deleted = store.delete(agent_id, session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session context not found")
    return {"deleted": True, "session_id": session_id}
