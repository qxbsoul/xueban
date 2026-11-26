import json
from xueban.api import xueban_teacher as t
from xueban.services import xueban_store as db

t.init()
now = "2026-01-01T00:00:00"
bid = "batch_delete"
db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", (bid, "delete.csv", "committed", "{}", "[]", "confirmed", now))
db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,submit_time,original_answer,solution_process,original_score,max_score,score_status,raw_json,import_batch_id,exam_node_id,question_version_id,grading_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("del-answer", "s1", "Q1", "e1", 1, now, "a", "p", 0, 1, "graded", "{}", bid, "N1", "qv", "graded"))
db.x("INSERT INTO teacher_grade_jobs (id,status,batch_id,total,processed,completed,failed,needs_review,current_answer_id,filters_json,error,created_at,updated_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("del-job", "completed", bid, 1, 1, 1, 0, 0, "", "{}", "", now, now, now))
db.x("INSERT INTO teacher_grade_job_items (id,job_id,answer_id,status,updated_at) VALUES (?,?,?,?,?)", ("del-item", "del-job", "del-answer", "completed", now))
db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,score,max_score,status,needs_review,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", ("del-grade", "del-answer", "s1", "Q1", "e1", 1, 1, 1, 1, "graded", 0, now))
db.x("INSERT INTO teacher_corrections (id,grade_run_id,before_json,after_json,note,teacher_id,created_at) VALUES (?,?,?,?,?,?,?)", ("del-correction", "del-grade", "{}", "{}", "note", "teacher", now))
db.x("INSERT INTO teacher_grade_attempts (id,task_id,answer_id,batch_id) VALUES (?,?,?,?)", ("del-attempt", bid, "del-answer", bid))
db.x("INSERT INTO teacher_scoring_point_confirmations (id,grade_run_id,answer_id,scoring_point_id,created_at) VALUES (?,?,?,?,?)", ("del-confirm", "del-grade", "del-answer", "Q1P1", now))
db.x("INSERT INTO teacher_analysis_runs (id,task_id,scope,scope_id,created_at) VALUES (?,?,?,?,?)", ("del-analysis", bid, "class", "c1", now))
db.x("INSERT INTO teacher_import_files (id,batch_id,filename,rows,fields_json,errors_json) VALUES (?,?,?,?,?,?)", ("del-file", bid, "delete.csv", 1, "[]", "[]"))
result = t.delete_batch(bid, {"id": "teacher", "role": "teacher"})
print(json.dumps({"deleted": result.get("deleted"), "answers": db.q1("SELECT COUNT(*) n FROM teacher_answers WHERE import_batch_id=?", (bid,))["n"], "jobs": db.q1("SELECT COUNT(*) n FROM teacher_grade_jobs WHERE batch_id=?", (bid,))["n"], "items": db.q1("SELECT COUNT(*) n FROM teacher_grade_job_items WHERE job_id='del-job'")["n"], "grades": db.q1("SELECT COUNT(*) n FROM teacher_grade_runs WHERE id='del-grade'")["n"], "attempts": db.q1("SELECT COUNT(*) n FROM teacher_grade_attempts WHERE task_id=?", (bid,))["n"], "confirmations": db.q1("SELECT COUNT(*) n FROM teacher_scoring_point_confirmations WHERE answer_id='del-answer'")["n"], "analysis": db.q1("SELECT COUNT(*) n FROM teacher_analysis_runs WHERE task_id=?", (bid,))["n"], "batches": db.q1("SELECT COUNT(*) n FROM teacher_batches WHERE id=?", (bid,))["n"]}))
