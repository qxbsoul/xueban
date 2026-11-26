import json
from xueban.api import xueban_teacher as t
from xueban.services import xueban_store as db

t.init()
now = "2026-01-01T00:00:00"
db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", ("batch_q", "q.csv", "committed", "{}", "[]", "confirmed", now))
db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,submit_time,original_answer,solution_process,original_score,max_score,score_status,raw_json,import_batch_id,exam_node_id,grading_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("q-answer", "missing-student", "missing-question", "q-event", 1, now, "a", "p", 0, 1, "graded", "{}", "batch_q", "N1", "pending"))
result = t.quality_gate("batch_q", {"id": "teacher", "role": "teacher"})
print(json.dumps({"blocking": result["blocking"], "types": [item["type"] for item in result["issues"]]}, ensure_ascii=False))
