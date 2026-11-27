"""分析流水线：自动触发、状态、幂等、版本失效、LLM 层与确定性层分离。"""
import json

from xueban.api import xueban_teacher as t
from xueban.services import analysis_pipeline as pipeline
from xueban.services import learning_analysis as la
from xueban.services import xueban_store as db

t.init()
NOW = "2026-01-01T00:00:00"
B = "batch_pipe"
db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", (B, "pipe.csv", "committed", "{}", "[]", "confirmed", NOW))
db.x("INSERT INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", ("class_p", "P1", "流水线班", 7, NOW))
db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", ("stu_p1", "user_p1", "P01", "class_p", "P01", 7, NOW))
db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", ("stu_p2", "user_p2", "P02", "class_p", "P02", 7, NOW))
db.x("INSERT INTO teacher_exams (id,exam_date,topics,description,available_questions,course_id) VALUES (?,?,?,?,?,?)", ("N01", "2026-01-01", "", "", "Q001", ""))
db.x("INSERT INTO teacher_questions (id,stem,reference_answer,max_score,scoring_points,indicator_id,question_type) VALUES (?,?,?,?,?,?,?)", ("Q001", "题干", "答案", 1, "Q001P1 1分", "I01", "填空题"))
db.x("INSERT INTO teacher_question_versions (version_id,batch_id,question_id,stem,reference_answer,max_score,scoring_points,indicator_id,question_type,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (f"qv:{B}:Q001", B, "Q001", "题干", "答案", 1, "Q001P1 1分", "I01", "填空题", NOW))
db.x("INSERT INTO teacher_indicators (id,code,topic_id,description) VALUES (?,?,?,?)", ("indicator_i01", "I01", "topic_x", "知识点1"))
for index, sid in enumerate(("stu_p1", "stu_p2")):
    db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,grading_status,import_batch_id,max_score,original_score,solution_process,original_answer,exam_node_id,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (f"ans_{sid}", sid, "Q001", f"E{sid}", 1, "graded", B, 1, 1, "步骤", "答案", "N01", f"qv:{B}:Q001"))
    db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,status,needs_review,step_results_json,created_at,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (f"g_{sid}", f"ans_{sid}", sid, "Q001", f"E{sid}", 1, 1, "m", 1, 1, "graded", 0, json.dumps([{"step_index": 1, "source_step_id": "src-1", "student_step": "步骤", "is_correct": True, "score": 1, "max_score": 1, "error_type": "", "judgment_reason": "对", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 1, "max_score": 1}]}], ensure_ascii=False), NOW, f"qv:{B}:Q001"))

# 无 Key：确定性层完成、LLM 层 partial（LLM 由判题事件触发，按对象生成）
pipeline._llm_key = lambda: ""
pipeline.record_grade_event("ans_stu_p1")
first = pipeline.auto_after_grading(B, requested_by="test", run_inline=True)
tasks_first = {row["id"]: row["status"] for row in db.q("SELECT id, status FROM teacher_analysis_tasks WHERE batch_id=?", (B,))}
deterministic = db.q1("SELECT * FROM teacher_analysis_tasks WHERE batch_id=? AND kind='deterministic' AND scope='batch'", (B,))
llm_partial = db.q1("SELECT * FROM teacher_analysis_tasks WHERE batch_id=? AND kind='llm' ORDER BY created_at DESC LIMIT 1", (B,))
detail = pipeline.task_detail(deterministic["id"])
version_before = pipeline.data_version(B)

# 幂等：同版本再次触发，任务不增加
second = pipeline.auto_after_grading(B, requested_by="test", run_inline=True)
count_after_second = db.q1("SELECT COUNT(*) n FROM teacher_analysis_tasks WHERE batch_id=?", (B,))["n"]

# 判题结果变化 → 版本变化、旧任务 stale、新任务创建
db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,status,needs_review,step_results_json,created_at,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
     ("g_new", "ans_stu_p1", "stu_p1", "Q001", "Enew", 1, 2, "m", 1, 1, "graded", 0, "[]", "2026-01-02T00:00:00", f"qv:{B}:Q001"))
version_after = pipeline.data_version(B)
stale = pipeline.list_tasks(B)
stale_flags = {row["id"]: row["stale"] for row in stale["tasks"]}
third = pipeline.auto_after_grading(B, requested_by="test", run_inline=True)
count_after_third = db.q1("SELECT COUNT(*) n FROM teacher_analysis_tasks WHERE batch_id=?", (B,))["n"]

# LLM 层：用假 caller 验证「只解释、引用证据、不重算数字」
pipeline._llm_key = lambda: "test-key"
pipeline._call_llm = lambda prompt, key: {"class_observations": [{"class_code": "P1", "summary": "引用 kp:I01", "evidence_refs": ["kp:I01", "Q001"]}], "error_explanations": [], "teaching_suggestions": [{"class_code": "P1", "suggestion": "针对 kp:I01 练习", "evidence_refs": ["kp:I01"], "confidence": 0.8}]}
llm_done = pipeline.enqueue(B, "batch", "", "llm", "test", force=True)
llm_task = pipeline.run_task(llm_done["task"]["id"])
llm_result = json.loads(llm_task["task"]["result_json"])

print(json.dumps({
    "first": {"deterministic_status": deterministic["status"], "llm_status": llm_partial["status"], "llm_error": llm_partial["error"], "tasks": len(tasks_first)},
    "detail": {"classes": len(detail["result"].get("classes") or []), "question_summary": detail["result"].get("question_performance", {}).get("summary"), "version": detail["task"]["data_version"]},
    "idempotent": {"after_second": count_after_second, "same": count_after_second == len(tasks_first)},
    "invalidation": {"version_changed": version_before != version_after, "stale_flags": sorted(set(stale_flags.values())), "after_third": count_after_third},
    "llm": {"status": llm_task["task"]["status"], "observations": llm_result.get("llm", {}).get("class_observations"), "digest_has_numbers": bool(llm_result.get("evidence_digest"))},
}, ensure_ascii=False, default=str))
