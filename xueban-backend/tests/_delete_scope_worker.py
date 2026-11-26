"""D4 回归：任务删除必须覆盖有/无 job、人工修正、评分点确认、多次重判等全部场景，且不留孤儿。"""
import json

from fastapi import HTTPException

from xueban.api import xueban_teacher as t
from xueban.services import xueban_store as db

USER = {"id": "teacher", "role": "teacher"}
t.init()
NOW = "2026-01-01T00:00:00"


def make_batch(bid, *, with_job=False, with_grade=False, with_confirmation=False, with_correction=False, regrades=1, running=False, student="s1"):
    db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", (bid, f"{bid}.csv", "committed", "{}", "[]", "confirmed", NOW))
    db.x("INSERT INTO teacher_questions (id,stem,max_score,scoring_points) VALUES (?,?,?,?)", (f"Q-{bid}", "stem", 5, "Q001P1 5分"))
    db.x("INSERT INTO teacher_question_versions (version_id,batch_id,question_id,stem,max_score,scoring_points,created_at) VALUES (?,?,?,?,?,?,?)", (f"qv:{bid}:Q", bid, f"Q-{bid}", "stem", 5, "Q001P1 5分", NOW))
    answer_id = f"{bid}:E1-r1"
    db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,original_answer,solution_process,original_score,max_score,grading_status,import_batch_id,exam_node_id,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (answer_id, student, f"Q-{bid}", "E1", 1, "a", "p", 0, 5, "graded" if with_grade else "pending", bid, "N01", f"qv:{bid}:Q"))
    if with_job:
        status = "running" if running else "completed"
        db.x("INSERT INTO teacher_grade_jobs (id,status,batch_id,total,processed,completed,failed,needs_review,current_answer_id,filters_json,error,created_at,updated_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"job-{bid}", status, bid, 1, 1, 1, 0, 0, "", "{}", "", NOW, NOW, NOW))
    grade_ids = []
    for version in range(1, regrades + 1):
        if not with_grade:
            break
        grade_id = f"grade-{bid}-{version}"
        grade_ids.append(grade_id)
        db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,score,max_score,status,needs_review,step_results_json,created_at,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (grade_id, answer_id, student, f"Q-{bid}", "E1", 1, version, 3, 5, "graded", 0, "[]", NOW, f"qv:{bid}:Q"))
        if with_job and version == regrades:
            db.x("INSERT INTO teacher_grade_job_items (id,job_id,answer_id,status,grade_id,updated_at,attempt_count) VALUES (?,?,?,?,?,?,?)", (f"item-{bid}", f"job-{bid}", answer_id, "completed", grade_id, NOW, 1))
            db.x("INSERT INTO teacher_grade_attempts (id,task_id,answer_id,batch_id,failure_stage,start_time,end_time,retryable) VALUES (?,?,?,?,?,?,?,?)", (f"attempt-{bid}", f"job-{bid}", answer_id, bid, "", NOW, NOW, 0))
    if with_grade and not with_job:
        db.x("INSERT INTO teacher_grade_attempts (id,task_id,answer_id,batch_id,failure_stage,start_time,end_time,retryable) VALUES (?,?,?,?,?,?,?,?)", (f"attempt-{bid}", "", answer_id, bid, "", NOW, NOW, 0))
    if with_confirmation:
        db.x("INSERT INTO teacher_scoring_point_confirmations (id,grade_run_id,answer_id,scoring_point_id,confirmed_score,max_score,confirmation_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)", (f"confirm-{bid}", grade_ids[-1], answer_id, "Q001P1", 3, 5, "confirmed", NOW, NOW))
    if with_correction:
        db.x("INSERT INTO teacher_corrections (id,grade_run_id,before_json,after_json,note,teacher_id,created_at) VALUES (?,?,?,?,?,?,?)", (f"correction-{bid}", grade_ids[-1], "{}", "{}", "note", "teacher", NOW))
    db.x("INSERT INTO teacher_analysis_runs (id,task_id,scope,scope_id,algorithm_version,created_at) VALUES (?,?,?,?,?,?)", (f"analysis-{bid}", bid, "class", "c1", "sp-beta-v1", NOW))
    db.x("INSERT INTO teacher_import_files (id,batch_id,filename,rows,fields_json,errors_json) VALUES (?,?,?,?,?,?)", (f"file-{bid}", bid, "x.csv", 1, "[]", "[]"))
    return answer_id


scenarios = {
    "never_graded": dict(with_job=False, with_grade=False),
    "graded_no_job": dict(with_job=False, with_grade=True, with_confirmation=True, with_correction=True),
    "graded_with_job": dict(with_job=True, with_grade=True, with_confirmation=True, with_correction=True),
    "confirmation_only": dict(with_job=True, with_grade=True, with_confirmation=True),
    "regraded": dict(with_job=True, with_grade=True, regrades=3, with_correction=True),
}
for name, kwargs in scenarios.items():
    make_batch(f"batch_{name}", **kwargs)
# 保留任务：删除其他任务后必须完好
make_batch("batch_keep", with_job=True, with_grade=True, with_confirmation=True, with_correction=True, regrades=2)

deleted = {}
for name in scenarios:
    result = t.delete_batch(f"batch_{name}", USER)
    deleted[name] = result["residual"]

running_batch = "batch_running"
make_batch(running_batch, with_job=True, with_grade=False, running=True)
# init() 会把心跳过期的 running 任务自动置为 paused；这里模拟一个仍然活着的 worker。
db.x("UPDATE teacher_grade_jobs SET heartbeat_at=? WHERE batch_id=?", (t.now(), running_batch))
running_error = 0
try:
    t.delete_batch(running_batch, USER)
except HTTPException as exc:
    running_error = exc.status_code

keep = {
    "answers": db.q1("SELECT COUNT(*) n FROM teacher_answers WHERE import_batch_id='batch_keep'")["n"],
    "grades": db.q1("SELECT COUNT(*) n FROM teacher_grade_runs WHERE answer_id LIKE 'batch_keep:%'")["n"],
    "confirmations": db.q1("SELECT COUNT(*) n FROM teacher_scoring_point_confirmations WHERE answer_id LIKE 'batch_keep:%'")["n"],
    "corrections": db.q1("SELECT COUNT(*) n FROM teacher_corrections WHERE grade_run_id LIKE 'grade-batch_keep-%'")["n"],
    "jobs": db.q1("SELECT COUNT(*) n FROM teacher_grade_jobs WHERE batch_id='batch_keep'")["n"],
    "versions": db.q1("SELECT COUNT(*) n FROM teacher_question_versions WHERE batch_id='batch_keep'")["n"],
}
print(json.dumps({"deleted": deleted, "keep": keep, "running_error": running_error}, ensure_ascii=False))
