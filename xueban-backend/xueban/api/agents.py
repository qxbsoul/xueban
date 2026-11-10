"""Agent Registry + Quick Create API — Story 2.10 AC5 + Story 12.1.

Endpoints (Registry — Story 2.10):
  GET  /api/agents/registry              — list all registered agents
  GET  /api/agents/registry/{agent_id}  — single agent manifest + status
  POST /api/agents/registry/refresh     — trigger re-handshake for an agent
  GET  /api/agents/routing-log          — task routing decisions log

Endpoints (Quick Create — Story 12.1):
  POST /api/agents               — quick-hire: name + soul → full Blueprint
  GET  /api/agents               — list created agents
  GET  /api/agents/{agent_id}    — get single created agent
  DELETE /api/agents/{agent_id}  — delete created agent

All success responses use {data, meta} envelope.
Errors raise HTTP exceptions with {error: {code, message}} body.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

from xueban.runtime.acp.registry import (
    AgentCapabilityManifest,
    AgentRegistryEntry,
    AgentRegistry,
    get_registry,
)
from xueban.runtime.contracts_builder import (
    AgentBlueprint,
    RoleProfile,
    ToolPolicy,
)
from xueban.runtime.defaults import (
    DEFAULT_EXECUTOR_KIND,
    DEFAULT_LLM_MODEL,
    DEFAULT_LLM_PROVIDER,
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_MCP_SERVERS,
)

# ---------------------------------------------------------------------------
# Quick Create storage
# ---------------------------------------------------------------------------

# Anchored to project root (xueban/api/agents.py → parents[2] = project root)
_AGENTS_DIR = Path(__file__).resolve().parents[2] / ".xueban" / "agents"

# Allowlist: agent_id must be alphanumeric, hyphens, underscores only
_AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _validate_agent_id(agent_id: str) -> None:
    if not _AGENT_ID_RE.match(agent_id):
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "INVALID_AGENT_ID", "message": "Invalid agent_id format"}},
        )


def _validate_avatar_color(color: Optional[str]) -> None:
    """avatar_color 必须是 #rgb/#rrggbb 十六进制（"" 视为清除，放行）。"""
    if color is None or color == "":
        return
    if not _HEX_COLOR_RE.match(color):
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "INVALID_AVATAR_COLOR", "message": "avatar_color must be a #rgb or #rrggbb hex string"}},
        )


def _agents_dir() -> Path:
    _AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    return _AGENTS_DIR


def _agent_path(agent_id: str) -> Path:
    agents_root = _agents_dir().resolve()
    resolved = (agents_root / f"{agent_id}.json").resolve()
    # Guard against path traversal — resolved path must be inside agents_root
    if not resolved.is_relative_to(agents_root):
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "INVALID_AGENT_ID", "message": "Invalid agent_id"}},
        )
    return resolved


def _save_agent(record: Dict[str, Any]) -> None:
    target = _agent_path(record["agent_id"])
    content = json.dumps(record, default=str).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-")
    try:
        os.write(fd, content)
        os.fsync(fd)
        os.close(fd)
        os.replace(tmp_name, str(target))
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            Path(tmp_name).unlink()
        except OSError:
            pass
        raise


def _load_agent(agent_id: str) -> Optional[Dict[str, Any]]:
    p = _agent_path(agent_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _list_agents() -> List[Dict[str, Any]]:
    d = _agents_dir()
    records = []
    for p in sorted(d.glob("*.json")):
        # 跳过设置文件（selection.json / *.tmp 等）——目录里只有 agent-*.json
        # 是 agent 记录；否则 selection.json 会被当 agent 混进列表（前端渲染
        # 成 name=None 的幽灵行，点击即 404）。
        if not p.name.startswith("agent-"):
            continue
        try:
            records.append(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("agents: skipping corrupt record %s: %s", p.name, exc)
    return records

router = APIRouter(prefix="/api/agents", tags=["agents"])


def _escape_fmt_str(s: str) -> str:
    """Escape { and } in user-controlled strings to prevent format-string injection."""
    return s.replace("{", "{{").replace("}", "}}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _envelope(data: Any, **meta: Any) -> Dict[str, Any]:
    return {"data": data, "meta": meta}


def _entry_to_dict(entry: AgentRegistryEntry) -> Dict[str, Any]:
    return {
        "agent_id": entry.manifest.agent_id,
        "display_name": entry.manifest.display_name,
        "version": entry.manifest.version,
        "tools": [t.name for t in entry.manifest.tools],
        "max_concurrency": entry.manifest.max_concurrency,
        "streaming": entry.manifest.streaming,
        "workspace_context": entry.manifest.workspace_context,
        "status": entry.status,
        "active_tasks": entry.active_tasks,
        "available_slots": entry.available_slots,
        "is_native": entry.is_native,
        "connected_at": entry.connected_at.isoformat(),
        "last_heartbeat": entry.last_heartbeat.isoformat(),
        "team_memberships": entry.team_memberships,
    }


def _manifest_to_dict(manifest: AgentCapabilityManifest) -> Dict[str, Any]:
    return {
        "agent_id": manifest.agent_id,
        "display_name": manifest.display_name,
        "version": manifest.version,
        "tools": [
            {"name": t.name, "description": t.description}
            for t in manifest.tools
        ],
        "max_concurrency": manifest.max_concurrency,
        "streaming": manifest.streaming,
        "memory": {
            "type": manifest.memory.type,
            "scope": manifest.memory.scope,
            "persistence": manifest.memory.persistence,
        },
        "protocols": manifest.protocols,
        "workspace_context": manifest.workspace_context,
    }


# ---------------------------------------------------------------------------
# Request/response bodies
# ---------------------------------------------------------------------------


class RefreshRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9_-]+$")


# 头像色：仅接受 #rgb / #rrggbb 十六进制，防止注入任意 CSS。
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


class QuickCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    # soul 即 persona。上限放宽到 8000:run-session「组建」保存时携带 LLM 生成的
    # 完整 persona(此前前端只传短副标题 sub,灵魂被削;现传 node.persona)。
    soul: str = Field(..., min_length=1, max_length=8000)
    workspace_id: str = "default"
    # 头像色（可选）：用户手选时传 #rrggbb；不传则前端按名字 hash 兜底。
    avatar_color: Optional[str] = Field(None, max_length=9)
    # 契约扩展(2026-06-01):组建保存把设计期的 model / tools 一并带过来,
    # 而不是退化成 Python 默认 blueprint。手动招人(只填 name+soul)不传 → 走默认。
    model: Optional[str] = Field(None, max_length=120)
    tools: Optional[List[str]] = Field(None, max_length=64)
    # RACI 分工(职责桶 → R/A/C/I)。组建保存由前端 deriveRaci 派生传入;手动招人不传 → {}。
    raci: Optional[Dict[str, str]] = None
    # ── ClawHub（2026-06）Agent 绑定 Claw + DiceBear 头像（全 Optional）──────
    # claw_id：驱动后端（Claw 完全接管语义）。仅格式校验；存在性由前端对照
    # GET /api/cli/detect 列表把关（Python 不持有注册表）。
    claw_id: Optional[str] = Field(None, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$|^$")
    # DiceBear 风格 id（见前端 src/config/clawhub.ts 的 DICEBEAR_STYLES）。
    avatar_style: Optional[str] = Field(None, max_length=40, pattern=r"^[a-z0-9-]+$|^$")
    # DiceBear 种子（缺省时前端按名字 hash）。
    avatar_seed: Optional[str] = Field(None, max_length=64)
    # 提示词生成的头像图（dataURL/URL；os-unified 2026-11-04）。dataURL 体积大，
    # 限 300KB（约 400KB base64 内），超出 422。
    avatar_image: Optional[str] = Field(None, max_length=400_000)


class QuickCreateResponse(BaseModel):
    agent_id: str
    blueprint: AgentBlueprint
    created_at: str
    source: str = "quick_hire"


class AgentPatchRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    soul: Optional[str] = Field(None, min_length=1, max_length=2000)
    skills: Optional[List[str]] = None
    tools: Optional[List[str]] = None
    # 传 "" 清除颜色（回到按名字 hash）；传 #rrggbb 设置。
    avatar_color: Optional[str] = Field(None, max_length=9)
    # ── ClawHub（2026-06）：传 "" 清空绑定/头像；传合法值设置 ──────────────────
    claw_id: Optional[str] = Field(None, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$|^$")
    avatar_style: Optional[str] = Field(None, max_length=40, pattern=r"^[a-z0-9-]+$|^$")
    avatar_seed: Optional[str] = Field(None, max_length=64)
    # 提示词生成头像图（os-unified 2026-11-04）：dataURL/URL；"" 清除。
    avatar_image: Optional[str] = Field(None, max_length=400_000)


class AgentChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/registry")
async def list_registry(
    status: Optional[Literal["online", "offline", "busy"]] = Query(None, description="Filter by status"),
    team_id: Optional[str] = Query(None, description="Filter by team membership"),
) -> Dict[str, Any]:
    """List all registered agents with their capability manifests and status."""
    registry = get_registry()
    entries = await registry.list_all()

    if status:
        entries = [e for e in entries if e.status == status]
    if team_id:
        entries = [e for e in entries if team_id in e.team_memberships]

    return _envelope(
        [_entry_to_dict(e) for e in entries],
        total=len(entries),
    )


@router.get("/registry/{agent_id}")
async def get_agent(agent_id: str) -> Dict[str, Any]:
    """Get a single agent's full manifest and status."""
    _validate_agent_id(agent_id)
    registry = get_registry()
    entry = await registry.get(agent_id)
    if not entry:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "AGENT_NOT_FOUND", "message": f"Agent '{agent_id}' not found in registry"}},
        )
    return _envelope({
        **_entry_to_dict(entry),
        "manifest": _manifest_to_dict(entry.manifest),
    })


@router.post("/registry/refresh")
async def refresh_agent(body: RefreshRequest) -> Dict[str, Any]:
    """Trigger re-handshake for a registered agent (marks it offline pending reconnection)."""
    registry = get_registry()
    ok = await registry.refresh_agent(body.agent_id)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "AGENT_NOT_FOUND", "message": f"Agent '{body.agent_id}' not found"}},
        )
    return _envelope({"agent_id": body.agent_id, "status": "offline", "queued_for_rehandshake": True})


@router.get("/routing-log")
async def get_routing_log(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    """Return the task routing decision log."""
    registry = get_registry()
    logs = await registry.get_routing_log(limit=limit, offset=offset)
    return _envelope(
        [
            {
                "log_id": log.log_id,
                "task_id": log.task_id,
                "subtask": log.subtask,
                "routed_to": log.routed_to,
                "reason": log.reason,
                "timestamp": log.timestamp.isoformat(),
            }
            for log in logs
        ],
        limit=limit,
        offset=offset,
        returned=len(logs),
    )


# ---------------------------------------------------------------------------
# Quick Create routes — Story 12.1
# ---------------------------------------------------------------------------


def _build_default_blueprint(
    name: str,
    soul: str,
    model: Optional[str] = None,
    tools: Optional[List[str]] = None,
    raci: Optional[Dict[str, str]] = None,
) -> AgentBlueprint:
    # 契约扩展:组建保存可带设计期的 model / tools / raci;不传则回退默认 / 空。
    tool_ids = [t for t in (tools or []) if isinstance(t, str) and t.strip()] or list(
        DEFAULT_MCP_SERVERS
    )
    # RACI:只接受合法档位,过滤脏值;为空则 {}。
    clean_raci = {
        str(k): str(v).upper()
        for k, v in (raci or {}).items()
        if isinstance(v, str) and str(v).upper() in ("R", "A", "C", "I")
    }
    role = RoleProfile(
        name=name,
        description=soul,
        persona=soul,
        tools=tool_ids,
        raci=clean_raci,
        executor_kind=DEFAULT_EXECUTOR_KIND,
        executor_provider=DEFAULT_LLM_PROVIDER,
        executor_model=(model.strip() if model and model.strip() else DEFAULT_LLM_MODEL),
    )
    tool_policies = [
        ToolPolicy(tool_id=tool_id, default_permission="allow")
        for tool_id in tool_ids
    ]
    return AgentBlueprint(
        name=name,
        goal=soul,
        role_profiles=[role],
        tool_policies=tool_policies,
        metadata={"source": "quick_hire", "max_iterations": DEFAULT_MAX_ITERATIONS},
    )


@router.post("")
async def quick_create_agent(body: QuickCreateRequest) -> Dict[str, Any]:
    _validate_avatar_color(body.avatar_color)
    blueprint = _build_default_blueprint(body.name, body.soul, body.model, body.tools, body.raci)
    now = datetime.now(timezone.utc).isoformat()
    agent_id = f"agent-{uuid4().hex[:12]}"
    record = {
        "agent_id": agent_id,
        "name": body.name,
        "soul": body.soul,
        "workspace_id": body.workspace_id,
        "blueprint": blueprint.model_dump(),
        "status": "idle",
        "source": "quick_hire",
        "created_at": now,
        # 头像色：手选才写；不传存 None（前端按名字 hash 兜底）。
        "avatar_color": body.avatar_color or None,
        # ── ClawHub（2026-06）：Claw 绑定 + DiceBear 头像；"" → None（清空语义）。
        "claw_id": (body.claw_id or "").strip() or None,
        "avatar_style": (body.avatar_style or "").strip() or None,
        "avatar_seed": (body.avatar_seed or "").strip() or None,
        "avatar_image": (body.avatar_image or "").strip() or None,
    }
    _save_agent(record)
    return _envelope({
        "agent_id": agent_id,
        "name": body.name,
        "avatar_color": record["avatar_color"],
        "claw_id": record["claw_id"],
        "avatar_style": record["avatar_style"],
        "avatar_seed": record["avatar_seed"],
        "avatar_image": record["avatar_image"],
        "blueprint": blueprint.model_dump(),
        "created_at": now,
        "source": "quick_hire",
    })


@router.get("")
async def list_agents(workspace_id: Optional[str] = Query(None, max_length=200)) -> Dict[str, Any]:
    records = _list_agents()
    if workspace_id:
        # D3: normalize missing/blank workspace_id to the UNASSIGNED sentinel so
        # legacy agents surface under ?workspace_id=unassigned (and never leak
        # into an arbitrary workspace's view). Mirrors teams._list_teams.
        from xueban.api.teams import normalize_workspace_id
        records = [
            r for r in records
            if normalize_workspace_id(r.get("workspace_id")) == workspace_id
        ]
    return _envelope(records, total=len(records))


@router.get("/{agent_id}")
async def get_created_agent(agent_id: str) -> Dict[str, Any]:
    _validate_agent_id(agent_id)
    # Skip if this looks like a registry path handled elsewhere
    if agent_id in ("registry", "routing-log", "refresh"):
        raise HTTPException(status_code=404, detail="Not found")
    record = _load_agent(agent_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "AGENT_NOT_FOUND", "message": f"Agent '{agent_id}' not found"}},
        )
    return _envelope(record)


def _remove_agent_from_teams(agent_id: str) -> None:
    """Remove agent_id from all team files after agent deletion (cascade cleanup)."""
    import json as _json
    # 跟随 _AGENTS_DIR（同 .xueban 根），生产路径不变，但测试 monkeypatch
    # _AGENTS_DIR 时这里也一起隔离，不会改写真实 .xueban/teams。
    teams_dir = _AGENTS_DIR.parent / "teams"
    if not teams_dir.exists():
        return
    for team_file in teams_dir.glob("*.json"):
        try:
            data = _json.loads(team_file.read_text(encoding="utf-8"))
            ids: list = data.get("agent_ids", [])
            if agent_id in ids:
                data["agent_ids"] = [aid for aid in ids if aid != agent_id]
                team_file.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass  # best-effort; don't block the delete response


@router.delete("/{agent_id}")
async def delete_agent(agent_id: str) -> Dict[str, Any]:
    _validate_agent_id(agent_id)
    p = _agent_path(agent_id)
    if not p.exists():
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "AGENT_NOT_FOUND", "message": f"Agent '{agent_id}' not found"}},
        )
    await get_registry().unregister(agent_id)
    p.unlink()
    # Cascade: remove this agent from all team agent_ids lists
    _remove_agent_from_teams(agent_id)
    return _envelope({"deleted": True, "agent_id": agent_id})


# ---------------------------------------------------------------------------
# PATCH — direct field update
# ---------------------------------------------------------------------------


@router.patch("/{agent_id}")
async def patch_agent(agent_id: str, body: AgentPatchRequest) -> Dict[str, Any]:
    _validate_agent_id(agent_id)
    record = _load_agent(agent_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "AGENT_NOT_FOUND", "message": f"Agent '{agent_id}' not found"}},
        )
    bp = record.get("blueprint", {})
    if body.name is not None:
        record["name"] = body.name
        bp["name"] = body.name
    if body.soul is not None:
        record["soul"] = body.soul
        bp["goal"] = body.soul
        if bp.get("role_profiles") and len(bp["role_profiles"]) > 0:
            bp["role_profiles"][0]["description"] = body.soul
            bp["role_profiles"][0]["persona"] = body.soul
    if body.avatar_color is not None:
        _validate_avatar_color(body.avatar_color)
        # "" → 清除（回到按名字 hash）；#rrggbb → 设置。
        record["avatar_color"] = body.avatar_color or None
    # ── ClawHub（2026-06）："" = 显式清空；合法值 = 设置。与 avatar_color 同语义。
    if body.claw_id is not None:
        record["claw_id"] = body.claw_id.strip() or None
    if body.avatar_style is not None:
        record["avatar_style"] = body.avatar_style.strip() or None
    if body.avatar_seed is not None:
        record["avatar_seed"] = body.avatar_seed.strip() or None
    # os-unified（2026-11-04）：提示词生成头像图；"" = 清除。
    if body.avatar_image is not None:
        record["avatar_image"] = body.avatar_image.strip() or None
    if body.skills is not None:
        if bp.get("role_profiles") and len(bp["role_profiles"]) > 0:
            bp["role_profiles"][0]["skills"] = body.skills
    if body.tools is not None:
        if bp.get("role_profiles") and len(bp["role_profiles"]) > 0:
            bp["role_profiles"][0]["tools"] = body.tools
        bp["tool_policies"] = [
            {"tool_id": t, "default_permission": "allow"} for t in body.tools
        ]
    record["blueprint"] = bp
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_agent(record)
    return _envelope(record)


# ---------------------------------------------------------------------------
# Pause / resume — runtime state toggle (no LLM cost while paused)
# ---------------------------------------------------------------------------


_VALID_RUNTIME_STATUSES = {"idle", "running", "paused", "error"}


def _set_agent_status(agent_id: str, target: str) -> Dict[str, Any]:
    """Persist the agent's runtime status. Returns updated record."""
    if target not in _VALID_RUNTIME_STATUSES:
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "INVALID_STATUS", "message": f"status must be one of {sorted(_VALID_RUNTIME_STATUSES)}"}},
        )
    record = _load_agent(agent_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "AGENT_NOT_FOUND", "message": f"Agent '{agent_id}' not found"}},
        )
    record["status"] = target
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_agent(record)
    return record


@router.post("/{agent_id}/pause")
async def pause_agent(agent_id: str) -> Dict[str, Any]:
    """Pause a running agent — sets status to 'paused'.

    Idempotent: pausing an already-paused agent is a no-op success.
    """
    _validate_agent_id(agent_id)
    record = _set_agent_status(agent_id, "paused")
    return _envelope(record)


@router.post("/{agent_id}/resume")
async def resume_agent(agent_id: str) -> Dict[str, Any]:
    """Resume a paused agent — sets status back to 'idle' (ready)."""
    _validate_agent_id(agent_id)
    record = _set_agent_status(agent_id, "idle")
    return _envelope(record)


# ---------------------------------------------------------------------------
# Chat-driven editing — user describes changes in natural language
# ---------------------------------------------------------------------------

_AGENT_EDIT_SYSTEM_PROMPT = """\
You are an Agent configuration assistant. The user will describe changes to an AI agent's configuration in natural language. You must interpret their request and output a JSON object with the fields to update.

Current agent configuration:
- Name: {name}
- Soul (role description): {soul}
- Skills: {skills}
- Tools: {tools}

Respond with ONLY a JSON object (no markdown, no explanation outside JSON) with this structure:
{{
  "updates": {{
    "name": "new name or null if unchanged",
    "soul": "new soul text or null if unchanged",
    "skills": ["skill1", "skill2"] or null if unchanged,
    "tools": ["tool1", "tool2"] or null if unchanged
  }},
  "explanation": "Brief description of what you changed (in the user's language)"
}}

Rules:
- If the user asks to modify the soul/role/persona, update the "soul" field
- If the user asks to add skills, merge with existing skills
- If the user asks to remove skills, remove from existing list
- If the user asks to add tools, merge with existing tools
- If the user pastes a skill workflow link or path, extract skill names and add them
- Set unchanged fields to null
- Keep explanation concise (1-2 sentences)
- Match the user's language (Chinese or English)
"""


@router.post("/{agent_id}/chat")
async def agent_chat_edit(
    agent_id: str,
    body: AgentChatRequest,
    x_llm_key: Optional[str] = Header(None, alias="X-LLM-Key"),
    x_llm_provider: Optional[str] = Header(None, alias="X-LLM-Provider"),
    x_llm_model: Optional[str] = Header(None, alias="X-LLM-Model"),
) -> Dict[str, Any]:
    """Chat-driven agent editing: user describes changes, LLM interprets and applies."""
    _validate_agent_id(agent_id)
    record = _load_agent(agent_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "AGENT_NOT_FOUND", "message": f"Agent '{agent_id}' not found"}},
        )

    if not x_llm_key:
        raise HTTPException(
            status_code=401,
            detail={"error": {"code": "NO_API_KEY", "message": "X-LLM-Key header required for chat editing"}},
        )

    bp = record.get("blueprint", {})
    rp = bp.get("role_profiles", [{}])[0] if bp.get("role_profiles") else {}
    current_skills = rp.get("skills", [])
    current_tools = rp.get("tools", [])

    system_prompt = _AGENT_EDIT_SYSTEM_PROMPT.format(
        name=_escape_fmt_str(record.get("name", "")),
        soul=_escape_fmt_str(record.get("soul", "")),
        skills=json.dumps(current_skills, ensure_ascii=False),
        tools=json.dumps(current_tools, ensure_ascii=False),
    )

    try:
        from xueban.llm import LLMConfig, create_provider, ProviderType
    except ImportError as e:
        raise HTTPException(
            status_code=500,
            detail={"error": {"code": "LLM_UNAVAILABLE", "message": str(e)}},
        )

    # Default provider is "zhipu"; pass X-LLM-Provider header to use OpenAI/Claude/etc.
    provider_name = (x_llm_provider or "zhipu").lower()
    provider_map = {
        "zhipu": ProviderType.ZHIPU,
        "openai": ProviderType.OPENAI,
        "claude": ProviderType.CLAUDE,
        "ollama": ProviderType.OLLAMA,
        "deepseek": ProviderType.DEEPSEEK,
    }
    ptype = provider_map.get(provider_name)
    if ptype is None:
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "UNKNOWN_PROVIDER", "message": f"Unknown provider: {provider_name}"}},
        )

    from xueban.api.chat import DEFAULT_MODELS
    model = x_llm_model or DEFAULT_MODELS.get(provider_name, "")
    if not model:
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "NO_MODEL", "message": f"X-LLM-Model header required for provider '{provider_name}'"}},
        )
    config = LLMConfig(model=model, api_key=x_llm_key)

    try:
        provider = create_provider(ptype, config)
        msgs = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": body.message},
        ]
        response = await provider.chat(msgs)
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail={"error": {"code": "LLM_ERROR", "message": str(e)}},
        )

    # Parse LLM response as JSON
    content = response.content.strip()
    # Strip markdown code fences if present
    if content.startswith("```"):
        lines = content.split("\n")
        lines = [l for l in lines if not l.startswith("```")]
        content = "\n".join(lines)

    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "LLM_PARSE_ERROR", "message": "LLM response was not valid JSON"}},
        )

    updates = result.get("updates", {}) if isinstance(result, dict) else {}
    explanation = result.get("explanation", "") if isinstance(result, dict) else ""
    applied_fields: List[str] = []

    # Validate types before passing to Pydantic — prevent LLM from injecting unexpected field types
    def _str_or_none(v: Any) -> Optional[str]:
        return v if isinstance(v, str) else None

    def _list_of_str_or_none(v: Any) -> Optional[List[str]]:
        if isinstance(v, list) and all(isinstance(i, str) for i in v):
            return v
        return None

    patch = AgentPatchRequest(
        name=_str_or_none(updates.get("name")),
        soul=_str_or_none(updates.get("soul")),
        skills=_list_of_str_or_none(updates.get("skills")),
        tools=_list_of_str_or_none(updates.get("tools")),
    )

    if patch.name is not None:
        record["name"] = patch.name
        bp["name"] = patch.name
        applied_fields.append("name")
    if patch.soul is not None:
        record["soul"] = patch.soul
        bp["goal"] = patch.soul
        if bp.get("role_profiles") and len(bp["role_profiles"]) > 0:
            bp["role_profiles"][0]["description"] = patch.soul
            bp["role_profiles"][0]["persona"] = patch.soul
        applied_fields.append("soul")
    if patch.skills is not None:
        if bp.get("role_profiles") and len(bp["role_profiles"]) > 0:
            bp["role_profiles"][0]["skills"] = patch.skills
        applied_fields.append("skills")
    if patch.tools is not None:
        if bp.get("role_profiles") and len(bp["role_profiles"]) > 0:
            bp["role_profiles"][0]["tools"] = patch.tools
        bp["tool_policies"] = [
            {"tool_id": t, "default_permission": "allow"} for t in patch.tools
        ]
        applied_fields.append("tools")

    if applied_fields:
        record["blueprint"] = bp
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        _save_agent(record)

    return _envelope({
        "agent": record,
        "reply": explanation,
        "applied": bool(applied_fields),
        "applied_fields": applied_fields,
    })
