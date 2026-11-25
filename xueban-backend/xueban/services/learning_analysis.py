"""Deterministic scoring-point analysis.

Mastery is a weighted Beta evidence estimate over scoring-point score ratios:
    alpha = a0 + sum(w * x)
    beta  = b0 + sum(w * (1 - x))
    mastery = alpha / (alpha + beta)
The 95% interval is a normal approximation to the Beta posterior, not a
black-box model. No evidence is treated as zero; it is reported as insufficient.
"""
from __future__ import annotations

import json
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


def _answers(batch_id: str) -> List[Dict[str, Any]]:
    return db.q("SELECT a.*, s.code student_code, s.class_id, c.code class_code, q.stem, q.question_type, q.max_score question_max_score, q.scoring_points, q.topic_id, q.indicator_id FROM teacher_answers a JOIN teacher_students s ON s.id=a.student_id LEFT JOIN teacher_classes c ON c.id=s.class_id LEFT JOIN teacher_questions q ON q.id=a.question_id WHERE a.import_batch_id=? ORDER BY a.student_id, a.question_id, a.revision DESC", (batch_id,))


def _evidence(batch_id: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    latest_event: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for answer in _answers(batch_id):
        key = (answer["student_id"], answer.get("event_id") or "", answer["question_id"])
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
        question = db.q1("SELECT * FROM teacher_questions WHERE id=?", (answer["question_id"],)) or {}
        kp = _knowledge_for_question(question)
        try:
            steps = json.loads(grade.get("step_results_json") or "[]")
        except Exception:
            steps = []
        links: List[Dict[str, Any]] = []
        for step in steps:
            for link in (step.get("scoring_point_links") or []):
                if not isinstance(link, dict):
                    continue
                sp = str(link.get("scoring_point_id") or "")
                full = float(link.get("max_score") or step.get("max_score") or 0)
                earned = float(link.get("awarded_score") or link.get("score") or 0)
                if not sp or full <= 0:
                    unmapped += 1
                    continue
                if earned < 0 or earned > full + 1e-6:
                    conflict += 1
                    continue
                if not kp:
                    unmapped += 1
                    continue
                links.append({"scoring_point_id": sp, "full": full, "earned": earned, "step": step, "grade": grade})
        if not links:
            # a grade without scoring-point links is retained in score totals but
            # cannot contribute to mastery; it is explicitly not treated as zero.
            continue
        # one question may contain multiple scoring points for the same KP.
        # Cap its total evidence weight at 1 so one question is not counted many times.
        per_question_weight = 1.0 / len(links)
        for item in links:
            x = max(0.0, min(1.0, item["earned"] / item["full"]))
            step = item["step"]
            evidence.append({
                "task_id": batch_id, "student_id": answer["student_id"], "student_code": answer.get("student_code"),
                "class_id": answer.get("class_id"), "class_code": answer.get("class_code"),
                "answer_id": answer["id"], "question_id": answer["question_id"], "exam_node_id": answer.get("exam_node_id"),
                "event_id": answer.get("event_id"), "revision": answer.get("revision"),
                "scoring_point_id": item["scoring_point_id"], "scoring_point_name": f"评分点 {item['scoring_point_id']}",
                "earned_score": round(item["earned"], 3), "full_score": round(item["full"], 3),
                "score_ratio": round(x, 4), "weight": round(per_question_weight, 6),
                "knowledge_point_id": kp["id"], "knowledge_point_name": kp["name"], "knowledge_topic": kp["topic"],
                "error_type": _canonical_error(step.get("error_type") or (item["grade"].get("error_type") if item["grade"].get("score", 0) < item["full"] else "")),
                "error_reason": step.get("judgment_reason") or item["grade"].get("judgment_reason") or "",
                "evidence_text": step.get("student_step") or answer.get("solution_process") or answer.get("original_answer") or "",
                "judgment_source": "manual" if item["grade"].get("status") in ("manual_corrected", "manual_confirmed") else "ai",
                "confidence": item["grade"].get("confidence") or step.get("confidence"),
                "grade_status": item["grade"].get("status"),
            })
    stats = {"answers": len(latest_event), "evidence": len(evidence), "unmapped": unmapped, "no_grade": no_grade, "invalid_score": conflict}
    return evidence, stats


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


def _interval(alpha: float, beta: float) -> Tuple[float, float]:
    mean = alpha / (alpha + beta)
    variance = alpha * beta / ((alpha + beta) ** 2 * (alpha + beta + 1))
    sd = math.sqrt(variance)
    return max(0.0, round(mean - 1.96 * sd, 4)), min(1.0, round(mean + 1.96 * sd, 4))


def student_profile(batch_id: str, student_id: str) -> Dict[str, Any]:
    evidence, stats = _evidence(batch_id)
    student = db.q1("SELECT s.*, c.code class_code FROM teacher_students s LEFT JOIN teacher_classes c ON c.id=s.class_id WHERE s.id=?", (student_id,))
    if not student:
        return {"error": "学生不存在"}
    own = [item for item in evidence if item["student_id"] == student_id]
    classes = db.q("SELECT COUNT(DISTINCT question_id) n FROM teacher_answers WHERE import_batch_id=? AND student_id=?", (batch_id, student_id))
    covered = len({item["question_id"] for item in own})
    total_questions = int(classes[0]["n"]) if classes else 0
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
        "task_id": batch_id, "algorithm_version": ALGORITHM_VERSION, "parameters": {"alpha0": A0, "beta0": B0, "min_evidence": MIN_EVIDENCE, "min_coverage": MIN_COVERAGE, "ci": "normal approximation to Beta posterior"},
        "mastery": points, "weak_points": weak,
        "coverage": {"total_questions": total_questions, "covered_questions": covered, "data_coverage": round(covered / total_questions, 4) if total_questions else 0},
        "evidence": own, "data_quality": stats,
        "suggestions": [f"针对「{item['knowledge_point_name']}」补强基础概念，并重做相关评分点题目。" for item in weak] or ["当前证据不足以形成稳定建议。"],
        "unknowns": [] if points else ["没有评分点级证据，暂不能估计掌握程度"],
    }


def class_profile(batch_id: str, class_id: str) -> Dict[str, Any]:
    cls = db.q1("SELECT * FROM teacher_classes WHERE id=?", (class_id,))
    if not cls:
        return {"error": "班级不存在"}
    students = db.q("SELECT * FROM teacher_students WHERE class_id=? ORDER BY code", (class_id,))
    profiles = [student_profile(batch_id, student["id"]) for student in students]
    valid_profiles = [item for item in profiles if item.get("mastery")]
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
        summary.append({"knowledge_point_id": item["knowledge_point_id"], "name": item["name"], "mean_mastery": round(mean, 4), "std_dev": round(math.sqrt(variance), 4), "valid_students": len(values), "total_students": len(students), "coverage": round(len(values) / len(students), 4) if students else 0, "insufficient_students": len(students) - len(values)})
    summary.sort(key=lambda item: item["mean_mastery"])
    return {"task_id": batch_id, "class": {"id": cls["id"], "code": cls["code"]}, "students": profiles, "knowledge_points": summary, "student_count": len(students), "valid_student_count": len(valid_profiles), "algorithm_version": ALGORITHM_VERSION, "parameters": {"alpha0": A0, "beta0": B0, "min_evidence": MIN_EVIDENCE, "min_coverage": MIN_COVERAGE}}


def all_classes(batch_id: str) -> List[Dict[str, Any]]:
    return db.q("SELECT id,code FROM teacher_classes ORDER BY code")


def save_analysis(batch_id: str, scope: str, scope_id: str, payload: Dict[str, Any]) -> str:
    analysis_id = db.new_id("analysis")
    db.x("INSERT INTO teacher_analysis_runs (id,task_id,scope,scope_id,algorithm_version,parameter_json,result_json,created_at) VALUES (?,?,?,?,?,?,?,?)", (analysis_id, batch_id, scope, scope_id, ALGORITHM_VERSION, PARAM_VERSION, json.dumps(payload, ensure_ascii=False), now()))
    return analysis_id
