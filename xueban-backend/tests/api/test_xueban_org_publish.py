from __future__ import annotations

import pytest

from xueban.services import xueban_company, xueban_store


@pytest.fixture()
def xueban_db(tmp_path, monkeypatch):
    db_file = tmp_path / "xueban.sqlite"
    monkeypatch.setattr(xueban_store, "_DB_PATH", db_file)
    monkeypatch.setattr(xueban_store, "_CONN", None)
    xueban_store.init()
    company = xueban_company.create_company("建制测试公司", "学情诊断", "真实组织发布")
    yield company
    monkeypatch.setattr(xueban_store, "_CONN", None)


def make_draft(second_department=False):
    departments = [{"id": "d1", "name": "策略部", "charter": "制定策略"}]
    employees = [
        {
            "id": "e1", "name": "林策略", "title": "策略负责人", "role": "strategy_lead",
            "system_prompt": "负责策略交付", "skills": ["市场研究"], "tools": ["web_search"],
            "department_id": "d1", "manager_id": None, "model_name": "mimo-v2.6-pro",
        },
        {
            "id": "e2", "name": "陈文案", "title": "文案", "role": "writer",
            "system_prompt": "负责文案交付", "skills": ["写作"], "tools": ["knowledge_search"],
            "department_id": "d1", "manager_id": "e1", "model_name": "mimo-v2.6-pro",
        },
    ]
    if second_department:
        departments.append({"id": "d2", "name": "内容部", "charter": "制作内容"})
    return {"departments": departments, "employees": employees}


def test_publish_creates_real_departments_employees_and_manager(xueban_db):
    before = len(xueban_company.roster(xueban_db["id"]))
    result = xueban_company.publish_org_draft(xueban_db["id"], make_draft())
    assert result["created"] == {"departments": 1, "employees": 2}
    roster = xueban_company.roster(xueban_db["id"])
    assert len(roster) == before + 2
    writer = next(item for item in roster if item["name"] == "陈文案")
    lead = next(item for item in roster if item["name"] == "林策略")
    assert writer["manager_id"] == lead["id"]
    assert writer["department_id"] == lead["department_id"]
    assert writer["skills"] == ["写作"]
    assert writer["tools"] == ["knowledge_search"]


def test_publish_rolls_back_all_rows_on_mid_transaction_failure(xueban_db, monkeypatch):
    xueban_company.create_department(xueban_db["id"], "已有部门")
    # 新草案第二个部门映射到已有主键，第一部门已插入后必然触发 IntegrityError。
    existing = xueban_company.list_departments(xueban_db["id"])[0]["id"]
    ids = iter(["dept_new", existing, "emp_1", "emp_2"])
    monkeypatch.setattr(xueban_store, "new_id", lambda _prefix: next(ids))
    before_departments = len(xueban_company.list_departments(xueban_db["id"]))
    before_employees = len(xueban_company.roster(xueban_db["id"]))
    with pytest.raises(Exception):
        xueban_company.publish_org_draft(xueban_db["id"], make_draft(second_department=True))
    assert len(xueban_company.list_departments(xueban_db["id"])) == before_departments
    assert len(xueban_company.roster(xueban_db["id"])) == before_employees
    assert all(dept["id"] != "dept_new" for dept in xueban_company.list_departments(xueban_db["id"]))


def test_employee_patch_persists_skills_tools_for_execution_prompt(xueban_db):
    employee = xueban_company.hire(xueban_db["id"], "执行员工", "writer", model_name="mimo-v2.6-pro")
    updated = xueban_company.update_employee(
        xueban_db["id"], employee["id"], skills=["写作", "写作", "校对"], tools=["web_search"],
    )
    assert updated["skills"] == ["写作", "校对"]
    assert updated["tools"] == ["web_search"]
    project = xueban_company.create_project(xueban_db["id"], "提示词测试", "验证员工权限进入真实提示词")
    task = xueban_company.create_task(project["id"], "写文案", assignee=employee["id"])
    prompt = xueban_company._build_prompt(xueban_db, project, updated, task, [], "")
    assert "写作、校对" in prompt
    assert "web_search" in prompt
    with pytest.raises(xueban_company.CompanyError):
        xueban_company.update_employee(xueban_db["id"], employee["id"], tools="web_search")
