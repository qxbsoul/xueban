"""Hierarchical knowledge index — StaffDeck-inspired doc → chapter → page tree.

Upgrades flat multi-chunk retrieval to "locate first, then retrieve":

  - ``build_index`` parses each source of a KnowledgePack and builds a document
    tree. Markdown heading outlines become nested chapter nodes; unstructured
    documents degrade to fixed-size page nodes (按 chunk 序号分页). The tree plus
    the chunks (with char offsets) is persisted to
    ``.xueban/knowledge/{pack_id}/hier_index.json`` next to pack.json.
  - ``query`` first scores tree nodes (document/chapter/page) against the query
    to locate the top candidate subtrees, then ranks only the chunks inside
    those subtrees. Each hit carries its hierarchical provenance (``path`` =
    "doc > chapter") and the response includes a ``trace`` describing which
    nodes were located — suitable for a retrieval-debug UI.
  - Old packs without a persisted tree are built on the spot (``ensure_index``);
    if parsing fails entirely the caller falls back to the existing flat
    retrieval path.

Storage honours the same conventions as ingest_pipeline.py: per-pack lock +
atomic_write_text, so concurrent ingests/queries don't corrupt the file.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from xueban.memory.ingest_pipeline import (
    atomic_write_text,
    pack_lock,
    parse_source,
)
from xueban.memory.knowledge_pack import KnowledgePack, KnowledgeSource


logger = logging.getLogger(__name__)


_KNOWLEDGE_ROOT = Path(".xueban/knowledge")
_INDEX_FILENAME = "hier_index.json"

# Unstructured documents degrade to page nodes of this many chunks each.
_PAGE_SIZE = 3
# Locate stage keeps at most this many top nodes; chunks are then drawn only
# from their subtrees.
_MAX_LOCATED = 3
# A token hit in a node title weighs more than one in body text.
_TITLE_WEIGHT = 3


# ---------------------------------------------------------------------------
# Tokenization (keyword-stub semantics + CJK bigrams so Chinese queries match)
# ---------------------------------------------------------------------------

_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
_CJK_RUN_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")


def _tokenize(text: str) -> List[str]:
    tokens = [t.lower() for t in _ASCII_TOKEN_RE.findall(text)]
    for run in _CJK_RUN_RE.findall(text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def _overlap(query: Counter, text: str) -> int:
    """Token-overlap score: sum of min(counts) over shared tokens."""
    if not query or not text:
        return 0
    return sum((query & Counter(_tokenize(text))).values())


# ---------------------------------------------------------------------------
# Chunking (mirrors ingest_pipeline.chunk_text, but keeps char offsets)
# ---------------------------------------------------------------------------


def _chunk_with_offsets(text: str, chunk_size: int, overlap: int) -> List[Dict[str, Any]]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if not text:
        return []
    overlap = max(0, min(overlap, chunk_size - 1))
    step = chunk_size - overlap
    chunks: List[Dict[str, Any]] = []
    n = len(text)
    i = 0
    while i < n:
        chunks.append(
            {
                "index": len(chunks),
                "start": i,
                "end": min(i + chunk_size, n),
                "text": text[i : i + chunk_size],
            }
        )
        i += step
    return chunks


# ---------------------------------------------------------------------------
# Tree building
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def _find_headings(text: str) -> List[Tuple[int, str, int]]:
    """Return [(level, title, char_offset)] for markdown ATX headings.

    Code-fence contents are not excluded — a heading-looking line inside a
    fence becomes a chapter too. Acceptable for the stub-grade index.
    """
    out: List[Tuple[int, str, int]] = []
    pos = 0
    for line in text.splitlines(keepends=True):
        m = _HEADING_RE.match(line.rstrip("\r\n"))
        if m:
            out.append((len(m.group(1)), m.group(2).strip(), pos))
        pos += len(line)
    return out


def _doc_title(source: KnowledgeSource, text: str) -> str:
    if source.source_type == "file":
        return Path(source.source_ref).name or source.source_ref
    if source.source_type == "text":
        for line in text.splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                return stripped[:60]
        return "(text)"
    return source.source_ref[:80]


def _chapters_from_headings(
    doc_id: str,
    headings: List[Tuple[int, str, int]],
    chunks: List[Dict[str, Any]],
    text_len: int,
) -> List[Dict[str, Any]]:
    """Build nested chapter nodes from a heading outline.

    A chapter spans [heading_offset, next heading of same-or-higher level);
    a chunk belongs to the chapter whose span contains the chunk's start.
    """
    nodes: List[Dict[str, Any]] = []
    for i, (level, title, start) in enumerate(headings):
        end = text_len
        for j in range(i + 1, len(headings)):
            if headings[j][0] <= level:
                end = headings[j][2]
                break
        idxs = [c["index"] for c in chunks if start <= c["start"] < end]
        nodes.append(
            {
                "node_id": f"{doc_id}/c{i}",
                "kind": "chapter",
                "title": title,
                "level": level,
                "chunk_indexes": idxs,
                "children": [],
            }
        )

    # Nest by heading level via a stack.
    roots: List[Dict[str, Any]] = []
    stack: List[Dict[str, Any]] = []
    for node in nodes:
        while stack and stack[-1]["level"] >= node["level"]:
            stack.pop()
        if stack:
            stack[-1]["children"].append(node)
        else:
            roots.append(node)
        stack.append(node)

    # Chunks before the first heading get a preamble node so every chunk is
    # reachable from the tree.
    preamble = [c["index"] for c in chunks if c["start"] < headings[0][2]]
    if preamble:
        roots.insert(
            0,
            {
                "node_id": f"{doc_id}/pre",
                "kind": "chapter",
                "title": "(前言)",
                "level": 0,
                "chunk_indexes": preamble,
                "children": [],
            },
        )
    return roots


def _pages_from_chunks(doc_id: str, chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Unstructured fallback: group chunks into fixed-size page nodes."""
    pages: List[Dict[str, Any]] = []
    for p, i in enumerate(range(0, len(chunks), _PAGE_SIZE)):
        idxs = [c["index"] for c in chunks[i : i + _PAGE_SIZE]]
        pages.append(
            {
                "node_id": f"{doc_id}/p{p}",
                "kind": "page",
                "title": f"第 {p + 1} 页 (chunks {idxs[0]}-{idxs[-1]})",
                "level": 1,
                "chunk_indexes": idxs,
                "children": [],
            }
        )
    return pages


def _build_document(
    doc_seq: int,
    source: KnowledgeSource,
    text: str,
    chunk_size: int,
    overlap: int,
) -> Dict[str, Any]:
    doc_id = f"d{doc_seq}"
    chunks = _chunk_with_offsets(text, chunk_size, overlap)
    headings = _find_headings(text)
    if headings:
        kind = "outline"
        children = _chapters_from_headings(doc_id, headings, chunks, len(text))
    else:
        kind = "paged"
        children = _pages_from_chunks(doc_id, chunks)
    node = {
        "node_id": doc_id,
        "kind": "document",
        "title": _doc_title(source, text),
        "level": 0,
        "chunk_indexes": [c["index"] for c in chunks],
        "children": children,
    }
    return {
        "doc_id": doc_id,
        "source_id": source.source_id,
        "title": node["title"],
        "kind": kind,
        "node": node,
        "chunks": chunks,
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _now() -> str:
    """ISO 8601 UTC with the canonical `Z` suffix (same convention as api/knowledge.py)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def index_path(pack_id: str, *, root: Optional[Path] = None) -> Path:
    return (root or _KNOWLEDGE_ROOT) / pack_id / _INDEX_FILENAME


def load_index(pack_id: str, *, root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Load the persisted tree; None when missing/corrupted/mismatched."""
    target = index_path(pack_id, root=root)
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("hier_index: corrupted index for pack %s: %s", pack_id, exc)
        return None
    if not isinstance(payload, dict) or payload.get("pack_id") != pack_id:
        return None
    return payload


def build_index(pack: KnowledgePack, *, root: Optional[Path] = None) -> Dict[str, Any]:
    """Parse all sources, build the document tree and persist it.

    Sources that fail to parse are skipped with a warning (same non-fatal
    semantics as the LightRAG hook in run_ingest); an all-failed pack yields
    an empty document list rather than raising.
    """
    documents: List[Dict[str, Any]] = []
    for seq, source in enumerate(pack.sources):
        try:
            text = parse_source(source)
        except Exception as exc:  # noqa: BLE001 — one bad source must not kill the tree
            logger.warning(
                "hier_index: skipping unparseable source %s of pack %s: %s",
                source.source_id,
                pack.pack_id,
                exc,
            )
            continue
        if not text.strip():
            continue
        documents.append(
            _build_document(
                len(documents),
                source,
                text,
                chunk_size=pack.retrieval_profile.chunk_size,
                overlap=pack.retrieval_profile.overlap,
            )
        )
    payload = {"pack_id": pack.pack_id, "built_at": _now(), "documents": documents}
    with pack_lock(pack.pack_id):
        atomic_write_text(
            index_path(pack.pack_id, root=root),
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
    return payload


def ensure_index(pack: KnowledgePack, *, root: Optional[Path] = None) -> Dict[str, Any]:
    """Load the persisted tree, building it on the spot for old packs."""
    return load_index(pack.pack_id, root=root) or build_index(pack, root=root)


# ---------------------------------------------------------------------------
# Tree walking / locating
# ---------------------------------------------------------------------------


def _walk_nodes(node: Dict[str, Any], path: str) -> Iterator[Tuple[Dict[str, Any], str]]:
    """Yield (node, "doc > ... > node" path) for node and its descendants."""
    yield node, path
    for child in node.get("children") or []:
        yield from _walk_nodes(child, f"{path} > {child['title']}")


def _chunk_path(node: Dict[str, Any], chunk_index: int, path: str) -> Tuple[str, str]:
    """Deepest "doc > chapter" path + node_id containing `chunk_index`."""
    for child in node.get("children") or []:
        if chunk_index in (child.get("chunk_indexes") or []):
            return _chunk_path(child, chunk_index, f"{path} > {child['title']}")
    return path, str(node.get("node_id") or "")


def query(
    pack: KnowledgePack,
    question: str,
    *,
    top_k: int = 5,
    root: Optional[Path] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Locate-then-retrieve over the pack's document tree.

    Returns (hits, trace). Each hit is ``{text, source, score, path, node_id}``
    where ``path`` is the hierarchical provenance ("doc > chapter"). ``trace``
    is ``{strategy: "hier", located: [{node_id, kind, title, path, score}],
    candidate_chunks: n}`` and describes the locating step for debugging UIs.

    An empty hit list means "no match inside the tree" — callers decide
    whether to fall back to flat retrieval.
    """
    index = ensure_index(pack, root=root)
    q_tokens = Counter(_tokenize(question))
    trace: Dict[str, Any] = {"strategy": "hier", "located": [], "candidate_chunks": 0}
    if not q_tokens:
        trace["reason"] = "empty-query-tokens"
        return [], trace

    # Stage 1 — locate: score every node (document/chapter/page); titles weigh
    # more than body text.
    located: List[Tuple[int, int, Dict[str, Any], Dict[str, Any], str]] = []
    chunk_scores: Dict[Tuple[int, int], int] = {}  # (doc_seq, chunk_index) -> score
    for doc_seq, doc in enumerate(index.get("documents") or []):
        scores: List[int] = []
        for c in doc.get("chunks") or []:
            s = _overlap(q_tokens, str(c.get("text") or ""))
            scores.append(s)
            chunk_scores[(doc_seq, int(c.get("index") or 0))] = s
        for node, path in _walk_nodes(doc["node"], str(doc["node"]["title"])):
            title_score = _overlap(q_tokens, str(node.get("title") or ""))
            body_score = sum(
                scores[i] for i in node.get("chunk_indexes") or [] if 0 <= i < len(scores)
            )
            score = title_score * _TITLE_WEIGHT + body_score
            if score > 0:
                located.append((score, doc_seq, doc, node, path))

    located.sort(key=lambda item: item[0], reverse=True)
    top = located[:_MAX_LOCATED]
    trace["located"] = [
        {
            "node_id": node["node_id"],
            "kind": node["kind"],
            "title": node["title"],
            "path": path,
            "score": score,
        }
        for score, _seq, _doc, node, path in top
    ]
    if not top:
        return [], trace

    # Stage 2 — retrieve: rank only chunks inside the located subtrees.
    candidates: List[Tuple[int, Dict[str, Any], Dict[str, Any]]] = []
    seen: set = set()
    for _score, doc_seq, doc, node, _path in top:
        for ci in node.get("chunk_indexes") or []:
            key = (doc_seq, ci)
            if key in seen:
                continue
            seen.add(key)
            chunk = next(
                (c for c in doc.get("chunks") or [] if c.get("index") == ci), None
            )
            if chunk is None:
                continue
            s = chunk_scores.get((doc_seq, ci), 0)
            if s > 0:
                candidates.append((s, doc, chunk))
    trace["candidate_chunks"] = len(seen)

    candidates.sort(key=lambda item: item[0], reverse=True)
    candidates = candidates[:top_k]
    max_score = candidates[0][0] if candidates else 0
    hits: List[Dict[str, Any]] = []
    for s, doc, chunk in candidates:
        path, node_id = _chunk_path(
            doc["node"], int(chunk["index"]), str(doc["node"]["title"])
        )
        hits.append(
            {
                "text": str(chunk["text"]),
                "source": str(doc.get("title") or ""),
                "score": round(s / max_score, 3) if max_score else 0.0,
                "path": path,
                "node_id": node_id,
            }
        )
    return hits, trace


# ---------------------------------------------------------------------------
# Tree browsing (GET /packs/{id}/index)
# ---------------------------------------------------------------------------


def _count_nodes(node: Dict[str, Any]) -> int:
    return 1 + sum(_count_nodes(c) for c in node.get("children") or [])


def tree_view(pack: KnowledgePack, *, root: Optional[Path] = None) -> Dict[str, Any]:
    """Navigable tree for the index endpoint — chunk texts stripped."""
    index = ensure_index(pack, root=root)
    documents: List[Dict[str, Any]] = []
    node_count = 0
    chunk_total = 0
    for doc in index.get("documents") or []:
        node_count += _count_nodes(doc["node"])
        chunk_total += len(doc.get("chunks") or [])
        documents.append(
            {
                "doc_id": doc["doc_id"],
                "source_id": doc["source_id"],
                "title": doc["title"],
                "kind": doc["kind"],
                "chunk_count": len(doc.get("chunks") or []),
                "tree": doc["node"],
            }
        )
    return {
        "pack_id": pack.pack_id,
        "built_at": index.get("built_at", ""),
        "documents": documents,
        "stats": {
            "documents": len(documents),
            "nodes": node_count,
            "chunks": chunk_total,
        },
    }


__all__ = [
    "index_path",
    "load_index",
    "build_index",
    "ensure_index",
    "query",
    "tree_view",
]
