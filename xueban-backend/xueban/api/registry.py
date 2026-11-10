"""Agent Pack Registry API — Story 12.5 AC3, AC4, AC6, AC7.

Prefix: /api/agents/registry/packs  (list + install + installed)

Note: /api/agents/registry (ACP runtime registry) is already handled in
`xueban/api/agents.py` — this module adds Pack-level endpoints under
a longer prefix to avoid collision.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from xueban.services.registry_service import get_registry_service

router = APIRouter(prefix="/api/agents/registry/packs", tags=["agent-pack-registry"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _envelope(data: Any, **meta: Any) -> Dict[str, Any]:
    return {"data": data, "meta": meta}


def _http_error(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"error": {"code": code, "message": message, **extra}},
    )


import re as _re
_WORKSPACE_ID_RE = _re.compile(r"^[a-zA-Z0-9_-]+$")


def _validate_workspace_id(workspace_id: str) -> None:
    """Raise 400 if workspace_id contains characters outside [a-zA-Z0-9_-]."""
    if not _WORKSPACE_ID_RE.match(workspace_id):
        raise _http_error(
            400,
            "INVALID_WORKSPACE_ID",
            "workspace_id must match [a-zA-Z0-9_-]+",
        )


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class InstallRequest(BaseModel):
    pack_id: str = Field(..., min_length=1, max_length=120)
    workspace_id: str = Field(
        "default",
        min_length=1,
        max_length=80,
        pattern=r"^[a-zA-Z0-9_-]+$",
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
async def list_packs(
    workspace_id: str = Query("default"),
    tags: Optional[str] = Query(None, description="Comma-separated tags to filter"),
    q: Optional[str] = Query(None, description="Keyword search in name/description"),
) -> Dict[str, Any]:
    """List all available Agent Packs with install status."""
    _validate_workspace_id(workspace_id)
    svc = get_registry_service()
    entries = svc.list_manifests(tags=tags, q=q)
    result: List[Dict[str, Any]] = []
    for entry in entries:
        manifest = svc.get_manifest(entry.id)
        if manifest is None:
            continue
        rec = svc.get_installed_record(entry.id, workspace_id)
        verified = rec.get("verified", False) if rec else manifest.signature is not None
        result.append({
            "id": manifest.id,
            "version": manifest.version,
            "name": manifest.name,
            "description": manifest.description,
            "author": manifest.author,
            "tags": entry.tags,
            "capabilities_summary": manifest.capabilities.tools,
            "install_status": svc.install_status(manifest.id, workspace_id),
            "verified": verified,
        })
    return _envelope(result, total=len(result))


@router.post("/install", status_code=201)
async def install_pack(body: InstallRequest) -> Dict[str, Any]:
    """Install an Agent Pack — creates an AgentBlueprint record."""
    svc = get_registry_service()

    # Step 1 — find pack
    manifest = svc.get_manifest(body.pack_id)
    if manifest is None:
        raise _http_error(404, "PACK_NOT_FOUND", f"Pack '{body.pack_id}' not found in registry")

    # Step 2 — verify signature (AC5)
    # `verified` = True only when a signature is PRESENT *and* passes HMAC check.
    verified = False
    if manifest.signature is not None:
        if not svc.verify_signature(manifest):
            raise _http_error(
                400,
                "MANIFEST_SIGNATURE_INVALID",
                "Manifest signature verification failed",
                pack_id=body.pack_id,
            )
        verified = True  # signature present and verified successfully

    # Step 3 — already installed same version?
    installed_ver = svc.get_installed_version(body.pack_id, body.workspace_id)
    if installed_ver == manifest.version:
        rec = svc.get_installed_record(body.pack_id, body.workspace_id)
        return _envelope(
            {**(rec or {}), "already_installed": True},
            warnings=[],
            deprecations=[],
        )

    # Step 4 — install_cmd
    # Subprocess execution is intentionally deferred to a post-MVP story (no
    # sandboxing / privilege model exists yet).  For now we surface a warning
    # in the response so callers know the hook was NOT run.
    install_cmd_warning: Optional[str] = None
    if manifest.install_cmd:
        install_cmd_warning = (
            f"install_cmd '{manifest.install_cmd}' was NOT executed — "
            "automatic hook execution is not yet supported. Run it manually."
        )

    # Step 5 — create AgentBlueprint-compatible record
    agent_id = f"agent-{uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()
    blueprint = {
        "name": manifest.name,
        "goal": manifest.soul,
        "role_profiles": [
            {
                "name": manifest.name,
                "description": manifest.description,
                "persona": manifest.soul,
                "tools": manifest.capabilities.tools,
                "executor_kind": manifest.kind,
                "executor_provider": manifest.capabilities.llm_provider,
            }
        ],
        "tool_policies": [
            {"tool_id": t, "default_permission": "allow"}
            for t in manifest.capabilities.tools
        ],
        "metadata": {
            "source": "catalog",
            "pack_id": manifest.id,
            "pack_version": manifest.version,
            "verified": verified,
        },
    }

    # Step 6 — persist installed-packs record (AC4 storage schema)
    install_rec: Dict[str, Any] = {
        "pack_id": manifest.id,
        "pack_version": manifest.version,
        "agent_id": agent_id,
        "installed_at": now,
        "verified": verified,
        "blueprint": blueprint,
    }
    try:
        svc.upsert_installed(body.workspace_id, install_rec)
    except Exception as exc:
        raise _http_error(500, "PERSISTENCE_FAILED", f"Persistence failed: {exc}") from exc

    response_warnings: List[str] = []
    if not verified:
        response_warnings.append("Pack has no signature — treat as community/unverified")
    if install_cmd_warning:
        response_warnings.append(install_cmd_warning)

    return _envelope(
        {
            "agent_id": agent_id,
            "pack_id": manifest.id,
            "pack_version": manifest.version,
            "blueprint": blueprint,
            "installed_at": now,
            "verified": verified,
            "already_installed": False,
        },
        warnings=response_warnings,
        deprecations=[],
    )


@router.get("/installed")
async def list_installed(
    workspace_id: str = Query("default"),
) -> Dict[str, Any]:
    """List installed Agent Packs for a workspace."""
    _validate_workspace_id(workspace_id)
    svc = get_registry_service()
    items = svc.list_installed(workspace_id)
    return _envelope(items, total=len(items))
