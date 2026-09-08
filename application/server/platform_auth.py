"""Opaque-session authentication for the desktop-compatible platform API.

The application role comes only from the current database record. This module
has no startup side effects, public bootstrap route, cloud configuration loader,
or dependency on Qt. End-user data is never exposed via a raw SQL endpoint.
"""

from __future__ import annotations

import hmac
import re
import sqlite3
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import timedelta
from functools import wraps

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ollama_chat_app.data.database import User, timestamp_to_db, user_from_row, utc_now
from ollama_chat_app.security.passwords import (
    PasswordPolicyError,
    normalize_username,
    validate_new_password,
)
from ollama_chat_app.services.auth import AuthService, RegistrationError

from .platform_accounts import advisor_term_valid
from .platform_experience_schema import BUSINESS_TABLES
from .platform_leases import make_offline_lease
from .platform_security import (
    PLATFORM_RUNTIME_ROLE,
    PLATFORM_SCHEMA,
    PlatformAuthSettings,
    PlatformAuthUnavailable,
    SharedAuthLimiter,
    make_security,
)
from .security import AuthCapacityError

USER_COLUMNS = (
    "id, username, username_normalized, created_at, last_login_at, "
    "role_code, account_status, updated_at"
)


class PlatformAuthError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class _RollbackProbe(Exception):
    """Internal control flow: never retain a readiness counter mutation."""


def _safe_operation(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except PlatformAuthError:
            raise
        except AuthCapacityError:
            raise PlatformAuthError(429, "认证操作过于频繁，请稍后再试。") from None
        except (RegistrationError, PasswordPolicyError):
            raise PlatformAuthError(422, "账号或密码不符合要求，或账号已存在。") from None
        except sqlite3.IntegrityError:
            raise PlatformAuthError(409, "账号操作未完成，请检查是否重复注册。") from None
        except Exception:
            raise PlatformAuthError(503, "认证服务暂不可用，请稍后再试。") from None

    return call


@dataclass(frozen=True)
class PlatformPrincipal:
    user: User
    token_digest: str


def user_view(user: User) -> dict:
    """The complete desktop User dataclass, without a password/hash/session secret."""
    return jsonable_encoder(asdict(user))


class PlatformAuth:
    def __init__(self, database, settings: PlatformAuthSettings):
        self.database = database
        self.settings = settings
        self.postgres = getattr(database, "dialect", "sqlite") == "postgresql"
        if settings.production and (not self.postgres or not hasattr(database, "identity")):
            raise PlatformAuthUnavailable("生产认证必须使用有身份隔离的 PostgreSQL 平台数据库。")
        self.security = make_security(settings)
        self.limiter = SharedAuthLimiter(database, settings)

    def _context(self, **kwargs):
        identity = getattr(self.database, "identity", None)
        return identity(**kwargs) if identity else nullcontext()

    def identity(self, principal: PlatformPrincipal):
        """Wrap all business-service calls in the server-validated identity."""
        return self._context(
            user_id=principal.user.id,
            role_code=principal.user.role_code,
            token_digest=principal.token_digest,
        )

    def _lock_suffix(self) -> str:
        return " FOR UPDATE" if self.postgres else ""

    @_safe_operation
    def register(self, username: str, password: str, *, client_ip: str) -> User:
        normalized = normalize_username(username)
        self.limiter.check(client_ip, normalized)
        with self.security.hash_slot():
            display, normalized, password_hash = AuthService._prepare_new_account(
                username, password
            )
        with (
            self._context(auth_username=normalized, registering=True),
            self.database.transaction() as connection,
        ):
            # Existing service helper enforces member + one default conversation.
            row = AuthService._insert_account(
                connection,
                display_username=display,
                normalized_username=normalized,
                password_hash=password_hash,
                role_code="member",
                now=timestamp_to_db(utc_now()),
            )
        return user_from_row(row)

    @_safe_operation
    def login(self, username: str, password: str, *, client_ip: str) -> dict:
        normalized = normalize_username(username)
        self.limiter.check(client_ip, normalized)
        with self._context(auth_username=normalized), self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {USER_COLUMNS}, password_hash FROM users WHERE username_normalized = ?",
                (normalized,),
            ).fetchone()
        stored_hash = self.security.dummy_hash if row is None else str(row["password_hash"])
        valid = self.security.verify_password(password, stored_hash)
        if not valid or row is None or row["account_status"] != "active":
            raise PlatformAuthError(401, "用户名或密码错误")
        user_id = int(row["id"])
        role = str(row["role_code"])
        replacement_hash = (
            self.security.hash_password(password)
            if self.security.hasher.check_needs_rehash(stored_hash)
            else stored_hash
        )
        token, digest = self.security.issue_token()
        now = utc_now()
        expiry = now + timedelta(seconds=self.settings.token_ttl_seconds)
        with (
            self._context(user_id=user_id, role_code=role),
            self.database.transaction() as connection,
        ):
            current = connection.execute(
                "SELECT password_hash, account_status, role_code FROM users WHERE id = ?"
                + self._lock_suffix(),
                (user_id,),
            ).fetchone()
            # Password changes and login races serialize on the same user row.
            if (
                current is None
                or current["account_status"] != "active"
                or not hmac.compare_digest(str(current["password_hash"]), stored_hash)
                or current["role_code"] != role
            ):
                raise PlatformAuthError(401, "用户名或密码错误")
            if not advisor_term_valid(connection, user_id, role):
                raise PlatformAuthError(401, "顾问账号尚未生效或已到期，请联系运营人员。")
            connection.execute(
                "UPDATE users SET password_hash = ?, last_login_at = ?, updated_at = ? "
                "WHERE id = ?",
                (replacement_hash, timestamp_to_db(now), timestamp_to_db(now), user_id),
            )
            connection.execute(
                "INSERT INTO platform_sessions "
                "(token_digest, user_id, created_at, expires_at, revoked_at) "
                "VALUES (?, ?, ?, ?, NULL)",
                (digest, user_id, timestamp_to_db(now), timestamp_to_db(expiry)),
            )
            updated = connection.execute(
                f"SELECT {USER_COLUMNS} FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            offline_lease = make_offline_lease(
                connection, user_id, role, now, expiry,
                self.settings.token_pepper.get_secret_value(),
            )
        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_at": timestamp_to_db(expiry),
            "user": user_view(user_from_row(updated)),
            "offline_lease": offline_lease,
            "sync_protocol": 2,
        }

    @_safe_operation
    def resolve_token(self, token: str) -> PlatformPrincipal:
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise PlatformAuthError(401, "需要重新登录")
        digest = self.security.token_digest(token)
        now = timestamp_to_db(utc_now())
        with self._context(token_digest=digest), self.database.connect() as connection:
            session = connection.execute(
                "SELECT user_id FROM platform_sessions WHERE token_digest = ? "
                "AND revoked_at IS NULL AND expires_at > ?",
                (digest, now),
            ).fetchone()
        if session is None:
            raise PlatformAuthError(401, "需要重新登录")
        with (
            self._context(user_id=int(session["user_id"]), token_digest=digest),
            self.database.connect() as connection,
        ):
            row = connection.execute(
                f"SELECT {USER_COLUMNS} FROM users WHERE id = ?",
                (int(session["user_id"]),),
            ).fetchone()
            if row is not None and not advisor_term_valid(
                connection, int(row["id"]), str(row["role_code"])
            ):
                raise PlatformAuthError(401, "顾问账号尚未生效或已到期，请联系运营人员。")
        if row is None or row["account_status"] != "active":
            raise PlatformAuthError(401, "需要重新登录")
        user = user_from_row(row)
        if user.role_code not in {"member", "advisor", "operator"}:
            raise PlatformAuthError(401, "需要重新登录")
        return PlatformPrincipal(user=user, token_digest=digest)

    @_safe_operation
    def logout(self, principal: PlatformPrincipal) -> None:
        with self.identity(principal), self.database.transaction() as connection:
            connection.execute(
                "UPDATE platform_sessions SET revoked_at = ? WHERE token_digest = ? "
                "AND user_id = ? AND revoked_at IS NULL",
                (timestamp_to_db(utc_now()), principal.token_digest, principal.user.id),
            )

    @_safe_operation
    def change_password(
        self,
        principal: PlatformPrincipal,
        current_password: str,
        new_password: str,
        *,
        client_ip: str,
    ) -> None:
        self.limiter.check(client_ip, principal.user.username_normalized)
        validate_new_password(new_password)
        with self.identity(principal), self.database.connect() as connection:
            row = connection.execute(
                "SELECT password_hash FROM users WHERE id = ?",
                (principal.user.id,),
            ).fetchone()
        stored_hash = self.security.dummy_hash if row is None else str(row["password_hash"])
        if not self.security.verify_password(current_password, stored_hash) or row is None:
            raise PlatformAuthError(401, "当前密码不正确")
        new_hash = self.security.hash_password(new_password)
        now = timestamp_to_db(utc_now())
        with self.identity(principal), self.database.transaction() as connection:
            current = connection.execute(
                "SELECT password_hash, account_status, role_code FROM users WHERE id = ?"
                + self._lock_suffix(),
                (principal.user.id,),
            ).fetchone()
            session = connection.execute(
                "SELECT user_id FROM platform_sessions WHERE token_digest = ? "
                "AND user_id = ? AND revoked_at IS NULL AND expires_at > ?",
                (principal.token_digest, principal.user.id, now),
            ).fetchone()
            if (
                current is None
                or current["account_status"] != "active"
                or not advisor_term_valid(connection, principal.user.id, str(current["role_code"]))
                or session is None
                or not hmac.compare_digest(str(current["password_hash"]), stored_hash)
            ):
                raise PlatformAuthError(401, "账号状态已变化，请重新登录")
            connection.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (new_hash, now, principal.user.id),
            )
            connection.execute(
                "UPDATE platform_sessions SET revoked_at = ? WHERE user_id = ? "
                "AND token_digest <> ? AND revoked_at IS NULL",
                (now, principal.user.id, principal.token_digest),
            )

    @_safe_operation
    def check_ready(self) -> dict:
        """Actual database security checks, not an unchecked environment flag."""
        if not self.postgres:
            if self.settings.production:
                raise PlatformAuthUnavailable("生产环境拒绝 SQLite。")
            with self.database.connect() as connection:
                connection.execute(
                    "SELECT token_digest, user_id, expires_at FROM platform_sessions LIMIT 0"
                )
                connection.execute(
                    "SELECT bucket_id, window_start, attempts FROM auth_rate_buckets LIMIT 0"
                )
            return {"status": "ready", "backend": "sqlite-test", "production_verified": False}
        with self._context():
            with self.database.connect() as connection:
                role = connection.execute(
                    "SELECT current_user, r.rolsuper, r.rolcreatedb, r.rolcreaterole, "
                    "r.rolbypassrls, r.rolreplication, EXISTS "
                    "(SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member=r.oid) "
                    "FROM pg_catalog.pg_roles r WHERE r.rolname=current_user"
                ).fetchone()
                if tuple(role[index] for index in range(7)) != (
                    PLATFORM_RUNTIME_ROLE,
                    False,
                    False,
                    False,
                    False,
                    False,
                    False,
                ):
                    raise PlatformAuthUnavailable("后端没有使用经过限制的专用运行角色。")
                privileges = connection.execute(
                    "SELECT has_schema_privilege(current_user, ?, 'USAGE'), "
                    "has_schema_privilege(current_user, ?, 'CREATE')",
                    (PLATFORM_SCHEMA, PLATFORM_SCHEMA),
                ).fetchone()
                if (privileges[0], privileges[1]) != (True, False):
                    raise PlatformAuthUnavailable("平台结构权限不符合要求。")
                tables = connection.execute(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
                    "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
                    "ON n.oid=c.relnamespace "
                    "WHERE n.nspname=? AND c.relkind='r'",
                    (PLATFORM_SCHEMA,),
                ).fetchall()
                names = {row[0] for row in tables}
                required = set(BUSINESS_TABLES) | {"platform_sessions", "auth_rate_buckets"}
                if not required <= names or any(not row[1] or not row[2] for row in tables):
                    raise PlatformAuthUnavailable("平台业务表或数据库行级隔离尚未就绪。")
                for table in ("platform_sessions", "auth_rate_buckets"):
                    for privilege in ("SELECT", "INSERT", "UPDATE"):
                        if not connection.execute(
                            "SELECT has_table_privilege(current_user, ?, ?)",
                            (f"{PLATFORM_SCHEMA}.{table}", privilege),
                        ).fetchone()[0]:
                            raise PlatformAuthUnavailable("共享认证表权限缺失。")
                if not connection.execute(
                    "SELECT has_table_privilege(current_user, ?, 'DELETE')",
                    (f"{PLATFORM_SCHEMA}.auth_rate_buckets",),
                ).fetchone()[0]:
                    raise PlatformAuthUnavailable("共享限流的过期清理权限缺失。")
            try:
                with self.database.transaction() as connection:
                    connection.execute("SET LOCAL statement_timeout = '5000ms'")
                    connection.execute("SET LOCAL lock_timeout = '3000ms'")
                    connection.execute("SELECT pg_catalog.pg_advisory_xact_lock(717380022, 1)")
                    probe = connection.execute(
                        "INSERT INTO auth_rate_buckets (bucket_id, window_start, attempts) "
                        "VALUES (0, 0, 0) ON CONFLICT (bucket_id) DO UPDATE SET "
                        "attempts = auth_rate_buckets.attempts RETURNING attempts"
                    ).fetchone()
                    if probe is None:
                        raise PlatformAuthUnavailable("共享限流写入验收失败。")
                    raise _RollbackProbe()
            except _RollbackProbe:
                pass
        return {
            "status": "ready",
            "backend": "postgresql",
            "production_verified": True,
            "runtime_role": PLATFORM_RUNTIME_ROLE,
            "rls_tables": len(tables),
            "shared_auth_limits": True,
        }


@_safe_operation
def bootstrap_operator(
    database, settings: PlatformAuthSettings, username: str, password: str, *, confirm: str
) -> User:
    """Local administrator-only operation; NEVER expose through HTTP or RPC.

    Credentials are supplied in memory by an administrator-side hidden prompt.
    Nothing is overwritten: an existing operator OR username rejects the operation.
    """
    if confirm != PLATFORM_SCHEMA:
        raise PlatformAuthError(400, "必须显式确认平台结构名称，尚未创建管理员。")
    postgres = getattr(database, "dialect", "sqlite") == "postgresql"
    if settings.production and not postgres:
        raise PlatformAuthError(400, "生产管理员初始化必须使用 PostgreSQL 管理连接。")
    security = make_security(settings)
    with security.hash_slot():
        display, normalized, password_hash = AuthService._prepare_new_account(username, password)
    with database.transaction() as connection:
        if postgres:
            connection.execute("SET LOCAL statement_timeout = '5000ms'")
            connection.execute("SET LOCAL lock_timeout = '3000ms'")
            connection.execute("SELECT pg_catalog.pg_advisory_xact_lock(717380022, 2)")
            row = connection.execute("SELECT current_user").fetchone()
            if row[0] != "postgres":
                raise PlatformAuthError(403, "管理员初始化仅允许本机专用管理连接执行。")
        existing = connection.execute(
            "SELECT 1 FROM users WHERE role_code = 'operator' OR username_normalized = ? LIMIT 1",
            (normalized,),
        ).fetchone()
        if existing is not None:
            raise PlatformAuthError(409, "管理员或同名账号已存在；没有覆盖或修改。")
        now = timestamp_to_db(utc_now())
        row = AuthService._insert_account(
            connection,
            display_username=display,
            normalized_username=normalized,
            password_hash=password_hash,
            role_code="operator",
            now=now,
        )
        AuthService._add_account_audit_log(
            connection,
            actor_user_id=int(row["id"]),
            action="operator.bootstrapped",
            entity_id=int(row["id"]),
            role_code="operator",
            now=now,
        )
    return user_from_row(row)


class PlatformCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=1, max_length=1024)


class PlatformPasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    current_password: SecretStr = Field(min_length=1, max_length=1024)
    new_password: SecretStr = Field(min_length=8, max_length=1024)


class SafeAuthRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError as error:
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": [
                            {"loc": item["loc"], "msg": item["msg"], "type": item["type"]}
                            for item in error.errors()
                        ]
                    },
                )

        return handler


def _http_call(function: Callable, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except PlatformAuthError as error:
        headers = {"WWW-Authenticate": "Bearer"} if error.status_code == 401 else None
        if error.status_code == 429:
            headers = {"Retry-After": "60"}
        raise HTTPException(error.status_code, str(error), headers=headers) from None


def authentication_dependency(auth: PlatformAuth):
    def authenticated(request: Request) -> PlatformPrincipal:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.casefold() != "bearer":
            raise HTTPException(401, "需要重新登录", headers={"WWW-Authenticate": "Bearer"})
        return _http_call(auth.resolve_token, token)

    return authenticated


def create_auth_router(auth: PlatformAuth) -> APIRouter:
    router = APIRouter(prefix="/v1/auth", tags=["authentication"], route_class=SafeAuthRoute)
    authenticated = authentication_dependency(auth)
    principal_dependency = Depends(authenticated)

    def client_ip(request: Request) -> str:
        # Never trust caller-supplied X-Forwarded-For here. Configure trusted
        # reverse proxies in the ASGI server, not in application request data.
        return request.client.host if request.client else "unknown"

    @router.post("/register", status_code=201)
    def register(payload: PlatformCredentials, request: Request):
        return user_view(
            _http_call(
                auth.register,
                payload.username,
                payload.password.get_secret_value(),
                client_ip=client_ip(request),
            )
        )

    @router.post("/login")
    def login(payload: PlatformCredentials, request: Request):
        return _http_call(
            auth.login,
            payload.username,
            payload.password.get_secret_value(),
            client_ip=client_ip(request),
        )

    @router.get("/me")
    def me(principal: PlatformPrincipal = principal_dependency):
        return user_view(principal.user)

    @router.post("/logout", status_code=204)
    def logout(principal: PlatformPrincipal = principal_dependency):
        _http_call(auth.logout, principal)
        return Response(status_code=204)

    @router.post("/change-password", status_code=204)
    def change_password(
        payload: PlatformPasswordChange,
        request: Request,
        principal: PlatformPrincipal = principal_dependency,
    ):
        _http_call(
            auth.change_password,
            principal,
            payload.current_password.get_secret_value(),
            payload.new_password.get_secret_value(),
            client_ip=client_ip(request),
        )
        return Response(status_code=204)

    return router
