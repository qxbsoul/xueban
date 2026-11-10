"""Agent Catalog Service — Story 8.7

职责：
  - 充当 Builder 发布产物的索引层（不是第二个真源）
  - list_apps / get_app / register_published_app / fork_app 四个核心方法
  - 文件持久化：.xueban/catalog/{app_id}.json，复杂度与 templates/custom 同级
  - 详情/列表 API 返回脱敏后的 view，不外泄 system_prompt / provider 凭证 / BYOK 密钥

设计要点：
  AC3：CatalogAppDetail.blueprint_snapshot 是脱敏视图（已剥离 system_prompt 等内部字段）
  AC5：register_published_app 接受发布产物元数据，不重复发明发布主源
  AC6：fork_app 基于 blueprint_snapshot 克隆出全新 AgentBlueprint；记录 metadata.forked_from
  AC7：blueprint_snapshot 校验失败时抛 CatalogBlueprintInvalid，error code 稳定为
       CATALOG_BLUEPRINT_INVALID
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from xueban.runtime.contracts_builder import AgentBlueprint
from xueban.runtime.errors import XuebanError


# ---------------------------------------------------------------------------
# Storage location
# ---------------------------------------------------------------------------

_CATALOG_DIR = Path(".xueban/catalog")


# ---------------------------------------------------------------------------
# Errors (AC7)
# ---------------------------------------------------------------------------


class CatalogBlueprintInvalid(XuebanError):
    """Catalog 条目的 blueprint_snapshot 无法通过 Builder 合同校验。"""

    code = "CATALOG_BLUEPRINT_INVALID"


class CatalogAppNotFound(XuebanError):
    """请求的 app_id 在 Catalog 中不存在。"""

    code = "CATALOG_APP_NOT_FOUND"


class CatalogSnapshotMissing(XuebanError):
    """Catalog 条目存在但 blueprint_snapshot 缺失或损坏。"""

    code = "CATALOG_SNAPSHOT_MISSING"


# ---------------------------------------------------------------------------
# Sensitive-field redaction (AC3)
# ---------------------------------------------------------------------------

_SENSITIVE_TOP_LEVEL = {
    "system_prompt",
    "private_key",
    "byok",
    "byok_token",
    "api_key",
    "provider_credentials",
    "credentials",
    "secret",
    "secrets",
}

_SENSITIVE_METADATA = {
    "system_prompt",
    "private_key",
    "byok",
    "byok_token",
    "api_key",
    "provider_credentials",
    "credentials",
    "secret",
    "secrets",
    "0g_private_key",
}


def _scrub_metadata(meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(meta, dict):
        return {}
    return {k: v for k, v in meta.items() if k.lower() not in _SENSITIVE_METADATA}


def _redact_role(role: Dict[str, Any]) -> Dict[str, Any]:
    """Strip sensitive role fields and redact metadata."""
    safe = {k: v for k, v in role.items() if k not in _SENSITIVE_TOP_LEVEL}
    safe["metadata"] = _scrub_metadata(safe.get("metadata"))

    sub_agents = safe.get("sub_agents") or []
    safe["sub_agents"] = [_redact_role(s) for s in sub_agents if isinstance(s, dict)]
    return safe


def _redact_tool_policy(tp: Dict[str, Any]) -> Dict[str, Any]:
    """Replace credentials_ref with a placeholder when present; scrub metadata."""
    safe = dict(tp)
    if safe.get("credentials_ref"):
        safe["credentials_ref"] = "[redacted]"
    safe["metadata"] = _scrub_metadata(safe.get("metadata"))
    return safe


def redact_blueprint_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Return a sanitized copy of a blueprint snapshot suitable for the wire.

    Removes keys in _SENSITIVE_TOP_LEVEL at every level (root, metadata, role, role.metadata,
    role.sub_agents.*, tool_policies.*).
    """
    if not isinstance(snapshot, dict):
        return {}

    safe = {k: v for k, v in snapshot.items() if k not in _SENSITIVE_TOP_LEVEL}
    safe["metadata"] = _scrub_metadata(safe.get("metadata"))

    roles = safe.get("role_profiles") or []
    safe["role_profiles"] = [_redact_role(r) for r in roles if isinstance(r, dict)]

    tool_policies = safe.get("tool_policies") or []
    safe["tool_policies"] = [_redact_tool_policy(tp) for tp in tool_policies if isinstance(tp, dict)]

    kb_list = safe.get("knowledge_bindings") or []
    safe["knowledge_bindings"] = [
        {
            **{k: v for k, v in kb.items() if k not in _SENSITIVE_TOP_LEVEL},
            "metadata": _scrub_metadata(kb.get("metadata")),
        }
        for kb in kb_list if isinstance(kb, dict)
    ]

    return safe


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

KitType = Literal[
    "all",
    "research",
    "knowledge_assistant",
    "review_approval",
    "persona",
    "custom",
]

# Type used for stored data: kit_type never persisted as 'all' (that's a filter sentinel).
_STORED_KIT_TYPES = {"research", "knowledge_assistant", "review_approval", "persona", "custom"}


def _infer_scope_hint(blueprint: "AgentBlueprint") -> Optional[str]:
    """Return 'team_member_candidate' if any role has that scope; else None.

    Story 13.5: Used when registering a published App to catalog.
    """
    for role in blueprint.role_profiles:
        cc = role.collaboration_contract
        if cc is not None and cc.scope == "team_member_candidate":
            return "team_member_candidate"
    return None


class CatalogAppSummary(BaseModel):
    """Summary card returned in the list endpoint."""

    app_id: str
    name: str
    goal: str
    kit_type: str
    author: str
    published_at: str
    fork_count: int = 0
    forked_from: Optional[str] = None
    template_id: str = ""
    workflow_id: str = ""
    blueprint_id: str = ""
    # T04 模板 fork 入口：列表卡片按 mode=="team" 过滤团队模板（取自快照）
    mode: str = "single"
    # Story 13.5: scope_hint 字段（可选，team_member_candidate 时写入）
    scope_hint: Optional[str] = None
    # Story 13.6 D2-b: 暴露第一个 RoleProfile 的 collaboration_contract，
    # 便于前端在不读 detail 的情况下做协作匹配（PromoteToTeamWizard Step 2）。
    collaboration_contract: Optional[Dict[str, Any]] = None


class CatalogAppDetail(CatalogAppSummary):
    """Detail view — adds mode + role list + sanitized snapshot."""

    mode: Literal["single", "team"] = "single"
    role_names: List[str] = Field(default_factory=list)
    role_count: int = 0
    description: str = ""
    blueprint_snapshot: Dict[str, Any] = Field(default_factory=dict)


class CatalogForkResult(BaseModel):
    """Result of POST /catalog/apps/{app_id}/fork."""

    blueprint_id: str
    forked_from: str
    blueprint: AgentBlueprint


class RegisterPublishedAppRequest(BaseModel):
    """Inputs needed to add a published Builder result to the Catalog index."""

    blueprint: AgentBlueprint
    template_id: str = ""
    workflow_id: str = ""
    author: str = "anonymous"
    kit_type: str = "custom"
    # 内置模板种子用固定 app_id 保证幂等（catalog_templates.ensure_builtin_templates）；
    # 空串 = 正常发布路径，自动生成。
    app_id: str = ""


# ---------------------------------------------------------------------------
# Catalog Service
# ---------------------------------------------------------------------------


class CatalogService:
    """File-backed Catalog index with list / get / register / fork."""

    def __init__(self, storage_dir: Optional[Path] = None) -> None:
        self._storage_dir = storage_dir or _CATALOG_DIR

    # -- storage helpers -------------------------------------------------

    def _ensure_dir(self) -> None:
        self._storage_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, app_id: str) -> Path:
        # restrict app_id to safe chars to avoid path traversal
        if not app_id or any(c in app_id for c in {"/", "\\", "..", " ", "\x00"}):
            raise CatalogAppNotFound(
                f"Invalid app_id: {app_id!r}",
                details={"app_id": app_id},
            )
        return self._storage_dir / f"{app_id}.json"

    def _load_record(self, app_id: str) -> Dict[str, Any]:
        path = self._path_for(app_id)
        if not path.exists():
            raise CatalogAppNotFound(
                f"Catalog app not found: {app_id}",
                details={"app_id": app_id},
            )
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CatalogSnapshotMissing(
                f"Catalog record for {app_id} is unreadable: {exc}",
                details={"app_id": app_id},
            ) from exc

    def _save_record(self, app_id: str, record: Dict[str, Any]) -> None:
        self._ensure_dir()
        path = self._path_for(app_id)
        path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _iter_records(self) -> List[Dict[str, Any]]:
        if not self._storage_dir.is_dir():
            return []
        records: List[Dict[str, Any]] = []
        for p in self._storage_dir.glob("*.json"):
            try:
                records.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                # Bad file — skip silently; do not break listing
                continue
        return records

    # -- read API --------------------------------------------------------

    def list_apps(
        self,
        kit_type: str = "all",
        q: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """List published apps with filter + pagination.

        Returns: { "apps": List[CatalogAppSummary dict], "total": int, "page": int, "page_size": int }
        """
        page = max(1, page)
        page_size = max(1, min(100, page_size))

        records = self._iter_records()

        # Filter
        needle = q.strip().lower()
        kt = kit_type.strip().lower() or "all"

        def _matches(rec: Dict[str, Any]) -> bool:
            if kt != "all" and rec.get("kit_type", "custom") != kt:
                return False
            if needle:
                hay = (rec.get("name", "") + " " + rec.get("goal", "")).lower()
                if needle not in hay:
                    return False
            return True

        filtered = [r for r in records if _matches(r)]

        # Sort: published_at desc, then app_id asc as tiebreaker
        filtered.sort(
            key=lambda r: (r.get("published_at", ""), r.get("app_id", "")),
            reverse=True,
        )

        total = len(filtered)
        start = (page - 1) * page_size
        page_items = filtered[start:start + page_size]

        summaries = [CatalogAppSummary(**self._summary_of(r)).model_dump(mode="json") for r in page_items]

        return {
            "apps": summaries,
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def get_app(self, app_id: str) -> CatalogAppDetail:
        rec = self._load_record(app_id)
        snapshot_full: Dict[str, Any] = rec.get("blueprint_snapshot") or {}
        if not snapshot_full:
            raise CatalogSnapshotMissing(
                f"Catalog record for {app_id} has no blueprint_snapshot",
                details={"app_id": app_id},
            )

        snapshot_safe = redact_blueprint_snapshot(snapshot_full)

        roles = snapshot_full.get("role_profiles") or []
        role_names = [r.get("name", "") for r in roles if isinstance(r, dict)]

        summary = self._summary_of(rec)
        # detail 的 mode 由快照显式给出（Literal["single","team"]），丢弃 summary 里的
        # 透传字符串，避免与显式关键字冲突
        summary.pop("mode", None)

        return CatalogAppDetail(
            **summary,
            mode=snapshot_full.get("mode", "single"),
            role_names=role_names,
            role_count=len(role_names),
            description=snapshot_full.get("goal", ""),
            blueprint_snapshot=snapshot_safe,
        )

    # -- write API -------------------------------------------------------

    def register_published_app(
        self,
        req: RegisterPublishedAppRequest,
    ) -> CatalogAppSummary:
        """Add a published Builder Blueprint to the Catalog index (AC5).

        Stores: app_id (new), name, goal, kit_type, author, published_at,
                blueprint_id, template_id, workflow_id, forked_from, blueprint_snapshot.
        Does NOT modify the source Blueprint.
        """
        bp = req.blueprint
        forked_from: Optional[str] = None
        meta = bp.metadata or {}
        if isinstance(meta, dict):
            ff = meta.get("forked_from")
            if isinstance(ff, str) and ff:
                forked_from = ff

        kit_type = req.kit_type if req.kit_type in _STORED_KIT_TYPES else "custom"

        # Story 13.5: infer scope_hint from blueprint roles.
        # Round-1 MEDIUM-2: 仅在 register 时计算一次；catalog 记录是发布时的不可变快照，
        # 后续 blueprint 编辑不会回写已发布的 catalog app。新发布会产生新 app_id。
        scope_hint = _infer_scope_hint(bp)

        app_id = req.app_id.strip() or f"app-{uuid4().hex[:12]}"
        record: Dict[str, Any] = {
            "app_id": app_id,
            "name": bp.name,
            "goal": bp.goal,
            "kit_type": kit_type,
            "author": req.author or "anonymous",
            "published_at": datetime.now(timezone.utc).isoformat(),
            "fork_count": 0,
            "blueprint_id": bp.blueprint_id,
            "template_id": req.template_id,
            "workflow_id": req.workflow_id,
            "forked_from": forked_from,
            "blueprint_snapshot": bp.model_dump(mode="json"),
        }

        # Include scope_hint only when non-None (keeps payload clean for standalone)
        if scope_hint is not None:
            record["scope_hint"] = scope_hint

        self._save_record(app_id, record)
        return CatalogAppSummary(**self._summary_of(record))

    def fork_app(self, app_id: str) -> CatalogForkResult:
        """Clone the snapshot into a new AgentBlueprint (AC6).

        - new blueprint_id
        - keep goal / mode / role_profiles / knowledge_bindings / publish_profile
        - publish_profile.target reset to 'none' (a fork is a draft, not yet published)
        - metadata.forked_from = source app_id
        - increments source record fork_count
        """
        rec = self._load_record(app_id)
        snapshot = rec.get("blueprint_snapshot") or {}
        if not snapshot:
            raise CatalogSnapshotMissing(
                f"Catalog record for {app_id} has no blueprint_snapshot",
                details={"app_id": app_id},
            )

        # Build a forked blueprint dict — strip blueprint_id (regen) and reset publish_profile
        forked_dict = dict(snapshot)
        forked_dict.pop("blueprint_id", None)

        # Reset publish_profile so the user re-publishes intentionally
        publish_profile = dict(forked_dict.get("publish_profile") or {})
        publish_profile["target"] = "none"
        publish_profile["visibility"] = "private"
        publish_profile["publish_ref"] = ""
        forked_dict["publish_profile"] = publish_profile

        # Inject forked_from in metadata
        existing_meta = forked_dict.get("metadata") or {}
        if not isinstance(existing_meta, dict):
            existing_meta = {}
        existing_meta = {**existing_meta, "forked_from": app_id}
        forked_dict["metadata"] = existing_meta

        try:
            forked = AgentBlueprint.model_validate(forked_dict)
        except Exception as exc:
            raise CatalogBlueprintInvalid(
                f"Stored blueprint snapshot failed validation for {app_id}: {exc}",
                details={"app_id": app_id, "reason": str(exc)},
            ) from exc

        # bump fork_count atomically (best-effort, single process)
        rec["fork_count"] = int(rec.get("fork_count", 0)) + 1
        self._save_record(app_id, rec)

        return CatalogForkResult(
            blueprint_id=forked.blueprint_id,
            forked_from=app_id,
            blueprint=forked,
        )

    # -- internals -------------------------------------------------------

    @staticmethod
    def _summary_of(rec: Dict[str, Any]) -> Dict[str, Any]:
        summary: Dict[str, Any] = {
            "app_id": rec.get("app_id", ""),
            "name": rec.get("name", ""),
            "goal": rec.get("goal", ""),
            "kit_type": rec.get("kit_type", "custom"),
            "author": rec.get("author", "anonymous"),
            "published_at": rec.get("published_at", ""),
            "fork_count": int(rec.get("fork_count", 0) or 0),
            "forked_from": rec.get("forked_from"),
            "template_id": rec.get("template_id", "") or "",
            "workflow_id": rec.get("workflow_id", "") or "",
            "blueprint_id": rec.get("blueprint_id", "") or "",
        }
        # Story 13.5: include scope_hint when present
        scope_hint = rec.get("scope_hint")
        if scope_hint is not None:
            summary["scope_hint"] = scope_hint
        # Story 13.6 D2-b: surface first role's collaboration_contract from snapshot.
        snap = rec.get("blueprint_snapshot")
        if isinstance(snap, dict):
            # 团队模板标记：快照 mode（single/team）透传到 summary，供前端模板入口过滤
            summary["mode"] = snap.get("mode", "single")
            roles = snap.get("role_profiles") or []
            if roles and isinstance(roles[0], dict):
                cc = roles[0].get("collaboration_contract")
                if isinstance(cc, dict):
                    summary["collaboration_contract"] = cc
        return summary


# ---------------------------------------------------------------------------
# Singleton accessor (matches existing module pattern)
# ---------------------------------------------------------------------------

_SERVICE_SINGLETON: Optional[CatalogService] = None


def get_service() -> CatalogService:
    global _SERVICE_SINGLETON
    if _SERVICE_SINGLETON is None:
        _SERVICE_SINGLETON = CatalogService()
    return _SERVICE_SINGLETON


def set_service(svc: CatalogService) -> None:
    global _SERVICE_SINGLETON
    _SERVICE_SINGLETON = svc
