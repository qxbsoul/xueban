"""D5 回归：单题统计必须区分事件平均与学生等权平均，且人工修正不得伪造评分点证据。"""
import json

from xueban.api import xueban_teacher as t
from xueban.services import learning_analysis as la
from xueban.services import xueban_store as db

BATCH = "batch_metrics"
CLASS = "class_metrics"
NOW = "2026-01-01T00:00:00"
t.init()
la.ensure_model()

db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", (BATCH, "m.csv", "committed", "{}", "[]", "confirmed", NOW))
db.x("INSERT INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", (CLASS, "CM", "统计班", 7, NOW))
db.x("INSERT INTO teacher_topics (id,code,title,goal,course_id,standard_id,created_at) VALUES (?,?,?,?,?,?,?)", ("topic_T", "T01", "主题", "", "", "", NOW))
db.x("INSERT INTO teacher_indicators (id,code,topic_id,description) VALUES (?,?,?,?)", ("indicator_I", "I01", "topic_T", "知识点一"))

students = {f"S{i:02d}": f"student_S{i:02d}" for i in range(1, 9)}
for code, sid in students.items():
    db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", (sid, f"user_{code}", code, CLASS, code, 7, NOW))


def add_question(qid, max_score=5):
    version_id = f"qv:{BATCH}:{qid}"
    db.x("INSERT INTO teacher_question_versions (version_id,batch_id,question_id,topic_id,stem,max_score,scoring_points,indicator_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)", (version_id, BATCH, qid, "T01", f"题干{qid}", max_score, f"{qid}P1 2分 {qid}P2 2分 {qid}P3 1分", "I01", NOW))
    return version_id


def add_answer(qid, version_id, student, event, score, sp_scores, status="graded", review=0):
    answer_id = f"{BATCH}:{event}-r1"
    db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,original_answer,solution_process,original_score,max_score,grading_status,import_batch_id,exam_node_id,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (answer_id, students[student], qid, event, 1, "a", "p", 0, 5, "needs_review" if review else ("graded" if status != "manual_corrected" else "graded"), BATCH, "N01", version_id))
    links = [{"scoring_point_id": f"{qid}P{i + 1}", "awarded_score": value, "max_score": 2 if i < 2 else 1} for i, value in enumerate(sp_scores)]
    steps = json.dumps([{"step_index": 1, "student_step": "步骤", "is_correct": score == 5, "score": score, "max_score": 5, "error_type": "" if score == 5 else "计算操作错误", "judgment_reason": "统计测试", "scoring_point_links": links}], ensure_ascii=False)
    db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,is_correct,step_results_json,needs_review,status,created_at,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"grade-{answer_id}", answer_id, students[student], qid, event, 1, 1, "deepseek-chat", score, 5, "partial", steps, review, status, NOW, version_id))


# Q011：A=2（1 次）、B=0（2 次）、C=2（1 次）→ 事件平均 1.0，学生等权 1.33
v11 = add_question("Q011")
add_answer("Q011", v11, "S01", "E11a", 2, [1, 1, 0])
add_answer("Q011", v11, "S02", "E11b1", 0, [0, 0, 0])
add_answer("Q011", v11, "S02", "E11b2", 0, [0, 0, 0])
add_answer("Q011", v11, "S03", "E11c", 2, [1, 1, 0])

# Q014：D01=0、D02=1、D03=0、D04=[3,1]（学生均 2）、D05=1 → 事件平均 1.0，学生等权 0.8
v14 = add_question("Q014")
add_answer("Q014", v14, "S01", "E14a", 0, [0, 0, 0])
add_answer("Q014", v14, "S02", "E14b", 1, [1, 0, 0])
add_answer("Q014", v14, "S03", "E14c", 0, [0, 0, 0])
add_answer("Q014", v14, "S04", "E14d1", 3, [1, 1, 1])
add_answer("Q014", v14, "S04", "E14d2", 1, [1, 0, 0])
add_answer("Q014", v14, "S05", "E14e", 1, [1, 0, 0])

# Q015：D01=3、D04=1 → 事件平均 2.0，学生等权 2.0
v15 = add_question("Q015")
add_answer("Q015", v15, "S01", "E15a", 3, [1, 1, 1])
add_answer("Q015", v15, "S04", "E15b", 1, [1, 0, 0])

# Q016：一条正常判题 + 一条人工整题修正；修正分进入成绩，但不产生评分点证据
v16 = add_question("Q016")
add_answer("Q016", v16, "S01", "E16a", 2, [1, 1, 0])
add_answer("Q016", v16, "S02", "E16b", 4, [1, 1, 1], status="manual_corrected")

# Q017：人工整题修正 + 教师逐评分点确认 → 只有确认过的评分点产生证据，且用确认分
v17 = add_question("Q017")
add_answer("Q017", v17, "S01", "E17a", 1, [1, 0, 0], status="manual_corrected")
grade17 = db.q1("SELECT * FROM teacher_grade_runs WHERE answer_id=?", (f"{BATCH}:E17a-r1",))
db.x("INSERT INTO teacher_scoring_point_confirmations (id,grade_run_id,answer_id,scoring_point_id,original_ai_score,confirmed_score,max_score,confirmation_status,teacher_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("confirm17", grade17["id"], grade17["answer_id"], "Q017P1", 1, 2, 2, "confirmed", "teacher", NOW, NOW))

rows = {row["question_id"]: row for row in la._question_analysis(BATCH, CLASS)}
detail = la.question_analysis_detail(BATCH, CLASS, "Q016")
profile_s1 = la.student_profile(BATCH, "student_S01")
q17_evidence = [item for item in profile_s1["evidence"] if item["question_id"] == "Q017"]
print(json.dumps({
    "Q011": {k: rows["Q011"][k] for k in ("average_score", "student_average_score", "student_score_unit_count", "valid_score_students")},
    "Q014": {k: rows["Q014"][k] for k in ("average_score", "student_average_score", "student_score_unit_count", "valid_score_students")},
    "Q015": {k: rows["Q015"][k] for k in ("average_score", "student_average_score", "student_score_unit_count", "valid_score_students")},
    "Q011_sp": [(sp["scoring_point_id"], sp["student_average_score"], sp["event_average_score"]) for sp in rows["Q011"]["scoring_points"]],
    "Q016": {k: rows["Q016"][k] for k in ("average_score", "student_average_score", "manual_correction_without_scoring_sync")},
    "Q016_sp": [(sp["scoring_point_id"], sp["valid_students"], sp["event_average_score"]) for sp in rows["Q016"]["scoring_points"]],
    "Q016_students": [(s["student_code"], s["status"], s["score"]) for s in detail["students"]],
    "Q017_evidence": [{"sp": item["scoring_point_id"], "earned": item["earned_score"], "full": item["full_score"], "source": item["judgment_source"]} for item in q17_evidence],
    "Q017_quality": profile_s1["data_quality"]["not_usable"],
}, ensure_ascii=False))
