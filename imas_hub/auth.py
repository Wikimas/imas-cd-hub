"""账户域（ADR 0003）：密码哈希 / 签名会话 / 登录依赖 / 邀请码 / 审计日志。

- 密码：stdlib ``pbkdf2_hmac``（OWASP 推荐，600k 迭代），存储格式
  ``pbkdf2$sha256$<iter>$<salt>$<hash>``（自描述，便于将来换算法）。
- 会话：itsdangerous 签名 cookie，无服务端 session 表；「记住我」30 天，否则 8 小时。
- CSRF：SameSite=Lax cookie（跨站 POST 不带 cookie）+ 写请求 Origin/Referer 同源校验
  （浏览器跨站请求必带 Origin；两者都缺的 curl 等非浏览器客户端放行）。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from imas_hub.config import SECRET_KEY

SESSION_COOKIE = "imas_hub_session"
SHORT_TTL = timedelta(hours=8)    # 不勾「记住我」：8 小时
LONG_TTL = timedelta(days=30)     # 「记住我」：30 天
PBKDF2_ITERATIONS = 600_000
PASSWORD_MIN = 8

USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# —— 密码 ——


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return (
        f"pbkdf2$sha256${PBKDF2_ITERATIONS}$"
        f"{salt.hex()}${digest.hex()}"
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, hash_name, iterations, salt_hex, digest_hex = stored.split("$")
        if algo != "pbkdf2" or hash_name != "sha256":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, int(iterations)
    )
    return hmac.compare_digest(actual, expected)


def validate_username(name: str) -> str | None:
    """返回错误信息；合法返回 None。"""
    if not name:
        return "用户名不能为空"
    if not USERNAME_RE.match(name):
        return "用户名需 3–32 位字母/数字/下划线/连字符"
    return None


def validate_password(pw: str) -> str | None:
    if not pw or len(pw) < PASSWORD_MIN:
        return f"密码至少 {PASSWORD_MIN} 位"
    return None


def random_password() -> str:
    """重置密码用的一次性随机密码（URL 安全，无需手动输入）。"""
    return secrets.token_urlsafe(12)


# —— 会话（签名 cookie） ——


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(SECRET_KEY, salt="imas-hub-session")


def make_session_token(uid: int, remember: bool) -> tuple[str, timedelta]:
    ttl = LONG_TTL if remember else SHORT_TTL
    token = _serializer().dumps({"uid": uid}, salt="imas-hub-session")
    return token, ttl


def _load_session_uid(token: str) -> int | None:
    """校验签名与过期（payload 内嵌签发时间 + 最长 TTL），解出 uid。"""
    try:
        data = _serializer().loads(token, max_age=int(LONG_TTL.total_seconds()))
        return int(data["uid"])
    except (BadSignature, SignatureExpired, KeyError, TypeError, ValueError):
        return None


# —— 请求辅助：当前用户 / 登录依赖 ——


def user_from_request(conn: sqlite3.Connection, request: Request) -> dict | None:
    """从 cookie 解析当前用户（含 active 检查）；未登录返回 None。"""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    uid = _load_session_uid(token)
    if uid is None:
        return None
    row = conn.execute(
        "SELECT id, username, role, active FROM user WHERE id=?", (uid,)
    ).fetchone()
    if not row or not row["active"]:
        return None
    return {"id": int(row["id"]), "username": row["username"], "role": row["role"]}


def require_login(request: Request) -> dict:
    """写接口依赖：未登录 401。"""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "请先登录")
    return user


def require_admin(request: Request) -> dict:
    """管理接口依赖：非 admin 403。"""
    user = require_login(request)
    if user["role"] != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user


def csrf_origin_ok(request: Request) -> bool:
    """写请求的 Origin/Referer（存在时）必须与 Host 同源；两者都缺（curl 等）放行。"""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return True
    host = request.headers.get("host", "").lower()
    for header in ("origin", "referer"):
        value = request.headers.get(header)
        if not value:
            continue
        netloc = urlparse(value).netloc.lower()
        if netloc != host:
            return False
    return True


# —— 邀请码 ——


def new_invite_code() -> str:
    return secrets.token_urlsafe(12)


def create_invite(
    conn: sqlite3.Connection, admin_id: int, days: int = 7
) -> tuple[str, str]:
    """生成一次性邀请码；返回 (code, expires_at)。"""
    days = max(1, min(int(days), 90))
    code = new_invite_code()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=days)
    conn.execute(
        """
        INSERT INTO invite(code, created_by, active, expires_at, created_at)
        VALUES (?, ?, 1, ?, ?)
        """,
        (code, admin_id, expires.replace(microsecond=0).isoformat(), utc_now()),
    )
    return code, expires.replace(microsecond=0).isoformat()


def check_invite(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    """有效邀请码（存在 / active=1 / 未用 / 未过期）返回行，否则 None。"""
    if not code:
        return None
    row = conn.execute(
        "SELECT * FROM invite WHERE code=? AND active=1", (code.strip(),)
    ).fetchone()
    if not row:
        return None
    if row["used_by"] is not None:
        return None
    if row["expires_at"] and row["expires_at"] < utc_now():
        return None
    return row


def consume_invite(
    conn: sqlite3.Connection, code: str, user_id: int
) -> None:
    conn.execute(
        """
        UPDATE invite SET active=0, used_by=?, used_at=?
        WHERE code=?
        """,
        (user_id, utc_now(), code.strip()),
    )


# —— 审计日志 ——


def record_audit(
    conn: sqlite3.Connection,
    user_id: int | None,
    action: str,
    entity: str,
    entity_id: int | None = None,
    detail: str | None = None,
) -> None:
    """写接口统一记录「谁改了什么」（同一事务内调用，随 commit 落库）。"""
    conn.execute(
        """
        INSERT INTO audit_log(user_id, action, entity, entity_id, detail, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user_id, action, entity, entity_id, detail, utc_now()),
    )
