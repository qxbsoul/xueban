"""HermesGateway — ACP session adapter with ExternalMemoryBridge hooks (Story 2.9).

Responsibilities:
  1. build_session_prompt()  — prepend drink() context fragment to session prompt.
  2. handle_session_update() — route xueban_memory_proposal updates to pour().
  3. Inject memory_feedback into xueban_envelope on next session.prompt (AC3).

Design:
  - HermesGateway is a thin coordinator; it delegates all memory logic to
    ExternalMemoryBridge. No business logic lives here.
  - AcpClient is optional (tests can pass None to exercise bridge logic only).
  - agent_card_loader is injected (same instance used by the bridge so cards
    are not loaded twice).

Usage example (sketch):
    river = InMemoryRiverStub()
    bridge = ExternalMemoryBridge(river, agent_card_loader=loader, sse_emitter=emit)
    gateway = HermesGateway(bridge=bridge, acp_client=acp_client)

    # Before calling ACP session/prompt:
    prompt_payload = await gateway.build_session_prompt(
        goal="Summarize the latest research",
        session_id="sess-abc",
        agent_id="hermes-01",
    )

    # When ACP session/update arrives:
    result = await gateway.handle_session_update(update_payload, "sess-abc", "hermes-01")
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from xueban.runtime.memory_bridge.bridge import ExternalMemoryBridge
from xueban.runtime.memory_bridge.types import MemoryFeedback, PourResult

logger = logging.getLogger("xueban.gateway.hermes")

# ACP session.update discriminator for memory proposals
_MEMORY_PROPOSAL_TYPE = "xueban_memory_proposal"


class HermesGateway:
    """Gateway adapter for Hermes external ACP agents.

    Args:
        bridge:     ExternalMemoryBridge instance (required).
        acp_client: AcpClient instance (optional in tests).
    """

    def __init__(
        self,
        bridge: ExternalMemoryBridge,
        acp_client: Optional[Any] = None,
    ) -> None:
        self._bridge = bridge
        self._acp_client = acp_client

    # ------------------------------------------------------------------
    # session.prompt construction hook (AC1 + AC3)
    # ------------------------------------------------------------------

    async def build_session_prompt(
        self,
        goal: str,
        session_id: str,
        agent_id: str,
        scope: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Build an ACP session.prompt payload with river context injected.

        Steps:
          1. Check for pending memory_feedback from last turn (AC3).
          2. Call bridge.drink() to obtain context fragment (AC1).
          3. Return assembled prompt payload.

        Returns:
            dict with keys:
              "prompt"            — the goal text
              "context_fragment"  — DrinkResult.to_acp_fragment() dict
              "xueban_envelope" — optional, contains "memory_feedback" if present
        """
        payload: Dict[str, Any] = {"prompt": goal}

        # AC3: inject last-turn feedback (take-and-clear)
        feedback: Optional[MemoryFeedback] = self._bridge.get_feedback(session_id)
        if feedback is not None:
            payload["xueban_envelope"] = {
                "memory_feedback": {
                    "accepted": [
                        {"candidate_id": i.candidate_id, "settled_at_layer": i.settled_at_layer}
                        for i in feedback.accepted
                    ],
                    "rejected": [
                        {"candidate_id": i.candidate_id, "reason": i.reason}
                        for i in feedback.rejected
                    ],
                    "deferred": [
                        {"candidate_id": i.candidate_id, "reason": i.reason}
                        for i in feedback.deferred
                    ],
                }
            }

        # AC1: drink context from river
        drink_result = await self._bridge.drink(
            query=goal,
            agent_id=agent_id,
            session_id=session_id,
            scope=scope,
        )
        payload["context_fragment"] = drink_result.to_acp_fragment()

        return payload

    # ------------------------------------------------------------------
    # session.update routing hook (AC2)
    # ------------------------------------------------------------------

    async def handle_session_update(
        self,
        update: Dict[str, Any],
        session_id: str,
        agent_id: str,
    ) -> Optional[PourResult]:
        """Route ACP session.update messages.

        If the update contains type="xueban_memory_proposal", delegates to
        bridge.pour() and returns the PourResult.

        For all other update types, logs and returns None (caller forwards to
        existing ACP update handler chain).

        Args:
            update:     The ACP session.update payload (the "update" sub-dict).
            session_id: Current ACP session ID.
            agent_id:   External agent identifier.

        Returns:
            PourResult if a memory proposal was processed, otherwise None.
        """
        # H4 fix: ACP spec uses "type" as the discriminator. The undocumented
        # "sessionUpdate" fallback risked false positives if an unrelated
        # payload happened to carry that key truthy. Stick to the spec.
        update_type = update.get("type")
        if update_type != _MEMORY_PROPOSAL_TYPE:
            logger.debug(
                "HermesGateway: non-memory update type=%s; passing through",
                update_type,
            )
            return None

        candidates: List[Dict[str, Any]] = update.get("candidates", [])
        if not isinstance(candidates, list) or len(candidates) == 0:
            logger.warning(
                "HermesGateway: xueban_memory_proposal missing or empty 'candidates' "
                "agent=%s session=%s",
                agent_id,
                session_id,
            )
            return None

        pour_result = await self._bridge.pour(
            candidates=candidates,
            agent_id=agent_id,
            session_id=session_id,
        )
        return pour_result
