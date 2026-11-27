"""Deterministic scoring-point analysis.

Mastery is a weighted Beta evidence estimate over scoring-point score ratios:
    alpha = a0 + sum(w * x)
    beta  = b0 + sum(w * (1 - x))
    mastery = alpha / (alpha + beta)
The 95% interval is an exact equal-tailed Beta interval computed from the
regularized incomplete Beta function. No evidence is treated as zero; it is
reported as insufficient.
"""
from __future__ import annotations

import hashlib
import json
import re
import math
import os
import time
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from . import xueban_store as db

ALGORITHM_VERSION = "sp-beta-v4"
ERROR_MIN_CONFIDENCE = float(os.environ.get("XUEBAN_ERROR_MIN_CONFIDENCE", "0.5"))
PARAM_VERSION = "a0=2,b0=2,min_evidence=2,min_coverage=0.25,min_questions=2;method_opportunity_denominator=independent"
A0 = 2.0
B0 = 2.0
# 证据门槛可配置（环境变量），但默认值必须保证“答完该知识点全部题目”可以达标。
MIN_EVIDENCE = float(os.environ.get("XUEBAN_MIN_EVIDENCE", "2"))
MIN_COVERAGE = float(os.environ.get("XUEBAN_MIN_COVERAGE", "0.25"))
MIN_QUESTIONS = int(os.environ.get("XUEBAN_MIN_QUESTIONS", "2"))

ERROR_MAP = {
    "概念理解错误": "概念理解错误", "概念": "概念理解错误", "审题": "概念理解错误", "concept": "概念理解错误",
    "方法选择错误": "方法选择错误", "方法": "方法选择错误", "公式": "方法选择错误", "公式使用错误": "方法选择错误",
    "条件遗漏": "方法选择错误", "method": "方法选择错误",
    "推理过程错误": "推理过程错误", "推理": "推理过程错误", "步骤遗漏": "推理过程错误", "reasoning": "推理过程错误",
    "计算操作错误": "计算操作错误", "计算": "计算操作错误", "运算错误": "计算操作错误", "calculation": "计算操作错误",
    "表达规范错误": "表达规范错误", "表达": "表达规范错误", "符号": "表达规范错误", "符号表达错误": "表达规范错误",
    "单位": "表达规范错误", "单位或格式错误": "表达规范错误", "expression": "表达规范错误",
    "未识别错误": "未识别错误", "其他": "未识别错误", "无法确定": "未识别错误", "无法判断": "未识别错误", "": "未识别错误",
}
ERROR_TAXONOMY = ["概念理解错误", "方法选择错误", "推理过程错误", "计算操作错误", "表达规范错误", "未识别错误"]
CORRECT_LABEL = "正确"


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _latest_grade(answer_id: str) -> Optional[Dict[str, Any]]:
    rows = db.q("SELECT * FROM teacher_grade_runs WHERE answer_id=? ORDER BY grading_version DESC, created_at DESC", (answer_id,))
    valid = [row for row in rows if row.get("status") != "failed"]
    return (valid or rows or [None])[0]


def _canonical_error(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "未识别错误"
    for key, label in ERROR_MAP.items():
        if key and key in text:
            return label
    return "未识别错误"


def migrate_scoring_points_scope() -> None:
    c = db.conn()
    row = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='scoring_points'").fetchone()
    if not row or "code TEXT UNIQUE" not in (row[0] or ""):
        return
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("CREATE TABLE scoring_points_new (id TEXT PRIMARY KEY, code TEXT, batch_id TEXT, question_version_id TEXT, name TEXT, description TEXT, created_at TEXT)")
        c.execute("INSERT INTO scoring_points_new (id,code,name,description,created_at) SELECT id,code,name,description,created_at FROM scoring_points")
        c.execute("DROP TABLE scoring_points")
        c.execute("ALTER TABLE scoring_points_new RENAME TO scoring_points")
        c.commit()
    except Exception:
        c.rollback(); raise


_MODEL_TABLES_READY = False
_MODEL_SIGNATURE: Optional[Tuple[int, str, int]] = None


def reset_model_cache() -> None:
    """测试/热更新用：清掉进程内的建表与模型同步标记。"""
    global _MODEL_TABLES_READY, _MODEL_SIGNATURE
    _MODEL_TABLES_READY = False
    _MODEL_SIGNATURE = None


def _model_signature() -> Tuple[int, str, int]:
    versions = db.q1("SELECT COUNT(*) n, COALESCE(MAX(created_at),'') t FROM teacher_question_versions") or {}
    indicators = db.q1("SELECT COUNT(*) n FROM teacher_indicators") or {}
    return (int(versions.get("n") or 0), str(versions.get("t") or ""), int(indicators.get("n") or 0))


_MODEL_INIT_LOCK = threading.RLock()


def ensure_model() -> None:
    with _MODEL_INIT_LOCK:
        _ensure_model_locked()


def _ensure_model_locked() -> None:
    """建表与评分点/映射同步。

    这个函数会被画像、机会、方法等多条链路反复调用；旧实现每次都重新建表、
    逐条 INSERT 所有题目版本（真实数据实测 39 次/请求、1.7s），是班级页 7 秒的主因。
    现在：建表与迁移只在进程内做一次；评分点同步只在题目版本/指标发生变化时重跑。
    """
    global _MODEL_TABLES_READY, _MODEL_SIGNATURE
    if not _MODEL_TABLES_READY:
        migrate_scoring_points_scope()
        db.x("CREATE TABLE IF NOT EXISTS scoring_points (id TEXT PRIMARY KEY, code TEXT, name TEXT, description TEXT, created_at TEXT)")
        db.x("CREATE TABLE IF NOT EXISTS scoring_point_knowledge_links (id TEXT PRIMARY KEY, scoring_point_id TEXT, knowledge_point_id TEXT, weight REAL, source TEXT, confidence REAL, status TEXT, created_at TEXT, updated_at TEXT)")
        db.x("CREATE TABLE IF NOT EXISTS learning_methods (id TEXT PRIMARY KEY, code TEXT, name TEXT NOT NULL, description TEXT DEFAULT '', topic_id TEXT DEFAULT '', source TEXT DEFAULT 'teacher', status TEXT DEFAULT 'active', created_at TEXT, updated_at TEXT)")
        db.x("CREATE TABLE IF NOT EXISTS scoring_point_method_links (id TEXT PRIMARY KEY, scoring_point_id TEXT, method_id TEXT, weight REAL, source TEXT, status TEXT, batch_id TEXT, created_at TEXT, updated_at TEXT)")
        db.x("CREATE TABLE IF NOT EXISTS scoring_point_link_history (id TEXT PRIMARY KEY, batch_id TEXT, scoring_point_id TEXT, entity TEXT, target_id TEXT, before_json TEXT, after_json TEXT, action TEXT, operator_id TEXT, created_at TEXT)")
        db.x("CREATE TABLE IF NOT EXISTS learning_knowledge_edges (id TEXT PRIMARY KEY, batch_id TEXT, from_kp TEXT, to_kp TEXT, source TEXT, status TEXT, confidence REAL, created_by TEXT, created_at TEXT, updated_at TEXT, UNIQUE(batch_id, from_kp, to_kp))")
        db.x("CREATE TABLE IF NOT EXISTS answer_error_labels (id TEXT PRIMARY KEY, batch_id TEXT, answer_id TEXT, grade_run_id TEXT, labels_json TEXT, source TEXT, model TEXT, prompt_version TEXT, confidence REAL, rationale TEXT, status TEXT, created_by TEXT, created_at TEXT, updated_at TEXT, UNIQUE(answer_id, grade_run_id))")
        db.x("CREATE TABLE IF NOT EXISTS teacher_method_opportunities (id TEXT PRIMARY KEY, batch_id TEXT, question_version_id TEXT, question_id TEXT, method_id TEXT, opportunity_type TEXT, mapping_status TEXT, confidence REAL, source TEXT, created_at TEXT, UNIQUE(batch_id, question_version_id, method_id))")
        db.x("CREATE TABLE IF NOT EXISTS teacher_version_compat (id TEXT PRIMARY KEY, kind TEXT, left_version TEXT, right_version TEXT, batch_id TEXT, status TEXT, basis TEXT, note TEXT, created_by TEXT, created_at TEXT, UNIQUE(kind, left_version, right_version))")
        compat_columns = {row["name"] for row in db.q("PRAGMA table_info(teacher_version_compat)")}
        if "batch_id" not in compat_columns:
            db.x("ALTER TABLE teacher_version_compat ADD COLUMN batch_id TEXT")
        db.x("CREATE TABLE IF NOT EXISTS recommendation_feedback (id TEXT PRIMARY KEY, batch_id TEXT, student_id TEXT, recommendation_type TEXT, target_id TEXT, reason TEXT, evidence_json TEXT, status TEXT, created_at TEXT, updated_at TEXT, completed_at TEXT, followup_json TEXT)")
        db.x("CREATE TABLE IF NOT EXISTS teacher_mapping_versions (batch_id TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 0, updated_at TEXT, updated_by TEXT)")
        db.x("CREATE TABLE IF NOT EXISTS error_label_gold (id TEXT PRIMARY KEY, batch_id TEXT, answer_id TEXT, labels_json TEXT, annotator TEXT, note TEXT, created_at TEXT, updated_at TEXT, UNIQUE(answer_id))")
        db.x("CREATE INDEX IF NOT EXISTS idx_method_opportunities_batch ON teacher_method_opportunities(batch_id, method_id)")
        db.x("CREATE INDEX IF NOT EXISTS idx_answer_error_labels_answer ON answer_error_labels(answer_id)")
        db.x("CREATE INDEX IF NOT EXISTS idx_error_gold_batch ON error_label_gold(batch_id)")
        db.x("CREATE INDEX IF NOT EXISTS idx_rec_feedback_student ON recommendation_feedback(student_id, batch_id)")
        for table, columns in {
            "scoring_points": ["batch_id", "question_version_id"],
            "scoring_point_knowledge_links": ["batch_id", "confirmed_by", "confirmed_at", "updated_by", "version"],
            "teacher_analysis_runs": ["result_hash", "mapping_version"],
        }.items():
            existing = {row[1] for row in db.conn().execute(f"PRAGMA table_info({table})")}
            for column in columns:
                if column not in existing:
                    db.x(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
        _MODEL_TABLES_READY = True
    signature = _model_signature()
    if signature == _MODEL_SIGNATURE:
        return
    for version in db.q("SELECT * FROM teacher_question_versions WHERE scoring_points IS NOT NULL AND scoring_points != ''"):
        kp = _knowledge_for_question(version)
        if not kp:
            continue
        for code in sorted(set(re.findall(r"\b[A-Z]\d{3}P\d+\b", str(version.get("scoring_points"))))):
            sp_id = f"sp:{version['version_id']}:{code}"
            db.x("INSERT OR IGNORE INTO scoring_points (id,code,batch_id,question_version_id,name,description,created_at) VALUES (?,?,?,?,?,?,?)", (sp_id, code, version["batch_id"], version["version_id"], code, f"来自批次 {version['batch_id']} 的评分点", now()))
            db.x("INSERT OR IGNORE INTO scoring_point_knowledge_links (id,scoring_point_id,knowledge_point_id,batch_id,weight,source,confidence,status,created_at,updated_at,version) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (f"link:{version['version_id']}:{code}:{kp['id']}", sp_id, kp["id"], version["batch_id"], 1.0, "legacy_indicator", 0.6, "legacy", now(), now(), "1"))
    _MODEL_SIGNATURE = signature


def list_methods() -> List[Dict[str, Any]]:
    ensure_model()
    return db.q("SELECT * FROM learning_methods WHERE status != 'disabled' ORDER BY code")


def upsert_method(code: str, name: str, description: str = "", topic_id: str = "", operator_id: str = "") -> Dict[str, Any]:
    ensure_model()
    code = str(code or "").strip()
    name = str(name or "").strip()
    if not code or not name:
        raise ValueError("方法编号和名称不能为空")
    row = db.q1("SELECT * FROM learning_methods WHERE code=?", (code,))
    if row:
        db.x("UPDATE learning_methods SET name=?, description=?, topic_id=?, status='active', updated_at=? WHERE id=?", (name, description, topic_id, now(), row["id"]))
    else:
        db.x("INSERT INTO learning_methods (id,code,name,description,topic_id,source,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)", (db.new_id("method"), code, name, description, topic_id, "teacher", "active", now(), now()))
    return db.q1("SELECT * FROM learning_methods WHERE code=?", (code,)) or {}


def _scoring_points_for(sp_id: str) -> List[Dict[str, Any]]:
    return db.q("SELECT * FROM scoring_points WHERE id=?", (sp_id,))


def _link_rows(sp_ids: List[str], table: str) -> List[Dict[str, Any]]:
    if not sp_ids:
        return []
    placeholders = ",".join("?" for _ in sp_ids)
    order = "CASE WHEN status='confirmed' THEN 0 WHEN status='legacy' THEN 1 ELSE 2 END"
    return db.q(f"SELECT * FROM {table} WHERE scoring_point_id IN ({placeholders}) AND status != 'disabled' AND weight > 0 ORDER BY {order}, weight DESC", tuple(sp_ids))


def mapping_for_batch(batch_id: str) -> Dict[str, Any]:
    """返回当前任务的评分点 → 知识点/方法映射，候选/已确认分开标记。"""
    ensure_model()
    points = db.q("SELECT * FROM scoring_points WHERE batch_id=? ORDER BY code", (batch_id,))
    if not points:
        # 兼容旧数据：没有任何批次评分点时，用答案引用到的评分点代码生成候选视图
        codes = set()
        for row in db.q("SELECT v.scoring_points FROM teacher_answers a JOIN teacher_question_versions v ON v.version_id=a.question_version_id WHERE a.import_batch_id=?", (batch_id,)):
            codes.update(re.findall(r"\b[A-Z]\d{3}P\d+\b", str(row.get("scoring_points") or "")))
        points = db.q("SELECT * FROM scoring_points WHERE code IN (%s) AND batch_id IS NULL" % ",".join("?" for _ in codes), tuple(codes)) if codes else []
    sp_ids = [point["id"] for point in points]
    knowledge = _link_rows(sp_ids, "scoring_point_knowledge_links")
    methods = _link_rows(sp_ids, "scoring_point_method_links")
    by_sp_k: Dict[str, List[Dict[str, Any]]] = {}
    by_sp_m: Dict[str, List[Dict[str, Any]]] = {}
    for link in knowledge:
        by_sp_k.setdefault(link["scoring_point_id"], []).append(link)
    for link in methods:
        by_sp_m.setdefault(link["scoring_point_id"], []).append(link)
    items = []
    for point in points:
        kp_links = by_sp_k.get(point["id"], [])
        method_links = by_sp_m.get(point["id"], [])
        items.append({
            "scoring_point_id": point["id"], "code": point["code"], "question_version_id": point.get("question_version_id"),
            "name": point.get("name") or point["code"],
            "mapping_status": ("confirmed" if any(link.get("status") == "confirmed" for link in kp_links)
                               else ("rejected" if kp_links and all(link.get("status") == "rejected" for link in kp_links)
                                     else ("pending_review" if kp_links and any(link.get("status") == "candidate" for link in kp_links) else ("legacy" if kp_links else "unmapped")))),
            "knowledge_links": [{"id": link["id"], "knowledge_point_id": link["knowledge_point_id"], "weight": link.get("weight"), "source": link.get("source"), "status": link.get("status"), "confirmed_by": link.get("confirmed_by"), "confirmed_at": link.get("confirmed_at"), "version": link.get("version")} for link in kp_links],
            "method_links": [{"id": link["id"], "method_id": link["method_id"], "weight": link.get("weight"), "source": link.get("source"), "status": link.get("status")} for link in method_links],
        })
    return {"batch_id": batch_id, "mapping_version": current_mapping_version(batch_id), "items": items, "methods": list_methods(), "summary": {"total": len(items), "confirmed": sum(1 for item in items if item["mapping_status"] == "confirmed"), "legacy": sum(1 for item in items if item["mapping_status"] in ("legacy", "pending_review")), "rejected": sum(1 for item in items if item["mapping_status"] == "rejected"), "unmapped": sum(1 for item in items if item["mapping_status"] == "unmapped")}}


_MAPPING_VERSION_CACHE: Dict[str, int] = {}


def current_mapping_version(batch_id: str) -> int:
    ensure_model()
    if not batch_id:
        return 0
    if batch_id in _MAPPING_VERSION_CACHE:
        return _MAPPING_VERSION_CACHE[batch_id]
    row = db.q1("SELECT version FROM teacher_mapping_versions WHERE batch_id=?", (batch_id,))
    version = int(row["version"]) if row else 0
    _MAPPING_VERSION_CACHE[batch_id] = version
    return version


def _bump_mapping_version(batch_id: str, operator_id: str = "") -> int:
    if not batch_id:
        return 0
    _MAPPING_VERSION_CACHE.pop(batch_id, None)
    _KP_OPPORTUNITY_CACHE.clear()
    version = current_mapping_version(batch_id) + 1
    db.x("INSERT INTO teacher_mapping_versions (batch_id,version,updated_at,updated_by) VALUES (?,?,?,?) ON CONFLICT(batch_id) DO UPDATE SET version=excluded.version, updated_at=excluded.updated_at, updated_by=excluded.updated_by", (batch_id, version, now(), operator_id))
    _MAPPING_VERSION_CACHE[batch_id] = version
    return version


def set_knowledge_links(sp_id: str, links: List[Dict[str, Any]], operator_id: str = "", action: str = "confirm") -> Dict[str, Any]:
    """教师确认评分点的知识点映射。权重按评分点归一化到 1，并保留修改前后历史。"""
    ensure_model()
    points = _scoring_points_for(sp_id)
    if not points:
        raise KeyError("评分点不存在")
    if action not in ("confirm", "reject"):
        raise ValueError("操作必须是 confirm 或 reject")
    point = points[0]
    if not point.get("batch_id"):
        raise ValueError("该评分点是全局 legacy 数据，不能直接改写；请在任务内生成评分点后再确认")
    before = db.q("SELECT * FROM scoring_point_knowledge_links WHERE scoring_point_id=? AND status != 'disabled'", (sp_id,))
    if action == "reject":
        if not before:
            raise ValueError("没有可拒绝的映射")
        with db.transaction():
            db.x("UPDATE scoring_point_knowledge_links SET status='rejected', updated_at=?, updated_by=? WHERE scoring_point_id=? AND status != 'disabled'", (now(), operator_id, sp_id))
            version = _bump_mapping_version(point.get("batch_id") or "", operator_id)
            db.x("INSERT INTO scoring_point_link_history (id,batch_id,scoring_point_id,entity,target_id,before_json,after_json,action,operator_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (db.new_id("linkhist"), point.get("batch_id"), sp_id, "knowledge", ",".join(row["knowledge_point_id"] for row in before), json.dumps(before, ensure_ascii=False), "{}", "reject", operator_id, now()))
        result = mapping_for_batch(point.get("batch_id") or "") if point.get("batch_id") else {"items": [], "summary": {}}
        result["mapping_version"] = version
        return result
    cleaned: Dict[str, float] = {}
    for link in links:
        kp = str(link.get("knowledge_point_id") or "").strip()
        if not kp:
            continue
        weight = float(link.get("weight") if link.get("weight") is not None else 1.0)
        if weight <= 0:
            raise ValueError("权重必须大于 0")
        cleaned[kp] = cleaned.get(kp, 0.0) + weight
    if not cleaned:
        raise ValueError("至少保留一个知识点")
    total = sum(cleaned.values())
    normalized = {kp: round(weight / total, 6) for kp, weight in cleaned.items()}
    round_error = round(1.0 - sum(normalized.values()), 6)
    if round_error:
        first = next(iter(normalized))
        normalized[first] = round(normalized[first] + round_error, 6)
    with db.transaction():
        db.x("UPDATE scoring_point_knowledge_links SET status='disabled', updated_at=?, updated_by=? WHERE scoring_point_id=? AND status != 'disabled'", (now(), operator_id, sp_id))
        for kp, weight in normalized.items():
            existing = db.q1("SELECT * FROM scoring_point_knowledge_links WHERE scoring_point_id=? AND knowledge_point_id=?", (sp_id, kp))
            version = int((existing or {}).get("version") or 0) + 1
            if existing:
                db.x("UPDATE scoring_point_knowledge_links SET weight=?, source='teacher', confidence=1.0, status='confirmed', batch_id=?, confirmed_by=?, confirmed_at=?, updated_by=?, version=?, updated_at=? WHERE id=?", (weight, point.get("batch_id"), operator_id, now(), operator_id, str(version), now(), existing["id"]))
            else:
                db.x("INSERT INTO scoring_point_knowledge_links (id,scoring_point_id,knowledge_point_id,batch_id,weight,source,confidence,status,confirmed_by,confirmed_at,updated_by,version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (db.new_id("link"), sp_id, kp, point.get("batch_id"), weight, "teacher", 1.0, "confirmed", operator_id, now(), operator_id, str(version), now(), now()))
        mapping_version = _bump_mapping_version(point.get("batch_id") or "", operator_id)
        db.x("INSERT INTO scoring_point_link_history (id,batch_id,scoring_point_id,entity,target_id,before_json,after_json,action,operator_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (db.new_id("linkhist"), point.get("batch_id"), sp_id, "knowledge", ",".join(normalized), json.dumps(before, ensure_ascii=False), json.dumps({"links": normalized, "mapping_version": mapping_version}, ensure_ascii=False), "confirm", operator_id, now()))
    result = mapping_for_batch(point.get("batch_id") or "") if point.get("batch_id") else {"items": [], "summary": {}}
    result["mapping_version"] = mapping_version
    return result


def set_method_links(sp_id: str, links: List[Dict[str, Any]], operator_id: str = "") -> Dict[str, Any]:
    ensure_model()
    points = _scoring_points_for(sp_id)
    if not points:
        raise KeyError("评分点不存在")
    point = points[0]
    if not point.get("batch_id"):
        raise ValueError("该评分点是全局 legacy 数据，不能直接改写；请在任务内生成评分点后再确认")
    cleaned: Dict[str, float] = {}
    for link in links:
        method_id = str(link.get("method_id") or "").strip()
        if not method_id:
            continue
        if not db.q1("SELECT id FROM learning_methods WHERE id=? AND status != 'disabled'", (method_id,)):
            raise ValueError(f"方法不存在：{method_id}")
        weight = float(link.get("weight") if link.get("weight") is not None else 1.0)
        if weight <= 0:
            raise ValueError("权重必须大于 0")
        cleaned[method_id] = cleaned.get(method_id, 0.0) + weight
    total = sum(cleaned.values()) or 1.0
    normalized = {mid: round(weight / total, 6) for mid, weight in cleaned.items()}
    before = db.q("SELECT * FROM scoring_point_method_links WHERE scoring_point_id=? AND status != 'disabled'", (sp_id,))
    with db.transaction():
        db.x("UPDATE scoring_point_method_links SET status='disabled', updated_at=? WHERE scoring_point_id=? AND status != 'disabled'", (now(), sp_id))
        for method_id, weight in normalized.items():
            db.x("INSERT INTO scoring_point_method_links (id,scoring_point_id,method_id,weight,source,status,batch_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)", (db.new_id("methodlink"), sp_id, method_id, weight, "teacher", "confirmed", point.get("batch_id"), now(), now()))
        db.x("INSERT INTO scoring_point_link_history (id,batch_id,scoring_point_id,entity,target_id,before_json,after_json,action,operator_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (db.new_id("linkhist"), point.get("batch_id"), sp_id, "method", ",".join(normalized), json.dumps(before, ensure_ascii=False), json.dumps(normalized, ensure_ascii=False), "confirm", operator_id, now()))
        _bump_mapping_version(point.get("batch_id") or "", operator_id)
    if point.get("batch_id"):
        generate_method_opportunities(point["batch_id"], operator_id)
    return {"scoring_point_id": sp_id, "methods": [{"method_id": mid, "weight": weight} for mid, weight in normalized.items()]}


def _knowledge_for_question(question: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    indicator = db.q1("SELECT * FROM teacher_indicators WHERE id=? OR code=?", (question.get("indicator_id"), question.get("indicator_id"))) or {}
    if not indicator:
        return None
    topic = db.q1("SELECT * FROM teacher_topics WHERE id=? OR code=?", (question.get("topic_id"), question.get("topic_id"))) or {}
    return {
        "id": f"kp:{indicator.get('code') or indicator.get('id')}",
        "code": indicator.get("code") or indicator.get("id"),
        "name": indicator.get("description") or indicator.get("code") or "未命名知识点",
        "topic": topic.get("title") or topic.get("code") or "",
        "topic_id": topic.get("id") or "",
    }


def _knowledge_links(scoring_point_id: str, batch_id: str = "") -> List[Dict[str, Any]]:
    points = db.q("SELECT * FROM scoring_points WHERE code=? AND (batch_id=? OR batch_id IS NULL) ORDER BY CASE WHEN batch_id=? THEN 0 ELSE 1 END", (scoring_point_id, batch_id, batch_id))
    for sp in points:
        links = db.q("SELECT l.*, i.description, i.code indicator_code FROM scoring_point_knowledge_links l LEFT JOIN teacher_indicators i ON l.knowledge_point_id = 'kp:' || i.code WHERE l.scoring_point_id=? AND l.status NOT IN ('disabled','rejected') AND l.weight > 0 ORDER BY CASE WHEN l.status='confirmed' THEN 0 WHEN l.status='legacy' THEN 1 ELSE 2 END, l.weight DESC", (sp["id"],))
        if links:
            return links
    return []


def _method_links(scoring_point_id: str, batch_id: str = "") -> List[Dict[str, Any]]:
    points = db.q("SELECT * FROM scoring_points WHERE code=? AND (batch_id=? OR batch_id IS NULL) ORDER BY CASE WHEN batch_id=? THEN 0 ELSE 1 END", (scoring_point_id, batch_id, batch_id))
    for sp in points:
        links = db.q("SELECT l.*, m.name method_name, m.code method_code FROM scoring_point_method_links l LEFT JOIN learning_methods m ON m.id=l.method_id WHERE l.scoring_point_id=? AND l.status NOT IN ('disabled','rejected') AND l.weight > 0 ORDER BY l.weight DESC", (sp["id"],))
        if links:
            return links
    return []


def method_opportunities(batch_id: str) -> List[Dict[str, Any]]:
    ensure_model()
    return db.q("SELECT * FROM teacher_method_opportunities WHERE batch_id=? ORDER BY question_id, method_id", (batch_id,))


def generate_method_opportunities(batch_id: str, operator_id: str = "") -> Dict[str, Any]:
    """从题目版本的评分点→方法映射生成“题目-方法机会”。

    机会来自题目结构（评分点映射），不是从学生已经用过的方法反推。
    """
    ensure_model()
    desired: Dict[tuple, Dict[str, Any]] = {}
    for version in db.q("SELECT * FROM teacher_question_versions WHERE batch_id=?", (batch_id,)):
        codes = sorted(set(re.findall(r"\b[A-Z]\d{3}P\d+\b", str(version.get("scoring_points") or ""))))
        for code in codes:
            for link in _method_links(code, batch_id):
                desired[(version["version_id"], link["method_id"])] = {"version": version, "link": link}
    removed = 0
    with db.transaction():
        for row in db.q("SELECT * FROM teacher_method_opportunities WHERE batch_id=? AND source='sp_method_map'", (batch_id,)):
            if (row["question_version_id"], row["method_id"]) not in desired:
                db.x("DELETE FROM teacher_method_opportunities WHERE id=?", (row["id"],))
                removed += 1
        created = 0
        for (version_id, method_id), entry in desired.items():
            version, link = entry["version"], entry["link"]
            if db.q1("SELECT id FROM teacher_method_opportunities WHERE batch_id=? AND question_version_id=? AND method_id=?", (batch_id, version_id, method_id)):
                db.x("UPDATE teacher_method_opportunities SET mapping_status=?, confidence=? WHERE batch_id=? AND question_version_id=? AND method_id=?", (link.get("status") or "legacy", float(link.get("weight") or 0), batch_id, version_id, method_id))
                continue
            db.x("INSERT INTO teacher_method_opportunities (id,batch_id,question_version_id,question_id,method_id,opportunity_type,mapping_status,confidence,source,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (db.new_id("methodopp"), batch_id, version_id, version["question_id"], method_id, "scoring_point_mapping", link.get("status") or "legacy", float(link.get("weight") or 0), "sp_method_map", now()))
            created += 1
    return {"batch_id": batch_id, "created": created, "removed": removed, "opportunities": method_opportunities(batch_id)}


def _method_opportunity_map(batch_id: str) -> Dict[str, Dict[str, Any]]:
    mapping: Dict[str, Dict[str, Any]] = {}
    for row in method_opportunities(batch_id):
        entry = mapping.setdefault(row["method_id"], {"questions": set(), "statuses": set()})
        entry["questions"].add(row["question_id"])
        entry["statuses"].add(row.get("mapping_status") or "legacy")
    return mapping


def _answers(batch_id: str) -> List[Dict[str, Any]]:
    # 任务内题面/满分/评分点以作答绑定的题目版本为准；旧数据无版本时才回退全局题表。
    return db.q("SELECT a.*, s.code student_code, s.class_id, c.code class_code, COALESCE(v.stem,q.stem) stem, COALESCE(v.question_type,q.question_type) question_type, COALESCE(v.max_score,q.max_score) question_max_score, COALESCE(v.scoring_points,q.scoring_points) scoring_points, COALESCE(v.topic_id,q.topic_id) topic_id, COALESCE(v.indicator_id,q.indicator_id) indicator_id, CASE WHEN v.version_id IS NOT NULL THEN 'version' ELSE 'global_fallback' END question_source FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id LEFT JOIN teacher_classes c ON c.id=s.class_id LEFT JOIN teacher_questions q ON q.id=a.question_id LEFT JOIN teacher_question_versions v ON v.version_id=a.question_version_id WHERE a.import_batch_id=? ORDER BY a.student_id, a.question_id, a.revision DESC", (batch_id,))


def grade_usage(grade: Optional[Dict[str, Any]], confirmations: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """统一“有效成绩/有效证据”口径。全系统只允许通过这里判断一条判题记录怎么用。

    - failed：不参与成绩，也不参与掌握估计。
    - needs_review：未定稿，不参与成绩与证据。
    - manual_corrected：整题分数有效；评分点证据只有在教师逐点确认后才有效。
    - manual_confirmed / graded：整题分数有效；评分点证据有效（未确认时使用 AI 评分点值）。
    """
    if not grade:
        return {"score": False, "evidence": False, "state": "pending", "reason": "没有判题结果"}
    status = str(grade.get("status") or "")
    if status == "failed":
        return {"score": False, "evidence": False, "state": "failed", "reason": "判题失败"}
    if grade.get("needs_review"):
        return {"score": False, "evidence": False, "state": "needs_review", "reason": "等待人工复核"}
    if status == "manual_corrected":
        has_points = bool(confirmations)
        return {"score": True, "evidence": has_points, "state": "manual_corrected", "reason": "整题人工修正；评分点仅在逐点确认后有效"}
    if status in ("graded", "manual_confirmed"):
        return {"score": True, "evidence": True, "state": status, "reason": "自动判题有效" if status == "graded" else "整题已确认"}
    return {"score": False, "evidence": False, "state": status or "unknown", "reason": "未知判题状态"}


def _evidence(batch_id: str, student_id: str = "", trace: Optional[Dict[str, Any]] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """从判题结果提取评分点证据。

    `trace` 会记录每一级的输入/保留/排除数量和样本，用于回答「这条证据到底在哪一级被过滤」。
    """
    ensure_model()
    answers = _answers(batch_id)
    if student_id:
        answers = [item for item in answers if item["student_id"] == student_id]
    latest_event: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for answer in answers:
        key = (answer["student_id"], answer.get("event_id") or f"answer:{answer['id']}", answer["question_id"])
        latest_event.setdefault(key, answer)
    t: Dict[str, Any] = trace if trace is not None else {}
    t.update({"answers": len(answers), "latest_events": len(latest_event), "grades_present": 0, "excluded": {}, "samples": [], "steps_total": 0, "steps_empty": 0, "steps_unlocatable": 0, "links_total": 0, "links_conflict": 0, "links_invalid_range": 0, "links_unmapped_point": 0, "evidence_rows": 0, "unmapped_points": 0, "students_with_evidence": 0})

    def _exclude(answer: Dict[str, Any], stage: str, reason: str) -> None:
        t["excluded"][stage] = t["excluded"].get(stage, 0) + 1
        if len(t["samples"]) < 200:
            t["samples"].append({"answer_id": answer["id"], "student_id": answer["student_id"], "student_code": answer.get("student_code"), "question_id": answer["question_id"], "stage": stage, "reason": reason})

    evidence: List[Dict[str, Any]] = []
    unmapped = 0
    reasons: Dict[str, int] = {}
    conflict = 0
    link_cache: Dict[str, List[Dict[str, Any]]] = {}
    grade_map: Dict[str, Optional[Dict[str, Any]]] = {}
    for answer in latest_event.values():
        grade = _latest_grade(answer["id"])
        grade_map[answer["id"]] = grade
        confirmed = {row["scoring_point_id"]: row for row in db.q("SELECT * FROM teacher_scoring_point_confirmations WHERE grade_run_id=?", (grade["id"],))} if grade else {}
        if grade:
            t["grades_present"] += 1
        usage = grade_usage(grade, confirmed)
        if not usage["score"]:
            reasons[usage["state"]] = reasons.get(usage["state"], 0) + 1
            _exclude(answer, usage["state"], usage["reason"])
            continue
        if not usage["evidence"] and not (usage["state"] == "manual_corrected" and confirmed):
            reasons["manual_corrected_without_point_confirmation"] = reasons.get("manual_corrected_without_point_confirmation", 0) + 1
            _exclude(answer, "manual_corrected_without_point_confirmation", "整题人工修正但评分点未逐点确认")
            continue
        try:
            steps = json.loads(grade.get("step_results_json") or "[]")
        except Exception:
            steps = []
        t["steps_total"] += len(steps)
        if not steps:
            t["steps_empty"] += 1
            _exclude(answer, "steps_empty", "判题结果没有步骤/评分点链接（旧版本解析丢失）")
            continue
        point_values = {}
        for step in steps:
            # 明确标记 source_step_id=None 的步骤无法定位回原文，其证据不得进入学情；
            # 但不能因此丢掉同一道题其他可定位步骤的证据。
            if "source_step_id" in step and not step.get("source_step_id"):
                t["steps_unlocatable"] += 1
                continue
            for link in (step.get("scoring_point_links") or []):
                if not isinstance(link, dict):
                    continue
                t["links_total"] += 1
                sp = str(link.get("scoring_point_id") or "")
                if usage["state"] == "manual_corrected" and sp not in confirmed:
                    # 只改了整题分、没有逐点确认时，不允许把 AI 评分点当作已确认证据。
                    t["links_unmapped_point"] += 1
                    continue
                full = float(link.get("max_score") or step.get("max_score") or 0)
                awarded = link.get("awarded_score")
                awarded = link.get("score") if awarded is None else awarded
                earned = float(awarded if awarded is not None else 0)
                if sp in confirmed:
                    earned = float(confirmed[sp].get("confirmed_score") or 0)
                    full = float(confirmed[sp].get("max_score") or full)
                if not sp or full <= 0:
                    t["links_invalid_range"] += 1
                    unmapped += 1
                    continue
                if sp in point_values:
                    old_earned, old_full = point_values[sp]["earned"], point_values[sp]["full"]
                    if abs(old_earned - earned) > 1e-6 or abs(old_full - full) > 1e-6:
                        conflict += 1
                        t["links_conflict"] += 1
                        point_values[sp]["conflict"] = True
                    continue
                point_values[sp] = {"earned": earned, "full": full, "step": step, "grade": grade, "conflict": False}
        usable = {sp: value for sp, value in point_values.items() if not value["conflict"] and 0 <= value["earned"] <= value["full"] + 1e-6}
        if not usable:
            _exclude(answer, "no_usable_point", "所有评分点都冲突或得分越界")
            continue
        # 权重分母只算「有知识点映射」的评分点：同一题里只要有一个评分点没映射，
        # 旧实现会把它的 1/n 权重白白扣掉，导致整题有效证据被稀释到门槛以下（表现为「证据不足」）。
        mapped_points: Dict[str, Tuple[Dict[str, Any], List[Dict[str, Any]]]] = {}
        for sp, meta in usable.items():
            if sp not in link_cache:
                link_cache[sp] = _knowledge_links(sp, batch_id)
            kp_links = link_cache[sp]
            if not kp_links:
                unmapped += 1
                t["unmapped_points"] += 1
                continue
            mapped_points[sp] = (meta, kp_links)
        if not mapped_points:
            _exclude(answer, "unmapped_scoring_points", "有评分点得分但没有知识点映射")
            continue
        per_question_weight = 1.0 / len(mapped_points)
        for sp, (meta, kp_links) in mapped_points.items():
            earned, full, step = meta["earned"], meta["full"], meta["step"]
            x = max(0.0, min(1.0, earned / full))
            method_links = _method_links(sp, batch_id)
            raw_error = step.get("error_type") or (meta["grade"].get("error_type") if meta["grade"].get("score", 0) < full else "")
            label_confidence = step.get("confidence") if step.get("confidence") is not None else meta["grade"].get("confidence")
            if earned >= full - 1e-9:
                error_type = CORRECT_LABEL
            else:
                error_type = _canonical_error(raw_error)
                if label_confidence is not None:
                    try:
                        if float(label_confidence) < ERROR_MIN_CONFIDENCE:
                            # 低置信度只保留原始标签与置信度，不伪装成确定分类。
                            error_type = "未识别错误"
                    except (TypeError, ValueError):
                        pass
            for kp_link in kp_links:
                kp_id = kp_link["knowledge_point_id"]
                kp_name = kp_link.get("description") or kp_link.get("indicator_code") or kp_id
                evidence.append({
                    "task_id": batch_id, "student_id": answer["student_id"], "student_code": answer.get("student_code"),
                    "class_id": answer.get("class_id"), "class_code": answer.get("class_code"),
                    "answer_id": answer["id"], "question_id": answer["question_id"], "exam_node_id": answer.get("exam_node_id"),
                    "event_id": answer.get("event_id"), "revision": answer.get("revision"),
                    "scoring_point_id": sp, "scoring_point_name": f"评分点 {sp}",
                    "earned_score": round(earned, 3), "full_score": round(full, 3),
                    "score_ratio": round(x, 4), "weight": round(per_question_weight * float(kp_link.get("weight") or 0), 6), "base_weight": round(per_question_weight, 6),
                    "knowledge_point_id": kp_id, "knowledge_point_name": kp_name, "knowledge_topic": "",
                    "error_type": error_type, "error_label_confidence": label_confidence, "error_label_raw": str(raw_error or ""),
                    "error_reason": step.get("judgment_reason") or meta["grade"].get("judgment_reason") or "",
                    "evidence_text": step.get("student_step") or answer.get("solution_process") or answer.get("original_answer") or "",
                    "judgment_source": "teacher_confirmed" if sp in confirmed else ("manual" if meta["grade"].get("status") == "manual_corrected" else "ai"),
                    "confidence": meta["grade"].get("confidence") or step.get("confidence"),
                    "grade_status": meta["grade"].get("status"),
                    "mapping_status": kp_link.get("status"),
                    "methods": [{"method_id": link["method_id"], "method_name": link.get("method_name") or link["method_id"], "weight": float(link.get("weight") or 0), "link_status": link.get("status")} for link in method_links],
                })
    t["evidence_rows"] = len(evidence)
    t["students_with_evidence"] = len({item["student_id"] for item in evidence})
    stats = {"answers": len(latest_event), "evidence": len(evidence), "unmapped": unmapped, "no_grade": sum(value for key, value in reasons.items() if key != "manual_corrected_without_point_confirmation"), "not_usable": reasons, "invalid_score": conflict, "_grade_map": grade_map}
    return evidence, stats


def evidence_trace(batch_id: str, student_id: str = "") -> Dict[str, Any]:
    """证据链逐级追溯：每一级的输入、保留、排除数量与样本，定位具体记录被哪一级过滤。"""
    trace: Dict[str, Any] = {}
    evidence, stats = _evidence(batch_id, student_id=student_id, trace=trace)
    stages = [
        {"stage": "raw_answers", "label": "原始作答", "count": trace["answers"], "excluded": 0, "reason": "任务内全部作答记录"},
        {"stage": "latest_events", "label": "最新作答事件", "count": trace["latest_events"], "excluded": trace["answers"] - trace["latest_events"], "reason": "同一学生同一事件同一题只保留最新修订"},
        {"stage": "grades_present", "label": "有判题结果", "count": trace["grades_present"], "excluded": trace["latest_events"] - trace["grades_present"], "reason": "没有判题结果"},
        {"stage": "grade_usable", "label": "判题可用于学情", "count": trace["grades_present"] - sum(trace["excluded"].get(key, 0) for key in ("failed", "needs_review", "pending", "unknown")), "excluded": sum(trace["excluded"].get(key, 0) for key in ("failed", "needs_review", "pending", "unknown")), "reason": "failed / needs_review / pending 由 grade_usage 排除"},
        {"stage": "steps_present", "label": "有步骤结构", "count": trace["grades_present"] - trace["steps_empty"] - sum(trace["excluded"].get(key, 0) for key in ("failed", "needs_review", "pending", "unknown", "manual_corrected_without_point_confirmation")), "excluded": trace["steps_empty"], "reason": "step_results 为空（历史解析丢失）"},
        {"stage": "scoring_point_links", "label": "评分点链接", "count": trace["links_total"], "excluded": trace["links_invalid_range"] + trace["links_unmapped_point"] + trace["links_conflict"], "reason": "无效分值 / 未逐点确认 / 重复冲突"},
        {"stage": "knowledge_mapped", "label": "有知识点映射", "count": trace["links_total"] - trace["links_invalid_range"] - trace["links_unmapped_point"] - trace["links_conflict"] - trace["unmapped_points"], "excluded": trace["unmapped_points"], "reason": "评分点没有知识点映射"},
        {"stage": "evidence_rows", "label": "最终证据行", "count": trace["evidence_rows"], "excluded": max(0, trace["links_total"] - trace["evidence_rows"]), "reason": "一个评分点可映射多个知识点，证据行可能多于链接数"},
    ]
    return {
        "batch_id": batch_id, "student_id": student_id,
        "stages": stages,
        "excluded_by_stage": {**trace["excluded"], **({"unmapped_points": trace["unmapped_points"]} if trace["unmapped_points"] else {})},
        "samples": trace["samples"],
        "students_with_evidence": trace["students_with_evidence"],
        "stats": stats,
        "evidence_rows": len(evidence),
    }


def _quantile(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    low = int(math.floor(pos)); high = int(math.ceil(pos))
    if low == high:
        return round(ordered[low], 4)
    return round(ordered[low] * (high - pos) + ordered[high] * (pos - low), 4)


def _status(mastery: float, evidence_weight: float, coverage: float, question_count: int) -> str:
    """证据门槛优先于掌握度：证据不足时不允许输出掌握结论。

    覆盖率分母是“该知识点在任务中实际可获得的题目机会”，不是全卷题目数，
    因此答完该知识点的全部题目可以达到 100%，不会出现“答得越全越不可能达标”。
    """
    if evidence_weight < MIN_EVIDENCE or coverage < MIN_COVERAGE or question_count < MIN_QUESTIONS:
        return "证据不足"
    if mastery >= 0.8:
        return "熟练掌握"
    if mastery >= 0.65:
        return "基本掌握"
    if mastery >= 0.5:
        return "初步掌握"
    return "掌握不足"


_KP_OPPORTUNITY_CACHE: Dict[Tuple[str, int], Dict[str, set]] = {}


def _kp_opportunities(batch_id: str) -> Dict[str, set]:
    """该任务中每个知识点实际可获得的题目机会（按批次题目版本解析评分点）。

    班级画像会为每个学生各调用一次；不做缓存时一次班级页要跑 40×800 次链接查询，
    接口会慢到前端以为请求失败（真实环境实测 >8s）。这里按「批次 + 映射版本」缓存。
    """
    cache_key = (batch_id, current_mapping_version(batch_id))
    cached = _KP_OPPORTUNITY_CACHE.get(cache_key)
    if cached is not None:
        return cached
    link_cache: Dict[str, List[Dict[str, Any]]] = {}
    result: Dict[str, set] = {}
    for row in db.q("SELECT a.question_id, COALESCE(v.scoring_points,q.scoring_points) scoring_points FROM teacher_answers a LEFT JOIN teacher_question_versions v ON v.version_id=a.question_version_id LEFT JOIN teacher_questions q ON q.id=a.question_id WHERE a.import_batch_id=?", (batch_id,)):
        for code in re.findall(r"\b[A-Z]\d{3}P\d+\b", str(row.get("scoring_points") or "")):
            if code not in link_cache:
                link_cache[code] = _knowledge_links(code, batch_id)
            for link in link_cache[code]:
                result.setdefault(link["knowledge_point_id"], set()).add(row["question_id"])
    _KP_OPPORTUNITY_CACHE[cache_key] = result
    return result


def _betacf(a: float, b: float, x: float) -> float:
    MAXIT = 300
    EPS = 3e-12
    FPMIN = 1e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x))
    if x < (a + 1) / (a + b + 2):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1 - x) / b


def _beta_quantile(p: float, a: float, b: float) -> float:
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if _betai(a, b, mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


_INTERVAL_CACHE: Dict[Tuple[float, float], Tuple[float, float]] = {}


def _interval(alpha: float, beta: float) -> Tuple[float, float]:
    """Beta(2,2) 后验的 95% 精确区间。

    精确区间要靠数值求分位，单次约 4ms；班级页 40 个学生 × 多个知识点会重复计算同一组
    (alpha, beta)。按 4 位小数缓存后命中率很高，且不影响数值（区间本身只保留 4 位）。
    """
    key = (round(float(alpha), 4), round(float(beta), 4))
    cached = _INTERVAL_CACHE.get(key)
    if cached is not None:
        return cached
    value = (round(_beta_quantile(0.025, key[0], key[1]), 4), round(_beta_quantile(0.975, key[0], key[1]), 4))
    if len(_INTERVAL_CACHE) > 8192:
        _INTERVAL_CACHE.clear()
    _INTERVAL_CACHE[key] = value
    return value


def _mastery_points(own: List[Dict[str, Any]], opportunities: Dict[str, set]) -> List[Dict[str, Any]]:
    """把评分点证据聚合成知识点掌握估计（Beta(2,2) 加权 + 精确区间）。

    单任务与多任务画像必须共用这一处计算：多任务不能对「每次任务的掌握度」取算术平均，
    必须把符合条件的底层证据重新估计。
    """
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for item in own:
        groups.setdefault(item["knowledge_point_id"], []).append(item)
    points = []
    for kp_id, items in groups.items():
        first = items[0]
        # 同一道题的多个评分点映射到同一知识点时先按题聚合：一道题只能算一个独立证据单元，
        # 不能因为拆成多个评分点就重复计入掌握程度（Beta 的线性求和保持不变，只影响证据计数与集中度）。
        by_question: Dict[str, Dict[str, Any]] = {}
        for item in items:
            weight_item = float(item["weight"]); ratio = float(item["score_ratio"])
            unit = by_question.setdefault(item["question_id"], {"weight": 0.0, "alpha": 0.0, "beta": 0.0})
            unit["weight"] += weight_item
            unit["alpha"] += weight_item * ratio
            unit["beta"] += weight_item * (1 - ratio)
        weight = sum(unit["weight"] for unit in by_question.values())
        alpha = A0 + sum(unit["alpha"] for unit in by_question.values())
        beta = B0 + sum(unit["beta"] for unit in by_question.values())
        mastery = alpha / (alpha + beta)
        low, high = _interval(alpha, beta)
        covered_questions = set(by_question)
        opportunity_count = len(opportunities.get(kp_id) or set())
        coverage = round(len(covered_questions) / opportunity_count, 4) if opportunity_count else 0
        error_weights: Dict[str, float] = {}
        error_counts: Dict[str, int] = {}
        for item in items:
            if item["score_ratio"] < 1 and item["error_type"] != CORRECT_LABEL:
                error_weights[item["error_type"]] = error_weights.get(item["error_type"], 0) + item["weight"] * (1 - item["score_ratio"])
                error_counts[item["error_type"]] = error_counts.get(item["error_type"], 0) + 1
        total_error = sum(error_weights.values())
        quality = {"ai": sum(1 for item in items if item.get("judgment_source") == "ai"), "teacher_confirmed": sum(1 for item in items if item.get("judgment_source") == "teacher_confirmed"), "legacy_mapping": sum(1 for item in items if item.get("mapping_status") == "legacy"), "confirmed_mapping": sum(1 for item in items if item.get("mapping_status") == "confirmed")}
        max_share = round(max(unit["weight"] for unit in by_question.values()) / weight, 4) if weight else 0
        points.append({
            "knowledge_point_id": kp_id, "knowledge_point_name": first["knowledge_point_name"], "topic": first["knowledge_topic"],
            "mastery_estimate": round(mastery, 4), "credible_interval_lower": low, "credible_interval_upper": high,
            "confidence_level": 0.95, "status": _status(mastery, weight, coverage, len(covered_questions)),
            "evidence_count": len(covered_questions), "evidence_point_count": len(items), "effective_evidence_weight": round(weight, 4),
            "covered_question_count": len(covered_questions), "opportunity_question_count": opportunity_count,
            "data_coverage": coverage,
            "evidence_quality": quality,
            "evidence_distribution": {"distinct_questions": len(covered_questions), "max_single_question_weight_share": max_share, "concentrated_on_single_question": max_share > 0.7},
            "error_types": [{
                "type": key, "weighted_count": round(value, 4), "ratio": round(value / total_error, 4),
                "occurrences": error_counts.get(key, 0),
                "frequency": "高频" if error_counts.get(key, 0) >= 4 else ("重复" if error_counts.get(key, 0) >= 2 else "单次"),
            } for key, value in sorted(error_weights.items(), key=lambda x: -x[1])],
        })
    points.sort(key=lambda item: (item["status"] == "证据不足", -item["effective_evidence_weight"], item["mastery_estimate"]))
    return points


def student_evidence_page(batch_id: str, student_id: str, page: int = 1, page_size: int = 20, question_id: str = "", scoring_point_id: str = "", error_type: str = "", keyword: str = "", batch_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """学生评分点证据分页：查询限定学生 + 明确任务范围，默认 20 条。"""
    evidence: List[Dict[str, Any]] = []
    for bid in (batch_ids or [batch_id]):
        if not bid:
            continue
        rows, _ = _evidence(bid)
        evidence.extend([item for item in rows if item.get("student_id") == student_id])
    def _keep(item: Dict[str, Any]) -> bool:
        if question_id and item.get("question_id") != question_id:
            return False
        if scoring_point_id and item.get("scoring_point_id") != scoring_point_id:
            return False
        if error_type and item.get("error_type") != error_type:
            return False
        if keyword:
            haystack = f"{item.get('evidence_text','')} {item.get('question_id','')} {item.get('scoring_point_id','')} {item.get('judgment_reason','')}".lower()
            if keyword.lower() not in haystack:
                return False
        return True
    filtered = [item for item in evidence if _keep(item)]
    page = max(1, int(page or 1)); page_size = max(1, min(100, int(page_size or 20)))
    start = (page - 1) * page_size
    items = []
    for item in filtered[start:start + page_size]:
        items.append({
            "answer_id": item.get("answer_id"), "student_id": item.get("student_id"), "student_code": item.get("student_code"),
            "question_id": item.get("question_id"), "scoring_point_id": item.get("scoring_point_id"),
            "earned_score": item.get("earned_score"), "full_score": item.get("full_score"), "score_ratio": item.get("score_ratio"),
            "error_type": item.get("error_type"), "error_label_raw": item.get("error_label_raw"),
            "error_label_confidence": item.get("error_label_confidence"),
            "evidence_text": item.get("evidence_text"), "judgment_source": item.get("judgment_source"),
            "mapping_status": item.get("mapping_status"), "revision": item.get("revision"), "event_id": item.get("event_id"),
            "knowledge_point_id": item.get("knowledge_point_id"), "knowledge_point_name": item.get("knowledge_point_name"),
            "judgment_reason": item.get("error_reason"),
        })
    return {
        "student_id": student_id, "batch_id": batch_id, "batch_ids": batch_ids or [batch_id],
        "total": len(filtered), "page": page, "page_size": page_size, "has_more": start + page_size < len(filtered),
        "items": items,
        "facets": {
            "questions": sorted({str(item.get("question_id")) for item in evidence}),
            "scoring_points": sorted({str(item.get("scoring_point_id")) for item in evidence}),
            "error_types": sorted({str(item.get("error_type")) for item in evidence}),
        },
        "unit": "评分点证据条数（同一评分点按评分点行计，不是学生数）",
    }


def student_profile(batch_id: str, student_id: str, evidence_override: Optional[List[Dict[str, Any]]] = None, stats_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    evidence, stats = (evidence_override, stats_override) if evidence_override is not None and stats_override is not None else _evidence(batch_id)
    grade_map: Dict[str, Optional[Dict[str, Any]]] = (stats or {}).get("_grade_map") or {}

    def _grade_for(answer_id: str) -> Optional[Dict[str, Any]]:
        # 班级画像会为每个学生建一次画像；答案级判题结果在 _evidence 里已经查过，
        # 这里复用同一份映射，避免 20 个学生重复跑 800 次 _latest_grade。
        return grade_map[answer_id] if answer_id in grade_map else _latest_grade(answer_id)

    student = db.q1("SELECT s.*, c.code class_code FROM teacher_students s LEFT JOIN teacher_classes c ON c.id=s.class_id WHERE s.id=?", (student_id,))
    if not student:
        return {"error": "学生不存在"}
    own = [item for item in evidence if item["student_id"] == student_id]
    raw_answers = db.q("SELECT * FROM teacher_answers WHERE import_batch_id=? AND student_id=? ORDER BY revision DESC", (batch_id, student_id))
    selected: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for answer in raw_answers:
        selected.setdefault((answer.get("event_id") or f"answer:{answer['id']}", answer["question_id"]), answer)
    answered_questions = len({answer["question_id"] for answer in selected.values()})
    graded_question_ids = set()
    personal_no_grade = 0
    for answer in selected.values():
        grade = _grade_for(answer["id"])
        confirmations = {row["scoring_point_id"]: row for row in db.q("SELECT * FROM teacher_scoring_point_confirmations WHERE grade_run_id=?", (grade["id"],))} if grade else {}
        if grade_usage(grade, confirmations)["score"]:
            graded_question_ids.add(answer["question_id"])
        else:
            personal_no_grade += 1
    graded_questions = len(graded_question_ids)
    covered = len({item["question_id"] for item in own})
    evidence_answers = {item["answer_id"] for item in own}
    personal_unmapped = 0
    personal_invalid = 0
    for answer in selected.values():
        grade = _grade_for(answer["id"])
        confirmations = {row["scoring_point_id"]: row for row in db.q("SELECT * FROM teacher_scoring_point_confirmations WHERE grade_run_id=?", (grade["id"],))} if grade else {}
        if not grade_usage(grade, confirmations)["score"]:
            continue
        if answer["id"] not in evidence_answers:
            personal_unmapped += 1
        try:
            steps = json.loads(grade.get("step_results_json") or "[]")
            for step in steps:
                for link in (step.get("scoring_point_links") or []):
                    full = float(link.get("max_score") or 0)
                    awarded = link.get("awarded_score")
                    awarded = link.get("score") if awarded is None else awarded
                    if full <= 0 or awarded is None or float(awarded) < 0 or float(awarded) > full:
                        personal_invalid += 1
        except Exception:
            personal_invalid += 1
    total_questions = answered_questions
    opportunities = _kp_opportunities(batch_id)
    points = _mastery_points(own, opportunities)
    weak = [item for item in points if item["status"] in ("掌握不足", "初步掌握")][:3]
    # 解题方法维度：与知识点分开统计，单位是方法而不是 KP。
    # 同一评分点可能同时映射多个知识点，证据行会重复出现；按 (answer, scoring_point, method) 去重。
    method_groups: Dict[str, Dict[Tuple[str, str, str], Dict[str, Any]]] = {}
    for item in own:
        for method in item.get("methods") or []:
            key = (item["answer_id"], item["scoring_point_id"], method["method_id"])
            method_groups.setdefault(method["method_id"], {})[key] = {"item": item, "method": method}
    opportunities = _method_opportunity_map(batch_id)
    method_names = {row["id"]: row["name"] for row in db.q("SELECT id,name FROM learning_methods")}
    methods = []
    for method_id in sorted(set(method_groups) | set(opportunities)):
        entries = list(method_groups.get(method_id, {}).values())
        opportunity = opportunities.get(method_id) or {"questions": set(), "statuses": set()}
        opportunity_questions = set(opportunity["questions"])
        observed_questions = {entry["item"]["question_id"] for entry in entries}
        total_weight = sum(float(entry["item"].get("base_weight") or 0) * float(entry["method"].get("weight") or 0) for entry in entries)
        alpha_m = A0 + sum(float(entry["item"].get("base_weight") or 0) * float(entry["method"].get("weight") or 0) * entry["item"]["score_ratio"] for entry in entries)
        beta_m = B0 + sum(float(entry["item"].get("base_weight") or 0) * float(entry["method"].get("weight") or 0) * (1 - entry["item"]["score_ratio"]) for entry in entries)
        mastery_m = alpha_m / (alpha_m + beta_m)
        low_m, high_m = _interval(alpha_m, beta_m)
        coverage = round(len(observed_questions & opportunity_questions) / len(opportunity_questions), 4) if opportunity_questions else None
        if not opportunity_questions:
            status = "暂不可计算"
            coverage_status = "机会数据缺失"
        elif not entries:
            status = "暂无证据"
            coverage_status = "有机会但无作答证据"
        else:
            status = _status(mastery_m, total_weight, coverage if coverage is not None else 0, len(observed_questions))
            coverage_status = "可计算"
        max_share = round(max(float(entry["item"].get("base_weight") or 0) * float(entry["method"].get("weight") or 0) for entry in entries) / total_weight, 4) if total_weight else None
        methods.append({
            "method_id": method_id,
            "method_name": (entries[0]["method"].get("method_name") if entries else None) or method_names.get(method_id) or method_id,
            "mastery_estimate": round(mastery_m, 4) if entries else None,
            "credible_interval_lower": low_m if entries else None, "credible_interval_upper": high_m if entries else None,
            "evidence_count": len(entries), "effective_evidence_weight": round(total_weight, 4),
            "covered_question_count": len(observed_questions),
            "opportunity_question_count": len(opportunity_questions),
            "unobserved_opportunity_count": len(opportunity_questions - observed_questions),
            "data_coverage": coverage, "coverage_status": coverage_status,
            "status": status,
            "evidence_distribution": {"max_single_question_weight_share": max_share, "concentrated_on_single_question": bool(max_share and max_share > 0.8)},
            "mapping_status": ("confirmed" if all(entry["method"].get("link_status") == "confirmed" for entry in entries) else "legacy") if entries else ("confirmed" if opportunity["statuses"] == {"confirmed"} else "legacy"),
            "opportunity_source": "sp_method_map" if opportunity_questions else "none",
        })
    method_order = {"掌握不足": 0, "初步掌握": 1, "证据不足": 2, "暂无证据": 3, "暂不可计算": 4}
    methods.sort(key=lambda item: (method_order.get(item["status"], 5), -item["effective_evidence_weight"]))
    return {
        "student": {"id": student["id"], "code": student["code"], "class_id": student["class_id"], "class_code": student.get("class_code")},
        "task_id": batch_id, "algorithm_version": ALGORITHM_VERSION, "parameter_version": PARAM_VERSION,
        "parameters": {"alpha0": A0, "beta0": B0, "min_evidence": MIN_EVIDENCE, "min_coverage": MIN_COVERAGE, "min_questions": MIN_QUESTIONS, "ci": "exact Beta equal-tailed interval (0.025, 0.975)", "coverage_denominator": "该知识点在任务中的可作答题目数"},
        "mastery": points, "weak_points": weak, "methods": methods,
        "coverage": {"expected_question_count": None, "answered_question_count": answered_questions, "graded_question_count": graded_questions, "evidence_question_count": covered, "answered_coverage": None, "graded_coverage": round(graded_questions / answered_questions, 4) if answered_questions else None, "evidence_coverage": round(covered / answered_questions, 4) if answered_questions else None},
        "evidence": own,
        "data_quality": {"raw_answers": len(raw_answers), "selected_answers": len(selected), "valid_answers": graded_questions, "no_valid_grade": personal_no_grade, "evidence_questions": covered, "evidence_count": len(own), "unmapped": personal_unmapped, "invalid_score": personal_invalid, "pending_review": personal_no_grade, "not_usable": stats.get("not_usable", {})},
        "batch_data_quality": {key: value for key, value in (stats or {}).items() if not str(key).startswith("_")},
        "suggestions": [f"针对「{item['knowledge_point_name']}」补强基础概念，并重做相关评分点题目。" for item in weak] or ["当前证据不足以形成稳定建议。"],
        "unknowns": [] if points else ["没有评分点级证据，暂不能估计掌握程度"],
    }


def _batch_question_signature(batch_id: str) -> Dict[str, Dict[str, Any]]:
    rows = db.q("SELECT question_id, version_id, max_score, scoring_points FROM teacher_question_versions WHERE batch_id=?", (batch_id,))
    return {str(row["question_id"]): {"version_id": row["version_id"], "max_score": float(row["max_score"] or 0), "scoring_points": str(row["scoring_points"] or "")} for row in rows}


def _batch_rule_versions(batch_id: str) -> Dict[str, str]:
    row = db.q1("""
        SELECT COALESCE(MAX(g.scoring_rule_version),'') scoring, COALESCE(MAX(g.standard_version),'') standard
        FROM teacher_grade_runs g JOIN teacher_answers a ON a.id=g.answer_id WHERE a.import_batch_id=?""", (batch_id,)) or {}
    return {"scoring_rule_version": str(row.get("scoring") or ""), "standard_version": str(row.get("standard") or "")}


def batch_comparability(batch_id: str, other_batch_id: str) -> Dict[str, Any]:
    """两个任务的合并可比性：映射版本、题目版本/满分/评分点、评分规则版本。

    只有显式 teacher_version_compat 登记过的差异才允许合并；否则返回不可比原因。
    """
    if not batch_id or not other_batch_id:
        return {"batch_id": other_batch_id, "comparable": True, "reasons": []}
    reasons: List[str] = []
    mapping_left = current_mapping_version(batch_id)
    mapping_right = current_mapping_version(other_batch_id)
    if mapping_left != mapping_right and not _version_compatible("mapping_version", str(mapping_left), str(mapping_right), batch_id):
        reasons.append(f"知识点映射版本不同（{mapping_left} vs {mapping_right}）")
    left_q = _batch_question_signature(batch_id)
    right_q = _batch_question_signature(other_batch_id)
    overlap = sorted(set(left_q) & set(right_q))
    for qid in overlap:
        left_item, right_item = left_q[qid], right_q[qid]
        if abs(left_item["max_score"] - right_item["max_score"]) > 1e-6:
            reasons.append(f"{qid} 满分不同（{left_item['max_score']} vs {right_item['max_score']}）")
        elif left_item["scoring_points"].strip() != right_item["scoring_points"].strip():
            if not _version_compatible("question_version", str(left_item["version_id"]), str(right_item["version_id"]), batch_id):
                reasons.append(f"{qid} 评分点定义不同且未登记兼容")
    rules_left = _batch_rule_versions(batch_id)
    rules_right = _batch_rule_versions(other_batch_id)
    for key, label in (("scoring_rule_version", "评分规则版本"), ("standard_version", "课程标准版本")):
        if rules_left[key] and rules_right[key] and rules_left[key] != rules_right[key]:
            if not _version_compatible(key, rules_left[key], rules_right[key], batch_id):
                reasons.append(f"{label}不同（{rules_left[key]} vs {rules_right[key]}）")
    return {
        "batch_id": other_batch_id, "comparable": not reasons, "reasons": reasons,
        "mapping_versions": [mapping_left, mapping_right],
        "overlap_questions": len(overlap),
        "only_in_reference": sorted(set(left_q) - set(right_q))[:20],
        "only_in_other": sorted(set(right_q) - set(left_q))[:20],
    }


def comparable_batch_selection(batch_ids: List[str], reference_batch_id: str = "") -> Dict[str, Any]:
    """把候选任务分成可比/不可比两组；参考任务始终保留。"""
    ordered = [batch_id for batch_id in dict.fromkeys(batch_ids) if batch_id]
    reference = reference_batch_id or (ordered[0] if ordered else "")
    included: List[str] = []
    excluded: List[Dict[str, Any]] = []
    details: List[Dict[str, Any]] = []
    for batch_id in ordered:
        if not reference or batch_id == reference:
            included.append(batch_id)
            continue
        result = batch_comparability(reference, batch_id)
        details.append(result)
        if result["comparable"]:
            included.append(batch_id)
        else:
            excluded.append({"batch_id": batch_id, "reasons": result["reasons"]})
    return {"reference": reference, "included": included, "excluded": excluded, "details": details}


def student_profile_multi(batch_ids: List[str], student_id: str) -> Dict[str, Any]:
    """学生「全部任务」画像：把多个任务的底层证据合并后重新估计，而不是平均各任务掌握度。

    机会题目（coverage 分母）按知识点跨任务取并集；同一道题出现在多个任务时只算一次。
    """
    student = db.q1("SELECT s.*, c.code class_code FROM teacher_students s LEFT JOIN teacher_classes c ON c.id=s.class_id WHERE s.id=?", (student_id,))
    if not student:
        return {"error": "学生不存在"}
    requested = [bid for bid in dict.fromkeys(batch_ids) if bid]
    selection = comparable_batch_selection(requested, requested[0] if requested else "")
    batch_ids = selection["included"]
    batches = {row["id"]: row for row in db.q("SELECT id, filename, created_at FROM teacher_batches WHERE id IN (" + ",".join("?" for _ in batch_ids) + ")", tuple(batch_ids))} if batch_ids else {}
    evidence: List[Dict[str, Any]] = []
    opportunities: Dict[str, set] = {}
    per_batch: List[Dict[str, Any]] = []
    answered_questions: set = set()
    graded_questions: set = set()
    raw_answers = 0
    not_usable: Dict[str, int] = {}
    for bid in batch_ids:
        rows = db.q("SELECT DISTINCT question_id FROM teacher_answers WHERE import_batch_id=? AND student_id=?", (bid, student_id))
        answered_questions.update(str(row["question_id"]) for row in rows)
        raw_answers += int(db.q1("SELECT COUNT(*) n FROM teacher_answers WHERE import_batch_id=? AND student_id=?", (bid, student_id))["n"] or 0)
        batch_evidence, batch_stats = _evidence(bid, student_id=student_id)
        evidence.extend(batch_evidence)
        for kp_id, qids in _kp_opportunities(bid).items():
            opportunities.setdefault(kp_id, set()).update(qids)
        for batch_row in db.q("SELECT a.id, a.question_id FROM teacher_answers a WHERE a.import_batch_id=? AND a.student_id=?", (bid, student_id)):
            grade = _latest_grade(batch_row["id"])
            if grade_usage(grade)["score"]:
                graded_questions.add(str(batch_row["question_id"]))
        for key, value in (batch_stats.get("not_usable") or {}).items():
            not_usable[key] = not_usable.get(key, 0) + int(value or 0)
        per_batch.append({
            "batch_id": bid, "filename": (batches.get(bid) or {}).get("filename"), "created_at": (batches.get(bid) or {}).get("created_at"),
            "answered_questions": len(rows), "evidence_count": len(batch_evidence), "evidence_coverage": round(len({item['question_id'] for item in batch_evidence}) / len(rows), 4) if rows else None,
        })
    points = _mastery_points(evidence, opportunities)
    weak = [item for item in points if item["status"] in ("掌握不足", "初步掌握")][:3]
    covered = len({item["question_id"] for item in evidence})
    return {
        "scope": "all",
        "student": {"id": student["id"], "code": student["code"], "class_id": student["class_id"], "class_code": student.get("class_code")},
        "task_id": "", "batch_ids": batch_ids,
        "excluded_batches": selection["excluded"], "comparability": selection["details"],
        "batches": [{"batch_id": bid, **{k: (batches.get(bid) or {}).get(k) for k in ("filename", "created_at")}} for bid in batch_ids],
        "algorithm_version": ALGORITHM_VERSION, "parameter_version": PARAM_VERSION,
        "parameters": {"alpha0": A0, "beta0": B0, "min_evidence": MIN_EVIDENCE, "min_coverage": MIN_COVERAGE, "min_questions": MIN_QUESTIONS, "ci": "exact Beta equal-tailed interval (0.025, 0.975)", "coverage_denominator": "该知识点在所选任务中的可作答题目并集"},
        "mastery": points, "weak_points": weak, "methods": [],
        "coverage": {"expected_question_count": None, "answered_question_count": len(answered_questions), "graded_question_count": len(graded_questions), "evidence_question_count": covered, "answered_coverage": None, "graded_coverage": round(len(graded_questions) / len(answered_questions), 4) if answered_questions else None, "evidence_coverage": round(covered / len(answered_questions), 4) if answered_questions else None},
        "evidence": evidence,
        "data_quality": {"raw_answers": raw_answers, "answered_questions": len(answered_questions), "graded_questions": len(graded_questions), "evidence_count": len(evidence), "evidence_questions": covered, "not_usable": not_usable},
        "per_batch": per_batch,
        "suggestions": [f"针对「{item['knowledge_point_name']}」补强基础概念，并重做相关评分点题目。" for item in weak] or ["当前证据不足以形成稳定建议。"],
        "unknowns": [] if points else ["所选任务内没有评分点级证据，暂不能估计掌握程度"],
    }


def _parse_points(text: str) -> Dict[str, float]:
    points = {}
    for code, score in re.findall(r"([A-Z]\d{3}P\d+)\s*(\d+(?:\.\d+)?)\s*分", str(text or "")):
        points[code] = float(score)
    return points


def _mapping_signature(codes: List[str], batch_id: str) -> Dict[str, Any]:
    result = {}
    for code in sorted(set(codes)):
        links = _knowledge_links(code, batch_id)
        result[code] = sorted([(link["knowledge_point_id"], round(float(link.get("weight") or 0), 6), str(link.get("status"))) for link in links])
    return result


def node_signature(batch_id: str, exam_node_id: str) -> Dict[str, Any]:
    """一个测验节点的可比性签名：题目版本、满分、评分点、映射、版本元数据、学生集合。"""
    rows = db.q("""
        SELECT a.question_id, a.student_id, COALESCE(v.version_id,'') version_id,
               COALESCE(v.max_score,q.max_score) max_score, COALESCE(v.scoring_points,q.scoring_points) scoring_points
        FROM teacher_answers a
        LEFT JOIN teacher_question_versions v ON v.version_id=a.question_version_id
        LEFT JOIN teacher_questions q ON q.id=a.question_id
        WHERE a.import_batch_id=? AND a.exam_node_id=?
    """, (batch_id, exam_node_id))
    questions: Dict[str, Any] = {}
    students = set()
    all_codes: List[str] = []
    for row in rows:
        students.add(row["student_id"])
        points = _parse_points(row.get("scoring_points") or "")
        all_codes.extend(points.keys())
        questions[row["question_id"]] = {
            "version_id": row.get("version_id") or "",
            "max_score": float(row.get("max_score") or 0),
            "scoring_points": points,
        }
    exam = db.q1("SELECT * FROM teacher_exams WHERE id=?", (exam_node_id,)) or {}
    corrections = db.q1("""
        SELECT COUNT(*) n FROM teacher_corrections c
        JOIN teacher_grade_runs g ON g.id=c.grade_run_id
        JOIN teacher_answers a ON a.id=g.answer_id
        WHERE a.import_batch_id=? AND a.exam_node_id=?
    """, (batch_id, exam_node_id))["n"]
    signature = {
        "exam_node_id": exam_node_id,
        "batch_id": batch_id,
        "exam_date": exam.get("exam_date") or "",
        "questions": questions,
        "question_ids": sorted(questions),
        "mapping": _mapping_signature(all_codes, batch_id),
        "student_ids": sorted(students),
        "grading_rule_version": exam.get("grading_rule_version") or "",
        "scoring_rule_version": exam.get("scoring_rule_version") or "",
        "mapping_version": exam.get("mapping_version") or "",
        "algorithm_version": ALGORITHM_VERSION,
        "parameter_version": PARAM_VERSION,
        "manual_corrections": int(corrections or 0),
    }
    signature["rule_hash"] = hashlib.sha256(json.dumps({key: signature[key] for key in ("questions", "mapping")}, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    return signature


def _version_compatible(kind: str, left: str, right: str, batch_id: str = "") -> Optional[Dict[str, Any]]:
    """显式版本兼容关系。没有记录时返回 None（不得仅凭版本字符串判断兼容）。"""
    if not left or not right:
        return None
    if left == right:
        return {"status": "same", "basis": "版本相同"}
    row = db.q1("""
        SELECT * FROM teacher_version_compat
        WHERE kind=? AND status='compatible' AND (batch_id IS NULL OR batch_id='' OR batch_id=?)
          AND ((left_version=? AND right_version=?) OR (left_version=? AND right_version=?))
        LIMIT 1
    """, (kind, batch_id, left, right, right, left))
    if not row:
        return None
    if not str(row.get("basis") or "").strip():
        return None
    return {"status": "compatible", "basis": row["basis"], "note": row.get("note") or ""}


def compare_signatures(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
    reasons: List[str] = []
    structural: List[str] = []
    version_reasons: List[str] = []
    compatibilities: Dict[str, Any] = {}
    if set(left["question_ids"]) != set(right["question_ids"]):
        structural.append(f"题目集合不同（{len(left['question_ids'])} 题 vs {len(right['question_ids'])} 题）")
    for qid in sorted(set(left["question_ids"]) & set(right["question_ids"])):
        a, b = left["questions"][qid], right["questions"][qid]
        if abs(float(a["max_score"]) - float(b["max_score"])) > 1e-6:
            structural.append(f"{qid} 满分不同（{a['max_score']} vs {b['max_score']}）")
        if a["scoring_points"] != b["scoring_points"]:
            structural.append(f"{qid} 评分点集合或分值不同")
        version_a, version_b = str(a.get("version_id") or ""), str(b.get("version_id") or "")
        if version_a and version_b and version_a != version_b:
            compat = _version_compatible("question_version", version_a, version_b, str(left.get("batch_id") or ""))
            compatibilities[f"question:{qid}"] = {"left": version_a, "right": version_b, "compat": compat}
            if compat is None:
                version_reasons.append(f"{qid} 题目版本不同且未验证等价（{version_a} vs {version_b}）")
    if left["mapping"] != right["mapping"]:
        structural.append("评分点与知识点的映射不同")
    required = ("grading_rule_version", "scoring_rule_version", "mapping_version")
    missing = [label for label in required if not str(left.get(label) or "").strip() or not str(right.get(label) or "").strip()]
    for label in required:
        value_a, value_b = str(left.get(label) or "").strip(), str(right.get(label) or "").strip()
        if not value_a or not value_b:
            continue
        compat = _version_compatible(label, value_a, value_b, str(left.get("batch_id") or ""))
        compatibilities[label] = {"left": value_a, "right": value_b, "compat": compat}
        if compat is None:
            version_reasons.append(f"{label} 不同且未验证兼容（{value_a} vs {value_b}）")
    if left.get("algorithm_version") != right.get("algorithm_version"):
        version_reasons.append(f"掌握度算法版本不同（{left.get('algorithm_version')} vs {right.get('algorithm_version')}）")
    if left.get("parameter_version") != right.get("parameter_version"):
        version_reasons.append("掌握度算法参数版本不同")
    reasons.extend(structural)
    reasons.extend(version_reasons)
    if structural:
        status = "non_comparable"
        reason = "；".join(structural)
    elif missing:
        status = "unverifiable"
        reason = "缺少 " + " / ".join(missing) + "，无法确认评分口径"
    elif version_reasons:
        status = "non_comparable"
        reason = "；".join(version_reasons)
    else:
        status = "comparable"
        same_versions = all(left.get(name) == right.get(name) for name in required)
        reason = "题目版本、评分点、映射、算法与规则版本一致" if same_versions else "版本差异已通过显式兼容记录验证"
    if left["manual_corrections"] or right["manual_corrections"]:
        reasons.append(f"存在人工修正（{left['manual_corrections']} / {right['manual_corrections']} 条），趋势需标注口径")
    overlap = sorted(set(left["student_ids"]) & set(right["student_ids"]))
    union = sorted(set(left["student_ids"]) | set(right["student_ids"]))
    overlap_rate = round(len(overlap) / len(union), 4) if union else None
    group_warning = None
    if overlap_rate is not None and overlap_rate < 0.8:
        group_warning = f"两次测验学生重合率仅 {round(overlap_rate * 100)}%，总体变化可能同时受学生群体变化影响"
    return {
        "status": status, "reason": reason, "reasons": reasons,
        "rule_hash_left": left["rule_hash"], "rule_hash_right": right["rule_hash"],
        "versions": {key: [left.get(key), right.get(key)] for key in required},
        "compatibilities": compatibilities,
        "student_overlap": overlap,
        "student_overlap_rate": overlap_rate,
        "student_only_left": sorted(set(left["student_ids"]) - set(right["student_ids"])),
        "student_only_right": sorted(set(right["student_ids"]) - set(left["student_ids"])),
        "group_change_warning": group_warning,
        "signature_diff": {"questions_only_left": sorted(set(left["question_ids"]) - set(right["question_ids"])), "questions_only_right": sorted(set(right["question_ids"]) - set(left["question_ids"]))},
    }


def ewma(values: List[Optional[float]], alpha: float = 0.4) -> List[Optional[float]]:
    """指数加权移动平均。None 不参与平滑，也不会把缺失点补成 0。"""
    result: List[Optional[float]] = []
    previous: Optional[float] = None
    for value in values:
        if value is None:
            result.append(None)
            continue
        previous = value if previous is None else alpha * float(value) + (1 - alpha) * previous
        result.append(round(previous, 4))
    return result


def _class_dimensions(batch_id: str, class_id: str, evidence: List[Dict[str, Any]]) -> Dict[str, Any]:
    answers = db.q("SELECT a.*, s.class_id, COALESCE(v.max_score,q.max_score) question_max_score FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id LEFT JOIN teacher_question_versions v ON v.version_id=a.question_version_id LEFT JOIN teacher_questions q ON q.id=a.question_id WHERE a.import_batch_id=? AND s.class_id=? ORDER BY a.exam_node_id, a.student_id, a.revision DESC", (batch_id, class_id))
    latest = {}
    for answer in answers:
        latest.setdefault((answer["student_id"], answer.get("event_id") or f"answer:{answer['id']}", answer["question_id"]), answer)
    groups: Dict[str, Dict[str, Any]] = {}
    for answer in latest.values():
        node = answer.get("exam_node_id") or "未分组"
        group = groups.setdefault(node, {"exam_node_id": node, "student_ids": set(), "question_ids": set(), "answers": 0, "graded": 0, "score_students": set(), "graded_students": set(), "failed": 0, "review": 0, "pending": 0, "score": 0.0, "max_score": 0.0})
        group["student_ids"].add(answer["student_id"]); group["question_ids"].add(answer["question_id"]); group["answers"] += 1
        grade = _latest_grade(answer["id"])
        usage = grade_usage(grade)
        if usage["state"] == "failed":
            group["failed"] += 1
        elif usage["score"]:
            group["graded"] += 1; group["graded_students"].add(answer["student_id"]); group["score_students"].add(answer["student_id"])
            group["score"] += float(grade.get("score") or 0); group["max_score"] += float(answer.get("question_max_score") or answer.get("max_score") or 0)
        elif usage["state"] == "needs_review":
            group["review"] += 1
        else:
            group["pending"] += 1
    result = []
    for node, group in groups.items():
        result.append({"exam_node_id": node, "answered_students": len(group["student_ids"]), "valid_judgement_students": len(group["graded_students"]), "valid_score_students": len(group["score_students"]), "questions": len(group["question_ids"]), "answers": group["answers"], "graded": group["graded"], "pending": group["pending"], "failed": group["failed"], "review": group["review"], "score": round(group["score"], 2), "max_score": round(group["max_score"], 2), "score_rate": round(group["score"] / group["max_score"] * 100, 2) if group["max_score"] else None})
    exams = db.q("SELECT id,exam_date,grading_rule_version,scoring_rule_version,mapping_version FROM teacher_exams WHERE id IN (SELECT DISTINCT exam_node_id FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id WHERE a.import_batch_id=? AND s.class_id=?) AND exam_date IS NOT NULL AND exam_date != '' ORDER BY exam_date", (batch_id, class_id))
    trends = []
    previous_signature: Optional[Dict[str, Any]] = None
    previous_node = ""
    for exam in exams:
        row = next((item for item in result if item["exam_node_id"] == exam["id"]), None)
        if not row:
            continue
        signature = node_signature(batch_id, exam["id"])
        if previous_signature is None:
            comparison = {"status": "unverifiable", "reason": "最早时间点，没有更早的对照时间点", "reasons": [], "versions": {}, "student_overlap": [], "signature_diff": {}}
        else:
            comparison = compare_signatures(previous_signature, signature)
        trends.append({
            "exam_node_id": exam["id"], "exam_date": exam["exam_date"],
            "answered_students": row["answered_students"], "valid_judgement_students": row["valid_judgement_students"],
            "valid_score_students": row["valid_score_students"], "score_rate": row["score_rate"], "questions": row["questions"],
            "comparable": comparison["status"] == "comparable", "comparable_status": comparison["status"],
            "comparable_reason": comparison["reason"], "comparability_detail": comparison,
            "student_overlap_rate": comparison.get("student_overlap_rate"),
            "group_change_warning": comparison.get("group_change_warning"),
            "compared_with": previous_node, "rule_hash": signature["rule_hash"],
            "sample_size": row["valid_score_students"],
        })
        previous_signature = signature
        previous_node = exam["id"]
    rates = [item["score_rate"] for item in trends]
    smoothed = ewma(rates)
    for item, value in zip(trends, smoothed):
        item["ewma_score_rate"] = value
    error_counts: Dict[str, Dict[str, Any]] = {}
    for item in evidence:
        if item["class_id"] != class_id or item["score_ratio"] >= 1 or item["error_type"] == CORRECT_LABEL:
            continue
        entry = error_counts.setdefault(item["error_type"], {"type": item["error_type"], "weighted_count": 0.0, "students": set(), "answers": set(), "knowledge_points": set(), "questions": set(), "exam_nodes": set()})
        entry["weighted_count"] += item["weight"] * (1 - item["score_ratio"])
        entry["students"].add(item["student_id"]); entry["answers"].add(item["answer_id"]); entry["knowledge_points"].add(item["knowledge_point_name"]); entry["questions"].add(item["question_id"]); entry["exam_nodes"].add(item.get("exam_node_id") or "未分组")
    total_error = sum(item["weighted_count"] for item in error_counts.values())
    errors = [{"type": item["type"], "weighted_count": round(item["weighted_count"], 4), "student_count": len(item["students"]), "answer_count": len(item["answers"]), "weighted_loss_share": round(item["weighted_count"] / total_error, 4) if total_error else None, "knowledge_points": sorted(item["knowledge_points"]), "questions": sorted(item["questions"]), "exam_nodes": sorted(item["exam_nodes"])} for item in error_counts.values()]
    return {"groups": result, "group_by": "测验节点（CSV 无独立题组字段，不虚构题组）", "groups_available": False, "group_data_reason": "CSV 没有题组编号、题组名称或题目所属题组字段", "trends": trends, "error_types": sorted(errors, key=lambda item: -item["weighted_count"])}


# ---------------------------------------------------------------- 题目适用范围与题目表现
#
# 统计单位是「学生 × 题目」，不是作答行：
# - 同一学生同一题可能有多条作答（不同测验节点、不同修订），只取当前有效版本；
# - 有效判题 / 待复核 / 失败 / 未判题 四类互斥；
# - 实际作答数按学生数统计，永远不超过应作答数；
# - 应作答来自题目适用范围（题单）与参与学生的交集，缺少题单时明确标记不可计算，
#   绝不用「班级人数 × 题目数量」代替。


def _has_table(name: str) -> bool:
    return bool(db.q1("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)))


def _split_question_ids(text: Any) -> List[str]:
    return sorted(set(re.findall(r"\bQ\d{3}\b", str(text or ""))))


def _question_assignment_map(batch_id: str) -> Tuple[Dict[Tuple[str, str], set], Dict[Tuple[str, str], set], Dict[str, Any]]:
    """返回 (班级级题单, 学生级题单, 元信息)。

    优先级：teacher_question_assignments（教师/导入的显式题单）> teacher_exams.available_questions。
    两者都没有时返回空表，调用方必须把「应作答」标为不可计算。
    """
    meta: Dict[str, Any] = {"source": "none", "student_level": False, "class_explicit": False, "note": ""}
    class_map: Dict[Tuple[str, str], set] = {}
    student_map: Dict[Tuple[str, str], set] = {}
    if _has_table("teacher_question_assignments"):
        for row in db.q("SELECT exam_node_id, class_id, student_id, question_id, source FROM teacher_question_assignments WHERE batch_id=?", (batch_id,)):
            node = str(row.get("exam_node_id") or "")
            question = str(row.get("question_id") or "")
            if not node or not question:
                continue
            if str(row.get("student_id") or ""):
                student_map.setdefault((node, str(row["student_id"])), set()).add(question)
                meta["student_level"] = True
            else:
                class_map.setdefault((node, str(row.get("class_id") or "")), set()).add(question)
                if str(row.get("source") or "") != "exam_available":
                    meta["class_explicit"] = True
        if class_map or student_map:
            meta["source"] = "assignments"
            meta["note"] = "使用任务题单（可到学生级）"
            return class_map, student_map, meta
    nodes = [row["exam_node_id"] for row in db.q("SELECT DISTINCT exam_node_id FROM teacher_answers WHERE import_batch_id=? AND COALESCE(exam_node_id,'')<>''", (batch_id,))]
    classes = [row["class_id"] for row in db.q("SELECT DISTINCT s.class_id FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id WHERE a.import_batch_id=? AND COALESCE(s.class_id,'')<>''", (batch_id,))]
    for node in nodes:
        exam = db.q1("SELECT available_questions FROM teacher_exams WHERE id=?", (node,)) or {}
        questions = set(_split_question_ids(exam.get("available_questions")))
        if not questions:
            continue
        for class_id in classes:
            class_map[(node, class_id)] = set(questions)
    if class_map:
        meta["source"] = "exam_available"
        meta["note"] = "使用测验节点配置的可用题目（班级级）"
    else:
        meta["note"] = "任务没有题目适用范围数据，应作答不可计算"
    return class_map, student_map, meta


def _node_participants(batch_id: str) -> Dict[Tuple[str, str], set]:
    result: Dict[Tuple[str, str], set] = {}
    for row in db.q("""
        SELECT DISTINCT a.exam_node_id, s.class_id, a.student_id
        FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id
        WHERE a.import_batch_id=? AND COALESCE(a.exam_node_id,'')<>''""", (batch_id,)):
        result.setdefault((row["exam_node_id"], row["class_id"] or ""), set()).add(row["student_id"])
    return result


def _is_submission(row: Dict[str, Any]) -> bool:
    return bool(str(row.get("original_answer") or "").strip() or str(row.get("solution_process") or "").strip())


def _effective_answer_pairs(batch_id: str, class_id: str = "") -> Dict[Tuple[str, str], Dict[str, Any]]:
    sql = """
        SELECT a.*, s.code student_code, s.class_id,
               COALESCE(v.stem,q.stem) stem, COALESCE(v.question_type,q.question_type) question_type,
               COALESCE(v.max_score,q.max_score) question_max_score, COALESCE(v.scoring_points,q.scoring_points) scoring_points
        FROM teacher_answers a
        JOIN teacher_students s ON s.id=a.student_id
        LEFT JOIN teacher_questions q ON q.id=a.question_id
        LEFT JOIN teacher_question_versions v ON v.version_id=a.question_version_id
        WHERE a.import_batch_id=?"""
    args: List[Any] = [batch_id]
    if class_id:
        sql += " AND s.class_id=?"
        args.append(class_id)
    sql += " ORDER BY a.student_id, a.question_id, a.revision DESC, COALESCE(a.submit_time,'') DESC, a.id DESC"
    pairs: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in db.q(sql, tuple(args)):
        key = (row["student_id"], row["question_id"])
        pair = pairs.setdefault(key, {"effective": row, "submitted": False, "records": 0})
        pair["records"] += 1
        if _is_submission(row):
            pair["submitted"] = True
    return pairs


def question_performance(batch_id: str, class_id: str = "") -> Dict[str, Any]:
    """题目表现：按学生对题目去重后的应作答/实际作答/状态分区与分数。"""
    pairs = _effective_answer_pairs(batch_id, class_id)
    class_map, student_map, meta = _question_assignment_map(batch_id)
    participants = _node_participants(batch_id)
    scope_classes = sorted({row["class_id"] for row in db.q("SELECT DISTINCT s.class_id FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id WHERE a.import_batch_id=? AND COALESCE(s.class_id,'')<>''", (batch_id,))}) if not class_id else [class_id]
    student_class = {row["id"]: row["class_id"] for row in db.q("SELECT id, class_id FROM teacher_students")}
    questions = sorted({question for (_sid, question) in pairs} | {question for values in class_map.values() for question in values})
    if not questions:
        return {"items": [], "summary": {"questions": 0}, "basis": meta, "warnings": []}
    grouped: Dict[str, Dict[str, Any]] = {}
    for (student_id, question_id), pair in pairs.items():
        item = grouped.setdefault(question_id, {
            "question_id": question_id, "stem": pair["effective"].get("stem"), "question_type": pair["effective"].get("question_type"),
            "max_scores": set(), "answered": set(), "graded": set(), "review": set(), "failed": set(), "pending": set(),
            "scores": {}, "distribution": {"0-59": 0, "60-79": 0, "80-100": 0},
            "scoring_points": {}, "error_types": {}, "manual_unsynced": 0, "records": 0, "unsubmitted": set(),
            "confirmed_unsubmitted": set(), "unknown_submission": set(),
        })
        if pair["effective"].get("question_max_score") is not None:
            item["max_scores"].add(float(pair["effective"]["question_max_score"]))
        item["records"] += pair["records"]
        if not pair["submitted"]:
            item["unsubmitted"].add(student_id)
            score_status = str(pair["effective"].get("score_status") or "").strip().lower()
            if score_status in ("unanswered", "unsubmitted", "未提交"):
                item["confirmed_unsubmitted"].add(student_id)
            else:
                item["unknown_submission"].add(student_id)
            continue
        item["answered"].add(student_id)
        effective = pair["effective"]
        grade = _latest_grade(effective["id"])
        confirmed = {row["scoring_point_id"]: row for row in db.q("SELECT * FROM teacher_scoring_point_confirmations WHERE grade_run_id=?", (grade["id"],))} if grade else {}
        usage = grade_usage(grade, confirmed)
        if usage["state"] == "failed":
            item["failed"].add(student_id); continue
        if usage["state"] == "needs_review":
            item["review"].add(student_id); continue
        if not usage["score"]:
            item["pending"].add(student_id); continue
        item["graded"].add(student_id)
        score = float(grade.get("score") or 0)
        item["scores"][student_id] = score
        if usage["state"] == "manual_corrected" and not usage["evidence"]:
            item["manual_unsynced"] += 1
        try:
            steps = json.loads(grade.get("step_results_json") or "[]")
        except Exception:
            steps = []
        if usage["state"] == "manual_corrected" and not usage["evidence"]:
            continue
        seen_points = set()
        for step in steps:
            for link in (step.get("scoring_point_links") or []):
                if not isinstance(link, dict):
                    continue
                sp = str(link.get("scoring_point_id") or "")
                if not sp or sp in seen_points:
                    continue
                seen_points.add(sp)
                full = float(link.get("max_score") or 0)
                awarded = link.get("awarded_score")
                awarded = link.get("score") if awarded is None else awarded
                awarded = float(awarded or 0)
                if sp in confirmed:
                    awarded = float(confirmed[sp].get("confirmed_score") or 0)
                    full = float(confirmed[sp].get("max_score") or full)
                sp_item = item["scoring_points"].setdefault(sp, {"scoring_point_id": sp, "full_scores": set(), "total_earned": 0.0, "valid_students": set(), "events": 0, "student_scores": {}, "unmapped_knowledge_count": 0, "invalid_score_count": 0})
                sp_item["full_scores"].add(full)
                sp_item["total_earned"] += awarded
                sp_item["valid_students"].add(student_id)
                sp_item["events"] += 1
                sp_item["student_scores"].setdefault(student_id, []).append(awarded)
                if awarded < 0 or awarded > full:
                    sp_item["invalid_score_count"] += 1
                if not _knowledge_links(sp, batch_id):
                    sp_item["unmapped_knowledge_count"] += 1
                error = CORRECT_LABEL if awarded >= full - 1e-9 else _canonical_error(step.get("error_type"))
                if awarded < full and error != CORRECT_LABEL:
                    entry = item["error_types"].setdefault(error, {"students": set(), "answers": 0, "scoring_points": set()})
                    entry["students"].add(student_id)
                    entry["answers"] += 1
                    entry["scoring_points"].add(sp)
    results: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for question_id in questions:
        item = grouped.get(question_id)
        nodes_for_question = sorted(node for (node, _cls), qids in class_map.items() if question_id in qids)
        expected_students: set = set()
        pool_students: set = set()
        basis = "unknown"
        # 只有“逐个学生”的题单才是可靠的应作答范围；班级级题单若是从
        # teacher_exams.available_questions 回填的题目池，不能当作每人必做。
        if student_map and any(question_id in qids for qids in student_map.values()):
            expected_students = {student for (node, student), qids in student_map.items() if question_id in qids and (not class_id or student_class.get(student) == class_id)}
            basis = "assignment"
        elif meta.get("class_explicit") and nodes_for_question:
            for node in nodes_for_question:
                for scope_class in scope_classes:
                    expected_students |= participants.get((node, scope_class), set())
            basis = "class_roster"
        elif nodes_for_question:
            # 抽测节点只说明“这个题目在这个节点出现过”，不说明每个学生应做；
            # 仅作为题目池参考，不能据此推算未提交。
            for node in nodes_for_question:
                for scope_class in scope_classes:
                    pool_students |= participants.get((node, scope_class), set())
            basis = "unknown"
        elif class_map or student_map:
            basis = "out_of_scope"
        answered = set(item["answered"]) if item else set()
        out_of_scope = answered - (expected_students or pool_students) if basis != "unknown" else set()
        not_answered = expected_students - answered if basis in ("assignment", "class_roster") else None
        max_scores = item["max_scores"] if item else set()
        max_score = next(iter(max_scores)) if len(max_scores) == 1 else (max(max_scores) if max_scores else None)
        scores = list(item["scores"].values()) if item else []
        student_average = sum(scores) / len(scores) if scores else None
        graded = len(item["graded"]) if item else 0
        missing = max(0, len(expected_students) - len(answered)) if basis != "uncomputable" else 0
        points = []
        if item:
            for sp in item["scoring_points"].values():
                full_values = sorted(sp["full_scores"])
                full = full_values[0] if len(full_values) == 1 else None
                valid = len(sp["valid_students"])
                event_average = sp["total_earned"] / sp["events"] if sp["events"] else None
                sp_student_means = [sum(values) / len(values) for values in sp["student_scores"].values() if values]
                student_average_sp = sum(sp_student_means) / len(sp_student_means) if sp_student_means else None
                points.append({
                    "scoring_point_id": sp["scoring_point_id"], "full_score": full, "full_score_consistent": len(full_values) <= 1,
                    "total_earned_score": round(sp["total_earned"], 2), "valid_events": sp["events"], "valid_students": valid,
                    "event_average_score": round(event_average, 2) if event_average is not None else None,
                    "student_average_score": round(student_average_sp, 2) if student_average_sp is not None else None,
                    "event_average_score_rate": round(event_average / full * 100, 2) if event_average is not None and full else None,
                    "student_average_score_rate": round(student_average_sp / full * 100, 2) if student_average_sp is not None and full else None,
                    "unmapped_knowledge_count": sp["unmapped_knowledge_count"], "invalid_score_count": sp["invalid_score_count"],
                    "missing_scoring_point_result_count": max(0, graded - valid), "insufficient_count": 0,
                })
        results.append({
            "question_id": question_id,
            "stem": item["stem"] if item else None,
            "question_type": item["question_type"] if item else None,
            "max_score": max_score,
            "max_score_consistent": len(max_scores) <= 1,
            "expected": 0 if basis == "out_of_scope" else (len(expected_students) if basis in ("assignment", "class_roster") else None),
            "pool_reference": len(pool_students) if basis == "unknown" else None,
            "answered": len(answered),
            "not_answered": 0 if basis == "out_of_scope" else (len(not_answered) if not_answered is not None else None),
            "confirmed_unsubmitted": len(item.get("confirmed_unsubmitted") or []) if item else 0,
            "unknown_submission": len(item.get("unknown_submission") or []) if item else 0,
            "graded": graded,
            "review": len(item["review"]) if item else 0,
            "failed": len(item["failed"]) if item else 0,
            "pending": len(item["pending"]) if item else 0,
            "unsubmitted_records": len(item["unsubmitted"]) if item else 0,
            "out_of_scope_answered": len(out_of_scope),
            "expected_basis": basis,
            "expected_note": (
                "应作答 = 逐个学生题单指定的题目" if basis == "assignment"
                else "应作答 = 班级题单 + 班级在册学生" if basis == "class_roster"
                else "题目不在任何已给适用范围：应作答 0，作答按超出范围处理" if basis == "out_of_scope"
                else "没有逐生题单：题目池不能推算未提交，应作答范围无法确定"
            ),
            "coverage_rate": round(len(answered) / len(expected_students), 4) if basis in ("assignment", "class_roster") and expected_students else None,
            "valid_rate": round(graded / len(expected_students), 4) if basis in ("assignment", "class_roster") and expected_students else None,
            "student_average_score": round(student_average, 2) if student_average is not None else None,
            "average_score": round(student_average, 2) if student_average is not None else None,
            "score_rate": round(student_average / max_score * 100, 2) if student_average is not None and max_score else None,
            "student_average_score_rate": round(student_average / max_score * 100, 2) if student_average is not None and max_score else None,
            "score_distribution": item["distribution"] if item else {"0-59": 0, "60-79": 0, "80-100": 0},
            "scoring_points": points,
            "error_types": [{"type": key, "student_count": len(value["students"]), "answer_count": value["answers"], "scoring_points": sorted(value["scoring_points"])} for key, value in (item["error_types"].items() if item else [])],
            "manual_correction_without_scoring_sync": item["manual_unsynced"] if item else 0,
            # 兼容旧字段名（前端与既有测试仍在使用）
            "answered_students": len(answered),
            "answered_events": len(answered),
            "valid_judgement_students": graded,
            "valid_judgement_events": graded,
            "valid_score_students": graded,
            "review_students": len(item["review"]) if item else 0,
            "failed_students": len(item["failed"]) if item else 0,
            "pending_students": len(item["pending"]) if item else 0,
            "student_score_unit_count": graded,
        })
        if item and item["scores"]:
            for score in item["scores"].values():
                if max_score:
                    rate = score / max_score * 100
                    item["distribution"]["0-59" if rate < 60 else ("60-79" if rate < 80 else "80-100")] += 1
            results[-1]["score_distribution"] = item["distribution"]
    if class_id:
        results = [row for row in results if int(row.get("answered") or 0) > 0 or int(row.get("expected") or 0) > 0]
    unknown_questions = [row["question_id"] for row in results if row["expected_basis"] == "unknown"]
    for row in results:
        if row["expected"] is not None and row["answered"] > row["expected"]:
            warnings.append(f"{row['question_id']} 实际提交 {row['answered']} 超过应作答 {row['expected']}（题单与参与数据不一致）")
    if unknown_questions:
        warnings.append(f"{len(unknown_questions)} 道题没有逐生题单，题目池不能推算未提交，应作答范围标记为无法确定")
    summary = {
        "questions": len(results),
        "expected": sum(row["expected"] or 0 for row in results) if results and all(row["expected"] is not None for row in results) else None,
        "expected_known": bool(results) and all(row["expected"] is not None for row in results),
        "expected_known_questions": sum(1 for row in results if row["expected"] is not None),
        "unknown_scope_questions": len(unknown_questions),
        "pool_reference": sum(row["pool_reference"] or 0 for row in results),
        "answered": sum(row["answered"] for row in results),
        "not_answered": sum(row["not_answered"] or 0 for row in results) if all(row["not_answered"] is not None for row in results) else None,
        "confirmed_unsubmitted": sum(row["confirmed_unsubmitted"] for row in results),
        "unknown_submission": sum(row["unknown_submission"] for row in results),
        "graded": sum(row["graded"] for row in results),
        "review": sum(row["review"] for row in results),
        "failed": sum(row["failed"] for row in results),
        "pending": sum(row["pending"] for row in results),
        "out_of_scope_answered": sum(row["out_of_scope_answered"] for row in results),
    }
    summary["partition_ok"] = summary["answered"] == summary["graded"] + summary["review"] + summary["failed"] + summary["pending"]
    summary["in_scope_answered"] = summary["answered"] - summary["out_of_scope_answered"]
    summary["expected_ok"] = (
        summary["expected"] == summary["in_scope_answered"] + summary["not_answered"]
        if summary["expected_known"] and summary["not_answered"] is not None else None
    )
    return {"items": results, "summary": summary, "basis": meta, "warnings": sorted(set(warnings))}


def _question_analysis(batch_id: str, class_id: str) -> List[Dict[str, Any]]:
    return question_performance(batch_id, class_id)["items"]


def question_expected_students(batch_id: str, class_id: str, question_id: str) -> set:
    class_map, student_map, meta = _question_assignment_map(batch_id)
    participants = _node_participants(batch_id)
    nodes = sorted(node for (node, _cls), qids in class_map.items() if question_id in qids)
    student_class = {row["id"]: row["class_id"] for row in db.q("SELECT id, class_id FROM teacher_students")}
    if student_map and any(question_id in qids for qids in student_map.values()):
        return {student for (node, student), qids in student_map.items() if question_id in qids and (not class_id or student_class.get(student) == class_id)}
    if not meta.get("class_explicit"):
        return set()
    scope_classes = [class_id] if class_id else sorted({row["class_id"] for row in db.q("SELECT DISTINCT s.class_id FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id WHERE a.import_batch_id=? AND COALESCE(s.class_id,'')<>''", (batch_id,))})
    expected: set = set()
    for node in nodes:
        for scope_class in scope_classes:
            expected |= participants.get((node, scope_class), set())
    return expected


def question_analysis_detail(batch_id: str, class_id: str, question_id: str) -> Dict[str, Any]:
    summary = next((row for row in question_performance(batch_id, class_id)["items"] if row["question_id"] == question_id), None)
    if not summary:
        return {"error": "当前批次/班级没有这道题"}
    rows = db.q("SELECT a.*, s.code student_code, s.class_id, COALESCE(v.stem,q.stem) stem, COALESCE(v.question_type,q.question_type) question_type, COALESCE(v.max_score,q.max_score) question_max_score, COALESCE(v.reference_answer,q.reference_answer) reference_answer, COALESCE(v.reference_solution,q.reference_solution) reference_solution, COALESCE(v.scoring_points,q.scoring_points) scoring_points FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id LEFT JOIN teacher_questions q ON q.id=a.question_id LEFT JOIN teacher_question_versions v ON v.version_id=a.question_version_id WHERE a.import_batch_id=? AND s.class_id=? AND a.question_id=? ORDER BY a.student_id, a.revision DESC, COALESCE(a.submit_time,'') DESC, a.id DESC", (batch_id, class_id, question_id))
    pairs: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        pair = pairs.setdefault(row["student_id"], {"effective": row, "submitted": False, "records": 0})
        pair["records"] += 1
        if _is_submission(row):
            pair["submitted"] = True
    expected = question_expected_students(batch_id, class_id, question_id)
    student_codes = {row["id"]: row["code"] for row in db.q("SELECT id, code FROM teacher_students")}
    distribution = {"0-59": 0, "60-79": 0, "80-100": 0}
    students: List[Dict[str, Any]] = []
    ordered = sorted(expected | set(pairs), key=lambda sid: student_codes.get(sid) or sid)
    for student_id in ordered:
        pair = pairs.get(student_id)
        entry = {"student_id": student_id, "student_code": student_codes.get(student_id), "answer_id": None, "event_id": None, "revision": None, "answer": "", "solution_process": "", "status": "not_answered", "grade_id": None, "score": None, "max_score": summary.get("max_score"), "score_rate": None, "scoring_points": [], "error_type": "未作答", "judgment_reason": "应作答但没有任何提交记录", "manual": False, "expected": student_id in expected, "out_of_scope": student_id not in expected}
        if pair and pair["submitted"]:
            row = pair["effective"]
            grade = _latest_grade(row["id"])
            status = "pending"; score = None; sp_scores = []; error = "未判定"; reason = ""; text = row.get("solution_process") or row.get("original_answer") or ""
            if grade:
                status = grade.get("status") or "graded"; reason = grade.get("judgment_reason") or ""
                confirmed = {item["scoring_point_id"]: item for item in db.q("SELECT * FROM teacher_scoring_point_confirmations WHERE grade_run_id=?", (grade["id"],))}
                usage = grade_usage(grade, confirmed)
                if usage["state"] == "failed":
                    status = "failed"; score = None
                elif usage["state"] == "needs_review":
                    status = "needs_review"; score = None
                elif usage["score"]:
                    status = "graded" if usage["state"] in ("graded", "manual_confirmed") else usage["state"]
                    score = grade.get("score")
                    try:
                        steps = json.loads(grade.get("step_results_json") or "[]")
                    except Exception:
                        steps = []
                    seen = set()
                    for step in steps:
                        for link in (step.get("scoring_point_links") or []):
                            if not isinstance(link, dict):
                                continue
                            sp = link.get("scoring_point_id")
                            if sp in seen:
                                continue
                            seen.add(sp)
                            awarded = link.get("awarded_score"); awarded = link.get("score") if awarded is None else awarded
                            sp_scores.append({"scoring_point_id": sp, "awarded_score": awarded, "max_score": link.get("max_score")})
                        if step.get("error_type"):
                            error = _canonical_error(step.get("error_type"))
                    if score is not None and row.get("question_max_score"):
                        rate = float(score) / float(row["question_max_score"]) * 100
                        distribution["0-59" if rate < 60 else ("60-79" if rate < 80 else "80-100")] += 1
            entry.update({"answer_id": row["id"], "event_id": row.get("event_id"), "revision": row.get("revision"), "answer": row.get("original_answer"), "solution_process": text, "status": status, "grade_id": grade.get("id") if grade else None, "score": score, "max_score": row.get("question_max_score"), "score_rate": round(float(score) / float(row["question_max_score"]) * 100, 2) if score is not None and row.get("question_max_score") else None, "scoring_points": sp_scores, "error_type": error, "judgment_reason": reason, "manual": status in ("manual_corrected", "manual_confirmed")})
        elif pair:
            explicit_absent = str(pair["effective"].get("score_status") or "").lower() in ("unanswered", "unsubmitted", "未提交")
            entry.update({
                "answer_id": pair["effective"]["id"], "event_id": pair["effective"].get("event_id"), "revision": pair["effective"].get("revision"),
                "answer": pair["effective"].get("original_answer"), "solution_process": pair["effective"].get("solution_process"),
                "status": "not_answered" if explicit_absent else "pending",
                "judgment_reason": "明确标记未提交" if explicit_absent else "提交记录为空，无法确定是未提交还是数据缺失",
            })
        students.append(entry)
    lists = {"answered": [], "graded": [], "review": [], "failed": [], "pending": [], "confirmed_unsubmitted": [], "not_answered": [], "out_of_scope": []}
    for entry in students:
        code = entry.get("student_code") or entry.get("student_id")
        if entry.get("status") == "not_answered":
            lists["not_answered"].append(code)
        elif entry.get("status") in lists:
            lists[entry["status"]].append(code)
        if entry.get("out_of_scope"):
            lists["out_of_scope"].append(code)
    for student_id, pair in pairs.items():
        if not pair["submitted"] and str(pair["effective"].get("score_status") or "").lower() in ("unanswered", "unsubmitted", "未提交"):
            lists["confirmed_unsubmitted"].append(student_codes.get(student_id) or student_id)
    return {
        "question": {"question_id": question_id, "stem": summary["stem"], "question_type": summary["question_type"], "max_score": summary["max_score"], "reference_answer": rows[0].get("reference_answer") if rows else "", "reference_solution": rows[0].get("reference_solution") if rows else "", "scoring_points": summary["scoring_points"]},
        "overall": summary, "score_distribution": distribution, "scoring_points": summary["scoring_points"], "error_types": summary["error_types"],
        "students": students,
        "scope": {"basis": summary.get("expected_basis"), "note": summary.get("expected_note"), "expected": summary.get("expected"), "pool_reference": summary.get("pool_reference")},
        "lists": lists,
        "partition": {"expected": summary.get("expected"), "answered": summary.get("answered"), "not_answered": summary.get("not_answered"), "confirmed_unsubmitted": summary.get("confirmed_unsubmitted"), "graded": summary.get("graded"), "review": summary.get("review"), "failed": summary.get("failed"), "pending": summary.get("pending")},
        "longitudinal": {"available": False, "reason": "当前题目没有可确认的多个可比独立测量时间点"},
    }


def question_signature(batch_id: str, exam_node_id: str, question_id: str) -> Dict[str, Any]:
    full = node_signature(batch_id, exam_node_id)
    question = full["questions"].get(question_id)
    if not question:
        return {}
    codes = sorted(question["scoring_points"].keys())
    filtered = dict(full)
    filtered["questions"] = {question_id: question}
    filtered["question_ids"] = [question_id]
    filtered["mapping"] = {code: full["mapping"].get(code, []) for code in codes}
    filtered["rule_hash"] = hashlib.sha256(json.dumps({key: filtered[key] for key in ("questions", "mapping")}, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    return filtered


def question_longitudinal(batch_id: str, class_id: str, question_id: str) -> Dict[str, Any]:
    rows = db.q("""
        SELECT a.*, s.code student_code, COALESCE(v.max_score,q.max_score) question_max_score,
               COALESCE(v.scoring_points,q.scoring_points) scoring_points, v.version_id, e.exam_date
        FROM teacher_answers a
        JOIN teacher_students s ON s.id=a.student_id
        LEFT JOIN teacher_questions q ON q.id=a.question_id
        LEFT JOIN teacher_question_versions v ON v.version_id=a.question_version_id
        LEFT JOIN teacher_exams e ON e.id=a.exam_node_id
        WHERE a.import_batch_id=? AND s.class_id=? AND a.question_id=?
        ORDER BY e.exam_date, a.student_id, a.revision DESC
    """, (batch_id, class_id, question_id))
    latest = {}
    for row in rows:
        latest.setdefault((row["student_id"], row.get("exam_node_id") or "unknown"), row)
    points: Dict[str, Dict[str, Any]] = {}
    for row in latest.values():
        node = row.get("exam_node_id") or "unknown"
        point = points.setdefault(node, {
            "exam_node_id": node, "exam_date": row.get("exam_date") or "", "students": set(), "scores": [],
            "max_scores": set(), "scoring_sets": set(), "versions": set(), "graded": 0, "failed": 0, "review": 0,
            "scoring_points": {},
        })
        point["students"].add(row["student_id"])
        point["max_scores"].add(row.get("question_max_score"))
        point["scoring_sets"].add(str(row.get("scoring_points") or ""))
        point["versions"].add(row.get("version_id") or "")
        grade = _latest_grade(row["id"])
        confirmed = {item["scoring_point_id"]: item for item in db.q("SELECT * FROM teacher_scoring_point_confirmations WHERE grade_run_id=?", (grade["id"],))} if grade else {}
        usage = grade_usage(grade, confirmed)
        if usage["state"] == "failed":
            point["failed"] += 1
            continue
        if usage["state"] == "needs_review":
            point["review"] += 1
            continue
        if not usage["score"]:
            continue
        point["scores"].append(float(grade.get("score") or 0)); point["graded"] += 1
        if not usage["evidence"]:
            continue
        try:
            steps = json.loads(grade.get("step_results_json") or "[]")
        except Exception:
            steps = []
        for step in steps:
            for link in (step.get("scoring_point_links") or []):
                if not isinstance(link, dict):
                    continue
                sp = str(link.get("scoring_point_id") or "")
                if not sp:
                    continue
                full_score = float(link.get("max_score") or 0)
                awarded = link.get("awarded_score"); awarded = link.get("score") if awarded is None else awarded
                awarded = float(awarded or 0)
                if sp in confirmed:
                    awarded = float(confirmed[sp].get("confirmed_score") or 0)
                    full_score = float(confirmed[sp].get("max_score") or full_score)
                entry = point["scoring_points"].setdefault(sp, {"scoring_point_id": sp, "full_score": full_score, "earned": [], "students": set()})
                entry["earned"].append(awarded); entry["students"].add(row["student_id"])
    timeline = []
    for node, point in points.items():
        max_score = next(iter(point["max_scores"]), None) if len(point["max_scores"]) == 1 else None
        scoring_points = []
        for sp in point["scoring_points"].values():
            scoring_points.append({
                "scoring_point_id": sp["scoring_point_id"], "full_score": sp["full_score"],
                "valid_students": len(sp["students"]),
                "average_earned": round(sum(sp["earned"]) / len(sp["earned"]), 2) if sp["earned"] else None,
                "score_rate": round(sum(sp["earned"]) / (len(sp["earned"]) * sp["full_score"]) * 100, 2) if sp["earned"] and sp["full_score"] else None,
            })
        timeline.append({
            "exam_node_id": node, "exam_date": point["exam_date"],
            "answered_students": len(point["students"]), "student_ids": sorted(point["students"]),
            "valid_judgement_students": point["graded"], "review_students": point["review"], "failed_students": point["failed"],
            "average_score": round(sum(point["scores"]) / len(point["scores"]), 2) if point["scores"] else None,
            "score_rate": round(sum(point["scores"]) / (len(point["scores"]) * max_score) * 100, 2) if point["scores"] and max_score else None,
            "max_score": max_score, "max_score_consistent": len(point["max_scores"]) <= 1,
            "scoring_points_consistent": len(point["scoring_sets"]) <= 1,
            "versions": sorted(point["versions"]), "scoring_points": sorted(scoring_points, key=lambda item: item["scoring_point_id"]),
            "sample_size": point["graded"],
        })
    timeline = [item for item in timeline if item["exam_date"]]
    timeline.sort(key=lambda item: item["exam_date"])
    comparisons = []
    for index in range(1, len(timeline)):
        left = question_signature(batch_id, timeline[index - 1]["exam_node_id"], question_id)
        right = question_signature(batch_id, timeline[index]["exam_node_id"], question_id)
        comparisons.append(compare_signatures(left, right) if left and right else {"status": "unverifiable", "reason": "缺少题目版本信息", "reasons": []})
    if len(timeline) < 2:
        status, reason = "insufficient", "可确认的独立测量时间点不足两个"
    elif any(item["status"] == "non_comparable" for item in comparisons):
        status = "non_comparable"
        reason = "；".join({item["reason"] for item in comparisons if item["status"] == "non_comparable"})
    elif all(item["status"] == "comparable" for item in comparisons):
        status, reason = "comparable", "题目版本、评分点、映射与规则版本一致"
    else:
        status = "unverifiable"
        reason = "；".join({item["reason"] for item in comparisons if item["status"] != "comparable"})
    rates = [item["score_rate"] for item in timeline]
    for item, value in zip(timeline, ewma(rates)):
        item["ewma_score_rate"] = value
    student_sets = [set(item["student_ids"]) for item in timeline]
    return {
        "task_id": batch_id, "class_id": class_id, "question_id": question_id,
        "status": status, "reason": reason, "points": timeline, "comparisons": comparisons,
        "student_set_comparison": {
            "common": sorted(set.intersection(*student_sets)) if student_sets else [],
            "per_point": [{"exam_node_id": item["exam_node_id"], "students": len(item["student_ids"])} for item in timeline],
        },
        "note": "EWMA 只用于展示趋势，原始分数始终保留在 points 中；样本量小的点不构成统计结论。",
    }


def class_profile(batch_id: str, class_id: str, batch_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    cls = db.q1("SELECT * FROM teacher_classes WHERE id=?", (class_id,))
    if not cls:
        return {"error": "班级不存在"}
    students = db.q("SELECT * FROM teacher_students WHERE class_id=? ORDER BY code", (class_id,))
    stats: Dict[str, Any] = {}
    if batch_ids:
        # 多任务：先按可比性筛选（映射/题目/规则版本），再合并底层证据重估；
        # 不允许把「每次任务的班级均值」再取平均，也不允许混入不可比任务。
        selection = comparable_batch_selection(batch_ids, batch_ids[0])
        batch_ids = selection["included"]
        evidence: List[Dict[str, Any]] = []
        for bid in batch_ids:
            batch_evidence, _ = _evidence(bid)
            evidence.extend([item for item in batch_evidence if str(item.get("class_id") or "") == class_id])
        profiles = [student_profile_multi(batch_ids, student["id"]) for student in students]
        questions: List[Dict[str, Any]] = []
    else:
        evidence, stats = _evidence(batch_id)
        evidence = [item for item in evidence if str(item.get("class_id") or "") == class_id]
        profiles = [student_profile(batch_id, student["id"], evidence, stats) for student in students]
        questions = _question_analysis(batch_id, class_id)
    questions = [q for q in questions if any(int(q.get(key) or 0) > 0 for key in ("expected", "answered", "graded", "review", "failed", "pending", "confirmed_unsubmitted"))]
    valid_profiles = [item for item in profiles if any(point.get("status") != "证据不足" for point in item.get("mastery", []))]
    evidence_by_kp: Dict[str, Dict[str, Any]] = {}
    for row in evidence:
        entry = evidence_by_kp.setdefault(row["knowledge_point_id"], {"students": set(), "answers": set(), "points": set()})
        entry["students"].add(row["student_id"])
        entry["answers"].add(row["answer_id"])
        entry["points"].add(row["scoring_point_id"])
    points: Dict[str, Dict[str, Any]] = {}
    for profile in valid_profiles:
        for point in profile["mastery"]:
            if point["status"] == "证据不足":
                continue
            entry = points.setdefault(point["knowledge_point_id"], {"knowledge_point_id": point["knowledge_point_id"], "name": point["knowledge_point_name"], "values": [], "evidence": [], "coverages": [], "evidence_detail": []})
            entry["values"].append(point["mastery_estimate"])
            entry["evidence"].append(point["effective_evidence_weight"])
            entry["coverages"].append(point.get("data_coverage") or 0)
            entry["evidence_detail"].append(point)
    status_order = ["熟练掌握", "基本掌握", "初步掌握", "掌握不足", "证据不足"]
    kp_meta: Dict[str, str] = {}
    for profile in profiles:
        for point in profile.get("mastery", []):
            kp_meta.setdefault(point["knowledge_point_id"], point["knowledge_point_name"])
    summary = []
    for kp_id, kp_name in kp_meta.items():
        item = points.get(kp_id)
        values = item["values"] if item else []
        mean = sum(values) / len(values) if values else None
        variance = sum((value - mean) ** 2 for value in values) / len(values) if values else 0
        weak_count = sum(1 for value in values if value < 0.65)
        # 每个知识点都覆盖全班：每个学生只能落在一个掌握状态，证据不足单独一段。
        students_by_status: Dict[str, List[Dict[str, Any]]] = {key: [] for key in status_order}
        for profile in profiles:
            point = next((entry for entry in profile.get("mastery", []) if entry["knowledge_point_id"] == kp_id), None)
            status = (point or {}).get("status") or "证据不足"
            students_by_status.setdefault(status, []).append({
                "student_id": profile.get("student", {}).get("id"),
                "student_code": profile.get("student", {}).get("code"),
                "mastery_estimate": (point or {}).get("mastery_estimate"),
                "evidence_count": (point or {}).get("evidence_count", 0),
                "effective_evidence_weight": (point or {}).get("effective_evidence_weight", 0),
                "covered_question_count": (point or {}).get("covered_question_count", 0),
                "opportunity_question_count": (point or {}).get("opportunity_question_count", 0),
            })
        status_counts = {status: len(rows) for status, rows in students_by_status.items() if rows}
        total_students = len(students)
        breakdown = [
            {"status": status, "count": len(students_by_status.get(status, [])), "share": round(len(students_by_status.get(status, [])) / total_students, 4) if total_students else 0, "students": students_by_status.get(status, [])}
            for status in status_order
        ]
        coverage_values = item["coverages"] if item else []
        summary.append({
            "knowledge_point_id": kp_id, "name": kp_name,
            "mean_mastery": round(mean, 4) if mean is not None else None, "std_dev": round(math.sqrt(variance), 4) if values else None,
            "q1": _quantile(values, 0.25), "median": _quantile(values, 0.5), "q3": _quantile(values, 0.75),
            "valid_students": len(values), "total_students": total_students,
            "students_with_answers": sum(1 for profile in profiles if profile.get("coverage", {}).get("answered_question_count")),
            "students_with_evidence": len(evidence_by_kp.get(kp_id, {}).get("students", set())),
            "students_with_any_evidence": len(evidence_by_kp.get(kp_id, {}).get("students", set())),
            "evidence_answers": len(evidence_by_kp.get(kp_id, {}).get("answers", set())),
            "evidence_scoring_points": len(evidence_by_kp.get(kp_id, {}).get("points", set())),
            "covered_question_count": max([int(entry.get("covered_question_count") or 0) for entry in (item["evidence_detail"] if item else [])] or [0]),
            "opportunity_question_count": max([int(entry.get("opportunity_question_count") or 0) for entry in (item["evidence_detail"] if item else [])] or [0]),
            "coverage": round(len(values) / total_students, 4) if total_students else 0,
            "evidence_coverage": round(len(evidence_by_kp.get(kp_id, {}).get("students", set())) / total_students, 4) if total_students else 0,
            "excluded_review": int(((stats or {}).get("not_usable") or {}).get("needs_review", 0)),
            "excluded_failed": int(((stats or {}).get("not_usable") or {}).get("failed", 0)),
            "mean_evidence_coverage": round(sum(coverage_values) / len(coverage_values), 4) if coverage_values else 0,
            "evidence_count": sum(entry["evidence_count"] for entry in (item["evidence_detail"] if item else [])),
            "status_distribution": status_counts,
            "status_breakdown": breakdown,
            "status_total_check": sum(status_counts.values()) == total_students,
            "weak_students": weak_count, "weak_rate": round(weak_count / len(values), 4) if values else None,
            "insufficient_students": total_students - len(values),
        })
    summary.sort(key=lambda item: (item["mean_mastery"] is None, item["mean_mastery"] or 0))
    def _kp_gap(kp: Dict[str, Any]) -> float:
        estimates = [float(student["mastery_estimate"]) for group in (kp.get("status_breakdown") or []) for student in (group.get("students") or []) if student.get("mastery_estimate") is not None]
        return round(max(estimates) - min(estimates), 4) if estimates else 0.0
    common = sorted(
        [kp for kp in summary if int(kp.get("valid_students") or 0) >= 3 and kp.get("mean_mastery") is not None],
        key=lambda kp: (float(kp.get("weak_rate") or 0), float(kp.get("mean_mastery") or 0)),
        reverse=True,
    )[:5]
    difference = sorted(
        [kp for kp in summary if int(kp.get("valid_students") or 0) >= 3 and kp.get("mean_mastery") is not None],
        key=lambda kp: (float(kp.get("std_dev") or 0), _kp_gap(kp)),
        reverse=True,
    )[:5]
    class_summary = {
        "common": [{"knowledge_point_id": kp["knowledge_point_id"], "name": kp["name"], "mean_mastery": kp.get("mean_mastery"), "weak_students": kp.get("weak_students"), "weak_rate": kp.get("weak_rate"), "valid_students": kp.get("valid_students")} for kp in common],
        "difference": [{"knowledge_point_id": kp["knowledge_point_id"], "name": kp["name"], "mean_mastery": kp.get("mean_mastery"), "std_dev": kp.get("std_dev"), "gap": _kp_gap(kp), "valid_students": kp.get("valid_students")} for kp in difference],
        "common_note": "按薄弱学生占比升序；只基于达到证据门槛的学生，不把证据不足当 0 分。",
        "difference_note": "按学生间标准差/最高-最低分差降序；差异大表示班内分层明显，不代表班级均值低。",
    }
    dimensions = _class_dimensions(batch_id, class_id, evidence)
    student_overall = []
    for profile in valid_profiles:
        values = [point["mastery_estimate"] for point in profile.get("mastery", []) if point.get("status") != "证据不足"]
        if values:
            student_overall.append(sum(values) / len(values))
    overall = student_overall
    weak_students = [profile for profile in valid_profiles if profile.get("weak_points")]
    method_summary: Dict[str, Dict[str, Any]] = {}
    for profile in valid_profiles:
        for method in profile.get("methods", []):
            if method.get("mastery_estimate") is None or method.get("status") in ("证据不足", "暂不可计算", "暂无证据"):
                continue
            entry = method_summary.setdefault(method["method_id"], {"method_id": method["method_id"], "name": method.get("method_name"), "values": [], "evidence": [], "coverages": []})
            entry["values"].append(method["mastery_estimate"])
            entry["evidence"].append(method.get("effective_evidence_weight") or 0)
            if method.get("data_coverage") is not None:
                entry["coverages"].append(method["data_coverage"])
    methods = [
        {"method_id": entry["method_id"], "name": entry["name"], "mean_mastery": round(sum(entry["values"]) / len(entry["values"]), 4), "valid_students": len(entry["values"]), "total_students": len(students), "insufficient_students": len(students) - len(entry["values"]), "evidence_count": len(entry["evidence"]), "mean_evidence_coverage": round(sum(entry["coverages"]) / len(entry["coverages"]), 4) if entry["coverages"] else None}
        for entry in method_summary.values()
    ]
    methods.sort(key=lambda item: item["mean_mastery"])
    return {
        "task_id": batch_id, "class": {"id": cls["id"], "code": cls["code"]}, "students": profiles,
        "knowledge_points": summary, "methods": methods,
        "student_count": len(students),
        "students_with_answers": sum(1 for profile in profiles if profile.get("coverage", {}).get("answered_question_count")),
        "students_with_evidence": sum(1 for profile in profiles if any(point.get("status") != "证据不足" for point in profile.get("mastery", []))),
        "valid_student_count": len(valid_profiles), "weak_student_count": len(weak_students),
        "insufficient_student_count": len(students) - len(valid_profiles),
        "overall_mastery": round(sum(overall) / len(overall), 4) if overall else None,
        "overall_mastery_note": "先按学生平均其有效知识点，再对有有效估计的学生等权平均；不代表统一量表分数",
        "overall_weak_rate": round(len(weak_students) / len(valid_profiles), 4) if valid_profiles else None,
        "questions": questions, "trends": dimensions["trends"], "error_types": dimensions["error_types"],
        "class_summary": class_summary,
        "scope": "multi" if batch_ids else "single", "batch_ids": batch_ids or [batch_id], "question_scope_note": "多任务视图下题目级统计只在单次任务内展示" if batch_ids else "",
        "excluded_batches": selection.get("excluded", []) if batch_ids else [], "comparability": selection.get("details", []) if batch_ids else [],
        "algorithm_version": ALGORITHM_VERSION, "parameter_version": PARAM_VERSION,
        "parameters": {"alpha0": A0, "beta0": B0, "min_evidence": MIN_EVIDENCE, "min_coverage": MIN_COVERAGE, "min_questions": MIN_QUESTIONS, "weak_threshold": 0.65},
        "weighting": {"primary_unit": "student", "note": "班级掌握以学生为基本单位等权聚合，不按作答次数加权"},
    }


def all_classes(batch_id: str) -> List[Dict[str, Any]]:
    return db.q("SELECT DISTINCT c.id,c.code FROM teacher_classes c JOIN teacher_students s ON s.class_id=c.id JOIN teacher_answers a ON a.student_id=s.id WHERE a.import_batch_id=? ORDER BY c.code", (batch_id,))


def mapping_audit(batch_id: str = "") -> Dict[str, Any]:
    ensure_model()
    scope = " WHERE batch_id=?" if batch_id else ""
    args = (batch_id,) if batch_id else ()
    total = db.q1(f"SELECT COUNT(*) n FROM scoring_points{scope}", args)["n"]
    mapped = db.q1("SELECT COUNT(DISTINCT l.scoring_point_id) n FROM scoring_point_knowledge_links l " + ("WHERE l.status != 'disabled' AND l.scoring_point_id IN (SELECT id FROM scoring_points WHERE batch_id=?)" if batch_id else "WHERE l.status != 'disabled'"), args)["n"]
    confirmed = db.q1("SELECT COUNT(DISTINCT l.scoring_point_id) n FROM scoring_point_knowledge_links l " + ("WHERE l.status='confirmed' AND l.scoring_point_id IN (SELECT id FROM scoring_points WHERE batch_id=?)" if batch_id else "WHERE l.status='confirmed'"), args)["n"]
    statuses = {row["status"]: row["n"] for row in db.q("SELECT l.status,COUNT(*) n FROM scoring_point_knowledge_links l " + ("WHERE l.scoring_point_id IN (SELECT id FROM scoring_points WHERE batch_id=?) " if batch_id else "") + "GROUP BY l.status", args)}
    bad = db.q1("SELECT COUNT(*) n FROM (SELECT l.scoring_point_id,SUM(l.weight) total FROM scoring_point_knowledge_links l " + ("WHERE l.status != 'disabled' AND l.scoring_point_id IN (SELECT id FROM scoring_points WHERE batch_id=?) " if batch_id else "WHERE l.status != 'disabled' ") + "GROUP BY l.scoring_point_id HAVING ABS(total-1.0) > 0.001)", args)["n"]
    duplicates = db.q1("SELECT COUNT(*) n FROM (SELECT batch_id,code,COUNT(*) c FROM scoring_points WHERE batch_id IS NOT NULL GROUP BY batch_id,code HAVING c>1)")["n"]
    methods = db.q1("SELECT COUNT(*) n FROM learning_methods WHERE status != 'disabled'")["n"]
    method_links = db.q1("SELECT COUNT(DISTINCT scoring_point_id) n FROM scoring_point_method_links WHERE status != 'disabled'")["n"]
    per_batch = [{
        "batch_id": row["batch_id"], "scoring_points": row["total"], "confirmed": row["confirmed"],
        "legacy": row["legacy"], "unmapped": row["unmapped"],
    } for row in db.q("""
        SELECT sp.batch_id, COUNT(*) total,
               SUM(CASE WHEN f.conf = 1 THEN 1 ELSE 0 END) confirmed,
               SUM(CASE WHEN COALESCE(f.conf, 0) = 0 AND f.legacy = 1 THEN 1 ELSE 0 END) legacy,
               SUM(CASE WHEN f.sp IS NULL THEN 1 ELSE 0 END) unmapped
        FROM scoring_points sp
        LEFT JOIN (
            SELECT scoring_point_id sp,
                   MAX(CASE WHEN status='confirmed' THEN 1 ELSE 0 END) conf,
                   MAX(CASE WHEN status='legacy' THEN 1 ELSE 0 END) legacy
            FROM scoring_point_knowledge_links WHERE status != 'disabled' GROUP BY scoring_point_id
        ) f ON f.sp = sp.id
        WHERE sp.batch_id IS NOT NULL
        GROUP BY sp.batch_id
    """)]
    return {
        "batch_id": batch_id, "scoring_points": total, "mapped_scoring_points": mapped, "unmapped_scoring_points": total - mapped,
        "confirmed_scoring_points": confirmed, "legacy_scoring_points": max(0, mapped - confirmed),
        "statuses": statuses, "illegal_weight_groups": bad, "duplicate_codes": duplicates,
        "methods": methods, "scoring_points_with_methods": method_links,
        "per_batch": per_batch,
        "mapping_status": "confirmed mappings exist" if confirmed else "legacy indicator mappings are not teacher-confirmed",
    }


def _ordered_batches(batch_ids: List[str]) -> List[Dict[str, Any]]:
    batches = {row["id"]: row for row in db.q("SELECT id,filename,created_at FROM teacher_batches")}
    ordered = [batches[bid] for bid in batch_ids if bid in batches]
    ordered.sort(key=lambda row: row.get("created_at") or "")
    return ordered


def student_knowledge_trend(student_id: str, batch_ids: List[str], alpha: float = 0.4) -> Dict[str, Any]:
    """跨任务的学生知识点趋势：每个任务一个时间点，原始值与 EWMA 同时保留。"""
    timeline = []
    series: Dict[str, Dict[str, Any]] = {}
    identity_issues = []
    for batch in _ordered_batches(batch_ids):
        profile = student_profile(batch["id"], student_id)
        if profile.get("error"):
            continue
        enrollment = db.q1("SELECT identity_status FROM teacher_enrollments WHERE batch_id=? AND student_id=?", (batch["id"], student_id)) or {}
        identity_status = enrollment.get("identity_status") or "missing"
        if identity_status != "same_person":
            identity_issues.append({"batch_id": batch["id"], "identity_status": identity_status})
        timeline.append({"batch_id": batch["id"], "label": batch.get("filename") or batch["id"], "date": batch.get("created_at") or "", "identity_status": identity_status})
        for point in profile.get("mastery", []):
            entry = series.setdefault(point["knowledge_point_id"], {"knowledge_point_id": point["knowledge_point_id"], "name": point["knowledge_point_name"], "points": []})
            entry["points"].append({
                "batch_id": batch["id"], "mastery_estimate": point["mastery_estimate"], "status": point["status"],
                "data_coverage": point["data_coverage"], "evidence_count": point["evidence_count"],
                "effective_evidence_weight": point["effective_evidence_weight"],
            })
    for entry in series.values():
        entry["points"].sort(key=lambda item: next((index for index, batch in enumerate(_ordered_batches(batch_ids)) if batch["id"] == item["batch_id"]), 0))
        values = [item["mastery_estimate"] if item["status"] != "证据不足" else None for item in entry["points"]]
        for item, value in zip(entry["points"], ewma(values, alpha)):
            item["ewma_mastery"] = value
    return {"student_id": student_id, "timeline": timeline, "knowledge_points": list(series.values()), "alpha": alpha,
            "identity_issues": identity_issues,
            "identity_warning": "跨任务学生身份尚未全部确认（同编号不等于同一人），趋势仅作观察" if len(timeline) > 1 and identity_issues else None,
            "note": "证据不足的时间点不参与 EWMA，也不会被补成 0；原始估计值始终保留。"}


def class_knowledge_trend(class_id: str, batch_ids: List[str], alpha: float = 0.4) -> Dict[str, Any]:
    timeline = []
    series: Dict[str, Dict[str, Any]] = {}
    for batch in _ordered_batches(batch_ids):
        profile = class_profile(batch["id"], class_id)
        if profile.get("error"):
            continue
        timeline.append({"batch_id": batch["id"], "label": batch.get("filename") or batch["id"], "date": batch.get("created_at") or "", "valid_students": profile.get("valid_student_count"), "student_count": profile.get("student_count")})
        for point in profile.get("knowledge_points", []):
            entry = series.setdefault(point["knowledge_point_id"], {"knowledge_point_id": point["knowledge_point_id"], "name": point["name"], "points": []})
            entry["points"].append({"batch_id": batch["id"], "mean_mastery": point["mean_mastery"], "valid_students": point["valid_students"], "total_students": point["total_students"], "mean_evidence_coverage": point["mean_evidence_coverage"], "status_distribution": point["status_distribution"]})
    order = {batch["id"]: index for index, batch in enumerate(_ordered_batches(batch_ids))}
    for entry in series.values():
        entry["points"].sort(key=lambda item: order.get(item["batch_id"], 0))
        values = [item["mean_mastery"] if item["valid_students"] else None for item in entry["points"]]
        for item, value in zip(entry["points"], ewma(values, alpha)):
            item["ewma_mastery"] = value
    return {"class_id": class_id, "timeline": timeline, "knowledge_points": list(series.values()), "alpha": alpha, "note": "班级趋势以学生为基本单位；样本量为 0 的时间点不参与 EWMA。"}


KNOWLEDGE_EDGE_SOURCES = ("teacher", "topic_sequence", "course_structure")


def _kp_nodes(batch_id: str) -> List[Dict[str, Any]]:
    mapping = mapping_for_batch(batch_id)
    used = set()
    for item in mapping["items"]:
        for link in item["knowledge_links"]:
            used.add(link["knowledge_point_id"])
    nodes = []
    for row in db.q("SELECT i.*, t.title topic_title FROM teacher_indicators i LEFT JOIN teacher_topics t ON t.id=i.topic_id ORDER BY i.code"):
        kp_id = f"kp:{row['code']}"
        nodes.append({"knowledge_point_id": kp_id, "code": row["code"], "name": row.get("description") or row["code"], "topic_id": row.get("topic_id"), "topic_title": row.get("topic_title") or "", "in_batch": kp_id in used})
    return nodes


def _graph_edges(batch_id: str) -> List[Dict[str, Any]]:
    return db.q("SELECT * FROM learning_knowledge_edges WHERE batch_id=? ORDER BY created_at", (batch_id,))


def _would_cycle(batch_id: str, from_kp: str, to_kp: str) -> bool:
    """从 to_kp 出发能否走回 from_kp；能则新增边会成环。"""
    if from_kp == to_kp:
        return True
    adjacency: Dict[str, List[str]] = {}
    for edge in _graph_edges(batch_id):
        adjacency.setdefault(edge["from_kp"], []).append(edge["to_kp"])
    stack = [to_kp]
    seen = set()
    while stack:
        node = stack.pop()
        if node == from_kp:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(adjacency.get(node, []))
    return False


def add_knowledge_edge(batch_id: str, from_kp: str, to_kp: str, source: str = "teacher", status: str = "confirmed", operator_id: str = "") -> Dict[str, Any]:
    ensure_model()
    if source not in KNOWLEDGE_EDGE_SOURCES:
        raise ValueError("依赖来源不合法")
    if status not in ("candidate", "confirmed"):
        raise ValueError("依赖状态不合法")
    valid = {node["knowledge_point_id"] for node in _kp_nodes(batch_id)}
    if from_kp not in valid or to_kp not in valid:
        raise ValueError("依赖节点必须是当前任务涉及的知识点")
    if from_kp == to_kp:
        raise ValueError("知识点不能依赖自己")
    existing = db.q1("SELECT * FROM learning_knowledge_edges WHERE batch_id=? AND from_kp=? AND to_kp=?", (batch_id, from_kp, to_kp))
    if existing:
        if status == "confirmed" and existing["status"] != "confirmed":
            db.x("UPDATE learning_knowledge_edges SET status='confirmed', source=?, created_by=?, updated_at=? WHERE id=?", (source, operator_id, now(), existing["id"]))
            return db.q1("SELECT * FROM learning_knowledge_edges WHERE id=?", (existing["id"],))
        raise ValueError("依赖关系已存在")
    if _would_cycle(batch_id, from_kp, to_kp):
        raise ValueError("该依赖会形成循环")
    edge_id = db.new_id("edge")
    db.x("INSERT INTO learning_knowledge_edges (id,batch_id,from_kp,to_kp,source,status,confidence,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (edge_id, batch_id, from_kp, to_kp, source, status, 0.6 if status == "candidate" else 1.0, operator_id, now(), now()))
    return db.q1("SELECT * FROM learning_knowledge_edges WHERE id=?", (edge_id,))


def delete_knowledge_edge(edge_id: str) -> bool:
    ensure_model()
    if not db.q1("SELECT id FROM learning_knowledge_edges WHERE id=?", (edge_id,)):
        return False
    db.x("DELETE FROM learning_knowledge_edges WHERE id=?", (edge_id,))
    return True


def generate_candidate_edges(batch_id: str, operator_id: str = "") -> Dict[str, Any]:
    """按课程主题顺序生成候选依赖（可解释规则，不靠知识点名称猜测），不覆盖教师确认。"""
    topics = db.q("SELECT * FROM teacher_topics ORDER BY created_at, id")
    indicators = db.q("SELECT * FROM teacher_indicators ORDER BY code")
    by_topic: Dict[str, List[str]] = {}
    for indicator in indicators:
        by_topic.setdefault(indicator.get("topic_id") or "", []).append(f"kp:{indicator['code']}")
    created = 0
    for index in range(len(topics) - 1):
        left = by_topic.get(topics[index]["id"], [])
        right = by_topic.get(topics[index + 1]["id"], [])
        for source in left:
            for target in right:
                existing = db.q1("SELECT status FROM learning_knowledge_edges WHERE batch_id=? AND from_kp=? AND to_kp=?", (batch_id, source, target))
                if existing:
                    continue
                try:
                    add_knowledge_edge(batch_id, source, target, "topic_sequence", "candidate", operator_id)
                    created += 1
                except ValueError:
                    continue
    return {"batch_id": batch_id, "created": created, "graph": knowledge_graph(batch_id)}


def knowledge_graph(batch_id: str) -> Dict[str, Any]:
    ensure_model()
    nodes = _kp_nodes(batch_id)
    edges = _graph_edges(batch_id)
    return {
        "batch_id": batch_id, "nodes": nodes, "edges": edges,
        "summary": {
            "nodes": len(nodes), "nodes_in_batch": sum(1 for node in nodes if node["in_batch"]),
            "edges": len(edges), "confirmed": sum(1 for edge in edges if edge["status"] == "confirmed"),
            "candidate": sum(1 for edge in edges if edge["status"] == "candidate"),
        },
    }


def knowledge_dependencies(batch_id: str, kp_id: str) -> Dict[str, Any]:
    ensure_model()
    adjacency: Dict[str, List[Dict[str, Any]]] = {}
    for edge in _graph_edges(batch_id):
        adjacency.setdefault(edge["to_kp"], []).append(edge)
    direct = [edge["from_kp"] for edge in adjacency.get(kp_id, [])]
    transitive: List[str] = []
    stack = list(direct)
    seen = {kp_id}
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        transitive.append(node)
        stack.extend(edge["from_kp"] for edge in adjacency.get(node, []))
    names = {node["knowledge_point_id"]: node["name"] for node in _kp_nodes(batch_id)}
    return {
        "knowledge_point_id": kp_id, "name": names.get(kp_id),
        "direct_prerequisites": [{"knowledge_point_id": item, "name": names.get(item)} for item in direct],
        "transitive_prerequisites": [{"knowledge_point_id": item, "name": names.get(item)} for item in transitive],
        "has_cycle": False,
    }


def student_prerequisite_gaps(student_id: str, batch_id: str) -> Dict[str, Any]:
    profile = student_profile(batch_id, student_id)
    statuses = {point["knowledge_point_id"]: point for point in profile.get("mastery", [])}
    graph = knowledge_graph(batch_id)
    names = {node["knowledge_point_id"]: node["name"] for node in graph["nodes"]}
    gaps = []
    for kp_id, point in statuses.items():
        if point["status"] not in ("掌握不足", "初步掌握"):
            continue
        dependencies = knowledge_dependencies(batch_id, kp_id)
        weak_prereqs = []
        seen_prereq = set()
        for item in dependencies["direct_prerequisites"] + dependencies["transitive_prerequisites"]:
            if item["knowledge_point_id"] in seen_prereq:
                continue
            seen_prereq.add(item["knowledge_point_id"])
            prerequisite = statuses.get(item["knowledge_point_id"])
            if not prerequisite or prerequisite["status"] in ("证据不足", "掌握不足", "初步掌握"):
                weak_prereqs.append({"knowledge_point_id": item["knowledge_point_id"], "name": item["name"], "status": prerequisite["status"] if prerequisite else "无证据"})
        if weak_prereqs:
            gaps.append({"knowledge_point_id": kp_id, "name": names.get(kp_id), "status": point["status"], "weak_prerequisites": weak_prereqs})
    return {"student_id": student_id, "batch_id": batch_id, "gaps": gaps}


ERROR_TAXONOMY = ["概念理解错误", "方法选择错误", "推理过程错误", "计算操作错误", "表达规范错误", "未识别错误"]


def _labels_for_step(step: Dict[str, Any]) -> List[str]:
    labels: List[str] = []
    canonical = _canonical_error(step.get("error_type"))
    mapping = {
        "概念理解错误": ["概念理解错误"],
        "方法选择错误": ["方法选择不当"],
        "推理过程错误": ["推理错误"],
        "计算操作错误": ["运算错误"],
        "表达规范错误": ["符号表达错误", "单位或格式错误"],
    }
    labels.extend(mapping.get(canonical, []))
    reason = str(step.get("judgment_reason") or "")
    keywords = {
        "概念理解错误": ("概念", "定义", "性质", "审题", "理解"),
        "公式使用错误": ("公式", "定理", "法则"),
        "运算错误": ("计算", "算错", "运算", "结果错误", "算成"),
        "推理错误": ("推理", "逻辑", "依据", "论证"),
        "步骤遗漏": ("缺少步骤", "跳步", "未写步骤", "步骤缺失", "没有写出", "缺少过程"),
        "条件遗漏": ("条件", "范围", "漏掉", "忽略"),
        "方法选择不当": ("方法", "思路", "策略"),
        "符号表达错误": ("符号", "书写", "正负号", "表达"),
        "单位或格式错误": ("单位", "格式", "答句"),
    }
    for label, words in keywords.items():
        if any(word in reason for word in words):
            labels.append(label)
    if step.get("is_correct") is False and not labels:
        labels.append("无法判断")
    return sorted(set(labels))


def classify_answer_errors(answer_id: str, grade_run_id: str = "", operator_id: str = "") -> Dict[str, Any]:
    ensure_model()
    answer = db.q1("SELECT * FROM teacher_answers WHERE id=?", (answer_id,))
    if not answer:
        raise KeyError("作答不存在")
    grade = db.q1("SELECT * FROM teacher_grade_runs WHERE id=?", (grade_run_id,)) if grade_run_id else _latest_grade(answer_id)
    if not grade:
        raise ValueError("该作答还没有判题结果")
    try:
        steps = json.loads(grade.get("step_results_json") or "[]")
    except Exception:
        steps = []
    # 只对“确实失分”的步骤分类；正确步骤不应被标成无法判断。
    failing_steps = [step for step in steps if step.get("is_correct") is False or (float(step.get("score") or 0) < float(step.get("max_score") or 0) - 1e-9)]
    full_score = float(grade.get("max_score") or 0)
    got_score = float(grade.get("score") or 0)
    if not failing_steps and full_score and got_score >= full_score - 1e-9:
        db.x("DELETE FROM answer_error_labels WHERE answer_id=? AND grade_run_id=?", (answer_id, grade["id"]))
        return {"answer_id": answer_id, "grade_run_id": grade["id"], "labels": [], "confidence": 0.0, "source": "rule", "model": "", "prompt_version": "error-rule-v1", "evidence": [], "no_error": True}
    evidence = []
    labels: Dict[str, Dict[str, Any]] = {}
    for step in failing_steps:
        step_labels = _labels_for_step(step)
        for operation in step.get("operation_units") or []:
            if operation.get("is_correct") is False:
                for label in _labels_for_step({**operation, "judgment_reason": operation.get("judgment_reason") or step.get("judgment_reason")}):
                    step_labels.append(label)
        step_labels = sorted(set(step_labels))
        for label in step_labels:
            entry = labels.setdefault(label, {"label": label, "evidence": [], "sources": set()})
            entry["evidence"].append({"step_index": step.get("step_index"), "error_type": step.get("error_type"), "reason": step.get("judgment_reason"), "score": step.get("score"), "max_score": step.get("max_score")})
            entry["sources"].add("error_type" if step.get("error_type") else "judgment_reason")
        evidence.append({"step_index": step.get("step_index"), "labels": step_labels})
    if not steps and str(answer.get("solution_process") or "").strip():
        labels.setdefault("步骤遗漏", {"label": "步骤遗漏", "evidence": [{"reason": "有解题过程但判题未产生步骤分析"}], "sources": {"grade"}})
    if not labels:
        labels.setdefault("无法判断", {"label": "无法判断", "evidence": [{"reason": "判题未提供可分类依据"}], "sources": {"grade"}})
    payload = [{"label": label, "evidence": entry["evidence"], "sources": sorted(entry["sources"])} for label, entry in labels.items()]
    confidence = min(1.0, round(0.4 + 0.1 * len(payload), 2))
    record_id = db.new_id("errlabel")
    db.x("""
        INSERT INTO answer_error_labels (id,batch_id,answer_id,grade_run_id,labels_json,source,model,prompt_version,confidence,rationale,status,created_by,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(answer_id,grade_run_id) DO UPDATE SET labels_json=excluded.labels_json, source=excluded.source, confidence=excluded.confidence, rationale=excluded.rationale, updated_at=excluded.updated_at
    """, (record_id, answer.get("import_batch_id") or "", answer_id, grade["id"], json.dumps(payload, ensure_ascii=False), "rule", "", "error-rule-v1", confidence, "基于已有判题步骤的 error_type 与 judgment_reason 规则分类", "auto", operator_id, now(), now()))
    return {"answer_id": answer_id, "grade_run_id": grade["id"], "labels": payload, "confidence": confidence, "source": "rule", "model": "", "prompt_version": "error-rule-v1", "evidence": evidence}


def set_error_labels(answer_id: str, labels: List[str], source: str, confidence: float, rationale: str, operator_id: str, grade_run_id: str = "", model: str = "", prompt_version: str = "") -> Dict[str, Any]:
    ensure_model()
    # 教师可能提交旧标签（步骤遗漏/公式使用错误等），统一归一为六类后再落库。
    label_set = sorted({_canonical_error(label) for label in labels if str(label or "").strip()})
    if not label_set:
        raise ValueError("至少提供一个合法的错误标签")
    grade = db.q1("SELECT * FROM teacher_grade_runs WHERE id=?", (grade_run_id,)) if grade_run_id else _latest_grade(answer_id)
    if not grade:
        raise ValueError("该作答还没有判题结果")
    answer = db.q1("SELECT * FROM teacher_answers WHERE id=?", (answer_id,)) or {}
    payload = [{"label": label, "evidence": [], "sources": [source]} for label in label_set]
    db.x("""
        INSERT INTO answer_error_labels (id,batch_id,answer_id,grade_run_id,labels_json,source,model,prompt_version,confidence,rationale,status,created_by,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(answer_id,grade_run_id) DO UPDATE SET labels_json=excluded.labels_json, source=excluded.source, model=excluded.model, prompt_version=excluded.prompt_version, confidence=excluded.confidence, rationale=excluded.rationale, status='confirmed', created_by=excluded.created_by, updated_at=excluded.updated_at
    """, (db.new_id("errlabel"), answer.get("import_batch_id") or "", answer_id, grade["id"], json.dumps(payload, ensure_ascii=False), source, model, prompt_version or ("error-rule-v1" if source == "rule" else ""), float(confidence), rationale, "confirmed" if source == "teacher" else "auto", operator_id, now(), now()))
    return {"answer_id": answer_id, "labels": label_set, "source": source, "confidence": confidence}


def error_label_summary(batch_id: str, class_id: str = "") -> Dict[str, Any]:
    ensure_model()
    params: List[Any] = [batch_id]
    scope = "a.import_batch_id=?"
    if class_id:
        scope += " AND s.class_id=?"
        params.append(class_id)
    rows = db.q(f"""
        SELECT l.*, a.student_id, s.code student_code FROM answer_error_labels l
        JOIN teacher_answers a ON a.id=l.answer_id
        JOIN teacher_students s ON s.id=a.student_id
        WHERE {scope}
    """, tuple(params))
    counts: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        for item in json.loads(row["labels_json"] or "[]"):
            entry = counts.setdefault(item["label"], {"label": item["label"], "answers": 0, "students": set(), "confirmed": 0})
            entry["answers"] += 1
            entry["students"].add(row["student_id"])
            if row["status"] == "confirmed":
                entry["confirmed"] += 1
    return {"items": [{"label": item["label"], "answer_count": item["answers"], "student_count": len(item["students"]), "confirmed_count": item["confirmed"]} for item in sorted(counts.values(), key=lambda entry: -entry["answers"])], "taxonomy": ERROR_TAXONOMY}


def recommendations(student_id: str, batch_id: str, limit: int = 10) -> Dict[str, Any]:
    profile = student_profile(batch_id, student_id)
    if profile.get("error"):
        return {"student_id": student_id, "items": [], "error": profile["error"]}
    opportunities = _kp_opportunities(batch_id)
    covered = {item["question_id"] for item in profile.get("evidence", [])}
    error_counts: Dict[str, int] = {}
    for item in profile.get("evidence", []):
        if item["score_ratio"] < 1 and item["error_type"] != CORRECT_LABEL:
            error_counts[item["error_type"]] = error_counts.get(item["error_type"], 0) + 1
    items = []
    for point in profile.get("mastery", []):
        kp_id = point["knowledge_point_id"]
        all_questions = sorted(opportunities.get(kp_id) or set())
        remaining = [qid for qid in all_questions if qid not in covered]
        suggested = remaining[:5] or all_questions[:5]
        if point["status"] == "证据不足":
            priority = 2
            reason = f"「{point['knowledge_point_name']}」证据不足（权重 {point['effective_evidence_weight']}，覆盖 {point['covered_question_count']}/{point['opportunity_question_count']}），需要用更多题目确认掌握情况"
        elif point["status"] in ("掌握不足", "初步掌握"):
            priority = 1
            reason = f"「{point['knowledge_point_name']}」{point['status']}（估计 {round(point['mastery_estimate'] * 100)}%，区间 {round(point['credible_interval_lower'] * 100)}–{round(point['credible_interval_upper'] * 100)}%），建议优先补强"
        else:
            continue
        items.append({"type": "knowledge_point", "priority": priority, "knowledge_point_id": kp_id, "knowledge_point_name": point["knowledge_point_name"], "reason": reason, "suggested_questions": suggested, "rehearsal": not remaining, "evidence": {"status": point["status"], "mastery": point["mastery_estimate"], "coverage": point["data_coverage"], "evidence_count": point["evidence_count"]}})
    for method in profile.get("methods", []):
        if method["status"] in ("证据不足", "掌握不足", "初步掌握"):
            items.append({"type": "method", "priority": 3, "method_id": method["method_id"], "method_name": method["method_name"], "reason": f"解题方法「{method['method_name']}」{method['status']}，需要针对性练习并保留步骤证据", "suggested_questions": [], "evidence": {"status": method["status"], "mastery": method["mastery_estimate"], "evidence_count": method["evidence_count"]}})
    if error_counts:
        top_error, count = max(error_counts.items(), key=lambda item: item[1])
        if count >= 2:
            items.append({"type": "error_pattern", "priority": 2, "error_type": top_error, "reason": f"「{_canonical_error(top_error)}」重复出现 {count} 次，建议专门练习同类步骤", "suggested_questions": [], "evidence": {"count": count}})
    for gap in student_prerequisite_gaps(student_id, batch_id)["gaps"]:
        items.append({"type": "prerequisite", "priority": 1, "knowledge_point_id": gap["knowledge_point_id"], "knowledge_point_name": gap["name"], "reason": f"「{gap['name']}」薄弱，且前置知识 {', '.join(item['name'] for item in gap['weak_prerequisites'])} 同样薄弱，建议先补前置", "suggested_questions": [], "evidence": {"weak_prerequisites": gap["weak_prerequisites"]}})
    items.sort(key=lambda item: (item["priority"], item.get("knowledge_point_name") or ""))
    return {"student_id": student_id, "batch_id": batch_id, "items": items[:limit], "note": "推荐基于评分点证据、覆盖率、前置依赖与错误类型；证据不足不等于 0 分掌握。"}


def class_knowledge_detail(batch_id: str, class_id: str, knowledge_point_id: str) -> Dict[str, Any]:
    """班级知识点详情：掌握分布、相关题目、评分点、错误类型与证据样本。"""
    profile = class_profile(batch_id, class_id)
    if profile.get("error"):
        return {"error": profile["error"]}
    point = next((item for item in profile.get("knowledge_points", []) if item["knowledge_point_id"] == knowledge_point_id), None)
    if not point:
        return {"error": "该班级没有这个知识点的数据"}
    evidence, _ = _evidence(batch_id)
    rows = [item for item in evidence if str(item.get("class_id") or "") == class_id and item["knowledge_point_id"] == knowledge_point_id]
    performance = {item["question_id"]: item for item in question_performance(batch_id, class_id)["items"]}
    question_ids = sorted({item["question_id"] for item in rows})
    scoring_point_ids = sorted({item["scoring_point_id"] for item in rows})
    error_stats: Dict[str, Dict[str, Any]] = {}
    for item in rows:
        if item["score_ratio"] >= 1 or item["error_type"] in ("未识别错误", "其他", "无法确定", ""):
            continue
        entry = error_stats.setdefault(item["error_type"], {"type": item["error_type"], "weighted_count": 0.0, "students": set(), "questions": set()})
        entry["weighted_count"] += float(item["weight"]) * (1 - float(item["score_ratio"]))
        entry["students"].add(item["student_id"])
        entry["questions"].add(item["question_id"])
    questions = []
    for question_id in question_ids:
        stat = performance.get(question_id) or {}
        questions.append({
            "question_id": question_id, "stem": stat.get("stem"), "question_type": stat.get("question_type"),
            "max_score": stat.get("max_score"), "expected": stat.get("expected"), "answered": stat.get("answered"), "graded": stat.get("graded"),
            "review": stat.get("review"), "failed": stat.get("failed"), "not_answered": stat.get("not_answered"),
            "score_rate": stat.get("score_rate"),
            "scoring_points": sorted({item["scoring_point_id"] for item in rows if item["question_id"] == question_id}),
        })
    return {
        "batch_id": batch_id, "class": profile.get("class"), "knowledge_point": {
            "knowledge_point_id": knowledge_point_id, "name": point["name"],
            "mean_mastery": point["mean_mastery"], "std_dev": point.get("std_dev"),
            "valid_students": point["valid_students"], "total_students": point["total_students"],
            "coverage": point.get("coverage"), "mean_evidence_coverage": point.get("mean_evidence_coverage"),
            "students_with_any_evidence": point.get("students_with_any_evidence"),
            "evidence_answers": point.get("evidence_answers"), "evidence_scoring_points": point.get("evidence_scoring_points"),
            "evidence_coverage": point.get("evidence_coverage"),
            "excluded_review": point.get("excluded_review"), "excluded_failed": point.get("excluded_failed"),
            "status_breakdown": point.get("status_breakdown") or [], "status_total_check": point.get("status_total_check"),
            "insufficient_students": point.get("insufficient_students"),
        },
        "questions": questions,
        "scoring_points": [{"scoring_point_id": sp, "question_id": next((item["question_id"] for item in rows if item["scoring_point_id"] == sp), ""), "evidence_count": sum(1 for item in rows if item["scoring_point_id"] == sp)} for sp in scoring_point_ids],
        "error_types": sorted([{"type": key, "weighted_count": round(value["weighted_count"], 4), "student_count": len(value["students"]), "questions": sorted(value["questions"])} for key, value in error_stats.items()], key=lambda item: -item["weighted_count"]),
        "evidence_sample": [{key: item[key] for key in ("student_code", "question_id", "scoring_point_id", "earned_score", "full_score", "error_type", "judgment_source", "mapping_status")} | {"evidence_text": (item.get("evidence_text") or "")[:160]} for item in rows[:30]],
        "evidence_count": len(rows),
        "note": "掌握均值只用达到证据门槛的学生；证据不足单独统计，不按 0 分。",
    }


def compare_classes_detailed(batch_id: str, class_ids: List[str]) -> Dict[str, Any]:
    profiles = {cid: class_profile(batch_id, cid) for cid in class_ids}
    summary = []
    for cid, profile in profiles.items():
        if profile.get("error"):
            continue
        answers = db.q("""
            SELECT a.student_id, a.grading_status, g.status grade_status, g.score, COALESCE(v.max_score,q.max_score) max_score
            FROM teacher_answers a
            JOIN teacher_students s ON s.id=a.student_id
            LEFT JOIN teacher_grade_runs g ON g.id=(SELECT g2.id FROM teacher_grade_runs g2 WHERE g2.answer_id=a.id ORDER BY g2.grading_version DESC, g2.created_at DESC LIMIT 1)
            LEFT JOIN teacher_question_versions v ON v.version_id=a.question_version_id
            LEFT JOIN teacher_questions q ON q.id=a.question_id
            WHERE a.import_batch_id=? AND s.class_id=?
        """, (batch_id, cid))
        usable = [row for row in answers if row.get("grade_status") not in (None, "failed", "needs_review")]
        students = {row["student_id"] for row in answers}
        scored = {}
        for row in usable:
            scored.setdefault(row["student_id"], 0.0)
            scored[row["student_id"]] += float(row["score"] or 0)
        summary.append({
            "class_id": cid, "class_code": profile["class"]["code"],
            "student_count": profile["student_count"], "students_with_answers": profile["students_with_answers"],
            "valid_student_count": profile["valid_student_count"], "insufficient_student_count": profile["insufficient_student_count"],
            "overall_mastery": profile["overall_mastery"],
            "answers": len(answers), "valid_answers": len(usable),
            "completion_rate": round(len(usable) / len(answers), 4) if answers else None,
            "scored_students": len(scored),
            "average_total_score": round(sum(scored.values()) / len(scored), 2) if scored else None,
            "has_evidence": bool(profile.get("knowledge_points")),
        })
    kp_ids = sorted({point["knowledge_point_id"] for profile in profiles.values() for point in profile.get("knowledge_points", [])})
    rows = []
    for kp_id in kp_ids:
        values = []
        for cid, profile in profiles.items():
            point = next((item for item in profile.get("knowledge_points", []) if item["knowledge_point_id"] == kp_id), None)
            values.append({
                "class_id": cid, "class_code": profile.get("class", {}).get("code"),
                "mean_mastery": point["mean_mastery"] if point else None,
                "valid_students": point["valid_students"] if point else 0,
                "total_students": profile.get("student_count"),
                "mean_evidence_coverage": point["mean_evidence_coverage"] if point else None,
                "status_distribution": point["status_distribution"] if point else {},
                "insufficient": point is None,
            })
        present = [item["mean_mastery"] for item in values if item["mean_mastery"] is not None]
        difference = round(max(present) - min(present), 4) if len(present) >= 2 else None
        rows.append({"knowledge_point_id": kp_id, "name": next((item["name"] for profile in profiles.values() for item in profile.get("knowledge_points", []) if item["knowledge_point_id"] == kp_id), kp_id), "values": values, "difference": difference, "pp_difference": round(difference * 100, 2) if difference is not None else None})
    min_sample = int(os.environ.get("XUEBAN_COMPARE_MIN_SAMPLE", "3"))
    min_coverage = float(os.environ.get("XUEBAN_COMPARE_MIN_COVERAGE", "0.3"))
    advantages: List[Dict[str, Any]] = []
    common_weak: List[Dict[str, Any]] = []
    insufficient: List[Dict[str, Any]] = []
    for row in rows:
        present_values = [item for item in row["values"] if item["mean_mastery"] is not None]
        low_sample = [item["class_code"] for item in row["values"] if item["mean_mastery"] is None or int(item["valid_students"] or 0) < min_sample]
        low_coverage = [item["class_code"] for item in row["values"] if (item.get("mean_evidence_coverage") or 0) < min_coverage]
        row["comparable"] = len(present_values) == len(row["values"]) and not low_sample
        row["sample_warning"] = f"{'、'.join(low_sample)} 有效学生少于 {min_sample} 人，不构成班级水平结论" if low_sample else ""
        row["coverage_warning"] = f"覆盖偏低：{'、'.join(low_coverage)} 证据覆盖率低于 {round(min_coverage * 100)}%" if low_coverage else ""
        if low_sample or len(present_values) < len(row["values"]):
            insufficient.append({"knowledge_point_id": row["knowledge_point_id"], "name": row["name"], "classes": low_sample, "reason": row["sample_warning"] or "至少一个班级没有有效估计"})
            continue
        if not present_values:
            continue
        if row["difference"] is not None and row["difference"] >= 0.1:
            best_item = max(present_values, key=lambda item: item["mean_mastery"])
            advantages.append({"knowledge_point_id": row["knowledge_point_id"], "name": row["name"], "class": best_item["class_code"], "mean_mastery": best_item["mean_mastery"], "pp_difference": row["pp_difference"], "valid_students": max(int(item["valid_students"] or 0) for item in row["values"])})
        if present_values and all(item["mean_mastery"] < 0.65 for item in present_values):
            common_weak.append({"knowledge_point_id": row["knowledge_point_id"], "name": row["name"], "means": {item["class_code"]: item["mean_mastery"] for item in row["values"]}, "valid_students": {item["class_code"]: item["valid_students"] for item in row["values"]}})
    rows.sort(key=lambda item: (item["pp_difference"] is None, -(abs(item["pp_difference"]) if item["pp_difference"] is not None else 0)))
    advantages.sort(key=lambda item: -(item["pp_difference"] or 0))
    common_weak.sort(key=lambda item: min(item["means"].values()))
    errors: Dict[str, Dict[str, Any]] = {}
    for cid, profile in profiles.items():
        for item in profile.get("error_types", []):
            entry = errors.setdefault(item["type"], {"type": item["type"], "by_class": {}})
            entry["by_class"][cid] = item["weighted_count"]
    return {
        "batch_id": batch_id, "classes": summary, "knowledge_points": rows,
        "advantages": advantages, "common_weak": common_weak, "insufficient": insufficient,
        "thresholds": {"min_sample": min_sample, "min_coverage": min_coverage, "weak_mastery": 0.65, "meaningful_pp": 10},
        "error_types": list(errors.values()),
        "note": "差值统一为百分点 pp；没有证据不按 0 分处理。",
    }


ANALYSIS_RETENTION = int(os.environ.get("XUEBAN_ANALYSIS_RETENTION", "5"))


def set_gold_error_labels(answer_id: str, labels: List[str], annotator: str, note: str = "") -> Dict[str, Any]:
    ensure_model()
    label_set = sorted({str(label) for label in labels if str(label) in ERROR_TAXONOMY})
    answer = db.q1("SELECT * FROM teacher_answers WHERE id=?", (answer_id,))
    if not answer:
        raise KeyError("作答不存在")
    db.x("INSERT INTO error_label_gold (id,batch_id,answer_id,labels_json,annotator,note,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(answer_id) DO UPDATE SET labels_json=excluded.labels_json, annotator=excluded.annotator, note=excluded.note, updated_at=excluded.updated_at", (db.new_id("goldlabel"), answer.get("import_batch_id") or "", answer_id, json.dumps(label_set, ensure_ascii=False), annotator, note, now(), now()))
    return {"answer_id": answer_id, "labels": label_set, "annotator": annotator}


def error_label_metrics(batch_id: str, class_id: str = "") -> Dict[str, Any]:
    """规则分类 vs 人工标注的多标签 Precision/Recall/F1 与混淆统计。"""
    ensure_model()
    params: List[Any] = [batch_id]
    scope = "a.import_batch_id=?"
    if class_id:
        scope += " AND s.class_id=?"
        params.append(class_id)
    rows = db.q(f"""
        SELECT g.answer_id, g.labels_json gold_json, l.labels_json predicted_json
        FROM error_label_gold g
        JOIN teacher_answers a ON a.id=g.answer_id
        JOIN teacher_students s ON s.id=a.student_id
        LEFT JOIN answer_error_labels l ON l.answer_id=g.answer_id AND l.source='rule'
        WHERE {scope}
    """, tuple(params))
    per_label: Dict[str, Dict[str, int]] = {}
    unknown_gold = 0
    unknown_pred = 0
    for row in rows:
        gold = set(json.loads(row["gold_json"] or "[]"))
        predicted = {item["label"] for item in json.loads(row.get("predicted_json") or "[]")}
        if "无法判断" in gold:
            unknown_gold += 1
        if "无法判断" in predicted:
            unknown_pred += 1
        for label in set(ERROR_TAXONOMY):
            entry = per_label.setdefault(label, {"tp": 0, "fp": 0, "fn": 0, "gold": 0, "predicted": 0})
            in_gold, in_pred = label in gold, label in predicted
            entry["gold"] += int(in_gold); entry["predicted"] += int(in_pred)
            if in_gold and in_pred:
                entry["tp"] += 1
            elif in_pred:
                entry["fp"] += 1
            elif in_gold:
                entry["fn"] += 1
    items = []
    for label, entry in per_label.items():
        precision = entry["tp"] / (entry["tp"] + entry["fp"]) if entry["tp"] + entry["fp"] else None
        recall = entry["tp"] / (entry["tp"] + entry["fn"]) if entry["tp"] + entry["fn"] else None
        f1 = (2 * precision * recall / (precision + recall)) if precision is not None and recall is not None and precision + recall else None
        if entry["gold"] or entry["predicted"]:
            items.append({"label": label, **entry, "precision": round(precision, 4) if precision is not None else None, "recall": round(recall, 4) if recall is not None else None, "f1": round(f1, 4) if f1 is not None else None})
    active = [item for item in items if item["f1"] is not None]
    macro = round(sum(item["f1"] for item in active) / len(active), 4) if active else None
    return {"batch_id": batch_id, "class_id": class_id, "answers_with_gold": len(rows), "labels": items, "macro_f1": macro, "unknown_rate": round(unknown_pred / len(rows), 4) if rows else None, "gold_unknown_rate": round(unknown_gold / len(rows), 4) if rows else None, "note": "人工标注是评价基准；规则/模型结果不参与真值。"}


def save_analysis(batch_id: str, scope: str, scope_id: str, payload: Dict[str, Any]) -> str:
    """分析快照：内容未变化时复用最新快照（不重复写库），变化时保留有限历史。"""
    ensure_model()
    mapping_version = current_mapping_version(batch_id)
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    result_hash = hashlib.sha256((body + f"|mapping:{mapping_version}").encode("utf-8")).hexdigest()[:32]
    latest = db.q1("SELECT id,result_hash FROM teacher_analysis_runs WHERE task_id=? AND scope=? AND scope_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1", (batch_id, scope, scope_id))
    if latest and latest.get("result_hash") == result_hash:
        return latest["id"]
    analysis_id = db.new_id("analysis")
    db.x("INSERT INTO teacher_analysis_runs (id,task_id,scope,scope_id,algorithm_version,parameter_json,result_json,result_hash,mapping_version,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (analysis_id, batch_id, scope, scope_id, ALGORITHM_VERSION, PARAM_VERSION, body, result_hash, str(mapping_version), now()))
    stale = db.q("SELECT id FROM teacher_analysis_runs WHERE task_id=? AND scope=? AND scope_id=? ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?", (batch_id, scope, scope_id, max(1, ANALYSIS_RETENTION)))
    for row in stale:
        db.x("DELETE FROM teacher_analysis_runs WHERE id=?", (row["id"],))
    return analysis_id
