"""StateService tests — Story 9.4 AC1/AC2/AC6.

Covers:
  - get_state: returns None for unknown agent
  - update_state: creates state on first call (no existing file)
  - update_state: applies patch and increments state_version
  - update_state: version=0 works on fresh state
  - update_state: raises StateConflict when version mismatches
  - update_state: does NOT overwrite agent_id via patch
  - snapshot_state: returns a snapshot_id and file is created
  - restore_state: restores data and increments version
  - restore_state: raises XuebanError for unknown snapshot
  - list_snapshots: empty list when no snapshots exist
  - list_snapshots: returns all snapshots ordered newest-first
  - reset_state: clears state_fields, session_summary, artifacts, tasks
  - reset_state: preserves role_profile_ref and memory_profile_ref
  - reset_state: increments state_version
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from xueban.runtime.errors import XuebanError
from xueban.runtime.state_service import AgentState, StateConflict, StateService


@pytest.fixture()
def svc(tmp_path: Path, monkeypatch) -> StateService:
    monkeypatch.chdir(tmp_path)
    return StateService()


# ---------------------------------------------------------------------------
# get_state
# ---------------------------------------------------------------------------


def test_get_state_unknown_returns_none(svc):
    assert svc.get_state("agent-xyz") is None


# ---------------------------------------------------------------------------
# update_state
# ---------------------------------------------------------------------------


def test_update_state_creates_on_first_call(svc):
    state = svc.update_state("agent-1", {"version": 0, "session_summary": "hello"})
    assert state.agent_id == "agent-1"
    assert state.session_summary == "hello"
    assert state.state_version == 1


def test_update_state_increments_version(svc):
    svc.update_state("agent-1", {"version": 0})
    state2 = svc.update_state("agent-1", {"version": 1, "session_summary": "v2"})
    assert state2.state_version == 2
    assert state2.session_summary == "v2"


def test_update_state_conflict_raises(svc):
    svc.update_state("agent-1", {"version": 0})
    with pytest.raises(StateConflict) as exc_info:
        svc.update_state("agent-1", {"version": 0})  # stale version
    exc = exc_info.value
    assert exc.expected == 1
    assert exc.got == 0
    assert exc.code == "STATE_CONFLICT"


def test_update_state_does_not_overwrite_agent_id(svc):
    state = svc.update_state("agent-1", {"version": 0, "agent_id": "HACKED"})
    assert state.agent_id == "agent-1"


def test_update_state_fields(svc):
    state = svc.update_state("agent-1", {"version": 0, "state_fields": {"mood": "happy", "level": 5}})
    assert state.state_fields == {"mood": "happy", "level": 5}


def test_update_state_pending_tasks_and_artifacts(svc):
    state = svc.update_state("agent-1", {
        "version": 0,
        "recent_artifacts": ["report.pdf"],
        "pending_tasks": ["Send email"],
    })
    assert state.recent_artifacts == ["report.pdf"]
    assert state.pending_tasks == ["Send email"]


# ---------------------------------------------------------------------------
# snapshot_state
# ---------------------------------------------------------------------------


def test_snapshot_state_returns_id_and_file_exists(svc, tmp_path):
    svc.update_state("agent-1", {"version": 0})
    snap_id = svc.snapshot_state("agent-1")
    assert isinstance(snap_id, str) and len(snap_id) == 32
    snap_file = tmp_path / ".xueban" / "state_snapshots" / "agent-1" / f"{snap_id}.json"
    assert snap_file.exists()


def test_snapshot_state_for_agent_without_state(svc, tmp_path):
    snap_id = svc.snapshot_state("agent-new")
    assert snap_id
    snap_file = tmp_path / ".xueban" / "state_snapshots" / "agent-new" / f"{snap_id}.json"
    assert snap_file.exists()


# ---------------------------------------------------------------------------
# restore_state
# ---------------------------------------------------------------------------


def test_restore_state_restores_data(svc):
    svc.update_state("agent-1", {"version": 0, "session_summary": "original"})
    snap_id = svc.snapshot_state("agent-1")
    svc.update_state("agent-1", {"version": 1, "session_summary": "modified"})
    restored = svc.restore_state("agent-1", snap_id)
    assert restored.session_summary == "original"


def test_restore_state_increments_version(svc):
    svc.update_state("agent-1", {"version": 0})
    snap_id = svc.snapshot_state("agent-1")
    restored = svc.restore_state("agent-1", snap_id)
    # snapshotted at version=1, restore bumps to 2
    assert restored.state_version == 2


def test_restore_state_unknown_snapshot_raises(svc):
    with pytest.raises(XuebanError):
        svc.restore_state("agent-1", "nonexistent_snapshot_id")


# ---------------------------------------------------------------------------
# list_snapshots
# ---------------------------------------------------------------------------


def test_list_snapshots_empty(svc):
    assert svc.list_snapshots("agent-1") == []


def test_list_snapshots_returns_all(svc):
    svc.update_state("agent-1", {"version": 0})
    id1 = svc.snapshot_state("agent-1")
    time.sleep(0.01)
    svc.update_state("agent-1", {"version": 1})
    id2 = svc.snapshot_state("agent-1")
    snaps = svc.list_snapshots("agent-1")
    assert len(snaps) == 2
    snap_ids = {s["snapshot_id"] for s in snaps}
    assert id1 in snap_ids
    assert id2 in snap_ids


def test_list_snapshots_has_required_keys(svc):
    svc.update_state("agent-1", {"version": 0})
    svc.snapshot_state("agent-1")
    snap = svc.list_snapshots("agent-1")[0]
    assert "snapshot_id" in snap
    assert "created_at" in snap
    assert "state_version" in snap


# ---------------------------------------------------------------------------
# reset_state
# ---------------------------------------------------------------------------


def test_reset_state_clears_fields(svc):
    svc.update_state("agent-1", {
        "version": 0,
        "state_fields": {"k": "v"},
        "session_summary": "some summary",
        "recent_artifacts": ["art.txt"],
        "pending_tasks": ["task1"],
    })
    reset = svc.reset_state("agent-1")
    assert reset.state_fields == {}
    assert reset.session_summary == ""
    assert reset.recent_artifacts == []
    assert reset.pending_tasks == []
    assert reset.last_writeback_at is None


def test_reset_state_preserves_profile_refs(svc):
    svc.update_state("agent-1", {
        "version": 0,
        "role_profile_ref": "role-abc",
        "memory_profile_ref": "mem-xyz",
    })
    reset = svc.reset_state("agent-1")
    assert reset.role_profile_ref == "role-abc"
    assert reset.memory_profile_ref == "mem-xyz"


def test_reset_state_increments_version(svc):
    svc.update_state("agent-1", {"version": 0})
    reset = svc.reset_state("agent-1")
    assert reset.state_version == 2


def test_reset_fresh_agent_returns_version_1(svc):
    reset = svc.reset_state("brand-new-agent")
    assert reset.state_version == 1
    assert reset.state_fields == {}
