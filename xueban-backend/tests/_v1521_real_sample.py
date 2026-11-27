"""v1.5.21 真实小规模对照：只重判当前失败记录，记录成功率、耗时、错误分布。"""
import json
import os
import time

os.environ["XUEBAN_XUEBAN_DB"] = r"D:\capital lion\xueban\xueban-backend\.xueban\xueban.sqlite"
os.environ["XUEBAN_HOME"] = r"D:\capital lion\xueban\xueban-backend"
os.environ["XUEBAN_JOB_CONCURRENCY"] = "2"

from xueban.api import xueban_teacher as t
from xueban.services import xueban_store as db

JOB = "job_23a2d13b29"
t.init()
if not t.default_llm_key():
    raise SystemExit("NO_KEY")

before = t.job_counts(JOB, "completed")
failed = db.q("SELECT i.id, i.answer_id FROM teacher_grade_job_items i WHERE i.job_id=? AND i.status='failed'", (JOB,))
print(json.dumps({"before": before, "failed": len(failed)}, ensure_ascii=False))

class BG:
    def add_task(self, *args, **kwargs):
        return None

started = time.time()
result = t.retry_job(JOB, BG(), {"id": "teacher_reliability", "role": "teacher"}, {"limit": 60})
selected = [item["answer_id"] for item in failed[:60]]
print(json.dumps({"retry": {"retried": result["retried"], "total": result["job"]["retry_total"], "status": result["job"]["status"]}}, ensure_ascii=False))
t.run_job(JOB)
elapsed = round(time.time() - started, 1)

after = t.job_counts(JOB, "completed")
items = db.q("SELECT answer_id, status, attempt_count, last_error_type, last_error, result_id FROM teacher_grade_job_items WHERE job_id=? AND answer_id IN (%s)" % ",".join("?" for _ in selected), (JOB, *selected))
attempts = db.q("SELECT answer_id, duration_ms, error_type, failure_stage FROM teacher_grade_attempts WHERE task_id=? AND answer_id IN (%s)" % ",".join("?" for _ in selected), (JOB, *selected))
durations = sorted(int(a["duration_ms"] or 0) for a in attempts)
p95 = durations[int(len(durations) * 0.95) - 1] if durations else 0
success = sum(1 for item in items if item["status"] == "completed")
final_failed = sum(1 for item in items if item["status"] == "failed")
kinds = {}
for item in items:
    if item["status"] == "failed":
        kinds[item["last_error_type"] or "UNKNOWN_ERROR"] = kinds.get(item["last_error_type"] or "UNKNOWN_ERROR", 0) + 1
attempt_kinds = {}
for attempt in attempts:
    key = attempt["error_type"] or "success"
    attempt_kinds[key] = attempt_kinds.get(key, 0) + 1
print(json.dumps({
    "elapsed_s": elapsed,
    "selected": len(selected),
    "success": success,
    "failed": final_failed,
    "success_rate": round(success / max(1, len(selected)), 4),
    "failure_rate": round(final_failed / max(1, len(selected)), 4),
    "avg_attempts": round(sum(int(item["attempt_count"] or 0) for item in items) / max(1, len(items)), 2),
    "p95_ms": p95,
    "attempts": len(attempts),
    "attempt_kinds": attempt_kinds,
    "failure_kinds": kinds,
    "after": after,
    "job": db.q1("SELECT status, retry_total, retry_success, retry_failed, worker_id FROM teacher_grade_jobs WHERE id=?", (JOB,)),
}, ensure_ascii=False, default=str))
