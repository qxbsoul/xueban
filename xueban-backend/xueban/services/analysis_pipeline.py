"""学情分析流水线：确定性统计 + LLM 语义分析，带任务状态、幂等与版本失效。

设计约束（对应任务书第三项）：
- 统计数字只由确定性层计算，LLM 只做解释并引用证据，不重新计算数字；
- 任务状态持久化：pending / running / completed / partial / failed；
- 同一个 (batch, scope, scope_id, kind, data_version) 只会有一个任务，重复触发复用；
- 判题/修正/映射变化会改变 data_version，旧结果自动标记 stale 并重新生成。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import learning_analysis as analysis
from . import xueban_store as db

PIPELINE_VERSION = "analysis-v1"
LLM_PROMPT_VERSION = "analysis-llm-v1"
TERMINAL = ("completed", "partial", "failed")
STATUSES = ("pending", "running", "completed", "partial", "failed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS teacher_analysis_tasks (
 id TEXT PRIMARY KEY, batch_id TEXT, scope TEXT, scope_id TEXT, kind TEXT,
 status TEXT, step TEXT, progress REAL DEFAULT 0,
 data_version TEXT, algorithm_version TEXT, mapping_version INTEGER,
 dedupe_key TEXT UNIQUE, result_json TEXT, error TEXT,
 requested_by TEXT, created_at TEXT, started_at TEXT, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_analysis_tasks_batch ON teacher_analysis_tasks(batch_id, kind, created_at);
CREATE TABLE IF NOT EXISTS teacher_question_assignments (
 id TEXT PRIMARY KEY, batch_id TEXT, exam_node_id TEXT, class_id TEXT, student_id TEXT DEFAULT '',
 question_id TEXT, source TEXT, created_at TEXT,
 UNIQUE(batch_id, exam_node_id, class_id, student_id, question_id)
);
"""

_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def ensure_schema() -> None:
    db.init()
    db.conn().executescript(_SCHEMA)
    db.conn().commit()


def _lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def data_version(batch_id: str) -> str:
    """判题/修正/确认/映射任一变化都会改变这个版本号。"""
    runs = db.q1("""
        SELECT COUNT(*) n, COALESCE(MAX(r.created_at), '') t
        FROM teacher_grade_runs r JOIN teacher_answers a ON a.id=r.answer_id
        WHERE a.import_batch_id=?""", (batch_id,)) or {}
    corrections = db.q1("""
        SELECT COUNT(*) n, COALESCE(MAX(c.created_at), '') t
        FROM teacher_corrections c JOIN teacher_grade_runs r ON r.id=c.grade_run_id
        JOIN teacher_answers a ON a.id=r.answer_id WHERE a.import_batch_id=?""", (batch_id,)) or {}
    confirmations = db.q1("""
        SELECT COUNT(*) n, COALESCE(MAX(sp.updated_at), '') t
        FROM teacher_scoring_point_confirmations sp JOIN teacher_grade_runs r ON r.id=sp.grade_run_id
        JOIN teacher_answers a ON a.id=r.answer_id WHERE a.import_batch_id=?""", (batch_id,)) or {}
    mapping = analysis.current_mapping_version(batch_id)
    raw = "|".join(str(value) for value in (
        runs.get("n"), runs.get("t"), corrections.get("n"), corrections.get("t"),
        confirmations.get("n"), confirmations.get("t"), mapping, analysis.ALGORITHM_VERSION,
        PIPELINE_VERSION,
    ))
    return _hash(raw)


def _dedupe_key(batch_id: str, scope: str, scope_id: str, kind: str, version: str) -> str:
    return _hash(f"{batch_id}|{scope}|{scope_id}|{kind}|{version}|{PIPELINE_VERSION}")


def enqueue(batch_id: str, scope: str = "batch", scope_id: str = "", kind: str = "deterministic", requested_by: str = "", force: bool = False) -> Dict[str, Any]:
    """创建或复用分析任务；同一数据版本不重复创建。"""
    ensure_schema()
    version = data_version(batch_id)
    key = _dedupe_key(batch_id, scope, scope_id, kind, version)
    existing = db.q1("SELECT * FROM teacher_analysis_tasks WHERE dedupe_key=?", (key,))
    if existing and not force:
        return {"task": existing, "reused": True}
    if existing and force:
        if existing.get("status") in ("pending", "running"):
            return {"task": existing, "reused": True, "note": "任务正在执行，复用当前任务"}
        # 同一数据版本只保留一条任务：强制重跑在原记录上重置，避免唯一键冲突与重复任务。
        db.x("UPDATE teacher_analysis_tasks SET status='pending', step='queued', progress=0, result_json='', error='', started_at='', finished_at='', requested_by=?, created_at=? WHERE id=?",
             (requested_by, now(), existing["id"]))
        return {"task": db.q1("SELECT * FROM teacher_analysis_tasks WHERE id=?", (existing["id"],)), "reused": False, "note": "同版本结果已重建"}
    task_id = db.new_id("atask")
    db.x("""INSERT INTO teacher_analysis_tasks
        (id,batch_id,scope,scope_id,kind,status,step,progress,data_version,algorithm_version,mapping_version,dedupe_key,result_json,error,requested_by,created_at,started_at,finished_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (task_id, batch_id, scope, scope_id, kind, "pending", "queued", 0.0, version,
         analysis.ALGORITHM_VERSION, int(analysis.current_mapping_version(batch_id)), key,
         "", "", requested_by, now(), "", ""))
    return {"task": db.q1("SELECT * FROM teacher_analysis_tasks WHERE id=?", (task_id,)), "reused": False}


def _update(task_id: str, **fields: Any) -> None:
    columns = ", ".join(f"{key}=?" for key in fields)
    db.x(f"UPDATE teacher_analysis_tasks SET {columns} WHERE id=?", tuple(fields.values()) + (task_id,))


def _deterministic_payload(batch_id: str, scope: str, scope_id: str) -> Dict[str, Any]:
    if scope == "class":
        profile = analysis.class_profile(batch_id, scope_id)
        performance = analysis.question_performance(batch_id, scope_id)
        return {"scope": "class", "class": profile, "question_performance": performance}
    if scope == "student":
        profile = analysis.student_profile(batch_id, scope_id)
        return {"scope": "student", "student": profile}
    classes = analysis.all_classes(batch_id)
    payloads = []
    for cls in classes:
        profile = analysis.class_profile(batch_id, cls["id"])
        payloads.append({"class_id": cls["id"], "code": cls["code"], "profile": profile})
    performance = analysis.question_performance(batch_id)
    return {"scope": "batch", "classes": payloads, "question_performance": performance}


def _llm_key() -> str:
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


def _dominant_status(point: Dict[str, Any]) -> Optional[str]:
    breakdown = [item for item in (point.get("status_breakdown") or []) if item.get("count")]
    if not breakdown:
        return None
    return max(breakdown, key=lambda item: item["count"]).get("status")


def _evidence_digest(payload: Dict[str, Any]) -> Dict[str, Any]:
    """给 LLM 的证据摘要：只给已算好的数字与可引用的 ID，不让它重算。"""
    classes = payload.get("classes") or []
    digest = {"classes": [], "question_performance": {"summary": payload.get("question_performance", {}).get("summary")}}
    for item in classes:
        profile = item.get("profile") or {}
        digest["classes"].append({
            "class_id": item.get("class_id"), "code": item.get("code"),
            "overall_mastery": profile.get("overall_mastery"),
            "valid_student_count": profile.get("valid_student_count"),
            "knowledge_points": [
                {"knowledge_point_id": point["knowledge_point_id"], "name": point["name"], "mean_mastery": point.get("mean_mastery"), "status": _dominant_status(point), "valid_students": point.get("valid_students"), "insufficient_students": point.get("insufficient_students")}
                for point in (profile.get("knowledge_points") or [])[:12]
            ],
            "top_errors": (profile.get("error_types") or [])[:8],
        })
    return digest


def _call_llm(prompt: str, key: str) -> Dict[str, Any]:
    body = json.dumps({"model": "deepseek-chat", "messages": [{"role": "user", "content": prompt}], "temperature": 0}, ensure_ascii=False).encode()
    request = urllib.request.Request("https://api.deepseek.com/chat/completions", data=body, headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"}, method="POST")
    with urllib.request.urlopen(request, timeout=60) as response:
        content = json.loads(response.read().decode())["choices"][0]["message"]["content"]
    import re

    match = re.search(r"\{.*\}", content, re.S)
    if not match:
        raise ValueError("模型没有返回 JSON")
    return json.loads(match.group(0))


def _llm_payload(batch_id: str) -> Dict[str, Any]:
    deterministic = _deterministic_payload(batch_id, "batch", "")
    digest = _evidence_digest(deterministic)
    key = _llm_key()
    if not key:
        return {"status": "partial", "error": "未配置 DEEPSEEK_API_KEY：确定性统计已完成，语义分析未执行", "deterministic": None, "llm": None}
    prompt = (
        "你是教研分析助手。下面 JSON 是系统已经算好的班级学情（数字不要修改、不要重算）。"
        "请只做语义解释，并引用 knowledge_point_id / 题目 / 错误类型作为依据。"
        "输出 JSON：{\"class_observations\":[{\"class_code\":\"\",\"summary\":\"\",\"evidence_refs\":[]}],"
        "\"error_explanations\":[{\"error_type\":\"\",\"explanation\":\"\",\"evidence_refs\":[]}],"
        "\"teaching_suggestions\":[{\"class_code\":\"\",\"suggestion\":\"\",\"evidence_refs\":[],\"confidence\":0.0}]}\n"
        + json.dumps(digest, ensure_ascii=False)
    )
    try:
        llm = _call_llm(prompt, key)
        return {"status": "completed", "error": "", "llm": llm, "evidence_digest": digest}
    except Exception as exc:  # noqa: BLE001 - 失败要可观测、可重试
        return {"status": "partial", "error": f"语义分析失败：{exc}", "llm": None, "evidence_digest": digest}


def run_task(task_id: str) -> Dict[str, Any]:
    task = db.q1("SELECT * FROM teacher_analysis_tasks WHERE id=?", (task_id,))
    if not task:
        return {"error": "任务不存在"}
    if task.get("status") in ("completed", "partial", "running"):
        return {"task": task, "skipped": True}
    lock = _lock(task_id)
    if not lock.acquire(blocking=False):
        return {"task": task, "skipped": True}
    try:
        _update(task_id, status="running", step="computing", progress=0.1, started_at=now())
        try:
            if task["kind"] == "llm":
                outcome = _llm_payload(task["batch_id"])
                result = {"kind": "llm", "llm": outcome.get("llm"), "error": outcome.get("error", ""), "evidence_digest": outcome.get("evidence_digest"), "model": "deepseek-chat", "prompt_version": LLM_PROMPT_VERSION, "generated_at": now()}
                _update(task_id, status=outcome["status"], step="llm", progress=1.0, result_json=json.dumps(result, ensure_ascii=False), error=outcome.get("error", ""), finished_at=now())
            else:
                payload = _deterministic_payload(task["batch_id"], task.get("scope") or "batch", task.get("scope_id") or "")
                result = dict(payload)
                result["generated_at"] = now()
                result["data_version"] = task["data_version"]
                result["algorithm_version"] = analysis.ALGORITHM_VERSION
                _update(task_id, status="completed", step="done", progress=1.0, result_json=json.dumps(result, ensure_ascii=False), error="", finished_at=now())
        except Exception as exc:  # noqa: BLE001 - 任务失败必须可见
            _update(task_id, status="failed", step="failed", progress=1.0, error=str(exc)[:500], finished_at=now())
        return {"task": db.q1("SELECT * FROM teacher_analysis_tasks WHERE id=?", (task_id,))}
    finally:
        lock.release()


def run_pending(batch_id: str = "", limit: int = 20) -> Dict[str, Any]:
    """执行待处理任务；自动触发时同步跑确定性任务，保证用户打开页面时已有结果。"""
    sql = "SELECT id FROM teacher_analysis_tasks WHERE status='pending'"
    args: List[Any] = []
    if batch_id:
        sql += " AND batch_id=?"
        args.append(batch_id)
    sql += " ORDER BY created_at LIMIT ?"
    args.append(max(1, limit))
    ran = []
    for row in db.q(sql, tuple(args)):
        ran.append(run_task(row["id"]))
    return {"ran": len(ran)}


def auto_after_grading(batch_id: str, requested_by: str = "system", run_inline: bool = True, include_llm: bool = True) -> Dict[str, Any]:
    """判题完成后自动生成分析：批次 + 每个班级 +（可选）LLM 语义分析。

    人工修正/确认只刷新确定性统计，避免每次编辑都触发一次模型调用；LLM 由用户显式触发或整批判题完成时生成。
    """
    ensure_schema()
    created = []
    created.append(enqueue(batch_id, "batch", "", "deterministic", requested_by)["task"]["id"])
    for cls in analysis.all_classes(batch_id):
        created.append(enqueue(batch_id, "class", cls["id"], "deterministic", requested_by)["task"]["id"])
    if include_llm:
        created.append(enqueue(batch_id, "batch", "", "llm", requested_by)["task"]["id"])
    if run_inline:
        run_pending(batch_id)
    return {"batch_id": batch_id, "tasks": created}


def start_background(batch_id: str) -> None:
    """LLM 阶段放到后台线程，避免阻塞判题 worker。"""

    def _run() -> None:
        try:
            run_pending(batch_id)
        except Exception:
            pass

    threading.Thread(target=_run, name=f"analysis-{batch_id[-6:]}", daemon=True).start()


def list_tasks(batch_id: str, limit: int = 20) -> Dict[str, Any]:
    ensure_schema()
    current = data_version(batch_id)
    rows = db.q("SELECT * FROM teacher_analysis_tasks WHERE batch_id=? ORDER BY created_at DESC LIMIT ?", (batch_id, max(1, min(100, limit))))
    for row in rows:
        row["stale"] = row.get("data_version") != current
        row["has_result"] = bool(row.get("result_json"))
        row.pop("result_json", None)
    latest = {
        "deterministic": next((row for row in rows if row["kind"] == "deterministic" and row.get("scope") == "batch"), None) or next((row for row in rows if row["kind"] == "deterministic"), None),
        "llm": next((row for row in rows if row["kind"] == "llm" and row.get("scope") == "batch"), None) or next((row for row in rows if row["kind"] == "llm"), None),
    }
    return {"batch_id": batch_id, "current_data_version": current, "tasks": rows, "latest": latest}


def task_detail(task_id: str) -> Dict[str, Any]:
    ensure_schema()
    task = db.q1("SELECT * FROM teacher_analysis_tasks WHERE id=?", (task_id,))
    if not task:
        raise KeyError("分析任务不存在")
    result = {}
    try:
        result = json.loads(task.get("result_json") or "{}")
    except Exception:
        result = {}
    task = dict(task)
    task.pop("result_json", None)
    task["stale"] = task.get("data_version") != data_version(task["batch_id"])
    return {"task": task, "result": result}
