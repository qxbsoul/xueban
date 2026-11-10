"""State API endpoint tests — Story 9.4 AC3/AC6.

Covers:
  - GET /state/{agent_id}: 404 when no state exists
  - GET /state/{agent_id}: 200 + {data, meta} envelope
  - PATCH /state/{agent_id}: creates state and returns envelope
  - PATCH /state/{agent_id}: STATE_CONFLICT 409 with correct error format
  - POST /state/{agent_id}/snapshot: returns snapshot_id
  - GET /state/{agent_id}/snapshots: returns list
  - POST /state/{agent_id}/restore/{snapshot_id}: restores state
  - POST /state/{agent_id}/restore/{snapshot_id}: 404 for unknown snapshot
  - POST /state/{agent_id}/reset: resets state
  - STATE_CONFLICT error response has {code, message, details.expected/got}
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xueban.runtime.state_service import StateService, set_service
from xueban.server import app

client = TestClient(app)

AGENT_ID = "test-agent-state-api"


@pytest.fixture(autouse=True)
def _isolated_state_svc(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    svc = StateService()
    set_service(svc)
    try:
        yield svc
    finally:
        set_service(StateService())


# ---------------------------------------------------------------------------
# GET /state/{agent_id}
# ---------------------------------------------------------------------------


def test_get_state_not_found():
    res = client.get(f"/state/{AGENT_ID}")
    assert res.status_code == 404
    body = res.json()
    error = body.get("detail", body).get("error", {})
    assert error["code"] == "AGENT_STATE_NOT_FOUND"


def test_get_state_ok():
    client.patch(f"/state/{AGENT_ID}", json={"version": 0, "session_summary": "hello"})
    res = client.get(f"/state/{AGENT_ID}")
    assert res.status_code == 200
    body = res.json()
    assert "data" in body
    assert "meta" in body
    assert body["data"]["session_summary"] == "hello"


# ---------------------------------------------------------------------------
# PATCH /state/{agent_id}
# ---------------------------------------------------------------------------


def test_patch_state_creates_and_returns_envelope():
    res = client.patch(f"/state/{AGENT_ID}", json={
        "version": 0,
        "state_fields": {"mood": "happy"},
        "session_summary": "test summary",
    })
    assert res.status_code == 200
    body = res.json()
    assert "data" in body
    assert body["data"]["state_version"] == 1
    assert body["data"]["state_fields"]["mood"] == "happy"


def test_patch_state_conflict_returns_409():
    client.patch(f"/state/{AGENT_ID}", json={"version": 0})
    res = client.patch(f"/state/{AGENT_ID}", json={"version": 0})  # stale
    assert res.status_code == 409
    body = res.json()
    error = body.get("detail", body).get("error", {})
    assert error["code"] == "STATE_CONFLICT"
    assert "message" in error
    assert error["details"]["expected"] == 1
    assert error["details"]["got"] == 0


def test_patch_state_conflict_message_not_empty():
    client.patch(f"/state/{AGENT_ID}", json={"version": 0})
    res = client.patch(f"/state/{AGENT_ID}", json={"version": 0})
    assert res.status_code == 409
    body = res.json()
    error = body.get("detail", body).get("error", {})
    assert error["message"] != ""


# ---------------------------------------------------------------------------
# POST /state/{agent_id}/snapshot
# ---------------------------------------------------------------------------


def test_create_snapshot_returns_snapshot_id():
    client.patch(f"/state/{AGENT_ID}", json={"version": 0})
    res = client.post(f"/state/{AGENT_ID}/snapshot")
    assert res.status_code == 200
    body = res.json()
    assert "data" in body
    assert "snapshot_id" in body["data"]
    assert len(body["data"]["snapshot_id"]) == 32


# ---------------------------------------------------------------------------
# GET /state/{agent_id}/snapshots
# ---------------------------------------------------------------------------


def test_list_snapshots_returns_envelope():
    client.patch(f"/state/{AGENT_ID}", json={"version": 0})
    client.post(f"/state/{AGENT_ID}/snapshot")
    res = client.get(f"/state/{AGENT_ID}/snapshots")
    assert res.status_code == 200
    body = res.json()
    assert "data" in body
    assert isinstance(body["data"], list)
    assert len(body["data"]) >= 1


def test_list_snapshots_empty_for_new_agent():
    res = client.get(f"/state/unknown-agent-xyz/snapshots")
    assert res.status_code == 200
    assert res.json()["data"] == []


def test_list_snapshots_items_have_required_fields():
    client.patch(f"/state/{AGENT_ID}", json={"version": 0})
    client.post(f"/state/{AGENT_ID}/snapshot")
    snap = client.get(f"/state/{AGENT_ID}/snapshots").json()["data"][0]
    assert "snapshot_id" in snap
    assert "created_at" in snap
    assert "state_version" in snap


# ---------------------------------------------------------------------------
# POST /state/{agent_id}/restore/{snapshot_id}
# ---------------------------------------------------------------------------


def test_restore_snapshot_ok():
    client.patch(f"/state/{AGENT_ID}", json={"version": 0, "session_summary": "original"})
    snap_id = client.post(f"/state/{AGENT_ID}/snapshot").json()["data"]["snapshot_id"]
    client.patch(f"/state/{AGENT_ID}", json={"version": 1, "session_summary": "modified"})
    res = client.post(f"/state/{AGENT_ID}/restore/{snap_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["data"]["session_summary"] == "original"


def test_restore_unknown_snapshot_returns_404():
    res = client.post(f"/state/{AGENT_ID}/restore/nonexistentsnapshotid0000000000")
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# POST /state/{agent_id}/reset
# ---------------------------------------------------------------------------


def test_reset_state_clears_data():
    client.patch(f"/state/{AGENT_ID}", json={
        "version": 0,
        "state_fields": {"key": "value"},
        "session_summary": "to be cleared",
    })
    res = client.post(f"/state/{AGENT_ID}/reset")
    assert res.status_code == 200
    body = res.json()
    assert body["data"]["state_fields"] == {}
    assert body["data"]["session_summary"] == ""


def test_reset_state_envelope():
    res = client.post(f"/state/{AGENT_ID}/reset")
    assert res.status_code == 200
    body = res.json()
    assert "data" in body
    assert "meta" in body
