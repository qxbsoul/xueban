"""Durable run event log + run state persistence (P1 — "model-visible means logged").

The append-only event log is the run's source of truth for evidence: every
durable fact (node started/completed, checkpoint saved, loop iteration,
terminal status) is appended here, and every projection (runs API, lineage,
replay) can be rebuilt from it. The SQLite run/request snapshots give the
runs API cold-start resilience — a restarted Python process no longer 404s
on finished runs.

Event taxonomy (P3):
- DURABLE events (this log): facts that must survive a reload.
- LIVE events (RunEventBus / SSE): in-flight observation only.

Wiring (xueban/server.py):
    SqliteRunEventLog / SqliteRunStore / SqliteRequestContextStore over one
    SQLite file (env SF_RUNTIME_STATE_DB, default <repo>/.xueban/runtime.sqlite).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from xueban.runtime.contracts import RunResult, RunSummary, RuntimeRequest, utc_now

# ---------------------------------------------------------------------------
# Event model + taxonomy
# ---------------------------------------------------------------------------

DURABLE_EVENT_TYPES: frozenset = frozenset(
    {
        "run.started",
        "run.completed",
        "run.failed",
        "run.cancelled",
        "node.started",
        "node.completed",
        "node.failed",
        "checkpoint.saved",
        "loop.iteration",
        "loop.completed",
        "loop.halt_requested",
        "task.ledger",
        "permission.checked",
        "permission.blocked",
    }
)


class RunEventRecord(BaseModel):
    """One append-only durable fact about a run."""

    run_id: str
    seq: int
    event_type: str
    node_id: Optional[str] = None
    iteration_index: Optional[int] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


def replay_run_summary(events: List[RunEventRecord]) -> Dict[str, Any]:
    """Project a run's event list into a compact summary (evidence/debug).

    Never a substitute for full projections — just proves the log is
    self-describing: status, step counts, loops and checkpoints all derive
    from the stream alone.
    """
    summary: Dict[str, Any] = {
        "run_id": events[0].run_id if events else None,
        "event_count": len(events),
        "status": "unknown",
        "nodes_executed": [],
        "checkpoints": [],
        "loops": {},
    }
    for event in events:
        etype = event.event_type
        if etype == "run.completed":
            summary["status"] = "succeeded"
        elif etype == "run.failed":
            summary["status"] = "failed"
        elif etype == "run.cancelled":
            summary["status"] = "cancelled"
        elif etype == "run.started":
            summary["status"] = "running"
            summary["workflow_id"] = event.payload.get("workflow_id")
        elif etype == "node.completed" and event.node_id:
            summary["nodes_executed"].append(event.node_id)
        elif etype == "checkpoint.saved":
            summary["checkpoints"].append(event.payload.get("checkpoint_id"))
        elif etype == "loop.iteration":
            loop_id = event.payload.get("loop_id")
            if loop_id:
                entry = summary["loops"].setdefault(
                    loop_id, {"iterations": 0, "last_converged": False}
                )
                entry["iterations"] += 1
                entry["last_converged"] = bool(event.payload.get("converged"))
        elif etype == "loop.completed":
            loop_id = event.payload.get("loop_id")
            if loop_id and loop_id in summary["loops"]:
                summary["loops"][loop_id]["status"] = event.payload.get("status")
        elif etype == "task.ledger":
            ledger = summary.setdefault("ledger", [])
            ledger.append(
                {
                    "loop_id": event.payload.get("loop_id"),
                    "iteration": event.iteration_index,
                    "done": event.payload.get("done"),
                    "goal": event.payload.get("goal"),
                }
            )
        elif etype == "permission.blocked":
            blocked = summary.setdefault("permissions_blocked", [])
            blocked.append(
                {"node_id": event.node_id, "action": event.payload.get("action"), "mode": event.payload.get("mode")}
            )
    return summary


# ---------------------------------------------------------------------------
# Event log backends
# ---------------------------------------------------------------------------


class InMemoryRunEventLog:
    def __init__(self) -> None:
        self._events: Dict[str, List[RunEventRecord]] = {}
        self._seq: Dict[str, int] = {}

    def append(
        self,
        run_id: str,
        event_type: str,
        *,
        node_id: Optional[str] = None,
        iteration_index: Optional[int] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> RunEventRecord:
        seq = self._seq.get(run_id, 0) + 1
        self._seq[run_id] = seq
        record = RunEventRecord(
            run_id=run_id,
            seq=seq,
            event_type=event_type,
            node_id=node_id,
            iteration_index=iteration_index,
            payload=payload or {},
        )
        self._events.setdefault(run_id, []).append(record)
        return record

    def iter_events(self, run_id: str) -> List[RunEventRecord]:
        return list(self._events.get(run_id, []))

    def latest_seq(self, run_id: str) -> int:
        return self._seq.get(run_id, 0)


class SqliteRunEventLog(InMemoryRunEventLog):
    """Append-only event log persisted to SQLite (WAL).

    Extends the in-memory log: reads hit memory after the first load for the
    run; appends write through to disk. A fresh process seeds memory from
    disk on first read, so history survives restarts.
    """

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self._path = str(path)
        self._lock = threading.Lock()
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS run_events (
                run_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                node_id TEXT,
                iteration_index INTEGER,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (run_id, seq)
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id)"
        )
        self._conn.commit()

    def append(
        self,
        run_id: str,
        event_type: str,
        *,
        node_id: Optional[str] = None,
        iteration_index: Optional[int] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> RunEventRecord:
        record = super().append(
            run_id,
            event_type,
            node_id=node_id,
            iteration_index=iteration_index,
            payload=payload,
        )
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO run_events "
                "(run_id, seq, event_type, node_id, iteration_index, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    record.run_id,
                    record.seq,
                    record.event_type,
                    record.node_id,
                    record.iteration_index,
                    json.dumps(record.payload, ensure_ascii=False, default=str),
                    record.created_at.isoformat(),
                ),
            )
            self._conn.commit()
        return record

    def iter_events(self, run_id: str) -> List[RunEventRecord]:
        if run_id in self._events:
            return list(self._events[run_id])
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, event_type, node_id, iteration_index, payload, created_at "
                "FROM run_events WHERE run_id = ? ORDER BY seq",
                (run_id,),
            ).fetchall()
        records = [
            RunEventRecord(
                run_id=run_id,
                seq=seq,
                event_type=event_type,
                node_id=node_id,
                iteration_index=iteration_index,
                payload=json.loads(payload),
                created_at=datetime.fromisoformat(created_at),
            )
            for seq, event_type, node_id, iteration_index, payload, created_at in rows
        ]
        if records:
            self._events[run_id] = records
            self._seq[run_id] = records[-1].seq
        return records

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ---------------------------------------------------------------------------
# Run / request-context snapshot stores (run_store / request_context_store seams)
# ---------------------------------------------------------------------------


class SqliteRunStore:
    """Persisted RunResult snapshots — the ``run_store`` seam implementation.

    put()/get() match the interface RuntimeService already calls
    (``self._run_store.put(result)`` / ``.get(run_id)``).
    """

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._lock = threading.Lock()
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS run_snapshots (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                workflow_id TEXT,
                updated_at TEXT NOT NULL,
                result_json TEXT NOT NULL
            )
            """
        )
        # Summary columns (added 2026-08-15 when list_runs moved to RunSummary
        # objects); add-column-if-missing keeps older DB files loadable.
        for column in ("request_id TEXT", "started_at TEXT"):
            name = column.split()[0]
            try:
                self._conn.execute(f"ALTER TABLE run_snapshots ADD COLUMN {column}")
            except sqlite3.OperationalError:
                pass  # column already present
        self._conn.commit()

    def put(self, result: RunResult) -> None:
        payload = result.model_dump_json()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO run_snapshots "
                "(run_id, status, workflow_id, updated_at, result_json, request_id, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    result.run.run_id,
                    result.run.status,
                    result.run.workflow_id,
                    utc_now().isoformat(),
                    payload,
                    result.run.request_id,
                    result.run.started_at.isoformat(),
                ),
            )
            self._conn.commit()

    def get(self, run_id: str) -> Optional[RunResult]:
        with self._lock:
            row = self._conn.execute(
                "SELECT result_json FROM run_snapshots WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return RunResult.model_validate_json(row[0])

    def list_runs(self, limit: int = 100) -> List[RunSummary]:
        """Return RunSummary objects (the run_store contract; the API's
        GET /runs consumes these directly)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, status, workflow_id, updated_at, request_id, started_at, result_json "
                "FROM run_snapshots ORDER BY COALESCE(started_at, updated_at) DESC LIMIT ?",
                (limit,),
            ).fetchall()
        summaries: List[RunSummary] = []
        for run_id, status, workflow_id, updated_at, request_id, started_at, result_json in rows:
            if request_id is None or started_at is None:
                # legacy row written before the summary columns existed —
                # backfill from the snapshot payload
                try:
                    parsed = RunResult.model_validate_json(result_json).run
                    request_id = request_id or parsed.request_id
                    started_at = started_at or parsed.started_at.isoformat()
                except Exception:  # noqa: BLE001 — degrade to placeholder fields
                    request_id = request_id or ""
                    started_at = started_at or updated_at
            summaries.append(
                RunSummary(
                    run_id=run_id,
                    request_id=request_id or "",
                    workflow_id=workflow_id or "",
                    status=status if status in RunSummary.model_fields["status"].annotation.__args__ else "failed",
                    started_at=datetime.fromisoformat(started_at or updated_at),
                    metadata={"source": "sqlite_run_store"},
                )
            )
        return summaries

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class SqliteRequestContextStore:
    """Persisted RuntimeRequest snapshots — the ``request_context_store`` seam.

    Lets GET /runs/{id}/graph answer after a restart (the projection needs
    the workflow definition).
    """

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._lock = threading.Lock()
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS request_contexts (
                run_id TEXT PRIMARY KEY,
                request_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def put(self, run_id: str, request: RuntimeRequest) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO request_contexts (run_id, request_json, updated_at) "
                "VALUES (?, ?, ?)",
                (run_id, request.model_dump_json(), utc_now().isoformat()),
            )
            self._conn.commit()

    def get(self, run_id: str) -> Optional[RuntimeRequest]:
        with self._lock:
            row = self._conn.execute(
                "SELECT request_json FROM request_contexts WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return RuntimeRequest.model_validate_json(row[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()


__all__ = [
    "DURABLE_EVENT_TYPES",
    "InMemoryRunEventLog",
    "RunEventRecord",
    "SqliteRunEventLog",
    "SqliteRunStore",
    "SqliteRequestContextStore",
    "replay_run_summary",
]
