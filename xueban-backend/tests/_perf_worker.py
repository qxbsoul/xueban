"""P4 回归：请求级 init 不再全量回填；分析快照去重 + 保留上限。"""
import json
import time

from xueban.api import xueban_teacher as t
from xueban.services import learning_analysis as la
from xueban.services import xueban_store as db

t.init()
t.bootstrap()
t.bootstrap()

writes = []


def trace(sql):
    if not sql.strip().upper().startswith("SELECT"):
        writes.append(sql)


db.conn().set_trace_callback(trace)
start = time.perf_counter()
for _ in range(20):
    t.init()
elapsed = time.perf_counter() - start
db.conn().set_trace_callback(None)

migrations = db.q("SELECT version FROM schema_migrations ORDER BY version")

la.ensure_model()
first = la.save_analysis("batch_perf", "class", "class_1", {"a": 1, "b": [1, 2, 3]})
second = la.save_analysis("batch_perf", "class", "class_1", {"a": 1, "b": [1, 2, 3]})
count_after_duplicate = db.q1("SELECT COUNT(*) n FROM teacher_analysis_runs")["n"]
for index in range(10):
    la.save_analysis("batch_perf", "class", "class_1", {"a": index})
final_rows = db.q("SELECT * FROM teacher_analysis_runs ORDER BY created_at")
last_id = la.save_analysis("batch_perf", "class", "class_1", {"a": 10})

print(json.dumps({
    "init_20_seconds": round(elapsed, 4),
    "init_write_statements": len(writes),
    "migrations": [row["version"] for row in migrations],
    "snapshot_same_id": first == second,
    "snapshot_count_after_duplicate": count_after_duplicate,
    "snapshot_final_count": len(final_rows),
    "snapshot_max_retained": la.ANALYSIS_RETENTION,
    "snapshot_latest_payload_hash_present": bool(db.q1("SELECT result_hash FROM teacher_analysis_runs WHERE id=?", (last_id,))["result_hash"]),
}, ensure_ascii=False))
