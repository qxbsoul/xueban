"""KnowledgePack + RetrievalProfile model tests — Story 9.1 AC6.

Covers:
  - Field defaults / range validation for RetrievalProfile.
  - KnowledgePack `model_validator(mode="after")` positive + negative cases:
      * empty `sources` → ValidationError
      * citation_required without sources → ValidationError (covered by sources rule)
      * top_k out of range (caught by Field constraint and re-checked by validator)
  - update_pack() refreshes `updated_at` and re-runs validation.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from xueban.memory.knowledge_pack import (
    KnowledgePack,
    KnowledgeSource,
    update_pack,
)
from xueban.memory.retrieval_profiles import RetrievalProfile


def _src() -> KnowledgeSource:
    return KnowledgeSource(source_type="text", source_ref="hello")


# ---------------------------------------------------------------------------
# RetrievalProfile
# ---------------------------------------------------------------------------


def test_retrieval_profile_defaults():
    p = RetrievalProfile()
    assert p.mode == "semantic"
    assert p.top_k == 5
    assert p.min_confidence == 0.5
    assert p.chunk_size == 512
    assert p.overlap == 64


def test_retrieval_profile_top_k_out_of_range():
    with pytest.raises(ValidationError):
        RetrievalProfile(top_k=0)
    with pytest.raises(ValidationError):
        RetrievalProfile(top_k=21)


def test_retrieval_profile_min_confidence_range():
    with pytest.raises(ValidationError):
        RetrievalProfile(min_confidence=-0.1)
    with pytest.raises(ValidationError):
        RetrievalProfile(min_confidence=1.5)


# ---------------------------------------------------------------------------
# KnowledgePack — happy path
# ---------------------------------------------------------------------------


def test_knowledge_pack_minimal_construction():
    pack = KnowledgePack(name="docs", sources=[_src()])
    assert pack.pack_id  # auto-generated
    assert pack.status == "pending"
    assert pack.citation_required is False
    assert pack.sources[0].ingest_status == "pending"
    assert pack.retrieval_profile.top_k == 5


def test_knowledge_pack_serializes_round_trip():
    pack = KnowledgePack(name="docs", sources=[_src()])
    payload = pack.model_dump(mode="json")
    rebuilt = KnowledgePack.model_validate(payload)
    assert rebuilt.pack_id == pack.pack_id
    assert rebuilt.sources[0].source_ref == "hello"


# ---------------------------------------------------------------------------
# KnowledgePack — model_validator (negative cases)
# ---------------------------------------------------------------------------


def test_knowledge_pack_rejects_empty_sources():
    with pytest.raises(ValidationError) as exc:
        KnowledgePack(name="docs", sources=[])
    assert "sources" in str(exc.value)


def test_knowledge_pack_missing_retrieval_profile_uses_default():
    """retrieval_profile has a default; missing field is legal."""
    pack = KnowledgePack(name="docs", sources=[_src()])
    assert isinstance(pack.retrieval_profile, RetrievalProfile)


def test_knowledge_pack_rejects_invalid_freshness():
    with pytest.raises(ValidationError):
        KnowledgePack(
            name="docs",
            sources=[_src()],
            freshness_policy="never",  # type: ignore[arg-type]
        )


def test_knowledge_pack_ignores_unknown_field():
    """H4: persisted model uses extra='ignore' so a Phase-2 schema upgrade does
    not invalidate older records. Unknown fields are silently dropped on load."""
    pack = KnowledgePack.model_validate(
        {
            "name": "docs",
            "sources": [_src().model_dump()],
            "future_field_added_in_9_2": {"vector_store_url": "https://qdrant/x"},
        }
    )
    assert pack.name == "docs"
    # No `future_field_added_in_9_2` attribute survives the parse.
    assert not hasattr(pack, "future_field_added_in_9_2")


# ---------------------------------------------------------------------------
# update_pack
# ---------------------------------------------------------------------------


def test_update_pack_refreshes_updated_at():
    pack = KnowledgePack(name="docs", sources=[_src()])
    initial_updated = pack.updated_at
    new_pack = update_pack(pack, name="docs2")
    assert new_pack.name == "docs2"
    assert new_pack.updated_at >= initial_updated
    assert new_pack.pack_id == pack.pack_id


def test_update_pack_runs_validator():
    pack = KnowledgePack(name="docs", sources=[_src()])
    # Removing all sources via update should trigger model_validator
    with pytest.raises(ValidationError):
        update_pack(pack, sources=[])
