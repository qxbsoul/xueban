"""班级分布、班级比较、学生跨任务可比性。"""
import json
import os

os.environ.setdefault("XUEBAN_MIN_EVIDENCE", "0.3")
os.environ.setdefault("XUEBAN_MIN_QUESTIONS", "1")

from xueban.api import xueban_teacher as t
from xueban.services import learning_analysis as la
from xueban.services import xueban_store as db

t.init()
NOW = "2026-01-01T00:00:00"
B1, B2 = "batch_cb1", "batch_cb2"
db.x("INSERT INTO teacher_topics (id,code,title,created_at) VALUES (?,?,?,?)", ("topic_x", "T01", "主题", NOW))
db.x("INSERT INTO teacher_indicators (id,code,topic_id,description) VALUES (?,?,?,?)", ("indicator_i01", "I01", "topic_x", "知识点1"))
for batch in (B1, B2):
    db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", (batch, f"{batch}.csv", "committed", "{}", "[]", "confirmed", NOW))
    max_score = 1 if batch == B1 else 2
    db.x("INSERT INTO teacher_exams (id,exam_date,topics,description,available_questions,course_id) VALUES (?,?,?,?,?,?)", (f"{batch}-N01", "2026-01-01", "", "", "Q001", ""))
    db.x("INSERT INTO teacher_questions (id,stem,reference_answer,max_score,scoring_points,indicator_id,question_type) VALUES (?,?,?,?,?,?,?)", (f"{batch}-Q001", "题干", "答案", max_score, "Q001P1 1分" if batch == B1 else "Q001P1 2分", "I01", "填空题"))
    db.x("INSERT INTO teacher_question_versions (version_id,batch_id,question_id,stem,reference_answer,max_score,scoring_points,indicator_id,question_type,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (f"qv:{batch}:Q001", batch, "Q001", "题干", "答案", max_score, "Q001P1 1分" if batch == B1 else "Q001P1 2分", "I01", "填空题", NOW))
for cls, code in (("class_a", "A"), ("class_b", "B")):
    db.x("INSERT INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", (cls, code, f"班{code}", 7, NOW))
STUDENTS = (("s_a1", "class_a"), ("s_a2", "class_a"), ("s_a3", "class_a"), ("s_a4", "class_a"), ("s_b1", "class_b"), ("s_b2", "class_b"), ("s_b3", "class_b"))
for sid, cls in STUDENTS:
    db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", (sid, f"user_{sid}", sid.upper(), cls, sid, 7, NOW))


def add_answer(batch, sid, score, max_score, status="graded"):
    aid = f"{batch}:{sid}:Q001"
    db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,grading_status,import_batch_id,max_score,original_score,solution_process,original_answer,exam_node_id,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (aid, sid, "Q001", f"E-{sid}-{batch}", 1, status, batch, max_score, score, "步骤", "答案", f"{batch}-N01", f"qv:{batch}:Q001"))
    steps = [{"step_index": 1, "source_step_id": "src-1", "student_step": "步骤", "is_correct": score >= max_score, "score": score, "max_score": max_score, "error_type": "" if score >= max_score else "计算操作错误", "judgment_reason": "x", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": score, "max_score": max_score}]}]
    if status == "needs_review":
        steps = []
    db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,status,needs_review,step_results_json,created_at,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (f"g-{batch}-{sid}", aid, sid, "Q001", f"E-{sid}-{batch}", 1, 1, "m", score, max_score, status, 1 if status == "needs_review" else 0, json.dumps(steps, ensure_ascii=False), NOW, f"qv:{batch}:Q001"))


# B1：A1 满分、A2 半分、A3 零分、A4 无作答；B1 班三人满分/高分
add_answer(B1, "s_a1", 1, 1); add_answer(B1, "s_a2", 0.5, 1); add_answer(B1, "s_a3", 0, 1)
add_answer(B1, "s_b1", 1, 1); add_answer(B1, "s_b2", 1, 1); add_answer(B1, "s_b3", 0.5, 1)
# B2：题目满分不同（2 分），用于可比性拦截
add_answer(B2, "s_a1", 1, 2); add_answer(B2, "s_a2", 2, 2)

profile_a = la.class_profile(B1, "class_a")
profile_b = la.class_profile(B1, "class_b")
compare = la.compare_classes_detailed(B1, ["class_a", "class_b"])
kd = la.class_knowledge_detail(B1, "class_a", "kp:I01")
single = la.student_profile(B1, "s_a1")
multi = la.student_profile_multi([B1, B2], "s_a1")

first_kp = profile_a["knowledge_points"][0]
print(json.dumps({
    "class_a": {
        "students": profile_a["student_count"], "valid": profile_a["valid_student_count"],
        "status_counts": {item["status"]: item["count"] for item in first_kp["status_breakdown"]},
        "sum_check": first_kp["status_total_check"], "breakdown_sum": sum(item["count"] for item in first_kp["status_breakdown"]),
        "mean": first_kp["mean_mastery"], "valid_students": first_kp["valid_students"],
        "students_in_breakdown": sum(len(item["students"]) for item in first_kp["status_breakdown"]),
    },
    "compare": {
        "rows": len(compare["knowledge_points"]), "first": {key: compare["knowledge_points"][0][key] for key in ("difference", "pp_difference", "comparable", "sample_warning")},
        "sorted_desc": all((compare["knowledge_points"][i]["pp_difference"] or 0) >= (compare["knowledge_points"][i + 1]["pp_difference"] or 0) for i in range(len(compare["knowledge_points"]) - 1)),
        "advantages": len(compare["advantages"]), "common_weak": len(compare["common_weak"]), "insufficient": len(compare["insufficient"]),
        "thresholds": compare["thresholds"],
    },
    "kp_detail": {"questions": len(kd["questions"]), "scoring_points": len(kd["scoring_points"]), "evidence": kd["evidence_count"], "status_buckets": len(kd["knowledge_point"]["status_breakdown"])},
    "student_multi": {"single_evidence": len(single.get("evidence") or []), "multi_evidence": len(multi.get("evidence") or []), "included": multi["batch_ids"], "excluded": multi["excluded_batches"], "reason": multi["excluded_batches"][0]["reasons"][0] if multi.get("excluded_batches") else ""},
}, ensure_ascii=False, default=str))
