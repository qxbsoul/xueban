"""LightRAG adapter — optional RAG backend for KnowledgePack (Story 9.1 Phase 2).

Activation: set ``LIGHTRAG_ENABLED=true`` in .env.  When disabled (the default)
every call falls through to the existing stub path in ingest_pipeline.py so
behaviour is completely unchanged for existing deployments.

LightRAG storage is 100 % local file-based:
  - KV store   → .xueban/lightrag/{pack_id}/kv_store_*.json
  - Vector store → .xueban/lightrag/{pack_id}/vdb_*.json
  - Graph store → .xueban/lightrag/{pack_id}/graph_*.graphml
  No PostgreSQL, Neo4j, Redis, or any other external service is required.

Configuration (all via .env / environment variables):
  LIGHTRAG_ENABLED          = true          # must be exactly "true" (case-insensitive)
  LIGHTRAG_WORKING_DIR      = .xueban/lightrag   # base dir; pack dirs are sub-dirs
  LIGHTRAG_LLM_BASE_URL     = https://api.openai.com/v1   # OpenAI-compatible endpoint
  LIGHTRAG_LLM_API_KEY      = sk-...        # BYOK — never hardcoded
  LIGHTRAG_LLM_MODEL        = gpt-4o-mini  # any model the endpoint supports
  LIGHTRAG_EMBED_BASE_URL   = https://api.openai.com/v1
  LIGHTRAG_EMBED_API_KEY    = sk-...        # can be the same key
  LIGHTRAG_EMBED_MODEL      = text-embedding-3-small
  LIGHTRAG_EMBED_DIM        = 1536          # must match the chosen model
  LIGHTRAG_QUERY_MODE       = hybrid        # naive | local | global | hybrid
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feature flag helpers
# ---------------------------------------------------------------------------

_ENABLED_ENV = "LIGHTRAG_ENABLED"


def is_enabled() -> bool:
    """Return True only if ``LIGHTRAG_ENABLED=true`` (case-insensitive) is set."""
    return os.environ.get(_ENABLED_ENV, "").strip().lower() == "true"


def _working_dir(pack_id: str) -> Path:
    base = Path(os.environ.get("LIGHTRAG_WORKING_DIR", ".xueban/lightrag"))
    d = base / pack_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# LightRAG instance factory (lazy import to avoid hard dep when disabled)
# ---------------------------------------------------------------------------

def _build_rag(pack_id: str) -> Any:
    """Instantiate and return a LightRAG instance configured for local file storage.

    Raises ImportError if lightrag-hku is not installed (handled by callers).
    Raises LightRagConfigError if required env vars are missing.
    """
    try:
        from lightrag import LightRAG  # type: ignore[import]
        from lightrag.llm.openai import (  # type: ignore[import]
            openai_complete_if_cache,
            openai_embed,
        )
        from lightrag.utils import EmbeddingFunc  # type: ignore[import]
        from functools import partial
    except ImportError as exc:
        raise ImportError(
            "lightrag-hku is not installed. "
            "Run: pip install 'xueban[lightrag]' or pip install lightrag-hku"
        ) from exc

    llm_base = os.environ.get("LIGHTRAG_LLM_BASE_URL", "https://api.openai.com/v1")
    llm_key = os.environ.get("LIGHTRAG_LLM_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
    llm_model = os.environ.get("LIGHTRAG_LLM_MODEL", "gpt-4o-mini")

    embed_base = os.environ.get("LIGHTRAG_EMBED_BASE_URL", "https://api.openai.com/v1")
    embed_key = os.environ.get("LIGHTRAG_EMBED_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
    embed_model = os.environ.get("LIGHTRAG_EMBED_MODEL", "text-embedding-3-small")
    embed_dim = int(os.environ.get("LIGHTRAG_EMBED_DIM", "1536"))

    if not llm_key:
        raise LightRagConfigError(
            "LIGHTRAG_LLM_API_KEY (or OPENAI_API_KEY) is required when LIGHTRAG_ENABLED=true"
        )
    if not embed_key:
        raise LightRagConfigError(
            "LIGHTRAG_EMBED_API_KEY (or OPENAI_API_KEY) is required when LIGHTRAG_ENABLED=true"
        )

    async def _llm_func(prompt: str, system_prompt: Optional[str] = None, **kw: Any) -> str:
        return await openai_complete_if_cache(
            llm_model,
            prompt,
            system_prompt=system_prompt,
            api_key=llm_key,
            base_url=llm_base,
            **kw,
        )

    async def _embed_func(texts: List[str]) -> Any:
        return await openai_embed(
            texts,
            model=embed_model,
            api_key=embed_key,
            base_url=embed_base,
        )

    rag = LightRAG(
        working_dir=str(_working_dir(pack_id)),
        llm_model_func=_llm_func,
        embedding_func=EmbeddingFunc(
            embedding_dim=embed_dim,
            max_token_size=8192,
            func=_embed_func,
        ),
        # Explicit local-only storage backends (all default to local file mode):
        kv_storage="JsonKVStorage",          # → kv_store_*.json files
        vector_storage="NanoVectorDBStorage", # → vdb_*.json files
        graph_storage="NetworkXStorage",      # → graph_*.graphml files
        doc_status_storage="JsonDocStatusStorage",
    )
    return rag


# ---------------------------------------------------------------------------
# Public errors
# ---------------------------------------------------------------------------

class LightRagConfigError(Exception):
    """Raised when required env vars are missing or malformed."""


class LightRagIngestError(Exception):
    """Raised when LightRAG insert fails."""


class LightRagQueryError(Exception):
    """Raised when LightRAG query fails."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ingest_text(pack_id: str, text: str, *, metadata: Optional[Dict[str, Any]] = None) -> None:
    """Ingest `text` into the LightRAG index for `pack_id`.

    This is a synchronous wrapper that runs the async LightRAG insert in a
    fresh event loop (or the current one if already inside one), matching the
    synchronous contract expected by ``run_ingest`` in ingest_pipeline.py.

    Args:
        pack_id: The UUID-hex identifier of the KnowledgePack.
        text:    Plain text content to index (already parsed by the pipeline).
        metadata: Optional dict carried to LightRAG (currently passed as-is).

    Raises:
        ImportError:         lightrag-hku not installed.
        LightRagConfigError: missing env vars.
        LightRagIngestError: LightRAG insert failed.
    """
    if not text.strip():
        logger.debug("lightrag_adapter.ingest_text: skipping empty text for pack %s", pack_id)
        return

    async def _run() -> None:
        rag = _build_rag(pack_id)
        await rag.initialize_storages()
        try:
            await rag.ainsert(text)
        finally:
            await rag.finalize_storages()

    try:
        asyncio.run(_run())
    except RuntimeError as exc:
        # Already inside an event loop (e.g. FastAPI BackgroundTasks in test env)
        if "cannot run nested event loop" in str(exc).lower() or \
                "no running event loop" in str(exc).lower():
            # Fallback: create a new thread-based event loop
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, _run())
                future.result()
        else:
            raise LightRagIngestError(
                f"LightRAG ingest failed for pack {pack_id}: {exc}"
            ) from exc
    except (LightRagConfigError, ImportError):
        raise
    except Exception as exc:
        raise LightRagIngestError(
            f"LightRAG ingest failed for pack {pack_id}: {type(exc).__name__}: {exc}"
        ) from exc


def query(pack_id: str, question: str, *, mode: Optional[str] = None) -> str:
    """Query the LightRAG index for `pack_id` and return the answer string.

    Args:
        pack_id:  The UUID-hex identifier of the KnowledgePack.
        question: The natural-language query.
        mode:     Query mode override: naive | local | global | hybrid.
                  Defaults to LIGHTRAG_QUERY_MODE env var or "hybrid".

    Returns:
        The answer text from LightRAG (may include citations/reasoning).

    Raises:
        ImportError:       lightrag-hku not installed.
        LightRagConfigError: missing env vars.
        LightRagQueryError:  query execution failed.
    """
    effective_mode = mode or os.environ.get("LIGHTRAG_QUERY_MODE", "hybrid")

    async def _run() -> str:
        from lightrag import QueryParam  # type: ignore[import]
        rag = _build_rag(pack_id)
        await rag.initialize_storages()
        try:
            result = await rag.aquery(
                question,
                param=QueryParam(mode=effective_mode),
            )
            return result if isinstance(result, str) else str(result)
        finally:
            await rag.finalize_storages()

    try:
        return asyncio.run(_run())
    except RuntimeError as exc:
        if "cannot run nested event loop" in str(exc).lower() or \
                "no running event loop" in str(exc).lower():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, _run())
                return future.result()
        raise LightRagQueryError(
            f"LightRAG query failed for pack {pack_id}: {exc}"
        ) from exc
    except (LightRagConfigError, ImportError):
        raise
    except Exception as exc:
        raise LightRagQueryError(
            f"LightRAG query failed for pack {pack_id}: {type(exc).__name__}: {exc}"
        ) from exc


def _finalize_hits(hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize hit scores in-place.

    LightRAG's merged chunk list is relevance-ordered but (in most versions)
    carries no similarity score, so when a chunk lacks a numeric score we
    derive one from its retrieval rank: first hit = 1.0, last = 1/n.  The
    value is a rank indicator, not a raw cosine similarity.
    """
    n = len(hits)
    for i, hit in enumerate(hits):
        score = hit.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            hit["score"] = round((n - i) / n, 3) if n else 0.0
        else:
            hit["score"] = round(float(score), 3)
    return hits


def _hits_from_structured(result: Any, top_k: int) -> List[Dict[str, Any]]:
    """Map a LightRAG ``aquery_data()`` result to ``[{text, source, score}]``."""
    if not isinstance(result, dict):
        return []
    data = result.get("data") or {}
    chunks = data.get("chunks") or []
    refs: Dict[str, str] = {}
    for ref in data.get("references") or []:
        if isinstance(ref, dict):
            rid = str(ref.get("reference_id") or "")
            if rid:
                refs[rid] = str(ref.get("file_path") or "")
    hits: List[Dict[str, Any]] = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        text = str(chunk.get("content") or "").strip()
        if not text:
            continue
        source = str(
            chunk.get("file_path")
            or refs.get(str(chunk.get("reference_id") or ""), "")
            or ""
        ).strip()
        hits.append(
            {
                "text": text,
                "source": source,
                "score": chunk.get("score") or chunk.get("rerank_score"),
            }
        )
        if len(hits) >= top_k:
            break
    return _finalize_hits(hits)


def _hits_from_context(context: Any, top_k: int) -> List[Dict[str, Any]]:
    """Best-effort parse of an ``aquery(only_need_context=True)`` string.

    LightRAG renders text chunks as one JSON object per line inside the
    "Document Chunks" section; keep every line that decodes to a dict with a
    non-empty ``content`` field.  Returns [] when nothing parses — the caller
    then falls back to the synthesized answer.
    """
    if not isinstance(context, str):
        return []
    import json

    hits: List[Dict[str, Any]] = []
    seen = set()
    for line in context.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        text = str(obj.get("content") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        hits.append(
            {
                "text": text,
                "source": str(obj.get("file_path") or "").strip(),
                "score": obj.get("score") or obj.get("rerank_score"),
            }
        )
        if len(hits) >= top_k:
            break
    return _finalize_hits(hits)


def query_hits(pack_id: str, question: str, *, mode: Optional[str] = None, top_k: int = 5) -> List[Dict[str, Any]]:
    """Query the LightRAG index and return raw retrieval chunks as hits.

    Unlike :func:`query` (which returns one LLM-synthesized answer), this
    returns the actual retrieved fragments as a list of
    ``{"text", "source", "score"}`` dicts ordered by relevance — suitable for
    multi-citation UIs.  ``query()`` is left untouched.

    Strategy:
      1. ``rag.aquery_data()`` (lightrag ≥ 1.4) — structured chunks with
         ``content`` / ``file_path`` / ``reference_id``, no LLM call.
      2. Older versions: ``aquery(only_need_context=True)`` and parse the
         JSON chunk lines out of the context string.

    An empty list means "no matches" (callers should NOT fall back).  When
    structured retrieval is unavailable or yields nothing parseable, raises
    :class:`LightRagQueryError` so callers can fall back to :func:`query`.

    Raises:
        ImportError:         lightrag-hku not installed.
        LightRagConfigError: missing env vars.
        LightRagQueryError:  structured retrieval unsupported/failed.
    """
    effective_mode = mode or os.environ.get("LIGHTRAG_QUERY_MODE", "hybrid")

    async def _run() -> List[Dict[str, Any]]:
        rag = _build_rag(pack_id)  # raises ImportError / LightRagConfigError first
        await rag.initialize_storages()
        try:
            from lightrag import QueryParam  # type: ignore[import]

            if hasattr(rag, "aquery_data"):
                param = QueryParam(mode=effective_mode, top_k=top_k, chunk_top_k=top_k)
                result = await rag.aquery_data(question, param=param)
                return _hits_from_structured(result, top_k)
            # Legacy lightrag: only_need_context returns the raw context string.
            param = QueryParam(mode="naive", top_k=top_k, only_need_context=True)
            context = await rag.aquery(question, param=param)
            hits = _hits_from_context(context, top_k)
            if not hits:
                raise LightRagQueryError(
                    f"LightRAG query_hits: no parseable chunks for pack {pack_id}"
                )
            return hits
        finally:
            await rag.finalize_storages()

    try:
        return asyncio.run(_run())
    except RuntimeError as exc:
        if "cannot run nested event loop" in str(exc).lower() or \
                "no running event loop" in str(exc).lower():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, _run())
                return future.result()
        raise LightRagQueryError(
            f"LightRAG query_hits failed for pack {pack_id}: {exc}"
        ) from exc
    except (LightRagConfigError, LightRagQueryError, ImportError):
        raise
    except Exception as exc:
        raise LightRagQueryError(
            f"LightRAG query_hits failed for pack {pack_id}: {type(exc).__name__}: {exc}"
        ) from exc


def delete_pack_index(pack_id: str) -> None:
    """Remove the LightRAG working directory for `pack_id`.

    Called when a KnowledgePack is deleted so no stale index data lingers.
    Safe to call even when LightRAG is disabled (no-op if dir doesn't exist).
    """
    import shutil
    base = Path(os.environ.get("LIGHTRAG_WORKING_DIR", ".xueban/lightrag"))
    target = base / pack_id
    if target.exists():
        try:
            shutil.rmtree(target)
            logger.debug("lightrag_adapter: removed index dir %s", target)
        except OSError as exc:
            logger.warning("lightrag_adapter: failed to remove index dir %s: %s", target, exc)


def backend_label() -> str:
    """Return a short string identifying the active RAG backend.

    Used by the API to expose ``meta.rag_backend`` so the UI can display
    whether the pack is backed by ``stub`` or ``lightrag``.
    """
    return "lightrag" if is_enabled() else "stub"


__all__ = [
    "is_enabled",
    "ingest_text",
    "query",
    "query_hits",
    "delete_pack_index",
    "backend_label",
    "LightRagConfigError",
    "LightRagIngestError",
    "LightRagQueryError",
]
