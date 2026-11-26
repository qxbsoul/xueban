"""P2 验收：错误分类人工评价（precision/recall/F1）与推荐反馈闭环记录。"""
import json

from tests.support_import import answer, make_files, question, student
from xueban.api import xueban_teacher as t
from xueban.services import learning_analysis as la
from xueban.services import xueban_store as db

USER = {"id": "teacher-id", "role": "teacher"}
t.init()
la.ensure_model()
batch_id = t.upload_import(
    make_files(
        [answer(event="E1", student_code="F01", qid="Q001", score="2"), answer(event="E2", student_code="F01", qid="Q002", score="1")],
        questions=[question(qid="Q001", indicator="I01", scoring_points="Q001P1 5分"), question(qid="Q002", indicator="I01", scoring_points="Q002P1 5分")],
        students=[student("F01")],
    ),
    USER,
)["batch_id"]
student_row = db.q1("SELECT * FROM teacher_students WHERE code='F01'")
rows = db.q("SELECT * FROM teacher_answers WHERE import_batch_id=? ORDER BY question_id", (batch_id,))


def grade(answer_row, score, error_type):
    steps = json.dumps([{"step_index": 1, "student_step": "步骤", "is_correct": False, "score": score, "max_score": 5, "error_type": error_type, "judgment_reason": "测试", "scoring_point_links": [{"scoring_point_id": f"{answer_row['question_id']}P1", "awarded_score": score, "max_score": 5}]}], ensure_ascii=False)
    db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,is_correct,step_results_json,needs_review,status,created_at,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"g:{answer_row['id']}", answer_row["id"], answer_row["student_id"], answer_row["question_id"], answer_row["event_id"], answer_row["revision"], 1, "deepseek-chat", score, 5, "incorrect", steps, 0, "graded", "2026-01-01T00:00:00", answer_row["question_version_id"]))
    db.x("UPDATE teacher_answers SET grading_status='graded' WHERE id=?", (answer_row["id"],))


grade(rows[0], 2, "运算错误")
grade(rows[1], 1, "概念理解错误")
for row in rows:
    la.classify_answer_errors(row["id"])
rule_labels = {row["answer_id"]: [item["label"] for item in json.loads(row["labels_json"] or "[]")] for row in db.q("SELECT * FROM answer_error_labels WHERE batch_id=?", (batch_id,))}

# 人工标注作为评价基准
la.set_gold_error_labels(rows[0]["id"], ["运算错误"], "teacher", "人工核对")
la.set_gold_error_labels(rows[1]["id"], ["概念理解错误"], "teacher", "人工核对")
metrics = la.error_label_metrics(batch_id)

# 推荐反馈
recommendations = la.recommendations(student_row["id"], batch_id)
first = recommendations["items"][0]
feedback = t.create_recommendation_feedback(student_row["id"], {"batch_id": batch_id, "recommendation_type": first["type"], "target_id": first.get("knowledge_point_id") or first.get("method_id") or first.get("error_type") or "", "reason": first["reason"], "evidence": first.get("evidence") or {}}, USER)["feedback"]
completed = t.complete_recommendation_feedback(feedback["id"], {"followup": {"practice_completed": True, "score_rate": 0.8}}, USER)["feedback"]
listed = t.list_recommendation_feedback(student_row["id"], batch_id, USER)

print(json.dumps({
    "rule_labels": rule_labels,
    "metrics": {"answers": metrics["answers_with_gold"], "macro_f1": metrics["macro_f1"], "labels": {item["label"]: {"precision": item["precision"], "recall": item["recall"], "f1": item["f1"], "gold": item["gold"], "predicted": item["predicted"]} for item in metrics["labels"] if item["gold"] or item["predicted"]}},
    "feedback": {"status": feedback["status"], "completed": completed["status"], "followup": json.loads(completed["followup_json"] or "{}"), "listed": len(listed["items"])},
}, ensure_ascii=False))
