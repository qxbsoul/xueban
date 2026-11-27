"""题目表现统计口径：应作答/实际/有效/复核/失败/未判/未作答、修订去重、超范围。"""
import json

from xueban.api import xueban_teacher as t
from xueban.services import learning_analysis as la
from xueban.services import xueban_store as db

t.init()
NOW = "2026-01-01T00:00:00"
B = "batch_qp"
db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", (B, "qp.csv", "committed", "{}", "[]", "confirmed", NOW))
db.x("INSERT INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", ("class_q1", "Q1", "一班", 7, NOW))
db.x("INSERT INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", ("class_q2", "Q2", "二班", 7, NOW))
for index, (sid, cls) in enumerate((("stu_1", "class_q1"), ("stu_2", "class_q1"), ("stu_3", "class_q1"), ("stu_4", "class_q2"))):
    db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", (sid, f"user_{sid}", sid.upper(), cls, sid, 7, NOW))
for node, available in (("N01", "Q001、Q002、Q003"), ("N02", "Q002")):
    db.x("INSERT INTO teacher_exams (id,exam_date,topics,description,available_questions,course_id) VALUES (?,?,?,?,?,?)", (node, "2026-01-0" + node[-1], "", "", available, ""))
for qid, points, max_score in (("Q001", "Q001P1 1分", 1), ("Q002", "Q002P1 1分", 1), ("Q003", "Q003P1 1分", 1), ("Q999", "Q999P1 1分", 1)):
    db.x("INSERT INTO teacher_questions (id,stem,reference_answer,max_score,scoring_points,question_type) VALUES (?,?,?,?,?,?)", (qid, f"{qid} 题干", "答案", max_score, points, "填空题"))
    db.x("INSERT INTO teacher_question_versions (version_id,batch_id,question_id,stem,reference_answer,max_score,scoring_points,question_type,created_at) VALUES (?,?,?,?,?,?,?,?,?)", (f"qv:{B}:{qid}", B, qid, f"{qid} 题干", "答案", max_score, points, "填空题", NOW))


def answer(answer_id, sid, qid, node, revision=1, status="graded", text="答案", process="步骤", submit=None):
    db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,grading_status,import_batch_id,max_score,original_score,submit_time,solution_process,original_answer,exam_node_id,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (answer_id, sid, qid, f"E-{answer_id}", revision, status, B, 1, 0, submit or NOW, process, text, node, f"qv:{B}:{qid}"))


def grade(answer_id, score, status="graded", review=0):
    db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,status,needs_review,step_results_json,created_at,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (f"g-{answer_id}", answer_id, "stu_1", "Q001", f"E-{answer_id}", 1, 1, "m", score, 1, status, review, "[]", NOW, ""))


# Q001：S1 两次修订（只取最新）、S2 有效、S3 失败、S4 待复核
answer("a1", "stu_1", "Q001", "N01", 1, "graded", submit="2026-01-01T09:00:00")
answer("a1b", "stu_1", "Q001", "N01", 2, "graded", submit="2026-01-01T09:05:00")
answer("a2", "stu_2", "Q001", "N01")
answer("a3", "stu_3", "Q001", "N01", "failed")
answer("a4", "stu_4", "Q001", "N01", "needs_review")
db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,status,needs_review,step_results_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("g-a1", "a1", "stu_1", "Q001", "E-a1", 1, 1, "m", 0, 1, "graded", 0, "[]", NOW))
db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,status,needs_review,step_results_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("g-a1b", "a1b", "stu_1", "Q001", "E-a1b", 2, 1, "m", 1, 1, "graded", 0, "[]", NOW))
db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,status,needs_review,step_results_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("g-a2", "a2", "stu_2", "Q001", "E-a2", 1, 1, "m", 1, 1, "graded", 0, "[]", NOW))
db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,status,needs_review,step_results_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("g-a3", "a3", "stu_3", "Q001", "E-a3", 1, 1, "m", 0, 1, "failed", 0, "[]", NOW))
db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,status,needs_review,step_results_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("g-a4", "a4", "stu_4", "Q001", "E-a4", 1, 1, "m", 1, 1, "needs_review", 1, "[]", NOW))

# Q002：S1 在两个节点各一次（同一学生只算一次）；S2 空提交（不算作答）
answer("b1", "stu_1", "Q002", "N01", submit="2026-01-01T10:00:00")
answer("b2", "stu_1", "Q002", "N02", submit="2026-02-01T10:00:00")
answer("b3", "stu_2", "Q002", "N02", text="", process="")
for gid in ("g-b1", "g-b2"):
    db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,status,needs_review,step_results_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (gid, "b1" if gid == "g-b1" else "b2", "stu_1", "Q002", "E", 1, 1, "m", 1, 1, "graded", 0, "[]", NOW))

# Q003：S3/S4 有效 → 两人都是 N01 参与者
answer("c1", "stu_3", "Q003", "N01")
answer("c2", "stu_4", "Q003", "N01")
for gid, aid in (("g-c1", "c1"), ("g-c2", "c2")):
    db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,status,needs_review,step_results_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (gid, aid, "stu_3" if aid == "c1" else "stu_4", "Q003", "E", 1, 1, "m", 1, 1, "graded", 0, "[]", NOW))

# 超出题单范围：Q999 不在 N01/N02 的可用题目里
answer("d1", "stu_1", "Q999", "N01")
db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,status,needs_review,step_results_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("g-d1", "d1", "stu_1", "Q999", "E", 1, 1, "m", 1, 1, "graded", 0, "[]", NOW))

result = la.question_performance(B)
class_one = la.question_performance(B, "class_q1")
detail = la.question_analysis_detail(B, "class_q1", "Q002")
rows = {row["question_id"]: row for row in result["items"]}
print(json.dumps({
    "summary": result["summary"],
    "basis": result["basis"],
    "warnings": result["warnings"],
    "Q001": {key: rows["Q001"][key] for key in ("expected", "answered", "not_answered", "graded", "review", "failed", "pending", "average_score", "score_rate", "expected_basis")},
    "Q002": {key: rows["Q002"][key] for key in ("expected", "answered", "not_answered", "graded", "failed", "pending")},
    "Q003": {key: rows["Q003"][key] for key in ("expected", "answered", "not_answered", "graded")},
    "Q999": {key: rows["Q999"][key] for key in ("expected", "answered", "not_answered", "out_of_scope_answered", "expected_basis")},
    "class_one": class_one["summary"],
    "detail": {"partition": detail["partition"], "students": len(detail["students"]), "statuses": sorted({item["status"] for item in detail["students"]})},
}, ensure_ascii=False, default=str))
