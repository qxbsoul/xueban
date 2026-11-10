"""ContextBuilder — Story 9.3 AC2/AC3.

Coordinates three-layer memory writeback and working-memory compression.
Designed as a thin coordination layer; does NOT modify existing memory APIs.

Layer mapping (Story 9.3 AC2):
  Working Memory  → SessionMemory  (xueban.memory.session)
  Episodic Memory → GlobalMemory   (xueban.memory.global_memory)
  Semantic Memory → UserMemory     (xueban.memory.user)
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from xueban.memory.memory_profile import CompressionPolicy, MemoryProfile, WritebackPolicy
from xueban.runtime.state_service import StateConflict, get_service as _get_state_service

logger = logging.getLogger(__name__)

# SSE event name (AC3)
MEMORY_COMPRESSED = "memory.compressed"

# Episodic categories that trigger episodic memory writeback
_EPISODIC_CATEGORIES = frozenset({"artifact", "feedback_signal"})
# Payload markers that indicate approval/rejection events
_APPROVAL_CATEGORIES = frozenset({"approval", "rejection"})


class ContextBuilder:
    """Coordinate three-layer memory for a single agent.

    Args:
        profile:        MemoryProfile for this agent.
        session_memory: Working memory (SessionMemory instance).
        global_memory:  Episodic memory (GlobalMemory instance).
        user_memory:    Semantic memory (UserMemory instance).
        llm_provider:   LLM provider for "summarize" compression (optional).
        event_bus:      RunEventBus instance for SSE events (optional).
    """

    def __init__(
        self,
        profile: MemoryProfile,
        *,
        session_memory: Optional[Any] = None,
        global_memory: Optional[Any] = None,
        user_memory: Optional[Any] = None,
        llm_provider: Optional[Any] = None,
        event_bus: Optional[Any] = None,
    ) -> None:
        self._profile = profile
        self._session = session_memory
        self._global = global_memory
        self._user = user_memory
        self._llm = llm_provider
        self._event_bus = event_bus
        self._last_compression_summary: str = ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def build_context(self, run_id: str, agent_id: str) -> List[Dict[str, Any]]:
        """Assemble inference context from all three memory layers.

        Returns a list of dicts suitable for passing to an LLM as messages.
        """
        context: List[Dict[str, Any]] = []

        # 1. Working memory — recent session interactions
        if self._session is not None:
            try:
                recent = await self._session.get_recent(agent_id, limit=20)
                for interaction in recent:
                    context.append({
                        "role": "assistant",
                        "content": str(interaction.output) if hasattr(interaction, "output") else str(interaction),
                        "layer": "working",
                    })
            except Exception:
                logger.warning("context_builder: working memory read failed", exc_info=True)

        # 2. Semantic memory — user preferences / rules
        if self._user is not None:
            try:
                semantic_items = await self._user.search(agent_id, limit=self._profile.semantic_retrieval_top_k)
                for item in semantic_items:
                    context.append({
                        "role": "system",
                        "content": str(item),
                        "layer": "semantic",
                    })
            except Exception:
                logger.warning("context_builder: semantic memory read failed", exc_info=True)

        # 3. Episodic memory — key past events / patterns
        if self._global is not None:
            try:
                patterns = await self._global.search(f"agent:{agent_id}", limit=5)
                for pattern in patterns:
                    context.append({
                        "role": "system",
                        "content": str(pattern),
                        "layer": "episodic",
                    })
            except Exception:
                logger.warning("context_builder: episodic memory read failed", exc_info=True)

        return context

    async def writeback(
        self,
        run_id: str,
        agent_id: str,
        events: List[Any],
        *,
        trigger: str = "always",
    ) -> None:
        """Write back to the appropriate memory layers according to policy.

        Args:
            run_id:   Run identifier.
            agent_id: Agent identifier.
            events:   MemoryEvent list from the run result.
            trigger:  One of "always" | "on_task_complete" | "on_session_end" | "manual".
                      Used to evaluate whether the configured writeback_policy fires.
        """
        policy = self._profile.writeback_policy

        if policy == WritebackPolicy.MANUAL:
            # Manual policy: skip auto-writeback
            return

        should_write = (
            policy == WritebackPolicy.ALWAYS
            or (policy == WritebackPolicy.ON_TASK_COMPLETE and trigger == "on_task_complete")
            or (policy == WritebackPolicy.ON_SESSION_END and trigger == "on_session_end")
        )

        if not should_write:
            return

        await self._writeback_working(run_id, agent_id, events)
        await self._writeback_episodic(run_id, agent_id, events)
        await self._writeback_semantic(run_id, agent_id, events)
        await self._writeback_state(run_id, agent_id, events)

    async def compress_if_needed(
        self,
        context: List[Dict[str, Any]],
        run_id: str = "",
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Apply compression policy if context exceeds working_memory_limit.

        Returns (compressed_context, compression_metadata) where metadata is
        suitable for StepRecord.metadata["memory_compression"].
        """
        profile = self._profile
        token_estimate = self._estimate_tokens(context)

        if token_estimate <= profile.working_memory_limit:
            return context, {}

        policy = profile.compression_policy
        t0 = time.monotonic()

        if policy == CompressionPolicy.NONE:
            logger.warning(
                "context_builder: context (%d tokens) exceeds limit (%d); compression=none",
                token_estimate,
                profile.working_memory_limit,
            )
            return context, {
                "policy": "none",
                "original_tokens": token_estimate,
                "compressed_tokens": token_estimate,
                "overbudget": True,
            }

        if policy == CompressionPolicy.SELECT_TOP_K:
            # Use compression_top_k (not semantic_retrieval_top_k — different semantics).
            k = profile.compression_top_k
            compressed = context[-k:] if len(context) > k else context
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            await self._emit_compressed_event(run_id, token_estimate, self._estimate_tokens(compressed), elapsed_ms)
            return compressed, {
                "policy": "select_top_k",
                "k": k,
                "original_tokens": token_estimate,
                "compressed_tokens": self._estimate_tokens(compressed),
                "elapsed_ms": elapsed_ms,
            }

        if policy == CompressionPolicy.SUMMARIZE:
            return await self._compress_summarize(context, run_id, token_estimate, t0)

        return context, {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _writeback_working(self, run_id: str, agent_id: str, events: List[Any]) -> None:
        """Write run summary to working (session) memory.

        Uses the compression summary when available (AC3 requirement: compressed
        context is written back to session memory so subsequent builds see
        the summarised history rather than the raw interaction log).
        """
        if self._session is None:
            return
        try:
            from xueban.memory.base import Interaction

            # Prefer the compression summary (set by compress_if_needed) so that
            # session memory reflects the compacted context, not just a generic
            # "writeback completed" placeholder.
            summary = (
                self._last_compression_summary
                or f"writeback from run {run_id} ({len(events)} events)"
            )

            interaction = Interaction(
                id=f"wb-{uuid4().hex[:10]}",
                user_id=agent_id,
                agent_id=agent_id,
                session_id=run_id,
                input=f"run:{run_id}",
                output=summary,
                timestamp=datetime.now(timezone.utc),
            )
            await self._session.save(interaction)
        except Exception:
            logger.warning("context_builder: working memory writeback failed", exc_info=True)

    async def _writeback_episodic(self, run_id: str, agent_id: str, events: List[Any]) -> None:
        """Write key events (approval/artifact) to episodic (global) memory."""
        if self._global is None:
            return
        try:
            key_events = [
                e for e in events
                if getattr(e, "category", None) in _EPISODIC_CATEGORIES
                or getattr(e, "category", None) in _APPROVAL_CATEGORIES
            ]
            if not key_events:
                return
            for evt in key_events:
                await self._global.save(
                    key=f"agent:{agent_id}:run:{run_id}:{getattr(evt, 'event_id', uuid4().hex[:8])}",
                    value={
                        "run_id": run_id,
                        "agent_id": agent_id,
                        "category": getattr(evt, "category", "unknown"),
                        "summary": getattr(evt, "summary", ""),
                        "payload": getattr(evt, "payload", {}),
                    },
                )
        except Exception:
            logger.warning("context_builder: episodic memory writeback failed", exc_info=True)

    async def _writeback_semantic(self, run_id: str, agent_id: str, events: List[Any]) -> None:
        """Write confirmed preferences/facts to semantic (user) memory."""
        if self._user is None:
            return
        try:
            feedback_events = [
                e for e in events
                if getattr(e, "category", None) == "feedback_signal"
            ]
            if not feedback_events:
                return

            from xueban.memory.base import Interaction

            for evt in feedback_events:
                interaction = Interaction(
                    id=f"sem-{uuid4().hex[:10]}",
                    user_id=agent_id,
                    agent_id=agent_id,
                    session_id=run_id,
                    input=f"feedback:{run_id}",
                    output=getattr(evt, "summary", ""),
                    timestamp=datetime.now(timezone.utc),
                    metadata=getattr(evt, "payload", {}),
                )
                await self._user.save(interaction)
        except Exception:
            logger.warning("context_builder: semantic memory writeback failed", exc_info=True)

    async def _writeback_state(self, run_id: str, agent_id: str, events: List[Any]) -> None:
        """Update AgentState via StateService after writeback (AC5)."""
        try:
            state_svc = _get_state_service()
            current = state_svc.get_state(agent_id)
            current_version = current.state_version if current is not None else 0

            # Gather recent artifact names from events
            artifacts: List[str] = []
            pending_tasks: List[str] = []
            for evt in events:
                category = getattr(evt, "category", None)
                if category == "artifact":
                    name = str(getattr(evt, "summary", "") or getattr(evt, "payload", {}).get("name", ""))
                    if name:
                        artifacts.append(name)
                elif category == "task":
                    status = getattr(evt, "payload", {}).get("status", "")
                    if status not in ("done", "completed"):
                        desc = str(getattr(evt, "summary", ""))
                        if desc:
                            pending_tasks.append(desc)

            patch: Dict[str, Any] = {
                "version": current_version,
                "recent_artifacts": artifacts[-10:],
                "pending_tasks": pending_tasks,
                "last_writeback_at": datetime.now(timezone.utc),
            }

            # session_summary: use latest "compressed" context if available
            if hasattr(self, "_last_compression_summary") and self._last_compression_summary:
                patch["session_summary"] = self._last_compression_summary

            state_svc.update_state(agent_id, patch)
        except StateConflict:
            logger.warning(
                "context_builder: state writeback skipped (version conflict) agent_id=%s run_id=%s",
                agent_id, run_id,
            )
        except Exception:
            logger.warning("context_builder: state writeback failed", exc_info=True)

    async def _compress_summarize(
        self,
        context: List[Dict[str, Any]],
        run_id: str,
        original_tokens: int,
        t0: float,
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """LLM-based rolling summary compression (Gather→Select→Structure→Compress)."""
        if self._llm is None:
            logger.warning("context_builder: summarize requested but no LLM provider; falling back to select_top_k")
            k = self._profile.compression_top_k
            compressed = context[-k:]
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            await self._emit_compressed_event(run_id, original_tokens, self._estimate_tokens(compressed), elapsed_ms)
            return compressed, {
                "policy": "summarize_fallback_select_top_k",
                "original_tokens": original_tokens,
                "compressed_tokens": self._estimate_tokens(compressed),
                "elapsed_ms": elapsed_ms,
            }

        # Gather relevant context items
        content_parts = [
            item.get("content", "") for item in context
            if item.get("layer") in ("working", "episodic")
        ]
        text_to_summarize = "\n".join(content_parts)

        _SUMMARIZE_CHAR_LIMIT = 8000
        truncated = len(text_to_summarize) > _SUMMARIZE_CHAR_LIMIT
        if truncated:
            logger.warning(
                "context_builder: summarize truncating input from %d to %d chars for run_id=%s",
                len(text_to_summarize), _SUMMARIZE_CHAR_LIMIT, run_id,
            )
        text_to_summarize = text_to_summarize[:_SUMMARIZE_CHAR_LIMIT]

        prompt = (
            "Summarize the following agent memory context concisely, preserving key facts and decisions:\n\n"
            f"{text_to_summarize}"
        )

        try:
            response = await self._llm.generate(prompt, max_tokens=512)
            summary_text = response.content if hasattr(response, "content") else str(response)
            tokens_used = getattr(response, "tokens_used", 0)
            self._last_compression_summary = summary_text

            compressed = [
                {"role": "system", "content": summary_text, "layer": "compressed"},
                *[item for item in context if item.get("layer") == "semantic"],
            ]
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            await self._emit_compressed_event(run_id, original_tokens, self._estimate_tokens(compressed), elapsed_ms)

            return compressed, {
                "policy": "summarize",
                "original_tokens": original_tokens,
                "compressed_tokens": self._estimate_tokens(compressed),
                "llm_tokens_used": tokens_used,
                "elapsed_ms": elapsed_ms,
                "input_truncated": truncated,
            }
        except Exception:
            logger.warning("context_builder: LLM summarize failed; falling back to select_top_k", exc_info=True)
            k = self._profile.compression_top_k
            compressed = context[-k:]
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return compressed, {
                "policy": "summarize_error_fallback",
                "original_tokens": original_tokens,
                "compressed_tokens": self._estimate_tokens(compressed),
                "elapsed_ms": elapsed_ms,
            }

    async def _emit_compressed_event(
        self,
        run_id: str,
        original_tokens: int,
        compressed_tokens: int,
        elapsed_ms: int,
    ) -> None:
        """Publish memory.compressed SSE event via RunEventBus."""
        if self._event_bus is None or not run_id:
            return
        try:
            self._event_bus.publish_node_event(
                run_id,
                MEMORY_COMPRESSED,
                "",
                {
                    "original_tokens": original_tokens,
                    "compressed_tokens": compressed_tokens,
                    "elapsed_ms": elapsed_ms,
                    "profile_id": self._profile.profile_id,
                },
            )
        except Exception:
            logger.warning("context_builder: failed to publish memory.compressed event", exc_info=True)

    @staticmethod
    def _estimate_tokens(context: List[Dict[str, Any]]) -> int:
        """Rough token estimate.

        Uses different ratios for ASCII vs non-ASCII content:
          - ASCII:     ~4 chars/token (English average)
          - Non-ASCII: ~1.5 chars/token (conservative for CJK / emoji)
        """
        total = 0
        for item in context:
            content = item.get("content", "")
            ascii_chars = sum(1 for c in content if ord(c) < 128)
            non_ascii_chars = len(content) - ascii_chars
            total += ascii_chars // 4 + non_ascii_chars * 2 // 3
        return max(1, total)
