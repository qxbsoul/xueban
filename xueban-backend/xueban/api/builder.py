"""Builder API 路由 — Story 8.1 (AC3) + Story 8.6 (真实发布)
                     + Story 13.3 (Catalog Agent → Team 角色引入)

5 个 endpoint，统一 {data, meta} envelope，错误路径走 XuebanError 体系。

Story 13.3 新增：
  POST /builder/blueprints/{blueprint_id}/import-agent — 从 Catalog 引入 Agent 的 RoleProfile
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Story 13.6 — anchor_agent_id format gate (mirrors catalog app_id naming).
_ANCHOR_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

from xueban.runtime.builder_service import (
    BuilderService,
    GenerateBlueprintRequest,
    InstantiateBlueprintRequest,
    PublishBlueprintRequest,
    RegressionBlockedError,
    SmokeRunBlueprintRequest,
    get_service,
    set_service,
)
from xueban.runtime.kits.registry import REGISTRY, discover_and_register_kits
from xueban.runtime.kits.evals import (
    KitSmokeRunner,
    SmokeRunOptions,
    get_eval_pack,
)
from xueban.runtime.contracts_builder import AgentBlueprint


router = APIRouter(prefix="/builder", tags=["builder"])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ok(data: Any, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"data": data, "meta": meta or {}}


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------


@router.post("/blueprints/generate")
async def generate_blueprint(req: GenerateBlueprintRequest) -> Dict[str, Any]:
    """从 goal/audience/mode 启发式生成 AgentBlueprint。"""
    svc = get_service()
    # P2: run sync service call in thread pool (consistent with smoke_run/publish)
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, svc.generate_blueprint, req)
    return _ok(
        data=result.blueprint.model_dump(mode="python"),
        meta=result.meta,
    )


@router.post("/blueprints/instantiate")
async def instantiate_blueprint(req: InstantiateBlueprintRequest) -> Dict[str, Any]:
    """AgentBlueprint → WorkflowTemplateSpec + WorkflowDefinition。"""
    svc = get_service()
    result = svc.instantiate_blueprint(req)
    return _ok(
        data={
            "blueprint": result.blueprint.model_dump(mode="python"),
            "template_spec": result.template_spec,
            "workflow_definition": result.workflow_definition,
        },
        meta={"warnings": result.warnings},
    )


@router.post("/blueprints/smoke-run")
async def smoke_run_blueprint(req: SmokeRunBlueprintRequest) -> Dict[str, Any]:
    """对 Blueprint 执行 5 项最小 Smoke Run 检查（Story 8.5）。"""
    svc = get_service()
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, svc.smoke_run_blueprint, req)
    return _ok(
        data={
            "status": result.status,
            "checks": [c.model_dump() for c in result.checks],
            "summary": result.summary,
            "recommended_fix": result.recommended_fix,
            "primary_blocker": result.primary_blocker,
        },
        meta={"warnings": result.warnings},
    )


@router.post("/blueprints/publish")
async def publish_blueprint(req: PublishBlueprintRequest) -> Dict[str, Any]:
    """发布 Blueprint — 真实回填 Template + WorkflowDefinition（Story 8.6 AC5）。

    Story 10.5 AC4: Kit completeness gate — if blueprint is bound to a kit_id and
    that Kit's default_eval_profile is empty, reject with HTTP 400.
    """
    svc = get_service()
    trace_id = f"trace-{uuid4().hex[:12]}"

    # Story 10.5 AC4: Kit completeness check
    bp_kit_id = req.blueprint.metadata.get("kit_id") if req.blueprint.metadata else None
    if bp_kit_id:
        kit = REGISTRY.get(bp_kit_id)
        if kit is not None:
            ep = kit.default_eval_profile
            ep_empty = (
                ep is None
                or (not ep.eval_criteria and not ep.smoke_eval_enabled and not ep.regression_gate)
            )
            if ep_empty:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": {
                            "code": "KIT_NOT_PUBLISHABLE",
                            "message": f"Kit '{bp_kit_id}' has empty default_eval_profile and cannot be published.",
                            "details": {"kit_id": bp_kit_id},
                            "trace_id": trace_id,
                        }
                    },
                )

    # Story 10.6 AC4 — Kit smoke regression gate (runs only when kit_id present
    # and an EvalPack is registered). Block on `verdict == "block"`.
    if bp_kit_id and get_eval_pack(bp_kit_id) is not None:
        runner = KitSmokeRunner()
        try:
            regression_report = await runner.run_regression(
                bp_kit_id, req.blueprint, options=SmokeRunOptions(mock_llm=True)
            )
        except Exception as exc:  # noqa: BLE001
            # Story 10.6 H3: fail-closed — runner crashes (corrupt baseline JSON,
            # eval module ImportError, etc.) must NOT silently bypass the
            # regression block. Surface as HTTP 422 with a clear error.
            import logging as _log
            _log.getLogger(__name__).exception(
                "Kit regression check failed for %s; failing closed", bp_kit_id
            )
            raise HTTPException(
                status_code=422,
                detail={
                    "error": {
                        "code": "REGRESSION_CHECK_FAILED",
                        "message": (
                            "Regression check could not run; publish blocked. "
                            "Please retry or contact support."
                        ),
                        "details": {
                            "kit_id": bp_kit_id,
                            "exception": f"{type(exc).__name__}: {exc}",
                        },
                        "trace_id": trace_id,
                    }
                },
            )
        # Story 10.6 M2: baseline auto-save on first successful smoke run.
        # When no baseline exists AND the current smoke passed, persist as
        # baseline so subsequent runs perform real regression comparison.
        if (
            regression_report is not None
            and regression_report.current is not None
            and regression_report.baseline_timestamp is None
            and regression_report.current.passed
        ):
            try:
                runner.save_baseline(regression_report.current)
            except Exception as exc:  # noqa: BLE001
                import logging as _log
                _log.getLogger(__name__).warning(
                    "Baseline auto-save failed for %s: %s", bp_kit_id, exc
                )
        if regression_report is not None and regression_report.verdict == "block":
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "code": "REGRESSION_BLOCKED",
                        "message": "Publish blocked: regression detected",
                        "details": {
                            "kit_id": bp_kit_id,
                            "reasons": regression_report.reasons,
                            "report": regression_report.model_dump(mode="json"),
                        },
                        "trace_id": trace_id,
                    }
                },
            )

    # Patch 2: run sync file I/O in thread pool to avoid blocking async event loop
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, svc.publish_blueprint, req)
    except RegressionBlockedError:
        # Patch 16: return HTTP 422 for REGRESSION_BLOCKED (REST semantics)
        # R2-Patch-2: omit exc.message and exc.details from response to avoid leaking
        # internal RegressionService reason strings (path / traceback fragments).
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "REGRESSION_BLOCKED",
                    "message": "Regression gate blocked publish. Please resolve failing regression checks before publishing.",
                    "details": {},
                    "trace_id": trace_id,
                }
            },
        )
    # Patch 6: include timestamp in meta (AC5 spec)
    return _ok(
        data={
            "template_id": result.template_id,
            "workflow_id": result.workflow_id,
            "kit_tags": result.kit_tags,
            "publish_status": result.publish_status,
            "links": result.links.model_dump(),
        },
        meta={
            "trace_id": trace_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


@router.get("/kits/{kit_id}")
async def get_builder_kit(kit_id: str) -> Dict[str, Any]:
    """返回指定 kit 的完整定义（含默认 Blueprint 摘要）。"""
    # Story 10.5 H3: Kit discovery moved to server.py startup; lazy fallback only
    # if registry is empty (e.g. when this router is mounted in a test fixture
    # that bypasses lifespan events).
    if not REGISTRY.list_kits():
        discover_and_register_kits()
    kit = REGISTRY.get(kit_id)
    if kit is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "code": "KIT_NOT_FOUND",
                    "message": f"Kit '{kit_id}' not found in registry.",
                    "details": {"kit_id": kit_id},
                }
            },
        )
    return _ok(data=kit.blueprint_summary(), meta={})


@router.get("/kits")
async def list_builder_kits() -> Dict[str, Any]:
    """返回已注册 Agent Kit 目录（REGISTRY 模式）。"""
    # Story 10.5 H3: Kit discovery moved to server.py startup; lazy fallback only.
    if not REGISTRY.list_kits():
        discover_and_register_kits()
    kits = REGISTRY.list_kits()
    return _ok(
        data=[k.metadata_only() for k in kits],
        meta={"count": len(kits)},
    )


# ---------------------------------------------------------------------------
# Story 13.3 — Catalog Agent → Team 角色引入
# ---------------------------------------------------------------------------


class ImportAgentRequest(BaseModel):
    # Round-1 follow-up M4: enforce non-empty catalog_agent_id at validation
    # boundary so empty string does not propagate to catalog_svc.get_app("").
    catalog_agent_id: str = Field(..., min_length=1)


@router.post("/blueprints/{blueprint_id}/import-agent")
async def import_agent_to_blueprint(
    blueprint_id: str,
    body: ImportAgentRequest,
) -> Dict[str, Any]:
    """从 Catalog 引入 Agent 的 RoleProfile（Story 13.3 AC5）。

    - 从 catalog_service 取出 CatalogApp（含 blueprint_snapshot）
    - 提取 snapshot.role_profiles[0] 作为引入的 RoleProfile
    - 生成新 role_id: imported-{catalog_id[:8]}-{timestamp}
    - metadata.imported_from = catalog_agent_id
    - 返回 {data: RoleProfile dict}（前端负责插入 blueprint）

    blueprint_id 是 URL 占位符（blueprint 存储在前端 builderStore），
    本端点不做持久化操作。
    """
    # Round-1 follow-up M3: function-body import is intentional — `catalog_service`
    # imports types/utilities back into this module path during runtime wiring,
    # so a top-level import would risk circular import at startup. Keep local.
    from xueban.runtime.catalog_service import (
        CatalogAppNotFound,
        CatalogSnapshotMissing,
        get_service as get_catalog_service,
    )

    trace_id = f"trace-{uuid4().hex[:12]}"
    timestamp = datetime.now(timezone.utc).isoformat()
    catalog_id = body.catalog_agent_id

    catalog_svc = get_catalog_service()

    try:
        catalog_app = catalog_svc.get_app(catalog_id)
    except CatalogAppNotFound:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "code": "CATALOG_APP_NOT_FOUND",
                    "message": f"Catalog agent not found: {catalog_id!r}",
                    "details": {"catalog_agent_id": catalog_id},
                    "trace_id": trace_id,
                }
            },
        )
    except CatalogSnapshotMissing:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "CATALOG_SNAPSHOT_MISSING",
                    "message": f"Catalog agent snapshot is missing or corrupted: {catalog_id!r}",
                    "details": {"catalog_agent_id": catalog_id},
                    "trace_id": trace_id,
                }
            },
        )

    snapshot = catalog_app.blueprint_snapshot
    role_profiles = snapshot.get("role_profiles") or []

    if not role_profiles:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "CATALOG_BLUEPRINT_INVALID",
                    "message": f"Catalog agent snapshot has no role_profiles: {catalog_id!r}",
                    "details": {"catalog_agent_id": catalog_id},
                    "trace_id": trace_id,
                }
            },
        )

    # 取 snapshot 的第一个 RoleProfile 作为引入角色
    source_role: Dict[str, Any] = dict(role_profiles[0]) if isinstance(role_profiles[0], dict) else {}

    # 校验结构
    if not source_role:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "CATALOG_BLUEPRINT_INVALID",
                    "message": f"Catalog agent role_profile is empty: {catalog_id!r}",
                    "details": {"catalog_agent_id": catalog_id},
                    "trace_id": trace_id,
                }
            },
        )

    # 生成新 role_id，保留 imported_from 谱系
    # Round-1 follow-up H2: append uuid4 suffix to avoid same-second role_id
    # collision when the same catalog agent is imported twice within 1s.
    new_role_id = f"imported-{catalog_id[:8]}-{int(time.time())}-{uuid4().hex[:6]}"
    source_role["role_id"] = new_role_id

    existing_meta: Dict[str, Any] = dict(source_role.get("metadata") or {})
    existing_meta["imported_from"] = catalog_id
    source_role["metadata"] = existing_meta

    return _ok(
        data=source_role,
        meta={
            "trace_id": trace_id,
            "timestamp": timestamp,
            "blueprint_id": blueprint_id,
            "catalog_agent_id": catalog_id,
        },
    )

# ---------------------------------------------------------------------------
# Story 13.6 — Standalone Agent → Team Promotion
# ---------------------------------------------------------------------------


class PromoteFromAgentRequest(BaseModel):
    anchor_agent_id: str = Field(..., min_length=1)


@router.post("/teams/from-agent")
async def promote_to_team_from_agent(body: PromoteFromAgentRequest) -> Dict[str, Any]:
    """Story 13.6 AC2 — 以一个已发布的 Catalog Agent 为锚点构造新的 Team Blueprint。

    与 import-agent 的差异：返回**完整 Blueprint**（不只是单个 RoleProfile），
    并把锚点角色标记 metadata.anchor=true，让前端 builderStore 整体 setBlueprint 接管。
    """
    from xueban.runtime.catalog_service import (
        CatalogAppNotFound,
        CatalogSnapshotMissing,
        get_service as get_catalog_service,
    )

    trace_id = f"trace-{uuid4().hex[:12]}"
    timestamp = datetime.now(timezone.utc).isoformat()
    catalog_id = body.anchor_agent_id

    # P10: validate anchor_agent_id format before any downstream lookup / log emit.
    if not isinstance(catalog_id, str) or not _ANCHOR_AGENT_ID_RE.match(catalog_id):
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "INVALID_ANCHOR_AGENT_ID",
                    "message": "anchor_agent_id must match ^[A-Za-z0-9_-]{1,64}$",
                    "details": {},
                    "trace_id": trace_id,
                }
            },
        )

    catalog_svc = get_catalog_service()

    try:
        catalog_app = catalog_svc.get_app(catalog_id)
    except CatalogAppNotFound:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "code": "CATALOG_APP_NOT_FOUND",
                    "message": f"Catalog agent not found: {catalog_id!r}",
                    "details": {"anchor_agent_id": catalog_id},
                    "trace_id": trace_id,
                }
            },
        )
    except CatalogSnapshotMissing:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "CATALOG_SNAPSHOT_MISSING",
                    "message": f"Catalog agent snapshot is missing or corrupted: {catalog_id!r}",
                    "details": {"anchor_agent_id": catalog_id},
                    "trace_id": trace_id,
                }
            },
        )

    # D3-a: server-side scope_hint gate — reject anchor that declared standalone.
    if getattr(catalog_app, "scope_hint", None) == "standalone":
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "CATALOG_AGENT_STANDALONE_LOCKED",
                    "message": (
                        f"Catalog agent {catalog_id!r} is declared standalone and "
                        "cannot be promoted to a Team anchor."
                    ),
                    "details": {"anchor_agent_id": catalog_id},
                    "trace_id": trace_id,
                }
            },
        )

    snapshot = catalog_app.blueprint_snapshot
    if not isinstance(snapshot, dict):
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "CATALOG_BLUEPRINT_INVALID",
                    "message": f"Catalog agent snapshot is not a dict: {catalog_id!r}",
                    "details": {"anchor_agent_id": catalog_id},
                    "trace_id": trace_id,
                }
            },
        )
    role_profiles = snapshot.get("role_profiles") or []

    if not role_profiles or not isinstance(role_profiles[0], dict):
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "CATALOG_BLUEPRINT_INVALID",
                    "message": f"Catalog agent snapshot has no usable role_profile: {catalog_id!r}",
                    "details": {"anchor_agent_id": catalog_id},
                    "trace_id": trace_id,
                }
            },
        )

    anchor_role: Dict[str, Any] = dict(role_profiles[0])
    # P3: collision-safe role_id (catalog_id[:8] prefix + uuid suffix). int(time.time())
    # caused same-second double-click to mint identical role_ids.
    anchor_role["role_id"] = f"anchor-{catalog_id[:8]}-{uuid4().hex[:12]}"
    anchor_meta = dict(anchor_role.get("metadata") or {})
    anchor_meta["anchor"] = True
    anchor_meta["imported_from"] = catalog_id
    anchor_role["metadata"] = anchor_meta

    # P10: validate the anchor role against RoleProfile schema before mounting it
    # into a Blueprint. Catches snapshots that pre-date current contract fields.
    try:
        from xueban.runtime.contracts_builder import RoleProfile as _RoleProfile
        _RoleProfile.model_validate(anchor_role)
    except Exception as exc:  # pydantic ValidationError or others
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "CATALOG_BLUEPRINT_INVALID",
                    "message": f"Anchor role failed schema validation: {exc}",
                    "details": {"anchor_agent_id": catalog_id, "reason": str(exc)},
                    "trace_id": trace_id,
                }
            },
        ) from exc

    anchor_name = catalog_app.name or anchor_role.get("name") or catalog_id
    new_blueprint: Dict[str, Any] = {
        "blueprint_id": f"team-from-{catalog_id[:8]}-{uuid4().hex[:8]}",
        "version": "1.0",
        "name": f"以 {anchor_name} 为核心的团队",
        "goal": snapshot.get("goal") or catalog_app.goal or "",
        "audience": snapshot.get("audience") or "",
        "mode": "team",
        "role_profiles": [anchor_role],
        "tool_policies": [],
        "knowledge_bindings": [],
        "memory_profile": snapshot.get("memory_profile") or {
            "scope": "session",
            "writeback_target": None,
            "enabled": True,
            "metadata": {},
        },
        "eval_profile": {
            "smoke_eval_enabled": False,
            "eval_criteria": [],
            "regression_gate": False,
            "metadata": {},
        },
        "publish_profile": {
            "target": "none",
            "visibility": "private",
            "publish_ref": "",
            "metadata": {},
        },
        "metadata": {
            "promoted_from_agent": catalog_id,
            "anchor_role_id": anchor_role["role_id"],
        },
    }

    # P10: audit log for promote (anchor → new team blueprint mapping).
    logger.info(
        "promote_to_team_from_agent: anchor=%s blueprint=%s role=%s trace=%s",
        catalog_id,
        new_blueprint["blueprint_id"],
        anchor_role["role_id"],
        trace_id,
    )

    return _ok(
        data=new_blueprint,
        meta={
            "trace_id": trace_id,
            "timestamp": timestamp,
            "anchor_agent_id": catalog_id,
        },
    )


# ---------------------------------------------------------------------------
# Story 10.1 / 10.2 / 10.3 / 10.4 — Kit-specific instantiate endpoints
#
# Each Kit's wizard posts user input to `/builder/kits/{kit}/instantiate` and
# receives a fully-built AgentBlueprint (built via the Kit's create_*_blueprint
# factory). This avoids duplicating blueprint construction logic on the frontend
# and keeps the canonical assembly path in Python.
# ---------------------------------------------------------------------------


def _kit_instantiate_response(blueprint, kit_id: str) -> Dict[str, Any]:
    return _ok(
        data=blueprint.model_dump(mode="python"),
        meta={
            "kit_id": kit_id,
            "trace_id": f"trace-{uuid4().hex[:12]}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


@router.post("/kits/research/instantiate")
async def instantiate_research_kit(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Story 10.1 AC5 — 从 ResearchGoalInputs 构建 Research Kit Blueprint。"""
    from xueban.runtime.kits.research_kit import (
        ResearchGoalInputs,
        create_research_blueprint,
    )

    try:
        inputs = ResearchGoalInputs(**payload)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "INVALID_KIT_INPUT", "message": str(exc), "details": {}}},
        ) from exc
    bp = create_research_blueprint(inputs)
    return _kit_instantiate_response(bp, "research_kit")


@router.post("/kits/knowledge_assistant/instantiate")
async def instantiate_knowledge_assistant_kit(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Story 10.2 AC5 — 从 KnowledgeAssistantGoalInputs 构建 Blueprint。"""
    from xueban.runtime.kits.knowledge_assistant_kit import (
        KnowledgeAssistantGoalInputs,
        create_knowledge_assistant_blueprint,
    )

    try:
        inputs = KnowledgeAssistantGoalInputs(**payload)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "INVALID_KIT_INPUT", "message": str(exc), "details": {}}},
        ) from exc
    bp = create_knowledge_assistant_blueprint(inputs)
    return _kit_instantiate_response(bp, "knowledge_assistant_kit")


@router.post("/kits/review_approval/instantiate")
async def instantiate_review_approval_kit(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Story 10.3 AC5 — 从 ReviewApprovalGoalInputs 构建 Blueprint。"""
    from xueban.runtime.kits.review_approval_kit import (
        ReviewApprovalGoalInputs,
        create_review_approval_blueprint,
    )

    try:
        inputs = ReviewApprovalGoalInputs(**payload)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "INVALID_KIT_INPUT", "message": str(exc), "details": {}}},
        ) from exc
    bp = create_review_approval_blueprint(inputs)
    return _kit_instantiate_response(bp, "review_approval_kit")


@router.post("/kits/persona_npc/instantiate")
async def instantiate_persona_npc_kit(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Story 10.4 AC5 — 从 PersonaNPCGoalInputs 构建 Blueprint。"""
    from xueban.runtime.kits.persona_npc_kit import (
        PersonaNPCGoalInputs,
        create_persona_npc_blueprint,
    )

    try:
        inputs = PersonaNPCGoalInputs(**payload)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "INVALID_KIT_INPUT", "message": str(exc), "details": {}}},
        ) from exc
    bp = create_persona_npc_blueprint(inputs)
    return _kit_instantiate_response(bp, "persona_npc_kit")


# ---------------------------------------------------------------------------
# Story 10.6 — Kit Smoke Run & Regression endpoints (AC1, AC4, AC6)
# ---------------------------------------------------------------------------


class KitSmokeRunRequest(BaseModel):
    kit_id: str
    blueprint: AgentBlueprint
    mock_llm: bool = True
    timeout_s: float = 60.0


@router.post("/blueprints/kit-smoke-run")
async def kit_smoke_run(req: KitSmokeRunRequest) -> Dict[str, Any]:
    """Run a Kit smoke eval pack against a Blueprint and return SmokeRunReport."""
    runner = KitSmokeRunner()
    options = SmokeRunOptions(mock_llm=req.mock_llm, timeout_s=req.timeout_s)
    report = await runner.run_smoke(req.kit_id, req.blueprint, options=options)
    return _ok(
        data=report.model_dump(mode="json"),
        meta={
            "trace_id": f"trace-{uuid4().hex[:12]}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


@router.post("/blueprints/kit-regression")
async def kit_regression(req: KitSmokeRunRequest) -> Dict[str, Any]:
    """Run a Kit regression check against persisted baseline (if any)."""
    runner = KitSmokeRunner()
    options = SmokeRunOptions(mock_llm=req.mock_llm, timeout_s=req.timeout_s)
    report = await runner.run_regression(req.kit_id, req.blueprint, options=options)
    return _ok(
        data=report.model_dump(mode="json"),
        meta={
            "trace_id": f"trace-{uuid4().hex[:12]}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )
