"""学情证据链只读审计：逐级统计原始作答→判题→步骤→评分点→映射→学生证据。"""
import argparse
import json
import re
import sqlite3
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--batch", default="batch_305316cdd0")
    parser.add_argument("--out", default=r"D:\capital lion\verify-artifacts\v1.5.15")
    args = parser.parse_args()
    path = Path(args.db).resolve()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    answers = [dict(row) for row in con.execute("SELECT * FROM teacher_answers WHERE import_batch_id=?", (args.batch,))]
    latest = {}
    for row in con.execute("""
        SELECT g.* FROM teacher_grade_runs g JOIN teacher_answers a ON a.id=g.answer_id
        WHERE a.import_batch_id=? AND g.id=(SELECT g2.id FROM teacher_grade_runs g2 WHERE g2.answer_id=g.answer_id ORDER BY g2.grading_version DESC, g2.created_at DESC LIMIT 1)
    """, (args.batch,)):
        latest[row["answer_id"]] = dict(row)
    versions = {row["version_id"]: dict(row) for row in con.execute("SELECT * FROM teacher_question_versions WHERE batch_id=?", (args.batch,))}
    points = [dict(row) for row in con.execute("SELECT * FROM scoring_points WHERE batch_id=?", (args.batch,))]
    links = [dict(row) for row in con.execute("""
        SELECT l.* FROM scoring_point_knowledge_links l JOIN scoring_points sp ON sp.id=l.scoring_point_id
        WHERE sp.batch_id=? AND l.status != 'disabled'
    """, (args.batch,))]
    mappings = {}
    for row in links:
        mappings.setdefault(row["scoring_point_id"], []).append(row)
    points_by_code = {row["code"]: row["id"] for row in points}
    status = {}
    for row in answers:
        status[row["grading_status"]] = status.get(row["grading_status"], 0) + 1
    grade_status = {}
    step_empty = 0
    raw_has_reasoning = 0
    step_links = 0
    mapped_steps = 0
    student_points = set()
    details = []
    specs_cache = {}
    for answer in answers:
        grade = latest.get(answer["id"])
        if not grade:
            continue
        grade_status[grade["status"]] = grade_status.get(grade["status"], 0) + 1
        try:
            steps = json.loads(grade.get("step_results_json") or "[]")
            raw = json.loads(grade.get("raw_response") or "{}")
        except Exception:
            steps, raw = [], {}
        if not steps:
            step_empty += 1
            if raw.get("reasoning_steps"):
                raw_has_reasoning += 1
                steps = raw["reasoning_steps"]
        version = versions.get(answer.get("question_version_id")) or {}
        specs = specs_cache.setdefault(version.get("version_id"), {m.group(1): float(m.group(2)) for m in re.finditer(r"([A-Z]\d{3}P\d+)[^\n]*?(?<![0-9.\-])([0-9]+(?:\.[0-9]+)?)\s*分", str(version.get("scoring_points") or ""))})
        for step in steps if isinstance(steps, list) else []:
            for link in step.get("scoring_point_links") or []:
                if isinstance(link, str):
                    link = {"scoring_point_id": link}
                if not isinstance(link, dict):
                    continue
                sp = str(link.get("scoring_point_id") or "")
                if sp in specs:
                    step_links += 1
                    sp_id = points_by_code.get(sp)
                    if sp_id and mappings.get(sp_id):
                        mapped_steps += 1
                        student_points.add((answer["student_id"], mappings[sp_id][0]["knowledge_point_id"]))
    report = {
        "database": str(path),
        "batch_id": args.batch,
        "answers_total": len(answers),
        "answers_status": status,
        "latest_grades": len(latest),
        "latest_grade_status": grade_status,
        "grades_with_empty_step_results": step_empty,
        "empty_steps_recoverable_from_raw_reasoning_steps": raw_has_reasoning,
        "valid_scoring_point_links_in_steps": step_links,
        "links_with_knowledge_mapping": mapped_steps,
        "mapped_points": len({sp for sp, rows in mappings.items() if rows}),
        "scoring_points": len(points),
        "mapping_links": len(links),
        "confirmed_links": sum(1 for row in links if row.get("status") == "confirmed"),
        "legacy_links": sum(1 for row in links if row.get("status") == "legacy"),
        "students_with_mapped_evidence": len({sid for sid, _ in student_points}),
        "exclusion_reason": "needs_review/failed 由 grade_usage 排除；空 step_results 使已判结果无法产生评分点证据",
    }
    (out / "evidence_chain_audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
