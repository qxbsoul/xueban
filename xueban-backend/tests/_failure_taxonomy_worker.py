"""判题失败分类、结构修复重试、重试幂等与人工保护。"""
import json
import os
import sqlite3

os.environ["XUEBAN_GRADER_ATTEMPTS"] = "2"
os.environ["XUEBAN_GRADER_RETRY_BASE_MS"] = "0"

from xueban.api import xueban_teacher as t
from xueban.services import learning_analysis as la
from xueban.services import xueban_store as db

t.init()
la.ensure_model()
NOW = "2026-01-01T00:00:00"
B = "batch_tax"
db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", (B, "tax.csv", "committed", "{}", "[]", "confirmed", NOW))
db.x("INSERT INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", ("class_t", "T1", "分类班", 7, NOW))
db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", ("stu_t", "user_t", "T01", "class_t", "T01", 7, NOW))
db.x("INSERT INTO teacher_questions (id,stem,reference_answer,max_score,scoring_points,question_type) VALUES (?,?,?,?,?,?)", ("Q001", "1+1=?", "2", 2, "Q001P1 2分", "解答题"))
db.x("INSERT INTO teacher_question_versions (version_id,batch_id,question_id,stem,reference_answer,max_score,scoring_points,question_type,created_at) VALUES (?,?,?,?,?,?,?,?,?)", (f"qv:{B}:Q001", B, "Q001", "1+1=?", "2", 2, "Q001P1 2分", "解答题", NOW))
db.x("INSERT INTO teacher_topics (id,code,title,created_at) VALUES (?,?,?,?)", ("topic_t", "T01", "分类主题", NOW))
db.x("INSERT INTO teacher_indicators (id,code,topic_id,description) VALUES (?,?,?,?)", ("indicator_t", "I01", "topic_t", "分类知识点"))
db.x("INSERT INTO scoring_point_knowledge_links (id,scoring_point_id,knowledge_point_id,weight,source,confidence,status,batch_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", ("link_t", "Q001P1", "kp:I01", 1.0, "teacher", 1.0, "confirmed", B, NOW, NOW))

VALID = {
    "score": 2, "max_score": 2, "is_correct": "correct",
    "step_results": [{"step_index": 1, "source_step_id": "src-1", "student_step": "1+1=2", "is_correct": True, "score": 2, "max_score": 2, "error_type": "", "judgment_reason": "正确", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 2, "max_score": 2}],
                       "operation_units": [{"operation_id": "op-1", "source_step_id": "src-1", "student_fragment": "1+1=2", "is_correct": True, "score": 2, "max_score": 2, "error_type": "", "judgment_reason": "正确", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 2, "max_score": 2}]}]}],
}


def answer(aid, text="1+1=2", process="1+1"):
    db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,grading_status,import_batch_id,max_score,original_score,solution_process,original_answer,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
         (aid, "stu_t", "Q001", f"E-{aid}", 1, "pending", B, 2, 0, process, text, f"qv:{B}:Q001"))


def run(aid, payload, job_id=""):
    calls = {"n": 0}

    def caller(*args, **kwargs):
        calls["n"] += 1
        value = payload(calls["n"]) if callable(payload) else payload
        if isinstance(value, Exception):
            raise value
        return value

    t._LLM_CALLER = caller
    result = t.grade_answer(aid, "test-key", job_id=job_id)
    t._LLM_CALLER = None
    grade = result.get("grade") or {}
    run_row = db.q1("SELECT * FROM teacher_grade_runs WHERE id=?", (grade.get("id"),)) if grade.get("id") else {}
    context = {}
    try:
        context = json.loads((run_row or {}).get("request_context_json") or "{}")
    except Exception:
        context = {}
    attempts = db.q("SELECT * FROM teacher_grade_attempts WHERE answer_id=? ORDER BY rowid", (aid,))
    return {"calls": calls["n"], "status": grade.get("status"), "review": int(grade.get("needs_review") or 0), "score": grade.get("score"), "error_type": context.get("error_type") or grade.get("error_type"), "stage": context.get("error_stage"), "exception_class": context.get("exception_class"), "attempts": len(attempts), "attempt_types": [item["error_type"] for item in attempts], "issues": [item.get("code") for item in json.loads((run_row or {}).get("structure_json") or "{}").get("validation_issues") or []]}


out = {}
for name, payload, text, process in (
    ("ok", VALID, "1+1=2", "1+1"),
    ("awarded_over_max", {**VALID, "score": 2, "step_results": [{"step_index": 1, "source_step_id": "src-1", "student_step": "1+1=2", "is_correct": False, "score": 2, "max_score": 2, "error_type": "", "judgment_reason": "x", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 9, "max_score": 2}], "operation_units": []}]}, "1+1=2", "1+1"),
    ("declared_max_clamped", {**VALID, "step_results": [{"step_index": 1, "source_step_id": "src-1", "student_step": "1+1=2", "is_correct": True, "score": 2, "max_score": 2, "error_type": "", "judgment_reason": "x", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 2, "max_score": 4}], "operation_units": []}]}, "1+1=2", "1+1"),
    ("unknown_point", {**VALID, "step_results": [{"step_index": 1, "source_step_id": "src-1", "student_step": "1+1=2", "is_correct": True, "score": 2, "max_score": 2, "error_type": "", "judgment_reason": "x", "scoring_point_links": [{"scoring_point_id": "Q999P9", "awarded_score": 2, "max_score": 2}], "operation_units": []}]}, "1+1=2", "1+1"),
    ("null_step_index", {**VALID, "step_results": [{"step_index": None, "source_step_id": "src-1", "student_step": "1+1=2", "is_correct": True, "score": 2, "max_score": 2, "error_type": "", "judgment_reason": "x", "scoring_point_links": [], "operation_units": []}]}, "1+1=2", "1+1"),
    ("null_step_score", {**VALID, "step_results": [{"step_index": 1, "source_step_id": "src-1", "student_step": "1+1=2", "is_correct": True, "score": None, "max_score": 2, "error_type": "", "judgment_reason": "x", "scoring_point_links": [], "operation_units": []}]}, "1+1=2", "1+1"),
    ("empty_answer", VALID, "", ""),
    ("db_persist", VALID, "1+1=2", "1+1"),
):
    answer(f"ans_{name}", text, process)
    if name == "db_persist":
        original = t.insert_grade
        persist_calls = {"n": 0}

        def broken(*args, **kwargs):
            persist_calls["n"] += 1
            if persist_calls["n"] == 1:
                raise sqlite3.OperationalError("disk I/O error")
            return original(*args, **kwargs)

        t.insert_grade = broken
        try:
            out[name] = run(f"ans_{name}", payload)
        finally:
            t.insert_grade = original
    else:
        out[name] = run(f"ans_{name}", payload)

# 结构错误重试：第一次坏、第二次好
retry_ok = {"n": 0}


def flaky(*args, **kwargs):
    retry_ok["n"] += 1
    if retry_ok["n"] == 1:
        return {"score": 2, "max_score": 2, "step_results": [{"step_index": 1, "source_step_id": "src-1", "student_step": "1+1=2", "score": 2, "max_score": 2, "judgment_reason": "x", "error_type": "", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 2, "max_score": 2}]}]}
    return VALID


answer("ans_flaky")
t._LLM_CALLER = flaky
flaky_result = t.grade_answer("ans_flaky", "test-key")
t._LLM_CALLER = None
out["structure_retry_success"] = {"calls": retry_ok["n"], "status": flaky_result["grade"]["status"]}

# 网络超时
import socket


def timeout_caller(*args, **kwargs):
    raise socket.timeout("The read operation timed out")


answer("ans_timeout")
t._LLM_CALLER = timeout_caller
timeout_result = t.grade_answer("ans_timeout", "test-key")
t._LLM_CALLER = None
timeout_grade = timeout_result["grade"]
tc = json.loads(db.q1("SELECT request_context_json FROM teacher_grade_runs WHERE id=?", (timeout_grade["id"],))["request_context_json"])
out["timeout"] = {"status": timeout_grade["status"], "error_type": tc.get("error_type"), "attempts": db.q1("SELECT COUNT(*) n FROM teacher_grade_attempts WHERE answer_id='ans_timeout'")["n"], "retryable": tc.get("retryable")}

# 幂等：同一 job 成功后重复判题不新增 run / evidence
db.x("INSERT INTO teacher_grade_jobs (id,status,total,created_at,updated_at,batch_id) VALUES (?,?,?,?,?,?)", ("job_tax", "running", 1, NOW, NOW, B))
ok_grade_id = db.q1("SELECT id FROM teacher_grade_runs WHERE answer_id='ans_ok' ORDER BY grading_version DESC LIMIT 1")["id"]
db.x("INSERT INTO teacher_grade_job_items (id,job_id,answer_id,status,grade_id,attempt_count,updated_at) VALUES (?,?,?,?,?,?,?)", ("item_tax", "job_tax", "ans_ok", "completed", ok_grade_id, 1, NOW))
before_runs = db.q1("SELECT COUNT(*) n FROM teacher_grade_runs WHERE answer_id='ans_ok'")["n"]
before_evidence = len([item for item in la._evidence(B)[0] if item["student_id"] == "stu_t" and item["question_id"] == "Q001"])
reused = t.grade_answer("ans_ok", "test-key", job_id="job_tax")
after_runs = db.q1("SELECT COUNT(*) n FROM teacher_grade_runs WHERE answer_id='ans_ok'")["n"]
out["idempotent"] = {"reused": bool(reused.get("reused")), "runs_before": before_runs, "runs_after": after_runs, "evidence_before": before_evidence, "evidence_after": len([item for item in la._evidence(B)[0] if item["student_id"] == "stu_t" and item["question_id"] == "Q001"])}

# 重试：只重试失败、跳过人工确认
db.x("INSERT INTO teacher_grade_jobs (id,status,total,failed,created_at,updated_at,batch_id) VALUES (?,?,?,?,?,?,?)", ("job_retry", "partially_failed", 4, 3, NOW, NOW, B))
db.x("INSERT INTO teacher_grade_job_items (id,job_id,answer_id,status,attempt_count,last_error,updated_at) VALUES (?,?,?,?,?,?,?)", ("ri_1", "job_retry", "ans_ok", "failed", 1, "boom", NOW))
db.x("INSERT INTO teacher_grade_job_items (id,job_id,answer_id,status,attempt_count,last_error,updated_at) VALUES (?,?,?,?,?,?,?)", ("ri_2", "job_retry", "ans_flaky", "failed", 2, "boom2", NOW))
db.x("INSERT INTO teacher_grade_job_items (id,job_id,answer_id,status,attempt_count,last_error,updated_at) VALUES (?,?,?,?,?,?,?)", ("ri_3", "job_retry", "ans_declared_max_clamped", "failed", 1, "boom3", NOW))
manual_grade = t.grade_answer("ans_ok", "test-key")["grade"]
db.x("UPDATE teacher_grade_runs SET status='manual_corrected', needs_review=0 WHERE id=?", (manual_grade["id"],))
db.x("UPDATE teacher_grade_job_items SET grade_id=? WHERE id='ri_1'", (manual_grade["id"],))

t.default_llm_key = lambda: "test-key"
t.run_job = lambda job_id: None
background = type("BG", (), {"add_task": lambda self, fn, *args: None})()
retry_result = t.retry_job("job_retry", background, {"id": "teacher_1", "role": "teacher"})
out["retry"] = {"retried": retry_result["retried"], "skipped_manual": retry_result["skipped_manual"], "statuses": {row["id"]: row["status"] for row in db.q("SELECT id,status FROM teacher_grade_job_items WHERE job_id='job_retry'")}, "manual_grade_status": db.q1("SELECT status FROM teacher_grade_runs WHERE id=?", (manual_grade["id"],))["status"]}

print(json.dumps(out, ensure_ascii=False, default=str))
