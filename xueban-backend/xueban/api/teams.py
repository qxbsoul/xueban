"""Team CRUD API — Story 12.2

Endpoints:
  POST   /api/teams                — create AgentTeam
  GET    /api/teams                — list teams (filter by workspace_id)
  GET    /api/teams/{team_id}      — get single team
  PATCH  /api/teams/{team_id}      — add/remove members
  DELETE /api/teams/{team_id}      — delete team
  POST   /api/teams/{team_id}/chat — chat-driven team editing

All success responses use {data, meta} envelope.
Errors raise HTTP exceptions with {error: {code, message}} body.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

_TEAMS_DIR = Path(__file__).resolve().parents[2] / ".xueban" / "teams"
_TEAM_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# D3: sentinel workspace for records that predate the workspace concept (no
# `workspace_id` field at all). We DO NOT resurrect the old "default" workspace
# (cancelled 2026-06-01, A design); legacy records are bucketed here so they
# stop acting as a cross-workspace wildcard. They surface only under an
# explicit ?workspace_id=unassigned filter (and in the unfiltered list).
UNASSIGNED_WORKSPACE = "unassigned"


def normalize_workspace_id(workspace_id: Optional[str]) -> str:
    """Backfill a missing/blank workspace_id to the UNASSIGNED sentinel.

    Explicit ids (including the legacy literal "default") are returned as-is.
    """
    if workspace_id is None or not str(workspace_id).strip():
        return UNASSIGNED_WORKSPACE
    return workspace_id


def _validate_team_id(team_id: str) -> None:
    if not _TEAM_ID_RE.match(team_id):
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "INVALID_TEAM_ID", "message": "Invalid team_id format"}},
        )


def _teams_dir() -> Path:
    _TEAMS_DIR.mkdir(parents=True, exist_ok=True)
    return _TEAMS_DIR


def _team_path(team_id: str) -> Path:
    teams_root = _teams_dir().resolve()
    resolved = (teams_root / f"{team_id}.json").resolve()
    if teams_root not in resolved.parents:
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "INVALID_TEAM_ID", "message": "Invalid team_id"}},
        )
    return resolved


def _save_team(record: Dict[str, Any]) -> None:
    target = _team_path(record["team_id"])
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


def _load_team(team_id: str) -> Optional[Dict[str, Any]]:
    p = _team_path(team_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _list_teams(workspace_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """List team records, optionally filtered by workspace_id.

    D3 (2026-06-06): legacy records without a `workspace_id` field are
    normalized to the UNASSIGNED sentinel for matching — they no longer act as
    a wildcard that leaks into every workspace's view. A filtered request only
    returns records whose (normalized) workspace_id equals the requested one;
    legacy records surface under ?workspace_id=unassigned. An unfiltered
    request (workspace_id is None) still returns everything.
    """
    d = _teams_dir()
    records = []
    for p in sorted(d.glob("*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
            if workspace_id is None:
                records.append(rec)
                continue
            if normalize_workspace_id(rec.get("workspace_id")) == workspace_id:
                records.append(rec)
        except (json.JSONDecodeError, OSError):
            pass
    return records


def _delete_team_file(team_id: str) -> None:
    p = _team_path(team_id)
    if p.exists():
        p.unlink()


def _escape_fmt_str(s: str) -> str:
    """Escape { and } in user-controlled strings to prevent format-string injection."""
    return s.replace("{", "{{").replace("}", "}}")


# ---------------------------------------------------------------------------
# Write-time validation (D1+D2)
# ---------------------------------------------------------------------------

from xueban.api import team_validation as _tv  # noqa: E402
from xueban.runtime import team_workflow_versions as _wf_versions  # noqa: E402


def _reject_dag(errors: List[str]) -> None:
    """Raise a 422 if the DAG structure validation produced errors."""
    if errors:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "INVALID_DAG",
                    "message": "Team workflow DAG is invalid: " + "; ".join(errors),
                    "errors": errors,
                }
            },
        )


def _reject_missing_agents(agent_ids: List[str]) -> None:
    """Raise a 422 if any agent_id does not resolve in the Python agent store.

    Skips empty/whitespace ids. De-dups before probing.
    """
    probe = [a for a in dict.fromkeys(agent_ids) if isinstance(a, str) and a.strip()]
    errors = _tv.validate_agent_ids(probe, exists=_tv.agent_exists_in_store)
    if errors:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "AGENT_NOT_FOUND",
                    "message": "Team references agents that do not exist: "
                    + "; ".join(errors),
                    "errors": errors,
                }
            },
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/teams", tags=["teams"])


def _envelope(data: Any, **meta: Any) -> Dict[str, Any]:
    return {"data": data, "meta": meta}


def _not_found(team_id: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"error": {"code": "TEAM_NOT_FOUND", "message": f"Team {team_id!r} not found"}},
    )


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class CreateTeamRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field("", max_length=500)
    agent_ids: List[str] = Field(..., min_length=1)
    workspace_id: str = Field("default", max_length=80)


class PatchTeamRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    description: Optional[str] = Field(None, max_length=500)
    # Move the team to a different workspace (e.g. user chose "create new
    # workspace" right after a single-agent run). When None the team stays put.
    workspace_id: Optional[str] = Field(None, max_length=80)
    add_agent_ids: List[str] = Field(default_factory=list)
    remove_agent_ids: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("", status_code=201)
async def create_team(body: CreateTeamRequest) -> Dict[str, Any]:
    """Create a new AgentTeam."""
    # D2: reject creation referencing agents that don't exist in the store.
    _reject_missing_agents(body.agent_ids)
    now = datetime.now(timezone.utc).isoformat()
    team_id = f"team-{uuid4().hex[:12]}"
    record: Dict[str, Any] = {
        "team_id": team_id,
        "name": body.name,
        "description": body.description,
        "workspace_id": body.workspace_id,
        "agent_ids": list(dict.fromkeys(body.agent_ids)),  # dedup, preserve order
        "created_at": now,
        "updated_at": now,
    }
    _save_team(record)
    return _envelope(record, created=True)


@router.get("")
async def list_teams(
    workspace_id: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """List all AgentTeams, optionally filtered by workspace."""
    records = _list_teams(workspace_id)
    return _envelope(records, total=len(records))


@router.get("/{team_id}")
async def get_team(team_id: str) -> Dict[str, Any]:
    """Get a single AgentTeam by ID."""
    _validate_team_id(team_id)
    record = _load_team(team_id)
    if record is None:
        raise _not_found(team_id)
    return _envelope(record)


@router.patch("/{team_id}")
async def patch_team(team_id: str, body: PatchTeamRequest) -> Dict[str, Any]:
    """Update team metadata or add/remove members."""
    _validate_team_id(team_id)
    record = _load_team(team_id)
    if record is None:
        raise _not_found(team_id)

    if body.name is not None:
        record["name"] = body.name
    if body.description is not None:
        record["description"] = body.description
    if body.workspace_id is not None:
        record["workspace_id"] = body.workspace_id

    # D2: newly-added members must resolve in the store. Existing members are
    # NOT re-validated (backward compat for teams that predate this check).
    if body.add_agent_ids:
        _reject_missing_agents(body.add_agent_ids)

    current_ids: List[str] = record.get("agent_ids", [])
    # Add new members (no duplicates)
    for aid in body.add_agent_ids:
        if aid not in current_ids:
            current_ids.append(aid)
    # Remove members
    remove_set = set(body.remove_agent_ids)
    current_ids = [aid for aid in current_ids if aid not in remove_set]

    record["agent_ids"] = current_ids
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_team(record)
    return _envelope(record)


@router.delete("/{team_id}", status_code=204)
async def delete_team(team_id: str) -> None:
    """Delete an AgentTeam."""
    _validate_team_id(team_id)
    record = _load_team(team_id)
    if record is None:
        raise _not_found(team_id)
    _delete_team_file(team_id)


# ---------------------------------------------------------------------------
# Workflow endpoints — Story 12-3
# ---------------------------------------------------------------------------


class TeamWorkflow(BaseModel):
    nodes: List[Dict[str, Any]] = Field(default_factory=list)
    edges: List[Dict[str, Any]] = Field(default_factory=list)


@router.get("/{team_id}/workflow")
async def get_team_workflow(team_id: str) -> Dict[str, Any]:
    """Get saved workflow graph for a team."""
    _validate_team_id(team_id)
    record = _load_team(team_id)
    if record is None:
        raise _not_found(team_id)
    workflow = record.get("workflow", {"nodes": [], "edges": []})
    return _envelope(workflow)


@router.put("/{team_id}/workflow")
async def put_team_workflow(team_id: str, body: TeamWorkflow) -> Dict[str, Any]:
    """Save workflow graph for a team."""
    _validate_team_id(team_id)
    record = _load_team(team_id)
    if record is None:
        raise _not_found(team_id)
    workflow = body.model_dump()
    # D1: validate DAG structure (no cycle via sequential edges, endpoints are
    # known node ids, no self-loop). D2: every node's non-empty agentId must
    # resolve in the agent store. Validation fires only on this incoming write,
    # so existing (possibly-legacy) team records keep loading via GET.
    _reject_dag(_tv.validate_workflow(workflow))
    _reject_missing_agents(_tv.workflow_agent_ids(workflow))
    record["workflow"] = workflow
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_team(record)
    # 版本仓快照（team_workflow_versions 独立小仓）：每次成功保存自动记一版，
    # 供 GET …/workflow/versions 列表与 restore/branch 回放。
    _wf_versions.record_version(team_id, workflow)
    return _envelope(record["workflow"])


# ---------------------------------------------------------------------------
# Workflow version endpoints — 版本管理（借鉴 StaffDeck SOP 版本）
# ---------------------------------------------------------------------------


class RestoreWorkflowRequest(BaseModel):
    version: int = Field(..., ge=1)


class BranchWorkflowRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)


@router.get("/{team_id}/workflow/versions")
async def list_team_workflow_versions(team_id: str) -> Dict[str, Any]:
    """List workflow version snapshots (metadata only, oldest first)."""
    _validate_team_id(team_id)
    record = _load_team(team_id)
    if record is None:
        raise _not_found(team_id)
    versions = [
        {
            "version": v["version"],
            "saved_at": v["saved_at"],
            "summary": v.get("summary"),
        }
        for v in _wf_versions.list_versions(team_id)
    ]
    return _envelope(versions, total=len(versions))


@router.post("/{team_id}/workflow/restore")
async def restore_team_workflow(team_id: str, body: RestoreWorkflowRequest) -> Dict[str, Any]:
    """Restore the team's workflow to a saved version.

    The restored snapshot goes through the same D1/D2 write-time validation as
    a fresh PUT (an agent referenced by the old graph may have been deleted
    since), then counts as a new version ("restored from vN") so history is
    append-only.
    """
    _validate_team_id(team_id)
    record = _load_team(team_id)
    if record is None:
        raise _not_found(team_id)
    entry = _wf_versions.get_version(team_id, body.version)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "code": "VERSION_NOT_FOUND",
                    "message": f"Workflow version v{body.version} not found for team {team_id!r}",
                }
            },
        )
    workflow = entry["workflow"]
    _reject_dag(_tv.validate_workflow(workflow))
    _reject_missing_agents(_tv.workflow_agent_ids(workflow))
    record["workflow"] = workflow
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_team(record)
    _wf_versions.record_version(team_id, workflow, summary=f"restored from v{body.version}")
    return _envelope(record["workflow"])


@router.post("/{team_id}/workflow/branch", status_code=201)
async def branch_team_workflow(team_id: str, body: BranchWorkflowRequest) -> Dict[str, Any]:
    """Copy the team (with its current workflow) into a new team variant.

    Default name is ``<原名> · 分支``；body.name 可覆盖。分支带走
    workflow/policy_matrix/validation_hooks，并在自己的版本仓里记下初始
    版本（summary 注明来源），后续各自演化互不影响。
    """
    _validate_team_id(team_id)
    record = _load_team(team_id)
    if record is None:
        raise _not_found(team_id)
    now = datetime.now(timezone.utc).isoformat()
    branch: Dict[str, Any] = {
        "team_id": f"team-{uuid4().hex[:12]}",
        "name": body.name or f"{record.get('name', team_id)} · 分支",
        "description": record.get("description", ""),
        "workspace_id": normalize_workspace_id(record.get("workspace_id")),
        "agent_ids": list(record.get("agent_ids", [])),
        "created_at": now,
        "updated_at": now,
    }
    for key in ("workflow", "policy_matrix", "validation_hooks"):
        if key in record:
            branch[key] = copy.deepcopy(record[key])
    _save_team(branch)
    workflow = branch.get("workflow")
    if isinstance(workflow, dict):
        latest = _wf_versions.latest_version(team_id)
        origin = f"{team_id} v{latest['version']}" if latest else team_id
        _wf_versions.record_version(branch["team_id"], workflow, summary=f"branched from {origin}")
    return _envelope(branch, created=True, branched_from=team_id)


# ---------------------------------------------------------------------------
# Policy endpoints — Story 12-3
# ---------------------------------------------------------------------------


class TeamPolicyRequest(BaseModel):
    matrix: Dict[str, Dict[str, str]] = Field(default_factory=dict)


class TeamChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)


@router.get("/{team_id}/policy")
async def get_team_policy(team_id: str) -> Dict[str, Any]:
    """Get saved policy matrix for a team."""
    _validate_team_id(team_id)
    record = _load_team(team_id)
    if record is None:
        raise _not_found(team_id)
    return _envelope(record.get("policy_matrix", {}))


@router.put("/{team_id}/policy")
async def put_team_policy(team_id: str, body: TeamPolicyRequest) -> Dict[str, Any]:
    """Save policy matrix for a team."""
    _validate_team_id(team_id)
    record = _load_team(team_id)
    if record is None:
        raise _not_found(team_id)
    record["policy_matrix"] = body.matrix
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_team(record)
    return _envelope(record["policy_matrix"])


# ---------------------------------------------------------------------------
# Validation hooks endpoints — N1/002
# ---------------------------------------------------------------------------
#
# PM decision Q12.1: `validation_hooks` is a TOP-LEVEL team field, parallel to
# `policy_obj` / `policy_matrix` / `workflow` (NOT nested inside policy). See
# `docs/design/team-validation-hook-v1.md` §H6 and §5 Hook Schema.
#
# Persistence shape on the team record:
#     record["validation_hooks"] = [HookSpec, …]   # raw list
#
# The wrapper `ValidationHooksConfig` is request-body symmetry only (mirrors
# `TeamWorkflow` / `TeamPolicyRequest`); we unwrap before storing.

from xueban.runtime.validation_hooks import ValidationHooksConfig  # noqa: E402


@router.get("/{team_id}/validation-hooks")
async def get_team_validation_hooks(team_id: str) -> Dict[str, Any]:
    """Get saved validation hooks for a team.

    Returns `{data: {validation_hooks: [...]}}`. Teams created before
    N1/002 won't have the field; we return an empty list (backwards
    compatible — AC1 in the design doc).
    """
    _validate_team_id(team_id)
    record = _load_team(team_id)
    if record is None:
        raise _not_found(team_id)
    hooks = record.get("validation_hooks", [])
    return _envelope({"validation_hooks": hooks})


@router.put("/{team_id}/validation-hooks")
async def put_team_validation_hooks(
    team_id: str, body: ValidationHooksConfig
) -> Dict[str, Any]:
    """Replace the team's validation hooks with the given list.

    Whole-list replacement (same pattern as `PUT /workflow` and
    `PUT /policy`). Per-hook discriminator + unique-id checks are enforced
    by the Pydantic model; FastAPI surfaces validation failures as 422.
    """
    _validate_team_id(team_id)
    record = _load_team(team_id)
    if record is None:
        raise _not_found(team_id)
    record["validation_hooks"] = [
        hook.model_dump(exclude_none=True) for hook in body.validation_hooks
    ]
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_team(record)
    return _envelope({"validation_hooks": record["validation_hooks"]})


# ---------------------------------------------------------------------------
# Chat-driven editing — user describes changes in natural language
# ---------------------------------------------------------------------------

_TEAM_EDIT_SYSTEM_PROMPT = """\
You are a Team configuration assistant. The user will describe changes to an AI agent team's configuration in natural language. You must interpret their request and output a JSON object with the fields to update.

Current team configuration:
- Name: {name}
- Description: {description}
- Agent IDs: {agent_ids}

Respond with ONLY a JSON object (no markdown, no explanation outside JSON) with this structure:
{{
  "updates": {{
    "name": "new name or null if unchanged",
    "description": "new description or null if unchanged",
    "add_agent_ids": ["agent-id-1"] or null if not adding,
    "remove_agent_ids": ["agent-id-2"] or null if not removing
  }},
  "explanation": "Brief description of what you changed (in the user's language)"
}}

Rules:
- If the user asks to rename the team, update the "name" field
- If the user asks to change the description/purpose, update the "description" field
- If the user asks to add agents/members, put their IDs in "add_agent_ids"
- If the user asks to remove agents/members, put their IDs in "remove_agent_ids"
- Set unchanged fields to null
- Keep explanation concise (1-2 sentences)
- Match the user's language (Chinese or English)
"""


@router.post("/{team_id}/chat")
async def team_chat_edit(
    team_id: str,
    body: TeamChatRequest,
    x_llm_key: Optional[str] = Header(None, alias="X-LLM-Key"),
    x_llm_provider: Optional[str] = Header(None, alias="X-LLM-Provider"),
    x_llm_model: Optional[str] = Header(None, alias="X-LLM-Model"),
) -> Dict[str, Any]:
    """Chat-driven team editing: user describes changes, LLM interprets and applies."""
    _validate_team_id(team_id)
    record = _load_team(team_id)
    if record is None:
        raise _not_found(team_id)

    if not x_llm_key:
        raise HTTPException(
            status_code=401,
            detail={"error": {"code": "NO_API_KEY", "message": "X-LLM-Key header required for chat editing"}},
        )

    system_prompt = _TEAM_EDIT_SYSTEM_PROMPT.format(
        name=_escape_fmt_str(record.get("name", "")),
        description=_escape_fmt_str(record.get("description", "")),
        agent_ids=json.dumps(record.get("agent_ids", []), ensure_ascii=False),
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

    content = response.content.strip()
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

    # Validate types before applying — prevent LLM from injecting unexpected field types
    new_name = updates.get("name")
    if isinstance(new_name, str) and new_name:
        record["name"] = new_name[:120]
        applied_fields.append("name")
    new_desc = updates.get("description")
    if isinstance(new_desc, str):
        record["description"] = new_desc[:500]
        applied_fields.append("description")

    current_ids: List[str] = record.get("agent_ids", [])
    add_ids = updates.get("add_agent_ids")
    remove_ids = updates.get("remove_agent_ids")
    # Validate agent id lists are lists of strings
    if not (isinstance(add_ids, list) and all(isinstance(i, str) for i in add_ids)):
        add_ids = None
    if not (isinstance(remove_ids, list) and all(isinstance(i, str) for i in remove_ids)):
        remove_ids = None
    if add_ids:
        for aid in add_ids:
            if aid not in current_ids:
                current_ids.append(aid)
        applied_fields.append("add_agent_ids")
    if remove_ids:
        remove_set = set(remove_ids)
        current_ids = [aid for aid in current_ids if aid not in remove_set]
        applied_fields.append("remove_agent_ids")
    record["agent_ids"] = current_ids

    if applied_fields:
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        _save_team(record)

    return _envelope({
        "team": record,
        "reply": explanation,
        "applied": bool(applied_fields),
        "applied_fields": applied_fields,
    })
