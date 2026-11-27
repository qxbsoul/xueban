"""证据聚合：同一题多评分点映射同一知识点只算一个独立单元；修订只取当前版本；低置信度错误标签不入统计。"""
import json
import os

os.environ.setdefault("XUEBAN_MIN_EVIDENCE", "0.3")
os.environ.setdefault("XUEBAN_MIN_QUESTIONS", "2")
os.environ.setdefault("XUEBAN_ERROR_MIN_CONFIDENCE", "0.5")

from xueban.api import xueban_teacher as t
from xueban.services import learning_analysis as la
from xueban.services import xueban_store as db

t.init()
la.ensure_model()
NOW = "2026-01-01T00:00:00"
B = "batch_agg"
db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", (B, "agg.csv", "committed", "{}", "[]", "confirmed", NOW))
db.x("INSERT INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", ("class_ag", "AG", "聚合班", 7, NOW))
db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", ("stu_ag", "user_ag", "AG01", "class_ag", "AG01", 7, NOW))
db.x("INSERT INTO teacher_topics (id,code,title,created_at) VALUES (?,?,?,?)", ("topic_ag", "T99", "聚合主题", NOW))
db.x("INSERT INTO teacher_indicators (id,code,topic_id,description) VALUES (?,?,?,?)", ("indicator_ag", "I01", "topic_ag", "聚合知识点"))
QUESTIONS = (("Q001", "Q001P1 1分,Q001P2 1分", 2), ("Q002", "Q002P1 1分", 1), ("Q003", "Q003P1 1分", 1), ("Q004", "Q004P1 1分", 1), ("Q005", "Q005P1 1分", 1))
for qid, points, max_score in QUESTIONS:
    db.x("INSERT INTO teacher_questions (id,stem,reference_answer,max_score,scoring_points,indicator_id,question_type) VALUES (?,?,?,?,?,?,?)", (qid, "题干", "答案", max_score, points, "I01", "解答题"))
    db.x("INSERT INTO teacher_question_versions (version_id,batch_id,question_id,stem,reference_answer,max_score,scoring_points,indicator_id,question_type,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (f"qv:{B}:{qid}", B, qid, "题干", "答案", max_score, points, "I01", "解答题", NOW))
for sp, qid in (("Q001P1", "Q001"), ("Q001P2", "Q001"), ("Q002P1", "Q002"), ("Q003P1", "Q003"), ("Q004P1", "Q004"), ("Q005P1", "Q005")):
    db.x("INSERT INTO scoring_point_knowledge_links (id,scoring_point_id,knowledge_point_id,weight,source,confidence,status,batch_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (f"link:{sp}", sp, "kp:I01", 1.0, "teacher", 1.0, "confirmed", B, NOW, NOW))


def add_answer(aid, qid, revision, submit, points, confidence=0.9):
    max_score = next(item[2] for item in QUESTIONS if item[0] == qid)
    db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,grading_status,import_batch_id,max_score,original_score,submit_time,solution_process,original_answer,exam_node_id,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (aid, "stu_ag", qid, f"E-{qid}", revision, "graded", B, max_score, 0, submit, "步骤", "答案", "N01", f"qv:{B}:{qid}"))
    links = [{"scoring_point_id": sp, "awarded_score": score, "max_score": 1} for sp, score in points]
    steps = [{"step_index": 1, "source_step_id": "s1", "student_step": "步骤", "is_correct": False, "score": sum(score for _, score in points), "max_score": sum(1 for _ in points), "error_type": "计算操作错误", "confidence": confidence, "judgment_reason": "x", "scoring_point_links": links}]
    db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,status,needs_review,step_results_json,created_at,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (f"g-{aid}", aid, "stu_ag", qid, f"E-{qid}", revision, 1, "m", sum(score for _, score in points), sum(1 for _ in points), "graded", 0, json.dumps(steps, ensure_ascii=False), NOW, f"qv:{B}:{qid}"))


db.x("INSERT INTO teacher_exams (id,exam_date,topics,description,available_questions,course_id) VALUES (?,?,?,?,?,?)", ("N01", "2026-01-01", "", "", "Q001,Q002,Q003,Q004,Q005", ""))
# Q001 两次修订：r1 全错，r2 P1 对、P2 错；只应取 r2
add_answer("a1", "Q001", 1, "2026-01-01T09:00:00", [("Q001P1", 0), ("Q001P2", 0)])
add_answer("a2", "Q001", 2, "2026-01-01T09:05:00", [("Q001P1", 1), ("Q001P2", 0)])
profile_one = la.student_profile(B, "stu_ag")
point_one = next(item for item in profile_one["mastery"] if item["knowledge_point_id"] == "kp:I01")

add_answer("a3", "Q002", 1, "2026-01-01T10:00:00", [("Q002P1", 1)])
profile_two = la.student_profile(B, "stu_ag")
point_two = next(item for item in profile_two["mastery"] if item["knowledge_point_id"] == "kp:I01")

# 低置信度（0.2）错误标签不得进入确定性错误统计；0.9 的进入。
add_answer("a4", "Q003", 1, "2026-01-01T11:00:00", [("Q003P1", 0)], confidence=0.2)
add_answer("a5", "Q004", 1, "2026-01-01T12:00:00", [("Q004P1", 0)], confidence=0.9)
add_answer("a6", "Q005", 1, "2026-01-01T13:00:00", [("Q005P1", 0)], confidence=0.9)
profile_final = la.student_profile(B, "stu_ag")
point_final = next(item for item in profile_final["mastery"] if item["knowledge_point_id"] == "kp:I01")

evidence = [item for item in la._evidence(B)[0] if item["knowledge_point_id"] == "kp:I01"]
low_conf = [item for item in evidence if item.get("error_label_confidence") is not None and float(item["error_label_confidence"]) < 0.5]
error_types = {item["type"]: item for item in point_final["error_types"]}

print(json.dumps({
    "one_question": {
        "evidence_count": point_one["evidence_count"], "evidence_point_count": point_one["evidence_point_count"],
        "weight": point_one["effective_evidence_weight"], "status": point_one["status"],
        "covered_questions": point_one["covered_question_count"], "mastery": point_one["mastery_estimate"],
    },
    "two_questions": {
        "evidence_count": point_two["evidence_count"], "evidence_point_count": point_two["evidence_point_count"],
        "weight": point_two["effective_evidence_weight"], "status": point_two["status"],
        "covered_questions": point_two["covered_question_count"], "mastery": point_two["mastery_estimate"],
    },
    "evidence_rows": len(evidence),
    "low_confidence": {
        "rows": len(low_conf),
        "labels": sorted({item["error_type"] for item in low_conf}),
        "raw_saved": all(item.get("error_label_raw") for item in low_conf),
    },
    "error_frequency": {
        "types": sorted(error_types),
        "calculation": error_types.get("计算操作错误"),
    },
}, ensure_ascii=False, default=str))
