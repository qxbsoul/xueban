"""学情证据链：未映射评分点不稀释、人工修正证据、多任务重新估计、逐级追溯。"""
import json

from xueban.api import xueban_teacher as t
from xueban.services import learning_analysis as analysis
from xueban.services import xueban_store as db

t.init()
NOW = "2026-01-01T00:00:00"
A, B = "batch_ea", "batch_eb"
db.x("INSERT INTO teacher_indicators (id,code,topic_id,description) VALUES (?,?,?,?)", ("indicator_i01", "I01", "topic_x", "知识点1"))
db.x("INSERT INTO teacher_topics (id,code,title,created_at) VALUES (?,?,?,?)", ("topic_x", "T01", "主题", NOW))
db.x("INSERT INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", ("class_e", "EC", "证据班", 7, NOW))
db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", ("stu_e", "user_e", "E01", "class_e", "E01", 7, NOW))

QUESTIONS = ((A, "Q001", "Q001P1 1分 Q001P2 1分", 2), (B, "Q002", "Q002P1 1分", 1))
for batch, qid, points, max_score in QUESTIONS:
    db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", (batch, f"{batch}.csv", "committed", "{}", "[]", "confirmed", NOW))
    db.x("INSERT INTO teacher_questions (id,topic_id,stem,reference_answer,max_score,scoring_points,indicator_id,question_type) VALUES (?,?,?,?,?,?,?,?)", (qid, "topic_x", f"{qid} 题干", "答案", max_score, points, "I01", "解答题"))
    db.x("INSERT INTO teacher_question_versions (version_id,batch_id,question_id,topic_id,stem,reference_answer,max_score,scoring_points,indicator_id,question_type,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (f"qv:{batch}:{qid}", batch, qid, "topic_x", f"{qid} 题干", "答案", max_score, points, "I01", "解答题", NOW))
    db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,grading_status,import_batch_id,max_score,original_score,solution_process,original_answer,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
         (f"ans_{batch}", "stu_e", qid, f"E_{batch}", 1, "graded", batch, max_score, 1, "1. 步骤", "答案", f"qv:{batch}:{qid}"))
analysis.ensure_model()

# Q001P2 没有可用的知识点映射（批次与全局都置为 disabled；ensure_model 不会覆盖已有行）：
# 验证「未映射的同题评分点不会稀释其他评分点权重」。
db.x("UPDATE scoring_point_knowledge_links SET status='disabled' WHERE scoring_point_id IN (SELECT id FROM scoring_points WHERE code='Q001P2')")


def insert_grade(grade_id, batch, qid, status, score, max_score, steps):
    db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,grading_prompt_version,grader_version,score,max_score,is_correct,step_results_json,needs_review,status,raw_response,created_at,question_version_id,structure_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (grade_id, f"ans_{batch}", "stu_e", qid, f"E_{batch}", 1, 1, "deepseek-chat", "teacher-grade-v4", "v1.5.13", score, max_score, "partial", json.dumps(steps, ensure_ascii=False), 0, status, "{}", NOW, f"qv:{batch}:{qid}", "{}"))


steps_a = [{"step_index": 1, "source_step_id": "src-1", "student_step": "步骤", "is_correct": True, "score": 2, "max_score": 2, "error_type": "", "judgment_reason": "对",
            "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 1, "max_score": 1}, {"scoring_point_id": "Q001P2", "awarded_score": 1, "max_score": 1}]}]
insert_grade("g_a", A, "Q001", "graded", 2, 2, steps_a)

steps_b = [{"step_index": 1, "source_step_id": "src-1", "student_step": "步骤", "is_correct": False, "score": 1, "max_score": 1, "error_type": "计算操作错误", "judgment_reason": "部分",
            "scoring_point_links": [{"scoring_point_id": "Q002P1", "awarded_score": 1, "max_score": 1}]}]
insert_grade("g_b", B, "Q002", "manual_corrected", 1, 1, steps_b)

evidence_a = analysis._evidence(A)[0]
trace_a = analysis.evidence_trace(A)
profile_a = analysis.student_profile(A, "stu_e")
evidence_b_before = analysis._evidence(B)[0]

db.x("INSERT INTO teacher_scoring_point_confirmations (id,grade_run_id,answer_id,scoring_point_id,original_ai_score,confirmed_score,max_score,confirmation_status,teacher_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
     ("conf_b", "g_b", f"ans_{B}", "Q002P1", 1, 1, 1, "confirmed", "teacher", NOW, NOW))
evidence_b_after = analysis._evidence(B)[0]
profile_multi = analysis.student_profile_multi([A, B], "stu_e")

print(json.dumps({
    "unmapped_sibling": {"rows": len(evidence_a), "points": [item["scoring_point_id"] for item in evidence_a], "weights": [item["weight"] for item in evidence_a], "base": [item["base_weight"] for item in evidence_a]},
    "trace_a": {"stages": {stage["stage"]: stage["count"] for stage in trace_a["stages"]}, "excluded": trace_a["excluded_by_stage"]},
    "profile_a": {"mastery": [{"name": item["knowledge_point_name"], "status": item["status"], "weight": item["effective_evidence_weight"]} for item in profile_a["mastery"]]},
    "manual_before": {"rows": len(evidence_b_before)},
    "manual_after": {"rows": len(evidence_b_after), "source": [item["judgment_source"] for item in evidence_b_after], "ratio": [item["score_ratio"] for item in evidence_b_after]},
    "multi": {"mastery": [{"name": item["knowledge_point_name"], "status": item["status"], "estimate": item["mastery_estimate"], "weight": item["effective_evidence_weight"], "covered": item["covered_question_count"], "opportunity": item["opportunity_question_count"], "rows": item["evidence_count"]} for item in profile_multi["mastery"]], "batches": len(profile_multi["batches"]), "coverage": profile_multi["coverage"]},
}, ensure_ascii=False))
