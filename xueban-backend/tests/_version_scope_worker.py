"""D3 回归：同一 question_id 在多个任务里题干/满分/评分点不同时，所有任务内查询必须各用各的版本。"""
import json

from tests.support_import import answer, make_files, question
from xueban.api import xueban_teacher as t
from xueban.services import xueban_store as db

USER = {"id": "teacher", "role": "teacher"}
t.init()

a = t.upload_import(make_files(
    [answer(event="E-A", final="A", max_score="5")],
    questions=[question(stem="题干-A", max_score="5", scoring_points="Q001P1 5分")],
), USER)
b = t.upload_import(make_files(
    [answer(event="E-B", final="B", max_score="10")],
    questions=[question(stem="题干-B", max_score="10", scoring_points="Q001P1 10分")],
), USER)
# C：非法满分 + 无评分点。如果 A 的视图读全局题表，就会被 C 污染成 blocking。
c = t.upload_import(make_files(
    [answer(event="E-C", final="C", max_score="0")],
    questions=[question(stem="题干-C", max_score="0", scoring_points="")],
), USER)

batch_a, batch_b, batch_c = a["batch_id"], b["batch_id"], c["batch_id"]
answers = {row["import_batch_id"]: row for row in db.q("SELECT * FROM teacher_answers")}

for batch in (batch_a, batch_b):
    row = answers[batch]
    db.x(
        "INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,grading_prompt_version,score,max_score,is_correct,step_results_json,needs_review,status,raw_response,created_at,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"grade-{batch}", row["id"], row["student_id"], row["question_id"], row["event_id"], row["revision"], 1, "deepseek-chat", "teacher-grade-v3", 1, 5, "partial", "[]", 0, "graded", "", "2026-01-01T00:00:00", row["question_version_id"]),
    )

for batch in (batch_a, batch_b, batch_c):
    t.confirm_preprocessing(batch, USER)

selected_a = t._selected_answers("", "", "", batch_a)[0]
selected_b = t._selected_answers("", "", "", batch_b)[0]
issues_a = t._build_issues(batch_a)
issues_c = t._build_issues(batch_c)
gate_a = t.quality_gate(batch_a, USER)
gate_c = t.quality_gate(batch_c, USER)
results = {item["answer_id"]: item for item in t.results(user=USER)["items"]}
detail_a = t.answer_detail(answers[batch_a]["id"], USER)
detail_b = t.answer_detail(answers[batch_b]["id"], USER)
result_a = t.result_detail(f"grade-{batch_a}", USER)
student = t.student_detail(answers[batch_a]["student_id"], USER)

print(json.dumps({
    "selected": {"a_max": selected_a["question_max_score"], "a_stem": selected_a["stem"], "a_source": selected_a["question_source"],
                  "b_max": selected_b["question_max_score"], "b_stem": selected_b["stem"]},
    "issues_a": {"by_type": issues_a["by_type"], "count": issues_a["total_issues"]},
    "issues_c": {"by_type": issues_c["by_type"]},
    "gate_a": {"blocking": gate_a["blocking"], "types": [i["type"] for i in gate_a["issues"]], "sources": gate_a["question_sources"]},
    "gate_c": {"blocking": gate_c["blocking"], "types": [i["type"] for i in gate_c["issues"]]},
    "results_by_batch": {
        label: {"stem": results[answers[batch]["id"]].get("stem"), "source": results[answers[batch]["id"]].get("question_source")}
        for label, batch in (("a", batch_a), ("b", batch_b))
    },
    "detail_a": {"stem": detail_a["question"].get("stem"), "source": detail_a["question_source"], "versions": len(detail_a["versions"])},
    "detail_b": {"stem": detail_b["question"].get("stem"), "source": detail_b["question_source"]},
    "result_a": {"stem": result_a["question"].get("stem"), "source": result_a["question_source"]},
    "student_stems": sorted({row.get("stem") for row in student["answers"]}),
    "answer_ids": {key: db.q1("SELECT id FROM teacher_answers WHERE import_batch_id=?", (key,))["id"] for key in (batch_a, batch_b, batch_c)},
}, ensure_ascii=False))
