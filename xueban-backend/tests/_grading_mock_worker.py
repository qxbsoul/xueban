"""P1 判题闭环（mock 模型）：创建任务 → 领取 → 成功/失败/超时 → attempt → 暂停/继续 → 重试 → 重判。"""
import json
import os

os.environ.setdefault("DEEPSEEK_API_KEY", "test-key-not-real")

from tests.support_import import answer, make_files, question, student
from xueban.api import xueban_teacher as t
from xueban.services import xueban_store as db

USER = {"id": "teacher", "role": "teacher"}


class FakeBackground:
    def add_task(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


def ok_grader(answer, question, steps, key, provider, standard, textbook_context=""):
    qid = question.get("question_id") or question.get("id")
    return {
        "score": 3,
        "max_score": 5,
        "is_correct": "partial",
        "feedback": "部分正确",
        "judgment_reason": "按评分点给分",
        "needs_review": False,
        "step_results": [{
            "step_index": 1, "student_step": steps[0] if steps else "步骤", "is_correct": False,
            "score": 3, "max_score": 5, "error_type": "计算操作错误", "judgment_reason": "第二步符号错误",
            "scoring_point_links": [
                {"scoring_point_id": f"{qid}P1", "awarded_score": 2, "max_score": 2},
                {"scoring_point_id": f"{qid}P2", "awarded_score": 1, "max_score": 3},
            ],
        }],
    }


def bad_score_grader(answer, question, steps, key, provider, standard, textbook_context=""):
    return {"score": 99, "max_score": 5, "step_results": []}


def bad_point_grader(answer, question, steps, key, provider, standard, textbook_context=""):
    return {"score": 2, "max_score": 5, "step_results": [{"step_index": 1, "student_step": "x", "is_correct": False, "score": 2, "max_score": 5, "error_type": "", "judgment_reason": "", "scoring_point_links": [{"scoring_point_id": "ZZZ999P1", "awarded_score": 2, "max_score": 2}]}]}


def timeout_grader(answer, question, steps, key, provider, standard, textbook_context=""):
    raise TimeoutError("timed out")


def import_batch(events, qspecs, stem="题干"):
    questions = [question(qid=qid, stem=f"{stem}-{qid}", max_score=str(maxs), scoring_points=sp, reference="参考答案") for qid, maxs, sp in qspecs]
    answers = [answer(event=event, student_code=student_code, qid=qid, final="答案", process="第一步\n第二步") for event, student_code, qid in events]
    students = [student(code=code) for code in sorted({item[1] for item in events})]
    result = t.upload_import(make_files(answers, questions=questions, students=students), USER)
    t.confirm_preprocessing(result["batch_id"], USER)
    return result["batch_id"]


t.init()

# A. 成功链路：2 题 3 次作答
batch_ok = import_batch(
    [("EOK1", "S01", "Q001"), ("EOK2", "S02", "Q001"), ("EOK3", "S01", "Q002")],
    [("Q001", 5, "Q001P1 2分 Q001P2 3分"), ("Q002", 5, "Q002P1 2分 Q002P2 3分")],
)
t._LLM_CALLER = ok_grader
t.batch(FakeBackground(), USER, {"batch_id": batch_ok})
job_ok = db.q1("SELECT * FROM teacher_grade_jobs WHERE batch_id=?", (batch_ok,))
items_ok = db.q("SELECT * FROM teacher_grade_job_items WHERE job_id=?", (job_ok["id"],))
grades_ok = db.q("SELECT * FROM teacher_grade_runs")
attempts_ok = db.q("SELECT * FROM teacher_grade_attempts WHERE task_id=?", (job_ok["id"],))

# B. 重判：同一作答重新判题必须新增 grading_version，历史保留
answer_id = items_ok[0]["answer_id"]
t.grade_answer(answer_id)
history = db.q("SELECT * FROM teacher_grade_runs WHERE answer_id=? ORDER BY grading_version", (answer_id,))

# C. 失败链路：分数越界 + 不存在的评分点 + 超时
batch_bad = import_batch(
    [("EBAD1", "S01", "Q003"), ("EBAD2", "S02", "Q003"), ("EBAD3", "S01", "Q003")],
    [("Q003", 5, "Q003P1 2分 Q003P2 3分")],
)
t._LLM_CALLER = bad_score_grader
t.batch(FakeBackground(), USER, {"batch_id": batch_bad})
job_bad = db.q1("SELECT * FROM teacher_grade_jobs WHERE batch_id=?", (batch_bad,))
items_bad = db.q("SELECT * FROM teacher_grade_job_items WHERE job_id=?", (job_bad["id"],))
attempts_bad = db.q("SELECT * FROM teacher_grade_attempts WHERE task_id=?", (job_bad["id"],))
failure_grade_status = sorted({g["status"] for g in db.q("SELECT g.status FROM teacher_grade_runs g JOIN teacher_grade_job_items i ON i.grade_id=g.id WHERE i.job_id=?", (job_bad["id"],))})

# 重试同 job：换回成功模型后只处理失败项
t._LLM_CALLER = ok_grader
retried = t.retry_job(job_bad["id"], FakeBackground(), USER, {})
job_bad_after = db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_bad["id"],))
attempts_after = db.q("SELECT * FROM teacher_grade_attempts WHERE task_id=?", (job_bad["id"],))

# D. 暂停/继续：有 pending 项时暂停不消费，继续后完成
batch_pause = import_batch(
    [("EPA1", "S01", "Q004"), ("EPA2", "S02", "Q004")],
    [("Q004", 5, "Q004P1 2分 Q004P2 3分")],
)
pause_job_id = "job_pause_flow"
db.x("INSERT INTO teacher_grade_jobs (id,status,batch_id,total,processed,completed,failed,needs_review,current_answer_id,filters_json,pause_requested,error,created_at,updated_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (pause_job_id, "queued", batch_pause, 2, 0, 0, 0, 0, "", "{}", 1, "", t.now(), t.now(), ""))
for row in db.q("SELECT id FROM teacher_answers WHERE import_batch_id=?", (batch_pause,)):
    db.x("INSERT INTO teacher_grade_job_items (id,job_id,answer_id,status,updated_at) VALUES (?,?,?,?,?)", (db.new_id("jobitem"), pause_job_id, row["id"], "pending", t.now()))
t.run_job(pause_job_id)
paused_job = db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (pause_job_id,))
paused_items = db.q("SELECT status, COUNT(*) n FROM teacher_grade_job_items WHERE job_id=? GROUP BY status", (pause_job_id,))
t._LLM_CALLER = ok_grader
t.resume_job(pause_job_id, FakeBackground(), USER)
resumed_job = db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (pause_job_id,))
resumed_items = db.q("SELECT status, COUNT(*) n FROM teacher_grade_job_items WHERE job_id=? GROUP BY status", (pause_job_id,))

# E. 暂停中的任务不允许再新建判题任务（避免同一批作答重复判题）
batch_block = import_batch([("EB1", "S01", "Q005")], [("Q005", 5, "Q005P1 2分 Q005P2 3分")])
db.x("INSERT INTO teacher_grade_jobs (id,status,batch_id,total,processed,completed,failed,needs_review,current_answer_id,filters_json,pause_requested,error,created_at,updated_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("job_block", "paused", batch_block, 1, 0, 0, 0, 0, "", "{}", 1, "", t.now(), t.now(), ""))
duplicate_status = 0
try:
    t.batch(FakeBackground(), USER, {"batch_id": batch_block})
except Exception as exc:
    duplicate_status = getattr(exc, "status_code", 0)

# F. 超时：模型超时必须记录可重试失败
batch_timeout = import_batch([("ET1", "S01", "Q006")], [("Q006", 5, "Q006P1 2分 Q006P2 3分")])
t._LLM_CALLER = timeout_grader
t.batch(FakeBackground(), USER, {"batch_id": batch_timeout})
job_timeout = db.q1("SELECT * FROM teacher_grade_jobs WHERE batch_id=?", (batch_timeout,))
timeout_attempts = db.q("SELECT * FROM teacher_grade_attempts WHERE task_id=?", (job_timeout["id"],))

print(json.dumps({
    "ok": {
        "job_status": job_ok["status"], "processed": job_ok["processed"], "completed": job_ok["completed"],
        "items": {status: sum(1 for i in items_ok if i["status"] == status) for status in ("pending", "running", "completed", "failed")},
        "grades": len(grades_ok), "attempts": len(attempts_ok),
        "attempt_fields": [{k: a[k] for k in ("batch_id", "student_id", "duration_ms", "retryable", "failure_stage")} for a in attempts_ok[:1]],
        "grade_versions": sorted({g["grading_version"] for g in grades_ok}),
        "prompt_version": grades_ok[0]["grading_prompt_version"],
        "grader_version": grades_ok[0]["grader_version"],
    },
    "regrade": {"versions": [g["grading_version"] for g in history], "count": len(history), "scores": [g["score"] for g in history]},
    "failure": {
        "job_status": job_bad["status"], "failed": job_bad["failed"],
        "item_statuses": sorted({i["status"] for i in items_bad}),
        "attempt_stages": sorted({a["failure_stage"] for a in attempts_bad}),
        "attempt_retryable": sorted({a["retryable"] for a in attempts_bad}),
        "grade_status": failure_grade_status,
    },
    "retry": {"same_job": retried["job"]["id"] == job_bad["id"], "status": job_bad_after["status"], "completed": job_bad_after["completed"], "attempts": len(attempts_after)},
    "pause": {"paused_status": paused_job["status"], "paused_items": {row["status"]: row["n"] for row in paused_items}, "resumed_status": resumed_job["status"], "resumed_items": {row["status"]: row["n"] for row in resumed_items}},
    "duplicate_status": duplicate_status,
    "timeout": {"job_status": job_timeout["status"], "stages": sorted({a["failure_stage"] for a in timeout_attempts}), "retryable": sorted({a["retryable"] for a in timeout_attempts})},
}, ensure_ascii=False))
