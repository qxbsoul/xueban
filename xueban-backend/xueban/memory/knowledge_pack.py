"""KnowledgePack + KnowledgeSource — Story 9.1 AC1.

First-class object owned by the Builder: bundles file/url/text/dataset sources,
retrieval policy and freshness rules into a reusable knowledge artifact that
can later be bound to an AgentBlueprint via `pack_id`.

Pure Pydantic v2; intentionally decoupled from `xueban/runtime/contracts.py`
so the runtime 7+1 freeze is not affected.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from xueban.memory.retrieval_profiles import RetrievalProfile


SourceType = Literal["file", "url", "text", "dataset"]
IngestStatus = Literal["pending", "processing", "done", "failed"]
PackStatus = Literal["pending", "indexing", "ready", "failed"]
FreshnessPolicy = Literal["always", "daily", "weekly", "on_demand"]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid4().hex


class KnowledgeSource(BaseModel):
    """Single source attached to a KnowledgePack."""

    # H4: persisted model uses extra="ignore" so a 9.2 / Phase 2 schema upgrade
    # (e.g. new `vector_store_url`) does not cause already-persisted packs to
    # vanish on rollback. Request DTOs in api/knowledge.py keep extra="forbid".
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    source_id: str = Field(default_factory=_new_id)
    source_type: SourceType
    source_ref: str = Field(min_length=1, description="file path / URL / inline ref")
    mime_type: str = ""
    imported_at: datetime = Field(default_factory=_now_utc)
    checksum: str = ""  # SHA-256 hex; populated after parse
    ingest_status: IngestStatus = "pending"
    chunk_count: int = 0
    error_message: str = ""


class KnowledgePack(BaseModel):
    """Reusable knowledge bundle bound by `pack_id`.

    AC1 fields covered: pack_id / name / description / sources[] / retrieval_profile /
    citation_required / freshness_policy / created_at / updated_at / status.

    AC1 validators:
      - sources non-empty
      - citation_required => at least one source present (covered by sources non-empty)
      - retrieval_profile.top_k inside its declared range (delegated to RetrievalProfile)
    """

    # H4: persisted model uses extra="ignore" for forward compat (see KnowledgeSource).
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    pack_id: str = Field(default_factory=_new_id)
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    sources: List[KnowledgeSource] = Field(default_factory=list)
    retrieval_profile: RetrievalProfile = Field(default_factory=RetrievalProfile)
    citation_required: bool = False
    freshness_policy: FreshnessPolicy = "on_demand"
    created_at: datetime = Field(default_factory=_now_utc)
    updated_at: datetime = Field(default_factory=_now_utc)
    status: PackStatus = "pending"

    @model_validator(mode="after")
    def _validate_pack(self) -> "KnowledgePack":
        if not self.sources:
            raise ValueError("KnowledgePack.sources must contain at least one source")

        # Defensive: top_k should be in the legal range. Field constraints already
        # enforce this at the schema level, but we re-check in case the profile is
        # constructed by hand without going through Pydantic validation.
        if not (1 <= self.retrieval_profile.top_k <= 20):
            raise ValueError(
                f"retrieval_profile.top_k out of range: {self.retrieval_profile.top_k}"
            )
        return self


def update_pack(pack: KnowledgePack, **changes: object) -> KnowledgePack:
    """Return a re-validated copy of `pack` with the given fields updated.

    `updated_at` is refreshed automatically. Re-validation runs all field +
    `model_validator` checks so domain invariants stay enforced.
    """
    payload = dict(changes)
    payload.setdefault("updated_at", _now_utc())
    merged = {**pack.model_dump(mode="python"), **payload}
    return KnowledgePack.model_validate(merged)


__all__ = [
    "KnowledgeSource",
    "KnowledgePack",
    "SourceType",
    "IngestStatus",
    "PackStatus",
    "FreshnessPolicy",
    "update_pack",
]
