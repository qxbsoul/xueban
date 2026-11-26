"""正式库迁移前只读检查（不写入、不迁移）。"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REQUIRED_TABLES = {
    "xueban_users": ["id", "email", "role", "auth_type"],
    "teacher_batches": ["id", "owner_id"],
    "teacher_answers": ["id", "import_batch_id", "student_id"],
    "teacher_students": ["id", "user_id", "code", "class_id"],
    "schema_migrations": ["version", "applied_at"],
}
OPTIONAL_TABLES = {"teacher_enrollments": ["id", "batch_id", "student_id", "identity_status"], "xueban_account_audit": ["id", "user_id", "action"]}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scalar(con, sql, default=0):
    try:
        row = con.execute(sql).fetchone()
        return row[0] if row and row[0] is not None else default
    except sqlite3.Error:
        return default


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--skip-sha", action="store_true")
    args = parser.parse_args()
    path = Path(args.db).resolve()
    if not path.exists():
        sys.exit(f"数据库不存在：{path}")
    stat = path.stat()
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    columns = {}
    for table in REQUIRED_TABLES:
        columns[table] = {row[1] for row in con.execute(f"PRAGMA table_info({table})")} if table in tables else set()
    report = {
        "database": str(path),
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "sha256": None if args.skip_sha else sha256(path),
        "integrity_check": scalar(con, "PRAGMA integrity_check", None),
        "journal_mode": scalar(con, "PRAGMA journal_mode", None),
        "schema_migrations": sorted(row[0] for row in con.execute("SELECT version FROM schema_migrations")) if "schema_migrations" in tables else None,
        "roles": {row[0]: row[1] for row in con.execute("SELECT COALESCE(role,'<null>'), COUNT(*) FROM xueban_users GROUP BY COALESCE(role,'<null>')")} if "xueban_users" in tables else {},
        "users_total": scalar(con, "SELECT COUNT(*) FROM xueban_users", 0),
        "guest_role_mismatch": scalar(con, "SELECT COUNT(*) FROM xueban_users WHERE auth_type='guest' AND COALESCE(role,'') <> 'guest'", 0),
        "missing_role": scalar(con, "SELECT COUNT(*) FROM xueban_users WHERE role IS NULL OR role=''", 0),
        "unknown_role": scalar(con, "SELECT COUNT(*) FROM xueban_users WHERE COALESCE(role,'') NOT IN ('teacher','student','guest')", 0),
        "batches_total": scalar(con, "SELECT COUNT(*) FROM teacher_batches", 0),
        "batch_owner": ({str(row[0]): row[1] for row in con.execute("SELECT COALESCE(owner_id,'<null>'), COUNT(*) FROM teacher_batches GROUP BY COALESCE(owner_id,'<null>')")} if "teacher_batches" in tables and "owner_id" in columns.get("teacher_batches", set()) else ({"<column-missing>": scalar(con, "SELECT COUNT(*) FROM teacher_batches", 0)} if "teacher_batches" in tables else {})),
        "answers_total": scalar(con, "SELECT COUNT(*) FROM teacher_answers", 0),
        "enrollments_total": scalar(con, "SELECT COUNT(*) FROM teacher_enrollments", 0) if "teacher_enrollments" in tables else None,
        "audit_total": scalar(con, "SELECT COUNT(*) FROM xueban_account_audit", 0) if "xueban_account_audit" in tables else None,
        "required_tables": {table: {"present": table in tables, "missing_columns": sorted(set(cols) - columns.get(table, set()))} for table, cols in REQUIRED_TABLES.items()},
        "optional_tables": {table: {"present": table in tables, "missing_columns": sorted(set(cols) - ({row[1] for row in con.execute(f'PRAGMA table_info({table})')} if table in tables else set()))} for table, cols in OPTIONAL_TABLES.items()},
        "wal_files": [str(p.name) + f" ({p.stat().st_size} bytes)" for p in path.parent.glob(path.name + "-*")],
        "read_only": True,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
