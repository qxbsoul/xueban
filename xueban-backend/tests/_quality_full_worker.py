import json
from xueban.api import xueban_teacher as t
from xueban.services import xueban_store as db

t.init()
now = "2026-01-01T00:00:00"
db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", ("batch_qf", "qf.csv", "committed", "{}", "[]", "confirmed", now))
db.x("INSERT INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", ("class_qf", "CQF", "Q班", 7, now))
db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", ("student_qf", "user_qf", "SQF", "class_qf", "SQF", 7, now))
db.x("INSERT INTO teacher_questions (id,topic_id,standard_id,course_id,material_type,question_type,stem,options,reference_answer,reference_solution,max_score,scoring_points,indicator_id,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("Q_BAD", "", "", "", "", "解答题", "题干", "", "", "", 0, "", "", ""))
db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,submit_time,original_answer,solution_process,original_score,max_score,score_status,raw_json,import_batch_id,exam_node_id,grading_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("qf-answer", "student_qf", "Q_BAD", "e-qf", 1, now, "a", "p", 0, 0, "pending", "{}", "batch_qf", "N1", "pending"))
result = t.quality_gate("batch_qf", {"id": "teacher", "role": "teacher"})
print(json.dumps({"blocking": result["blocking"], "types": [item["type"] for item in result["issues"]]}, ensure_ascii=False))
