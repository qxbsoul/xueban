"""数据质量：题目级问题必须影响该题作答；三种「没有答案」必须区分；总数对账。"""
import json

from xueban.api import xueban_teacher as t
from xueban.services import learning_analysis as analysis
from xueban.services import xueban_store as db

t.init()
NOW = "2026-01-01T00:00:00"
BT = "batch_dq"
db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", (BT, "dq.csv", "committed", "{}", "[]", "confirmed", NOW))
db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", ("stu_dq", "user_dq", "D01", "class_dq", "D01", 7, NOW))
db.x("INSERT INTO teacher_topics (id,code,title,created_at) VALUES (?,?,?,?)", ("topic_dq", "T01", "主题", NOW))
QUESTIONS = {
    "Q001": ("Q001P1 1分 Q001P2 1分", 3),      # 评分点合计 2 != 满分 3 → 题目级阻断问题
    "Q002": ("Q002P1 2分", 2),                  # 干净题目
    "Q003": ("Q003P1 0分", 1),                  # 评分点满分为 0 → 非法
}
for qid, (points, max_score) in QUESTIONS.items():
    db.x("INSERT INTO teacher_questions (id,topic_id,stem,reference_answer,max_score,scoring_points,question_type) VALUES (?,?,?,?,?,?,?)", (qid, "topic_dq", f"{qid} 题干", "答案", max_score, points, "解答题"))
    db.x("INSERT INTO teacher_question_versions (version_id,batch_id,question_id,topic_id,stem,reference_answer,max_score,scoring_points,question_type,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (f"qv:{BT}:{qid}", BT, qid, "topic_dq", f"{qid} 题干", "答案", max_score, points, "解答题", NOW))

ANSWERS = (
    ("a_q1_ok", "Q001", "有答案", "有过程", "已评分"),
    ("a_q1_empty", "Q001", "", "", "已评分"),        # 空答案 + 空过程 + 无未提交标记 → empty_answer_unknown
    ("a_q2_unsubmitted", "Q002", "", "", "未提交"),   # 明确未提交 → explicit_unsubmitted（accepted）
    ("a_q2_ok", "Q002", "有答案", "有过程", "已评分"),
    ("a_q3_ok", "Q003", "有答案", "有过程", "已评分"), # 评分点非法 → 阻断
)
for index, (answer_id, qid, answer, process, status) in enumerate(ANSWERS):
    db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,grading_status,import_batch_id,max_score,original_score,score_status,solution_process,original_answer,question_version_id,exam_node_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (answer_id, "stu_dq", qid, f"E{index}", 1, "graded", BT, QUESTIONS[qid][1], 1, status, process, answer, f"qv:{BT}:{qid}", "N01"))
    db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,status,needs_review,step_results_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (f"g_{answer_id}", answer_id, "stu_dq", qid, f"E{index}", 1, 1, "m", 1, 1, "graded", 0, "[]", NOW))

report = t._build_issues(BT)
quality = t.batch_data_quality(BT, 1, 50)
records = {row["answer_id"]: row for row in quality["items"]}

print(json.dumps({
    "report": {"raw": report["raw_records"], "issues": report["total_issues"], "by_type": [(item["type"], item["count"]) for item in report["by_type"]], "missing": report["missing"]},
    "summary": quality["summary"],
    "unlinked": quality["unlinked_issues"],
    "records": {key: {"status": value["primary_status"], "reasons": value["reasons"], "issues": value["issue_types"]} for key, value in records.items()},
    "questions": {item["question_id"]: {"total": item["total"], "abnormal": item["abnormal"], "valid": item["valid"], "issues": item["issue_types"]} for item in quality["questions"]},
    "blocking": [item["issue_type"] for item in quality["blocking_issues"]],
    "issues_total": quality["issues_total"], "issues_linked": quality["issues_linked"],
}, ensure_ascii=False))
