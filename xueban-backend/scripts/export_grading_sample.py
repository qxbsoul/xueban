"""导出真实判题小样本与人工评价表模板（只读，不调用任何模型）。

用法：
  python scripts/export_grading_sample.py --db ../.xueban/xueban.sqlite --batch batch_xxx --out ../verify-artifacts/v1.5.11
"""
import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path

CATEGORIES = [
    ("fully_correct", "g.is_correct='correct' OR (g.score = a.max_score AND a.max_score > 0)"),
    ("partial", "g.is_correct='partial' OR (g.score > 0 AND g.score < a.max_score)"),
    ("incorrect", "g.is_correct='incorrect' OR (g.score = 0 AND a.max_score > 0)"),
    ("needs_review", "g.needs_review=1"),
    ("missing_process", "COALESCE(a.solution_process,'')=''"),
    ("pending", "a.grading_status='pending'"),
    ("multi_operation_line", "(instr(COALESCE(a.solution_process,''),'；') > 0 OR instr(COALESCE(a.solution_process,''),';') > 0)"),
]


def rows_for(con, batch_id, condition, limit, has_mapping):
    mapping_expr = "g.mapping_version" if has_mapping else "NULL AS mapping_version"
    sql = f"""
        SELECT a.id answer_id, a.student_id, s.code student_code, a.question_id, a.event_id, a.revision,
               a.original_answer, a.solution_process, a.original_score, a.max_score, a.grading_status,
               COALESCE(v.stem, q.stem) stem, COALESCE(v.question_type, q.question_type) question_type,
               COALESCE(v.scoring_points, q.scoring_points) scoring_points,
               COALESCE(v.reference_answer, q.reference_answer) reference_answer,
               COALESCE(v.reference_solution, q.reference_solution) reference_solution,
               COALESCE(v.topic_id, q.topic_id) topic_id, COALESCE(v.indicator_id, q.indicator_id) indicator_id,
               b.filename batch_filename, e.exam_date, a.import_batch_id batch_id,
               g.id grade_id, g.score, g.is_correct, g.needs_review, g.step_results_json,
               g.grading_model, g.grading_prompt_version, {mapping_expr}
        FROM teacher_answers a
        JOIN teacher_students s ON s.id=a.student_id
        JOIN teacher_batches b ON b.id=a.import_batch_id
        LEFT JOIN teacher_exams e ON e.id=a.exam_node_id
        LEFT JOIN teacher_question_versions v ON v.version_id=a.question_version_id
        LEFT JOIN teacher_questions q ON q.id=a.question_id
        LEFT JOIN teacher_grade_runs g ON g.id = (
            SELECT g2.id FROM teacher_grade_runs g2 WHERE g2.answer_id=a.id
            ORDER BY g2.grading_version DESC, g2.created_at DESC LIMIT 1
        )
        WHERE a.import_batch_id=? AND ({condition})
        ORDER BY RANDOM() LIMIT ?
    """
    return con.execute(sql, (batch_id, limit)).fetchall()


def evaluable_reason(row) -> tuple:
    if not str(row["stem"] or "").strip():
        return False, "缺少题面"
    if not str(row["reference_answer"] or "").strip() and not str(row["scoring_points"] or "").strip():
        return False, "缺少参考答案与评分点"
    try:
        if float(row["max_score"] or 0) <= 0:
            return False, "缺少满分"
    except (TypeError, ValueError):
        return False, "满分非法"
    if not str(row["original_answer"] or "").strip() and not str(row["solution_process"] or "").strip():
        return False, "缺少学生作答"
    return True, ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--batch", default="batch_305316cdd0")
    parser.add_argument("--out", default=r"D:\capital lion\verify-artifacts\v1.5.11")
    parser.add_argument("--per-category", type=int, default=4)
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        sys.exit(f"数据库不存在：{db_path}")
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    seen = set()
    sample = []
    coverage = {}
    has_mapping = any(row[1] == "mapping_version" for row in con.execute("PRAGMA table_info(teacher_grade_runs)"))
    for name, condition in CATEGORIES:
        rows = rows_for(con, args.batch, condition, args.per_category, has_mapping)
        coverage[name] = len(rows)
        for row in rows:
            if row["answer_id"] in seen:
                continue
            seen.add(row["answer_id"])
            sample.append(row)
    sample_path = out / "real_grading_sample.csv"
    template_path = out / "manual_review_template.csv"

    headers = ["answer_id", "batch_id", "batch_filename", "exam_date", "student_code", "question_id", "event_id", "revision", "stem", "question_type", "scoring_points",
               "reference_answer", "reference_solution", "topic_id", "indicator_id", "original_answer", "solution_process", "max_score", "original_score", "grading_status",
               "grade_id", "score", "is_correct", "needs_review", "grading_model", "grading_prompt_version", "mapping_version", "step_results_json", "evaluable", "unevaluable_reason"]
    with sample_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        for row in sample:
            evaluable, reason = evaluable_reason(row)
            writer.writerow([row[key] if row[key] is not None else "" for key in headers[:-2]] + [evaluable, reason])

    template_headers = ["answer_id", "batch_id", "student_code", "question_id", "stem", "scoring_points", "reference_answer", "topic_id", "indicator_id",
                        "ai_score", "manual_score", "manual_scoring_points", "scoring_point_hit", "evidence_accuracy",
                        "manual_error_type", "manual_reason", "should_needs_review", "reviewer", "reviewed_at", "notes"]
    with template_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(template_headers)
        for row in sample:
            writer.writerow([row["answer_id"], row["batch_id"], row["student_code"], row["question_id"], row["stem"], row["scoring_points"], row["reference_answer"], row["topic_id"], row["indicator_id"],
                             row["score"] if row["score"] is not None else "", "", "", "", "", "", "", "", "", "", ""])

    duplicates = len(sample) - len({row["answer_id"] for row in sample})
    missing_reference = sum(1 for row in sample if not str(row["reference_answer"] or "").strip())
    missing_stem = sum(1 for row in sample if not str(row["stem"] or "").strip())
    unevaluable = [row["answer_id"] for row in sample if not evaluable_reason(row)[0]]
    config = {
        "api_key_configured": bool(__import__("os").environ.get("DEEPSEEK_API_KEY", "").strip()),
        "batch_id": args.batch,
        "sample_size": len(sample),
        "category_coverage": coverage,
        "sample_csv": str(sample_path),
        "manual_template_csv": str(template_path),
        "quality": {"duplicates": duplicates, "missing_reference_answer": missing_reference, "missing_stem": missing_stem, "unevaluable": unevaluable},
        "note": "本脚本只读数据库，不调用模型；人工评分后请用该模板计算一致率，不要用模型结果反填人工分数。evaluable=false 的记录只用于说明证据不足，不纳入一致率分母。",
    }
    (out / "real_grading_sample.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(config, ensure_ascii=False))


if __name__ == "__main__":
    main()
