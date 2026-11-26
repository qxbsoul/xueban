"""P2 回归：知识依赖图、错误分类、个性化推荐、多班对比（含无数据班级）。"""
import json

from xueban.api import xueban_teacher as t
from xueban.services import learning_analysis as la
from xueban.services import xueban_store as db

USER = {"id": "teacher", "role": "teacher"}
BATCH = "batch_extra"
NOW = "2026-01-01T00:00:00"
t.init()
la.ensure_model()

db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", (BATCH, "extra.csv", "committed", "{}", "[]", "confirmed", NOW))
db.x("INSERT INTO teacher_topics (id,code,title,goal,course_id,standard_id,created_at) VALUES (?,?,?,?,?,?,?)", ("topic_1", "T1", "主题一", "", "", "", "2026-01-01T00:00:01"))
db.x("INSERT INTO teacher_topics (id,code,title,goal,course_id,standard_id,created_at) VALUES (?,?,?,?,?,?,?)", ("topic_2", "T2", "主题二", "", "", "", "2026-01-01T00:00:02"))
db.x("INSERT INTO teacher_indicators (id,code,topic_id,description) VALUES (?,?,?,?)", ("ind_1", "I1", "topic_1", "知识点一"))
db.x("INSERT INTO teacher_indicators (id,code,topic_id,description) VALUES (?,?,?,?)", ("ind_2", "I2", "topic_2", "知识点二"))
for cls, code in (("class_a", "A1"), ("class_b", "B1")):
    db.x("INSERT INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", (cls, code, code, 7, NOW))
    db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", (f"student_{code}", f"user_{code}", code, cls, code, 7, NOW))

questions = [("Q001", "I1", "topic_1"), ("Q002", "I1", "topic_1"), ("Q003", "I2", "topic_2"), ("Q004", "I2", "topic_2")]
for qid, indicator, topic in questions:
    db.x("INSERT INTO teacher_question_versions (version_id,batch_id,question_id,topic_id,stem,max_score,scoring_points,indicator_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)", (f"qv:{BATCH}:{qid}", BATCH, qid, topic, f"题干{qid}", 5, f"{qid}P1 5分", indicator, NOW))

# A1 全部作答且全部判为错误（含计算/符号错误）；B1 未判题
for qid, indicator, topic in questions:
    answer_id = f"{BATCH}:A1:{qid}"
    db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,original_answer,solution_process,original_score,max_score,grading_status,import_batch_id,exam_node_id,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (answer_id, "student_A1", qid, f"e:A1:{qid}", 1, "错答", "第一步\n第二步", 0, 5, "graded", BATCH, "N01", f"qv:{BATCH}:{qid}"))
    steps = json.dumps([{"step_index": 1, "student_step": "第一步", "is_correct": False, "score": 0, "max_score": 5, "error_type": "计算操作错误", "judgment_reason": "符号写错且计算错误", "scoring_point_links": [{"scoring_point_id": f"{qid}P1", "awarded_score": 0, "max_score": 5}]}], ensure_ascii=False)
    db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,is_correct,step_results_json,needs_review,status,created_at,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"g:A1:{qid}", answer_id, "student_A1", qid, f"e:A1:{qid}", 1, 1, "deepseek-chat", 0, 5, "incorrect", steps, 0, "graded", NOW, f"qv:{BATCH}:{qid}"))
    b_answer = f"{BATCH}:B1:{qid}"
    db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,original_answer,solution_process,original_score,max_score,grading_status,import_batch_id,exam_node_id,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (b_answer, "student_B1", qid, f"e:B1:{qid}", 1, "答", "步骤", 0, 5, "pending", BATCH, "N01", f"qv:{BATCH}:{qid}"))

# 1) 依赖图
generated = la.generate_candidate_edges(BATCH, USER["id"])
graph_before = la.knowledge_graph(BATCH)
edge = la.add_knowledge_edge(BATCH, "kp:I1", "kp:I2", "teacher", "confirmed", USER["id"])
duplicate_error = cycle_error = ""
try:
    la.add_knowledge_edge(BATCH, "kp:I1", "kp:I2", "teacher", "confirmed", USER["id"])
except ValueError as exc:
    duplicate_error = str(exc)
try:
    la.add_knowledge_edge(BATCH, "kp:I2", "kp:I1", "teacher", "confirmed", USER["id"])
except ValueError as exc:
    cycle_error = str(exc)
dependencies = la.knowledge_dependencies(BATCH, "kp:I2")
gaps = la.student_prerequisite_gaps("student_A1", BATCH)

# 2) 错误分类
classify = la.classify_answer_errors(f"{BATCH}:A1:Q001", operator_id=USER["id"])
teacher_labels = la.set_error_labels(f"{BATCH}:A1:Q001", ["概念理解错误", "步骤遗漏"], "teacher", 1.0, "教师复核", USER["id"])
summary = la.error_label_summary(BATCH)

# 3) 推荐
rec = la.recommendations("student_A1", BATCH)

# 4) 多班对比
compare = la.compare_classes_detailed(BATCH, ["class_a", "class_b"])

print(json.dumps({
    "graph": {"generated": generated["created"], "before_edges": graph_before["summary"], "edge_status": edge["status"], "duplicate_error": duplicate_error, "cycle_error": cycle_error, "dependencies": [item["knowledge_point_id"] for item in dependencies["direct_prerequisites"]]},
    "gaps": [{"kp": item["knowledge_point_id"], "weak": [p["knowledge_point_id"] for p in item["weak_prerequisites"]]} for item in gaps["gaps"]],
    "classify": {"labels": [item["label"] for item in classify["labels"]], "source": classify["source"], "confidence": classify["confidence"], "prompt_version": classify["prompt_version"], "has_evidence": all(item["evidence"] for item in classify["labels"])},
    "teacher_labels": teacher_labels,
    "summary": summary["items"],
    "recommendations": [{"type": item["type"], "priority": item["priority"], "kp": item.get("knowledge_point_id"), "method": item.get("method_id"), "error": item.get("error_type"), "questions": item["suggested_questions"]} for item in rec["items"]],
    "compare": {"classes": [{k: row[k] for k in ("class_id", "valid_student_count", "overall_mastery", "completion_rate", "has_evidence")} for row in compare["classes"]], "kp_rows": len(compare["knowledge_points"]), "insufficient_values": sum(1 for row in compare["knowledge_points"] for value in row["values"] if value["insufficient"])},
}, ensure_ascii=False))
