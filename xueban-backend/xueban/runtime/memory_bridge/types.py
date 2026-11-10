"""Pydantic models for ExternalMemoryBridge (Story 2.9).

Types:
    DrinkResult       — fence-wrapped context fragment returned from drink()
    SedimentCandidate — a single pour proposal from an external agent
    PourResult        — aggregated result of a pour() call (three buckets)
    MemoryFeedback    — single-round feedback injected into next session.prompt
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# DrinkResult
# ---------------------------------------------------------------------------


class DrinkResult(BaseModel):
    """ACP context fragment produced by drink().

    Matches the ACP session.prompt ``type: context`` extension shape:
    {
      "type": "context",
      "fence": "xueban-context",
      "fence_uuid": "<uuid4 per-turn>",
      "text": "<drink content>"
    }
    """

    type: Literal["context"] = "context"
    fence: Literal["xueban-context"] = "xueban-context"
    fence_uuid: str = Field(default_factory=lambda: str(uuid4()))
    text: str = ""
    empty: bool = False
    warning: Optional[str] = None

    def to_acp_fragment(self) -> Dict[str, Any]:
        """Serialize to the ACP wire format for session.prompt injection."""
        return {
            "type": self.type,
            "fence": self.fence,
            "fence_uuid": self.fence_uuid,
            "text": self.text,
        }


# ---------------------------------------------------------------------------
# SedimentCandidate
# ---------------------------------------------------------------------------


class SedimentCandidate(BaseModel):
    """A single memory pour proposal from an external agent.

    Mirrors the ACP session.update ``type: xueban_memory_proposal`` shape:
    {
      "content": "...",
      "confidence": 0.82,
      "target_layer": "alluvium",
      ...
    }
    """

    candidate_id: str = Field(default_factory=lambda: f"cand-{uuid4().hex[:12]}")
    content: str
    confidence: float = 1.0
    target_layer: str = "alluvium"
    metadata: Dict[str, Any] = Field(default_factory=dict)

    # Set by bridge after pour — tracks resolution
    source_agent_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Bucket items
# ---------------------------------------------------------------------------


class AcceptedItem(BaseModel):
    candidate_id: str
    settled_at_layer: str = "alluvium"


class RejectedItem(BaseModel):
    candidate_id: str
    reason: Literal[
        "duplicate",
        "low_confidence",
        "mode_not_writable",
        "no_pour_targets",  # Story 2.9 H2: two_way mode but agent_card.pour_targets empty
        "over_capacity",  # Story 2.9 Round 4 M-A: pour candidates exceed MAX_POUR_CANDIDATES
        "agent_card_unreadable",  # Story 2.9 Round 4 M-D: loader raised → fail closed
        "river_error",
        "unknown",
    ] = "unknown"


class DeferredItem(BaseModel):
    candidate_id: str
    reason: Literal[
        "needs_social_signal",
        "river_timeout",
        "unknown",
    ] = "unknown"


# ---------------------------------------------------------------------------
# PourResult
# ---------------------------------------------------------------------------


class PourResult(BaseModel):
    """Aggregated result of a pour() call — three classification buckets."""

    accepted: List[AcceptedItem] = Field(default_factory=list)
    rejected: List[RejectedItem] = Field(default_factory=list)
    deferred: List[DeferredItem] = Field(default_factory=list)

    @property
    def accepted_count(self) -> int:
        return len(self.accepted)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)

    @property
    def deferred_count(self) -> int:
        return len(self.deferred)

    @property
    def is_empty(self) -> bool:
        return self.accepted_count == 0 and self.rejected_count == 0 and self.deferred_count == 0


# ---------------------------------------------------------------------------
# MemoryFeedback
# ---------------------------------------------------------------------------


class MemoryFeedback(BaseModel):
    """Single-round feedback injected into xueban_envelope.memory_feedback.

    Lifecycle: produced by bridge after pour(), consumed once in the next
    session.prompt construction by HermesGateway, then discarded.
    """

    accepted: List[AcceptedItem] = Field(default_factory=list)
    rejected: List[RejectedItem] = Field(default_factory=list)
    deferred: List[DeferredItem] = Field(default_factory=list)

    @classmethod
    def from_pour_result(cls, result: PourResult) -> "MemoryFeedback":
        return cls(
            accepted=result.accepted,
            rejected=result.rejected,
            deferred=result.deferred,
        )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InvalidMemoryBridgeMode(ValueError):
    """Raised when agent-card memory_bridge.mode is not a valid value."""


class AgentCardUnreadable(RuntimeError):
    """Raised when the agent_card_loader callable itself fails (Round 4 M-D).

    Distinct from :class:`InvalidMemoryBridgeMode` so that bridge entry points
    can fail **closed** to a safe response (empty drink / all-rejected pour)
    while still surfacing genuine config errors (invalid mode literal) to the
    caller.
    """
