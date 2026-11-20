from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .utils import json_dumps, utc_now

if TYPE_CHECKING:
    from .database import Database


SESSION_COOKIE = "pilot_session"
SESSION_SECONDS = 12 * 60 * 60
USERNAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{1,31}$")
ROLES = {"admin", "operator"}


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _password_hash(password: str, salt: str) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 240_000)
    return digest.hex()


def _validate_password(password: str) -> None:
    if not 8 <= len(password) <= 128:
        raise HTTPException(422, "密码长度必须为 8~128 位")


class AuthService:
    def __init__(self, database: Database):
        self.db = database

    def initialize(self) -> None:
        now = utc_now()
        defaults = [
            ("admin", "系统管理员", "admin", os.getenv("TESTSYSTEM_ADMIN_PASSWORD", "admin123")),
            ("operator", "生产操作工", "operator", os.getenv("TESTSYSTEM_OPERATOR_PASSWORD", "operator123")),
        ]
        with self.db.transaction() as connection:
            for username, display_name, role, password in defaults:
                salt = secrets.token_hex(16)
                connection.execute(
                    "INSERT OR IGNORE INTO users(username, display_name, role, password_salt, password_hash, "
                    "enabled, session_version, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?)",
                    (username, display_name, role, salt, _password_hash(password, salt), now, now),
                )

    def _secret(self) -> bytes:
        secret = self.db.setting("auth_secret")
        if not secret:
            secret = secrets.token_urlsafe(48)
            self.db.save_setting("auth_secret", secret)
        return str(secret).encode("utf-8")

    def _public_user(self, row: Any) -> dict[str, Any]:
        return {
            "username": row["username"],
            "display_name": row["display_name"],
            "role": row["role"],
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM users WHERE username = ?", (username.strip(),))
        if row is None or not row["enabled"]:
            return None
        candidate = _password_hash(password, row["password_salt"])
        if not hmac.compare_digest(candidate, row["password_hash"]):
            return None
        return self._public_user(row)

    def issue_token(self, username: str) -> str:
        row = self.db.one("SELECT * FROM users WHERE username = ? AND enabled = 1", (username,))
        if row is None:
            raise HTTPException(401, "账号不存在或已停用")
        payload = {
            "sub": username,
            "sv": row["session_version"],
            "iat": int(time.time()),
            "exp": int(time.time()) + SESSION_SECONDS,
        }
        encoded = _b64encode(json_dumps(payload).encode("utf-8"))
        signature = _b64encode(hmac.new(self._secret(), encoded.encode("ascii"), hashlib.sha256).digest())
        return f"{encoded}.{signature}"

    def user_from_token(self, token: str | None) -> dict[str, Any] | None:
        if not token or token.count(".") != 1:
            return None
        try:
            encoded, signature = token.split(".", 1)
            expected = _b64encode(hmac.new(self._secret(), encoded.encode("ascii"), hashlib.sha256).digest())
            if not hmac.compare_digest(signature, expected):
                return None
            payload = json.loads(_b64decode(encoded))
            if int(payload["exp"]) <= int(time.time()):
                return None
            row = self.db.one("SELECT * FROM users WHERE username = ? AND enabled = 1", (payload["sub"],))
            if row is None or int(payload["sv"]) != row["session_version"]:
                return None
            return self._public_user(row)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None

    def list_users(self) -> list[dict[str, Any]]:
        return [self._public_user(row) for row in self.db.all("SELECT * FROM users ORDER BY role, username")]

    def create_user(self, username: str, display_name: str, role: str, password: str) -> dict[str, Any]:
        username = username.strip()
        display_name = display_name.strip()
        if not USERNAME_PATTERN.fullmatch(username):
            raise HTTPException(422, "用户名需以字母开头，2~32 位，仅含字母、数字、点、下划线或连字符")
        if not display_name or len(display_name) > 64:
            raise HTTPException(422, "显示名称不能为空且不能超过 64 个字符")
        if role not in ROLES:
            raise HTTPException(422, "角色仅支持 admin 或 operator")
        _validate_password(password)
        if self.db.one("SELECT 1 FROM users WHERE username = ?", (username,)):
            raise HTTPException(409, "用户名已存在")
        salt = secrets.token_hex(16)
        now = utc_now()
        self.db.execute(
            "INSERT INTO users(username, display_name, role, password_salt, password_hash, enabled, "
            "session_version, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?)",
            (username, display_name, role, salt, _password_hash(password, salt), now, now),
        )
        row = self.db.one("SELECT * FROM users WHERE username = ?", (username,))
        return self._public_user(row)

    def set_enabled(self, username: str, enabled: bool, current_username: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM users WHERE username = ?", (username,))
        if row is None:
            raise HTTPException(404, "用户不存在")
        if username == current_username and not enabled:
            raise HTTPException(422, "不能停用当前登录账号")
        if row["role"] == "admin" and not enabled:
            admins = self.db.one("SELECT COUNT(*) AS count FROM users WHERE role = 'admin' AND enabled = 1")
            if admins["count"] <= 1:
                raise HTTPException(422, "至少需要保留一个可用的管理员账号")
        self.db.execute(
            "UPDATE users SET enabled = ?, session_version = session_version + 1, updated_at = ? WHERE username = ?",
            (int(enabled), utc_now(), username),
        )
        return self._public_user(self.db.one("SELECT * FROM users WHERE username = ?", (username,)))

    def reset_password(self, username: str, new_password: str) -> None:
        if self.db.one("SELECT 1 FROM users WHERE username = ?", (username,)) is None:
            raise HTTPException(404, "用户不存在")
        _validate_password(new_password)
        self._update_password(username, new_password)

    def change_password(self, username: str, current_password: str, new_password: str) -> None:
        if self.authenticate(username, current_password) is None:
            raise HTTPException(422, "当前密码不正确")
        _validate_password(new_password)
        self._update_password(username, new_password)

    def _update_password(self, username: str, password: str) -> None:
        salt = secrets.token_hex(16)
        self.db.execute(
            "UPDATE users SET password_salt = ?, password_hash = ?, session_version = session_version + 1, "
            "updated_at = ? WHERE username = ?",
            (salt, _password_hash(password, salt), utc_now(), username),
        )


class LoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class UserPayload(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    display_name: str = Field(min_length=1, max_length=64)
    role: str
    password: str = Field(min_length=8, max_length=128)


class EnabledPayload(BaseModel):
    enabled: bool


class PasswordPayload(BaseModel):
    password: str = Field(min_length=8, max_length=128)


class ChangePasswordPayload(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


def create_auth_router(service: AuthService) -> APIRouter:
    router = APIRouter(prefix="/api/auth", tags=["认证与权限"])

    def set_session(response: Response, username: str) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            service.issue_token(username),
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=os.getenv("TESTSYSTEM_SECURE_COOKIE", "false").lower() == "true",
            samesite="strict",
            path="/",
        )

    @router.post("/login")
    def login(payload: LoginPayload, response: Response) -> dict[str, Any]:
        user = service.authenticate(payload.username, payload.password)
        if user is None:
            raise HTTPException(401, "用户名或密码错误，或账号已停用")
        set_session(response, user["username"])
        return {"user": user}

    @router.post("/logout")
    def logout(response: Response) -> dict[str, str]:
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"message": "已退出登录"}

    @router.get("/me")
    def me(request: Request) -> dict[str, Any]:
        return {"user": request.state.user}

    @router.post("/password")
    def change_password(payload: ChangePasswordPayload, request: Request, response: Response) -> dict[str, str]:
        username = request.state.user["username"]
        service.change_password(username, payload.current_password, payload.new_password)
        set_session(response, username)
        return {"message": "密码已更新"}

    @router.get("/users")
    def users() -> list[dict[str, Any]]:
        return service.list_users()

    @router.post("/users", status_code=201)
    def create_user(payload: UserPayload) -> dict[str, Any]:
        return service.create_user(payload.username, payload.display_name, payload.role, payload.password)

    @router.put("/users/{username}/enabled")
    def set_enabled(username: str, payload: EnabledPayload, request: Request) -> dict[str, Any]:
        return service.set_enabled(username, payload.enabled, request.state.user["username"])

    @router.put("/users/{username}/password")
    def reset_password(username: str, payload: PasswordPayload) -> dict[str, str]:
        service.reset_password(username, payload.password)
        return {"message": "密码已重置，该用户需要重新登录"}

    return router
