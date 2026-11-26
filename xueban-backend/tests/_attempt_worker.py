import json
import sys
from xueban.api import xueban_teacher as t
from xueban.services import xueban_store as db

t.init()
now = "2026-01-01T00:00:00"
db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", ("batch_attempt", "a.csv", "committed", "{}", "[]", "confirmed", now))
db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,submit_time,original_answer,solution_process,original_score,max_score,score_status,raw_json,import_batch_id,exam_node_id,grading_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("attempt-answer", "student_attempt", "Q1", "e1", 1, now, "a", "p", 0, 1, "pending", "{}", "batch_attempt", "N1", "pending"))
answer = db.q1("SELECT * FROM teacher_answers WHERE id='attempt-answer'")
stage = "json_parse" if len(sys.argv) > 1 else "model_response"
t.record_attempt("job_attempt", answer, 1, stage, "timeout", "timeout", start=now)
attempt = db.q1("SELECT * FROM teacher_grade_attempts WHERE task_id='job_attempt'")
print(json.dumps({"batch_id": attempt["batch_id"], "student_id": attempt["student_id"], "retryable": attempt["retryable"], "duration_ms": attempt["duration_ms"]}, ensure_ascii=False))
