"""River Memory — cross-iteration memory substrate (design §5.7).

The loop engine's memory: a flowing main river (pour/drink/scoop), a sediment
layer where successful patterns settle (settle/dredge), and dams for time-travel
(checkpoint snapshots: build_dam/open_dam). Backed by an in-process store; a
durable backend (DB / 0G) can replace the store without touching the interface.

Mapping to the cybernetic loop:
  drink/scoop -> next iteration recalls prior context (cross-iteration memory)
  pour        -> iteration output flows into the main river
  dredge      -> Controller/A reads settled skills (SEED-style recall)
  settle      -> A node sediments a reusable pattern on convergence
  build_dam/open_dam -> counterfactual replay / time-travel debugging (attrib. method ③)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

RiverKind = Literal["pattern", "observation", "decision", "artifact"]


class RiverChunk(BaseModel):
    chunk_id: str = Field(default_factory=lambda: f"river-{uuid4().hex[:12]}")
    run_id: str = ""
    loop_id: Optional[str] = None
    kind: RiverKind = "observation"
    content: Dict[str, Any] = Field(default_factory=dict)
    tags: List[str] = Field(default_factory=list)
    iteration_ref: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RiverMemory:
    """In-process River Memory (main river + sediment + dams)."""

    def __init__(self) -> None:
        self._main: List[RiverChunk] = []
        self._sediment: List[RiverChunk] = []
        self._dams: Dict[str, List[RiverChunk]] = {}

    # ---- main river (flowing) ----
    def pour(
        self,
        content: Dict[str, Any],
        *,
        run_id: str = "",
        loop_id: Optional[str] = None,
        kind: RiverKind = "observation",
        tags: Optional[List[str]] = None,
        iteration_ref: Optional[str] = None,
    ) -> RiverChunk:
        chunk = RiverChunk(
            run_id=run_id,
            loop_id=loop_id,
            kind=kind,
            content=content,
            tags=list(tags or []),
            iteration_ref=iteration_ref,
        )
        self._main.append(chunk)
        return chunk

    def drink(
        self,
        *,
        kind: Optional[RiverKind] = None,
        tags: Optional[List[str]] = None,
        loop_id: Optional[str] = None,
        run_id: Optional[str] = None,
        limit: int = 10,
    ) -> List[RiverChunk]:
        return self._filter(self._main, kind=kind, tags=tags, loop_id=loop_id, run_id=run_id, limit=limit)

    def scoop(self, predicate: Callable[[RiverChunk], bool], *, limit: int = 50) -> List[RiverChunk]:
        return [c for c in self._main if predicate(c)][:limit]

    # ---- sediment layer (settled patterns / skills) ----
    def settle(
        self,
        content: Dict[str, Any],
        *,
        run_id: str = "",
        loop_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
        iteration_ref: Optional[str] = None,
    ) -> RiverChunk:
        chunk = RiverChunk(
            run_id=run_id,
            loop_id=loop_id,
            kind="pattern",
            content=content,
            tags=list(tags or []),
            iteration_ref=iteration_ref,
        )
        self._sediment.append(chunk)
        return chunk

    def dredge(
        self,
        *,
        tags: Optional[List[str]] = None,
        loop_id: Optional[str] = None,
        run_id: Optional[str] = None,
        limit: int = 5,
    ) -> List[RiverChunk]:
        return self._filter(self._sediment, kind=None, tags=tags, loop_id=loop_id, run_id=run_id, limit=limit)

    # ---- dams (checkpoint snapshots for time-travel / counterfactual) ----
    def build_dam(self, name: str) -> str:
        snapshot = list(self._main) + list(self._sediment)
        self._dams[name] = snapshot
        return name

    def open_dam(self, name: str) -> List[RiverChunk]:
        return list(self._dams.get(name, []))

    def dam_names(self) -> List[str]:
        return list(self._dams.keys())

    # ---- maintenance ----
    def clear(self, run_id: Optional[str] = None) -> None:
        if run_id is None:
            self._main.clear()
            self._sediment.clear()
            self._dams.clear()
            return
        self._main = [c for c in self._main if c.run_id != run_id]
        self._sediment = [c for c in self._sediment if c.run_id != run_id]

    def __len__(self) -> int:
        return len(self._main) + len(self._sediment)

    # ---- internal ----
    @staticmethod
    def _filter(
        store: List[RiverChunk],
        *,
        kind: Optional[RiverKind],
        tags: Optional[List[str]],
        loop_id: Optional[str],
        run_id: Optional[str],
        limit: int,
    ) -> List[RiverChunk]:
        tag_set = set(tags or [])
        out: List[RiverChunk] = []
        for chunk in reversed(store):  # most recent first
            if kind is not None and chunk.kind != kind:
                continue
            if loop_id is not None and chunk.loop_id != loop_id:
                continue
            if run_id is not None and chunk.run_id != run_id:
                continue
            if tag_set and not tag_set.intersection(chunk.tags):
                continue
            out.append(chunk)
            if len(out) >= limit:
                break
        out.reverse()
        return out
