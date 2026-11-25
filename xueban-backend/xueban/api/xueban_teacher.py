"""教师端：赛题数据导入、班级/学生、原始作答与判题。"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Body, Depends, File, Form, HTTPException, UploadFile

from ..services import xueban_store as db
from .xueban_auth import current_user

router = APIRouter(prefix="/api/teacher", tags=["teacher"])
_LOCK = threading.RLock()
_DATA_ROOT = Path(os.environ.get("XUEBAN_TEACHER_DATA") or Path(__file__).resolve().parents[3] / "test" / "4_赛题数据" / "模拟学情数据")
SCHEMA = """
CREATE TABLE IF NOT EXISTS teacher_classes (id TEXT PRIMARY KEY, code TEXT UNIQUE, name TEXT, grade INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS teacher_students (id TEXT PRIMARY KEY, user_id TEXT UNIQUE, code TEXT UNIQUE, class_id TEXT, name TEXT, grade INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS teacher_questions (id TEXT PRIMARY KEY, topic_id TEXT, material_type TEXT, question_type TEXT, stem TEXT, options TEXT, reference_answer TEXT, reference_solution TEXT, max_score REAL, scoring_points TEXT, indicator_id TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS teacher_exams (id TEXT PRIMARY KEY, exam_date TEXT, topics TEXT, description TEXT, available_questions TEXT);
CREATE TABLE IF NOT EXISTS teacher_batches (id TEXT PRIMARY KEY, filename TEXT, status TEXT, counts_json TEXT, errors_json TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS teacher_answers (
 id TEXT PRIMARY KEY, student_id TEXT, question_id TEXT, event_id TEXT, revision INTEGER, submit_time TEXT,
 original_answer TEXT, solution_process TEXT, original_score REAL, max_score REAL, score_status TEXT,
 raw_json TEXT, import_batch_id TEXT, exam_node_id TEXT, UNIQUE(event_id, revision)
);
CREATE TABLE IF NOT EXISTS teacher_grade_runs (
 id TEXT PRIMARY KEY, answer_id TEXT, student_id TEXT, question_id TEXT, event_id TEXT, revision INTEGER,
 grading_version INTEGER, grading_model TEXT, grading_prompt_version TEXT, score REAL, max_score REAL,
 is_correct TEXT, step_results_json TEXT, error_type TEXT, feedback TEXT, judgment_reason TEXT, status TEXT,
 raw_response TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS teacher_grade_jobs (
 id TEXT PRIMARY KEY, status TEXT, total INTEGER, processed INTEGER, failed INTEGER, filters_json TEXT,
 error TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS teacher_corrections (
 id TEXT PRIMARY KEY, grade_run_id TEXT, before_json TEXT, after_json TEXT, note TEXT, teacher_id TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_teacher_answers_student ON teacher_answers(student_id, question_id);
CREATE INDEX IF NOT EXISTS idx_teacher_answers_event ON teacher_answers(event_id, revision);
CREATE INDEX IF NOT EXISTS idx_teacher_runs_answer ON teacher_grade_runs(answer_id, created_at);
CREATE INDEX IF NOT EXISTS idx_teacher_students_class ON teacher_students(class_id);
"""

def init() -> None:
    db.init()
    with _LOCK:
        c = db.conn()
        c.executescript(SCHEMA)
        c.commit()

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())

def _student_user(code: str) -> Optional[Dict[str, Any]]:
    return db.q1("SELECT * FROM xueban_users WHERE email = ? AND COALESCE(role,'teacher')='student'", (f"{code.lower()}@xueban.local",))

def _ensure_account(email: str, password: str, display: str, role: str) -> Dict[str, Any]:
    import hashlib, secrets
    row = db.q1("SELECT * FROM xueban_users WHERE email = ?", (email,))
    if row:
        db.x("UPDATE xueban_users SET role = ?, display_name = ? WHERE id = ?", (role, display, row["id"]))
        return db.q1("SELECT * FROM xueban_users WHERE id = ?", (row["id"],))
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
    uid = db.new_id(role)
    db.x("INSERT INTO xueban_users (id,email,password_hash,password_salt,display_name,auth_type,role,created_at) VALUES (?,?,?,?,?,?,?,?)",
         (uid, email, digest, salt, display, "password", role, _now()))
    return db.q1("SELECT * FROM xueban_users WHERE id = ?", (uid,))

def _read_csvs(files: Optional[List[UploadFile]]) -> Dict[str, List[Dict[str, str]]]:
    result: Dict[str, List[Dict[str, str]]] = {}
    if not files:
        for path in _DATA_ROOT.glob("*.csv"):
            with path.open(encoding="utf-8-sig", newline="") as handle:
                result[path.name] = list(csv.DictReader(handle))
        return result
    for upload in files:
        raw = upload.file.read()
        text = raw.decode("utf-8-sig")
        result[upload.filename or "upload.csv"] = list(csv.DictReader(io.StringIO(text)))
    return result

def _class_code(row: Dict[str, str]) -> str:
    return (row.get("模拟班级编号") or row.get("班级编号") or "DC01").strip()

def _score_status(value: str) -> str:
    value = (value or "").strip()
    if value in {"未作答", "空", "未提交"}: return "unanswered"
    if value in {"待评分", "待判题"}: return "pending"
    if value in {"已评分", "已判题"}: return "graded"
    return "pending"

def _ensure_import_tables() -> None:
    init()

def import_rows(tables: Dict[str, List[Dict[str, str]]], filename: str = "seed") -> Dict[str, Any]:
    _ensure_import_tables()
    students = next((v for k, v in tables.items() if "模拟学生" in k), [])
    questions = next((v for k, v in tables.items() if "题目" in k), [])
    answers = next((v for k, v in tables.items() if "作答" in k), [])
    requirements = next((v for k, v in tables.items() if "课程要求" in k), [])
    exams = next((v for k, v in tables.items() if "测验" in k), [])
    errors: List[Dict[str, Any]] = []
    batch_id = db.new_id("batch")
    counts = {"students": 0, "classes": 0, "questions": 0, "answers": 0, "requirements": 0, "exams": 0}
    with _LOCK:
        for row in students:
            code = (row.get("学生编号") or "").strip()
            if not code:
                errors.append({"row": row, "error": "缺少学生编号"}); continue
            class_code = _class_code(row)
            class_id = f"class_{class_code}"
            db.x("INSERT OR IGNORE INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", (class_id, class_code, f"{class_code} 班", int(row.get("年级") or 7), _now()))
            user = _ensure_account(f"{code.lower()}@xueban.local", "Student@1234", code, "student")
            db.x("INSERT OR REPLACE INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", (f"student_{code}", user["id"], code, class_id, code, int(row.get("年级") or 7), _now()))
            counts["students"] += 1
        counts["classes"] = len({(r.get("模拟班级编号") or "DC01") for r in students})
        for row in questions:
            qid = (row.get("材料编号") or "").strip()
            if not qid:
                errors.append({"row": row, "error": "缺少材料编号"}); continue
            db.x("INSERT OR REPLACE INTO teacher_questions (id,topic_id,material_type,question_type,stem,options,reference_answer,reference_solution,max_score,scoring_points,indicator_id,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                qid, row.get("主题编号"), row.get("材料类型"), row.get("题型"), row.get("题干或内容"), row.get("选项"), row.get("参考答案"), row.get("参考解析"), float(row.get("满分") or 0), row.get("评分点"), row.get("指标编号"), row.get("来源或资料文件")))
            counts["questions"] += 1
        for row in requirements:
            counts["requirements"] += 1
        for row in exams:
            nid = (row.get("节点编号") or "").strip()
            if nid:
                db.x("INSERT OR REPLACE INTO teacher_exams (id,exam_date,topics,description,available_questions) VALUES (?,?,?,?,?)", (nid, row.get("模拟测验日期"), row.get("已教及测量主题"), row.get("说明"), row.get("可用题目编号")))
                counts["exams"] += 1
        for row in answers:
            event = (row.get("作答事件编号") or "").strip()
            if not event:
                errors.append({"row": row, "error": "缺少作答事件编号"}); continue
            code = (row.get("学生编号") or "").strip(); qid = (row.get("材料编号") or "").strip()
            student = db.q1("SELECT id FROM teacher_students WHERE code = ?", (code,))
            if not student:
                errors.append({"row": row, "error": f"未找到学生 {code}"}); continue
            if not db.q1("SELECT id FROM teacher_questions WHERE id = ?", (qid,)):
                errors.append({"row": row, "error": f"未找到题目 {qid}"}); continue
            revision = int(row.get("修订版本") or 1)
            status = _score_status(row.get("评分状态") or "")
            raw = json.dumps(row, ensure_ascii=False)
            db.x("INSERT OR IGNORE INTO teacher_answers (id,student_id,question_id,event_id,revision,submit_time,original_answer,solution_process,original_score,max_score,score_status,raw_json,import_batch_id,exam_node_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                f"{event}-r{revision}", student["id"], qid, event, revision, row.get("模拟提交时间"), row.get("学生最终作答"), row.get("解答过程"), float(row.get("得分") or 0), float(row.get("满分") or 0), status, raw, batch_id, row.get("节点编号")))
            counts["answers"] += 1
        db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,created_at) VALUES (?,?,?,?,?,?)", (batch_id, filename, "committed", json.dumps(counts, ensure_ascii=False), json.dumps(errors, ensure_ascii=False), _now()))
    return {"batch_id": batch_id, "counts": counts, "errors": errors[:50]}

def _teacher(user: Dict[str, Any]) -> bool:
    return (user.get("role") or "teacher") == "teacher"

def _visible_answer(answer: Dict[str, Any], user: Dict[str, Any]) -> bool:
    if _teacher(user): return True
    student = db.q1("SELECT id FROM teacher_students WHERE user_id = ?", (user["id"],))
    return bool(student and answer.get("student_id") == student["id"])

@router.post("/bootstrap")
def bootstrap() -> Dict[str, Any]:
    """本地首次初始化演示教师/学生账号与赛题数据；不用于生产身份认证。"""
    init()
    _ensure_account("teacher@xueban.local", "Teacher@1234", "林老师", "teacher")
    tables = _read_csvs(None)
    return import_rows(tables, "赛题数据/模拟学情数据")

@router.post("/seed")
def seed(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    if not _teacher(user): raise HTTPException(403, "仅教师可操作")
    init()
    teacher = _ensure_account("teacher@xueban.local", "Teacher@1234", "林老师", "teacher")
    tables = _read_csvs(None)
    result = import_rows(tables, "赛题数据/模拟学情数据")
    return {"teacher": {"email": teacher["email"], "display_name": teacher["display_name"]}, **result}

@router.get("/summary")
def summary(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init()
    if not _teacher(user):
        student = db.q1("SELECT * FROM teacher_students WHERE user_id = ?", (user["id"],))
        if not student: return {"student": True, "answers": 0, "pending": 0}
        return {"student": True, "answers": db.q1("SELECT COUNT(*) n FROM teacher_answers WHERE student_id=?", (student["id"],))["n"], "pending": db.q1("SELECT COUNT(*) n FROM teacher_answers WHERE student_id=? AND score_status='pending'", (student["id"],))["n"]}
    return {
        "students": db.q1("SELECT COUNT(*) n FROM teacher_students")["n"],
        "classes": db.q1("SELECT COUNT(*) n FROM teacher_classes")["n"],
        "questions": db.q1("SELECT COUNT(*) n FROM teacher_questions")["n"],
        "answers": db.q1("SELECT COUNT(*) n FROM teacher_answers")["n"],
        "pending": db.q1("SELECT COUNT(*) n FROM teacher_answers WHERE score_status='pending'")["n"],
        "graded": db.q1("SELECT COUNT(*) n FROM teacher_grade_runs")["n"],
        "recent_batches": db.q("SELECT * FROM teacher_batches ORDER BY created_at DESC LIMIT 5"),
    }

@router.get("/classes")
def classes(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); return {"items": db.q("SELECT c.*, (SELECT COUNT(*) FROM teacher_students s WHERE s.class_id=c.id) student_count FROM teacher_classes c ORDER BY c.code")}

@router.get("/classes/{class_id}/students")
def class_students(class_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); rows = db.q("SELECT s.*, (SELECT COUNT(*) FROM teacher_answers a WHERE a.student_id=s.id) answers, (SELECT COUNT(*) FROM teacher_answers a WHERE a.student_id=s.id AND a.score_status='pending') pending FROM teacher_students s WHERE s.class_id=? ORDER BY s.code", (class_id,))
    return {"items": rows}

@router.get("/students/{student_id}")
def student_detail(student_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); student = db.q1("SELECT * FROM teacher_students WHERE id=?", (student_id,))
    if not student: raise HTTPException(404, "学生不存在")
    if not _teacher(user) and student["user_id"] != user["id"]: raise HTTPException(403, "不能查看其他学生")
    answers = db.q("SELECT a.*, q.stem, q.question_type FROM teacher_answers a JOIN teacher_questions q ON q.id=a.question_id WHERE a.student_id=? ORDER BY a.submit_time DESC", (student_id,))
    return {"student": student, "answers": answers}

@router.get("/answers")
def answers(status: str = "", class_id: str = "", limit: int = 100, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); sql = "SELECT a.*, s.code student_code, q.stem, q.question_type FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id JOIN teacher_questions q ON q.id=a.question_id WHERE 1=1"; args: List[Any] = []
    if not _teacher(user):
        student = db.q1("SELECT id FROM teacher_students WHERE user_id=?", (user["id"],)); sql += " AND a.student_id=?"; args.append(student["id"] if student else "")
    if status: sql += " AND a.score_status=?"; args.append(status)
    if class_id: sql += " AND s.class_id=?"; args.append(class_id)
    sql += " ORDER BY a.submit_time DESC LIMIT ?"; args.append(max(1, min(limit, 500)))
    return {"items": db.q(sql, tuple(args))}

@router.get("/answers/{answer_id}")
def answer_detail(answer_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); answer = db.q1("SELECT * FROM teacher_answers WHERE id=?", (answer_id,))
    if not answer: raise HTTPException(404, "作答不存在")
    if not _visible_answer(answer, user): raise HTTPException(403, "不能查看其他学生")
    question = db.q1("SELECT * FROM teacher_questions WHERE id=?", (answer["question_id"],))
    revisions = db.q("SELECT * FROM teacher_answers WHERE event_id=? ORDER BY revision", (answer["event_id"],))
    grades = db.q("SELECT * FROM teacher_grade_runs WHERE answer_id=? ORDER BY grading_version DESC, created_at DESC", (answer_id,))
    return {"answer": answer, "question": question, "revisions": revisions, "grades": grades}

def _split_steps(answer: Dict[str, Any]) -> List[str]:
    raw = (answer.get("solution_process") or "").strip()
    if not raw: return [f"最终作答：{answer.get('original_answer') or '未作答'}"]
    parts = [re.sub(r"^\s*\d+[.、)]\s*", "", line).strip() for line in raw.splitlines() if line.strip()]
    return parts or [raw]

def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).replace("＝", "=").replace("，", ",").lower()

def _llm_grade(answer: Dict[str, Any], question: Dict[str, Any], steps: List[str], llm_key: str, provider: str) -> Optional[Dict[str, Any]]:
    import urllib.request
    base = "https://api.deepseek.com" if provider == "deepseek" else "https://api.openai.com"
    model = "deepseek-chat" if provider == "deepseek" else "gpt-4o-mini"
    prompt = json.dumps({
        "题目": question.get("stem"), "参考答案": question.get("reference_answer"),
        "参考解析": question.get("reference_solution"), "评分点": question.get("scoring_points"),
        "学生最终作答": answer.get("original_answer"), "学生解题步骤": steps,
        "要求": "只判题，不做学情分析；逐步骤返回 step_index,student_step,expected_step,is_correct,score,max_score,error_type,judgment_reason；最后返回 score,max_score,is_correct,error_type,feedback,judgment_reason。输出 JSON。",
    }, ensure_ascii=False)
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0}, ensure_ascii=False).encode()
    request = urllib.request.Request(base + "/chat/completions", data=body, headers={"Content-Type": "application/json", "Authorization": f"Bearer {llm_key}"}, method="POST")
    with urllib.request.urlopen(request, timeout=45) as response:
        payload = json.loads(response.read().decode())
    content = payload["choices"][0]["message"]["content"]
    match = re.search(r"\{.*\}", content, re.S)
    return json.loads(match.group(0)) if match else None

def grade_answer(answer_id: str, llm_key: str = "", provider: str = "deepseek") -> Dict[str, Any]:
    init(); answer = db.q1("SELECT * FROM teacher_answers WHERE id=?", (answer_id,))
    if not answer: raise HTTPException(404, "作答不存在")
    question = db.q1("SELECT * FROM teacher_questions WHERE id=?", (answer["question_id"],))
    steps = _split_steps(answer)
    if llm_key:
        try:
            llm = _llm_grade(answer, question or {}, steps, llm_key, provider)
            if llm:
                max_score = float((question or {}).get("max_score") or answer.get("max_score") or 0)
                score = max(0.0, min(max_score, float(llm.get("score") or 0)))
                step_results = llm.get("step_results") or []
                status = "graded" if step_results else "needs_review"
                version = (db.q1("SELECT COALESCE(MAX(grading_version),0) n FROM teacher_grade_runs WHERE answer_id=?", (answer_id,))["n"] or 0) + 1
                db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,grading_prompt_version,score,max_score,is_correct,step_results_json,error_type,feedback,judgment_reason,status,raw_response,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (db.new_id("grade"), answer_id, answer["student_id"], answer["question_id"], answer["event_id"], answer["revision"], version, ("deepseek-chat" if provider == "deepseek" else "gpt-4o-mini"), "teacher-grade-v1", score, max_score, str(llm.get("is_correct") or "partial"), json.dumps(step_results, ensure_ascii=False), str(llm.get("error_type") or ""), str(llm.get("feedback") or ""), str(llm.get("judgment_reason") or ""), status, json.dumps(llm, ensure_ascii=False), _now()))
                if answer.get("score_status") == "pending": db.x("UPDATE teacher_answers SET score_status='graded' WHERE id=?", (answer_id,))
                return {"grade": db.q1("SELECT * FROM teacher_grade_runs WHERE answer_id=? ORDER BY grading_version DESC LIMIT 1", (answer_id,))}
        except Exception:
            pass
    expected = (question or {}).get("reference_answer") or ""
    final = answer.get("original_answer") or ""
    correct = bool(expected and _normalize(final) == _normalize(expected))
    max_score = float((question or {}).get("max_score") or answer.get("max_score") or 0)
    step_results = []
    for index, step in enumerate(steps):
        step_score = round(max_score / max(1, len(steps)), 2)
        if index == len(steps) - 1: step_score = max_score if correct else 0
        step_results.append({"step_index": index + 1, "student_step": step, "expected_step": expected if index == len(steps) - 1 else "", "is_correct": bool(index == len(steps) - 1 and correct), "score": step_score, "max_score": step_score, "error_type": "" if correct else "answer_mismatch", "judgment_reason": "最终答案与参考答案一致" if correct else "最终答案与参考答案不一致"})
    score = sum(item["score"] for item in step_results)
    status = "graded" if expected else "needs_review"
    run = {"id": db.new_id("grade"), "answer": answer, "question": question, "score": score, "max_score": max_score, "is_correct": "correct" if correct else "incorrect", "step_results": step_results, "status": status, "feedback": "答案正确" if correct else "答案与参考答案不一致，请核对解题过程。", "judgment_reason": "按参考答案与评分点进行规则辅助判题；无可用模型密钥时不伪造 AI 结论。"}
    version = (db.q1("SELECT COALESCE(MAX(grading_version),0) n FROM teacher_grade_runs WHERE answer_id=?", (answer_id,))["n"] or 0) + 1
    db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,grading_prompt_version,score,max_score,is_correct,step_results_json,error_type,feedback,judgment_reason,status,raw_response,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        run["id"], answer_id, answer["student_id"], answer["question_id"], answer["event_id"], answer["revision"], version, "rule-assisted" if not llm_key else provider, "teacher-grade-v1", score, max_score, run["is_correct"], json.dumps(step_results, ensure_ascii=False), "" if correct else "answer_mismatch", run["feedback"], run["judgment_reason"], status, "", _now()))
    if answer.get("score_status") == "pending": db.x("UPDATE teacher_answers SET score_status='graded' WHERE id=?", (answer_id,))
    return {"grade": db.q1("SELECT * FROM teacher_grade_runs WHERE id=?", (run["id"],))}

@router.post("/answers/{answer_id}/grade")
def grade(answer_id: str, user: Dict[str, Any] = Depends(current_user), payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    if not _teacher(user): raise HTTPException(403, "仅教师可判题")
    return grade_answer(answer_id, str(payload.get("llm_key") or ""), str(payload.get("provider") or "deepseek"))

def _run_job(job_id: str, ids: List[str]) -> None:
    failed = 0
    for index, answer_id in enumerate(ids):
        try: grade_answer(answer_id)
        except Exception: failed += 1
        db.x("UPDATE teacher_grade_jobs SET processed=?, failed=?, updated_at=? WHERE id=?", (index + 1, failed, _now(), job_id))
    db.x("UPDATE teacher_grade_jobs SET status='done', failed=?, updated_at=? WHERE id=?", (failed, _now(), job_id))

@router.post("/grade/batch")
def batch(background: BackgroundTasks, user: Dict[str, Any] = Depends(current_user), payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    if not _teacher(user): raise HTTPException(403, "仅教师可判题")
    init(); class_id = str(payload.get("class_id") or ""); limit = max(1, min(int(payload.get("limit") or 20), 50))
    sql = "SELECT a.id FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id WHERE a.score_status='pending'"; args=[]
    if class_id: sql += " AND s.class_id=?"; args.append(class_id)
    sql += " ORDER BY a.submit_time LIMIT ?"; args.append(limit)
    ids = [r["id"] for r in db.q(sql, tuple(args))]
    job_id = db.new_id("job")
    db.x("INSERT INTO teacher_grade_jobs (id,status,total,processed,failed,filters_json,error,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)", (job_id, "running", len(ids), 0, 0, json.dumps({"class_id": class_id}, ensure_ascii=False), "", _now(), _now()))
    background.add_task(_run_job, job_id, ids)
    return {"job": db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_id,))}

@router.get("/grade/jobs/{job_id}")
def job(job_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); row = db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_id,))
    if not row: raise HTTPException(404, "任务不存在")
    return {"job": row}

@router.get("/grade/jobs/{job_id}/failures")
def job_failures(job_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); return {"items": db.q("SELECT a.id,a.question_id,a.student_id FROM teacher_answers a WHERE a.score_status='pending' LIMIT 20")}

@router.post("/grades/{grade_id}/manual")
def manual(grade_id: str, user: Dict[str, Any] = Depends(current_user), payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    if not _teacher(user): raise HTTPException(403, "仅教师可修正")
    init(); before = db.q1("SELECT * FROM teacher_grade_runs WHERE id=?", (grade_id,))
    if not before: raise HTTPException(404, "判题结果不存在")
    score = float(payload.get("score") or 0); feedback = str(payload.get("feedback") or ""); note = str(payload.get("note") or "")
    db.x("UPDATE teacher_grade_runs SET score=?, feedback=?, status='manual_corrected' WHERE id=?", (score, feedback, grade_id))
    after = db.q1("SELECT * FROM teacher_grade_runs WHERE id=?", (grade_id,))
    db.x("INSERT INTO teacher_corrections (id,grade_run_id,before_json,after_json,note,teacher_id,created_at) VALUES (?,?,?,?,?,?,?)", (db.new_id("correction"), grade_id, json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), note, user["id"], _now()))
    return {"grade": after, "correction": db.q1("SELECT * FROM teacher_corrections WHERE grade_run_id=? ORDER BY created_at DESC LIMIT 1", (grade_id,))}

@router.get("/imports")
def imports(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); return {"items": db.q("SELECT * FROM teacher_batches ORDER BY created_at DESC LIMIT 20")}

@router.post("/imports/preview")
def preview(files: Optional[List[UploadFile]] = File(default=None), user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    init(); tables = _read_csvs(files); return {"tables": {k: len(v) for k, v in tables.items()}, "sample": {k: v[:3] for k, v in tables.items()}}

@router.post("/imports")
def upload_import(background: BackgroundTasks, files: Optional[List[UploadFile]] = File(default=None), user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    if not _teacher(user): raise HTTPException(403, "仅教师可导入")
    tables = _read_csvs(files); return import_rows(tables, "upload")
