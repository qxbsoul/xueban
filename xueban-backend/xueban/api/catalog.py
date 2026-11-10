"""Agent Catalog API — Story 8.7

3 endpoints, all wrapped in the shared `{data, meta}` envelope:
  - GET  /catalog/apps                 — list+filter+search+page
  - GET  /catalog/apps/{app_id}        — sanitized detail
  - POST /catalog/apps/{app_id}/fork   — clone snapshot → new Blueprint

Errors flow through the existing XuebanError handler in xueban.server,
which maps them to {error: {...}} JSON envelopes.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, Body, Query
from pydantic import BaseModel

from xueban.runtime.catalog_service import (
    CatalogService,
    get_service,
    redact_blueprint_snapshot,
)
from xueban.runtime.contracts_builder import AgentBlueprint


router = APIRouter(prefix="/catalog", tags=["catalog"])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ok(data: Any, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"data": data, "meta": meta or {}}


# ---------------------------------------------------------------------------
# Team-template fork materialization — T04 一键 fork 黄金路径
# ---------------------------------------------------------------------------
#
# mode=="team" 的模板 fork 不能只吐一个 Blueprint 草稿：评审/用户期望 fork 后
# 直接在群聊里选中团队开跑。因此这里把 snapshot 里的 6 个 role_profiles 落成
# .xueban/agents/*.json（形状对齐 agents.py quick_create），再按
# blueprint.metadata["team_dag"] 落成 .xueban/teams/team-*.json（含
# workflow nodes/edges —— approve 边 = L6 人工审批门，conditional 边 = 驳回回炉）。
# 写前走 team_validation 的 D1（DAG 结构）+ D2（agent 存在性）校验，与
# teams.py 写路径同一套规则，失败抛 CATALOG_BLUEPRINT_INVALID。

# 允许的边 mode（对齐 team_validation._map_mode / 前端 TeamWorkflowEdge）
_EDGE_MODES = {"direct", "approve", "conditional", "parallel"}


def _materialize_team_fork(
    blueprint: AgentBlueprint,
    source_app_id: str,
    workspace_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Fork 落库：role_profiles → agents，metadata.team_dag → team workflow。

    返回 {"agents": [{agent_id, name}], "team": team_record}。
    """
    # 延迟 import：避免 api 层模块在 import 期互相拉环（agents/teams 不 import catalog，
    # 但保持一致风格，且方便测试 monkeypatch 模块属性后生效）。
    from datetime import datetime, timezone

    from xueban.api import agents as agents_api
    from xueban.api import teams as teams_api
    from xueban.api import team_validation as _tv
    from xueban.runtime.catalog_service import CatalogBlueprintInvalid

    ws = (workspace_id or "").strip() or "default"
    now = datetime.now(timezone.utc).isoformat()

    # 1) 每个 role_profile → 一个 agent 记录（D2 前提：先落 agents 再落 team）
    agent_id_by_role: Dict[str, str] = {}
    agents_out: List[Dict[str, str]] = []
    for role in blueprint.role_profiles:
        agent_id = f"agent-{uuid4().hex[:12]}"
        soul = (role.persona or role.description or role.name).strip()
        agent_bp = agents_api._build_default_blueprint(
            name=role.name,
            soul=soul,
            model=role.executor_model,
            tools=role.tools or None,
            raci=role.raci or None,
        )
        # _build_default_blueprint 只接受 model；provider 按模板角色覆盖（glm/zhipu）
        agent_bp.role_profiles[0].executor_provider = role.executor_provider
        agent_bp.metadata = {
            **agent_bp.metadata,
            "source": "catalog_fork",
            "forked_from": source_app_id,
        }
        record = {
            "agent_id": agent_id,
            "name": role.name,
            "soul": soul,
            "workspace_id": ws,
            "blueprint": agent_bp.model_dump(),
            "status": "idle",
            "source": "catalog_fork",
            "created_at": now,
            "avatar_color": None,
        }
        agents_api._save_agent(record)
        agent_id_by_role[role.role_id] = agent_id
        agents_out.append({"agent_id": agent_id, "name": role.name})

    # 2) metadata.team_dag → team workflow（node role_id 映射到新 agent_id）
    dag = (blueprint.metadata or {}).get("team_dag") or {}
    role_by_id = {r.role_id: r for r in blueprint.role_profiles}
    nodes: List[Dict[str, Any]] = []
    for i, n in enumerate(dag.get("nodes") or []):
        if not isinstance(n, dict):
            continue
        role = role_by_id.get(n.get("role_id") or "")
        if role is None:
            continue
        pos = n.get("position") if isinstance(n.get("position"), dict) else {}
        nodes.append({
            "id": str(n.get("id") or role.role_id),
            "type": "agentTask",
            "position": {
                "x": float(pos.get("x", 60)),
                "y": float(pos.get("y", 40 + i * 110)),
            },
            "data": {
                "agentId": agent_id_by_role[role.role_id],
                "name": role.name,
                "soul": (role.description or role.name)[:120],
            },
        })
    if not nodes:
        # 模板没带 team_dag：退化为纵向自动布局、无边，保证 team 仍可建可跑
        for i, role in enumerate(blueprint.role_profiles):
            nodes.append({
                "id": role.role_id,
                "type": "agentTask",
                "position": {"x": 60.0, "y": float(40 + i * 110)},
                "data": {
                    "agentId": agent_id_by_role[role.role_id],
                    "name": role.name,
                    "soul": (role.description or role.name)[:120],
                },
            })
    edges: List[Dict[str, Any]] = []
    for e in dag.get("edges") or []:
        if not isinstance(e, dict):
            continue
        src, dst = e.get("source"), e.get("target")
        if not isinstance(src, str) or not isinstance(dst, str):
            continue
        mode = e.get("mode")
        edge: Dict[str, Any] = {
            "id": str(e.get("id") or f"e-{src}-{dst}"),
            "source": src,
            "target": dst,
            "data": {"mode": mode if mode in _EDGE_MODES else "direct"},
        }
        if isinstance(e.get("label"), str) and e["label"]:
            edge["label"] = e["label"]
        edges.append(edge)
    workflow = {"nodes": nodes, "edges": edges}

    # 3) 与 teams.py 写路径相同的 D1+D2 校验，不过则视为模板快照损坏
    errors = _tv.validate_workflow(workflow)
    errors += _tv.validate_agent_ids(
        _tv.workflow_agent_ids(workflow), exists=_tv.agent_exists_in_store
    )
    if errors:
        raise CatalogBlueprintInvalid(
            f"Team template fork materialization failed validation: {'; '.join(errors)}",
            details={"app_id": source_app_id, "errors": errors},
        )

    team_id = f"team-{uuid4().hex[:12]}"
    team_record: Dict[str, Any] = {
        "team_id": team_id,
        "name": blueprint.name,
        "description": (blueprint.goal or "")[:500],
        "workspace_id": ws,
        "agent_ids": [a["agent_id"] for a in agents_out],
        "created_at": now,
        "updated_at": now,
        "workflow": workflow,
        "policy_matrix": {},
    }
    teams_api._save_team(team_record)
    return {"agents": agents_out, "team": team_record}


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------


@router.get("/apps")
async def list_apps(
    kit_type: str = Query("all"),
    q: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> Dict[str, Any]:
    """List published Catalog apps with optional filter / search / pagination."""
    svc: CatalogService = get_service()
    result = svc.list_apps(kit_type=kit_type, q=q, page=page, page_size=page_size)
    return _ok(
        data={"apps": result["apps"]},
        meta={
            "total": result["total"],
            "page": result["page"],
            "page_size": result["page_size"],
            "kit_type": kit_type,
            "q": q,
        },
    )


@router.get("/apps/{app_id}")
async def get_app(app_id: str) -> Dict[str, Any]:
    """Return sanitized detail for a single Catalog app (AC3 — no sensitive fields)."""
    svc: CatalogService = get_service()
    detail = svc.get_app(app_id)
    return _ok(
        data=detail.model_dump(mode="json"),
        meta={"trace_id": f"trace-{uuid4().hex[:12]}"},
    )


class ForkRequest(BaseModel):
    """fork 可选请求体：把落库的 agents/team 挂到指定 workspace（缺省 default）。"""

    workspace_id: Optional[str] = None


@router.post("/apps/{app_id}/fork")
async def fork_app(
    app_id: str,
    body: Optional[ForkRequest] = Body(default=None),
) -> Dict[str, Any]:
    """Fork a Catalog app into a new editable Blueprint (AC6).

    mode=="team" 的模板额外落库：6 个 agent + 带 DAG workflow 的 team 记录，
    响应里带上 agents / team，前端 fork 后即可选中团队直接在群聊运行。
    """
    svc: CatalogService = get_service()
    result = svc.fork_app(app_id)
    data: Dict[str, Any] = {
        "blueprint_id": result.blueprint_id,
        "forked_from": result.forked_from,
        "blueprint": redact_blueprint_snapshot(result.blueprint.model_dump(mode="json")),
    }
    if result.blueprint.mode == "team":
        materialized = _materialize_team_fork(
            result.blueprint,
            app_id,
            workspace_id=body.workspace_id if body else None,
        )
        data["agents"] = materialized["agents"]
        data["team"] = materialized["team"]
    return _ok(
        data=data,
        meta={"trace_id": f"trace-{uuid4().hex[:12]}"},
    )
