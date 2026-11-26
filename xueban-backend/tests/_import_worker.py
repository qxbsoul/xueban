"""D1 回归：导入去重必须限定在任务内，跨任务相同 event_id 不能互相吞掉。"""
import json

from fastapi import HTTPException

from tests.support_import import answer, make_files, question
from xueban.api import xueban_teacher as t
from xueban.services import xueban_store as db

USER = {"id": "teacher", "role": "teacher"}

t.init()

# 1) 任务 A：Q001 满分 5、题干 A
a = t.upload_import(make_files(
    [answer(event="E-SAME", final="A 的答案", process="A 的步骤", max_score="5")],
    questions=[question(stem="题干-A", max_score="5", scoring_points="Q001P1 5分")],
), USER)

# 2) 任务 B：相同 event_id + revision，但题目满分/题干不同 —— 必须保留
b = t.upload_import(make_files(
    [answer(event="E-SAME", final="B 的答案", process="B 的步骤", max_score="10")],
    questions=[question(stem="题干-B", max_score="10", scoring_points="Q001P1 10分")],
), USER)

# 3) 再次导入 A 的同一份 CSV（新任务），不得因为已有 event 而被跳过
c = t.upload_import(make_files(
    [answer(event="E-SAME", final="A 的答案", process="A 的步骤", max_score="5")],
    questions=[question(stem="题干-A", max_score="5", scoring_points="Q001P1 5分")],
), USER)

# 4) 同一任务内的重复行仍然必须被识别：同一批数据里出现相同 event+revision 时，
#    import_rows 的去重守卫只跳过重复行，不会把整批作答吞掉。
dup_tables = t.read_csvs(make_files([answer(event="E-DUP", revision=1), answer(event="E-DUP", revision=1)], questions=[question()]))[0]
same_batch = t.import_rows(dup_tables, "dup.csv")

# 5) 同一文件内重复 event+revision 必须在校验阶段被阻断
validate_files = make_files([answer(event="E-1", revision=1), answer(event="E-1", revision=1)])
validate_rows, validate_meta = t.read_csvs(validate_files)
validate_errors = [e for e in t.validate(validate_rows, validate_meta) if e.get("type") == "duplicate_event_revision"]
blocked = 0
try:
    t.upload_import(make_files([answer(event="E-1", revision=1), answer(event="E-1", revision=1)]), USER)
except HTTPException as exc:
    blocked = exc.status_code

payload = {
    "a_counts": a["counts"], "b_counts": b["counts"], "c_counts": c["counts"],
    "same_event_answers": db.q1("SELECT COUNT(*) n FROM teacher_answers WHERE event_id='E-SAME'")["n"],
    "a_answer": db.q1("SELECT original_answer FROM teacher_answers WHERE import_batch_id=?", (a["batch_id"],)),
    "b_answer": db.q1("SELECT original_answer FROM teacher_answers WHERE import_batch_id=?", (b["batch_id"],)),
    "a_version": db.q1("SELECT stem,max_score FROM teacher_question_versions WHERE batch_id=?", (a["batch_id"],)),
    "b_version": db.q1("SELECT stem,max_score FROM teacher_question_versions WHERE batch_id=?", (b["batch_id"],)),
    "fixed_first": same_batch["counts"],
    "fixed_rows": db.q1("SELECT COUNT(*) n FROM teacher_answers WHERE import_batch_id=?", (same_batch["batch_id"],))["n"],
    "fixed_errors": [e for e in same_batch["errors"] if e.get("type") == "duplicate_answer_in_batch"],
    "validate_duplicate_errors": len(validate_errors), "blocked_status": blocked,
}
print(json.dumps(payload, ensure_ascii=False))
