"""
xueban/assembly/activation.py

Catalog-level activation for spontaneous workflow assembly.

IMPORTANT: This module handles catalog-level activation ("which blocks to select
for this goal?"), NOT node-level runtime activation (WorkflowActivationSpec in
highlevel.py, which decides "should this node execute during a running workflow?").
These are different concerns at different layers.

v1 is a tag-based greedy set-cover selector, NOT a learning algorithm.
v1 topology is a linear chain. v2 will introduce capability-dependency graph
inference. v3 will explore RL-driven assembly.
"""
from __future__ import annotations

import re
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class CatalogActivationCandidate(BaseModel):
    block_id: str
    matched_capabilities: List[str] = Field(default_factory=list)
    suppressed_reason: Optional[str] = None


class ActivationResult(BaseModel):
    candidates: List[CatalogActivationCandidate] = Field(default_factory=list)
    # complete=True means ALL required_capabilities are covered by candidates.
    complete: bool = False
    missing_capabilities: List[str] = Field(default_factory=list)
    fallback_policy: Literal["reject", "surface_to_user", "escalate_to_llm"] = "surface_to_user"


def _tokenize(text: str) -> List[str]:
    """Split goal text into lowercase tokens, supporting CJK and ASCII."""
    # Split on whitespace and common punctuation, keep CJK chars as individual tokens
    tokens = re.findall(r"[\u4e00-\u9fff]|[a-zA-Z0-9]+", text)
    return [t.lower() for t in tokens]


class ActivationSelector:
    """
    Catalog-level activation selector (v1: tag-based greedy set-cover).

    Algorithm:
    1. Tokenize goal → goal_tokens
    2. For each block in catalog:
       - If any tag matches a goal token → block is a candidate
       - matched_capabilities = block.capabilities (all capabilities the block provides)
    3. Compute required_capabilities = union of all capabilities in catalog that
       are "reachable" from goal tokens (tags of matching blocks → their capabilities)
    4. Greedy minimum set cover: pick fewest blocks that cover required_capabilities
    5. complete = (required_capabilities are fully covered AND candidates non-empty)
    6. OOD: goal tokens match no tags at all → complete=False, missing=["unknown"]

    Pluggable hook (Phase 2+): override `rerank(candidates)` for LLM re-ranking.
    """

    def select(
        self,
        goal: str,
        catalog: Dict[str, "WorkflowBlockSpec"],  # type: ignore[name-defined]
    ) -> ActivationResult:
        goal_tokens = set(_tokenize(goal))

        goal_lower = goal.lower()

        # Step 1: find all blocks whose tags overlap with goal tokens or goal text.
        # Two matching modes:
        #   a) tag in goal_tokens (ASCII single-word match)
        #   b) tag in goal_lower  (substring match — catches CJK multi-char words like "规划")
        tag_matched: List[tuple[str, "WorkflowBlockSpec"]] = []  # type: ignore[name-defined]
        for block_id, block in catalog.items():
            if block.local_activation is None:
                continue
            block_tags = {t.lower() for t in (block.local_activation.tags or [])}
            if (goal_tokens & block_tags) or any(tag in goal_lower for tag in block_tags):
                tag_matched.append((block_id, block))

        # OOD: no tags matched at all
        if not tag_matched:
            return ActivationResult(
                candidates=[],
                complete=False,
                missing_capabilities=["unknown"],
                fallback_policy="surface_to_user",
            )

        # Step 2: required_capabilities = union of capabilities of all tag-matched blocks
        required_capabilities: set[str] = set()
        for _, block in tag_matched:
            required_capabilities.update(block.capabilities)

        # Step 3: greedy minimum set cover over tag-matched blocks
        # Sort blocks by descending number of capabilities (heuristic for fewer blocks)
        candidates_pool = sorted(tag_matched, key=lambda x: len(x[1].capabilities), reverse=True)
        covered: set[str] = set()
        selected: List[CatalogActivationCandidate] = []

        for block_id, block in candidates_pool:
            new_caps = set(block.capabilities) - covered
            if new_caps:
                covered.update(new_caps)
                selected.append(
                    CatalogActivationCandidate(
                        block_id=block_id,
                        matched_capabilities=list(new_caps),
                    )
                )
            if covered >= required_capabilities:
                break

        missing = list(required_capabilities - covered)
        complete = len(selected) > 0 and len(missing) == 0

        return ActivationResult(
            candidates=selected,
            complete=complete,
            missing_capabilities=missing,
            fallback_policy="surface_to_user",
        )


class ConnectionResolver:
    """
    Resolves a list of activation candidates into WorkflowAssemblyLinkSpec connections.

    v1 (strategy="linear"): linear chain — block1 → block2 → ... → END.
    v2 (strategy="capability"): capability-dependency graph inference.
        When block A's capabilities satisfy block B's input_requirements, add edge A→B.
        Supports fan-out (one provider → multiple consumers) and fan-in (multiple
        providers → one consumer). Detects cycles and handles isolated blocks.
    """

    def resolve(
        self,
        candidates: List[CatalogActivationCandidate],
        catalog: Optional[Dict[str, "WorkflowBlockSpec"]] = None,  # type: ignore[name-defined]
        strategy: Literal["linear", "capability"] = "linear",
    ) -> List["WorkflowAssemblyLinkSpec"]:  # type: ignore[name-defined]
        if not candidates:
            return []
        if strategy == "capability" and catalog is not None:
            return self._resolve_capability(candidates, catalog)
        return self._resolve_linear(candidates)

    def _resolve_linear(
        self,
        candidates: List[CatalogActivationCandidate],
    ) -> List["WorkflowAssemblyLinkSpec"]:  # type: ignore[name-defined]
        from xueban.highlevel import WorkflowAssemblyLinkSpec

        links: List[WorkflowAssemblyLinkSpec] = []
        for i, candidate in enumerate(candidates):
            next_id = candidates[i + 1].block_id if i + 1 < len(candidates) else "END"
            links.append(
                WorkflowAssemblyLinkSpec.model_validate(
                    {"from": candidate.block_id, "to": next_id}
                )
            )
        return links

    def _resolve_capability(
        self,
        candidates: List[CatalogActivationCandidate],
        catalog: Dict[str, "WorkflowBlockSpec"],  # type: ignore[name-defined]
    ) -> List["WorkflowAssemblyLinkSpec"]:  # type: ignore[name-defined]
        from xueban.highlevel import WorkflowAssemblyLinkSpec

        candidate_ids = [c.block_id for c in candidates]

        # Build capability → provider block mapping (only among candidates)
        cap_providers: Dict[str, List[str]] = {}
        for bid in candidate_ids:
            block = catalog.get(bid)
            if block is None:
                continue
            for cap in block.capabilities:
                cap_providers.setdefault(cap, []).append(bid)

        # Build edges: for each candidate, find providers for its input_requirements
        edges: List[tuple[str, str]] = []
        has_incoming: set[str] = set()
        has_outgoing: set[str] = set()

        for bid in candidate_ids:
            block = catalog.get(bid)
            if block is None:
                continue
            for req in block.input_requirements:
                providers = cap_providers.get(req, [])
                for provider_id in providers:
                    if provider_id != bid:  # no self-loops
                        edges.append((provider_id, bid))
                        has_outgoing.add(provider_id)
                        has_incoming.add(bid)

        # Cycle detection via DFS
        adj: Dict[str, List[str]] = {}
        for src, dst in edges:
            adj.setdefault(src, []).append(dst)

        WHITE, GRAY, BLACK = 0, 1, 2
        color: Dict[str, int] = {bid: WHITE for bid in candidate_ids}

        def _has_cycle(node: str) -> bool:
            color[node] = GRAY
            for neighbor in adj.get(node, []):
                if color.get(neighbor) == GRAY:
                    return True
                if color.get(neighbor) == WHITE and _has_cycle(neighbor):
                    return True
            color[node] = BLACK
            return False

        for bid in candidate_ids:
            if color[bid] == WHITE:
                if _has_cycle(bid):
                    raise ValueError(
                        f"Cycle detected in capability dependency graph among candidates: {candidate_ids}"
                    )

        # Blocks with no incoming edges and no outgoing edges → isolated, connect to END
        # Blocks with no outgoing edges (but have incoming) → terminal, connect to END
        links: List[WorkflowAssemblyLinkSpec] = []
        for src, dst in edges:
            links.append(
                WorkflowAssemblyLinkSpec.model_validate({"from": src, "to": dst})
            )

        # Connect terminal blocks (no downstream consumers) to END
        for bid in candidate_ids:
            if bid not in has_outgoing:
                links.append(
                    WorkflowAssemblyLinkSpec.model_validate({"from": bid, "to": "END"})
                )

        return links
