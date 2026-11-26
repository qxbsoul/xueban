"""导出评分点映射人工审核清单（只读，不自动确认）。"""
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
    link_columns = {row[1] for row in con.execute("PRAGMA table_info(scoring_point_knowledge_links)")}
    extra = {name: (f"l.{name}" if name in link_columns else "NULL") for name in ("confirmed_by", "confirmed_at", "version")}
    rows = con.execute(f"""
        SELECT b.id batch_id, v.question_id, sp.code scoring_point, sp.name scoring_point_name,
               l.knowledge_point_id, i.code kp_code, i.description kp_name,
               l.weight, l.source, l.status, {extra['confirmed_by']} AS confirmed_by, {extra['confirmed_at']} AS confirmed_at, {extra['version']} AS version
        FROM scoring_point_knowledge_links l
        JOIN scoring_points sp ON sp.id=l.scoring_point_id
        JOIN teacher_batches b ON b.id=sp.batch_id
        LEFT JOIN teacher_question_versions v ON v.version_id=sp.question_version_id
        LEFT JOIN teacher_indicators i ON l.knowledge_point_id='kp:' || i.code
        WHERE l.status != 'disabled' AND b.id=?
        ORDER BY v.question_id, sp.code, l.weight DESC
    """, (args.batch,)).fetchall()
    path = out / "mapping_review_checklist.csv"
    headers = ["batch_id", "question_id", "scoring_point", "scoring_point_name", "knowledge_point_id", "kp_code", "kp_name",
               "weight", "source", "status", "confirmed_by", "confirmed_at", "version",
               "teacher_decision", "reviewed_kp_code", "reviewed_weight", "decision_note", "reviewer", "reviewed_at"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        for row in rows:
            writer.writerow([row[key] if key in row.keys() and row[key] is not None else "" for key in headers[:-6]] + ["", "", "", "", "", ""])
    summary = {
        "batch_id": args.batch,
        "total": len(rows),
        "legacy": sum(1 for row in rows if row["status"] == "legacy"),
        "confirmed": sum(1 for row in rows if row["status"] == "confirmed"),
        "rejected": sum(1 for row in rows if row["status"] == "rejected"),
        "questions": len({row["question_id"] for row in rows}),
        "scoring_points": len({row["scoring_point"] for row in rows}),
        "checklist_csv": str(path),
        "note": "清单只读导出；teacher_decision 由教师人工填写，不能自动把 legacy 改成 confirmed。",
    }
    (out / "mapping_review_checklist.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
