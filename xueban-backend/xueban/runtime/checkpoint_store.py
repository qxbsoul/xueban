from __future__ import annotations

import threading
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from xueban.runtime.contracts import CheckpointRef, utc_now


WritebackTarget = Literal["host", "docs", "memory", "graph"]


class StoredCheckpointRecord(BaseModel):
    checkpoint_id: str
    run_id: str
    step_id: Optional[str] = None
    target: WritebackTarget
    location: str
    state_ref: Optional[str] = None
    next_node_id: Optional[str] = None
    resume_supported: bool = False
    stored_at: datetime = Field(default_factory=utc_now)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class BaseCheckpointStore:
    def put(self, checkpoint: CheckpointRef) -> StoredCheckpointRecord:
        raise NotImplementedError

    def get(self, checkpoint_id: str) -> Optional[CheckpointRef]:
        raise NotImplementedError

    def get_record(self, checkpoint_id: str) -> Optional[StoredCheckpointRecord]:
        raise NotImplementedError

    def list_run(self, run_id: str) -> List[StoredCheckpointRecord]:
        raise NotImplementedError


class InMemoryCheckpointStore(BaseCheckpointStore):
    def __init__(self) -> None:
        self._checkpoints: Dict[str, CheckpointRef] = {}
        self._records: Dict[str, StoredCheckpointRecord] = {}

    def put(self, checkpoint: CheckpointRef) -> StoredCheckpointRecord:
        checkpoint_copy = checkpoint.model_copy(deep=True)
        target = checkpoint_copy.writeback.target
        record = StoredCheckpointRecord(
            checkpoint_id=checkpoint_copy.checkpoint_id,
            run_id=checkpoint_copy.run_id,
            step_id=checkpoint_copy.step_id,
            target=target,
            location=f"{target}://checkpoints/{checkpoint_copy.checkpoint_id}",
            state_ref=checkpoint_copy.state_ref,
            next_node_id=checkpoint_copy.writeback.next_node_id,
            resume_supported=bool(checkpoint_copy.writeback.resume_supported),
            metadata={
                **checkpoint_copy.metadata,
                "current_node_id": checkpoint_copy.state.current_node_id,
            },
        )
        self._checkpoints[checkpoint_copy.checkpoint_id] = checkpoint_copy
        self._records[checkpoint_copy.checkpoint_id] = record
        return record

    def get(self, checkpoint_id: str) -> Optional[CheckpointRef]:
        checkpoint = self._checkpoints.get(checkpoint_id)
        return checkpoint.model_copy(deep=True) if checkpoint is not None else None

    def get_record(self, checkpoint_id: str) -> Optional[StoredCheckpointRecord]:
        record = self._records.get(checkpoint_id)
        return record.model_copy(deep=True) if record is not None else None

    def list_run(self, run_id: str) -> List[StoredCheckpointRecord]:
        return [
            record.model_copy(deep=True)
            for record in self._records.values()
            if record.run_id == run_id
        ]


class SqliteCheckpointStore(BaseCheckpointStore):
    """Checkpoint store persisted to SQLite (WAL) — survives process restarts.

    Same four-method contract as InMemoryCheckpointStore; full CheckpointRef
    JSON is the value, StoredCheckpointRecord doubles as the index row.
    """

    def __init__(self, path: str) -> None:
        import sqlite3

        self._path = str(path)
        self._lock = threading.Lock()
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                checkpoint_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                stored_at TEXT NOT NULL,
                record_json TEXT NOT NULL,
                checkpoint_json TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_checkpoints_run ON checkpoints(run_id)"
        )
        self._conn.commit()

    def put(self, checkpoint: CheckpointRef) -> StoredCheckpointRecord:
        checkpoint_copy = checkpoint.model_copy(deep=True)
        target = checkpoint_copy.writeback.target
        record = StoredCheckpointRecord(
            checkpoint_id=checkpoint_copy.checkpoint_id,
            run_id=checkpoint_copy.run_id,
            step_id=checkpoint_copy.step_id,
            target=target,
            location=f"sqlite://{self._path}#checkpoints/{checkpoint_copy.checkpoint_id}",
            state_ref=checkpoint_copy.state_ref,
            next_node_id=checkpoint_copy.writeback.next_node_id,
            resume_supported=bool(checkpoint_copy.writeback.resume_supported),
            metadata={
                **checkpoint_copy.metadata,
                "current_node_id": checkpoint_copy.state.current_node_id,
            },
        )
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO checkpoints "
                "(checkpoint_id, run_id, stored_at, record_json, checkpoint_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    record.checkpoint_id,
                    record.run_id,
                    record.stored_at.isoformat(),
                    record.model_dump_json(),
                    checkpoint_copy.model_dump_json(),
                ),
            )
            self._conn.commit()
        return record

    def get(self, checkpoint_id: str) -> Optional[CheckpointRef]:
        with self._lock:
            row = self._conn.execute(
                "SELECT checkpoint_json FROM checkpoints WHERE checkpoint_id = ?",
                (checkpoint_id,),
            ).fetchone()
        if row is None:
            return None
        return CheckpointRef.model_validate_json(row[0])

    def get_record(self, checkpoint_id: str) -> Optional[StoredCheckpointRecord]:
        with self._lock:
            row = self._conn.execute(
                "SELECT record_json FROM checkpoints WHERE checkpoint_id = ?",
                (checkpoint_id,),
            ).fetchone()
        if row is None:
            return None
        return StoredCheckpointRecord.model_validate_json(row[0])

    def list_run(self, run_id: str) -> List[StoredCheckpointRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT record_json FROM checkpoints WHERE run_id = ? ORDER BY stored_at",
                (run_id,),
            ).fetchall()
        return [StoredCheckpointRecord.model_validate_json(row[0]) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


