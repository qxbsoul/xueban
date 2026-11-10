"""Sessions API + WorkspaceContext tests — Story 11.5.

Covers:
  AC1 — AgentWorkspaceContext.add_step() persists step, resume_prompt() non-empty
  AC2 — WorkspaceContextStore: save/load/delete/TTL-expiry
  AC3 — POST /api/sessions/{sid}/checkpoint: creates checkpoint entry
  AC4 — GET  /api/sessions/{sid}/context: returns correct envelope
  AC5 — GET  /api/sessions/{sid}/report: in_progress when no report; returns report when set
  AC6 — DELETE /api/sessions/{sid}/context: clears context, 404 afterwards
  AC7 — 404 when session not found
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from xueban.runtime.workspace_context import (
    AgentWorkspaceContext,
    WorkspaceContextStore,
    get_store,
)
from xueban.api.sessions import set_report
from xueban.server import app

client = TestClient(app)

AGENT_ID = "test-agent-11-5"
SESSION_ID = "test-session-11-5"


# ---------------------------------------------------------------------------
# Fixture: fresh store per test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_store(monkeypatch):
    from collections import OrderedDict
    fresh = WorkspaceContextStore()
    import xueban.runtime.workspace_context as _wc_mod
    import xueban.api.sessions as _sessions_mod
    monkeypatch.setattr(_wc_mod, "_store", fresh)
    monkeypatch.setattr(_sessions_mod, "_report_cache", OrderedDict())
    yield fresh


# ---------------------------------------------------------------------------
# AC1 — AgentWorkspaceContext unit
# ---------------------------------------------------------------------------


class TestAgentWorkspaceContext:
    def test_add_step_increments(self):
        ctx = AgentWorkspaceContext(agent_id="a", session_id="s", task_description="t")
        ctx.add_step("bash", "output", "ran a command")
        assert len(ctx.completed_steps) == 1
        step = ctx.completed_steps[0]
        assert step.tool_called == "bash"
        assert step.index == 0
        assert step.status == "done"

    def test_add_step_truncates_result(self):
        ctx = AgentWorkspaceContext(agent_id="a", session_id="s", task_description="t")
        long_result = "x" * 5000
        ctx.add_step("tool", long_result)
        assert len(ctx.completed_steps[0].result_summary) <= 2000

    def test_resume_prompt_empty_when_no_steps(self):
        ctx = AgentWorkspaceContext(agent_id="a", session_id="s", task_description="t")
        assert ctx.resume_prompt() == ""

    def test_resume_prompt_non_empty_after_steps(self):
        ctx = AgentWorkspaceContext(agent_id="a", session_id="s", task_description="t")
        for i in range(3):
            ctx.add_step("bash", f"result {i}")
        prompt = ctx.resume_prompt()
        assert "bash" in prompt
        assert "3" in prompt

    def test_add_checkpoint_summary(self):
        ctx = AgentWorkspaceContext(agent_id="a", session_id="s", task_description="t")
        ctx.add_step("bash", "r1")
        ctx.add_step("bash", "r2")
        ctx.add_checkpoint_summary(0, "segment 0 done")
        assert len(ctx.checkpoint_summaries) == 1
        cp = ctx.checkpoint_summaries[0]
        assert cp.segment_index == 0
        assert cp.steps_in_segment == 2
        assert cp.summary == "segment 0 done"

    def test_checkpoint_summary_truncated(self):
        ctx = AgentWorkspaceContext(agent_id="a", session_id="s", task_description="t")
        ctx.add_step("bash", "r")
        ctx.add_checkpoint_summary(0, "x" * 300)
        assert len(ctx.checkpoint_summaries[0].summary) <= 200


# ---------------------------------------------------------------------------
# AC2 — WorkspaceContextStore
# ---------------------------------------------------------------------------


class TestWorkspaceContextStore:
    def test_save_and_load(self):
        store = WorkspaceContextStore()
        ctx = AgentWorkspaceContext(agent_id="a1", session_id="s1", task_description="task")
        store.save(ctx)
        loaded = store.load("a1", "s1")
        assert loaded is not None
        assert loaded.agent_id == "a1"

    def test_load_missing_returns_none(self):
        store = WorkspaceContextStore()
        assert store.load("no-agent", "no-session") is None

    def test_delete(self):
        store = WorkspaceContextStore()
        ctx = AgentWorkspaceContext(agent_id="a2", session_id="s2", task_description="t")
        store.save(ctx)
        result = store.delete("a2", "s2")
        assert result is True
        assert store.load("a2", "s2") is None

    def test_delete_missing_returns_false(self):
        store = WorkspaceContextStore()
        assert store.delete("ghost", "ghost") is False

    def test_ttl_expiry(self):
        store = WorkspaceContextStore()
        ctx = AgentWorkspaceContext(agent_id="a3", session_id="s3", task_description="t")
        store.save(ctx)
        key = store._key("a3", "s3")
        # Manually expire the entry
        store._store[key].expires_at = time.monotonic() - 1
        assert store.load("a3", "s3") is None


# ---------------------------------------------------------------------------
# AC3 — POST /api/sessions/{sid}/checkpoint
# ---------------------------------------------------------------------------


class TestCheckpointEndpoint:
    def test_creates_checkpoint(self, _fresh_store):
        ctx = AgentWorkspaceContext(
            agent_id=AGENT_ID, session_id=SESSION_ID, task_description="do work"
        )
        ctx.add_step("bash", "result")
        ctx.add_step("bash", "result2")
        _fresh_store.save(ctx)

        resp = client.post(
            f"/api/sessions/{SESSION_ID}/checkpoint",
            json={"agent_id": AGENT_ID, "summary": "manual cp"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["checkpoint_index"] == 0
        assert data["summary"] == "manual cp"
        assert data["steps_taken"] == 2

    def test_checkpoint_404_when_no_session(self):
        resp = client.post(
            "/api/sessions/ghost-session/checkpoint",
            json={"agent_id": "ghost-agent"},
        )
        assert resp.status_code == 404

    def test_checkpoint_auto_summary(self, _fresh_store):
        ctx = AgentWorkspaceContext(
            agent_id=AGENT_ID, session_id=SESSION_ID, task_description="do work"
        )
        ctx.add_step("bash", "r1")
        _fresh_store.save(ctx)

        resp = client.post(
            f"/api/sessions/{SESSION_ID}/checkpoint",
            json={"agent_id": AGENT_ID},
        )
        assert resp.status_code == 200
        assert resp.json()["summary"] != ""


# ---------------------------------------------------------------------------
# AC4 — GET /api/sessions/{sid}/context
# ---------------------------------------------------------------------------


class TestContextEndpoint:
    def test_returns_context(self, _fresh_store):
        ctx = AgentWorkspaceContext(
            agent_id=AGENT_ID,
            session_id=SESSION_ID,
            task_description="important task",
            working_dir="/tmp/work",
        )
        for i in range(5):
            ctx.add_step("bash", f"output {i}")
        _fresh_store.save(ctx)

        resp = client.get(
            f"/api/sessions/{SESSION_ID}/context",
            params={"agent_id": AGENT_ID},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == AGENT_ID
        assert data["session_id"] == SESSION_ID
        assert data["task_description"] == "important task"
        assert data["working_dir"] == "/tmp/work"
        assert data["steps_taken"] == 5
        assert len(data["recent_steps"]) == 5

    def test_recent_steps_capped_at_10(self, _fresh_store):
        ctx = AgentWorkspaceContext(agent_id=AGENT_ID, session_id=SESSION_ID, task_description="t")
        for i in range(15):
            ctx.add_step("bash", f"r{i}")
        _fresh_store.save(ctx)

        resp = client.get(
            f"/api/sessions/{SESSION_ID}/context",
            params={"agent_id": AGENT_ID},
        )
        assert resp.status_code == 200
        assert len(resp.json()["recent_steps"]) == 10

    def test_404_when_not_found(self):
        resp = client.get(
            "/api/sessions/no-session/context",
            params={"agent_id": "no-agent"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# AC5 — GET /api/sessions/{sid}/report
# ---------------------------------------------------------------------------


class TestReportEndpoint:
    def test_in_progress_when_no_report(self, _fresh_store):
        ctx = AgentWorkspaceContext(agent_id=AGENT_ID, session_id=SESSION_ID, task_description="t")
        ctx.add_step("bash", "r")
        _fresh_store.save(ctx)

        resp = client.get(
            f"/api/sessions/{SESSION_ID}/report",
            params={"agent_id": AGENT_ID},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "in_progress"

    def test_returns_report_when_set(self, _fresh_store):
        set_report(AGENT_ID, SESSION_ID, {"status": "completed", "steps_taken": 7, "task_id": "t1", "agent_id": AGENT_ID, "session_id": SESSION_ID, "duration_seconds": 1.0, "segments": 1, "task_description": "t", "outputs": [], "errors": [], "recovery_attempts": 0})

        resp = client.get(
            f"/api/sessions/{SESSION_ID}/report",
            params={"agent_id": AGENT_ID},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"
        assert resp.json()["steps_taken"] == 7

    def test_404_when_no_session_and_no_report(self):
        resp = client.get(
            "/api/sessions/ghost-session/report",
            params={"agent_id": "ghost-agent"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# AC6 — DELETE /api/sessions/{sid}/context
# ---------------------------------------------------------------------------


class TestDeleteContextEndpoint:
    def test_deletes_context(self, _fresh_store):
        ctx = AgentWorkspaceContext(agent_id=AGENT_ID, session_id=SESSION_ID, task_description="t")
        _fresh_store.save(ctx)

        resp = client.delete(
            f"/api/sessions/{SESSION_ID}/context",
            params={"agent_id": AGENT_ID},
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

        # context is gone
        assert _fresh_store.load(AGENT_ID, SESSION_ID) is None

    def test_404_when_not_found(self):
        resp = client.delete(
            "/api/sessions/ghost/context",
            params={"agent_id": "ghost"},
        )
        assert resp.status_code == 404
