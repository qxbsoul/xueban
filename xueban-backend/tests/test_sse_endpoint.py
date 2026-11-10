"""Story 2.6 / Story 4.1 — SSE endpoint /workflow/runs/{id}/events 测试。

Strategy: test SSE formatting via format_sse_event directly, and
test endpoint existence + headers via TestClient on pre-closed runs.
Full streaming integration is covered by test_events_bus.py (bus logic).
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from xueban.runtime.contracts import AgentEvent
from xueban.runtime.events import (
    RunEventBus,
    format_sse_event,
    AgentEventType,
    NODE_STARTED,
    NODE_SUCCEEDED,
    RUN_COMPLETED,
)


# ---------------------------------------------------------------------------
# format_sse_event unit tests (AC2 SSE format)
# ---------------------------------------------------------------------------

def _event(run_id: str = "r1", type_: str = "agent.completed") -> AgentEvent:
    return AgentEvent(run_id=run_id, node_id="n1", agent_id="a1", type=type_)


class TestFormatSseEvent:
    def test_id_line_present(self):
        chunk = format_sse_event(7, _event())
        assert "id: 7\n" in chunk

    def test_event_line_matches_type(self):
        chunk = format_sse_event(0, _event(type_="agent.thinking"))
        assert "event: agent.thinking\n" in chunk

    def test_data_line_is_valid_json(self):
        evt = _event(type_="agent.completed")
        chunk = format_sse_event(0, evt)
        data_line = next(l for l in chunk.splitlines() if l.startswith("data: "))
        payload = json.loads(data_line[len("data: "):])
        assert payload["run_id"] == "r1"

    def test_chunk_ends_with_double_newline(self):
        chunk = format_sse_event(0, _event())
        assert chunk.endswith("\n\n")

    def test_seq_0_formatted(self):
        chunk = format_sse_event(0, _event())
        assert "id: 0" in chunk

    def test_last_event_id_for_reconnect(self):
        """Verify seq=5 produces id:5 that can be used as Last-Event-ID."""
        chunk = format_sse_event(5, _event())
        lines = chunk.strip().split("\n")
        assert lines[0] == "id: 5"


# ---------------------------------------------------------------------------
# SSE endpoint — headers and 200 OK (via TestClient)
# ---------------------------------------------------------------------------

class TestSseEndpointHeaders:
    def test_sse_endpoint_returns_200_for_closed_run(self):
        from xueban.server import run_event_bus as bus
        run_id = "hdr-test-1"
        bus.close_run(run_id)

        with TestClient(app=__import__("xueban.server", fromlist=["app"]).app) as client:
            # Use a short timeout — endpoint terminates after draining closed run
            response = client.get(
                f"/workflow/runs/{run_id}/events",
                timeout=5,
            )
        assert response.status_code == 200

    def test_sse_endpoint_content_type_is_event_stream(self):
        from xueban.server import run_event_bus as bus, app
        run_id = "hdr-test-2"
        bus.close_run(run_id)

        with TestClient(app=app) as client:
            response = client.get(f"/workflow/runs/{run_id}/events", timeout=5)
        assert "text/event-stream" in response.headers.get("content-type", "")

    def test_sse_endpoint_delivers_pre_published_events(self):
        from xueban.server import run_event_bus as bus, app

        run_id = "content-test-1"
        bus.publish(run_id, AgentEvent(run_id=run_id, node_id="n1", agent_id="a1", type="agent.thinking"))
        bus.publish(run_id, AgentEvent(run_id=run_id, node_id="n1", agent_id="a1", type="agent.completed"))
        bus.close_run(run_id)

        with TestClient(app=app) as client:
            response = client.get(f"/workflow/runs/{run_id}/events", timeout=5)

        body = response.text
        assert "agent.thinking" in body
        assert "agent.completed" in body

    def test_sse_last_event_id_reconnection(self):
        """Reconnecting with Last-Event-ID skips already-received events."""
        from xueban.server import run_event_bus as bus, app

        run_id = "reconnect-test-1"
        bus.publish(run_id, AgentEvent(run_id=run_id, node_id="n1", agent_id="a1", type="agent.thinking"))    # seq 0
        bus.publish(run_id, AgentEvent(run_id=run_id, node_id="n1", agent_id="a1", type="agent.tool_called")) # seq 1
        bus.publish(run_id, AgentEvent(run_id=run_id, node_id="n1", agent_id="a1", type="agent.completed"))   # seq 2
        bus.close_run(run_id)

        with TestClient(app=app) as client:
            response = client.get(
                f"/workflow/runs/{run_id}/events",
                headers={"Last-Event-ID": "0"},
                timeout=5,
            )

        body = response.text
        # seq 1 and 2 should appear
        assert "agent.tool_called" in body
        assert "agent.completed" in body
        # seq 0 thinking should NOT appear (already seen)
        # We verify seq 0 line is not present
        assert "id: 0\n" not in body


# ---------------------------------------------------------------------------
# Story 4.1 — dict lifecycle events delivered via SSE
# ---------------------------------------------------------------------------

class TestSseNodeLifecycleEvents:
    def test_node_started_event_delivered_as_sse(self):
        """Node lifecycle dict events must be formatted and delivered correctly."""
        from xueban.server import run_event_bus as bus, app

        run_id = "lifecycle-test-1"
        bus.publish_node_event(run_id, NODE_STARTED, "node-A", {"node_type": "agent", "step_id": "step-001"})
        bus.publish_node_event(run_id, NODE_SUCCEEDED, "node-A", {"step_id": "step-001", "output_summary": "done"})
        bus.publish_node_event(run_id, RUN_COMPLETED, "", {"status": "succeeded", "errors": []})
        bus.close_run(run_id)

        with TestClient(app=app) as client:
            response = client.get(f"/workflow/runs/{run_id}/events", timeout=5)

        body = response.text
        assert response.status_code == 200
        assert f"event: {NODE_STARTED}" in body
        assert f"event: {NODE_SUCCEEDED}" in body
        assert f"event: {RUN_COMPLETED}" in body

    def test_sse_cache_control_headers_present(self):
        """Story 4.1 AC1: Cache-Control and X-Accel-Buffering headers must be set."""
        from xueban.server import run_event_bus as bus, app

        run_id = "headers-test-4-1"
        bus.close_run(run_id)

        with TestClient(app=app) as client:
            response = client.get(f"/workflow/runs/{run_id}/events", timeout=5)

        assert response.headers.get("cache-control") == "no-cache"
        assert response.headers.get("x-accel-buffering") == "no"

    def test_multiple_clients_get_independent_queues(self):
        """Story 4.1 AC1: Per-run isolation — run-A events must not appear in run-B stream."""
        from xueban.server import run_event_bus as bus, app

        run_a, run_b = "iso-run-A", "iso-run-B"
        bus.publish_node_event(run_a, NODE_STARTED, "n1", {"node_type": "agent"})
        bus.close_run(run_a)
        bus.publish_node_event(run_b, NODE_SUCCEEDED, "n2", {"step_id": "s"})
        bus.close_run(run_b)

        with TestClient(app=app) as client:
            resp_a = client.get(f"/workflow/runs/{run_a}/events", timeout=5)
            resp_b = client.get(f"/workflow/runs/{run_b}/events", timeout=5)

        assert NODE_STARTED in resp_a.text
        assert NODE_STARTED not in resp_b.text
        assert NODE_SUCCEEDED in resp_b.text
        assert NODE_SUCCEEDED not in resp_a.text
