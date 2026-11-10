"""InMemoryRiverStub — Task 0 (Story 2.9).

A minimal in-memory stub that satisfies the river v1 ABC contract.
Used while river v1 baseline (Story 1.6) is not yet implemented.

Interface contract (must match future river v1 API exactly):
    async def drink(query: str, scope: list[str]) -> list[str]
    async def pour(candidate: dict, source_agent_id: str) -> str

When river v1 is ready, replace InMemoryRiverStub with the real river
instance via dependency injection — no code changes needed in bridge.py.
"""

from __future__ import annotations

from typing import Any, Dict, List


class InMemoryRiverStub:
    """In-memory stub implementing the river v1 drink/pour contract.

    - drink() always returns an empty list (no existing memory).
    - pour() stores the candidate in a local list and returns "accepted".
    - Internal store is NOT persisted; it exists only for the lifetime of this
      object (useful for integration tests that want to inspect poured items).
    """

    def __init__(self) -> None:
        self._store: List[Dict[str, Any]] = []

    async def drink(self, query: str, scope: List[str]) -> List[str]:
        """Retrieve relevant context fragments from river memory.

        Stub implementation: always returns an empty list.
        River v1 will query Sediment layers (alluvium/sandstone/bedrock)
        with Write Gate Read-side filtering.

        Args:
            query:  Semantic search query.
            scope:  List of layer names to search (e.g. ["alluvium", "sandstone"]).

        Returns:
            List of context text fragments (empty in stub).
        """
        return []  # stub: no stored memories yet

    async def pour(self, candidate: Dict[str, Any], source_agent_id: str) -> str:
        """Propose a memory candidate for write-back to the river.

        Stub implementation: accepts every candidate unconditionally and stores
        it in the in-memory list.

        Args:
            candidate:       Candidate dict (content, confidence, target_layer, …).
            source_agent_id: ID of the external agent proposing the memory.

        Returns:
            "accepted" | "rejected" | "deferred"  — stub always returns "accepted".
        """
        self._store.append({**candidate, "source": source_agent_id})
        return "accepted"

    def get_store(self) -> List[Dict[str, Any]]:
        """Inspect the in-memory store (test/debug only)."""
        return list(self._store)
