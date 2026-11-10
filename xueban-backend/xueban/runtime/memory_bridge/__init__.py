"""memory_bridge — ExternalMemoryBridge package (Story 2.9).

Public API:
    ExternalMemoryBridge  — main bridge class (bridge.py)
    InMemoryRiverStub     — in-memory river stub for testing (river_stub.py)
    DrinkResult           — fence-wrapped context fragment (types.py)
    PourResult            — pour classification buckets (types.py)
    MemoryFeedback        — single-round feedback payload (types.py)
    SedimentCandidate     — individual pour proposal (types.py)
"""

from xueban.runtime.memory_bridge.bridge import ExternalMemoryBridge
from xueban.runtime.memory_bridge.circuit_breaker import CircuitBreaker
from xueban.runtime.memory_bridge.fence import (
    build_drink_result,
    build_empty_drink_result,
    new_fence_uuid,
    validate_fence,
)
from xueban.runtime.memory_bridge.river_stub import InMemoryRiverStub
from xueban.runtime.memory_bridge.types import (
    AcceptedItem,
    DeferredItem,
    DrinkResult,
    InvalidMemoryBridgeMode,
    MemoryFeedback,
    PourResult,
    RejectedItem,
    SedimentCandidate,
)

__all__ = [
    "ExternalMemoryBridge",
    "CircuitBreaker",
    "InMemoryRiverStub",
    "DrinkResult",
    "PourResult",
    "MemoryFeedback",
    "SedimentCandidate",
    "AcceptedItem",
    "RejectedItem",
    "DeferredItem",
    "InvalidMemoryBridgeMode",
    "build_drink_result",
    "build_empty_drink_result",
    "new_fence_uuid",
    "validate_fence",
]
