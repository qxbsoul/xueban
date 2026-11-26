import json
from fastapi import HTTPException
from xueban.api import xueban_teacher as t
from xueban.services import xueban_store as db

class FakeBackground:
    def add_task(self, *args, **kwargs):
        return None

t.init()
now = "2026-01-01T00:00:00"
db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", ("batch_job", "job.csv", "committed", "{}", "[]", "confirmed", now))
db.x("INSERT INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", ("class_job", "CJ", "J班", 7, now))
db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", ("student_job", "user_job", "SJ", "class_job", "SJ", 7, now))
db.x("INSERT INTO teacher_questions (id,topic_id,standard_id,course_id,material_type,question_type,stem,options,reference_answer,reference_solution,max_score,scoring_points,indicator_id,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("Q1", "", "", "", "", "解答题", "题干", "", "答案", "解析", 2, "Q001P1 2分", "", ""))
db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,submit_time,original_answer,solution_process,original_score,max_score,score_status,raw_json,import_batch_id,exam_node_id,grading_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("answer_job", "student_job", "Q1", "e1", 1, now, "a", "p", 0, 2, "pending", "{}", "batch_job", "N1", "pending"))
db.x("INSERT INTO teacher_grade_jobs (id,status,batch_id,total,processed,completed,failed,needs_review,current_answer_id,filters_json,error,created_at,updated_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("job_same", "partially_failed", "batch_job", 1, 1, 0, 1, 0, "", "{}", "", now, now, now))
db.x("INSERT INTO teacher_grade_job_items (id,job_id,answer_id,status,last_error,updated_at) VALUES (?,?,?,?,?,?)", ("item_same", "job_same", "answer_job", "failed", "timeout", now))
db.x("INSERT INTO teacher_grade_jobs (id,status,batch_id,total,processed,completed,failed,needs_review,current_answer_id,filters_json,error,created_at,updated_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("job_pause", "running", "batch_job", 1, 0, 0, 0, 0, "", "{}", "", now, now, ""))
t.retry_job("job_same", FakeBackground(), {"id": "teacher", "role": "teacher"}, {})
t.pause_job("job_pause", {"id": "teacher", "role": "teacher"})
duplicate_error = 0
try:
    t.batch(FakeBackground(), {"id": "teacher", "role": "teacher"}, {"batch_id": "batch_job"})
except HTTPException as exc:
    duplicate_error = exc.status_code
print(json.dumps({"retry_job_id": db.q1("SELECT id FROM teacher_grade_jobs WHERE id='job_same'")["id"], "failed_status": db.q1("SELECT status FROM teacher_grade_job_items WHERE id='item_same'")["status"], "paused_status": db.q1("SELECT status FROM teacher_grade_jobs WHERE id='job_pause'")["status"], "duplicate_error": duplicate_error}, ensure_ascii=False))
