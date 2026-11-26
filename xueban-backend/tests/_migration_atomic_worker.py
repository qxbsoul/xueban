"""v4/v5 迁移原子性、幂等性与数据对照（隔离临时库）。"""
import json

from xueban.api import xueban_teacher as t
from xueban.services import xueban_store as db

t.init()
now = "2026-01-01T00:00:00"
db.x("INSERT INTO xueban_users (id,email,display_name,auth_type,role,created_at) VALUES (?,?,?,?,?,?)", ("g1", "g1@test.local", "游客1", "guest", "teacher", now))
db.x("INSERT INTO xueban_users (id,email,display_name,auth_type,role,created_at) VALUES (?,?,?,?,?,?)", ("g2", "g2@test.local", "游客2", "guest", None, now))
db.x("INSERT INTO xueban_users (id,email,display_name,auth_type,role,created_at) VALUES (?,?,?,?,?,?)", ("t1", "t1@test.local", "教师", "password", "teacher", now))
db.x("INSERT INTO xueban_users (id,email,display_name,auth_type,role,created_at) VALUES (?,?,?,?,?,?)", ("s1", "s1@test.local", "学生", "password", "student", now))
db.x("INSERT INTO xueban_users (id,email,display_name,auth_type,role,created_at) VALUES (?,?,?,?,?,?)", ("n1", "n1@test.local", "无角色", "password", None, now))
db.x("INSERT INTO xueban_users (id,email,display_name,auth_type,role,created_at) VALUES (?,?,?,?,?,?)", ("u1", "u1@test.local", "未知角色", "password", "admin", now))
db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", ("batch_m", "m.csv", "committed", "{}", "[]", "confirmed", now))
db.x("INSERT INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", ("class_m", "CM", "班级", 7, now))
db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", ("student_m", "s1", "M01", "class_m", "M01", 7, now))
db.x("INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,grading_status,import_batch_id,max_score,original_score) VALUES (?,?,?,?,?,?,?,?,?)", ("answer_m", "student_m", "Q001", "E1", 1, "pending", "batch_m", 5, 0))
db.x("INSERT INTO teacher_questions (id,stem,max_score,scoring_points) VALUES (?,?,?,?)", ("Q001", "题干", 5, "Q001P1 5分"))

before = {
    "roles": {row["id"]: row["role"] for row in db.q("SELECT id, role FROM xueban_users ORDER BY id")},
    "audit": db.q1("SELECT COUNT(*) n FROM xueban_account_audit")["n"],
}
t.bootstrap()
first = {
    "roles": {row["id"]: row["role"] for row in db.q("SELECT id, role FROM xueban_users ORDER BY id")},
    "audit": db.q1("SELECT COUNT(*) n FROM xueban_account_audit")["n"],
    "enrollments": [dict(row) for row in db.q("SELECT * FROM teacher_enrollments")],
    "migrations": [row["version"] for row in db.q("SELECT version FROM schema_migrations ORDER BY version")],
    "status": t.migration_status(),
}
t.bootstrap()
second = {
    "roles": {row["id"]: row["role"] for row in db.q("SELECT id, role FROM xueban_users ORDER BY id")},
    "audit": db.q1("SELECT COUNT(*) n FROM xueban_account_audit")["n"],
    "enrollments": db.q1("SELECT COUNT(*) n FROM teacher_enrollments")["n"],
    "migrations": [row["version"] for row in db.q("SELECT version FROM schema_migrations ORDER BY version")],
}
print(json.dumps({
    "before": before,
    "first": first,
    "second": second,
    "expected_enrollments": 1,
    "audit_delta": first["audit"] - before["audit"],
}, ensure_ascii=False))
