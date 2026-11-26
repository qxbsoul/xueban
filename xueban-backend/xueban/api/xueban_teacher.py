"""教师端：课程/课程标准、赛题导入、原始作答、统一判题与结果。"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import secrets
import threading
import time
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, BackgroundTasks, Body, Depends, File, HTTPException, UploadFile

from ..services import xueban_store as db
from ..services import learning_analysis as analysis
from .xueban_auth import current_user

router = APIRouter(prefix="/api/teacher", tags=["teacher"])
_LOCK = threading.RLock()
_JOB_KEYS: Dict[str, str] = {}
_JOB_CONTEXTS: Dict[str, Dict[str, str]] = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS teacher_courses (id TEXT PRIMARY KEY, code TEXT UNIQUE, title TEXT, grade INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS teacher_standards (id TEXT PRIMARY KEY, code TEXT UNIQUE, title TEXT, version TEXT, course_id TEXT, source_pages TEXT, content_json TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS teacher_topics (id TEXT PRIMARY KEY, code TEXT UNIQUE, title TEXT, goal TEXT, course_id TEXT, standard_id TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS teacher_indicators (id TEXT PRIMARY KEY, code TEXT UNIQUE, topic_id TEXT, description TEXT);
CREATE TABLE IF NOT EXISTS teacher_classes (id TEXT PRIMARY KEY, code TEXT UNIQUE, name TEXT, grade INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS teacher_students (id TEXT PRIMARY KEY, user_id TEXT UNIQUE, code TEXT UNIQUE, class_id TEXT, name TEXT, grade INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS teacher_question_versions (version_id TEXT PRIMARY KEY, batch_id TEXT, question_id TEXT, topic_id TEXT, standard_id TEXT, course_id TEXT, material_type TEXT, question_type TEXT, stem TEXT, options TEXT, reference_answer TEXT, reference_solution TEXT, max_score REAL, scoring_points TEXT, indicator_id TEXT, source TEXT, source_order INTEGER, created_at TEXT, UNIQUE(batch_id,question_id));
CREATE TABLE IF NOT EXISTS teacher_questions (id TEXT PRIMARY KEY, topic_id TEXT, standard_id TEXT, course_id TEXT, material_type TEXT, question_type TEXT, stem TEXT, options TEXT, reference_answer TEXT, reference_solution TEXT, max_score REAL, scoring_points TEXT, indicator_id TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS teacher_exams (id TEXT PRIMARY KEY, exam_date TEXT, topics TEXT, description TEXT, available_questions TEXT, course_id TEXT, grading_rule_version TEXT, scoring_rule_version TEXT, mapping_version TEXT);
CREATE TABLE IF NOT EXISTS teacher_batches (id TEXT PRIMARY KEY, filename TEXT, status TEXT, counts_json TEXT, errors_json TEXT, preprocess_status TEXT DEFAULT 'review', preprocess_report_json TEXT, owner_id TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS teacher_grade_attempts (
 id TEXT PRIMARY KEY, task_id TEXT, answer_id TEXT, student_code TEXT, class_code TEXT,
 batch_id TEXT, student_id TEXT, question_id TEXT, event_id TEXT, revision INTEGER, attempt_number INTEGER, model TEXT,
 prompt_version TEXT, start_time TEXT, end_time TEXT, failure_stage TEXT, error_type TEXT,
 http_status INTEGER, error_message TEXT, retryable INTEGER, result_id TEXT, duration_ms INTEGER
);
CREATE TABLE IF NOT EXISTS teacher_analysis_runs (id TEXT PRIMARY KEY, task_id TEXT, scope TEXT, scope_id TEXT, algorithm_version TEXT, parameter_json TEXT, result_json TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS teacher_scoring_point_confirmations (id TEXT PRIMARY KEY, grade_run_id TEXT, answer_id TEXT, scoring_point_id TEXT, original_ai_score REAL, confirmed_score REAL, max_score REAL, confirmation_status TEXT, teacher_id TEXT, note TEXT, scoring_rule_version TEXT, created_at TEXT, updated_at TEXT, UNIQUE(grade_run_id, scoring_point_id));
CREATE TABLE IF NOT EXISTS teacher_scoring_point_confirmation_history (id TEXT PRIMARY KEY, confirmation_id TEXT, grade_run_id TEXT, answer_id TEXT, scoring_point_id TEXT, before_json TEXT, after_json TEXT, version INTEGER, operator_id TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS teacher_data_issues (
 id TEXT PRIMARY KEY, task_id TEXT, issue_key TEXT, issue_type TEXT, category TEXT,
 severity TEXT, source_file TEXT, source_row_number INTEGER, student_id TEXT,
 student_code TEXT, class_id TEXT, class_code TEXT, question_id TEXT, event_id TEXT,
 revision INTEGER, raw_value TEXT, expected_value TEXT, reason TEXT, detection_rule TEXT,
 handling_status TEXT, handling_result TEXT, created_at TEXT, resolved_at TEXT, resolved_by TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_teacher_data_issues_key ON teacher_data_issues(task_id,issue_key);
CREATE TABLE IF NOT EXISTS teacher_import_files (id TEXT PRIMARY KEY, batch_id TEXT, filename TEXT, rows INTEGER, fields_json TEXT, errors_json TEXT);
CREATE TABLE IF NOT EXISTS teacher_quality_gate_overrides (id TEXT PRIMARY KEY, batch_id TEXT, teacher_id TEXT, reason TEXT, issues_json TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS teacher_enrollments (id TEXT PRIMARY KEY, batch_id TEXT, student_id TEXT, class_id TEXT, code TEXT, name TEXT, grade INTEGER, identity_status TEXT DEFAULT 'unverified', identity_note TEXT, created_at TEXT, updated_at TEXT, UNIQUE(batch_id, student_id));
CREATE TABLE IF NOT EXISTS teacher_answers (
 id TEXT PRIMARY KEY, student_id TEXT, question_id TEXT, event_id TEXT, revision INTEGER, submit_time TEXT,
 original_answer TEXT, solution_process TEXT, original_score REAL, max_score REAL, score_status TEXT, grading_status TEXT DEFAULT 'pending',
 raw_json TEXT, import_batch_id TEXT, exam_node_id TEXT, course_id TEXT, standard_id TEXT, UNIQUE(import_batch_id, event_id, revision)
);
CREATE TABLE IF NOT EXISTS teacher_grade_runs (
 id TEXT PRIMARY KEY, answer_id TEXT, student_id TEXT, question_id TEXT, event_id TEXT, revision INTEGER,
 grading_version INTEGER, grading_model TEXT, grading_prompt_version TEXT, grader_version TEXT,
 course_id TEXT, standard_id TEXT, standard_version TEXT, scoring_rule_version TEXT, textbook_course_id TEXT, textbook_unit_id TEXT, textbook_context TEXT,
 score REAL, max_score REAL, is_correct TEXT, step_results_json TEXT, error_type TEXT, feedback TEXT, judgment_reason TEXT,
 needs_review INTEGER DEFAULT 0, status TEXT, raw_response TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS teacher_grade_jobs (
 id TEXT PRIMARY KEY, status TEXT, total INTEGER, processed INTEGER, completed INTEGER, failed INTEGER, needs_review INTEGER,
 current_answer_id TEXT, filters_json TEXT, error TEXT, created_at TEXT, updated_at TEXT, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS teacher_grade_job_items (
 id TEXT PRIMARY KEY, job_id TEXT, answer_id TEXT, status TEXT, grade_id TEXT, error TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS teacher_corrections (
 id TEXT PRIMARY KEY, grade_run_id TEXT, before_json TEXT, after_json TEXT, note TEXT, teacher_id TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_teacher_answers_student ON teacher_answers(student_id, question_id);
CREATE INDEX IF NOT EXISTS idx_teacher_answers_event ON teacher_answers(event_id, revision);
CREATE INDEX IF NOT EXISTS idx_teacher_questions_topic ON teacher_questions(topic_id);
CREATE INDEX IF NOT EXISTS idx_teacher_runs_answer ON teacher_grade_runs(answer_id, created_at);
CREATE INDEX IF NOT EXISTS idx_teacher_job_items ON teacher_grade_job_items(job_id, status);
CREATE INDEX IF NOT EXISTS idx_teacher_enrollments_batch ON teacher_enrollments(batch_id, student_id);
CREATE INDEX IF NOT EXISTS idx_teacher_enrollments_student ON teacher_enrollments(student_id);
CREATE INDEX IF NOT EXISTS idx_teacher_students_class ON teacher_students(class_id);
"""


_SCHEMA_READY = False
SCHEMA_VERSION = 3


def init() -> None:
    """请求级初始化：只保证表结构存在一次，不再每次请求做全量回填/UPSERT。

    历史数据回填与迁移在 bootstrap() 里执行（进程启动一次），并通过
    schema_migrations 记录版本，避免重复扫描全表。
    """
    global _SCHEMA_READY
    db.init()
    if _SCHEMA_READY:
        return
    with _LOCK:
        if _SCHEMA_READY:
            return
        c = db.conn()
        c.executescript(SCHEMA)
        migrations = {
            "teacher_questions": [("standard_id", "TEXT"), ("course_id", "TEXT")],
            "teacher_exams": [("course_id", "TEXT"), ("grading_rule_version", "TEXT"), ("scoring_rule_version", "TEXT"), ("mapping_version", "TEXT")],
            "teacher_answers": [("course_id", "TEXT"), ("standard_id", "TEXT"), ("question_version_id", "TEXT"), ("grading_status", "TEXT DEFAULT 'pending'")],
            "teacher_grade_runs": [("question_version_id", "TEXT"), ("grader_version", "TEXT"), ("course_id", "TEXT"), ("standard_id", "TEXT"), ("standard_version", "TEXT"), ("scoring_rule_version", "TEXT"), ("mapping_version", "TEXT"), ("textbook_course_id", "TEXT"), ("textbook_unit_id", "TEXT"), ("textbook_context", "TEXT"), ("step_analysis_status", "TEXT"), ("needs_review", "INTEGER DEFAULT 0")],
            "teacher_grade_jobs": [("completed", "INTEGER DEFAULT 0"), ("needs_review", "INTEGER DEFAULT 0"), ("current_answer_id", "TEXT"), ("finished_at", "TEXT"), ("batch_id", "TEXT"), ("textbook_course_id", "TEXT"), ("textbook_unit_id", "TEXT"), ("textbook_context", "TEXT"), ("provider", "TEXT"), ("model", "TEXT"), ("prompt_version", "TEXT"), ("pause_requested", "INTEGER DEFAULT 0"), ("worker_id", "TEXT"), ("started_at", "TEXT"), ("paused_at", "TEXT"), ("resumed_at", "TEXT"), ("last_checkpoint_at", "TEXT"), ("heartbeat_at", "TEXT"), ("retry_of", "TEXT"), ("cancellation_requested", "INTEGER DEFAULT 0")],
            "teacher_batches": [("preprocess_status", "TEXT DEFAULT 'review'"), ("preprocess_report_json", "TEXT"), ("owner_id", "TEXT")],
            "teacher_grade_attempts": [("batch_id", "TEXT"), ("student_id", "TEXT"), ("duration_ms", "INTEGER")],
            "teacher_grade_job_items": [("attempt_count", "INTEGER DEFAULT 0"), ("current_attempt", "INTEGER DEFAULT 1"), ("started_at", "TEXT"), ("completed_at", "TEXT"), ("last_error", "TEXT"), ("grading_version", "INTEGER"), ("checkpoint_status", "TEXT"), ("updated_at", "TEXT")],
            "teacher_scoring_point_confirmations": [("version", "INTEGER DEFAULT 1"), ("source", "TEXT")],
        }
        for table, columns in migrations.items():
            existing = {row[1] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}
            for column, ddl in columns:
                if column not in existing:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        c.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_teacher_batches_owner ON teacher_batches(owner_id, created_at)")
        c.commit()
        _SCHEMA_READY = True


def recover_stale_jobs() -> Dict[str, int]:
    """把心跳过期的 running/pausing 任务恢复为 paused，并归还 running item。

    服务重启或进程崩溃后由 bootstrap 调用；测试可直接调用验证恢复语义。
    """
    cutoff = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")
    jobs = db.q("SELECT id FROM teacher_grade_jobs WHERE status IN ('running','pausing') AND (heartbeat_at IS NULL OR heartbeat_at < ?)", (cutoff,))
    with db.transaction():
        db.x("UPDATE teacher_grade_jobs SET status='paused', pause_requested=1, updated_at=? WHERE status IN ('running','pausing') AND (heartbeat_at IS NULL OR heartbeat_at < ?)", (now(), cutoff))
        items = db.q1("SELECT COUNT(*) n FROM teacher_grade_job_items WHERE status='running' AND job_id IN (SELECT id FROM teacher_grade_jobs WHERE status='paused')")["n"]
        db.x("UPDATE teacher_grade_job_items SET status='pending', updated_at=? WHERE status='running' AND job_id IN (SELECT id FROM teacher_grade_jobs WHERE status='paused')", (now(),))
    return {"jobs": len(jobs), "items": int(items or 0)}


def bootstrap() -> None:
    """进程启动时的迁移与回填：记录版本，重复调用安全但不会重复扫描。"""
    init()
    applied = {row["version"] for row in db.q("SELECT version FROM schema_migrations")}
    with _LOCK:
        c = db.conn()
        if 1 not in applied:
            migrate_answer_scope()
            c.execute("UPDATE teacher_answers SET grading_status=COALESCE(NULLIF(grading_status,''),'pending')")
            c.execute("UPDATE teacher_answers SET grading_status='graded' WHERE id IN (SELECT answer_id FROM teacher_grade_runs WHERE status NOT IN ('failed'))")
            c.execute("UPDATE teacher_answers SET grading_status='needs_review' WHERE id IN (SELECT answer_id FROM teacher_grade_runs WHERE needs_review=1)")
            c.execute("""UPDATE teacher_answers
                SET course_id = COALESCE(course_id, (SELECT q.course_id FROM teacher_questions q WHERE q.id=teacher_answers.question_id)),
                    standard_id = COALESCE(standard_id, (SELECT q.standard_id FROM teacher_questions q WHERE q.id=teacher_answers.question_id))
                WHERE course_id IS NULL OR standard_id IS NULL""")
            c.commit()
            db.x("INSERT OR REPLACE INTO schema_migrations (version, applied_at) VALUES (?,?)", (1, now()))
        if 2 not in applied:
            ensure_question_versions()
            db.x("INSERT OR REPLACE INTO schema_migrations (version, applied_at) VALUES (?,?)", (2, now()))
        if 3 not in applied:
            recover_stale_jobs()
            db.x("INSERT OR REPLACE INTO schema_migrations (version, applied_at) VALUES (?,?)", (3, now()))
        if 4 not in applied:
            # v1.5.11 角色收敛：游客不得再因 role 缺失/旧数据而拥有教师权限。
            migrated = []
            for row in db.q("SELECT id, email, role, auth_type FROM xueban_users WHERE auth_type='guest' AND COALESCE(role,'') <> 'guest'"):
                db.x("UPDATE xueban_users SET role='guest' WHERE id=?", (row["id"],))
                migrated.append({"user_id": row["id"], "to": "guest", "reason": "auth_type=guest"})
            for row in db.q("SELECT id, email, role, auth_type FROM xueban_users WHERE auth_type<>'guest' AND (role IS NULL OR role='')"):
                bound = db.q1("SELECT id FROM teacher_students WHERE user_id=?", (row["id"],))
                target = "student" if bound else "guest"
                db.x("UPDATE xueban_users SET role=? WHERE id=?", (target, row["id"]))
                migrated.append({"user_id": row["id"], "to": target, "reason": "missing role, least privilege"})
            for item in migrated:
                db.x("INSERT INTO xueban_account_audit (id,user_id,actor_id,action,detail_json,created_at) VALUES (?,?,?,?,?,?)", (db.new_id("accountaudit"), item["user_id"], "system", "role_migration", json.dumps(item, ensure_ascii=False), now()))
            db.x("INSERT OR REPLACE INTO schema_migrations (version, applied_at) VALUES (?,?)", (4, now()))
        if 5 not in applied:
            # 为 v1.5.10 及以前的批次补任务内学生快照；默认身份未验证，不推断同一人。
            db.x("""
                INSERT OR IGNORE INTO teacher_enrollments (id,batch_id,student_id,class_id,code,name,grade,identity_status,created_at,updated_at)
                SELECT 'enroll_' || hex(randomblob(8)), a.import_batch_id, s.id, s.class_id, s.code, s.name, s.grade, 'unverified', ?, ?
                FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id
                GROUP BY a.import_batch_id, s.id
            """, (now(), now()))
            db.x("INSERT OR REPLACE INTO schema_migrations (version, applied_at) VALUES (?,?)", (5, now()))


def reset_runtime_cache() -> None:
    """测试/热重载用：清掉进程内“已初始化”标记。"""
    global _SCHEMA_READY
    _SCHEMA_READY = False


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


# 任务内题目内容一律以作答绑定的 question_version 为准；只有旧数据没有版本时才回退全局题表。
# 回退是显式的：question_source 会告诉调用方/前端这次用的是版本还是全局兼容表。
QUESTION_JOIN = """
    LEFT JOIN teacher_question_versions v ON v.version_id = a.question_version_id
    LEFT JOIN teacher_questions q ON q.id = a.question_id
"""
QUESTION_COLUMNS = """
    COALESCE(v.stem, q.stem) AS stem,
    COALESCE(v.options, q.options) AS options,
    COALESCE(v.reference_answer, q.reference_answer) AS reference_answer,
    COALESCE(v.reference_solution, q.reference_solution) AS reference_solution,
    COALESCE(v.max_score, q.max_score) AS question_max_score,
    COALESCE(v.scoring_points, q.scoring_points) AS scoring_points,
    COALESCE(v.question_type, q.question_type) AS question_type,
    COALESCE(v.topic_id, q.topic_id) AS topic_id,
    COALESCE(v.indicator_id, q.indicator_id) AS indicator_id,
    COALESCE(v.material_type, q.material_type) AS material_type,
    CASE WHEN v.version_id IS NOT NULL THEN 'version' ELSE 'global_fallback' END AS question_source
"""


def _table_exists(name: str) -> bool:
    return bool(db.q1("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)))


def question_version_fallback_count(batch_id: str) -> int:
    """返回当前任务里没有题目版本、只能回退全局题表的作答数。"""
    row = db.q1(
        "SELECT COUNT(*) n FROM teacher_answers a LEFT JOIN teacher_question_versions v ON v.version_id=a.question_version_id "
        "WHERE a.import_batch_id=? AND v.version_id IS NULL",
        (batch_id,),
    ) or {}
    return int(row.get("n") or 0)


def teacher(user: Dict[str, Any]) -> bool:
    """严格角色白名单：缺失/未知角色一律不是教师（v1.5.11 安全修复）。"""
    return str(user.get("role") or "").strip().lower() == "teacher"


def student(user: Dict[str, Any]) -> bool:
    return str(user.get("role") or "").strip().lower() == "student"


def require_member(user: Dict[str, Any]) -> None:
    """教师或学生；游客与未知角色一律拒绝教师/学生业务接口。"""
    if not (teacher(user) or student(user)):
        raise HTTPException(403, "当前身份无权访问该资源")


def migrate_answer_scope() -> None:
    c = db.conn()
    row = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='teacher_answers'").fetchone()
    if not row or "UNIQUE(event_id, revision)" not in (row[0] or ""):
        return
    sql = row[0].replace("CREATE TABLE teacher_answers", "CREATE TABLE teacher_answers_new").replace("UNIQUE(event_id, revision)", "UNIQUE(import_batch_id, event_id, revision)")
    cols = [item[1] for item in c.execute("PRAGMA table_info(teacher_answers)").fetchall()]
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute(sql)
        c.execute(f"INSERT INTO teacher_answers_new ({','.join(cols)}) SELECT {','.join(cols)} FROM teacher_answers")
        c.execute("DROP TABLE teacher_answers")
        c.execute("ALTER TABLE teacher_answers_new RENAME TO teacher_answers")
        c.commit()
    except Exception:
        c.rollback()
        raise


def ensure_question_versions() -> None:
    rows = db.q("SELECT DISTINCT a.import_batch_id batch_id, a.question_id FROM teacher_answers a WHERE a.import_batch_id IS NOT NULL AND a.import_batch_id != ''")
    for row in rows:
        q = db.q1("SELECT * FROM teacher_questions WHERE id=?", (row["question_id"],))
        if not q:
            continue
        version_id = f"qv:{row['batch_id']}:{row['question_id']}"
        db.x("INSERT OR IGNORE INTO teacher_question_versions (version_id,batch_id,question_id,topic_id,standard_id,course_id,material_type,question_type,stem,options,reference_answer,reference_solution,max_score,scoring_points,indicator_id,source,source_order,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (version_id, row["batch_id"], row["question_id"], q.get("topic_id"), q.get("standard_id"), q.get("course_id"), q.get("material_type"), q.get("question_type"), q.get("stem"), q.get("options"), q.get("reference_answer"), q.get("reference_solution"), q.get("max_score"), q.get("scoring_points"), q.get("indicator_id"), q.get("source"), 0, now()))
        db.x("UPDATE teacher_answers SET question_version_id=? WHERE import_batch_id=? AND question_id=? AND (question_version_id IS NULL OR question_version_id='')", (version_id, row["batch_id"], row["question_id"]))
        db.x("UPDATE teacher_grade_runs SET question_version_id=? WHERE answer_id IN (SELECT id FROM teacher_answers WHERE import_batch_id=? AND question_id=?) AND (question_version_id IS NULL OR question_version_id='')", (version_id, row["batch_id"], row["question_id"]))


def owned_batch_ids(user: Dict[str, Any], batch_ids: List[str]) -> List[str]:
    if not teacher(user):
        return batch_ids
    own = str(user.get("id") or "")
    allowed = []
    for bid in batch_ids:
        try:
            owned_batch(bid, user)
            allowed.append(bid)
        except HTTPException:
            continue
    return allowed


def latest_owned_batch(user: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return db.q1("SELECT * FROM teacher_batches WHERE owner_id IS NULL OR owner_id=? ORDER BY created_at DESC LIMIT 1", (user.get("id"),))


def student_in_scope(student_id: str, user: Dict[str, Any]) -> Dict[str, Any]:
    """学生必须出现在当前教师可见任务的作答中，否则视为不存在。"""
    row = db.q1("SELECT * FROM teacher_students WHERE id=?", (student_id,))
    if not row:
        raise HTTPException(404, "学生不存在")
    hit = db.q1("""
        SELECT 1 AS found FROM teacher_answers a
        JOIN teacher_batches b ON b.id = a.import_batch_id
        WHERE a.student_id=? AND (b.owner_id IS NULL OR b.owner_id=?) LIMIT 1
    """, (student_id, user.get("id")))
    if teacher(user) and not hit:
        raise HTTPException(404, "学生不存在")
    return row


def require_teacher(user: Dict[str, Any]) -> None:
    if not teacher(user):
        raise HTTPException(403, "仅教师可执行该操作")


def owned_batch(batch_id: str, user: Dict[str, Any]) -> Dict[str, Any]:
    """任务归属校验：新任务必须属于当前教师；v1.5.9 及以前的历史任务 owner_id 为空，作为历史共享任务。

    不存在与无权访问统一返回 404，避免泄露资源是否存在。
    """
    row = db.q1("SELECT * FROM teacher_batches WHERE id=?", (batch_id,))
    if not row:
        raise HTTPException(404, "任务不存在")
    owner = str(row.get("owner_id") or "")
    if owner and owner != user.get("id"):
        raise HTTPException(404, "任务不存在")
    return row


def batch_scope_sql(column: str = "id") -> str:
    """教师可见任务范围：自己的任务 + 历史无主任务。"""
    return f"({column}.owner_id IS NULL OR {column}.owner_id = ?)"


def owned_job(job_id: str, user: Dict[str, Any]) -> Dict[str, Any]:
    job = db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_id,))
    if not job:
        raise HTTPException(404, "任务不存在")
    owned_batch(job.get("batch_id") or "", user)
    return job


def owned_answer(answer_id: str, user: Dict[str, Any]) -> Dict[str, Any]:
    answer = db.q1("SELECT * FROM teacher_answers WHERE id=?", (answer_id,))
    if not answer:
        raise HTTPException(404, "作答不存在")
    if teacher(user):
        owned_batch(answer.get("import_batch_id") or "", user)
    return answer


def owned_grade(grade_id: str, user: Dict[str, Any]) -> Dict[str, Any]:
    grade = db.q1("SELECT * FROM teacher_grade_runs WHERE id=?", (grade_id,))
    if not grade:
        raise HTTPException(404, "判题结果不存在")
    owned_answer(grade.get("answer_id") or "", user)
    return grade


def owned_scoring_point(sp_id: str, user: Dict[str, Any]) -> Dict[str, Any]:
    point = db.q1("SELECT * FROM scoring_points WHERE id=?", (sp_id,))
    if not point:
        raise HTTPException(404, "评分点不存在")
    batch_id = str(point.get("batch_id") or "")
    if batch_id:
        owned_batch(batch_id, user)
    elif teacher(user):
        # 全局 legacy 评分点只允许读取；写操作必须在端点内拒绝
        pass
    return point


def student_for(user: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return db.q1("SELECT * FROM teacher_students WHERE user_id = ?", (user["id"],))


def ensure_account(email: str, password: Optional[str], display: str, role: str) -> Dict[str, Any]:
    row = db.q1("SELECT * FROM xueban_users WHERE email = ?", (email,))
    if row:
        db.x("UPDATE xueban_users SET role = ?, display_name = ? WHERE id = ?", (role, display, row["id"]))
        return db.q1("SELECT * FROM xueban_users WHERE id = ?", (row["id"],))
    password = password or secrets.token_urlsafe(24)
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
    uid = db.new_id(role)
    db.x("INSERT INTO xueban_users (id,email,password_hash,password_salt,display_name,auth_type,role,must_change_password,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
         (uid, email, digest, salt, display, "password", role, 1 if role == "student" else 0, now()))
    return db.q1("SELECT * FROM xueban_users WHERE id = ?", (uid,))


def ensure_student_account(code: str, display: str) -> Tuple[Dict[str, Any], str]:
    """返回 (学生账号, 初始密码)。只有新建账号时才返回初始密码，且库里只存哈希。"""
    email = f"{code.lower()}@xueban.local"
    row = db.q1("SELECT * FROM xueban_users WHERE email = ?", (email,))
    if row:
        if (row.get("role") or "") != "student":
            db.x("UPDATE xueban_users SET role='student', display_name=? WHERE id=?", (display, row["id"]))
            row = db.q1("SELECT * FROM xueban_users WHERE id=?", (row["id"],))
        return row, ""
    password = secrets.token_urlsafe(12)
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
    uid = db.new_id("student")
    db.x("INSERT INTO xueban_users (id,email,password_hash,password_salt,display_name,auth_type,role,must_change_password,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
         (uid, email, digest, salt, display, "password", "student", 1, now()))
    return db.q1("SELECT * FROM xueban_users WHERE id = ?", (uid,)), password


def read_csvs(files: Optional[List[UploadFile]]) -> Tuple[Dict[str, List[Dict[str, str]]], Dict[str, Dict[str, Any]]]:
    if not files:
        raise HTTPException(422, "必须上传至少一个 CSV 文件；正式流程不读取仓库测试数据")
    tables: Dict[str, List[Dict[str, str]]] = {}
    meta: Dict[str, Dict[str, Any]] = {}
    for upload in files:
        try:
            text = upload.file.read().decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise HTTPException(422, f"{upload.filename} 不是 UTF-8 编码") from exc
        reader = csv.DictReader(io.StringIO(text))
        name = upload.filename or "upload.csv"
        tables[name] = list(reader)
        meta[name] = {"rows": len(tables[name]), "fields": reader.fieldnames or []}
    return tables, meta


def find_table(tables: Dict[str, List[Dict[str, str]]], *keywords: str) -> Optional[List[Dict[str, str]]]:
    for name, rows in tables.items():
        if any(keyword in name for keyword in keywords):
            return rows
    return None


def score_status(value: str) -> str:
    value = (value or "").strip()
    if value in {"未作答", "空", "未提交"}:
        return "unanswered"
    if value in {"已评分", "已判题"}:
        return "graded"
    return "pending"


REQUIRED = {
    "模拟学生": ["学生编号", "模拟班级编号", "年级"],
    "课程要求": ["主题编号", "主题", "主题目标", "指标编号", "观察指标", "课标整理编号"],
    "测验节点": ["节点编号", "模拟测验日期", "已教及测量主题", "可用题目编号"],
    "题目与材料": ["材料编号", "主题编号", "题型", "题干或内容", "参考答案", "满分", "评分点", "指标编号"],
    "作答记录": ["记录编号", "学生编号", "材料编号", "节点编号", "作答事件编号", "修订版本", "模拟提交时间", "评分状态", "得分", "满分", "学生最终作答", "解答过程"],
}


def validate(tables: Dict[str, List[Dict[str, str]]], meta: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    errors: List[Dict[str, Any]] = []
    found: Dict[str, List[Dict[str, str]]] = {}
    for label, fields in REQUIRED.items():
        rows = find_table(tables, label)
        if rows is None:
            errors.append({"type": "missing_file", "table": label, "message": f"缺少 {label}.csv"})
            continue
        found[label] = rows
        name = next(name for name, value in tables.items() if value is rows)
        actual = set(meta.get(name, {}).get("fields") or [])
        missing = [field for field in fields if field not in actual]
        if missing:
            errors.append({"type": "missing_fields", "table": label, "fields": missing, "message": f"{label} 缺少字段：{', '.join(missing)}"})
    students = found.get("模拟学生", [])
    questions = found.get("题目与材料", [])
    exams = found.get("测验节点", [])
    answers = found.get("作答记录", [])
    student_codes = {str(row.get("学生编号", "")).strip() for row in students}
    question_codes = {str(row.get("材料编号", "")).strip() for row in questions}
    exam_codes = {str(row.get("节点编号", "")).strip() for row in exams}
    topics = {str(row.get("主题编号", "")).strip() for row in found.get("课程要求", [])}
    for row in questions:
        if not str(row.get("参考答案", "")).strip() or not str(row.get("评分点", "")).strip():
            errors.append({"type": "missing_scoring_rule", "message": f"题目 {row.get('材料编号')} 缺参考答案或评分点"})
        if str(row.get("主题编号", "")).strip() not in topics:
            errors.append({"type": "orphan_question", "message": f"题目 {row.get('材料编号')} 的主题无课程要求"})
    for row in students:
        if not str(row.get("年级", "")).strip().isdigit():
            errors.append({"type": "invalid_type", "message": f"学生 {row.get('学生编号')} 年级不是整数"})
    for row in questions:
        try:
            float(row.get("满分") or 0)
        except (TypeError, ValueError):
            errors.append({"type": "invalid_type", "message": f"题目 {row.get('材料编号')} 满分不是数字"})
    for row in answers:
        try:
            int(row.get("修订版本") or 0); float(row.get("得分") or 0); float(row.get("满分") or 0)
        except (TypeError, ValueError):
            errors.append({"type": "invalid_type", "message": f"作答 {row.get('记录编号')} 修订版本/得分不是数字"})
    seen = set()
    for row in answers:
        key = (str(row.get("作答事件编号", "")).strip(), str(row.get("修订版本", "1")).strip())
        if key in seen:
            errors.append({"type": "duplicate_event_revision", "message": f"重复 event_id+revision：{key[0]} r{key[1]}"})
        seen.add(key)
        if str(row.get("学生编号", "")).strip() not in student_codes:
            errors.append({"type": "orphan_answer", "message": f"作答 {row.get('记录编号')} 学生不存在"})
        if str(row.get("材料编号", "")).strip() not in question_codes:
            errors.append({"type": "orphan_answer", "message": f"作答 {row.get('记录编号')} 题目不存在"})
        if str(row.get("节点编号", "")).strip() not in exam_codes:
            errors.append({"type": "orphan_answer", "message": f"作答 {row.get('记录编号')} 测验节点不存在"})
    return errors




def import_rows(tables: Dict[str, List[Dict[str, str]]], filename: str = "upload", owner_id: str = "") -> Dict[str, Any]:
    init()
    students = find_table(tables, "模拟学生") or []
    requirements = find_table(tables, "课程要求") or []
    questions = find_table(tables, "题目与材料") or []
    exams = find_table(tables, "测验节点") or []
    answers = find_table(tables, "作答记录") or []
    errors: List[Dict[str, Any]] = []
    student_credentials: List[Dict[str, str]] = []
    batch_id = db.new_id("batch")
    counts = {"students": 0, "classes": 0, "courses": 0, "standards": 0, "requirements": 0, "questions": 0, "exams": 0, "answers": 0, "skipped": 0}
    with db.transaction():
        for row in requirements:
            topic = str(row.get("主题编号") or "").strip()
            indicator = str(row.get("指标编号") or "").strip()
            if not topic:
                continue
            standard_id = ""
            db.x("INSERT OR REPLACE INTO teacher_topics (id,code,title,goal,course_id,standard_id,created_at) VALUES (?,?,?,?,?,?,?)", (f"topic_{topic}", topic, row.get("主题"), row.get("主题目标"), "", standard_id, now()))
            if indicator:
                db.x("INSERT OR REPLACE INTO teacher_indicators (id,code,topic_id,description) VALUES (?,?,?,?)", (f"indicator_{indicator}", indicator, f"topic_{topic}", row.get("观察指标")))
            counts["requirements"] += 1
        for row in students:
            code = str(row.get("学生编号", "")).strip()
            class_code = str(row.get("模拟班级编号", "")).strip() or "DC01"
            class_id = f"class_{class_code}"
            db.x("INSERT OR IGNORE INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", (class_id, class_code, f"{class_code} 班", int(row.get("年级") or 7), now()))
            user, initial_password = ensure_student_account(code, code)
            if initial_password:
                student_credentials.append({"code": code, "email": f"{code.lower()}@xueban.local", "initial_password": initial_password, "must_change_password": True})
            db.x("INSERT OR REPLACE INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", (f"student_{code}", user["id"], code, class_id, code, int(row.get("年级") or 7), now()))
            # 任务内学生快照：同编号不等于同一人，身份默认未验证，需教师确认。
            db.x("INSERT OR IGNORE INTO teacher_enrollments (id,batch_id,student_id,class_id,code,name,grade,identity_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (db.new_id("enroll"), batch_id, f"student_{code}", class_id, code, code, int(row.get("年级") or 7), "unverified", now(), now()))
            counts["students"] += 1
        counts["classes"] = len({str(row.get("模拟班级编号", "")).strip() for row in students})
        question_versions = {}
        for row in questions:
            qid = str(row.get("材料编号", "")).strip()
            topic = str(row.get("主题编号", "")).strip()
            standard_id = ""
            db.x("INSERT OR REPLACE INTO teacher_questions (id,topic_id,standard_id,course_id,material_type,question_type,stem,options,reference_answer,reference_solution,max_score,scoring_points,indicator_id,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (qid, topic, standard_id, "", row.get("材料类型"), row.get("题型"), row.get("题干或内容"), row.get("选项"), row.get("参考答案"), row.get("参考解析"), float(row.get("满分") or 0), row.get("评分点"), row.get("指标编号"), row.get("来源或资料文件")))
            version_id = f"qv:{batch_id}:{qid}"
            db.x("INSERT OR REPLACE INTO teacher_question_versions (version_id,batch_id,question_id,topic_id,standard_id,course_id,material_type,question_type,stem,options,reference_answer,reference_solution,max_score,scoring_points,indicator_id,source,source_order,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (version_id, batch_id, qid, topic, standard_id, "", row.get("材料类型"), row.get("题型"), row.get("题干或内容"), row.get("选项"), row.get("参考答案"), row.get("参考解析"), float(row.get("满分") or 0), row.get("评分点"), row.get("指标编号"), row.get("来源或资料文件"), len(questions), now()))
            question_versions[qid] = version_id
            counts["questions"] += 1
        for row in exams:
            nid = str(row.get("节点编号", "")).strip()
            if nid:
                db.x("INSERT OR REPLACE INTO teacher_exams (id,exam_date,topics,description,available_questions,course_id) VALUES (?,?,?,?,?,?)", (nid, row.get("模拟测验日期"), row.get("已教及测量主题"), row.get("说明"), row.get("可用题目编号"), ""))
                counts["exams"] += 1
        for row in answers:
            event = str(row.get("作答事件编号", "")).strip()
            revision = int(row.get("修订版本") or 1)
            code = str(row.get("学生编号", "")).strip()
            qid = str(row.get("材料编号", "")).strip()
            student = db.q1("SELECT id FROM teacher_students WHERE code = ?", (code,))
            question = db.q1("SELECT id FROM teacher_questions WHERE id = ?", (qid,))
            if not student or not question:
                errors.append({"type": "orphan_answer", "message": f"{event} 无学生或题目"})
                continue
            duplicate = db.q1(
                "SELECT id FROM teacher_answers WHERE import_batch_id=? AND event_id=? AND revision=?",
                (batch_id, event, revision),
            )
            if duplicate:
                counts["skipped"] += 1
                errors.append({"type": "duplicate_answer_in_batch", "row": row.get("记录编号"), "event_id": event, "revision": revision, "answer_id": duplicate["id"], "message": f"本任务内已存在 {event} r{revision}，已跳过重复作答"})
                continue
            db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,submit_time,original_answer,solution_process,original_score,max_score,score_status,raw_json,import_batch_id,exam_node_id,course_id,standard_id,question_version_id,grading_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"{batch_id}:{event}-r{revision}", student["id"], qid, event, revision, row.get("模拟提交时间"), row.get("学生最终作答"), row.get("解答过程"), float(row.get("得分") or 0), float(row.get("满分") or 0), score_status(row.get("评分状态") or ""), json.dumps(row, ensure_ascii=False), batch_id, row.get("节点编号"), "", "", question_versions.get(qid, f"qv:{batch_id}:{qid}"), "pending"))
            counts["answers"] += 1
        db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,owner_id,created_at) VALUES (?,?,?,?,?,?,?)", (batch_id, filename, "committed", json.dumps(counts, ensure_ascii=False), json.dumps(errors, ensure_ascii=False), owner_id, now()))
        db.x("INSERT INTO teacher_import_files (id,batch_id,filename,rows,fields_json,errors_json) VALUES (?,?,?,?,?,?)", (db.new_id("file"), batch_id, filename, len(answers), json.dumps(list(tables), ensure_ascii=False), json.dumps(errors[:100], ensure_ascii=False)))
    return {"batch_id": batch_id, "counts": counts, "errors": errors[:100], "student_credentials": student_credentials}


# 题表同时承载“真题”和“教材/学习卡”材料行；统计口径必须分开，不能把材料算成题目。
MATERIAL_CONDITION = "(COALESCE(material_type,'') IN ('教材','学习卡','材料') OR (COALESCE(max_score,0) <= 0 AND COALESCE(scoring_points,'') = ''))"


def question_stats() -> Dict[str, int]:
    row = db.q1(f"SELECT COUNT(*) total, SUM(CASE WHEN {MATERIAL_CONDITION} THEN 1 ELSE 0 END) materials FROM teacher_questions") or {}
    total = int(row.get("total") or 0)
    materials = int(row.get("materials") or 0)
    return {"questions": total - materials, "materials": materials, "question_rows": total}


@router.get("/meta")
def meta(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); require_teacher(user)
    own = str(user.get("id") or "")
    classes = db.q("""
        SELECT DISTINCT c.* FROM teacher_classes c
        JOIN teacher_students s ON s.class_id=c.id
        JOIN teacher_answers a ON a.student_id=s.id
        JOIN teacher_batches b ON b.id=a.import_batch_id
        WHERE b.owner_id IS NULL OR b.owner_id=?
        ORDER BY c.code
    """, (own,))
    exams = db.q("""
        SELECT DISTINCT e.* FROM teacher_exams e
        JOIN teacher_answers a ON a.exam_node_id=e.id
        JOIN teacher_batches b ON b.id=a.import_batch_id
        WHERE b.owner_id IS NULL OR b.owner_id=?
        ORDER BY e.id
    """, (own,))
    return {"classes": classes, "exams": exams}


@router.get("/summary")
def summary(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); require_member(user)
    if student(user):
        student_row = student_for(user)
        if not student_row:
            raise HTTPException(403, "当前账号未绑定学生档案")
        return {"student": True, "answers": db.q1("SELECT COUNT(*) n FROM teacher_answers WHERE student_id=?", (student_row["id"],))["n"], "pending": db.q1("SELECT COUNT(*) n FROM teacher_answers WHERE student_id=? AND grading_status='pending'", (student_row["id"],))["n"], "review": db.q1("SELECT COUNT(*) n FROM teacher_grade_runs WHERE student_id=? AND needs_review=1", (student_row["id"],))["n"]}
    own = str(user.get("id") or "")
    scope = "SELECT id FROM teacher_batches WHERE owner_id IS NULL OR owner_id=?"
    stats = question_stats()
    return {
        "students": db.q1(f"SELECT COUNT(DISTINCT a.student_id) n FROM teacher_answers a WHERE a.import_batch_id IN ({scope})", (own,))["n"],
        "classes": db.q1(f"SELECT COUNT(DISTINCT s.class_id) n FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id WHERE a.import_batch_id IN ({scope})", (own,))["n"],
        "courses": db.q1("SELECT COUNT(*) n FROM teacher_courses")["n"], **stats,
        "answers": db.q1(f"SELECT COUNT(*) n FROM teacher_answers WHERE import_batch_id IN ({scope})", (own,))["n"],
        "pending": db.q1(f"SELECT COUNT(*) n FROM teacher_answers WHERE grading_status='pending' AND import_batch_id IN ({scope})", (own,))["n"],
        "graded": db.q1(f"SELECT COUNT(*) n FROM teacher_grade_runs WHERE answer_id IN (SELECT id FROM teacher_answers WHERE import_batch_id IN ({scope}))", (own,))["n"],
        "review": db.q1(f"SELECT COUNT(*) n FROM teacher_grade_runs WHERE needs_review=1 AND answer_id IN (SELECT id FROM teacher_answers WHERE import_batch_id IN ({scope}))", (own,))["n"],
        "jobs_running": db.q1(f"SELECT COUNT(*) n FROM teacher_grade_jobs WHERE status IN ('queued','running') AND batch_id IN ({scope})", (own,))["n"],
        "recent_batches": db.q(f"SELECT * FROM teacher_batches WHERE owner_id IS NULL OR owner_id=? ORDER BY created_at DESC LIMIT 5", (own,)),
        "recent_jobs": db.q(f"SELECT * FROM teacher_grade_jobs WHERE batch_id IN ({scope}) ORDER BY created_at DESC LIMIT 5", (own,)),
    }


@router.get("/classes")
def classes(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); require_teacher(user)
    own = str(user.get("id") or "")
    return {"items": db.q("""
        SELECT c.*, (SELECT COUNT(DISTINCT a.student_id) FROM teacher_answers a JOIN teacher_students s2 ON s2.id=a.student_id
                      WHERE s2.class_id=c.id AND a.import_batch_id IN (SELECT id FROM teacher_batches WHERE owner_id IS NULL OR owner_id=?)) student_count
        FROM teacher_classes c
        WHERE EXISTS (SELECT 1 FROM teacher_students s JOIN teacher_answers a ON a.student_id=s.id
                      JOIN teacher_batches b ON b.id=a.import_batch_id
                      WHERE s.class_id=c.id AND (b.owner_id IS NULL OR b.owner_id=?))
        ORDER BY c.code
    """, (own, own))}


@router.get("/students")
def students(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); require_teacher(user)
    own = str(user.get("id") or "")
    return {"items": db.q("""
        SELECT DISTINCT s.id,s.user_id,s.code,s.class_id,s.name,s.grade,c.code class_code
        FROM teacher_students s
        LEFT JOIN teacher_classes c ON c.id=s.class_id
        JOIN teacher_answers a ON a.student_id=s.id
        JOIN teacher_batches b ON b.id=a.import_batch_id
        WHERE b.owner_id IS NULL OR b.owner_id=?
        ORDER BY s.code
    """, (own,))}

@router.get("/classes/{class_id}/students")
def class_students(class_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); require_teacher(user)
    own = str(user.get("id") or "")
    if not db.q1("""SELECT 1 AS found FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id
                    JOIN teacher_batches b ON b.id=a.import_batch_id
                    WHERE s.class_id=? AND (b.owner_id IS NULL OR b.owner_id=?) LIMIT 1""", (class_id, own)):
        raise HTTPException(404, "班级不存在")
    return {"items": db.q("""
        SELECT s.*,
          (SELECT COUNT(*) FROM teacher_answers a WHERE a.student_id=s.id AND a.import_batch_id IN (SELECT id FROM teacher_batches WHERE owner_id IS NULL OR owner_id=?)) answers,
          (SELECT COUNT(*) FROM teacher_answers a WHERE a.student_id=s.id AND a.grading_status='pending' AND a.import_batch_id IN (SELECT id FROM teacher_batches WHERE owner_id IS NULL OR owner_id=?)) pending
        FROM teacher_students s WHERE s.class_id=? ORDER BY s.code
    """, (own, own, class_id))}


def _password_hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()


@router.get("/student-accounts/unbound")
def unbound_student_accounts(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    rows = db.q("""
        SELECT u.id, u.email, u.display_name, u.must_change_password, u.created_at
        FROM xueban_users u
        LEFT JOIN teacher_students s ON s.user_id = u.id
        WHERE u.role='student' AND s.id IS NULL
        ORDER BY u.created_at DESC
    """)
    return {"items": rows}


@router.get("/students/{student_id}/account")
def student_account(student_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    student = student_in_scope(student_id, user)
    account = db.q1("SELECT id,email,display_name,role,auth_type,must_change_password,created_at FROM xueban_users WHERE id=?", (student.get("user_id"),)) or {}
    audit = db.q("SELECT * FROM xueban_account_audit WHERE user_id=? ORDER BY created_at DESC LIMIT 20", (student.get("user_id"),))
    return {"student": student, "account": account, "audit": audit}


@router.post("/students/{student_id}/reset-password")
def reset_student_password(student_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """教师为学生重置密码：返回一次性初始密码，库中只存哈希，并要求首次登录改密。"""
    require_teacher(user); init()
    student = student_in_scope(student_id, user)
    account = db.q1("SELECT * FROM xueban_users WHERE id=?", (student.get("user_id"),))
    if not account:
        raise HTTPException(404, "该学生还没有账号，请先绑定或重新导入")
    password = secrets.token_urlsafe(12)
    salt = secrets.token_hex(16)
    db.x("UPDATE xueban_users SET password_hash=?, password_salt=?, must_change_password=1 WHERE id=?", (_password_hash(password, salt), salt, account["id"]))
    db.x("DELETE FROM xueban_sessions WHERE user_id=?", (account["id"],))
    db.x("INSERT INTO xueban_account_audit (id,user_id,actor_id,action,created_at) VALUES (?,?,?,?,?)", (db.new_id("accountaudit"), account["id"], user["id"], "teacher_reset_password", now()))
    return {"student_id": student_id, "email": account.get("email"), "initial_password": password, "must_change_password": True}


@router.post("/students/{student_id}/bind-account")
def bind_student_account(student_id: str, payload: Dict[str, Any] = Body(...), user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """把已有学生账号绑定到花名册学生；只允许绑未绑定的学生角色账号。"""
    require_teacher(user); init()
    student = student_in_scope(student_id, user)
    account_id = str(payload.get("user_id") or "").strip()
    email = str(payload.get("email") or "").strip().lower()
    account = db.q1("SELECT * FROM xueban_users WHERE id=?", (account_id,)) if account_id else db.q1("SELECT * FROM xueban_users WHERE email=?", (email,))
    if not account:
        raise HTTPException(404, "账号不存在")
    if (account.get("role") or "") != "student":
        raise HTTPException(422, "只能绑定学生角色账号")
    occupied = db.q1("SELECT * FROM teacher_students WHERE user_id=? AND id<>?", (account["id"], student_id))
    if occupied:
        raise HTTPException(409, f"该账号已绑定学生 {occupied['code']}")
    with db.transaction():
        db.x("UPDATE teacher_students SET user_id=? WHERE id=?", (account["id"], student_id))
        db.x("INSERT INTO xueban_account_audit (id,user_id,actor_id,action,detail_json,created_at) VALUES (?,?,?,?,?,?)", (db.new_id("accountaudit"), account["id"], user["id"], "teacher_bind_account", json.dumps({"student_id": student_id, "code": student["code"]}, ensure_ascii=False), now()))
    return {"student_id": student_id, "account": {"id": account["id"], "email": account.get("email")}}


@router.get("/students/{student_id}")
def student_detail(student_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); require_member(user)
    if teacher(user):
        student = student_in_scope(student_id, user)
        own = str(user.get("id") or "")
        answers = db.q(f"SELECT a.*, {QUESTION_COLUMNS} FROM teacher_answers a {QUESTION_JOIN} JOIN teacher_batches b ON b.id=a.import_batch_id WHERE a.student_id=? AND (b.owner_id IS NULL OR b.owner_id=?) ORDER BY a.submit_time DESC", (student_id, own))
        return {"student": student, "answers": answers}
    student = db.q1("SELECT * FROM teacher_students WHERE id=?", (student_id,))
    if not student:
        raise HTTPException(404, "学生不存在")
    if student["user_id"] != user["id"]:
        raise HTTPException(403, "不能查看其他学生")
    return {"student": student, "answers": db.q(f"SELECT a.*, {QUESTION_COLUMNS} FROM teacher_answers a {QUESTION_JOIN} WHERE a.student_id=? ORDER BY a.submit_time DESC", (student_id,))}


@router.get("/answers")
def answers(status: str = "", class_id: str = "", course_id: str = "", standard_id: str = "", exam_node_id: str = "", question_id: str = "", limit: int = 500, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); require_member(user)
    sql = f"SELECT a.*, s.code student_code, {QUESTION_COLUMNS} FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id JOIN teacher_batches b ON b.id=a.import_batch_id {QUESTION_JOIN} WHERE 1=1"; args: List[Any] = []
    if teacher(user):
        sql += " AND (b.owner_id IS NULL OR b.owner_id=?)"; args.append(str(user.get("id") or ""))
    else:
        student_row = student_for(user)
        if not student_row:
            raise HTTPException(403, "当前账号未绑定学生档案")
        sql += " AND a.student_id=?"; args.append(student_row["id"])
    if status:
        sql += " AND a.grading_status=?"; args.append(status)
    if class_id:
        sql += " AND s.class_id=?"; args.append(class_id)
    if course_id:
        sql += " AND COALESCE(v.course_id, q.course_id)=?"; args.append(course_id)
    if standard_id:
        sql += " AND COALESCE(v.standard_id, q.standard_id)=?"; args.append(standard_id)
    if exam_node_id:
        sql += " AND a.exam_node_id=?"; args.append(exam_node_id)
    if question_id:
        sql += " AND a.question_id=?"; args.append(question_id)
    sql += " ORDER BY a.submit_time DESC LIMIT ?"; args.append(max(1, min(limit, 1000)))
    return {"items": db.q(sql, tuple(args))}


def visible_answer(answer: Dict[str, Any], user: Dict[str, Any]) -> bool:
    if teacher(user):
        return True
    student = student_for(user)
    return bool(student and answer.get("student_id") == student["id"])


def effective_grade(answer_id: str) -> Optional[Dict[str, Any]]:
    grades = db.q("SELECT * FROM teacher_grade_runs WHERE answer_id=? ORDER BY grading_version DESC, created_at DESC", (answer_id,))
    valid = [row for row in grades if row.get("status") != "failed"]
    return (valid or grades or [None])[0]


@router.get("/answers/{answer_id}")
def answer_detail(answer_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); answer = db.q1("SELECT * FROM teacher_answers WHERE id=?", (answer_id,))
    if not answer:
        raise HTTPException(404, "作答不存在")
    if not visible_answer(answer, user):
        raise HTTPException(403, "不能查看其他学生")
    grades = db.q("SELECT * FROM teacher_grade_runs WHERE answer_id=? ORDER BY grading_version DESC, created_at DESC", (answer_id,))
    grade = effective_grade(answer_id)
    corrections = db.q("SELECT * FROM teacher_corrections WHERE grade_run_id IN (SELECT id FROM teacher_grade_runs WHERE answer_id=?) ORDER BY created_at DESC", (answer_id,))
    version = db.q1("SELECT * FROM teacher_question_versions WHERE version_id=?", (answer.get("question_version_id"),))
    question = version or db.q1("SELECT * FROM teacher_questions WHERE id=?", (answer["question_id"],))
    # 历史版本必须限定在同任务内：不同批次可以用相同的 event_id。
    revisions = db.q(
        "SELECT * FROM teacher_answers WHERE student_id=? AND event_id=? AND COALESCE(import_batch_id,'')=COALESCE(?,'') ORDER BY revision",
        (answer["student_id"], answer["event_id"], answer.get("import_batch_id")),
    )
    return {"answer": answer, "question": question, "question_source": "version" if version else "global_fallback", "versions": db.q("SELECT * FROM teacher_question_versions WHERE batch_id=? AND question_id=? ORDER BY created_at DESC", (answer.get("import_batch_id"), answer["question_id"])), "revisions": revisions, "grade": grade, "grades": grades, "corrections": corrections, "standard": db.q1("SELECT * FROM teacher_standards WHERE id=?", (answer.get("standard_id"),)) if answer.get("standard_id") else None}


def teacher_meta(answer: Dict[str, Any]) -> Dict[str, Any]:
    version = db.q1("SELECT * FROM teacher_question_versions WHERE version_id=?", (answer.get("question_version_id"),))
    q = version or db.q1("SELECT * FROM teacher_questions WHERE id=?", (answer.get("question_id"),)) or {}
    std = db.q1("SELECT * FROM teacher_standards WHERE id=?", (answer.get("standard_id") or q.get("standard_id"),)) or {}
    return {"question": q, "question_source": "version" if version else "global_fallback", "standard": std, "topic": db.q1("SELECT * FROM teacher_topics WHERE id=?", (q.get("topic_id"),)) or {}}


def split_steps(answer: Dict[str, Any], question: Dict[str, Any]) -> List[str]:
    raw = (answer.get("solution_process") or "").strip()
    if not raw:
        return []
    lines = [re.sub(r"^\s*\d+[.、)]\s*", "", line).strip() for line in raw.splitlines() if line.strip()]
    return [line for line in lines if line] or [raw]


def normalize(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).replace("＝", "=").replace("，", ",").lower()


def llm_grade(answer: Dict[str, Any], question: Dict[str, Any], steps: List[str], key: str, provider: str, standard: Dict[str, Any], textbook_context: str = "") -> Dict[str, Any]:
    base = "https://api.deepseek.com" if provider == "deepseek" else "https://api.openai.com"
    model = "deepseek-chat" if provider == "deepseek" else "gpt-4o-mini"
    payload = {"题目": question.get("stem"), "参考答案": question.get("reference_answer"), "参考解析": question.get("reference_solution"), "原始评分点": question.get("scoring_points"), "课程标准": standard.get("content_json") or "", "教师教材章节": textbook_context, "学生最终作答": answer.get("original_answer"), "学生原始解题过程": answer.get("solution_process"), "模型辅助步骤": steps, "要求": "只判题，不做学情分析。优先按原始评分点；无标准步骤时允许模型拆解并标记 model_assisted_steps。逐步骤返回 step_index,student_step,normalized_step,operation_type,reasoning_purpose,prerequisite_steps,expected_step,is_correct,score,max_score,error_type,judgment_reason,confidence,scoring_point_links。每个步骤还必须返回 operation_units 数组：operation_id,source_step_id,operation_index,student_fragment,normalized_operation,operation_type,reasoning_purpose,prerequisite_operations,expected_operation,is_correct,score,max_score,error_type,judgment_reason,confidence,scoring_point_links。同一行多个独立数学操作必须拆成多个 operation_units，不能合并。汇总 score,max_score,is_correct,feedback,judgment_reason,needs_review。仅输出 JSON。"}
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], "temperature": 0}, ensure_ascii=False).encode()
    request = urllib.request.Request(base + "/chat/completions", data=body, headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"}, method="POST")
    with urllib.request.urlopen(request, timeout=45) as response:
        content = json.loads(response.read().decode())["choices"][0]["message"]["content"]
    match = re.search(r"\{.*\}", content, re.S)
    if not match:
        raise ValueError("model returned no JSON")
    return json.loads(match.group(0))


GRADING_PROMPT_VERSION = "teacher-grade-v3"
GRADER_VERSION = "v1.5.11"

# 测试可替换的模型调用钩子；生产默认真实 HTTP 调用，不会读取任何测试配置。
_LLM_CALLER = None


def call_grader(answer: Dict[str, Any], question: Dict[str, Any], steps: List[str], key: str, provider: str, standard: Dict[str, Any], textbook_context: str = "") -> Dict[str, Any]:
    caller = _LLM_CALLER or llm_grade
    return caller(answer, question, steps, key, provider, standard, textbook_context)


def validate_llm_result(result: Dict[str, Any], max_score: float, question: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(result, dict):
        raise ValueError("model result is not an object")
    score = float(result.get("score"))
    if not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= max_score:
        raise ValueError("score outside 0..max_score")
    if "step_results" not in result and isinstance(result.get("steps"), list):
        result["step_results"] = result["steps"]
    steps = result.get("step_results")
    if steps is None:
        steps = []
    if not isinstance(steps, list):
        raise ValueError("step_results is not a list")
    total = 0.0
    seen = set()
    all_step_ids = {int(item.get("step_index")) for item in steps if isinstance(item, dict) and str(item.get("step_index") or "").isdigit()}
    valid_point_ids = set(re.findall(r"\b[A-Z]\d{3}P\d+\b", str(question.get("scoring_points") or "")))
    seen_scoring_points = set()
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("step is not an object")
        index = int(step.get("step_index"))
        if index <= 0 or index in seen:
            raise ValueError("invalid step index")
        seen.add(index)
        step_score = float(step.get("score"))
        step_max = float(step.get("max_score"))
        if step_score < 0 or step_max < 0 or step_score > step_max:
            raise ValueError("step score outside 0..max_score")
        for key in ("student_step", "is_correct", "judgment_reason", "error_type"):
            if key not in step:
                raise ValueError(f"step missing {key}")
        step.setdefault("normalized_step", "")
        step.setdefault("operation_type", "")
        step.setdefault("reasoning_purpose", "")
        step.setdefault("prerequisite_steps", [])
        step.setdefault("expected_step", "")
        step.setdefault("confidence", None)
        links = step.get("scoring_point_links")
        if isinstance(links, list):
            normalized = []
            for link in links:
                if isinstance(link, str):
                    normalized.append({"scoring_point_id": link, "awarded_score": step_score, "max_score": step_max})
                elif isinstance(link, dict):
                    normalized.append(link)
            step["scoring_point_links"] = normalized
            link_total = 0.0
            for link in normalized:
                point = str(link.get("scoring_point_id") or "")
                if valid_point_ids and point not in valid_point_ids:
                    raise ValueError(f"unknown scoring point {point}")
                if point in seen_scoring_points:
                    raise ValueError(f"duplicate scoring point {point}")
                seen_scoring_points.add(point)
                awarded = link.get("awarded_score")
                awarded = link.get("score") if awarded is None else awarded
                awarded = float(awarded or 0)
                full = float(link.get("max_score") or 0)
                if full <= 0 or awarded < 0 or awarded > full:
                    raise ValueError(f"invalid scoring point score {point}")
                link_total += awarded
            if link_total > step_score + 0.05:
                raise ValueError("scoring point allocation exceeds step score")
        operations = step.get("operation_units")
        if isinstance(operations, list) and operations:
            seen_ops = set()
            op_total = 0.0
            for operation in operations:
                if not isinstance(operation, dict):
                    raise ValueError("operation unit is not an object")
                operation_id = str(operation.get("operation_id") or "")
                if not operation_id or operation_id in seen_ops:
                    raise ValueError("invalid operation id")
                seen_ops.add(operation_id)
                source_step = operation.get("source_step_id")
                if source_step is not None and str(source_step).isdigit() and int(source_step) not in all_step_ids:
                    raise ValueError("operation source step does not exist")
                operation["source_step_id"] = step.get("step_index")
                operation["operation_index"] = len(seen_ops)
                operation.setdefault("student_fragment", step.get("student_step") or "")
                operation.setdefault("normalized_operation", operation.get("normalized_step") or "")
                operation.setdefault("prerequisite_operations", [])
                operation.setdefault("confidence", None)
                op_score = float(operation.get("score") or 0)
                op_max = float(operation.get("max_score") or 0)
                if op_score < 0 or op_score > op_max:
                    raise ValueError("operation score outside range")
                op_total += op_score
            if abs(op_total - step_score) > 0.05:
                result["needs_review"] = True
                step["operation_note"] = "操作单元分数合计与步骤分数不一致，待人工复核"
        step["operation_units"] = operations if isinstance(operations, list) else []
        total += step_score
    steps = [step for step in steps if str(step.get("student_step") or "").strip() not in {"未作答", "未提交", "未体现"}]
    result["step_results"] = steps
    if steps and abs(sum(float(step.get("score") or 0) for step in steps) - score) > 0.05:
        raise ValueError("step score sum does not match total")
    qtype = str(question.get("question_type") or "")
    process_type = any(key in qtype for key in ("解答", "计算", "证明", "应用", "综合"))
    if process_type and not steps:
        result["needs_review"] = True
    if process_type and steps and any(not step.get("operation_units") for step in steps):
        result["needs_review"] = True
        result["needs_review"] = True
    result["score"] = score
    result["step_results"] = steps
    result.setdefault("needs_review", False)
    result.setdefault("is_correct", "partial")
    result.setdefault("feedback", "")
    result.setdefault("judgment_reason", "")
    return result

def insert_grade(answer: Dict[str, Any], question: Dict[str, Any], standard: Dict[str, Any], version: int, model: str, score: float, max_score: float, correct: str, steps: List[Dict[str, Any]], error_type: str, feedback: str, reason: str, review: int, status: str, raw: str, textbook_course_id: str = "", textbook_unit_id: str = "", textbook_context: str = "", step_analysis_status: str = "") -> Dict[str, Any]:
    grade_id = db.new_id("grade")
    db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,grading_prompt_version,grader_version,course_id,standard_id,standard_version,scoring_rule_version,score,max_score,is_correct,step_results_json,error_type,feedback,judgment_reason,needs_review,status,raw_response,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (grade_id, answer["id"], answer["student_id"], answer["question_id"], answer["event_id"], answer["revision"], version, model, GRADING_PROMPT_VERSION, GRADER_VERSION, answer.get("course_id") or question.get("course_id"), answer.get("standard_id") or question.get("standard_id"), standard.get("version"), "scoring_points", score, max_score, correct, json.dumps(steps, ensure_ascii=False), error_type, feedback, reason, review, status, raw, now()))
    db.x("UPDATE teacher_grade_runs SET textbook_course_id=?, textbook_unit_id=?, textbook_context=?, step_analysis_status=?, question_version_id=?, mapping_version=? WHERE id=?", (textbook_course_id, textbook_unit_id, textbook_context[:4000], step_analysis_status, answer.get("question_version_id") or "", str(analysis.current_mapping_version(answer.get("import_batch_id") or "")), grade_id))
    db.x("UPDATE teacher_answers SET grading_status=? WHERE id=?", ('failed' if status == 'failed' else ('needs_review' if review else 'graded'), answer["id"]))
    return db.q1("SELECT * FROM teacher_grade_runs WHERE id=?", (grade_id,))


def default_llm_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"):
        try:
            if not candidate.exists():
                continue
            for line in candidate.read_text(encoding="utf-8").splitlines():
                if line.startswith("DEEPSEEK_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            continue
    return ""

def record_attempt(job_id: str, answer: Dict[str, Any], number: int, stage: str, error_type: str, message: str, result_id: str = "", http_status: int = 0, start: str = "") -> None:
    retryable = 1 if stage in ("model_response", "json_parse", "structure_validation") else 0
    end = now()
    try:
        duration = max(0, int((datetime.fromisoformat(end) - datetime.fromisoformat(start or end)).total_seconds() * 1000))
    except Exception:
        duration = 0
    db.x("INSERT INTO teacher_grade_attempts (id,task_id,answer_id,batch_id,student_id,student_code,class_code,question_id,event_id,revision,attempt_number,model,prompt_version,start_time,end_time,failure_stage,error_type,http_status,error_message,retryable,result_id,duration_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (db.new_id("attempt"), job_id, answer.get("id"), answer.get("import_batch_id") or "", answer.get("student_id") or "", answer.get("student_code") or "", answer.get("class_code") or "", answer.get("question_id"), answer.get("event_id"), answer.get("revision"), number, "deepseek-chat", GRADING_PROMPT_VERSION, start or end, end, stage, error_type, http_status, message[:1000], retryable, result_id, duration))


def grade_answer(answer_id: str, key: str = "", provider: str = "deepseek", textbook_course_id: str = "", textbook_unit_id: str = "", textbook_context: str = "", job_id: str = "") -> Dict[str, Any]:
    init(); attempt_start = now(); answer = db.q1("SELECT * FROM teacher_answers WHERE id=?", (answer_id,))
    if not answer:
        raise HTTPException(404, "作答不存在")
    context = teacher_meta(answer); question = context["question"]; standard = context["standard"]
    steps = split_steps(answer, question)
    max_score = float(question.get("max_score") or answer.get("max_score") or 0)
    version = (db.q1("SELECT COALESCE(MAX(grading_version),0) n FROM teacher_grade_runs WHERE answer_id=?", (answer_id,))["n"] or 0) + 1
    if not key:
        key = default_llm_key()
    if not key:
        # 没有模型凭据时明确失败，不写入任何成绩/证据，也不做规则猜测。
        raise HTTPException(503, {"code": "GRADER_NOT_CONFIGURED", "message": "判题模型未配置：请在服务端配置 DEEPSEEK_API_KEY 后再判题"})
    try:
        llm = validate_llm_result(call_grader(answer, question, steps, key, provider, standard, textbook_context), max_score, question)
        score = float(llm["score"])
        has_process = bool(str(answer.get("solution_process") or "").strip())
        has_steps = bool(llm.get("step_results"))
        analysis_status = "no_process" if not has_process else ("completed" if has_steps else "failed")
        if has_process and not has_steps:
            llm["needs_review"] = True
            llm["judgment_reason"] = (str(llm.get("judgment_reason") or "") + " 步骤分析未成功，待人工复核。").strip()
        llm["step_analysis_status"] = analysis_status
        grade = insert_grade(answer, question, standard, version, "deepseek-chat" if provider == "deepseek" else "gpt-4o-mini", score, max_score, str(llm.get("is_correct") or "partial"), llm.get("step_results") or [], str(llm.get("error_type") or ""), str(llm.get("feedback") or ""), str(llm.get("judgment_reason") or ""), 1 if llm.get("needs_review") else 0, "needs_review" if llm.get("needs_review") else "graded", json.dumps(llm, ensure_ascii=False), "", "", "", analysis_status)
        record_attempt(job_id, answer, version, "", "", "", grade.get("id"), start=attempt_start)
        return {"grade": grade}
    except HTTPException:
        raise
    except Exception as exc:
        message = str(exc)
        upper = message.upper()
        stage = "json_parse" if "JSON" in upper else ("structure_validation" if "STEP" in upper or "SCORE" in upper or "MISSING" in upper or "OPERATION" in upper else "model_response")
        record_attempt(job_id, answer, version, stage, "model_error", message, start=attempt_start)
        grade = insert_grade(answer, question, standard, version, "model-error", 0, max_score, "failed", [], "model_error", "", message[:500], 1, "failed", "", textbook_course_id, textbook_unit_id, textbook_context)
        return {"grade": grade}


@router.post("/answers/{answer_id}/grade")
def grade(answer_id: str, user: Dict[str, Any] = Depends(current_user), payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    require_teacher(user)
    return grade_answer(answer_id, str(payload.get("llm_key") or ""), str(payload.get("provider") or "deepseek"), str(payload.get("textbook_course_id") or ""), str(payload.get("textbook_unit_id") or ""), str(payload.get("textbook_context") or ""))


@router.get("/grade/config")
def grade_config(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """判题是否可用。前端据此显式提示“判题服务未配置”，而不是等 503。"""
    init(); require_teacher(user)
    configured = bool(default_llm_key())
    return {
        "configured": configured,
        "provider": "deepseek",
        "model": "deepseek-chat",
        "prompt_version": GRADING_PROMPT_VERSION,
        "grader_version": GRADER_VERSION,
        "message": "判题模型已配置" if configured else "判题模型未配置：请在服务端 .env 配置 DEEPSEEK_API_KEY",
    }


def job_counts(job_id: str) -> Dict[str, int]:
    row = db.q1("SELECT COUNT(*) total, SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) completed, SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed, SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) pending, SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) running FROM teacher_grade_job_items WHERE job_id=?", (job_id,)) or {}
    return {key: int(row.get(key) or 0) for key in ("total", "completed", "failed", "pending", "running")}


_JOB_LOCKS: Dict[str, threading.Lock] = {}
_JOB_LOCKS_GUARD = threading.Lock()


def _job_lock(job_id: str) -> threading.Lock:
    with _JOB_LOCKS_GUARD:
        return _JOB_LOCKS.setdefault(job_id, threading.Lock())


def run_job(job_id: str) -> None:
    """同一 job 串行执行：共享 SQLite 连接下避免两个 worker 并发写坏状态。"""
    lock = _job_lock(job_id)
    if not lock.acquire(blocking=False):
        return
    try:
        _run_job_once(job_id)
    finally:
        lock.release()
        with _JOB_LOCKS_GUARD:
            if not lock.locked():
                _JOB_LOCKS.pop(job_id, None)


def _run_job_once(job_id: str) -> None:
    key = default_llm_key()
    job = db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_id,))
    if not job:
        return
    if not key:
        db.x("UPDATE teacher_grade_jobs SET status='failed', error=?, updated_at=? WHERE id=?", ("未配置 DEEPSEEK_API_KEY", now(), job_id))
        return
    context = {name: str(job.get(name) or "") for name in ("textbook_course_id", "textbook_unit_id", "textbook_context")}
    worker_id = f"worker-{job_id[-6:]}-{int(time.time())}"
    c = db.conn()
    cur = c.execute("UPDATE teacher_grade_jobs SET status='running', started_at=COALESCE(NULLIF(started_at,''),?), worker_id=?, heartbeat_at=?, error='', updated_at=? WHERE id=? AND status IN ('queued','paused')", (now(), worker_id, now(), now(), job_id))
    c.commit()
    if cur.rowcount != 1:
        return
    try:
        while True:
            current = db.q1("SELECT pause_requested, cancellation_requested FROM teacher_grade_jobs WHERE id=?", (job_id,)) or {}
            if current.get("pause_requested") or current.get("cancellation_requested"):
                db.x("UPDATE teacher_grade_jobs SET status='paused', paused_at=?, updated_at=? WHERE id=?", (now(), now(), job_id))
                return
            item = db.q1("SELECT * FROM teacher_grade_job_items WHERE job_id=? AND status='pending' ORDER BY rowid LIMIT 1", (job_id,))
            if not item:
                break
            claimed = c.execute("UPDATE teacher_grade_job_items SET status='running', attempt_count=attempt_count+1, started_at=?, updated_at=? WHERE id=? AND status='pending'", (now(), now(), item["id"]))
            c.commit()
            if claimed.rowcount != 1:
                continue  # 另一 worker 已领取，避免重复判题
            try:
                result = grade_answer(item["answer_id"], key, "deepseek", context.get("textbook_course_id", ""), context.get("textbook_unit_id", ""), context.get("textbook_context", ""), job_id)
                grade = result.get("grade") or {}
                if grade.get("status") == "failed" or grade.get("is_correct") == "failed":
                    db.x("UPDATE teacher_grade_job_items SET status='failed', grade_id=?, last_error=?, completed_at=?, updated_at=? WHERE id=?", (grade.get("id"), grade.get("judgment_reason") or "模型判题失败", now(), now(), item["id"]))
                else:
                    db.x("UPDATE teacher_grade_job_items SET status='completed', grade_id=?, grading_version=?, last_error='', completed_at=?, updated_at=? WHERE id=?", (grade.get("id"), grade.get("grading_version"), now(), now(), item["id"]))
            except Exception as exc:
                db.x("UPDATE teacher_grade_job_items SET status='failed', last_error=?, completed_at=?, updated_at=? WHERE id=?", (str(exc)[:500], now(), now(), item["id"]))
            counts = job_counts(job_id)
            review = db.q1("SELECT COUNT(*) n FROM teacher_grade_runs r JOIN teacher_grade_job_items i ON i.grade_id=r.id WHERE i.job_id=? AND r.needs_review=1", (job_id,)) or {}
            db.x("UPDATE teacher_grade_jobs SET processed=?, completed=?, failed=?, needs_review=?, current_answer_id=?, last_checkpoint_at=?, heartbeat_at=?, updated_at=? WHERE id=?", (counts["completed"] + counts["failed"], counts["completed"], counts["failed"], int(review.get("n") or 0), item["answer_id"], now(), now(), now(), job_id))
    except Exception as exc:
        # 非单题异常（数据库/进程级）不能让任务永远停在 running。
        db.x("UPDATE teacher_grade_jobs SET status='failed', error=?, finished_at=?, updated_at=? WHERE id=?", (str(exc)[:500], now(), now(), job_id))
        return
    counts = job_counts(job_id)
    review = db.q1("SELECT COUNT(*) n FROM teacher_grade_runs r JOIN teacher_grade_job_items i ON i.grade_id=r.id WHERE i.job_id=? AND r.needs_review=1", (job_id,)) or {}
    if counts["pending"]:
        final_status = "partially_failed" if counts["completed"] else "failed"
    else:
        final_status = "completed" if counts["failed"] == 0 else ("failed" if counts["completed"] == 0 else "partially_failed")
    db.x("UPDATE teacher_grade_jobs SET status=?, processed=?, completed=?, failed=?, needs_review=?, finished_at=?, current_answer_id='', heartbeat_at=?, updated_at=? WHERE id=?",
         (final_status, counts["completed"] + counts["failed"], counts["completed"], counts["failed"], int(review.get("n") or 0), now(), now(), now(), job_id))


@router.post("/grade/batch")
def batch(background: BackgroundTasks, user: Dict[str, Any] = Depends(current_user), payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    require_teacher(user); init()
    if not default_llm_key():
        raise HTTPException(503, "未配置 DEEPSEEK_API_KEY，管理员配置后才能判题")
    batch_id = str(payload.get("batch_id") or "")
    if not batch_id:
        raise HTTPException(422, "必须选择导入任务")
    owned_batch(batch_id, user)
    batch_row = db.q1("SELECT preprocess_status FROM teacher_batches WHERE id=?", (batch_id,))
    if not batch_row or (batch_row.get("preprocess_status") or "review") != "confirmed":
        raise HTTPException(409, "请先在任务详情确认数据预处理报告")
    # 质量闸门是服务端的硬约束：前端禁用只是提示，不是安全边界。
    gate = quality_gate(batch_id, user)
    force = bool(payload.get("force"))
    if gate["blocking"]:
        if not force:
            raise HTTPException(409, {
                "code": "QUALITY_GATE_BLOCKED",
                "message": "质量闸门存在阻断问题，不能开始判题",
                "issues": [item for item in gate["issues"] if item.get("blocking")],
                "hint": "修正数据后重试；如确需继续，必须显式 force=true 并填写 force_reason",
            })
        reason = str(payload.get("force_reason") or "").strip()
        if len(reason) < 5:
            raise HTTPException(422, "强制绕过质量闸门必须填写不少于 5 个字的理由")
        db.x("INSERT INTO teacher_quality_gate_overrides (id,batch_id,teacher_id,reason,issues_json,created_at) VALUES (?,?,?,?,?,?)", (db.new_id("qgoverride"), batch_id, user["id"], reason, json.dumps([item for item in gate["issues"] if item.get("blocking")], ensure_ascii=False), now()))
    filters = {name: str(payload.get(name) or "") for name in ("class_id", "exam_node_id")}
    sql = "SELECT a.id FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id WHERE a.import_batch_id=? AND a.grading_status='pending'"; args: List[Any] = [batch_id]
    for column, key in (("s.class_id", "class_id"), ("a.exam_node_id", "exam_node_id")):
        if filters[key]:
            sql += f" AND {column}=?"; args.append(filters[key])
    sql += " ORDER BY a.student_id, a.exam_node_id, a.question_id, a.revision DESC"
    ids = [row["id"] for row in db.q(sql, tuple(args))]
    if not ids:
        raise HTTPException(409, "该任务当前没有待判题作答")
    running = db.q1("SELECT id,status FROM teacher_grade_jobs WHERE batch_id=? AND status IN ('running','queued','pausing','paused') ORDER BY created_at DESC LIMIT 1", (batch_id,))
    if running:
        raise HTTPException(409, f"该任务已有未结束的判题任务 {running['id']}（{running['status']}），请继续或重试该任务，不要新建任务")
    job_id = db.new_id("job")
    context = {name: str(payload.get(name) or "") for name in ("textbook_course_id", "textbook_unit_id", "textbook_context")}
    db.x("INSERT INTO teacher_grade_jobs (id,status,batch_id,total,processed,completed,failed,needs_review,current_answer_id,filters_json,textbook_course_id,textbook_unit_id,textbook_context,provider,model,prompt_version,error,created_at,updated_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (job_id, "queued", batch_id, len(ids), 0, 0, 0, 0, "", json.dumps(filters, ensure_ascii=False), context["textbook_course_id"], context["textbook_unit_id"], context["textbook_context"], "deepseek", "deepseek-chat", GRADING_PROMPT_VERSION, "", now(), now(), ""))
    for answer_id in ids:
        db.x("INSERT OR IGNORE INTO teacher_grade_job_items (id,job_id,answer_id,status,updated_at) VALUES (?,?,?,?,?)", (db.new_id("jobitem"), job_id, answer_id, "pending", now()))
    background.add_task(run_job, job_id)
    return {"job": db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_id,)), "quality_gate": {"blocking": gate["blocking"], "forced": bool(gate["blocking"] and force), "issues": gate["issues"]}}


@router.post("/grade/jobs/{job_id}/pause")
def pause_job(job_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    job = db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_id,))
    if not job:
        raise HTTPException(404, "任务不存在")
    cutoff = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")
    stale = not job.get("heartbeat_at") or str(job.get("heartbeat_at")) < cutoff
    db.x("UPDATE teacher_grade_jobs SET pause_requested=1, updated_at=? WHERE id=?", (now(), job_id))
    if job.get("status") in ("queued", "paused") or (job.get("status") in ("running", "pausing") and stale):
        # 没有活跃 worker（排队中/心跳过期）时直接落 paused，避免永远停在 pausing。
        db.x("UPDATE teacher_grade_jobs SET status='paused', paused_at=?, updated_at=? WHERE id=?", (now(), now(), job_id))
        db.x("UPDATE teacher_grade_job_items SET status='pending', updated_at=? WHERE job_id=? AND status='running'", (now(), job_id))
    else:
        db.x("UPDATE teacher_grade_jobs SET status='pausing' WHERE id=? AND status='running'", (job_id,))
    return {"job": db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_id,))}


@router.post("/grade/jobs/{job_id}/resume")
def resume_job(job_id: str, background: BackgroundTasks, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    if not default_llm_key():
        raise HTTPException(503, "未配置 DEEPSEEK_API_KEY，管理员配置后才能判题")
    job = db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_id,))
    if not job:
        raise HTTPException(404, "任务不存在")
    if job.get("status") in ("running", "queued", "pausing"):
        raise HTTPException(409, f"任务正在执行（{job.get('status')}），不能重复继续")
    db.x("UPDATE teacher_grade_jobs SET pause_requested=0, cancellation_requested=0, status='queued', resumed_at=?, updated_at=? WHERE id=?", (now(), now(), job_id))
    background.add_task(run_job, job_id)
    return {"job": db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_id,))}


@router.post("/grade/jobs/{job_id}/retry")
def retry_job(job_id: str, background: BackgroundTasks, user: Dict[str, Any] = Depends(current_user), payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    require_teacher(user); init()
    if not default_llm_key():
        raise HTTPException(503, "未配置 DEEPSEEK_API_KEY，管理员配置后才能判题")
    job = db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_id,))
    if not job:
        raise HTTPException(404, "判题任务不存在")
    if job.get("status") in ("running", "queued", "pausing"):
        raise HTTPException(409, f"任务正在执行（{job.get('status')}），不能重试")
    failed = db.q("SELECT id FROM teacher_grade_job_items WHERE job_id=? AND status='failed'", (job_id,))
    if not failed:
        raise HTTPException(409, "没有失败项")
    with db.transaction():
        db.x("UPDATE teacher_grade_job_items SET status='pending', last_error=COALESCE(last_error,''), updated_at=? WHERE job_id=? AND status='failed'", (now(), job_id))
        db.x("UPDATE teacher_grade_jobs SET status='queued', pause_requested=0, cancellation_requested=0, error='', updated_at=? WHERE id=?", (now(), job_id))
    background.add_task(run_job, job_id)
    return {"job": db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_id,)), "retried": len(failed)}

@router.get("/grade/jobs")
def jobs(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); require_teacher(user)
    own = str(user.get("id") or "")
    return {"items": db.q("""
        SELECT j.* FROM teacher_grade_jobs j JOIN teacher_batches b ON b.id=j.batch_id
        WHERE b.owner_id IS NULL OR b.owner_id=? ORDER BY j.created_at DESC LIMIT 50
    """, (own,))}


@router.get("/grade/jobs/{job_id}")
def job(job_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); require_teacher(user)
    row = db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_id,))
    if not row:
        raise HTTPException(404, "任务不存在")
    return {"job": row, "items": db.q("SELECT * FROM teacher_grade_job_items WHERE job_id=?", (job_id,))}


@router.get("/grade/jobs/{job_id}/failures")
def job_failures(job_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); require_teacher(user)
    return {"items": db.q("SELECT * FROM teacher_grade_job_items WHERE job_id=? AND status='failed'", (job_id,))}


@router.get("/batches")
def list_batches(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    own = str(user.get("id") or "")
    rows = db.q("SELECT * FROM teacher_batches WHERE owner_id IS NULL OR owner_id=? ORDER BY created_at DESC", (own,))
    items = []
    for batch in rows:
        stats = db.q1("SELECT COUNT(*) total, SUM(CASE WHEN grading_status='pending' THEN 1 ELSE 0 END) pending, SUM(CASE WHEN grading_status='graded' THEN 1 ELSE 0 END) graded, SUM(CASE WHEN grading_status IN ('failed') THEN 1 ELSE 0 END) failed FROM teacher_answers WHERE import_batch_id=?", (batch["id"],)) or {}
        people = db.q1("SELECT COUNT(DISTINCT a.student_id) students, COUNT(DISTINCT s.class_id) classes, COUNT(DISTINCT a.question_id) questions FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id WHERE a.import_batch_id=?", (batch["id"],)) or {}
        job = db.q1("SELECT * FROM teacher_grade_jobs WHERE batch_id=? ORDER BY created_at DESC LIMIT 1", (batch["id"],))
        review = db.q1("SELECT COUNT(*) n FROM teacher_grade_runs r JOIN teacher_answers a ON a.id=r.answer_id WHERE a.import_batch_id=? AND r.needs_review=1", (batch["id"],)) or {}
        items.append({**batch, **stats, **people, "needs_review": int(review.get("n") or 0), "latest_job": job, "task_status": (job or {}).get("status") or "imported"})
    return {"items": items}

@router.delete("/batches/{batch_id}")
def delete_batch(batch_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """删除一个导入任务及其全部业务关联数据。

    清理顺序必须是“先子后父”：评分点确认/人工修正/判题结果依赖 answers，
    因此必须在删除 answers 之前完成，否则子查询会变成空集，留下孤儿记录。
    """
    require_teacher(user); init()
    batch = db.q1("SELECT * FROM teacher_batches WHERE id=?", (batch_id,))
    if not batch:
        raise HTTPException(404, "导入任务不存在")
    job_ids = [row["id"] for row in db.q("SELECT id FROM teacher_grade_jobs WHERE batch_id=?", (batch_id,))]
    if job_ids:
        placeholders = ",".join("?" for _ in job_ids)
        running = db.q1(f"SELECT id FROM teacher_grade_jobs WHERE id IN ({placeholders}) AND status IN ('running','queued','pausing') LIMIT 1", tuple(job_ids))
        if running:
            raise HTTPException(409, "任务中还有运行中的任务，请先暂停后再删除任务")
    answer_scope = "SELECT id FROM teacher_answers WHERE import_batch_id=?"
    grade_scope = f"SELECT id FROM teacher_grade_runs WHERE answer_id IN ({answer_scope})"
    with db.transaction():
        # 1. 评分点确认 / 人工修正（依赖 grade_run + answer）
        db.x(f"DELETE FROM teacher_scoring_point_confirmations WHERE answer_id IN ({answer_scope}) OR grade_run_id IN ({grade_scope})", (batch_id, batch_id))
        db.x(f"DELETE FROM teacher_corrections WHERE grade_run_id IN ({grade_scope})", (batch_id,))
        # 2. 判题尝试（按任务、按 job、按作答三种归属都要覆盖）
        if job_ids:
            placeholders = ",".join("?" for _ in job_ids)
            db.x(f"DELETE FROM teacher_grade_attempts WHERE batch_id=? OR answer_id IN ({answer_scope}) OR task_id IN ({placeholders})", tuple([batch_id, batch_id] + job_ids))
            db.x(f"DELETE FROM teacher_grade_job_items WHERE job_id IN ({placeholders})", tuple(job_ids))
            db.x(f"DELETE FROM teacher_grade_jobs WHERE id IN ({placeholders})", tuple(job_ids))
        else:
            db.x(f"DELETE FROM teacher_grade_attempts WHERE batch_id=? OR answer_id IN ({answer_scope})", (batch_id, batch_id))
        # 3. 判题结果
        db.x(f"DELETE FROM teacher_grade_runs WHERE answer_id IN ({answer_scope})", (batch_id,))
        # 4. 分析快照、评分点与映射、题目版本
        db.x("DELETE FROM teacher_analysis_runs WHERE task_id=?", (batch_id,))
        db.x("DELETE FROM teacher_enrollments WHERE batch_id=?", (batch_id,))
        if _table_exists("scoring_point_knowledge_links"):
            db.x("DELETE FROM scoring_point_knowledge_links WHERE batch_id=?", (batch_id,))
        if _table_exists("scoring_points"):
            db.x("DELETE FROM scoring_points WHERE batch_id=?", (batch_id,))
        db.x("DELETE FROM teacher_question_versions WHERE batch_id=?", (batch_id,))
        # 5. 作答与被删任务本身
        db.x("DELETE FROM teacher_answers WHERE import_batch_id=?", (batch_id,))
        db.x("DELETE FROM teacher_import_files WHERE batch_id=?", (batch_id,))
        db.x("DELETE FROM teacher_batches WHERE id=?", (batch_id,))
    residual = {
        "answers": db.q1(f"SELECT COUNT(*) n FROM ({answer_scope})", (batch_id,))["n"],
        "grade_runs": db.q1(f"SELECT COUNT(*) n FROM ({grade_scope})", (batch_id,))["n"],
        "confirmations": db.q1(f"SELECT COUNT(*) n FROM teacher_scoring_point_confirmations WHERE answer_id IN ({answer_scope})", (batch_id,))["n"],
        "corrections": db.q1(f"SELECT COUNT(*) n FROM teacher_corrections WHERE grade_run_id IN ({grade_scope})", (batch_id,))["n"],
        "attempts": db.q1("SELECT COUNT(*) n FROM teacher_grade_attempts WHERE batch_id=?", (batch_id,))["n"],
        "analysis_runs": db.q1("SELECT COUNT(*) n FROM teacher_analysis_runs WHERE task_id=?", (batch_id,))["n"],
        "enrollments": db.q1("SELECT COUNT(*) n FROM teacher_enrollments WHERE batch_id=?", (batch_id,))["n"],
        "question_versions": db.q1("SELECT COUNT(*) n FROM teacher_question_versions WHERE batch_id=?", (batch_id,))["n"],
        "jobs": db.q1("SELECT COUNT(*) n FROM teacher_grade_jobs WHERE batch_id=?", (batch_id,))["n"],
        "import_files": db.q1("SELECT COUNT(*) n FROM teacher_import_files WHERE batch_id=?", (batch_id,))["n"],
        "batch": db.q1("SELECT COUNT(*) n FROM teacher_batches WHERE id=?", (batch_id,))["n"],
    }
    if any(residual.values()):
        raise HTTPException(500, f"任务删除后仍有残留数据：{residual}")
    return {"deleted": True, "batch_id": batch_id, "residual": residual}

@router.get("/batches/{batch_id}")
def batch_detail(batch_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    batch = db.q1("SELECT * FROM teacher_batches WHERE id=?", (batch_id,))
    if not batch:
        raise HTTPException(404, "导入任务不存在")
    stats = db.q1("SELECT COUNT(*) total, SUM(CASE WHEN grading_status='pending' THEN 1 ELSE 0 END) pending, SUM(CASE WHEN grading_status='graded' THEN 1 ELSE 0 END) graded, SUM(CASE WHEN grading_status='failed' THEN 1 ELSE 0 END) failed, SUM(CASE WHEN grading_status='needs_review' THEN 1 ELSE 0 END) review FROM teacher_answers WHERE import_batch_id=?", (batch_id,)) or {}
    classes = db.q("SELECT c.id,c.code,COUNT(DISTINCT s.id) students,COUNT(a.id) answers,SUM(CASE WHEN a.grading_status='pending' THEN 1 ELSE 0 END) pending,SUM(CASE WHEN a.grading_status='graded' THEN 1 ELSE 0 END) graded,SUM(CASE WHEN a.grading_status='failed' THEN 1 ELSE 0 END) failed FROM teacher_classes c JOIN teacher_students s ON s.class_id=c.id LEFT JOIN teacher_answers a ON a.student_id=s.id AND a.import_batch_id=? GROUP BY c.id ORDER BY c.code", (batch_id,))
    job = db.q1("SELECT * FROM teacher_grade_jobs WHERE batch_id=? ORDER BY created_at DESC LIMIT 1", (batch_id,))
    return {"batch": {**batch, **stats}, "classes": classes, "jobs": db.q("SELECT * FROM teacher_grade_jobs WHERE batch_id=? ORDER BY created_at DESC", (batch_id,)), "latest_job": job}

@router.get("/batches/{batch_id}/enrollments")
def batch_enrollments(batch_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    owned_batch(batch_id, user)
    rows = db.q("SELECT e.*, s.user_id FROM teacher_enrollments e LEFT JOIN teacher_students s ON s.id=e.student_id WHERE e.batch_id=? ORDER BY e.code", (batch_id,))
    return {"items": rows, "counts": {"total": len(rows), "unverified": sum(1 for row in rows if row["identity_status"] == "unverified"), "same_person": sum(1 for row in rows if row["identity_status"] == "same_person"), "distinct": sum(1 for row in rows if row["identity_status"] == "distinct")}}


@router.post("/batches/{batch_id}/enrollments/{student_id}/identity")
def set_enrollment_identity(batch_id: str, student_id: str, payload: Dict[str, Any] = Body(...), user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """任务内学生身份确认：同编号跨任务不自动视为同一人。"""
    require_teacher(user); init()
    owned_batch(batch_id, user)
    status = str(payload.get("status") or "").strip()
    if status not in ("unverified", "same_person", "distinct", "uncertain"):
        raise HTTPException(422, "身份状态不合法")
    row = db.q1("SELECT * FROM teacher_enrollments WHERE batch_id=? AND student_id=?", (batch_id, student_id))
    if not row:
        raise HTTPException(404, "任务内学生不存在")
    if status in ("same_person", "distinct") and not str(payload.get("note") or "").strip():
        raise HTTPException(422, "确认同一人/不同人时必须填写依据")
    db.x("UPDATE teacher_enrollments SET identity_status=?, identity_note=?, updated_at=? WHERE id=?", (status, str(payload.get("note") or ""), now(), row["id"]))
    return {"enrollment": db.q1("SELECT * FROM teacher_enrollments WHERE id=?", (row["id"],))}


@router.get("/batches/{batch_id}/classes/{class_id}/students")
def batch_students(batch_id: str, class_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    rows = db.q("SELECT s.id,s.code,s.class_id,c.code class_code,COUNT(a.id) total,SUM(CASE WHEN a.grading_status='pending' THEN 1 ELSE 0 END) pending,SUM(CASE WHEN a.grading_status='graded' THEN 1 ELSE 0 END) graded,SUM(CASE WHEN a.grading_status='failed' THEN 1 ELSE 0 END) failed FROM teacher_students s JOIN teacher_classes c ON c.id=s.class_id LEFT JOIN teacher_answers a ON a.student_id=s.id AND a.import_batch_id=? WHERE s.class_id=? GROUP BY s.id ORDER BY s.code", (batch_id, class_id))
    return {"items": rows}

@router.get("/batches/{batch_id}/classes/{class_id}/students/{student_id}/questions")
def batch_student_questions(batch_id: str, class_id: str, student_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    rows = _selected_answers("", class_id, student_id, batch_id)
    for row in rows:
        row["score"] = None if row.get("score") is None else row.get("score")
    return {"items": rows}

def _add_issue(task_id: str, key: str, issue_type: str, category: str, severity: str, row: Dict[str, Any], raw: str, expected: str, reason: str, rule: str, status: str = "detected") -> None:
    db.x("INSERT OR IGNORE INTO teacher_data_issues (id,task_id,issue_key,issue_type,category,severity,source_file,source_row_number,student_id,student_code,class_id,class_code,question_id,event_id,revision,raw_value,expected_value,reason,detection_rule,handling_status,handling_result,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (db.new_id("issue"), task_id, key, issue_type, category, severity, "CSV", row.get("source_row") or 0, row.get("student_id"), row.get("student_code"), row.get("class_id"), row.get("class_code"), row.get("question_id"), row.get("event_id"), row.get("revision"), str(raw)[:1000], str(expected)[:1000], reason, rule, status, "", now()))


def _build_issues(batch_id: str) -> Dict[str, Any]:
    rows = db.q(f"SELECT a.*, s.code student_code, s.class_id, c.code class_code, {QUESTION_COLUMNS} FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id LEFT JOIN teacher_classes c ON c.id=s.class_id {QUESTION_JOIN} WHERE a.import_batch_id=? ORDER BY a.student_id, a.event_id, a.revision", (batch_id,))
    db.x("DELETE FROM teacher_data_issues WHERE task_id=? AND handling_status='detected'", (batch_id,))
    for row in rows:
        key = f"answer:{row['id']}"
        if not str(row.get("solution_process") or "").strip():
            _add_issue(batch_id, key + ":process", "missing_process", "answer", "info", row, "", "学生未提交解题过程", "学生未提交解题过程；不视为数据错误", "solution_process empty", "accepted")
        if not str(row.get("original_answer") or "").strip():
            _add_issue(batch_id, key + ":answer", "missing_final_answer", "answer", "warning", row, "", "学生未填写最终答案", "最终作答为空，需确认是否缺考或漏答", "original_answer empty", "pending_review")
        max_score = float(row.get("question_max_score") or row.get("max_score") or 0)
        raw_score = float(row.get("original_score") or 0)
        if raw_score < 0 or raw_score > max_score:
            _add_issue(batch_id, key + ":score", "score_out_of_range", "score", "error", row, f"{raw_score}/{max_score}", f"0..{max_score}", "原始得分超出题目满分范围（题目满分取任务版本）", "original_score < 0 or > question_version.max_score", "pending_review")
        if not row.get("exam_node_id"):
            _add_issue(batch_id, key + ":exam", "missing_exam_node", "answer", "error", row, "", "必须关联测验节点", "作答缺少测验节点", "exam_node_id empty", "pending_review")
    # question-level checks：按任务内作答绑定的题目版本检查，不回读全局最新题面
    for q in db.q(f"""
        SELECT DISTINCT a.question_id AS id, {QUESTION_COLUMNS}
        FROM teacher_answers a {QUESTION_JOIN}
        WHERE a.import_batch_id=?""", (batch_id,)):
        qrow = {"question_id": q.get("id"), "source_row": 0}
        if not str(q.get("stem") or "").strip():
            _add_issue(batch_id, f"question:{q['id']}:stem", "missing_stem", "question", "error", qrow, "", "题干不能为空", "题目缺少题干", "stem empty", "pending_review")
        if float(q.get("question_max_score") or 0) <= 0:
            _add_issue(batch_id, f"question:{q['id']}:max", "invalid_max_score", "question", "error", qrow, q.get("question_max_score"), ">0", "题目满分必须大于 0（取任务题目版本）", "question_version.max_score <= 0", "pending_review")
        if not str(q.get("reference_answer") or "").strip():
            _add_issue(batch_id, f"question:{q['id']}:ref", "missing_reference_answer", "question", "error", qrow, "", "参考答案不能为空", "题目缺少参考答案", "reference_answer empty", "pending_review")
        if not str(q.get("scoring_points") or "").strip():
            _add_issue(batch_id, f"question:{q['id']}:points", "missing_scoring_points", "question", "error", qrow, "", "评分点不能为空", "题目缺少原始评分点", "scoring_points empty", "pending_review")
        else:
            nums = [float(n) for n in re.findall(r"(\d+(?:\.\d+)?)\s*分", str(q.get("scoring_points")))]
            if nums and abs(sum(nums) - float(q.get("question_max_score") or 0)) > 0.01:
                _add_issue(batch_id, f"question:{q['id']}:sum", "scoring_points_sum_mismatch", "question", "error", qrow, sum(nums), q.get("question_max_score"), "评分点分值之和与题目满分不一致", "sum(score in scoring_points) != question_version.max_score", "pending_review")
        topic = db.q1("SELECT * FROM teacher_topics WHERE id=? OR code=?", (q.get("topic_id"), q.get("topic_id"))) or {}
        if not topic:
            _add_issue(batch_id, f"question:{q['id']}:topic", "missing_topic", "knowledge", "warning", qrow, q.get("topic_id"), "存在的主题", "题目主题编号不存在", "teacher_topics lookup", "pending_review")
        indicator = db.q1("SELECT * FROM teacher_indicators WHERE id=? OR code=?", (q.get("indicator_id"), q.get("indicator_id"))) or {}
        if q.get("indicator_id") and not indicator:
            _add_issue(batch_id, f"question:{q['id']}:indicator", "missing_indicator", "knowledge", "warning", qrow, q.get("indicator_id"), "存在的指标", "题目指标编号不存在", "teacher_indicators lookup", "pending_review")
        if indicator and topic and str(indicator.get("topic_id")) not in (str(topic.get("id")), f"topic_{topic.get('code')}"):
            _add_issue(batch_id, f"question:{q['id']}:indicator_topic", "indicator_topic_mismatch", "knowledge", "error", qrow, indicator.get("topic_id"), topic.get("id"), "指标不属于该题目主题", "indicator.topic_id != question.topic_id", "pending_review")
    # duplicate / conflict groups
    groups = db.q("SELECT student_id,event_id,revision,COUNT(*) n,COUNT(DISTINCT original_answer || '|' || COALESCE(solution_process,'')) variants FROM teacher_answers WHERE import_batch_id=? GROUP BY student_id,event_id,revision HAVING n>1", (batch_id,))
    for g in groups:
        row = {"student_id": g["student_id"], "event_id": g["event_id"], "revision": g["revision"], "source_row": 0}
        if int(g.get("variants") or 0) > 1:
            _add_issue(batch_id, f"duplicate:{g['student_id']}:{g['event_id']}:{g['revision']}", "conflicting_duplicate", "answer", "error", row, g.get("n"), 1, "同事件同版本内容冲突", "student_id+event_id+revision count >1 and distinct content", "pending_review")
        else:
            _add_issue(batch_id, f"duplicate:{g['student_id']}:{g['event_id']}:{g['revision']}", "exact_duplicate", "answer", "warning", row, g.get("n"), 1, "完全重复记录，有效集只保留一份", "same content and key", "auto_normalized")
    issues = db.q("SELECT * FROM teacher_data_issues WHERE task_id=? ORDER BY created_at, id", (batch_id,))
    answer_issue_ids = {row["student_id"] + ":" + str(row["event_id"]) + ":" + str(row["question_id"]) for row in issues if row["severity"] in ("error",) and row.get("student_id")}
    valid = 0
    for row in rows:
        key = row["student_id"] + ":" + str(row["event_id"]) + ":" + str(row["question_id"])
        if key not in answer_issue_ids:
            valid += 1
    missing_process = sum(1 for row in rows if not str(row.get("solution_process") or "").strip())
    anomalies = sum(1 for issue in issues if issue["severity"] == "error")
    pending = sum(1 for issue in issues if issue["handling_status"] == "pending_review")
    duplicates = sum(1 for issue in issues if issue["category"] == "answer" and "duplicate" in issue["issue_type"])
    classes = []
    for cls in db.q("SELECT * FROM teacher_classes ORDER BY code"):
        cr = [row for row in rows if row.get("class_id") == cls["id"]]
        cissues = [issue for issue in issues if issue.get("class_code") == cls["code"]]
        classes.append({"code": cls["code"], "students": len({row["student_id"] for row in cr}), "answers": len(cr), "valid": len(cr) - len({issue.get("student_id") for issue in cissues if issue["severity"] == "error" and issue.get("student_id")}), "missing": sum(1 for row in cr if not str(row.get("solution_process") or "").strip()), "duplicates": len([issue for issue in cissues if "duplicate" in issue["issue_type"]]), "anomalies": len([issue for issue in cissues if issue["severity"] == "error"]), "pending": len([issue for issue in cissues if issue["handling_status"] == "pending_review"])})
    status_counts = {row["handling_status"]: int(row["n"]) for row in db.q("SELECT handling_status,COUNT(*) n FROM teacher_data_issues WHERE task_id=? GROUP BY handling_status", (batch_id,))}
    total_issues = sum(status_counts.values())
    pending_issues = status_counts.get("detected", 0) + status_counts.get("pending_review", 0)
    report = {"raw_records": len(rows), "valid_records": valid, "normalized_records": sum(1 for issue in issues if issue["handling_status"] == "auto_normalized"), "duplicate_groups": duplicates, "conflict_duplicates": sum(1 for issue in issues if issue["issue_type"] == "conflicting_duplicate"), "missing": {"solution_process": missing_process, "original_answer": sum(1 for row in rows if not str(row.get("original_answer") or "").strip())}, "anomalies": anomalies, "associations": sum(1 for issue in issues if issue["category"] == "knowledge"), "pending_confirmation": pending, "excluded": sum(1 for issue in issues if issue["handling_status"] == "excluded"), "unknown": 1, "classes": classes, "total_issues": total_issues, "pending_issues": pending_issues, "accepted_issues": status_counts.get("accepted", 0), "auto_normalized_issues": status_counts.get("auto_normalized", 0), "resolved_issues": status_counts.get("resolved", 0), "excluded_issues": status_counts.get("excluded", 0), "status_counts": status_counts, "issues": [issue for issue in issues if issue["handling_status"] in ("detected", "pending_review")][:100], "by_type": [{"type": r["issue_type"], "count": r["n"]} for r in db.q("SELECT issue_type,COUNT(*) n FROM teacher_data_issues WHERE task_id=? GROUP BY issue_type ORDER BY n DESC", (batch_id,))], "unknowns": ["赛题未提供每个学生每个测验节点的完整应答题清单，预期作答缺失暂不能确定"]}
    return report


@router.get("/batches/{batch_id}/preprocessing")
def preprocessing(batch_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    batch = db.q1("SELECT * FROM teacher_batches WHERE id=?", (batch_id,))
    if not batch:
        raise HTTPException(404, "任务不存在")
    report = _build_issues(batch_id)
    status = batch.get("preprocess_status") or "review"
    db.x("UPDATE teacher_batches SET preprocess_report_json=?, preprocess_status=? WHERE id=?", (json.dumps(report, ensure_ascii=False), status, batch_id))
    return {"batch_id": batch_id, "status": status, "report": report}

@router.get("/batches/{batch_id}/issues")
def task_issues(batch_id: str, status: str = "pending", page: int = 1, page_size: int = 50, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    allowed = {"pending": ["detected", "pending_review"], "accepted": ["accepted"], "auto_normalized": ["auto_normalized"], "resolved": ["resolved"], "excluded": ["excluded"], "all": ["detected", "pending_review", "accepted", "auto_normalized", "resolved", "excluded"]}
    statuses = allowed.get(status, allowed["pending"])
    placeholders = ",".join("?" for _ in statuses)
    total = db.q1(f"SELECT COUNT(*) n FROM teacher_data_issues WHERE task_id=? AND handling_status IN ({placeholders})", tuple([batch_id] + statuses))["n"]
    page = max(1, page); page_size = max(1, min(200, page_size))
    items = db.q(f"SELECT * FROM teacher_data_issues WHERE task_id=? AND handling_status IN ({placeholders}) ORDER BY created_at, id LIMIT ? OFFSET ?", tuple([batch_id] + statuses + [page_size, (page - 1) * page_size]))
    return {"items": items, "total": total, "page": page, "page_size": page_size, "has_more": page * page_size < total, "status": status}

@router.post("/batches/{batch_id}/preprocessing/confirm")
def confirm_preprocessing(batch_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    if not db.q1("SELECT id FROM teacher_batches WHERE id=?", (batch_id,)):
        raise HTTPException(404, "任务不存在")
    db.x("UPDATE teacher_batches SET preprocess_status='confirmed' WHERE id=?", (batch_id,))
    return {"batch_id": batch_id, "status": "confirmed"}

@router.get("/batches/{batch_id}/overview")
def batch_overview(batch_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    batch = db.q1("SELECT * FROM teacher_batches WHERE id=?", (batch_id,))
    if not batch:
        raise HTTPException(404, "导入任务不存在")
    stats = db.q1("SELECT COUNT(*) total, SUM(CASE WHEN grading_status='pending' THEN 1 ELSE 0 END) pending, SUM(CASE WHEN grading_status='graded' THEN 1 ELSE 0 END) graded, SUM(CASE WHEN grading_status='failed' THEN 1 ELSE 0 END) failed, SUM(CASE WHEN grading_status='needs_review' THEN 1 ELSE 0 END) review FROM teacher_answers WHERE import_batch_id=?", (batch_id,)) or {}
    personal = _personal_rows("", "", "", batch_id)
    classes = []
    for cls in db.q("SELECT * FROM teacher_classes ORDER BY code"):
        cls_students = [row for row in personal if row.get("class_id") == cls["id"] and row.get("exam_node_id")]
        completed = [row for row in cls_students if row.get("complete") and row.get("score_rate") is not None]
        scores = [row["score"] for row in completed]
        rates = [row["score_rate"] for row in completed]
        total_students = db.q1("SELECT COUNT(*) n FROM teacher_students WHERE class_id=?", (cls["id"],))["n"]
        answers = db.q1("SELECT COUNT(*) n FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id WHERE a.import_batch_id=? AND s.class_id=?", (batch_id, cls["id"]))["n"]
        classes.append({"id": cls["id"], "code": cls["code"], "students": total_students, "answers": answers, "completed_students": len(completed), "average_score": round(sum(scores)/len(scores),2) if scores else None, "average_rate": round(sum(rates)/len(rates),2) if rates else None, "highest": round(max(scores),2) if scores else None, "lowest": round(min(scores),2) if scores else None})
    job = db.q1("SELECT * FROM teacher_grade_jobs WHERE batch_id=? ORDER BY created_at DESC LIMIT 1", (batch_id,))
    return {"batch": {**batch, **stats}, "classes": classes, "latest_job": job, "jobs": db.q("SELECT * FROM teacher_grade_jobs WHERE batch_id=? ORDER BY created_at DESC", (batch_id,))}

@router.get("/batches/{batch_id}/classes/{class_id}/overview")
def class_overview(batch_id: str, class_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    cls = db.q1("SELECT * FROM teacher_classes WHERE id=?", (class_id,))
    if not cls:
        raise HTTPException(404, "班级不存在")
    personal = [row for row in _personal_rows("", class_id, "", batch_id) if row.get("exam_node_id")]
    students = []
    for stu in db.q("SELECT * FROM teacher_students WHERE class_id=? ORDER BY code", (class_id,)):
        rows = [row for row in personal if row["student_id"] == stu["id"]]
        graded = sum(row["graded_questions"] for row in rows); total = sum(row["total_questions"] for row in rows)
        score = sum(row["score"] for row in rows if row["score"] is not None); max_score = sum(row["total_max_score"] for row in rows)
        failed = db.q1("SELECT COUNT(*) n FROM teacher_answers WHERE import_batch_id=? AND student_id=? AND grading_status='failed'", (batch_id, stu["id"]))["n"]
        review = db.q1("SELECT COUNT(*) n FROM teacher_grade_runs r JOIN teacher_answers a ON a.id=r.answer_id WHERE a.import_batch_id=? AND a.student_id=? AND r.needs_review=1", (batch_id, stu["id"]))["n"]
        students.append({"id": stu["id"], "code": stu["code"], "total_questions": total, "graded_questions": graded, "pending": total-graded, "failed": failed, "review": review, "score": round(score,2), "max_score": round(max_score,2), "score_rate": round(score/max_score*100,2) if max_score else None, "complete": total > 0 and graded == total})
    completed = [s for s in students if s["complete"]]
    return {"class": cls, "students": students, "completed_students": len(completed), "student_count": len(students)}

@router.get("/batches/{batch_id}/classes/{class_id}/students/{student_id}/groups")
def student_groups(batch_id: str, class_id: str, student_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    rows = [row for row in _selected_answers("", class_id, student_id, batch_id)]
    groups = {}
    for row in rows:
        key = row.get("exam_node_id") or "all"
        group = groups.setdefault(key, {"id": key, "name": f"测验节点 {key}" if key != "all" else "全部题目", "questions": 0, "graded": 0, "score": 0.0, "max_score": 0.0, "review": 0})
        group["questions"] += 1
        group["max_score"] += float(row.get("question_max_score") or 0)
        if row.get("score") is not None:
            group["graded"] += 1; group["score"] += row["score"]
    for group in groups.values():
        group["pending"] = group["questions"] - group["graded"]
        group["score_rate"] = round(group["score"] / group["max_score"] * 100, 2) if group["max_score"] else None
    return {"items": list(groups.values()), "group_by": "测验节点（CSV 无独立题组字段）"}

@router.get("/batches/{batch_id}/classes/{class_id}/students/{student_id}/groups/{group_id}/questions")
def student_group_questions(batch_id: str, class_id: str, student_id: str, group_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    rows = [row for row in _selected_answers("", class_id, student_id, batch_id) if (row.get("exam_node_id") or "all") == group_id]
    return {"items": rows}

@router.get("/batches/{batch_id}/answers/{answer_id}")
def batch_answer_detail(batch_id: str, answer_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    row = db.q1("SELECT a.* FROM teacher_answers a WHERE a.id=? AND a.import_batch_id=?", (answer_id, batch_id))
    if not row:
        raise HTTPException(404, "该作答不属于当前任务")
    return answer_detail(answer_id, user)

@router.get("/analysis/compare")
def analysis_compare(batch_id: str = "", class_ids: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    if not batch_id:
        row = latest_owned_batch(user)
        batch_id = row["id"] if row else ""
    available = {row["id"]: row for row in analysis.all_classes(batch_id)}
    requested = [item for item in class_ids.split(",") if item]
    selected = []
    for item in requested or list(available):
        if item not in selected and item in available:
            selected.append(item)
    if len(selected) < 2:
        raise HTTPException(422, "至少选择当前任务中的两个班级")
    profiles = [(cid, analysis.class_profile(batch_id, cid)) for cid in selected]
    keys = sorted({point["knowledge_point_id"] for _, profile in profiles for point in profile.get("knowledge_points", [])})
    rows = []
    for key in keys:
        name = next((point["name"] for _, profile in profiles for point in profile.get("knowledge_points", []) if point["knowledge_point_id"] == key), key)
        values = []
        for cid, profile in profiles:
            point = next((item for item in profile.get("knowledge_points", []) if item["knowledge_point_id"] == key), None)
            values.append({"class_id": cid, "class_code": available[cid]["code"], "mean": point.get("mean_mastery") if point else None, "valid_students": point.get("valid_students", 0) if point else 0, "total_students": profile.get("student_count"), "coverage": point.get("coverage") if point else None, "std_dev": point.get("std_dev") if point else None})
        present = [item for item in values if item["mean"] is not None]
        rows.append({"knowledge_point_id": key, "name": name, "values": values, "difference": round(max(item["mean"] for item in present) - min(item["mean"] for item in present), 4) if len(present) >= 2 else None, "common_weak": len(present) == len(values) and all(item["mean"] < 0.65 for item in present)})
    return {"task_id": batch_id, "classes": [{"id": cid, "code": available[cid]["code"], "students": db.q1("SELECT COUNT(DISTINCT a.student_id) n FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id WHERE a.import_batch_id=? AND s.class_id=?", (batch_id, cid))["n"]} for cid in selected], "rows": rows, "common_weak": [row for row in rows if row["common_weak"]], "algorithm_version": analysis.ALGORITHM_VERSION, "parameters": {"alpha0": analysis.A0, "beta0": analysis.B0, "min_evidence": analysis.MIN_EVIDENCE, "min_coverage": analysis.MIN_COVERAGE}}

@router.get("/analysis/classes")
def analysis_classes(batch_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    if not batch_id:
        row = latest_owned_batch(user)
        batch_id = row["id"] if row else ""
    if not batch_id:
        return {"task_id": "", "classes": []}
    return {"task_id": batch_id, "classes": analysis.all_classes(batch_id)}

@router.get("/analysis/classes/{class_id}")
def analysis_class(class_id: str, batch_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    if not batch_id:
        row = latest_owned_batch(user)
        batch_id = row["id"] if row else ""
    if not any(row["id"] == class_id for row in analysis.all_classes(batch_id)):
        raise HTTPException(404, "该班级不属于当前任务")
    result = analysis.class_profile(batch_id, class_id)
    result["analysis_id"] = analysis.save_analysis(batch_id, "class", class_id, result)
    return result

@router.get("/analysis/classes/{class_id}/questions/{question_id}")
def analysis_question(class_id: str, question_id: str, batch_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    if not batch_id:
        row = latest_owned_batch(user)
        batch_id = row["id"] if row else ""
    if not any(row["id"] == class_id for row in analysis.all_classes(batch_id)):
        raise HTTPException(404, "该班级不属于当前任务")
    result = analysis.question_analysis_detail(batch_id, class_id, question_id)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result

@router.get("/analysis/classes/{class_id}/questions/{question_id}/longitudinal")
def analysis_longitudinal(class_id: str, question_id: str, batch_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    if not batch_id:
        row = latest_owned_batch(user)
        batch_id = row["id"] if row else ""
    if not any(row["id"] == class_id for row in analysis.all_classes(batch_id)):
        raise HTTPException(404, "该班级不属于当前任务")
    return analysis.question_longitudinal(batch_id, class_id, question_id)

@router.get("/analysis/students/{student_id}")
def analysis_student(student_id: str, batch_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init()
    if teacher(user):
        student_in_scope(student_id, user)
    else:
        mine = student_for(user)
        if not mine or mine["id"] != student_id:
            raise HTTPException(403, "不能查看其他学生学情")
    if not batch_id:
        row = latest_owned_batch(user)
        batch_id = row["id"] if row else ""
    if not db.q1("SELECT 1 FROM teacher_answers WHERE import_batch_id=? AND student_id=? LIMIT 1", (batch_id, student_id)):
        raise HTTPException(404, "该学生不属于当前任务或没有作答数据")
    result = analysis.student_profile(batch_id, student_id)
    if "error" in result:
        raise HTTPException(404, result["error"])
    result["analysis_id"] = analysis.save_analysis(batch_id, "student", student_id, result)
    return result

@router.get("/analysis/me")
def analysis_me(batch_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init()
    mine = student_for(user)
    if not mine:
        raise HTTPException(403, "当前账号不是学生")
    if not batch_id:
        row = db.q1("SELECT a.import_batch_id id FROM teacher_answers a WHERE a.student_id=? ORDER BY a.id DESC LIMIT 1", (mine["id"],))
        batch_id = row["id"] if row else ""
    if not batch_id:
        raise HTTPException(404, "当前学生没有可分析任务")
    result = analysis.student_profile(batch_id, mine["id"])
    result["analysis_id"] = analysis.save_analysis(batch_id, "student", mine["id"], result)
    return result

@router.get("/analysis/comparability")
def analysis_comparability(batch_id: str = "", nodes: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    if not batch_id:
        row = latest_owned_batch(user)
        batch_id = row["id"] if row else ""
    node_ids = [item for item in nodes.split(",") if item]
    if len(node_ids) < 2:
        raise HTTPException(422, "至少提供两个测验节点")
    signatures = [analysis.node_signature(batch_id, node) for node in node_ids]
    comparisons = [analysis.compare_signatures(signatures[index], signatures[index + 1]) for index in range(len(signatures) - 1)]
    return {"batch_id": batch_id, "nodes": node_ids, "signatures": signatures, "comparisons": comparisons}


@router.get("/analysis/trend/student/{student_id}")
def analysis_student_trend(student_id: str, batch_ids: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init()
    if teacher(user):
        student_in_scope(student_id, user)
    else:
        mine = student_for(user)
        if not mine or mine["id"] != student_id:
            raise HTTPException(403, "不能查看其他学生趋势")
    if not batch_ids:
        row = db.q1("SELECT import_batch_id id FROM teacher_answers WHERE student_id=? ORDER BY id DESC LIMIT 1", (student_id,))
        batch_ids = row["id"] if row else ""
    selected = [item for item in batch_ids.split(",") if item]
    return analysis.student_knowledge_trend(student_id, owned_batch_ids(user, selected))


@router.get("/analysis/trend/class/{class_id}")
def analysis_class_trend(class_id: str, batch_ids: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    if not batch_ids:
        row = latest_owned_batch(user)
        batch_ids = row["id"] if row else ""
    selected = owned_batch_ids(user, [item for item in batch_ids.split(",") if item])
    return analysis.class_knowledge_trend(class_id, selected)


@router.get("/analysis/students/{student_id}/knowledge/{knowledge_point_id}")
def analysis_student_knowledge(student_id: str, knowledge_point_id: str, batch_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init()
    if teacher(user):
        student_in_scope(student_id, user)
    else:
        mine = student_for(user)
        if not mine or mine["id"] != student_id:
            raise HTTPException(403, "不能查看其他学生知识点")
    if not batch_id:
        row = db.q1("SELECT import_batch_id id FROM teacher_answers WHERE student_id=? ORDER BY id DESC LIMIT 1", (student_id,))
        batch_id = row["id"] if row else ""
    if teacher(user):
        owned_batch(batch_id, user)
    profile = analysis.student_profile(batch_id, student_id)
    point = next((item for item in profile.get("mastery", []) if item["knowledge_point_id"] == knowledge_point_id), None)
    evidence = [item for item in profile.get("evidence", []) if item["knowledge_point_id"] == knowledge_point_id]
    methods = sorted({method["method_name"] for item in evidence for method in item.get("methods") or []})
    questions = []
    seen = set()
    for item in evidence:
        if item["question_id"] in seen:
            continue
        seen.add(item["question_id"])
        question = db.q1("SELECT COALESCE(v.stem,q.stem) stem, COALESCE(v.max_score,q.max_score) max_score FROM teacher_answers a LEFT JOIN teacher_question_versions v ON v.version_id=a.question_version_id LEFT JOIN teacher_questions q ON q.id=a.question_id WHERE a.import_batch_id=? AND a.question_id=? LIMIT 1", (batch_id, item["question_id"])) or {}
        questions.append({"question_id": item["question_id"], "stem": question.get("stem"), "max_score": question.get("max_score")})
    return {
        "batch_id": batch_id, "student_id": student_id, "knowledge_point_id": knowledge_point_id,
        "knowledge_point": point, "questions": questions, "evidence": evidence, "methods": methods,
        "evidence_count": len(evidence),
        "traceable": all(item.get("answer_id") and item.get("scoring_point_id") for item in evidence),
    }


@router.get("/knowledge-graph")
def knowledge_graph(batch_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    if not batch_id:
        row = latest_owned_batch(user)
        batch_id = row["id"] if row else ""
    return analysis.knowledge_graph(batch_id)


@router.post("/knowledge-graph/candidates")
def knowledge_graph_candidates(payload: Dict[str, Any] = Body(default={}), user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    batch_id = str(payload.get("batch_id") or "")
    if not batch_id:
        raise HTTPException(422, "必须指定任务")
    return analysis.generate_candidate_edges(batch_id, user["id"])


@router.post("/knowledge-graph/edges")
def add_knowledge_graph_edge(payload: Dict[str, Any] = Body(...), user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    batch_id = str(payload.get("batch_id") or "")
    if not batch_id:
        raise HTTPException(422, "必须指定任务")
    try:
        edge = analysis.add_knowledge_edge(batch_id, str(payload.get("from_kp") or ""), str(payload.get("to_kp") or ""), "teacher", "confirmed", user["id"])
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"edge": edge}


@router.delete("/knowledge-graph/edges/{edge_id}")
def delete_knowledge_graph_edge(edge_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    if not analysis.delete_knowledge_edge(edge_id):
        raise HTTPException(404, "依赖关系不存在")
    return {"deleted": True, "edge_id": edge_id}


@router.get("/knowledge-graph/students/{student_id}/gaps")
def knowledge_graph_student_gaps(student_id: str, batch_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init()
    if teacher(user):
        student_in_scope(student_id, user)
    else:
        mine = student_for(user)
        if not mine or mine["id"] != student_id:
            raise HTTPException(403, "不能查看其他学生")
    if not batch_id:
        row = db.q1("SELECT import_batch_id id FROM teacher_answers WHERE student_id=? ORDER BY id DESC LIMIT 1", (student_id,))
        batch_id = row["id"] if row else ""
    if teacher(user):
        owned_batch(batch_id, user)
    return analysis.student_prerequisite_gaps(student_id, batch_id)


@router.get("/knowledge-graph/{knowledge_point_id}/dependencies")
def knowledge_graph_dependencies(knowledge_point_id: str, batch_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    if not batch_id:
        row = latest_owned_batch(user)
        batch_id = row["id"] if row else ""
    return analysis.knowledge_dependencies(batch_id, knowledge_point_id)


@router.post("/answers/{answer_id}/error-labels/classify")
def classify_errors(answer_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    try:
        return analysis.classify_answer_errors(answer_id, operator_id=user["id"])
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.get("/answers/{answer_id}/error-labels")
def get_error_labels(answer_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init()
    answer = db.q1("SELECT * FROM teacher_answers WHERE id=?", (answer_id,))
    if not answer or not visible_answer(answer, user):
        raise HTTPException(404, "作答不存在")
    rows = db.q("SELECT * FROM answer_error_labels WHERE answer_id=? ORDER BY updated_at DESC", (answer_id,))
    gold = db.q1("SELECT * FROM error_label_gold WHERE answer_id=?", (answer_id,))
    return {"items": [{**row, "labels": json.loads(row["labels_json"] or "[]")} for row in rows], "gold": {**gold, "labels": json.loads(gold["labels_json"] or "[]")} if gold else None, "taxonomy": analysis.ERROR_TAXONOMY}


@router.post("/answers/{answer_id}/error-labels")
def set_error_labels(answer_id: str, payload: Dict[str, Any] = Body(...), user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    source = str(payload.get("source") or "teacher")
    if source not in ("teacher", "llm"):
        raise HTTPException(422, "来源必须是 teacher 或 llm")
    try:
        result = analysis.set_error_labels(answer_id, payload.get("labels") or [], source, float(payload.get("confidence") or (1.0 if source == "teacher" else 0.6)), str(payload.get("rationale") or ""), user["id"], str(payload.get("grade_run_id") or ""), str(payload.get("model") or ""), str(payload.get("prompt_version") or ""))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return result


@router.post("/batches/{batch_id}/error-labels/classify")
def classify_batch_errors(batch_id: str, payload: Dict[str, Any] = Body(default={}), user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """用已有判题证据批量生成错误标签（规则分类，不重新调用模型）。"""
    require_teacher(user); init()
    class_id = str(payload.get("class_id") or "")
    sql = "SELECT a.id FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id WHERE a.import_batch_id=? AND a.grading_status IN ('graded','needs_review','manual_confirmed','manual_corrected')"
    args: List[Any] = [batch_id]
    if class_id:
        sql += " AND s.class_id=?"; args.append(class_id)
    classified = 0
    skipped = 0
    for row in db.q(sql, tuple(args)):
        try:
            analysis.classify_answer_errors(row["id"], operator_id=user["id"])
            classified += 1
        except Exception:
            skipped += 1
    return {"batch_id": batch_id, "classified": classified, "skipped": skipped, "summary": analysis.error_label_summary(batch_id, class_id)}


@router.get("/batches/{batch_id}/method-opportunities")
def batch_method_opportunities(batch_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    owned_batch(batch_id, user)
    rows = analysis.method_opportunities(batch_id)
    return {"items": rows, "summary": {"total": len(rows), "methods": len({row["method_id"] for row in rows}), "questions": len({row["question_id"] for row in rows})}}


@router.post("/batches/{batch_id}/method-opportunities/generate")
def batch_method_opportunities_generate(batch_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    owned_batch(batch_id, user)
    return analysis.generate_method_opportunities(batch_id, user["id"])


@router.get("/version-compat")
def version_compat(kind: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    analysis.ensure_model()
    if kind:
        return {"items": db.q("SELECT * FROM teacher_version_compat WHERE kind=? ORDER BY created_at DESC", (kind,))}
    return {"items": db.q("SELECT * FROM teacher_version_compat ORDER BY created_at DESC")}


@router.post("/version-compat")
def create_version_compat(payload: Dict[str, Any] = Body(...), user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    analysis.ensure_model()
    kind = str(payload.get("kind") or "").strip()
    left = str(payload.get("left_version") or "").strip()
    right = str(payload.get("right_version") or "").strip()
    basis = str(payload.get("basis") or "").strip()
    if kind not in ("grading_rule_version", "scoring_rule_version", "mapping_version", "question_version"):
        raise HTTPException(422, "版本类型不合法")
    if not left or not right or not basis:
        raise HTTPException(422, "必须提供两个版本号和兼容依据")
    if left == right:
        raise HTTPException(422, "相同版本不需要登记兼容关系")
    db.x("INSERT OR REPLACE INTO teacher_version_compat (id,kind,left_version,right_version,status,basis,note,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)", (db.new_id("versioncompat"), kind, left, right, "compatible", basis, str(payload.get("note") or ""), user["id"], now()))
    return {"items": db.q("SELECT * FROM teacher_version_compat WHERE kind=? AND left_version=? AND right_version=?", (kind, left, right))}


@router.post("/students/{student_id}/recommendations/feedback")
def create_recommendation_feedback(student_id: str, payload: Dict[str, Any] = Body(...), user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """记录推荐 → 练习 → 反馈的闭环起点（不引入复杂推荐模型）。"""
    init()
    if teacher(user):
        student_in_scope(student_id, user)
    else:
        mine = student_for(user)
        if not mine or mine["id"] != student_id:
            raise HTTPException(403, "不能记录其他学生的推荐反馈")
    batch_id = str(payload.get("batch_id") or "")
    if teacher(user):
        owned_batch(batch_id, user)
    if not batch_id:
        raise HTTPException(422, "必须指定任务")
    feedback_id = db.new_id("recfeedback")
    db.x("INSERT INTO recommendation_feedback (id,batch_id,student_id,recommendation_type,target_id,reason,evidence_json,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (feedback_id, batch_id, student_id, str(payload.get("recommendation_type") or "knowledge_point"), str(payload.get("target_id") or ""), str(payload.get("reason") or ""), json.dumps(payload.get("evidence") or {}, ensure_ascii=False), "planned", now(), now()))
    return {"feedback": db.q1("SELECT * FROM recommendation_feedback WHERE id=?", (feedback_id,))}


@router.post("/recommendations/feedback/{feedback_id}/complete")
def complete_recommendation_feedback(feedback_id: str, payload: Dict[str, Any] = Body(default={}), user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init()
    row = db.q1("SELECT * FROM recommendation_feedback WHERE id=?", (feedback_id,))
    if not row:
        raise HTTPException(404, "反馈记录不存在")
    if teacher(user):
        owned_batch(row["batch_id"], user)
    else:
        mine = student_for(user)
        if not mine or mine["id"] != row["student_id"]:
            raise HTTPException(403, "不能修改其他学生的推荐反馈")
    db.x("UPDATE recommendation_feedback SET status='completed', followup_json=?, completed_at=?, updated_at=? WHERE id=?", (json.dumps(payload.get("followup") or {}, ensure_ascii=False), now(), now(), feedback_id))
    return {"feedback": db.q1("SELECT * FROM recommendation_feedback WHERE id=?", (feedback_id,))}


@router.get("/students/{student_id}/recommendations/feedback")
def list_recommendation_feedback(student_id: str, batch_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init()
    if teacher(user):
        student_in_scope(student_id, user)
    else:
        mine = student_for(user)
        if not mine or mine["id"] != student_id:
            raise HTTPException(403, "不能查看其他学生的推荐反馈")
    if batch_id:
        if teacher(user):
            owned_batch(batch_id, user)
        rows = db.q("SELECT * FROM recommendation_feedback WHERE student_id=? AND batch_id=? ORDER BY created_at DESC", (student_id, batch_id))
    else:
        rows = db.q("SELECT * FROM recommendation_feedback WHERE student_id=? ORDER BY created_at DESC LIMIT 50", (student_id,))
    return {"items": rows}


@router.post("/answers/{answer_id}/error-labels/gold")
def set_gold_error_labels(answer_id: str, payload: Dict[str, Any] = Body(...), user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """人工标注（评价基准），与规则/模型标签分开保存。"""
    require_teacher(user); init()
    owned_answer(answer_id, user)
    try:
        return analysis.set_gold_error_labels(answer_id, payload.get("labels") or [], user["id"], str(payload.get("note") or ""))
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.get("/batches/{batch_id}/error-labels/metrics")
def batch_error_label_metrics(batch_id: str, class_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    owned_batch(batch_id, user)
    return analysis.error_label_metrics(batch_id, class_id)


@router.get("/batches/{batch_id}/error-labels")
def batch_error_labels(batch_id: str, class_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    return analysis.error_label_summary(batch_id, class_id)


@router.get("/students/{student_id}/recommendations")
def student_recommendations(student_id: str, batch_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init()
    if teacher(user):
        student_in_scope(student_id, user)
    else:
        mine = student_for(user)
        if not mine or mine["id"] != student_id:
            raise HTTPException(403, "不能查看其他学生推荐")
    if not batch_id:
        row = db.q1("SELECT import_batch_id id FROM teacher_answers WHERE student_id=? ORDER BY id DESC LIMIT 1", (student_id,))
        batch_id = row["id"] if row else ""
    if teacher(user):
        owned_batch(batch_id, user)
    return analysis.recommendations(student_id, batch_id)


@router.get("/analysis/compare-detailed")
def analysis_compare_detailed(batch_id: str = "", class_ids: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    if not batch_id:
        row = latest_owned_batch(user)
        batch_id = row["id"] if row else ""
    available = {row["id"] for row in analysis.all_classes(batch_id)}
    selected = [item for item in class_ids.split(",") if item and item in available]
    if len(selected) < 2:
        raise HTTPException(422, "至少选择当前任务中的两个班级")
    return analysis.compare_classes_detailed(batch_id, selected)


@router.get("/batches/{batch_id}/quality-gate")
def quality_gate(batch_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    batch = db.q1("SELECT * FROM teacher_batches WHERE id=?", (batch_id,))
    if not batch:
        raise HTTPException(404, "任务不存在")
    issues = []
    if (batch.get("preprocess_status") or "review") != "confirmed":
        issues.append({"type": "preprocess_unconfirmed", "blocking": True, "message": "数据预处理尚未确认"})
    orphan_students = db.q1("SELECT COUNT(*) n FROM teacher_answers a LEFT JOIN teacher_students s ON s.id=a.student_id WHERE a.import_batch_id=? AND s.id IS NULL", (batch_id,))["n"]
    if orphan_students:
        issues.append({"type": "orphan_student", "blocking": True, "count": orphan_students, "message": "作答存在孤立学生"})
    orphan_questions = db.q1("SELECT COUNT(*) n FROM teacher_answers a LEFT JOIN teacher_questions q ON q.id=a.question_id LEFT JOIN teacher_question_versions v ON v.version_id=a.question_version_id WHERE a.import_batch_id=? AND q.id IS NULL AND v.version_id IS NULL", (batch_id,))["n"]
    if orphan_questions:
        issues.append({"type": "orphan_question", "blocking": True, "count": orphan_questions, "message": "作答对应的题目既无版本也不存在于题表"})
    invalid_max = db.q1(f"SELECT COUNT(*) n FROM teacher_answers a {QUESTION_JOIN} WHERE a.import_batch_id=? AND COALESCE(COALESCE(v.max_score,q.max_score),a.max_score,0) <= 0", (batch_id,))["n"]
    if invalid_max:
        issues.append({"type": "invalid_max_score", "blocking": True, "count": invalid_max, "message": "存在满分缺失或非正数（按任务题目版本判定）"})
    question_rows = db.q(f"SELECT DISTINCT a.question_id AS id, {QUESTION_COLUMNS}, CASE WHEN v.version_id IS NOT NULL THEN 1 ELSE 0 END AS has_version FROM teacher_answers a {QUESTION_JOIN} WHERE a.import_batch_id=?", (batch_id,))
    missing_ref = sum(1 for q in question_rows if not str(q.get("reference_answer") or "").strip())
    if missing_ref:
        issues.append({"type": "missing_reference_answer", "blocking": False, "count": missing_ref, "message": "存在缺少参考答案的题目，判题置信度会下降"})
    missing_points = sum(1 for q in question_rows if not str(q.get("scoring_points") or "").strip())
    if missing_points:
        issues.append({"type": "missing_scoring_points", "blocking": True, "count": missing_points, "message": "存在缺少评分点的题目"})
    unmapped = sum(1 for q in question_rows if not str(q.get("indicator_id") or "").strip())
    if unmapped:
        issues.append({"type": "unmapped_knowledge", "blocking": False, "count": unmapped, "message": "存在无法映射知识点的题目，相关评分点不进入掌握估计"})
    fallback = sum(1 for q in question_rows if not q.get("has_version"))
    if fallback:
        issues.append({"type": "question_version_fallback", "blocking": False, "count": fallback, "message": "有题目没有任务版本，只能回退全局题表；建议重新导入以生成版本"})
    duplicates = db.q1("SELECT COUNT(*) n FROM (SELECT import_batch_id,event_id,revision,COUNT(*) c FROM teacher_answers WHERE import_batch_id=? GROUP BY event_id,revision HAVING c>1)", (batch_id,))["n"]
    if duplicates:
        issues.append({"type": "duplicate_event_revision", "blocking": True, "count": duplicates, "message": "存在重复 event+revision"})
    no_process = db.q1("SELECT COUNT(*) n FROM teacher_answers WHERE import_batch_id=? AND (solution_process IS NULL OR solution_process='')", (batch_id,))["n"]
    if no_process:
        issues.append({"type": "missing_process", "blocking": False, "count": no_process, "message": "部分学生未提交解题过程，只做整体判定"})
    blocking = any(item.get("blocking") for item in issues)
    versioned = sum(1 for q in question_rows if q.get("has_version"))
    return {"batch_id": batch_id, "blocking": blocking, "issues": issues, "answer_count": db.q1("SELECT COUNT(*) n FROM teacher_answers WHERE import_batch_id=?", (batch_id,))["n"], "confirmed": (batch.get("preprocess_status") or "") == "confirmed", "question_sources": {"version": versioned, "global_fallback": fallback}}

@router.get("/batches/{batch_id}/mapping")
def batch_mapping(batch_id: str, kp_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    owned_batch(batch_id, user)
    result = analysis.mapping_for_batch(batch_id)
    if kp_id:
        result["items"] = [item for item in result["items"] if any(link["knowledge_point_id"] == kp_id for link in item.get("knowledge_links") or [])]
        result["filter"] = {"knowledge_point_id": kp_id}
    return result


@router.get("/batches/{batch_id}/mapping/impact")
def mapping_impact(batch_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """映射变更影响面：当前版本、旧版本判题与分析快照。"""
    require_teacher(user); init()
    owned_batch(batch_id, user)
    version = analysis.current_mapping_version(batch_id)
    stale_grades = db.q("SELECT id,answer_id,mapping_version,created_at FROM teacher_grade_runs WHERE answer_id IN (SELECT id FROM teacher_answers WHERE import_batch_id=?) AND COALESCE(mapping_version,'0') <> ? ORDER BY created_at DESC LIMIT 200", (batch_id, str(version)))
    stale_analyses = db.q("SELECT id,scope,scope_id,mapping_version,created_at FROM teacher_analysis_runs WHERE task_id=? AND COALESCE(mapping_version,'0') <> ? ORDER BY created_at DESC LIMIT 50", (batch_id, str(version)))
    return {"batch_id": batch_id, "mapping_version": version, "stale_grades": len(stale_grades), "stale_grade_items": stale_grades, "stale_analyses": len(stale_analyses), "stale_analysis_items": stale_analyses, "note": "映射版本变化后旧判题/旧快照仍保留，但需重新判题或重算才能反映新映射"}


@router.get("/batches/{batch_id}/quality-gate-overrides")
def quality_gate_overrides(batch_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    return {"items": db.q("SELECT * FROM teacher_quality_gate_overrides WHERE batch_id=? ORDER BY created_at DESC", (batch_id,))}


@router.get("/analysis/mapping-audit")
def analysis_mapping_audit(batch_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    if not batch_id:
        row = latest_owned_batch(user)
        batch_id = row["id"] if row else ""
    if batch_id:
        owned_batch(batch_id, user)
    return analysis.mapping_audit(batch_id)


@router.get("/knowledge-points")
def knowledge_points(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    topics = {row["id"]: row for row in db.q("SELECT * FROM teacher_topics")}
    items = []
    for row in db.q("SELECT * FROM teacher_indicators ORDER BY code"):
        topic = topics.get(row.get("topic_id")) or {}
        items.append({"id": f"kp:{row['code']}", "code": row["code"], "name": row.get("description") or row["code"], "topic_id": row.get("topic_id"), "topic_title": topic.get("title") or ""})
    return {"items": items}


@router.get("/methods")
def methods(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    return {"items": analysis.list_methods()}


@router.post("/methods")
def create_method(payload: Dict[str, Any] = Body(...), user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    try:
        method = analysis.upsert_method(str(payload.get("code") or ""), str(payload.get("name") or ""), str(payload.get("description") or ""), str(payload.get("topic_id") or ""), user["id"])
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"method": method}


@router.post("/scoring-points/{sp_id}/knowledge-links")
def set_scoring_point_knowledge_links(sp_id: str, payload: Dict[str, Any] = Body(...), user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    try:
        result = analysis.set_knowledge_links(sp_id, payload.get("links") or [], user["id"], str(payload.get("action") or "confirm"))
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"scoring_point_id": sp_id, "mapping": result}


@router.post("/scoring-points/{sp_id}/methods")
def set_scoring_point_methods(sp_id: str, payload: Dict[str, Any] = Body(...), user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    try:
        result = analysis.set_method_links(sp_id, payload.get("links") or [], user["id"])
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    point = db.q1("SELECT batch_id FROM scoring_points WHERE id=?", (sp_id,)) or {}
    if point.get("batch_id"):
        analysis.generate_method_opportunities(point["batch_id"], user["id"])
    return result


@router.get("/scoring-points/{sp_id}")
def scoring_point_detail(sp_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    point = db.q1("SELECT * FROM scoring_points WHERE id=?", (sp_id,))
    if not point:
        raise HTTPException(404, "评分点不存在")
    history = db.q("SELECT * FROM scoring_point_link_history WHERE scoring_point_id=? ORDER BY created_at DESC LIMIT 50", (sp_id,))
    return {
        "scoring_point": point,
        "knowledge_links": db.q("SELECT * FROM scoring_point_knowledge_links WHERE scoring_point_id=? AND status != 'disabled'", (sp_id,)),
        "method_links": db.q("SELECT * FROM scoring_point_method_links WHERE scoring_point_id=? AND status != 'disabled'", (sp_id,)),
        "history": history,
    }


@router.get("/audit")
def audit(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    own = str(user.get("id") or "")
    batch = db.q1("SELECT * FROM teacher_batches WHERE owner_id IS NULL OR owner_id=? ORDER BY created_at DESC LIMIT 1", (own,)) or {}
    counts = json.loads(batch.get("counts_json") or "{}")
    if "answers" in counts:
        counts["answers_source"] = int(counts.get("answers") or 0) + int(counts.get("skipped") or 0)
    stats = question_stats()
    scope = "SELECT id FROM teacher_batches WHERE owner_id IS NULL OR owner_id=?"
    db_counts = {
        "students": db.q1(f"SELECT COUNT(DISTINCT a.student_id) n FROM teacher_answers a WHERE a.import_batch_id IN ({scope})", (own,))["n"],
        "classes": db.q1(f"SELECT COUNT(DISTINCT s.class_id) n FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id WHERE a.import_batch_id IN ({scope})", (own,))["n"],
        **stats,
        "exams": db.q1(f"SELECT COUNT(DISTINCT a.exam_node_id) n FROM teacher_answers a WHERE a.import_batch_id IN ({scope})", (own,))["n"],
        "answers": db.q1(f"SELECT COUNT(*) n FROM teacher_answers WHERE import_batch_id IN ({scope})", (own,))["n"],
    }
    classes = db.q(f"""
        SELECT c.code, c.id,
          (SELECT COUNT(DISTINCT a.student_id) FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id WHERE s.class_id=c.id AND a.import_batch_id IN ({scope})) students,
          (SELECT COUNT(*) FROM teacher_answers a JOIN teacher_students s2 ON s2.id=a.student_id WHERE s2.class_id=c.id AND a.import_batch_id IN ({scope})) answers
        FROM teacher_classes c ORDER BY c.code
    """, (own, own))
    status = {row["score_status"]: row["n"] for row in db.q(f"SELECT score_status, COUNT(*) n FROM teacher_answers WHERE import_batch_id IN ({scope}) GROUP BY score_status", (own,))}
    return {"batch": {"id": batch.get("id"), "filename": batch.get("filename"), "created_at": batch.get("created_at"), "counts": counts, "errors": json.loads(batch.get("errors_json") or "[]")}, "database": db_counts, "classes": classes, "status": status}

def _selected_answers(exam_node_id: str = "", class_id: str = "", student_id: str = "", batch_id: str = "") -> List[Dict[str, Any]]:
    sql = f"SELECT a.*, s.code student_code, s.class_id, c.code class_code, {QUESTION_COLUMNS} FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id LEFT JOIN teacher_classes c ON c.id=s.class_id {QUESTION_JOIN} WHERE 1=1"; args: List[Any] = []
    if batch_id:
        sql += " AND a.import_batch_id=?"; args.append(batch_id)
    for column, value in (("a.exam_node_id", exam_node_id), ("s.class_id", class_id), ("a.student_id", student_id)):
        if value:
            sql += f" AND {column}=?"; args.append(value)
    rows = db.q(sql + " ORDER BY a.student_id, a.event_id, a.revision DESC, a.submit_time DESC", tuple(args))
    latest: Dict[tuple, Dict[str, Any]] = {}
    for row in rows:
        key = (row["student_id"], row["event_id"], row["question_id"])
        if key not in latest:
            latest[key] = row
    result = []
    for row in latest.values():
        grade = effective_grade(row["id"])
        if grade and grade.get("status") != "failed":
            row["score"] = float(grade.get("score") or 0)
            row["grade_status"] = grade.get("status")
            row["grading_version"] = grade.get("grading_version")
        else:
            row["score"] = None
            row["grade_status"] = grade.get("status") if grade else "pending"
            row["grading_version"] = grade.get("grading_version") if grade else None
        result.append(row)
    return result

def _personal_rows(exam_node_id: str = "", class_id: str = "", student_id: str = "", batch_id: str = "") -> List[Dict[str, Any]]:
    groups: Dict[tuple, Dict[str, Any]] = {}
    for row in _selected_answers(exam_node_id, class_id, student_id, batch_id):
        key = (row["student_id"], row.get("exam_node_id") or "")
        group = groups.setdefault(key, {"student_id": row["student_id"], "student_code": row["student_code"], "class_id": row.get("class_id"), "class_code": row.get("class_code"), "exam_node_id": row.get("exam_node_id"), "total_questions": 0, "graded_questions": 0, "score": 0.0, "graded_max_score": 0.0, "total_max_score": 0.0})
        group["total_questions"] += 1
        group["total_max_score"] += float(row.get("question_max_score") or row.get("max_score") or 0)
        if row["score"] is not None:
            group["graded_questions"] += 1
            group["score"] += row["score"]
            group["graded_max_score"] += float(row.get("question_max_score") or row.get("max_score") or 0)
    for group in groups.values():
        group["score_rate"] = round(group["score"] / group["graded_max_score"] * 100, 2) if group["graded_max_score"] else None
        group["complete"] = group["graded_questions"] == group["total_questions"]
    return sorted(groups.values(), key=lambda item: (item["class_code"] or "", item["student_code"] or "", item["exam_node_id"] or ""))

@router.get("/scores/personal")
def personal_scores(exam_node_id: str = "", class_id: str = "", student_id: str = "", batch_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); require_member(user)
    if student(user):
        mine = student_for(user)
        if not mine:
            raise HTTPException(403, "当前账号未绑定学生档案")
        student_id = mine["id"]
    elif not batch_id:
        row = latest_owned_batch(user)
        batch_id = row["id"] if row else ""
        if not batch_id:
            return {"items": []}
    return {"items": _personal_rows(exam_node_id, class_id, student_id, str(batch_id or ''))}

@router.get("/scores/classes")
def class_scores(exam_node_id: str = "", class_id: str = "", batch_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    if not batch_id:
        row = latest_owned_batch(user)
        batch_id = row["id"] if row else ""
        if not batch_id:
            return {"items": []}
    personal = _personal_rows(exam_node_id, class_id, "", str(batch_id or ''))
    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for row in personal:
        groups.setdefault((row.get("class_id"), row.get("class_code"), row.get("exam_node_id")), []).append(row)
    items = []
    for (cid, code, exam), rows in groups.items():
        completed = [row for row in rows if row["complete"] and row["score_rate"] is not None]
        scores = [row["score"] for row in completed]
        rates = [row["score_rate"] for row in completed]
        items.append({"class_id": cid, "class_code": code, "exam_node_id": exam, "participants": len(rows), "completed_students": len(completed), "average_score": round(sum(scores) / len(scores), 2) if scores else None, "average_rate": round(sum(rates) / len(rates), 2) if rates else None, "highest": round(max(scores), 2) if scores else None, "lowest": round(min(scores), 2) if scores else None})
    return {"items": sorted(items, key=lambda item: (item["class_code"] or "", item["exam_node_id"] or ""))}

@router.get("/results")
def results(course_id: str = "", standard_id: str = "", class_id: str = "", student_id: str = "", exam_node_id: str = "", question_id: str = "", status: str = "", review: str = "", limit: int = 500, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); require_member(user); sql = "SELECT g.*, a.original_answer, a.score_status, s.code student_code, s.class_id, c.code class_code, COALESCE(v.stem,q.stem) AS stem, COALESCE(v.question_type,q.question_type) AS question_type, CASE WHEN v.version_id IS NOT NULL THEN 'version' ELSE 'global_fallback' END AS question_source FROM teacher_grade_runs g JOIN teacher_answers a ON a.id=g.answer_id JOIN teacher_students s ON s.id=g.student_id JOIN teacher_batches b ON b.id=a.import_batch_id LEFT JOIN teacher_classes c ON c.id=s.class_id LEFT JOIN teacher_question_versions v ON v.version_id=COALESCE(NULLIF(g.question_version_id,''),a.question_version_id) LEFT JOIN teacher_questions q ON q.id=g.question_id WHERE g.id = (SELECT g2.id FROM teacher_grade_runs g2 WHERE g2.answer_id=g.answer_id ORDER BY g2.grading_version DESC, g2.created_at DESC LIMIT 1)"; args: List[Any] = []
    if teacher(user):
        sql += " AND (b.owner_id IS NULL OR b.owner_id=?)"; args.append(str(user.get("id") or ""))
    else:
        student_row = student_for(user)
        if not student_row:
            raise HTTPException(403, "当前账号未绑定学生档案")
        sql += " AND g.student_id=?"; args.append(student_row["id"])
    for column, value in (("g.course_id", course_id), ("g.standard_id", standard_id), ("s.class_id", class_id), ("g.student_id", student_id), ("g.question_id", question_id), ("a.exam_node_id", exam_node_id), ("g.status", status)):
        if value:
            sql += f" AND {column}=?"; args.append(value)
    if review == "1":
        sql += " AND g.needs_review=1"
    sql += " ORDER BY g.created_at DESC LIMIT ?"; args.append(max(1, min(limit, 1000)))
    return {"items": db.q(sql, tuple(args))}


@router.get("/results/{grade_id}")
def result_detail(grade_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); grade = db.q1("SELECT * FROM teacher_grade_runs WHERE id=?", (grade_id,))
    if not grade:
        raise HTTPException(404, "判题结果不存在")
    answer = db.q1("SELECT * FROM teacher_answers WHERE id=?", (grade["answer_id"],)) or {}
    if not visible_answer(answer, user):
        raise HTTPException(403, "不能查看其他学生")
    question = db.q1("SELECT * FROM teacher_question_versions WHERE version_id=?", (grade.get("question_version_id") or answer.get("question_version_id"),)) or db.q1("SELECT * FROM teacher_questions WHERE id=?", (grade["question_id"],)) or {}
    standard = db.q1("SELECT * FROM teacher_standards WHERE id=?", (grade.get("standard_id"),)) or {}
    revisions = db.q(
        "SELECT * FROM teacher_answers WHERE event_id=? AND student_id=? AND COALESCE(import_batch_id,'')=COALESCE(?,'') ORDER BY revision",
        (answer.get("event_id"), answer.get("student_id"), answer.get("import_batch_id")),
    )
    return {"grade": grade, "answer": answer, "question": question, "question_source": "version" if grade.get("question_version_id") or answer.get("question_version_id") else "global_fallback", "standard": standard, "revisions": revisions, "history": db.q("SELECT * FROM teacher_grade_runs WHERE answer_id=? ORDER BY grading_version DESC", (grade["answer_id"],)), "corrections": db.q("SELECT * FROM teacher_corrections WHERE grade_run_id=? ORDER BY created_at DESC", (grade_id,))}


@router.get("/imports")
def imports(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); require_teacher(user)
    own = str(user.get("id") or "")
    return {"items": db.q("SELECT * FROM teacher_batches WHERE owner_id IS NULL OR owner_id=? ORDER BY created_at DESC LIMIT 50", (own,))}


@router.get("/imports/{batch_id}")
def import_detail(batch_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); require_teacher(user)
    row = db.q1("SELECT * FROM teacher_batches WHERE id=?", (batch_id,))
    if not row:
        raise HTTPException(404, "导入任务不存在")
    return {"batch": row, "files": db.q("SELECT * FROM teacher_import_files WHERE batch_id=?", (batch_id,))}


@router.post("/imports/preview")
def preview(files: Optional[List[UploadFile]] = File(default=None), user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); require_teacher(user)
    tables, meta = read_csvs(files)
    errors = validate(tables, meta)
    return {"files": [{"filename": name, **meta[name], "sample": rows[:3]} for name, rows in tables.items()], "errors": errors[:100], "error_count": len(errors)}


@router.post("/imports")
def upload_import(files: Optional[List[UploadFile]] = File(default=None), user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user)
    tables, meta = read_csvs(files)
    errors = validate(tables, meta)
    blocking = [e for e in errors if e.get("type") in {"missing_file", "missing_fields", "duplicate_event_revision"}]
    if blocking:
        raise HTTPException(422, {"code": "IMPORT_VALIDATION_FAILED", "errors": blocking[:50]})
    return import_rows(tables, ", ".join(meta), str(user.get("id") or ""))


@router.get("/grades/{grade_id}/scoring-points")
def get_scoring_points(grade_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    grade = db.q1("SELECT * FROM teacher_grade_runs WHERE id=?", (grade_id,))
    if not grade:
        raise HTTPException(404, "判题结果不存在")
    try:
        steps = json.loads(grade.get("step_results_json") or "[]")
    except Exception:
        steps = []
    originals = {}
    for step in steps:
        for link in (step.get("scoring_point_links") or []):
            if isinstance(link, dict):
                sp = link.get("scoring_point_id")
                if sp and sp not in originals:
                    awarded = link.get("awarded_score"); awarded = link.get("score") if awarded is None else awarded
                    originals[sp] = {"scoring_point_id": sp, "original_ai_score": awarded, "max_score": link.get("max_score")}
    confirmations = {row["scoring_point_id"]: row for row in db.q("SELECT * FROM teacher_scoring_point_confirmations WHERE grade_run_id=?", (grade_id,))}
    return {"items": [dict(value, confirmation=confirmations.get(key)) for key, value in originals.items()]}

@router.post("/grades/{grade_id}/scoring-points")
def confirm_scoring_points(grade_id: str, user: Dict[str, Any] = Depends(current_user), payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    require_teacher(user); init()
    grade = db.q1("SELECT * FROM teacher_grade_runs WHERE id=?", (grade_id,))
    if not grade:
        raise HTTPException(404, "判题结果不存在")
    items = payload.get("items") or []
    if not isinstance(items, list) or not items:
        raise HTTPException(422, "至少提交一个评分点")
    try:
        steps = json.loads(grade.get("step_results_json") or "[]")
    except Exception:
        steps = []
    originals = {}
    for step in steps:
        for link in (step.get("scoring_point_links") or []):
            if isinstance(link, dict) and link.get("scoring_point_id") not in originals:
                awarded = link.get("awarded_score"); awarded = link.get("score") if awarded is None else awarded
                originals[link["scoring_point_id"]] = {"score": float(awarded or 0), "max": float(link.get("max_score") or 0)}
    total = 0.0
    for item in items:
        sp = str(item.get("scoring_point_id") or "")
        if sp not in originals:
            raise HTTPException(422, f"评分点 {sp} 不属于当前题目判题结果")
        score = float(item.get("score"))
        if score < 0 or score > originals[sp]["max"]:
            raise HTTPException(422, f"评分点 {sp} 分数超出范围")
        total += score
        before = db.q1("SELECT * FROM teacher_scoring_point_confirmations WHERE grade_run_id=? AND scoring_point_id=?", (grade_id, sp))
        version = int((before or {}).get("version") or 0) + 1
        confirmation_id = (before or {}).get("id") or db.new_id("spconfirm")
        db.x("INSERT INTO teacher_scoring_point_confirmations (id,grade_run_id,answer_id,scoring_point_id,original_ai_score,confirmed_score,max_score,confirmation_status,teacher_id,note,scoring_rule_version,version,source,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(grade_run_id,scoring_point_id) DO UPDATE SET original_ai_score=excluded.original_ai_score,confirmed_score=excluded.confirmed_score,max_score=excluded.max_score,confirmation_status='confirmed',teacher_id=excluded.teacher_id,note=excluded.note,version=excluded.version,source=excluded.source,updated_at=excluded.updated_at", (confirmation_id, grade_id, grade["answer_id"], sp, originals[sp]["score"], score, originals[sp]["max"], "confirmed", user["id"], str(item.get("note") or ""), "scoring_points", version, "teacher", now(), now()))
        db.x("INSERT INTO teacher_scoring_point_confirmation_history (id,confirmation_id,grade_run_id,answer_id,scoring_point_id,before_json,after_json,version,operator_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (db.new_id("spchist"), confirmation_id, grade_id, grade["answer_id"], sp, json.dumps(before, ensure_ascii=False) if before else "{}", json.dumps({"confirmed_score": score, "max_score": originals[sp]["max"], "original_ai_score": originals[sp]["score"]}, ensure_ascii=False), version, user["id"], now()))
    if abs(total - float(grade.get("score") or 0)) > 0.05:
        db.x("UPDATE teacher_grade_runs SET needs_review=1, status='needs_review' WHERE id=?", (grade_id,))
    else:
        db.x("UPDATE teacher_grade_runs SET needs_review=0, status='manual_confirmed' WHERE id=?", (grade_id,))
    return {"grade_id": grade_id, "confirmed_total": total, "matches_whole_score": abs(total - float(grade.get("score") or 0)) <= 0.05, "items": db.q("SELECT * FROM teacher_scoring_point_confirmations WHERE grade_run_id=?", (grade_id,))}

@router.post("/grades/{grade_id}/confirm")
def confirm(grade_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    before = db.q1("SELECT * FROM teacher_grade_runs WHERE id=?", (grade_id,))
    if not before:
        raise HTTPException(404, "判题结果不存在")
    after = dict(before); after["status"] = "manual_confirmed"; after["needs_review"] = 0
    db.x("UPDATE teacher_grade_runs SET status='manual_confirmed', needs_review=0 WHERE id=?", (grade_id,))
    db.x("INSERT INTO teacher_corrections (id,grade_run_id,before_json,after_json,note,teacher_id,created_at) VALUES (?,?,?,?,?,?,?)", (db.new_id("correction"), grade_id, json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), "教师确认 AI 判题结果", user["id"], now()))
    return {"grade": db.q1("SELECT * FROM teacher_grade_runs WHERE id=?", (grade_id,))}

@router.post("/grades/{grade_id}/manual")
def manual(grade_id: str, user: Dict[str, Any] = Depends(current_user), payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    require_teacher(user); init()
    before = db.q1("SELECT * FROM teacher_grade_runs WHERE id=?", (grade_id,))
    if not before:
        raise HTTPException(404, "判题结果不存在")
    note = str(payload.get("note") or "").strip()
    if not note:
        raise HTTPException(422, "人工修正必须填写修改原因")
    score = float(payload.get("score") if payload.get("score") is not None else 0)
    if score < 0 or score > float(before.get("max_score") or 0):
        raise HTTPException(422, "修正分数必须在 0 到题目满分之间")
    after = dict(before)
    after["score"] = score
    after["feedback"] = str(payload.get("feedback") or "")
    after["status"] = "manual_corrected"
    after["needs_review"] = 0
    db.x("UPDATE teacher_grade_runs SET score=?, feedback=?, status='manual_corrected', needs_review=0 WHERE id=?", (score, after["feedback"], grade_id))
    db.x("INSERT INTO teacher_corrections (id,grade_run_id,before_json,after_json,note,teacher_id,created_at) VALUES (?,?,?,?,?,?,?)", (db.new_id("correction"), grade_id, json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), note, user["id"], now()))
    return {"grade": db.q1("SELECT * FROM teacher_grade_runs WHERE id=?", (grade_id,)), "correction": db.q1("SELECT * FROM teacher_corrections WHERE grade_run_id=? ORDER BY created_at DESC LIMIT 1", (grade_id,))}
