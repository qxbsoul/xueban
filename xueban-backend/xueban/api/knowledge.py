"""Knowledge API — Story 9.1 AC4.

Routes:
  POST   /knowledge/packs                — create pack (async ingest)
  GET    /knowledge/packs                — list packs (limit / offset)
  GET    /knowledge/packs/{pack_id}      — single-pack detail
  PATCH  /knowledge/packs/{pack_id}      — update name / description / retrieval_profile
  DELETE /knowledge/packs/{pack_id}      — delete pack
  POST   /knowledge/packs/{pack_id}/reindex — force re-ingest
  POST   /knowledge/packs/{pack_id}/query   — RAG query over the pack's index

All success responses use the shared `{data, meta}` envelope.
All errors raise XuebanError → handled by the global handler in
`xueban.server`, which produces `{error: {code, message, trace_id}}`.

Hardening (post-review 2026-04-27):
  - C4 / H2: pack_id is a UUID-hex allowlist; non-matching IDs raise
    KnowledgePackInvalidId (400) instead of being conflated with NotFound (404).
  - C5 / H6: BG ingest re-checks pack existence before each save, logs failures,
    and flips status to "failed" instead of silently swallowing XuebanError.
  - H7: delete uses shutil.rmtree to handle nested directories (e.g. pdfplumber
    cache) cleanly.
  - H5: PATCH on retrieval_profile fields that affect chunking
    (chunk_size / overlap / mode) auto-enqueues a reindex so the persisted
    index stays consistent with the profile.
  - L2: timestamps emitted with the canonical `Z` UTC suffix.
  - M3: corrupted records on listing log a warning and surface a `meta.skipped`
    counter so operators can see ingestion drift.
  - C3 piggybacks: pack.json writes go through atomic_write_text + per-pack lock.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Query
from pydantic import BaseModel, ConfigDict, Field

from xueban.memory.ingest_pipeline import (
    atomic_write_text,
    pack_lock,
    run_ingest,
)
from xueban.memory.knowledge_pack import (
    FreshnessPolicy,
    KnowledgePack,
    KnowledgeSource,
    SourceType,
    update_pack,
)
from xueban.memory.retrieval_profiles import RetrievalProfile
from xueban.runtime.errors import XuebanError


logger = logging.getLogger(__name__)


_KNOWLEDGE_DIR = Path(".xueban/knowledge")

# C4: pack_id allowlist. Production pack IDs are UUID4 hex (32 chars). Any
# user-supplied path that doesn't match is rejected with a distinct 400 error
# so attack attempts don't show up as 404s in audit logs.
_PACK_ID_RE = re.compile(r"^[a-f0-9]{32}$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class KnowledgePackNotFound(XuebanError):
    """Requested pack_id does not exist."""

    code = "KNOWLEDGE_PACK_NOT_FOUND"


class KnowledgePackInvalid(XuebanError):
    """Persisted pack record could not be validated against the current schema."""

    code = "KNOWLEDGE_PACK_INVALID"


class KnowledgePackInvalidId(XuebanError):
    """pack_id failed format / safety validation (e.g. path-traversal attempt)."""

    code = "KNOWLEDGE_PACK_INVALID_ID"


# ---------------------------------------------------------------------------
# Service (file-backed; mirrors CatalogService convention)
# ---------------------------------------------------------------------------


class KnowledgeService:
    """File-backed CRUD service for KnowledgePack."""

    def __init__(self, storage_dir: Optional[Path] = None) -> None:
        self._storage_dir = storage_dir or _KNOWLEDGE_DIR

    # -- storage helpers -------------------------------------------------

    def _ensure_dir(self) -> None:
        self._storage_dir.mkdir(parents=True, exist_ok=True)

    def _record_path(self, pack_id: str) -> Path:
        # C4: explicit allowlist instead of substring blocklist. UUID4 hex shape.
        if not pack_id or not _PACK_ID_RE.match(pack_id):
            raise KnowledgePackInvalidId(
                f"Invalid pack_id format: {pack_id!r} (expected 32-char hex UUID)",
                details={"pack_id": pack_id},
            )
        return self._storage_dir / pack_id / "pack.json"

    def pack_dir(self, pack_id: str) -> Path:
        """Return the per-pack storage directory; raises if pack_id is malformed."""
        return self._record_path(pack_id).parent

    def exists(self, pack_id: str) -> bool:
        """Cheap existence check used by the BG task before each save (C5)."""
        try:
            return self._record_path(pack_id).exists()
        except XuebanError:
            return False

    def save(self, pack: KnowledgePack) -> KnowledgePack:
        """Atomically persist `pack`. Holds the per-pack lock so concurrent
        BG ingests don't lose updates (C3)."""
        self._ensure_dir()
        target = self._record_path(pack.pack_id)
        with pack_lock(pack.pack_id):
            atomic_write_text(
                target,
                json.dumps(pack.model_dump(mode="json"), ensure_ascii=False, indent=2),
            )
        return pack

    def load(self, pack_id: str) -> KnowledgePack:
        target = self._record_path(pack_id)
        if not target.exists():
            raise KnowledgePackNotFound(
                f"KnowledgePack not found: {pack_id}",
                details={"pack_id": pack_id},
            )
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            return KnowledgePack.model_validate(payload)
        except json.JSONDecodeError as exc:
            raise KnowledgePackInvalid(
                f"KnowledgePack record corrupted for {pack_id}: {exc}",
                details={"pack_id": pack_id},
            ) from exc
        except ValueError as exc:  # pydantic validation
            raise KnowledgePackInvalid(
                f"KnowledgePack record failed validation for {pack_id}: {exc}",
                details={"pack_id": pack_id},
            ) from exc

    def delete(self, pack_id: str) -> None:
        self._record_path(pack_id)  # validate pack_id format first (raises on bad IDs)
        # H7: shutil.rmtree handles arbitrarily nested directories (e.g. cache
        # subdirs from optional parsers). Hold the per-pack lock so a BG ingest
        # mid-flight can't recreate the directory after we wipe it (C5 partner).
        # P4: exists() check moved inside the lock to avoid TOCTOU on concurrent
        # DELETE (two concurrent requests both pass exists(), second rmtree raises
        # FileNotFoundError which we convert to NotFound rather than 500).
        with pack_lock(pack_id):
            target = self._record_path(pack_id)
            if not target.exists():
                raise KnowledgePackNotFound(
                    f"KnowledgePack not found: {pack_id}",
                    details={"pack_id": pack_id},
                )
            try:
                shutil.rmtree(target.parent, ignore_errors=False)
            except FileNotFoundError:
                raise KnowledgePackNotFound(
                    f"KnowledgePack not found: {pack_id}",
                    details={"pack_id": pack_id},
                )

    def list(self, limit: int = 20, offset: int = 0) -> Dict[str, Any]:
        """List packs ordered by created_at desc.

        Returns `{packs, total, limit, offset, skipped}`. `skipped` counts
        records that failed validation/parse — they're left on disk so an
        operator can investigate, but the route surfaces the count via meta
        so the UX doesn't silently drop packs (M3).
        """
        if not self._storage_dir.is_dir():
            return {"packs": [], "total": 0, "limit": limit, "offset": offset, "skipped": 0}

        items: List[KnowledgePack] = []
        skipped = 0
        for pack_dir in sorted(self._storage_dir.iterdir()):
            if not pack_dir.is_dir():
                continue
            target = pack_dir / "pack.json"
            if not target.exists():
                continue
            try:
                payload = json.loads(target.read_text(encoding="utf-8"))
                items.append(KnowledgePack.model_validate(payload))
            except (json.JSONDecodeError, ValueError, OSError) as exc:
                # OSError covers FileNotFoundError from a concurrent DELETE that
                # removed the file between iterdir() and read_text() (P5).
                skipped += 1
                logger.warning(
                    "KnowledgeService.list skipped corrupted record %s: %s",
                    pack_dir.name,
                    exc,
                )
                continue

        items.sort(key=lambda p: p.created_at, reverse=True)
        total = len(items)
        page = items[offset : offset + limit]
        return {
            "packs": [p.model_dump(mode="json") for p in page],
            "total": total,
            "limit": limit,
            "offset": offset,
            "skipped": skipped,
        }


# ---------------------------------------------------------------------------
# Service singleton
# ---------------------------------------------------------------------------

_SERVICE_SINGLETON: Optional[KnowledgeService] = None


def get_service() -> KnowledgeService:
    global _SERVICE_SINGLETON
    if _SERVICE_SINGLETON is None:
        _SERVICE_SINGLETON = KnowledgeService()
    return _SERVICE_SINGLETON


def set_service(svc: KnowledgeService) -> None:
    global _SERVICE_SINGLETON
    _SERVICE_SINGLETON = svc


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class KnowledgeSourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_type: SourceType
    source_ref: str = Field(min_length=1)
    mime_type: str = ""


class CreatePackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    sources: List[KnowledgeSourceInput] = Field(min_length=1)
    retrieval_profile: Optional[RetrievalProfile] = None
    citation_required: bool = False
    freshness_policy: FreshnessPolicy = "on_demand"


class UpdatePackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = None
    retrieval_profile: Optional[RetrievalProfile] = None
    citation_required: Optional[bool] = None
    freshness_policy: Optional[FreshnessPolicy] = None


class QueryPackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=50)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    """ISO 8601 UTC with the canonical `Z` suffix (per .claude/rules/datetime.md, L2)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rag_backend_label() -> str:
    """Return the active RAG backend label for response meta."""
    try:
        from xueban.runtime import lightrag_adapter  # noqa: PLC0415
        return lightrag_adapter.backend_label()
    except Exception:  # noqa: BLE001 — non-critical
        return "stub"


def _envelope(data: Any, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "data": data,
        "meta": {
            "trace_id": f"trace-{uuid4().hex[:12]}",
            "timestamp": _now(),
            "rag_backend": _rag_backend_label(),
            **(meta or {}),
        },
    }


# Fields whose change requires a fresh ingest because they govern chunking/index
# semantics. Used by PATCH to auto-enqueue reindex (H5).
_REINDEX_TRIGGER_FIELDS: Tuple[str, ...] = ("chunk_size", "overlap", "mode")


def _profile_changes_require_reindex(
    old: RetrievalProfile, new: RetrievalProfile
) -> bool:
    return any(
        getattr(old, f) != getattr(new, f) for f in _REINDEX_TRIGGER_FIELDS
    )


def _reset_sources_for_reindex(pack: KnowledgePack) -> List[KnowledgeSource]:
    return [
        s.model_copy(
            update={
                "ingest_status": "pending",
                "error_message": "",
                "chunk_count": 0,
            }
        )
        for s in pack.sources
    ]


def _ingest_and_save(pack_id: str, svc: KnowledgeService) -> None:
    """Background task: load pack, mark indexing, run ingest, save final state.

    C5/H6: re-checks pack existence before EVERY save so a concurrent DELETE
    doesn't get its directory recreated by `mkdir(exist_ok=True)`. XuebanError
    no longer silently returns — failures are logged and (when possible) surfaced
    in the persisted pack record so the polling UI sees `failed` instead of
    spinning forever on `pending`/`indexing`.
    """
    # Hold the per-pack lock for the entire BG run so DELETE / PATCH / a second
    # ingest serialize cleanly. Anything that takes the same lock will block.
    with pack_lock(pack_id):
        try:
            pack = svc.load(pack_id)
        except KnowledgePackNotFound:
            logger.info(
                "BG ingest skipped: pack %s already deleted before start", pack_id
            )
            return
        except XuebanError as exc:
            logger.warning(
                "BG ingest aborted on load for %s: %s (%s)",
                pack_id,
                exc.message,
                exc.code,
            )
            return

        indexing = update_pack(pack, status="indexing")
        if not svc.exists(pack_id):
            logger.info("BG ingest aborted: pack %s removed before indexing", pack_id)
            return
        try:
            svc.save(indexing)
        except XuebanError as exc:
            logger.warning("BG ingest aborted on save(indexing) for %s: %s", pack_id, exc.message)
            return

        try:
            finished = run_ingest(indexing)
        except Exception as exc:
            logger.exception("BG ingest crashed for %s", pack_id)
            try:
                finished = update_pack(
                    indexing,
                    status="failed",
                    sources=[
                        {
                            **s.model_dump(),
                            "ingest_status": "failed",
                            "error_message": (
                                f"unexpected ingest error: {type(exc).__name__}: {exc}"
                            ),
                        }
                        for s in indexing.sources
                    ],
                )
            except Exception:  # pragma: no cover — last-resort guard
                logger.exception(
                    "BG ingest could not record failure state for %s", pack_id
                )
                return

        # C5: final guard — the pack might have been DELETEd while ingest was
        # parsing/indexing. Don't resurrect it.
        if not svc.exists(pack_id):
            logger.info(
                "BG ingest discarded result: pack %s deleted during run", pack_id
            )
            return
        try:
            svc.save(finished)
        except XuebanError as exc:  # pragma: no cover — defensive
            logger.warning(
                "BG ingest could not save final state for %s: %s",
                pack_id,
                exc.message,
            )
            return

        # 层级索引（借鉴 StaffDeck）：ingest 完成后顺手把文档树持久化；
        # 失败仅记日志 —— 查询/浏览时会现场重建，不影响 ingest 结果。
        try:
            from xueban.runtime import hier_index  # noqa: PLC0415

            hier_index.build_index(finished, root=svc.pack_dir(pack_id).parent)
        except Exception as exc:  # noqa: BLE001 — non-critical, rebuilt lazily
            logger.warning("hier_index build skipped for pack %s: %s", pack_id, exc)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


# 2026-08-19 前缀对齐：/knowledge → /api/knowledge。整条网关链路
# （Vite 代理 → Node:8002 → proxyFallback）只路由 /api/*；旧裸路径是
# VITE_API_BASE 直连 Python 时代的遗留，前端已同步迁移（src/api/knowledge.ts）。
router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


@router.post("/packs")
async def create_pack(
    payload: CreatePackRequest,
    background_tasks: BackgroundTasks,
) -> Dict[str, Any]:
    svc = get_service()

    sources = [
        KnowledgeSource(
            source_type=s.source_type,
            source_ref=s.source_ref,
            mime_type=s.mime_type,
        )
        for s in payload.sources
    ]
    pack = KnowledgePack(
        name=payload.name,
        description=payload.description,
        sources=sources,
        retrieval_profile=payload.retrieval_profile or RetrievalProfile(),
        citation_required=payload.citation_required,
        freshness_policy=payload.freshness_policy,
        status="pending",
    )
    svc.save(pack)
    background_tasks.add_task(_ingest_and_save, pack.pack_id, svc)
    return _envelope(pack.model_dump(mode="json"))


@router.get("/packs")
async def list_packs(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    svc = get_service()
    page = svc.list(limit=limit, offset=offset)
    return _envelope(
        data={"packs": page["packs"]},
        meta={
            "total": page["total"],
            "limit": page["limit"],
            "offset": page["offset"],
            "skipped": page["skipped"],
        },
    )


@router.get("/packs/{pack_id}")
async def get_pack(pack_id: str) -> Dict[str, Any]:
    svc = get_service()
    pack = svc.load(pack_id)
    return _envelope(pack.model_dump(mode="json"))


@router.patch("/packs/{pack_id}")
async def patch_pack(
    pack_id: str,
    payload: UpdatePackRequest,
    background_tasks: BackgroundTasks,
) -> Dict[str, Any]:
    svc = get_service()

    # P3: hold the per-pack lock across the entire load→compute→save sequence so
    # a concurrent BG ingest cannot overwrite our changes (or vice-versa). The
    # lock is an RLock so the nested svc.save() that also acquires it is safe.
    needs_reindex = False
    with pack_lock(pack_id):
        pack = svc.load(pack_id)

        changes: Dict[str, Any] = {}
        if payload.name is not None:
            changes["name"] = payload.name
        if payload.description is not None:
            changes["description"] = payload.description
        if payload.retrieval_profile is not None:
            if _profile_changes_require_reindex(pack.retrieval_profile, payload.retrieval_profile):
                needs_reindex = True
            changes["retrieval_profile"] = payload.retrieval_profile
        if payload.citation_required is not None:
            changes["citation_required"] = payload.citation_required
        if payload.freshness_policy is not None:
            changes["freshness_policy"] = payload.freshness_policy

        if needs_reindex:
            # H5: chunking-relevant changes invalidate the persisted index. Reset
            # sources to pending and enqueue a fresh ingest so the index stays in
            # sync with the new profile.
            changes["sources"] = [s.model_dump() for s in _reset_sources_for_reindex(pack)]
            changes["status"] = "pending"

        updated = update_pack(pack, **changes)
        svc.save(updated)

    if needs_reindex:
        background_tasks.add_task(_ingest_and_save, updated.pack_id, svc)
    return _envelope(updated.model_dump(mode="json"))


@router.delete("/packs/{pack_id}")
async def delete_pack(pack_id: str) -> Dict[str, Any]:
    svc = get_service()
    svc.delete(pack_id)
    # Best-effort cleanup of LightRAG index dir (no-op when LIGHTRAG_ENABLED is false).
    try:
        from xueban.runtime import lightrag_adapter  # noqa: PLC0415
        lightrag_adapter.delete_pack_index(pack_id)
    except Exception:  # noqa: BLE001 — cleanup is non-critical
        logger.debug("lightrag_adapter.delete_pack_index skipped for %s", pack_id)
    return _envelope({"deleted": True, "pack_id": pack_id})


@router.post("/packs/{pack_id}/reindex")
async def reindex_pack(
    pack_id: str,
    background_tasks: BackgroundTasks,
) -> Dict[str, Any]:
    svc = get_service()
    # P3: same load→save lock guard as patch_pack to prevent lost-update race.
    with pack_lock(pack_id):
        pack = svc.load(pack_id)
        reset_sources = _reset_sources_for_reindex(pack)
        pack = update_pack(pack, sources=[s.model_dump() for s in reset_sources], status="pending")
        svc.save(pack)
    background_tasks.add_task(_ingest_and_save, pack.pack_id, svc)
    return _envelope(pack.model_dump(mode="json"))


def _graph_context(
    pack_id: str, question: str, *, root: Optional[Path] = None
) -> Optional[Dict[str, Any]]:
    """PackGraph 图遍历扩展：query 提及的实体 + 1 跳邻居。

    与 hier_index / LightRAG 同一容错语义：任何失败只告警并返回 None，
    绝不阻断检索主路径。
    """
    try:
        from xueban.memory import pack_graph  # noqa: PLC0415

        return pack_graph.query_context(pack_id, question, root=root)
    except Exception as exc:  # noqa: BLE001 — graph expansion must not break queries
        logger.warning("pack_graph query_context failed for pack %s: %s", pack_id, exc)
        return None


@router.post("/packs/{pack_id}/query")
async def query_pack(pack_id: str, payload: QueryPackRequest) -> Dict[str, Any]:
    """RAG query over a pack's index.

    层级检索优先（借鉴 StaffDeck）：hier_index 先在文档树上定位候选
    文档/章节节点，再在其子树内取 chunk 合成 hits —— 命中时直接返回，每条带
    `path`（doc > chapter 层级来源），响应带 `trace`（定位路径，供检索调试）。

    层级索引无命中 / 构建失败 → 回落现有平铺检索：LightRAG enabled → prefer
    lightrag_adapter.query_hits() and pass through the real retrieved fragments
    as multiple `{text, source, score}` hits.  When the adapter can't do
    structured retrieval (raises), fall back to the original behaviour: wrap the
    synthesized answer as a single hit.  Disabled / not configured / query
    failure → empty `hits` (never 500): callers degrade to "no citations"
    instead of breaking the chat flow.

    两条路径的响应都带 `graph_context`（PackGraph 图遍历扩展：query 提及的
    实体 + 1 跳邻居，无 LLM、确定性）；图不存在或无命中时为 null。
    """
    svc = get_service()
    pack = svc.load(pack_id)  # same 400/404 guards as the other handlers

    hits: List[Dict[str, Any]] = []
    trace: Dict[str, Any] = {"strategy": "flat", "reason": "hier-unavailable"}

    # 层级检索（locate → retrieve）。老 pack 无索引时 hier_index 现场构建；
    # 任何失败都不阻断下面的平铺检索。
    try:
        from xueban.runtime import hier_index  # noqa: PLC0415

        hier_hits, hier_trace = hier_index.query(
            pack, payload.query, top_k=payload.top_k, root=svc.pack_dir(pack_id).parent
        )
        if hier_hits:
            return _envelope(
                data={
                    "hits": hier_hits[: payload.top_k],
                    "trace": hier_trace,
                    "graph_context": _graph_context(
                        pack_id, payload.query, root=svc.pack_dir(pack_id).parent
                    ),
                },
                meta={"pack_id": pack_id, "query": payload.query},
            )
        trace = {
            "strategy": "flat",
            "reason": "hier-no-match",
            "located": hier_trace.get("located", []),
        }
    except Exception as exc:  # noqa: BLE001 — hierarchical index must not break queries
        logger.warning(
            "hier_index query unavailable for pack %s, falling back to flat retrieval: %s",
            pack_id,
            exc,
        )

    try:
        from xueban.runtime import lightrag_adapter  # noqa: PLC0415

        if lightrag_adapter.is_enabled():
            raw_hits = lightrag_adapter.query_hits(
                pack_id, payload.query, top_k=payload.top_k
            )
            for raw in raw_hits:
                if not isinstance(raw, dict):
                    continue
                text = str(raw.get("text") or "").strip()
                if not text:
                    continue
                score = raw.get("score")
                hits.append(
                    {
                        "text": text,
                        "source": str(raw.get("source") or "").strip() or pack.name,
                        "score": float(score) if isinstance(score, (int, float)) else 1.0,
                    }
                )
    except Exception as exc:  # noqa: BLE001 — query failure must not 500
        logger.warning(
            "knowledge query_hits unavailable for pack %s, falling back to synthesized answer: %s",
            pack_id,
            exc,
        )
        # 回落：adapter 不支持多片段检索 / 检索失败 → 保持原有行为，把
        # LightRAG 合成答案包成单条 hit（score=1.0, source=pack 名）。
        try:
            answer = lightrag_adapter.query(pack_id, payload.query)  # type: ignore[possibly-undefined]
            if isinstance(answer, str) and answer.strip():
                hits = [{"text": answer.strip(), "source": pack.name, "score": 1.0}]
        except Exception as exc2:  # noqa: BLE001 — never 500
            logger.warning("knowledge query failed for pack %s: %s", pack_id, exc2)

    return _envelope(
        data={
            "hits": hits[: payload.top_k],
            "trace": trace,
            "graph_context": _graph_context(
                pack_id, payload.query, root=svc.pack_dir(pack_id).parent
            ),
        },
        meta={"pack_id": pack_id, "query": payload.query},
    )


# ---------------------------------------------------------------------------
# Hierarchical index browsing (借鉴 StaffDeck 的文档→章节→页面可导航索引)
#
# 树由 xueban/runtime/hier_index.py 构建并持久化在 pack 目录下
# (hier_index.json)；老 pack 首次访问时现场构建。chunk 正文不透出，只给
# 节点结构 + 统计，供前端做树浏览。
# ---------------------------------------------------------------------------


@router.get("/packs/{pack_id}/index")
async def get_pack_index(pack_id: str) -> Dict[str, Any]:
    svc = get_service()
    pack = svc.load(pack_id)  # 404/400 guards, same as other handlers
    try:
        from xueban.runtime import hier_index  # noqa: PLC0415

        tree = hier_index.tree_view(pack, root=svc.pack_dir(pack_id).parent)
    except Exception as exc:  # noqa: BLE001 — 构建失败给空树，不 500
        logger.warning("hier_index tree_view failed for pack %s: %s", pack_id, exc)
        tree = {
            "pack_id": pack_id,
            "built_at": "",
            "documents": [],
            "stats": {"documents": 0, "nodes": 0, "chunks": 0},
        }
    return _envelope(data=tree, meta={"pack_id": pack_id})


# ---------------------------------------------------------------------------
# Knowledge graph export (2026-08-19, 从 Shadow 搬迁知识图谱可视化的数据源)
#
# LightRAG 的实体-关系图以 GraphML 落在 working_dir/graph_*.graphml。这里用
# 标准库 xml.etree 解析（stub 模式无 networkx 依赖也能跑），转成前端
# GraphData 形状（见 src/common/utils/graphAnalysis.ts）。
# ---------------------------------------------------------------------------


def _parse_graphml(graphml_path: Path) -> Dict[str, Any]:
    """Parse a LightRAG GraphML file into {nodes, links} JSON."""
    root = ET.parse(graphml_path).getroot()
    ns = {"g": root.tag.split("}")[0] + "}"} if root.tag.startswith("{") else {}

    def _q(tag: str) -> str:
        return ns["g"] + tag if ns else tag

    key_names: Dict[str, str] = {}
    for key in root.findall(f"{_q('key')}"):
        key_names[key.get("id", "")] = key.get("attr.name", key.get("id", ""))

    def _attrs(element: ET.Element) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for data in element.findall(f"{_q('data')}"):
            name = key_names.get(data.get("key", ""), data.get("key", ""))
            if name:
                out[name] = (data.text or "").strip()
        return out

    degree: Dict[str, int] = {}
    raw_links: List[Dict[str, str]] = []
    for edge in root.iter(_q("edge")):
        src, tgt = edge.get("source", ""), edge.get("target", "")
        if not src or not tgt:
            continue
        attrs = _attrs(edge)
        raw_links.append({
            "source": src,
            "target": tgt,
            "relation": attrs.get("relation") or attrs.get("weight") or "related",
        })
        degree[src] = degree.get(src, 0) + 1
        degree[tgt] = degree.get(tgt, 0) + 1

    nodes: List[Dict[str, Any]] = []
    for node in root.iter(_q("node")):
        nid = node.get("id", "")
        if not nid:
            continue
        attrs = _attrs(node)
        name = attrs.get("entity_id") or attrs.get("label") or nid
        nodes.append({
            "id": nid,
            "name": name,
            # LightRAG entity_type 是自由字符串（person/org/event/...），
            # 归一到 Shadow 图谱的 'tag'（概念节点），原值放 raw_type。
            "type": "tag",
            "raw_type": attrs.get("entity_type", ""),
            "text": attrs.get("description", ""),
            "val": min(degree.get(nid, 0) + 1, 10),
        })

    return {"nodes": nodes, "links": raw_links}


def _load_pack_graph(
    pack_id: str, *, root: Optional[Path] = None
) -> Tuple[Dict[str, Any], str, str, int, Optional[Dict[str, Any]]]:
    """Return ({nodes, links}, rag_backend, graph_backend, conflict_count, health).

    graph_backend: ``lightrag``（LLM 抽取的 GraphML）→ ``pack_graph``（确定性
    图层，LightRAG 缺席/未产出时的 fallback）→ ``none``（两边都没有）。
    health 为图健康指标（孤岛/hub/密度/连通分量），两种后端都计算；空图为 None。
    """
    from xueban.memory import pack_graph  # noqa: PLC0415
    from xueban.runtime import lightrag_adapter  # noqa: PLC0415

    backend = lightrag_adapter.backend_label()
    if lightrag_adapter.is_enabled():
        base = Path(os.environ.get("LIGHTRAG_WORKING_DIR", ".xueban/lightrag"))
        graphmls = sorted((base / pack_id).glob("graph_*.graphml")) if (base / pack_id).exists() else []
        if graphmls:
            data = _parse_graphml(graphmls[0])
            return (
                data,
                backend,
                "lightrag",
                0,
                pack_graph.graph_health(data["nodes"], data["links"]),
            )

    # Deterministic fallback: PackGraph built at ingest (no LLM required).
    try:
        payload = pack_graph.load_graph(pack_id, root=root)
    except Exception as exc:  # noqa: BLE001 — graph fallback must not 500
        logger.warning("pack_graph load failed for pack %s: %s", pack_id, exc)
        payload = None
    if payload and payload.get("nodes"):
        data = pack_graph.to_graph_data(payload)
        graph = {"nodes": data["nodes"], "links": data["links"]}
        return (
            graph,
            backend,
            "pack_graph",
            len(data.get("conflicts") or []),
            pack_graph.graph_health(graph["nodes"], graph["links"]),
        )
    return {"nodes": [], "links": []}, backend, "none", 0, None


@router.get("/packs/{pack_id}/graph")
async def get_pack_graph(pack_id: str) -> Dict[str, Any]:
    svc = get_service()
    svc.load(pack_id)  # 404 when pack missing (same guard as other handlers)
    graph, backend, graph_backend, conflict_count, health = _load_pack_graph(
        pack_id, root=svc.pack_dir(pack_id).parent
    )
    return {
        "data": graph,
        "meta": {
            "pack_id": pack_id,
            "rag_backend": backend,
            "graph_backend": graph_backend,
            "conflict_count": conflict_count,
            "health": health,
            "node_count": len(graph["nodes"]),
            "link_count": len(graph["links"]),
        },
    }


__all__ = [
    "router",
    "KnowledgeService",
    "get_service",
    "set_service",
    "KnowledgePackNotFound",
    "KnowledgePackInvalid",
    "KnowledgePackInvalidId",
]
