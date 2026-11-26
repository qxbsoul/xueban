import json
import sys
from xueban.api import xueban_teacher as t
from xueban.services import xueban_store as db
from xueban.services import learning_analysis as la

t.init()
now = "2026-01-01T00:00:00"
db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", ("batch_scope", "scope.csv", "committed", "{}", "[]", "confirmed", now))
db.x("INSERT INTO teacher_topics (id,code,title,goal,course_id,standard_id,created_at) VALUES (?,?,?,?,?,?,?)", ("topic_s", "TS", "主题", "", "", "", now))
db.x("INSERT INTO teacher_indicators (id,code,topic_id,description) VALUES (?,?,?,?)", ("indicator_s", "IS", "topic_s", "知识点"))
db.x("INSERT INTO teacher_question_versions (version_id,batch_id,question_id,topic_id,standard_id,course_id,material_type,question_type,stem,options,reference_answer,reference_solution,max_score,scoring_points,indicator_id,source,source_order,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("qv:batch_scope:Q001", "batch_scope", "Q001", "TS", "", "", "", "解答题", "题干", "", "答案", "解析", 2, "Q001P1 2分", "IS", "", 1, now))
db.x("INSERT INTO teacher_questions (id,topic_id,standard_id,course_id,material_type,question_type,stem,options,reference_answer,reference_solution,max_score,scoring_points,indicator_id,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("Q001", "TS", "", "", "", "解答题", "题干", "", "答案", "解析", 2, "Q001P1 2分", "IS", ""))
db.x("INSERT INTO teacher_exams (id,exam_date,topics,description,available_questions,course_id) VALUES (?,?,?,?,?,?)", ("N01", "2026-01-20", "", "", "Q001", ""))
db.x("INSERT INTO teacher_exams (id,exam_date,topics,description,available_questions,course_id) VALUES (?,?,?,?,?,?)", ("N02", "2026-06-17", "", "", "Q001", ""))
if len(sys.argv) > 1 and sys.argv[1] == "versioned":
    db.x("UPDATE teacher_exams SET grading_rule_version='g1', scoring_rule_version='s1', mapping_version='m1'")
for index, cls in enumerate(("C1", "C2", "C3"), start=1):
    db.x("INSERT INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", (f"class_{cls}", cls, f"{cls}班", 7, now))
    db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", (f"student_{cls}", f"user_{cls}", f"S{index}", f"class_{cls}", f"S{index}", 7, now))
    exams = ("N01", "N02") if cls == "C1" else ("N01",)
    for exam in exams:
        answer_id = f"a:{cls}:{exam}"
        db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,submit_time,original_answer,solution_process,original_score,max_score,score_status,raw_json,import_batch_id,exam_node_id,course_id,standard_id,question_version_id,grading_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (answer_id, f"student_{cls}", "Q001", f"e:{cls}:{exam}", 1, now, "2", "步骤", 2, 2, "graded", "{}", "batch_scope", exam, "", "", "qv:batch_scope:Q001", "graded"))
        steps = json.dumps([{"step_index": 1, "student_step": "步骤", "expected_step": "步骤", "is_correct": True, "score": 2, "max_score": 2, "error_type": "", "judgment_reason": "ok", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 2, "max_score": 2}]}], ensure_ascii=False)
        db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,grading_prompt_version,grader_version,score,max_score,is_correct,step_results_json,error_type,feedback,judgment_reason,needs_review,status,raw_response,created_at,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"g:{cls}:{exam}", answer_id, f"student_{cls}", "Q001", f"e:{cls}:{exam}", 1, 1, "deepseek-chat", "v1", "v1", 2, 2, "correct", steps, "", "", "ok", 0, "graded", "", now, "qv:batch_scope:Q001"))
la.ensure_model()
compare = t.analysis_compare("batch_scope", "class_C1,class_C2,class_C3", {"id": "teacher", "role": "teacher"})
long = la.question_longitudinal("batch_scope", "class_C1", "Q001")
profile = la.class_profile("batch_scope", "class_C1")
print(json.dumps({"class_count": len(compare["classes"]), "row_values": [len(row["values"]) for row in compare["rows"]], "longitudinal_points": len(long["points"]), "longitudinal_status": long["status"], "trend_status": profile["trends"][0]["comparable_status"] if profile["trends"] else None}, ensure_ascii=False))
