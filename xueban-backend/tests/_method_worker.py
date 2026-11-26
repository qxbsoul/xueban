"""P1 方法机会覆盖率：分母来自独立机会数据，缺失时 coverage=null 且不得显示 100%。"""
import json

from xueban.api import xueban_teacher as t
from xueban.services import learning_analysis as la
from xueban.services import xueban_store as db

BATCH = "batch_methods"
CLASS = "class_methods"
NOW = "2026-01-01T00:00:00"
t.init()
la.ensure_model()

db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", (BATCH, "m.csv", "committed", "{}", "[]", "confirmed", NOW))
db.x("INSERT INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", (CLASS, "MC", "方法班", 7, NOW))
db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", ("student_M1", "user_M1", "M1", CLASS, "M1", 7, NOW))
db.x("INSERT INTO teacher_topics (id,code,title,goal,course_id,standard_id,created_at) VALUES (?,?,?,?,?,?,?)", ("topic_m", "TM", "主题", "", "", "", NOW))
db.x("INSERT INTO teacher_indicators (id,code,topic_id,description) VALUES (?,?,?,?)", ("indicator_m", "IM", "topic_m", "知识点"))

questions = ["Q001", "Q002", "Q003"]
for qid in questions:
    db.x("INSERT INTO teacher_question_versions (version_id,batch_id,question_id,topic_id,stem,max_score,scoring_points,indicator_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)", (f"qv:{BATCH}:{qid}", BATCH, qid, "TM", f"题干{qid}", 2, f"{qid}P1 2分", "IM", NOW))

for qid in ("Q001", "Q002"):
    answer_id = f"{BATCH}:{qid}"
    db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,original_answer,solution_process,original_score,max_score,grading_status,import_batch_id,exam_node_id,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (answer_id, "student_M1", qid, f"e:{qid}", 1, "2", "步骤", 2, 2, "graded", BATCH, "N01", f"qv:{BATCH}:{qid}"))
    steps = json.dumps([{"step_index": 1, "student_step": "步骤", "is_correct": True, "score": 2, "max_score": 2, "error_type": "", "judgment_reason": "ok", "scoring_point_links": [{"scoring_point_id": f"{qid}P1", "awarded_score": 2, "max_score": 2}]}], ensure_ascii=False)
    db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,is_correct,step_results_json,needs_review,status,created_at,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"g:{answer_id}", answer_id, "student_M1", qid, f"e:{qid}", 1, 1, "deepseek-chat", 2, 2, "correct", steps, 0, "graded", NOW, f"qv:{BATCH}:{qid}"))

methods = {}
for code, name in (("M1", "方法一"), ("M2", "方法二"), ("M3", "方法三"), ("M4", "方法四")):
    methods[code] = la.upsert_method(code, name, "", "TM", "teacher")
mapping = la.mapping_for_batch(BATCH)
sp = {item["code"]: item["scoring_point_id"] for item in mapping["items"]}


def link(code, method_codes):
    la.set_method_links(sp[code], [{"method_id": methods[code2]["id"], "weight": 1} for code2 in method_codes], "teacher")


link("Q001P1", ["M1", "M3", "M4"])
link("Q002P1", ["M1", "M3"])
link("Q003P1", ["M1", "M2"])

# 模拟 M3 缺少独立机会数据：只有学生证据，没有题目-方法机会
db.x("DELETE FROM teacher_method_opportunities WHERE method_id=?", (methods["M3"]["id"],))

profile = la.student_profile(BATCH, "student_M1")
methods_out = {item["method_id"]: item for item in profile["methods"]}
before_counts = {name: len([row for row in la.method_opportunities(BATCH) if row["method_id"] == methods[name]["id"]]) for name in ("M1", "M2")}
# 变更映射后必须同步机会：Q002P1 不再映射 M1，只映射 M2
la.set_method_links(sp["Q002P1"], [{"method_id": methods["M2"]["id"], "weight": 1}], "teacher")
after_counts = {name: len([row for row in la.method_opportunities(BATCH) if row["method_id"] == methods[name]["id"]]) for name in ("M1", "M2")}
print(json.dumps({
    "M1": {k: methods_out[methods["M1"]["id"]][k] for k in ("status", "data_coverage", "covered_question_count", "opportunity_question_count", "coverage_status", "effective_evidence_weight")},
    "M2": {k: methods_out[methods["M2"]["id"]][k] for k in ("status", "data_coverage", "covered_question_count", "opportunity_question_count", "coverage_status")},
    "M3": {k: methods_out[methods["M3"]["id"]][k] for k in ("status", "data_coverage", "covered_question_count", "opportunity_question_count", "coverage_status")},
    "M4": {k: methods_out[methods["M4"]["id"]][k] for k in ("status", "data_coverage", "covered_question_count", "opportunity_question_count", "evidence_distribution")},
    "opportunities": len(la.method_opportunities(BATCH)),
    "before_counts": before_counts,
    "after_counts": after_counts,
}, ensure_ascii=False))
