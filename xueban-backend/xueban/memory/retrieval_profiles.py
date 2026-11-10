"""RetrievalProfile — Story 9.1 AC2.

Independent Pydantic v2 model that describes how a KnowledgePack is retrieved
at runtime. Lives outside of the runtime-frozen 7+1 contracts so 9.2 / 9.3 can
import it via `pack_id` references without touching `xueban/runtime/contracts.py`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


RetrievalMode = Literal["semantic", "keyword", "hybrid"]


class RetrievalProfile(BaseModel):
    """Configuration that drives a KnowledgePack's retrieval at query time.

    Defaults match Story 9.1 AC2:
      - mode = "semantic" (MVP keyword index occupies the slot; vector store is Phase 2)
      - top_k = 5 (range 1..20)
      - min_confidence = 0.5 (range 0.0..1.0)
      - chunk_size = 512
      - overlap = 64
    """

    # H4: persisted model uses extra="ignore" so a 9.2 / Phase 2 schema upgrade
    # (e.g. new `embedding_model` / `vector_store_url` fields) does not cause
    # already-persisted packs to vanish on rollback. Request DTOs that wrap this
    # in xueban/api/knowledge.py keep `extra="forbid"` for input strictness.
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    mode: RetrievalMode = "semantic"
    top_k: int = Field(default=5, ge=1, le=20)
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    chunk_size: int = Field(default=512, ge=32, le=8192)
    overlap: int = Field(default=64, ge=0, le=2048)

    @model_validator(mode="after")
    def _overlap_lt_chunk_size(self) -> "RetrievalProfile":
        if self.overlap >= self.chunk_size:
            raise ValueError(
                f"overlap ({self.overlap}) must be less than chunk_size ({self.chunk_size})"
            )
        return self


__all__ = ["RetrievalProfile", "RetrievalMode"]
