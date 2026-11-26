import json
from xueban.api import xueban_teacher as t
from xueban.services import xueban_store as db

t.init()
now = "2026-01-01T00:00:00"
db.x("INSERT INTO teacher_grade_jobs (id,status,batch_id,total,processed,completed,failed,needs_review,current_answer_id,filters_json,pause_requested,error,created_at,updated_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("job_pause_test", "queued", "batch_pause", 1, 0, 0, 0, 0, "", "{}", 1, "", now, now, ""))
db.x("INSERT INTO teacher_grade_job_items (id,job_id,answer_id,status,attempt_count,updated_at) VALUES (?,?,?,?,?,?)", ("item_pause_test", "job_pause_test", "answer_pause_test", "pending", 0, now))
t.run_job("job_pause_test")
print(json.dumps({"job_status": db.q1("SELECT status FROM teacher_grade_jobs WHERE id='job_pause_test'")["status"], "item_status": db.q1("SELECT status FROM teacher_grade_job_items WHERE id='item_pause_test'")["status"], "attempts": db.q1("SELECT attempt_count FROM teacher_grade_job_items WHERE id='item_pause_test'")["attempt_count"]}, ensure_ascii=False))
