"""P0 权限矩阵（HTTP）：游客/学生/教师/未登录；跨教师、跨学生、跨任务资源隔离。"""
import json
import os
import warnings

os.environ.setdefault("DEEPSEEK_API_KEY", "test-key-not-real")
warnings.filterwarnings("ignore")

from fastapi.testclient import TestClient

from tests.support_import import answer, make_files, question, student
from xueban.api import xueban_teacher as teacher_api
from xueban.server import app
from xueban.services import learning_analysis as analysis
from xueban.services import xueban_store as db

teacher_api.init()
analysis.ensure_model()
# 历史脏数据：guest 账号曾以 teacher 角色存在
db.x("INSERT INTO xueban_users (id,email,display_name,auth_type,role,created_at) VALUES (?,?,?,?,?,?)", ("legacy_guest", "legacy-guest@test.local", "历史游客", "guest", "teacher", "2026-01-01T00:00:00"))
# role 为空的密码账号：不得升级为教师
import hashlib, secrets
salt = secrets.token_hex(16)
digest = hashlib.pbkdf2_hmac("sha256", b"Unknown123!", bytes.fromhex(salt), 200_000).hex()
db.x("INSERT INTO xueban_users (id,email,password_hash,password_salt,display_name,auth_type,role,created_at) VALUES (?,?,?,?,?,?,?,?)", ("legacy_null", "legacy-null@test.local", digest, salt, "未知角色", "password", None, "2026-01-01T00:00:00"))
teacher_api.bootstrap()

client = TestClient(app)


def register(email, name, role):
    response = client.post("/api/xueban/auth/register", json={"email": email, "password": "Passw0rd!", "display_name": name, "company_name": name + "组", "role": role})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}, response.json()["user"]


def upload(headers, code, event):
    files = make_files([answer(event=event, student_code=code)], questions=[question()], students=[student(code)])
    payload = [("files", (item.filename, item.file.read(), "text/csv")) for item in files]
    response = client.post("/api/teacher/imports", files=payload, headers=headers)
    assert response.status_code == 200, response.text
    batch_id = response.json()["batch_id"]
    client.post(f"/api/teacher/batches/{batch_id}/preprocessing/confirm", headers=headers)
    return batch_id


headers_a, user_a = register("teacher-a@test.local", "教师A", "teacher")
headers_b, user_b = register("teacher-b@test.local", "教师B", "teacher")
headers_s, user_s = register("student-x@test.local", "学生X", "student")

batch_a = upload(headers_a, "TA01", "EA1")
analysis.mapping_for_batch(batch_a)
batch_b = upload(headers_b, "TB01", "EB1")
batch_legacy = upload(headers_a, "TL01", "EL1")
db.x("UPDATE teacher_batches SET owner_id=NULL WHERE id=?", (batch_legacy,))

answer_a = db.q1("SELECT id FROM teacher_answers WHERE import_batch_id=?", (batch_a,))["id"]
scoring_point_a = db.q1("SELECT id FROM scoring_points WHERE batch_id=? LIMIT 1", (batch_a,))["id"]
answer_b = db.q1("SELECT id FROM teacher_answers WHERE import_batch_id=?", (batch_b,))["id"]
student_a = db.q1("SELECT id FROM teacher_students WHERE code='TA01'")["id"]
student_b = db.q1("SELECT id FROM teacher_students WHERE code='TB01'")["id"]
for aid, score in ((answer_a, 3), (answer_b, 4)):
    row = db.q1("SELECT * FROM teacher_answers WHERE id=?", (aid,))
    db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,status,needs_review,step_results_json,created_at,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"g-{aid}", aid, row["student_id"], row["question_id"], row["event_id"], row["revision"], 1, "deepseek-chat", score, 5, "graded", 0, "[]", "2026-01-01T00:00:00", row["question_version_id"]))
grade_a = f"g-{answer_a}"
grade_b = f"g-{answer_b}"

# 学生 X 绑定到 B 的档案，用于学生本人资源与跨学生越权测试
db.x("UPDATE teacher_students SET user_id=? WHERE id=?", (user_s["id"], student_b))

guest = client.post("/api/xueban/auth/guest", json={})
guest_headers = {"Authorization": f"Bearer {guest.json()['token']}"}
anon = {}

roles = {
    "teacher_a": headers_a,
    "teacher_b": headers_b,
    "student_x": headers_s,
    "guest": guest_headers,
    "anon": anon,
}

checks = {
    "batches": ("/api/teacher/batches", 200),
    "summary": ("/api/teacher/summary", 200),
    "students": ("/api/teacher/students", 200),
    "answers": ("/api/teacher/answers", 200),
    "analysis_classes": ("/api/teacher/analysis/classes", 200),
}
matrix = {}
for role, headers in roles.items():
    matrix[role] = {}
    for name, (path, _) in checks.items():
        response = client.get(path, headers=headers)
        matrix[role][name] = response.status_code

matrix["teacher_a"]["batch_b_detail"] = client.get(f"/api/teacher/batches/{batch_b}", headers=headers_a).status_code
matrix["teacher_b"]["batch_a_detail"] = client.get(f"/api/teacher/batches/{batch_a}", headers=headers_b).status_code
matrix["teacher_a"]["answer_b"] = client.get(f"/api/teacher/answers/{answer_b}", headers=headers_a).status_code
matrix["teacher_b"]["grade_a"] = client.get(f"/api/teacher/results/{grade_a}", headers=headers_b).status_code
matrix["teacher_a"]["student_b"] = client.get(f"/api/teacher/students/{student_b}", headers=headers_a).status_code
matrix["teacher_a"]["analysis_student_b"] = client.get(f"/api/teacher/analysis/students/{student_b}?batch_id={batch_b}", headers=headers_a).status_code
matrix["teacher_a"]["legacy_batch"] = client.get(f"/api/teacher/batches/{batch_legacy}", headers=headers_a).status_code
matrix["teacher_a"]["own_batch"] = client.get(f"/api/teacher/batches/{batch_a}", headers=headers_a).status_code
matrix["teacher_b"]["scoring_point_a"] = client.get(f"/api/teacher/scoring-points/{scoring_point_a}", headers=headers_b).status_code
matrix["teacher_a"]["scoring_point_a"] = client.get(f"/api/teacher/scoring-points/{scoring_point_a}", headers=headers_a).status_code
db.x("INSERT INTO learning_knowledge_edges (id,batch_id,from_kp,to_kp,source,status,confidence,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", ("edge_a", batch_a, "kp:I01", "kp:I02", "teacher", "confirmed", 1.0, user_a["id"], "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
matrix["teacher_b"]["edge_delete_a"] = client.delete("/api/teacher/knowledge-graph/edges/edge_a", headers=headers_b).status_code
matrix["teacher_a"]["edge_delete_a"] = client.delete("/api/teacher/knowledge-graph/edges/edge_a", headers=headers_a).status_code
matrix["teacher_b"]["graph_candidates_a"] = client.post("/api/teacher/knowledge-graph/candidates", json={"batch_id": batch_a}, headers=headers_b).status_code
matrix["teacher_b"]["graph_edge_add_a"] = client.post("/api/teacher/knowledge-graph/edges", json={"batch_id": batch_a, "from_kp": "kp:I01", "to_kp": "kp:I02"}, headers=headers_b).status_code
matrix["teacher_b"]["version_compat_a"] = client.post("/api/teacher/version-compat", json={"batch_id": batch_a, "kind": "grading_rule_version", "left_version": "g1", "right_version": "g2", "basis": "x"}, headers=headers_b).status_code
matrix["student_x"]["teacher_batch"] = client.get(f"/api/teacher/batches/{batch_a}", headers=headers_s).status_code
matrix["student_x"]["teacher_summary"] = client.get("/api/teacher/summary", headers=headers_s).status_code
matrix["student_x"]["own_me"] = client.get("/api/teacher/analysis/me", headers=headers_s).status_code
matrix["student_x"]["other_student_analysis"] = client.get(f"/api/teacher/analysis/students/{student_a}", headers=headers_s).status_code
matrix["guest"]["me"] = client.get("/api/xueban/auth/me", headers=guest_headers).status_code
matrix["guest"]["real_batch"] = client.get(f"/api/teacher/batches/{batch_a}", headers=guest_headers).status_code
matrix["guest"]["real_answer"] = client.get(f"/api/teacher/answers/{answer_a}", headers=guest_headers).status_code
matrix["guest"]["analysis_me"] = client.get("/api/teacher/analysis/me", headers=guest_headers).status_code
matrix["guest"]["delete_batch"] = client.delete(f"/api/teacher/batches/{batch_a}", headers=guest_headers).status_code
matrix["guest"]["password_login"] = client.post("/api/xueban/auth/login", json={"email": "legacy-guest@test.local", "password": "anything", "role": "guest"}).status_code

guest_role = client.get("/api/xueban/auth/me", headers=guest_headers).json()["user"]["role"]
legacy_roles = {row["id"]: row["role"] for row in db.q("SELECT id, role FROM xueban_users WHERE id IN ('legacy_guest','legacy_null')")}
audit_count = db.q1("SELECT COUNT(*) n FROM xueban_account_audit WHERE action='role_migration'")["n"]
counts = {
    "teacher_a_batches": [row["id"] for row in client.get("/api/teacher/batches", headers=headers_a).json()["items"]],
    "teacher_b_batches": [row["id"] for row in client.get("/api/teacher/batches", headers=headers_b).json()["items"]],
    "own_batches": {"a": batch_a, "b": batch_b, "legacy": batch_legacy},
}
anon_routes = {}
openapi_paths = app.openapi().get("paths", {})
for path, operations in openapi_paths.items():
    if not path.startswith("/api/teacher") or path == "/api/teacher/migrations/status":
        continue
    for method in set(operations) & {"get", "post", "delete"}:
        response = client.request(method.upper(), path.replace("{batch_id}", batch_a).replace("{answer_id}", answer_a).replace("{grade_id}", grade_a).replace("{job_id}", "missing").replace("{student_id}", student_a).replace("{class_id}", "class_DC01").replace("{question_id}", "Q001").replace("{knowledge_point_id}", "kp:I01").replace("{edge_id}", "missing").replace("{feedback_id}", "missing").replace("{sp_id}", scoring_point_a))
        anon_routes[f"{method.upper()} {path}"] = response.status_code

print(json.dumps({
    "matrix": matrix, "guest_role": guest_role, "legacy_roles": legacy_roles,
    "audit_count": audit_count, "counts": counts,
    "student_own_answers": len(client.get("/api/teacher/answers", headers=headers_s).json().get("items", [])),
    "anon_routes": anon_routes,
}, ensure_ascii=False))
