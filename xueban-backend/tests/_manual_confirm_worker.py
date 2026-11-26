import json
import sys
from xueban.api import xueban_teacher as t
from xueban.services import xueban_store as db
from xueban.services import learning_analysis as la

t.init()
mode = sys.argv[1] if len(sys.argv) > 1 else "match"
now = "2026-01-01T00:00:00"
db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", ("batch_c", "C.csv", "committed", "{}", "[]", "confirmed", now))
db.x("INSERT INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", ("class_c", "CC", "C班", 7, now))
db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", ("student_c", "user_c", "SC", "class_c", "SC", 7, now))
db.x("INSERT INTO teacher_topics (id,code,title,goal,course_id,standard_id,created_at) VALUES (?,?,?,?,?,?,?)", ("topic_c", "TC", "主题", "", "", "", now))
db.x("INSERT INTO teacher_indicators (id,code,topic_id,description) VALUES (?,?,?,?)", ("indicator_c", "IC", "topic_c", "测试知识点"))
db.x("INSERT INTO teacher_question_versions (version_id,batch_id,question_id,topic_id,standard_id,course_id,material_type,question_type,stem,options,reference_answer,reference_solution,max_score,scoring_points,indicator_id,source,source_order,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("qv:batch_c:Q001", "batch_c", "Q001", "TC", "", "", "", "解答题", "题干", "", "答案", "解析", 5, "Q001P1 5分", "IC", "", 1, now))
db.x("INSERT INTO teacher_questions (id,topic_id,standard_id,course_id,material_type,question_type,stem,options,reference_answer,reference_solution,max_score,scoring_points,indicator_id,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("Q001", "TC", "", "", "", "解答题", "题干", "", "答案", "解析", 5, "Q001P1 5分", "IC", ""))
db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,submit_time,original_answer,solution_process,original_score,max_score,score_status,raw_json,import_batch_id,exam_node_id,course_id,standard_id,question_version_id,grading_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("c:answer", "student_c", "Q001", "e1", 1, now, "5", "步骤", 5, 5, "graded", "{}", "batch_c", "N1", "", "", "qv:batch_c:Q001", "graded"))
steps = json.dumps([{"step_index": 1, "student_step": "步骤", "expected_step": "步骤", "is_correct": True, "score": 5, "max_score": 5, "error_type": "", "judgment_reason": "ok", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 5, "max_score": 5}]}], ensure_ascii=False)
db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,grading_prompt_version,grader_version,score,max_score,is_correct,step_results_json,error_type,feedback,judgment_reason,needs_review,status,raw_response,created_at,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("grade_c", "c:answer", "student_c", "Q001", "e1", 1, 1, "deepseek-chat", "v1", "v1", 5, 5, "correct", steps, "", "", "ok", 0, "graded", "", now, "qv:batch_c:Q001"))
la.ensure_model()
score = 3 if mode == "mismatch" else 5
result = t.confirm_scoring_points("grade_c", {"id": "teacher_c", "role": "teacher"}, {"items": [{"scoring_point_id": "Q001P1", "score": score, "note": "test"}]})
if mode == "rejudge":
    db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,grading_prompt_version,grader_version,score,max_score,is_correct,step_results_json,error_type,feedback,judgment_reason,needs_review,status,raw_response,created_at,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("grade_v2", "c:answer", "student_c", "Q001", "e1", 1, 2, "deepseek-chat", "v1", "v1", 5, 5, "correct", steps, "", "", "ok", 0, "graded", "", now, "qv:batch_c:Q001"))
grade = db.q1("SELECT * FROM teacher_grade_runs WHERE id='grade_c'")
profile = la.student_profile("batch_c", "student_c")
print(json.dumps({"matches": result["matches_whole_score"], "status": grade.get("status"), "confirmations": db.q1("SELECT COUNT(*) n FROM teacher_scoring_point_confirmations WHERE grade_run_id='grade_c'")["n"], "latest_version": db.q1("SELECT MAX(grading_version) n FROM teacher_grade_runs WHERE answer_id='c:answer'")["n"], "mastery": profile["mastery"][0]["mastery_estimate"] if profile["mastery"] else 0}, ensure_ascii=False))
