"""P1 回归：评分点→知识点多对多映射、权重归一化、确认历史、方法映射与统计。"""
import json
import os

os.environ.setdefault("DEEPSEEK_API_KEY", "test-key-not-real")

from tests.support_import import answer, make_files, question
from xueban.api import xueban_teacher as t
from xueban.services import learning_analysis as la
from xueban.services import xueban_store as db

USER = {"id": "teacher", "role": "teacher"}


class FakeBackground:
    def add_task(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


def grader(answer, question, steps, key, provider, standard, textbook_context=""):
    qid = question.get("question_id")
    return {"score": 3, "max_score": 5, "is_correct": "partial", "feedback": "", "judgment_reason": "部分正确", "needs_review": False,
            "step_results": [{"step_index": 1, "reasoning_step_id": "rs-1", "source_step_id": "src-1", "student_step": "步骤", "is_correct": False, "score": 4, "max_score": 5, "error_type": "计算操作错误", "judgment_reason": "计算错误", "evidence_text": "步骤",
                              "scoring_point_links": [{"scoring_point_id": f"{qid}P1", "awarded_score": 4, "max_score": 5}],
                              "operation_units": [{"operation_id": "op1", "source_step_id": "src-1", "student_fragment": "步骤", "operation_type": "计算", "score": 4, "max_score": 5, "is_correct": False, "judgment_reason": "计算错误", "evidence_text": "步骤", "scoring_point_links": [{"scoring_point_id": f"{qid}P1", "awarded_score": 4, "max_score": 5}]}]}]}


t.init()
la.ensure_model()
batch = t.upload_import(make_files(
    [answer(event="E1", student_code="S01", qid="Q001"), answer(event="E2", student_code="S01", qid="Q002")],
    questions=[question(qid="Q001", indicator="I01", scoring_points="Q001P1 5分"), question(qid="Q002", indicator="I02", scoring_points="Q002P1 5分")],
), USER)
batch_id = batch["batch_id"]
t.confirm_preprocessing(batch_id, USER)
t._LLM_CALLER = grader
t.batch(FakeBackground(), USER, {"batch_id": batch_id})

# 第二知识点：把 Q001P1 同时映射到 I01 与 I02（权重 3:1）
db.x("INSERT OR IGNORE INTO teacher_topics (id,code,title,goal,course_id,standard_id,created_at) VALUES (?,?,?,?,?,?,?)", ("topic_T02", "T02", "主题二", "", "", "", t.now()))
db.x("INSERT OR IGNORE INTO teacher_indicators (id,code,topic_id,description) VALUES (?,?,?,?)", ("indicator_I02", "I02", "topic_T02", "知识点二"))
mapping = la.mapping_for_batch(batch_id)
sp = next(item for item in mapping["items"] if item["code"] == "Q001P1")
before_status = sp["mapping_status"]
set_result = la.set_knowledge_links(sp["scoring_point_id"], [
    {"knowledge_point_id": "kp:I01", "weight": 3},
    {"knowledge_point_id": "kp:I02", "weight": 1},
], USER["id"])
confirmed = next(item for item in set_result["items"] if item["code"] == "Q001P1")
history = db.q("SELECT * FROM scoring_point_link_history WHERE scoring_point_id=?", (sp["scoring_point_id"],))

# 方法映射：两个方法 3:1
method_a = t.create_method({"code": "M1", "name": "代入消元法", "description": "先代入再求解"}, USER)["method"]
method_b = t.create_method({"code": "M2", "name": "加减消元法", "description": "先加减再求解"}, USER)["method"]
la.set_method_links(sp["scoring_point_id"], [{"method_id": method_a["id"], "weight": 3}, {"method_id": method_b["id"], "weight": 1}], USER["id"])
audit = la.mapping_audit(batch_id)

profile = la.student_profile(batch_id, "student_S01")
q1_evidence = [item for item in profile["evidence"] if item["question_id"] == "Q001"]
method_stats = profile["methods"]
method_links = db.q("SELECT * FROM scoring_point_method_links WHERE scoring_point_id=?", (sp["scoring_point_id"],))

# 评分点人工确认历史：连续确认两次
grade = db.q1("SELECT * FROM teacher_grade_runs WHERE answer_id LIKE ? ORDER BY created_at DESC LIMIT 1", ("%E1%",))
first = t.confirm_scoring_points(grade["id"], USER, {"items": [{"scoring_point_id": "Q001P1", "score": 4}]})
second = t.confirm_scoring_points(grade["id"], USER, {"items": [{"scoring_point_id": "Q001P1", "score": 3}]})
confirm_history = db.q("SELECT * FROM teacher_scoring_point_confirmation_history WHERE grade_run_id=? ORDER BY created_at", (grade["id"],))

print(json.dumps({
    "before_status": before_status,
    "confirmed_status": confirmed["mapping_status"],
    "weights": {link["knowledge_point_id"]: link["weight"] for link in confirmed["knowledge_links"]},
    "weight_sum": round(sum(link["weight"] for link in confirmed["knowledge_links"]), 6),
    "history_count": len(history),
    "history_operator": history[0]["operator_id"] if history else None,
    "audit": {"confirmed": audit["confirmed_scoring_points"], "legacy": audit["legacy_scoring_points"], "illegal": audit["illegal_weight_groups"], "methods": audit["methods"], "with_methods": audit["scoring_points_with_methods"]},
    "evidence_weights": {item["knowledge_point_id"]: item["weight"] for item in q1_evidence},
    "evidence_mapping_status": sorted({item["mapping_status"] for item in q1_evidence}),
    "methods": [{"name": item["method_name"], "status": item["status"], "weight": item["effective_evidence_weight"]} for item in method_stats],
    "method_link_count": len([link for link in method_links if link["status"] != "disabled"]),
    "confirm_history": [{"version": row["version"], "before": json.loads(row["before_json"] or "{}"), "after": json.loads(row["after_json"] or "{}")} for row in confirm_history],
    "confirm_versions": [first["items"][0]["version"], second["items"][0]["version"]] if first.get("items") and second.get("items") else [],
}, ensure_ascii=False))
