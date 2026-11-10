"""Story 2.6 / Story 4.1 — RunEventBus 发布 / 订阅 + Last-Event-ID 断点续传 + ring buffer + 新常量测试。"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from xueban.runtime.contracts import AgentEvent
from xueban.runtime.events import (
    AgentGapDetectedEvent,
    RunEventBus,
    format_sse_event,
    GAP_DETECTED,
    NODE_STARTED,
    NODE_SUCCEEDED,
    NODE_FAILED,
    NODE_REJECTED,
    POLICY_VIOLATION,
    POLICY_UPDATED,
    RUN_COMPLETED,
    _RING_BUFFER_MAX,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _event(run_id: str, type_: str) -> AgentEvent:
    return AgentEvent(run_id=run_id, node_id="n1", agent_id="a1", type=type_)


# ---------------------------------------------------------------------------
# publish / get_events
# ---------------------------------------------------------------------------

class TestRunEventBusPublish:
    def test_publish_returns_seq_0_first(self):
        bus = RunEventBus()
        evt = _event("r1", "agent.output")
        seq = bus.publish("r1", evt)
        assert seq == 0

    def test_publish_increments_seq(self):
        bus = RunEventBus()
        seq0 = bus.publish("r1", _event("r1", "agent.thinking"))
        seq1 = bus.publish("r1", _event("r1", "agent.completed"))
        assert seq1 == seq0 + 1

    def test_get_events_returns_all(self):
        bus = RunEventBus()
        bus.publish("r1", _event("r1", "agent.thinking"))
        bus.publish("r1", _event("r1", "agent.completed"))
        events = bus.get_events("r1")
        assert len(events) == 2

    def test_get_events_from_seq_filters(self):
        bus = RunEventBus()
        bus.publish("r1", _event("r1", "agent.thinking"))     # seq 0
        bus.publish("r1", _event("r1", "agent.tool_called"))  # seq 1
        bus.publish("r1", _event("r1", "agent.completed"))    # seq 2
        events = bus.get_events("r1", from_seq=1)
        assert len(events) == 2
        assert events[0][0] == 1

    def test_separate_runs_isolated(self):
        bus = RunEventBus()
        bus.publish("run-A", _event("run-A", "agent.output"))
        bus.publish("run-B", _event("run-B", "agent.output"))
        assert len(bus.get_events("run-A")) == 1
        assert len(bus.get_events("run-B")) == 1

    def test_latest_seq_none_when_empty(self):
        bus = RunEventBus()
        assert bus.latest_seq("no-such-run") is None

    def test_latest_seq_after_publish(self):
        bus = RunEventBus()
        bus.publish("r1", _event("r1", "agent.output"))
        bus.publish("r1", _event("r1", "agent.completed"))
        assert bus.latest_seq("r1") == 1


# ---------------------------------------------------------------------------
# subscribe — basic iteration
# ---------------------------------------------------------------------------

class TestRunEventBusSubscribe:
    @pytest.mark.asyncio
    async def test_subscribe_drains_existing_events(self):
        bus = RunEventBus()
        bus.publish("r1", _event("r1", "agent.thinking"))
        bus.publish("r1", _event("r1", "agent.completed"))
        bus.close_run("r1")

        pairs = [(s, e) async for s, e in bus.subscribe("r1")]
        assert len(pairs) == 2
        assert pairs[0][0] == 0
        assert pairs[1][0] == 1

    @pytest.mark.asyncio
    async def test_subscribe_receives_late_publish(self):
        bus = RunEventBus()

        async def producer():
            await asyncio.sleep(0.01)
            bus.publish("r1", _event("r1", "agent.thinking"))
            bus.close_run("r1")

        asyncio.create_task(producer())
        pairs = [(s, e) async for s, e in bus.subscribe("r1")]
        assert len(pairs) == 1
        assert pairs[0][1].type == "agent.thinking"

    @pytest.mark.asyncio
    async def test_subscribe_with_last_seq_skips_earlier(self):
        bus = RunEventBus()
        bus.publish("r1", _event("r1", "agent.thinking"))    # seq 0
        bus.publish("r1", _event("r1", "agent.tool_called")) # seq 1
        bus.publish("r1", _event("r1", "agent.completed"))   # seq 2
        bus.close_run("r1")

        # Resume from seq 0 → should only see seq 1 and 2
        pairs = [(s, e) async for s, e in bus.subscribe("r1", last_seq=0)]
        seqs = [s for s, _ in pairs]
        assert seqs == [1, 2]


# ---------------------------------------------------------------------------
# close_run
# ---------------------------------------------------------------------------

class TestRunEventBusCloseRun:
    @pytest.mark.asyncio
    async def test_close_run_terminates_subscriber(self):
        bus = RunEventBus()
        bus.close_run("r-empty")  # close before any publish
        collected = [(s, e) async for s, e in bus.subscribe("r-empty")]
        assert collected == []

    @pytest.mark.asyncio
    async def test_close_run_after_publish_drains_then_exits(self):
        bus = RunEventBus()
        bus.publish("r1", _event("r1", "agent.output"))
        bus.close_run("r1")
        pairs = [(s, e) async for s, e in bus.subscribe("r1")]
        assert len(pairs) == 1


# ---------------------------------------------------------------------------
# format_sse_event
# ---------------------------------------------------------------------------

class TestFormatSseEvent:
    def test_sse_format_contains_id_and_event(self):
        evt = _event("r1", "agent.completed")
        chunk = format_sse_event(42, evt)
        assert "id: 42" in chunk
        assert "event: agent.completed" in chunk
        assert "data: " in chunk
        assert chunk.endswith("\n\n")

    def test_sse_format_contains_run_id_in_data(self):
        evt = _event("my-run", "agent.completed")
        chunk = format_sse_event(0, evt)
        assert "my-run" in chunk

    def test_sse_format_accepts_dict_event(self):
        """Story 4.1: format_sse_event must handle plain dict lifecycle events."""
        evt = {"type": NODE_STARTED, "run_id": "r1", "node_id": "n1", "node_type": "agent"}
        chunk = format_sse_event(3, evt)
        assert "id: 3" in chunk
        assert f"event: {NODE_STARTED}" in chunk
        assert chunk.endswith("\n\n")


# ---------------------------------------------------------------------------
# Story 4.1 — new event type constants
# ---------------------------------------------------------------------------

class TestEventTypeConstants:
    def test_node_lifecycle_constants_defined(self):
        assert NODE_STARTED == "node.started"
        assert NODE_SUCCEEDED == "node.succeeded"
        assert NODE_FAILED == "node.failed"
        assert NODE_REJECTED == "node.rejected"

    def test_policy_constants_defined(self):
        assert POLICY_VIOLATION == "policy.violation"
        assert POLICY_UPDATED == "policy.updated"

    def test_run_completed_defined(self):
        assert RUN_COMPLETED == "run.completed"

    def test_gap_detected_defined(self):
        assert GAP_DETECTED == "agent.gap_detected"


# ---------------------------------------------------------------------------
# Story 4.1 — ring buffer (maxlen = 1000)
# ---------------------------------------------------------------------------

class TestRingBuffer:
    def test_ring_buffer_max_constant(self):
        assert _RING_BUFFER_MAX == 1000

    def test_ring_buffer_evicts_oldest_when_full(self):
        bus = RunEventBus()
        # Publish exactly maxlen + 1 events
        for i in range(_RING_BUFFER_MAX + 1):
            bus.publish("r1", {"type": "node.started", "i": i})
        # Only the last 1000 should be buffered
        events = bus.get_events("r1")
        assert len(events) == _RING_BUFFER_MAX
        # The oldest event (seq 0) should have been evicted
        seqs = [s for s, _ in events]
        assert 0 not in seqs
        # The newest event (seq 1000) should be present
        assert 1000 in seqs

    def test_ring_buffer_seq_monotonic_regardless_of_eviction(self):
        bus = RunEventBus()
        for i in range(_RING_BUFFER_MAX + 50):
            bus.publish("r1", {"type": "x", "i": i})
        events = bus.get_events("r1")
        seqs = [s for s, _ in events]
        assert seqs == sorted(seqs)


# ---------------------------------------------------------------------------
# Story 4.1 — publish_node_event convenience wrapper
# ---------------------------------------------------------------------------

class TestPublishNodeEvent:
    def test_publish_node_event_stores_dict_with_type(self):
        bus = RunEventBus()
        bus.publish_node_event("r1", NODE_STARTED, "node-A", {"step_id": "step-001"})
        events = bus.get_events("r1")
        assert len(events) == 1
        seq, evt = events[0]
        assert isinstance(evt, dict)
        assert evt["type"] == NODE_STARTED
        assert evt["node_id"] == "node-A"
        assert evt["step_id"] == "step-001"

    def test_publish_node_event_returns_seq(self):
        bus = RunEventBus()
        seq = bus.publish_node_event("r1", NODE_SUCCEEDED, "node-B")
        assert seq == 0


class TestGapDetectedEvent:
    def test_gap_event_formats_as_named_sse_event(self):
        event = AgentGapDetectedEvent(
            run_id="run-gap",
            node_id="writer",
            gap_type="incomplete_log",
            description="实验日志缺少 baseline 数据。",
            choices=[
                {"id": "A", "label": "补充数据", "action": "pause"},
                {"id": "B", "label": "从论文移除此对比", "action": "drop"},
                {"id": "C", "label": "注释为 'will be updated'", "action": "annotate"},
            ],
        )
        chunk = format_sse_event(9, event)
        assert "event: agent.gap_detected" in chunk
        assert "baseline" in chunk


# ---------------------------------------------------------------------------
# purge_run — memory cleanup (Story 2.6 review patch)
# ---------------------------------------------------------------------------

class TestPurgeRun:
    def test_purge_removes_all_state(self):
        bus = RunEventBus()
        bus.publish("r1", _event("r1", "agent.output"))
        bus.close_run("r1")
        bus.purge_run("r1")
        assert bus.get_events("r1") == []
        assert bus.latest_seq("r1") is None

    def test_purge_nonexistent_run_is_noop(self):
        bus = RunEventBus()
        bus.purge_run("no-such-run")

    def test_purge_allows_reuse_of_run_id(self):
        bus = RunEventBus()
        bus.publish("r1", _event("r1", "agent.thinking"))
        bus.close_run("r1")
        bus.purge_run("r1")
        seq = bus.publish("r1", _event("r1", "agent.completed"))
        assert seq == 0
        assert len(bus.get_events("r1")) == 1


# ---------------------------------------------------------------------------
# Story 4.1 — parallel publish (P5 zero-contention)
# ---------------------------------------------------------------------------

class TestParallelPublish:
    @pytest.mark.asyncio
    async def test_three_concurrent_publishers_no_loss(self):
        bus = RunEventBus()

        async def producer(node_id: str, count: int):
            for i in range(count):
                bus.publish_node_event("r-par", NODE_STARTED, node_id, {"i": i})
                await asyncio.sleep(0)

        await asyncio.gather(
            producer("A", 10),
            producer("B", 10),
            producer("C", 10),
        )
        events = bus.get_events("r-par")
        assert len(events) == 30
        # All sequence numbers are unique
        seqs = [s for s, _ in events]
        assert len(set(seqs)) == 30
