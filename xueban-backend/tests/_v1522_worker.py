"""v1.5.22 audit: unanswered/missing separation, coverage detail, review reclassification."""
import json
import os

from xueban.api import xueban_teacher as t
from xueban.services import learning_analysis as la
from xueban.services import xueban_store as db

B = "batch_v1522"
NOW = "2026-08-01T00:00:00"
t.init(); la.ensure_model()
db.x("DELETE FROM teacher_answers WHERE import_batch_id=?", (B,))
db.x("DELETE FROM teacher_batches WHERE id=?", (B,))
db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", (B, "v1522.csv", "committed", "{}", "[]", "ready", NOW))
db.x("INSERT INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", ("class_v22", "V22", "V22班", 7, NOW))
for sid in ("v22a", "v22b", "v22c"):
    db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", (sid, f"u_{sid}", sid.upper(), "class_v22", sid, 7, NOW))
db.x("INSERT INTO teacher_questions (id,stem,reference_answer,max_score,scoring_points,question_type,topic_id,indicator_id) VALUES (?,?,?,?,?,?,?,?)", ("Q901", "1+1", "2", 2, "Q901P1 1分\nQ901P2 1分", "解答题", "topic_T01", "indicator_T01I1"))
db.x("INSERT INTO teacher_question_versions (version_id,batch_id,question_id,stem,reference_answer,max_score,scoring_points,question_type,topic_id,indicator_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("qv:v22:Q901", B, "Q901", "1+1", "2", 2, "Q901P1 1分\nQ901P2 1分", "解答题", "topic_T01", "indicator_T01I1", NOW))
for i in (1, 2):
    code = f"Q901P{i}"
    db.x("INSERT OR REPLACE INTO scoring_points (id,code,batch_id,name,description,created_at) VALUES (?,?,?,?,?,?)", (f"sp_v22_{code}", code, B, "点", "", NOW))
    db.x("INSERT OR REPLACE INTO scoring_point_knowledge_links (id,scoring_point_id,knowledge_point_id,weight,source,confidence,status,batch_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (f"link_v22_{code}", f"sp_v22_{code}", "kp:T01I1", 1.0, "legacy_indicator", 0.6, "legacy", B, NOW, NOW))

answers = (
    ("v22_normal", "v22a", "1+1=2", "步骤", "graded", ""),
    ("v22_unanswered", "v22b", "", "", "pending", "unanswered"),
    ("v22_missing", "v22c", "", "", "pending", ""),
    ("v22_pending", "v22a", "1+1=2", "步骤", "needs_review", ""),
    ("v22_wait", "v22b", "1+1=2", "步骤", "pending", ""),
)
for aid, sid, answer, process, status, score_status in answers:
    db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,grading_status,import_batch_id,max_score,original_answer,solution_process,question_version_id,exam_node_id,score_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (aid, sid, "Q901", f"E-{aid}", 1, status, B, 2, answer, process, "qv:v22:Q901", "N01", score_status))

steps = [{"step_index": 1, "student_step": "1+1=2", "score": 2, "max_score": 2, "scoring_point_links": [{"scoring_point_id": "Q901P1", "awarded_score": 1, "max_score": 1}, {"scoring_point_id": "Q901P2", "awarded_score": 1, "max_score": 1}]}]
db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,status,needs_review,step_results_json,structure_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
     ("g_v22_normal", "v22_normal", "v22a", "Q901", "E-v22_normal", 1, 1, "m", 2, 2, "graded", 0, json.dumps(steps, ensure_ascii=False), "{}", NOW))
# 只有非关键告警的复核记录：应可重新归为自动判题
caps = [{"step_index": 1, "student_step": "1+1=2", "score": 2, "max_score": 2, "scoring_point_links": [{"scoring_point_id": "Q901P1", "awarded_score": 1, "max_score": 1}]}]
structure = {"validation_issues": [{"code": "scoring_point_awarded_capped", "severity": "review", "message": "capped"}]}
db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,status,needs_review,step_results_json,structure_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
     ("g_v22_review", "v22_pending", "v22a", "Q901", "E-v22_pending", 1, 1, "m", 1, 2, "needs_review", 1, json.dumps(caps, ensure_ascii=False), json.dumps(structure, ensure_ascii=False), NOW))

pending = t.pending_answer_ids(B)
audit = t.data_audit(B)
profile = la.class_profile(B, "class_v22")
reclass = t.reclassify_noncritical_reviews(B, dry_run=True)
usage_review = la.grade_usage(db.q1("SELECT * FROM teacher_grade_runs WHERE id='g_v22_review'"))
print(json.dumps({
    "pending": pending,
    "audit": {k: audit[k] for k in ("raw_answers", "normal_answers", "unanswered_answers", "missing_answers", "latest_grades", "evidence_rows", "students_with_evidence")},
    "profile": [{k: p.get(k) for k in ("knowledge_point_id", "valid_students", "students_with_any_evidence", "evidence_answers", "evidence_coverage", "coverage", "opportunity_question_count")} for p in profile.get("knowledge_points", [])],
    "reclass": reclass,
    "usage_review": usage_review,
}, ensure_ascii=False, default=str))
