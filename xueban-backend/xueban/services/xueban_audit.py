"""Immutable employee configuration versions and append-only task execution audit."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

from xueban.services import xueban_store as db


def config_snapshot(employee: Dict[str, Any]) -> Dict[str, Any]:
    """Only execution-relevant, restorable configuration; never credentials or metrics."""
    return {
        "name": employee.get("name") or "",
        "role": employee.get("role") or "",
        "title": employee.get("title") or "",
        "department_id": employee.get("department_id"),
        "manager_id": employee.get("manager_id"),
        "system_prompt": employee.get("system_prompt") or "",
        "avatar": employee.get("avatar") or "",
        "seniority": employee.get("seniority") or "mid",
        "skills": list(employee.get("skills") or []),
        "tools": list(employee.get("tools") or []),
        "permissions": list(employee.get("permissions") or []),
        "model_provider": employee.get("model_provider") or "",
        "model_name": employee.get("model_name") or "",
        "status": employee.get("status") or "idle",
        "employment": employee.get("employment") or "ai",
        "day_rate_cents": int(employee.get("day_rate_cents") or 0),
    }


def insert_config_version(
    connection: sqlite3.Connection,
    employee: Dict[str, Any],
    source: str,
    restored_from_version: Optional[int] = None,
    created_at: Optional[str] = None,
) -> Dict[str, Any]:
    row = connection.execute(
        "SELECT COALESCE(MAX(version), 0) AS version FROM employee_config_versions WHERE employee_id = ?",
        (employee["id"],),
    ).fetchone()
    version = int(row["version"] or 0) + 1
    version_id = db.new_id("cfg")
    ts = created_at or db.now()
    connection.execute(
        """INSERT INTO employee_config_versions
           (id, employee_id, company_id, version, config_json, source, restored_from_version, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            version_id, employee["id"], employee["company_id"], version,
            json.dumps(config_snapshot(employee), ensure_ascii=False), source,
            restored_from_version, ts,
        ),
    )
    return {
        "id": version_id, "employee_id": employee["id"], "company_id": employee["company_id"],
        "version": version, "config": config_snapshot(employee), "source": source,
        "restored_from_version": restored_from_version, "created_at": ts,
    }


def record_config_version(
    employee: Dict[str, Any], source: str, restored_from_version: Optional[int] = None,
) -> Dict[str, Any]:
    with db._LOCK:
        connection = db.conn()
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = insert_config_version(connection, employee, source, restored_from_version)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise


def _version(row: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(row)
    try:
        result["config"] = json.loads(result.pop("config_json"))
    except (TypeError, json.JSONDecodeError):
        result["config"] = {}
        result.pop("config_json", None)
    return result


def list_config_versions(company_id: str, employee_id: str) -> List[Dict[str, Any]]:
    rows = db.q(
        """SELECT * FROM employee_config_versions
           WHERE company_id = ? AND employee_id = ? ORDER BY version DESC""",
        (company_id, employee_id),
    )
    return [_version(row) for row in rows]


def get_config_version(company_id: str, employee_id: str, version: int) -> Optional[Dict[str, Any]]:
    row = db.q1(
        """SELECT * FROM employee_config_versions
           WHERE company_id = ? AND employee_id = ? AND version = ?""",
        (company_id, employee_id, int(version)),
    )
    return _version(row) if row else None


def start_execution(
    task: Dict[str, Any], employee: Dict[str, Any], config_version: Dict[str, Any],
    input_summary: str, started_at: str,
) -> Dict[str, Any]:
    with db._LOCK:
        connection = db.conn()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT COALESCE(MAX(attempt), 0) AS attempt FROM task_executions WHERE task_id = ?",
                (task["id"],),
            ).fetchone()
            attempt = int(row["attempt"] or 0) + 1
            execution_id = db.new_id("exe")
            connection.execute(
                """INSERT INTO task_executions
                   (id, task_id, company_id, employee_id, config_version_id, config_version,
                    attempt, input_summary, model, started_at, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?, 'running', ?)""",
                (
                    execution_id, task["id"], task["company_id"], employee["id"],
                    config_version["id"], config_version["version"], attempt,
                    input_summary[:4000], employee.get("model_name") or "", started_at, started_at,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return get_execution(execution_id)  # type: ignore[return-value]


def finish_execution(
    execution_id: str, status: str, ended_at: str, duration_ms: int,
    output_summary: str = "", model: str = "", error: Optional[str] = None,
) -> None:
    db.x(
        """UPDATE task_executions SET status = ?, ended_at = ?, duration_ms = ?,
           output_summary = ?, model = ?, error = ? WHERE id = ? AND status = 'running'""",
        (
            status, ended_at, max(0, int(duration_ms)), output_summary[:4000], model,
            (error or "")[:4000] or None, execution_id,
        ),
    )


def get_execution(execution_id: str) -> Optional[Dict[str, Any]]:
    return db.q1("SELECT * FROM task_executions WHERE id = ?", (execution_id,))


def list_executions(company_id: str, task_id: str) -> List[Dict[str, Any]]:
    return db.q(
        """SELECT * FROM task_executions WHERE company_id = ? AND task_id = ?
           ORDER BY attempt DESC""",
        (company_id, task_id),
    )
