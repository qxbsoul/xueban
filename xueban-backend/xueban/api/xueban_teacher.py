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
 original_answer TEXT, solution_process TEXT, original_score REAL, max_score REAL, score_status TEXT,
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
            "teacher_answers": [("course_id", "TEXT"), ("standard_id", "TEXT")],
            "teacher_grade_runs": [("grader_version", "TEXT"), ("course_id", "TEXT"), ("standard_id", "TEXT"), ("standard_version", "TEXT"), ("scoring_rule_version", "TEXT"), ("textbook_course_id", "TEXT"), ("textbook_unit_id", "TEXT"), ("textbook_context", "TEXT"), ("needs_review", "INTEGER DEFAULT 0")],
            "teacher_grade_jobs": [("completed", "INTEGER DEFAULT 0"), ("needs_review", "INTEGER DEFAULT 0"), ("current_answer_id", "TEXT"), ("finished_at", "TEXT")],
        }
        for table, columns in migrations.items():
            existing = {row[1] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}
            for column, ddl in columns:
                if column not in existing:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        c.execute("""UPDATE teacher_answers
            SET course_id = COALESCE(course_id, (SELECT q.course_id FROM teacher_questions q WHERE q.id=teacher_answers.question_id)),
                standard_id = COALESCE(standard_id, (SELECT q.standard_id FROM teacher_questions q WHERE q.id=teacher_answers.question_id))
            WHERE course_id IS NULL OR standard_id IS NULL""")
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
            db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,submit_time,original_answer,solution_process,original_score,max_score,score_status,raw_json,import_batch_id,exam_node_id,course_id,standard_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"{event}-r{revision}", student["id"], qid, event, revision, row.get("模拟提交时间"), row.get("学生最终作答"), row.get("解答过程"), float(row.get("得分") or 0), float(row.get("满分") or 0), score_status(row.get("评分状态") or ""), json.dumps(row, ensure_ascii=False), batch_id, row.get("节点编号"), "", ""))
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
        return {"student": True, "answers": db.q1("SELECT COUNT(*) n FROM teacher_answers WHERE student_id=?", (student["id"],))["n"], "pending": db.q1("SELECT COUNT(*) n FROM teacher_answers WHERE student_id=? AND score_status='pending'", (student["id"],))["n"], "review": db.q1("SELECT COUNT(*) n FROM teacher_grade_runs WHERE student_id=? AND needs_review=1", (student["id"],))["n"]}
    return {"students": db.q1("SELECT COUNT(*) n FROM teacher_students")["n"], "classes": db.q1("SELECT COUNT(*) n FROM teacher_classes")["n"], "courses": db.q1("SELECT COUNT(*) n FROM teacher_courses")["n"], "questions": db.q1("SELECT COUNT(*) n FROM teacher_questions")["n"], "answers": db.q1("SELECT COUNT(*) n FROM teacher_answers")["n"], "pending": db.q1("SELECT COUNT(*) n FROM teacher_answers WHERE score_status='pending'")["n"], "graded": db.q1("SELECT COUNT(*) n FROM teacher_grade_runs")["n"], "review": db.q1("SELECT COUNT(*) n FROM teacher_grade_runs WHERE needs_review=1")["n"], "jobs_running": db.q1("SELECT COUNT(*) n FROM teacher_grade_jobs WHERE status IN ('queued','running')")["n"], "recent_batches": db.q("SELECT * FROM teacher_batches ORDER BY created_at DESC LIMIT 5"), "recent_jobs": db.q("SELECT * FROM teacher_grade_jobs ORDER BY created_at DESC LIMIT 5")}


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
    return {"items": db.q("SELECT s.*, (SELECT COUNT(*) FROM teacher_answers a WHERE a.student_id=s.id) answers, (SELECT COUNT(*) FROM teacher_answers a WHERE a.student_id=s.id AND a.score_status='pending') pending FROM teacher_students s WHERE s.class_id=? ORDER BY s.code", (class_id,))}


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
        sql += " AND a.score_status=?"; args.append(status)
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
    db.x("UPDATE teacher_answers SET score_status='graded' WHERE id=?", (answer["id"],))
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


def run_job(job_id: str, ids: List[str]) -> None:
    key = _JOB_KEYS.pop(job_id, "")
    context = _JOB_CONTEXTS.pop(job_id, {})
    completed = failed = review = 0
    db.x("UPDATE teacher_grade_jobs SET status='running', current_answer_id=?, updated_at=? WHERE id=?", (ids[0] if ids else "", now(), job_id))
    for index, answer_id in enumerate(ids):
        try:
            result = grade_answer(answer_id, key, "deepseek", context.get("textbook_course_id", ""), context.get("textbook_unit_id", ""), context.get("textbook_context", ""))
            grade = result.get("grade") or {}
            if grade.get("status") == "failed" or grade.get("is_correct") == "failed":
                failed += 1
                if grade.get("needs_review"):
                    review += 1
                db.x("INSERT INTO teacher_grade_job_items (id,job_id,answer_id,status,grade_id,error,updated_at) VALUES (?,?,?,?,?,?,?)", (db.new_id("jobitem"), job_id, answer_id, "failed", grade.get("id"), grade.get("judgment_reason") or "model_error", now()))
            else:
                completed += 1
                if grade.get("needs_review"):
                    review += 1
                db.x("INSERT INTO teacher_grade_job_items (id,job_id,answer_id,status,grade_id,error,updated_at) VALUES (?,?,?,?,?,?,?)", (db.new_id("jobitem"), job_id, answer_id, "completed", grade.get("id"), "", now()))
        except Exception as exc:
            failed += 1
            db.x("INSERT INTO teacher_grade_job_items (id,job_id,answer_id,status,grade_id,error,updated_at) VALUES (?,?,?,?,?,?,?)", (db.new_id("jobitem"), job_id, answer_id, "failed", "", str(exc)[:300], now()))
        db.x("UPDATE teacher_grade_jobs SET processed=?, completed=?, failed=?, needs_review=?, current_answer_id=?, updated_at=? WHERE id=?", (index + 1, completed, failed, review, answer_id, now(), job_id))
    final_status = "completed" if failed == 0 else ("failed" if completed == 0 else "partially_failed")
    db.x("UPDATE teacher_grade_jobs SET status=?, finished_at=?, current_answer_id='', updated_at=? WHERE id=?", (final_status, now(), now(), job_id))


@router.post("/grade/batch")
def batch(background: BackgroundTasks, user: Dict[str, Any] = Depends(current_user), payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    require_teacher(user); init()
    if not str(payload.get("llm_key") or "") and not default_llm_key():
        raise HTTPException(503, "未配置 DEEPSEEK_API_KEY，管理员配置后才能判题")
    filters = {name: str(payload.get(name) or "") for name in ("class_id", "course_id", "standard_id", "exam_node_id")}
    status_filter = str(payload.get("status") or "pending")
    sql = "SELECT a.id FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id JOIN teacher_questions q ON q.id=a.question_id WHERE 1=1"; args: List[Any] = []
    if status_filter:
        sql += " AND a.score_status=?"; args.append(status_filter)
    for column, key in (("s.class_id", "class_id"), ("q.course_id", "course_id"), ("q.standard_id", "standard_id"), ("a.exam_node_id", "exam_node_id")):
        if filters[key]:
            sql += f" AND {column}=?"; args.append(filters[key])
    sql += " ORDER BY a.submit_time"
    ids = [row["id"] for row in db.q(sql, tuple(args))]
    job_id = db.new_id("job")
    db.x("INSERT INTO teacher_grade_jobs (id,status,total,processed,completed,failed,needs_review,current_answer_id,filters_json,error,created_at,updated_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (job_id, "queued", len(ids), 0, 0, 0, 0, "", json.dumps(filters, ensure_ascii=False), "", now(), now(), ""))
    _JOB_KEYS[job_id] = str(payload.get("llm_key") or default_llm_key())
    _JOB_CONTEXTS[job_id] = {name: str(payload.get(name) or "") for name in ("textbook_course_id", "textbook_unit_id", "textbook_context")}
    background.add_task(run_job, job_id, ids)
    return {"job": db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_id,))}


@router.post("/grade/jobs/{job_id}/retry")
def retry_job(job_id: str, background: BackgroundTasks, user: Dict[str, Any] = Depends(current_user), payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    require_teacher(user); init()
    if not str(payload.get("llm_key") or "") and not default_llm_key():
        raise HTTPException(503, "未配置 DEEPSEEK_API_KEY，管理员配置后才能判题")
    failed = [row["answer_id"] for row in db.q("SELECT answer_id FROM teacher_grade_job_items WHERE job_id=? AND status='failed'", (job_id,))]
    new_id = db.new_id("job")
    db.x("INSERT INTO teacher_grade_jobs (id,status,total,processed,completed,failed,needs_review,current_answer_id,filters_json,error,created_at,updated_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (new_id, "queued", len(failed), 0, 0, 0, 0, "", json.dumps({"retry_of": job_id}, ensure_ascii=False), "", now(), now(), ""))
    _JOB_KEYS[new_id] = str(payload.get("llm_key") or default_llm_key())
    _JOB_CONTEXTS[new_id] = {name: str(payload.get(name) or "") for name in ("textbook_course_id", "textbook_unit_id", "textbook_context")}
    background.add_task(run_job, new_id, failed)
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


@router.get("/results")
def results(course_id: str = "", standard_id: str = "", class_id: str = "", student_id: str = "", exam_node_id: str = "", question_id: str = "", status: str = "", review: str = "", limit: int = 500, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); sql = "SELECT g.*, a.original_answer, a.score_status, s.code student_code, s.class_id, c.code class_code, q.stem, q.question_type FROM teacher_grade_runs g JOIN teacher_answers a ON a.id=g.answer_id JOIN teacher_students s ON s.id=g.student_id LEFT JOIN teacher_classes c ON c.id=s.class_id JOIN teacher_questions q ON q.id=g.question_id WHERE 1=1"; args: List[Any] = []
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
