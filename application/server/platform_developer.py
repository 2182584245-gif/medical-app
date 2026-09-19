"""Bounded privileged console; reviewed code owns every SQL identifier and action.

Credentials never appear in responses. Business edits are staged until the
target's next explicit client-start call; account security changes are immediate.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
from contextlib import nullcontext
from datetime import timedelta
from functools import wraps
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ollama_chat_app.data.database import timestamp_to_db, utc_now
from ollama_chat_app.security.passwords import (
    clean_username,
    normalize_username,
    validate_new_password,
)

from .platform_ai_proxy import AIProxyError, _private_file
from .platform_auth import USER_COLUMNS, PlatformAuthError
from .platform_experience_schema import BUSINESS_COLUMNS


class DeveloperError(RuntimeError):
    def __init__(self, status=422, code="开发者操作未完成，请检查输入。"):
        self.status, self.code = status, code
        super().__init__(code)


class _ValidationRollback(Exception):
    """A nested savepoint checks DB constraints without persisting the trial edit."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Skill(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    instructions: str = Field(max_length=6000)
    enabled: bool = True
    triggers: list[str] = Field(default_factory=list, max_length=20)


class Knowledge(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(max_length=12000)
    source: str = Field(default="", max_length=1000)
    enabled: bool = True
    reviewed: bool = False
    keywords: list[str] = Field(default_factory=list, max_length=20)


class AISettings(StrictModel):
    model: Literal["deepseek-v4-flash", "deepseek-v4-flash-vision-exp"] = "deepseek-v4-flash"
    complexity: Literal["simple", "standard", "detailed"] = "simple"
    tone: Literal["warm", "concise", "formal"] = "warm"
    enabled: bool = True
    max_tokens: int = Field(default=2048, ge=256, le=2048)
    system_prompt: str = Field(default="", max_length=12000)
    skills: list[Skill] = Field(default_factory=list, max_length=20)
    knowledge: list[Knowledge] = Field(default_factory=list, max_length=50)
    daily_tips: list[str] = Field(default_factory=list, max_length=20)


class Features(StrictModel):
    today: bool = True
    ai: bool = True
    statistics: bool = True
    services: bool = True
    commerce: bool = True
    appointments: bool = True


class RuntimeSettings(StrictModel):
    announcement: str = Field(default="", max_length=2000)
    support_contact: str = Field(default="", max_length=300)
    maintenance_message: str = Field(default="", max_length=2000)
    maintenance_enabled: bool = False
    client_timeout_seconds: int = Field(default=60, ge=30, le=120)


class ApplicationSettings(StrictModel):
    ai: AISettings = Field(default_factory=AISettings)
    features: Features = Field(default_factory=Features)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)


class PublishRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    settings: ApplicationSettings
    api_key: SecretStr | None = None


class LoginRequest(StrictModel):
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=1, max_length=1024)


class AccountPatch(StrictModel):
    username: str | None = Field(default=None, min_length=1, max_length=64)
    password: SecretStr | None = Field(default=None, min_length=8, max_length=1024)
    account_status: Literal["active", "disabled", "locked"] | None = None


class RecordPatch(StrictModel):
    changes: dict = Field(min_length=1, max_length=40)


def validate_settings(value):
    data = ApplicationSettings.model_validate(value).model_dump()
    for skill in data["ai"]["skills"]:
        if any(not isinstance(v, str) or len(v) > 100 for v in skill["triggers"]):
            raise DeveloperError()
    for item in data["ai"]["knowledge"]:
        if any(not isinstance(v, str) or len(v) > 100 for v in item["keywords"]):
            raise DeveloperError()
    if any(len(v) > 500 for v in data["ai"]["daily_tips"]):
        raise DeveloperError()
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode()) > 262144:
        raise DeveloperError(413, "设置内容过多，请缩小专业资料或技能内容。")
    return data


def _fingerprint(row):
    raw = json.dumps(dict(row), ensure_ascii=False, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()


def _safe_json(value):
    """Never return a nested credential accidentally imported into preferences."""
    if isinstance(value, dict):
        return {
            key: _safe_json(item)
            for key, item in value.items()
            if not re.search(r"password|token|secret|api.?key|credential", str(key), re.I)
        }
    if isinstance(value, list):
        return [_safe_json(item) for item in value]
    return value


# All ownership/identifiers are constants. No request can choose a SQL clause.
EXCLUDED_TABLES = {
    "users",
    "app_settings",
    "audit_logs",
    "user_file_contents",
    "products",
    "product_events",
}
TABLES = {
    name: cols
    for name, cols in BUSINESS_COLUMNS.items()
    if name not in EXCLUDED_TABLES
    and (
        "user_id" in cols
        or "member_user_id" in cols
        or "advisor_user_id" in cols
        or name
        in {"messages", "report_items", "order_items", "visit_task_details", "chat_attachments"}
    )
}


def ownership(table):
    columns = TABLES[table]
    names = [name for name in ("user_id", "member_user_id", "advisor_user_id") if name in columns]
    if names:
        return "(" + " OR ".join(f"{name} = ?" for name in names) + ")", len(names)
    if table == "chat_attachments":
        return (
            "message_id IN (SELECT m.id FROM messages m JOIN conversations c "
            "ON c.id=m.conversation_id WHERE c.user_id = ?)",
            1,
        )
    foreign = {
        "messages": ("conversation_id", "conversations", "user_id"),
        "report_items": ("report_id", "medical_reports", "user_id"),
        "order_items": ("order_id", "orders", "user_id"),
        "visit_task_details": ("task_id", "visit_tasks", "member_user_id"),
    }[table]
    fk, parent, owner = foreign
    return f"{fk} IN (SELECT id FROM {parent} WHERE {owner} = ?)", 1


def _safe_row(row):
    result = {}
    for key in dict(row):
        if key in {"relative_path", "password_hash", "username_normalized"}:
            continue
        value = row[key]
        if isinstance(value, bytes):
            continue
        if key.endswith("_json") and isinstance(value, str):
            try:
                value = json.dumps(_safe_json(json.loads(value)), ensure_ascii=False)
            except ValueError:
                value = "{}"
        result[key] = value
    return result


def editable_columns(table):
    if table in {
        "user_files",
        "order_items",
        "orders",
        "chat_attachments",
        "member_cart",
        "member_favorites",
    }:
        return set()
    return {
        name
        for name in TABLES[table]
        if name
        not in {
            "id",
            "created_at",
            "updated_at",
            "relative_path",
            "sha256",
            "size_bytes",
            "requested_by",
        }
        and not name.endswith("_id")
    }


def ordering(table):
    columns = TABLES[table]
    return ",".join(columns[:2]) if table in {"member_cart", "member_favorites"} else columns[0]


class DeveloperService:
    def __init__(self, auth, *, key_path="", legacy_key_configured=False):
        self.auth, self.database = auth, auth.database
        self.key_path = os.environ.get("HEALTHLIFE_DEVELOPER_KEY_FILE", "") or key_path
        self.legacy_key_configured = legacy_key_configured

    def identity(self, principal, *, developer=False, activation=False):
        identity = getattr(self.database, "identity", None)
        return (
            identity(
                user_id=principal.user.id,
                role_code=principal.user.role_code,
                token_digest=principal.token_digest,
                developer=developer,
                developer_activation=activation,
            )
            if identity
            else nullcontext()
        )

    def ready(self):
        try:
            with self.database.connect() as connection:
                connection.execute("SELECT user_id FROM developer_grants LIMIT 0")
                connection.execute("SELECT token_digest FROM developer_sessions LIMIT 0")
                connection.execute("SELECT revision FROM developer_settings_snapshots LIMIT 0")
                connection.execute("SELECT id FROM developer_pending_changes LIMIT 0")
                connection.execute("SELECT revision FROM developer_client_sessions LIMIT 0")
            return True
        except sqlite3.Error:
            return False

    def require_ready(self):
        if not self.ready():
            raise DeveloperError(503, "云端开发者结构尚未升级，请管理员完成专用迁移。")

    def login(self, username, password, client_ip):
        self.require_ready()
        session = self.auth.login(username, password, client_ip=client_ip)
        principal = self.auth.resolve_token(session["access_token"])
        with self.auth.identity(principal), self.database.transaction() as connection:
            grant = connection.execute(
                "SELECT enabled FROM developer_grants WHERE user_id = ?", (principal.user.id,)
            ).fetchone()
            if principal.user.role_code != "operator" or grant is None or grant[0] != 1:
                connection.execute(
                    "UPDATE platform_sessions SET revoked_at = ? WHERE token_digest = ?",
                    (timestamp_to_db(utc_now()), principal.token_digest),
                )
            else:
                expires_at = timestamp_to_db(utc_now() + timedelta(minutes=15))
                connection.execute(
                    "INSERT INTO developer_sessions (token_digest,user_id,expires_at) "
                    "VALUES (?,?,?)",
                    (principal.token_digest, principal.user.id, expires_at),
                )
                return {**session, "developer": True, "expires_at": expires_at}
        raise DeveloperError(403, "此账号未获开发者授权。")

    def authorize(self, request, *, developer=True):
        header = request.headers.get("authorization", "")
        if not re.fullmatch(r"Bearer [A-Za-z0-9_-]{43}", header):
            raise DeveloperError(401, "请重新登录。")
        principal = self.auth.resolve_token(header[7:])
        if developer:
            self.require_ready()
            with self.auth.identity(principal), self.database.connect() as connection:
                grant = connection.execute(
                    "SELECT enabled FROM developer_grants WHERE user_id = ?", (principal.user.id,)
                ).fetchone()
                step = connection.execute(
                    "SELECT expires_at FROM developer_sessions "
                    "WHERE token_digest = ? AND user_id = ?",
                    (principal.token_digest, principal.user.id),
                ).fetchone()
            if (
                principal.user.role_code != "operator"
                or not grant
                or grant[0] != 1
                or not step
                or step[0] <= timestamp_to_db(utc_now())
            ):
                raise DeveloperError(403, "开发者授权已失效，请重新验证。")
        return principal

    def snapshot(self, principal, revision=None):
        if not self.ready():
            return {
                "revision": 0,
                "settings": ApplicationSettings().model_dump(),
                "api_key_configured": self.legacy_key_configured,
            }
        with self.auth.identity(principal), self.database.connect() as connection:
            if revision is None:
                row = connection.execute(
                    "SELECT revision,settings_json,encrypted_key FROM developer_settings_snapshots "
                    "ORDER BY revision DESC LIMIT 1"
                ).fetchone()
            elif revision == 0:
                row = None
            else:
                row = connection.execute(
                    "SELECT revision,settings_json,encrypted_key FROM developer_settings_snapshots "
                    "WHERE revision = ?",
                    (revision,),
                ).fetchone()
                if not row:
                    raise DeveloperError(409, "设置版本不存在，请重新打开应用。")
        return {
            "revision": int(row[0]) if row else 0,
            "settings": validate_settings(json.loads(row[1]))
            if row
            else ApplicationSettings().model_dump(),
            "api_key_configured": bool(row and row[2]) or self.legacy_key_configured,
        }

    def _crypt_key(self):
        try:
            raw = _private_file(self.key_path, 32)
            if len(raw) != 32:
                raise ValueError
            return raw
        except Exception:
            raise DeveloperError(503, "服务器未配置独立的 API Key 加密密钥文件。") from None

    def publish(self, principal, value):
        data = validate_settings(value.settings.model_dump())
        secret = value.api_key.get_secret_value() if value.api_key is not None else None
        if secret is not None and not re.fullmatch(r"[A-Za-z0-9_-]{16,512}", secret):
            raise DeveloperError(422, "API Key 格式无效。")
        key = self._crypt_key() if secret else None
        with self.identity(principal, developer=True), self.database.transaction() as connection:
            old = connection.execute(
                "SELECT revision,encrypted_key FROM developer_settings_snapshots "
                "ORDER BY revision DESC LIMIT 1"
            ).fetchone()
            revision = int(old[0]) if old else 0
            if revision != value.expected_revision:
                raise DeveloperError(409, "设置已被其他开发者更新，请重新读取后再修改。")
            encrypted = old[1] if old else None
            if secret:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM

                nonce = os.urandom(12)
                encrypted = base64.b64encode(
                    nonce
                    + AESGCM(key).encrypt(nonce, secret.encode(), b"medical-app-developer-api-v1")
                ).decode()
            connection.execute(
                "INSERT INTO developer_settings_snapshots "
                "(revision,settings_json,encrypted_key,created_at,created_by) VALUES (?,?,?,?,?)",
                (
                    revision + 1,
                    json.dumps(data, ensure_ascii=False),
                    encrypted,
                    timestamp_to_db(utc_now()),
                    principal.user.id,
                ),
            )
            self._audit(
                connection,
                principal,
                "developer.settings.published",
                None,
                {"revision": revision + 1, "key_changed": secret is not None},
            )
        return self.snapshot(principal, revision + 1)

    def key_for_revision(self, principal, revision):
        if revision == 0:
            return None
        with self.auth.identity(principal), self.database.connect() as connection:
            row = connection.execute(
                "SELECT encrypted_key FROM developer_settings_snapshots WHERE revision = ?",
                (revision,),
            ).fetchone()
        if not row or not row[0]:
            return None
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            raw = base64.b64decode(row[0], validate=True)
            return (
                AESGCM(self._crypt_key())
                .decrypt(raw[:12], raw[12:], b"medical-app-developer-api-v1")
                .decode()
            )
        except Exception:
            raise AIProxyError() from None

    @staticmethod
    def _audit(connection, principal, action, entity_id, details):
        connection.execute(
            "INSERT INTO audit_logs "
            "(actor_user_id,action,entity_type,entity_id,details_json,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                principal.user.id,
                action,
                "developer_console",
                entity_id,
                json.dumps(details, ensure_ascii=False),
                timestamp_to_db(utc_now()),
            ),
        )

    def users(self, principal, role_code, offset=0, limit=100):
        if (
            role_code not in {"member", "advisor", "operator"}
            or not 0 <= offset <= 100000
            or not 1 <= limit <= 200
        ):
            raise DeveloperError()
        with self.identity(principal, developer=True), self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {USER_COLUMNS} FROM users WHERE role_code = ? "
                "ORDER BY id LIMIT ? OFFSET ?",
                (role_code, limit, offset),
            ).fetchall()
            total = connection.execute(
                "SELECT count(*) FROM users WHERE role_code = ?", (role_code,)
            ).fetchone()[0]
        return {"users": [_safe_row(row) for row in rows], "total": total, "offset": offset}

    def detail(self, principal, user_id):
        with self.identity(principal, developer=True), self.database.connect() as connection:
            user = connection.execute(
                f"SELECT {USER_COLUMNS} FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if not user:
                raise DeveloperError(404, "未找到该用户。")
            tables, truncated = {}, []
            for name in TABLES:
                condition, count = ownership(name)
                rows = connection.execute(
                    f"SELECT * FROM {name} WHERE {condition} ORDER BY {ordering(name)} LIMIT 501",
                    (user_id,) * count,
                ).fetchall()
                tables[name] = [_safe_row(row) for row in rows[:500]]
                if len(rows) > 500:
                    truncated.append(name)
            pending = connection.execute(
                "SELECT id,table_name,row_id,changes_json,state,created_at "
                "FROM developer_pending_changes "
                "WHERE target_user_id = ? AND state <> 'applied' ORDER BY id LIMIT 500",
                (user_id,),
            ).fetchall()
        return {
            "user": _safe_row(user),
            "tables": tables,
            "editable_fields": {name: sorted(editable_columns(name)) for name in TABLES},
            "truncated_tables": truncated,
            "pending_changes": [_safe_row(row) for row in pending],
        }

    def table_page(self, principal, user_id, table, offset=0, limit=100):
        if table not in TABLES or not 0 <= offset <= 100000 or not 1 <= limit <= 200:
            raise DeveloperError()
        condition, count = ownership(table)
        with self.identity(principal, developer=True), self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM {table} WHERE {condition} "
                f"ORDER BY {ordering(table)} LIMIT ? OFFSET ?",
                (*((user_id,) * count), limit, offset),
            ).fetchall()
            total = connection.execute(
                f"SELECT count(*) FROM {table} WHERE {condition}", (user_id,) * count
            ).fetchone()[0]
        return {
            "records": [_safe_row(row) for row in rows],
            "total": int(total),
            "offset": offset,
            "editable_fields": sorted(editable_columns(table)),
        }

    def account_patch(self, principal, user_id, value):
        changes = {}
        if value.username is not None:
            name = clean_username(value.username)
            if not name:
                raise DeveloperError()
            changes.update(username=name, username_normalized=normalize_username(name))
        if value.password is not None:
            password = value.password.get_secret_value()
            validate_new_password(password)
            changes["password_hash"] = self.auth.security.hash_password(password)
        if value.account_status is not None:
            if user_id == principal.user.id and value.account_status != "active":
                raise DeveloperError(409, "不能在当前会话中停用自己。")
            changes["account_status"] = value.account_status
        if not changes:
            raise DeveloperError()
        now = timestamp_to_db(utc_now())
        with self.identity(principal, developer=True), self.database.transaction() as connection:
            if not connection.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone():
                raise DeveloperError(404, "未找到该用户。")
            columns = list(changes)
            connection.execute(
                "UPDATE users SET "
                + ",".join(f"{name} = ?" for name in columns)
                + ",updated_at = ? WHERE id = ?",
                (*changes.values(), now, user_id),
            )
            # Sensitive account changes are immediately effective, unlike staged
            # profile/content changes. Revoke every old target session.
            connection.execute(
                "UPDATE platform_sessions SET revoked_at = ? "
                "WHERE user_id = ? AND revoked_at IS NULL",
                (now, user_id),
            )
            self._audit(
                connection,
                principal,
                "developer.account.updated",
                user_id,
                {"fields": ["password" if k == "password_hash" else k for k in columns]},
            )
        return {"updated": True, "security_effect": "immediate", "sessions_revoked": True}

    def _record(self, connection, user_id, table, row_id):
        condition, count = ownership(table)
        pk = TABLES[table][0]
        return connection.execute(
            f"SELECT * FROM {table} WHERE {pk} = ? AND {condition}",
            (row_id, *((user_id,) * count)),
        ).fetchone()

    def stage_record(self, principal, user_id, table, row_id, value):
        if table not in TABLES or not editable_columns(table):
            raise DeveloperError(422, "该表只允许查看，不能直接修改。")
        changes = value.changes
        if not set(changes) <= editable_columns(table):
            raise DeveloperError(422, "不能更改记录归属、身份、文件路径或系统字段。")
        for name, item in changes.items():
            if (
                not isinstance(item, (str, int, float, bool, type(None)))
                or isinstance(item, str)
                and len(item) > 64000
            ):
                raise DeveloperError()
            if name.endswith("_json") and item is not None:
                try:
                    parsed = json.loads(item)
                    if _safe_json(parsed) != parsed:
                        raise ValueError
                except (ValueError, TypeError):
                    raise DeveloperError(
                        422, "结构化字段必须是有效 JSON，且不能包含密钥或口令。"
                    ) from None
        if len(json.dumps(changes, ensure_ascii=False).encode()) > 65536:
            raise DeveloperError(413, "单条修改内容过大。")
        with self.identity(principal, developer=True), self.database.transaction() as connection:
            row = self._record(connection, user_id, table, row_id)
            if not row:
                raise DeveloperError(404, "没有找到属于该用户的记录。")
            # Validate database constraints now, not on the user's next launch.
            # The inner transaction is always rolled back; no trial data becomes
            # visible to live sync readers or durable audit records.
            try:
                with self.database.transaction() as trial:
                    pk = TABLES[table][0]
                    condition, count = ownership(table)
                    trial.execute(
                        f"UPDATE {table} SET "
                        + ",".join(f"{name} = ?" for name in changes)
                        + f" WHERE {pk} = ? AND {condition}",
                        (*changes.values(), row_id, *((user_id,) * count)),
                    )
                    raise _ValidationRollback
            except _ValidationRollback:
                pass
            if table == "staff_account_terms":
                # An expired advisor cannot sign in to activate a queued term
                # extension. Validity belongs to immediate account security,
                # alongside password resets and account activation/lockout.
                now = timestamp_to_db(utc_now())
                connection.execute(
                    "UPDATE staff_account_terms SET "
                    + ",".join(f"{name} = ?" for name in changes)
                    + ",updated_at = ? WHERE user_id = ?",
                    (*changes.values(), now, user_id),
                )
                connection.execute(
                    "UPDATE platform_sessions SET revoked_at = ? "
                    "WHERE user_id = ? AND revoked_at IS NULL",
                    (now, user_id),
                )
                self._audit(
                    connection,
                    principal,
                    "developer.account.validity_updated",
                    user_id,
                    {"fields": sorted(changes), "security_effect": "immediate"},
                )
                return {
                    "updated": True,
                    "state": "applied",
                    "security_effect": "immediate",
                    "sessions_revoked": True,
                    "applies": "immediate",
                }
            existing = connection.execute(
                "SELECT id FROM developer_pending_changes WHERE target_user_id = ? "
                "AND table_name = ? AND row_id = ? AND state = 'pending'",
                (user_id, table, row_id),
            ).fetchone()
            if existing:
                raise DeveloperError(409, "该记录已有等待下次启动应用时生效的修改。")
            cursor = connection.execute(
                "INSERT INTO developer_pending_changes "
                "(target_user_id,table_name,row_id,changes_json,base_fingerprint,state,"
                "created_by,created_at) "
                "VALUES (?,?,?,?,?,'pending',?,?) RETURNING id",
                (
                    user_id,
                    table,
                    row_id,
                    json.dumps(changes, ensure_ascii=False),
                    _fingerprint(row),
                    principal.user.id,
                    timestamp_to_db(utc_now()),
                ),
            )
            pending_id = int(cursor.fetchone()[0])
            self._audit(
                connection,
                principal,
                "developer.record.staged",
                user_id,
                {"table": table, "row_id": row_id, "fields": sorted(changes)},
            )
        return {"pending_id": pending_id, "state": "pending", "applies": "next_app_start"}

    def client_start(self, principal):
        activation = {"applied": 0, "conflicts": 0}
        if self.ready():
            with (
                self.identity(principal, activation=True),
                self.database.transaction() as connection,
            ):
                pending = connection.execute(
                    "SELECT id,table_name,row_id,changes_json,base_fingerprint "
                    "FROM developer_pending_changes "
                    "WHERE target_user_id = ? AND state = 'pending' ORDER BY id LIMIT 100",
                    (principal.user.id,),
                ).fetchall()
                for item in pending:
                    table = item["table_name"]
                    row = (
                        self._record(connection, principal.user.id, table, item["row_id"])
                        if table in TABLES
                        else None
                    )
                    changes = json.loads(item["changes_json"])
                    if (
                        row
                        and _fingerprint(row) == item["base_fingerprint"]
                        and set(changes) <= editable_columns(table)
                    ):
                        now = timestamp_to_db(utc_now())
                        if "updated_at" in TABLES[table]:
                            changes["updated_at"] = now
                        pk = TABLES[table][0]
                        condition, count = ownership(table)
                        try:
                            with self.database.transaction() as trial:
                                trial.execute(
                                    f"UPDATE {table} SET "
                                    + ",".join(f"{name} = ?" for name in changes)
                                    + f" WHERE {pk} = ? AND {condition}",
                                    (
                                        *changes.values(),
                                        item["row_id"],
                                        *((principal.user.id,) * count),
                                    ),
                                )
                            state = "applied"
                            activation["applied"] += 1
                        except sqlite3.Error:
                            state = "conflict"
                            activation["conflicts"] += 1
                    else:
                        state = "conflict"
                        activation["conflicts"] += 1
                    connection.execute(
                        "UPDATE developer_pending_changes SET state = ? WHERE id = ?",
                        (state, item["id"]),
                    )
        snapshot = self.snapshot(principal)
        if self.ready():
            with self.auth.identity(principal), self.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO developer_client_sessions "
                    "(token_digest,user_id,revision,captured_at) VALUES (?,?,?,?) "
                    "ON CONFLICT(token_digest) DO UPDATE SET "
                    "revision=excluded.revision,captured_at=excluded.captured_at",
                    (
                        principal.token_digest,
                        principal.user.id,
                        snapshot["revision"],
                        timestamp_to_db(utc_now()),
                    ),
                )
        return {**snapshot, "activation": activation}

    def capture_for_ai(self, principal, claimed_revision=None):
        """Never trust a client-selected historical revision or a missing header."""
        if not self.ready():
            if claimed_revision not in (None, 0):
                raise DeveloperError(409, "设置版本尚未就绪。")
            return self.snapshot(principal, 0)
        with self.auth.identity(principal), self.database.transaction() as connection:
            captured = connection.execute(
                "SELECT revision FROM developer_client_sessions "
                "WHERE token_digest = ? AND user_id = ?",
                (principal.token_digest, principal.user.id),
            ).fetchone()
            if captured is None:
                snapshot = self.snapshot(principal)
                revision = snapshot["revision"]
                connection.execute(
                    "INSERT INTO developer_client_sessions "
                    "(token_digest,user_id,revision,captured_at) VALUES (?,?,?,?)",
                    (
                        principal.token_digest,
                        principal.user.id,
                        revision,
                        timestamp_to_db(utc_now()),
                    ),
                )
            else:
                revision = int(captured[0])
                snapshot = self.snapshot(principal, revision)
            if claimed_revision is not None and claimed_revision != revision:
                raise DeveloperError(409, "会话设置版本不一致，请重新打开应用。")
        return snapshot

    def enforce_operation(self, principal, service, method, arguments):
        """Enforce captured service switches before execution or idempotent replay."""
        from .platform_rpc import RpcError

        if principal.user.role_code == "operator":
            return  # Operators retain maintenance/recovery access.
        configuration = self.capture_for_ai(principal)["settings"]
        if configuration["runtime"]["maintenance_enabled"]:
            raise RpcError(503, "application_maintenance")
        if principal.user.role_code != "member":
            return
        if service == "sync" and method == "apply_queued":
            intent = arguments.get("intent")
            if not isinstance(intent, dict):
                raise RpcError(422, "invalid_offline_intent")
            service, method = intent.get("service"), intent.get("method")
        switches = configuration["features"]
        required = []
        if service in {"ai", "chat"}:
            required.append("ai")
            if not configuration["ai"]["enabled"]:
                raise RpcError(403, "feature_disabled_ai")
        if service == "commerce":
            required.extend(("services", "commerce"))
        if service == "service_management":
            required.append("services")
            if method in {"request_appointment", "list_appointments"}:
                required.append("appointments")
        if service == "health":
            if method in {
                "add_life_record",
                "delete_life_record",
                "delete_life_records",
                "add_reminder",
                "update_reminder",
                "delete_reminder",
                "delete_reminders",
                "pause_reminder",
                "resume_reminder",
                "toggle_reminder",
            }:
                required.append("today")
            if method == "get_record_statistics":
                required.append("statistics")
            if method == "get_service_summary":
                required.append("services")
        if service == "sync" and method == "import_member":
            required.extend(("today", "ai", "services", "commerce", "appointments"))
        for feature in required:
            if not switches[feature]:
                # Explicit failure leaves the local outbox queued; never silently
                # drop a member's offline intent or report a successful upload.
                raise RpcError(403, "feature_disabled_" + feature)


def create_developer_router(service):
    router = APIRouter()

    def guarded(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except DeveloperError as error:
                return JSONResponse({"detail": error.code}, error.status)
            except PlatformAuthError as error:
                return JSONResponse({"detail": str(error)}, error.status_code)
            except sqlite3.IntegrityError:
                return JSONResponse({"detail": "数据约束冲突，未完成操作。"}, 409)
            except Exception:
                return JSONResponse(
                    {"detail": "开发者服务暂不可用，原始异常和敏感数据已隐藏。"}, 503
                )

        return wrapped

    @router.post("/v1/developer/login")
    @guarded
    def login(value: LoginRequest, request: Request):
        return service.login(
            value.username,
            value.password.get_secret_value(),
            request.client.host if request.client else "unknown",
        )

    @router.get("/v1/client-settings")
    @guarded
    def client_settings(request: Request):
        return service.snapshot(service.authorize(request, developer=False))

    @router.post("/v1/client-start")
    @guarded
    def client_start(request: Request):
        return service.client_start(service.authorize(request, developer=False))

    @router.get("/v1/developer/settings")
    @guarded
    def get_settings(request: Request):
        return service.snapshot(service.authorize(request))

    @router.put("/v1/developer/settings")
    @guarded
    def publish(value: PublishRequest, request: Request):
        return service.publish(service.authorize(request), value)

    @router.get("/v1/developer/users")
    @guarded
    def users(request: Request, role_code: str = "member", offset: int = 0, limit: int = 100):
        return service.users(service.authorize(request), role_code, offset, limit)

    @router.get("/v1/developer/users/{user_id}")
    @guarded
    def detail(user_id: int, request: Request):
        return service.detail(service.authorize(request), user_id)

    @router.patch("/v1/developer/users/{user_id}")
    @guarded
    def patch_account(user_id: int, value: AccountPatch, request: Request):
        return service.account_patch(service.authorize(request), user_id, value)

    @router.patch("/v1/developer/users/{user_id}/records/{table}/{row_id}")
    @guarded
    def patch_record(user_id: int, table: str, row_id: int, value: RecordPatch, request: Request):
        return service.stage_record(service.authorize(request), user_id, table, row_id, value)

    @router.get("/v1/developer/users/{user_id}/records/{table}")
    @guarded
    def table_page(user_id: int, table: str, request: Request, offset: int = 0, limit: int = 100):
        return service.table_page(service.authorize(request), user_id, table, offset, limit)

    @router.get("/v1/developer/status")
    @router.get("/v1/developer/server-status")
    @guarded
    def status(request: Request):
        principal = service.authorize(request)
        from ollama_chat_app.config import APP_VERSION

        with service.auth.identity(principal), service.database.connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return {
            "backend": "aliyun",
            "database": service.database.dialect,
            "status": "ready",
            "app_version": APP_VERSION,
            "server_utc": timestamp_to_db(utc_now()),
            "developer_schema": "platform_0004",
            "settings_revision": service.snapshot(principal)["revision"],
            "key_configured": service.snapshot(principal)["api_key_configured"],
            "server_control": "safe_application_settings_only",
            "https_required": service.auth.settings.require_https,
        }

    return router
