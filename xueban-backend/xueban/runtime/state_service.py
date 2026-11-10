"""AgentState + StateService — Story 9.4 AC1/AC2.

File-system backed state management for agents.
State files:   .xueban/agent_state/{agent_id}.json
Snapshot files:.xueban/state_snapshots/{agent_id}/{snapshot_id}.json
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from xueban.runtime.errors import XuebanError

logger = logging.getLogger(__name__)

_XUEBAN_ROOT = Path(".xueban")
_STATE_DIR = _XUEBAN_ROOT / "agent_state"
_SNAPSHOT_DIR = _XUEBAN_ROOT / "state_snapshots"

_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_agent_id(agent_id: str) -> None:
    if not _ID_RE.match(agent_id):
        raise XuebanError(
            f"Invalid agent_id {agent_id!r}: must match [A-Za-z0-9_-]{{1,128}}",
            details={"agent_id": agent_id},
        )


def _validate_snapshot_id(snapshot_id: str) -> None:
    if not _ID_RE.match(snapshot_id):
        raise XuebanError(
            f"Invalid snapshot_id {snapshot_id!r}: must match [A-Za-z0-9_-]{{1,128}}",
            details={"snapshot_id": snapshot_id},
        )


def _write_secure(path: Path, content: str) -> None:
    """Write text to path and restrict to owner-only (0o600)."""
    path.write_text(content, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        logger.warning("state_service: chmod 0o600 failed for %s (Windows/ACL limitation)", path)


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class StateConflict(XuebanError):
    """Optimistic lock version mismatch."""

    code = "STATE_CONFLICT"

    def __init__(self, expected: int, got: int) -> None:
        super().__init__(
            f"State version conflict: expected {expected}, got {got}",
            details={"expected": expected, "got": got},
        )
        self.expected = expected
        self.got = got


class SnapshotNotFound(XuebanError):
    """Requested snapshot does not exist."""

    code = "SNAPSHOT_NOT_FOUND"

    def __init__(self, agent_id: str, snapshot_id: str) -> None:
        super().__init__(
            f"Snapshot {snapshot_id!r} not found for agent {agent_id!r}",
            details={"agent_id": agent_id, "snapshot_id": snapshot_id},
        )


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class AgentState(BaseModel):
    """Persistent state for a single agent."""

    agent_id: str
    role_profile_ref: str = ""
    memory_profile_ref: str = ""
    state_fields: Dict[str, Any] = Field(default_factory=dict)
    session_summary: str = ""
    recent_artifacts: List[str] = Field(default_factory=list)
    pending_tasks: List[str] = Field(default_factory=list)
    last_writeback_at: Optional[datetime] = None
    state_version: int = 0
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    model_config = {"use_enum_values": True}


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class StateService:
    """CRUD + snapshot + restore for AgentState with optimistic locking."""

    def get_state(self, agent_id: str) -> Optional[AgentState]:
        _validate_agent_id(agent_id)
        path = _STATE_DIR / f"{agent_id}.json"
        if not path.exists():
            return None
        return AgentState.model_validate_json(path.read_text(encoding="utf-8"))

    def update_state(self, agent_id: str, patch: Dict[str, Any]) -> AgentState:
        """Patch agent state fields.  Caller MUST supply ``version`` matching current state_version."""
        _validate_agent_id(agent_id)
        state = self.get_state(agent_id)
        if state is None:
            state = AgentState(agent_id=agent_id)

        incoming_version: int = patch.get("version", state.state_version)
        if incoming_version != state.state_version:
            raise StateConflict(expected=state.state_version, got=incoming_version)

        # Apply permitted patch keys (never touch state_version / agent_id via patch)
        _MUTABLE = {
            "role_profile_ref", "memory_profile_ref", "state_fields",
            "session_summary", "recent_artifacts", "pending_tasks", "last_writeback_at",
        }
        for k, v in patch.items():
            if k in _MUTABLE:
                setattr(state, k, v)

        state.state_version += 1
        state.updated_at = _utc_now()

        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = _STATE_DIR / f"{agent_id}.json"
        _write_secure(path, state.model_dump_json(indent=2))
        return state

    def snapshot_state(self, agent_id: str) -> str:
        """Create a snapshot of current state. Returns snapshot_id."""
        _validate_agent_id(agent_id)
        state = self.get_state(agent_id)
        if state is None:
            state = AgentState(agent_id=agent_id)

        snapshot_id = uuid4().hex
        snap_dir = _SNAPSHOT_DIR / agent_id
        snap_dir.mkdir(parents=True, exist_ok=True)
        snap_path = snap_dir / f"{snapshot_id}.json"
        _write_secure(snap_path, state.model_dump_json(indent=2))
        logger.info("state_service: snapshot created agent_id=%s snapshot_id=%s", agent_id, snapshot_id)
        return snapshot_id

    def restore_state(self, agent_id: str, snapshot_id: str) -> AgentState:
        """Restore agent state from a snapshot."""
        _validate_agent_id(agent_id)
        _validate_snapshot_id(snapshot_id)
        snap_path = _SNAPSHOT_DIR / agent_id / f"{snapshot_id}.json"
        if not snap_path.exists():
            raise SnapshotNotFound(agent_id, snapshot_id)

        restored = AgentState.model_validate_json(snap_path.read_text(encoding="utf-8"))
        # Bump version so concurrent writers know state changed
        restored.state_version += 1
        restored.updated_at = _utc_now()

        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        state_path = _STATE_DIR / f"{agent_id}.json"
        _write_secure(state_path, restored.model_dump_json(indent=2))
        return restored

    def list_snapshots(self, agent_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """List snapshots for an agent ordered newest-first (max ``limit`` entries)."""
        _validate_agent_id(agent_id)
        snap_dir = _SNAPSHOT_DIR / agent_id
        if not snap_dir.exists():
            return []
        results: List[Dict[str, Any]] = []
        for p in snap_dir.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                results.append({
                    "snapshot_id": p.stem,
                    "created_at": data.get("updated_at") or data.get("created_at"),
                    "state_version": data.get("state_version", 0),
                })
            except Exception:
                logger.warning("state_service: unreadable snapshot %s", p, exc_info=True)
        results.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        return results[:limit]

    def reset_state(self, agent_id: str) -> AgentState:
        """Clear state_fields + session_summary; keep profile refs and bump version."""
        _validate_agent_id(agent_id)
        state = self.get_state(agent_id)
        if state is None:
            state = AgentState(agent_id=agent_id)

        state.state_fields = {}
        state.session_summary = ""
        state.recent_artifacts = []
        state.pending_tasks = []
        state.last_writeback_at = None
        state.state_version += 1
        state.updated_at = _utc_now()

        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = _STATE_DIR / f"{agent_id}.json"
        _write_secure(path, state.model_dump_json(indent=2))
        return state


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_SERVICE_SINGLETON: Optional[StateService] = None


def get_service() -> StateService:
    global _SERVICE_SINGLETON
    if _SERVICE_SINGLETON is None:
        _SERVICE_SINGLETON = StateService()
    return _SERVICE_SINGLETON


def set_service(svc: StateService) -> None:
    global _SERVICE_SINGLETON
    _SERVICE_SINGLETON = svc
