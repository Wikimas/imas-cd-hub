"""账户页面与 API（ADR 0003）：登录 / 注册（邀请码）/ 登出 / 管理页。

- 首个管理员由 CLI ``user bootstrap-admin`` 创建；此后发号/停用/重置密码走 Web /admin。
- 注册是「管理员发号」的变体：只有持一次性邀请码才能注册（符合 PRINCIPLES §7 不做开放注册）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from imas_hub import __version__
from imas_hub.auth import (
    SESSION_COOKIE,
    check_invite,
    consume_invite,
    create_invite,
    hash_password,
    make_session_token,
    random_password,
    record_audit,
    require_admin,
    utc_now,
    validate_password,
    validate_username,
    verify_password,
)
from imas_hub.config import COOKIE_SECURE
from imas_hub.db.database import connect

router = APIRouter()

TEMPLATES_DIR = Path(__file__).with_name("templates")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _set_session_cookie(response: JSONResponse, uid: int, remember: bool) -> None:
    token, ttl = make_session_token(uid, remember)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(ttl.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        path="/",
    )


def _auth_failure(msg: str) -> HTTPException:
    return HTTPException(401, msg)


class LoginBody(BaseModel):
    username: str
    password: str
    remember: bool = False


class RegisterBody(BaseModel):
    username: str
    password: str
    code: str  # 一次性邀请码


class InviteBody(BaseModel):
    days: int = Field(default=7, ge=1, le=90, description="有效期天数")


@router.get("/login", include_in_schema=False)
def login_page(request: Request):
    if getattr(request.state, "user", None):
        from fastapi.responses import RedirectResponse

        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        request, "login.html", {"version": __version__}
    )


@router.post("/api/login")
def api_login(body: LoginBody):
    username = (body.username or "").strip()
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, username, password_hash, role, active FROM user "
            "WHERE username=? COLLATE NOCASE",
            (username,),
        ).fetchone()
        if not row or not row["active"] or not verify_password(body.password, row["password_hash"]):
            raise _auth_failure("用户名或密码错误")
        uid = int(row["id"])
        now = utc_now()
        conn.execute(
            "UPDATE user SET last_login_at=? WHERE id=?", (now, uid)
        )
        record_audit(conn, uid, "auth.login", "user", uid, row["username"])
        conn.commit()
    finally:
        conn.close()
    resp = JSONResponse({"ok": True, "url": "/", "username": row["username"]})
    _set_session_cookie(resp, uid, bool(body.remember))
    return resp


@router.get("/register", include_in_schema=False)
def register_page(request: Request, code: str = ""):
    if getattr(request.state, "user", None):
        from fastapi.responses import RedirectResponse

        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        request,
        "register.html",
        {"code": (code or "").strip(), "version": __version__},
    )


@router.post("/api/register")
def api_register(body: RegisterBody):
    """持一次性邀请码自助注册（= 管理员发号）；成功后自动登录。"""
    username = (body.username or "").strip()
    err = validate_username(username)
    if err:
        raise HTTPException(400, err)
    err = validate_password(body.password)
    if err:
        raise HTTPException(400, err)
    conn = connect()
    try:
        inv = check_invite(conn, body.code)
        if not inv:
            raise HTTPException(400, "邀请码无效、已使用或已过期")
        if conn.execute(
            "SELECT 1 FROM user WHERE username=? COLLATE NOCASE", (username,)
        ).fetchone():
            raise HTTPException(409, "用户名已存在")
        now = utc_now()
        cur = conn.execute(
            """
            INSERT INTO user(username, password_hash, role, active, created_at, updated_at)
            VALUES (?, ?, 'editor', 1, ?, ?)
            """,
            (username, hash_password(body.password), now, now),
        )
        uid = int(cur.lastrowid)
        consume_invite(conn, body.code, uid)
        record_audit(conn, uid, "auth.register", "user", uid, username)
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    finally:
        conn.close()
    resp = JSONResponse({"ok": True, "url": "/", "username": username})
    _set_session_cookie(resp, uid, False)
    return resp


@router.post("/api/logout")
def api_logout(request: Request):
    user = getattr(request.state, "user", None)
    if user:
        conn = connect()
        try:
            record_audit(conn, user["id"], "auth.logout", "user", user["id"], user["username"])
            conn.commit()
        finally:
            conn.close()
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# —— 管理页（admin） ——


@router.get("/admin", include_in_schema=False)
def admin_page(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        from fastapi.responses import RedirectResponse

        return RedirectResponse("/login", status_code=302)
    if user["role"] != "admin":
        raise HTTPException(403, "需要管理员权限")
    conn = connect()
    try:
        users = [
            dict(r)
            for r in conn.execute(
                """
                SELECT u.id, u.username, u.role, u.active, u.last_login_at,
                       u.created_at,
                       (SELECT COUNT(*) FROM audit_log a
                        WHERE a.user_id = u.id) AS audit_count
                FROM user u
                ORDER BY u.active DESC, u.id
                """
            ).fetchall()
        ]
        invites = [
            dict(r)
            for r in conn.execute(
                """
                SELECT i.id, i.code, i.active, i.expires_at, i.created_at,
                       i.used_at, i.used_by, u.username AS used_by_name
                FROM invite i
                LEFT JOIN user u ON u.id = i.used_by
                ORDER BY i.created_at DESC
                LIMIT 50
                """
            ).fetchall()
        ]
        logs = [
            dict(r)
            for r in conn.execute(
                """
                SELECT a.created_at, u.username, a.action, a.entity,
                       a.entity_id, a.detail
                FROM audit_log a
                LEFT JOIN user u ON u.id = a.user_id
                ORDER BY a.created_at DESC, a.id DESC
                LIMIT 200
                """
            ).fetchall()
        ]
    finally:
        conn.close()
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "users": users,
            "invites": invites,
            "logs": logs,
            "now_iso": utc_now(),
            "version": __version__,
        },
    )


@router.post("/api/admin/invites")
def api_admin_create_invite(body: InviteBody, admin: dict = Depends(require_admin)):
    conn = connect()
    try:
        code, expires = create_invite(conn, admin["id"], days=body.days)
        record_audit(
            conn,
            admin["id"],
            "invite.create",
            "invite",
            None,
            f"code={code} days={body.days}",
        )
        conn.commit()
        return {"ok": True, "code": code, "expires_at": expires}
    except HTTPException:
        conn.rollback()
        raise
    finally:
        conn.close()


@router.post("/api/admin/users/{user_id}/toggle")
def api_admin_toggle_user(user_id: int, admin: dict = Depends(require_admin)):
    """停用 / 启用账号（停用后会话与登录立即失效）。"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, username, role, active FROM user WHERE id=?", (user_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "用户不存在")
        if row["role"] == "admin" and row["id"] == admin["id"]:
            raise HTTPException(400, "不能停用自己的账号")
        new_active = 0 if row["active"] else 1
        conn.execute(
            "UPDATE user SET active=?, updated_at=? WHERE id=?",
            (new_active, utc_now(), user_id),
        )
        record_audit(
            conn,
            admin["id"],
            "user.active" if new_active else "user.deactivate",
            "user",
            user_id,
            f"username={row['username']}",
        )
        conn.commit()
        return {"ok": True, "user_id": user_id, "active": new_active}
    except HTTPException:
        conn.rollback()
        raise
    finally:
        conn.close()


@router.post("/api/admin/users/{user_id}/password")
def api_admin_reset_password(user_id: int, admin: dict = Depends(require_admin)):
    """重置为一次性随机密码；明文只返回这一次，由管理员转交。"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, username FROM user WHERE id=?", (user_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "用户不存在")
        plain = random_password()
        conn.execute(
            "UPDATE user SET password_hash=?, updated_at=? WHERE id=?",
            (hash_password(plain), utc_now(), user_id),
        )
        record_audit(
            conn,
            admin["id"],
            "user.password",
            "user",
            user_id,
            f"username={row['username']}",
        )
        conn.commit()
        return {"ok": True, "user_id": user_id, "password": plain}
    except HTTPException:
        conn.rollback()
        raise
    finally:
        conn.close()
