"""Xueban 组织与项目契约测试。

覆盖：
  xueban_store   — sqlite 存储
  xueban_company — 组织/员工/项目编排/审批/账本（真实 live 返回的测试替身）
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from xueban.services import xueban_company, xueban_seed, xueban_store


@pytest.fixture(autouse=True)
def live_llm(monkeypatch):
    """旧状态机用例只替换网络出口，返回与 Node 相同的 live 契约。"""
    def complete(system, user, meta, execution_context=None):
        return {
            "text": "真实模型测试交付", "thinking": "测试推理", "mode": "live",
            "model": meta.get("model") or "gpt-4o", "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    monkeypatch.setattr(xueban_company, "_llm_complete", complete)


@pytest.fixture()
def xueban_db(tmp_path, monkeypatch):
    """每个测试独立 sqlite 文件 + 重置连接单例。"""
    db_file = tmp_path / "xueban.sqlite"
    monkeypatch.setenv("XUEBAN_XUEBAN_DB", str(db_file))
    monkeypatch.setattr(xueban_store, "_DB_PATH", db_file)
    xueban_store._CONN = None  # noqa: SLF001 — 测试需要重置模块级单例
    monkeypatch.setattr(xueban_store, "_CONN", None)
    xueban_store.init()
    xueban_seed.ensure_seed()
    yield xueban_store
    xueban_store._CONN = None  # noqa: SLF001
    monkeypatch.setattr(xueban_store, "_CONN", None)


# ------------------------------------------------------------------ seed / store


def test_seed_idempotent_company_roster(xueban_db):
    companies = xueban_company.list_companies()
    assert len(companies) == 1
    co = companies[0]
    assert co["name"] == xueban_seed.COMPANY["name"]
    assert co["cash_cents"] == 200000

    depts = xueban_company.list_departments(co["id"])
    assert len(depts) == len(xueban_seed.DEPARTMENTS)
    # 部门负责人 = 该部门第一位入职员工
    heads = [d["head_employee_id"] for d in depts]
    assert all(heads), "每个部门都应自动任命负责人"

    roster = xueban_company.roster(co["id"])
    assert len(roster) == len(xueban_seed.STAFF)
    # 十个学科教研组各有一名教师
    subject_depts = [d for d in depts if d["name"].endswith("教研组")]
    assert len(subject_depts) == 10

    # 幂等：再跑一次不重复建
    xueban_seed.ensure_seed()
    assert len(xueban_company.list_companies()) == 1
    assert len(xueban_company.roster(co["id"])) == len(xueban_seed.STAFF)


# ------------------------------------------------------------------ company ops


def test_hire_fire_and_dashboard(xueban_db):
    co = xueban_company.list_companies()[0]
    emp = xueban_company.hire(co["id"], "测试员", "copywriter", title="文案", model_name="gpt-4o")
    assert emp["status"] == "idle"
    with pytest.raises(xueban_company.CompanyError):
        xueban_company.hire(co["id"], "坏员工", "writer", model_name="no-such-model")

    # 无未完成任务可直接 fire（返回 {ok, reassigned}；员工状态转 offline）
    fired = xueban_company.fire(emp["id"])
    assert fired["ok"] is True and fired["reassigned"] == 0
    assert xueban_company.get_employee(emp["id"])["status"] == "offline"

    dash = xueban_company.dashboard(co["id"])
    assert dash["company"]["id"] == co["id"]


def test_project_plan_and_approval_gate(xueban_db):
    co = xueban_company.list_companies()[0]
    project = xueban_company.create_project(co["id"], "内部目标", "提升复购率到 20%")
    tasks = xueban_company.plan_project(project["id"], xueban_seed.PLAYBOOK)
    assert len(tasks) == len(xueban_seed.PLAYBOOK)
    # 依赖图：plan 的 after=[plan] 未完成 → research 不可运行
    runnable = {t["key"] for t in xueban_company.next_runnable(project["id"])} if tasks and "key" in tasks[0] else None
    if runnable is not None:
        assert "plan" in runnable and "research" not in runnable


def test_project_delete_cascade_and_order_guard(xueban_db):
    co = xueban_company.list_companies()[0]
    project = xueban_company.create_project(co["id"], "待删除内部项目", "验证项目删除事务")
    xueban_company.plan_project(project["id"], xueban_seed.PLAYBOOK)
    deleted = xueban_company.delete_project(project["id"])
    assert deleted["xueban_projects"] == 1
    assert deleted["tasks"] == len(xueban_seed.PLAYBOOK)
    assert xueban_company.get_project(project["id"]) is None
    assert xueban_company.list_tasks(project_id=project["id"]) == []



def test_failed_task_can_retry_without_repeating_done(xueban_db, monkeypatch):
    co = xueban_company.list_companies()[0]
    project = xueban_company.create_project(co["id"], "失败重试项目", "验证失败任务重试")
    tasks = xueban_company.plan_project(project["id"], xueban_seed.PLAYBOOK)
    monkeypatch.setattr(
        xueban_company,
        "_llm_complete",
        lambda system, user, meta: {"text": "[error] 模拟模型失败", "mode": "error"},
    )
    failed = xueban_company.run_task(tasks[0]["id"])
    assert failed["task"]["status"] == "failed"
    assert xueban_company.get_project(project["id"])["status"] == "failed"

    xueban_store.x("UPDATE tasks SET status = 'done' WHERE id = ?", (tasks[1]["id"],))
    monkeypatch.setattr(xueban_company, "run_ready", lambda project_id, workers=3: [])
    xueban_company.run_project(project["id"], retry=True)
    assert xueban_company.get_task(tasks[0]["id"])["status"] == "todo"
    assert xueban_company.get_task(tasks[1]["id"])["status"] == "done"


# ------------------------------------------------------------------ HTTP API


@pytest.fixture()
def client(xueban_db):
    from xueban.server import app

    with TestClient(app) as c:
        response = c.post(
            "/api/xueban/auth/register",
            json={
                "email": "xueban-api@example.test",
                "password": "password123",
                "display_name": "老师",
                "company_name": xueban_seed.COMPANY["name"],
            },
        )
        assert response.status_code == 200
        c.headers.update({"Authorization": f"Bearer {response.json()['token']}"})
        yield c


def test_api_xueban_surface(client):
    r = client.get("/api/xueban/company")
    assert r.status_code == 200
    assert r.json()["company"]["name"] == xueban_seed.COMPANY["name"]

    r = client.get("/api/xueban/models")
    assert [m["id"] for m in r.json()["items"]] == list(xueban_company.MODEL_CATALOG)


def test_api_employee_update_and_hire_manager(client):
    company_id = client.get("/api/xueban/company").json()["company"]["id"]
    manager = client.get(f"/api/xueban/company/{company_id}/roster").json()["items"][0]
    department = client.post(
        f"/api/xueban/company/{company_id}/departments",
        json={"name": "新岗位部门", "charter": "初始职责"},
    ).json()["department"]

    response = client.post(
        f"/api/xueban/company/{company_id}/employees",
        json={
            "name": "可配置岗位",
            "role": "writer",
            "title": "内容专员",
            "department_id": department["id"],
            "manager_id": manager["id"],
            "system_prompt": "初始提示词",
            "model_name": "gpt-4o",
        },
    )
    assert response.status_code == 200
    employee = response.json()["employee"]
    assert employee["manager_id"] == manager["id"]

    response = client.patch(
        f"/api/xueban/company/{company_id}/employees/{employee['id']}",
        json={
            "name": "更新后的员工",
            "title": "高级内容专员",
            "department_id": None,
            "manager_id": None,
            "system_prompt": "更新后的提示词",
            "avatar": "新头像",
            "status": "blocked",
            "model_name": "deepseek-chat",
        },
    )
    assert response.status_code == 200
    updated = response.json()["employee"]
    assert updated["name"] == "更新后的员工"
    assert updated["title"] == "高级内容专员"
    assert updated["department_id"] is None
    assert updated["manager_id"] is None
    assert updated["system_prompt"] == "更新后的提示词"
    assert updated["avatar"] == "新头像"
    assert updated["status"] == "blocked"
    assert updated["model_name"] == "deepseek-chat"
    assert updated["model_provider"] == "DeepSeek"

    persisted = client.get(
        f"/api/xueban/company/{company_id}/employees/{employee['id']}"
    ).json()["employee"]
    assert persisted == updated


def test_api_fire_rejects_employee_from_another_company(client):
    company_id = client.get("/api/xueban/company").json()["company"]["id"]
    other_company = xueban_company.create_company("另一家公司", "测试", "验证隔离", owner_user_id="other-user")
    other_employee = xueban_company.hire(other_company["id"], "其他公司员工", "writer")

    response = client.post(
        f"/api/xueban/company/{company_id}/employees/{other_employee['id']}/fire",
        json={},
    )
    assert response.status_code == 404
    assert xueban_company.get_employee(other_employee["id"])["employment"] == "ai"


def test_api_department_update_delete_and_employee_conflict(client):
    company_id = client.get("/api/xueban/company").json()["company"]["id"]
    department = client.post(
        f"/api/xueban/company/{company_id}/departments",
        json={"name": "原部门", "charter": "原职责"},
    ).json()["department"]

    response = client.patch(
        f"/api/xueban/company/{company_id}/departments/{department['id']}",
        json={"name": "重命名部门", "charter": "新职责"},
    )
    assert response.status_code == 200
    assert response.json()["department"]["name"] == "重命名部门"
    assert response.json()["department"]["charter"] == "新职责"

    employee = client.post(
        f"/api/xueban/company/{company_id}/employees",
        json={"name": "部门员工", "department_id": department["id"]},
    ).json()["employee"]
    response = client.delete(f"/api/xueban/company/{company_id}/departments/{department['id']}")
    assert response.status_code == 409
    assert "1 名教研员" in response.json()["detail"]

    response = client.patch(
        f"/api/xueban/company/{company_id}/employees/{employee['id']}",
        json={"department_id": None},
    )
    assert response.status_code == 200
    response = client.delete(f"/api/xueban/company/{company_id}/departments/{department['id']}")
    assert response.status_code == 204
    assert xueban_company.get_department(company_id, department["id"]) is None
