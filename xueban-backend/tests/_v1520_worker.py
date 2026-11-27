"""v1.5.20：预处理下钻、应作答口径、证据分页/错误分类、确定性缓存。"""
import json
import os

os.environ["XUEBAN_MIN_EVIDENCE"] = "1"
os.environ["XUEBAN_MIN_QUESTIONS"] = "1"

from xueban.api import xueban_teacher as t
from xueban.services import analysis_pipeline, learning_analysis as la
from xueban.services import xueban_store as db

t.init()
la.ensure_model()
NOW = "2026-06-01T00:00:00"
B = "batch_v1520"
db.x("DELETE FROM teacher_answers WHERE import_batch_id=?", (B,))
db.x("DELETE FROM teacher_data_issues WHERE task_id=?", (B,))
db.x("DELETE FROM teacher_batches WHERE id=?", (B,))
db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", (B, "v1520.csv", "committed", "{}", "[]", "preprocessing", NOW))
db.x("INSERT INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", ("class_v", "VC", "v1520班", 7, NOW))
db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", ("stu_v", "u_v", "V01", "class_v", "V01", 7, NOW))
db.x("INSERT INTO teacher_topics (id,code,title,created_at) VALUES (?,?,?,?)", ("topic_v", "TV", "主题V", NOW))
db.x("INSERT INTO teacher_indicators (id,code,topic_id,description) VALUES (?,?,?,?)", ("indicator_v", "IV", "topic_v", "知识点V"))
db.x("INSERT INTO teacher_exams (id,exam_date,description,available_questions) VALUES (?,?,?,?)", ("NV", "2026-06-01", "抽测", "Q101,Q102,Q103,Q104,Q105"))
QUESTIONS = (
    ("Q101", "选择题", 60, "\n".join(f"Q101P{i} 1分" for i in range(1, 61)), "A"),
    ("Q102", "解答题", 2, "Q102P1 2分", "2"),
    ("Q103", "", 1, "Q103P1 1分", "3"),
    ("Q104", "选择题", 1, "Q104P1 1分", ""),
    ("Q105", "解答题", 1, "Q105P1 1分\nQ105P2 1分", "5"),
)
for qid, qtype, max_score, points, answer in QUESTIONS:
    db.x("INSERT OR REPLACE INTO teacher_questions (id,stem,reference_answer,max_score,scoring_points,topic_id,indicator_id,question_type,material_type) VALUES (?,?,?,?,?,?,?,?,?)", (qid, f"{qid} 题干", "答案", max_score, points, "topic_v", "indicator_v", qtype, "题目"))
    db.x("INSERT OR REPLACE INTO teacher_question_versions (version_id,batch_id,question_id,stem,reference_answer,max_score,scoring_points,topic_id,indicator_id,question_type,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (f"qv:{B}:{qid}", B, qid, f"{qid} 题干", "答案", max_score, points, "topic_v", "indicator_v", qtype, NOW))
    for line in points.split("\n"):
        code = line.split()[0]
        db.x("INSERT OR REPLACE INTO scoring_points (id,code,batch_id,name,description,created_at) VALUES (?,?,?,?,?,?)", (f"sp_{code}", code, B, "点", "", NOW))
        db.x("INSERT OR REPLACE INTO scoring_point_knowledge_links (id,scoring_point_id,knowledge_point_id,weight,source,confidence,status,batch_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (f"link_{code}", f"sp_{code}", "kp:IV", 1.0, "teacher", 1.0, "confirmed", B, NOW, NOW))
    db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,grading_status,import_batch_id,max_score,original_answer,solution_process,question_version_id,exam_node_id,score_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"a_{qid}", "stu_v", qid, f"E-{qid}", 1, "pending", B, max_score, answer, "步骤" if qid == "Q105" else "", f"qv:{B}:{qid}", "NV", "graded"))

report = t.run_preprocessing(B)["report"]
anomaly = t.preprocess_records(B, state="anomaly", issue_status="all", page=1, page_size=20, user={"id": "teacher_v", "role": "teacher"})
pending = t.preprocess_records(B, state="pending_confirmation", issue_status="all", page=1, page_size=20, user={"id": "teacher_v", "role": "teacher"})
valid = t.preprocess_records(B, state="valid", issue_status="all", page=1, page_size=20, user={"id": "teacher_v", "role": "teacher"})
all_records = t.preprocess_records(B, state="all", issue_status="all", page=1, page_size=20, user={"id": "teacher_v", "role": "teacher"})
pending_issue_filter = t.preprocess_records(B, state="all", issue_status="pending_review", page=1, page_size=20, user={"id": "teacher_v", "role": "teacher"})

db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,status,needs_review,step_results_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("g_v", "a_Q101", "stu_v", "Q101", "E-Q101", 1, 1, "m", 60, 60, "graded", 0, "[]", NOW))
steps = []
for index in range(60):
    full = index % 2 == 0
    label = "" if full else ("合并同类项错误" if index % 4 == 1 else "计算错误")
    steps.append({"student_step": f"步骤{index}", "score": 1 if full else 0, "max_score": 1, "error_type": label, "confidence": 0.2 if index % 5 == 0 else 0.9, "scoring_point_links": [{"scoring_point_id": f"Q101P{index + 1}", "awarded_score": 1 if full else 0, "max_score": 1}]})
db.x("UPDATE teacher_grade_runs SET step_results_json=? WHERE id='g_v'", (json.dumps(steps, ensure_ascii=False),))
page1 = la.student_evidence_page(B, "stu_v", page=1, page_size=20)
page3 = la.student_evidence_page(B, "stu_v", page=3, page_size=20)
filtered = la.student_evidence_page(B, "stu_v", page=1, page_size=20, error_type="正确")
labels = [item["error_type"] for item in page1["items"]]

cache1 = analysis_pipeline.get_deterministic(B, "batch")
tasks1 = db.q("SELECT id FROM teacher_analysis_tasks WHERE batch_id=? AND scope='batch' AND kind='deterministic'", (B,))
cache2 = analysis_pipeline.get_deterministic(B, "batch")
tasks2 = db.q("SELECT id FROM teacher_analysis_tasks WHERE batch_id=? AND scope='batch' AND kind='deterministic'", (B,))

print(json.dumps({
    "report": {key: report[key] for key in ("raw_records", "valid_records", "anomaly_records", "pending_records", "state_sum", "reconciled", "process_reminders", "issue_records", "issue_records_distinct", "pending_issues")},
    "anomaly": {"total": anomaly["total"], "records": [{"q": r["question_id"], "issue_count": r["issue_count"], "types": sorted(i["issue_type"] for i in r["issues"]), "statuses": sorted({i["handling_status"] for i in r["issues"]})} for r in anomaly["records"]]},
    "pending": {"total": pending["total"], "questions": [r["question_id"] for r in pending["records"]], "reasons": [r["flags"] for r in pending["records"]]},
    "valid": {"total": valid["total"], "questions": [r["question_id"] for r in valid["records"]]},
    "units": all_records["units"],
    "issue_filter_total": pending_issue_filter["total"],
    "evidence": {"total": page1["total"], "page1": len(page1["items"]), "page3": len(page3["items"]), "correct_total": filtered["total"], "labels": labels},
    "cache": {"same_tasks": [r["id"] for r in tasks1] == [r["id"] for r in tasks2], "task_count": len(tasks2), "has_classes": bool(cache1 and cache1.get("classes"))},
}, ensure_ascii=False, default=str))
