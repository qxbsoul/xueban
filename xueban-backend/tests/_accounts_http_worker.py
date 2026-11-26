"""D7 回归（HTTP）：学生账号发放、首次改密、重置、绑定与数据隔离。"""
import json
import os
import warnings

os.environ.setdefault("DEEPSEEK_API_KEY", "test-key-not-real")
warnings.filterwarnings("ignore")

from fastapi.testclient import TestClient

from tests.support_import import answer, make_files, question, student
from xueban.server import app
from xueban.services import xueban_store as db

client = TestClient(app)
teacher = client.post("/api/xueban/auth/register", json={"email": "owner@test.local", "password": "Passw0rd!", "display_name": "教师", "company_name": "测试组", "role": "teacher"})
assert teacher.status_code == 200, teacher.text
th = {"Authorization": f"Bearer {teacher.json()['token']}"}

files = make_files(
    [answer(event="E1", student_code="S01"), answer(event="E2", student_code="S02")],
    questions=[question()],
    students=[student("S01"), student("S02", "C2")],
)
payload = [("files", (item.filename, item.file.read(), "text/csv")) for item in files]
imported = client.post("/api/teacher/imports", files=payload, headers=th)
assert imported.status_code == 200, imported.text
batch_id = imported.json()["batch_id"]
credentials = imported.json()["student_credentials"]
assert len(credentials) == 2, imported.text
client.post(f"/api/teacher/batches/{batch_id}/preprocessing/confirm", headers=th)

s01 = next(item for item in credentials if item["code"] == "S01")
login = client.post("/api/xueban/auth/login", json={"email": s01["email"], "password": s01["initial_password"], "role": "student"})
sh = {"Authorization": f"Bearer {login.json()['token']}"}
me = client.get("/api/teacher/analysis/me", headers=sh)
own = client.get(f"/api/teacher/analysis/students/{db.q1('SELECT id FROM teacher_students WHERE code=?', ('S02',))['id']}", headers=sh)
changed = client.post("/api/xueban/auth/password", json={"current_password": s01["initial_password"], "new_password": "NewPassw0rd!"}, headers=sh)
relogin = client.post("/api/xueban/auth/login", json={"email": s01["email"], "password": "NewPassw0rd!", "role": "student"})
old_login = client.post("/api/xueban/auth/login", json={"email": s01["email"], "password": s01["initial_password"], "role": "student"})

student_row = db.q1("SELECT * FROM teacher_students WHERE code='S01'")
reset = client.post(f"/api/teacher/students/{student_row['id']}/reset-password", headers=th)
reset_login = client.post("/api/xueban/auth/login", json={"email": s01["email"], "password": reset.json()["initial_password"], "role": "student"})

# 自注册学生账号 → 未绑定列表 → 绑定到花名册学生 S02
self_reg = client.post("/api/xueban/auth/register", json={"email": "self@test.local", "password": "SelfPassw0rd!", "display_name": "自注册学生", "company_name": "个人空间", "role": "student"})
unbound = client.get("/api/teacher/student-accounts/unbound", headers=th).json()["items"]
db.x("UPDATE teacher_students SET user_id='' WHERE code='S02'")
s02_row = db.q1("SELECT * FROM teacher_students WHERE code='S02'")
self_user_id = self_reg.json()["user"]["id"]
bound = client.post(f"/api/teacher/students/{s02_row['id']}/bind-account", json={"email": "self@test.local"}, headers=th)
self_me = client.get("/api/teacher/analysis/me", headers={"Authorization": f"Bearer {self_reg.json()['token']}"})

account_row = db.q1("SELECT * FROM xueban_users WHERE email=?", (s01["email"],))
credential_rows = db.q("SELECT * FROM xueban_account_audit WHERE user_id=? ORDER BY created_at", (account_row["id"],))

print(json.dumps({
    "credential_count": len(credentials),
    "credential_has_password": bool(s01.get("initial_password")),
    "login_status": login.status_code,
    "login_must_change": login.json()["user"]["must_change_password"],
    "me_status": me.status_code,
    "me_student": me.json()["student"]["code"] if me.status_code == 200 else None,
    "other_student_status": own.status_code,
    "change_status": changed.status_code,
    "relogin_status": relogin.status_code,
    "relogin_must_change": relogin.json()["user"]["must_change_password"],
    "old_password_status": old_login.status_code,
    "reset_status": reset.status_code,
    "reset_login_status": reset_login.status_code,
    "reset_must_change": reset_login.json()["user"]["must_change_password"],
    "unbound_contains_self": any(item["email"] == "self@test.local" for item in unbound),
    "bind_status": bound.status_code,
    "self_me_status": self_me.status_code,
    "self_me_student": self_me.json()["student"]["code"] if self_me.status_code == 200 else None,
    "password_stored_hashed": account_row["password_hash"] != s01["initial_password"] and len(account_row["password_hash"]) == 64,
    "audit_actions": sorted({row["action"] for row in credential_rows}),
}, ensure_ascii=False))
