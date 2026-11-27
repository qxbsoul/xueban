"""教师端：课程/课程标准、赛题导入、原始作答、统一判题与结果。"""
from __future__ import annotations

import csv
import hashlib
import http.client
import io
import json
import logging
import math
import os
import random
import re
import secrets
import socket
import ssl
import threading
import time
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError

from fastapi import APIRouter, BackgroundTasks, Body, Depends, File, HTTPException, UploadFile

from ..services import xueban_store as db
from ..services import learning_analysis as analysis
from ..services import analysis_pipeline
from .xueban_auth import current_user

router = APIRouter(prefix="/api/teacher", tags=["teacher"])
logger = logging.getLogger(__name__)
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
CREATE TABLE IF NOT EXISTS schema_migration_failures (
 id INTEGER PRIMARY KEY AUTOINCREMENT, version INTEGER, error TEXT, failed_at TEXT
);
"""


_SCHEMA_READY = False
SCHEMA_VERSION = 8
MIGRATION_VERSIONS = (1, 2, 3, 4, 5, 6, 7, 8)
_MIGRATION_STATE: Dict[str, Any] = {"status": "unknown", "error": "", "failed_version": None}


def migration_state() -> Dict[str, Any]:
    """当前进程的迁移状态；unknown 表示尚未执行 bootstrap（例如单元测试直连）。"""
    return dict(_MIGRATION_STATE)


def _set_migration_state(status: str, error: str = "", failed_version: Optional[int] = None) -> None:
    _MIGRATION_STATE.update({"status": status, "error": error[:500], "failed_version": failed_version})


def migration_status() -> Dict[str, Any]:
    """可查询的迁移状态：已完成版本、失败版本/原因（已脱敏）。只读，不触发迁移。"""
    try:
        completed = [row["version"] for row in db.q("SELECT version FROM schema_migrations ORDER BY version")]
    except Exception:
        completed = []
    try:
        failures = db.q("SELECT version, error, failed_at FROM schema_migration_failures ORDER BY id DESC LIMIT 10")
    except Exception:
        failures = []
    state = migration_state()
    latest = max(completed) if completed else 0
    if state["status"] == "unknown":
        status = "ready" if set(MIGRATION_VERSIONS).issubset(set(completed)) else "pending"
    else:
        status = state["status"]
    return {
        "status": status,
        "current_version": latest,
        "target_version": MIGRATION_VERSIONS[-1],
        "completed": completed,
        "failed_version": state.get("failed_version"),
        "error": sanitize_migration_error(state.get("error") or ""),
        "recent_failures": [{"version": row["version"], "error": sanitize_migration_error(row.get("error") or ""), "failed_at": row.get("failed_at")} for row in failures],
    }


def sanitize_migration_error(message: str) -> str:
    """迁移错误可观测，但不能把密钥/密码写进日志或状态接口。"""
    text = str(message or "").replace("\n", " ")[:500]
    text = re.sub(r"(sk-[A-Za-z0-9_-]{6,})", "sk-***", text)
    text = re.sub(r"(?i)(password|token|secret|api[_-]?key)\s*[=:]\s*\S+", r"\1=***", text)
    return text


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
            "teacher_grade_runs": [("question_version_id", "TEXT"), ("grader_version", "TEXT"), ("course_id", "TEXT"), ("standard_id", "TEXT"), ("standard_version", "TEXT"), ("scoring_rule_version", "TEXT"), ("mapping_version", "TEXT"), ("textbook_course_id", "TEXT"), ("textbook_unit_id", "TEXT"), ("textbook_context", "TEXT"), ("step_analysis_status", "TEXT"), ("structure_json", "TEXT"), ("request_context_json", "TEXT"), ("needs_review", "INTEGER DEFAULT 0")],
            "teacher_grade_jobs": [("completed", "INTEGER DEFAULT 0"), ("needs_review", "INTEGER DEFAULT 0"), ("current_answer_id", "TEXT"), ("finished_at", "TEXT"), ("batch_id", "TEXT"), ("textbook_course_id", "TEXT"), ("textbook_unit_id", "TEXT"), ("textbook_context", "TEXT"), ("provider", "TEXT"), ("model", "TEXT"), ("prompt_version", "TEXT"), ("pause_requested", "INTEGER DEFAULT 0"), ("worker_id", "TEXT"), ("started_at", "TEXT"), ("paused_at", "TEXT"), ("resumed_at", "TEXT"), ("last_checkpoint_at", "TEXT"), ("heartbeat_at", "TEXT"), ("retry_of", "TEXT"), ("cancellation_requested", "INTEGER DEFAULT 0"), ("retry_total", "INTEGER DEFAULT 0"), ("retry_success", "INTEGER DEFAULT 0"), ("retry_failed", "INTEGER DEFAULT 0"), ("retry_started_at", "TEXT DEFAULT ''")],
            "teacher_batches": [("preprocess_status", "TEXT DEFAULT 'review'"), ("preprocess_report_json", "TEXT"), ("owner_id", "TEXT")],
            "teacher_grade_attempts": [("batch_id", "TEXT"), ("student_id", "TEXT"), ("duration_ms", "INTEGER"), ("grading_version", "INTEGER")],
            "teacher_grade_job_items": [("attempt_count", "INTEGER DEFAULT 0"), ("current_attempt", "INTEGER DEFAULT 1"), ("started_at", "TEXT"), ("completed_at", "TEXT"), ("last_error", "TEXT"), ("grading_version", "INTEGER"), ("checkpoint_status", "TEXT"), ("updated_at", "TEXT"), ("retry_round", "INTEGER DEFAULT 0")],
            "teacher_scoring_point_confirmations": [("version", "INTEGER DEFAULT 1"), ("source", "TEXT")],
        }
        for table, columns in migrations.items():
            existing = {row[1] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}
            for column, ddl in columns:
                if column not in existing:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        c.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS schema_migration_failures (id INTEGER PRIMARY KEY AUTOINCREMENT, version INTEGER, error TEXT, failed_at TEXT)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_teacher_batches_owner ON teacher_batches(owner_id, created_at)")
        # 同一作答的判题版本必须唯一：既防止重复保存，也让「重判只新增版本」可审计。
        try:
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_teacher_grade_runs_answer_version ON teacher_grade_runs(answer_id, grading_version)")
        except Exception:
            logger.warning("teacher_grade_runs(answer_id,grading_version) 存在历史重复，唯一索引未创建", exc_info=True)
        c.commit()
        _SCHEMA_READY = True


def _stale_cutoff(seconds: Optional[int] = None) -> str:
    window = seconds if seconds is not None else int(os.environ.get("XUEBAN_JOB_STALE_SECONDS", "300") or 300)
    return (datetime.now() - timedelta(seconds=max(30, window))).isoformat(timespec="seconds")


def recover_stale_jobs() -> Dict[str, int]:
    """把心跳过期的 running/pausing 任务恢复为 paused，并归还 running item。

    服务重启、worker 线程异常退出、或进程被强杀后，任务会一直停在 running；
    这个函数是唯一的回收入口，必须幂等且可以随时被调用（启动、查看任务、继续任务前）。
    """
    cutoff = _stale_cutoff()
    jobs = db.q("SELECT id FROM teacher_grade_jobs WHERE status IN ('running','pausing') AND (heartbeat_at IS NULL OR heartbeat_at < ?)", (cutoff,))
    reclaimed = 0
    if not jobs:
        return {"jobs": 0, "items": 0}
    ids = [row["id"] for row in jobs]
    placeholders = ",".join("?" for _ in ids)
    with db.transaction():
        db.x(f"UPDATE teacher_grade_jobs SET status='paused', pause_requested=1, updated_at=? WHERE id IN ({placeholders})", tuple([now()] + ids))
        items = db.q(f"SELECT id FROM teacher_grade_job_items WHERE status='running' AND job_id IN ({placeholders})", tuple(ids))
        reclaimed = len(items)
        if items:
            item_ids = [row["id"] for row in items]
            marks = ",".join("?" for _ in item_ids)
            db.x(f"UPDATE teacher_grade_job_items SET status='pending', started_at=NULL, updated_at=? WHERE id IN ({marks})", tuple([now()] + item_ids))
    for job_id in ids:
        try:
            sync_job_counts(job_id)
        except Exception:
            logger.exception("回收后同步任务计数失败 job=%s", job_id)
    return {"jobs": len(jobs), "items": reclaimed}


def normalize_finished_jobs() -> int:
    """归一：判题结果为 failed 的 item 必须是 failed；结束的 paused 任务回归 completed/failed；计数回写。"""
    db.x("""UPDATE teacher_grade_job_items SET status='failed'
            WHERE status='completed' AND grade_id IN (SELECT id FROM teacher_grade_runs WHERE status='failed')""")
    changed = 0
    for row in db.q("SELECT id, status, total FROM teacher_grade_jobs"):
        counts = job_counts(row["id"], str(row["status"]))
        job_total = int(row.get("total") or 0)
        complete = counts["total"] > 0 and counts["total"] == job_total and counts["pending"] == 0 and counts["running"] == 0
        if complete and str(row["status"]) == "paused":
            status = "failed" if counts["success"] + counts["review"] == 0 and counts["failed"] > 0 else "completed"
            sync_job_counts(row["id"])
            db.x("UPDATE teacher_grade_jobs SET status=?, finished_at=COALESCE(NULLIF(finished_at,''),?), updated_at=? WHERE id=?", (status, now(), now(), row["id"]))
            changed += 1
        else:
            sync_job_counts(row["id"])
    return changed


def reclaim_stale_items(job_id: str) -> int:
    """回收同一任务里租约过期的 running item（worker 线程已死但没有归还）。

    与 recover_stale_jobs 的区别：这里只处理单个任务的 item 租约，不改变任务状态。
    """
    cutoff = _stale_cutoff(int(os.environ.get("XUEBAN_ITEM_LEASE_SECONDS", "900") or 900))
    rows = db.q("SELECT id FROM teacher_grade_job_items WHERE job_id=? AND status='running' AND (started_at IS NULL OR started_at < ?)", (job_id, cutoff))
    if not rows:
        return 0
    ids = [row["id"] for row in rows]
    marks = ",".join("?" for _ in ids)
    db.x(f"UPDATE teacher_grade_job_items SET status='pending', started_at=NULL, last_error=COALESCE(NULLIF(last_error,''),?), updated_at=? WHERE id IN ({marks})", tuple(["租约过期由系统回收"] + [now()] + ids))
    return len(ids)


def _migration_v1() -> None:
    migrate_answer_scope()
    db.x("UPDATE teacher_answers SET grading_status=COALESCE(NULLIF(grading_status,''),'pending')")
    db.x("UPDATE teacher_answers SET grading_status='graded' WHERE id IN (SELECT answer_id FROM teacher_grade_runs WHERE status NOT IN ('failed'))")
    db.x("UPDATE teacher_answers SET grading_status='needs_review' WHERE id IN (SELECT answer_id FROM teacher_grade_runs WHERE needs_review=1)")
    db.x("""UPDATE teacher_answers
        SET course_id = COALESCE(course_id, (SELECT q.course_id FROM teacher_questions q WHERE q.id=teacher_answers.question_id)),
            standard_id = COALESCE(standard_id, (SELECT q.standard_id FROM teacher_questions q WHERE q.id=teacher_answers.question_id))
        WHERE course_id IS NULL OR standard_id IS NULL""")


def _migration_v2() -> None:
    ensure_question_versions()


def _migration_v3() -> None:
    recover_stale_jobs()


def _migration_v4() -> None:
    """v1.5.11 角色收敛：游客不得再因 role 缺失/旧数据而拥有教师权限。"""
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


def _migration_v5() -> None:
    # 为 v1.5.10 及以前的批次补任务内学生快照；默认身份未验证，不推断同一人。
    db.x("""
        INSERT OR IGNORE INTO teacher_enrollments (id,batch_id,student_id,class_id,code,name,grade,identity_status,created_at,updated_at)
        SELECT 'enroll_' || hex(randomblob(8)), a.import_batch_id, s.id, s.class_id, s.code, s.name, s.grade, 'unverified', ?, ?
        FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id
        GROUP BY a.import_batch_id, s.id
    """, (now(), now()))



def _migration_v6() -> None:
    """v1.5.16：题目题单、分析任务表，并回填现有任务的班级级题单。"""
    analysis_pipeline.ensure_schema()
    batches = [row["id"] for row in db.q("SELECT id FROM teacher_batches")]
    for batch_id in batches:
        nodes = [row["exam_node_id"] for row in db.q("SELECT DISTINCT exam_node_id FROM teacher_answers WHERE import_batch_id=? AND COALESCE(exam_node_id,'')<>''", (batch_id,))]
        classes = [row["class_id"] for row in db.q("SELECT DISTINCT s.class_id FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id WHERE a.import_batch_id=? AND COALESCE(s.class_id,'')<>''", (batch_id,))]
        for node in nodes:
            exam = db.q1("SELECT available_questions FROM teacher_exams WHERE id=?", (node,)) or {}
            questions = analysis._split_question_ids(exam.get("available_questions"))
            for class_id in classes:
                for question_id in questions:
                    db.x("INSERT OR IGNORE INTO teacher_question_assignments (id,batch_id,exam_node_id,class_id,student_id,question_id,source,created_at) VALUES (?,?,?,?,?,?,?,?)",
                         (f"assign:{batch_id}:{node}:{class_id}:{question_id}", batch_id, node, class_id, "", question_id, "exam_available", now()))


def _migration_v8() -> None:
    """v1.5.19：任务重判进度字段、item 重判轮次、旧状态归一。"""
    c = db.conn()
    job_cols = {row[1] for row in c.execute("PRAGMA table_info(teacher_grade_jobs)")}
    for name, ddl in (("retry_total", "INTEGER DEFAULT 0"), ("retry_success", "INTEGER DEFAULT 0"), ("retry_failed", "INTEGER DEFAULT 0"), ("retry_started_at", "TEXT DEFAULT ''")):
        if name not in job_cols:
            c.execute(f"ALTER TABLE teacher_grade_jobs ADD COLUMN {name} {ddl}")
    item_cols = {row[1] for row in c.execute("PRAGMA table_info(teacher_grade_job_items)")}
    if "retry_round" not in item_cols:
        c.execute("ALTER TABLE teacher_grade_job_items ADD COLUMN retry_round INTEGER DEFAULT 0")
    c.commit()
    # 旧状态归一：部分失败统一为 completed（失败数量通过 counts 表达），整体失败保持 failed。
    db.x("UPDATE teacher_grade_jobs SET status='completed' WHERE status='partially_failed'")


def _migration_v7() -> None:
    """v1.5.17：分析任务对象化（证据版本/提示词/模型/superseded）与学情更新事件表。"""
    analysis_pipeline.ensure_schema()


def refresh_analysis(batch_id: str, requested_by: str = "", include_llm: bool = False) -> None:
    """判题/修正/重解析后刷新确定性分析；整批判题完成时再触发 LLM 语义分析。"""
    if not batch_id:
        return
    try:
        analysis_pipeline.auto_after_grading(batch_id, requested_by=requested_by or "system", include_llm=include_llm)
    except Exception:
        logger.exception("自动分析失败 batch=%s", batch_id)


def refresh_analysis_for_grade(grade_id: str, requested_by: str = "") -> None:
    row = db.q1("SELECT a.import_batch_id bid, g.answer_id FROM teacher_grade_runs g JOIN teacher_answers a ON a.id=g.answer_id WHERE g.id=?", (grade_id,))
    if not row or not row.get("bid"):
        return
    try:
        analysis_pipeline.record_grade_event(str(row["answer_id"]), "manual_correction")
    except Exception:
        logger.exception("记录人工修正事件失败 grade=%s", grade_id)
    refresh_analysis(str(row["bid"]), requested_by)


MIGRATIONS = {1: _migration_v1, 2: _migration_v2, 3: _migration_v3, 4: _migration_v4, 5: _migration_v5, 6: _migration_v6, 7: _migration_v7, 8: _migration_v8}


def _apply_migration(version: int, migrate_fn) -> bool:
    """单个版本在独立事务中执行；版本登记与数据修改同事务提交或回滚。"""
    with db.transaction():
        if db.q1("SELECT version FROM schema_migrations WHERE version=?", (version,)):
            return False
        migrate_fn()
        db.x("INSERT INTO schema_migrations (version, applied_at) VALUES (?,?)", (version, now()))
    return True


def bootstrap() -> None:
    """进程启动时的迁移：逐版本原子执行；失败记录并抛错，由启动钩子终止服务。"""
    init()
    _set_migration_state("running")
    current = 0
    try:
        for version in MIGRATION_VERSIONS:
            current = version
            _apply_migration(version, MIGRATIONS[version])
    except Exception as exc:
        message = sanitize_migration_error(str(exc))
        _set_migration_state("failed", message, current)
        try:
            db.x("INSERT INTO schema_migration_failures (version, error, failed_at) VALUES (?,?,?)", (current, message, now()))
        except Exception:
            logger.exception("记录迁移失败状态失败")
        logger.error("教师库迁移 v%s 失败，服务不会继续启动：%s", current, message)
        raise
    _set_migration_state("ready")


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
    with db.transaction():
        c.execute(sql)
        c.execute(f"INSERT INTO teacher_answers_new ({','.join(cols)}) SELECT {','.join(cols)} FROM teacher_answers")
        c.execute("DROP TABLE teacher_answers")
        c.execute("ALTER TABLE teacher_answers_new RENAME TO teacher_answers")


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
            db.x("INSERT OR REPLACE INTO teacher_questions (id,topic_id,standard_id,course_id,material_type,question_type,stem,options,reference_answer,reference_solution,max_score,scoring_points,indicator_id,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (qid, topic, standard_id, "", row.get("材料类型"), row.get("题型"), row.get("题干或内容"), row.get("选项"), row.get("参考答案"), row.get("参考解析"), _number_or_none(row.get("满分")), row.get("评分点"), row.get("指标编号"), row.get("来源或资料文件")))
            version_id = f"qv:{batch_id}:{qid}"
            db.x("INSERT OR REPLACE INTO teacher_question_versions (version_id,batch_id,question_id,topic_id,standard_id,course_id,material_type,question_type,stem,options,reference_answer,reference_solution,max_score,scoring_points,indicator_id,source,source_order,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (version_id, batch_id, qid, topic, standard_id, "", row.get("材料类型"), row.get("题型"), row.get("题干或内容"), row.get("选项"), row.get("参考答案"), row.get("参考解析"), _number_or_none(row.get("满分")), row.get("评分点"), row.get("指标编号"), row.get("来源或资料文件"), len(questions), now()))
            question_versions[qid] = version_id
            counts["questions"] += 1
        for row in exams:
            nid = str(row.get("节点编号", "")).strip()
            if nid:
                db.x("INSERT OR REPLACE INTO teacher_exams (id,exam_date,topics,description,available_questions,course_id) VALUES (?,?,?,?,?,?)", (nid, row.get("模拟测验日期"), row.get("已教及测量主题"), row.get("说明"), row.get("可用题目编号"), ""))
                counts["exams"] += 1
        for row in answers:
            event = str(row.get("作答事件编号", "")).strip()
            try:
                revision = int(str(row.get("修订版本") or "1").strip())
            except (TypeError, ValueError):
                errors.append({"type": "invalid_numeric", "row": row.get("记录编号"), "field": "修订版本", "value": row.get("修订版本"), "message": "修订版本不是整数，整行未导入（不伪造默认版本）"})
                continue
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
            db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,submit_time,original_answer,solution_process,original_score,max_score,score_status,raw_json,import_batch_id,exam_node_id,course_id,standard_id,question_version_id,grading_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"{batch_id}:{event}-r{revision}", student["id"], qid, event, revision, row.get("模拟提交时间"), row.get("学生最终作答"), row.get("解答过程"), _number_or_none(row.get("得分")), _number_or_none(row.get("满分")), score_status(row.get("评分状态") or ""), json.dumps(row, ensure_ascii=False), batch_id, row.get("节点编号"), "", "", question_versions.get(qid, f"qv:{batch_id}:{qid}"), "pending"))
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


@router.get("/migrations/status")
def migrations_status() -> Dict[str, Any]:
    """迁移状态（公开只读，不含教师数据/密钥）。"""
    return migration_status()


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
        # 完成/复核按「作答的当前状态」计数；按判题记录数会重复计算重判与历史版本（1623 vs 1612）。
        "graded": db.q1(f"SELECT COUNT(*) n FROM teacher_answers WHERE grading_status='graded' AND import_batch_id IN ({scope})", (own,))["n"],
        "failed": db.q1(f"SELECT COUNT(*) n FROM teacher_answers WHERE grading_status='failed' AND import_batch_id IN ({scope})", (own,))["n"],
        "review": db.q1(f"SELECT COUNT(*) n FROM teacher_answers WHERE grading_status='needs_review' AND import_batch_id IN ({scope})", (own,))["n"],
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


def build_source_steps(answer: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把学生原始解题过程切成可定位的 source_step。

    source_step 只是原始文本片段，不宣称是数学推理步骤；数学步骤由模型在
    reasoning_step/operation_unit 层拆分。偏移量用于把每条证据定位回原文。
    没有解题过程时，最终答案作为 final_answer 片段，供填空/选择等题型定位证据。
    """
    raw = str(answer.get("solution_process") or "")
    if not raw.strip():
        final = str(answer.get("original_answer") or "").strip()
        if final:
            return [{"source_step_id": "src-1", "source_order": 1, "original_text": final, "start_offset": 0, "end_offset": len(final), "source_type": "final_answer"}]
        return []
    steps: List[Dict[str, Any]] = []
    offset = 0
    for line in raw.splitlines(keepends=True):
        text = line.rstrip("\r\n")
        stripped = re.sub(r"^\s*\d+[.、)]\s*", "", text).strip()
        if stripped:
            leading = len(text) - len(text.lstrip())
            start = offset + leading
            steps.append({
                "source_step_id": f"src-{len(steps) + 1}",
                "source_order": len(steps) + 1,
                "original_text": text.strip(),
                "start_offset": start,
                "end_offset": start + len(text.strip()),
            })
        offset += len(line)
    if not steps:
        text = raw.strip()
        steps.append({"source_step_id": "src-1", "source_order": 1, "original_text": text, "start_offset": raw.find(text), "end_offset": raw.find(text) + len(text)})
    return steps


def parse_scoring_points(text: Any) -> Dict[str, float]:
    """解析题目版本的原始评分点分值；解析不可靠时返回空字典（调用方标记待确认）。"""
    if isinstance(text, dict):
        return {str(key): float(value) for key, value in text.items() if float(value or 0) > 0}
    if isinstance(text, list):
        result = {}
        for item in text:
            if isinstance(item, dict):
                code = str(item.get("code") or item.get("scoring_point_id") or "")
                value = item.get("max_score") or item.get("score") or 0
                if code and float(value or 0) > 0:
                    result[code] = float(value)
        return result
    raw = str(text or "")
    specs: Dict[str, float] = {}
    for match in re.finditer(r"([A-Z]\d{3}P\d+)\s*[^\n]*?(?<![0-9.\-])([0-9]+(?:\.[0-9]+)?)\s*分", raw):
        specs[match.group(1)] = float(match.group(2))
    return specs


def split_steps(answer: Dict[str, Any], question: Dict[str, Any]) -> List[str]:
    return [item["original_text"] for item in build_source_steps(answer)]


def normalize(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).replace("＝", "=").replace("，", ",").lower()


def llm_grade(answer: Dict[str, Any], question: Dict[str, Any], steps: List[str], key: str, provider: str, standard: Dict[str, Any], textbook_context: str = "") -> Dict[str, Any]:
    base = "https://api.deepseek.com" if provider == "deepseek" else "https://api.openai.com"
    model = "deepseek-chat" if provider == "deepseek" else "gpt-4o-mini"
    source_steps = build_source_steps(answer)
    payload = {"题目": question.get("stem"), "参考答案": question.get("reference_answer"), "参考解析": question.get("reference_solution"), "原始评分点": question.get("scoring_points"), "课程标准": standard.get("content_json") or "", "教师教材章节": textbook_context, "学生最终作答": answer.get("original_answer"), "学生原始解题过程": answer.get("solution_process"), "source_steps": source_steps, "要求": "只判题，不做学情分析。source_steps 是学生原始文本片段，只能作为证据定位，不等于数学步骤。优先按原始评分点；无标准步骤时允许模型拆解并标记 model_assisted_steps。逐步骤返回 reasoning_step_id,source_step_id,step_index,student_step,normalized_step,operation_type,reasoning_purpose,prerequisite_steps,expected_step,is_correct,score,max_score,error_type,judgment_reason,confidence,evidence_text,scoring_point_links,knowledge_links。每个步骤还必须返回 operation_units 数组：operation_id,source_step_id,operation_index,student_fragment,normalized_operation,operation_type,reasoning_purpose,prerequisite_operations,expected_operation,is_correct,score,max_score,error_type,judgment_reason,confidence,evidence_text,scoring_point_links,knowledge_links。同一行多个独立数学操作必须拆成多个 operation_units，不能合并。knowledge_links 必须标注 source_type=model_inferred 或 existing_mapping，不能声称教师确认。评分点只能引用原始评分点；分数必须闭合。汇总 score,max_score,is_correct,feedback,judgment_reason,needs_review。仅输出 JSON。"}
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], "temperature": 0}, ensure_ascii=False).encode()
    request = urllib.request.Request(base + "/chat/completions", data=body, headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"}, method="POST")
    with urllib.request.urlopen(request, timeout=45) as response:
        content = json.loads(response.read().decode())["choices"][0]["message"]["content"]
    match = re.search(r"\{.*\}", content, re.S)
    if not match:
        truncated = bool(content.strip()) and not content.rstrip().endswith("}")
        raise GradeFailure("model_output_truncated" if truncated else "model_json_invalid", "json_parse", "模型输出被截断（没有完整 JSON）" if truncated else "模型没有返回 JSON", content[:600])
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        truncated = not content.rstrip().endswith("}")
        raise GradeFailure("model_output_truncated" if truncated else "model_json_invalid", "json_parse", "模型输出被截断（JSON 不完整）" if truncated else "模型返回的 JSON 无法解析", f"{exc}: {content[:600]}") from exc


GRADING_PROMPT_VERSION = "teacher-grade-v4"
GRADER_VERSION = "v1.5.13"

# 测试可替换的模型调用钩子；生产默认真实 HTTP 调用，不会读取任何测试配置。
_LLM_CALLER = None


def call_grader(answer: Dict[str, Any], question: Dict[str, Any], steps: List[str], key: str, provider: str, standard: Dict[str, Any], textbook_context: str = "") -> Dict[str, Any]:
    caller = _LLM_CALLER or llm_grade
    return caller(answer, question, steps, key, provider, standard, textbook_context)


# ---------------------------------------------------------------- 错误分类
# 判题失败必须能被分类、计数、重试和解释。旧实现用字符串猜阶段
# （"JSON" in msg、"STEP" in msg），IncompleteRead 之类会被归到 model_response，
# 前端只能看到原始堆栈。这里按异常类型显式分类，并给出中文说明与可重试标记。
ERROR_TYPE_LABELS: Dict[str, str] = {
    # 网络类
    "network_incomplete_read": "模型响应中断（内容不完整）",
    "network_timeout": "模型响应超时",
    "network_disconnected": "模型服务断开连接",
    "network_ssl_eof": "TLS 连接中断",
    "network_reset": "连接被重置",
    "network_unreachable": "无法连接模型服务",
    "http_error": "模型服务返回错误状态",
    # 输出类
    "model_output_truncated": "模型输出被截断",
    "model_json_invalid": "模型没有返回合法 JSON",
    "model_incomplete_output": "模型输出缺少必填字段",
    "model_contract_violation": "模型返回结构不符合判题契约",
    # 评分点类
    "scoring_point_mapping_error": "评分点与题目映射不匹配",
    "scoring_point_max_mismatch": "评分点满分不匹配",
    "score_type_error": "分数类型或空值错误",
    # 数据/配置/持久化
    "answer_missing": "原始作答数据缺失",
    "question_config_error": "题目配置错误",
    "db_persist_error": "判题结果持久化失败",
    "model_error": "模型判题失败",
    "unknown_error": "未知错误",
}

ERROR_STAGE_LABELS: Dict[str, str] = {
    "answer_read": "读取作答",
    "question_config": "读取题目配置",
    "prompt_build": "构建提示词",
    "model_request": "请求模型",
    "model_response": "接收模型响应",
    "json_parse": "解析 JSON",
    "structure_validation": "结构校验",
    "scoring_point": "评分点校验",
    "persist": "持久化",
    "analysis_update": "学情更新",
    "reparse": "历史结果重解析",
    "": "未知阶段",
}


class GradeFailure(ValueError):
    """可分类、可留痕、可决定是否重试的判题失败。

    继承 ValueError 保持既有校验调用方（捕获 ValueError）兼容；分类器会先识别 GradeFailure。
    """

    def __init__(self, code: str, stage: str, message: str, detail: str = "", retryable: bool = False, http_status: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.message = message
        self.detail = detail or str(message)
        self.retryable = retryable
        self.http_status = http_status

    def __str__(self) -> str:
        if self.detail and self.detail != self.message:
            return f"{self.message}（{self.detail}）"
        return self.message


# 结构类错误允许「修复性重试」一次：网络错误由 call_grader_with_retry 负责，
# 结构错误只在这里再请求一次模型，避免同一份坏输出反复消耗额度。
STRUCTURE_RETRY_CODES = {
    "model_output_truncated",
    "model_json_invalid",
    "model_incomplete_output",
    "model_contract_violation",
    "score_type_error",
}


def infer_error_type_from_message(message: str, fallback: str = "model_error") -> str:
    """历史失败记录只有原始异常文本（旧版本统一记成 model_error），按文本还原分类。"""
    text = str(message or "").lower()
    if not text:
        return fallback
    if "ssl" in text or "unexpected_eof" in text:
        return "network_ssl_eof"
    if "incompleteread" in text:
        return "network_incomplete_read"
    if "timed out" in text or "timeout" in text:
        return "network_timeout"
    if "remote end closed" in text or "remotedisconnected" in text:
        return "network_disconnected"
    if "connection reset" in text or "connection aborted" in text:
        return "network_reset"
    if "float()" in text or "nonetype" in text:
        return "score_type_error"
    if "max_score" in text and ("exceeds" in text or "does not match" in text):
        return "scoring_point_max_mismatch"
    if "unknown scoring point" in text or "scoring point id" in text:
        return "scoring_point_mapping_error"
    if "outside 0.." in text or "allocation exceeds" in text or "sum does not match" in text:
        return "model_contract_violation"
    if "no json" in text or "jsondecode" in text:
        return "model_json_invalid"
    return fallback


def error_label(error_type: str) -> str:
    return ERROR_TYPE_LABELS.get(str(error_type or ""), "模型判题失败")


_ERROR_TYPES: List[Tuple[type, str, str, bool]] = [
    (http.client.IncompleteRead, "network_incomplete_read", "模型响应只收到了部分内容就中断（IncompleteRead）", True),
    (http.client.RemoteDisconnected, "network_disconnected", "模型服务提前断开连接（RemoteDisconnected）", True),
    (socket.timeout, "network_timeout", "等待模型响应超时（ReadTimeout）", True),
    (TimeoutError, "network_timeout", "等待模型响应超时", True),
    (ssl.SSLEOFError, "network_ssl_eof", "TLS 连接异常中断（SSL EOF）", True),
    (ssl.SSLError, "network_ssl_eof", "TLS 连接异常", True),
    (ConnectionResetError, "network_reset", "连接被重置", True),
    (ConnectionAbortedError, "network_reset", "连接被中止", True),
    (BrokenPipeError, "network_reset", "连接已断开", True),
    (URLError, "network_unreachable", "无法连接模型服务", True),
]


def classify_grader_error(exc: BaseException) -> Dict[str, Any]:
    """把判题异常映射成 (阶段, 类型, 中文说明, 可重试标记, http 状态)。"""
    if isinstance(exc, GradeFailure):
        return {"stage": exc.stage, "error_type": exc.code, "message": exc.message, "detail": exc.detail[:1000], "retryable": exc.retryable, "http_status": exc.http_status, "exception_class": type(exc).__name__}
    status = int(getattr(exc, "code", 0) or 0) if isinstance(exc, HTTPError) else 0
    if isinstance(exc, HTTPError):
        retryable = status >= 500 or status == 429
        return {"stage": "model_response", "error_type": "http_error", "message": f"模型服务返回 HTTP {status}", "detail": str(exc)[:500], "retryable": retryable, "http_status": status}
    for cls, error_type, message, retryable in _ERROR_TYPES:
        if isinstance(exc, cls):
            return {"stage": "model_response", "error_type": error_type, "message": message, "detail": str(exc)[:500], "retryable": retryable, "http_status": 0}
    text = str(exc)
    upper = text.upper()
    if isinstance(exc, json.JSONDecodeError) or "NO JSON" in upper or "JSON" in upper and "DECODE" in upper:
        return {"stage": "json_parse", "error_type": "model_json_invalid", "message": "模型没有返回合法 JSON", "detail": text[:500], "retryable": False, "http_status": 0, "exception_class": type(exc).__name__}
    if isinstance(exc, (TypeError, KeyError)):
        # TypeError/KeyError 基本都来自空值或缺失字段，必须定位到数据类型而不是当成 0 分。
        return {"stage": "structure_validation", "error_type": "score_type_error" if isinstance(exc, TypeError) else "model_incomplete_output", "message": "分数/字段类型错误：模型输出存在空值或缺失字段" if isinstance(exc, TypeError) else "模型输出缺少必填字段", "detail": text[:500], "retryable": False, "http_status": 0, "exception_class": type(exc).__name__}
    if isinstance(exc, ValueError):
        lower = text.lower()
        if "unknown scoring point" in lower or "invalid scoring point id" in lower or "cannot be parsed" in lower:
            return {"stage": "scoring_point", "error_type": "scoring_point_mapping_error", "message": "评分点与题目映射不匹配（引用了题目中不存在的评分点）", "detail": text[:500], "retryable": False, "http_status": 0, "exception_class": type(exc).__name__}
        if "max_score" in lower and ("exceeds" in lower or "does not match" in lower):
            return {"stage": "scoring_point", "error_type": "scoring_point_max_mismatch", "message": "评分点满分不匹配（模型声明满分与题目配置不一致）", "detail": text[:500], "retryable": False, "http_status": 0, "exception_class": type(exc).__name__}
        if "score is missing" in lower or "score is not a number" in lower:
            return {"stage": "structure_validation", "error_type": "score_type_error", "message": "分数字段为空或类型错误", "detail": text[:500], "retryable": False, "http_status": 0, "exception_class": type(exc).__name__}
        if "missing" in lower or "is not an object" in lower or "is not a list" in lower:
            return {"stage": "structure_validation", "error_type": "model_incomplete_output", "message": "模型返回结构缺少必填字段", "detail": text[:500], "retryable": False, "http_status": 0, "exception_class": type(exc).__name__}
        if "not a number" in lower or "not finite" in lower:
            return {"stage": "structure_validation", "error_type": "score_type_error", "message": "分数类型错误（不是有效数字）", "detail": text[:500], "retryable": False, "http_status": 0, "exception_class": type(exc).__name__}
        if "outside" in lower or "exceeds" in lower or "does not match" in lower or "allocation" in lower:
            return {"stage": "structure_validation", "error_type": "model_contract_violation", "message": "模型返回结构不满足判题契约（分值/评分点/闭合）", "detail": text[:500], "retryable": False, "http_status": 0, "exception_class": type(exc).__name__}
        return {"stage": "structure_validation", "error_type": "model_contract_violation", "message": "模型返回结构校验失败", "detail": text[:500], "retryable": False, "http_status": 0, "exception_class": type(exc).__name__}
    if isinstance(exc, HTTPException):
        raise exc
    return {"stage": "model_response", "error_type": "unknown_error", "message": "判题过程发生未知错误", "detail": text[:500], "retryable": False, "http_status": 0, "exception_class": type(exc).__name__}


def call_grader_with_retry(answer: Dict[str, Any], question: Dict[str, Any], steps: List[str], key: str, provider: str, standard: Dict[str, Any], textbook_context: str = "", on_attempt=None) -> Dict[str, Any]:
    """通信类错误按指数退避 + 随机抖动重试，次数有上限。

    只重试网络/服务端错误；结构校验失败重试同一个模型没有意义，直接进入人工复核。
    每次失败的尝试都会通过 on_attempt 落一条 attempt 记录，便于区分「一条作答失败」与
    「一条作答尝试了多次」。
    """
    attempts = max(1, min(5, int(os.environ.get("XUEBAN_GRADER_ATTEMPTS", "3") or 3)))
    base_ms = max(0, int(os.environ.get("XUEBAN_GRADER_RETRY_BASE_MS", "800") or 800))
    last_error: Optional[BaseException] = None
    for index in range(1, attempts + 1):
        started = time.time()
        try:
            return call_grader(answer, question, steps, key, provider, standard, textbook_context)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - 分类后决定是否重试
            info = classify_grader_error(exc)
            info["attempt"] = index
            info["attempts"] = attempts
            info["duration_ms"] = int((time.time() - started) * 1000)
            if on_attempt is not None:
                try:
                    on_attempt(info)
                except Exception:
                    logger.exception("记录判题尝试失败")
            last_error = exc
            if not info["retryable"] or index >= attempts:
                break
            delay = base_ms * (2 ** (index - 1)) / 1000.0
            time.sleep(delay + random.uniform(0, max(delay * 0.3, 0.01)))
    assert last_error is not None
    raise last_error


def _normalize_knowledge_links(item: Dict[str, Any], batch_id: str, aliases: Dict[str, str], issues: List[Dict[str, Any]], scope: str, confirmed_mappings: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    raw = item.get("knowledge_links") or item.get("knowledge_points") or []
    if isinstance(raw, str):
        raw = [raw]
    result = []
    version = analysis.current_mapping_version(batch_id) if batch_id else 0
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, str):
            entry = {"knowledge_point_id": entry}
        if not isinstance(entry, dict):
            continue
        raw_point_id = str(entry.get("knowledge_point_id") or entry.get("code") or "").strip()
        if not raw_point_id:
            continue
        canonical = aliases.get(raw_point_id) or aliases.get(raw_point_id[3:] if raw_point_id.startswith("kp:") else raw_point_id)
        if canonical is None and aliases:
            issues.append({"code": "unknown_knowledge_point", "severity": "review", "message": f"{scope} 引用了未知知识点 {raw_point_id}"})
            canonical = raw_point_id
        point_id = canonical or raw_point_id
        claimed_source = str(entry.get("source_type") or "model_inferred")
        claimed_status = str(entry.get("confirmation_status") or "pending")
        mapping = confirmed_mappings.get(point_id)
        source_type = claimed_source
        confirmation_status = claimed_status
        confirmed_by = None
        confirmed_at = None
        link_id = None
        if claimed_source == "model_inferred":
            source_type = "model_inferred"
            confirmation_status = "pending"
            if claimed_status not in ("pending", "", "unverified"):
                issues.append({"code": "invalid_confirmation_claim", "severity": "review", "message": f"{scope} 模型推断知识点 {point_id} 不得声称已确认，已强制为 pending"})
        elif claimed_source in ("teacher_confirmed", "existing_mapping") or claimed_status in ("confirmed", "teacher_confirmed"):
            if mapping:
                source_type = "existing_mapping"
                confirmation_status = "confirmed"
                confirmed_by = mapping.get("confirmed_by")
                confirmed_at = mapping.get("confirmed_at")
                link_id = mapping.get("id")
            else:
                source_type = "model_inferred"
                confirmation_status = "pending"
                issues.append({"code": "mapping_claim_not_found", "severity": "review", "message": f"{scope} 声称知识点 {point_id} 已有确认映射，但当前批次没有该知识点的教师确认记录"})
        else:
            source_type = "model_inferred"
            confirmation_status = "pending"
        result.append({
            "knowledge_point_id": point_id,
            "relation_type": str(entry.get("relation_type") or scope),
            "source_type": source_type,
            "confidence": entry.get("confidence"),
            "mapping_version": version,
            "confirmation_status": confirmation_status,
            "confirmed_by": confirmed_by,
            "confirmed_at": confirmed_at,
            "mapping_link_id": link_id,
        })
    return result


def _number(value: Any, scope: str, field: str, default: Optional[float] = None) -> float:
    """严格数值转换。

    判题契约里的 None/空串/非数字都意味着模型返回不完整，必须报出可定位的错误，
    不能像 `float(x or 0)` 那样把 None 静默当成 0 分（那会把「没判」变成「判了 0 分」）。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        if default is not None:
            return float(default)
        raise ValueError(f"{scope} {field} is missing")
    if isinstance(value, bool):
        raise ValueError(f"{scope} {field} is not a number: {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{scope} {field} is not a number: {value!r}")
    if not math.isfinite(number):
        raise ValueError(f"{scope} {field} is not finite: {value!r}")
    return number


def _validate_point_link(link: Any, point_specs: Dict[str, float], scope: str, issues: List[Dict[str, Any]], default_awarded: float = 0.0, default_max: float = 0.0, layer_sums: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """reasoning_step 与 operation_unit 共用同一评分点链接校验。

    真实模型会把一个 1 分评分点拆成 0.5+0.5 分落到多个操作单元上；旧实现要求每条链接的
    max_score 与原始评分点满分完全相等，于是把所有合法的部分得分判成契约错误（真实库 198/709）。
    这里改为：
    - 声明满分必须是原始满分的 **部分值**（0..原始满分），超过原始满分才是真错误；
    - 单条 awarded 不得超过其声明满分，也不得超过原始满分；
    - 返回的 `max_score` 统一取原始满分（证据分母必须是评分点满分，否则 0.5/0.5 会被算成 100%）；
    - 同时记录 `declared_max` 供审计，累计同一评分点声明满分不得重复计入整题满分。
    """
    if isinstance(link, str):
        link = {"scoring_point_id": link, "awarded_score": default_awarded, "max_score": default_max}
    if not isinstance(link, dict):
        raise ValueError(f"{scope} scoring point link is not an object")
    point = str(link.get("scoring_point_id") or "").strip()
    if not re.fullmatch(r"[A-Z]\d{3}P\d+", point):
        raise ValueError(f"{scope} invalid scoring point id {point}")
    if not point_specs:
        raise GradeFailure("question_config_error", "question_config", "题目没有可解析的评分点配置", f"{scope} scoring points cannot be parsed; cannot validate {point}")
    if point not in point_specs:
        raise GradeFailure("scoring_point_mapping_error", "scoring_point", f"评分点 {point} 不在题目评分点配置中", f"{scope} unknown scoring point {point}")
    original_max = float(point_specs[point])
    awarded_raw = link.get("awarded_score")
    if awarded_raw is None:
        awarded_raw = link.get("score")
    awarded = 0.0 if awarded_raw is None or (isinstance(awarded_raw, str) and not awarded_raw.strip()) else _number(awarded_raw, scope, "awarded_score")
    declared_raw = link.get("max_score")
    declared = original_max if declared_raw is None or (isinstance(declared_raw, str) and not declared_raw.strip()) else _number(declared_raw, scope, "max_score")
    if declared < 0:
        raise GradeFailure("scoring_point_max_mismatch", "scoring_point", f"评分点 {point} 声明满分为负数", f"{scope} scoring point {point} max_score cannot be negative")
    if declared > original_max + 0.05:
        # 模型常把 1 分评分点写成 2 分，但实际给分不超过原始满分。
        # 这属于「声明满分错误」，可以把声明值修回题目配置（不是降低校验标准）；
        # 若 awarded 本身超过原始满分，仍然判定为失败。
        if awarded <= original_max + 1e-9:
            issues.append({"code": "scoring_point_declared_max_clamped", "severity": "warning", "message": f"{scope} 评分点 {point} 声明满分 {declared} 超过原始 {original_max}，已按原始满分计入"})
            declared = original_max
        else:
            raise GradeFailure("scoring_point_max_mismatch", "scoring_point", f"评分点 {point} 声明满分 {declared} 且给分 {awarded} 超过原始满分 {original_max}", f"{scope} scoring point {point} max_score {declared} exceeds original {original_max}, awarded {awarded}")
    if awarded < 0 or awarded > declared + 1e-9 or awarded > original_max + 1e-9:
        raise GradeFailure("scoring_point_max_mismatch", "scoring_point", f"评分点 {point} 给分 {awarded} 超出 0..{declared}", f"{scope} scoring point {point} awarded {awarded} outside 0..{declared}")
    if layer_sums is not None:
        layer_sums[point] = layer_sums.get(point, 0.0) + declared
        if layer_sums[point] > original_max + 0.05:
            issues.append({"code": "scoring_point_declared_max_exceeds", "severity": "warning", "message": f"{scope} 对评分点 {point} 声明的满分合计 {round(layer_sums[point], 3)} 超过原始满分 {original_max}，可能存在重复计分"})
    return {"scoring_point_id": point, "awarded_score": round(awarded, 6), "max_score": original_max, "declared_max": round(declared, 6)}


def validate_llm_result(result: Dict[str, Any], max_score: float, question: Dict[str, Any], source_steps: Optional[List[Dict[str, Any]]] = None, batch_id: str = "") -> Dict[str, Any]:
    if not isinstance(result, dict):
        raise ValueError("model result is not an object")
    issues: List[Dict[str, Any]] = []
    derived_from_steps = False
    if result.get("score") is None:
        # 模型偶尔漏掉顶层 score，但步骤分数齐全：用步骤和做确定性修复（不伪造分数），并留痕。
        candidates = result.get("step_results") or result.get("reasoning_steps") or result.get("steps")
        if isinstance(candidates, list) and candidates and all(isinstance(item, dict) and item.get("score") is not None for item in candidates):
            try:
                result["score"] = round(sum(float(item["score"]) for item in candidates), 6)
                derived_from_steps = True
            except (TypeError, ValueError):
                pass
    score = _number(result.get("score"), "模型结果", "score")
    if not 0 <= score <= max_score:
        raise ValueError(f"score outside 0..max_score ({score} not in 0..{max_score})")
    if derived_from_steps:
        issues.append({"code": "score_derived_from_steps", "severity": "warning", "message": "模型未返回顶层 score，已按步骤分数求和修复"})
    if not result.get("step_results"):
        for alias in ("reasoning_steps", "steps"):
            if isinstance(result.get(alias), list) and result.get(alias):
                result["step_results"] = result[alias]
                result["step_results_alias"] = alias
                break
    steps = result.get("step_results")
    if steps is None:
        steps = []
    if not isinstance(steps, list):
        raise ValueError("step_results is not a list")
    if source_steps is None:
        # 兼容直接调用校验函数的旧路径；正式判题始终传入真实 source_steps。
        texts = [str(step.get("student_step") or "") for step in steps if isinstance(step, dict)]
        source_steps = [{"source_step_id": f"src-{index + 1}", "source_order": index + 1, "original_text": text, "start_offset": 0, "end_offset": len(text)} for index, text in enumerate(texts)]
        legacy_source = True
    else:
        legacy_source = False
    source_ids = {item["source_step_id"] for item in source_steps}
    point_specs = parse_scoring_points(question.get("scoring_points"))
    if point_specs and abs(sum(point_specs.values()) - max_score) > 0.05:
        issues.append({"code": "scoring_points_sum_mismatch", "severity": "review", "message": f"原始评分点合计 {sum(point_specs.values())} 与满分 {max_score} 不一致"})
    aliases: Dict[str, str] = {}
    for row in db.q("SELECT id, code FROM teacher_indicators"):
        canonical = str(row.get("code") or row.get("id") or "").strip()
        if not canonical:
            continue
        for variant in (row.get("id"), row.get("code"), f"kp:{row.get('id')}", f"kp:{row.get('code')}"):
            if variant:
                aliases[str(variant)] = canonical
    confirmed_mappings: Dict[str, Dict[str, Any]] = {}
    if batch_id:
        try:
            for row in db.q("SELECT * FROM scoring_point_knowledge_links WHERE batch_id=? AND status='confirmed' AND confirmed_by IS NOT NULL AND confirmed_by != ''", (batch_id,)):
                raw_key = str(row["knowledge_point_id"])
                key = aliases.get(raw_key) or aliases.get(raw_key[3:] if raw_key.startswith("kp:") else raw_key) or raw_key
                confirmed_mappings[key] = row
        except Exception:
            confirmed_mappings = {}
    point_awarded: Dict[str, float] = {}
    point_evidence: Dict[str, int] = {}
    step_declared: Dict[str, float] = {}
    op_declared: Dict[str, float] = {}
    seen = set()
    reasoning_ids = set()
    all_step_indices = {int(item.get("step_index")) for item in steps if isinstance(item, dict) and str(item.get("step_index") or "").isdigit()}
    total = 0.0
    for step in steps:
        if not isinstance(step, dict):
            raise GradeFailure("model_incomplete_output", "structure_validation", "步骤不是对象", "step is not an object")
        if step.get("step_index") is None or str(step.get("step_index")).strip() == "":
            raise GradeFailure("model_incomplete_output", "structure_validation", "步骤缺少 step_index", "step missing step_index")
        try:
            index = int(step.get("step_index"))
        except (TypeError, ValueError):
            raise GradeFailure("score_type_error", "structure_validation", f"step_index 不是整数：{step.get('step_index')!r}", "invalid step_index type")
        if index <= 0 or index in seen:
            raise ValueError("invalid step index")
        seen.add(index)
        reasoning_id = str(step.get("reasoning_step_id") or f"rs-{index}")
        if reasoning_id in reasoning_ids:
            raise ValueError(f"duplicate reasoning step id {reasoning_id}")
        reasoning_ids.add(reasoning_id)
        step["reasoning_step_id"] = reasoning_id
        step_score = _number(step.get("score"), f"推理步骤 {index}", "score")
        step_max = _number(step.get("max_score"), f"推理步骤 {index}", "max_score")
        if step_score < 0 or step_max < 0 or step_score > step_max:
            raise ValueError(f"推理步骤 {index} score outside 0..max_score")
        for key in ("student_step", "is_correct", "judgment_reason", "error_type"):
            if key not in step:
                raise ValueError(f"step missing {key}")
        source_step_id = step.get("source_step_id")
        if not source_step_id and len(source_steps) == 1 and source_steps[0].get("source_type") == "final_answer":
            source_step_id = source_steps[0]["source_step_id"]
        if legacy_source:
            step["source_step_id"] = source_step_id if source_step_id in source_ids else f"src-{index}"
        elif source_step_id not in source_ids:
            issues.append({"code": "reasoning_step_without_source", "severity": "review", "message": f"推理步骤 {index} 未关联有效 source_step（{source_step_id}）"})
            step["source_step_id"] = None
        else:
            step["source_step_id"] = source_step_id
        step.setdefault("normalized_step", "")
        step.setdefault("operation_type", "")
        step.setdefault("reasoning_purpose", "")
        step.setdefault("prerequisite_steps", [])
        step.setdefault("expected_step", "")
        step.setdefault("confidence", None)
        step.setdefault("evidence_text", step.get("student_step") or "")
        if not str(step.get("evidence_text") or "").strip():
            issues.append({"code": "step_without_evidence", "severity": "warning", "message": f"推理步骤 {index} 缺少证据文本（使用学生原文兜底）"})
        step["knowledge_links"] = _normalize_knowledge_links(step, batch_id, aliases, issues, "reasoning_step", confirmed_mappings)
        links = step.get("scoring_point_links")
        step_links = []
        if isinstance(links, list):
            for link in links:
                step_links.append(_validate_point_link(link, point_specs, "reasoning_step", issues, step_score, step_max, layer_sums=step_declared))
        step["scoring_point_links"] = step_links
        operations = step.get("operation_units")
        normalized_operations = []
        op_has_any_links = False
        missing_operations: List[str] = []
        if isinstance(operations, list) and operations:
            seen_ops = set()
            op_total = 0.0
            for position, operation in enumerate(operations, start=1):
                if not isinstance(operation, dict):
                    raise ValueError("operation unit is not an object")
                operation_id = str(operation.get("operation_id") or f"op-{index}-{position}")
                if operation_id in seen_ops:
                    raise ValueError("invalid operation id")
                seen_ops.add(operation_id)
                operation["operation_id"] = operation_id
                operation["reasoning_step_id"] = reasoning_id
                operation["source_step_id"] = operation.get("source_step_id") or step.get("source_step_id")
                if not legacy_source and operation.get("source_step_id") not in source_ids:
                    issues.append({"code": "operation_without_source", "severity": "review", "message": f"操作 {operation_id} 未关联有效 source_step"})
                    operation["source_step_id"] = None
                operation["operation_index"] = position
                operation.setdefault("student_fragment", step.get("student_step") or "")
                operation.setdefault("normalized_operation", operation.get("normalized_step") or "")
                operation.setdefault("expected_operation", "")
                operation.setdefault("operation_type", "")
                operation.setdefault("reasoning_purpose", "")
                operation.setdefault("prerequisite_operations", [])
                operation.setdefault("confidence", None)
                operation.setdefault("evidence_text", operation.get("student_fragment") or "")
                operation["knowledge_links"] = _normalize_knowledge_links(operation, batch_id, aliases, issues, "operation_unit", confirmed_mappings)
                op_score = _number(operation.get("score"), f"操作 {operation_id}", "score", default=0.0)
                op_max = _number(operation.get("max_score"), f"操作 {operation_id}", "max_score", default=0.0)
                if op_score < 0 or op_score > op_max:
                    raise ValueError(f"操作 {operation_id} score outside range")
                op_total += op_score
                raw_op_links = operation.get("scoring_point_links")
                op_links = []
                if isinstance(raw_op_links, list):
                    for link in raw_op_links:
                        op_links.append(_validate_point_link(link, point_specs, "operation_unit", issues, op_score, op_max, layer_sums=op_declared))
                operation["scoring_point_links"] = op_links
                if op_links:
                    op_has_any_links = True
                    for link in op_links:
                        point = link["scoring_point_id"]
                        point_awarded[point] = point_awarded.get(point, 0.0) + link["awarded_score"]
                        point_evidence[point] = point_evidence.get(point, 0) + 1
                else:
                    missing_operations.append(operation_id)
                normalized_operations.append(operation)
            if abs(op_total - step_score) > 0.05:
                issues.append({"code": "operation_sum_mismatch", "severity": "warning", "message": f"步骤 {index} 操作分合计 {round(op_total, 2)} 与步骤分 {step_score} 不一致（评分点闭合仍单独校验）"})
        if op_has_any_links:
            for operation_id in missing_operations:
                issues.append({"code": "operation_without_scoring_point", "severity": "warning", "message": f"操作 {operation_id} 未关联任何评分点（该操作得分不计入评分点证据）"})
            if step_links:
                step_sums: Dict[str, float] = {}
                for link in step_links:
                    step_sums[link["scoring_point_id"]] = step_sums.get(link["scoring_point_id"], 0.0) + link["awarded_score"]
                op_sums = {point: sum(link["awarded_score"] for operation in normalized_operations for link in operation.get("scoring_point_links") or [] if link["scoring_point_id"] == point) for point in set(step_sums) | {link["scoring_point_id"] for operation in normalized_operations for link in operation.get("scoring_point_links") or []}}
                if any(abs(step_sums.get(point, 0.0) - op_sums.get(point, 0.0)) > 0.05 for point in set(step_sums) | set(op_sums)):
                    issues.append({"code": "step_links_ignored", "severity": "review", "message": f"步骤 {index} 的步骤级与操作级评分点分配不一致；以操作级为唯一计分来源，步骤级仅校验不累计"})
        else:
            for link in step_links:
                point = link["scoring_point_id"]
                point_awarded[point] = point_awarded.get(point, 0.0) + link["awarded_score"]
                point_evidence[point] = point_evidence.get(point, 0) + 1
        step["operation_units"] = normalized_operations
        total += step_score
    result["source_steps"] = source_steps
    result["validation_issues"] = issues
    result["scoring_points"] = {point: {"awarded": round(point_awarded.get(point, 0.0), 4), "max": point_specs.get(point), "evidence_count": point_evidence.get(point, 0)} for point in sorted(set(point_specs) | set(point_awarded))}
    result["unaddressed_scoring_points"] = sorted(set(point_specs) - set(point_awarded))
    if result["unaddressed_scoring_points"]:
        # 未处置评分点本身不是错误：学生没做到的点得 0 分，而「总分与评分点合计是否闭合」
        # 才是判断模型有没有漏算的依据（见下方 scoring_points_do_not_close）。
        # 这里保留为可追溯的告警，不再自动把整题打成 needs_review。
        issues.append({"code": "unaddressed_scoring_points", "severity": "info", "message": "未处置评分点按 0 分计：" + ",".join(result["unaddressed_scoring_points"])})
    for point, awarded in point_awarded.items():
        limit = point_specs.get(point)
        if limit is not None and awarded > limit + 0.05:
            raise ValueError(f"scoring point {point} awarded exceeds its max")
    awarded_total = sum(point_awarded.values())
    if awarded_total > score + 0.05:
        raise GradeFailure("model_contract_violation", "structure_validation", "评分点得分合计超过总分", f"scoring point allocation {awarded_total} exceeds total score {score}")
    if point_specs and abs(awarded_total - score) > 0.05:
        issues.append({"code": "scoring_points_do_not_close", "severity": "review", "message": f"评分点得分合计 {round(awarded_total, 2)} 与总分 {score} 不一致"})
    if abs(sum(float(step.get("max_score") or 0) for step in steps) - float(question.get("max_score") or max_score)) > 0.05:
        issues.append({"code": "step_max_sum_mismatch", "severity": "warning", "message": "步骤满分合计与题目满分不一致（评分点闭合仍单独校验）"})
    steps = [step for step in steps if str(step.get("student_step") or "").strip() not in {"未作答", "未提交", "未体现"}]
    result["step_results"] = steps
    if steps and abs(sum(float(step.get("score") or 0) for step in steps) - score) > 0.05:
        raise GradeFailure("model_contract_violation", "structure_validation", "步骤得分合计与总分不一致", f"step score sum {sum(float(step.get('score') or 0) for step in steps)} != total {score}")
    qtype = str(question.get("question_type") or "")
    process_type = any(key in qtype for key in ("解答", "计算", "证明", "应用", "综合"))
    if process_type and not steps:
        issues.append({"code": "process_without_steps", "severity": "review", "message": "过程题未返回有效步骤"})
    if process_type and steps and any(not step.get("operation_units") for step in steps):
        issues.append({"code": "steps_without_operations", "severity": "review", "message": "存在未拆分操作单元的推理步骤"})
    result["score"] = score
    result["step_results"] = steps
    model_requested_review = bool(result.get("needs_review"))
    quality_flags = list(result.get("quality_flags") or [])
    if model_requested_review:
        quality_flags.append("model_requested_review")
    validator_review = any(item.get("severity") == "review" for item in issues)
    trust_model_review = str(os.environ.get("XUEBAN_GRADER_TRUST_MODEL_REVIEW", "0")).strip() in ("1", "true", "yes")
    # 模型自报 needs_review 只作为「建议」记录，不单独把整题打成待复核：
    # 真实历史数据里 709/709 全部自报复核（明显是提示词副作用），若照单全收，
    # 学情证据会永远为 0。真正决定是否需要人工复核的是验证器发现的 review 级问题
    # （分值不闭合、评分点未知/超分、证据无法定位等）。
    review = validator_review or (trust_model_review and model_requested_review)
    result["needs_review"] = review
    result["model_requested_review"] = model_requested_review
    result["quality_flags"] = sorted(set(quality_flags))
    result["validation"] = {"issue_count": len(issues), "review_required": review, "model_requested_review": model_requested_review, "trust_model_review": trust_model_review, "source_step_count": len(source_steps), "reasoning_step_count": len(steps), "operation_unit_count": sum(len(step.get("operation_units") or []) for step in steps), "scoring_point_count": len(point_specs), "scoring_point_awarded": round(awarded_total, 4)}
    result.setdefault("is_correct", "partial")
    result.setdefault("feedback", "")
    result.setdefault("judgment_reason", "")
    return result

def insert_grade(answer: Dict[str, Any], question: Dict[str, Any], standard: Dict[str, Any], version: int, model: str, score: float, max_score: float, correct: str, steps: List[Dict[str, Any]], error_type: str, feedback: str, reason: str, review: int, status: str, raw: str, textbook_course_id: str = "", textbook_unit_id: str = "", textbook_context: str = "", step_analysis_status: str = "", structure: Optional[Dict[str, Any]] = None, request_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    grade_id = db.new_id("grade")
    db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,grading_prompt_version,grader_version,course_id,standard_id,standard_version,scoring_rule_version,score,max_score,is_correct,step_results_json,error_type,feedback,judgment_reason,needs_review,status,raw_response,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (grade_id, answer["id"], answer["student_id"], answer["question_id"], answer["event_id"], answer["revision"], version, model, GRADING_PROMPT_VERSION, GRADER_VERSION, answer.get("course_id") or question.get("course_id"), answer.get("standard_id") or question.get("standard_id"), standard.get("version"), "scoring_points", score, max_score, correct, json.dumps(steps, ensure_ascii=False), error_type, feedback, reason, review, status, raw, now()))
    db.x("UPDATE teacher_grade_runs SET textbook_course_id=?, textbook_unit_id=?, textbook_context=?, step_analysis_status=?, question_version_id=?, mapping_version=?, structure_json=?, request_context_json=? WHERE id=?", (textbook_course_id, textbook_unit_id, textbook_context[:4000], step_analysis_status, answer.get("question_version_id") or "", str(analysis.current_mapping_version(answer.get("import_batch_id") or "")), json.dumps(structure or {}, ensure_ascii=False), json.dumps(request_context or {}, ensure_ascii=False), grade_id))
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

def record_attempt(job_id: str, answer: Dict[str, Any], grading_version: int, stage: str, error_type: str, message: str, result_id: str = "", http_status: int = 0, start: str = "", duration_ms: Optional[int] = None) -> None:
    retryable = 1 if stage in ("model_response", "json_parse", "structure_validation") else 0
    if error_type.startswith("network_") or error_type == "http_error":
        retryable = 1
    end = now()
    try:
        duration = int(duration_ms) if duration_ms is not None else max(0, int((datetime.fromisoformat(end) - datetime.fromisoformat(start or end)).total_seconds() * 1000))
    except Exception:
        duration = int(duration_ms or 0)
    try:
        attempt_number = int(db.q1("SELECT COUNT(*) n FROM teacher_grade_attempts WHERE answer_id=? AND COALESCE(task_id,'')=?", (answer.get("id"), job_id or ""))["n"]) + 1
    except Exception:
        attempt_number = 1
    db.x("INSERT INTO teacher_grade_attempts (id,task_id,answer_id,batch_id,student_id,student_code,class_code,question_id,event_id,revision,attempt_number,grading_version,model,prompt_version,start_time,end_time,failure_stage,error_type,http_status,error_message,retryable,result_id,duration_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (db.new_id("attempt"), job_id, answer.get("id"), answer.get("import_batch_id") or "", answer.get("student_id") or "", answer.get("student_code") or "", answer.get("class_code") or "", answer.get("question_id"), answer.get("event_id"), answer.get("revision"), attempt_number, grading_version, "deepseek-chat", GRADING_PROMPT_VERSION, start or end, end, stage, error_type, http_status, message[:1000], retryable, result_id, duration))


def grade_answer(answer_id: str, key: str = "", provider: str = "deepseek", textbook_course_id: str = "", textbook_unit_id: str = "", textbook_context: str = "", job_id: str = "", raw_override: Optional[Dict[str, Any]] = None, model_label: str = "", reparsed_from: str = "", attempt_stage: str = "") -> Dict[str, Any]:
    init(); attempt_start = now(); answer = db.q1("SELECT * FROM teacher_answers WHERE id=?", (answer_id,))
    if not answer:
        raise HTTPException(404, "作答不存在")
    if job_id:
        # 幂等：同一个 job 里已经成功保存过的作答不再重判（页面刷新/重复投递都走这里）。
        done = db.q1("SELECT grade_id FROM teacher_grade_job_items WHERE job_id=? AND answer_id=? AND status='completed' AND grade_id IS NOT NULL AND grade_id<>''", (job_id, answer_id))
        if done:
            existing = db.q1("SELECT * FROM teacher_grade_runs WHERE id=?", (done["grade_id"],))
            if existing:
                return {"grade": existing, "reused": True}
    context = teacher_meta(answer); question = context["question"]; standard = context["standard"]
    steps = split_steps(answer, question)
    max_score = float(question.get("max_score") or answer.get("max_score") or 0)

    version = (db.q1("SELECT COALESCE(MAX(grading_version),0) n FROM teacher_grade_runs WHERE answer_id=?", (answer_id,))["n"] or 0) + 1
    if not key:
        key = default_llm_key()
    if not key and raw_override is None:
        # 没有模型凭据时明确失败，不写入任何成绩/证据，也不做规则猜测。
        raise HTTPException(503, {"code": "GRADER_NOT_CONFIGURED", "message": "判题模型未配置：请在服务端配置 DEEPSEEK_API_KEY 后再判题"})
    model_name = model_label or ("deepseek-chat" if provider == "deepseek" else "gpt-4o-mini")
    recorded_attempts = {"n": 0}
    try:
        # 数据/配置类失败也在 try 内抛出，确保落 failed 记录与 attempt，而不是裸 500。
        if max_score <= 0:
            raise GradeFailure("question_config_error", "question_config", "题目满分缺失或为 0，无法判题", f"question max_score={max_score!r}")
        if raw_override is None and not str(answer.get("original_answer") or "").strip() and not str(answer.get("solution_process") or "").strip():
            raise GradeFailure("answer_missing", "answer_read", "学生作答与解题过程都为空，无法判题", "both original_answer and solution_process are empty")
        source_steps = build_source_steps(answer)

        def _record_failed_attempt(info: Dict[str, Any]) -> None:
            recorded_attempts["n"] += 1
            end_dt = datetime.now()
            start_dt = end_dt - timedelta(milliseconds=int(info.get("duration_ms") or 0))
            record_attempt(
                job_id, answer, version, str(info.get("stage") or "model_response"), str(info.get("error_type") or "unknown_error"),
                f'{info.get("message")}: {info.get("detail")}',
                http_status=int(info.get("http_status") or 0),
                start=start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                duration_ms=int(info.get("duration_ms") or 0),
            )

        # 结构类错误允许一次修复性重试：网络错误在 call_grader_with_retry 内按退避重试，
        # 结构错误（截断/缺字段/分值不闭合）在这里重新请求一次模型；仍失败才落 failed。
        llm = None
        for structure_attempt in range(2):
            raw = raw_override if raw_override is not None else call_grader_with_retry(answer, question, steps, key, provider, standard, textbook_context, on_attempt=_record_failed_attempt)
            try:
                llm = validate_llm_result(dict(raw), max_score, question, source_steps=source_steps, batch_id=answer.get("import_batch_id") or "")
                break
            except Exception as exc:  # noqa: BLE001 - 结构错误统一分类后决定是否修复性重试
                info = classify_grader_error(exc)
                if info["error_type"] not in STRUCTURE_RETRY_CODES or structure_attempt >= 1 or raw_override is not None:
                    raise
                record_attempt(job_id, answer, version, str(info["stage"]), str(info["error_type"]), f'{info["message"]}: {info.get("detail","")}', start=attempt_start)
                logger.warning("判题结构校验失败，进行修复性重试 answer=%s code=%s", answer_id, info["error_type"])
                continue
        if llm is None:
            raise GradeFailure("unknown_error", "structure_validation", "判题结果为空", "validation produced no result")
        context_warnings = []
        if context.get("question_source") == "global_fallback":
            context_warnings.append("global_question_fallback")
        if not str(standard.get("content_json") or "").strip():
            context_warnings.append("missing_standard")
        if not str(textbook_context or "").strip():
            context_warnings.append("missing_textbook_context")
        if "global_question_fallback" in context_warnings:
            llm.setdefault("validation_issues", []).append({"code": "global_question_fallback", "severity": "review", "message": "题目版本缺失，使用了全局题表回退"})
            llm["needs_review"] = True
        llm["context_warnings"] = context_warnings
        score = float(llm["score"])
        has_process = bool(str(answer.get("solution_process") or "").strip())
        has_steps = bool(llm.get("step_results"))
        analysis_status = "no_process" if not has_process else ("completed" if has_steps else "failed")
        if has_process and not has_steps:
            llm["needs_review"] = True
            llm["judgment_reason"] = (str(llm.get("judgment_reason") or "") + " 步骤分析未成功，待人工复核。").strip()
        llm["step_analysis_status"] = analysis_status
        structure = {
            "source_steps": llm.get("source_steps") or source_steps,
            "validation": llm.get("validation") or {},
            "validation_issues": llm.get("validation_issues") or [],
            "quality_flags": llm.get("quality_flags") or [],
            "model_requested_review": bool(llm.get("model_requested_review")),
            "context_warnings": context_warnings,
            "scoring_points": llm.get("scoring_points") or {},
            "unaddressed_scoring_points": llm.get("unaddressed_scoring_points") or [],
        }
        request_context = {
            "question_version_id": answer.get("question_version_id") or "",
            "question_source": context.get("question_source") or "global_fallback",
            "standard_id": answer.get("standard_id") or question.get("standard_id") or "",
            "standard_version": standard.get("version") or "",
            "topic_id": question.get("topic_id") or "",
            "textbook_course_id": textbook_course_id,
            "textbook_unit_id": textbook_unit_id,
            "textbook_context_chars": len(textbook_context or ""),
            "grading_model": model_name,
            "prompt_version": GRADING_PROMPT_VERSION,
            "grader_version": GRADER_VERSION,
            "mapping_version": analysis.current_mapping_version(answer.get("import_batch_id") or ""),
            "source_step_count": len(source_steps),
            "context_warnings": context_warnings,
        }
        if reparsed_from:
            request_context["reparsed_from"] = reparsed_from
            request_context["reparse_note"] = "从历史 raw_response 重新解析生成新版本，未调用模型"
        try:
            grade = insert_grade(answer, question, standard, version, model_name, score, max_score, str(llm.get("is_correct") or "partial"), llm.get("step_results") or [], str(llm.get("error_type") or ""), str(llm.get("feedback") or ""), str(llm.get("judgment_reason") or ""), 1 if llm.get("needs_review") else 0, "needs_review" if llm.get("needs_review") else "graded", json.dumps(llm, ensure_ascii=False), textbook_course_id, textbook_unit_id, textbook_context, analysis_status, structure, request_context)
        except Exception as exc:  # noqa: BLE001 - 持久化失败必须独立分类，不能伪装成模型错误
            raise GradeFailure("db_persist_error", "persist", "判题结果写入数据库失败", f"{type(exc).__name__}: {exc}") from exc
        record_attempt(job_id, answer, version, attempt_stage, "", "从历史 raw_response 重解析生成新版本（未调用模型）" if attempt_stage == "reparse" else "", grade.get("id"), start=attempt_start)
        try:
            analysis_pipeline.record_grade_event(answer["id"], "reparse" if attempt_stage == "reparse" else "grade_changed")
        except Exception:
            logger.exception("记录学情更新事件失败 answer=%s", answer_id)
        return {"grade": grade}
    except HTTPException:
        raise
    except Exception as exc:
        info = classify_grader_error(exc)
        message = str(exc)
        # 模型调用内部已经逐次记录过 attempt 时，不再重复写一条完全相同的失败记录。
        if recorded_attempts.get("n", 0) == 0:
            record_attempt(job_id, answer, version, str(info["stage"]), str(info["error_type"]), f'{info["message"]}: {message}', http_status=int(info.get("http_status") or 0), start=attempt_start)
        structure = {"source_steps": build_source_steps(answer), "validation": {"issue_count": 1, "review_required": True}, "validation_issues": [{"code": "grading_failed", "severity": "blocking", "message": info["message"], "detail": message[:300]}]}
        request_context = {"question_version_id": answer.get("question_version_id") or "", "question_source": context.get("question_source") or "global_fallback", "prompt_version": GRADING_PROMPT_VERSION, "grader_version": GRADER_VERSION, "mapping_version": analysis.current_mapping_version(answer.get("import_batch_id") or ""), "error_type": info["error_type"], "error_stage": info["stage"], "error_stage_label": ERROR_STAGE_LABELS.get(str(info["stage"]), str(info["stage"])), "error_message": info["message"], "error_detail": message[:1000], "exception_class": info.get("exception_class") or type(exc).__name__, "retryable": bool(info["retryable"])}
        grade = insert_grade(answer, question, standard, version, "model-error", 0, max_score, "failed", [], str(info["error_type"]), str(info["message"]), str(info["message"]), 0, "failed", "", textbook_course_id, textbook_unit_id, textbook_context, "failed", structure, request_context)
        return {"grade": grade}


@router.post("/answers/{answer_id}/grade")
def grade(answer_id: str, user: Dict[str, Any] = Depends(current_user), payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    require_teacher(user)
    result = grade_answer(answer_id, str(payload.get("llm_key") or ""), str(payload.get("provider") or "deepseek"), str(payload.get("textbook_course_id") or ""), str(payload.get("textbook_unit_id") or ""), str(payload.get("textbook_context") or ""))
    grade = result.get("grade") or {}
    if grade.get("status") and grade.get("status") != "failed":
        # 单题重判成功后，把该作答在任务里的失败项恢复成 completed，避免“失败数”与实际结果脱节；
        # 失败历史仍保留在 attempts / 旧 grading_version 中，不做删除。
        stale_items = db.q("SELECT id, job_id FROM teacher_grade_job_items WHERE answer_id=? AND status='failed'", (answer_id,))
        touched_jobs = set()
        for item in stale_items:
            db.x("UPDATE teacher_grade_job_items SET status='completed', grade_id=?, last_error='', completed_at=?, updated_at=? WHERE id=?", (grade.get("id"), now(), now(), item["id"]))
            touched_jobs.add(item["job_id"])
        for job_id in touched_jobs:
            sync_job_counts(job_id)
    refresh_analysis_for_grade(str(grade.get("id") or ""), str(user.get("id") or ""))
    return result


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


def job_counts(job_id: str, status: str = "") -> Dict[str, int]:
    """统一计数口径（单一来源）。

    - success：item completed 且最新判题结果不是 needs_review/failed
    - review：item completed 且最新判题结果 needs_review=1（待复核也是已处理，但单独计数）
    - failed：item failed
    - completed = success + review + failed
    - pending + running + completed = total
    """
    rows = db.q("""
        SELECT i.status item_status, r.status run_status, COALESCE(r.needs_review, 0) review
        FROM teacher_grade_job_items i
        LEFT JOIN teacher_grade_runs r ON r.id=i.grade_id
        WHERE i.job_id=?
    """, (job_id,))
    total = len(rows)
    pending = running = success = review = failed = 0
    for row in rows:
        item_status = str(row.get("item_status") or "pending")
        if item_status == "running":
            running += 1
        elif item_status == "pending":
            pending += 1
        elif item_status == "failed":
            failed += 1
        elif item_status == "completed":
            if int(row.get("review") or 0) == 1 or str(row.get("run_status") or "") == "needs_review":
                review += 1
            elif str(row.get("run_status") or "") == "failed":
                failed += 1
            else:
                success += 1
        else:
            pending += 1
    completed = success + review + failed
    return {
        "total": total, "pending": pending, "running": running,
        "success": success, "review": review, "failed": failed,
        "completed": completed,
        "queued": pending if status in ("queued", "paused", "pausing") else 0,
        "consistent": total == pending + running + completed,
    }


def sync_job_counts(job_id: str) -> Dict[str, int]:
    """用统一计数重算任务字段；completed 字段只存成功数，待复核单独存 needs_review。"""
    row = db.q1("SELECT status FROM teacher_grade_jobs WHERE id=?", (job_id,)) or {}
    counts = job_counts(job_id, str(row.get("status") or ""))
    db.x("UPDATE teacher_grade_jobs SET processed=?, completed=?, failed=?, needs_review=?, updated_at=? WHERE id=?",
         (counts["completed"], counts["success"], counts["failed"], counts["review"], now(), job_id))
    counts["processed"] = counts["completed"]
    counts["needs_review"] = counts["review"]
    return counts


def reparse_candidates(batch_id: str) -> List[Dict[str, Any]]:
    """找出「模型已返回内容，但解析阶段丢了步骤」的最新判题记录。

    只取每个作答的最新版本，避免对已经重解析过的记录重复处理（幂等）。
    """
    return db.q("""
        SELECT g.id grade_id, g.answer_id, g.raw_response, g.status, g.needs_review, g.created_at
        FROM teacher_grade_runs g JOIN teacher_answers a ON a.id = g.answer_id
        WHERE a.import_batch_id=?
          AND (g.step_results_json IS NULL OR g.step_results_json IN ('', '[]'))
          AND COALESCE(g.raw_response, '') <> ''
          AND g.id = (SELECT g2.id FROM teacher_grade_runs g2 WHERE g2.answer_id = g.answer_id ORDER BY g2.grading_version DESC, g2.created_at DESC LIMIT 1)
        ORDER BY g.created_at, g.id
    """, (batch_id,))


def reparse_history(batch_id: str, dry_run: bool = True, limit: int = 0) -> Dict[str, Any]:
    """从历史 raw_response 重新解析，生成新的 grading_version。

    - 不调用模型、不改写原始作答、不覆盖旧判题记录；
    - 新记录带 `reparsed_from` 与 attempt(stage=reparse) 审计；
    - 解析失败（模型本身分值越界/不闭合）如实记为 failed，不伪造及格。
    """
    candidates = reparse_candidates(batch_id)
    if limit and limit > 0:
        candidates = candidates[:limit]
    result: Dict[str, Any] = {"batch_id": batch_id, "dry_run": bool(dry_run), "candidates": len(candidates), "reparsed": 0, "needs_review": 0, "failed": 0, "skipped": 0, "failures": [], "new_grades": []}
    for row in candidates:
        answer = db.q1("SELECT * FROM teacher_answers WHERE id=?", (row["answer_id"],))
        if not answer:
            result["skipped"] += 1
            continue
        question = teacher_meta(answer)["question"]
        max_score = float(question.get("max_score") or answer.get("max_score") or 0)
        try:
            payload = json.loads(row["raw_response"])
        except Exception as exc:
            result["failed"] += 1
            result["failures"].append({"grade_id": row["grade_id"], "answer_id": row["answer_id"], "reason": f"raw_response 不是合法 JSON：{exc}"})
            continue
        if dry_run:
            try:
                validated = validate_llm_result(dict(payload), max_score, question, source_steps=build_source_steps(answer), batch_id=batch_id)
                if validated.get("needs_review"):
                    result["needs_review"] += 1
                else:
                    result["reparsed"] += 1
                steps = validated.get("step_results") or []
                result["new_grades"].append({"grade_id": row["grade_id"], "answer_id": row["answer_id"], "would_status": "needs_review" if validated.get("needs_review") else "graded", "steps": len(steps), "links": sum(len(step.get("scoring_point_links") or []) for step in steps), "issues": [item.get("code") for item in validated.get("validation_issues") or []]})
            except Exception as exc:
                result["failed"] += 1
                result["failures"].append({"grade_id": row["grade_id"], "answer_id": row["answer_id"], "reason": str(exc)[:300]})
            continue
        try:
            with db.transaction():
                saved = grade_answer(answer["id"], raw_override=payload, model_label="deepseek-chat", reparsed_from=row["grade_id"], attempt_stage="reparse")
                grade = saved.get("grade") or {}
                if grade.get("status") == "failed":
                    result["failed"] += 1
                    result["failures"].append({"grade_id": row["grade_id"], "answer_id": row["answer_id"], "reason": grade.get("judgment_reason") or "重解析失败"})
                elif grade.get("needs_review"):
                    result["needs_review"] += 1
                else:
                    result["reparsed"] += 1
                result["new_grades"].append({"grade_id": row["grade_id"], "new_grade_id": grade.get("id"), "answer_id": row["answer_id"], "status": grade.get("status"), "grading_version": grade.get("grading_version"), "steps": len(json.loads(grade.get("step_results_json") or "[]")), "issues": [item.get("code") for item in (json.loads(grade.get("structure_json") or "{}").get("validation_issues") or [])]})
                # 让 job item 指向最新有效结果，避免任务统计停在旧版本上。
                db.x("UPDATE teacher_grade_job_items SET grade_id=?, grading_version=?, status='completed', last_error='', completed_at=?, updated_at=? WHERE answer_id=? AND grade_id=?",
                     (grade.get("id"), grade.get("grading_version"), now(), now(), answer["id"], row["grade_id"]))
        except Exception as exc:
            info = classify_grader_error(exc)
            result["failed"] += 1
            result["failures"].append({"grade_id": row["grade_id"], "answer_id": row["answer_id"], "reason": f'{info["message"]}：{exc}'[:300]})
    if not dry_run:
        for item in db.q("SELECT DISTINCT i.job_id FROM teacher_grade_job_items i JOIN teacher_answers a ON a.id=i.answer_id WHERE a.import_batch_id=?", (batch_id,)):
            try:
                sync_job_counts(item["job_id"])
            except Exception:
                logger.exception("重解析后同步任务计数失败 job=%s", item["job_id"])
    result["remaining_candidates"] = len(reparse_candidates(batch_id))
    if not dry_run:
        refresh_analysis(batch_id, requested_by="reparse")
    return result


@router.post("/batches/{batch_id}/reparse-history")
def reparse_history_api(batch_id: str, user: Dict[str, Any] = Depends(current_user), payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """把历史「模型已判但解析丢失」的记录重新解析成新版本（默认 dry_run）。"""
    require_teacher(user); init()
    owned_batch(batch_id, user)
    return reparse_history(batch_id, dry_run=bool(payload.get("dry_run", True)), limit=int(payload.get("limit") or 0))


_JOB_LOCKS: Dict[str, threading.Lock] = {}
_JOB_LOCKS_GUARD = threading.Lock()


def _job_lock(job_id: str) -> threading.Lock:
    with _JOB_LOCKS_GUARD:
        return _JOB_LOCKS.setdefault(job_id, threading.Lock())


def run_job(job_id: str) -> None:
    """同一 job 串行执行；拿不到锁时延迟重试，不能把任务丢在 queued。"""
    lock = _job_lock(job_id)
    if not lock.acquire(blocking=False):
        threading.Timer(1.0, run_job, args=(job_id,)).start()
        return
    try:
        _run_job_once(job_id)
    finally:
        lock.release()
        with _JOB_LOCKS_GUARD:
            if not lock.locked():
                _JOB_LOCKS.pop(job_id, None)


def ensure_job_worker(job_id: str) -> None:
    """queued/retrying 且没有活跃心跳时补一个 worker，修复进程重启/后台任务丢失导致的卡死。"""
    row = db.q1("SELECT status, heartbeat_at FROM teacher_grade_jobs WHERE id=?", (job_id,))
    if not row or str(row.get("status") or "") not in ("queued", "retrying"):
        return
    heartbeat = str(row.get("heartbeat_at") or "")
    cutoff = (datetime.now() - timedelta(seconds=10)).isoformat(timespec="seconds")
    if heartbeat and heartbeat >= cutoff:
        return
    threading.Thread(target=run_job, args=(job_id,), daemon=True, name=f"job-{job_id[-6:]}").start()


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
    cur = c.execute("UPDATE teacher_grade_jobs SET status='running', started_at=COALESCE(NULLIF(started_at,''),?), worker_id=?, heartbeat_at=?, error='', updated_at=? WHERE id=? AND status IN ('queued','paused','retrying')", (now(), worker_id, now(), now(), job_id))
    c.commit()
    if cur.rowcount != 1:
        return
    try:
        reclaimed_once = False
        while True:
            current = db.q1("SELECT status, pause_requested, cancellation_requested FROM teacher_grade_jobs WHERE id=?", (job_id,)) or {}
            if current.get("pause_requested") or current.get("cancellation_requested"):
                counts = sync_job_counts(job_id)
                db.x("UPDATE teacher_grade_jobs SET status='paused', paused_at=?, current_answer_id='', heartbeat_at=?, updated_at=? WHERE id=?", (now(), now(), now(), job_id))
                return
            item = db.q1("SELECT * FROM teacher_grade_job_items WHERE job_id=? AND status='pending' ORDER BY rowid LIMIT 1", (job_id,))
            if not item:
                if not reclaimed_once and reclaim_stale_items(job_id):
                    reclaimed_once = True
                    continue
                break
            claimed = c.execute("UPDATE teacher_grade_job_items SET status='running', attempt_count=attempt_count+1, started_at=?, updated_at=? WHERE id=? AND status='pending'", (now(), now(), item["id"]))
            c.commit()
            if claimed.rowcount != 1:
                continue
            # 长模型调用前后都刷新心跳，避免健康的 worker 被误判为失联。
            db.x("UPDATE teacher_grade_jobs SET heartbeat_at=?, current_answer_id=?, updated_at=? WHERE id=?", (now(), item["answer_id"], now(), job_id))
            grade_failed = True
            try:
                result = grade_answer(item["answer_id"], key, str(job.get("provider") or "deepseek"), context.get("textbook_course_id", ""), context.get("textbook_unit_id", ""), context.get("textbook_context", ""), job_id)
                grade = result.get("grade") or {}
                if grade.get("status") == "failed" or grade.get("is_correct") == "failed":
                    db.x("UPDATE teacher_grade_job_items SET status='failed', grade_id=?, last_error=?, completed_at=?, updated_at=? WHERE id=?", (grade.get("id"), grade.get("judgment_reason") or "模型判题失败", now(), now(), item["id"]))
                else:
                    db.x("UPDATE teacher_grade_job_items SET status='completed', grade_id=?, grading_version=?, last_error='', completed_at=?, updated_at=? WHERE id=?", (grade.get("id"), grade.get("grading_version"), now(), now(), item["id"]))
                    grade_failed = False
            except Exception as exc:
                db.x("UPDATE teacher_grade_job_items SET status='failed', last_error=?, completed_at=?, updated_at=? WHERE id=?", (str(exc)[:500], now(), now(), item["id"]))
            if int(item.get("retry_round") or 0) > 0:
                db.x("UPDATE teacher_grade_jobs SET retry_success=retry_success+?, retry_failed=retry_failed+? WHERE id=?",
                     (0 if grade_failed else 1, 1 if grade_failed else 0, job_id))
            sync_job_counts(job_id)
            db.x("UPDATE teacher_grade_jobs SET heartbeat_at=?, current_answer_id='', last_checkpoint_at=?, updated_at=? WHERE id=?", (now(), now(), now(), job_id))
    except Exception as exc:
        db.x("UPDATE teacher_grade_jobs SET status='failed', error=?, finished_at=?, updated_at=? WHERE id=?", (str(exc)[:500], now(), now(), job_id))
        return
    current = db.q1("SELECT status, pause_requested FROM teacher_grade_jobs WHERE id=?", (job_id,)) or {}
    counts = job_counts(job_id, str(current.get("status") or ""))
    if current.get("pause_requested") and counts["pending"]:
        final_status = "paused"
    elif counts["success"] + counts["review"] == 0 and counts["failed"] > 0:
        final_status = "failed"
    else:
        final_status = "completed"
    sync_job_counts(job_id)
    db.x("UPDATE teacher_grade_jobs SET status=?, processed=?, completed=?, failed=?, needs_review=?, finished_at=?, current_answer_id='', heartbeat_at=?, updated_at=? WHERE id=?",
         (final_status, counts["completed"], counts["success"], counts["failed"], counts["review"], now() if final_status != "paused" else "", now(), now(), job_id))
    refresh_analysis(str(job.get("batch_id") or ""), requested_by="grading", include_llm=True)


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
    preprocess_status = (batch_row or {}).get("preprocess_status") or "review"
    if preprocess_status not in ("ready", "confirmed"):
        raise HTTPException(409, "数据预处理尚未完成，请稍后再开始判题")
    force = bool(payload.get("force"))
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
    return {"job": db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_id,)), "quality_gate": {"blocking": False, "forced": False, "issues": []}}


@router.post("/grade/jobs/{job_id}/pause")
def pause_job(job_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """幂等暂停：running→pausing；queued/失联→直接 paused；paused 重复调用返回当前状态。"""
    require_teacher(user); init()
    recover_stale_jobs()
    job = db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_id,))
    if not job:
        raise HTTPException(404, "任务不存在")
    status = str(job.get("status") or "")
    if status in ("completed", "failed"):
        counts = sync_job_counts(job_id)
        return {"job": job, "counts": counts, "note": "任务已结束，无需暂停"}
    db.x("UPDATE teacher_grade_jobs SET pause_requested=1, updated_at=? WHERE id=?", (now(), job_id))
    if status == "paused":
        pass
    elif status in ("queued", "pausing"):
        # 没有活跃 worker（queued）或已经在暂停中：直接落 paused，运行中的 item 归还队列。
        db.x("UPDATE teacher_grade_job_items SET status='pending', started_at=NULL, updated_at=? WHERE job_id=? AND status='running'", (now(), job_id))
        db.x("UPDATE teacher_grade_jobs SET status='paused', paused_at=?, current_answer_id='', updated_at=? WHERE id=?", (now(), now(), job_id))
    else:
        db.x("UPDATE teacher_grade_jobs SET status='pausing' WHERE id=? AND status='running'", (job_id,))
    counts = sync_job_counts(job_id)
    return {"job": db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_id,)), "counts": counts, "note": ""}


@router.post("/grade/jobs/{job_id}/resume")
def resume_job(job_id: str, background: BackgroundTasks, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """只有 paused 允许继续；running/queued/retrying 重复调用返回当前状态（幂等），不再新建 worker。"""
    require_teacher(user); init()
    if not default_llm_key():
        raise HTTPException(503, "未配置 DEEPSEEK_API_KEY，管理员配置后才能判题")
    recover_stale_jobs()
    job = db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_id,))
    if not job:
        raise HTTPException(404, "任务不存在")
    status = str(job.get("status") or "")
    if status in ("running", "queued", "retrying"):
        counts = sync_job_counts(job_id)
        return {"job": job, "counts": counts, "reused": True, "note": "任务已在执行，复用当前 worker"}
    if status != "paused":
        raise HTTPException(409, f"只有已暂停的任务可以继续（当前 {status}）")
    db.x("UPDATE teacher_grade_jobs SET pause_requested=0, cancellation_requested=0, status='queued', resumed_at=?, updated_at=? WHERE id=?", (now(), now(), job_id))
    counts = sync_job_counts(job_id)
    background.add_task(run_job, job_id)
    return {"job": db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_id,)), "counts": counts, "reused": False, "note": ""}


@router.post("/grade/jobs/{job_id}/retry")
def retry_job(job_id: str, background: BackgroundTasks, user: Dict[str, Any] = Depends(current_user), payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    require_teacher(user); init()
    if not default_llm_key():
        raise HTTPException(503, "未配置 DEEPSEEK_API_KEY，管理员配置后才能判题")
    job = db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_id,))
    if not job:
        raise HTTPException(404, "判题任务不存在")
    status = str(job.get("status") or "")
    if status in ("running", "queued", "pausing", "retrying"):
        counts = sync_job_counts(job_id)
        return {"job": job, "counts": counts, "retried": 0, "skipped_manual": 0, "reused": True, "note": "任务正在执行或重判，复用当前任务"}
    failed = db.q("""
        SELECT i.id, i.answer_id, i.grade_id,
               (SELECT r.status FROM teacher_grade_runs r WHERE r.id=i.grade_id) run_status
        FROM teacher_grade_job_items i WHERE i.job_id=? AND i.status='failed'
    """, (job_id,))
    if not failed:
        raise HTTPException(409, "没有失败项")
    protected = [item for item in failed if str(item.get("run_status") or "") in ("manual_corrected", "manual_confirmed")]
    retryable = [item for item in failed if item not in protected]
    if not retryable:
        raise HTTPException(409, "失败项都已人工确认，不能自动重试")
    with db.transaction():
        db.x("""UPDATE teacher_grade_job_items SET status='pending', completed_at='', retry_round=retry_round+1, updated_at=?
                WHERE job_id=? AND status='failed'
                AND id NOT IN (SELECT i2.id FROM teacher_grade_job_items i2 JOIN teacher_grade_runs r2 ON r2.id=i2.grade_id
                               WHERE i2.job_id=? AND r2.status IN ('manual_corrected','manual_confirmed'))""", (now(), job_id, job_id))
        db.x("""UPDATE teacher_grade_jobs SET status='retrying', pause_requested=0, cancellation_requested=0, error='',
                retry_total=?, retry_success=0, retry_failed=0, retry_started_at=?, finished_at='', updated_at=? WHERE id=?""",
             (len(retryable), now(), now(), job_id))
    counts = sync_job_counts(job_id)
    background.add_task(run_job, job_id)
    return {"job": db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_id,)), "retried": len(retryable), "skipped_manual": len(protected), "counts": counts, "reused": False, "note": ""}

@router.get("/grade/jobs")
def jobs(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); require_teacher(user)
    recover_stale_jobs()
    normalize_finished_jobs()
    own = str(user.get("id") or "")
    return {"items": db.q("""
        SELECT j.* FROM teacher_grade_jobs j JOIN teacher_batches b ON b.id=j.batch_id
        WHERE b.owner_id IS NULL OR b.owner_id=? ORDER BY j.created_at DESC LIMIT 50
    """, (own,))}


ITEM_STATE_LABELS = {"pending": "排队中", "running": "处理中", "completed": "成功", "failed": "失败"}
JOB_STATE_LABELS = {"preprocessing": "正在预处理", "queued": "排队中", "running": "正在判题", "pausing": "正在暂停", "paused": "已暂停", "retrying": "正在重判", "completed": "已完成", "partially_failed": "已完成", "failed": "任务失败"}


@router.get("/grade/jobs/{job_id}")
def job(job_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); require_teacher(user)
    row = db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_id,))
    if not row:
        raise HTTPException(404, "任务不存在")
    if row.get("status") in ("running", "pausing"):
        recover_stale_jobs()
        reclaim_stale_items(job_id)
        row = db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_id,))
    if row.get("status") in ("queued", "retrying"):
        ensure_job_worker(job_id)
        row = db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_id,))
    counts = job_counts(job_id, str(row.get("status") or ""))
    counts["needs_review"] = counts["review"]
    counts["processed"] = counts["completed"]
    items = db.q("SELECT * FROM teacher_grade_job_items WHERE job_id=? ORDER BY rowid", (job_id,))
    for item in items:
        item["state"] = ITEM_STATE_LABELS.get(str(item.get("status") or ""), str(item.get("status") or ""))
    total = counts["total"] or 0
    retry_total = int(row.get("retry_total") or 0)
    job_payload = dict(row)
    job_payload.update({
        "success": counts["success"], "review": counts["review"], "failed": counts["failed"],
        "pending_count": counts["pending"], "running_count": counts["running"],
        "completed_count": counts["completed"],
    })
    return {
        "job": job_payload,
        "items": items,
        "counts": counts,
        "progress": round(counts["completed"] / total * 100, 1) if total else 0,
        "retry_progress": {
            "total": retry_total,
            "finished": int(row.get("retry_success") or 0) + int(row.get("retry_failed") or 0),
            "success": int(row.get("retry_success") or 0),
            "failed": int(row.get("retry_failed") or 0),
            "percent": round((int(row.get("retry_success") or 0) + int(row.get("retry_failed") or 0)) / retry_total * 100, 1) if retry_total else 0,
        } if retry_total else None,
        "processed_consistent": int(row.get("processed") or 0) == counts["completed"],
        "counts_consistent": counts["consistent"],
        "state": JOB_STATE_LABELS.get(str(row.get("status") or ""), str(row.get("status") or "")),
        "failures_url": f"/api/teacher/grade/jobs/{job_id}/failures",
    }


@router.get("/grade/jobs/{job_id}/failures")
def job_failures(job_id: str, page: int = 1, page_size: int = 20, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """失败明细分页。必须与 job.failed 完全对账，且一条作答的多次尝试只算一条失败。"""
    init(); require_teacher(user)
    job_row = owned_job(job_id, user)
    page = max(1, int(page or 1)); page_size = max(1, min(200, int(page_size or 20)))
    total = int(db.q1("SELECT COUNT(*) n FROM teacher_grade_job_items WHERE job_id=? AND status='failed'", (job_id,))["n"] or 0)
    rows = db.q("""
        SELECT i.id item_id, i.answer_id, i.status, i.attempt_count, i.last_error, i.updated_at, i.completed_at,
               a.student_id, a.question_id, a.event_id, a.revision, s.code student_code, c.code class_code,
               r.id grade_id, r.error_type run_error_type, r.judgment_reason, r.request_context_json
        FROM teacher_grade_job_items i
        LEFT JOIN teacher_answers a ON a.id = i.answer_id
        LEFT JOIN teacher_students s ON s.id = a.student_id
        LEFT JOIN teacher_classes c ON c.id = s.class_id
        LEFT JOIN teacher_grade_runs r ON r.id = i.grade_id
        WHERE i.job_id=? AND i.status='failed'
        ORDER BY i.updated_at DESC, i.rowid DESC LIMIT ? OFFSET ?
    """, (job_id, page_size, (page - 1) * page_size))
    items = []
    for row in rows:
        context: Dict[str, Any] = {}
        try:
            context = json.loads(row.get("request_context_json") or "{}")
        except Exception:
            context = {}
        attempts = db.q("SELECT attempt_number, start_time, end_time, model, prompt_version, failure_stage, error_type, http_status, error_message, retryable, duration_ms, result_id FROM teacher_grade_attempts WHERE answer_id=? AND (COALESCE(task_id,'')=? OR COALESCE(task_id,'')='') ORDER BY attempt_number", (row["answer_id"], job_id))
        last_attempt = attempts[-1] if attempts else {}
        # 历史失败记录没有 request_context；回退到最后一次 attempt 的具体分类，而不是笼统的 model_error。
        error_type = str(context.get("error_type") or last_attempt.get("error_type") or row.get("run_error_type") or "model_error")
        if error_type in ("model_error", "unknown_error"):
            error_type = infer_error_type_from_message(str(last_attempt.get("error_message") or context.get("error_detail") or ""), error_type)
        exception_class = str(context.get("exception_class") or "")
        exception_message = str(context.get("error_detail") or "")
        if not exception_message and attempts:
            exception_message = str(last_attempt.get("error_message") or "")
        readable = str(context.get("error_message") or ("模型返回结构不符合判题契约" if error_type == "model_contract_violation" else error_label(error_type)))
        if context.get("error_detail") and context.get("error_detail") not in readable:
            readable = f"{readable}（{str(context.get('error_detail'))[:160]}）"
        items.append({
            "item_id": row["item_id"], "answer_id": row["answer_id"], "student_code": row.get("student_code"), "class_code": row.get("class_code"),
            "student_id": row.get("student_id"), "question_id": row.get("question_id"), "event_id": row.get("event_id"), "revision": row.get("revision"),
            "status": row["status"], "state": ITEM_STATE_LABELS.get(str(row["status"]), str(row["status"])),
            "attempt_count": int(row.get("attempt_count") or 0), "last_error": row.get("last_error") or "",
            "error_type": error_type, "error_label": error_label(error_type), "readable_reason": readable,
            "error_detail": str(context.get("error_detail") or ""), "exception_class": exception_class,
            "exception_message": exception_message,
            "retryable": bool(context.get("retryable")), "error_stage": context.get("error_stage") or last_attempt.get("failure_stage") or "",
            "error_stage_label": ERROR_STAGE_LABELS.get(str(context.get("error_stage") or last_attempt.get("failure_stage") or ""), str(context.get("error_stage") or last_attempt.get("failure_stage") or "")),
            "first_failed_at": row.get("updated_at"), "last_failed_at": row.get("completed_at") or last_attempt.get("end_time") or row.get("updated_at"),
            "grade_id": row.get("grade_id"), "manual_protected": bool(db.q1("SELECT 1 x FROM teacher_grade_runs r WHERE r.answer_id=? AND r.status IN ('manual_corrected','manual_confirmed') LIMIT 1", (row["answer_id"],))),
            "attempts": attempts,
        })
    counts = job_counts(job_id)
    return {
        "items": items, "total": total, "page": page, "page_size": page_size,
        "has_more": page * page_size < total,
        "counts": {**counts, "attempts": int(db.q1("SELECT COUNT(*) n FROM teacher_grade_attempts WHERE task_id=?", (job_id,))["n"] or 0)},
        "job": {"id": job_id, "status": job_row.get("status"), "failed": int(job_row.get("failed") or 0), "state": JOB_STATE_LABELS.get(str(job_row.get("status") or ""), str(job_row.get("status") or ""))},
        "consistent": total == counts["failed"] == int(job_row.get("failed") or 0),
    }


@router.get("/batches")
def list_batches(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    own = str(user.get("id") or "")
    rows = db.q("SELECT * FROM teacher_batches WHERE owner_id IS NULL OR owner_id=? ORDER BY created_at DESC", (own,))
    items = []
    for batch in rows:
        stats = db.q1("SELECT COUNT(*) total, SUM(CASE WHEN grading_status='pending' THEN 1 ELSE 0 END) pending, SUM(CASE WHEN grading_status='graded' THEN 1 ELSE 0 END) graded, SUM(CASE WHEN grading_status='failed' THEN 1 ELSE 0 END) failed, SUM(CASE WHEN grading_status='needs_review' THEN 1 ELSE 0 END) needs_review FROM teacher_answers WHERE import_batch_id=?", (batch["id"],)) or {}
        people = db.q1("SELECT COUNT(DISTINCT a.student_id) students, COUNT(DISTINCT s.class_id) classes, COUNT(DISTINCT a.question_id) questions FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id WHERE a.import_batch_id=?", (batch["id"],)) or {}
        job = db.q1("SELECT * FROM teacher_grade_jobs WHERE batch_id=? ORDER BY created_at DESC LIMIT 1", (batch["id"],))
        items.append({**batch, **stats, **people, "needs_review": int(stats.get("needs_review") or 0), "latest_job": job, "task_status": (job or {}).get("status") or "imported"})
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


def _number_or_none(value: Any) -> Optional[float]:
    """把 CSV/数据库里的值转成有限浮点数；非法值返回 None（不伪造 0）。"""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_timestamp(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def process_requirement(question_type: Any) -> str:
    """题目是否要求解题过程：answer_only / requires_process / process_optional / unknown。"""
    text = str(question_type or "").strip()
    if not text:
        return "unknown"
    if any(key in text for key in ("选择", "判断", "单选", "多选", "填空")):
        return "answer_only"
    if any(key in text for key in ("解答", "计算", "证明", "应用", "综合", "作图", "简答", "论述")):
        return "requires_process"
    return "process_optional"


def _build_issues(batch_id: str) -> Dict[str, Any]:
    rows = db.q(f"SELECT a.*, s.code student_code, s.class_id, c.code class_code, {QUESTION_COLUMNS} FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id LEFT JOIN teacher_classes c ON c.id=s.class_id {QUESTION_JOIN} WHERE a.import_batch_id=? ORDER BY a.student_id, a.event_id, a.revision", (batch_id,))
    db.x("DELETE FROM teacher_data_issues WHERE task_id=? AND (resolved_by IS NULL OR resolved_by='') AND (handling_result IS NULL OR handling_result='')", (batch_id,))
    # 1. 孤儿作答：JOIN 会隐藏它们，必须单独查，否则「学生缺失」永远统计不到。
    for orphan in db.q("SELECT a.* FROM teacher_answers a LEFT JOIN teacher_students s ON s.id=a.student_id WHERE a.import_batch_id=? AND s.id IS NULL", (batch_id,)):
        _add_issue(batch_id, f"answer:{orphan['id']}:student", "missing_student", "answer", "error", {"question_id": orphan.get("question_id"), "student_id": orphan.get("student_id"), "event_id": orphan.get("event_id"), "revision": orphan.get("revision")}, orphan.get("student_id"), "存在的学生", "作答关联的学生不存在", "teacher_students lookup", "pending_review")
    # 2. 题目级：题干/满分/参考答案/评分点/主题/指标/题目版本
    #    评分点 -> 知识点映射按 code 查（批次映射优先，缺省回退全局），与 _knowledge_links 口径一致。
    mapped_codes: set = set()
    mapping_available = _table_exists("scoring_point_knowledge_links") and _table_exists("scoring_points")
    if mapping_available:
        mapped_codes = {
            str(row["code"]) for row in db.q(
                "SELECT DISTINCT sp.code AS code FROM scoring_point_knowledge_links l JOIN scoring_points sp ON sp.id=l.scoring_point_id WHERE l.batch_id IS NULL OR l.batch_id=?",
                (batch_id,),
            )
        }
    for q in db.q(f"""
        SELECT DISTINCT a.question_id AS id, {QUESTION_COLUMNS}
        FROM teacher_answers a {QUESTION_JOIN}
        WHERE a.import_batch_id=?""", (batch_id,)):
        qrow = {"question_id": q.get("id"), "source_row": 0}
        if not str(q.get("stem") or "").strip():
            _add_issue(batch_id, f"question:{q['id']}:stem", "missing_stem", "question", "error", qrow, "", "题干不能为空", "题目缺少题干（版本与全局题表都没有）", "stem empty", "pending_review")
        max_value = _number_or_none(q.get("question_max_score"))
        if max_value is None or max_value <= 0:
            _add_issue(batch_id, f"question:{q['id']}:max", "invalid_max_score", "question", "error", qrow, q.get("question_max_score"), ">0 的数值", "题目满分必须大于 0（取任务题目版本）", "question_version.max_score <= 0 or not numeric", "pending_review")
        if not str(q.get("reference_answer") or "").strip():
            _add_issue(batch_id, f"question:{q['id']}:ref", "missing_reference_answer", "question", "error", qrow, "", "参考答案不能为空", "题目缺少参考答案", "reference_answer empty", "pending_review")
        codes = re.findall(r"\b[A-Z]\d{3}P\d+\b", str(q.get("scoring_points") or ""))
        if not str(q.get("scoring_points") or "").strip():
            _add_issue(batch_id, f"question:{q['id']}:points", "missing_scoring_points", "question", "error", qrow, "", "评分点不能为空", "题目缺少原始评分点", "scoring_points empty", "pending_review")
        else:
            duplicated = sorted({code for code in codes if codes.count(code) > 1})
            if duplicated:
                _add_issue(batch_id, f"question:{q['id']}:dup_points", "scoring_point_duplicate", "question", "error", qrow, ",".join(duplicated), "每个评分点只出现一次", "同一评分点在原始评分点文本中重复出现", "duplicated scoring point codes", "pending_review")
            parsed = parse_scoring_points(q.get("scoring_points"))
            invalid_points = [code for code in codes if code not in parsed or parsed[code] <= 0]
            if invalid_points:
                _add_issue(batch_id, f"question:{q['id']}:point_max", "scoring_point_max_invalid", "question", "error", qrow, ",".join(sorted(invalid_points)), "每个评分点都有正的满分", "评分点满分缺失、为 0 或无法解析", "parse_scoring_points missing/<=0", "pending_review")
            nums = [float(n) for n in re.findall(r"(\d+(?:\.\d+)?)\s*分", str(q.get("scoring_points")))]
            if nums and max_value is not None and abs(sum(nums) - max_value) > 0.01:
                _add_issue(batch_id, f"question:{q['id']}:sum", "scoring_points_sum_mismatch", "question", "error", qrow, sum(nums), max_value, "评分点分值之和与题目满分不一致", "sum(score in scoring_points) != question_version.max_score", "pending_review")
        topic = db.q1("SELECT * FROM teacher_topics WHERE id=? OR code=?", (q.get("topic_id"), q.get("topic_id"))) or {}
        if not topic:
            _add_issue(batch_id, f"question:{q['id']}:topic", "missing_topic", "knowledge", "warning", qrow, q.get("topic_id"), "存在的主题", "题目主题编号不存在", "teacher_topics lookup", "pending_review")
        indicator = db.q1("SELECT * FROM teacher_indicators WHERE id=? OR code=?", (q.get("indicator_id"), q.get("indicator_id"))) or {}
        if q.get("indicator_id") and not indicator:
            _add_issue(batch_id, f"question:{q['id']}:indicator", "missing_indicator", "knowledge", "warning", qrow, q.get("indicator_id"), "存在的指标", "题目指标编号不存在", "teacher_indicators lookup", "pending_review")
        if indicator and topic and str(indicator.get("topic_id")) not in (str(topic.get("id")), f"topic_{topic.get('code')}"):
            _add_issue(batch_id, f"question:{q['id']}:indicator_topic", "indicator_topic_mismatch", "knowledge", "error", qrow, indicator.get("topic_id"), topic.get("id"), "指标不属于该题目主题", "indicator.topic_id != question.topic_id", "pending_review")
        if mapping_available and codes and not mapped_codes:
            _add_issue(batch_id, f"question:{q['id']}:mapping", "missing_knowledge_mapping", "knowledge", "warning", qrow, ",".join(sorted(set(codes))), "每个评分点至少有 1 条知识点映射", "该题目的评分点没有任何知识点映射", "scoring_point_knowledge_links missing", "pending_review")
        unmapped_codes = sorted({code for code in codes if code not in mapped_codes}) if mapping_available else []
        if unmapped_codes:
            _add_issue(batch_id, f"question:{q['id']}:mapping_missing_points", "unmapped_scoring_point", "knowledge", "warning", qrow, ",".join(unmapped_codes), "全部评分点已映射知识点", "部分评分点没有知识点映射，其证据会被丢弃", "scoring_point_knowledge_links missing per code", "pending_review")
    # 3. 作答级：空值/数值/时间/版本/得分
    for row in rows:
        key = f"answer:{row['id']}"
        answer_text = str(row.get("original_answer") or "").strip()
        process_text = str(row.get("solution_process") or "").strip()
        status_text = str(row.get("score_status") or "")
        if not process_text:
            _add_issue(batch_id, key + ":process", "missing_process", "answer", "info", row, "", "学生未提交解题过程", "学生未提交解题过程；不视为数据错误", "solution_process empty", "accepted")
        if not answer_text and not process_text:
            # 三种「没有答案」必须区分，不能一律当作答错。
            if status_text and any(word in status_text for word in ("未提交", "缺考", "未作答")):
                _add_issue(batch_id, key + ":unsubmitted", "explicit_unsubmitted", "answer", "info", row, status_text, "原始记录明确未提交", "原始评分状态明确记录了未提交/缺考", "score_status explicit unsubmitted", "accepted")
            else:
                _add_issue(batch_id, key + ":empty", "empty_answer_unknown", "answer", "warning", row, status_text, "应明确是否作答", "答案与过程都为空且没有未提交标记，无法判断是漏答还是缺记录", "answer+process empty, status unclear", "pending_review")
        elif not answer_text:
            _add_issue(batch_id, key + ":answer", "missing_final_answer", "answer", "warning", row, "", "学生未填写最终答案", "有解题过程但最终作答为空", "original_answer empty, process present", "pending_review")
        max_value = _number_or_none(row.get("question_max_score") if row.get("question_max_score") is not None else row.get("max_score"))
        raw_score = _number_or_none(row.get("original_score"))
        if row.get("original_score") is not None and raw_score is None:
            _add_issue(batch_id, key + ":score_type", "invalid_numeric", "answer", "error", row, row.get("original_score"), "数值", "原始得分不是合法数值", "original_score not numeric", "pending_review")
        if row.get("max_score") is not None and _number_or_none(row.get("max_score")) is None:
            _add_issue(batch_id, key + ":max_type", "invalid_numeric", "answer", "error", row, row.get("max_score"), "数值", "作答行满分不是合法数值", "answer.max_score not numeric", "pending_review")
        if raw_score is not None and max_value is not None and (raw_score < 0 or raw_score > max_value):
            _add_issue(batch_id, key + ":score", "score_out_of_range", "score", "error", row, f"{raw_score}/{max_value}", f"0..{max_value}", "原始得分超出题目满分范围（题目满分取任务版本）", "original_score < 0 or > question_version.max_score", "pending_review")
        if not row.get("exam_node_id"):
            _add_issue(batch_id, key + ":exam", "missing_exam_node", "answer", "error", row, "", "必须关联测验节点", "作答缺少测验节点", "exam_node_id empty", "pending_review")
        if str(row.get("submit_time") or "").strip() and _parse_timestamp(row.get("submit_time")) is None:
            _add_issue(batch_id, key + ":time", "invalid_timestamp", "answer", "warning", row, row.get("submit_time"), "可解析的日期时间", "提交时间无法解析", "submit_time not parseable", "pending_review")
        version_id = str(row.get("question_version_id") or "")
        if not version_id:
            _add_issue(batch_id, key + ":version", "missing_question_version", "question", "error", row, "", f"qv:{batch_id}:{row.get('question_id')}", "作答没有绑定题目版本，只能回退全局题表", "question_version_id empty", "pending_review")
        else:
            version = db.q1("SELECT * FROM teacher_question_versions WHERE version_id=?", (version_id,))
            if not version or str(version.get("batch_id") or "") != batch_id or str(version.get("question_id") or "") != str(row.get("question_id") or ""):
                _add_issue(batch_id, key + ":version_mismatch", "question_version_mismatch", "question", "error", row, version_id, f"qv:{batch_id}:{row.get('question_id')}", "作答绑定的题目版本不属于当前任务或题目不一致", "question_version.batch_id/question_id mismatch", "pending_review")
    # 4. 跨任务身份/班级冲突（只提示，不自动合并）
    for conflict in db.q("""
        SELECT e.student_id, e.batch_id, e.class_id enrollment_class, s.class_id roster_class, s.code
        FROM teacher_enrollments e JOIN teacher_students s ON s.id=e.student_id
        WHERE e.batch_id=? AND COALESCE(e.class_id,'')<>'' AND COALESCE(s.class_id,'')<>'' AND e.class_id<>s.class_id""", (batch_id,)):
        _add_issue(batch_id, f"enrollment:{conflict['student_id']}:class", "student_class_conflict", "answer", "error", {"student_id": conflict["student_id"], "student_code": conflict["code"]}, conflict["enrollment_class"], conflict["roster_class"], "任务内班级与全局学生班级不一致", "teacher_enrollments.class_id != teacher_students.class_id", "pending_review")
    # 5. 重复/冲突
    groups = db.q("SELECT student_id,event_id,revision,COUNT(*) n,COUNT(DISTINCT original_answer || '|' || COALESCE(solution_process,'')) variants FROM teacher_answers WHERE import_batch_id=? GROUP BY student_id,event_id,revision HAVING n>1", (batch_id,))
    for g in groups:
        row = {"student_id": g["student_id"], "event_id": g["event_id"], "revision": g["revision"], "source_row": 0}
        if int(g.get("variants") or 0) > 1:
            _add_issue(batch_id, f"duplicate:{g['student_id']}:{g['event_id']}:{g['revision']}", "conflicting_duplicate", "answer", "error", row, g.get("n"), 1, "同事件同版本内容冲突", "student_id+event_id+revision count >1 and distinct content", "pending_review")
        else:
            _add_issue(batch_id, f"duplicate:{g['student_id']}:{g['event_id']}:{g['revision']}", "exact_duplicate", "answer", "warning", row, g.get("n"), 1, "完全重复记录，有效集只保留一份", "same content and key", "auto_normalized")
    # 6. 作答版本冲突：同一学生同一事件里，较高 revision 的提交时间早于较低 revision
    for pair in db.q("""
        SELECT student_id, event_id, MIN(CASE WHEN revision=1 THEN submit_time END) first_time,
               MAX(CASE WHEN revision=(SELECT MAX(revision) FROM teacher_answers a2 WHERE a2.student_id=a.student_id AND a2.event_id=a.event_id AND a2.import_batch_id=a.import_batch_id) THEN submit_time END) last_time
        FROM teacher_answers a WHERE import_batch_id=? GROUP BY student_id, event_id
        HAVING first_time IS NOT NULL AND last_time IS NOT NULL""", (batch_id,)):
        first_dt = _parse_timestamp(pair.get("first_time")); last_dt = _parse_timestamp(pair.get("last_time"))
        if first_dt and last_dt and last_dt < first_dt:
            _add_issue(batch_id, f"revision:{pair['student_id']}:{pair['event_id']}", "revision_time_conflict", "answer", "warning", {"student_id": pair["student_id"], "event_id": pair["event_id"], "source_row": 0}, f"{pair['last_time']} < {pair['first_time']}", "修订版本越新提交时间不应更早", "newer revision has earlier submit_time", "pending_review")
    issues = db.q("SELECT * FROM teacher_data_issues WHERE task_id=? ORDER BY created_at, id", (batch_id,))
    by_answer: Dict[Tuple[Any, Any, Any, Any], List[Dict[str, Any]]] = {}
    by_question: Dict[str, List[Dict[str, Any]]] = {}
    for issue in issues:
        if issue.get("student_id"):
            by_answer.setdefault((issue.get("student_id"), issue.get("event_id"), issue.get("revision"), issue.get("question_id")), []).append(issue)
        elif issue.get("question_id"):
            by_question.setdefault(str(issue.get("question_id")), []).append(issue)
    # 预处理主状态：每条原始记录只能落入 valid / anomaly / pending_confirmation 之一。
    # 质量问题（重复、缺过程、空答案等）作为独立质量标记，不按问题条数重复计入异常。
    states = {"valid": 0, "anomaly": 0, "pending_confirmation": 0}
    flags: Dict[str, int] = {}
    requirements: Dict[str, int] = {}
    class_states: Dict[str, Dict[str, int]] = {}
    process_reminders = 0
    empty_answers = 0
    excluded_records = 0
    for row in rows:
        key = (row["student_id"], row.get("event_id"), row.get("revision"), row.get("question_id"))
        related = by_answer.get(key, []) + by_question.get(str(row.get("question_id")), [])
        requirement = process_requirement(row.get("question_type"))
        requirements[requirement] = requirements.get(requirement, 0) + 1
        has_process = bool(str(row.get("solution_process") or "").strip())
        has_answer = bool(str(row.get("original_answer") or "").strip())
        row_flags = set()
        if not has_process:
            row_flags.add("missing_process")
            if requirement == "requires_process":
                row_flags.add("step_evidence_insufficient")
                process_reminders += 1
            elif requirement == "unknown":
                row_flags.add("process_requirement_unknown")
        if not has_answer and not has_process:
            row_flags.add("empty_answer")
            empty_answers += 1
        if any("duplicate" in str(item.get("issue_type") or "") for item in related):
            row_flags.add("duplicate")
        if requirement == "requires_process" and not has_process:
            pass
        excluded = any(str(item.get("handling_status") or "") == "excluded" for item in related)
        blocking = any(
            str(item.get("severity") or "") == "error"
            and str(item.get("handling_status") or "") not in DATA_QUALITY_RESOLVED
            for item in related
        )
        pending_issue = any(str(item.get("handling_status") or "") == "pending_review" for item in related)
        if excluded:
            state = "anomaly"
            row_flags.add("excluded")
            excluded_records += 1
        elif blocking:
            state = "anomaly"
        elif pending_issue or ("process_requirement_unknown" in row_flags):
            state = "pending_confirmation"
        else:
            state = "valid"
        states[state] += 1
        for flag in row_flags:
            flags[flag] = flags.get(flag, 0) + 1
        class_code = str(row.get("class_code") or "未分班")
        bucket = class_states.setdefault(class_code, {"valid": 0, "anomaly": 0, "pending_confirmation": 0})
        bucket[state] += 1
    raw_records = len(rows)
    state_sum = states["valid"] + states["anomaly"] + states["pending_confirmation"]
    classes = []
    for cls in db.q("SELECT * FROM teacher_classes ORDER BY code"):
        cr = [row for row in rows if row.get("class_id") == cls["id"]]
        bucket = class_states.get(str(cls["code"]), {"valid": 0, "anomaly": 0, "pending_confirmation": 0})
        classes.append({
            "code": cls["code"], "students": len({row["student_id"] for row in cr}), "answers": len(cr),
            "valid": bucket["valid"], "anomaly": bucket["anomaly"], "pending_confirmation": bucket["pending_confirmation"],
            "reconciled": sum(bucket.values()) == len(cr),
        })
    status_counts = {row["handling_status"]: int(row["n"]) for row in db.q("SELECT handling_status,COUNT(*) n FROM teacher_data_issues WHERE task_id=? GROUP BY handling_status", (batch_id,))}
    total_issues = sum(status_counts.values())
    pending_issues = status_counts.get("detected", 0) + status_counts.get("pending_review", 0)
    report = {
        "raw_records": raw_records,
        "valid_records": states["valid"],
        "anomaly_records": states["anomaly"],
        "pending_records": states["pending_confirmation"],
        "pending_confirmation": states["pending_confirmation"],
        "anomalies": states["anomaly"],
        "state_sum": state_sum,
        "reconciled": state_sum == raw_records,
        "states": states,
        "quality_flags": flags,
        "process_requirements": requirements,
        "process_reminders": process_reminders,
        "empty_answers": empty_answers,
        "excluded_records": excluded_records,
        "missing_process": flags.get("missing_process", 0),
        "normalized_records": sum(1 for issue in issues if issue["handling_status"] == "auto_normalized"),
        "duplicate_groups": sum(1 for issue in issues if issue["category"] == "answer" and "duplicate" in issue["issue_type"]),
        "conflict_duplicates": sum(1 for issue in issues if issue["issue_type"] == "conflicting_duplicate"),
        "missing": {
            "solution_process": flags.get("missing_process", 0),
            "original_answer": sum(1 for row in rows if not str(row.get("original_answer") or "").strip()),
            "explicit_unsubmitted": sum(1 for issue in issues if issue["issue_type"] == "explicit_unsubmitted"),
            "unknown": sum(1 for issue in issues if issue["issue_type"] == "empty_answer_unknown"),
        },
        "associations": sum(1 for issue in issues if issue["category"] == "knowledge"),
        "excluded": excluded_records,
        "classes": classes,
        "total_issues": total_issues,
        "pending_issues": pending_issues,
        "accepted_issues": status_counts.get("accepted", 0),
        "auto_normalized_issues": status_counts.get("auto_normalized", 0),
        "resolved_issues": status_counts.get("resolved", 0),
        "excluded_issues": status_counts.get("excluded", 0),
        "status_counts": status_counts,
        "issues": [issue for issue in issues if issue["handling_status"] in ("detected", "pending_review")][:100],
        "by_type": [{"type": r["issue_type"], "count": r["n"]} for r in db.q("SELECT issue_type,COUNT(*) n FROM teacher_data_issues WHERE task_id=? GROUP BY issue_type ORDER BY n DESC", (batch_id,))],
        "unknowns": ["赛题未提供每个学生每个测验节点的完整应答题清单，预期作答缺失暂不能确定"],
    }
    return report


_PREPROCESS_LOCK = threading.Lock()


def run_preprocessing(batch_id: str) -> Dict[str, Any]:
    """计算并持久化预处理报告；幂等，可被上传后线程、页面刷新或确认操作调用。"""
    with _PREPROCESS_LOCK:
        batch = db.q1("SELECT preprocess_status FROM teacher_batches WHERE id=?", (batch_id,))
        if not batch:
            raise KeyError("任务不存在")
        report = _build_issues(batch_id)
        status = "ready"
        db.x("UPDATE teacher_batches SET preprocess_report_json=?, preprocess_status=? WHERE id=?", (json.dumps(report, ensure_ascii=False), status, batch_id))
        return {"batch_id": batch_id, "status": status, "report": report}


@router.get("/batches/{batch_id}/preprocessing")
def preprocessing(batch_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    batch = db.q1("SELECT * FROM teacher_batches WHERE id=?", (batch_id,))
    if not batch:
        raise HTTPException(404, "任务不存在")
    status = str(batch.get("preprocess_status") or "review")
    if status in ("ready", "confirmed"):
        try:
            report = json.loads(batch.get("preprocess_report_json") or "{}")
        except Exception:
            report = {}
        if not report:
            result = run_preprocessing(batch_id)
            report, status = result["report"], result["status"]
        return {"batch_id": batch_id, "status": status, "report": report}
    # 上传后的后台线程还没完成：同步补算一次，保证刷新页面时能拿到真实结果。
    result = run_preprocessing(batch_id)
    return {"batch_id": batch_id, "status": result["status"], "report": result["report"]}

DATA_QUALITY_BLOCKING = {
    "score_out_of_range", "scoring_points_sum_mismatch", "question_version_mismatch", "missing_question_version",
    "invalid_numeric", "student_class_conflict", "duplicate_event_revision", "conflicting_duplicate",
    "missing_student", "missing_stem", "invalid_max_score", "missing_reference_answer", "missing_scoring_points",
}
DATA_QUALITY_PENDING = {
    "missing_final_answer", "empty_answer_unknown", "missing_exam_node", "invalid_timestamp",
    "scoring_point_duplicate", "scoring_point_max_invalid", "revision_time_conflict", "missing_topic",
    "missing_indicator", "indicator_topic_mismatch",
}
DATA_QUALITY_RESOLVED = ("accepted", "auto_normalized", "resolved")


def batch_data_quality(batch_id: str, page: int = 1, page_size: int = 50) -> Dict[str, Any]:
    """统一主状态：每条作答只能落入 valid/abnormal/pending_review/excluded 之一。

    题目级问题（如评分点合计与满分不一致）原先只带 question_id，没有 student_id/event_id，
    在按作答关联时永远匹配不上，于是「阻断级问题」对任何作答都不产生影响（真实库 4 条）。
    这里把题目级问题展开到该题目的全部作答上，并单独给出题目级状态与未关联问题清单，
    保证「原始记录数 = 有效 + 异常 + 待确认 + 已排除」且没有记录被静默丢弃。
    """
    answers = db.q("SELECT a.id, a.student_id, s.code student_code, a.question_id, a.event_id, a.revision, a.grading_status FROM teacher_answers a LEFT JOIN teacher_students s ON s.id=a.student_id WHERE a.import_batch_id=? ORDER BY a.student_id, a.event_id, a.question_id", (batch_id,))
    latest = {}
    for row in db.q("""SELECT g.* FROM teacher_grade_runs g JOIN teacher_answers a ON a.id=g.answer_id
        WHERE a.import_batch_id=? AND g.id=(SELECT g2.id FROM teacher_grade_runs g2 WHERE g2.answer_id=g.answer_id ORDER BY g2.grading_version DESC, g2.created_at DESC LIMIT 1)""", (batch_id,)):
        latest[row["answer_id"]] = row
    issues = db.q("SELECT * FROM teacher_data_issues WHERE task_id=?", (batch_id,))
    by_answer: Dict[Tuple[Any, Any, Any, Any], List[Dict[str, Any]]] = {}
    by_question: Dict[str, List[Dict[str, Any]]] = {}
    unlinked: List[Dict[str, Any]] = []
    for issue in issues:
        if issue.get("student_id"):
            by_answer.setdefault((issue.get("student_id"), issue.get("event_id"), issue.get("revision"), issue.get("question_id")), []).append(issue)
        elif issue.get("question_id"):
            by_question.setdefault(str(issue.get("question_id")), []).append(issue)
        else:
            unlinked.append(issue)
    records = []
    counts = {"valid": 0, "abnormal": 0, "pending_review": 0, "excluded": 0}
    linked_issue_counts: Dict[str, int] = {}
    question_records: Dict[str, Dict[str, Any]] = {}
    for answer in answers:
        related = by_answer.get((answer["student_id"], answer["event_id"], answer["revision"], answer["question_id"]), []) + by_question.get(str(answer["question_id"]), [])
        grade = latest.get(answer["id"])
        blocking = [item for item in related if str(item.get("issue_type") or "") in DATA_QUALITY_BLOCKING and str(item.get("handling_status") or "") not in DATA_QUALITY_RESOLVED]
        pending_items = [item for item in related if str(item.get("issue_type") or "") in DATA_QUALITY_PENDING and str(item.get("handling_status") or "") not in DATA_QUALITY_RESOLVED]
        status = "valid"
        reasons: List[str] = []
        if any(str(item.get("handling_status") or "") == "excluded" for item in related):
            status = "excluded"; reasons.append("问题记录已排除")
        elif grade and grade.get("status") == "failed":
            status = "abnormal"; reasons.append("判题失败")
        elif blocking:
            status = "abnormal"
            reasons.extend(sorted({f"{item.get('issue_type')}：{item.get('reason')}" for item in blocking}))
        elif not grade or str(answer.get("grading_status") or "") == "pending":
            status = "pending_review"; reasons.append("尚未判题")
        elif grade.get("needs_review") or grade.get("status") == "needs_review" or str(answer.get("grading_status") or "") == "needs_review":
            status = "pending_review"; reasons.append("判题待复核")
        elif pending_items:
            status = "pending_review"
            reasons.extend(sorted({f"{item.get('issue_type')}：{item.get('reason')}" for item in pending_items}))
        if not reasons:
            reasons.append("无异常")
        counts[status] += 1
        for issue in related:
            key = str(issue.get("issue_type") or "unknown")
            linked_issue_counts[key] = linked_issue_counts.get(key, 0) + 1
        question = question_records.setdefault(str(answer["question_id"]), {"question_id": answer["question_id"], "total": 0, "valid": 0, "abnormal": 0, "pending_review": 0, "excluded": 0, "issue_types": sorted({str(item.get("issue_type")) for item in by_question.get(str(answer["question_id"]), [])})})
        question["total"] += 1
        question[status] += 1
        records.append({"answer_id": answer["id"], "student_id": answer["student_id"], "student_code": answer["student_code"], "question_id": answer["question_id"], "event_id": answer["event_id"], "revision": answer["revision"], "primary_status": status, "reasons": reasons, "issue_types": [item.get("issue_type") for item in related], "issue_ids": [item.get("id") for item in related], "raw_values": [{"type": item.get("issue_type"), "raw": item.get("raw_value"), "expected": item.get("expected_value")} for item in related if item.get("raw_value") or item.get("expected_value")], "grading_status": answer.get("grading_status"), "grade_status": (grade or {}).get("status")})
    total = len(records)
    page = max(1, int(page or 1)); page_size = max(1, min(200, int(page_size or 50)))
    start = (page - 1) * page_size
    reconciled = sum(counts.values()) == total
    return {
        "batch_id": batch_id,
        "summary": {"total": total, **counts, "reconciled": reconciled},
        "issue_counts": {row["issue_type"]: int(row["n"]) for row in db.q("SELECT issue_type, COUNT(*) n FROM teacher_data_issues WHERE task_id=? GROUP BY issue_type", (batch_id,))},
        "linked_issue_counts": linked_issue_counts,
        "issues_total": len(issues),
        "issues_linked": len(issues) - len(unlinked),
        "unlinked_issues": [{"id": item.get("id"), "issue_type": item.get("issue_type"), "reason": item.get("reason")} for item in unlinked],
        "questions": sorted(question_records.values(), key=lambda item: str(item["question_id"])),
        "blocking_issues": [{"id": item.get("id"), "issue_type": item.get("issue_type"), "question_id": item.get("question_id"), "student_id": item.get("student_id"), "event_id": item.get("event_id"), "reason": item.get("reason"), "raw_value": item.get("raw_value"), "expected_value": item.get("expected_value"), "handling_status": item.get("handling_status")} for item in issues if str(item.get("issue_type") or "") in DATA_QUALITY_BLOCKING and str(item.get("handling_status") or "") not in DATA_QUALITY_RESOLVED],
        "items": records[start:start + page_size],
        "page": page, "page_size": page_size,
        "has_more": start + page_size < total,
    }


@router.get("/batches/{batch_id}/data-quality")
def batch_data_quality_api(batch_id: str, page: int = 1, page_size: int = 50, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    owned_batch(batch_id, user)
    return batch_data_quality(batch_id, page, page_size)


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
    batch = db.q1("SELECT preprocess_status, preprocess_report_json FROM teacher_batches WHERE id=?", (batch_id,))
    if not batch:
        raise HTTPException(404, "任务不存在")
    if not str(batch.get("preprocess_report_json") or ""):
        run_preprocessing(batch_id)
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

@router.get("/analysis/class-cards")
def analysis_class_cards(batch_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """班级卡片：只返回真实统计，供卡片网格选择使用。"""
    require_teacher(user); init()
    if not batch_id:
        row = latest_owned_batch(user)
        batch_id = row["id"] if row else ""
    if not batch_id:
        return {"batch_id": "", "cards": []}
    owned_batch(batch_id, user)
    cards = []
    for cls in analysis.all_classes(batch_id):
        profile = analysis.class_profile(batch_id, cls["id"])
        points = profile.get("knowledge_points") or []
        coverages = [point["coverage"] for point in points if point.get("total_students")]
        at_risk = sum(1 for item in (profile.get("students") or []) if item.get("weak_points"))
        cards.append({
            "id": cls["id"], "code": cls["code"], "name": cls.get("name") or cls["code"],
            "student_count": profile.get("student_count") or 0,
            "valid_student_count": profile.get("valid_student_count") or 0,
            "average_mastery": profile.get("overall_mastery"),
            "knowledge_point_coverage": round(sum(coverages) / len(coverages), 4) if coverages else None,
            "at_risk_count": at_risk,
            "weak_rate": profile.get("overall_weak_rate"),
        })
    return {"batch_id": batch_id, "cards": cards}


@router.get("/analysis/classes/{class_id}/semantic")
def analysis_class_semantic(class_id: str, batch_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """班级语义分析：只读当前有效结果，不在请求里同步调用模型。"""
    require_teacher(user); init()
    if not batch_id:
        row = latest_owned_batch(user)
        batch_id = row["id"] if row else ""
    if not batch_id:
        raise HTTPException(404, "没有可分析任务")
    owned_batch(batch_id, user)
    latest = analysis_pipeline.latest_valid_llm(batch_id, "class", class_id)
    task = db.q1("SELECT id,status,error,evidence_version,created_at,finished_at FROM teacher_analysis_tasks WHERE batch_id=? AND scope='class' AND scope_id=? AND kind='llm' ORDER BY created_at DESC LIMIT 1", (batch_id, class_id)) or {}
    recent_cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 600))
    if not latest and str(task.get("status") or "") not in ("pending", "running") and str(task.get("created_at") or "") < recent_cutoff:
        # 语义分析由系统自动生成：首次查看时入队并后台执行；10 分钟内失败过不反复重试，避免刷页面反复消耗模型。
        try:
            created = analysis_pipeline.enqueue(batch_id, "class", class_id, "llm", requested_by=f"view:{user.get('id') or ''}")
            if created["task"].get("status") == "pending":
                analysis_pipeline.start_background(batch_id)
            task = db.q1("SELECT id,status,error,evidence_version,created_at,finished_at FROM teacher_analysis_tasks WHERE id=?", (created["task"]["id"],)) or task
        except Exception:
            logger.exception("自动创建班级语义分析失败 batch=%s class=%s", batch_id, class_id)
    return {"available": bool(latest), "result": (latest or {}).get("result"), "task": task, "analyzed_evidence_version": (latest or {}).get("task", {}).get("evidence_version")}


@router.get("/analysis/students/{student_id}/semantic")
def analysis_student_semantic(student_id: str, batch_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    if not batch_id:
        row = latest_owned_batch(user)
        batch_id = row["id"] if row else ""
    if not batch_id:
        raise HTTPException(404, "没有可分析任务")
    owned_batch(batch_id, user)
    latest = analysis_pipeline.latest_valid_llm(batch_id, "student", student_id)
    task = db.q1("SELECT id,status,error,evidence_version,created_at,finished_at FROM teacher_analysis_tasks WHERE batch_id=? AND scope='student' AND scope_id=? AND kind='llm' ORDER BY created_at DESC LIMIT 1", (batch_id, student_id)) or {}
    recent_cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 600))
    if not latest and str(task.get("status") or "") not in ("pending", "running") and str(task.get("created_at") or "") < recent_cutoff:
        try:
            created = analysis_pipeline.enqueue(batch_id, "student", student_id, "llm", requested_by=f"view:{user.get('id') or ''}")
            if created["task"].get("status") == "pending":
                analysis_pipeline.start_background(batch_id)
            task = db.q1("SELECT id,status,error,evidence_version,created_at,finished_at FROM teacher_analysis_tasks WHERE id=?", (created["task"]["id"],)) or task
        except Exception:
            logger.exception("自动创建学生语义分析失败 batch=%s student=%s", batch_id, student_id)
    return {"available": bool(latest), "result": (latest or {}).get("result"), "task": task, "analyzed_evidence_version": (latest or {}).get("task", {}).get("evidence_version")}


@router.get("/analysis/classes/{class_id}")
def analysis_class(class_id: str, batch_id: str = "", batch_ids: str = "", scope: str = "single", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    if not batch_id:
        row = latest_owned_batch(user)
        batch_id = row["id"] if row else ""
    if scope in ("all", "multi"):
        # 多任务班级学情：每个学生用合并证据重新估计，再按学生等权聚合。
        selected = [item for item in batch_ids.split(",") if item] or [row["id"] for row in db.q("SELECT id FROM teacher_batches WHERE owner_id IS NULL OR owner_id=? ORDER BY created_at", (str(user.get("id") or ""),))]
        selected = owned_batch_ids(user, selected)
        if not selected:
            raise HTTPException(404, "没有可分析任务")
        if not any(row["id"] == class_id for row in analysis.all_classes(selected[0])):
            raise HTTPException(404, "该班级不属于当前任务")
        result = analysis.class_profile(selected[0], class_id, batch_ids=selected)
        return result
    if not any(row["id"] == class_id for row in analysis.all_classes(batch_id)):
        raise HTTPException(404, "该班级不属于当前任务")
    result = analysis.class_profile(batch_id, class_id)
    result["analysis_id"] = analysis.save_analysis(batch_id, "class", class_id, result)
    return result

@router.get("/analysis/classes/{class_id}/knowledge/{knowledge_point_id}")
def analysis_class_knowledge(class_id: str, knowledge_point_id: str, batch_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """班级知识点详情：分布、相关题目、评分点、错误与证据。"""
    require_teacher(user); init()
    if not batch_id:
        row = latest_owned_batch(user)
        batch_id = row["id"] if row else ""
    if not batch_id:
        raise HTTPException(404, "没有可分析任务")
    owned_batch(batch_id, user)
    if not any(row["id"] == class_id for row in analysis.all_classes(batch_id)):
        raise HTTPException(404, "该班级不属于当前任务")
    result = analysis.class_knowledge_detail(batch_id, class_id, knowledge_point_id)
    if "error" in result:
        raise HTTPException(404, result["error"])
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

def student_batch_ids(student_id: str, user: Dict[str, Any]) -> List[str]:
    """该学生可见的任务（教师按归属过滤历史无主任务）。"""
    rows = db.q("""SELECT DISTINCT a.import_batch_id id FROM teacher_answers a JOIN teacher_batches b ON b.id=a.import_batch_id
                   WHERE a.student_id=? AND (b.owner_id IS NULL OR b.owner_id=?) ORDER BY a.import_batch_id""", (student_id, str(user.get("id") or "")))
    return [row["id"] for row in rows]


@router.get("/analysis/student-list")
def analysis_student_list(batch_id: str = "", class_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """学生横向信息条：按班级分组，返回编号、总得分、掌握程度、薄弱知识点数。"""
    require_teacher(user); init()
    if not batch_id:
        row = latest_owned_batch(user)
        batch_id = row["id"] if row else ""
    if not batch_id:
        return {"batch_id": "", "groups": []}
    owned_batch(batch_id, user)
    score_rows = {row["student_id"]: row for row in db.q("""
        SELECT a.student_id, SUM(g.score) score, SUM(g.max_score) max_score, COUNT(*) n
        FROM teacher_answers a
        JOIN teacher_grade_runs g ON g.id=(SELECT g2.id FROM teacher_grade_runs g2 WHERE g2.answer_id=a.id ORDER BY g2.grading_version DESC, g2.created_at DESC LIMIT 1)
        WHERE a.import_batch_id=? AND g.status IN ('graded','manual_corrected','manual_confirmed')
        GROUP BY a.student_id""", (batch_id,))}
    groups = []
    for cls in analysis.all_classes(batch_id):
        if class_id and cls["id"] != class_id:
            continue
        profile = analysis.class_profile(batch_id, cls["id"])
        items = []
        for wrapper in profile.get("students") or []:
            student = wrapper.get("student") or {}
            points = [point for point in (wrapper.get("mastery") or []) if point.get("status") != "证据不足"]
            mastery = round(sum(point["mastery_estimate"] for point in points) / len(points), 4) if points else None
            score = score_rows.get(student.get("id")) or {}
            items.append({
                "id": student.get("id"), "code": student.get("code"),
                "total_score": round(float(score.get("score") or 0), 2) if score else None,
                "total_max_score": round(float(score.get("max_score") or 0), 2) if score else None,
                "graded_count": int(score.get("n") or 0),
                "mastery": mastery, "weak_count": len(wrapper.get("weak_points") or []),
                "evidence_count": (wrapper.get("data_quality") or {}).get("evidence_count", 0),
            })
        items.sort(key=lambda item: (-(item["mastery"] if item["mastery"] is not None else -1), item["code"] or ""))
        groups.append({"class_id": cls["id"], "class_code": cls["code"], "students": items})
    return {"batch_id": batch_id, "groups": groups}


@router.get("/analysis/students/{student_id}")
def analysis_student(student_id: str, batch_id: str = "", scope: str = "single", batch_ids: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init()
    if teacher(user):
        student_in_scope(student_id, user)
    else:
        mine = student_for(user)
        if not mine or mine["id"] != student_id:
            raise HTTPException(403, "不能查看其他学生学情")
    if scope == "all":
        # 全部任务：必须把底层证据合并后重新估计，不能对每次任务的掌握度取平均。
        selected = [item for item in batch_ids.split(",") if item] or student_batch_ids(student_id, user)
        selected = owned_batch_ids(user, selected)
        if not selected:
            raise HTTPException(404, "该学生没有可分析任务")
        result = analysis.student_profile_multi(selected, student_id)
        if "error" in result:
            raise HTTPException(404, result["error"])
        return result
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
def analysis_me(batch_id: str = "", scope: str = "single", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init()
    mine = student_for(user)
    if not mine:
        raise HTTPException(403, "当前账号不是学生")
    if scope == "all":
        selected = owned_batch_ids(user, student_batch_ids(mine["id"], user))
        if not selected:
            raise HTTPException(404, "当前学生没有可分析任务")
        return analysis.student_profile_multi(selected, mine["id"])
    if not batch_id:
        # 按任务创建时间选「最新任务」，不能用字符串 id 排序（新格式 id 与旧格式 id 混排不可靠）。
        row = db.q1("""SELECT a.import_batch_id id FROM teacher_answers a JOIN teacher_batches b ON b.id=a.import_batch_id
                       WHERE a.student_id=? ORDER BY b.created_at DESC LIMIT 1""", (mine["id"],))
        batch_id = row["id"] if row else ""
    if not batch_id:
        raise HTTPException(404, "当前学生没有可分析任务")
    result = analysis.student_profile(batch_id, mine["id"])
    result["analysis_id"] = analysis.save_analysis(batch_id, "student", mine["id"], result)
    return result


@router.get("/analysis/evidence-trace")
def analysis_evidence_trace(batch_id: str = "", student_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """证据链逐级追溯：从原始作答到最终证据行，每一级数量与排除样本。"""
    require_teacher(user); init()
    if not batch_id:
        row = latest_owned_batch(user)
        batch_id = row["id"] if row else ""
    if not batch_id:
        raise HTTPException(404, "没有可分析任务")
    owned_batch(batch_id, user)
    if student_id:
        student_in_scope(student_id, user)
    return analysis.evidence_trace(batch_id, student_id)


@router.get("/analysis/overview")
def analysis_overview(batch_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """总体概览：任务、班级、知识点、错误、趋势与证据质量的一次性汇总。"""
    require_teacher(user); init()
    if not batch_id:
        row = latest_owned_batch(user)
        batch_id = row["id"] if row else ""
    if not batch_id:
        return {"task_id": "", "classes": [], "knowledge_points": [], "error_types": [], "trends": [], "suggestions": []}
    owned_batch(batch_id, user)
    batch = db.q1("SELECT * FROM teacher_batches WHERE id=?", (batch_id,)) or {}
    classes = analysis.all_classes(batch_id)
    class_rows = []
    kp_merge: Dict[str, Dict[str, Any]] = {}
    error_merge: Dict[str, Dict[str, Any]] = {}
    trend_rows: List[Dict[str, Any]] = []
    for cls in classes:
        profile = analysis.class_profile(batch_id, cls["id"])
        if "error" in profile:
            continue
        class_rows.append({
            "id": cls["id"], "code": cls["code"], "student_count": profile.get("student_count"),
            "valid_student_count": profile.get("valid_student_count"), "insufficient_student_count": profile.get("insufficient_student_count"),
            "overall_mastery": profile.get("overall_mastery"), "overall_weak_rate": profile.get("overall_weak_rate"),
            "knowledge_point_count": len(profile.get("knowledge_points") or []),
            "evidence_coverage": round(sum(item.get("coverage") or 0 for item in profile.get("knowledge_points") or []) / len(profile.get("knowledge_points") or []), 4) if profile.get("knowledge_points") else None,
            "trends": profile.get("trends") or [],
        })
        for point in profile.get("knowledge_points") or []:
            entry = kp_merge.setdefault(point["knowledge_point_id"], {"knowledge_point_id": point["knowledge_point_id"], "name": point["name"], "classes": [], "weights": [], "values": []})
            entry["classes"].append(cls["code"])
            # 证据不足的知识点没有均值：只参与展示，不参与加权平均（不能当 0 分）。
            if point.get("mean_mastery") is not None:
                entry["values"].append(point["mean_mastery"]); entry["weights"].append(max(1, int(point.get("valid_students") or 0)))
        for error in profile.get("error_types") or []:
            entry = error_merge.setdefault(error["type"], {"type": error["type"], "weighted_count": 0.0, "knowledge_points": set()})
            entry["weighted_count"] += float(error.get("weighted_count") or 0)
            entry["knowledge_points"].update(error.get("knowledge_points") or [])
    knowledge = []
    for entry in kp_merge.values():
        total_weight = sum(entry["weights"]) or 0
        mean = sum(value * weight for value, weight in zip(entry["values"], entry["weights"])) / total_weight if total_weight else None
        knowledge.append({"knowledge_point_id": entry["knowledge_point_id"], "name": entry["name"], "classes": entry["classes"], "mean_mastery": round(mean, 4) if mean is not None else None, "valid_students": total_weight})
    knowledge.sort(key=lambda item: (item["mean_mastery"] is None, item["mean_mastery"] or 0))
    errors = [{"type": entry["type"], "weighted_count": round(entry["weighted_count"], 4), "knowledge_points": sorted(entry["knowledge_points"])} for entry in error_merge.values()]
    errors.sort(key=lambda item: -item["weighted_count"])
    for row in class_rows:
        for point in row["trends"]:
            trend_rows.append({**point, "class_code": row["code"]})
    quality = batch_data_quality(batch_id, 1, 1)["summary"]
    performance = analysis.question_performance(batch_id)
    analysis_tasks = analysis_pipeline.list_tasks(batch_id, limit=6)
    weak = [item for item in knowledge if item["mean_mastery"] is not None and item["mean_mastery"] < 0.65][:3]
    suggestions = []
    for item in weak:
        suggestions.append(f"「{item['name']}」整体掌握度 {round((item['mean_mastery'] or 0) * 100)}%，建议安排概念回顾与同类变式训练。")
    if quality.get("pending_review"):
        suggestions.append(f"仍有 {quality['pending_review']} 条作答处于待确认/待复核状态，结论只覆盖已有有效证据。")
    if not suggestions:
        suggestions.append("当前证据不足以形成稳定教学建议。")
    return {
        "task_id": batch_id, "batch": {"id": batch.get("id"), "filename": batch.get("filename"), "created_at": batch.get("created_at")},
        "classes": class_rows, "knowledge_points": knowledge,
        "error_types": errors, "trends": trend_rows,
        "data_quality": quality, "suggestions": suggestions,
        "question_performance": {"summary": performance["summary"], "basis": performance["basis"], "warnings": performance["warnings"]},
        "analysis": {
            "data_version": analysis_tasks["current_data_version"],
            "latest": {kind: ({"id": task["id"], "status": task["status"], "stale": task["stale"], "error": task.get("error") or "", "finished_at": task.get("finished_at")} if task else None) for kind, task in (analysis_tasks.get("latest") or {}).items()},
        },
        "algorithm_version": analysis.ALGORITHM_VERSION,
    }


@router.get("/analysis/knowledge-matrix")
def analysis_knowledge_matrix(batch_id: str = "", class_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """知识点 × 学生掌握矩阵（用于「知识点分析」页）。"""
    require_teacher(user); init()
    if not batch_id:
        row = latest_owned_batch(user)
        batch_id = row["id"] if row else ""
    if not batch_id:
        return {"knowledge_points": [], "students": [], "cells": []}
    owned_batch(batch_id, user)
    classes = analysis.all_classes(batch_id)
    selected = [cls for cls in classes if not class_id or cls["id"] == class_id]
    if class_id and not selected:
        raise HTTPException(404, "该班级不属于当前任务")
    students: Dict[str, Dict[str, Any]] = {}
    kps: Dict[str, Dict[str, Any]] = {}
    cells: List[Dict[str, Any]] = []
    for cls in selected:
        profile = analysis.class_profile(batch_id, cls["id"])
        if "error" in profile:
            continue
        for student in profile.get("students") or []:
            sid = student["student"]["id"]
            students[sid] = {"id": sid, "code": student["student"]["code"], "class_id": student["student"].get("class_id"), "class_code": cls["code"]}
            for point in student.get("mastery") or []:
                kps[point["knowledge_point_id"]] = {"knowledge_point_id": point["knowledge_point_id"], "name": point["knowledge_point_name"]}
                cells.append({
                    "student_id": sid, "student_code": student["student"]["code"], "class_code": cls["code"],
                    "knowledge_point_id": point["knowledge_point_id"], "status": point["status"],
                    "mastery_estimate": point["mastery_estimate"], "credible_interval_lower": point["credible_interval_lower"], "credible_interval_upper": point["credible_interval_upper"],
                    "evidence_count": point["evidence_count"], "effective_evidence_weight": point["effective_evidence_weight"],
                    "data_coverage": point["data_coverage"], "covered_question_count": point["covered_question_count"], "opportunity_question_count": point["opportunity_question_count"],
                })
    return {
        "task_id": batch_id, "classes": [{"id": cls["id"], "code": cls["code"]} for cls in selected],
        "students": sorted(students.values(), key=lambda item: item["code"]),
        "knowledge_points": sorted(kps.values(), key=lambda item: item["knowledge_point_id"]),
        "cells": cells,
        "status_legend": ["证据不足", "掌握不足", "初步掌握", "基本掌握", "熟练掌握"],
        "algorithm_version": analysis.ALGORITHM_VERSION,
    }


@router.get("/analysis/errors")
def analysis_errors(batch_id: str = "", class_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """错误分析：按类型汇总（只用已保存的判题证据与有效标签，不调用模型）。"""
    require_teacher(user); init()
    if not batch_id:
        row = latest_owned_batch(user)
        batch_id = row["id"] if row else ""
    if not batch_id:
        return {"types": [], "by_question": [], "label_source": {}}
    owned_batch(batch_id, user)
    classes = [cls for cls in analysis.all_classes(batch_id) if not class_id or cls["id"] == class_id]
    if class_id and not classes:
        raise HTTPException(404, "该班级不属于当前任务")
    types: Dict[str, Dict[str, Any]] = {}
    by_question: Dict[str, Dict[str, Any]] = {}
    for cls in classes:
        profile = analysis.class_profile(batch_id, cls["id"])
        if "error" in profile:
            continue
        for error in profile.get("error_types") or []:
            entry = types.setdefault(error["type"], {"type": error["type"], "weighted_count": 0.0, "knowledge_points": set(), "classes": set()})
            entry["weighted_count"] += float(error.get("weighted_count") or 0)
            entry["knowledge_points"].update(error.get("knowledge_points") or [])
            entry["classes"].add(cls["code"])
        for question in profile.get("questions") or []:
            entry = by_question.setdefault(str(question.get("question_id")), {"question_id": question.get("question_id"), "question_type": question.get("question_type"), "answered": 0, "valid": 0, "review": 0, "failed": 0, "average_score": question.get("average_score"), "score_rate": question.get("score_rate")})
            entry["answered"] += int(question.get("answered_students") or 0)
            entry["valid"] += int(question.get("valid_judgement_students") or 0)
            entry["review"] += int(question.get("review_students") or 0)
            entry["failed"] += int(question.get("failed_students") or 0)
    label_source = {row["source"]: int(row["n"]) for row in db.q("SELECT source, COUNT(*) n FROM answer_error_labels WHERE batch_id=? GROUP BY source", (batch_id,))}
    labels = {row["status"]: int(row["n"]) for row in db.q("SELECT status, COUNT(*) n FROM answer_error_labels WHERE batch_id=? GROUP BY status", (batch_id,))}
    return {
        "task_id": batch_id,
        "types": sorted([{"type": entry["type"], "weighted_count": round(entry["weighted_count"], 4), "knowledge_points": sorted(entry["knowledge_points"]), "classes": sorted(entry["classes"])} for entry in types.values()], key=lambda item: -item["weighted_count"]),
        "by_question": sorted(by_question.values(), key=lambda item: str(item["question_id"])),
        "label_source": label_source, "label_status": labels,
        "gold_labels": int(db.q1("SELECT COUNT(*) n FROM error_label_gold WHERE batch_id=?", (batch_id,))["n"] or 0),
        "algorithm_version": analysis.ALGORITHM_VERSION,
    }


@router.get("/analysis/questions")
def analysis_questions(batch_id: str = "", class_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """题目表现：应作答/实际/有效/复核/失败/未判/未作答（按学生-题目去重）。"""
    require_teacher(user); init()
    if not batch_id:
        row = latest_owned_batch(user)
        batch_id = row["id"] if row else ""
    if not batch_id:
        return {"items": [], "summary": {}, "basis": {"source": "none"}, "warnings": []}
    owned_batch(batch_id, user)
    return analysis.question_performance(batch_id, class_id)


@router.get("/analysis/tasks")
def analysis_task_list(batch_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    if not batch_id:
        row = latest_owned_batch(user)
        batch_id = row["id"] if row else ""
    if not batch_id:
        return {"batch_id": "", "tasks": [], "latest": {}}
    owned_batch(batch_id, user)
    return analysis_pipeline.list_tasks(batch_id)


@router.post("/analysis/tasks/run")
def analysis_task_run(user: Dict[str, Any] = Depends(current_user), payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """手动触发/重跑分析；同版本任务复用，force 也只会在没有执行中的任务时重建。"""
    require_teacher(user); init()
    batch_id = str(payload.get("batch_id") or "")
    if not batch_id:
        row = latest_owned_batch(user)
        batch_id = row["id"] if row else ""
    if not batch_id:
        raise HTTPException(404, "没有可分析任务")
    owned_batch(batch_id, user)
    scope = str(payload.get("scope") or "batch")
    scope_id = str(payload.get("scope_id") or "")
    kind = str(payload.get("kind") or "deterministic")
    if kind not in ("deterministic", "llm"):
        raise HTTPException(422, "kind 必须是 deterministic 或 llm")
    if scope not in ("batch", "class", "student"):
        raise HTTPException(422, "scope 必须是 batch/class/student")
    if scope == "class" and not scope_id:
        raise HTTPException(422, "班级范围必须提供 class_id")
    if scope == "student" and not scope_id:
        raise HTTPException(422, "学生范围必须提供 student_id")
    created = analysis_pipeline.enqueue(batch_id, scope, scope_id, kind, requested_by=str(user.get("id") or ""), force=bool(payload.get("force")))
    task = created["task"]
    if task.get("status") == "pending":
        # 立即返回已受理；确定性统计与 LLM 都在后台线程执行，前端轮询真实状态。
        analysis_pipeline.start_background(batch_id)
    return {"task": task, "reused": created.get("reused", False), "note": created.get("note", "")}


@router.get("/analysis/tasks/{task_id}")
def analysis_task_detail(task_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    task = db.q1("SELECT * FROM teacher_analysis_tasks WHERE id=?", (task_id,))
    if not task:
        raise HTTPException(404, "分析任务不存在")
    owned_batch(str(task.get("batch_id") or ""), user)
    return analysis_pipeline.task_detail(task_id)


@router.get("/analysis/suggestions")
def analysis_suggestions(batch_id: str = "", class_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """教学建议：完全由证据与错误分布推导，不调用模型。"""
    require_teacher(user); init()
    if not batch_id:
        row = latest_owned_batch(user)
        batch_id = row["id"] if row else ""
    if not batch_id:
        return {"suggestions": [], "evidence_basis": {}}
    owned_batch(batch_id, user)
    classes = [cls for cls in analysis.all_classes(batch_id) if not class_id or cls["id"] == class_id]
    if class_id and not classes:
        raise HTTPException(404, "该班级不属于当前任务")
    items: List[Dict[str, Any]] = []
    for cls in classes:
        profile = analysis.class_profile(batch_id, cls["id"])
        if "error" in profile:
            continue
        for point in (profile.get("knowledge_points") or [])[:5]:
            if point.get("mean_mastery") is None:
                continue
            if point["mean_mastery"] < 0.65:
                items.append({
                    "level": "class", "class_code": cls["code"], "knowledge_point_id": point["knowledge_point_id"], "knowledge_point_name": point["name"],
                    "mastery": point["mean_mastery"], "weak_students": point.get("weak_students"),
                    "valid_students": point.get("valid_students"), "insufficient_students": point.get("insufficient_students"),
                    "suggestion": f"「{point['name']}」班级均值为 {round(point['mean_mastery'] * 100)}%，{point.get('weak_students') or 0} 人低于 65%，建议先做概念诊断再分层练习。",
                })
    return {
        "task_id": batch_id,
        "suggestions": sorted(items, key=lambda item: item["mastery"]),
        "evidence_basis": {"algorithm_version": analysis.ALGORITHM_VERSION, "rule": "知识点均值 < 0.65 且证据达到门槛", "model_calls": 0},
    }

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
    selection = analysis.comparable_batch_selection(owned_batch_ids(user, selected))
    result = analysis.student_knowledge_trend(student_id, selection["included"])
    result["excluded_batches"] = selection["excluded"]
    result["comparability"] = selection["details"]
    return result


@router.get("/analysis/trend/class/{class_id}")
def analysis_class_trend(class_id: str, batch_ids: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    if not batch_ids:
        row = latest_owned_batch(user)
        batch_ids = row["id"] if row else ""
    selected = owned_batch_ids(user, [item for item in batch_ids.split(",") if item])
    selection = analysis.comparable_batch_selection(selected)
    result = analysis.class_knowledge_trend(class_id, selection["included"])
    result["excluded_batches"] = selection["excluded"]
    result["comparability"] = selection["details"]
    return result


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
    owned_batch(batch_id, user)
    return analysis.generate_candidate_edges(batch_id, user["id"])


@router.post("/knowledge-graph/edges")
def add_knowledge_graph_edge(payload: Dict[str, Any] = Body(...), user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    batch_id = str(payload.get("batch_id") or "")
    if not batch_id:
        raise HTTPException(422, "必须指定任务")
    owned_batch(batch_id, user)
    try:
        edge = analysis.add_knowledge_edge(batch_id, str(payload.get("from_kp") or ""), str(payload.get("to_kp") or ""), "teacher", "confirmed", user["id"])
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"edge": edge}


@router.delete("/knowledge-graph/edges/{edge_id}")
def delete_knowledge_graph_edge(edge_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    edge = db.q1("SELECT * FROM learning_knowledge_edges WHERE id=?", (edge_id,))
    if not edge:
        raise HTTPException(404, "依赖关系不存在")
    owned_batch(str(edge.get("batch_id") or ""), user)
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
    own = str(user.get("id") or "")
    scope = "SELECT id FROM teacher_batches WHERE owner_id IS NULL OR owner_id=?"
    if kind:
        return {"items": db.q(f"SELECT * FROM teacher_version_compat WHERE kind=? AND (batch_id IS NULL OR batch_id IN ({scope})) ORDER BY created_at DESC", (kind, own))}
    return {"items": db.q(f"SELECT * FROM teacher_version_compat WHERE batch_id IS NULL OR batch_id IN ({scope}) ORDER BY created_at DESC", (own,))}


@router.post("/version-compat")
def create_version_compat(payload: Dict[str, Any] = Body(...), user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    analysis.ensure_model()
    batch_id = str(payload.get("batch_id") or "").strip()
    if not batch_id:
        raise HTTPException(422, "必须指定任务")
    owned_batch(batch_id, user)
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
    db.x("INSERT OR REPLACE INTO teacher_version_compat (id,kind,left_version,right_version,batch_id,status,basis,note,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (db.new_id("versioncompat"), kind, left, right, batch_id, "compatible", basis, str(payload.get("note") or ""), user["id"], now()))
    return {"items": db.q("SELECT * FROM teacher_version_compat WHERE kind=? AND left_version=? AND right_version=? AND batch_id=?", (kind, left, right, batch_id))}


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
    try:
        structure = json.loads(grade.get("structure_json") or "{}")
    except Exception:
        structure = {}
    all_points = set((structure.get("scoring_points") or {}).keys()) or set(originals.keys())
    confirmed_count = db.q1("SELECT COUNT(DISTINCT scoring_point_id) n FROM teacher_scoring_point_confirmations WHERE grade_run_id=? AND confirmation_status='confirmed'", (grade_id,))["n"]
    fully_confirmed = int(confirmed_count) >= len(all_points)
    if abs(total - float(grade.get("score") or 0)) > 0.05 or not fully_confirmed:
        db.x("UPDATE teacher_grade_runs SET needs_review=1, status='needs_review' WHERE id=?", (grade_id,))
    else:
        db.x("UPDATE teacher_grade_runs SET needs_review=0, status='manual_confirmed' WHERE id=?", (grade_id,))
    refresh_analysis_for_grade(grade_id, str(user.get("id") or ""))
    return {"grade_id": grade_id, "confirmed_total": total, "matches_whole_score": abs(total - float(grade.get("score") or 0)) <= 0.05, "fully_confirmed": fully_confirmed, "confirmed_points": int(confirmed_count), "total_points": len(all_points), "items": db.q("SELECT * FROM teacher_scoring_point_confirmations WHERE grade_run_id=?", (grade_id,))}

@router.post("/grades/{grade_id}/confirm")
def confirm(grade_id: str, user: Dict[str, Any] = Depends(current_user), payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    require_teacher(user); init()
    before = db.q1("SELECT * FROM teacher_grade_runs WHERE id=?", (grade_id,))
    if not before:
        raise HTTPException(404, "判题结果不存在")
    try:
        structure = json.loads(before.get("structure_json") or "{}")
    except Exception:
        structure = {}
    validation = structure.get("validation") or {}
    if validation.get("review_required") and not payload.get("force"):
        raise HTTPException(409, {"code": "STRUCTURE_REVIEW_REQUIRED", "message": "判题结构存在待复核问题，请逐评分点确认或人工修正；如确需整题确认，传 force=true 并说明理由"})
    note = "教师确认 AI 判题结果" + (f"；强制确认理由：{payload.get('force_reason')}" if payload.get("force") else "")
    after = dict(before); after["status"] = "manual_confirmed"; after["needs_review"] = 0
    db.x("UPDATE teacher_grade_runs SET status='manual_confirmed', needs_review=0 WHERE id=?", (grade_id,))
    db.x("INSERT INTO teacher_corrections (id,grade_run_id,before_json,after_json,note,teacher_id,created_at) VALUES (?,?,?,?,?,?,?)", (db.new_id("correction"), grade_id, json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), note, user["id"], now()))
    refresh_analysis_for_grade(grade_id, str(user.get("id") or ""))
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
    try:
        structure = json.loads(before.get("structure_json") or "{}")
    except Exception:
        structure = {}
    point_sum = sum(float(item.get("awarded") or 0) for item in (structure.get("scoring_points") or {}).values())
    structure["manual_override"] = {"whole_score": score, "points_sum": round(point_sum, 4), "points_synced": abs(point_sum - score) <= 0.05, "note": note, "teacher_id": user["id"], "at": now()}
    db.x("UPDATE teacher_grade_runs SET score=?, feedback=?, status='manual_corrected', needs_review=0, structure_json=? WHERE id=?", (score, after["feedback"], json.dumps(structure, ensure_ascii=False), grade_id))
    db.x("INSERT INTO teacher_corrections (id,grade_run_id,before_json,after_json,note,teacher_id,created_at) VALUES (?,?,?,?,?,?,?)", (db.new_id("correction"), grade_id, json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), note, user["id"], now()))
    refresh_analysis_for_grade(grade_id, str(user.get("id") or ""))
    return {"grade": db.q1("SELECT * FROM teacher_grade_runs WHERE id=?", (grade_id,)), "correction": db.q1("SELECT * FROM teacher_corrections WHERE grade_run_id=? ORDER BY created_at DESC LIMIT 1", (grade_id,))}
