"""v1.5.10 → v1.5.11 升级路径：旧表缺 owner_id 时 init/bootstrap 必须成功且不丢数据。"""
import json

from xueban.api import xueban_teacher as t
from xueban.services import xueban_store as db

t.init()
db.x("INSERT INTO xueban_users (id,email,display_name,auth_type,role,created_at) VALUES (?,?,?,?,?,?)", ("old_guest", "old-guest@test.local", "旧游客", "guest", "teacher", "2026-01-01T00:00:00"))
db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", ("old_batch", "old.csv", "committed", "{}", "[]", "confirmed", "2026-01-01T00:00:00"))
db.x("DROP INDEX IF EXISTS idx_teacher_batches_owner")
db.x("ALTER TABLE teacher_batches DROP COLUMN owner_id")
t.reset_runtime_cache()
t.init()
t.bootstrap()

columns = {row["name"] for row in db.q("PRAGMA table_info(teacher_batches)")}
print(json.dumps({
    "has_owner": "owner_id" in columns,
    "batch_preserved": db.q1("SELECT COUNT(*) n FROM teacher_batches WHERE id='old_batch'")["n"],
    "guest_role": db.q1("SELECT role FROM xueban_users WHERE id='old_guest'")["role"],
    "migrations": [row["version"] for row in db.q("SELECT version FROM schema_migrations ORDER BY version")],
    "index": db.q1("SELECT COUNT(*) n FROM sqlite_master WHERE type='index' AND name='idx_teacher_batches_owner'")["n"],
}, ensure_ascii=False))
