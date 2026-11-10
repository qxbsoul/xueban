"""MemoryProfile — Story 9.3 AC1.

Pydantic v2 data contract that describes how an agent manages its three-layer
memory (working / episodic / semantic) and when it writes back after a run.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


class WritebackPolicy(str, Enum):
    ALWAYS = "always"
    ON_TASK_COMPLETE = "on_task_complete"
    ON_SESSION_END = "on_session_end"
    MANUAL = "manual"


class StateSyncPolicy(str, Enum):
    EAGER = "eager"
    LAZY = "lazy"
    DISABLED = "disabled"


class CompressionPolicy(str, Enum):
    NONE = "none"
    SUMMARIZE = "summarize"
    SELECT_TOP_K = "select_top_k"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MemoryProfile(BaseModel):
    """Memory configuration for one agent."""

    profile_id: str = Field(default_factory=lambda: str(uuid4()))
    working_memory_limit: int = Field(default=4096, ge=256)
    episodic_retention_days: int = Field(default=30, ge=0)
    semantic_retrieval_top_k: int = Field(default=5, ge=1)
    # Number of context items to keep when compression_policy="select_top_k".
    # Separate from semantic_retrieval_top_k to avoid semantic confusion.
    compression_top_k: int = Field(default=10, ge=1)
    writeback_policy: WritebackPolicy = WritebackPolicy.ON_TASK_COMPLETE
    state_sync_policy: StateSyncPolicy = StateSyncPolicy.LAZY
    compression_policy: CompressionPolicy = CompressionPolicy.NONE
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    model_config = {"use_enum_values": True}
