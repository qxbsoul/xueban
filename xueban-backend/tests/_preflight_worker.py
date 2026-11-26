"""预检回归：学生维度关联、.env Key、迁移不完整、只读 SHA 不变。

使用独立最小 schema，刻意允许两个学生共享 event/revision/question，
用于验证预检不会跨学生误关联（正式表当前唯一约束不允许这种历史形态）。
"""
import csv
import hashlib
import json
import os
import sqlite3
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = Path(os.environ["XUEBAN_XUEBAN_DB"])
TMP = DB.parent
OUT = TMP / "preflight-out"
SCRIPT = TMP / "scripts" / "full_grading_preflight.py"
SCRIPT.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(ROOT / "scripts" / "full_grading_preflight.py", SCRIPT)
con = sqlite3.connect(DB)
con.executescript("""
CREATE TABLE teacher_batches (id TEXT PRIMARY KEY, filename TEXT, owner_id TEXT, created_at TEXT);
CREATE TABLE teacher_classes (id TEXT PRIMARY KEY, code TEXT);
CREATE TABLE teacher_students (id TEXT PRIMARY KEY, user_id TEXT, code TEXT, class_id TEXT, name TEXT, grade INTEGER);
CREATE TABLE teacher_questions (id TEXT PRIMARY KEY, stem TEXT, max_score REAL, scoring_points TEXT, reference_answer TEXT, reference_solution TEXT, question_type TEXT, topic_id TEXT, indicator_id TEXT, standard_id TEXT);
CREATE TABLE teacher_question_versions (version_id TEXT PRIMARY KEY, batch_id TEXT, question_id TEXT, stem TEXT, max_score REAL, scoring_points TEXT, reference_answer TEXT, reference_solution TEXT, question_type TEXT, topic_id TEXT, indicator_id TEXT);
CREATE TABLE teacher_answers (id TEXT PRIMARY KEY, student_id TEXT, question_id TEXT, event_id TEXT, revision INTEGER, original_answer TEXT, solution_process TEXT, original_score REAL, max_score REAL, grading_status TEXT, import_batch_id TEXT, exam_node_id TEXT, question_version_id TEXT, standard_id TEXT);
CREATE TABLE teacher_grade_runs (id TEXT PRIMARY KEY, answer_id TEXT, grading_version INTEGER, created_at TEXT, status TEXT, needs_review INTEGER, structure_json TEXT, textbook_context TEXT);
CREATE TABLE teacher_data_issues (id TEXT PRIMARY KEY, task_id TEXT, issue_type TEXT, severity TEXT, student_id TEXT, student_code TEXT, question_id TEXT, event_id TEXT, revision INTEGER, reason TEXT, handling_status TEXT);
CREATE TABLE teacher_grade_jobs (id TEXT PRIMARY KEY, batch_id TEXT, status TEXT, processed INTEGER, completed INTEGER, failed INTEGER, needs_review INTEGER, created_at TEXT);
CREATE TABLE teacher_grade_job_items (id TEXT PRIMARY KEY, job_id TEXT, status TEXT);
CREATE TABLE scoring_points (id TEXT PRIMARY KEY, code TEXT, batch_id TEXT);
CREATE TABLE scoring_point_knowledge_links (id TEXT PRIMARY KEY, scoring_point_id TEXT, knowledge_point_id TEXT, status TEXT);
CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT);
""")
now = "2026-01-01T00:00:00"
con.execute("INSERT INTO teacher_batches VALUES ('batch_p','p.csv',NULL,?)", (now,))
con.execute("INSERT INTO teacher_classes VALUES ('class_p','CP')")
con.execute("INSERT INTO teacher_students VALUES ('student_p1','u1','S01','class_p','S01',7)")
con.execute("INSERT INTO teacher_students VALUES ('student_p2','u2','S02','class_p','S02',7)")
con.execute("INSERT INTO teacher_questions VALUES ('Q001','题干',3,'Q001P1 3分','3','解析','解答题','T','I01','')")
con.execute("INSERT INTO teacher_question_versions VALUES ('qv:p:Q001','batch_p','Q001','题干',3,'Q001P1 3分','3','解析','解答题','T','I01')")
for sid in ("student_p1", "student_p2"):
    con.execute("INSERT INTO teacher_answers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"answer_{sid}", sid, "Q001", "E1", 1, "3", "步骤", 3, 3, "pending", "batch_p", "N01", "qv:p:Q001", ""))
con.execute("INSERT INTO teacher_data_issues VALUES ('issue_p1','batch_p','score_out_of_range','major','student_p1','S01','Q001','E1',1,'分数越界','pending_review')")
con.execute("INSERT INTO schema_migrations VALUES (1,?)", (now,))
con.execute("INSERT INTO schema_migrations VALUES (2,?)", (now,))
con.commit()
con.close()

before = hashlib.sha256(DB.read_bytes()).hexdigest()


def run(with_env_file):
    env = dict(os.environ)
    env.pop("DEEPSEEK_API_KEY", None)
    env["PYTHONPATH"] = str(ROOT)
    env["XUEBAN_HOME"] = str(TMP / "home")
    cwd = TMP / ("with-env" if with_env_file else "without-env")
    cwd.mkdir(parents=True, exist_ok=True)
    if with_env_file:
        (cwd / ".env").write_text("DEEPSEEK_API_KEY=dummy-not-used\n", encoding="utf-8")
    out = OUT / ("a" if with_env_file else "b")
    result = subprocess.run([sys.executable, str(SCRIPT), "--db", str(DB), "--batch", "batch_p", "--out", str(out)], cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise AssertionError(result.stderr + result.stdout)
    return json.loads((out / "full_grading_preflight.json").read_text(encoding="utf-8"))


with_env = run(True)
without_env = run(False)
after = hashlib.sha256(DB.read_bytes()).hexdigest()
anomalies = []
with (OUT / "a" / "full_grading_anomalies.csv").open(encoding="utf-8-sig", newline="") as handle:
    for row in csv.DictReader(handle):
        if row["issue_code"].startswith("pending_issue"):
            anomalies.append({"answer_id": row["answer_id"], "student": row["student_code"], "issue": row["issue_code"]})
print(json.dumps({
    "with_env": {"model": with_env["states"]["model"], "migration": with_env["migration"], "blocking": with_env["blocking_reasons"]},
    "without_env": {"model": without_env["states"]["model"]},
    "anomalies": anomalies,
    "sha_before": before,
    "sha_after": after,
    "read_only": before == after,
}, ensure_ascii=False))
