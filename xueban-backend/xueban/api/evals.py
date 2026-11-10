"""Eval API — Story 9.5 AC3.

Endpoints:
  POST   /evals/profiles                      — create EvalProfile
  GET    /evals/profiles                      — list EvalProfiles
  GET    /evals/profiles/{profile_id}         — get EvalProfile
  PATCH  /evals/profiles/{profile_id}         — update EvalProfile
  DELETE /evals/profiles/{profile_id}         — delete EvalProfile
  POST   /evals/run/{blueprint_id}            — trigger smoke eval (async)
  GET    /evals/results/{result_id}           — get SmokeEvalResult
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from xueban.runtime.eval_service import (
    EvalMetric,
    EvalProfile,
    EvalProfileInvalidId,
    EvalProfileNotFound,
    EvalResultInvalidId,
    EvalResultNotFound,
    EvalService,
    SmokeEvalResult,
    get_service,
)

router = APIRouter(prefix="/evals", tags=["evals"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _envelope(data: Any, **meta: Any) -> Dict[str, Any]:
    return {"data": data, "meta": meta}


def _handle_eval_error(exc: Exception) -> HTTPException:
    if isinstance(exc, EvalProfileNotFound):
        return HTTPException(status_code=404, detail={"error": {"code": exc.code, "message": str(exc)}})
    if isinstance(exc, EvalResultNotFound):
        return HTTPException(status_code=404, detail={"error": {"code": exc.code, "message": str(exc)}})
    if isinstance(exc, (EvalProfileInvalidId, EvalResultInvalidId)):
        return HTTPException(status_code=400, detail={"error": {"code": exc.code, "message": str(exc)}})
    return HTTPException(status_code=500, detail={"error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred"}})


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class UpdateProfileRequest(BaseModel):
    name: Optional[str] = None
    test_prompts: Optional[List[str]] = None
    success_metrics: Optional[List[EvalMetric]] = None
    expected_artifacts: Optional[List[str]] = None
    citation_checks: Optional[bool] = None
    latency_budget_ms: Optional[int] = None
    failure_thresholds: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Profile CRUD
# ---------------------------------------------------------------------------


@router.post("/profiles", status_code=201)
async def create_profile(profile: EvalProfile) -> Dict[str, Any]:
    svc: EvalService = get_service()
    try:
        created = svc.create_profile(profile)
        return _envelope(created.model_dump(mode="json"))
    except Exception as exc:
        raise _handle_eval_error(exc)


@router.get("/profiles")
async def list_profiles() -> Dict[str, Any]:
    svc: EvalService = get_service()
    profiles = svc.list_profiles()
    return _envelope([p.model_dump(mode="json") for p in profiles], total=len(profiles))


@router.get("/profiles/{profile_id}")
async def get_profile(profile_id: str) -> Dict[str, Any]:
    svc: EvalService = get_service()
    try:
        profile = svc.get_profile(profile_id)
        return _envelope(profile.model_dump(mode="json"))
    except Exception as exc:
        raise _handle_eval_error(exc)


@router.patch("/profiles/{profile_id}")
async def update_profile(profile_id: str, body: UpdateProfileRequest) -> Dict[str, Any]:
    svc: EvalService = get_service()
    updates = body.model_dump(exclude_unset=True)
    try:
        updated = svc.update_profile(profile_id, updates)
        return _envelope(updated.model_dump(mode="json"))
    except Exception as exc:
        raise _handle_eval_error(exc)


@router.delete("/profiles/{profile_id}", status_code=204)
async def delete_profile(profile_id: str) -> None:
    svc: EvalService = get_service()
    try:
        svc.delete_profile(profile_id)
    except Exception as exc:
        raise _handle_eval_error(exc)


# ---------------------------------------------------------------------------
# Smoke eval — async run
# ---------------------------------------------------------------------------


def _run_eval_background(svc: EvalService, blueprint_id: str, profile_id: str, result_id: str) -> None:
    import logging as _logging
    _log = _logging.getLogger(__name__)
    try:
        svc.run_smoke_eval(blueprint_id, profile_id, result_id)
    except Exception:
        _log.exception("Background smoke eval failed: result_id=%s", result_id)
        try:
            failed = SmokeEvalResult(
                result_id=result_id,
                profile_id=profile_id,
                blueprint_id=blueprint_id,
                overall_pass=False,
                status="failed",
            )
            svc._persist_result(failed)
        except Exception:
            _log.exception("Failed to persist error state for result_id=%s", result_id)


@router.post("/run/{blueprint_id}", status_code=202)
async def run_smoke_eval(
    blueprint_id: str,
    profile_id: str,
    background_tasks: BackgroundTasks,
) -> Dict[str, Any]:
    svc: EvalService = get_service()
    try:
        result_id = svc.start_smoke_eval(blueprint_id, profile_id)
    except Exception as exc:
        raise _handle_eval_error(exc)

    background_tasks.add_task(
        _run_eval_background, svc, blueprint_id, profile_id, result_id
    )
    return _envelope({"result_id": result_id, "status": "running"})


# ---------------------------------------------------------------------------
# Result query
# ---------------------------------------------------------------------------


@router.get("/results/{result_id}")
async def get_eval_result(result_id: str) -> Dict[str, Any]:
    svc: EvalService = get_service()
    try:
        result = svc.get_result(result_id)
        return _envelope(result.model_dump(mode="json"))
    except Exception as exc:
        raise _handle_eval_error(exc)
