"""D17 回归（HTTP 层）：同一 event_id 在不同任务中的历史 revisions 不得互相串数据。"""
import json
import warnings

warnings.filterwarnings("ignore")

from fastapi.testclient import TestClient

from tests.support_import import answer, make_files, question
from xueban.server import app
from xueban.services import xueban_store as db

client = TestClient(app)
register = client.post("/api/xueban/auth/register", json={"email": "rev@test.local", "password": "Passw0rd!", "display_name": "教师", "company_name": "测试组", "role": "teacher"})
assert register.status_code == 200, register.text
headers = {"Authorization": f"Bearer {register.json()['token']}"}


def upload(files):
    payload = [("files", (item.filename, item.file.read(), "text/csv")) for item in files]
    response = client.post("/api/teacher/imports", files=payload, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


a = upload(make_files(
    [answer(event="E-SHARED", revision=1, final="A-r1"), answer(event="E-SHARED", revision=2, final="A-r2")],
    questions=[question(stem="题干-A", max_score="5", scoring_points="Q001P1 5分")],
))
b = upload(make_files(
    [answer(event="E-SHARED", revision=1, final="B-r1")],
    questions=[question(stem="题干-B", max_score="10", scoring_points="Q001P1 10分")],
))
for batch in (a["batch_id"], b["batch_id"]):
    assert client.post(f"/api/teacher/batches/{batch}/preprocessing/confirm", headers=headers).status_code == 200

rows = {row["import_batch_id"]: row for row in db.q("SELECT * FROM teacher_answers")}
a_id = rows[a["batch_id"]]["id"]
b_id = rows[b["batch_id"]]["id"]

for batch, answer_id, score in ((a["batch_id"], a_id, 3), (b["batch_id"], b_id, 7)):
    row = rows[batch]
    db.x(
        "INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,score,max_score,status,needs_review,step_results_json,created_at,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"grade-{batch}", answer_id, row["student_id"], row["question_id"], "E-SHARED", 1, 1, "deepseek-chat", score, 5, "graded", 0, "[]", "2026-01-01T00:00:00", row["question_version_id"]),
    )

detail_a = client.get(f"/api/teacher/answers/{a_id}", headers=headers)
detail_b = client.get(f"/api/teacher/answers/{b_id}", headers=headers)
result_a = client.get(f"/api/teacher/results/grade-{a['batch_id']}", headers=headers)
result_b = client.get(f"/api/teacher/results/grade-{b['batch_id']}", headers=headers)

print(json.dumps({
    "status": {"a": detail_a.status_code, "b": detail_b.status_code, "ra": result_a.status_code, "rb": result_b.status_code},
    "a_revisions": [row["id"] for row in detail_a.json()["revisions"]],
    "b_revisions": [row["id"] for row in detail_b.json()["revisions"]],
    "a_result_revisions": [row["id"] for row in result_a.json()["revisions"]],
    "b_result_revisions": [row["id"] for row in result_b.json()["revisions"]],
    "a_source": detail_a.json()["question_source"],
    "b_source": detail_b.json()["question_source"],
}, ensure_ascii=False))
