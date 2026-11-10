"""Artifacts API — artifact 级别的小端点（GAP：产物 filecard 置顶）.

POST /api/artifacts/{artifact_id}/pin — toggle 置顶（持久化）

pin 状态落独立小仓 `.xueban/artifacts/artifact_pins.json`（存储细节见
xueban/runtime/artifact_pins.py）；artifact-lineage 投影在导出时把
`pinned` 标进 artifact 节点 metadata，前端无需额外拉取。
"""

from __future__ import annotations

import re
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from xueban.api._limiter import limiter
from xueban.runtime.artifact_pins import toggle_pin

router = APIRouter(tags=["artifacts"])

_ARTIFACT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def _envelope(data: Any, **meta: Any) -> Dict[str, Any]:
    """统一返回格式 envelope."""
    return {"data": data, "meta": meta}


def _validate_artifact_id(artifact_id: str) -> None:
    if not _ARTIFACT_ID_RE.match(artifact_id):
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "INVALID_ARTIFACT_ID", "message": "Invalid artifact_id format"}},
        )


@router.post("/api/artifacts/{artifact_id}/pin")
@limiter.limit("120/minute")
async def pin_artifact(request: Request, artifact_id: str) -> Dict[str, Any]:
    """Toggle pin state for one artifact (persisted).

    Toggle semantics: pinning an already-pinned artifact unpins it. The
    artifact_id is not checked against any run — artifact ids are globally
    unique (`artifact-<hex>`) and the pin is a pure display flag.
    """
    _validate_artifact_id(artifact_id)
    pinned_at = toggle_pin(artifact_id)
    return _envelope({
        "artifact_id": artifact_id,
        "pinned": pinned_at is not None,
        "pinned_at": pinned_at,
    })
