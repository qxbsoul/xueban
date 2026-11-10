"""XUEBAN 独立项目真实模型执行：HTTP 凭据透传、fail closed 与 Python 状态边界。"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from xueban.services import xueban_company, xueban_seed, xueban_store


@pytest.fixture()
def xueban_db(tmp_path, monkeypatch):
    db_file = tmp_path / "xueban.sqlite"
    monkeypatch.setenv("XUEBAN_XUEBAN_DB", str(db_file))
    monkeypatch.setattr(xueban_store, "_DB_PATH", db_file)
    monkeypatch.setattr(xueban_store, "_CONN", None)
    xueban_store.init()
    xueban_seed.ensure_seed()
    yield xueban_store
    monkeypatch.setattr(xueban_store, "_CONN", None)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeNode:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def __call__(self, *args, **kwargs):
        assert kwargs == {"timeout": 120.0, "trust_env": False}
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def post(self, url, json, headers):
        self.calls.append({"url": url, "json": json, "headers": headers})
        response = self.responder(url, json, headers)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture()
def fake_node(monkeypatch):
    def install(responder):
        node = FakeNode(responder)
        monkeypatch.setattr(xueban_company.httpx, "Client", node)
        return node
    return install


def live_response(**overrides):
    payload = {
        "text": "真实项目交付正文",
        "thinking": "真实推理摘要",
        "mode": "live",
        "model": "gpt-4o",
        "usage": {"input_tokens": 123, "output_tokens": 45},
    }
    payload.update(overrides)
    return FakeResponse(payload)


def make_task(risk="medium"):
    company = xueban_company.list_companies()[0]
    employee = xueban_company.roster(company["id"])[0]
    project = xueban_company.create_project(company["id"], "真实执行项目", "验证真实模型交付")
    task = xueban_company.create_task(
        project["id"], "真实文案任务", assignee=employee["id"], risk=risk,
    )
    return company, employee, project, task


def test_project_run_forwards_authorization_and_model_headers(fake_node, xueban_db):
    node = fake_node(lambda url, body, headers: live_response(model="mimo-v2.6-pro"))
    from xueban.server import app

    with TestClient(app) as client:
        registered = client.post("/api/xueban/auth/register", json={
            "email": "live-xueban@example.test", "password": "password123",
            "display_name": "老板", "company_name": "增长工场 Growth Studio",
        })
        assert registered.status_code == 200
        token = registered.json()["token"]
        company_id = client.get(
            "/api/xueban/company", headers={"Authorization": f"Bearer {token}"},
        ).json()["company"]["id"]
        employee = xueban_company.hire(
            company_id, "MiMo 文案", "writer", model_name="mimo-v2.6-pro",
        )
        project_id = client.post(
            f"/api/xueban/company/{company_id}/projects",
            headers={"Authorization": f"Bearer {token}"},
            json={"goal": "验证独立项目真实执行", "auto_plan": False},
        ).json()["project"]["id"]
        task = xueban_company.create_task(
            project_id, "真实文案任务", assignee=employee["id"], risk="medium",
        )

        response = client.post(
            f"/api/xueban/projects/{project_id}/run",
            headers={"Authorization": f"Bearer {token}", "X-MiMo-Key": "sk-mimo-task"},
            json={"max_rounds": 1},
        )

    assert response.status_code == 200
    result = response.json()
    persisted = result["tasks"][0]
    assert persisted["id"] == task["id"]
    assert persisted["status"] == "review"
    assert persisted["output"]["text"] == "真实项目交付正文"
    assert persisted["output"]["thinking"] == "真实推理摘要"
    assert persisted["output"]["mode"] == "live"
    assert persisted["output"]["model"] == "mimo-v2.6-pro"
    assert persisted["output"]["usage"] == {"input_tokens": 123, "output_tokens": 45}
    assert persisted["output"]["tokens"] == {"in": 123, "out": 45}
    assert result["pending_approvals"][0]["task_id"] == task["id"]
    expenses = [row for row in xueban_company.ledger_entries(company_id) if row["ref_id"] == task["id"]]
    assert len(expenses) == 1 and expenses[0]["kind"] == "expense"

    assert len(node.calls) == 1
    call = node.calls[0]
    assert call["url"].endswith("/api/xueban-internal/tasks/complete")
    assert call["json"]["company_id"] == company_id
    assert call["json"]["provider"] == "mimo"
    assert call["json"]["model"] == "mimo-v2.6-pro"
    assert call["headers"]["Authorization"] == f"Bearer {token}"
    assert call["headers"]["x-mimo-key"] == "sk-mimo-task"
    assert "authorization" not in call["json"]


@pytest.mark.parametrize("failure", [
    FakeResponse({"error": {"message": "缺少 API Key"}}, status_code=401),
    httpx.ConnectError("Node offline"),
    live_response(text="   ", thinking="只有思考"),
])
def test_model_failures_close_task_without_delivery_approval_or_ledger(
    failure, fake_node, xueban_db,
):
    fake_node(lambda url, body, headers: failure)
    company, employee, project, task = make_task()
    before_ledger = len(xueban_company.ledger_entries(company["id"]))
    context = xueban_company.ExecutionContext("Bearer company-token", {"x-openai-key": "sk-task"})

    result = xueban_company.run_task(task["id"], execution_context=context)

    assert result["task"]["status"] == "failed"
    assert result["approval"] is None
    assert result["output"]["mode"] == "error"
    assert result["output"]["text"] == ""
    assert result["output"]["cost_cents"] == 0
    assert "simulated" not in str(result).lower()
    assert xueban_company.get_project(project["id"])["status"] == "failed"
    assert xueban_company.list_approvals(company["id"], status="pending") == []
    assert len(xueban_company.ledger_entries(company["id"])) == before_ledger
    assert xueban_company.get_employee(employee["id"])["tasks_done"] == 0


def test_missing_request_context_fails_without_calling_node(fake_node, xueban_db):
    node = fake_node(lambda url, body, headers: live_response())
    company, employee, project, task = make_task(risk="low")

    result = xueban_company.run_task(task["id"])

    assert result["task"]["status"] == "failed"
    assert result["output"]["text"] == ""
    assert result["error"] == "缺少教研组登录凭据，任务未执行"
    assert node.calls == []
    assert xueban_company.get_project(project["id"])["status"] == "failed"
