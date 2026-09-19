"""Explicit privileged local operations and a separately authenticated cloud adapter.

Local grants are installed only by controlled provisioning, never by a login
name or a packaged universal password. Passwords are reset, not recoverable.
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
import time
from dataclasses import asdict
from datetime import date, datetime
from threading import RLock

from ..data.database import timestamp_to_db, utc_now
from ..security.passwords import clean_username, hash_password, normalize_username
from .auth import AuthService
from .developer_settings import DeveloperError, default_snapshot, safe_snapshot, validate_settings

SETTINGS_KEY = "developer.published.v1"
GRANT_KEY = "developer.grant.v1"
PENDING_KEY = "developer.pending.v1"
KEY_REVISION_KEY = "developer.key-revision.v1"
TABLE_LABELS = {
    "member_profiles": "会员个人档案",
    "advisor_profiles": "顾问个人档案",
    "user_preferences": "个人偏好",
    "profile_facts": "确认的个人事实",
    "life_records": "生活与医疗记录",
    "reminders": "提醒",
    "conversations": "AI 对话",
    "messages": "AI 聊天消息",
    "chat_attachments": "聊天附件元信息",
    "memberships": "会员状态",
    "staff_account_terms": "顾问账号期限",
    "advisor_bindings": "顾问与会员关系",
    "visit_tasks": "上门安排",
    "visit_task_details": "上门地点与服务",
    "visit_records": "上门记录",
    "medical_reports": "健康报告",
    "report_items": "报告指标",
    "user_files": "文件元信息",
    "ai_insights": "AI 建议",
    "advisor_summaries": "顾问总结",
    "orders": "虚拟订单",
    "member_cart": "购物车",
    "member_favorites": "收藏",
    "product_recommendations": "商品推荐",
}
# Table names/ownership predicates are program constants, never interpolated user SQL.
OWNERS = {
    **dict.fromkeys(
        (
            "member_profiles",
            "advisor_profiles",
            "user_preferences",
            "profile_facts",
            "life_records",
            "reminders",
            "conversations",
            "memberships",
            "staff_account_terms",
            "medical_reports",
            "user_files",
            "ai_insights",
            "member_cart",
            "member_favorites",
        ),
        "user_id = ?",
    ),
    **dict.fromkeys(
        (
            "advisor_bindings",
            "visit_tasks",
            "visit_records",
            "advisor_summaries",
            "product_recommendations",
        ),
        "member_user_id = ? OR advisor_user_id = ?",
    ),
    "orders": "member_user_id = ?",
    "messages": "conversation_id IN (SELECT id FROM conversations WHERE user_id = ?)",
    "chat_attachments": "message_id IN (SELECT m.id FROM messages m JOIN conversations c "
    "ON c.id = m.conversation_id WHERE c.user_id = ?)",
    "visit_task_details": "task_id IN (SELECT id FROM visit_tasks WHERE member_user_id = ? "
    "OR advisor_user_id = ?)",
    "report_items": "report_id IN (SELECT id FROM medical_reports WHERE user_id = ?)",
}
READ_ONLY_TABLES = {
    "chat_attachments",
    "user_files",
    "member_favorites",
    "orders",
    "product_recommendations",
    "advisor_bindings",
}
PROTECTED_COLUMNS = {
    "id",
    "user_id",
    "member_user_id",
    "advisor_user_id",
    "conversation_id",
    "message_id",
    "task_id",
    "report_id",
    "file_id",
    "product_id",
    "created_at",
    "updated_at",
    "requested_by",
    "sequence_no",
    "password_hash",
    "relative_path",
    "sha256",
}


def _setting(connection, key, user_id=None):
    return connection.execute(
        "SELECT id, value_json FROM app_settings WHERE setting_key = ? AND user_id IS ? "
        "ORDER BY id DESC LIMIT 1",
        (key, user_id),
    ).fetchone()


def _put_setting(connection, key, value, user_id=None):
    now = timestamp_to_db(utc_now())
    old = _setting(connection, key, user_id)
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if old:
        connection.execute(
            "UPDATE app_settings SET value_json = ?, updated_at = ? WHERE id = ?",
            (raw, now, old["id"]),
        )
    else:
        connection.execute(
            "INSERT INTO app_settings "
            "(user_id,setting_key,value_json,created_at,updated_at) VALUES (?,?,?,?,?)",
            (user_id, key, raw, now, now),
        )


def _safe_row(row):
    """Administrative data access still excludes authentication material."""

    def sanitize(value):
        if isinstance(value, dict):
            return {
                key: sanitize(item)
                for key, item in value.items()
                if not re.search(r"password|token|secret|api.?key|credential", str(key), re.I)
            }
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        return value

    result = {}
    for key, value in dict(row).items():
        if key in {"password_hash", "relative_path", "username_normalized"} or isinstance(
            value, bytes
        ):
            continue
        if key.endswith("_json") and isinstance(value, str):
            try:
                value = json.dumps(sanitize(json.loads(value)), ensure_ascii=False)
            except ValueError:
                value = "[无法解析的 JSON]"
        result[key] = value
    return result


def provision_local_developer(database, operator_user_id: int) -> None:
    """Trusted installation tool only; does not create/reset a password or run from UI."""
    database.initialize()
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT role_code, account_status FROM users WHERE id = ?", (operator_user_id,)
        ).fetchone()
        if row is None or row["role_code"] != "operator" or row["account_status"] != "active":
            raise DeveloperError("只能为已有的启用中管理者开通开发权限。")
        _put_setting(connection, GRANT_KEY, {"enabled": True}, operator_user_id)


class LaunchSecretStore:
    """Global defaults freeze per launch; explicit per-user replacements remain live."""

    def __init__(self, store):
        self._store = store
        self._defaults = {
            provider: store.get_default_api_key(provider)
            for provider in ("deepseek_cloud", "ollama_cloud")
        }

    def __getattr__(self, name):
        return getattr(self._store, name)

    def get_default_api_key(self, provider="deepseek_cloud"):
        return self._defaults.get(provider)

    def get_effective_api_key(self, user_id, provider="deepseek_cloud"):
        return (
            self._store.get_api_key(user_id, provider)
            or self._store.get_persistent_api_key(user_id, provider)
            or self.get_default_api_key(provider)
        )


class LocalDeveloperService:
    uses_network = False

    def __init__(self, database, secret_store):
        self.database, self.secret_store = database, secret_store
        self.auth = AuthService(database)
        self._activate_default_key_at_startup()
        self.launch_secret_store = LaunchSecretStore(secret_store)
        self._actor = None
        self._expires = 0.0
        self._failures = []
        self._lock = RLock()
        self._activate_pending_at_startup()
        self._startup = self._read_snapshot()

    def _activate_default_key_at_startup(self):
        with self.database.connect() as connection:
            row = _setting(connection, KEY_REVISION_KEY)
        if row:
            revision = json.loads(row["value_json"])
            if type(revision) is not int or revision < 1:
                raise DeveloperError("密钥配置版本不正确。")
            key = self.secret_store.get_persistent_api_key(
                f"developer-default-revision-{revision}", "deepseek_cloud"
            )
            if key:
                self.secret_store.set_default_api_key(key, "deepseek_cloud")

    def _activate_pending_at_startup(self):
        """Apply only unchanged baselines; conflicting edits remain visibly pending."""
        with self.database.transaction() as connection:
            queues = connection.execute(
                "SELECT user_id,value_json FROM app_settings WHERE setting_key = ?", (PENDING_KEY,)
            ).fetchall()
            for queue in queues:
                pending = json.loads(queue["value_json"])
                remaining = []
                for item in pending:
                    table = item["table"]
                    if table not in OWNERS or table in READ_ONLY_TABLES:
                        continue
                    identity = item["identity"]
                    if identity not in {"id", "user_id", "product_id", "task_id"}:
                        continue
                    predicate = OWNERS[table]
                    params = (item["row_id"],) + (queue["user_id"],) * predicate.count("?")
                    row = connection.execute(
                        f'SELECT * FROM "{table}" WHERE "{identity}" = ? AND ({predicate})',
                        params,
                    ).fetchone()
                    if row is None or dict(row) != item["baseline"]:
                        item["state"] = "conflict"
                        remaining.append(item)
                        continue
                    patch = item["changes"]
                    columns = set(row.keys())
                    if set(patch) - columns or set(patch) & PROTECTED_COLUMNS:
                        continue
                    if "updated_at" in columns:
                        patch = {**patch, "updated_at": timestamp_to_db(utc_now())}
                    try:
                        connection.execute(
                            f'UPDATE "{table}" SET '
                            + ",".join(f'"{key}" = ?' for key in patch)
                            + f' WHERE "{identity}" = ? AND ({predicate})',
                            (*patch.values(), *params),
                        )
                    except sqlite3.IntegrityError:
                        item["state"] = "conflict"
                        remaining.append(item)
                _put_setting(connection, PENDING_KEY, remaining, queue["user_id"])

    def _read_snapshot(self):
        with self.database.connect() as connection:
            row = _setting(connection, SETTINGS_KEY)
        return safe_snapshot(json.loads(row["value_json"])) if row else default_snapshot()

    def load_startup_snapshot(self):
        return copy.deepcopy(self._startup)

    @property
    def startup_snapshot(self):
        return self.load_startup_snapshot()

    def authenticate(self, username, password):
        with self._lock:
            self.close_session()
            now = time.monotonic()
            self._failures = [stamp for stamp in self._failures if now - stamp < 300]
            if len(self._failures) >= 5:
                raise DeveloperError("尝试次数过多，请 5 分钟后再试。")
            try:
                user = self.auth.authenticate(username, password)
                self._actor = user.id
                self._expires = now + 900
                self._require()
            except Exception:
                self.close_session()
                self._failures.append(now)
                raise DeveloperError("账号、密码或开发权限无效。") from None
            self._failures.clear()
            return asdict(user)

    def _require(self):
        if self._actor is None or time.monotonic() >= self._expires:
            self.close_session()
            raise DeveloperError("开发会话已到期，请重新验证身份。")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT role_code,account_status FROM users WHERE id = ?", (self._actor,)
            ).fetchone()
            grant = _setting(connection, GRANT_KEY, self._actor)
        if (
            row is None
            or row["role_code"] != "operator"
            or row["account_status"] != "active"
            or grant is None
            or json.loads(grant["value_json"]).get("enabled") is not True
        ):
            self.close_session()
            raise DeveloperError("当前账号没有开发权限。")

    def close_session(self):
        self._actor = None
        self._expires = 0.0

    def _audit(self, connection, action, user_id=None, fields=()):
        connection.execute(
            "INSERT INTO audit_logs"
            "(actor_user_id,action,entity_type,entity_id,details_json,created_at) "
            "VALUES(?,?,'developer',?,?,?)",
            (
                self._actor,
                action,
                user_id,
                json.dumps({"fields": list(fields)}),
                timestamp_to_db(utc_now()),
            ),
        )

    def list_users(self, role_code, offset=0, limit=100):
        self._require()
        if role_code not in {"member", "advisor", "operator"}:
            raise DeveloperError("用户类别不正确。")
        if not 0 <= offset <= 100000 or not 1 <= limit <= 200:
            raise DeveloperError("分页范围不正确。")
        with self.database.connect() as connection:
            return {
                "users": [
                    dict(row)
                    for row in connection.execute(
                        "SELECT id,username,role_code,account_status,created_at,updated_at,"
                        "last_login_at "
                        "FROM users WHERE role_code = ? ORDER BY id LIMIT ? OFFSET ?",
                        (role_code, limit, offset),
                    )
                ],
                "total": connection.execute(
                    "SELECT count(*) FROM users WHERE role_code = ?", (role_code,)
                ).fetchone()[0],
                "offset": offset,
            }

    def user_details(self, user_id):
        self._require()
        with self.database.connect() as connection:
            user = connection.execute(
                "SELECT id,username,role_code,account_status,created_at,updated_at,last_login_at "
                "FROM users WHERE id = ?",
                (int(user_id),),
            ).fetchone()
            if user is None:
                raise DeveloperError("用户不存在。")
            tables = {}
            for table, predicate in OWNERS.items():
                rows = connection.execute(
                    f'SELECT * FROM "{table}" WHERE {predicate}',
                    (int(user_id),) * predicate.count("?"),
                ).fetchall()
                tables[table] = [_safe_row(row) for row in rows]
            pending = _setting(connection, PENDING_KEY, int(user_id))
            return {
                "user": dict(user),
                "tables": tables,
                "pending_changes": [
                    {k: v for k, v in item.items() if k != "baseline"}
                    for item in json.loads(pending["value_json"])
                ]
                if pending
                else [],
            }

    def table_page(self, user_id, table, offset=0, limit=100):
        self._require()
        if table not in OWNERS or not 0 <= offset <= 100000 or not 1 <= limit <= 200:
            raise DeveloperError("分页范围不正确。")
        predicate = OWNERS[table]
        with self.database.connect() as connection:
            params = (int(user_id),) * predicate.count("?")
            columns = {row["name"] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            identity = "id" if "id" in columns else "user_id" if "user_id" in columns else "task_id"
            rows = connection.execute(
                f'SELECT * FROM "{table}" WHERE {predicate} ORDER BY "{identity}" LIMIT ? OFFSET ?',
                (*params, limit, offset),
            ).fetchall()
            total = connection.execute(
                f'SELECT count(*) FROM "{table}" WHERE {predicate}', params
            ).fetchone()[0]
        return {"records": [_safe_row(row) for row in rows], "total": total, "offset": offset}

    def update_user(self, user_id, changes):
        self._require()
        if (
            not isinstance(changes, dict)
            or not changes
            or set(changes) - {"username", "password", "account_status"}
        ):
            raise DeveloperError("只能调整账号名称、重置密码或停用状态。")
        patch = {}
        if "username" in changes:
            patch["username"] = clean_username(changes["username"])
            patch["username_normalized"] = normalize_username(changes["username"])
            if not patch["username"] or len(patch["username"]) > 64:
                raise DeveloperError("账号名称应为 1 至 64 个字符。")
        if "password" in changes:
            patch["password_hash"] = hash_password(changes["password"])
        if "account_status" in changes:
            if changes["account_status"] not in {"active", "disabled"}:
                raise DeveloperError("账号状态不正确。")
            if int(user_id) == self._actor and changes["account_status"] != "active":
                raise DeveloperError("不能停用本次操作的开发者账号。")
            patch["account_status"] = changes["account_status"]
        patch["updated_at"] = timestamp_to_db(utc_now())
        try:
            with self.database.transaction() as connection:
                result = connection.execute(
                    "UPDATE users SET "
                    + ",".join(f'"{name}" = ?' for name in patch)
                    + " WHERE id = ?",
                    (*patch.values(), int(user_id)),
                )
                if result.rowcount != 1:
                    raise DeveloperError("用户不存在。")
                self._audit(connection, "developer.user_updated", int(user_id), changes)
        except sqlite3.IntegrityError:
            raise DeveloperError("账号已存在或内容不满足数据约束。") from None
        return {"updated": True}

    def update_record(self, user_id, table, row_id, changes):
        self._require()
        if table not in OWNERS or table in READ_ONLY_TABLES:
            raise DeveloperError("该关联表只读，不能直接改写业务归属或交易信息。")
        if not isinstance(changes, dict) or not changes or set(changes) & PROTECTED_COLUMNS:
            raise DeveloperError("不能改写记录编号、所属用户、关联关系或系统时间。")
        raw = json.dumps(changes, ensure_ascii=False, allow_nan=False)
        if len(raw.encode()) > 128 * 1024:
            raise DeveloperError("单条修改不能超过 128 KB。")
        predicate = OWNERS[table]
        with self.database.transaction() as connection:
            columns = {row["name"] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            if set(changes) - columns:
                raise DeveloperError("记录包含未知字段。")
            for field, value in changes.items():
                if isinstance(value, (dict, list)):
                    raise DeveloperError("请将 JSON 字段填写为有效 JSON 文本。")
                if field.endswith("_json") and value is not None:
                    try:
                        json.loads(value)
                    except (TypeError, ValueError):
                        raise DeveloperError("JSON 字段格式不正确。") from None
                if value is not None and field.endswith("_at"):
                    try:
                        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                        if parsed.tzinfo is None:
                            raise ValueError
                    except ValueError:
                        raise DeveloperError("日期时间必须有效且带时区。") from None
                if value is not None and field in {
                    "birth_date",
                    "local_date",
                    "report_date",
                    "period_start",
                    "period_end",
                }:
                    try:
                        date.fromisoformat(str(value))
                    except ValueError:
                        raise DeveloperError("日期应为有效的 年-月-日。") from None
            identity = (
                "id"
                if "id" in columns
                else "product_id"
                if table == "member_cart"
                else "task_id"
                if table == "visit_task_details"
                else "user_id"
            )
            patch = dict(changes)
            if "updated_at" in columns:
                patch["updated_at"] = timestamp_to_db(utc_now())
            try:
                params = (int(row_id),) + (int(user_id),) * predicate.count("?")
                baseline = connection.execute(
                    f'SELECT * FROM "{table}" WHERE "{identity}" = ? AND ({predicate})',
                    params,
                ).fetchone()
                if baseline is None:
                    raise DeveloperError("记录不存在或不属于所选用户。")
                try:
                    if table == "user_preferences" and "preferences_json" in changes:
                        from .preferences import PreferencesService

                        PreferencesService.validate(json.loads(changes["preferences_json"]))
                    if table == "life_records":
                        from .health import HealthService

                        category = changes.get("category", baseline["category"])
                        details = changes.get("details_json", baseline["details_json"])
                        HealthService._validate_category(category)
                        HealthService._validate_record_details(
                            category, json.loads(details) if details else None
                        )
                except (ValueError, RuntimeError):
                    raise DeveloperError("记录参数不满足该板块的业务校验。") from None
                if table == "staff_account_terms":
                    connection.execute(
                        f'UPDATE "{table}" SET '
                        + ",".join(f'"{key}" = ?' for key in patch)
                        + f' WHERE "{identity}" = ? AND ({predicate})',
                        (*patch.values(), *params),
                    )
                    self._audit(connection, "developer.staff_term_updated", int(user_id), changes)
                    return {"updated": True, "state": "applied", "security_effect": "immediate"}
                connection.execute("SAVEPOINT developer_validate")
                result = connection.execute(
                    f'UPDATE "{table}" SET '
                    + ",".join(f'"{key}" = ?' for key in patch)
                    + f' WHERE "{identity}" = ? AND ({predicate})',
                    (*patch.values(), int(row_id), *((int(user_id),) * predicate.count("?"))),
                )
                if result.rowcount != 1:
                    raise DeveloperError("记录不存在或不属于所选用户。")
                connection.execute("ROLLBACK TO developer_validate")
                connection.execute("RELEASE developer_validate")
                old = _setting(connection, PENDING_KEY, int(user_id))
                pending = json.loads(old["value_json"]) if old else []
                previous = next(
                    (
                        item
                        for item in pending
                        if (item["table"], item["row_id"]) == (table, int(row_id))
                    ),
                    None,
                )
                combined = {**(previous["changes"] if previous else {}), **changes}
                pending = [
                    item
                    for item in pending
                    if (item["table"], item["row_id"]) != (table, int(row_id))
                ]
                if len(pending) >= 500:
                    raise DeveloperError("待生效修改过多，请先重启应用处理后再继续。")
                pending.append(
                    {
                        "table": table,
                        "row_id": int(row_id),
                        "identity": identity,
                        "changes": combined,
                        "baseline": dict(baseline),
                        "state": "pending",
                    }
                )
                _put_setting(connection, PENDING_KEY, pending, int(user_id))
                self._audit(
                    connection, "developer.record_staged", int(user_id), [table, *changes.keys()]
                )
            except sqlite3.IntegrityError:
                raise DeveloperError("修改不满足数据库约束，原记录已保留。") from None
        return {"state": "pending"}

    def settings(self):
        self._require()
        value = self._read_snapshot()
        value["api_key_configured"] = bool(self.secret_store.get_default_api_key("deepseek_cloud"))
        with self.database.connect() as connection:
            staged = _setting(connection, KEY_REVISION_KEY)
        if staged:
            key = self.secret_store.get_persistent_api_key(
                f"developer-default-revision-{json.loads(staged['value_json'])}", "deepseek_cloud"
            )
            value["api_key_configured"] = bool(key) or value["api_key_configured"]
        return value

    def publish_settings(self, expected_revision, settings, api_key=None):
        self._require()
        validated = validate_settings(settings)
        with self.database.transaction() as connection:
            row = _setting(connection, SETTINGS_KEY)
            current = safe_snapshot(json.loads(row["value_json"])) if row else default_snapshot()
            if type(expected_revision) is not int or current["revision"] != expected_revision:
                raise DeveloperError("设置已被另一位开发者修改，请重新读取后再保存。")
            if api_key is not None:
                if not isinstance(api_key, str) or not api_key.strip().startswith("sk-"):
                    raise DeveloperError("API Key 格式不正确；留空表示保持原有密钥。")
                revision = current["revision"] + 1
                self.secret_store.save_persistent_api_key(
                    f"developer-default-revision-{revision}", api_key, "deepseek_cloud"
                )
                _put_setting(connection, KEY_REVISION_KEY, revision)
            value = {"revision": current["revision"] + 1, "settings": validated}
            _put_setting(connection, SETTINGS_KEY, value)
            self._audit(connection, "developer.settings_published", fields=["settings"])
        return self.settings()

    def status(self):
        self._require()
        return {
            "backend": "本地模式",
            "database": "SQLite 本地数据库",
            "cloud_connected": False,
            "developer_schema": 1,
            "startup_revision": self._startup["revision"],
            "note": "此界面不执行远程终端、数据库连接串或云服务器系统变更。",
        }


class RemoteDeveloperService:
    uses_network = True

    def __init__(self, app_client, *, client_factory=None, secret_store=None):
        from .cloud_client import CloudAPIClient

        self.app_client = app_client
        self.client = (client_factory or CloudAPIClient)(app_client.base_url)
        self.launch_secret_store = LaunchSecretStore(secret_store) if secret_store else None
        self._startup = None
        self._active_identity = None
        self._trusted_user_snapshots = {}

    def load_startup_snapshot(self):
        user = getattr(self.app_client, "_user", None) or {}
        identity = (user.get("id"), getattr(self.app_client, "session_serial", 0))
        if identity != self._active_identity:
            received = safe_snapshot(self.app_client.request("POST", "/v1/client-start"))
            self._active_identity = identity
            self._startup = received
            self._trusted_user_snapshots[identity[0]] = copy.deepcopy(received)
            apply = getattr(self.app_client, "apply_session_settings", None)
            if callable(apply):
                apply(
                    received["revision"], received["settings"]["runtime"]["client_timeout_seconds"]
                )
        return copy.deepcopy(self._startup)

    def cached_snapshot_for_user(self, user_id):
        return copy.deepcopy(self._trusted_user_snapshots.get(user_id))

    @property
    def startup_snapshot(self):
        return copy.deepcopy(self._startup)

    def authenticate(self, username, password):
        return self.client.developer_login(username, password)

    def close_session(self):
        self.client.clear_session()

    def list_users(self, role_code, offset=0, limit=100):
        if role_code not in {"operator", "advisor", "member"}:
            raise DeveloperError("用户类别不正确。")
        return self.client.request(
            "GET",
            "/v1/developer/users",
            params={"role_code": role_code, "offset": offset, "limit": limit},
        )

    def user_details(self, user_id):
        return self.client.request("GET", f"/v1/developer/users/{int(user_id)}")

    def table_page(self, user_id, table, offset=0, limit=100):
        if table not in {*OWNERS, "order_items"}:
            raise DeveloperError("不支持的记录类别。")
        return self.client.request(
            "GET",
            f"/v1/developer/users/{int(user_id)}/records/{table}",
            params={"offset": offset, "limit": limit},
        )

    def update_user(self, user_id, changes):
        return self.client.request("PATCH", f"/v1/developer/users/{int(user_id)}", json=changes)

    def update_record(self, user_id, table, row_id, changes):
        if table not in OWNERS:
            raise DeveloperError("不支持的记录类别。")
        return self.client.request(
            "PATCH",
            f"/v1/developer/users/{int(user_id)}/records/{table}/{int(row_id)}",
            json={"changes": changes},
        )

    def settings(self):
        return self.client.request("GET", "/v1/developer/settings")

    def publish_settings(self, expected_revision, settings, api_key=None):
        payload = {"expected_revision": expected_revision, "settings": validate_settings(settings)}
        if api_key:
            payload["api_key"] = api_key
        return self.client.request("PUT", "/v1/developer/settings", json=payload)

    def status(self):
        return self.client.request("GET", "/v1/developer/status")
