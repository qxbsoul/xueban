"""导出历史 pending issue 审核清单（只读，不删除、不改 accepted）。"""
import argparse
import csv
import json
import sqlite3
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--batch", default="batch_305316cdd0")
    parser.add_argument("--out", default=r"D:\capital lion\verify-artifacts\v1.5.12")
    args = parser.parse_args()
    db_path = Path(args.db).resolve()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT i.id issue_id, i.issue_type, i.category, i.severity, i.handling_status,
               i.student_code, i.class_code, i.question_id, i.event_id, i.revision,
               i.raw_value, i.expected_value, i.reason, i.detection_rule, i.created_at,
               a.original_answer, a.solution_process, a.max_score, a.original_score,
               COALESCE(v.stem, q.stem) stem, COALESCE(v.scoring_points, q.scoring_points) scoring_points
        FROM teacher_data_issues i
        LEFT JOIN teacher_answers a ON a.import_batch_id=i.task_id AND a.event_id=i.event_id AND a.revision=i.revision AND a.question_id=i.question_id
        LEFT JOIN teacher_question_versions v ON v.version_id=a.question_version_id
        LEFT JOIN teacher_questions q ON q.id=i.question_id
        WHERE i.task_id=? AND i.handling_status='pending_review'
        ORDER BY i.issue_type, i.student_code
    """, (args.batch,)).fetchall()
    path = out / "pending_issues_review.csv"
    headers = ["issue_id", "issue_type", "category", "severity", "handling_status", "student_code", "class_code", "question_id", "event_id", "revision",
               "raw_value", "expected_value", "reason", "detection_rule", "created_at",
               "original_answer", "solution_process", "max_score", "original_score", "stem", "scoring_points",
               "auto_fixable", "needs_teacher", "suggested_action", "impact", "teacher_decision", "reviewer", "reviewed_at"]
    suggestions = {
        "missing_final_answer": ("否", "是", "请学生补交最终答案，或按缺失作答标记；不能按 0 分直接判"),
        "score_out_of_range": ("是（需回填写入前确认）", "是", "核对原始分与满分，修正原始分或满分后重算"),
        "scoring_points_sum_mismatch": ("否", "是", "核对评分点分值与满分，必要时重判"),
        "missing_process": ("否", "否", "缺失解题过程属于证据不足；保留记录，不按 0 分"),
    }
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        for row in rows:
            auto, teacher, action = suggestions.get(row["issue_type"], ("待评估", "是", "逐条核对原始数据"))
            writer.writerow([row[key] if row[key] is not None else "" for key in headers[:21]] + [auto, teacher, action, "影响该作答的判题与学情证据", "", "", ""])
    counts = {}
    for row in rows:
        counts[row["issue_type"]] = counts.get(row["issue_type"], 0) + 1
    summary = {"batch_id": args.batch, "pending_total": len(rows), "by_type": counts, "checklist_csv": str(path),
               "note": "只读清单；不得为通过闸门把 pending 强改为 accepted，也不得删除问题记录。"}
    (out / "pending_issues_review.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
