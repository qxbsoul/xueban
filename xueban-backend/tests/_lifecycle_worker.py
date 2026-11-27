"""v1.5.19 生命周期：统一计数、暂停恢复幂等、重判进度、预处理总数对账、质量闸门移除。"""
import json
import os

os.environ.setdefault("XUEBAN_MIN_EVIDENCE", "0.3")

from xueban.api import xueban_teacher as t
from xueban.services import learning_analysis as la
from xueban.services import xueban_store as db

t.init()
la.ensure_model()
NOW = "2026-01-01T00:00:00"
B = "batch_life"
db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", (B, "life.csv", "committed", "{}", "[]", "ready", NOW))
db.x("INSERT INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", ("class_l", "L1", "生命周期班", 7, NOW))
db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", ("stu_l", "u_l", "L01", "class_l", "L01", 7, NOW))
db.x("INSERT INTO teacher_topics (id,code,title,created_at) VALUES (?,?,?,?)", ("topic_l", "T01", "主题", NOW))
db.x("INSERT INTO teacher_indicators (id,code,topic_id,description) VALUES (?,?,?,?)", ("indicator_l", "I01", "topic_l", "知识点"))
QUESTIONS = (("Q001", "选择题", "Q001P1 1分", 1), ("Q002", "解答题", "Q002P1 2分", 2), ("Q003", "", "Q003P1 1分", 1), ("Q004", "解答题", "Q004P1 2分", 2), ("Q005", "选择题", "Q005P1 1分", 1))
for qid, qtype, points, max_score in QUESTIONS:
    db.x("INSERT INTO teacher_questions (id,stem,reference_answer,max_score,scoring_points,topic_id,indicator_id,question_type) VALUES (?,?,?,?,?,?,?,?)", (qid, f"{qid} 题干", "答案", max_score, points, "topic_l", "indicator_l", qtype))
    db.x("INSERT INTO teacher_question_versions (version_id,batch_id,question_id,stem,reference_answer,max_score,scoring_points,topic_id,indicator_id,question_type,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (f"qv:{B}:{qid}", B, qid, f"{qid} 题干", "答案", max_score, points, "topic_l", "indicator_l", qtype, NOW))
    db.x("INSERT INTO scoring_points (id,code,batch_id,name,description,created_at) VALUES (?,?,?,?,?,?)", (f"sp_{qid}", f"{qid}P1", B, "点", "", NOW))
    db.x("INSERT INTO scoring_point_knowledge_links (id,scoring_point_id,knowledge_point_id,weight,source,confidence,status,batch_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (f"link_{qid}", f"sp_{qid}", "kp:I01", 1.0, "teacher", 1.0, "confirmed", B, NOW, NOW))
for qid, answer, process in (("Q001", "A", ""), ("Q002", "2", ""), ("Q003", "5", ""), ("Q004", "2", "步骤"), ("Q005", "", "")):
    db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,grading_status,import_batch_id,max_score,original_answer,solution_process,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
         (f"ans_{qid}", "stu_l", qid, f"E-{qid}", 1, "pending", B, 1, answer, process, f"qv:{B}:{qid}"))

# 计数任务：1 成功 + 1 待复核 + 1 失败 + 1 pending + 1 running
db.x("INSERT INTO teacher_grade_jobs (id,status,batch_id,total,processed,completed,failed,needs_review,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", ("job_life", "completed", B, 5, 3, 1, 1, 1, NOW, NOW))
db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,status,needs_review,step_results_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("g_ok", "ans_Q001", "stu_l", "Q001", "E", 1, 1, "m", 1, 1, "graded", 0, "[]", NOW))
db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,status,needs_review,step_results_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("g_review", "ans_Q002", "stu_l", "Q002", "E", 1, 1, "m", 1, 2, "needs_review", 1, "[]", NOW))
db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,status,needs_review,step_results_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("g_fail", "ans_Q003", "stu_l", "Q003", "E", 1, 1, "m", 0, 1, "failed", 0, "[]", NOW))
for index, (item_id, answer_id, status, grade_id) in enumerate((("i1", "ans_Q001", "completed", "g_ok"), ("i2", "ans_Q002", "completed", "g_review"), ("i3", "ans_Q003", "failed", "g_fail"), ("i4", "ans_Q004", "pending", None), ("i5", "ans_Q005", "running", None))):
    db.x("INSERT INTO teacher_grade_job_items (id,job_id,answer_id,status,grade_id,attempt_count,updated_at) VALUES (?,?,?,?,?,?,?)", (item_id, "job_life", answer_id, status, grade_id or "", 1, NOW))

counts = t.job_counts("job_life", "completed")
job_payload = t.job("job_life", {"id": "teacher_x", "role": "teacher"})

# 暂停/继续幂等
db.x("INSERT INTO teacher_grade_jobs (id,status,batch_id,total,created_at,updated_at) VALUES (?,?,?,?,?,?)", ("job_p", "queued", B, 2, NOW, NOW))
db.x("INSERT INTO teacher_grade_job_items (id,job_id,answer_id,status,updated_at) VALUES (?,?,?,?,?)", ("pi1", "job_p", "ans_Q004", "pending", NOW))
db.x("INSERT INTO teacher_grade_job_items (id,job_id,answer_id,status,updated_at) VALUES (?,?,?,?,?)", ("pi2", "job_p", "ans_Q005", "pending", NOW))
pause1 = t.pause_job("job_p", {"id": "teacher_x", "role": "teacher"})
pause2 = t.pause_job("job_p", {"id": "teacher_x", "role": "teacher"})
resume1 = t.resume_job("job_p", type("BG", (), {"add_task": lambda self, fn, *a: None})(), {"id": "teacher_x", "role": "teacher"})
resume2 = t.resume_job("job_p", type("BG", (), {"add_task": lambda self, fn, *a: None})(), {"id": "teacher_x", "role": "teacher"})

# 重判进度
db.x("UPDATE teacher_grade_job_items SET status='failed', last_error='x' WHERE id='i3'")
retry1 = t.retry_job("job_life", type("BG", (), {"add_task": lambda self, fn, *a: None})(), {"id": "teacher_x", "role": "teacher"}, {})
retry2 = t.retry_job("job_life", type("BG", (), {"add_task": lambda self, fn, *a: None})(), {"id": "teacher_x", "role": "teacher"}, {})
item_retry = db.q1("SELECT status, retry_round FROM teacher_grade_job_items WHERE id='i3'")

# 预处理报告
pre = t.preprocessing(B, {"id": "teacher_x", "role": "teacher"})["report"]

print(json.dumps({
    "counts": counts,
    "job": {"progress": job_payload["progress"], "retry": job_payload["retry_progress"], "consistent": job_payload["counts_consistent"], "state": job_payload["state"]},
    "pause": {"p1": pause1["job"]["status"], "p2": pause2["job"]["status"]},
    "resume": {"r1": resume1["job"]["status"], "r2": resume2.get("reused"), "r2_status": resume2["job"]["status"]},
    "retry": {"r1": retry1["job"]["status"], "retry_total": retry1["job"]["retry_total"], "r2_reused": retry2.get("reused"), "item": dict(item_retry)},
    "preprocess": {"raw": pre["raw_records"], "valid": pre["valid_records"], "anomaly": pre["anomaly_records"], "pending": pre["pending_records"], "sum": pre["state_sum"], "reconciled": pre["reconciled"], "requirements": pre["process_requirements"], "reminders": pre["process_reminders"], "missing_process": pre["missing_process"]},
}, ensure_ascii=False, default=str))
