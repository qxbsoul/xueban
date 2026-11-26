"""只读查询迁移状态（服务未启动时也能用）。"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path


def query(path: Path) -> dict:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    completed = sorted(row[0] for row in con.execute("SELECT version FROM schema_migrations")) if "schema_migrations" in tables else []
    failures = []
    if "schema_migration_failures" in tables:
        failures = [{"version": row[0], "error": row[1], "failed_at": row[2]} for row in con.execute("SELECT version,error,failed_at FROM schema_migration_failures ORDER BY id DESC LIMIT 10")]
    status = "ready" if set(range(1, 6)).issubset(set(completed)) else ("failed" if failures else "pending")
    return {"database": str(path), "status": status, "completed": completed, "current_version": max(completed) if completed else 0, "target_version": 5, "recent_failures": failures}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    args = parser.parse_args()
    path = Path(args.db).resolve()
    if not path.exists():
        sys.exit(f"数据库不存在：{path}")
    print(json.dumps(query(path), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
