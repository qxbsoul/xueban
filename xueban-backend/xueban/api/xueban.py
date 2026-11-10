"""XUEBAN OS HTTP API — 移植自 backend-poc/app/main.py 的 company/commerce 端点。

所有路由挂在 /api/xueban 前缀下，与 Xueban 既有 /api/* 隔离：
  company / departments / roster / hire / fire / org      — 组织
  projects / tasks / approvals / pnl / ledger / income    — 经营
  models / seed                                           — 元数据 / 初始化

与 backend-poc 的差异：
- 无旧版单人 run 概念。
- 端点统一加 xueban 前缀，避免与前端既有 /api/* 路由撞名。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from .xueban_auth import authorize_request, ensure_company, list_user_companies
from ..services import xueban_company as company
from ..services import xueban_seed
from ..services.xueban_company import create_department

router = APIRouter(prefix="/api/xueban", tags=["xueban"], dependencies=[Depends(authorize_request)])


def _company_or_404(company_id: Optional[str] = None) -> Dict[str, Any]:
    if company_id:
        return company.company_or_404(company_id)
    raise HTTPException(422, "缺少公司 ID")


def _company_err(exc: company.CompanyError) -> HTTPException:
    return HTTPException(exc.status, exc.message)


def _execution_context(request: Request) -> company.ExecutionContext:
    # Authorization 仅用于公司鉴权；模型 key 走独立白名单头，避免两套凭据冲突。
    model_headers = {
        name: value for name, value in request.headers.items()
        if name.lower() in company.MODEL_HEADER_NAMES and value.strip()
    }
    return company.ExecutionContext(
        authorization=request.headers.get("authorization", ""),
        model_headers=model_headers,
    )


def _order_err(exc: orders.OrderError) -> HTTPException:
    return HTTPException(exc.status, exc.message)


# ------------------------------------------------------------------ meta

@router.get("/models")
def models():
    return {"items": [dict(v, id=k) for k, v in company.MODEL_CATALOG.items()]}


@router.post("/seed")
def seed(request: Request):
    """只初始化当前账号的公司，不再创建或返回共享默认公司。"""
    c = ensure_company(request.state.xueban_user)
    xueban_seed.ensure_products(c["id"])
    return {"company": c}


# ------------------------------------------------------------------ company: 组织

@router.get("/company")
def company_default(request: Request):
    c = ensure_company(request.state.xueban_user)
    return {"company": c, "dashboard": company.dashboard(c["id"])}


@router.get("/companies")
def companies(request: Request):
    return {"items": list_user_companies(request.state.xueban_user["id"])}


@router.get("/company/{company_id}")
def company_detail(company_id: str):
    _company_or_404(company_id)
    return {"dashboard": company.dashboard(company_id)}


@router.get("/company/{company_id}/org")
def company_org(company_id: str):
    try:
        return company.org_chart(company_id)
    except company.CompanyError as exc:
        raise _company_err(exc) from exc


@router.post("/company/{company_id}/org/publish")
def company_publish_org(company_id: str, payload: dict = Body(...)):
    """人工确认后的草案才进入这里；部门与员工在同一 SQLite 事务中创建。"""
    _company_or_404(company_id)
    try:
        return company.publish_org_draft(company_id, payload.get("draft"))
    except company.CompanyError as exc:
        raise _company_err(exc) from exc


@router.get("/company/{company_id}/roster")
def company_roster(company_id: str):
    _company_or_404(company_id)
    return {"items": company.roster(company_id)}


@router.get("/company/{company_id}/departments")
def company_departments(company_id: str):
    _company_or_404(company_id)
    return {"items": company.list_departments(company_id)}


@router.post("/company/{company_id}/departments")
def company_new_department(company_id: str, payload: dict = Body(...)):
    co = _company_or_404(company_id)
    dept = create_department(
        co["id"],
        payload.get("name") or "新部门",
        charter=payload.get("charter") or "",
        budget_cents=int(payload.get("budget_cents") or 0),
    )
    return {"department": dept}


@router.patch("/company/{company_id}/departments/{department_id}")
def company_update_department(company_id: str, department_id: str, payload: dict = Body(...)):
    try:
        dept = company.update_department(
            company_id,
            department_id,
            **{key: payload[key] for key in ("name", "charter") if key in payload},
        )
    except company.CompanyError as exc:
        raise _company_err(exc) from exc
    return {"department": dept}


@router.delete("/company/{company_id}/departments/{department_id}", status_code=204)
def company_delete_department(company_id: str, department_id: str):
    try:
        company.delete_department(company_id, department_id)
    except company.CompanyError as exc:
        raise _company_err(exc) from exc


@router.get("/company/{company_id}/employees/{employee_id}")
def employee_detail(company_id: str, employee_id: str):
    emp = company.get_employee(employee_id)
    if not emp or emp["company_id"] != company_id:
        raise HTTPException(404, f"员工不存在：{employee_id}")
    return {"employee": emp}


@router.get("/company/{company_id}/employees/{employee_id}/config-versions")
def employee_config_versions(company_id: str, employee_id: str):
    try:
        return {"items": company.list_config_versions(company_id, employee_id)}
    except company.CompanyError as exc:
        raise _company_err(exc) from exc


@router.post("/company/{company_id}/employees/{employee_id}/config-versions/{version}/restore")
def employee_restore_config_version(company_id: str, employee_id: str, version: int):
    try:
        return company.restore_config_version(company_id, employee_id, version)
    except company.CompanyError as exc:
        raise _company_err(exc) from exc


@router.patch("/company/{company_id}/employees/{employee_id}")
def company_update_employee(company_id: str, employee_id: str, payload: dict = Body(...)):
    allowed = {
        "name", "title", "department_id", "manager_id", "system_prompt",
        "avatar", "status", "model_name", "skills", "tools",
    }
    try:
        emp = company.update_employee(
            company_id,
            employee_id,
            **{key: value for key, value in payload.items() if key in allowed},
        )
    except company.CompanyError as exc:
        raise _company_err(exc) from exc
    return {"employee": emp}


@router.post("/company/{company_id}/employees")
def company_hire(company_id: str, payload: dict = Body(...)):
    try:
        emp = company.hire(
            company_id,
            payload.get("name") or "新员工",
            payload.get("role") or "writer",
            department_id=payload.get("department_id"),
            manager_id=payload.get("manager_id"),
            title=payload.get("title") or "",
            seniority=payload.get("seniority") or "mid",
            system_prompt=payload.get("system_prompt") or "",
            avatar=payload.get("avatar"),
            skills=payload.get("skills") or [],
            tools=payload.get("tools") or [],
            permissions=payload.get("permissions") or [],
            model_name=payload.get("model_name") or "gpt-4o",
            day_rate_cents=int(payload.get("day_rate_cents") or 1000),
        )
    except company.CompanyError as exc:
        raise _company_err(exc) from exc
    return {"employee": emp}


@router.post("/company/{company_id}/employees/{employee_id}/fire")
def company_fire(company_id: str, employee_id: str, payload: dict = Body(default={})):
    try:
        return company.fire(
            employee_id,
            reassign_to=payload.get("reassign_to"),
            company_id=company_id,
        )
    except company.CompanyError as exc:
        raise _company_err(exc) from exc


# ------------------------------------------------------------------ company: 项目 / 任务

@router.get("/company/{company_id}/projects")
def company_projects(company_id: str, limit: int = Query(50, ge=1, le=200)):
    _company_or_404(company_id)
    return {"items": company.list_projects(company_id, limit=limit)}


@router.post("/company/{company_id}/projects")
def company_new_project(company_id: str, payload: dict = Body(...)):
    """老板下一个目标，平台自动拆成任务并分派。"""
    goal = (payload.get("goal") or "").strip()
    if len(goal) < 4:
        raise HTTPException(422, "请把业务目标说清楚，至少要一句话")
    co = _company_or_404(company_id)
    name = payload.get("name") or goal[:24]
    project = company.create_project(
        co["id"], name, goal,
        client_name=payload.get("client_name") or "",
        budget_cents=int(payload.get("budget_cents") or 0),
        order_id=payload.get("order_id"),
    )
    tasks = []
    if payload.get("auto_plan", True):
        tasks = company.plan_project(project["id"], xueban_seed.PLAYBOOK)
    return {"project": company.get_project(project["id"]), "tasks": tasks}


@router.get("/projects/{project_id}")
def project_detail(project_id: str):
    p = company.get_project(project_id)
    if not p:
        raise HTTPException(404, f"项目不存在：{project_id}")
    tasks = company.list_tasks(project_id=project_id)
    approvals = [a for a in company.list_approvals(p["company_id"], limit=100) if a.get("project_id") == project_id]
    return {"project": p, "tasks": tasks, "approvals": approvals}


@router.delete("/projects/{project_id}", status_code=204)
def project_delete(project_id: str):
    try:
        company.delete_project(project_id)
    except company.CompanyError as exc:
        raise _company_err(exc) from exc


@router.post("/projects/{project_id}/run")
def project_run(project_id: str, request: Request, payload: dict = Body(default={})):
    try:
        return company.run_project(
            project_id,
            max_rounds=int(payload.get("max_rounds") or 12),
            workers=int(payload.get("workers") or 3),
            retry=bool(payload.get("retry")),
            execution_context=_execution_context(request),
        )
    except company.CompanyError as exc:
        raise _company_err(exc) from exc


@router.get("/tasks/{task_id}")
def task_detail(task_id: str):
    t = company.get_task(task_id)
    if not t:
        raise HTTPException(404, f"任务不存在：{task_id}")
    return {"task": t}


@router.patch("/tasks/{task_id}")
def task_update(task_id: str, payload: dict = Body(...)):
    allowed = {"title", "brief", "assignee", "assignee_employee_id", "priority"}
    try:
        task = company.update_task(
            task_id, **{key: value for key, value in payload.items() if key in allowed},
        )
    except company.CompanyError as exc:
        raise _company_err(exc) from exc
    return {"task": task}


@router.get("/tasks/{task_id}/executions")
def task_executions(task_id: str):
    t = company.get_task(task_id)
    if not t:
        raise HTTPException(404, f"任务不存在：{task_id}")
    try:
        return {"items": company.list_task_executions(t["company_id"], task_id)}
    except company.CompanyError as exc:
        raise _company_err(exc) from exc


@router.post("/tasks/{task_id}/run")
def task_run(task_id: str, request: Request, payload: dict = Body(default={})):
    try:
        return company.run_task(
            task_id,
            force=bool(payload.get("force")),
            execution_context=_execution_context(request),
        )
    except company.CompanyError as exc:
        raise _company_err(exc) from exc


# ------------------------------------------------------------------ approvals

@router.get("/company/{company_id}/approvals")
def company_approvals(company_id: str, status: str = Query("", description="pending / approved / rejected")):
    _company_or_404(company_id)
    return {"items": company.list_approvals(company_id, status=status or None)}


@router.post("/approvals/{approval_id}/decide")
def approval_decide(approval_id: str, payload: dict = Body(...)):
    try:
        a = company.decide_approval(
            approval_id,
            payload.get("decision") or "",
            note=payload.get("note") or "",
            decided_by=payload.get("decided_by") or "老板",
        )
    except company.CompanyError as exc:
        raise _company_err(exc) from exc
    return {"approval": a}


# ------------------------------------------------------------------ pnl / ledger

@router.get("/company/{company_id}/pnl")
def company_pnl(company_id: str):
    _company_or_404(company_id)
    return {"pnl": company.pnl(company_id), "ledger": company.ledger_entries(company_id, limit=50)}


@router.post("/company/{company_id}/income")
def company_income(company_id: str, payload: dict = Body(...)):
    try:
        amount = int(payload.get("amount_cents") or 0)
    except (TypeError, ValueError):
        raise HTTPException(422, "金额需为整数分") from None
    if amount <= 0:
        raise HTTPException(422, "金额必须大于 0")
    try:
        return company.add_income(
            company_id,
            payload.get("project_id"),
            amount,
            payload.get("description") or "项目收款",
            category=payload.get("category") or "项目收入",
            ref_id=payload.get("ref_id"),
        )
    except company.CompanyError as exc:
        raise _company_err(exc) from exc
