"""教师端：课程/课程标准、赛题导入、原始作答、统一判题与结果。"""
from __future__ import annotations

import csv
import hashlib
import io
import json
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
CREATE TABLE IF NOT EXISTS teacher_questions (id TEXT PRIMARY KEY, topic_id TEXT, standard_id TEXT, course_id TEXT, material_type TEXT, question_type TEXT, stem TEXT, options TEXT, reference_answer TEXT, reference_solution TEXT, max_score REAL, scoring_points TEXT, indicator_id TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS teacher_exams (id TEXT PRIMARY KEY, exam_date TEXT, topics TEXT, description TEXT, available_questions TEXT, course_id TEXT);
CREATE TABLE IF NOT EXISTS teacher_batches (id TEXT PRIMARY KEY, filename TEXT, status TEXT, counts_json TEXT, errors_json TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS teacher_import_files (id TEXT PRIMARY KEY, batch_id TEXT, filename TEXT, rows INTEGER, fields_json TEXT, errors_json TEXT);
CREATE TABLE IF NOT EXISTS teacher_answers (
 id TEXT PRIMARY KEY, student_id TEXT, question_id TEXT, event_id TEXT, revision INTEGER, submit_time TEXT,
 original_answer TEXT, solution_process TEXT, original_score REAL, max_score REAL, score_status TEXT, grading_status TEXT DEFAULT 'pending',
 raw_json TEXT, import_batch_id TEXT, exam_node_id TEXT, course_id TEXT, standard_id TEXT, UNIQUE(event_id, revision)
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
CREATE INDEX IF NOT EXISTS idx_teacher_students_class ON teacher_students(class_id);
"""


def init() -> None:
    db.init()
    with _LOCK:
        c = db.conn()
        c.executescript(SCHEMA)
        migrations = {
            "teacher_questions": [("standard_id", "TEXT"), ("course_id", "TEXT")],
            "teacher_exams": [("course_id", "TEXT")],
            "teacher_answers": [("course_id", "TEXT"), ("standard_id", "TEXT"), ("grading_status", "TEXT DEFAULT 'pending'")],
            "teacher_grade_runs": [("grader_version", "TEXT"), ("course_id", "TEXT"), ("standard_id", "TEXT"), ("standard_version", "TEXT"), ("scoring_rule_version", "TEXT"), ("textbook_course_id", "TEXT"), ("textbook_unit_id", "TEXT"), ("textbook_context", "TEXT"), ("needs_review", "INTEGER DEFAULT 0")],
            "teacher_grade_jobs": [("completed", "INTEGER DEFAULT 0"), ("needs_review", "INTEGER DEFAULT 0"), ("current_answer_id", "TEXT"), ("finished_at", "TEXT"), ("batch_id", "TEXT"), ("textbook_course_id", "TEXT"), ("textbook_unit_id", "TEXT"), ("textbook_context", "TEXT"), ("provider", "TEXT"), ("model", "TEXT"), ("prompt_version", "TEXT"), ("pause_requested", "INTEGER DEFAULT 0"), ("worker_id", "TEXT"), ("started_at", "TEXT"), ("paused_at", "TEXT"), ("resumed_at", "TEXT"), ("last_checkpoint_at", "TEXT"), ("heartbeat_at", "TEXT"), ("retry_of", "TEXT"), ("cancellation_requested", "INTEGER DEFAULT 0")],
            "teacher_grade_job_items": [("attempt_count", "INTEGER DEFAULT 0"), ("current_attempt", "INTEGER DEFAULT 1"), ("started_at", "TEXT"), ("completed_at", "TEXT"), ("last_error", "TEXT"), ("grading_version", "INTEGER"), ("checkpoint_status", "TEXT"), ("updated_at", "TEXT")],
        }
        for table, columns in migrations.items():
            existing = {row[1] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}
            for column, ddl in columns:
                if column not in existing:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        c.execute("UPDATE teacher_answers SET grading_status=COALESCE(NULLIF(grading_status,''),'pending')")
        c.execute("UPDATE teacher_answers SET grading_status='graded' WHERE id IN (SELECT answer_id FROM teacher_grade_runs WHERE status NOT IN ('failed'))")
        c.execute("UPDATE teacher_answers SET grading_status='needs_review' WHERE id IN (SELECT answer_id FROM teacher_grade_runs WHERE needs_review=1)")
        c.execute("""UPDATE teacher_answers
            SET course_id = COALESCE(course_id, (SELECT q.course_id FROM teacher_questions q WHERE q.id=teacher_answers.question_id)),
                standard_id = COALESCE(standard_id, (SELECT q.standard_id FROM teacher_questions q WHERE q.id=teacher_answers.question_id))
            WHERE course_id IS NULL OR standard_id IS NULL""")
        cutoff = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")
        c.execute("UPDATE teacher_grade_jobs SET status='paused', pause_requested=1 WHERE status IN ('running','pausing') AND (heartbeat_at IS NULL OR heartbeat_at < ?)", (cutoff,))
        c.execute("UPDATE teacher_grade_job_items SET status='pending', updated_at=? WHERE status='running' AND job_id IN (SELECT id FROM teacher_grade_jobs WHERE status='paused')", (now(),))
        c.commit()


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def teacher(user: Dict[str, Any]) -> bool:
    return (user.get("role") or "teacher") == "teacher"


def require_teacher(user: Dict[str, Any]) -> None:
    if not teacher(user):
        raise HTTPException(403, "仅教师可执行该操作")


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
    db.x("INSERT INTO xueban_users (id,email,password_hash,password_salt,display_name,auth_type,role,created_at) VALUES (?,?,?,?,?,?,?,?)",
         (uid, email, digest, salt, display, "password", role, now()))
    return db.q1("SELECT * FROM xueban_users WHERE id = ?", (uid,))


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




def import_rows(tables: Dict[str, List[Dict[str, str]]], filename: str = "upload") -> Dict[str, Any]:
    init()
    students = find_table(tables, "模拟学生") or []
    requirements = find_table(tables, "课程要求") or []
    questions = find_table(tables, "题目与材料") or []
    exams = find_table(tables, "测验节点") or []
    answers = find_table(tables, "作答记录") or []
    errors: List[Dict[str, Any]] = []
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
            user = ensure_account(f"{code.lower()}@xueban.local", None, code, "student")
            db.x("INSERT OR REPLACE INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", (f"student_{code}", user["id"], code, class_id, code, int(row.get("年级") or 7), now()))
            counts["students"] += 1
        counts["classes"] = len({str(row.get("模拟班级编号", "")).strip() for row in students})
        for row in questions:
            qid = str(row.get("材料编号", "")).strip()
            topic = str(row.get("主题编号", "")).strip()
            standard_id = ""
            db.x("INSERT OR REPLACE INTO teacher_questions (id,topic_id,standard_id,course_id,material_type,question_type,stem,options,reference_answer,reference_solution,max_score,scoring_points,indicator_id,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (qid, topic, standard_id, "", row.get("材料类型"), row.get("题型"), row.get("题干或内容"), row.get("选项"), row.get("参考答案"), row.get("参考解析"), float(row.get("满分") or 0), row.get("评分点"), row.get("指标编号"), row.get("来源或资料文件")))
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
            if db.q1("SELECT id FROM teacher_answers WHERE event_id=? AND revision=?", (event, revision)):
                counts["skipped"] += 1
                continue
            db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,submit_time,original_answer,solution_process,original_score,max_score,score_status,raw_json,import_batch_id,exam_node_id,course_id,standard_id,grading_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"{event}-r{revision}", student["id"], qid, event, revision, row.get("模拟提交时间"), row.get("学生最终作答"), row.get("解答过程"), float(row.get("得分") or 0), float(row.get("满分") or 0), score_status(row.get("评分状态") or ""), json.dumps(row, ensure_ascii=False), batch_id, row.get("节点编号"), "", "", "pending"))
            counts["answers"] += 1
        db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,created_at) VALUES (?,?,?,?,?,?)", (batch_id, filename, "committed", json.dumps(counts, ensure_ascii=False), json.dumps(errors, ensure_ascii=False), now()))
        db.x("INSERT INTO teacher_import_files (id,batch_id,filename,rows,fields_json,errors_json) VALUES (?,?,?,?,?,?)", (db.new_id("file"), batch_id, filename, len(answers), json.dumps(list(tables), ensure_ascii=False), json.dumps(errors[:100], ensure_ascii=False)))
    return {"batch_id": batch_id, "counts": counts, "errors": errors[:100]}


@router.get("/meta")
def meta(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); require_teacher(user)
    return {"classes": db.q("SELECT * FROM teacher_classes"), "exams": db.q("SELECT * FROM teacher_exams")}


@router.get("/summary")
def summary(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init()
    if not teacher(user):
        student = student_for(user)
        if not student:
            return {"student": True, "answers": 0, "pending": 0, "review": 0}
        return {"student": True, "answers": db.q1("SELECT COUNT(*) n FROM teacher_answers WHERE student_id=?", (student["id"],))["n"], "pending": db.q1("SELECT COUNT(*) n FROM teacher_answers WHERE student_id=? AND grading_status='pending'", (student["id"],))["n"], "review": db.q1("SELECT COUNT(*) n FROM teacher_grade_runs WHERE student_id=? AND needs_review=1", (student["id"],))["n"]}
    return {"students": db.q1("SELECT COUNT(*) n FROM teacher_students")["n"], "classes": db.q1("SELECT COUNT(*) n FROM teacher_classes")["n"], "courses": db.q1("SELECT COUNT(*) n FROM teacher_courses")["n"], "questions": db.q1("SELECT COUNT(*) n FROM teacher_questions")["n"], "answers": db.q1("SELECT COUNT(*) n FROM teacher_answers")["n"], "pending": db.q1("SELECT COUNT(*) n FROM teacher_answers WHERE grading_status='pending'")["n"], "graded": db.q1("SELECT COUNT(*) n FROM teacher_grade_runs")["n"], "review": db.q1("SELECT COUNT(*) n FROM teacher_grade_runs WHERE needs_review=1")["n"], "jobs_running": db.q1("SELECT COUNT(*) n FROM teacher_grade_jobs WHERE status IN ('queued','running')")["n"], "recent_batches": db.q("SELECT * FROM teacher_batches ORDER BY created_at DESC LIMIT 5"), "recent_jobs": db.q("SELECT * FROM teacher_grade_jobs ORDER BY created_at DESC LIMIT 5")}


@router.get("/classes")
def classes(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); require_teacher(user)
    return {"items": db.q("SELECT c.*, (SELECT COUNT(*) FROM teacher_students s WHERE s.class_id=c.id) student_count FROM teacher_classes c ORDER BY c.code")}


@router.get("/students")
def students(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); require_teacher(user)
    return {"items": db.q("SELECT s.id,s.user_id,s.code,s.class_id,s.name,s.grade,c.code class_code FROM teacher_students s LEFT JOIN teacher_classes c ON c.id=s.class_id ORDER BY s.code")}

@router.get("/classes/{class_id}/students")
def class_students(class_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); require_teacher(user)
    return {"items": db.q("SELECT s.*, (SELECT COUNT(*) FROM teacher_answers a WHERE a.student_id=s.id) answers, (SELECT COUNT(*) FROM teacher_answers a WHERE a.student_id=s.id AND a.grading_status='pending') pending FROM teacher_students s WHERE s.class_id=? ORDER BY s.code", (class_id,))}


@router.get("/students/{student_id}")
def student_detail(student_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); student = db.q1("SELECT * FROM teacher_students WHERE id=?", (student_id,))
    if not student:
        raise HTTPException(404, "学生不存在")
    if not teacher(user) and student["user_id"] != user["id"]:
        raise HTTPException(403, "不能查看其他学生")
    return {"student": student, "answers": db.q("SELECT a.*, q.stem, q.question_type, q.reference_answer FROM teacher_answers a JOIN teacher_questions q ON q.id=a.question_id WHERE a.student_id=? ORDER BY a.submit_time DESC", (student_id,))}


@router.get("/answers")
def answers(status: str = "", class_id: str = "", course_id: str = "", standard_id: str = "", exam_node_id: str = "", question_id: str = "", limit: int = 500, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); sql = "SELECT a.*, s.code student_code, q.stem, q.question_type FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id JOIN teacher_questions q ON q.id=a.question_id WHERE 1=1"; args: List[Any] = []
    if not teacher(user):
        student = student_for(user)
        sql += " AND a.student_id=?"
        args.append(student["id"] if student else "")
    if status:
        sql += " AND a.grading_status=?"; args.append(status)
    if class_id:
        sql += " AND s.class_id=?"; args.append(class_id)
    if course_id:
        sql += " AND q.course_id=?"; args.append(course_id)
    if standard_id:
        sql += " AND q.standard_id=?"; args.append(standard_id)
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


@router.get("/answers/{answer_id}")
def answer_detail(answer_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); answer = db.q1("SELECT * FROM teacher_answers WHERE id=?", (answer_id,))
    if not answer:
        raise HTTPException(404, "作答不存在")
    if not visible_answer(answer, user):
        raise HTTPException(403, "不能查看其他学生")
    return {"answer": answer, "question": db.q1("SELECT * FROM teacher_questions WHERE id=?", (answer["question_id"],)), "revisions": db.q("SELECT * FROM teacher_answers WHERE event_id=? ORDER BY revision", (answer["event_id"],)), "grades": db.q("SELECT * FROM teacher_grade_runs WHERE answer_id=? ORDER BY grading_version DESC, created_at DESC", (answer_id,)), "standard": db.q1("SELECT * FROM teacher_standards WHERE id=?", (answer.get("standard_id"),)) if answer.get("standard_id") else None}


def teacher_meta(answer: Dict[str, Any]) -> Dict[str, Any]:
    q = db.q1("SELECT * FROM teacher_questions WHERE id=?", (answer.get("question_id"),)) or {}
    std = db.q1("SELECT * FROM teacher_standards WHERE id=?", (answer.get("standard_id") or q.get("standard_id"),)) or {}
    return {"question": q, "standard": std, "topic": db.q1("SELECT * FROM teacher_topics WHERE id=?", (q.get("topic_id"),)) or {}}


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
    payload = {"题目": question.get("stem"), "参考答案": question.get("reference_answer"), "参考解析": question.get("reference_solution"), "原始评分点": question.get("scoring_points"), "课程标准": standard.get("content_json") or "", "教师教材章节": textbook_context, "学生最终作答": answer.get("original_answer"), "学生原始解题过程": answer.get("solution_process"), "模型辅助步骤": steps, "要求": "只判题，不做学情分析。优先按原始评分点；无标准步骤时允许模型拆解并标记 model_assisted_steps。逐步骤返回 step_index,student_step,expected_step,related_scoring_point,is_correct,score,max_score,error_type,judgment_reason。汇总 score,max_score,is_correct,feedback,judgment_reason,needs_review。仅输出 JSON。"}
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], "temperature": 0}, ensure_ascii=False).encode()
    request = urllib.request.Request(base + "/chat/completions", data=body, headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"}, method="POST")
    with urllib.request.urlopen(request, timeout=45) as response:
        content = json.loads(response.read().decode())["choices"][0]["message"]["content"]
    match = re.search(r"\{.*\}", content, re.S)
    if not match:
        raise ValueError("model returned no JSON")
    return json.loads(match.group(0))


def validate_llm_result(result: Dict[str, Any], max_score: float, question: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(result, dict):
        raise ValueError("model result is not an object")
    score = float(result.get("score"))
    if not isinstance(score, (int, float)) or not 0 <= score <= max_score:
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
        total += step_score
    if steps and abs(total - score) > 0.05:
        raise ValueError("step score sum does not match total")
    if question.get("question_type") in {"解答题", "计算题", "证明题"} and not steps:
        result["needs_review"] = True
    result["score"] = score
    result["step_results"] = steps
    result.setdefault("needs_review", False)
    result.setdefault("is_correct", "partial")
    result.setdefault("feedback", "")
    result.setdefault("judgment_reason", "")
    return result

def insert_grade(answer: Dict[str, Any], question: Dict[str, Any], standard: Dict[str, Any], version: int, model: str, score: float, max_score: float, correct: str, steps: List[Dict[str, Any]], error_type: str, feedback: str, reason: str, review: int, status: str, raw: str, textbook_course_id: str = "", textbook_unit_id: str = "", textbook_context: str = "") -> Dict[str, Any]:
    grade_id = db.new_id("grade")
    db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,grading_prompt_version,grader_version,course_id,standard_id,standard_version,scoring_rule_version,score,max_score,is_correct,step_results_json,error_type,feedback,judgment_reason,needs_review,status,raw_response,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (grade_id, answer["id"], answer["student_id"], answer["question_id"], answer["event_id"], answer["revision"], version, model, "teacher-grade-v2", "v1.4.1", answer.get("course_id") or question.get("course_id"), answer.get("standard_id") or question.get("standard_id"), standard.get("version"), "scoring_points", score, max_score, correct, json.dumps(steps, ensure_ascii=False), error_type, feedback, reason, review, status, raw, now()))
    db.x("UPDATE teacher_grade_runs SET textbook_course_id=?, textbook_unit_id=?, textbook_context=? WHERE id=?", (textbook_course_id, textbook_unit_id, textbook_context[:4000], grade_id))
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

def grade_answer(answer_id: str, key: str = "", provider: str = "deepseek", textbook_course_id: str = "", textbook_unit_id: str = "", textbook_context: str = "") -> Dict[str, Any]:
    init(); answer = db.q1("SELECT * FROM teacher_answers WHERE id=?", (answer_id,))
    if not answer:
        raise HTTPException(404, "作答不存在")
    context = teacher_meta(answer); question = context["question"]; standard = context["standard"]
    steps = split_steps(answer, question)
    max_score = float(question.get("max_score") or answer.get("max_score") or 0)
    version = (db.q1("SELECT COALESCE(MAX(grading_version),0) n FROM teacher_grade_runs WHERE answer_id=?", (answer_id,))["n"] or 0) + 1
    if not key:
        key = default_llm_key()
    if not key:
        raise HTTPException(503, "未配置 DEEPSEEK_API_KEY，管理员配置后才能判题")
    if key:
        try:
            llm = validate_llm_result(llm_grade(answer, question, steps, key, provider, standard, textbook_context), max_score, question)
            score = float(llm["score"])
            grade = insert_grade(answer, question, standard, version, "deepseek-chat" if provider == "deepseek" else "gpt-4o-mini", score, max_score, str(llm.get("is_correct") or "partial"), llm.get("step_results") or [], str(llm.get("error_type") or ""), str(llm.get("feedback") or ""), str(llm.get("judgment_reason") or ""), 1 if llm.get("needs_review") else 0, "needs_review" if llm.get("needs_review") else "graded", json.dumps(llm, ensure_ascii=False))
            return {"grade": grade}
        except Exception as exc:
            grade = insert_grade(answer, question, standard, version, "model-error", 0, max_score, "failed", [], "model_error", "", str(exc)[:500], 1, "failed", "", textbook_course_id, textbook_unit_id, textbook_context)
            return {"grade": grade}
    expected = question.get("reference_answer") or ""
    correct = bool(expected and normalize(answer.get("original_answer") or "") == normalize(expected))
    step_results: List[Dict[str, Any]] = []
    if steps:
        each = round(max_score / len(steps), 2)
        for index, step in enumerate(steps):
            last = index == len(steps) - 1
            step_results.append({"step_index": index + 1, "student_step": step, "expected_step": "", "related_scoring_point": question.get("scoring_points") or "", "is_correct": bool(last and correct), "score": max_score if last and correct else (each if not last else 0), "max_score": max_score if last else each, "error_type": "" if last and correct else "rule_uncertain", "judgment_reason": "规则辅助：只有最终答案可确定；步骤判定需人工复核。"})
    review = 0 if not steps and correct else 1
    score = max_score if correct else 0
    grade = insert_grade(answer, question, standard, version, "rule-assisted", score, max_score, "correct" if correct else "incorrect", step_results, "" if correct else "answer_mismatch", "最终答案与参考答案一致。" if correct else "规则辅助判定：最终答案与参考答案不一致。", "无模型密钥，标记 rule-assisted，不伪装成 AI 判题。" if not steps else "步骤为规则拆分，需人工复核。", review, "needs_review" if review else "graded", "", textbook_course_id, textbook_unit_id, textbook_context)
    return {"grade": grade}


@router.post("/answers/{answer_id}/grade")
def grade(answer_id: str, user: Dict[str, Any] = Depends(current_user), payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    require_teacher(user)
    return grade_answer(answer_id, str(payload.get("llm_key") or ""), str(payload.get("provider") or "deepseek"), str(payload.get("textbook_course_id") or ""), str(payload.get("textbook_unit_id") or ""), str(payload.get("textbook_context") or ""))


def run_job(job_id: str) -> None:
    key = default_llm_key()
    job = db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_id,))
    if not job:
        return
    if not key:
        db.x("UPDATE teacher_grade_jobs SET status='failed', error=?, updated_at=? WHERE id=?", ("未配置 DEEPSEEK_API_KEY", now(), job_id))
        return
    context = {name: str(job.get(name) or "") for name in ("textbook_course_id", "textbook_unit_id", "textbook_context")}
    db.x("UPDATE teacher_grade_jobs SET status='running', started_at=COALESCE(NULLIF(started_at,''),?), worker_id=?, heartbeat_at=?, error='', updated_at=? WHERE id=?", (now(), f"worker-{job_id[-6:]}", now(), now(), job_id))
    while True:
        current = db.q1("SELECT pause_requested, cancellation_requested FROM teacher_grade_jobs WHERE id=?", (job_id,)) or {}
        if current.get("pause_requested") or current.get("cancellation_requested"):
            db.x("UPDATE teacher_grade_jobs SET status='paused', paused_at=?, updated_at=? WHERE id=?", (now(), now(), job_id))
            return
        item = db.q1("SELECT * FROM teacher_grade_job_items WHERE job_id=? AND status='pending' ORDER BY rowid LIMIT 1", (job_id,))
        if not item:
            break
        db.x("UPDATE teacher_grade_job_items SET status='running', attempt_count=attempt_count+1, started_at=?, updated_at=? WHERE id=? AND status='pending'", (now(), now(), item["id"]))
        try:
            result = grade_answer(item["answer_id"], key, "deepseek", context.get("textbook_course_id", ""), context.get("textbook_unit_id", ""), context.get("textbook_context", ""))
            grade = result.get("grade") or {}
            if grade.get("status") == "failed" or grade.get("is_correct") == "failed":
                db.x("UPDATE teacher_grade_job_items SET status='failed', grade_id=?, last_error=?, completed_at=?, updated_at=? WHERE id=?", (grade.get("id"), grade.get("judgment_reason") or "模型判题失败", now(), now(), item["id"]))
            else:
                db.x("UPDATE teacher_grade_job_items SET status='completed', grade_id=?, grading_version=?, last_error='', completed_at=?, updated_at=? WHERE id=?", (grade.get("id"), grade.get("grading_version"), now(), now(), item["id"]))
        except Exception as exc:
            db.x("UPDATE teacher_grade_job_items SET status='failed', last_error=?, completed_at=?, updated_at=? WHERE id=?", (str(exc)[:500], now(), now(), item["id"]))
        counts = db.q1("SELECT COUNT(*) total, SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) completed, SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed FROM teacher_grade_job_items WHERE job_id=?", (job_id,)) or {}
        review = db.q1("SELECT COUNT(*) n FROM teacher_grade_runs r JOIN teacher_grade_job_items i ON i.grade_id=r.id WHERE i.job_id=? AND r.needs_review=1", (job_id,)) or {}
        db.x("UPDATE teacher_grade_jobs SET processed=?, completed=?, failed=?, needs_review=?, current_answer_id=?, last_checkpoint_at=?, heartbeat_at=?, updated_at=? WHERE id=?", (int(counts.get("total") or 0), int(counts.get("completed") or 0), int(counts.get("failed") or 0), int(review.get("n") or 0), item["answer_id"], now(), now(), now(), job_id))
    counts = db.q1("SELECT COUNT(*) total, SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) completed, SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed FROM teacher_grade_job_items WHERE job_id=?", (job_id,)) or {}
    failed_count = int(counts.get("failed") or 0); completed_count = int(counts.get("completed") or 0)
    final_status = "completed" if failed_count == 0 else ("failed" if completed_count == 0 else "partially_failed")
    db.x("UPDATE teacher_grade_jobs SET status=?, finished_at=?, current_answer_id='', heartbeat_at=?, updated_at=? WHERE id=?", (final_status, now(), now(), now(), job_id))


@router.post("/grade/batch")
def batch(background: BackgroundTasks, user: Dict[str, Any] = Depends(current_user), payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    require_teacher(user); init()
    if not default_llm_key():
        raise HTTPException(503, "未配置 DEEPSEEK_API_KEY，管理员配置后才能判题")
    batch_id = str(payload.get("batch_id") or "")
    if not batch_id:
        raise HTTPException(422, "必须选择导入批次")
    filters = {name: str(payload.get(name) or "") for name in ("class_id", "exam_node_id")}
    sql = "SELECT a.id FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id WHERE a.import_batch_id=? AND a.grading_status='pending'"; args: List[Any] = [batch_id]
    for column, key in (("s.class_id", "class_id"), ("a.exam_node_id", "exam_node_id")):
        if filters[key]:
            sql += f" AND {column}=?"; args.append(filters[key])
    sql += " ORDER BY a.student_id, a.exam_node_id, a.question_id, a.revision DESC"
    ids = [row["id"] for row in db.q(sql, tuple(args))]
    if not ids:
        raise HTTPException(409, "该批次当前没有待判题作答")
    running = db.q1("SELECT id FROM teacher_grade_jobs WHERE batch_id=? AND status IN ('running','queued','pausing') ORDER BY created_at DESC LIMIT 1", (batch_id,))
    if running:
        raise HTTPException(409, f"该批次已有运行中的任务 {running['id']}，请先暂停或等待完成")
    job_id = db.new_id("job")
    context = {name: str(payload.get(name) or "") for name in ("textbook_course_id", "textbook_unit_id", "textbook_context")}
    db.x("INSERT INTO teacher_grade_jobs (id,status,batch_id,total,processed,completed,failed,needs_review,current_answer_id,filters_json,textbook_course_id,textbook_unit_id,textbook_context,provider,model,prompt_version,error,created_at,updated_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (job_id, "queued", batch_id, len(ids), 0, 0, 0, 0, "", json.dumps(filters, ensure_ascii=False), context["textbook_course_id"], context["textbook_unit_id"], context["textbook_context"], "deepseek", "deepseek-chat", "teacher-grade-v3", "", now(), now(), ""))
    for answer_id in ids:
        db.x("INSERT OR IGNORE INTO teacher_grade_job_items (id,job_id,answer_id,status,updated_at) VALUES (?,?,?,?,?)", (db.new_id("jobitem"), job_id, answer_id, "pending", now()))
    background.add_task(run_job, job_id)
    return {"job": db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_id,))}


@router.post("/grade/jobs/{job_id}/pause")
def pause_job(job_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    db.x("UPDATE teacher_grade_jobs SET pause_requested=1, updated_at=? WHERE id=?", (now(), job_id))
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
    db.x("UPDATE teacher_grade_jobs SET pause_requested=0, cancellation_requested=0, status='queued', resumed_at=?, updated_at=? WHERE id=?", (now(), now(), job_id))
    background.add_task(run_job, job_id)
    return {"job": db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_id,))}


@router.post("/grade/jobs/{job_id}/retry")
def retry_job(job_id: str, background: BackgroundTasks, user: Dict[str, Any] = Depends(current_user), payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    require_teacher(user); init()
    if not default_llm_key():
        raise HTTPException(503, "未配置 DEEPSEEK_API_KEY，管理员配置后才能判题")
    old = db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_id,))
    if not old:
        raise HTTPException(404, "任务不存在")
    failed = [row["answer_id"] for row in db.q("SELECT answer_id FROM teacher_grade_job_items WHERE job_id=? AND status='failed'", (job_id,))]
    if not failed:
        raise HTTPException(409, "没有失败项")
    new_id = db.new_id("job")
    context = {name: str(old.get(name) or "") for name in ("batch_id", "textbook_course_id", "textbook_unit_id", "textbook_context")}
    db.x("INSERT INTO teacher_grade_jobs (id,status,batch_id,total,processed,completed,failed,needs_review,current_answer_id,filters_json,textbook_course_id,textbook_unit_id,textbook_context,provider,model,prompt_version,retry_of,error,created_at,updated_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (new_id, "queued", context["batch_id"], len(failed), 0, 0, 0, 0, "", old.get("filters_json") or "{}", context["textbook_course_id"], context["textbook_unit_id"], context["textbook_context"], old.get("provider") or "deepseek", old.get("model") or "deepseek-chat", old.get("prompt_version") or "teacher-grade-v3", job_id, "", now(), now(), ""))
    for answer_id in failed:
        db.x("INSERT OR IGNORE INTO teacher_grade_job_items (id,job_id,answer_id,status,updated_at) VALUES (?,?,?,?,?)", (db.new_id("jobitem"), new_id, answer_id, "pending", now()))
    background.add_task(run_job, new_id)
    return {"job": db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (new_id,))}

@router.get("/grade/jobs")
def jobs(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); require_teacher(user)
    return {"items": db.q("SELECT * FROM teacher_grade_jobs ORDER BY created_at DESC LIMIT 50")}


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
    rows = db.q("SELECT * FROM teacher_batches ORDER BY created_at DESC")
    items = []
    for batch in rows:
        stats = db.q1("SELECT COUNT(*) total, SUM(CASE WHEN grading_status='pending' THEN 1 ELSE 0 END) pending, SUM(CASE WHEN grading_status='graded' THEN 1 ELSE 0 END) graded, SUM(CASE WHEN grading_status IN ('failed') THEN 1 ELSE 0 END) failed FROM teacher_answers WHERE import_batch_id=?", (batch["id"],)) or {}
        people = db.q1("SELECT COUNT(DISTINCT a.student_id) students, COUNT(DISTINCT s.class_id) classes, COUNT(DISTINCT a.question_id) questions FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id WHERE a.import_batch_id=?", (batch["id"],)) or {}
        job = db.q1("SELECT * FROM teacher_grade_jobs WHERE batch_id=? ORDER BY created_at DESC LIMIT 1", (batch["id"],))
        review = db.q1("SELECT COUNT(*) n FROM teacher_grade_runs r JOIN teacher_answers a ON a.id=r.answer_id WHERE a.import_batch_id=? AND r.needs_review=1", (batch["id"],)) or {}
        items.append({**batch, **stats, **people, "needs_review": int(review.get("n") or 0), "latest_job": job, "task_status": (job or {}).get("status") or "imported"})
    return {"items": items}

@router.get("/batches/{batch_id}")
def batch_detail(batch_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    batch = db.q1("SELECT * FROM teacher_batches WHERE id=?", (batch_id,))
    if not batch:
        raise HTTPException(404, "导入批次不存在")
    stats = db.q1("SELECT COUNT(*) total, SUM(CASE WHEN grading_status='pending' THEN 1 ELSE 0 END) pending, SUM(CASE WHEN grading_status='graded' THEN 1 ELSE 0 END) graded, SUM(CASE WHEN grading_status='failed' THEN 1 ELSE 0 END) failed, SUM(CASE WHEN grading_status='needs_review' THEN 1 ELSE 0 END) review FROM teacher_answers WHERE import_batch_id=?", (batch_id,)) or {}
    classes = db.q("SELECT c.id,c.code,COUNT(DISTINCT s.id) students,COUNT(a.id) answers,SUM(CASE WHEN a.grading_status='pending' THEN 1 ELSE 0 END) pending,SUM(CASE WHEN a.grading_status='graded' THEN 1 ELSE 0 END) graded,SUM(CASE WHEN a.grading_status='failed' THEN 1 ELSE 0 END) failed FROM teacher_classes c JOIN teacher_students s ON s.class_id=c.id LEFT JOIN teacher_answers a ON a.student_id=s.id AND a.import_batch_id=? GROUP BY c.id ORDER BY c.code", (batch_id,))
    job = db.q1("SELECT * FROM teacher_grade_jobs WHERE batch_id=? ORDER BY created_at DESC LIMIT 1", (batch_id,))
    return {"batch": {**batch, **stats}, "classes": classes, "jobs": db.q("SELECT * FROM teacher_grade_jobs WHERE batch_id=? ORDER BY created_at DESC", (batch_id,)), "latest_job": job}

@router.get("/batches/{batch_id}/classes/{class_id}/students")
def batch_students(batch_id: str, class_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    rows = db.q("SELECT s.id,s.code,s.class_id,c.code class_code,COUNT(a.id) total,SUM(CASE WHEN a.grading_status='pending' THEN 1 ELSE 0 END) pending,SUM(CASE WHEN a.grading_status='graded' THEN 1 ELSE 0 END) graded,SUM(CASE WHEN a.grading_status='failed' THEN 1 ELSE 0 END) failed FROM teacher_students s JOIN teacher_classes c ON c.id=s.class_id LEFT JOIN teacher_answers a ON a.student_id=s.id AND a.import_batch_id=? WHERE s.class_id=? GROUP BY s.id ORDER BY s.code", (batch_id, class_id))
    return {"items": rows}

@router.get("/batches/{batch_id}/classes/{class_id}/students/{student_id}/questions")
def batch_student_questions(batch_id: str, class_id: str, student_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    rows = _selected_answers("", class_id, student_id)
    rows = [row for row in rows if row.get("import_batch_id") == batch_id]
    for row in rows:
        row["score"] = None if row.get("score") is None else row.get("score")
    return {"items": rows}

@router.get("/batches/{batch_id}/overview")
def batch_overview(batch_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    batch = db.q1("SELECT * FROM teacher_batches WHERE id=?", (batch_id,))
    if not batch:
        raise HTTPException(404, "导入批次不存在")
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
        score = sum(row["score"] for row in rows if row["score"] is not None); max_score = sum(row["graded_max_score"] for row in rows)
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
        raise HTTPException(404, "该作答不属于当前批次")
    return answer_detail(answer_id, user)

@router.get("/audit")
def audit(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    batch = db.q1("SELECT * FROM teacher_batches ORDER BY created_at DESC LIMIT 1") or {}
    counts = json.loads(batch.get("counts_json") or "{}")
    if "answers" in counts:
        counts["answers_source"] = int(counts.get("answers") or 0) + int(counts.get("skipped") or 0)
    db_counts = {"students": db.q1("SELECT COUNT(*) n FROM teacher_students")["n"], "classes": db.q1("SELECT COUNT(*) n FROM teacher_classes")["n"], "questions": db.q1("SELECT COUNT(*) n FROM teacher_questions")["n"], "exams": db.q1("SELECT COUNT(*) n FROM teacher_exams")["n"], "answers": db.q1("SELECT COUNT(*) n FROM teacher_answers")["n"]}
    classes = db.q("SELECT c.code, c.id, (SELECT COUNT(*) FROM teacher_students s WHERE s.class_id=c.id) students, (SELECT COUNT(*) FROM teacher_answers a JOIN teacher_students s2 ON s2.id=a.student_id WHERE s2.class_id=c.id) answers FROM teacher_classes c ORDER BY c.code")
    status = {row["score_status"]: row["n"] for row in db.q("SELECT score_status, COUNT(*) n FROM teacher_answers GROUP BY score_status")}
    return {"batch": {"id": batch.get("id"), "filename": batch.get("filename"), "created_at": batch.get("created_at"), "counts": counts, "errors": json.loads(batch.get("errors_json") or "[]")}, "database": db_counts, "classes": classes, "status": status}

def _selected_answers(exam_node_id: str = "", class_id: str = "", student_id: str = "", batch_id: str = "") -> List[Dict[str, Any]]:
    sql = "SELECT a.*, s.code student_code, s.class_id, c.code class_code, q.max_score question_max_score, q.question_type FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id LEFT JOIN teacher_classes c ON c.id=s.class_id JOIN teacher_questions q ON q.id=a.question_id WHERE 1=1"; args: List[Any] = []
    if batch_id:
        sql += " AND a.import_batch_id=?"; args.append(batch_id)
    for column, value in (("a.exam_node_id", exam_node_id), ("s.class_id", class_id), ("a.student_id", student_id)):
        if value:
            sql += f" AND {column}=?"; args.append(value)
    rows = db.q(sql + " ORDER BY a.student_id, a.event_id, a.revision DESC, a.submit_time DESC", tuple(args))
    latest: Dict[tuple, Dict[str, Any]] = {}
    for row in rows:
        key = (row["student_id"], row["event_id"])
        if key not in latest:
            latest[key] = row
    result = []
    for row in latest.values():
        grade = db.q1("SELECT * FROM teacher_grade_runs WHERE answer_id=? ORDER BY grading_version DESC, created_at DESC LIMIT 1", (row["id"],))
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
    for row in _selected_answers(exam_node_id, class_id, student_id):
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
    init()
    if not teacher(user):
        mine = student_for(user)
        student_id = mine["id"] if mine else ""
    return {"items": _personal_rows(exam_node_id, class_id, student_id, str(batch_id or ''))}

@router.get("/scores/classes")
def class_scores(exam_node_id: str = "", class_id: str = "", batch_id: str = "", user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
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
    init(); sql = "SELECT g.*, a.original_answer, a.score_status, s.code student_code, s.class_id, c.code class_code, q.stem, q.question_type FROM teacher_grade_runs g JOIN teacher_answers a ON a.id=g.answer_id JOIN teacher_students s ON s.id=g.student_id LEFT JOIN teacher_classes c ON c.id=s.class_id JOIN teacher_questions q ON q.id=g.question_id WHERE g.id = (SELECT g2.id FROM teacher_grade_runs g2 WHERE g2.answer_id=g.answer_id ORDER BY g2.grading_version DESC, g2.created_at DESC LIMIT 1)"; args: List[Any] = []
    if not teacher(user):
        student = student_for(user)
        sql += " AND g.student_id=?"; args.append(student["id"] if student else "")
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
    question = db.q1("SELECT * FROM teacher_questions WHERE id=?", (grade["question_id"],)) or {}
    standard = db.q1("SELECT * FROM teacher_standards WHERE id=?", (grade.get("standard_id"),)) or {}
    return {"grade": grade, "answer": answer, "question": question, "standard": standard, "revisions": db.q("SELECT * FROM teacher_answers WHERE event_id=? ORDER BY revision", (answer.get("event_id"),)), "history": db.q("SELECT * FROM teacher_grade_runs WHERE answer_id=? ORDER BY grading_version DESC", (grade["answer_id"],)), "corrections": db.q("SELECT * FROM teacher_corrections WHERE grade_run_id=? ORDER BY created_at DESC", (grade_id,))}


@router.get("/imports")
def imports(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); require_teacher(user)
    return {"items": db.q("SELECT * FROM teacher_batches ORDER BY created_at DESC LIMIT 50")}


@router.get("/imports/{batch_id}")
def import_detail(batch_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); require_teacher(user)
    row = db.q1("SELECT * FROM teacher_batches WHERE id=?", (batch_id,))
    if not row:
        raise HTTPException(404, "导入批次不存在")
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
    return import_rows(tables, ", ".join(meta))


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
    score = max(0.0, min(float(before.get("max_score") or 0), float(payload.get("score") or 0)))
    after = dict(before)
    after["score"] = score
    after["feedback"] = str(payload.get("feedback") or "")
    after["status"] = "manual_corrected"
    after["needs_review"] = 0
    db.x("UPDATE teacher_grade_runs SET score=?, feedback=?, status='manual_corrected', needs_review=0 WHERE id=?", (score, after["feedback"], grade_id))
    db.x("INSERT INTO teacher_corrections (id,grade_run_id,before_json,after_json,note,teacher_id,created_at) VALUES (?,?,?,?,?,?,?)", (db.new_id("correction"), grade_id, json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), note, user["id"], now()))
    return {"grade": db.q1("SELECT * FROM teacher_grade_runs WHERE id=?", (grade_id,)), "correction": db.q1("SELECT * FROM teacher_corrections WHERE grade_run_id=? ORDER BY created_at DESC LIMIT 1", (grade_id,))}
