"""D2 回归（HTTP）：质量闸门必须由服务端强制执行，绕过必须带理由并留痕。"""
import json
import os
import warnings

os.environ.setdefault("DEEPSEEK_API_KEY", "test-key-not-real")
warnings.filterwarnings("ignore")

from fastapi.testclient import TestClient

from tests.support_import import answer, make_files, question
from xueban.server import app
from xueban.services import xueban_store as db

client = TestClient(app)
register = client.post("/api/xueban/auth/register", json={"email": "gate@test.local", "password": "Passw0rd!", "display_name": "教师", "company_name": "测试组", "role": "teacher"})
assert register.status_code == 200, register.text
headers = {"Authorization": f"Bearer {register.json()['token']}"}


def upload(files):
    payload = [("files", (item.filename, item.file.read(), "text/csv")) for item in files]
    response = client.post("/api/teacher/imports", files=payload, headers=headers)
    assert response.status_code == 200, response.text
    batch_id = response.json()["batch_id"]
    assert client.post(f"/api/teacher/batches/{batch_id}/preprocessing/confirm", headers=headers).status_code == 200
    return batch_id


# 阻断任务：题目缺评分点
bad_batch = upload(make_files([answer(event="E-BAD", student_code="S01")], questions=[question(stem="坏题", scoring_points="")]))
gate = client.get(f"/api/teacher/batches/{bad_batch}/quality-gate", headers=headers).json()
plain = client.post("/api/teacher/grade/batch", json={"batch_id": bad_batch}, headers=headers)
no_reason = client.post("/api/teacher/grade/batch", json={"batch_id": bad_batch, "force": True}, headers=headers)
forced = client.post("/api/teacher/grade/batch", json={"batch_id": bad_batch, "force": True, "force_reason": "赛题数据缺失评分点，教师确认按整题判分"}, headers=headers)
overrides = client.get(f"/api/teacher/batches/{bad_batch}/quality-gate-overrides", headers=headers).json()["items"]

# 正常任务：不阻断时可以创建
good_batch = upload(make_files([answer(event="E-GOOD", student_code="S01")], questions=[question(stem="好题", scoring_points="Q001P1 5分")]))
good = client.post("/api/teacher/grade/batch", json={"batch_id": good_batch}, headers=headers)

print(json.dumps({
    "gate_blocking": gate["blocking"], "gate_types": [item["type"] for item in gate["issues"]],
    "plain_status": plain.status_code, "plain_code": (plain.json().get("detail") or {}).get("code") if plain.status_code == 409 else None,
    "no_reason_status": no_reason.status_code,
    "forced_status": forced.status_code,
    "override_count": len(overrides),
    "override_reason": overrides[0]["reason"] if overrides else None,
    "override_issues": json.loads(overrides[0]["issues_json"]) if overrides else [],
    "good_status": good.status_code,
}, ensure_ascii=False))
