"""MemoryProfile model tests — Story 9.3 AC1 + AC6.

Covers:
  - Field defaults and types.
  - model_validator: working_memory_limit >= 256.
  - model_validator: semantic_retrieval_top_k >= 1.
  - model_validator: episodic_retention_days >= 0.
  - profile_id auto-generated UUID.
  - created_at / updated_at timestamps present.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from xueban.memory.memory_profile import (
    CompressionPolicy,
    MemoryProfile,
    StateSyncPolicy,
    WritebackPolicy,
)


# ---------------------------------------------------------------------------
# Positive cases
# ---------------------------------------------------------------------------

def test_defaults():
    p = MemoryProfile()
    assert p.working_memory_limit == 4096
    assert p.episodic_retention_days == 30
    assert p.semantic_retrieval_top_k == 5
    assert p.writeback_policy == WritebackPolicy.ON_TASK_COMPLETE
    assert p.state_sync_policy == StateSyncPolicy.LAZY
    assert p.compression_policy == CompressionPolicy.NONE
    assert p.profile_id
    assert p.created_at is not None
    assert p.updated_at is not None


def test_custom_values():
    p = MemoryProfile(
        working_memory_limit=8192,
        episodic_retention_days=0,
        semantic_retrieval_top_k=10,
        writeback_policy=WritebackPolicy.ALWAYS,
        compression_policy=CompressionPolicy.SUMMARIZE,
    )
    assert p.working_memory_limit == 8192
    assert p.episodic_retention_days == 0
    assert p.semantic_retrieval_top_k == 10


def test_unique_profile_ids():
    p1 = MemoryProfile()
    p2 = MemoryProfile()
    assert p1.profile_id != p2.profile_id


def test_writeback_policy_manual():
    p = MemoryProfile(writeback_policy=WritebackPolicy.MANUAL)
    assert p.writeback_policy == WritebackPolicy.MANUAL


def test_compression_select_top_k():
    p = MemoryProfile(compression_policy=CompressionPolicy.SELECT_TOP_K)
    assert p.compression_policy == CompressionPolicy.SELECT_TOP_K


# ---------------------------------------------------------------------------
# Negative cases (model_validator)
# ---------------------------------------------------------------------------

def test_working_memory_limit_too_low():
    with pytest.raises(ValidationError) as exc_info:
        MemoryProfile(working_memory_limit=255)
    assert "working_memory_limit" in str(exc_info.value) or "255" in str(exc_info.value)


def test_semantic_retrieval_top_k_zero():
    with pytest.raises(ValidationError) as exc_info:
        MemoryProfile(semantic_retrieval_top_k=0)
    assert "semantic_retrieval_top_k" in str(exc_info.value) or "0" in str(exc_info.value)


def test_episodic_retention_days_negative():
    with pytest.raises(ValidationError) as exc_info:
        MemoryProfile(episodic_retention_days=-1)
    assert "episodic_retention_days" in str(exc_info.value) or "-1" in str(exc_info.value)


def test_invalid_writeback_policy():
    with pytest.raises(ValidationError):
        MemoryProfile(writeback_policy="bogus")  # type: ignore


def test_min_valid_working_memory_limit():
    p = MemoryProfile(working_memory_limit=256)
    assert p.working_memory_limit == 256
