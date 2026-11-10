"""Memory API — Story 9.3 AC4.

Routes:
  POST   /memory/profiles                    — create MemoryProfile
  GET    /memory/profiles                    — list all MemoryProfiles
  GET    /memory/profiles/{profile_id}       — get MemoryProfile
  PATCH  /memory/profiles/{profile_id}       — update fields
  DELETE /memory/profiles/{profile_id}       — delete profile
  POST   /memory/writeback/{run_id}          — manual writeback (policy="manual")
  GET    /memory/state/{agent_id}            — three-layer summary skeleton (Story 9.4 stub)

All responses use the {data, meta} standard envelope.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from xueban.memory.memory_profile import (
    CompressionPolicy,
    MemoryProfile,
    StateSyncPolicy,
    WritebackPolicy,
)

# 2026-08-19 前缀对齐：裸 /memory/* → /api/memory/*（网关只路由 /api/*，
# 与 xueban/api/knowledge.py 同批迁移；前端 src/api/memory.ts 已同步）。
router = APIRouter(prefix="/api/memory", tags=["memory"])

# In-memory store (replaced by DB in future story)
_profiles: Dict[str, MemoryProfile] = {}

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _check_profile_id(profile_id: str) -> None:
    if not _UUID_RE.match(profile_id):
        raise HTTPException(status_code=400, detail={"code": "INVALID_PROFILE_ID", "message": "profile_id must be a UUID"})


def _get_or_404(profile_id: str) -> MemoryProfile:
    _check_profile_id(profile_id)
    profile = _profiles.get(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail={"code": "PROFILE_NOT_FOUND", "message": f"profile {profile_id} not found"})
    return profile


# ------------------------------------------------------------------
# Request / response bodies
# ------------------------------------------------------------------

class CreateProfileRequest(BaseModel):
    working_memory_limit: int = Field(default=4096, ge=256)
    episodic_retention_days: int = Field(default=30, ge=0)
    semantic_retrieval_top_k: int = Field(default=5, ge=1)
    compression_top_k: int = Field(default=10, ge=1)
    writeback_policy: WritebackPolicy = WritebackPolicy.ON_TASK_COMPLETE
    state_sync_policy: StateSyncPolicy = StateSyncPolicy.LAZY
    compression_policy: CompressionPolicy = CompressionPolicy.NONE


class PatchProfileRequest(BaseModel):
    working_memory_limit: Optional[int] = Field(default=None, ge=256)
    episodic_retention_days: Optional[int] = Field(default=None, ge=0)
    semantic_retrieval_top_k: Optional[int] = Field(default=None, ge=1)
    compression_top_k: Optional[int] = Field(default=None, ge=1)
    writeback_policy: Optional[WritebackPolicy] = None
    state_sync_policy: Optional[StateSyncPolicy] = None
    compression_policy: Optional[CompressionPolicy] = None


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------

@router.post("/profiles", status_code=201)
async def create_profile(body: CreateProfileRequest) -> Dict[str, Any]:
    profile = MemoryProfile(**body.model_dump())
    _profiles[profile.profile_id] = profile
    return {"data": profile.model_dump(mode="json"), "meta": {"created_at": _utc_now().isoformat()}}


@router.get("/profiles")
async def list_profiles() -> Dict[str, Any]:
    """List all MemoryProfiles (newest-updated first).

    2026-08-23: 补齐列表端点，前端 MemoryProfilePanel 不再依赖 localStorage 逐个回填。
    """
    profiles = sorted(_profiles.values(), key=lambda p: p.updated_at, reverse=True)
    return {
        "data": [p.model_dump(mode="json") for p in profiles],
        "meta": {"count": len(profiles)},
    }


@router.get("/profiles/{profile_id}")
async def get_profile(profile_id: str) -> Dict[str, Any]:
    profile = _get_or_404(profile_id)
    return {"data": profile.model_dump(mode="json"), "meta": {}}


@router.patch("/profiles/{profile_id}")
async def update_profile(profile_id: str, body: PatchProfileRequest) -> Dict[str, Any]:
    profile = _get_or_404(profile_id)
    patch = body.model_dump(exclude_none=True)
    updated = profile.model_copy(update={**patch, "updated_at": _utc_now()})
    _profiles[profile_id] = updated
    return {"data": updated.model_dump(mode="json"), "meta": {}}


@router.delete("/profiles/{profile_id}", status_code=204)
async def delete_profile(profile_id: str) -> None:
    _get_or_404(profile_id)
    del _profiles[profile_id]


@router.post("/writeback/{run_id}", status_code=202)
async def manual_writeback(run_id: str) -> Dict[str, Any]:
    """Manually trigger memory writeback for a run (used when writeback_policy=manual).

    Returns 202 Accepted — the writeback is scheduled asynchronously via
    ContextBuilder. Full async queue integration is implemented in Story 9.4.
    """
    logger.info("manual_writeback requested for run_id=%s (stub — full impl in Story 9.4)", run_id)
    return {
        "data": {
            "run_id": run_id,
            "status": "writeback_triggered",
            "memories_recalled": 0,  # 14-1: stub — non-zero when Story 9.4 writeback lands
        },
        "meta": {
            "triggered_at": _utc_now().isoformat(),
            "note": "stub — ContextBuilder integration in Story 9.4",
        },
    }


@router.get("/stats")
async def get_memory_stats(agent_id: Optional[str] = None) -> Dict[str, Any]:
    """Return memory configuration parameters for MemoryStatsBar (Story 14.1 placeholder mode).

    Returns config-level values, not live counts — live counts require Story 9.4
    writeback implementation. agent_id is accepted but ignored until 9.4 lands.
    """
    profile: Optional[MemoryProfile] = None
    if agent_id:
        for p in _profiles.values():
            if p.profile_id == agent_id:
                profile = p
                break

    wml = profile.working_memory_limit if profile else 4096
    erd = profile.episodic_retention_days if profile else 30

    return {
        "data": {
            "working_memory_limit": wml,
            "episodic_retention_days": erd,
            "semantic_skills_count": 0,
            "_note": "Live counts pending Story 9.4 writeback implementation",
        },
        "meta": {"mode": "placeholder"},
    }


@router.get("/state/{agent_id}")
async def get_memory_state(agent_id: str) -> Dict[str, Any]:
    """Return three-layer memory summary for an agent (Story 9.4 stub)."""
    return {
        "data": {
            "agent_id": agent_id,
            "working": {"summary": None, "item_count": 0},
            "episodic": {"summary": None, "item_count": 0},
            "semantic": {"summary": None, "item_count": 0},
        },
        "meta": {"note": "stub — full implementation in Story 9.4"},
    }
