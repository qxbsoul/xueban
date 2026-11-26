"""D9 回归：趋势可比性必须检查题目集合/满分/评分点/映射/版本，并给出原因；EWMA 不填补缺失。"""
import json

from xueban.api import xueban_teacher as t
from xueban.services import learning_analysis as la
from xueban.services import xueban_store as db

USER = {"id": "teacher", "role": "teacher"}
NOW = "2026-01-01T00:00:00"
t.init()
la.ensure_model()

db.x("INSERT INTO teacher_topics (id,code,title,goal,course_id,standard_id,created_at) VALUES (?,?,?,?,?,?,?)", ("topic_t", "TT", "主题", "", "", "", NOW))
db.x("INSERT INTO teacher_indicators (id,code,topic_id,description) VALUES (?,?,?,?)", ("indicator_t", "IT", "topic_t", "知识点"))


def make_batch(batch_id, class_id, structure, versions=None):
    db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", (batch_id, f"{batch_id}.csv", "committed", "{}", "[]", "confirmed", NOW))
    db.x("INSERT INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", (class_id, class_id[-2:], class_id, 7, NOW))
    db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", (f"student_{class_id}", f"user_{class_id}", class_id, class_id, class_id, 7, NOW))
    for node, qids in structure.items():
        date = "2026-01-20" if node == "N1" else "2026-06-17"
        db.x("INSERT OR REPLACE INTO teacher_exams (id,exam_date,topics,description,available_questions,course_id,grading_rule_version,scoring_rule_version,mapping_version) VALUES (?,?,?,?,?,?,?,?,?)", (node, date, "", "", ",".join(qids), "", (versions or {}).get("grading_rule_version"), (versions or {}).get("scoring_rule_version"), (versions or {}).get("mapping_version")))
        for qid in qids:
            version_id = f"qv:{batch_id}:{qid}"
            db.x("INSERT OR REPLACE INTO teacher_question_versions (version_id,batch_id,question_id,topic_id,stem,max_score,scoring_points,indicator_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)", (version_id, batch_id, qid, "TT", f"题干{qid}", 2, f"{qid}P1 2分", "IT", NOW))
            answer_id = f"{batch_id}:{node}:{qid}"
            db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,original_answer,solution_process,original_score,max_score,grading_status,import_batch_id,exam_node_id,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (answer_id, f"student_{class_id}", qid, f"e:{node}:{qid}", 1, "2", "步骤", 2, 2, "graded", batch_id, node, version_id))
            steps = json.dumps([{"step_index": 1, "student_step": "步骤", "is_correct": True, "score": 2, "max_score": 2, "error_type": "", "judgment_reason": "ok", "scoring_point_links": [{"scoring_point_id": f"{qid}P1", "awarded_score": 2, "max_score": 2}]}], ensure_ascii=False)
            db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,is_correct,step_results_json,needs_review,status,created_at,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"g:{batch_id}:{node}:{qid}", answer_id, f"student_{class_id}", qid, f"e:{node}:{qid}", 1, 1, "deepseek-chat", 2, 2, "correct", steps, 0, "graded", NOW, version_id))


# 1) 题集不同：N1 两题、N2 一题；即使回填版本也必须 non_comparable
make_batch("batch_trend_struct", "class_t1", {"N1": ["Q1", "Q2"], "N2": ["Q1"]}, {"grading_rule_version": "g1", "scoring_rule_version": "s1", "mapping_version": "m1"})
struct = la.class_profile("batch_trend_struct", "class_t1")["trends"]

# 2) 题集与版本一致：comparable
make_batch("batch_trend_ok", "class_t2", {"N1": ["Q1"], "N2": ["Q1"]}, {"grading_rule_version": "g1", "scoring_rule_version": "s1", "mapping_version": "m1"})
ok = la.class_profile("batch_trend_ok", "class_t2")["trends"]

# 3) 题集一致但缺版本：unverifiable
make_batch("batch_trend_unver", "class_t3", {"N1": ["Q1"], "N2": ["Q1"]})
unver = la.class_profile("batch_trend_unver", "class_t3")["trends"]

print(json.dumps({
    "struct": [{"status": item["comparable_status"], "reason": item["comparable_reason"], "diff": item["comparability_detail"].get("signature_diff")} for item in struct],
    "ok": [{"status": item["comparable_status"], "reason": item["comparable_reason"], "ewma": item["ewma_score_rate"]} for item in ok],
    "unver": [{"status": item["comparable_status"], "reason": item["comparable_reason"]} for item in unver],
    "ewma_none": la.ewma([50, None, 70], 0.5),
    "ewma_basic": la.ewma([10, 20, 30], 0.5),
}, ensure_ascii=False))
