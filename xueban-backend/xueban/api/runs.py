"""Runs projection API — 暴露 RuntimeService 的 6 个 projection 端点.

对应前端 src/api/runs.ts 的 6 个端点：
  GET /api/runs/{runId}/graph              → workflow execution graph
  GET /api/runs/{runId}/task-tree          → task tree projection
  GET /api/runs/{runId}/artifact-lineage   → artifact lineage graph
  GET /api/runs/{runId}/memory-graph       → memory relation graph
  GET /api/runs/{runId}/checkpoint-lineage → checkpoint lineage graph
  GET /api/runs/{runId}/training-dataset   → activation training dataset

数据来源：RuntimeService 的 export_* 方法（已实现，缺 HTTP 端点）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from xueban.runtime.events import format_sse_event
from xueban.runtime.service import RuntimeService

router = APIRouter(tags=["runs"])


def _envelope(data: Any, **meta: Any) -> Dict[str, Any]:
    """统一返回格式 envelope."""
    return {"data": data, "meta": meta}


def _get_runtime() -> RuntimeService:
    """获取 RuntimeService singleton（xueban.server 模块级实例）。

    lazy import 避免循环依赖（server.py import 本 router）—— 与
    xueban/api/inbox.py 的 `from xueban.server import ...` 同模式。
    """
    from xueban.server import runtime_service as svc

    if svc is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "code": "RUNTIME_UNAVAILABLE",
                    "message": "RuntimeService not initialized",
                }
            },
        )
    return svc


def _extract_loop_record(output: Any) -> Optional[Dict[str, Any]]:
    """Pull the loop record out of a step output (current + legacy flat format)."""
    if not isinstance(output, dict):
        return None
    rec = output.get("loop_record")
    if isinstance(rec, dict):
        return rec
    if "loop_id" in output:
        # Legacy format: loop_* keys at top level (no persisted iterations).
        return output
    return None


def _loop_entries(run_result: Any) -> List[Dict[str, Any]]:
    """Extract loop entries from step outputs — shared by /loops and loops/stream."""
    loops: List[Dict[str, Any]] = []
    for step in run_result.steps:
        loop_data = _extract_loop_record(step.output)
        if loop_data is None:
            continue
        if "loop_record" not in (step.output or {}):
            # Legacy format: normalize loop_* top-level keys.
            loop_data = {
                "loop_id": step.output.get("loop_id"),
                "loop_kind": step.output.get("loop_kind"),
                "loop_status": step.output.get("loop_status"),
                "loop_iterations": step.output.get("loop_iterations", 0),
                "loop_converged": step.output.get("loop_converged", False),
                "loop_depth": step.output.get("loop_depth", 0),
                "loop_retried_count": step.output.get("loop_retried_count", 0),
                "loop_halted_by_watch": step.output.get("loop_halted_by_watch", False),
                "loop_memory_recalled": step.output.get("loop_memory_recalled", 0),
                "loop_memory_settled": step.output.get("loop_memory_settled", False),
            }
        loops.append({
            "step_id": step.step_id,
            "node_id": step.node_id,
            "status": step.status,
            "started_at": step.started_at.isoformat(),
            "ended_at": step.ended_at.isoformat() if step.ended_at else None,
            "loop": loop_data,
        })
    return loops


@router.get("/runs")
async def list_runs(limit: Optional[int] = Query(default=None, ge=1)) -> List[Dict[str, Any]]:
    """List historical runs (RunSummary shape), newest first.

    `limit` caps the number of entries returned. Raw array (NOT envelope) —
    matches the existing server.py `/runs` contract (List[RunSummary]) and the
    frontend listRuns() raw-array resolution (Story 15.1 review B2).
    """
    runtime = _get_runtime()
    summaries = runtime.list_runs()
    if limit is not None:
        summaries = summaries[:limit]
    return [item.model_dump(mode="json") for item in summaries]


@router.get("/runs/{run_id}/graph")
async def get_run_graph(run_id: str) -> Dict[str, Any]:
    """Get workflow execution graph for a run.

    Returns the flat RunGraph projection (top-level run_id/workflow_id/
    entrypoint + per-node run status) — matches the src/api/runs.ts contract;
    no envelope wrapper.
    """
    runtime = _get_runtime()
    projection = runtime.export_run_graph(run_id)
    if projection is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "RUN_NOT_FOUND", "message": f"Run {run_id!r} not found"}},
        )
    return projection.model_dump(mode="json")


@router.get("/runs/{run_id}/task-tree")
async def get_task_tree(run_id: str) -> Dict[str, Any]:
    """Get task tree projection for a run.

    Returns:
        TaskTreeProjection with run/task/step hierarchy.
    """
    runtime = _get_runtime()
    projection = runtime.export_task_tree(run_id)
    if projection is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "RUN_NOT_FOUND", "message": f"Run {run_id!r} not found"}},
        )

    return projection.model_dump(mode="json")


@router.get("/runs/{run_id}/artifact-lineage")
async def get_artifact_lineage(run_id: str) -> Dict[str, Any]:
    """Get artifact lineage graph for a run.

    Returns:
        ArtifactLineageProjection with artifact ancestry edges.
    """
    runtime = _get_runtime()
    projection = runtime.export_artifact_lineage(run_id=run_id)
    if projection is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "RUN_NOT_FOUND", "message": f"Run {run_id!r} not found"}},
        )

    return projection.model_dump(mode="json")


@router.get("/runs/{run_id}/memory-graph")
async def get_memory_graph(run_id: str) -> Dict[str, Any]:
    """Get memory relation graph for a run.

    Returns:
        MemoryRelationProjection with memory event/handoff/activation edges.
    """
    runtime = _get_runtime()
    projection = runtime.export_memory_relation_graph(run_id)
    if projection is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "RUN_NOT_FOUND", "message": f"Run {run_id!r} not found"}},
        )

    return projection.model_dump(mode="json")


@router.get("/runs/{run_id}/checkpoint-lineage")
async def get_checkpoint_lineage(run_id: str) -> Dict[str, Any]:
    """Get checkpoint lineage graph for a run.

    Returns:
        CheckpointLineageProjection with checkpoint ancestry edges.
    """
    runtime = _get_runtime()
    projection = runtime.export_checkpoint_lineage(run_id)
    if projection is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "RUN_NOT_FOUND", "message": f"Run {run_id!r} not found"}},
        )

    return projection.model_dump(mode="json")


@router.get("/runs/{run_id}/training-dataset")
async def get_training_dataset(run_id: str) -> Dict[str, Any]:
    """Get activation training dataset for a run.

    Returns:
        ActivationTrainingDataset with activation samples for RL training.
    """
    runtime = _get_runtime()
    projection = runtime.export_activation_training_dataset(run_id)
    if projection is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "RUN_NOT_FOUND", "message": f"Run {run_id!r} not found"}},
        )

    return projection.model_dump(mode="json")


@router.get("/runs/{run_id}/loops")
async def get_run_loops(run_id: str) -> Dict[str, Any]:
    """Get loop engine data for a run.

    Returns:
        List of loop records with iterations, convergence status, and metrics.
        Extracted from step outputs where node_type == "control.loop".
    """
    runtime = _get_runtime()
    run_result = runtime.get_run(run_id)
    if run_result is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "RUN_NOT_FOUND", "message": f"Run {run_id!r} not found"}},
        )

    loops = _loop_entries(run_result)
    return _envelope({
        "run_id": run_id,
        "loops": loops,
        "total_count": len(loops),
    })


@router.get("/runs/{run_id}/loops/{loop_id}/verdicts")
async def get_loop_verdicts(run_id: str, loop_id: str) -> Dict[str, Any]:
    """Verifier verdicts for a loop — per-iteration {iteration, score, rationale, claims[]}.

    Source: the persisted loop_record iterations (step.output.loop_record).
    evidence.sensor carries the LLM-as-a-Verifier fields written by
    RuntimeService._sense_with_verifier (verifier_score / verifier_reasoning),
    evidence.claims carries the Chain-of-Evidence claims. Non-verifier sensors
    fall back to the threshold classification as rationale.
    """
    runtime = _get_runtime()
    run_result = runtime.get_run(run_id)
    if run_result is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "RUN_NOT_FOUND", "message": f"Run {run_id!r} not found"}},
        )

    loop_record: Optional[Dict[str, Any]] = None
    for step in run_result.steps:
        rec = _extract_loop_record(step.output)
        if rec is not None and rec.get("loop_id") == loop_id:
            loop_record = rec
            break

    if loop_record is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "LOOP_NOT_FOUND", "message": f"Loop {loop_id!r} not found in run {run_id!r}"}},
        )

    verdicts: List[Dict[str, Any]] = []
    for iteration in loop_record.get("iterations") or []:
        if not isinstance(iteration, dict):
            continue
        evidence = iteration.get("evidence") or {}
        sensor = evidence.get("sensor") or {}
        score = iteration.get("verifier_score")
        if score is None:
            score = sensor.get("verifier_score")
        verdicts.append({
            "iteration": iteration.get("index"),
            "score": score,
            "rationale": sensor.get("verifier_reasoning") or sensor.get("classification"),
            "claims": evidence.get("claims") or [],
        })

    return _envelope({
        "run_id": run_id,
        "loop_id": loop_id,
        "loop_status": loop_record.get("status") or loop_record.get("loop_status"),
        "verdicts": verdicts,
    }, total=len(verdicts))


@router.get("/runs/{run_id}/loops/stream")
async def stream_loop_events(run_id: str, request: Request) -> StreamingResponse:
    """SSE stream of loop progress for a run — replaces 5s polling.

    First frame: `loop.snapshot` with the current loops state (same shape as
    GET /runs/{id}/loops `data`). Subsequent frames: incremental loop-domain
    facts (`loop.iteration` / `loop.completed` / `loop.halt_requested` /
    `task.ledger`, plus `node.retried` carrying a loop_id) — fanned out from
    RuntimeService._emit_run_event onto the RunEventBus. Supports the
    Last-Event-ID header / ?last_event_id= query param for reconnection,
    same as /workflow/runs/{id}/events. The stream may be attached before the
    run starts (snapshot is empty then) and terminates when the run closes.
    """
    runtime = _get_runtime()

    last_event_id_raw = (
        request.headers.get("last-event-id")
        or request.headers.get("Last-Event-ID")
        or request.query_params.get("last_event_id")
    )
    last_seq: Optional[int] = None
    if last_event_id_raw is not None:
        try:
            last_seq = int(last_event_id_raw)
        except ValueError:
            pass

    def _snapshot() -> Dict[str, Any]:
        run_result = runtime.get_run(run_id)
        loops = _loop_entries(run_result) if run_result is not None else []
        return {"run_id": run_id, "loops": loops, "total_count": len(loops)}

    def _is_loop_event(event: Any) -> bool:
        if isinstance(event, dict):
            event_type = event.get("type", "")
            loop_id = event.get("loop_id")
        else:
            event_type = getattr(event, "type", "")
            loop_id = None
        if isinstance(event_type, str) and event_type.startswith(("loop.", "task.ledger")):
            return True
        return event_type == "node.retried" and bool(loop_id)

    bus = runtime.event_bus

    async def _generate():
        # Snapshot frame uses the bus's latest seq so a Last-Event-ID
        # reconnect resumes right after it without replaying stale frames.
        latest = bus.latest_seq(run_id) if bus is not None else None
        yield format_sse_event(
            latest if latest is not None else -1,
            {"type": "loop.snapshot", **_snapshot()},
        )
        if bus is None:
            return
        async for seq, event in bus.subscribe(run_id, last_seq=last_seq):
            if _is_loop_event(event):
                yield format_sse_event(seq, event)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/runs/{run_id}/loops/{loop_id}/halt")
async def halt_loop(run_id: str, loop_id: str) -> Dict[str, Any]:
    """Halt a running loop.

    This endpoint allows frontend-initiated loop halting for control.loop nodes.
    It sets a signal that the loop engine checks on each iteration.

    Returns:
        Confirmation of the halt request.
    """
    runtime = _get_runtime()
    run_result = runtime.get_run(run_id)
    if run_result is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "RUN_NOT_FOUND", "message": f"Run {run_id!r} not found"}},
        )

    # Check if loop exists
    loop_found = False
    loop_status = None
    for step in run_result.steps:
        if isinstance(step.output, dict):
            loop_data = step.output.get("loop_record") or step.output
            if loop_data and loop_data.get("loop_id") == loop_id:
                loop_found = True
                loop_status = loop_data.get("loop_status") or loop_data.get("status")
                break

    if not loop_found:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "LOOP_NOT_FOUND", "message": f"Loop {loop_id!r} not found in run {run_id!r}"}},
        )

    # Cannot halt already completed loops
    if loop_status and loop_status in ("converged", "halted", "diverged"):
        return _envelope({
            "run_id": run_id,
            "loop_id": loop_id,
            "status": loop_status,
            "message": f"Loop is already {loop_status}, halt request ignored",
        }, already_halted=True)

    # Live halt: the loop engine consumes this signal at its next iteration
    # boundary (RuntimeService.request_loop_halt → _consume_loop_halt).
    runtime.request_loop_halt(run_id, loop_id)
    return _envelope({
        "run_id": run_id,
        "loop_id": loop_id,
        "status": "halt_requested",
        "message": "Halt signal set; the loop halts at its next iteration boundary.",
    })


@router.get("/runs/{run_id}/event-log")
async def get_run_event_log(run_id: str) -> Dict[str, Any]:
    """Durable append-only event log for a run (P1 evidence stream).

    Every durable fact (run/node lifecycle, checkpoints, loop iterations)
    lives here and survives process restarts — the replay source for all
    projections and the run-evidence export for competition audits.
    """
    runtime = _get_runtime()

    events = runtime.get_run_event_log(run_id)
    run_result = runtime.get_run(run_id)
    if not events and run_result is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "RUN_NOT_FOUND", "message": f"Run {run_id!r} not found"}},
        )

    from xueban.runtime.run_event_log import RunEventRecord, replay_run_summary

    records = [RunEventRecord.model_validate(evt) for evt in events]
    return _envelope({
        "run_id": run_id,
        "events": events,
        "summary": replay_run_summary(records),
    }, total=len(events))


# This router and xueban.server expose legacy handlers with identical
# function names and paths. Give this canonical router stable, distinct
# operation IDs so generated OpenAPI clients never receive duplicates.
for _route in router.routes:
    _route.operation_id = f"runtime_projection_{_route.name}"


# === Neo 2026-08-14: /api 双注册别名 =========================================
# Node proxyFallback（server/src/proxy-fallback.ts）转发前会重新拼上 /api
# 前缀，而本 router 历史路径是 /runs/...（tests/test_runtime_contract.py
# 直接打无前缀路径，保持不动）。两条路径都可用：/runs/... 供直连 Python，
# /api/runs/... 供 Vite → Node → proxyFallback → Python 端到端链路。
for _route in list(router.routes):
    _methods = sorted(getattr(_route, "methods", None) or {"GET"})
    router.add_api_route(
        "/api" + _route.path,
        _route.endpoint,
        methods=_methods,
        name=f"{_route.name}_api",
        tags=["runs"],
    )
