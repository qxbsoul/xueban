"""ContextBuilder tests — Story 9.3 AC2/AC3 + AC6.

Covers:
  - writeback() respects WritebackPolicy (always / on_task_complete / manual).
  - writeback() skips on_session_end when trigger=on_task_complete.
  - compress_if_needed() returns unchanged context when under budget.
  - compress_if_needed() applies select_top_k correctly.
  - compress_if_needed() falls back to select_top_k when summarize has no LLM.
  - compress_if_needed() calls LLM.generate() for summarize policy.
  - MEMORY_COMPRESSED event published via event_bus on compression.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xueban.memory.memory_profile import (
    CompressionPolicy,
    MemoryProfile,
    WritebackPolicy,
)
from xueban.runtime.context_builder import MEMORY_COMPRESSED, ContextBuilder


# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------

@dataclass
class _FakeInteraction:
    output: str = "past output"
    user_id: str = "agent-1"
    session_id: str = "run-1"


class _FakeSessionMemory:
    def __init__(self):
        self.saved: List[Any] = []

    async def get_recent(self, user_id: str, limit: int = 10) -> List[_FakeInteraction]:
        return [_FakeInteraction(output="prev output")]

    async def save(self, interaction: Any) -> None:
        self.saved.append(interaction)


class _FakeGlobalMemory:
    def __init__(self):
        self.saved: Dict[str, Any] = {}

    async def search(self, query: str, limit: int = 5) -> List[str]:
        return []

    async def save(self, key: str, value: Any) -> None:
        self.saved[key] = value


class _FakeUserMemory:
    def __init__(self):
        self.saved: List[Any] = []

    async def search(self, query: str, limit: int = 5) -> List[str]:
        return []

    async def save(self, interaction: Any) -> None:
        self.saved.append(interaction)


class _FakeLLMProvider:
    def __init__(self, response_content: str = "summary text"):
        self._content = response_content
        self.called_with: List[str] = []

    async def generate(self, prompt: str, **kwargs) -> Any:
        self.called_with.append(prompt)

        class _Resp:
            content = self._content
            tokens_used = 42

        return _Resp()


class _FakeEventBus:
    def __init__(self):
        self.events: List[Dict[str, Any]] = []

    def publish_node_event(self, run_id: str, event_type: str, node_id: str, payload: Any) -> int:
        self.events.append({"run_id": run_id, "type": event_type, "payload": payload})
        return len(self.events)


def _make_event(category: str, event_id: str = "e1", summary: str = "ok"):
    class _Evt:
        pass

    e = _Evt()
    e.category = category
    e.event_id = event_id
    e.summary = summary
    e.payload = {}
    return e


def _make_context(n: int, chars_each: int = 20) -> List[Dict[str, Any]]:
    return [{"role": "assistant", "content": "x" * chars_each, "layer": "working"} for _ in range(n)]


# ---------------------------------------------------------------------------
# Tests: writeback policy
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_writeback_always_fires():
    profile = MemoryProfile(writeback_policy=WritebackPolicy.ALWAYS)
    session = _FakeSessionMemory()
    builder = ContextBuilder(profile, session_memory=session)
    await builder.writeback("run-1", "agent-1", [], trigger="on_task_complete")
    assert len(session.saved) == 1


@pytest.mark.asyncio
async def test_writeback_on_task_complete_fires_with_correct_trigger():
    profile = MemoryProfile(writeback_policy=WritebackPolicy.ON_TASK_COMPLETE)
    session = _FakeSessionMemory()
    builder = ContextBuilder(profile, session_memory=session)
    await builder.writeback("run-1", "agent-1", [], trigger="on_task_complete")
    assert len(session.saved) == 1


@pytest.mark.asyncio
async def test_writeback_on_task_complete_skips_wrong_trigger():
    profile = MemoryProfile(writeback_policy=WritebackPolicy.ON_TASK_COMPLETE)
    session = _FakeSessionMemory()
    builder = ContextBuilder(profile, session_memory=session)
    await builder.writeback("run-1", "agent-1", [], trigger="on_session_end")
    assert len(session.saved) == 0


@pytest.mark.asyncio
async def test_writeback_manual_never_auto_fires():
    profile = MemoryProfile(writeback_policy=WritebackPolicy.MANUAL)
    session = _FakeSessionMemory()
    builder = ContextBuilder(profile, session_memory=session)
    for trigger in ("always", "on_task_complete", "on_session_end"):
        await builder.writeback("run-1", "agent-1", [], trigger=trigger)
    assert len(session.saved) == 0


@pytest.mark.asyncio
async def test_writeback_episodic_writes_key_events():
    profile = MemoryProfile(writeback_policy=WritebackPolicy.ALWAYS)
    global_mem = _FakeGlobalMemory()
    builder = ContextBuilder(profile, global_memory=global_mem)
    events = [_make_event("artifact"), _make_event("step_result")]
    await builder.writeback("run-1", "agent-1", events, trigger="always")
    # Only artifact events trigger episodic writeback
    assert any("artifact" in k or "run-1" in k for k in global_mem.saved)


@pytest.mark.asyncio
async def test_writeback_semantic_writes_feedback_events():
    profile = MemoryProfile(writeback_policy=WritebackPolicy.ALWAYS)
    user_mem = _FakeUserMemory()
    builder = ContextBuilder(profile, user_memory=user_mem)
    events = [_make_event("feedback_signal")]
    await builder.writeback("run-1", "agent-1", events, trigger="always")
    assert len(user_mem.saved) == 1


# ---------------------------------------------------------------------------
# Tests: compress_if_needed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compress_no_op_when_under_budget():
    profile = MemoryProfile(working_memory_limit=4096, compression_policy=CompressionPolicy.NONE)
    builder = ContextBuilder(profile)
    ctx = _make_context(5, chars_each=20)  # 5*20=100 chars / 4 = 25 tokens
    result, meta = await builder.compress_if_needed(ctx, run_id="r1")
    assert result == ctx
    assert meta == {}


@pytest.mark.asyncio
async def test_compress_none_policy_over_budget_warns():
    # 10 items * 2000 chars / 4 = 5000 tokens > limit 256
    profile = MemoryProfile(working_memory_limit=256, compression_policy=CompressionPolicy.NONE)
    builder = ContextBuilder(profile)
    ctx = _make_context(10, chars_each=2000)
    result, meta = await builder.compress_if_needed(ctx, run_id="r1")
    assert result == ctx
    assert meta.get("overbudget") is True


@pytest.mark.asyncio
async def test_compress_select_top_k():
    profile = MemoryProfile(
        working_memory_limit=256,
        compression_policy=CompressionPolicy.SELECT_TOP_K,
        compression_top_k=3,  # compression uses compression_top_k, not semantic_retrieval_top_k
    )
    builder = ContextBuilder(profile)
    ctx = _make_context(10, chars_each=200)
    result, meta = await builder.compress_if_needed(ctx, run_id="r1")
    assert len(result) == 3
    assert result == ctx[-3:]
    assert meta["policy"] == "select_top_k"


@pytest.mark.asyncio
async def test_compress_summarize_falls_back_when_no_llm():
    profile = MemoryProfile(
        working_memory_limit=256,
        compression_policy=CompressionPolicy.SUMMARIZE,
        compression_top_k=2,  # fallback uses compression_top_k
    )
    builder = ContextBuilder(profile)  # no llm_provider
    ctx = _make_context(10, chars_each=200)
    result, meta = await builder.compress_if_needed(ctx, run_id="r1")
    assert len(result) == 2
    assert "fallback" in meta["policy"]


@pytest.mark.asyncio
async def test_compress_summarize_calls_llm():
    profile = MemoryProfile(
        working_memory_limit=256,
        compression_policy=CompressionPolicy.SUMMARIZE,
        semantic_retrieval_top_k=2,
    )
    llm = _FakeLLMProvider(response_content="this is a summary")
    bus = _FakeEventBus()
    builder = ContextBuilder(profile, llm_provider=llm, event_bus=bus)
    ctx = _make_context(10, chars_each=200)
    result, meta = await builder.compress_if_needed(ctx, run_id="r1")
    assert len(llm.called_with) == 1
    assert "summary" in result[0]["content"]
    assert meta["policy"] == "summarize"
    assert meta["llm_tokens_used"] == 42


@pytest.mark.asyncio
async def test_compress_publishes_memory_compressed_event():
    profile = MemoryProfile(
        working_memory_limit=256,
        compression_policy=CompressionPolicy.SELECT_TOP_K,
        semantic_retrieval_top_k=2,
    )
    bus = _FakeEventBus()
    builder = ContextBuilder(profile, event_bus=bus)
    ctx = _make_context(10, chars_each=200)
    await builder.compress_if_needed(ctx, run_id="run-abc")
    assert any(e["type"] == MEMORY_COMPRESSED for e in bus.events)
    compressed_evt = next(e for e in bus.events if e["type"] == MEMORY_COMPRESSED)
    assert compressed_evt["run_id"] == "run-abc"
