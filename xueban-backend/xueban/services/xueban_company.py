"""XUEBAN OS company layer — ported from backend-poc/app/company.py (2026-11-04).

与 backend-poc 版的差异：
  1. db → xueban_store（本包 sqlite 存储层，独立 xueban.sqlite）；
  2. LLM 统一经 Node 教研组内部补全接口真实执行；无凭据、上游失败或空输出均 fail closed；
  3. Playbook / 教研员种子数据不在这里 —— 挂载层（api/xueban.py）按需 seed。

三条硬规则（原样保留）：
  1. 教研员是可配置、可调用、可追踪的 agent 实例（ID/岗位/权限/模型/成本/绩效）。
  2. 每项任务可追到责任人、执行过程、交接记录、成本和交付结果。
  3. 风险分级决定谁来批：low 自动放行，medium 部门负责人，high 老师本人。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional

import httpx

from xueban.services import xueban_audit as audit
from xueban.services import xueban_store as db

# 模型目录：不同岗位用不同模型，成本与能力都不同。
MODEL_CATALOG = {
    "claude-sonnet": {"label": "Claude Sonnet", "vendor": "Anthropic", "in_cents": 3.0, "out_cents": 15.0, "tier": "旗舰"},
    "gpt-4o": {"label": "GPT-4o", "vendor": "OpenAI", "in_cents": 2.5, "out_cents": 10.0, "tier": "旗舰"},
    "gemini-flash": {"label": "Gemini Flash", "vendor": "Google", "in_cents": 0.08, "out_cents": 0.3, "tier": "轻量"},
    "deepseek-chat": {"label": "DeepSeek V3", "vendor": "DeepSeek", "in_cents": 0.27, "out_cents": 1.1, "tier": "性价比"},
    "qwen-max": {"label": "Qwen Max", "vendor": "Alibaba", "in_cents": 0.6, "out_cents": 1.8, "tier": "性价比"},
    "mimo-v2.6-pro": {"label": "MiMo v2.6 Pro", "vendor": "MiMo", "in_cents": 0.0, "out_cents": 0.0, "tier": "旗舰"},
}

RISK_LEVELS = ["low", "medium", "high"]
TASK_STATUS = ["todo", "blocked", "running", "review", "done", "failed", "cancelled"]
EMP_STATUS = ["idle", "working", "awaiting_approval", "blocked", "offline"]

MODEL_HEADER_NAMES = frozenset({
    "x-llm-key", "x-llm-provider",
    *(f"x-{provider}-key" for provider in (
        "anthropic", "openai", "deepseek", "zhipu", "mimo", "google", "qwen",
        "moonshot", "mistral", "groq", "openrouter", "ollama", "lmstudio", "azure",
    )),
})
_PROVIDER_BY_VENDOR = {
    "Anthropic": "anthropic", "OpenAI": "openai", "Google": "google",
    "DeepSeek": "deepseek", "Alibaba": "qwen", "MiMo": "mimo",
}


@dataclass(frozen=True)
class ExecutionContext:
    """单次 HTTP 请求的只读凭据上下文；显式传入线程，避免并发串号。"""

    authorization: str
    model_headers: Mapping[str, str]

    def __post_init__(self) -> None:
        headers = {str(k).lower(): str(v) for k, v in self.model_headers.items() if str(v).strip()}
        object.__setattr__(self, "model_headers", MappingProxyType(headers))


class CompanyError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _j(value: Any, default: Any = None) -> Any:
    if not value:
        return default if default is not None else []
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default if default is not None else []


def _money(cents: Any) -> float:
    return round(int(cents) / 100, 2)


# ------------------------------------------------------------------ 教研组

def create_company(name: str, industry: str, mission: str, owner_name: str = "老师",
                   team_id: Optional[str] = None, cash_cents: int = 0,
                   owner_user_id: Optional[str] = None) -> Dict[str, Any]:
    cid = db.new_id("co")
    db.x(
        """INSERT INTO companies (id, name, logo, industry, mission, team_id, cash_cents, owner_name, owner_user_id, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (cid, name, name[:1], industry, mission, team_id, int(cash_cents), owner_name, owner_user_id, db.now()),
    )
    return get_company(cid)


def get_company(company_id: str) -> Optional[Dict[str, Any]]:
    return db.q1("SELECT * FROM companies WHERE id = ?", (company_id,))


def list_companies() -> List[Dict[str, Any]]:
    return db.q("SELECT * FROM companies ORDER BY created_at")


def company_or_404(company_id: str) -> Dict[str, Any]:
    c = get_company(company_id)
    if not c:
        raise CompanyError(f"教研组不存在：{company_id}", 404)
    return c


# ------------------------------------------------------------------ 组织与教研员

def create_department(company_id: str, name: str, charter: str = "", parent_id: Optional[str] = None,
                      budget_cents: int = 0, team_id: Optional[str] = None, sort: int = 0) -> Dict[str, Any]:
    did = db.new_id("dept")
    db.x(
        """INSERT INTO departments (id, company_id, name, parent_id, charter, budget_cents, team_id, sort, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (did, company_id, name, parent_id, charter, int(budget_cents), team_id, int(sort), db.now()),
    )
    return db.q1("SELECT * FROM departments WHERE id = ?", (did,))


def list_departments(company_id: str) -> List[Dict[str, Any]]:
    return db.q("SELECT * FROM departments WHERE company_id = ? ORDER BY sort, created_at", (company_id,))


def get_department(company_id: str, department_id: str) -> Optional[Dict[str, Any]]:
    return db.q1(
        "SELECT * FROM departments WHERE id = ? AND company_id = ?",
        (department_id, company_id),
    )


def update_department(company_id: str, department_id: str, **fields: Any) -> Dict[str, Any]:
    dept = get_department(company_id, department_id)
    if not dept:
        raise CompanyError(f"部门不存在：{department_id}", 404)
    updates: Dict[str, Any] = {}
    if "name" in fields:
        name = str(fields["name"] or "").strip()
        if not name:
            raise CompanyError("部门名称不能为空", 422)
        updates["name"] = name
    if "charter" in fields:
        updates["charter"] = str(fields["charter"] or "")
    if updates:
        assignments = ", ".join(f"{key} = ?" for key in updates)
        db.x(
            f"UPDATE departments SET {assignments} WHERE id = ? AND company_id = ?",
            tuple(updates.values()) + (department_id, company_id),
        )
    return get_department(company_id, department_id)


def delete_department(company_id: str, department_id: str) -> None:
    dept = get_department(company_id, department_id)
    if not dept:
        raise CompanyError(f"部门不存在：{department_id}", 404)
    employees = db.q1(
        "SELECT COUNT(*) AS count FROM employees WHERE company_id = ? AND department_id = ?",
        (company_id, department_id),
    )
    if employees["count"]:
        raise CompanyError(f"部门仍有 {employees['count']} 名教研员，请先调整教研员所属部门", 409)
    db.x("DELETE FROM departments WHERE id = ? AND company_id = ?", (department_id, company_id))


def set_department_head(department_id: str, employee_id: str) -> None:
    db.x(
        "UPDATE departments SET head_employee_id = ? WHERE id = ? AND head_employee_id IS NULL",
        (employee_id, department_id),
    )


def _validate_department(company_id: str, department_id: Optional[str]) -> Optional[str]:
    if not department_id:
        return None
    if not get_department(company_id, department_id):
        raise CompanyError("部门不存在或不属于当前教研组", 422)
    return department_id


def _validate_manager(company_id: str, manager_id: Optional[str], employee_id: Optional[str] = None) -> Optional[str]:
    if not manager_id:
        return None
    if manager_id == employee_id:
        raise CompanyError("教研员不能将自己设为上级", 422)
    manager = get_employee(manager_id)
    if not manager or manager["company_id"] != company_id:
        raise CompanyError("上级教研员不存在或不属于当前教研组", 422)
    return manager_id


def hire(company_id: str, name: str, role: str, department_id: Optional[str] = None, title: str = "",
         seniority: str = "mid", system_prompt: str = "", skills: Optional[List[str]] = None,
         tools: Optional[List[str]] = None, permissions: Optional[List[str]] = None,
         model_name: str = "gpt-4o", day_rate_cents: int = 1000, employment: str = "ai",
         manager_id: Optional[str] = None, avatar: Optional[str] = None) -> Dict[str, Any]:
    company_or_404(company_id)
    department_id = _validate_department(company_id, department_id)
    manager_id = _validate_manager(company_id, manager_id)
    if model_name not in MODEL_CATALOG:
        raise CompanyError(f"未知模型：{model_name}", 422)
    eid = db.new_id("emp")
    db.x(
        """INSERT INTO employees (id, company_id, department_id, manager_id, name, role, title, avatar, seniority,
                                  system_prompt, skills_json, tools_json, permissions_json,
                                  model_provider, model_name, status, employment,
                                  success_rate, tasks_done, token_cost_cents, day_rate_cents, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            eid, company_id, department_id, manager_id, name, role, title or role, avatar or name[:1], seniority,
            system_prompt, json.dumps(skills or [], ensure_ascii=False),
            json.dumps(tools or [], ensure_ascii=False),
            json.dumps(permissions or [], ensure_ascii=False),
            MODEL_CATALOG[model_name]["vendor"], model_name, "idle", employment,
            0.0, 0, 0, int(day_rate_cents), db.now(),
        ),
    )
    employee = get_employee(eid)
    audit.record_config_version(employee, "hire")
    return employee


def get_employee(employee_id: str) -> Optional[Dict[str, Any]]:
    e = db.q1("SELECT * FROM employees WHERE id = ?", (employee_id,))
    if not e:
        return None
    for k in ("skills_json", "tools_json", "permissions_json"):
        e[k.replace("_json", "")] = _j(e.get(k), [])
    e["model"] = MODEL_CATALOG.get(e.get("model_name") or "", {"label": e.get("model_name") or "-"})
    return e


def update_employee(company_id: str, employee_id: str, record_version: bool = True, **fields: Any) -> Dict[str, Any]:
    employee = get_employee(employee_id)
    if not employee or employee["company_id"] != company_id:
        raise CompanyError(f"教研员不存在：{employee_id}", 404)

    updates: Dict[str, Any] = {}
    if "name" in fields:
        name = str(fields["name"] or "").strip()
        if not name:
            raise CompanyError("教研员姓名不能为空", 422)
        updates["name"] = name
    for key in ("title", "system_prompt", "avatar"):
        if key in fields:
            updates[key] = str(fields[key] or "")
    if "department_id" in fields:
        updates["department_id"] = _validate_department(company_id, fields["department_id"])
    if "manager_id" in fields:
        updates["manager_id"] = _validate_manager(company_id, fields["manager_id"], employee_id)
    if "status" in fields:
        if fields["status"] not in EMP_STATUS:
            raise CompanyError(f"未知教研员状态：{fields['status']}", 422)
        updates["status"] = fields["status"]
    if "model_name" in fields:
        model_name = fields["model_name"]
        if model_name not in MODEL_CATALOG:
            raise CompanyError(f"未知模型：{model_name}", 422)
        updates["model_name"] = model_name
        updates["model_provider"] = MODEL_CATALOG[model_name]["vendor"]
    for key in ("skills", "tools"):
        if key in fields:
            value = fields[key]
            if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
                raise CompanyError(f"{key} 必须是非空字符串组成的数组", 422)
            updates[f"{key}_json"] = json.dumps(list(dict.fromkeys(item.strip() for item in value)), ensure_ascii=False)

    if updates:
        assignments = ", ".join(f"{key} = ?" for key in updates)
        db.x(
            f"UPDATE employees SET {assignments} WHERE id = ? AND company_id = ?",
            tuple(updates.values()) + (employee_id, company_id),
        )
    updated = get_employee(employee_id)
    if record_version:
        audit.record_config_version(updated, "patch")
    return updated


def list_config_versions(company_id: str, employee_id: str) -> List[Dict[str, Any]]:
    employee = get_employee(employee_id)
    if not employee or employee["company_id"] != company_id:
        raise CompanyError(f"教研员不存在：{employee_id}", 404)
    return audit.list_config_versions(company_id, employee_id)


def restore_config_version(company_id: str, employee_id: str, version: int) -> Dict[str, Any]:
    employee = get_employee(employee_id)
    if not employee or employee["company_id"] != company_id:
        raise CompanyError(f"教研员不存在：{employee_id}", 404)
    historical = audit.get_config_version(company_id, employee_id, version)
    if not historical:
        raise CompanyError(f"配置版本不存在：v{version}", 404)
    config = historical["config"]
    updated = update_employee(
        company_id, employee_id, record_version=False,
        **{key: config[key] for key in (
            "name", "title", "department_id", "manager_id", "system_prompt", "avatar",
            "status", "model_name", "skills", "tools",
        ) if key in config},
    )
    # API PATCH 只开放常用字段；恢复还需还原版本内的其余执行配置。
    db.x(
        """UPDATE employees SET role = ?, seniority = ?, permissions_json = ?,
           employment = ?, day_rate_cents = ? WHERE id = ? AND company_id = ?""",
        (
            config.get("role") or updated["role"], config.get("seniority") or "mid",
            json.dumps(config.get("permissions") or [], ensure_ascii=False),
            config.get("employment") or "ai", int(config.get("day_rate_cents") or 0),
            employee_id, company_id,
        ),
    )
    updated = get_employee(employee_id)
    restored = audit.record_config_version(updated, "restore", int(version))
    return {"employee": updated, "version": restored}


def roster(company_id: str) -> List[Dict[str, Any]]:
    rows = db.q(
        """SELECT e.*, d.name AS department_name FROM employees e
           LEFT JOIN departments d ON d.id = e.department_id
           WHERE e.company_id = ? ORDER BY d.sort, e.created_at""",
        (company_id,),
    )
    out = []
    for e in rows:
        e["skills"] = _j(e.get("skills_json"))
        e["tools"] = _j(e.get("tools_json"))
        e["permissions"] = _j(e.get("permissions_json"))
        e["model"] = MODEL_CATALOG.get(e.get("model_name") or "", {"label": e.get("model_name") or "-"})
        out.append(e)
    return out


def fire(employee_id: str, reassign_to: Optional[str] = None,
         company_id: Optional[str] = None) -> Dict[str, Any]:
    e = get_employee(employee_id)
    if not e or (company_id is not None and e["company_id"] != company_id):
        raise CompanyError(f"教研员不存在：{employee_id}", 404)
    company_id = company_id or e["company_id"]
    if reassign_to:
        assignee = get_employee(reassign_to)
        if not assignee or assignee["company_id"] != company_id or reassign_to == employee_id:
            raise CompanyError("接手教研员不存在或不属于当前教研组", 422)
    open_tasks = db.q(
        "SELECT id FROM tasks WHERE assignee_employee_id = ? AND status IN ('todo','running','review')",
        (employee_id,),
    )
    if open_tasks and not reassign_to:
        raise CompanyError(f"该教研员还有 {len(open_tasks)} 项未完成任务，需先指定接手人", 409)
    if reassign_to:
        db.x(
            "UPDATE tasks SET assignee_employee_id = ? WHERE assignee_employee_id = ? AND status IN ('todo','running','review')",
            (reassign_to, employee_id),
        )
    db.x(
        "UPDATE employees SET status = 'offline', employment = 'retired' WHERE id = ? AND company_id = ?",
        (employee_id, company_id),
    )
    return {"ok": True, "reassigned": len(open_tasks)}


def org_chart(company_id: str) -> Dict[str, Any]:
    depts = list_departments(company_id)
    emps = roster(company_id)
    by_dept: Dict[str, List[Dict]] = {}
    for e in emps:
        by_dept.setdefault(e.get("department_id"), []).append(e)
    tree = []
    for d in depts:
        members = by_dept.get(d["id"], [])
        head = next((m for m in members if m["id"] == d.get("head_employee_id")), None)
        tree.append(
            {
                "id": d["id"],
                "name": d["name"],
                "charter": d.get("charter"),
                "budget": _money(d.get("budget_cents") or 0),
                "head": {"id": head["id"], "name": head["name"], "title": head["title"]} if head else None,
                "members": [
                    {
                        "id": m["id"], "name": m["name"], "title": m["title"], "avatar": m["avatar"],
                        "status": m["status"], "model": m["model"].get("label"), "skills": m["skills"][:3],
                    }
                    for m in members
                ],
            }
        )
    return {"company_id": company_id, "departments": tree, "headcount": len(emps)}


def publish_org_draft(company_id: str, value: Any) -> Dict[str, Any]:
    """校验人工确认的草案，并在一个事务中创建全部部门与教研员。"""
    company_or_404(company_id)
    if not isinstance(value, dict):
        raise CompanyError("组织草案格式无效", 422)
    departments = value.get("departments")
    employees = value.get("employees")
    if not isinstance(departments, list) or not 1 <= len(departments) <= 12:
        raise CompanyError("部门数量必须为 1–12", 422)
    if not isinstance(employees, list) or not 1 <= len(employees) <= 50:
        raise CompanyError("教研员数量必须为 1–50", 422)

    def text(item: Any, key: str, label: str) -> str:
        if not isinstance(item, dict) or not isinstance(item.get(key), str) or not item[key].strip():
            raise CompanyError(f"{label}.{key} 必填", 422)
        return item[key].strip()

    def strings(item: Dict[str, Any], key: str, label: str) -> List[str]:
        values = item.get(key)
        if not isinstance(values, list) or len(values) > 30:
            raise CompanyError(f"{label}.{key} 必须是最多 30 项的字符串数组", 422)
        if any(not isinstance(entry, str) or not entry.strip() for entry in values):
            raise CompanyError(f"{label}.{key} 含有无效条目", 422)
        return list(dict.fromkeys(entry.strip() for entry in values))

    dept_ids: set[str] = set()
    dept_names: set[str] = set()
    clean_departments: List[Dict[str, str]] = []
    for index, item in enumerate(departments):
        label = f"departments[{index}]"
        dept = {"id": text(item, "id", label), "name": text(item, "name", label), "charter": text(item, "charter", label)}
        if dept["id"] in dept_ids or dept["name"].casefold() in dept_names:
            raise CompanyError(f"部门 id 或名称重复：{dept['name']}", 422)
        dept_ids.add(dept["id"])
        dept_names.add(dept["name"].casefold())
        clean_departments.append(dept)

    employee_ids: set[str] = set()
    employee_names: set[str] = set()
    clean_employees: List[Dict[str, Any]] = []
    for index, item in enumerate(employees):
        label = f"employees[{index}]"
        if not isinstance(item, dict):
            raise CompanyError(f"{label} 格式无效", 422)
        model_name = text(item, "model_name", label)
        if model_name not in MODEL_CATALOG:
            raise CompanyError(f"未知模型：{model_name}", 422)
        manager_id = item.get("manager_id")
        if manager_id is not None and (not isinstance(manager_id, str) or not manager_id.strip()):
            raise CompanyError(f"{label}.manager_id 必须为草案教研员 id 或 null", 422)
        employee = {
            "id": text(item, "id", label), "name": text(item, "name", label),
            "title": text(item, "title", label), "role": text(item, "role", label),
            "system_prompt": text(item, "system_prompt", label),
            "department_id": text(item, "department_id", label),
            "manager_id": manager_id.strip() if isinstance(manager_id, str) and manager_id.strip() else None,
            "model_name": model_name, "skills": strings(item, "skills", label), "tools": strings(item, "tools", label),
        }
        if employee["id"] in employee_ids or employee["name"].casefold() in employee_names:
            raise CompanyError(f"教研员 id 或姓名重复：{employee['name']}", 422)
        if employee["department_id"] not in dept_ids:
            raise CompanyError(f"教研员 {employee['name']} 的部门不在草案中", 422)
        if employee["manager_id"] == employee["id"]:
            raise CompanyError(f"教研员 {employee['name']} 不能向自己汇报", 422)
        employee_ids.add(employee["id"])
        employee_names.add(employee["name"].casefold())
        clean_employees.append(employee)

    managers = {employee["id"]: employee["manager_id"] for employee in clean_employees}
    for employee in clean_employees:
        if employee["manager_id"] and employee["manager_id"] not in employee_ids:
            raise CompanyError(f"教研员 {employee['name']} 的上级不在草案中", 422)
    for start in employee_ids:
        seen: set[str] = set()
        current: Optional[str] = start
        while current:
            if current in seen:
                raise CompanyError("上级关系存在循环", 422)
            seen.add(current)
            current = managers.get(current)

    department_map = {dept["id"]: db.new_id("dept") for dept in clean_departments}
    employee_map = {employee["id"]: db.new_id("emp") for employee in clean_employees}
    with db._LOCK:
        connection = db.conn()
        try:
            connection.execute("BEGIN IMMEDIATE")
            ts = db.now()
            for sort, dept in enumerate(clean_departments):
                connection.execute(
                    """INSERT INTO departments (id, company_id, name, parent_id, charter, budget_cents, team_id, sort, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (department_map[dept["id"]], company_id, dept["name"], None, dept["charter"], 0, None, sort, ts),
                )
            for employee in clean_employees:
                connection.execute(
                    """INSERT INTO employees (id, company_id, department_id, manager_id, name, role, title, avatar, seniority,
                                              system_prompt, skills_json, tools_json, permissions_json,
                                              model_provider, model_name, status, employment,
                                              success_rate, tasks_done, token_cost_cents, day_rate_cents, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        employee_map[employee["id"]], company_id, department_map[employee["department_id"]],
                        employee_map.get(employee["manager_id"]), employee["name"], employee["role"], employee["title"],
                        employee["name"][:1], "mid", employee["system_prompt"],
                        json.dumps(employee["skills"], ensure_ascii=False), json.dumps(employee["tools"], ensure_ascii=False), "[]",
                        MODEL_CATALOG[employee["model_name"]]["vendor"], employee["model_name"], "idle", "ai",
                        0.0, 0, 0, 1000, ts,
                    ),
                )
                audit.insert_config_version(
                    connection,
                    {
                        **employee, "id": employee_map[employee["id"]], "company_id": company_id,
                        "department_id": department_map[employee["department_id"]],
                        "manager_id": employee_map.get(employee["manager_id"]),
                        "avatar": employee["name"][:1], "seniority": "mid", "permissions": [],
                        "model_provider": MODEL_CATALOG[employee["model_name"]]["vendor"],
                        "status": "idle", "employment": "ai", "day_rate_cents": 1000,
                    },
                    "org_publish", created_at=ts,
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return {
        "ok": True, "company_id": company_id, "department_map": department_map, "employee_map": employee_map,
        "created": {"departments": len(clean_departments), "employees": len(clean_employees)},
        "org": org_chart(company_id),
    }


# ------------------------------------------------------------------ 项目

def create_project(company_id: str, name: str, goal: str, client_name: str = "",
                   budget_cents: int = 0, due_at: Optional[str] = None,
                   order_id: Optional[str] = None,
                   owner_employee_id: Optional[str] = None) -> Dict[str, Any]:
    pid = db.new_id("prj")
    ts = db.now()
    db.x(
        """INSERT INTO xueban_projects (id, company_id, name, goal, client_name, owner_employee_id, status,
                                     budget_cents, revenue_cents, cost_cents, due_at, order_id, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (pid, company_id, name, goal, client_name, owner_employee_id, "planning",
         int(budget_cents), 0, 0, due_at, order_id, ts, ts),
    )
    return get_project(pid)


def get_project(project_id: str) -> Optional[Dict[str, Any]]:
    return db.q1("SELECT * FROM xueban_projects WHERE id = ?", (project_id,))


def list_projects(company_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    return db.q(
        "SELECT * FROM xueban_projects WHERE company_id = ? ORDER BY created_at DESC LIMIT ?",
        (company_id, int(limit)),
    )


def _recost_project(project_id: str) -> None:
    row = db.q1("SELECT COALESCE(SUM(cost_cents), 0) AS c FROM tasks WHERE project_id = ?", (project_id,))
    db.x("UPDATE xueban_projects SET cost_cents = ?, updated_at = ? WHERE id = ?",
         (row["c"], db.now(), project_id))


# ------------------------------------------------------------------ 任务协议

def create_task(project_id: str, title: str, brief: str = "", assignee: Optional[str] = None,
                assigner: Optional[str] = None, depends_on: Optional[List[str]] = None,
                mode: str = "pipeline", priority: str = "normal", risk: str = "medium",
                parent_id: Optional[str] = None, due_at: Optional[str] = None) -> Dict[str, Any]:
    project = get_project(project_id)
    if not project:
        raise CompanyError(f"项目不存在：{project_id}", 404)
    tid = db.new_id("tsk")
    db.x(
        """INSERT INTO tasks (id, project_id, company_id, parent_id, title, brief, assignee_employee_id,
                              assigner_employee_id, status, priority, mode, depends_on_json,
                              cost_cents, due_at, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            tid, project_id, project["company_id"], parent_id, title, brief, assignee, assigner,
            "todo", priority, mode, json.dumps(depends_on or []), 0, due_at, db.now(),
        ),
    )
    if risk in ("medium", "high"):
        db.x(
            "UPDATE tasks SET review_json = ? WHERE id = ?",
            (json.dumps({"risk": risk, "required": True}, ensure_ascii=False), tid),
        )
    return get_task(tid)


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    t = db.q1("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not t:
        return None
    t["depends_on"] = _j(t.get("depends_on_json"), [])
    t["output"] = _j(t.get("output_json"), None)
    t["handoff"] = _j(t.get("handoff_json"), None)
    t["review"] = _j(t.get("review_json"), None)
    t["input"] = _j(t.get("input_json"), {})
    emp = get_employee(t["assignee_employee_id"]) if t.get("assignee_employee_id") else None
    t["assignee"] = ({"id": emp["id"], "name": emp["name"], "title": emp["title"], "avatar": emp["avatar"]}
                     if emp else None)
    return t


def update_task(task_id: str, **fields: Any) -> Dict[str, Any]:
    task = get_task(task_id)
    if not task:
        raise CompanyError(f"任务不存在：{task_id}", 404)
    if task["status"] == "running":
        raise CompanyError("运行中的任务不能编辑或重新分配", 409)
    updates: Dict[str, Any] = {}
    if "title" in fields:
        title = str(fields["title"] or "").strip()
        if not title:
            raise CompanyError("任务标题不能为空", 422)
        updates["title"] = title
    if "brief" in fields:
        updates["brief"] = str(fields["brief"] or "")
    if "priority" in fields:
        priority = fields["priority"]
        if priority not in ("low", "normal", "high", "urgent"):
            raise CompanyError("优先级必须为 low / normal / high / urgent", 422)
        updates["priority"] = priority
    assignee_id = fields.get("assignee", fields.get("assignee_employee_id"))
    if "assignee" in fields or "assignee_employee_id" in fields:
        employee = get_employee(assignee_id) if assignee_id else None
        if not employee or employee["company_id"] != task["company_id"]:
            raise CompanyError("负责人不存在或不属于当前教研组", 422)
        if employee.get("employment") == "retired" or employee.get("status") == "offline":
            raise CompanyError("不能分配给已停用或离职教研员", 422)
        updates["assignee_employee_id"] = employee["id"]
        if task["status"] == "failed" and employee["id"] != task.get("assignee_employee_id"):
            updates["status"] = "todo"
            updates["started_at"] = None
            updates["finished_at"] = None
    if updates:
        assignments = ", ".join(f"{key} = ?" for key in updates)
        db.x(
            f"UPDATE tasks SET {assignments} WHERE id = ? AND company_id = ?",
            tuple(updates.values()) + (task_id, task["company_id"]),
        )
    return get_task(task_id)


def list_task_executions(company_id: str, task_id: str) -> List[Dict[str, Any]]:
    task = get_task(task_id)
    if not task or task["company_id"] != company_id:
        raise CompanyError(f"任务不存在：{task_id}", 404)
    return audit.list_executions(company_id, task_id)


def list_tasks(project_id: Optional[str] = None, company_id: Optional[str] = None,
               assignee: Optional[str] = None, status: Optional[str] = None,
               limit: int = 200) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM tasks WHERE 1=1"
    args: List[Any] = []
    if project_id:
        sql += " AND project_id = ?"
        args.append(project_id)
    if company_id:
        sql += " AND company_id = ?"
        args.append(company_id)
    if assignee:
        sql += " AND assignee_employee_id = ?"
        args.append(assignee)
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY created_at LIMIT ?"
    args.append(int(limit))
    rows = db.q(sql, tuple(args))
    return [get_task(r["id"]) for r in rows]


def _deps_done(task: Dict[str, Any]) -> bool:
    for dep in task.get("depends_on") or []:
        d = db.q1("SELECT status FROM tasks WHERE id = ?", (dep,))
        if not d or d["status"] != "done":
            return False
    return True


def next_runnable(project_id: str) -> List[Dict[str, Any]]:
    """可执行 = 依赖已全部完成 + 状态 todo（依赖图表达并行度）。"""
    rows = db.q(
        "SELECT id FROM tasks WHERE project_id = ? AND status = 'todo' ORDER BY created_at",
        (project_id,),
    )
    ready = []
    for r in rows:
        t = get_task(r["id"])
        if t and _deps_done(t):
            ready.append(t)
    return ready


def plan_project(project_id: str, playbook: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按剧本把项目拆成任务。after 字段形成依赖（同层任务天然可并行）。"""
    project = get_project(project_id)
    if not project:
        raise CompanyError(f"项目不存在：{project_id}", 404)
    by_role = {e["role"]: e for e in roster(project["company_id"])}
    created: Dict[str, Dict[str, Any]] = {}
    for item in playbook:
        emp = by_role.get(item["role"])
        if not emp:
            continue
        deps = [created[k]["id"] for k in item.get("after", []) if k in created]
        t = create_task(
            project_id,
            item["title"],
            brief=item.get("brief", ""),
            assignee=emp["id"],
            assigner=None,
            depends_on=deps,
            mode=item.get("mode", "pipeline"),
            risk=item.get("risk", "low"),
        )
        created[item["key"]] = t
    _advance_project(project_id)
    return [get_task(t["id"]) for t in created.values()]


def handoff_context(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    """交接记录：把上游交付物收成结构化上下文，下游不重读全库。"""
    blocks = []
    for dep_id in task.get("depends_on") or []:
        dep = get_task(dep_id)
        if not dep:
            continue
        out = dep.get("output") or {}
        blocks.append(
            {
                "from_task": dep["id"],
                "from_title": dep["title"],
                "from_employee": (dep.get("assignee") or {}).get("name"),
                "summary": out.get("summary") or out.get("text") or "",
                "artifact": out.get("artifact"),
            }
        )
    return blocks


def _estimate_tokens(prompt: str, output: str) -> tuple:
    # 中文约 1.5 字/token，保守估计
    return int(len(prompt) / 1.5), int(len(output) / 1.5)


def _summarize(text: str, limit: int = 160) -> str:
    line = " ".join((text or "").split())
    return line[:limit] + ("…" if len(line) > limit else "")


def force_auto_approve(company: Optional[Dict[str, Any]]) -> bool:
    return bool((company or {}).get("status") == "demo_auto")


def _llm_failure(message: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "text": "", "thinking": "", "mode": "error", "model": meta.get("model") or "",
        "usage": {}, "error": message,
    }


def _llm_complete(
    system: str,
    user: str,
    meta: Dict[str, Any],
    execution_context: Optional[ExecutionContext] = None,
) -> Dict[str, Any]:
    """调用 Node 真实模型出口；任何契约或传输错误都返回不可交付的失败结果。"""
    if execution_context is None or not execution_context.authorization.strip():
        return _llm_failure("缺少教研组登录凭据，任务未执行", meta)
    provider = str(meta.get("provider") or "")
    model = str(meta.get("model") or "")
    company_id = str(meta.get("company_id") or "")
    if not provider or not model or not company_id:
        return _llm_failure("任务模型或教研组信息不完整", meta)

    url = os.environ.get("XUEBAN_NODE_URL", "http://127.0.0.1:8002").rstrip("/")
    headers = {"Authorization": execution_context.authorization, **execution_context.model_headers}
    try:
        with httpx.Client(timeout=120.0, trust_env=False) as client:
            response = client.post(
                f"{url}/api/xueban-internal/tasks/complete",
                json={
                    "company_id": company_id, "provider": provider, "model": model,
                    "system": system, "user": user,
                },
                headers=headers,
            )
        if response.status_code != 200:
            try:
                detail = response.json().get("error", {})
                message = detail.get("message") or f"Node 返回 HTTP {response.status_code}"
            except Exception:  # noqa: BLE001 — 非 JSON 错误同样 fail closed
                message = f"Node 返回 HTTP {response.status_code}"
            return _llm_failure(f"真实模型调用失败：{message}", meta)
        result = response.json()
    except Exception as exc:  # noqa: BLE001 — Node 不可用不得降级
        return _llm_failure(f"真实模型服务不可用：{exc}", meta)

    if not isinstance(result, dict) or result.get("mode") != "live":
        return _llm_failure("真实模型返回契约无效", meta)
    text = result.get("text")
    usage = result.get("usage")
    if not isinstance(text, str) or not text.strip():
        return _llm_failure("真实模型没有返回有效内容", meta)
    if not isinstance(result.get("model"), str) or not result["model"].strip():
        return _llm_failure("真实模型返回缺少模型标识", meta)
    if not isinstance(usage, dict):
        return _llm_failure("真实模型返回缺少用量信息", meta)
    return {
        "text": text.strip(),
        "thinking": result.get("thinking") if isinstance(result.get("thinking"), str) else "",
        "mode": "live",
        "model": result["model"],
        "usage": usage,
    }


def run_task(
    task_id: str,
    force: bool = False,
    execution_context: Optional[ExecutionContext] = None,
) -> Dict[str, Any]:
    """执行一个任务：任务协议真正落地的地方。"""
    task = get_task(task_id)
    if not task:
        raise CompanyError(f"任务不存在：{task_id}", 404)
    if task["status"] in ("done", "cancelled"):
        raise CompanyError(f"任务已{task['status']}，不能重复执行", 409)
    if not force and not _deps_done(task):
        raise CompanyError("上游任务尚未完成，任务被阻塞", 409)

    emp = get_employee(task["assignee_employee_id"]) if task.get("assignee_employee_id") else None
    if not emp:
        raise CompanyError("任务没有负责人，先指派教研员", 422)
    if emp.get("status") == "offline":
        raise CompanyError(f"{emp['name']} 已离职", 409)

    project = get_project(task["project_id"])
    company = get_company(task["company_id"])
    versions = audit.list_config_versions(task["company_id"], emp["id"])
    config_version = versions[0] if versions else audit.record_config_version(emp, "legacy_snapshot")

    started_at = db.now()
    started_clock = time.monotonic()
    db.x("UPDATE tasks SET status = 'running', started_at = ? WHERE id = ?", (started_at, task_id))
    db.x("UPDATE employees SET status = 'working' WHERE id = ?", (emp["id"],))

    upstream = handoff_context(task)
    prompt = _build_prompt(company, project, emp, task, upstream, "")
    execution = audit.start_execution(task, emp, config_version, prompt, started_at)

    model_name = emp.get("model_name") or ""
    catalog = MODEL_CATALOG.get(model_name, {"in_cents": 0, "out_cents": 0})
    llm_args = {
        "system": emp.get("system_prompt") or f"你是{company['name']}的{emp['title']}。",
        "user": prompt,
        "meta": {
            "role": emp.get("role"),
            "task": task["title"],
            "feedback": (task.get("review") or {}).get("rejected_reason") or "",
            "company_id": task["company_id"],
            "provider": _PROVIDER_BY_VENDOR.get(emp.get("model_provider") or "", ""),
            "model": model_name,
        },
    }
    result = _llm_complete(
        **llm_args,
        **({"execution_context": execution_context} if execution_context is not None else {}),
    )

    if result.get("mode") != "live":
        error = result.get("error") or "真实模型执行失败"
        output = {
            "text": "", "summary": "", "thinking": "", "mode": "error",
            "model": model_name, "usage": {}, "tokens": {"in": 0, "out": 0},
            "cost_cents": 0, "cost_breakdown": {"model": 0, "labor": 0}, "error": error,
        }
        db.x(
            "UPDATE tasks SET status = 'failed', output_json = ?, handoff_json = ?, finished_at = ? WHERE id = ?",
            (json.dumps(output, ensure_ascii=False), json.dumps(upstream, ensure_ascii=False), db.now(), task_id),
        )
        db.x("UPDATE employees SET status = 'idle' WHERE id = ?", (emp["id"],))
        ended_at = db.now()
        audit.finish_execution(
            execution["id"], "failed", ended_at,
            int((time.monotonic() - started_clock) * 1000), model=model_name, error=error,
        )
        _advance_project(project["id"])
        return {"task": get_task(task_id), "approval": None, "output": output, "error": error}

    text = result["text"]
    usage = result["usage"]
    estimated_in, estimated_out = _estimate_tokens(prompt, text)
    in_tok = usage.get("input_tokens")
    out_tok = usage.get("output_tokens")
    if not isinstance(in_tok, int) or isinstance(in_tok, bool) or in_tok < 0:
        in_tok = estimated_in
    if not isinstance(out_tok, int) or isinstance(out_tok, bool) or out_tok < 0:
        out_tok = estimated_out
    model_cost = int(round(in_tok / 1000 * catalog["in_cents"] + out_tok / 1000 * catalog["out_cents"]))
    # 工时分摊：一个任务按 1/8 个工作日计
    labor_cost = int(round((emp.get("day_rate_cents") or 0) / 8))
    cost = model_cost + labor_cost

    output = {
        "text": text,
        "summary": _summarize(text),
        "thinking": result.get("thinking") or "",
        "mode": "live",
        "model": result["model"],
        "usage": usage,
        "tokens": {"in": in_tok, "out": out_tok},
        "cost_cents": cost,
        "cost_breakdown": {"model": model_cost, "labor": labor_cost},
    }

    # 风险闸门：中高风险任务做完不直接算完，先挂审批
    risk = (task.get("review") or {}).get("risk", "low")
    needs_approval = risk in ("medium", "high") and not force_auto_approve(company)

    db.x(
        """UPDATE tasks SET status = ?, output_json = ?, handoff_json = ?, cost_cents = ?,
                           finished_at = ? WHERE id = ?""",
        (
            "review" if needs_approval else "done",
            json.dumps(output, ensure_ascii=False),
            json.dumps(upstream, ensure_ascii=False),
            cost, db.now(), task_id,
        ),
    )
    audit.finish_execution(
        execution["id"], "review" if needs_approval else "done", db.now(),
        int((time.monotonic() - started_clock) * 1000),
        output_summary=output["summary"], model=result["model"],
    )
    db.x(
        """UPDATE employees SET tasks_done = tasks_done + 1, token_cost_cents = token_cost_cents + ?,
                               status = ? WHERE id = ?""",
        (cost, "awaiting_approval" if needs_approval else "idle", emp["id"]),
    )
    db.x(
        "INSERT INTO ledger (id, company_id, project_id, kind, category, amount_cents, description, ref_id, occurred_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (db.new_id("led"), task["company_id"], project["id"], "expense", "执行成本", cost,
         f"{emp['name']} · {task['title']}", task_id, db.now()),
    )
    _recost_project(project["id"])

    approval = None
    if needs_approval:
        approval = request_approval(
            company_id=task["company_id"],
            project_id=project["id"],
            task_id=task_id,
            kind="task_delivery",
            title=f"{task['title']} · 待验收",
            risk=risk,
            payload={"summary": output["summary"], "by": emp["name"], "cost_cents": cost},
            requested_by=emp["id"],
        )

    return {"task": get_task(task_id), "approval": approval, "output": output}


def _build_prompt(company: Dict[str, Any], project: Dict[str, Any], emp: Dict[str, Any],
                  task: Dict[str, Any], upstream: List[Dict[str, Any]], ctx_block: str) -> str:
    parts = [
        f"教研组：{company['name']}（{company.get('industry') or ''}）",
        f"项目：{project['name']}｜目标：{project.get('goal') or ''}",
        f"你的岗位：{emp['title']}（{emp.get('seniority') or 'mid'}）",
        f"技能：{'、'.join(emp.get('skills') or []) or '按岗位常规执行'}",
        f"允许使用的工具权限：{'、'.join(emp.get('tools') or []) or '无'}；不得声称使用未授权工具",
        f"本次任务：{task['title']}",
    ]
    if task.get("brief"):
        parts.append(f"要求：{task['brief']}")
    if upstream:
        lines = [
            f"- {u['from_employee'] or '上游'} 交付《{u['from_title']}》：{(u['summary'] or '')[:120]}"
            for u in upstream
        ]
        parts.append("上游交接：\n" + "\n".join(lines))
    if ctx_block:
        parts.append("课程资源库：\n" + ctx_block)
    parts.append("请直接交付可用的工作成果，不要解释过程。")
    return "\n\n".join(parts)


def run_ready(
    project_id: str,
    workers: int = 3,
    execution_context: Optional[ExecutionContext] = None,
) -> List[Dict[str, Any]]:
    """并行跑当前所有可执行任务（依赖图的同一层）。"""
    from concurrent.futures import ThreadPoolExecutor

    ready = next_runnable(project_id)
    if not ready:
        return []
    batch = ready[:workers]
    context_args = {"execution_context": execution_context} if execution_context is not None else {}
    with ThreadPoolExecutor(max_workers=len(batch), thread_name_prefix="sf-xueban") as ex:
        futures = [ex.submit(run_task, t["id"], **context_args) for t in batch]
        out = []
        for f in futures:
            try:
                out.append(f.result())
            except CompanyError as exc:
                out.append({"error": exc.message})
            except Exception as exc:  # noqa: BLE001 — 单个任务失败不拖垮整个项目
                out.append({"error": f"{type(exc).__name__}: {exc}"})
        return out


def delete_project(project_id: str) -> Dict[str, int]:
    """删除内部项目及其任务、审批与台账记录。"""
    project = get_project(project_id)
    if not project:
        raise CompanyError(f"项目不存在：{project_id}", 404)
    linked_order = project.get("order_id") or db.q1("SELECT id FROM orders WHERE project_id = ?", (project_id,))
    if linked_order:
        raise CompanyError("项目正在运行中，请先停止后再删除", 409)
    return db.delete_project_cascade(project_id)


def run_project(
    project_id: str,
    max_rounds: int = 12,
    workers: int = 3,
    retry: bool = False,
    execution_context: Optional[ExecutionContext] = None,
) -> Dict[str, Any]:
    """把项目推到「没有可执行任务」或「卡在人工审批」为止；retry 只重置失败任务。"""
    project = get_project(project_id)
    if not project:
        raise CompanyError(f"项目不存在：{project_id}", 404)
    if retry:
        db.x("UPDATE tasks SET status = 'todo', started_at = NULL, finished_at = NULL WHERE project_id = ? AND status = 'failed'", (project_id,))
    db.x("UPDATE xueban_projects SET status = 'executing', updated_at = ? WHERE id = ?", (db.now(), project_id))

    rounds: List[List[Dict[str, Any]]] = []
    stopped = "completed"
    context_args = {"execution_context": execution_context} if execution_context is not None else {}
    for _ in range(max_rounds):
        batch = run_ready(project_id, workers=workers, **context_args)
        if not batch:
            break
        rows = []
        for r in batch:
            task = r.get("task") or {}
            rows.append(
                {
                    "task_id": task.get("id"),
                    "title": task.get("title"),
                    "by": (task.get("assignee") or {}).get("name"),
                    "status": task.get("status"),
                    "cost": _money(task.get("cost_cents") or 0),
                    "needs_approval": bool(r.get("approval")),
                    "error": r.get("error"),
                }
            )
        rounds.append(rows)
        if any(r.get("approval") for r in batch):
            stopped = "approval"
            break
    else:
        stopped = "max_rounds"

    _advance_project(project_id)
    pending = [
        a for a in list_approvals(project["company_id"], status="pending")
        if a.get("project_id") == project_id
    ]
    return {
        "project": get_project(project_id),
        "rounds": rounds,
        "stopped": stopped,
        "pending_approvals": pending,
        "tasks": list_tasks(project_id=project_id),
    }


# ------------------------------------------------------------------ 审批

def request_approval(company_id: str, project_id: Optional[str], task_id: Optional[str],
                     kind: str, title: str, risk: str = "medium",
                     payload: Optional[Dict] = None,
                     requested_by: Optional[str] = None) -> Dict[str, Any]:
    aid = db.new_id("apr")
    db.x(
        """INSERT INTO approvals (id, company_id, project_id, task_id, kind, title, risk, payload_json,
                                  requested_by, status, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (aid, company_id, project_id, task_id, kind, title, risk,
         json.dumps(payload or {}, ensure_ascii=False), requested_by, "pending", db.now()),
    )
    return get_approval(aid)


def get_approval(approval_id: str) -> Optional[Dict[str, Any]]:
    a = db.q1("SELECT * FROM approvals WHERE id = ?", (approval_id,))
    if a:
        a["payload"] = _j(a.get("payload_json"), {})
    return a


def list_approvals(company_id: str, status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM approvals WHERE company_id = ?"
    args: List[Any] = [company_id]
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(int(limit))
    return [dict(a, payload=_j(a.get("payload_json"), {})) for a in db.q(sql, tuple(args))]


def decide_approval(approval_id: str, decision: str, note: str = "",
                    decided_by: str = "老师") -> Dict[str, Any]:
    if decision not in ("approved", "rejected"):
        raise CompanyError("决定只能是 approved 或 rejected", 422)
    a = get_approval(approval_id)
    if not a:
        raise CompanyError(f"审批不存在：{approval_id}", 404)
    if a["status"] != "pending":
        raise CompanyError("该审批已处理", 409)

    db.x(
        "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ?, note = ? WHERE id = ?",
        (decision, decided_by, db.now(), note, approval_id),
    )

    if a.get("task_id"):
        task = get_task(a["task_id"])
        if task:
            if decision == "approved":
                db.x("UPDATE tasks SET status = 'done' WHERE id = ?", (a["task_id"],))
                if task.get("assignee_employee_id"):
                    db.x("UPDATE employees SET status = 'idle' WHERE id = ?", (task["assignee_employee_id"],))
            else:
                # 驳回即返工：任务回 todo，保留上一版产出作为证据
                review = task.get("review") or {}
                review["rejected_reason"] = note or "验收未通过"
                review["rejected_at"] = db.now()
                db.x(
                    "UPDATE tasks SET status = 'todo', review_json = ? WHERE id = ?",
                    (json.dumps(review, ensure_ascii=False), a["task_id"]),
                )
                if task.get("assignee_employee_id"):
                    db.x("UPDATE employees SET status = 'idle' WHERE id = ?", (task["assignee_employee_id"],))

    if a.get("project_id"):
        _advance_project(a["project_id"])
    return get_approval(approval_id)


def _advance_project(project_id: str) -> None:
    project = get_project(project_id)
    if not project:
        return
    rows = db.q("SELECT status, COUNT(*) AS n FROM tasks WHERE project_id = ? GROUP BY status", (project_id,))
    counts = {r["status"]: r["n"] for r in rows}
    total = sum(counts.values())
    if total and counts.get("done", 0) == total:
        db.x("UPDATE xueban_projects SET status = 'delivered', updated_at = ? WHERE id = ?", (db.now(), project_id))
    elif counts.get("running") or counts.get("review"):
        db.x("UPDATE xueban_projects SET status = 'executing', updated_at = ? WHERE id = ?", (db.now(), project_id))
    elif counts.get("failed"):
        db.x("UPDATE xueban_projects SET status = 'failed', updated_at = ? WHERE id = ?", (db.now(), project_id))
    elif project["status"] == "planning" and total:
        db.x("UPDATE xueban_projects SET status = 'ready', updated_at = ? WHERE id = ?", (db.now(), project_id))


# ------------------------------------------------------------------ 经营数据

def pnl(company_id: str) -> Dict[str, Any]:
    income = db.q1(
        "SELECT COALESCE(SUM(amount_cents),0) AS s FROM ledger WHERE company_id = ? AND kind = 'income'",
        (company_id,),
    )["s"]
    expense = db.q1(
        "SELECT COALESCE(SUM(amount_cents),0) AS s FROM ledger WHERE company_id = ? AND kind = 'expense'",
        (company_id,),
    )["s"]
    projects = list_projects(company_id, limit=200)
    delivered = [p for p in projects if p["status"] == "delivered"]
    return {
        "revenue": _money(income),
        "cost": _money(expense),
        "profit": _money(income - expense),
        "margin": round((income - expense) / income * 100, 1) if income else 0.0,
        "projects_total": len(projects),
        "projects_delivered": len(delivered),
        "avg_project_cost": _money(expense / len(projects)) if projects else 0,
    }


def dashboard(company_id: str) -> Dict[str, Any]:
    company = company_or_404(company_id)
    emps = roster(company_id)
    projects = list_projects(company_id, limit=200)
    tasks = list_tasks(company_id=company_id, limit=500)
    pending = list_approvals(company_id, status="pending")

    by_status: Dict[str, int] = {}
    for t in tasks:
        by_status[t["status"]] = by_status.get(t["status"], 0) + 1

    return {
        "company": company,
        "pnl": pnl(company_id),
        "headcount": {
            "total": len(emps),
            "working": sum(1 for e in emps if e["status"] == "working"),
            "idle": sum(1 for e in emps if e["status"] == "idle"),
            "awaiting": sum(1 for e in emps if e["status"] == "awaiting_approval"),
        },
        "tasks": {"total": len(tasks), "by_status": by_status},
        "approvals_pending": len(pending),
        "projects": [
            {
                "id": p["id"], "name": p["name"], "status": p["status"], "client": p.get("client_name"),
                "budget": _money(p["budget_cents"]), "cost": _money(p["cost_cents"]),
                "revenue": _money(p["revenue_cents"]),
            }
            for p in projects[:6]
        ],
    }


def add_income(company_id: str, project_id: Optional[str], amount_cents: int,
               description: str, category: str = "项目收入",
               ref_id: Optional[str] = None) -> Dict[str, Any]:
    db.x(
        "INSERT INTO ledger (id, company_id, project_id, kind, category, amount_cents, description, ref_id, occurred_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (db.new_id("led"), company_id, project_id, "income", category, int(amount_cents),
         description, ref_id, db.now()),
    )
    if project_id:
        db.x(
            "UPDATE xueban_projects SET revenue_cents = revenue_cents + ?, updated_at = ? WHERE id = ?",
            (int(amount_cents), db.now(), project_id),
        )
    return pnl(company_id)


def ledger_entries(company_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    rows = db.q(
        "SELECT * FROM ledger WHERE company_id = ? ORDER BY occurred_at DESC LIMIT ?",
        (company_id, int(limit)),
    )
    for r in rows:
        r["amount"] = _money(r["amount_cents"])
    return rows
