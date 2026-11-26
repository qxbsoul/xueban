"""P5 集成回归（HTTP 全链路）：导入 → 闸门 → 映射 → 判题(mock) → 学情 → 错误分类 → 推荐 → 依赖图 → 删除。"""
import json
import os
import re
import warnings

os.environ.setdefault("DEEPSEEK_API_KEY", "test-key-not-real")
warnings.filterwarnings("ignore")

from fastapi.testclient import TestClient

from tests.support_import import answer, make_files, question, requirement, student
from xueban.server import app
from xueban.services import xueban_store as db
from xueban.api import xueban_teacher as teacher_api

client = TestClient(app)


def grader(ans, question_row, steps, key, provider, standard, textbook_context=""):
    codes = re.findall(r"[A-Z]\d{3}P\d+", str(question_row.get("scoring_points") or ""))
    code = codes[0] if codes else "Q001P1"
    return {
        "score": 3, "max_score": 5, "is_correct": "partial", "feedback": "部分正确", "judgment_reason": "第二步符号错误", "needs_review": False,
        "step_results": [{
            "step_index": 1, "student_step": "第一步", "is_correct": False, "score": 3, "max_score": 5,
            "error_type": "计算操作错误", "judgment_reason": "符号写错且计算错误",
            "scoring_point_links": [{"scoring_point_id": code, "awarded_score": 3, "max_score": 5}],
            "operation_units": [{"operation_id": "op1", "source_step_id": 1, "student_fragment": "第一步", "operation_type": "计算", "score": 3, "max_score": 5, "is_correct": False, "judgment_reason": "计算错误"}],
        }],
    }


teacher_api._LLM_CALLER = grader

register = client.post("/api/xueban/auth/register", json={"email": "flow@test.local", "password": "Passw0rd!", "display_name": "教师", "company_name": "测试组", "role": "teacher"})
assert register.status_code == 200, register.text
headers = {"Authorization": f"Bearer {register.json()['token']}"}

files = make_files(
    [
        answer(event="E1", student_code="S01", qid="Q001", final="答1", process="第一步\n第二步"),
        answer(event="E2", student_code="S02", qid="Q002", final="答2", process="第一步"),
    ],
    questions=[question(qid="Q001", indicator="I01", scoring_points="Q001P1 5分"), question(qid="Q002", indicator="I02", scoring_points="Q002P1 5分")],
    students=[student("S01"), student("S02", "C2")],
    requirements=[requirement("T01", "I01"), requirement("T02", "I02")],
)
payload = [("files", (item.filename, item.file.read(), "text/csv")) for item in files]
imported = client.post("/api/teacher/imports", files=payload, headers=headers)
assert imported.status_code == 200, imported.text
batch_id = imported.json()["batch_id"]
confirm = client.post(f"/api/teacher/batches/{batch_id}/preprocessing/confirm", headers=headers)
gate = client.get(f"/api/teacher/batches/{batch_id}/quality-gate", headers=headers).json()

mapping_before = client.get(f"/api/teacher/batches/{batch_id}/mapping", headers=headers).json()
sp = mapping_before["items"][0]
link = client.post(f"/api/teacher/scoring-points/{sp['scoring_point_id']}/knowledge-links", json={"links": [{"knowledge_point_id": "kp:I01", "weight": 1}]}, headers=headers)
audit = client.get(f"/api/teacher/analysis/mapping-audit?batch_id={batch_id}", headers=headers).json()
method = client.post("/api/teacher/methods", json={"code": "M1", "name": "代入消元法"}, headers=headers).json()["method"]
method_link = client.post(f"/api/teacher/scoring-points/{sp['scoring_point_id']}/methods", json={"links": [{"method_id": method["id"], "weight": 1}]}, headers=headers)

graded = client.post("/api/teacher/grade/batch", json={"batch_id": batch_id}, headers=headers)
job_id = graded.json()["job"]["id"] if graded.status_code == 200 else ""
job = client.get(f"/api/teacher/grade/jobs/{job_id}", headers=headers).json().get("job", {}) if job_id else {}
items = client.get(f"/api/teacher/grade/jobs/{job_id}", headers=headers).json().get("items", []) if job_id else []
attempts = client.get(f"/api/teacher/grade/jobs/{job_id}/failures", headers=headers).json().get("items", []) if job_id else []

classes = client.get(f"/api/teacher/analysis/classes?batch_id={batch_id}", headers=headers).json()["classes"]
class_detail = client.get(f"/api/teacher/analysis/classes/{classes[0]['id']}?batch_id={batch_id}", headers=headers).json()
question_detail = client.get(f"/api/teacher/analysis/classes/{classes[0]['id']}/questions/Q001?batch_id={batch_id}", headers=headers)
students = client.get("/api/teacher/students", headers=headers).json()["items"]
profile = client.get(f"/api/teacher/analysis/students/{students[0]['id']}?batch_id={batch_id}", headers=headers).json()
compare = client.get(f"/api/teacher/analysis/compare-detailed?batch_id={batch_id}&class_ids={classes[0]['id']},{classes[1]['id']}", headers=headers)

classify = client.post(f"/api/teacher/batches/{batch_id}/error-labels/classify", json={}, headers=headers)
labels = client.get(f"/api/teacher/batches/{batch_id}/error-labels", headers=headers).json()
recommend = client.get(f"/api/teacher/students/{students[0]['id']}/recommendations?batch_id={batch_id}", headers=headers)

graph = client.post("/api/teacher/knowledge-graph/candidates", json={"batch_id": batch_id}, headers=headers)
graph_view = client.get(f"/api/teacher/knowledge-graph?batch_id={batch_id}", headers=headers).json()

deleted = client.delete(f"/api/teacher/batches/{batch_id}", headers=headers)
residual = {
    "answers": db.q1("SELECT COUNT(*) n FROM teacher_answers WHERE import_batch_id=?", (batch_id,))["n"],
    "grades": db.q1("SELECT COUNT(*) n FROM teacher_grade_runs WHERE answer_id LIKE ?", (f"{batch_id}%",))["n"],
    "versions": db.q1("SELECT COUNT(*) n FROM teacher_question_versions WHERE batch_id=?", (batch_id,))["n"],
    "batch": db.q1("SELECT COUNT(*) n FROM teacher_batches WHERE id=?", (batch_id,))["n"],
}

print(json.dumps({
    "import_counts": imported.json()["counts"],
    "credentials": len(imported.json().get("student_credentials") or []),
    "confirm": confirm.status_code,
    "gate": {"blocking": gate["blocking"], "types": [item["type"] for item in gate["issues"]]},
    "mapping_before": [item["mapping_status"] for item in mapping_before["items"]],
    "link_status": link.status_code,
    "audit_confirmed": audit["confirmed_scoring_points"],
    "method_link": method_link.status_code,
    "grade_status": graded.status_code,
    "job": {"status": job.get("status"), "processed": job.get("processed"), "completed": job.get("completed")},
    "items": {"count": len(items), "completed": sum(1 for item in items if item["status"] == "completed")},
    "failures": len(attempts),
    "classes": len(classes),
    "class_valid_students": class_detail.get("valid_student_count"),
    "question_detail": question_detail.status_code,
    "question_avg": question_detail.json().get("overall", {}).get("student_average_score") if question_detail.status_code == 200 else None,
    "profile_status": profile.get("student", {}).get("code"),
    "profile_evidence": profile.get("data_quality", {}).get("evidence_count"),
    "compare_status": compare.status_code,
    "classify_status": classify.status_code,
    "classify_count": classify.json().get("classified") if classify.status_code == 200 else None,
    "labels": labels.get("items"),
    "recommend_status": recommend.status_code,
    "recommendation_types": sorted({item["type"] for item in recommend.json().get("items", [])}) if recommend.status_code == 200 else [],
    "graph_candidates": graph.json().get("created") if graph.status_code == 200 else None,
    "graph_edges": graph_view.get("summary", {}).get("edges"),
    "delete_status": deleted.status_code,
    "residual": residual,
}, ensure_ascii=False))
