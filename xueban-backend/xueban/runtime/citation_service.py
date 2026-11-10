"""CitationService — Story 9.2.

First-class citation provenance contract: every retrieval-grounded answer can be
traced back to a specific KnowledgePack source/chunk that contributed to it.

MVP characteristics:
  - Pure file-system persistence at `.xueban/citations/{run_id}.json`,
    matching the FileCheckpointStore root convention so cleanup is one rm.
  - `CitationTrace` is a Pydantic v2 model — independent of `runtime/contracts.py`
    so the runtime 7+1 freeze is not affected. Plumbing into the runtime is via
    `StepRecord.metadata['citation_summary']` only (additive).
  - `attach_trace` appends; persistence is atomic per-run via tmp-file rename.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from xueban.runtime.errors import XuebanError

logger = logging.getLogger(__name__)

# Resolved relative to cwd by default; override via XUEBAN_STORAGE_ROOT env var.
_STORAGE_ROOT = Path(os.environ.get("XUEBAN_STORAGE_ROOT", ".xueban"))
_CITATIONS_DIR = _STORAGE_ROOT / "citations"
_EXCERPT_LIMIT = 200

# Whitelist for run_id: alphanumeric, dash, underscore, 1-128 chars.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid4().hex


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CitationNotFound(XuebanError):
    """Requested run_id has no citation trace file."""

    code = "CITATION_NOT_FOUND"


# ---------------------------------------------------------------------------
# Data contract
# ---------------------------------------------------------------------------


class CitationTrace(BaseModel):
    """Single citation trace — one (pack, source, chunk) → (run, node) link.

    AC1 fields:
      trace_id / pack_id / source_id / chunk_id / excerpt / confidence /
      retrieved_at / task_or_artifact_ref.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    trace_id: str = Field(default_factory=_new_id)
    pack_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    excerpt: str = Field(default="", max_length=_EXCERPT_LIMIT)
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    retrieved_at: datetime = Field(default_factory=_now_utc)
    # `task_or_artifact_ref` is a free-form string per the AC; canonical encoding
    # is `run:{run_id}/node:{node_id}` or an artifact name. Callers pick whatever
    # is most informative.
    task_or_artifact_ref: str = Field(default="", max_length=400)


class CitationSummary(BaseModel):
    """Light summary attached to StepRecord.metadata['citation_summary']."""

    model_config = ConfigDict(extra="forbid")

    count: int = 0
    citation_missing: bool = False


class CitationReport(BaseModel):
    """Structured export for trajectory archive / BriefBoard / Report views."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    traces: List[CitationTrace] = Field(default_factory=list)
    citation_missing: bool = False


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class CitationService:
    """File-backed CRUD for CitationTrace.

    Per-run JSON file is the unit of persistence. Reads/writes are guarded by
    a process-wide lock so two concurrent appends from different runs/threads
    cannot interleave their writes.
    """

    def __init__(self, storage_dir: Optional[Path] = None) -> None:
        self._storage_dir = storage_dir or _CITATIONS_DIR
        self._lock = threading.Lock()

    # -- storage helpers -------------------------------------------------

    def _ensure_dir(self) -> None:
        self._storage_dir.mkdir(parents=True, exist_ok=True)

    def _record_path(self, run_id: str) -> Path:
        # Whitelist check prevents path traversal; reject any non-UUID-like run_id.
        if not run_id or not _RUN_ID_RE.fullmatch(run_id):
            raise CitationNotFound(
                f"Invalid run_id: {run_id!r}",
                details={"run_id": run_id},
            )
        return self._storage_dir / f"{run_id}.json"

    def _load_raw(self, run_id: str) -> List[Dict[str, Any]]:
        target = self._record_path(run_id)
        if not target.exists():
            return []
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.error("Corrupted citation file %s — returning empty list", target)
            return []
        traces = payload.get("traces") if isinstance(payload, dict) else None
        if not isinstance(traces, list):
            return []
        return traces

    def _persist(self, run_id: str, traces: List[Dict[str, Any]]) -> None:
        self._ensure_dir()
        target = self._record_path(run_id)
        tmp = target.with_suffix(".json.tmp")
        payload = {
            "run_id": run_id,
            "traces": traces,
            "updated_at": _now_utc().isoformat(),
        }
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, target)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    # -- public API ------------------------------------------------------

    def attach_trace(
        self,
        run_id: str,
        node_id: str,
        traces: List[CitationTrace],
    ) -> List[CitationTrace]:
        """Append `traces` (all bound to `node_id`) to the run's citation file.

        Returns the list of traces actually persisted (with `task_or_artifact_ref`
        filled in if the caller left it blank).
        """
        if not run_id:
            raise ValueError("attach_trace requires non-empty run_id")
        if not traces:
            return []

        canonical_ref = f"run:{run_id}/node:{node_id}" if node_id else f"run:{run_id}"
        normalised: List[CitationTrace] = []
        for t in traces:
            ref = t.task_or_artifact_ref or canonical_ref
            excerpt = (t.excerpt or "")[:_EXCERPT_LIMIT]
            normalised.append(
                t.model_copy(update={"task_or_artifact_ref": ref, "excerpt": excerpt})
            )

        with self._lock:
            existing = self._load_raw(run_id)
            existing.extend(t.model_dump(mode="json") for t in normalised)
            self._persist(run_id, existing)
        return normalised

    def get_traces(
        self,
        run_id: str,
        node_id: Optional[str] = None,
        *,
        require: bool = False,
    ) -> List[CitationTrace]:
        """Return traces for the run, optionally filtered to a single node.

        Args:
            require: If True, raise CitationNotFound when no trace file exists
                     (eliminates the TOCTOU window between has_traces + get_traces).
        """
        with self._lock:
            if require and not self._record_path(run_id).exists():
                raise CitationNotFound(
                    f"No citation traces for run_id={run_id!r}",
                    details={"run_id": run_id},
                )
            raw = self._load_raw(run_id)

        out: List[CitationTrace] = []
        for item in raw:
            try:
                trace = CitationTrace.model_validate(item)
            except ValueError:
                # Skip corrupted records; don't fail the whole query.
                continue
            if node_id and not _matches_node(trace, node_id):
                continue
            out.append(trace)
        return out

    def export_traces(self, run_id: str) -> Dict[str, Any]:
        """Return a dict suitable for embedding in trajectory exports."""
        traces = self.get_traces(run_id)
        report = CitationReport(
            run_id=run_id,
            traces=traces,
            citation_missing=len(traces) == 0,
        )
        return report.model_dump(mode="json")

    def summary_for_step(
        self,
        traces: List[CitationTrace],
        citation_required: bool,
    ) -> CitationSummary:
        """Compute the StepRecord.metadata['citation_summary'] payload."""
        count = len(traces)
        missing = bool(citation_required) and count == 0
        return CitationSummary(count=count, citation_missing=missing)

    def has_traces(self, run_id: str) -> bool:
        return self._record_path(run_id).exists()


def _matches_node(trace: CitationTrace, node_id: str) -> bool:
    """Match a trace to a node_id via the canonical task_or_artifact_ref encoding.

    Uses a word-boundary check so that node:1 does not accidentally match node:10.
    """
    ref = trace.task_or_artifact_ref or ""
    # Match "node:{id}" followed by "/" or end-of-string to avoid prefix collisions.
    return bool(re.search(re.escape(f"node:{node_id}") + r"(/|$)", ref))


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


_SERVICE_SINGLETON: Optional[CitationService] = None
_SINGLETON_LOCK = threading.Lock()


def get_service() -> CitationService:
    global _SERVICE_SINGLETON
    if _SERVICE_SINGLETON is None:
        with _SINGLETON_LOCK:
            if _SERVICE_SINGLETON is None:
                _SERVICE_SINGLETON = CitationService()
    return _SERVICE_SINGLETON


def set_service(svc: CitationService) -> None:
    global _SERVICE_SINGLETON
    _SERVICE_SINGLETON = svc


__all__ = [
    "CitationTrace",
    "CitationSummary",
    "CitationReport",
    "CitationService",
    "CitationNotFound",
    "get_service",
    "set_service",
]
