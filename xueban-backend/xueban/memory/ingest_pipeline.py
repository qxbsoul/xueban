"""KnowledgePack ingest pipeline — Story 9.1 AC3.

Stages: parse → chunk → embed/index → status update.

MVP characteristics:
  - parse supports `.txt` / `.md` directly; `.pdf` uses pdfplumber when available
    and falls back to a clear PARSE_FAILED error otherwise. URL fetch uses httpx
    + html.parser (stdlib) so we don't pull in BeautifulSoup as a hard dep.
  - chunk is plain character-window with overlap.
  - index is keyword-based (token frequency); the `BaseIndex` ABC is the
    extension point that Phase 2 will swap for a real vector store.
  - Persistence: `.xueban/knowledge/{pack_id}/index.json` next to the
    existing FileCheckpointStore convention so unified cleanup is one rm.
  - Stage failures mark the source `failed` + `error_message` but do not
    crash the pack ingest — siblings keep ingesting.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import tempfile
import threading
from abc import ABC, abstractmethod
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from xueban.memory.knowledge_pack import (
    KnowledgePack,
    KnowledgeSource,
    PackStatus,
    update_pack,
)
from xueban.runtime.errors import XuebanError


logger = logging.getLogger(__name__)


_KNOWLEDGE_ROOT = Path(".xueban/knowledge")
_PARSE_TIMEOUT_S = 15.0
_MAX_REMOTE_BYTES = 8 * 1024 * 1024  # 8 MB cap on remote fetch (S layer guard)
_STREAM_CHUNK_BYTES = 64 * 1024


# C1: file source sandbox. `parse_source(file)` only accepts paths inside this
# directory (or any extra dirs the operator opts into via env). Symlinks that
# escape the sandbox are rejected. Default is `<cwd>/.xueban/uploads`, which
# matches the convention of `.xueban/knowledge/` for unified cleanup.
_DEFAULT_FILE_SANDBOX = Path(".xueban/uploads")


def _allowed_file_roots() -> List[Path]:
    """Return the absolute, resolved sandbox roots that `file://` sources may live in.

    Override / extend via `XUEBAN_KNOWLEDGE_FILE_ROOTS` (os.pathsep separated).
    """
    extras_env = os.environ.get("XUEBAN_KNOWLEDGE_FILE_ROOTS", "")
    extras = [Path(p) for p in extras_env.split(os.pathsep) if p.strip()]
    roots: List[Path] = []
    for raw in [_DEFAULT_FILE_SANDBOX, *extras]:
        try:
            resolved = raw.expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        roots.append(resolved)
    return roots


def _is_path_inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


# Per-pack lock for serializing read-modify-write on pack.json + index.json (C3).
# Lives in-process — single-worker FastAPI deployment is the current contract;
# multi-worker callers must pin a worker per pack via reverse proxy hash.
_pack_locks: Dict[str, threading.RLock] = {}
_pack_locks_guard = threading.Lock()


def pack_lock(pack_id: str) -> threading.RLock:
    """Return the per-pack lock used by ingest + service writers."""
    with _pack_locks_guard:
        lock = _pack_locks.get(pack_id)
        if lock is None:
            lock = threading.RLock()
            _pack_locks[pack_id] = lock
        return lock


def atomic_write_text(target: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Write `content` to `target` atomically via tempfile + os.replace.

    On Windows os.replace is atomic *within the same volume*; we keep the temp
    file in `target.parent` to satisfy that guarantee. Callers must hold the
    per-pack lock if they need read-after-write consistency.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fp:
            fp.write(content)
        os.replace(tmp_path, target)
    except Exception:
        # Best-effort cleanup; os.replace already moved the file on success.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# C2: SSRF guard for `url` source.
_ALLOWED_URL_SCHEMES = {"http", "https"}

# RFC 6598 CGNAT shared address space. `ipaddress.ip_address().is_private`
# returns False for this range in Python ≤3.10, so we block it explicitly.
_CGNAT_RANGE = ipaddress.ip_network("100.64.0.0/10")


def _resolve_host_ip(host: str) -> ipaddress._BaseAddress:
    """Resolve `host` to an IP address. Raises IngestError on DNS failure."""
    try:
        # Use the first AF_UNSPEC result; we re-check below regardless of family.
        info = socket.getaddrinfo(host, None)
        if not info:
            raise IngestError(
                "INGEST_PARSE_FAILED",
                f"DNS resolution returned no records for {host!r}",
            )
        ip_str = info[0][4][0]
        return ipaddress.ip_address(ip_str)
    except (socket.gaierror, ValueError) as exc:
        raise IngestError(
            "INGEST_PARSE_FAILED",
            f"DNS resolution failed for {host!r}: {exc}",
        ) from exc


def _is_disallowed_ip(ip: ipaddress._BaseAddress) -> bool:
    """Return True if `ip` must not be fetched (SSRF guard, C2)."""
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return True
    # RFC 6598 CGNAT (100.64.0.0/10) — not covered by is_private in Python ≤3.10.
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_RANGE:
        return True
    return False


def _enforce_url_safety(url: str) -> None:
    """Reject schemes/hosts that could be used for SSRF.

    Blocks: non-http(s) schemes, missing host, hostnames that resolve to
    loopback / private / link-local / multicast / reserved / CGNAT IPs (per
    RFC 1918, RFC 3927, RFC 6598, IANA reserved). Does NOT mitigate DNS
    rebinding by itself — callers must also pin the resolved IP through the
    request lifetime; the httpx fetcher below uses a single-shot resolve
    immediately before the request to keep the window small.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_URL_SCHEMES:
        raise IngestError(
            "INGEST_PARSE_FAILED",
            f"URL scheme {parsed.scheme!r} not allowed (http/https only)",
        )
    host = parsed.hostname
    if not host:
        raise IngestError(
            "INGEST_PARSE_FAILED",
            "URL is missing a host component",
        )
    ip = _resolve_host_ip(host)
    if _is_disallowed_ip(ip):
        raise IngestError(
            "INGEST_PARSE_FAILED",
            f"URL host {host!r} resolved to disallowed address {ip}",
        )


class IngestError(XuebanError):
    """Raised when an individual ingest stage fails for a source.

    `code` carries the stage:
      INGEST_PARSE_FAILED / INGEST_CHUNK_FAILED / INGEST_INDEX_FAILED
    """

    code = "INGEST_FAILED"

    def __init__(self, code: str, message: str, source_id: str = "") -> None:
        super().__init__(message, details={"source_id": source_id})
        self.code = code


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


class _HtmlTextExtractor(HTMLParser):
    """Minimal stdlib HTML→text stripper."""

    _SKIP_TAGS = {"script", "style", "noscript", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._buf: List[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:  # noqa: ARG002
        if tag.lower() in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._buf.append(data)

    def get_text(self) -> str:
        text = "".join(self._buf)
        # Collapse runs of whitespace to single space
        return re.sub(r"\s+", " ", text).strip()


def _read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _read_pdf_file(path: Path) -> str:
    try:
        import pdfplumber  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional dependency guard
        raise IngestError(
            "INGEST_PARSE_FAILED",
            "pdfplumber is required for .pdf parsing; install pdfplumber to ingest PDF files",
        ) from exc

    pieces: List[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            pieces.append(txt)
    return "\n\n".join(pieces).strip()


def _strip_html(html: str) -> str:
    parser = _HtmlTextExtractor()
    parser.feed(html)
    parser.close()
    return parser.get_text()


def _resolve_sandboxed_file(ref: str, source_id: str) -> Path:
    """Resolve `ref` and reject anything outside the configured sandbox roots (C1).

    - Symlinks are resolved BEFORE comparison so a symlink inside the sandbox
      pointing to /etc/passwd is rejected.
    - Relative refs are anchored to the *first* allowed root to give callers a
      single predictable upload directory.
    """
    roots = _allowed_file_roots()
    if not roots:
        raise IngestError(
            "INGEST_PARSE_FAILED",
            "No valid file sandbox roots are configured. "
            "Set XUEBAN_KNOWLEDGE_FILE_ROOTS to configure allowed upload directories.",
            source_id=source_id,
        )
    raw = Path(ref).expanduser()
    if not raw.is_absolute():
        # Relative paths anchor to the first sandbox root for predictability.
        raw = roots[0] / raw
    try:
        resolved = raw.resolve()
    except (OSError, RuntimeError) as exc:
        raise IngestError(
            "INGEST_PARSE_FAILED",
            f"Could not resolve file path {ref!r}: {exc}",
            source_id=source_id,
        ) from exc
    if not any(_is_path_inside(resolved, root) for root in roots):
        allowed = ", ".join(str(r) for r in roots)
        raise IngestError(
            "INGEST_PARSE_FAILED",
            f"File path {ref!r} is outside the allowed sandbox ({allowed}). "
            f"Set XUEBAN_KNOWLEDGE_FILE_ROOTS to add roots.",
            source_id=source_id,
        )
    return resolved


def _fetch_url_streaming(url: str, source_id: str) -> Tuple[bytes, str]:
    """Fetch `url` in streaming mode, capping body at `_MAX_REMOTE_BYTES` (H1).

    Returns `(body_bytes, content_type_header)`. Raises IngestError on
    transport / size / SSRF errors.
    """
    _enforce_url_safety(url)
    buf = bytearray()
    ctype = ""
    try:
        with httpx.Client(timeout=_PARSE_TIMEOUT_S, follow_redirects=True) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                # Re-check the *final* URL after redirects in case it landed on a
                # private host (DNS rebinding / open redirect).
                final = str(resp.url)
                if final != url:
                    _enforce_url_safety(final)
                ctype = resp.headers.get("content-type", "")
                for chunk in resp.iter_bytes(_STREAM_CHUNK_BYTES):
                    if not chunk:
                        continue
                    remaining = _MAX_REMOTE_BYTES - len(buf)
                    if remaining <= 0:
                        break
                    buf.extend(chunk[:remaining])
                    if len(buf) >= _MAX_REMOTE_BYTES:
                        break
    except httpx.HTTPError as exc:
        raise IngestError(
            "INGEST_PARSE_FAILED",
            f"HTTP fetch failed for {url!r}: {type(exc).__name__}: {exc}",
            source_id=source_id,
        ) from exc
    return bytes(buf), ctype


def parse_source(source: KnowledgeSource) -> str:
    """Convert a source into plain text, raising IngestError on failure."""
    try:
        if source.source_type == "text":
            return source.source_ref

        if source.source_type == "file":
            path = _resolve_sandboxed_file(source.source_ref, source.source_id)
            if not path.exists():
                raise IngestError(
                    "INGEST_PARSE_FAILED",
                    f"File not found: {source.source_ref}",
                    source_id=source.source_id,
                )
            suffix = path.suffix.lower()
            if suffix in {".txt", ".md", ".markdown"}:
                return _read_text_file(path)
            if suffix == ".pdf":
                return _read_pdf_file(path)
            raise IngestError(
                "INGEST_PARSE_FAILED",
                f"Unsupported file type: {suffix or '<no extension>'}",
                source_id=source.source_id,
            )

        if source.source_type == "url":
            content, ctype = _fetch_url_streaming(source.source_ref, source.source_id)
            text = content.decode("utf-8", errors="replace")
            if "html" in ctype.lower() or text.lstrip().startswith("<"):
                return _strip_html(text)
            return text

        if source.source_type == "dataset":
            # L1: `dataset` is a declared SourceType but has no resolver yet.
            # Returning ref verbatim was silently misleading (status flipped to
            # ready over a literal string). Reject explicitly until 9.x lands a
            # dataset registry.
            raise IngestError(
                "INGEST_PARSE_FAILED",
                "dataset source_type is not supported until a dataset registry lands. "
                "Use file/url/text for now.",
                source_id=source.source_id,
            )

        raise IngestError(
            "INGEST_PARSE_FAILED",
            f"Unknown source_type: {source.source_type}",
            source_id=source.source_id,
        )
    except IngestError:
        raise
    except Exception as exc:  # pragma: no cover - last-resort guard
        raise IngestError(
            "INGEST_PARSE_FAILED",
            f"Parse failed: {type(exc).__name__}: {exc}",
            source_id=source.source_id,
        ) from exc


# ---------------------------------------------------------------------------
# Chunk
# ---------------------------------------------------------------------------


def chunk_text(text: str, chunk_size: int, overlap: int) -> List[str]:
    """Split `text` into overlapping windows of `chunk_size` chars.

    `overlap` is clamped to `< chunk_size` to avoid an infinite loop.
    Empty input returns an empty list.
    """
    if chunk_size <= 0:
        raise IngestError("INGEST_CHUNK_FAILED", "chunk_size must be positive")
    if not text:
        return []
    overlap = max(0, min(overlap, chunk_size - 1))
    step = chunk_size - overlap
    chunks: List[str] = []
    n = len(text)
    i = 0
    while i < n:
        chunks.append(text[i : i + chunk_size])
        i += step
    return chunks


# ---------------------------------------------------------------------------
# Index (BaseIndex ABC + keyword MVP impl)
# ---------------------------------------------------------------------------


class BaseIndex(ABC):
    """Replaceable index backend. Phase 2 vector-store impls subclass this."""

    @abstractmethod
    def add(self, pack_id: str, source_id: str, chunks: List[str]) -> None: ...

    @abstractmethod
    def persist(self, pack_id: str) -> Path: ...


_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+", re.UNICODE)


def _tokenize(text: str) -> List[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


class KeywordIndex(BaseIndex):
    """In-memory keyword frequency index, persisted as JSON.

    Schema (intentionally simple — Phase 2 replaces it):
      {
        "pack_id": "...",
        "documents": [
          {"source_id": "...", "chunk_index": 0, "text": "...", "tokens": {"foo": 2}}
        ]
      }
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        self._root = root or _KNOWLEDGE_ROOT
        self._docs: Dict[str, List[Dict[str, object]]] = {}

    def add(self, pack_id: str, source_id: str, chunks: List[str]) -> None:
        bucket = self._docs.setdefault(pack_id, [])
        for idx, chunk in enumerate(chunks):
            tokens = Counter(_tokenize(chunk))
            bucket.append(
                {
                    "source_id": source_id,
                    "chunk_index": idx,
                    "text": chunk,
                    "tokens": dict(tokens),
                }
            )

    def persist(self, pack_id: str) -> Path:
        target_dir = self._root / pack_id
        target = target_dir / "index.json"
        payload = {"pack_id": pack_id, "documents": self._docs.get(pack_id, [])}
        # C3: hold the per-pack lock + write atomically so concurrent ingests
        # don't truncate each other's index.json mid-flight.
        with pack_lock(pack_id):
            atomic_write_text(
                target,
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            )
        return target


def index_chunks(
    pack_id: str,
    source_id: str,
    chunks: List[str],
    index: Optional[BaseIndex] = None,
) -> Path:
    """Add chunks to `index` (creating one if not provided) and persist.

    Returns the path of the persisted index file. Raises IngestError on failure.
    """
    try:
        idx = index or KeywordIndex()
        idx.add(pack_id, source_id, chunks)
        return idx.persist(pack_id)
    except Exception as exc:
        raise IngestError(
            "INGEST_INDEX_FAILED",
            f"Index persist failed: {type(exc).__name__}: {exc}",
            source_id=source_id,
        ) from exc


# ---------------------------------------------------------------------------
# Run ingest
# ---------------------------------------------------------------------------


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _stage_source(
    pack_id: str,
    source: KnowledgeSource,
    chunk_size: int,
    overlap: int,
    index: BaseIndex,
) -> Tuple[KnowledgeSource, Optional[IngestError]]:
    """Run parse → chunk → index for one source. Returns updated source + error."""
    # parse
    try:
        text = parse_source(source)
    except IngestError as exc:
        return (
            source.model_copy(
                update={
                    "ingest_status": "failed",
                    "error_message": exc.message,
                }
            ),
            exc,
        )

    # chunk
    try:
        chunks = chunk_text(text, chunk_size=chunk_size, overlap=overlap)
    except IngestError as exc:
        return (
            source.model_copy(
                update={
                    "ingest_status": "failed",
                    "error_message": exc.message,
                    "checksum": _checksum(text),
                }
            ),
            exc,
        )

    # index
    try:
        index_chunks(pack_id, source.source_id, chunks, index=index)
    except IngestError as exc:
        return (
            source.model_copy(
                update={
                    "ingest_status": "failed",
                    "error_message": exc.message,
                    "checksum": _checksum(text),
                    "chunk_count": len(chunks),
                }
            ),
            exc,
        )

    return (
        source.model_copy(
            update={
                "ingest_status": "done",
                "error_message": "",
                "checksum": _checksum(text),
                "chunk_count": len(chunks),
            }
        ),
        None,
    )


def run_ingest(
    pack: KnowledgePack,
    *,
    index: Optional[BaseIndex] = None,
) -> KnowledgePack:
    """Synchronously execute the ingest pipeline against `pack` and return the updated copy.

    Status semantics:
      - During: pack.status = 'indexing' (caller is expected to have flipped this
        already; we re-emit it here as a defensive write).
      - After: 'ready' if every source ended `done`, else 'failed' (any source failed).

    LightRAG integration (optional):
      When ``LIGHTRAG_ENABLED=true`` the parsed text for each successful source is
      also fed into LightRAG's graph+vector index (stored under
      ``.xueban/lightrag/{pack_id}/``).  Failures in LightRAG are logged as
      warnings but do NOT flip the source to ``failed`` — the keyword index is
      always the authoritative fallback so existing behaviour is preserved.

    PackGraph integration (always on):
      After all sources are staged, a deterministic knowledge graph
      (``xueban.memory.pack_graph``) is rebuilt from every parseable source
      and persisted to ``graph.json`` next to ``index.json``.  No LLM is
      involved; failures are logged as warnings and never affect ingest status.
    """
    # Lazy import to keep the dependency optional; guarded by is_enabled() check
    # below, but we import the module unconditionally so misconfiguration
    # (missing env var, wrong value) surfaces early with a clear error.
    from xueban.runtime import lightrag_adapter  # noqa: PLC0415 — intentional lazy

    idx = index or KeywordIndex()
    lightrag_active = lightrag_adapter.is_enabled()

    new_sources: List[KnowledgeSource] = []
    any_failed = False
    for source in pack.sources:
        # Mark in-progress → done/failed
        in_progress = source.model_copy(update={"ingest_status": "processing"})
        finished, err = _stage_source(
            pack.pack_id,
            in_progress,
            chunk_size=pack.retrieval_profile.chunk_size,
            overlap=pack.retrieval_profile.overlap,
            index=idx,
        )
        if err is not None:
            any_failed = True
        else:
            # Source parsed + keyword-indexed successfully.
            # Optionally feed into LightRAG as well.
            if lightrag_active:
                try:
                    # Re-parse to get the plain text; cheap for text/md sources.
                    text = parse_source(in_progress)
                    lightrag_adapter.ingest_text(pack.pack_id, text)
                except Exception as lr_exc:  # noqa: BLE001 — non-fatal, log + continue
                    logger.warning(
                        "LightRAG ingest warning for pack %s source %s: %s — "
                        "keyword index is still intact",
                        pack.pack_id,
                        source.source_id,
                        lr_exc,
                    )
        new_sources.append(finished)

    final_status: PackStatus = "failed" if any_failed else "ready"
    final_pack = update_pack(pack, sources=[s.model_dump() for s in new_sources], status=final_status)

    # PackGraph (deterministic knowledge layer): rebuild from every parseable
    # source. Same non-fatal contract as the LightRAG hook above — graph build
    # failures log a warning and never flip ingest status. 内容指纹未变
    # （Shadow graph_build_state 式设计）时跳过重建。
    if any(s.ingest_status == "done" for s in final_pack.sources):
        try:
            from xueban.memory import pack_graph  # noqa: PLC0415 — avoids import cycle

            if not pack_graph.is_graph_fresh(final_pack):
                pack_graph.build_graph(final_pack)
        except Exception as exc:  # noqa: BLE001 — non-fatal by design
            logger.warning(
                "PackGraph build failed for pack %s: %s — keyword index is still intact",
                pack.pack_id,
                exc,
            )
    return final_pack


async def run_ingest_async(
    pack: KnowledgePack,
    *,
    index: Optional[BaseIndex] = None,
) -> KnowledgePack:
    """Async wrapper that offloads the (sync, IO-bound) ingest to a thread.

    Used by the FastAPI BackgroundTasks hook so the request handler returns
    immediately with `pending` while the pipeline runs in the background.
    """
    return await asyncio.to_thread(run_ingest, pack, index=index)


__all__ = [
    "IngestError",
    "BaseIndex",
    "KeywordIndex",
    "parse_source",
    "chunk_text",
    "index_chunks",
    "run_ingest",
    "run_ingest_async",
    "pack_lock",
    "atomic_write_text",
]
