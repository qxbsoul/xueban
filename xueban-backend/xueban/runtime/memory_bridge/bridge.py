"""ExternalMemoryBridge — Story 2.9 core implementation.

Three-pipeline bridge between external ACP agents and Xueban river memory:

  drink()      — river档案馆读取 → fence封装 → ACP context片段
  pour()       — ACP session.update proposals → river write-gate → 三桶分类
  get_feedback() — 取回上一轮 pour 结果（取用即清，单轮时效）

Mode matrix (AC4):
  two_way   : drink enabled, pour enabled
  read_only : drink enabled, pour → all rejected(mode_not_writable)
  isolated  : drink → empty context, pour → all rejected

Circuit breaker (AC5 / NFR13):
  timeout = 5s; open window = 60s; (agent_id, operation) granularity.

Trajectory writes (AC5 / FR40):
  memory_drink / memory_pour / memory_feedback events → jsonl log.

SSE events:
  agent.memory_drink, agent.memory_pour,
  agent.memory_bridge_circuit_break, agent.memory_bridge_circuit_recover,
  agent.memory_feedback
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from xueban.runtime.memory_bridge.circuit_breaker import CircuitBreaker
from xueban.runtime.memory_bridge.fence import (
    build_drink_result,
    build_empty_drink_result,
    new_fence_uuid,
)
from xueban.runtime.memory_bridge.types import (
    AcceptedItem,
    DeferredItem,
    DrinkResult,
    AgentCardUnreadable,
    InvalidMemoryBridgeMode,
    MemoryFeedback,
    PourResult,
    RejectedItem,
    SedimentCandidate,
)
from xueban.runtime.sanitize import sanitize_trajectory

logger = logging.getLogger("xueban.memory_bridge")

VALID_MODES = {"two_way", "read_only", "isolated"}

# Round 4 P3 follow-up M-A: hard cap on pour candidates per call. Each candidate
# can take up to TIMEOUT_SECONDS (5s) under a tripped breaker, so an unbounded
# input could trivially block a session for minutes. 100 covers realistic
# research / coding agent batches with headroom; over-cap candidates are
# rejected with reason="over_capacity" instead of silently dropped.
MAX_POUR_CANDIDATES = 100

# Default trajectory log location — override via TRAJECTORY_JSONL_PATH env var.
_DEFAULT_TRAJECTORY_PATH = Path("trajectory.jsonl")


class ExternalMemoryBridge:
    """Bridge between external ACP agents and Xueban river memory.

    Constructor args:
        river:             Object implementing async drink(query, scope) and
                           async pour(candidate, source_agent_id).
                           Accepts InMemoryRiverStub or future river v1 instance.
        agent_card_loader: Callable(agent_id) → dict with at minimum:
                           {"memory_bridge": {"mode": "two_way", "drink_from": [...],
                                              "pour_targets": [...]}}
                           Pass None to skip agent-card lookups (tests / isolated mode).
        sse_emitter:       Optional callable(event_type: str, payload: dict) for SSE.
        trajectory_path:   Optional path to append trajectory JSONL events.
    """

    def __init__(
        self,
        river: Any,
        agent_card_loader: Optional[Callable[[str], Dict[str, Any]]] = None,
        sse_emitter: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        trajectory_path: Optional[Path] = None,
    ) -> None:
        self._river = river
        self._agent_card_loader = agent_card_loader
        self._sse_emitter = sse_emitter
        self._trajectory_path = trajectory_path or _DEFAULT_TRAJECTORY_PATH

        # Per-(agent_id, operation) circuit breaker
        self._circuit_breaker = CircuitBreaker(sse_emitter=sse_emitter)

        # session_id → PourResult (single-round cache)
        self._pending_feedback: Dict[str, PourResult] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_mode(self, agent_id: str) -> str:
        """Resolve memory_bridge.mode from agent card.

        Round 4 P3 follow-up M-D: fail **closed** when the loader raises.
        Previously the loader's exception silently fell back to ``two_way``,
        which combined with an empty drink scope could leak across tenants if
        a future river v1 implementation interprets ``drink_from=[]`` as
        "all layers". We now raise :class:`InvalidMemoryBridgeMode` (a
        ``ValueError`` subclass) so the bridge entry points can short-circuit
        to a safe state (drink: empty + warning, pour: reject all).
        """
        if self._agent_card_loader is None:
            return "two_way"
        try:
            card = self._agent_card_loader(agent_id)
            mode = card.get("memory_bridge", {}).get("mode", "two_way")
        except Exception as exc:
            logger.warning(
                "Failed to load agent card for %s; failing closed (M-D): %s",
                agent_id,
                exc,
            )
            raise AgentCardUnreadable(
                f"agent_card_loader raised for agent {agent_id!r}: {exc}"
            ) from exc
        if mode not in VALID_MODES:
            raise InvalidMemoryBridgeMode(
                f"agent {agent_id} has invalid memory_bridge.mode={mode!r}; "
                f"must be one of {sorted(VALID_MODES)}"
            )
        return mode

    def _get_drink_scope(self, agent_id: str, scope: Optional[List[str]]) -> List[str]:
        """Merge caller-provided scope with agent-card drink_from list.

        Round 4 M-D: when the loader raises we propagate the failure so the
        bridge fails closed (caller treats it as agent_card_unreadable) rather
        than silently using an empty scope.
        """
        if scope is not None:
            return scope
        if self._agent_card_loader is None:
            return []
        try:
            card = self._agent_card_loader(agent_id)
            return list(card.get("memory_bridge", {}).get("drink_from", []))
        except Exception as exc:
            raise AgentCardUnreadable(
                f"agent_card_loader raised reading drink_from for {agent_id!r}: {exc}"
            ) from exc

    def _get_pour_targets(self, agent_id: str) -> List[str]:
        """Return agent-card memory_bridge.pour_targets list (Story 2.9 AC2 precondition).

        AC2 requires `pour_targets` to be non-empty for two_way mode pours; an
        empty list means the agent is configured to not write to any layer.
        """
        if self._agent_card_loader is None:
            return []
        try:
            card = self._agent_card_loader(agent_id)
            return list(card.get("memory_bridge", {}).get("pour_targets", []))
        except Exception as exc:
            # Round 4 M-D: fail closed — caller will reject all candidates.
            raise AgentCardUnreadable(
                f"agent_card_loader raised reading pour_targets for {agent_id!r}: {exc}"
            ) from exc

    def _emit(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self._sse_emitter is not None:
            try:
                self._sse_emitter(event_type, payload)
            except Exception:
                logger.exception("SSE emitter raised; continuing")

    def _write_trajectory(self, event: str, data: Dict[str, Any]) -> None:
        """Append one trajectory event to the JSONL log with sanitisation."""
        record = {"event": event, "ts": datetime.now(timezone.utc).isoformat(), **data}
        # Sanitise — strip API keys / PII before persisting
        cleaned, _removed = sanitize_trajectory(record)
        try:
            with open(self._trajectory_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(cleaned, ensure_ascii=False) + "\n")
        except Exception:
            logger.exception("Failed to write trajectory event=%s", event)

    # ------------------------------------------------------------------
    # drink — AC1
    # ------------------------------------------------------------------

    async def drink(
        self,
        query: str,
        agent_id: str,
        session_id: str,
        scope: Optional[List[str]] = None,
    ) -> DrinkResult:
        """River档案馆读取 → fence封装 → return DrinkResult.

        Mode:
            isolated  → return empty context without calling river.
            two_way / read_only → call river.drink under circuit breaker.

        Returns:
            DrinkResult with fence metadata set.
        """
        fence_uuid = new_fence_uuid()
        # Round 4 M-D: fail closed on agent-card loader errors — drink returns
        # empty context with warning="agent_card_unreadable" instead of leaking
        # into an unsafe two_way default.
        try:
            mode = self._get_mode(agent_id)
        except AgentCardUnreadable as exc:
            logger.warning(
                "drink: failing closed for agent=%s: %s", agent_id, exc
            )
            result = build_empty_drink_result(
                fence_uuid=fence_uuid, warning="agent_card_unreadable"
            )
            self._emit(
                "agent.memory_drink",
                {
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "query": query,
                    "result_chunk_count": 0,
                    "fence_uuid": fence_uuid,
                    "mode": "fail_closed",
                    "warning": "agent_card_unreadable",
                },
            )
            self._write_trajectory(
                "memory_drink",
                {
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "fence_uuid": fence_uuid,
                    "result": {"empty": True, "warning": "agent_card_unreadable"},
                },
            )
            return result

        if mode == "isolated":
            result = build_empty_drink_result(fence_uuid=fence_uuid)
            self._emit(
                "agent.memory_drink",
                {
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "query": query,
                    "result_chunk_count": 0,
                    "fence_uuid": fence_uuid,
                    "mode": mode,
                },
            )
            self._write_trajectory(
                "memory_drink",
                {
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "fence_uuid": fence_uuid,
                    "result": {"empty": True, "mode": mode},
                },
            )
            return result

        try:
            resolved_scope = self._get_drink_scope(agent_id, scope)
        except AgentCardUnreadable as exc:
            logger.warning(
                "drink: scope loader failed for agent=%s: %s", agent_id, exc
            )
            result = build_empty_drink_result(
                fence_uuid=fence_uuid, warning="agent_card_unreadable"
            )
            self._emit(
                "agent.memory_drink",
                {
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "query": query,
                    "result_chunk_count": 0,
                    "fence_uuid": fence_uuid,
                    "mode": "fail_closed",
                    "warning": "agent_card_unreadable",
                },
            )
            return result

        # Call river.drink under circuit breaker
        fragments, timed_out = await self._circuit_breaker.call(
            agent_id,
            "drink",
            self._river.drink(query, resolved_scope),
        )

        if timed_out:
            result = build_empty_drink_result(
                fence_uuid=fence_uuid, warning="river_unreachable"
            )
            chunk_count = 0
        else:
            fragments = fragments or []
            text = "\n".join(str(f) for f in fragments)
            result = build_drink_result(text=text, fence_uuid=fence_uuid)
            chunk_count = len(fragments)

        self._emit(
            "agent.memory_drink",
            {
                "agent_id": agent_id,
                "session_id": session_id,
                "query": query,
                "result_chunk_count": chunk_count,
                "fence_uuid": fence_uuid,
                "mode": mode,
                "timed_out": timed_out,
            },
        )
        self._write_trajectory(
            "memory_drink",
            {
                "agent_id": agent_id,
                "session_id": session_id,
                "fence_uuid": fence_uuid,
                "result": {
                    "chunk_count": chunk_count,
                    "empty": result.empty,
                    "warning": result.warning,
                },
            },
        )
        return result

    # ------------------------------------------------------------------
    # pour — AC2
    # ------------------------------------------------------------------

    async def pour(
        self,
        candidates: List[Dict[str, Any]],
        agent_id: str,
        session_id: str,
    ) -> PourResult:
        """Pour proposals from external agent → river Write Gate → 三桶分类.

        Mode:
            read_only / isolated → all rejected(mode_not_writable).
            two_way              → call river.pour for each candidate.

        Returns:
            PourResult with accepted/rejected/deferred buckets populated.
        """
        pour_result = PourResult()
        candidate_ids: List[str] = []

        # C1 fix: short-circuit on empty candidates to avoid emitting a ghost
        # SSE event / trajectory entry for a no-op pour. Direct callers (not
        # going through hermes.py's handle_session_update) are expected to
        # filter empty inputs themselves; this is defense-in-depth.
        if not candidates:
            return pour_result

        # Round 4 M-D: fail closed on agent-card loader errors — reject all.
        try:
            mode = self._get_mode(agent_id)
        except AgentCardUnreadable as exc:
            logger.warning(
                "pour: failing closed for agent=%s: %s", agent_id, exc
            )
            for raw in candidates:
                cid = raw.get("candidate_id") or f"cand-fail-{len(candidate_ids)}"
                pour_result.rejected.append(
                    RejectedItem(candidate_id=cid, reason="agent_card_unreadable")
                )
                candidate_ids.append(cid)
            self._emit(
                "agent.memory_pour",
                {
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "accepted_count": 0,
                    "rejected_count": pour_result.rejected_count,
                    "deferred_count": 0,
                    "mode": "fail_closed",
                    "warning": "agent_card_unreadable",
                },
            )
            return pour_result

        # Round 4 M-A: hard cap. Excess candidates are rejected with a clear
        # reason so callers can detect / retry; processing continues for the
        # first MAX_POUR_CANDIDATES so well-behaved batches still succeed.
        if len(candidates) > MAX_POUR_CANDIDATES:
            logger.warning(
                "pour: agent=%s sent %d candidates > cap %d; rejecting overflow",
                agent_id,
                len(candidates),
                MAX_POUR_CANDIDATES,
            )
            overflow = candidates[MAX_POUR_CANDIDATES:]
            candidates = candidates[:MAX_POUR_CANDIDATES]
            for raw in overflow:
                cid = raw.get("candidate_id") or f"cand-over-{uuid4().hex[:8]}"
                pour_result.rejected.append(
                    RejectedItem(candidate_id=cid, reason="over_capacity")
                )
                candidate_ids.append(cid)

        # H2 fix: AC2 precondition — two_way mode requires non-empty pour_targets.
        # Empty list ⇒ reject all candidates with a clear reason instead of
        # silently writing to the default layer. Loader errors here also fail
        # closed to agent_card_unreadable (M-D).
        try:
            two_way_no_targets = (
                mode == "two_way" and not self._get_pour_targets(agent_id)
            )
        except AgentCardUnreadable as exc:
            logger.warning(
                "pour: pour_targets loader failed for agent=%s: %s", agent_id, exc
            )
            for raw in candidates:
                cid = raw.get("candidate_id") or f"cand-fail-{len(candidate_ids)}"
                pour_result.rejected.append(
                    RejectedItem(candidate_id=cid, reason="agent_card_unreadable")
                )
                candidate_ids.append(cid)
            self._emit(
                "agent.memory_pour",
                {
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "accepted_count": 0,
                    "rejected_count": pour_result.rejected_count,
                    "deferred_count": 0,
                    "mode": "fail_closed",
                    "warning": "agent_card_unreadable",
                },
            )
            return pour_result

        _allowed_fields = set(SedimentCandidate.model_fields)
        for raw in candidates:
            # C2 fix: log when caller passes unknown fields so silent data loss
            # is observable. Known-safe extras (e.g. arbitrary metadata) should
            # be placed under the explicit `metadata` field.
            extra_keys = [k for k in raw if k not in _allowed_fields]
            if extra_keys:
                logger.warning(
                    "ExternalMemoryBridge.pour: dropped unknown SedimentCandidate "
                    "fields agent=%s extras=%s",
                    agent_id,
                    extra_keys,
                )
            cand = SedimentCandidate(**{k: v for k, v in raw.items() if k in _allowed_fields})
            cand.source_agent_id = agent_id
            candidate_ids.append(cand.candidate_id)

            if mode in ("read_only", "isolated"):
                pour_result.rejected.append(
                    RejectedItem(candidate_id=cand.candidate_id, reason="mode_not_writable")
                )
                continue

            if two_way_no_targets:
                pour_result.rejected.append(
                    RejectedItem(candidate_id=cand.candidate_id, reason="no_pour_targets")
                )
                continue

            # two_way: call river.pour under circuit breaker
            river_response, timed_out = await self._circuit_breaker.call(
                agent_id,
                "pour",
                self._river.pour(
                    {
                        "candidate_id": cand.candidate_id,
                        "content": cand.content,
                        "confidence": cand.confidence,
                        "target_layer": cand.target_layer,
                        "source_agent_id": agent_id,
                        **cand.metadata,
                    },
                    agent_id,
                ),
            )

            if timed_out:
                pour_result.deferred.append(
                    DeferredItem(candidate_id=cand.candidate_id, reason="river_timeout")
                )
                continue

            river_status = str(river_response) if river_response is not None else "rejected"

            if river_status == "accepted":
                pour_result.accepted.append(
                    AcceptedItem(
                        candidate_id=cand.candidate_id,
                        settled_at_layer=cand.target_layer,
                    )
                )
            elif river_status == "deferred":
                pour_result.deferred.append(
                    DeferredItem(candidate_id=cand.candidate_id, reason="needs_social_signal")
                )
            else:
                pour_result.rejected.append(
                    RejectedItem(candidate_id=cand.candidate_id, reason="river_error")
                )

        # Store for next-turn feedback injection (single-round cache)
        if not pour_result.is_empty:
            self._pending_feedback[session_id] = pour_result

        self._emit(
            "agent.memory_pour",
            {
                "agent_id": agent_id,
                "session_id": session_id,
                "accepted_count": pour_result.accepted_count,
                "rejected_count": pour_result.rejected_count,
                "deferred_count": pour_result.deferred_count,
                "mode": mode,
            },
        )
        self._write_trajectory(
            "memory_pour",
            {
                "agent_id": agent_id,
                "session_id": session_id,
                "candidate_ids": candidate_ids,
                "result": {
                    "accepted": pour_result.accepted_count,
                    "rejected": pour_result.rejected_count,
                    "deferred": pour_result.deferred_count,
                },
            },
        )
        return pour_result

    # ------------------------------------------------------------------
    # get_feedback — AC3
    # ------------------------------------------------------------------

    def get_feedback(self, session_id: str) -> Optional[MemoryFeedback]:
        """Return last-turn pour result as MemoryFeedback and clear the cache.

        Single-round time-validity: calling this method a second time for the
        same session_id returns None (already consumed).

        Returns:
            MemoryFeedback if a pour happened last turn, otherwise None.
        """
        pour_result = self._pending_feedback.pop(session_id, None)
        if pour_result is None:
            return None

        feedback = MemoryFeedback.from_pour_result(pour_result)
        self._emit(
            "agent.memory_feedback",
            {
                "session_id": session_id,
                "accepted_count": len(feedback.accepted),
                "rejected_count": len(feedback.rejected),
                "deferred_count": len(feedback.deferred),
            },
        )
        self._write_trajectory(
            "memory_feedback",
            {
                "session_id": session_id,
                "candidate_ids": (
                    [i.candidate_id for i in feedback.accepted]
                    + [i.candidate_id for i in feedback.rejected]
                    + [i.candidate_id for i in feedback.deferred]
                ),
                "result": {
                    "accepted": len(feedback.accepted),
                    "rejected": len(feedback.rejected),
                    "deferred": len(feedback.deferred),
                },
            },
        )
        return feedback
