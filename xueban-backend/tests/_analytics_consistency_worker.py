"""学情分析一致性：任意班级比较、错误标签幂等、知识依赖完整性、跨任务隔离。"""
import json
import os

# 该夹具只有 1 道题、3 个评分点：把证据门槛降到 0.3，专门验证「比较/聚合」逻辑本身；
# 真实数据的默认门槛（权重 2、2 道题）已在 V1.5.16 真实回归里单独验证。
os.environ.setdefault("XUEBAN_MIN_EVIDENCE", "0.3")
os.environ.setdefault("XUEBAN_MIN_QUESTIONS", "1")

from xueban.api import xueban_teacher as t
from xueban.services import learning_analysis as la
from xueban.services import xueban_store as db

t.init()
NOW = "2026-01-01T00:00:00"
B1, B2 = "batch_c1", "batch_c2"
CLASSES = (("class_c1", "C1", "stu_c1"), ("class_c2", "C2", "stu_c2"), ("class_c3", "C3", "stu_c3"))
for topic_id, code in (("topic_1", "I01"), ("topic_2", "I02"), ("topic_3", "I03")):
    db.x("INSERT INTO teacher_topics (id,code,title,created_at) VALUES (?,?,?,?)", (topic_id, topic_id.upper(), topic_id, NOW))
    db.x("INSERT INTO teacher_indicators (id,code,topic_id,description) VALUES (?,?,?,?)", (f"indicator_{code.lower()}", code, topic_id, f"知识点{code}"))
for batch in (B1, B2):
    db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", (batch, f"{batch}.csv", "committed", "{}", "[]", "confirmed", NOW))
    db.x("INSERT INTO teacher_questions (id,topic_id,stem,reference_answer,max_score,scoring_points,indicator_id,question_type) VALUES (?,?,?,?,?,?,?,?)", (f"{batch}-Q001", "topic_1", "题干", "答案", 3, "Q001P1 1分 Q001P2 1分 Q001P3 1分", "I01", "解答题"))
    db.x("INSERT INTO teacher_question_versions (version_id,batch_id,question_id,topic_id,stem,reference_answer,max_score,scoring_points,indicator_id,question_type,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (f"qv:{batch}:Q001", batch, "Q001", "topic_1", "题干", "答案", 3, "Q001P1 1分 Q001P2 1分 Q001P3 1分", "I01", "解答题", NOW))
for index, (class_id, code, student_id) in enumerate(CLASSES):
    db.x("INSERT INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", (class_id, code, f"班{code}", 7, NOW))
    db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", (student_id, f"user_{code}", code, class_id, code, 7, NOW))
    for batch in (B1, B2):
        answer_id = f"{batch}:{student_id}:Q001"
        db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,grading_status,import_batch_id,max_score,original_score,solution_process,original_answer,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
             (answer_id, student_id, "Q001", f"E-{student_id}-{batch}", 1, "graded", batch, 3, 1, "1. 步骤", "答案", f"qv:{batch}:Q001"))
la.ensure_model()

# 让三个评分点分别映射到三个知识点，形成可比较的节点
for code, kp in (("Q001P2", "kp:I02"), ("Q001P3", "kp:I03")):
    db.x("UPDATE scoring_point_knowledge_links SET knowledge_point_id=? WHERE scoring_point_id IN (SELECT id FROM scoring_points WHERE code=? AND batch_id=?)", (kp, code, B1))

def steps(score, error):
    # 三个评分点各 1 分，按得分依次给分，保证「评分点合计 == 总分」且不超分。
    return [{"step_index": 1, "source_step_id": "src-1", "student_step": "步骤", "is_correct": error is not None, "score": score, "max_score": 3, "error_type": error or "", "judgment_reason": "部分正确" if error else "正确",
             "scoring_point_links": [
                 {"scoring_point_id": "Q001P1", "awarded_score": 1, "max_score": 1},
                 {"scoring_point_id": "Q001P2", "awarded_score": 1 if score >= 2 else 0, "max_score": 1},
                 {"scoring_point_id": "Q001P3", "awarded_score": 1 if score >= 3 else 0, "max_score": 1},
             ]}]
for index, (class_id, code, student_id) in enumerate(CLASSES):
    for batch in (B1, B2):
        answer_id = f"{batch}:{student_id}:Q001"
        db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,status,needs_review,step_results_json,created_at,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (f"g-{batch}-{student_id}", answer_id, student_id, "Q001", f"E-{student_id}-{batch}", 1, 1, "m", index + 1, 3, "graded", 0, json.dumps(steps(index + 1, "概念理解错误" if index == 0 else None), ensure_ascii=False), NOW, f"qv:{batch}:Q001"))
# 满分作答：分类应判定无错误
db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,status,needs_review,step_results_json,created_at,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
     ("g-full", f"{B1}:stu_c1:Q001-full", "stu_c1", "Q001", "E-full", 1, 1, "m", 3, 3, "graded", 0, json.dumps([{"step_index": 1, "source_step_id": "src-1", "student_step": "步骤", "is_correct": True, "score": 3, "max_score": 3, "error_type": "", "judgment_reason": "全对", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 1, "max_score": 1}]}], ensure_ascii=False), NOW, f"qv:{B1}:Q001"))
db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,grading_status,import_batch_id,max_score,original_score,solution_process,original_answer,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
     (f"{B1}:stu_c1:Q001-full", "stu_c1", "Q001", "E-full", 1, "graded", B1, 3, 3, "1. 步骤", "答案", f"qv:{B1}:Q001"))

# 1) 任意班级组合比较
compare_all = la.compare_classes_detailed(B1, [item[0] for item in CLASSES])
compare_two = la.compare_classes_detailed(B1, [CLASSES[0][0], CLASSES[2][0]])
compare_single = la.compare_classes_detailed(B1, [CLASSES[1][0]])

# 2) 错误标签幂等
first = la.classify_answer_errors(f"{B1}:stu_c1:Q001", operator_id="teacher")
rows_after_first = db.q1("SELECT COUNT(*) n FROM answer_error_labels WHERE answer_id=?", (f"{B1}:stu_c1:Q001",))["n"]
second = la.classify_answer_errors(f"{B1}:stu_c1:Q001", operator_id="teacher")
rows_after_second = db.q1("SELECT COUNT(*) n FROM answer_error_labels WHERE answer_id=?", (f"{B1}:stu_c1:Q001",))["n"]
stored = json.loads(db.q1("SELECT labels_json FROM answer_error_labels WHERE answer_id=?", (f"{B1}:stu_c1:Q001",))["labels_json"])
clean = la.classify_answer_errors(f"{B1}:stu_c1:Q001-full", operator_id="teacher")
clean_labels = db.q1("SELECT COUNT(*) n FROM answer_error_labels WHERE answer_id=?", (f"{B1}:stu_c1:Q001-full",))["n"]

# 3) 知识依赖图完整性
graph_before = la.knowledge_graph(B1)
edge_1 = la.add_knowledge_edge(B1, "kp:I01", "kp:I02", "teacher", "confirmed", "teacher")
edge_2 = la.add_knowledge_edge(B1, "kp:I02", "kp:I03", "teacher", "confirmed", "teacher")
errors = {}
for label, kwargs in (
    ("self", ("kp:I01", "kp:I01")),
    ("cycle", ("kp:I03", "kp:I01")),
    ("duplicate", ("kp:I01", "kp:I02")),
    ("unknown", ("kp:I01", "kp:I99")),
):
    try:
        la.add_knowledge_edge(B1, kwargs[0], kwargs[1], "teacher", "confirmed", "teacher")
        errors[label] = None
    except ValueError as exc:
        errors[label] = str(exc)
deps = la.knowledge_dependencies(B1, "kp:I03")
graph_after = la.knowledge_graph(B1)
deleted = la.delete_knowledge_edge(edge_1["id"])
graph_final = la.knowledge_graph(B1)

# 4) 跨任务隔离
single = la.student_profile(B1, "stu_c1")
multi = la.student_profile_multi([B1, B2], "stu_c1")

print(json.dumps({
    "compare": {
        "all_classes": len(compare_all.get("classes") or []),
        "all_kp_rows": len(compare_all.get("knowledge_points") or []),
        "two_classes": [item["class_code"] for item in (compare_two.get("classes") or [])],
        "single_class": [item["class_code"] for item in (compare_single.get("classes") or [])],
        "first_row": (compare_two.get("knowledge_points") or [None])[0],
    },
    "labels": {
        "first": sorted(first.get("labels") or []),
        "second": sorted(second.get("labels") or []),
        "rows_first": rows_after_first, "rows_second": rows_after_second,
        "stored": sorted(stored),
        "clean_no_error": bool(clean.get("no_error")), "clean_rows": clean_labels,
    },
    "graph": {
        "nodes_before": graph_before["summary"]["nodes"],
        "nodes": [node["knowledge_point_id"] for node in graph_after["nodes"]],
        "edges": len(graph_after["edges"]),
        "confirmed": graph_after["summary"]["confirmed"],
        "errors": errors,
        "direct": [item["knowledge_point_id"] for item in deps.get("direct_prerequisites") or []], "transitive": [item["knowledge_point_id"] for item in deps.get("transitive_prerequisites") or []],
        "after_delete": graph_final["summary"]["edges"],
        "deleted": deleted,
    },
    "isolation": {
        "single_tasks": sorted({item["task_id"] for item in single.get("evidence") or []}),
        "multi_tasks": sorted({item["task_id"] for item in multi.get("evidence") or []}),
        "single_rows": len(single.get("evidence") or []), "multi_rows": len(multi.get("evidence") or []),
    },
}, ensure_ascii=False, default=str))
