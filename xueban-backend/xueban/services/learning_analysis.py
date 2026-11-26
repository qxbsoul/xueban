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

import json
import re
import math
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from . import xueban_store as db

ALGORITHM_VERSION = "sp-beta-v1"
PARAM_VERSION = "a0=2,b0=2,min_evidence=2"
A0 = 2.0
B0 = 2.0
MIN_EVIDENCE = 2.0
MIN_COVERAGE = 0.2

ERROR_MAP = {
    "概念理解错误": "概念理解错误", "概念": "概念理解错误", "concept": "概念理解错误",
    "方法选择错误": "方法选择错误", "方法": "方法选择错误", "method": "方法选择错误",
    "推理过程错误": "推理过程错误", "推理": "推理过程错误", "reasoning": "推理过程错误",
    "计算操作错误": "计算操作错误", "计算": "计算操作错误", "calculation": "计算操作错误",
    "表达规范错误": "表达规范错误", "表达": "表达规范错误", "expression": "表达规范错误",
    "其他": "其他", "无法确定": "无法确定", "": "无法确定",
}


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _latest_grade(answer_id: str) -> Optional[Dict[str, Any]]:
    rows = db.q("SELECT * FROM teacher_grade_runs WHERE answer_id=? ORDER BY grading_version DESC, created_at DESC", (answer_id,))
    valid = [row for row in rows if row.get("status") != "failed"]
    return (valid or rows or [None])[0]


def _canonical_error(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "无法确定"
    for key, label in ERROR_MAP.items():
        if key and key in text:
            return label
    return "其他"


def ensure_model() -> None:
    db.x("CREATE TABLE IF NOT EXISTS scoring_points (id TEXT PRIMARY KEY, code TEXT UNIQUE, name TEXT, description TEXT, created_at TEXT)")
    db.x("CREATE TABLE IF NOT EXISTS scoring_point_knowledge_links (id TEXT PRIMARY KEY, scoring_point_id TEXT, knowledge_point_id TEXT, weight REAL, source TEXT, confidence REAL, status TEXT, created_at TEXT, updated_at TEXT)")
    for question in db.q("SELECT * FROM teacher_questions WHERE scoring_points IS NOT NULL AND scoring_points != ''"):
        kp = _knowledge_for_question(question)
        if not kp:
            continue
        for code in sorted(set(re.findall(r"\b[A-Z]\d{3}P\d+\b", str(question.get("scoring_points"))))):
            sp_id = f"sp:{code}"
            db.x("INSERT OR IGNORE INTO scoring_points (id,code,name,description,created_at) VALUES (?,?,?,?,?)", (sp_id, code, code, f"来自题目 {question['id']} 的原始评分点", now()))
            db.x("INSERT OR IGNORE INTO scoring_point_knowledge_links (id,scoring_point_id,knowledge_point_id,weight,source,confidence,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)", (f"link:{code}:{kp['id']}", sp_id, kp["id"], 1.0, "legacy_indicator", 0.6, "legacy", now(), now()))


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


def _knowledge_links(scoring_point_id: str) -> List[Dict[str, Any]]:
    sp = db.q1("SELECT * FROM scoring_points WHERE id=? OR code=?", (f"sp:{scoring_point_id}", scoring_point_id))
    if not sp:
        return []
    return db.q("SELECT l.*, i.description, i.code indicator_code FROM scoring_point_knowledge_links l LEFT JOIN teacher_indicators i ON l.knowledge_point_id = 'kp:' || i.code WHERE l.scoring_point_id=? AND l.status != 'disabled' AND l.weight > 0 ORDER BY l.weight DESC", (sp["id"],))


def _answers(batch_id: str) -> List[Dict[str, Any]]:
    return db.q("SELECT a.*, s.code student_code, s.class_id, c.code class_code, q.stem, q.question_type, q.max_score question_max_score, q.scoring_points, q.topic_id, q.indicator_id FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id LEFT JOIN teacher_classes c ON c.id=s.class_id LEFT JOIN teacher_questions q ON q.id=a.question_id WHERE a.import_batch_id=? ORDER BY a.student_id, a.question_id, a.revision DESC", (batch_id,))


def _evidence(batch_id: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    ensure_model()
    latest_event: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for answer in _answers(batch_id):
        key = (answer["student_id"], answer.get("event_id") or f"answer:{answer['id']}", answer["question_id"])
        latest_event.setdefault(key, answer)
    evidence: List[Dict[str, Any]] = []
    unmapped = 0
    no_grade = 0
    conflict = 0
    for answer in latest_event.values():
        grade = _latest_grade(answer["id"])
        if not grade or grade.get("status") == "failed":
            no_grade += 1
            continue
        if grade.get("needs_review") or grade.get("status") == "manual_corrected":
            # whole-question manual edits do not rewrite scoring-point evidence.
            no_grade += 1
            continue
        try:
            steps = json.loads(grade.get("step_results_json") or "[]")
        except Exception:
            steps = []
        point_values = {}
        for step in steps:
            for link in (step.get("scoring_point_links") or []):
                if not isinstance(link, dict):
                    continue
                sp = str(link.get("scoring_point_id") or "")
                full = float(link.get("max_score") or step.get("max_score") or 0)
                awarded = link.get("awarded_score")
                awarded = link.get("score") if awarded is None else awarded
                earned = float(awarded if awarded is not None else 0)
                if not sp or full <= 0:
                    unmapped += 1
                    continue
                if sp in point_values:
                    old_earned, old_full = point_values[sp]["earned"], point_values[sp]["full"]
                    if abs(old_earned - earned) > 1e-6 or abs(old_full - full) > 1e-6:
                        conflict += 1
                        point_values[sp]["conflict"] = True
                    continue
                point_values[sp] = {"earned": earned, "full": full, "step": step, "grade": grade, "conflict": False}
        usable = {sp: value for sp, value in point_values.items() if not value["conflict"] and 0 <= value["earned"] <= value["full"] + 1e-6}
        if not usable:
            continue
        per_question_weight = 1.0 / len(usable)
        for sp, meta in usable.items():
            earned, full, step = meta["earned"], meta["full"], meta["step"]
            x = max(0.0, min(1.0, earned / full))
            kp_links = _knowledge_links(sp)
            if not kp_links:
                unmapped += 1
                continue
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
                    "score_ratio": round(x, 4), "weight": round(per_question_weight * float(kp_link.get("weight") or 0), 6),
                    "knowledge_point_id": kp_id, "knowledge_point_name": kp_name, "knowledge_topic": "",
                    "error_type": _canonical_error(step.get("error_type") or (meta["grade"].get("error_type") if meta["grade"].get("score", 0) < full else "")),
                    "error_reason": step.get("judgment_reason") or meta["grade"].get("judgment_reason") or "",
                    "evidence_text": step.get("student_step") or answer.get("solution_process") or answer.get("original_answer") or "",
                    "judgment_source": "manual" if meta["grade"].get("status") in ("manual_corrected", "manual_confirmed") else "ai",
                    "confidence": meta["grade"].get("confidence") or step.get("confidence"),
                    "grade_status": meta["grade"].get("status"),
                    "mapping_status": kp_link.get("status"),
                })
    stats = {"answers": len(latest_event), "evidence": len(evidence), "unmapped": unmapped, "no_grade": no_grade, "invalid_score": conflict}
    return evidence, stats


def _quantile(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    low = int(math.floor(pos)); high = int(math.ceil(pos))
    if low == high:
        return round(ordered[low], 4)
    return round(ordered[low] * (high - pos) + ordered[high] * (pos - low), 4)


def _status(mastery: float, evidence_weight: float, coverage: float) -> str:
    if evidence_weight < MIN_EVIDENCE or coverage < MIN_COVERAGE:
        return "证据不足"
    if mastery >= 0.8:
        return "掌握程度较高"
    if mastery >= 0.65:
        return "基本掌握"
    if mastery >= 0.5:
        return "有待巩固"
    return "掌握不足"


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


def _interval(alpha: float, beta: float) -> Tuple[float, float]:
    return round(_beta_quantile(0.025, alpha, beta), 4), round(_beta_quantile(0.975, alpha, beta), 4)


def student_profile(batch_id: str, student_id: str, evidence_override: Optional[List[Dict[str, Any]]] = None, stats_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    evidence, stats = (evidence_override, stats_override) if evidence_override is not None and stats_override is not None else _evidence(batch_id)
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
        grade = _latest_grade(answer["id"])
        if grade and grade.get("status") != "failed" and not grade.get("needs_review") and grade.get("status") != "manual_corrected":
            graded_question_ids.add(answer["question_id"])
        else:
            personal_no_grade += 1
    graded_questions = len(graded_question_ids)
    covered = len({item["question_id"] for item in own})
    evidence_answers = {item["answer_id"] for item in own}
    personal_unmapped = 0
    personal_invalid = 0
    for answer in selected.values():
        grade = _latest_grade(answer["id"])
        if not grade or grade.get("status") == "failed" or grade.get("needs_review") or grade.get("status") == "manual_corrected":
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
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for item in own:
        groups.setdefault(item["knowledge_point_id"], []).append(item)
    points = []
    for kp_id, items in groups.items():
        first = items[0]
        weight = sum(float(item["weight"]) for item in items)
        alpha = A0 + sum(float(item["weight"]) * item["score_ratio"] for item in items)
        beta = B0 + sum(float(item["weight"]) * (1 - item["score_ratio"]) for item in items)
        mastery = alpha / (alpha + beta)
        low, high = _interval(alpha, beta)
        coverage = round(len({item["question_id"] for item in items}) / total_questions, 4) if total_questions else 0
        error_weights: Dict[str, float] = {}
        for item in items:
            if item["score_ratio"] < 1 and item["error_type"] != "无法确定":
                error_weights[item["error_type"]] = error_weights.get(item["error_type"], 0) + item["weight"] * (1 - item["score_ratio"])
        total_error = sum(error_weights.values())
        points.append({
            "knowledge_point_id": kp_id, "knowledge_point_name": first["knowledge_point_name"], "topic": first["knowledge_topic"],
            "mastery_estimate": round(mastery, 4), "credible_interval_lower": low, "credible_interval_upper": high,
            "evidence_count": len(items), "effective_evidence_weight": round(weight, 4),
            "covered_question_count": len({item["question_id"] for item in items}), "data_coverage": coverage,
            "confidence_level": 0.95, "status": _status(mastery, weight, coverage),
            "error_types": [{"type": key, "weighted_count": round(value, 4), "ratio": round(value / total_error, 4)} for key, value in sorted(error_weights.items(), key=lambda x: -x[1])],
        })
    points.sort(key=lambda item: (item["status"] == "证据不足", -item["effective_evidence_weight"], item["mastery_estimate"]))
    weak = [item for item in points if item["status"] in ("掌握不足", "有待巩固")][:3]
    return {
        "student": {"id": student["id"], "code": student["code"], "class_id": student["class_id"], "class_code": student.get("class_code")},
        "task_id": batch_id, "algorithm_version": ALGORITHM_VERSION, "parameters": {"alpha0": A0, "beta0": B0, "min_evidence": MIN_EVIDENCE, "min_coverage": MIN_COVERAGE, "ci": "exact Beta equal-tailed interval (0.025, 0.975)"},
        "mastery": points, "weak_points": weak,
        "coverage": {"expected_question_count": None, "answered_question_count": answered_questions, "graded_question_count": graded_questions, "evidence_question_count": covered, "answered_coverage": None, "graded_coverage": round(graded_questions / answered_questions, 4) if answered_questions else None, "evidence_coverage": round(covered / answered_questions, 4) if answered_questions else None},
        "evidence": own,
        "data_quality": {"raw_answers": len(raw_answers), "selected_answers": len(selected), "valid_answers": graded_questions, "no_valid_grade": personal_no_grade, "evidence_questions": covered, "evidence_count": len(own), "unmapped": personal_unmapped, "invalid_score": personal_invalid, "pending_review": personal_no_grade},
        "batch_data_quality": stats,
        "suggestions": [f"针对「{item['knowledge_point_name']}」补强基础概念，并重做相关评分点题目。" for item in weak] or ["当前证据不足以形成稳定建议。"],
        "unknowns": [] if points else ["没有评分点级证据，暂不能估计掌握程度"],
    }


def _class_dimensions(batch_id: str, class_id: str, evidence: List[Dict[str, Any]]) -> Dict[str, Any]:
    answers = db.q("SELECT a.*, s.class_id FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id WHERE a.import_batch_id=? AND s.class_id=? ORDER BY a.exam_node_id, a.student_id, a.revision DESC", (batch_id, class_id))
    latest = {}
    for answer in answers:
        latest.setdefault((answer["student_id"], answer.get("event_id") or f"answer:{answer['id']}", answer["question_id"]), answer)
    groups: Dict[str, Dict[str, Any]] = {}
    for answer in latest.values():
        node = answer.get("exam_node_id") or "未分组"
        group = groups.setdefault(node, {"exam_node_id": node, "student_ids": set(), "question_ids": set(), "answers": 0, "graded": 0, "score_students": set(), "graded_students": set(), "failed": 0, "review": 0, "pending": 0, "score": 0.0, "max_score": 0.0})
        group["student_ids"].add(answer["student_id"]); group["question_ids"].add(answer["question_id"]); group["answers"] += 1
        grade = _latest_grade(answer["id"])
        if grade and grade.get("status") == "failed":
            group["failed"] += 1
        elif grade and grade.get("status") != "manual_corrected" and not grade.get("needs_review"):
            # whole-question manual corrections remain valid score evidence.
            group["graded"] += 1; group["graded_students"].add(answer["student_id"]); group["score_students"].add(answer["student_id"]); group["score"] += float(grade.get("score") or 0); group["max_score"] += float(answer.get("max_score") or 0)
        elif grade and grade.get("status") == "manual_corrected":
            group["graded"] += 1; group["graded_students"].add(answer["student_id"]); group["score_students"].add(answer["student_id"]); group["score"] += float(grade.get("score") or 0); group["max_score"] += float(answer.get("max_score") or 0)
        elif grade and grade.get("needs_review"):
            group["review"] += 1
        else:
            group["pending"] += 1
    result = []
    for node, group in groups.items():
        result.append({"exam_node_id": node, "answered_students": len(group["student_ids"]), "valid_judgement_students": len(group["graded_students"]), "valid_score_students": len(group["score_students"]), "questions": len(group["question_ids"]), "answers": group["answers"], "graded": group["graded"], "pending": group["pending"], "failed": group["failed"], "review": group["review"], "score": round(group["score"], 2), "max_score": round(group["max_score"], 2), "score_rate": round(group["score"] / group["max_score"] * 100, 2) if group["max_score"] else None})
    exams = db.q("SELECT id,exam_date FROM teacher_exams WHERE id IN (SELECT DISTINCT exam_node_id FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id WHERE a.import_batch_id=? AND s.class_id=?) AND exam_date IS NOT NULL AND exam_date != '' ORDER BY exam_date", (batch_id, class_id))
    trends = []
    reference = result[0] if result else None
    for exam in exams:
        row = next((item for item in result if item["exam_node_id"] == exam["id"]), None)
        if row:
            comparable = bool(reference and row["questions"] == reference["questions"] and row["max_score"] == reference["max_score"])
            trends.append({"exam_node_id": exam["id"], "exam_date": exam["exam_date"], "answered_students": row["answered_students"], "valid_judgement_students": row["valid_judgement_students"], "valid_score_students": row["valid_score_students"], "score_rate": row["score_rate"], "questions": row["questions"], "comparable": comparable, "comparable_reason": "题目数与满分一致" if comparable else "题目构成或满分不同，不可直接比较能力变化"})
    error_counts: Dict[str, Dict[str, Any]] = {}
    for item in evidence:
        if item["class_id"] != class_id or item["score_ratio"] >= 1 or item["error_type"] == "无法确定":
            continue
        entry = error_counts.setdefault(item["error_type"], {"type": item["error_type"], "weighted_count": 0.0, "students": set(), "answers": set(), "knowledge_points": set(), "questions": set(), "exam_nodes": set()})
        entry["weighted_count"] += item["weight"] * (1 - item["score_ratio"])
        entry["students"].add(item["student_id"]); entry["answers"].add(item["answer_id"]); entry["knowledge_points"].add(item["knowledge_point_name"]); entry["questions"].add(item["question_id"]); entry["exam_nodes"].add(item.get("exam_node_id") or "未分组")
    total_error = sum(item["weighted_count"] for item in error_counts.values())
    errors = [{"type": item["type"], "weighted_count": round(item["weighted_count"], 4), "student_count": len(item["students"]), "answer_count": len(item["answers"]), "weighted_loss_share": round(item["weighted_count"] / total_error, 4) if total_error else None, "knowledge_points": sorted(item["knowledge_points"]), "questions": sorted(item["questions"]), "exam_nodes": sorted(item["exam_nodes"])} for item in error_counts.values()]
    return {"groups": result, "group_by": "测验节点（CSV 无独立题组字段，不虚构题组）", "groups_available": False, "group_data_reason": "CSV 没有题组编号、题组名称或题目所属题组字段", "trends": trends, "error_types": sorted(errors, key=lambda item: -item["weighted_count"])}


def _question_analysis(batch_id: str, class_id: str) -> List[Dict[str, Any]]:
    rows = db.q("SELECT a.*, s.code student_code, s.class_id, q.stem, q.question_type, q.max_score question_max_score, q.scoring_points FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id JOIN teacher_questions q ON q.id=a.question_id WHERE a.import_batch_id=? AND s.class_id=? ORDER BY a.question_id, a.student_id, a.revision DESC", (batch_id, class_id))
    latest = {}
    for row in rows:
        latest.setdefault((row["student_id"], row.get("event_id") or f"answer:{row['id']}", row["question_id"]), row)
    grouped = {}
    for row in latest.values():
        item = grouped.setdefault(row["question_id"], {"question_id": row["question_id"], "stem": row.get("stem"), "question_type": row.get("question_type"), "max_score": float(row.get("question_max_score") or 0), "answered_students": set(), "answered_events": 0, "valid_judgement_students": set(), "valid_judgement_events": 0, "valid_score_students": set(), "review_students": set(), "failed_students": set(), "pending_students": set(), "event_scores": [], "student_scores": {}, "scoring_points": {}, "error_types": {}, "distribution": {"0-59": 0, "60-79": 0, "80-100": 0}, "manual_unsynced": 0})
        student = row["student_id"]; item["answered_students"].add(student); item["answered_events"] += 1
        grade = _latest_grade(row["id"])
        if not grade:
            item["pending_students"].add(student); continue
        if grade.get("status") == "failed":
            item["failed_students"].add(student); continue
        if grade.get("needs_review"):
            item["review_students"].add(student); continue
        item["valid_judgement_students"].add(student); item["valid_judgement_events"] += 1
        score = float(grade.get("score") or 0); item["event_scores"].append(score); item["valid_score_students"].add(student)
        item["student_scores"].setdefault(student, []).append(score)
        if item["max_score"]:
            rate = score / item["max_score"] * 100
            item["distribution"]["0-59" if rate < 60 else ("60-79" if rate < 80 else "80-100")] += 1
        try:
            steps = json.loads(grade.get("step_results_json") or "[]")
        except Exception:
            steps = []
        if grade.get("status") == "manual_corrected":
            item["manual_unsynced"] += 1
            continue
        seen = set()
        for step in steps:
            for link in (step.get("scoring_point_links") or []):
                if not isinstance(link, dict):
                    continue
                sp = str(link.get("scoring_point_id") or "")
                if not sp or sp in seen:
                    continue
                seen.add(sp)
                full = float(link.get("max_score") or 0); awarded = link.get("awarded_score"); awarded = link.get("score") if awarded is None else awarded; awarded = float(awarded or 0)
                sp_item = item["scoring_points"].setdefault(sp, {"scoring_point_id": sp, "full_scores": set(), "total_earned": 0.0, "valid_students": set(), "events": 0, "student_scores": {}, "unmapped_knowledge_count": 0, "invalid_score_count": 0, "insufficient_count": 0})
                sp_item["full_scores"].add(full); sp_item["total_earned"] += awarded; sp_item["valid_students"].add(student); sp_item["events"] += 1
                sp_item["student_scores"].setdefault(student, []).append(awarded)
                if awarded < 0 or awarded > full:
                    sp_item["invalid_score_count"] += 1
                if not _knowledge_links(sp):
                    sp_item["unmapped_knowledge_count"] += 1
                error = _canonical_error(step.get("error_type"))
                if awarded < full and error != "无法确定":
                    entry = item["error_types"].setdefault(error, {"students": set(), "answers": 0, "scoring_points": set()})
                    entry["students"].add(student); entry["answers"] += 1; entry["scoring_points"].add(sp)
    result = []
    for item in grouped.values():
        event_scores = item["event_scores"]; student_means = [sum(values) / len(values) for values in item["student_scores"].values() if values]
        points = []
        for sp in item["scoring_points"].values():
            full_values = sorted(sp["full_scores"]); full = full_values[0] if len(full_values) == 1 else None
            valid = len(sp["valid_students"])
            event_average = sp["total_earned"] / sp["events"] if sp["events"] else None
            student_means = [sum(values) / len(values) for values in sp["student_scores"].values() if values]
            student_average = sum(student_means) / len(student_means) if student_means else None
            missing = max(0, len(item["valid_score_students"]) - valid)
            points.append({"scoring_point_id": sp["scoring_point_id"], "full_score": full, "full_score_consistent": len(full_values) <= 1, "total_earned_score": round(sp["total_earned"], 2), "valid_events": sp["events"], "valid_students": valid, "event_average_score": round(event_average, 2) if event_average is not None else None, "student_average_score": round(student_average, 2) if student_average is not None else None, "event_average_score_rate": round(event_average / full * 100, 2) if event_average is not None and full else None, "student_average_score_rate": round(student_average / full * 100, 2) if student_average is not None and full else None, "unmapped_knowledge_count": sp["unmapped_knowledge_count"], "invalid_score_count": sp["invalid_score_count"], "missing_scoring_point_result_count": missing, "insufficient_count": 0})
        result.append({"question_id": item["question_id"], "stem": item["stem"], "question_type": item["question_type"], "max_score": item["max_score"], "answered_students": len(item["answered_students"]), "answered_events": item["answered_events"], "valid_judgement_students": len(item["valid_judgement_students"]), "valid_judgement_events": item["valid_judgement_events"], "valid_score_students": len(item["valid_score_students"]), "review_students": len(item["review_students"]), "failed_students": len(item["failed_students"]), "pending_students": len(item["pending_students"]), "average_score": round(sum(event_scores) / len(event_scores), 2) if event_scores else None, "student_average_score": round(sum(student_means) / len(student_means), 2) if student_means else None, "score_rate": round(sum(event_scores) / (len(event_scores) * item["max_score"]) * 100, 2) if event_scores and item["max_score"] else None, "score_distribution": item["distribution"], "scoring_points": points, "error_types": [{"type": key, "student_count": len(value["students"]), "answer_count": value["answers"], "scoring_points": sorted(value["scoring_points"])} for key, value in item["error_types"].items()], "manual_correction_without_scoring_sync": item["manual_unsynced"]})
    return sorted(result, key=lambda row: row["question_id"])


def question_analysis_detail(batch_id: str, class_id: str, question_id: str) -> Dict[str, Any]:
    summary = next((row for row in _question_analysis(batch_id, class_id) if row["question_id"] == question_id), None)
    if not summary:
        return {"error": "当前批次/班级没有这道题"}
    rows = db.q("SELECT a.*, s.code student_code, s.class_id, q.stem, q.question_type, q.max_score question_max_score, q.reference_answer, q.reference_solution, q.scoring_points FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id JOIN teacher_questions q ON q.id=a.question_id WHERE a.import_batch_id=? AND s.class_id=? AND a.question_id=? ORDER BY a.student_id, a.revision DESC", (batch_id, class_id, question_id))
    latest = {}
    for row in rows:
        latest.setdefault((row["student_id"], row.get("event_id") or f"answer:{row['id']}"), row)
    students = []
    distribution = {"0-59": 0, "60-79": 0, "80-100": 0}
    for row in latest.values():
        grade = _latest_grade(row["id"])
        status = "pending"; score = None; sp_scores = []; error = "无法确定"; reason = ""; text = row.get("solution_process") or row.get("original_answer") or ""
        if grade:
            status = grade.get("status") or "graded"; reason = grade.get("judgment_reason") or ""
            if status == "failed":
                score = None
            elif grade.get("needs_review"):
                status = "needs_review"; score = None
            else:
                score = grade.get("score")
                try:
                    steps = json.loads(grade.get("step_results_json") or "[]")
                except Exception:
                    steps = []
                if status != "manual_corrected":
                    for step in steps:
                        for link in (step.get("scoring_point_links") or []):
                            if isinstance(link, dict):
                                awarded = link.get("awarded_score"); awarded = link.get("score") if awarded is None else awarded
                                sp_scores.append({"scoring_point_id": link.get("scoring_point_id"), "awarded_score": awarded, "max_score": link.get("max_score")})
                            if step.get("error_type"):
                                error = _canonical_error(step.get("error_type"))
                if score is not None and row.get("question_max_score"):
                    rate = float(score) / float(row["question_max_score"]) * 100
                    distribution["0-59" if rate < 60 else ("60-79" if rate < 80 else "80-100")] += 1
        students.append({"student_id": row["student_id"], "student_code": row["student_code"], "answer_id": row["id"], "event_id": row.get("event_id"), "revision": row.get("revision"), "answer": row.get("original_answer"), "solution_process": text, "status": status, "score": score, "max_score": row.get("question_max_score"), "score_rate": round(float(score) / float(row["question_max_score"]) * 100, 2) if score is not None and row.get("question_max_score") else None, "scoring_points": sp_scores, "error_type": error, "judgment_reason": reason, "manual": status in ("manual_corrected", "manual_confirmed")})
    return {"question": {"question_id": question_id, "stem": summary["stem"], "question_type": summary["question_type"], "max_score": summary["max_score"], "reference_answer": rows[0].get("reference_answer") if rows else "", "reference_solution": rows[0].get("reference_solution") if rows else "", "scoring_points": summary["scoring_points"]}, "overall": summary, "score_distribution": distribution, "scoring_points": summary["scoring_points"], "error_types": summary["error_types"], "students": students, "longitudinal": {"available": False, "reason": "当前题目没有可确认的多个可比独立测量时间点"}}


def class_profile(batch_id: str, class_id: str) -> Dict[str, Any]:
    cls = db.q1("SELECT * FROM teacher_classes WHERE id=?", (class_id,))
    if not cls:
        return {"error": "班级不存在"}
    students = db.q("SELECT * FROM teacher_students WHERE class_id=? ORDER BY code", (class_id,))
    evidence, stats = _evidence(batch_id)
    profiles = [student_profile(batch_id, student["id"], evidence, stats) for student in students]
    valid_profiles = [item for item in profiles if any(point.get("status") != "证据不足" for point in item.get("mastery", []))]
    points: Dict[str, Dict[str, Any]] = {}
    for profile in valid_profiles:
        for point in profile["mastery"]:
            if point["status"] == "证据不足":
                continue
            entry = points.setdefault(point["knowledge_point_id"], {"knowledge_point_id": point["knowledge_point_id"], "name": point["knowledge_point_name"], "values": [], "evidence": []})
            entry["values"].append(point["mastery_estimate"])
            entry["evidence"].append(point["effective_evidence_weight"])
    summary = []
    for item in points.values():
        values = item["values"]
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values) if values else 0
        weak_count = sum(1 for value in values if value < 0.65)
        summary.append({"knowledge_point_id": item["knowledge_point_id"], "name": item["name"], "mean_mastery": round(mean, 4), "std_dev": round(math.sqrt(variance), 4), "q1": _quantile(values, 0.25), "median": _quantile(values, 0.5), "q3": _quantile(values, 0.75), "valid_students": len(values), "total_students": len(students), "students_with_answers": sum(1 for profile in profiles if profile.get("coverage", {}).get("answered_question_count")), "students_with_evidence": sum(1 for profile in profiles if any(point.get("knowledge_point_id") == item["knowledge_point_id"] for point in profile.get("mastery", []))), "coverage": round(len(values) / len(students), 4) if students else 0, "weak_students": weak_count, "weak_rate": round(weak_count / len(values), 4) if values else None, "insufficient_students": len(students) - len(values)})
    summary.sort(key=lambda item: item["mean_mastery"])
    dimensions = _class_dimensions(batch_id, class_id, evidence)
    student_overall = []
    for profile in valid_profiles:
        values = [point["mastery_estimate"] for point in profile.get("mastery", []) if point.get("status") != "证据不足"]
        if values:
            student_overall.append(sum(values) / len(values))
    overall = student_overall
    weak_students = [profile for profile in valid_profiles if profile.get("weak_points")]
    return {"task_id": batch_id, "class": {"id": cls["id"], "code": cls["code"]}, "students": profiles, "knowledge_points": summary, "student_count": len(students), "students_with_answers": sum(1 for profile in profiles if profile.get("coverage", {}).get("answered_question_count")), "students_with_evidence": sum(1 for profile in profiles if profile.get("mastery")), "valid_student_count": len(valid_profiles), "weak_student_count": len(weak_students), "insufficient_student_count": len(students) - len(valid_profiles), "overall_mastery": round(sum(overall) / len(overall), 4) if overall else None, "overall_mastery_note": "先按学生平均其有效知识点，再对有有效估计的学生等权平均；不代表统一量表分数", "overall_weak_rate": round(len(weak_students) / len(valid_profiles), 4) if valid_profiles else None, "questions": _question_analysis(batch_id, class_id), "trends": dimensions["trends"], "error_types": dimensions["error_types"], "algorithm_version": ALGORITHM_VERSION, "parameters": {"alpha0": A0, "beta0": B0, "min_evidence": MIN_EVIDENCE, "min_coverage": MIN_COVERAGE, "weak_threshold": 0.65}}


def all_classes(batch_id: str) -> List[Dict[str, Any]]:
    return db.q("SELECT DISTINCT c.id,c.code FROM teacher_classes c JOIN teacher_students s ON s.class_id=c.id JOIN teacher_answers a ON a.student_id=s.id WHERE a.import_batch_id=? ORDER BY c.code", (batch_id,))


def mapping_audit() -> Dict[str, Any]:
    ensure_model()
    total = db.q1("SELECT COUNT(*) n FROM scoring_points")["n"]
    mapped = db.q1("SELECT COUNT(DISTINCT scoring_point_id) n FROM scoring_point_knowledge_links WHERE status != 'disabled'")["n"]
    statuses = {row["status"]: row["n"] for row in db.q("SELECT status,COUNT(*) n FROM scoring_point_knowledge_links GROUP BY status")}
    bad = db.q1("SELECT COUNT(*) n FROM (SELECT scoring_point_id,SUM(weight) total FROM scoring_point_knowledge_links WHERE status != 'disabled' GROUP BY scoring_point_id HAVING ABS(total-1.0) > 0.001)")["n"]
    duplicates = db.q1("SELECT COUNT(*) n FROM (SELECT code,COUNT(*) c FROM scoring_points GROUP BY code HAVING c>1)")["n"]
    return {"scoring_points": total, "mapped_scoring_points": mapped, "unmapped_scoring_points": total - mapped, "statuses": statuses, "illegal_weight_groups": bad, "duplicate_codes": duplicates, "mapping_status": "legacy indicator mappings are not teacher-confirmed"}


def save_analysis(batch_id: str, scope: str, scope_id: str, payload: Dict[str, Any]) -> str:
    analysis_id = db.new_id("analysis")
    db.x("INSERT INTO teacher_analysis_runs (id,task_id,scope,scope_id,algorithm_version,parameter_json,result_json,created_at) VALUES (?,?,?,?,?,?,?,?)", (analysis_id, batch_id, scope, scope_id, ALGORITHM_VERSION, PARAM_VERSION, json.dumps(payload, ensure_ascii=False), now()))
    return analysis_id
