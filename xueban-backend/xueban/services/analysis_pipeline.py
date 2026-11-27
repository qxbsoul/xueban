"""学情分析流水线：确定性统计 + 对象级 LLM 语义分析，带版本、依赖、幂等与失效。

v1.5.17 起，LLM 分析按「对象」生成（学生 / 班级），并记录生成时的证据版本：
- 判题结果变化 → 写入事件 → 只对受影响的学生/班级标记旧结果 superseded 并重新生成；
- 同一证据版本重复事件不会重复创建任务；
- 生成失败不会覆盖旧的有效结果（旧任务保留 result_json，只是标记 superseded）；
- 任务状态持久化，进程重启后可继续 pending 任务。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import learning_analysis as analysis
from . import xueban_store as db

PIPELINE_VERSION = "analysis-v2"
LLM_PROMPT_VERSION = "analysis-llm-v2"
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
CREATE TABLE IF NOT EXISTS teacher_analysis_events (
 id TEXT PRIMARY KEY, batch_id TEXT, answer_id TEXT, student_id TEXT, class_id TEXT,
 event_type TEXT, created_at TEXT, processed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_analysis_events_pending ON teacher_analysis_events(batch_id, processed_at, created_at);
"""

_TASK_EXTRA_COLUMNS = (
    ("object_type", "TEXT DEFAULT ''"),
    ("object_id", "TEXT DEFAULT ''"),
    ("evidence_version", "TEXT DEFAULT ''"),
    ("prompt_version", "TEXT DEFAULT ''"),
    ("model", "TEXT DEFAULT ''"),
    ("superseded", "INTEGER DEFAULT 0"),
)

_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def ensure_schema() -> None:
    db.init()
    connection = db.conn()
    connection.executescript(_SCHEMA)
    existing = {row[1] for row in connection.execute("PRAGMA table_info(teacher_analysis_tasks)")}
    for name, ddl in _TASK_EXTRA_COLUMNS:
        if name not in existing:
            connection.execute(f"ALTER TABLE teacher_analysis_tasks ADD COLUMN {name} {ddl}")
    connection.commit()


def _lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def data_version(batch_id: str) -> str:
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


def evidence_version(batch_id: str, scope: str, scope_id: str) -> str:
    """对象级证据版本：只反映该学生/班级当前有效判题结果的变化。"""
    if scope == "student":
        row = db.q1("""
            SELECT COUNT(*) n, COALESCE(MAX(r.grading_version), 0) v, COALESCE(MAX(r.created_at), '') t
            FROM teacher_grade_runs r JOIN teacher_answers a ON a.id=r.answer_id
            WHERE a.import_batch_id=? AND a.student_id=?""", (batch_id, scope_id)) or {}
    elif scope == "class":
        row = db.q1("""
            SELECT COUNT(*) n, COALESCE(MAX(r.grading_version), 0) v, COALESCE(MAX(r.created_at), '') t
            FROM teacher_grade_runs r JOIN teacher_answers a ON a.id=r.answer_id
            JOIN teacher_students s ON s.id=a.student_id
            WHERE a.import_batch_id=? AND s.class_id=?""", (batch_id, scope_id)) or {}
    else:
        return data_version(batch_id)
    raw = f"{scope}|{scope_id}|{row.get('n')}|{row.get('v')}|{row.get('t')}|{analysis.ALGORITHM_VERSION}|{analysis.current_mapping_version(batch_id)}"
    return _hash(raw)


def _dedupe_key(batch_id: str, scope: str, scope_id: str, kind: str, version: str) -> str:
    return _hash(f"{batch_id}|{scope}|{scope_id}|{kind}|{version}|{PIPELINE_VERSION}")


def enqueue(batch_id: str, scope: str = "batch", scope_id: str = "", kind: str = "deterministic", requested_by: str = "", force: bool = False) -> Dict[str, Any]:
    ensure_schema()
    version = evidence_version(batch_id, scope, scope_id) if kind == "llm" else data_version(batch_id)
    key = _dedupe_key(batch_id, scope, scope_id, kind, version)
    existing = db.q1("SELECT * FROM teacher_analysis_tasks WHERE dedupe_key=?", (key,))
    if existing and not force:
        return {"task": existing, "reused": True}
    if existing and force:
        if existing.get("status") in ("pending", "running"):
            return {"task": existing, "reused": True, "note": "任务正在执行，复用当前任务"}
        db.x("UPDATE teacher_analysis_tasks SET status='pending', step='queued', progress=0, result_json='', error='', started_at='', finished_at='', requested_by=?, created_at=?, superseded=0 WHERE id=?",
             (requested_by, now(), existing["id"]))
        return {"task": db.q1("SELECT * FROM teacher_analysis_tasks WHERE id=?", (existing["id"],)), "reused": False, "note": "同版本结果已重建"}
    if kind == "llm":
        running = db.q1("SELECT * FROM teacher_analysis_tasks WHERE batch_id=? AND scope=? AND scope_id=? AND kind='llm' AND status IN ('pending','running') ORDER BY created_at DESC LIMIT 1", (batch_id, scope, scope_id))
        if running and not force:
            return {"task": running, "reused": True, "note": "该对象已有执行中的分析"}
    task_id = db.new_id("atask")
    db.x("""INSERT INTO teacher_analysis_tasks
        (id,batch_id,scope,scope_id,kind,status,step,progress,data_version,algorithm_version,mapping_version,dedupe_key,result_json,error,requested_by,created_at,started_at,finished_at,object_type,object_id,evidence_version,prompt_version,model,superseded)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (task_id, batch_id, scope, scope_id, kind, "pending", "queued", 0.0, version,
         analysis.ALGORITHM_VERSION, int(analysis.current_mapping_version(batch_id)), key,
         "", "", requested_by, now(), "", "", scope if scope != "batch" else "", scope_id, version,
         LLM_PROMPT_VERSION if kind == "llm" else "", "deepseek-chat" if kind == "llm" else "", 0))
    if kind == "llm" and scope in ("student", "class"):
        # 旧版本保留用于审计，只标记过期；生成失败时页面仍可回退到旧的有效结果。
        db.x("UPDATE teacher_analysis_tasks SET superseded=1 WHERE batch_id=? AND scope=? AND scope_id=? AND kind='llm' AND id<>? AND superseded=0", (batch_id, scope, scope_id, task_id))
    return {"task": db.q1("SELECT * FROM teacher_analysis_tasks WHERE id=?", (task_id,)), "reused": False}


def _update(task_id: str, **fields: Any) -> None:
    columns = ", ".join(f"{key}=?" for key in fields)
    db.x(f"UPDATE teacher_analysis_tasks SET {columns} WHERE id=?", tuple(fields.values()) + (task_id,))


def _deterministic_payload(batch_id: str, scope: str, scope_id: str) -> Dict[str, Any]:
    if scope == "class":
        return {"scope": "class", "class": analysis.class_profile(batch_id, scope_id), "question_performance": analysis.question_performance(batch_id, scope_id)}
    if scope == "student":
        return {"scope": "student", "student": analysis.student_profile(batch_id, scope_id)}
    classes = analysis.all_classes(batch_id)
    payloads = [{"class_id": cls["id"], "code": cls["code"], "profile": analysis.class_profile(batch_id, cls["id"])} for cls in classes]
    return {"scope": "batch", "classes": payloads, "question_performance": analysis.question_performance(batch_id)}


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


def _class_digest(profile: Dict[str, Any], performance: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "class": {"id": profile.get("class", {}).get("id"), "code": profile.get("class", {}).get("code")},
        "overall_mastery": profile.get("overall_mastery"),
        "valid_student_count": profile.get("valid_student_count"),
        "student_count": profile.get("student_count"),
        "knowledge_points": [
            {"knowledge_point_id": point["knowledge_point_id"], "name": point["name"], "mean_mastery": point.get("mean_mastery"), "status": _dominant_status(point), "valid_students": point.get("valid_students"), "insufficient_students": point.get("insufficient_students"), "coverage": point.get("coverage")}
            for point in (profile.get("knowledge_points") or [])[:12]
        ],
        "top_errors": (profile.get("error_types") or [])[:8],
        "question_summary": performance.get("summary"),
    }


def _student_digest(profile: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "student": {"id": profile.get("student", {}).get("id"), "code": profile.get("student", {}).get("code")},
        "coverage": profile.get("coverage"),
        "mastery": [
            {"knowledge_point_id": point["knowledge_point_id"], "name": point["knowledge_point_name"], "mastery": point.get("mastery_estimate"), "status": point.get("status"), "evidence": point.get("evidence_count")}
            for point in (profile.get("mastery") or [])[:12]
        ],
        "weak_points": profile.get("weak_points") or [],
        "error_types": (profile.get("error_types") or [])[:8],
    }


def _build_llm_prompt(scope: str, digest: Dict[str, Any]) -> str:
    if scope == "student":
        return (
            "你是教研分析助手。下面 JSON 是系统已经算好的单个学生学情（数字不要修改、不要重算）。"
            "请只做语义诊断，并引用 knowledge_point_id / 题目作为依据。输出 JSON："
            "{\"student_diagnosis\":{\"summary\":\"\",\"evidence_refs\":[]},"
            "\"error_reasons\":[{\"error_type\":\"\",\"explanation\":\"\",\"evidence_refs\":[]}],"
            "\"practice_recommendations\":[{\"suggestion\":\"\",\"evidence_refs\":[],\"confidence\":0.0}]}\n"
            + json.dumps(digest, ensure_ascii=False)
        )
    return (
        "你是教研分析助手。下面 JSON 是系统已经算好的班级学情（数字不要修改、不要重算）。"
        "请只做语义解释，并引用 knowledge_point_id / 题目 / 错误类型作为依据。输出 JSON："
        "{\"class_observations\":[{\"class_code\":\"\",\"summary\":\"\",\"evidence_refs\":[]}],"
        "\"error_explanations\":[{\"error_type\":\"\",\"explanation\":\"\",\"evidence_refs\":[]}],"
        "\"teaching_suggestions\":[{\"class_code\":\"\",\"suggestion\":\"\",\"evidence_refs\":[],\"confidence\":0.0}]}\n"
        + json.dumps(digest, ensure_ascii=False)
    )


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


def _llm_payload(batch_id: str, scope: str, scope_id: str) -> Dict[str, Any]:
    if scope == "class":
        digest = _class_digest(analysis.class_profile(batch_id, scope_id), analysis.question_performance(batch_id, scope_id))
    elif scope == "student":
        digest = _student_digest(analysis.student_profile(batch_id, scope_id))
    else:
        classes = analysis.all_classes(batch_id)
        digest = {"classes": [_class_digest(analysis.class_profile(batch_id, cls["id"]), analysis.question_performance(batch_id, cls["id"])) for cls in classes]}
    key = _llm_key()
    base = {"scope": scope, "object_id": scope_id, "evidence_version": evidence_version(batch_id, scope, scope_id), "prompt_version": LLM_PROMPT_VERSION, "model": "deepseek-chat", "evidence_digest": digest}
    if not key:
        return {**base, "status": "partial", "error": "未配置 DEEPSEEK_API_KEY：确定性统计已完成，语义分析未执行", "llm": None}
    try:
        return {**base, "status": "completed", "error": "", "llm": _call_llm(_build_llm_prompt(scope, digest), key)}
    except Exception as exc:  # noqa: BLE001 - 失败要可观测、可重试，且不覆盖旧结果
        return {**base, "status": "partial", "error": f"语义分析失败：{exc}", "llm": None}


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
                outcome = _llm_payload(task["batch_id"], task.get("scope") or "batch", task.get("scope_id") or "")
                result = {key: outcome.get(key) for key in ("scope", "object_id", "evidence_version", "prompt_version", "model", "llm", "error", "evidence_digest")}
                result["kind"] = "llm"
                _update(task_id, status=outcome["status"], step="llm", progress=1.0, result_json=json.dumps(result, ensure_ascii=False), error=outcome.get("error", ""), finished_at=now())
            else:
                result = _deterministic_payload(task["batch_id"], task.get("scope") or "batch", task.get("scope_id") or "")
                result.update({"generated_at": now(), "data_version": task["data_version"], "algorithm_version": analysis.ALGORITHM_VERSION})
                _update(task_id, status="completed", step="done", progress=1.0, result_json=json.dumps(result, ensure_ascii=False), error="", finished_at=now())
        except Exception as exc:  # noqa: BLE001 - 任务失败必须可见
            _update(task_id, status="failed", step="failed", progress=1.0, error=str(exc)[:500], finished_at=now())
        return {"task": db.q1("SELECT * FROM teacher_analysis_tasks WHERE id=?", (task_id,))}
    finally:
        lock.release()


def run_pending(batch_id: str = "", limit: int = 20) -> Dict[str, Any]:
    sql = "SELECT id FROM teacher_analysis_tasks WHERE status='pending'"
    args: List[Any] = []
    if batch_id:
        sql += " AND batch_id=?"
        args.append(batch_id)
    sql += " ORDER BY created_at LIMIT ?"
    args.append(max(1, limit))
    ran = 0
    for row in db.q(sql, tuple(args)):
        run_task(row["id"])
        ran += 1
    return {"ran": ran}


def recover_stale_tasks(batch_id: str = "", minutes: int = 10) -> int:
    """进程重启后把卡在 running 且没有心跳的任务回收成 pending。"""
    ensure_schema()
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - max(1, minutes) * 60))
    sql = "SELECT id FROM teacher_analysis_tasks WHERE status='running' AND COALESCE(started_at,'') < ?"
    args: List[Any] = [cutoff]
    if batch_id:
        sql += " AND batch_id=?"
        args.append(batch_id)
    rows = db.q(sql, tuple(args))
    for row in rows:
        _update(row["id"], status="pending", step="queued", progress=0.0, started_at="", error="进程重启后回收执行中的任务")
    return len(rows)


def record_grade_event(answer_id: str, event_type: str = "grade_changed") -> Optional[Dict[str, Any]]:
    """判题结果变化事件；只记录，不在这里同步跑分析。"""
    answer = db.q1("""SELECT a.id, a.import_batch_id, a.student_id, s.class_id
                      FROM teacher_answers a LEFT JOIN teacher_students s ON s.id=a.student_id
                      WHERE a.id=?""", (answer_id,))
    if not answer or not answer.get("import_batch_id"):
        return None
    ensure_schema()
    event_id = db.new_id("aevent")
    db.x("INSERT INTO teacher_analysis_events (id,batch_id,answer_id,student_id,class_id,event_type,created_at,processed_at) VALUES (?,?,?,?,?,?,?,?)",
         (event_id, answer["import_batch_id"], answer_id, answer.get("student_id") or "", answer.get("class_id") or "", event_type, now(), ""))
    return {"id": event_id, "batch_id": answer["import_batch_id"], "student_id": answer.get("student_id"), "class_id": answer.get("class_id")}


def process_events(batch_id: str, run_inline: bool = False) -> Dict[str, Any]:
    """把未处理事件转成对象级分析任务；同对象同证据版本只保留一个任务。"""
    ensure_schema()
    events = db.q("SELECT * FROM teacher_analysis_events WHERE batch_id=? AND COALESCE(processed_at,'')='' ORDER BY created_at", (batch_id,))
    targets: Dict[Tuple[str, str], set] = {}
    for event in events:
        if event.get("student_id"):
            targets.setdefault(("student", event["student_id"]), set()).add(event["id"])
        if event.get("class_id"):
            targets.setdefault(("class", event["class_id"]), set()).add(event["id"])
    created = []
    for (scope, scope_id), event_ids in targets.items():
        task = enqueue(batch_id, scope, scope_id, "llm", requested_by="analysis-event")["task"]
        created.append({"scope": scope, "scope_id": scope_id, "task_id": task["id"], "status": task["status"]})
        for event_id in event_ids:
            db.x("UPDATE teacher_analysis_events SET processed_at=? WHERE id=?", (now(), event_id))
    if run_inline:
        run_pending(batch_id)
    else:
        start_background(batch_id)
    return {"batch_id": batch_id, "events": len(events), "objects": len(targets), "tasks": created}


def start_background(batch_id: str) -> None:
    def _run() -> None:
        try:
            run_pending(batch_id)
        except Exception:
            pass

    threading.Thread(target=_run, name=f"analysis-{batch_id[-6:]}", daemon=True).start()


def auto_after_grading(batch_id: str, requested_by: str = "system", run_inline: bool = True, include_llm: bool = True) -> Dict[str, Any]:
    ensure_schema()
    created = [enqueue(batch_id, "batch", "", "deterministic", requested_by)["task"]["id"]]
    for cls in analysis.all_classes(batch_id):
        created.append(enqueue(batch_id, "class", cls["id"], "deterministic", requested_by)["task"]["id"])
    # 事件无论来自整批判题、单题重判还是人工修正，都必须转成对象级分析任务；
    # include_llm 只决定是否在本次请求里同步执行，未同步的交给后台线程。
    process_events(batch_id, run_inline=include_llm and run_inline)
    if include_llm and run_inline:
        pass
    elif run_inline:
        run_pending(batch_id)
    return {"batch_id": batch_id, "tasks": created}


def get_deterministic(batch_id: str, scope: str = "batch", scope_id: str = "") -> Optional[Dict[str, Any]]:
    """读取当前数据版本已持久化的确定性结果；不存在才生成一次，之后直接复用。"""
    ensure_schema()
    version = data_version(batch_id)
    row = db.q1("""SELECT * FROM teacher_analysis_tasks
                   WHERE batch_id=? AND scope=? AND COALESCE(scope_id,'')=? AND kind='deterministic'
                     AND status='completed' AND data_version=?
                   ORDER BY created_at DESC LIMIT 1""", (batch_id, scope, scope_id, version))
    if row and row.get("result_json"):
        try:
            return json.loads(row["result_json"])
        except Exception:
            pass
    task = enqueue(batch_id, scope, scope_id, "deterministic", requested_by="read-cache")["task"]
    if task.get("status") == "pending":
        run_task(task["id"])
    row = db.q1("SELECT * FROM teacher_analysis_tasks WHERE id=?", (task["id"],))
    if row and row.get("result_json"):
        try:
            return json.loads(row["result_json"])
        except Exception:
            return None
    return None


def list_tasks(batch_id: str, limit: int = 40) -> Dict[str, Any]:
    ensure_schema()
    recover_stale_tasks(batch_id)
    current = data_version(batch_id)
    rows = db.q("SELECT * FROM teacher_analysis_tasks WHERE batch_id=? ORDER BY created_at DESC LIMIT ?", (batch_id, max(1, min(200, limit))))
    for row in rows:
        row["stale"] = row.get("data_version") != current if row.get("kind") == "deterministic" else row.get("evidence_version") != evidence_version(batch_id, row.get("scope") or "batch", row.get("scope_id") or "")
        row["has_result"] = bool(row.get("result_json"))
        row.pop("result_json", None)
    latest = {
        "deterministic": next((row for row in rows if row["kind"] == "deterministic" and row.get("scope") == "batch"), None) or next((row for row in rows if row["kind"] == "deterministic"), None),
        "llm": next((row for row in rows if row["kind"] == "llm" and not row.get("superseded")), None),
    }
    return {"batch_id": batch_id, "current_data_version": current, "tasks": rows, "latest": latest}


def latest_valid_llm(batch_id: str, scope: str, scope_id: str) -> Optional[Dict[str, Any]]:
    """返回该对象最近一次成功（completed/partial 且有内容）的分析；失败的生成不会覆盖它。"""
    ensure_schema()
    rows = db.q("""SELECT * FROM teacher_analysis_tasks
                   WHERE batch_id=? AND scope=? AND scope_id=? AND kind='llm'
                   ORDER BY created_at DESC""", (batch_id, scope, scope_id))
    for row in rows:
        if row.get("status") in ("completed", "partial") and row.get("result_json"):
            try:
                result = json.loads(row["result_json"])
            except Exception:
                continue
            if result.get("llm"):
                return {"task": {key: row.get(key) for key in ("id", "status", "evidence_version", "prompt_version", "model", "created_at", "finished_at", "superseded", "error")}, "result": result}
    return None


def task_detail(task_id: str) -> Dict[str, Any]:
    ensure_schema()
    task = db.q1("SELECT * FROM teacher_analysis_tasks WHERE id=?", (task_id,))
    if not task:
        raise KeyError("分析任务不存在")
    try:
        result = json.loads(task.get("result_json") or "{}")
    except Exception:
        result = {}
    task = dict(task)
    task.pop("result_json", None)
    if task.get("kind") == "deterministic":
        task["stale"] = task.get("data_version") != data_version(task["batch_id"])
    else:
        task["stale"] = task.get("evidence_version") != evidence_version(task["batch_id"], task.get("scope") or "batch", task.get("scope_id") or "")
    return {"task": task, "result": result}
