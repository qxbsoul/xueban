"""迁移中途失败：v4 的角色修改与审计必须整体回滚，失败可观测，重试成功。"""
import json

from fastapi.testclient import TestClient

from xueban.api import xueban_teacher as t
from xueban.server import app
from xueban.services import xueban_store as db

t.init()
now = "2026-01-01T00:00:00"
db.x("INSERT INTO xueban_users (id,email,display_name,auth_type,role,created_at) VALUES (?,?,?,?,?,?)", ("g1", "g1@test.local", "游客1", "guest", "teacher", now))
db.x("INSERT INTO xueban_users (id,email,display_name,auth_type,role,created_at) VALUES (?,?,?,?,?,?)", ("t1", "t1@test.local", "教师", "password", "teacher", now))
before = {"roles": {row["id"]: row["role"] for row in db.q("SELECT id, role FROM xueban_users ORDER BY id")}, "audit": db.q1("SELECT COUNT(*) n FROM xueban_account_audit")["n"]}
original_v4 = t.MIGRATIONS[4]


def broken_v4():
    db.x("UPDATE xueban_users SET role='guest' WHERE id='g1'")
    db.x("INSERT INTO xueban_account_audit (id,user_id,actor_id,action,detail_json,created_at) VALUES (?,?,?,?,?,?)", ("audit_broken", "g1", "system", "role_migration", "{}", now))
    raise RuntimeError("injected v4 failure password=SuperSecret sk-abc123456789")


t.MIGRATIONS[4] = broken_v4
failure = None
try:
    t.bootstrap()
except Exception as exc:
    failure = t.sanitize_migration_error(str(exc))
after_failure = {
    "roles": {row["id"]: row["role"] for row in db.q("SELECT id, role FROM xueban_users ORDER BY id")},
    "audit": db.q1("SELECT COUNT(*) n FROM xueban_account_audit")["n"],
    "migrations": [row["version"] for row in db.q("SELECT version FROM schema_migrations ORDER BY version")],
    "failures": [dict(row) for row in db.q("SELECT version, error FROM schema_migration_failures ORDER BY id DESC")],
    "state": t.migration_state(),
}
client = TestClient(app)
status_response = client.get("/api/teacher/migrations/status")
blocked_response = client.get("/api/teacher/batches")
import asyncio
import xueban.server as server_module
original_bootstrap = server_module._xueban_teacher_api.bootstrap
server_module._xueban_teacher_api.bootstrap = lambda: (_ for _ in ()).throw(RuntimeError("startup boom"))
startup_failed = False
try:
    asyncio.run(server_module._bootstrap_teacher_store())
except RuntimeError:
    startup_failed = True
finally:
    server_module._xueban_teacher_api.bootstrap = original_bootstrap
t.MIGRATIONS[4] = original_v4
t.bootstrap()
after_retry = {
    "roles": {row["id"]: row["role"] for row in db.q("SELECT id, role FROM xueban_users ORDER BY id")},
    "audit": db.q1("SELECT COUNT(*) n FROM xueban_account_audit")["n"],
    "migrations": [row["version"] for row in db.q("SELECT version FROM schema_migrations ORDER BY version")],
    "status": t.migration_status(),
}
print(json.dumps({
    "failure": failure,
    "before": before,
    "after_failure": after_failure,
    "status_http": {"status_code": status_response.status_code, "body": status_response.json()},
    "blocked_http": {"status_code": blocked_response.status_code, "body": blocked_response.json()},
    "startup_failed": startup_failed,
    "after_retry": after_retry,
}, ensure_ascii=False))
