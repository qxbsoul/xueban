"""v1.5.21 队列/并发/暂停/重启/幂等：真实 SQLite + 假模型 caller。"""
import json
import os
import threading
import time

os.environ["DEEPSEEK_API_KEY"] = "test-key"
os.environ["XUEBAN_GRADER_ATTEMPTS"] = "2"
os.environ["XUEBAN_GRADER_RETRY_BASE_MS"] = "1"

from xueban.api import xueban_teacher as t
from xueban.services import xueban_store as db

NOW = "2026-07-02T00:00:00"
B = "batch_queue1521"
t.init()
db.x("DELETE FROM teacher_answers WHERE import_batch_id=?", (B,))
db.x("DELETE FROM teacher_batches WHERE id=?", (B,))
db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", (B, "queue.csv", "committed", "{}", "[]", "ready", NOW))
db.x("INSERT INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", ("class_q", "QC", "队列班", 7, NOW))
db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", ("stu_q", "u_q", "Q01", "class_q", "Q01", 7, NOW))
db.x("INSERT INTO teacher_questions (id,stem,reference_answer,max_score,scoring_points,question_type) VALUES (?,?,?,?,?,?)", ("Q001", "1+1", "2", 2, "Q001P1 2分", "解答题"))
db.x("INSERT INTO teacher_question_versions (version_id,batch_id,question_id,stem,reference_answer,max_score,scoring_points,question_type,created_at) VALUES (?,?,?,?,?,?,?,?,?)", ("qv:Q001", B, "Q001", "1+1", "2", 2, "Q001P1 2分", "解答题", NOW))


def make_answers(count, prefix):
    ids = []
    for index in range(count):
        answer_id = f"ans_{prefix}_{index}"
        ids.append(answer_id)
        db.x("INSERT OR REPLACE INTO teacher_answers (id,student_id,question_id,event_id,revision,grading_status,import_batch_id,max_score,original_answer,solution_process,question_version_id,exam_node_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
             (answer_id, "stu_q", "Q001", f"E-{prefix}-{index}", 1, "pending", B, 2, "2", "1+1=2", "qv:Q001", "N01"))
    return ids


def make_job(job_id, answer_ids, concurrency=2):
    db.x("INSERT OR REPLACE INTO teacher_grade_jobs (id,status,batch_id,total,processed,completed,failed,needs_review,concurrency,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (job_id, "queued", B, len(answer_ids), 0, 0, 0, 0, concurrency, NOW, NOW))
    for index, answer_id in enumerate(answer_ids):
        db.x("INSERT OR REPLACE INTO teacher_grade_job_items (id,job_id,answer_id,status,attempt_count,updated_at) VALUES (?,?,?,?,?,?)", (f"item_{job_id}_{index}", job_id, answer_id, "pending", 0, NOW))


VALID = {"score": 2, "max_score": 2, "is_correct": "correct", "step_results": [{"step_index": 1, "source_step_id": "src-1", "student_step": "1+1=2", "is_correct": True, "score": 2, "max_score": 2, "error_type": "", "judgment_reason": "正确", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 2, "max_score": 2}], "operation_units": [{"operation_id": "op-1", "source_step_id": "src-1", "student_fragment": "1+1=2", "is_correct": True, "score": 2, "max_score": 2, "error_type": "", "judgment_reason": "正确", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 2, "max_score": 2}]}]}]}
counts = {}
active = {"n": 0, "max": 0}
lock = threading.Lock()


def caller(answer, question, steps, key, provider, standard, context):
    answer_id = str(answer.get("id"))
    with lock:
        counts[answer_id] = counts.get(answer_id, 0) + 1
        active["n"] += 1
        active["max"] = max(active["max"], active["n"])
    try:
        time.sleep(0.05)
        if "fail" in answer_id:
            raise t.GradeFailure("scoring_point_max_mismatch", "scoring_point", "模拟评分点失败")
        return dict(VALID)
    finally:
        with lock:
            active["n"] -= 1


t._LLM_CALLER = caller

results = {}
for concurrency in (2, 3, 5, 8):
    job_id = f"job_stress_{concurrency}"
    answer_ids = make_answers(12, f"s{concurrency}")
    make_job(job_id, answer_ids, concurrency)
    started = time.time()
    t.run_job(job_id)
    elapsed = int((time.time() - started) * 1000)
    items = db.q("SELECT status, answer_id, attempt_count, last_error, last_error_type, last_error_detail FROM teacher_grade_job_items WHERE job_id=?", (job_id,))
    results[f"stress_{concurrency}"] = {"job_concurrency": db.q1("SELECT concurrency FROM teacher_grade_jobs WHERE id=?", (job_id,))["concurrency"], "elapsed_ms": elapsed, "completed": sum(1 for i in items if i["status"] == "completed"), "failed": sum(1 for i in items if i["status"] == "failed"), "max_active": active["max"], "duplicate_calls": sum(1 for v in counts.values() if v > 1), "failed_detail": [{"answer": i["answer_id"], "error": i.get("last_error"), "kind": i.get("last_error_type"), "detail": i.get("last_error_detail"), "attempts": db.q("SELECT error_type,error_message FROM teacher_grade_attempts WHERE answer_id=? ORDER BY rowid", (i["answer_id"],))} for i in items if i["status"] == "failed"]}

# 单题失败不影响其他题
job_fail = "job_one_fail"
answers_fail = make_answers(4, "mix")
answers_fail[1] = "ans_mix_fail"
db.x("UPDATE teacher_answers SET id='ans_mix_fail' WHERE id='ans_mix_1'")
# 修正 item 引用
db.x("UPDATE teacher_grade_job_items SET answer_id='ans_mix_fail' WHERE answer_id='ans_mix_1'")
make_job(job_fail, answers_fail, 2)
t.run_job(job_fail)
mix = db.q("SELECT status FROM teacher_grade_job_items WHERE job_id=?", (job_fail,))
results["single_failure"] = {"completed": sum(1 for i in mix if i["status"] == "completed"), "failed": sum(1 for i in mix if i["status"] == "failed")}

# 暂停后恢复
job_pause = "job_pause1521"
answers_pause = make_answers(6, "pause")
make_job(job_pause, answers_pause, 2)
worker = threading.Thread(target=t.run_job, args=(job_pause,), daemon=True)
worker.start()
time.sleep(0.03)
t.pause_job(job_pause, {"id": "teacher_q", "role": "teacher"})
worker.join(timeout=20)
paused = db.q1("SELECT status FROM teacher_grade_jobs WHERE id=?", (job_pause,))
paused_pending = db.q1("SELECT COUNT(*) n FROM teacher_grade_job_items WHERE job_id=? AND status='pending'", (job_pause,))["n"]
t.resume_job(job_pause, type("BG", (), {"add_task": lambda self, fn, *a: None})(), {"id": "teacher_q", "role": "teacher"})
t.run_job(job_pause)
resumed = db.q1("SELECT status FROM teacher_grade_jobs WHERE id=?", (job_pause,))
results["pause_resume"] = {"paused": paused["status"], "pending": paused_pending, "resumed": resumed["status"]}

# 重启恢复：模拟 running + 旧心跳 + running item
job_restart = "job_restart1521"
answers_restart = make_answers(2, "restart")
make_job(job_restart, answers_restart, 1)
db.x("UPDATE teacher_grade_jobs SET status='running', heartbeat_at='2020-01-01T00:00:00' WHERE id=?", (job_restart,))
db.x("UPDATE teacher_grade_job_items SET status='running' WHERE job_id=? AND answer_id=?", (job_restart, answers_restart[0]))
recovered = t.recover_jobs_on_startup()
for _ in range(100):
    time.sleep(0.05)
    if db.q1("SELECT status FROM teacher_grade_jobs WHERE id=?", (job_restart,))["status"] in ("completed", "failed"):
        break
results["restart"] = {"recovered": recovered["recovered"], "status": db.q1("SELECT status FROM teacher_grade_jobs WHERE id=?", (job_restart,))["status"]}

# 重试只处理失败，不覆盖成功
job_retry = "job_retry1521"
answers_retry = make_answers(2, "retry")
make_job(job_retry, answers_retry, 1)
db.x("UPDATE teacher_grade_job_items SET status='completed', grade_id='existing_success' WHERE job_id=? AND answer_id=?", (job_retry, answers_retry[0]))
db.x("UPDATE teacher_grade_job_items SET status='failed', last_error='x' WHERE job_id=? AND answer_id=?", (job_retry, answers_retry[1]))
db.x("UPDATE teacher_grade_jobs SET status='completed', processed=2, completed=1, failed=1 WHERE id=?", (job_retry,))
retry_result = t.retry_job(job_retry, type("BG", (), {"add_task": lambda self, fn, *a: None})(), {"id": "teacher_q", "role": "teacher"}, {"limit": 1})
retry_items = db.q("SELECT answer_id,status,grade_id FROM teacher_grade_job_items WHERE job_id=?", (job_retry,))
results["retry_only_failed"] = {"result": retry_result, "items": [{"answer_id": i["answer_id"], "status": i["status"], "grade_id": i["grade_id"]} for i in retry_items]}

# 网络失败后重试只生成一份有效结果
job_net = "job_net1521"
answers_net = make_answers(1, "net")
make_job(job_net, answers_net, 1)
sequence = {"n": 0}


def flaky(answer, question, steps, key, provider, standard, context):
    sequence["n"] += 1
    if sequence["n"] == 1:
        raise t.ModelRequestError("INCOMPLETE_RESPONSE", "模拟不完整响应")
    return dict(VALID)


t._LLM_CALLER = flaky
t.run_job(job_net)
runs = db.q("SELECT status FROM teacher_grade_runs WHERE answer_id=?", (answers_net[0],))
results["one_valid_result"] = {"runs": len(runs), "valid": sum(1 for r in runs if r["status"] != "failed"), "calls": sequence["n"]}

print(json.dumps(results, ensure_ascii=False, default=str))
