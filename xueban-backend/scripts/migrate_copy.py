"""在独立副本上预演 v4/v5 迁移，并核对迁移前后业务数据。

不会写入 --source；所有写入只发生在 --target-dir 的副本。
"""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot(path: Path) -> dict:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def one(sql, default=0):
        try:
            row = con.execute(sql).fetchone()
            return row[0] if row and row[0] is not None else default
        except sqlite3.Error:
            return default

    data = {
        "users_total": one("SELECT COUNT(*) FROM xueban_users"),
        "roles": {row[0]: row[1] for row in con.execute("SELECT COALESCE(role,'<null>'), COUNT(*) FROM xueban_users GROUP BY COALESCE(role,'<null>')")},
        "guest_role_mismatch": one("SELECT COUNT(*) FROM xueban_users WHERE auth_type='guest' AND COALESCE(role,'') <> 'guest'"),
        "teacher_password": one("SELECT COUNT(*) FROM xueban_users WHERE auth_type='password' AND role='teacher'"),
        "student_password": one("SELECT COUNT(*) FROM xueban_users WHERE auth_type='password' AND role='student'"),
        "guest_password": one("SELECT COUNT(*) FROM xueban_users WHERE auth_type='guest' AND role='guest'"),
        "missing_role": one("SELECT COUNT(*) FROM xueban_users WHERE role IS NULL OR role=''"),
        "unknown_role": one("SELECT COUNT(*) FROM xueban_users WHERE COALESCE(role,'') NOT IN ('teacher','student','guest')"),
        "batches": one("SELECT COUNT(*) FROM teacher_batches"),
        "answers": one("SELECT COUNT(*) FROM teacher_answers"),
        "students": one("SELECT COUNT(*) FROM teacher_students"),
        "runs": one("SELECT COUNT(*) FROM teacher_grade_runs"),
        "issues": one("SELECT COUNT(*) FROM teacher_data_issues"),
        "versions": one("SELECT COUNT(*) FROM teacher_question_versions"),
        "analysis_runs": one("SELECT COUNT(*) FROM teacher_analysis_runs"),
        "enrollments": one("SELECT COUNT(*) FROM teacher_enrollments") if "teacher_enrollments" in tables else None,
        "enrollments_unverified": one("SELECT COUNT(*) FROM teacher_enrollments WHERE identity_status='unverified'") if "teacher_enrollments" in tables else None,
        "audit": one("SELECT COUNT(*) FROM xueban_account_audit") if "xueban_account_audit" in tables else None,
        "migrations": sorted(row[0] for row in con.execute("SELECT version FROM schema_migrations")) if "schema_migrations" in tables else [],
        "integrity": one("PRAGMA integrity_check", None),
        "expected_enrollments": one("""
            SELECT COUNT(*) FROM (
              SELECT a.import_batch_id, s.id FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id
              GROUP BY a.import_batch_id, s.id
            )
        """),
    }
    return data


def prepare(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    for suffix in ("-wal", "-shm"):
        side = Path(str(source) + suffix)
        if side.exists():
            shutil.copy2(side, Path(str(target) + suffix))


def run_bootstrap(db_path: Path, home: Path):
    os.environ["XUEBAN_XUEBAN_DB"] = str(db_path)
    os.environ["XUEBAN_HOME"] = str(home)
    root = Path(__file__).resolve().parents[1]
    code = """
import json
from xueban.api import xueban_teacher as t
t.bootstrap()
print(json.dumps(t.migration_status(), ensure_ascii=False))
"""
    env = dict(os.environ, PYTHONPATH=str(root))
    result = subprocess.run([sys.executable, "-c", code], cwd=str(root), env=env, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(result.stdout + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


def fail_and_retry(source: Path, workdir: Path) -> dict:
    """在独立副本上注入 v4 中途异常，验证整体回滚与重试。"""
    target = workdir / "failure.sqlite"
    prepare(source, target)
    before = snapshot(target)
    os.environ["XUEBAN_XUEBAN_DB"] = str(target)
    os.environ["XUEBAN_HOME"] = str(workdir / "failure-home")
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from xueban.api import xueban_teacher as teacher
    from xueban.services import xueban_store as db

    teacher.init()
    original_v4 = teacher.MIGRATIONS[4]

    def broken_v4():
        db.x("UPDATE xueban_users SET role='guest' WHERE id=(SELECT id FROM xueban_users WHERE auth_type='guest' LIMIT 1)")
        db.x("INSERT INTO xueban_account_audit (id,user_id,actor_id,action,detail_json,created_at) VALUES (?,?,?,?,?,?)", (db.new_id("auditfail"), "injected", "system", "role_migration", "{}", teacher.now()))
        raise RuntimeError("injected migration failure at v4")

    teacher.MIGRATIONS[4] = broken_v4
    failure = None
    try:
        teacher.bootstrap()
    except Exception as exc:
        failure = teacher.sanitize_migration_error(str(exc))
    after_failure = snapshot(target)
    failures = db.q("SELECT version, error FROM schema_migration_failures ORDER BY id DESC LIMIT 3")
    state_failed = teacher.migration_state()
    teacher.MIGRATIONS[4] = original_v4
    teacher.bootstrap()
    after_retry = snapshot(target)
    return {
        "failure": failure,
        "state_after_failure": state_failed,
        "failure_rows": failures,
        "before": before,
        "after_failure": after_failure,
        "after_retry": after_retry,
        "rollback_ok": after_failure["roles"] == before["roles"] and (after_failure["audit"] or 0) == 0 and 4 not in after_failure["migrations"],
        "retry_ok": 4 in after_retry["migrations"] and 5 in after_retry["migrations"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--target-dir", required=True)
    parser.add_argument("--fail-test", action="store_true")
    args = parser.parse_args()
    source = Path(args.source).resolve()
    target_dir = Path(args.target_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    source_sha_before = sha256(source)
    before = snapshot(source)
    target = target_dir / "rehearsal.sqlite"
    prepare(source, target)
    status = run_bootstrap(target, target_dir / "home")
    after = snapshot(target)
    duplicate_status = run_bootstrap(target, target_dir / "home")
    after_duplicate = snapshot(target)
    report = {
        "source": str(source),
        "source_sha256_before": source_sha_before,
        "source_sha256_after": sha256(source),
        "source_untouched": source_sha_before == sha256(source),
        "target": str(target),
        "target_sha256": sha256(target),
        "before": before,
        "after": after,
        "status": status,
        "duplicate_status": duplicate_status,
        "duplicate_unchanged": {key: after[key] == after_duplicate[key] for key in ("users_total", "roles", "audit", "enrollments", "migrations")},
        "checks": {
            "versions_1_to_5": set(range(1, 6)).issubset(set(after["migrations"])),
            "guest_mismatch_zero": after["guest_role_mismatch"] == 0,
            "missing_role_zero": after["missing_role"] == 0,
            "teacher_unchanged": after["teacher_password"] == before["teacher_password"],
            "student_unchanged": after["student_password"] == before["student_password"],
            "business_rows_unchanged": all(after[key] == before[key] for key in ("batches", "answers", "students", "runs", "issues", "versions", "analysis_runs")),
            "audit_delta_matches_changes": (after["audit"] or 0) - (before["audit"] or 0) == before["guest_role_mismatch"] + before["missing_role"],
            "enrollments_match_pairs": after["enrollments"] == after["expected_enrollments"],
            "enrollments_all_unverified": after["enrollments"] == after["enrollments_unverified"],
            "integrity_ok": str(after["integrity"]).lower() == "ok",
        },
    }
    if args.fail_test:
        report["failure_test"] = fail_and_retry(source, target_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
