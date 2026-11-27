"""判题结果变化 → 学情分析自动更新：对象级证据版本、去重、失效、失败不覆盖、人工修正保留历史。"""
import json
import os

os.environ.setdefault("XUEBAN_MIN_EVIDENCE", "0.3")
os.environ.setdefault("XUEBAN_MIN_QUESTIONS", "1")

from xueban.api import xueban_teacher as t
from xueban.services import analysis_pipeline as pipeline
from xueban.services import learning_analysis as la
from xueban.services import xueban_store as db

t.init()
la.ensure_model()
NOW = "2026-01-01T00:00:00"
B = "batch_upd"
db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", (B, "upd.csv", "committed", "{}", "[]", "confirmed", NOW))
db.x("INSERT INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", ("class_u", "U1", "更新班", 7, NOW))
db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", ("stu_u1", "u1", "U01", "class_u", "U01", 7, NOW))
db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", ("stu_u2", "u2", "U02", "class_u", "U02", 7, NOW))
db.x("INSERT INTO teacher_topics (id,code,title,created_at) VALUES (?,?,?,?)", ("topic_u", "T01", "更新主题", NOW))
db.x("INSERT INTO teacher_indicators (id,code,topic_id,description) VALUES (?,?,?,?)", ("indicator_u", "I01", "topic_u", "更新知识点"))
db.x("INSERT INTO teacher_questions (id,stem,reference_answer,max_score,scoring_points,question_type) VALUES (?,?,?,?,?,?)", ("Q001", "1+1=?", "2", 2, "Q001P1 2分", "解答题"))
db.x("INSERT INTO teacher_question_versions (version_id,batch_id,question_id,stem,reference_answer,max_score,scoring_points,question_type,created_at) VALUES (?,?,?,?,?,?,?,?,?)", (f"qv:{B}:Q001", B, "Q001", "1+1=?", "2", 2, "Q001P1 2分", "解答题", NOW))
db.x("INSERT INTO scoring_points (id,code,batch_id,name,description,created_at) VALUES (?,?,?,?,?,?)", ("sp_u1", "Q001P1", B, "评分点1", "", NOW))
db.x("INSERT INTO scoring_point_knowledge_links (id,scoring_point_id,knowledge_point_id,weight,source,confidence,status,batch_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", ("link_u", "sp_u1", "kp:I01", 1.0, "teacher", 1.0, "confirmed", B, NOW, NOW))
for sid in ("stu_u1", "stu_u2"):
    db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,grading_status,import_batch_id,max_score,original_score,solution_process,original_answer,exam_node_id,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (f"ans_{sid}", sid, "Q001", f"E-{sid}", 1, "graded", B, 2, 1, "1+1", "1+1=2", "N01", f"qv:{B}:Q001"))


def add_grade(answer_id, version, score, status="graded", raw=None, needs_review=0):
    steps = [{"step_index": 1, "source_step_id": "src-1", "student_step": "1+1", "is_correct": score >= 2, "score": score, "max_score": 2, "error_type": "" if score >= 2 else "计算操作错误", "judgment_reason": "x", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": score, "max_score": 2}], "operation_units": []}]
    grade_id = f"g_{answer_id}_{version}"
    db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,status,needs_review,step_results_json,raw_response,created_at,question_version_id,structure_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (grade_id, answer_id, "stu_u1" if answer_id.endswith("u1") else "stu_u2", "Q001", "E", 1, version, "m", score, 2, status, needs_review, json.dumps(steps, ensure_ascii=False), raw or json.dumps({"score": score}), NOW, f"qv:{B}:Q001", "{}"))
    return grade_id


add_grade("ans_stu_u1", 1, 1)
add_grade("ans_stu_u2", 1, 2)
db.x("UPDATE teacher_answers SET grading_status='graded' WHERE import_batch_id=?", (B,))


def fake_llm(prompt, key):
    return {"class_observations": [{"class_code": "U1", "summary": "ok", "evidence_refs": ["kp:I01"]}], "error_explanations": [], "teaching_suggestions": [{"class_code": "U1", "suggestion": "x", "evidence_refs": ["kp:I01"], "confidence": 0.8}]}


pipeline._llm_key = lambda: "test-key"
pipeline._call_llm = fake_llm

# v1：产生事件并处理
pipeline.record_grade_event("ans_stu_u1")
first = pipeline.process_events(B, run_inline=True)
tasks_first = db.q("SELECT * FROM teacher_analysis_tasks WHERE batch_id=? AND kind='llm' ORDER BY created_at", (B,))
student_task = next((row for row in tasks_first if row["scope"] == "student"), {})
class_task = next((row for row in tasks_first if row["scope"] == "class"), {})
student_result = json.loads(student_task.get("result_json") or "{}")
mastery_v1 = {point["knowledge_point_id"]: point["mean_mastery"] for point in la.class_profile(B, "class_u")["knowledge_points"]}

# 重复事件：不应新建任务
pipeline.record_grade_event("ans_stu_u1")
second = pipeline.process_events(B, run_inline=True)
count_after_duplicate = db.q1("SELECT COUNT(*) n FROM teacher_analysis_tasks WHERE batch_id=? AND kind='llm'", (B,))["n"]

# 判题变化：v2 满分
add_grade("ans_stu_u1", 2, 2)
db.x("UPDATE teacher_answers SET grading_status='graded' WHERE id='ans_stu_u1'")
pipeline.record_grade_event("ans_stu_u1")
third = pipeline.process_events(B, run_inline=True)
tasks_after = db.q("SELECT * FROM teacher_analysis_tasks WHERE batch_id=? AND kind='llm' ORDER BY created_at", (B,))
new_student_tasks = [row for row in tasks_after if row["scope"] == "student"]
old_task = next((row for row in new_student_tasks if row["evidence_version"] != pipeline.evidence_version(B, "student", "stu_u1")), None)
latest = pipeline.latest_valid_llm(B, "student", "stu_u1")
mastery_v2 = {point["knowledge_point_id"]: point["mean_mastery"] for point in la.class_profile(B, "class_u")["knowledge_points"]}

# LLM 失败：新版本 partial，不能覆盖上一个有效结果
def broken_llm(prompt, key):
    raise RuntimeError("model down")


pipeline._call_llm = broken_llm
add_grade("ans_stu_u1", 3, 1)
db.x("UPDATE teacher_answers SET grading_status='graded' WHERE id='ans_stu_u1'")
pipeline.record_grade_event("ans_stu_u1")
fourth = pipeline.process_events(B, run_inline=True)
latest_after_failure = pipeline.latest_valid_llm(B, "student", "stu_u1")
failed_tasks = [row for row in db.q("SELECT status,error FROM teacher_analysis_tasks WHERE batch_id=? AND kind='llm' AND scope='student' ORDER BY created_at", (B,)) if row["status"] in ("partial", "failed")]

# 人工修正：保留 AI 原始记录，当前有效分数与统计更新
pipeline._call_llm = fake_llm
ai_row = db.q1("SELECT * FROM teacher_grade_runs WHERE answer_id='ans_stu_u1' ORDER BY grading_version DESC LIMIT 1")
ai_score = ai_row["score"]; ai_raw = ai_row["raw_response"]
manual_id = "manual_u1"
db.x("UPDATE teacher_grade_runs SET status='manual_corrected', score=0.5, needs_review=0 WHERE id=?", (ai_row["id"],))
db.x("INSERT INTO teacher_scoring_point_confirmations (id,grade_run_id,answer_id,scoring_point_id,original_ai_score,confirmed_score,max_score,confirmation_status,teacher_id,scoring_rule_version,version,source,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
     ("spc_u1", ai_row["id"], "ans_stu_u1", "Q001P1", ai_score, 0.5, 2, "confirmed", "teacher_1", "scoring_points", 1, "teacher", NOW, NOW))
db.x("INSERT INTO teacher_corrections (id,grade_run_id,before_json,after_json,note,teacher_id,created_at) VALUES (?,?,?,?,?,?,?)", ("corr_u1", ai_row["id"], "{}", "{\"score\":0.5}", "人工修正", "teacher_1", NOW))
analysis_after_manual = la.student_profile(B, "stu_u1")
manual_point = next((point for point in analysis_after_manual["mastery"] if point["knowledge_point_id"] == "kp:I01"), {})
row_after = db.q1("SELECT score, status, raw_response, grading_version FROM teacher_grade_runs WHERE id=?", (ai_row["id"],))

print(json.dumps({
    "first": {"events": first["events"], "tasks": first["objects"], "statuses": sorted({row["status"] for row in tasks_first}), "student_status": student_task.get("status"), "result_keys": sorted(student_result.keys()), "evidence_version": student_task.get("evidence_version"), "prompt_version": student_task.get("prompt_version"), "model": student_task.get("model")},
    "duplicate": {"llm_tasks": count_after_duplicate, "events": second["events"]},
    "changed": {"new_tasks": third["objects"], "total_llm_tasks": len(tasks_after), "old_superseded": (old_task or {}).get("superseded"), "latest_status": (latest or {}).get("task", {}).get("status"), "mastery_v1": mastery_v1, "mastery_v2": mastery_v2},
    "failure": {"latest_still_valid": bool(latest_after_failure and latest_after_failure["result"].get("llm")), "failed_count": len(failed_tasks), "failed_status": failed_tasks[0]["status"] if failed_tasks else None},
    "debug_evidence": {"rows": len(la._evidence(B, trace={})[0]), "mapping": la._knowledge_links("Q001P1", B), "profile": la.student_profile(B, "stu_u1").get("mastery")},
    "manual": {"ai_score_preserved": ai_score, "ai_raw_preserved": bool(ai_raw), "row_after": {"score": row_after["score"], "status": row_after["status"]}, "knowledge_mastery": manual_point.get("mastery_estimate"), "evidence_count": manual_point.get("evidence_count"), "grading_version": row_after["grading_version"]},
}, ensure_ascii=False, default=str))
