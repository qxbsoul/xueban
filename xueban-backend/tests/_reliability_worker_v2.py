"""判题可靠性：重试、分类、幂等、原子领取、租约回收、暂停。"""
import json
import threading

import http.client

from xueban.api import xueban_teacher as t
from xueban.services import xueban_store as db

t.init()
NOW = "2026-01-01T00:00:00"
BATCH = "batch_r"
db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", (BATCH, "r.csv", "committed", "{}", "[]", "confirmed", NOW))
db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", ("stu_r", "user_r", "R01", "class_r", "R01", 7, NOW))
db.x("INSERT INTO teacher_questions (id,stem,reference_answer,max_score,scoring_points,question_type) VALUES (?,?,?,?,?,?)", ("Q001", "1+1=?", "2", 2, "Q001P1 2分", "解答题"))
db.x("INSERT INTO teacher_question_versions (version_id,batch_id,question_id,stem,reference_answer,max_score,scoring_points,question_type,created_at) VALUES (?,?,?,?,?,?,?,?,?)", (f"qv:{BATCH}:Q001", BATCH, "Q001", "1+1=?", "2", 2, "Q001P1 2分", "解答题", NOW))
for index in range(4):
    db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,grading_status,import_batch_id,max_score,original_score,solution_process,original_answer,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (f"ans_{index}", "stu_r", "Q001", f"E{index}", 1, "pending", BATCH, 2, 0, "1+1=2", "2", f"qv:{BATCH}:Q001"))

VALID = {
    "score": 2, "max_score": 2, "is_correct": "correct",
    "step_results": [{"step_index": 1, "source_step_id": "src-1", "student_step": "1+1=2", "is_correct": True, "score": 2, "max_score": 2, "error_type": "", "judgment_reason": "正确", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 2, "max_score": 2}],
                       "operation_units": [{"operation_id": "op-1", "source_step_id": "src-1", "student_fragment": "1+1=2", "is_correct": True, "score": 2, "max_score": 2, "error_type": "", "judgment_reason": "正确", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 2, "max_score": 2}]}]}],
}
INVALID = {
    "score": 2, "max_score": 2, "is_correct": "correct",
    "step_results": [{"step_index": 1, "source_step_id": "src-1", "student_step": "1+1=2", "is_correct": True, "score": 2, "max_score": 2, "error_type": "", "judgment_reason": "正确", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 9, "max_score": 2}]}],
}

calls = {"n": 0}


def flaky(answer, question, steps, key, provider, standard, context):
    calls["n"] += 1
    if calls["n"] == 1:
        raise http.client.IncompleteRead(b"partial", 10)
    return dict(VALID)


def invalid(answer, question, steps, key, provider, standard, context):
    calls["n"] += 1
    return dict(INVALID)


t._LLM_CALLER = flaky
retried = t.grade_answer("ans_0", key="test", job_id="")
retry_calls = calls["n"]
attempts_after_retry = db.q("SELECT failure_stage, error_type, duration_ms FROM teacher_grade_attempts WHERE answer_id='ans_0' ORDER BY rowid")

# 非通信类错误不重试
calls["n"] = 0
t._LLM_CALLER = invalid
failed = t.grade_answer("ans_1", key="test", job_id="")
attempts_after_invalid = db.q1("SELECT COUNT(*) n FROM teacher_grade_attempts WHERE answer_id='ans_1'")["n"]

# 幂等：同一 job 已完成，不再产生新版本
t._LLM_CALLER = flaky
calls["n"] = 0
db.x("INSERT INTO teacher_grade_jobs (id,status,batch_id,total,processed,completed,failed,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)", ("job_r", "running", BATCH, 1, 1, 1, 0, NOW, NOW))
db.x("INSERT INTO teacher_grade_job_items (id,job_id,answer_id,status,grade_id,updated_at) VALUES (?,?,?,?,?,?)", ("item_r", "job_r", "ans_0", "completed", retried["grade"]["id"], NOW))
reused = t.grade_answer("ans_0", key="test", job_id="job_r")
versions = db.q1("SELECT COUNT(*) n FROM teacher_grade_runs WHERE answer_id='ans_0'")["n"]

# 原子领取：两个线程同时抢同一条
db.x("INSERT INTO teacher_grade_job_items (id,job_id,answer_id,status,updated_at) VALUES (?,?,?,?,?)", ("item_claim", "job_r", "ans_2", "pending", NOW))
claimed = {"wins": 0}
lock = threading.Lock()


def claim():
    with db._LOCK:
        cur = db.conn().execute("UPDATE teacher_grade_job_items SET status='running', started_at=? WHERE id='item_claim' AND status='pending'", (NOW,))
        db.conn().commit()
    with lock:
        claimed["wins"] += int(cur.rowcount)


threads = [threading.Thread(target=claim) for _ in range(4)]
[thread.start() for thread in threads]
[thread.join() for thread in threads]

# 租约回收：running 超过租约
db.x("UPDATE teacher_grade_job_items SET status='running', started_at=? WHERE id='item_claim'", ("2020-01-01T00:00:00",))
reclaimed_items = t.reclaim_stale_items("job_r")

# 任务心跳过期回收
db.x("INSERT INTO teacher_grade_jobs (id,status,batch_id,total,processed,completed,failed,heartbeat_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", ("job_stale", "running", BATCH, 1, 0, 0, 0, "2020-01-01T00:00:00", NOW, NOW))
db.x("INSERT INTO teacher_grade_job_items (id,job_id,answer_id,status,started_at,updated_at) VALUES (?,?,?,?,?,?)", ("item_stale", "job_stale", "ans_3", "running", "2020-01-01T00:00:00", NOW))
recovered = t.recover_stale_jobs()
stale_job = db.q1("SELECT status, pause_requested FROM teacher_grade_jobs WHERE id='job_stale'")
stale_item = db.q1("SELECT status FROM teacher_grade_job_items WHERE id='item_stale'")["status"]

# 暂停后不领取：pause_requested=1 时 run_job 直接落 paused
calls["n"] = 0
db.x("INSERT INTO teacher_grade_jobs (id,status,batch_id,total,processed,completed,failed,pause_requested,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", ("job_paused", "queued", BATCH, 1, 0, 0, 0, 1, NOW, NOW))
db.x("INSERT INTO teacher_grade_job_items (id,job_id,answer_id,status,updated_at) VALUES (?,?,?,?,?)", ("item_paused", "job_paused", "ans_3", "pending", NOW))
t.run_job("job_paused")
paused_status = db.q1("SELECT status FROM teacher_grade_jobs WHERE id='job_paused'")["status"]
paused_item = db.q1("SELECT status FROM teacher_grade_job_items WHERE id='item_paused'")["status"]

print(json.dumps({
    "retried": {"status": retried["grade"]["status"], "calls": retry_calls, "attempts": attempts_after_retry},
    "invalid": {"status": failed["grade"]["status"], "attempts": attempts_after_invalid, "error_type": failed["grade"]["error_type"]},
    "idempotent": {"reused": bool(reused.get("reused")), "versions": versions},
    "claim": claimed,
    "reclaim": {"items": reclaimed_items, "status": db.q1("SELECT status FROM teacher_grade_job_items WHERE id='item_claim'")["status"]},
    "recover": {"jobs": recovered["jobs"], "items": recovered["items"], "job": stale_job, "item": stale_item},
    "pause": {"job": paused_status, "item": paused_item},
}, ensure_ascii=False))
