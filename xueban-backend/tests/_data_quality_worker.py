"""数据质量主状态互斥与总数对账。"""
import json

from xueban.api import xueban_teacher as t
from xueban.services import xueban_store as db

t.init()
BT = "batch_q"; now = "2026-01-01T00:00:00"
db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", (BT, "q.csv", "committed", "{}", "[]", "confirmed", now))
db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", ("stu_q", "user_q", "S01", "class_q", "S01", 7, now))
for index, qid in enumerate(("Q001", "Q002", "Q003", "Q004")):
    db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,grading_status,import_batch_id,max_score,original_score) VALUES (?,?,?,?,?,?,?,?,?)", (f"a{index}", "stu_q", qid, f"E{index}", 1, "graded", BT, 2, 1))
db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,status,needs_review,step_results_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("g0", "a0", "stu_q", "Q001", "E0", 1, 1, "m", 1, 2, "graded", 0, "[]", now))
db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,status,needs_review,step_results_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("g1", "a1", "stu_q", "Q002", "E1", 1, 1, "m", 0, 2, "failed", 0, "[]", now))
db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,status,needs_review,step_results_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("g2", "a2", "stu_q", "Q003", "E2", 1, 1, "m", 1, 2, "needs_review", 1, "[]", now))
db.x("INSERT INTO teacher_data_issues (id,task_id,issue_type,severity,student_id,student_code,question_id,event_id,revision,reason,handling_status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", ("i1", BT, "score_out_of_range", "major", "stu_q", "S01", "Q002", "E1", 1, "越界", "pending_review", now))
db.x("INSERT INTO teacher_data_issues (id,task_id,issue_type,severity,student_id,student_code,question_id,event_id,revision,reason,handling_status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", ("i2", BT, "missing_process", "minor", "stu_q", "S01", "Q003", "E2", 1, "缺过程", "pending_review", now))
report = t.batch_data_quality(BT)
print(json.dumps(report, ensure_ascii=False))
