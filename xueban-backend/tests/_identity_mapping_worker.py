"""P1/P2 验收：任务内学生快照与身份不确定；映射拒绝/版本/影响面。"""
import json

from tests.support_import import answer, make_files, question, student
from xueban.api import xueban_teacher as t
from xueban.services import learning_analysis as la
from xueban.services import xueban_store as db

USER = {"id": "teacher-id", "role": "teacher"}
t.init()
la.ensure_model()

batch_a = t.upload_import(make_files([answer(event="EA", student_code="S01")], questions=[question(qid="Q001", indicator="I01", scoring_points="Q001P1 5分")], students=[student("S01", "C1")]), USER)["batch_id"]
batch_b = t.upload_import(make_files([answer(event="EB", student_code="S01")], questions=[question(qid="Q001", indicator="I01", scoring_points="Q001P1 5分")], students=[student("S01", "C2")]), USER)["batch_id"]
student_row = db.q1("SELECT * FROM teacher_students WHERE code='S01'")
enrollments_a = db.q("SELECT * FROM teacher_enrollments WHERE batch_id=?", (batch_a,))
enrollments_b = db.q("SELECT * FROM teacher_enrollments WHERE batch_id=?", (batch_b,))

# 身份确认：A 明确同一人，B 明确不同人；未确认时保持 unverified
la.set_gold_error_labels  # noqa: keep import used
t.set_enrollment_identity(batch_a, student_row["id"], {"status": "same_person", "note": "同一学号且教师确认同一人"}, USER)
t.set_enrollment_identity(batch_b, student_row["id"], {"status": "distinct", "note": "同名不同校区，教师确认不是同一人"}, USER)
trend = la.student_knowledge_trend(student_row["id"], [batch_a, batch_b])

# 映射：确认 → 拒绝 → 版本与影响面
mapping = la.mapping_for_batch(batch_a)
sp = mapping["items"][0]
snap0 = la.save_analysis(batch_a, "class", "class_C1", {"data": 1})
snap0_again = la.save_analysis(batch_a, "class", "class_C1", {"data": 1})
confirmed = la.set_knowledge_links(sp["scoring_point_id"], [{"knowledge_point_id": "kp:I01", "weight": 1}], USER["id"])
v1 = confirmed["mapping_version"]
snap1 = la.save_analysis(batch_a, "class", "class_C1", {"data": 1})
rejected = la.set_knowledge_links(sp["scoring_point_id"], [], USER["id"], "reject")
v2 = rejected["mapping_version"]
snap2 = la.save_analysis(batch_a, "class", "class_C1", {"data": 1})
item = next(row for row in rejected["items"] if row["code"] == "Q001P1")
history = db.q("SELECT action, before_json, after_json FROM scoring_point_link_history WHERE scoring_point_id=? ORDER BY created_at", (sp["scoring_point_id"],))

# 旧判题/旧快照（版本 0）应被影响面识别
answer_id = db.q1("SELECT id FROM teacher_answers WHERE import_batch_id=?", (batch_a,))["id"]
row = db.q1("SELECT * FROM teacher_answers WHERE id=?", (answer_id,))
db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,status,needs_review,step_results_json,created_at,question_version_id,mapping_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("grade_old", answer_id, row["student_id"], row["question_id"], row["event_id"], row["revision"], 1, "deepseek-chat", 3, 5, "graded", 0, "[]", "2026-01-01T00:00:00", row["question_version_id"], "0"))
db.x("INSERT INTO teacher_analysis_runs (id,task_id,scope,scope_id,algorithm_version,result_json,mapping_version,created_at) VALUES (?,?,?,?,?,?,?,?)", ("analysis_old", batch_a, "class", "class_C1", la.ALGORITHM_VERSION, "{}", "0", "2026-01-01T00:00:00"))
impact = t.mapping_impact(batch_a, USER)

# 删除 A 不影响 B 的 enrollment
t.delete_batch(batch_a, USER)
remaining_b = db.q1("SELECT COUNT(*) n FROM teacher_enrollments WHERE batch_id=?", (batch_b,))["n"]
remaining_a = db.q1("SELECT COUNT(*) n FROM teacher_enrollments WHERE batch_id=?", (batch_a,))["n"]

print(json.dumps({
    "enrollment": {"a_count": len(enrollments_a), "b_count": len(enrollments_b), "a_status": enrollments_a[0]["identity_status"], "b_status": enrollments_b[0]["identity_status"], "a_class": enrollments_a[0]["class_id"], "b_class": enrollments_b[0]["class_id"], "same_student_id": enrollments_a[0]["student_id"] == enrollments_b[0]["student_id"]},
    "trend_identity": {"issues": trend["identity_issues"], "warning": trend["identity_warning"]},
    "mapping": {"mapping_version_before": mapping["mapping_version"], "v1": v1, "v2": v2, "item_status": item["mapping_status"], "summary": rejected["summary"], "history_actions": [row["action"] for row in history]},
    "impact": {"stale_grades": impact["stale_grades"], "stale_analyses": impact["stale_analyses"], "version": impact["mapping_version"]},
    "snapshots": {"snap0": snap0, "snap0_again": snap0_again, "snap1": snap1, "snap2": snap2, "reused": snap0 == snap0_again, "new_after_v1": snap1 != snap0, "new_after_v2": snap2 != snap1},
    "delete": {"remaining_a": remaining_a, "remaining_b": remaining_b},
}, ensure_ascii=False))
