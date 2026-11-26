"""全量判题准入预检（只读，不调用模型、不修改数据库）。"""
import argparse
import csv
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path


def columns(con, table):
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")} if table in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")} else set()


def parse_points(text):
    if isinstance(text, dict):
        return {str(k): float(v) for k, v in text.items() if float(v or 0) > 0}
    specs = {}
    for match in re.finditer(r"([A-Z]\d{3}P\d+)\s*[^\n]*?(?<![0-9.\-])([0-9]+(?:\.[0-9]+)?)\s*分", str(text or "")):
        specs[match.group(1)] = float(match.group(2))
    return specs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--batch", default="batch_305316cdd0")
    parser.add_argument("--out", default=r"D:\capital lion\verify-artifacts\v1.5.13")
    args = parser.parse_args()
    path = Path(args.db).resolve()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    answer_cols = columns(con, "teacher_answers")
    grade_cols = columns(con, "teacher_grade_runs")
    version_cols = columns(con, "teacher_question_versions")
    question_cols = columns(con, "teacher_questions")
    batch_cols = columns(con, "teacher_batches")
    has_versions = "teacher_question_versions" in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    has_issues = "teacher_data_issues" in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    has_sp = "scoring_points" in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    has_links = "scoring_point_knowledge_links" in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    rows = con.execute(f"""
        SELECT a.*, s.code student_code, s.class_id, c.code class_code,
               COALESCE(v.stem, q.stem) stem,
               COALESCE(v.reference_answer, q.reference_answer) reference_answer,
               COALESCE(v.reference_solution, q.reference_solution) reference_solution,
               COALESCE(v.max_score, q.max_score) question_max_score,
               COALESCE(v.scoring_points, q.scoring_points) scoring_points,
               COALESCE(v.question_type, q.question_type) question_type,
               v.version_id version_pk, v.question_id version_question_id, v.batch_id version_batch_id,
               q.id global_question_id, a.standard_id answer_standard_id,
               (SELECT COUNT(*) FROM teacher_grade_runs g WHERE g.answer_id=a.id) grade_count,
               (SELECT MAX(g.grading_version) FROM teacher_grade_runs g WHERE g.answer_id=a.id) latest_version
        FROM teacher_answers a
        JOIN teacher_students s ON s.id=a.student_id
        LEFT JOIN teacher_classes c ON c.id=s.class_id
        LEFT JOIN teacher_question_versions v ON v.version_id=a.question_version_id
        LEFT JOIN teacher_questions q ON q.id=a.question_id
        WHERE a.import_batch_id=?
        ORDER BY a.student_id, a.question_id, a.event_id
    """, (args.batch,)).fetchall()
    latest_grades = {}
    if "teacher_grade_runs" in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        for row in con.execute("""
            SELECT g.* FROM teacher_grade_runs g JOIN teacher_answers a ON a.id=g.answer_id
            WHERE a.import_batch_id=? AND g.id=(
              SELECT g2.id FROM teacher_grade_runs g2 WHERE g2.answer_id=g.answer_id
              ORDER BY g2.grading_version DESC, g2.created_at DESC LIMIT 1
            )
        """, (args.batch,)):
            latest_grades[row["answer_id"]] = dict(row)

    issue_rows = []
    if has_issues:
        issue_rows = [dict(row) for row in con.execute("SELECT * FROM teacher_data_issues WHERE task_id=? AND handling_status='pending_review'", (args.batch,))]
    issue_by_answer = {}
    for row in issue_rows:
        issue_by_answer.setdefault((row.get("event_id"), row.get("revision"), row.get("question_id")), []).append(row)

    anomalies = []
    counts = Counter()
    categories = Counter()
    seen = {}
    for row in rows:
        item = dict(row)
        answer_id = item["id"]
        issue_list = []
        key = (item.get("student_id"), item.get("event_id"), item.get("revision"), item.get("question_id"))
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            issue_list.append({"code": "duplicate_event_revision", "severity": "blocking", "message": "同一学生同一事件/版本重复作答"})
        if not str(item.get("stem") or "").strip():
            issue_list.append({"code": "missing_question_stem", "severity": "blocking", "message": "缺题目内容"})
        if float(item.get("question_max_score") or 0) <= 0:
            issue_list.append({"code": "missing_max_score", "severity": "blocking", "message": "缺题目满分"})
        if not str(item.get("scoring_points") or "").strip():
            issue_list.append({"code": "missing_scoring_points", "severity": "blocking", "message": "缺原始评分点"})
        else:
            specs = parse_points(item.get("scoring_points"))
            if not specs:
                issue_list.append({"code": "unparsable_scoring_points", "severity": "review", "message": "评分点无法解析为结构化分值"})
            elif abs(sum(specs.values()) - float(item.get("question_max_score") or 0)) > 0.05:
                issue_list.append({"code": "scoring_points_sum_mismatch", "severity": "review", "message": "评分点合计与满分不一致"})
        if not item.get("version_pk"):
            issue_list.append({"code": "missing_question_version", "severity": "review", "message": "缺题目版本，判题将回退全局题表"})
        elif item.get("version_question_id") != item.get("question_id") or str(item.get("version_batch_id") or "") != args.batch:
            issue_list.append({"code": "question_version_mismatch", "severity": "blocking", "message": "作答绑定的题目版本与题目/批次不一致"})
        if not str(item.get("reference_answer") or "").strip():
            issue_list.append({"code": "missing_reference_answer", "severity": "review", "message": "缺参考答案"})
        if not str(item.get("reference_solution") or "").strip():
            issue_list.append({"code": "missing_reference_solution", "severity": "review", "message": "缺参考解析"})
        if not str(item.get("solution_process") or "").strip():
            issue_list.append({"code": "missing_solution_process", "severity": "review", "message": "缺解题过程"})
        if not str(item.get("answer_standard_id") or "").strip():
            issue_list.append({"code": "missing_standard", "severity": "analysis", "message": "作答未关联课程标准（影响学情分析，不阻断判题）"})
        for issue in issue_by_answer.get((item.get("event_id"), item.get("revision"), item.get("question_id")), []):
            severity = "blocking" if issue.get("issue_type") in ("score_out_of_range", "scoring_points_sum_mismatch") else "review"
            issue_list.append({"code": f"pending_issue:{issue.get('issue_type')}", "severity": severity, "message": issue.get("reason") or "未处理数据问题"})
        grade = latest_grades.get(answer_id)
        if grade:
            counts["graded"] += 1
            if grade.get("status") == "failed":
                counts["failed"] += 1
            if grade.get("needs_review"):
                counts["needs_review"] += 1
            if grade.get("structure_json"):
                try:
                    structure = json.loads(grade.get("structure_json") or "{}")
                    validation = structure.get("validation") or {}
                    if validation.get("issue_count"):
                        issue_list.append({"code": "historical_structure_issues", "severity": "info", "message": f"历史判题存在 {validation.get('issue_count')} 个结构校验问题"})
                except Exception:
                    issue_list.append({"code": "historical_structure_unreadable", "severity": "review", "message": "历史判题结构 JSON 无法解析"})
        else:
            counts["pending"] += 1
        blocking = any(item["severity"] == "blocking" for item in issue_list)
        review = any(item["severity"] == "review" for item in issue_list)
        analysis_only = any(item["severity"] == "analysis" for item in issue_list)
        if grade:
            categories["already_graded"] += 1
        elif blocking:
            categories["pending_blocked"] += 1
        elif review:
            categories["pending_review"] += 1
            categories["gradable_pending"] += 1
        else:
            categories["pending_direct"] += 1
            categories["gradable_pending"] += 1
        if analysis_only:
            categories["analysis_only"] += 1
        if issue_list:
            anomalies.append({"answer_id": answer_id, "batch_id": args.batch, "question_id": item.get("question_id"), "student_code": item.get("student_code"), "question_version_id": item.get("question_version_id"), "status": grade.get("status") if grade else "pending", "category": "excluded" if blocking else ("review" if review else "info"), "issues": issue_list})

    mappings = {"total": 0, "confirmed": 0, "legacy": 0, "rejected": 0}
    if has_sp and has_links:
        for row in con.execute("""
            SELECT COALESCE(l.status,'legacy') status, COUNT(*) n
            FROM scoring_points sp LEFT JOIN scoring_point_knowledge_links l ON l.scoring_point_id=sp.id AND l.status != 'disabled'
            WHERE sp.batch_id=? GROUP BY COALESCE(l.status,'legacy')
        """, (args.batch,)):
            mappings[row["status"]] = row["n"]
            mappings["total"] += row["n"]
    job = None
    if "teacher_grade_jobs" in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        job_row = con.execute("SELECT * FROM teacher_grade_jobs WHERE batch_id=? ORDER BY created_at DESC LIMIT 1", (args.batch,)).fetchone()
        if job_row:
            job = {key: job_row[key] for key in ("id", "status", "processed", "completed", "failed", "needs_review", "created_at")}
            job["items"] = {row[0]: row[1] for row in con.execute("SELECT status, COUNT(*) FROM teacher_grade_job_items WHERE job_id=? GROUP BY status", (job_row["id"],))}
            job["processed_consistent"] = int(job_row["processed"] or 0) == int(job_row["completed"] or 0) + int(job_row["failed"] or 0)
    graded_without_textbook = 0
    historical_without_structure = 0
    if "teacher_grade_runs" in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        graded_without_textbook = con.execute("SELECT COUNT(*) FROM teacher_grade_runs g JOIN teacher_answers a ON a.id=g.answer_id WHERE a.import_batch_id=? AND (g.textbook_context IS NULL OR g.textbook_context='')", (args.batch,)).fetchone()[0]
        if "structure_json" in grade_cols:
            historical_without_structure = con.execute("SELECT COUNT(*) FROM teacher_grade_runs g JOIN teacher_answers a ON a.id=g.answer_id WHERE a.import_batch_id=? AND (g.structure_json IS NULL OR g.structure_json='')", (args.batch,)).fetchone()[0]
        else:
            historical_without_structure = con.execute("SELECT COUNT(*) FROM teacher_grade_runs g JOIN teacher_answers a ON a.id=g.answer_id WHERE a.import_batch_id=?", (args.batch,)).fetchone()[0]
    conflicts = 0
    if "teacher_grade_runs" in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        conflicts = con.execute("""
            SELECT COUNT(*) FROM (SELECT answer_id, grading_version, COUNT(*) n FROM teacher_grade_runs
            WHERE answer_id IN (SELECT id FROM teacher_answers WHERE import_batch_id=?)
            GROUP BY answer_id, grading_version HAVING n > 1)
        """, (args.batch,)).fetchone()[0]
    summary = {
        "database": str(path),
        "batch_id": args.batch,
        "counts": dict(counts),
        "categories": dict(categories),
        "answers_total": len(rows),
        "pending_issues": len(issue_rows),
        "mappings": mappings,
        "history_version_conflicts": conflicts,
        "graded_without_textbook_context": graded_without_textbook,
        "historical_grades_without_structure": historical_without_structure,
        "mapping_unconfirmed": max(0, mappings.get("total", 0) - mappings.get("confirmed", 0)),
        "job": job,
        "model_config": {
            "api_key_configured": bool(os.environ.get("DEEPSEEK_API_KEY", "").strip()),
            "provider": "deepseek",
            "model": "deepseek-chat",
            "prompt_version": "teacher-grade-v4",
            "grader_version": "v1.5.13",
        },
        "estimated_api_calls": categories.get("gradable_pending", 0),
        "note": "只读预检；不调用模型、不修改数据库。分数口径：failed/needs_review 不计入有效判题。",
    }
    (out / "full_grading_preflight.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with (out / "full_grading_anomalies.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["answer_id", "batch_id", "question_id", "student_code", "question_version_id", "status", "category", "issue_code", "severity", "message"])
        for row in anomalies:
            for issue in row["issues"]:
                writer.writerow([row["answer_id"], row["batch_id"], row["question_id"], row["student_code"], row["question_version_id"], row["status"], row["category"], issue["code"], issue["severity"], issue["message"]])
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
