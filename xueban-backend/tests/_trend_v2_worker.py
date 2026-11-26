"""P1 趋势验收：版本不同必须 non_comparable；显式兼容才 comparable；学生群体变化给出警告。"""
import json

from xueban.api import xueban_teacher as t
from xueban.services import learning_analysis as la
from xueban.services import xueban_store as db

NOW = "2026-01-01T00:00:00"
t.init()
la.ensure_model()
db.x("INSERT INTO teacher_topics (id,code,title,goal,course_id,standard_id,created_at) VALUES (?,?,?,?,?,?,?)", ("topic_t", "TT", "主题", "", "", "", NOW))
db.x("INSERT INTO teacher_indicators (id,code,topic_id,description) VALUES (?,?,?,?)", ("indicator_t", "IT", "topic_t", "知识点"))


def make_batch(batch_id, class_id, structure, versions, student_count=1):
    db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", (batch_id, f"{batch_id}.csv", "committed", "{}", "[]", "confirmed", NOW))
    db.x("INSERT INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", (class_id, class_id[-2:], class_id, 7, NOW))
    for index in range(student_count):
        sid = f"student_{class_id}_{index}"
        db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", (sid, f"user_{sid}", f"{class_id}{index}", class_id, f"S{index}", 7, NOW))
    for node, qids in structure.items():
        date = "2026-01-20" if node.endswith("1") else "2026-06-17"
        fields = versions.get(node, {})
        db.x("INSERT OR REPLACE INTO teacher_exams (id,exam_date,topics,description,available_questions,course_id,grading_rule_version,scoring_rule_version,mapping_version) VALUES (?,?,?,?,?,?,?,?,?)", (node, date, "", "", ",".join(qids), "", fields.get("grading_rule_version"), fields.get("scoring_rule_version"), fields.get("mapping_version")))
        for qid in qids:
            version_id = f"qv:{batch_id}:{qid}"
            db.x("INSERT OR REPLACE INTO teacher_question_versions (version_id,batch_id,question_id,topic_id,stem,max_score,scoring_points,indicator_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)", (version_id, batch_id, qid, "TT", f"题干{qid}", 2, f"{qid}P1 2分", "IT", NOW))
            for index in range(student_count):
                answer_id = f"{batch_id}:{node}:{qid}:{index}"
                db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,original_answer,solution_process,original_score,max_score,grading_status,import_batch_id,exam_node_id,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (answer_id, f"student_{class_id}_{index}", qid, f"e:{node}:{qid}:{index}", 1, "2", "步骤", 2, 2, "graded", batch_id, node, version_id))
                steps = json.dumps([{"step_index": 1, "student_step": "步骤", "is_correct": True, "score": 2, "max_score": 2, "error_type": "", "judgment_reason": "ok", "scoring_point_links": [{"scoring_point_id": f"{qid}P1", "awarded_score": 2, "max_score": 2}]}], ensure_ascii=False)
                db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,is_correct,step_results_json,needs_review,status,created_at,question_version_id,mapping_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"g:{answer_id}", answer_id, f"student_{class_id}_{index}", qid, f"e:{node}:{qid}:{index}", 1, 1, "deepseek-chat", 2, 2, "correct", steps, 0, "graded", NOW, version_id, "0"))


# A. 规则版本不同 → non_comparable；显式兼容后 → comparable
make_batch("batch_v_diff", "class_v1", {"N11": ["Q001"], "N12": ["Q001"]}, {"N11": {"grading_rule_version": "g1", "scoring_rule_version": "s1", "mapping_version": "m1"}, "N12": {"grading_rule_version": "g2", "scoring_rule_version": "s1", "mapping_version": "m1"}})
diff = la.class_profile("batch_v_diff", "class_v1")["trends"]
sig1, sig2 = la.node_signature("batch_v_diff", "N11"), la.node_signature("batch_v_diff", "N12")
diff_compare = la.compare_signatures(sig1, sig2)
db.x("INSERT INTO teacher_version_compat (id,kind,left_version,right_version,status,basis,note,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)", ("compat1", "grading_rule_version", "g1", "g2", "compatible", "规则差异经教研组书面确认等价", "", "teacher", NOW))
compat_compare = la.compare_signatures(sig1, sig2)

# B. 完全相同版本 → comparable
make_batch("batch_v_same", "class_v2", {"N21": ["Q001"], "N22": ["Q001"]}, {"N21": {"grading_rule_version": "g1", "scoring_rule_version": "s1", "mapping_version": "m1"}, "N22": {"grading_rule_version": "g1", "scoring_rule_version": "s1", "mapping_version": "m1"}})
same = la.class_profile("batch_v_same", "class_v2")["trends"]

# C. 学生群体变化：1 人 vs 2 人 → comparable 但给出重合率警告
make_batch("batch_overlap", "class_v3", {"N31": ["Q001"], "N32": ["Q001"]}, {"N31": {"grading_rule_version": "g1", "scoring_rule_version": "s1", "mapping_version": "m1"}, "N32": {"grading_rule_version": "g1", "scoring_rule_version": "s1", "mapping_version": "m1"}}, student_count=1)
db.x("UPDATE teacher_exams SET grading_rule_version='g1', scoring_rule_version='s1', mapping_version='m1' WHERE id IN ('N31','N32')")
# 给 N32 增加第二个学生
db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", ("student_extra", "user_extra", "EXTRA", "class_v3", "EXTRA", 7, NOW))
db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,original_answer,solution_process,original_score,max_score,grading_status,import_batch_id,exam_node_id,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", ("batch_overlap:N32:extra", "student_extra", "Q001", "e:extra", 1, "2", "步骤", 2, 2, "graded", "batch_overlap", "N32", "qv:batch_overlap:Q001"))
overlap = la.compare_signatures(la.node_signature("batch_overlap", "N31"), la.node_signature("batch_overlap", "N32"))

print(json.dumps({
    "diff": [{"status": item["comparable_status"], "reason": item["comparable_reason"]} for item in diff],
    "compat_required": {"status": diff_compare["status"], "reason": diff_compare["reason"]},
    "compat_after": {"status": compat_compare["status"], "reason": compat_compare["reason"]},
    "same": [{"status": item["comparable_status"], "reason": item["comparable_reason"]} for item in same],
    "overlap": {"status": overlap["status"], "rate": overlap["student_overlap_rate"], "warning": overlap["group_change_warning"], "only_left": overlap["student_only_left"], "only_right": overlap["student_only_right"]},
}, ensure_ascii=False))
