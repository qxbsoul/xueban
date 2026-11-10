"""XUEBAN 本地账号、会话与教研组归属校验。

账号和业务数据同存 xueban.sqlite；密码与会话令牌均不以明文入库。
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ..services import xueban_company as company
from ..services import xueban_seed
from ..services import xueban_store as db


auth_router = APIRouter(prefix="/api/xueban/auth", tags=["xueban-auth"])
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _public_user(user: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": user["id"],
        "email": user.get("email"),
        "display_name": user["display_name"],
        "auth_type": user["auth_type"],
    }


def _password_hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _bearer(request: Request) -> str:
    value = request.headers.get("authorization", "")
    if not value.startswith("Bearer "):
        raise HTTPException(401, "请先登录或进入未注册演示")
    token = value[7:].strip()
    if not token:
        raise HTTPException(401, "登录状态无效，请重新登录")
    return token


def current_user(request: Request) -> Dict[str, Any]:
    db.init()
    token_hash = _token_hash(_bearer(request))
    row = db.q1(
        """SELECT u.*, s.expires_at FROM xueban_sessions s
           JOIN xueban_users u ON u.id = s.user_id WHERE s.token_hash = ?""",
        (token_hash,),
    )
    if not row or int(row["expires_at"]) <= int(time.time()):
        db.x("DELETE FROM xueban_sessions WHERE token_hash = ?", (token_hash,))
        raise HTTPException(401, "登录已过期，请重新登录")
    return row


def list_user_companies(user_id: str):
    return db.q("SELECT * FROM companies WHERE owner_user_id = ? ORDER BY created_at", (user_id,))


def ensure_company(user: Dict[str, Any], company_name: Optional[str] = None):
    existing = db.q1("SELECT * FROM companies WHERE owner_user_id = ? ORDER BY created_at LIMIT 1", (user["id"],))
    if existing:
        return existing
    # 首次启用账号层时，把原来的未归属演示教研组交给第一个账号，保留既有数据。
    legacy = db.q1("SELECT * FROM companies WHERE owner_user_id IS NULL ORDER BY created_at LIMIT 1")
    has_owned = db.q1("SELECT 1 AS found FROM companies WHERE owner_user_id IS NOT NULL LIMIT 1")
    if legacy and not has_owned:
        name = (company_name or legacy["name"]).strip()
        db.x(
            "UPDATE companies SET name = ?, owner_name = ?, owner_user_id = ? WHERE id = ?",
            (name, user["display_name"], user["id"], legacy["id"]),
        )
        return company.get_company(legacy["id"])
    return xueban_seed.create_workspace(
        (company_name or f"{user['display_name']}的教研组").strip(), user["display_name"], user["id"]
    )


def _issue_session(user: Dict[str, Any]) -> Dict[str, Any]:
    token = secrets.token_urlsafe(32)
    ttl = 24 * 3600 if user["auth_type"] == "guest" else 7 * 24 * 3600
    db.x(
        "INSERT INTO xueban_sessions (token_hash, user_id, expires_at, created_at) VALUES (?,?,?,?)",
        (_token_hash(token), user["id"], int(time.time()) + ttl, db.now()),
    )
    return {"token": token, "user": _public_user(user), "company": ensure_company(user)}


def issue_schedule_session(user: Dict[str, Any]) -> str:
    """为教研组定时任务签发长期服务会话；原令牌只进入加密任务配置。"""
    token = secrets.token_urlsafe(32)
    db.x(
        "INSERT INTO xueban_sessions (token_hash, user_id, expires_at, created_at) VALUES (?,?,?,?)",
        (_token_hash(token), user["id"], int(time.time()) + 10 * 365 * 24 * 3600, db.now()),
    )
    return token


def authorize_request(request: Request) -> Dict[str, Any]:
    user = current_user(request)
    request.state.xueban_user = user
    params = request.path_params
    company_id = params.get("company_id")
    if params.get("project_id"):
        project = company.get_project(params["project_id"])
        company_id = project.get("company_id") if project else None
    elif params.get("task_id"):
        task = company.get_task(params["task_id"])
        company_id = task.get("company_id") if task else None
    elif params.get("approval_id"):
        approval = db.q1("SELECT company_id FROM approvals WHERE id = ?", (params["approval_id"],))
        company_id = approval.get("company_id") if approval else None
    if company_id:
        owned = db.q1(
            "SELECT 1 AS found FROM companies WHERE id = ? AND owner_user_id = ?",
            (company_id, user["id"]),
        )
        if not owned:
            raise HTTPException(404, "资源不存在或不属于当前教研组")
    return user


@auth_router.post("/register")
def register(payload: dict = Body(...)):
    db.init()
    email = str(payload.get("email") or "").strip().lower()
    password = str(payload.get("password") or "")
    display_name = str(payload.get("display_name") or "").strip()
    company_name = str(payload.get("company_name") or "").strip()
    if not _EMAIL.match(email):
        raise HTTPException(422, "请输入有效邮箱")
    if len(password) < 8:
        raise HTTPException(422, "密码至少 8 位")
    if not display_name or not company_name:
        raise HTTPException(422, "请填写老师姓名和教研组名称")
    if db.q1("SELECT 1 AS found FROM xueban_users WHERE email = ?", (email,)):
        raise HTTPException(409, "该邮箱已注册，请直接登录")
    salt = secrets.token_hex(16)
    user_id = db.new_id("user")
    db.x(
        """INSERT INTO xueban_users (id, email, password_hash, password_salt, display_name, auth_type, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (user_id, email, _password_hash(password, salt), salt, display_name, "password", db.now()),
    )
    user = db.q1("SELECT * FROM xueban_users WHERE id = ?", (user_id,))
    workspace = ensure_company(user, company_name)
    xueban_seed.ensure_products(workspace["id"])
    return _issue_session(user)


@auth_router.post("/login")
def login(payload: dict = Body(...)):
    db.init()
    email = str(payload.get("email") or "").strip().lower()
    password = str(payload.get("password") or "")
    user = db.q1("SELECT * FROM xueban_users WHERE email = ? AND auth_type = 'password'", (email,))
    if not user or not hmac.compare_digest(user["password_hash"], _password_hash(password, user["password_salt"])):
        raise HTTPException(401, "邮箱或密码不正确")
    return _issue_session(user)


@auth_router.post("/guest")
def guest():
    db.init()
    user_id = db.new_id("guest")
    db.x(
        "INSERT INTO xueban_users (id, display_name, auth_type, created_at) VALUES (?,?,?,?)",
        (user_id, "演示老师", "guest", db.now()),
    )
    user = db.q1("SELECT * FROM xueban_users WHERE id = ?", (user_id,))
    workspace = ensure_company(user, "未注册演示教研组")
    xueban_seed.ensure_products(workspace["id"])
    return _issue_session(user)


@auth_router.get("/me")
def me(user: Dict[str, Any] = Depends(current_user)):
    return {"user": _public_user(user), "company": ensure_company(user)}


@auth_router.post("/logout")
def logout(request: Request):
    token_hash = _token_hash(_bearer(request))
    db.x("DELETE FROM xueban_sessions WHERE token_hash = ?", (token_hash,))
    return {"ok": True}
