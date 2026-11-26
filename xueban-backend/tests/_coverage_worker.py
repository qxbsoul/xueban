"""D6 回归：覆盖率分母必须是该知识点的可作答题目数，不是全卷题目数。"""
import json

from xueban.api import xueban_teacher as t
from xueban.services import learning_analysis as la
from xueban.services import xueban_store as db

BATCH = "batch_coverage"
CLASS = "class_coverage"
NOW = "2026-01-01T00:00:00"
t.init()
la.ensure_model()

db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", (BATCH, "c.csv", "committed", "{}", "[]", "confirmed", NOW))
db.x("INSERT INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", (CLASS, "CC", "覆盖班", 7, NOW))
db.x("INSERT INTO teacher_topics (id,code,title,goal,course_id,standard_id,created_at) VALUES (?,?,?,?,?,?,?)", ("topic_c", "TC", "主题", "", "", "", NOW))
db.x("INSERT INTO teacher_indicators (id,code,topic_id,description) VALUES (?,?,?,?)", ("indicator_a", "IA", "topic_c", "知识点A"))
db.x("INSERT INTO teacher_indicators (id,code,topic_id,description) VALUES (?,?,?,?)", ("indicator_b", "IB", "topic_c", "知识点B"))
for code, sid in (("SA", "student_ca"), ("SB", "student_cb")):
    db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", (sid, f"user_{code}", code, CLASS, code, 7, NOW))

# 知识点 A 只有 2 道题；知识点 B 有 10 道题（模拟“单知识点只覆盖少量题目”）
questions = [("Q001", "IA"), ("Q002", "IA")] + [(f"Q{i:03d}", "IB") for i in range(3, 13)]
for qid, indicator in questions:
    db.x("INSERT INTO teacher_question_versions (version_id,batch_id,question_id,topic_id,stem,max_score,scoring_points,indicator_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)", (f"qv:{BATCH}:{qid}", BATCH, qid, "TC", f"题干{qid}", 2, f"{qid}P1 2分", indicator, NOW))

answered = {"SA": [qid for qid, _ in questions], "SB": ["Q001"]}
for code, sid in (("SA", "student_ca"), ("SB", "student_cb")):
    for qid in answered[code]:
        answer_id = f"{BATCH}:{code}:{qid}"
        db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,original_answer,solution_process,original_score,max_score,grading_status,import_batch_id,exam_node_id,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (answer_id, sid, qid, f"e:{code}:{qid}", 1, "2", "步骤", 2, 2, "graded", BATCH, "N01", f"qv:{BATCH}:{qid}"))
        steps = json.dumps([{"step_index": 1, "student_step": "步骤", "is_correct": True, "score": 2, "max_score": 2, "error_type": "", "judgment_reason": "ok", "scoring_point_links": [{"scoring_point_id": f"{qid}P1", "awarded_score": 2, "max_score": 2}]}], ensure_ascii=False)
        db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,is_correct,step_results_json,needs_review,status,created_at,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"g:{code}:{qid}", answer_id, sid, qid, f"e:{code}:{qid}", 1, 1, "deepseek-chat", 2, 2, "correct", steps, 0, "graded", NOW, f"qv:{BATCH}:{qid}"))

profiles = {code: la.student_profile(BATCH, sid) for code, sid in (("SA", "student_ca"), ("SB", "student_cb"))}


def pick(profile, kp_id):
    return next((point for point in profile["mastery"] if point["knowledge_point_id"] == kp_id), None)


payload = {
    "SA": {kp: {key: value for key, value in (pick(profiles["SA"], f"kp:{kp}") or {}).items() if key in ("status", "data_coverage", "covered_question_count", "opportunity_question_count", "evidence_count", "effective_evidence_weight")} for kp in ("IA", "IB")},
    "SB": {kp: {key: value for key, value in (pick(profiles["SB"], f"kp:{kp}") or {}).items() if key in ("status", "data_coverage", "covered_question_count", "opportunity_question_count", "evidence_count")} for kp in ("IA", "IB")},
    "parameters": profiles["SA"]["parameters"],
    "answer_count": db.q1("SELECT COUNT(*) n FROM teacher_answers")["n"],
    "beta_check": {p: round(la._betai(3.0, 5.0, la._beta_quantile(p, 3.0, 5.0)), 6) for p in (0.025, 0.5, 0.975)},
}
print(json.dumps(payload, ensure_ascii=False))
