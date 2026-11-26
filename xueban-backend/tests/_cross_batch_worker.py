import json
import sys
from xueban.api import xueban_teacher as t
from xueban.services import xueban_store as db
from xueban.services import learning_analysis as la

t.init()
now = "2026-01-01T00:00:00"
db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", ("batch_a", "A.csv", "committed", "{}", "[]", "confirmed", now))
db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", ("batch_b", "B.csv", "committed", "{}", "[]", "confirmed", now))
db.x("INSERT INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", ("class_a", "CA", "A班", 7, now))
db.x("INSERT INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", ("class_b", "CB", "B班", 7, now))
db.x("INSERT INTO xueban_users (id,email,display_name,auth_type,role,created_at) VALUES (?,?,?,?,?,?)", ("user_a", "a@test.local", "A", "password", "student", now))
db.x("INSERT INTO xueban_users (id,email,display_name,auth_type,role,created_at) VALUES (?,?,?,?,?,?)", ("user_b", "b@test.local", "B", "password", "student", now))
db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", ("student_a", "user_a", "SA", "class_a", "SA", 7, now))
db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", ("student_b", "user_b", "SB", "class_b", "SB", 7, now))
db.x("INSERT INTO teacher_topics (id,code,title,goal,course_id,standard_id,created_at) VALUES (?,?,?,?,?,?,?)", ("topic_t", "T1", "主题", "", "", "", now))
db.x("INSERT INTO teacher_indicators (id,code,topic_id,description) VALUES (?,?,?,?)", ("indicator_i", "I1", "topic_t", "测试知识点"))
for batch, qv, max_score, stem in (("batch_a", "qv:batch_a:Q1", 5, "A题干"), ("batch_b", "qv:batch_b:Q1", 10, "B题干")):
    db.x("INSERT INTO teacher_question_versions (version_id,batch_id,question_id,topic_id,standard_id,course_id,material_type,question_type,stem,options,reference_answer,reference_solution,max_score,scoring_points,indicator_id,source,source_order,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (qv, batch, "Q1", "T1", "", "", "", "解答题", stem, "", "答案", "解析", max_score, f"Q001P1 {max_score}分", "I1", "", 1, now))
    db.x("INSERT OR REPLACE INTO teacher_questions (id,topic_id,standard_id,course_id,material_type,question_type,stem,options,reference_answer,reference_solution,max_score,scoring_points,indicator_id,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("Q1", "T1", "", "", "", "解答题", stem, "", "答案", "解析", max_score, f"Q001P1 {max_score}分", "I1", ""))
# answers have same event_id/revision deliberately, different batch
db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,submit_time,original_answer,solution_process,original_score,max_score,score_status,raw_json,import_batch_id,exam_node_id,course_id,standard_id,question_version_id,grading_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("a:answer", "student_a", "Q1", "same-event", 1, now, "学生A答案", "步骤", 0, 5, "graded", "{}", "batch_a", "N1", "", "", "qv:batch_a:Q1", "graded"))
db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,submit_time,original_answer,solution_process,original_score,max_score,score_status,raw_json,import_batch_id,exam_node_id,course_id,standard_id,question_version_id,grading_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("b:answer", "student_b", "Q1", "same-event", 1, now, "学生B答案", "步骤", 5, 10, "graded", "{}", "batch_b", "N1", "", "", "qv:batch_b:Q1", "graded"))
for batch, answer, student, max_score, score in (("batch_a", "a:answer", "student_a", 5, 0), ("batch_b", "b:answer", "student_b", 10, 5)):
    steps = json.dumps([{"step_index": 1, "student_step": "步骤", "expected_step": "步骤", "is_correct": score == max_score, "score": score, "max_score": max_score, "error_type": "计算操作错误" if score < max_score else "", "judgment_reason": "测试", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": score, "max_score": max_score}]}], ensure_ascii=False)
    db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,grading_prompt_version,grader_version,score,max_score,is_correct,step_results_json,error_type,feedback,judgment_reason,needs_review,status,raw_response,created_at,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"{batch}:grade", answer, student, "Q1", "same-event", 1, 1, "deepseek-chat", "v1", "v1", score, max_score, "partial", steps, "", "", "测试", 0, "graded", "", now, f"qv:{batch}:Q1"))
la.ensure_model()
ev, st = la._evidence("batch_a")
a = la.student_profile("batch_a", "student_a")
b = la.student_profile("batch_b", "student_b")
qa = la.question_analysis_detail("batch_a", "class_a", "Q1")
qb = la.question_analysis_detail("batch_b", "class_b", "Q1")
print(json.dumps({"answers": db.q1("SELECT COUNT(*) n FROM teacher_answers")["n"], "a_max": qa["question"]["max_score"], "b_max": qb["question"]["max_score"], "a_stem": qa["question"]["stem"], "b_stem": qb["question"]["stem"], "a_mastery": a["mastery"][0]["mastery_estimate"] if a["mastery"] else None, "b_mastery": b["mastery"][0]["mastery_estimate"] if b["mastery"] else None, "a_batch_answers": db.q1("SELECT COUNT(*) n FROM teacher_answers WHERE import_batch_id='batch_a'")["n"], "evidence": st, "sp": db.q("SELECT * FROM scoring_points"), "links": db.q("SELECT * FROM scoring_point_knowledge_links"), "b_batch_answers": db.q1("SELECT COUNT(*) n FROM teacher_answers WHERE import_batch_id='batch_b'")["n"]}, ensure_ascii=False))

if len(sys.argv) > 1 and sys.argv[1] == "delete":
    t.delete_batch("batch_a", {"id": "teacher", "role": "teacher"})
    print(json.dumps({"b_answers": db.q1("SELECT COUNT(*) n FROM teacher_answers WHERE import_batch_id='batch_b'")["n"], "b_versions": db.q1("SELECT COUNT(*) n FROM teacher_question_versions WHERE batch_id='batch_b'")["n"], "a_batches": db.q1("SELECT COUNT(*) n FROM teacher_batches WHERE id='batch_a'")["n"]}, ensure_ascii=False))
