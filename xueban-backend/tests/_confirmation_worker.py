"""人工确认语义：部分评分点确认不得整题误标；结构有问题不得静默整题确认。"""
import json

from fastapi import HTTPException

from xueban.api import xueban_teacher as t
from xueban.services import xueban_store as db

USER = {"id": "teacher", "role": "teacher"}
t.init()
now = "2026-01-01T00:00:00"
db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", ("batch_c", "c.csv", "committed", "{}", "[]", "confirmed", now))
db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", ("student_c", "user_c", "C01", "class_c", "C01", 7, now))
db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,grading_status,import_batch_id,max_score,original_score) VALUES (?,?,?,?,?,?,?,?,?)", ("answer_c", "student_c", "Q001", "E1", 1, "graded", "batch_c", 5, 3))
steps = [{"step_index": 1, "source_step_id": "src-1", "student_step": "步骤", "is_correct": False, "score": 3, "max_score": 5, "error_type": "", "judgment_reason": "",
          "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 2, "max_score": 3}, {"scoring_point_id": "Q001P2", "awarded_score": 1, "max_score": 2}],
          "operation_units": []}]
structure = {"source_steps": [{"source_step_id": "src-1"}], "validation": {"review_required": False}, "scoring_points": {"Q001P1": {"awarded": 2, "max": 3}, "Q001P2": {"awarded": 1, "max": 2}}}


def make_grade(grade_id, validation):
    db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,grading_prompt_version,grader_version,score,max_score,is_correct,step_results_json,structure_json,needs_review,status,raw_response,created_at,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (grade_id, "answer_c", "student_c", "Q001", "E1", 1, 1, "deepseek-chat", "teacher-grade-v4", "v1.5.13", 3, 5, "partial", json.dumps(steps, ensure_ascii=False), json.dumps({**structure, "validation": validation}, ensure_ascii=False), 1, "needs_review", "{}", now, ""))


make_grade("grade_c1", {"review_required": False})
first = t.confirm_scoring_points("grade_c1", USER, {"items": [{"scoring_point_id": "Q001P1", "score": 2}]})
status_after_first = db.q1("SELECT status, needs_review FROM teacher_grade_runs WHERE id='grade_c1'")
second = t.confirm_scoring_points("grade_c1", USER, {"items": [{"scoring_point_id": "Q001P1", "score": 2}, {"scoring_point_id": "Q001P2", "score": 1}]})
status_after_second = db.q1("SELECT status, needs_review FROM teacher_grade_runs WHERE id='grade_c1'")
manual = t.manual("grade_c1", USER, {"score": 4, "note": "人工修正总分"})
manual_structure = json.loads(db.q1("SELECT structure_json FROM teacher_grade_runs WHERE id='grade_c1'")["structure_json"])

make_grade("grade_c2", {"review_required": True, "issue_count": 1})
blocked = None
try:
    t.confirm("grade_c2", USER, {})
except HTTPException as exc:
    blocked = {"status_code": exc.status_code, "detail": exc.detail}

print(json.dumps({
    "first": {"fully": first["fully_confirmed"], "status": status_after_first["status"], "confirmed": first["confirmed_points"], "total": first["total_points"]},
    "second": {"fully": second["fully_confirmed"], "status": status_after_second["status"]},
    "manual": {"status": manual["grade"]["status"], "override": manual_structure.get("manual_override")},
    "blocked": blocked,
}, ensure_ascii=False))
