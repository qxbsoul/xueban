"""Employee config immutability, task execution audit, editing and company isolation."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from xueban.server import app
from xueban.services import xueban_company, xueban_store


@pytest.fixture()
def xueban_db(tmp_path, monkeypatch):
    db_file = tmp_path / "xueban.sqlite"
    monkeypatch.setattr(xueban_store, "_DB_PATH", db_file)
    monkeypatch.setattr(xueban_store, "_CONN", None)
    xueban_store.init()
    yield xueban_store
    monkeypatch.setattr(xueban_store, "_CONN", None)


def register(client: TestClient, suffix: str):
    response = client.post("/api/xueban/auth/register", json={
        "email": f"audit-{suffix}@example.test", "password": "password123",
        "display_name": "老板", "company_name": f"审计公司 {suffix}",
    })
    assert response.status_code == 200
    body = response.json()
    return {"Authorization": f"Bearer {body['token']}"}, body["company"]


def test_config_versions_are_immutable_restorable_and_company_scoped(xueban_db):
    with TestClient(app) as client:
        headers, company = register(client, "owner")
        employee = xueban_company.hire(
            company["id"], "初始员工", "writer", system_prompt="初版职责",
            skills=["写作"], tools=["web_search"], model_name="gpt-4o",
        )
        xueban_company.update_employee(company["id"], employee["id"], system_prompt="第二版职责")
        xueban_company.update_employee(company["id"], employee["id"], skills=["校对"])

        response = client.get(
            f"/api/xueban/company/{company['id']}/employees/{employee['id']}/config-versions",
            headers=headers,
        )
        assert response.status_code == 200
        versions = response.json()["items"]
        assert [item["version"] for item in versions] == [3, 2, 1]
        assert versions[-1]["config"]["system_prompt"] == "初版职责"
        first_json = versions[-1]["config"].copy()

        restored = client.post(
            f"/api/xueban/company/{company['id']}/employees/{employee['id']}/config-versions/1/restore",
            headers=headers, json={},
        )
        assert restored.status_code == 200
        assert restored.json()["version"]["version"] == 4
        assert restored.json()["version"]["source"] == "restore"
        assert restored.json()["version"]["restored_from_version"] == 1
        assert restored.json()["version"]["config"] == first_json
        assert xueban_company.get_employee(employee["id"])["skills"] == ["写作"]

        other_headers, _ = register(client, "other")
        forbidden = client.get(
            f"/api/xueban/company/{company['id']}/employees/{employee['id']}/config-versions",
            headers=other_headers,
        )
        assert forbidden.status_code == 404


def test_org_publish_writes_initial_version_in_same_transaction(xueban_db):
    company = xueban_company.create_company("发布版本公司", "学情诊断", "验证版本")
    result = xueban_company.publish_org_draft(company["id"], {
        "departments": [{"id": "d", "name": "内容部", "charter": "内容"}],
        "employees": [{
            "id": "e", "name": "文案", "title": "文案", "role": "writer",
            "system_prompt": "负责内容", "skills": ["写作"], "tools": [],
            "department_id": "d", "manager_id": None, "model_name": "gpt-4o",
        }],
    })
    employee_id = result["employee_map"]["e"]
    versions = xueban_company.list_config_versions(company["id"], employee_id)
    assert len(versions) == 1
    assert versions[0]["version"] == 1
    assert versions[0]["source"] == "org_publish"
    assert versions[0]["config"]["system_prompt"] == "负责内容"


def test_execution_retries_append_and_failed_reassignment_uses_new_config(xueban_db, monkeypatch):
    outcomes = iter([
        {"text": "", "thinking": "", "mode": "error", "model": "gpt-4o", "usage": {}, "error": "上游失败"},
        {"text": "真实交付内容", "thinking": "推理摘要", "mode": "live", "model": "gpt-4o", "usage": {}},
    ])
    monkeypatch.setattr(xueban_company, "_llm_complete", lambda *args, **kwargs: next(outcomes))

    with TestClient(app) as client:
        headers, company = register(client, "execution")
        first = xueban_company.hire(company["id"], "第一位", "writer", model_name="gpt-4o")
        second = xueban_company.hire(company["id"], "第二位", "writer", model_name="gpt-4o")
        project = xueban_company.create_project(company["id"], "审计项目", "验证执行记录")
        task = xueban_company.create_task(project["id"], "原任务", brief="原要求", assignee=first["id"], risk="low")

        failed = xueban_company.run_task(task["id"])
        assert failed["task"]["status"] == "failed"
        patched = client.patch(
            f"/api/xueban/tasks/{task['id']}", headers=headers,
            json={"title": "新任务", "brief": "新要求", "priority": "high", "assignee": second["id"]},
        )
        assert patched.status_code == 200
        assert patched.json()["task"]["status"] == "todo"
        assert patched.json()["task"]["assignee"]["id"] == second["id"]

        succeeded = xueban_company.run_task(task["id"])
        assert succeeded["task"]["status"] == "done"
        response = client.get(f"/api/xueban/tasks/{task['id']}/executions", headers=headers)
        assert response.status_code == 200
        executions = response.json()["items"]
        assert [item["attempt"] for item in executions] == [2, 1]
        assert executions[0]["status"] == "done"
        assert executions[0]["employee_id"] == second["id"]
        assert executions[0]["config_version"] == 1
        assert "真实交付内容" in executions[0]["output_summary"]
        assert executions[0]["duration_ms"] >= 0
        assert executions[1]["status"] == "failed"
        assert executions[1]["error"] == "上游失败"
        assert executions[1]["ended_at"]
        assert executions[1]["config_version_id"] != executions[0]["config_version_id"]

        other_headers, _ = register(client, "execution-other")
        assert client.get(f"/api/xueban/tasks/{task['id']}/executions", headers=other_headers).status_code == 404


def test_running_task_rejects_patch(xueban_db):
    with TestClient(app) as client:
        headers, company = register(client, "running")
        employee = xueban_company.hire(company["id"], "执行人", "writer", model_name="gpt-4o")
        project = xueban_company.create_project(company["id"], "运行项目", "验证运行状态")
        task = xueban_company.create_task(project["id"], "运行任务", assignee=employee["id"])
        xueban_store.x("UPDATE tasks SET status = 'running' WHERE id = ?", (task["id"],))
        response = client.patch(
            f"/api/xueban/tasks/{task['id']}", headers=headers, json={"title": "不允许"},
        )
        assert response.status_code == 409
        assert xueban_company.get_task(task["id"])["title"] == "运行任务"
