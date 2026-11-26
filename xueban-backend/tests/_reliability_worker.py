"""P2 后台任务可靠性：重启恢复 stale job、双 worker 并发只判一次、计数一致。"""
import json
import threading
import time

from tests.support_import import answer, make_files, question, student
from xueban.api import xueban_teacher as t
from xueban.services import xueban_store as db

USER = {"id": "teacher-id", "role": "teacher"}


class Background:
    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *args, **kwargs):
        self.tasks.append((fn, args, kwargs))


t.init()
calls = []
lock = threading.Lock()


def slow_grader(answer_row, question_row, steps, key, provider, standard, textbook_context=""):
    with lock:
        calls.append(answer_row["id"])
    time.sleep(0.25)
    qid = question_row.get("question_id") or question_row.get("id")
    return {"score": 3, "max_score": 5, "is_correct": "partial", "feedback": "部分正确", "judgment_reason": "按评分点给分", "needs_review": False, "step_results": [{"step_index": 1, "student_step": steps[0] if steps else "步骤", "is_correct": False, "score": 3, "max_score": 5, "error_type": "计算操作错误", "judgment_reason": "符号错误", "scoring_point_links": [{"scoring_point_id": f"{qid}P1", "awarded_score": 3, "max_score": 5}]}]}


t._LLM_CALLER = slow_grader
batch_id = t.upload_import(make_files([answer(event=f"E{i}", student_code=f"S{i}") for i in range(3)], questions=[question(qid="Q001", scoring_points="Q001P1 5分")], students=[student(f"S{i}") for i in range(3)]), USER)["batch_id"]
t.confirm_preprocessing(batch_id, USER)
t.batch(Background(), USER, {"batch_id": batch_id})
job = db.q1("SELECT * FROM teacher_grade_jobs WHERE batch_id=?", (batch_id,))
job_id = job["id"]

# A. 模拟崩溃后重启：running + 过期心跳 + running item
db.x("UPDATE teacher_grade_jobs SET status='running', heartbeat_at='2026-01-01T00:00:00' WHERE id=?", (job_id,))
db.x("UPDATE teacher_grade_job_items SET status='running' WHERE job_id=? AND rowid=(SELECT MIN(rowid) FROM teacher_grade_job_items WHERE job_id=?)", (job_id, job_id))
recovered = t.recover_stale_jobs()
after_recover = db.q1("SELECT status, pause_requested FROM teacher_grade_jobs WHERE id=?", (job_id,))
items_after_recover = db.q1("SELECT COUNT(*) n FROM teacher_grade_job_items WHERE job_id=? AND status='running'", (job_id,))["n"]

# B. 双 worker 并发领取：同一个 job 只应被一个 worker 执行
db.x("UPDATE teacher_grade_jobs SET status='queued', pause_requested=0, heartbeat_at=NULL WHERE id=?", (job_id,))
for item in db.q("SELECT * FROM teacher_grade_job_items WHERE job_id=?", (job_id,)):
    db.x("UPDATE teacher_grade_job_items SET status='pending', grade_id=NULL WHERE id=?", (item["id"],))
calls.clear()
threads = [threading.Thread(target=t.run_job, args=(job_id,)) for _ in range(2)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
final = db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_id,))
items = db.q("SELECT * FROM teacher_grade_job_items WHERE job_id=?", (job_id,))
grades = db.q("SELECT * FROM teacher_grade_runs WHERE answer_id IN (SELECT answer_id FROM teacher_grade_job_items WHERE job_id=?)", (job_id,))
print(json.dumps({
    "recover": {"jobs": recovered["jobs"], "items": recovered["items"], "status": after_recover["status"], "pause_requested": after_recover["pause_requested"], "still_running": items_after_recover},
    "concurrency": {"job_id": job_id, "grader_calls": len(calls), "unique_calls": len(set(calls)), "item_count": len(items), "job_status": final["status"], "processed": final["processed"], "completed": sum(1 for item in items if item["status"] == "completed"), "failed": sum(1 for item in items if item["status"] == "failed"), "grade_rows": len(grades)},
}, ensure_ascii=False))
