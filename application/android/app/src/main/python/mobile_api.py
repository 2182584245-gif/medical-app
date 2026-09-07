"""Authenticated, allow-listed Android facade over the shared desktop services.

Only dispatch is exposed to the offline WebView. Native-only file functions take
paths supplied by the Android system picker, never by untrusted web content.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import sqlite3
import tempfile
import threading
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from mobile_providers import MODEL_NOTICE, MobileProvider, _model, validate_local_url

from ollama_chat_app.config import DEEPSEEK_DEFAULT_MODEL
from ollama_chat_app.data.database import SCHEMA_VERSION, Database, timestamp_to_db, utc_now
from ollama_chat_app.providers.base import ProviderError
from ollama_chat_app.services.ai_assistant import AiAssistantError, AiAssistantService
from ollama_chat_app.services.auth import AuthError, AuthService
from ollama_chat_app.services.backup import BackupError, PortableBackupService
from ollama_chat_app.services.chat import ChatError, ChatService
from ollama_chat_app.services.commerce import CommerceError, CommerceService
from ollama_chat_app.services.file_management import (
    MAX_FILE_SIZE,
    FileManagementError,
    FileManagementService,
)
from ollama_chat_app.services.health import HealthService, HealthServiceError
from ollama_chat_app.services.service_management import (
    ServiceManagementError,
    ServiceManagementService,
)
from ollama_chat_app.time_utils import as_beijing, beijing_now

_LOCK = threading.RLock()
_APP: MobileApplication | None = None
_SAFE_ERRORS = (
    AuthError,
    HealthServiceError,
    ServiceManagementError,
    CommerceError,
    FileManagementError,
    AiAssistantError,
    BackupError,
    ChatError,
    ProviderError,
)
_METHODS = {
    "health": [
        "get_dashboard",
        "list_life_records",
        "add_life_record",
        "get_record_statistics",
        "delete_life_record",
        "get_profile",
        "save_profile",
        "list_reminders",
        "add_reminder",
        "update_reminder",
        "pause_reminder",
        "resume_reminder",
        "toggle_reminder",
        "delete_reminder",
        "get_service_summary",
    ],
    "management": [
        "get_dashboard",
        "create_advisor",
        "list_members",
        "get_member_overview",
        "list_advisors",
        "get_advisor_work_statistics",
        "save_advisor_profile",
        "save_membership",
        "bind_advisor",
        "create_visit_task",
        "start_visit_task",
        "complete_visit_task",
        "list_visit_tasks",
        "list_visit_records",
        "set_account_enabled",
    ],
    "commerce": [
        "list_products",
        "create_product",
        "update_product",
        "set_product_active",
        "delete_product",
        "recommend_product",
        "list_recommendations",
        "record_product_view",
        "mark_interested",
        "mark_product_interested",
        "simulate_purchase",
        "reorder",
        "list_orders",
        "mark_order_delivered",
    ],
    "files": [
        "list_files",
        "delete_file",
        "extract_pdf_text",
        "extract_text",
        "create_report_draft",
        "add_report_item_draft",
        "confirm_report_item",
        "confirm_report",
        "archive_report",
        "delete_report",
        "list_reports",
        "get_report",
        "storage_usage",
    ],
    "ai": [
        "get_ai_config",
        "update_ai_config",
        "build_member_context",
        "list_drafts",
        "get_draft",
        "confirm_proposal",
        "dismiss_proposal",
    ],
}
_AUTH_PARAMS = {"user_id", "actor_user_id", "operator_user_id"}
_DATETIME_FIELDS = {
    "occurred_at",
    "scheduled_at",
    "created_at",
    "updated_at",
    "last_login_at",
    "starts_at",
    "ends_at",
    "started_at",
    "visited_at",
    "next_visit_at",
    "effective_at",
    "generated_at",
    "completed_at",
    "imported_at",
    "beijing_now",
}


class MobileInputError(ValueError):
    pass


def _serialize(value: Any, field: str = "") -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _serialize(asdict(value))
    if isinstance(value, datetime):
        return as_beijing(value).isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {
            str(key): _serialize(item, str(key))
            for key, item in value.items()
            if key not in {"password_hash", "api_key"}
        }
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    if (
        isinstance(value, str)
        and field in _DATETIME_FIELDS
        and re.match(r"^\d{4}-\d{2}-\d{2}[T ]", value)
    ):
        try:
            return as_beijing(value).isoformat(timespec="seconds")
        except ValueError:
            return value
    if isinstance(value, bytes):
        raise MobileInputError("文件内容只能通过系统文件操作读取")
    return value


def _json(value: Any) -> str:
    return json.dumps(_serialize(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _safe_error(error: Exception) -> str:
    if isinstance(error, (*_SAFE_ERRORS, MobileInputError)):
        return str(error)[:1200]
    if isinstance(error, sqlite3.Error):
        return "本地数据库操作失败，请关闭应用后重新打开；请保留原数据并导出备份，不要卸载应用。"
    if isinstance(error, OSError):
        return "文件操作失败，请检查可用存储空间及所选文件权限。"
    if isinstance(error, (ValueError, TypeError, KeyError, OverflowError)):
        return "输入内容格式不正确，请检查填写的字段、数字及日期时间。"
    return "操作未完成，请重试；如果持续发生，请先导出备份并联系开发者。"


def _params_allowed(params: dict[str, Any], names: set[str]) -> None:
    if set(params) - names:
        raise MobileInputError("请求包含不支持的字段")


class MobileApplication:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        os.environ["OLLAMA_DUAL_CHAT_DATA_DIR"] = str(self.data_dir)
        self.database = Database(self.data_dir / "app.db")
        self.auth = AuthService(self.database)
        self.health = HealthService(self.database)
        self.management = ServiceManagementService(self.database)
        self.commerce = CommerceService(self.database)
        self.files = FileManagementService(self.database, ocr_engine=self._native_ocr)
        self.ai = AiAssistantService(self.database, self.health)
        self.chat = ChatService(self.database)
        self.backup = PortableBackupService(self.database)
        self.user_id: int | None = None
        self.keys: dict[str, str] = {}
        self.provider = "ollama_cloud"
        self.models: dict[str, str] = {
            "deepseek": DEEPSEEK_DEFAULT_MODEL,
            "ollama_cloud": "",
            "local": "",
        }
        self.local_url = "http://127.0.0.1:11434"
        self.ocr_cache: dict[tuple[int, int], str] = {}
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE messages SET status='error', error_message=?, updated_at=? "
                "WHERE role='assistant' AND status='pending'",
                ("上次请求因应用关闭而中断，可重新发送。", timestamp_to_db(utc_now())),
            )

    def _user(self, roles: set[str] | None = None):
        user = self.auth.get_user(self.user_id) if self.user_id is not None else None
        if user is None or user.account_status != "active":
            self.logout()
            raise MobileInputError("请先登录；账号可能已停用或数据已更换")
        if roles is not None and user.role_code not in roles:
            raise MobileInputError("当前账号没有此操作权限")
        return user

    def logout(self) -> None:
        self.user_id = None
        self.keys.clear()
        self.ocr_cache.clear()
        self.provider = "ollama_cloud"
        self.models = {"deepseek": DEEPSEEK_DEFAULT_MODEL, "ollama_cloud": "", "local": ""}
        self.local_url = "http://127.0.0.1:11434"

    def _migration_allowed(self, *, importing: bool = False) -> bool:
        with self.database.connect() as connection:
            count = int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        if importing and count == 0:
            return True
        user = self.auth.get_user(self.user_id) if self.user_id is not None else None
        if not user or user.account_status != "active":
            return False
        if self.auth.has_operator():
            return user.role_code == "operator"
        return count == 1

    def state(self) -> dict[str, Any]:
        user = self.auth.get_user(self.user_id) if self.user_id is not None else None
        if user and user.account_status != "active":
            self.logout()
            user = None
        return {
            "user": user,
            "has_operator": self.auth.has_operator(),
            "beijing_now": beijing_now(),
            "can_backup": self._migration_allowed(),
            "can_import": self._migration_allowed(importing=True),
        }

    def provider_status(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.models[self.provider],
            "local_url": self.local_url,
            "has_api_key": bool(self.keys.get(self.provider)),
            "supports_images": self.provider != "deepseek",
            "notice": MODEL_NOTICE
            if self.provider == "ollama_cloud"
            else (
                "DeepSeek 按官网规则计费；密钥仅本次登录有效。"
                if self.provider == "deepseek"
                else "不自动检测；手机 localhost 指手机自身，电脑 Ollama 请填写同一局域网 IP。"
            ),
        }

    def configure(self, params: dict[str, Any]) -> dict[str, Any]:
        _params_allowed(params, {"provider", "api_key", "model", "local_url"})
        provider = params.get("provider", self.provider)
        if provider not in {"deepseek", "ollama_cloud", "local"}:
            raise MobileInputError("请选择支持的 AI 服务")
        local_url = validate_local_url(params.get("local_url", self.local_url))
        key = params.get("api_key", self.keys.get(provider, ""))
        if (
            not isinstance(key, str)
            or len(key) > 4096
            or (
                key.strip() and (not key.strip().isascii() or any(ord(c) < 33 for c in key.strip()))
            )
        ):
            raise MobileInputError("API Key 格式无效，请完整复制且不要带空格或换行")
        model = params.get("model", self.models[provider])
        if not isinstance(model, str):
            raise MobileInputError("模型名称格式无效")
        if provider == "deepseek":
            if model and model != DEEPSEEK_DEFAULT_MODEL:
                raise MobileInputError("DeepSeek 仅支持 deepseek-v4-flash")
            model = DEEPSEEK_DEFAULT_MODEL
        elif model:
            model = _model(model)
        self.provider, self.local_url = provider, local_url
        self.models[provider] = model
        if provider != "local":
            self.keys[provider] = key.strip()
        return self.provider_status()

    def _provider(self) -> MobileProvider:
        return MobileProvider(self.provider, self.keys.get(self.provider, ""), self.local_url)

    def _provider_and_model(self):
        provider = self._provider()
        model = self.models[self.provider]
        if not model:
            names = provider.list_models()
            if not names:
                raise MobileInputError("该服务没有可用模型，请先在官网或 Ollama 安装模型")
            model = names[0]
            self.models[self.provider] = model
        return provider, model

    def _native_ocr(self, content: bytes) -> str:
        # Called only on explicit extract_text. No optional desktop OCR import.
        try:
            from java import jclass
        except ImportError:
            raise MobileInputError("原生图片识别仅可在安卓应用中运行") from None
        with tempfile.TemporaryDirectory(prefix="ocr-", dir=self.data_dir) as folder:
            path = Path(folder) / "source.image"
            path.write_bytes(content)
            return str(jclass("cn.healthlife.local.NativeOcr").recognize(str(path)))

    def execute(self, action: str, params: dict[str, Any]) -> Any:
        if action == "auth.state":
            _params_allowed(params, set())
            return self.state()
        if action in {"auth.register", "auth.bootstrap_operator", "auth.login"}:
            _params_allowed(params, {"username", "password"})
            if not all(isinstance(params.get(key), str) for key in ("username", "password")):
                raise MobileInputError("请输入用户名和密码")
            method = {
                "auth.register": self.auth.register,
                "auth.bootstrap_operator": self.auth.bootstrap_operator,
                "auth.login": self.auth.login,
            }[action]
            self.logout()
            user = method(params["username"], params["password"])
            self.user_id = user.id
            return user
        if action == "auth.logout":
            _params_allowed(params, set())
            return self.logout()
        user = self._user()
        if _AUTH_PARAMS & set(params):
            raise MobileInputError("不能指定或冒充当前登录用户")
        if action == "auth.create_staff":
            _params_allowed(params, {"username", "password", "role_code"})
            return self.auth.create_staff(user.id, **params)
        if action == "system.check":
            _params_allowed(params, set())
            with self.database.connect() as connection:
                integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
                foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
                tables = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM sqlite_master "
                        "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    ).fetchone()[0]
                )
            return {
                "integrity": integrity,
                "foreign_key_errors": foreign_keys,
                "schema_version": SCHEMA_VERSION,
                "table_count": tables,
                "beijing_now": beijing_now(),
                "network_tested": False,
            }
        if action == "provider.status":
            _params_allowed(params, set())
            return self.provider_status()
        if action == "provider.configure":
            return self.configure(params)
        if action == "provider.models":
            _params_allowed(params, set())
            names = self._provider().list_models()
            if names and self.models[self.provider] not in names:
                self.models[self.provider] = names[0]
            return {
                "models": names,
                "selected": self.models[self.provider],
                "notice": self.provider_status()["notice"],
            }
        if action == "chat.list":
            _params_allowed(params, {"limit"})
            limit = params.get("limit", 200)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
                raise MobileInputError("消息条数需为 1 至 500")
            return self.chat.list_messages(user.id, limit=limit)
        if action == "chat.send":
            _params_allowed(params, {"content"})
            provider, model = self._provider_and_model()
            pending = self.chat.begin_message(user.id, params.get("content"), self.provider, model)
            try:
                result = provider.chat(model, self.chat.context_for_provider(user.id))
                return self.chat.complete_message(
                    user.id,
                    pending.assistant_message.id,
                    result,
                    provider=self.provider,
                    model=model,
                )
            except Exception as error:
                self.chat.fail_message(
                    user.id,
                    pending.assistant_message.id,
                    _safe_error(error),
                    provider=self.provider,
                    model=model,
                )
                raise
        if action == "ai.create_member_draft":
            _params_allowed(params, {"prompt", "member_user_id", "context_days", "file_ids"})
            values = dict(params)
            file_ids = values.pop("file_ids", [])
            if not isinstance(file_ids, list) or len(file_ids) > 2:
                raise MobileInputError("最多选择两张已上传图片")
            images = []
            for file_id in file_ids:
                stored = self.files.get_file(user.id, file_id)
                if not stored["media_type"].startswith("image/"):
                    raise MobileInputError("AI 图片输入只能选择图片，PDF 请先提取文字")
                images.append(stored["content"])
            provider, model = self._provider_and_model()
            return self.ai.create_member_draft(
                user.id, provider, self.provider, model, images=images, **values
            )
        if action == "ai.create_advisor_summary_draft":
            _params_allowed(params, {"member_user_id", "context_days"})
            provider, model = self._provider_and_model()
            return self.ai.create_advisor_summary_draft(
                user.id, provider=provider, provider_name=self.provider, model=model, **params
            )
        try:
            namespace, method_name = action.split(".", 1)
        except ValueError:
            raise MobileInputError("不支持的操作") from None
        if namespace not in _METHODS or method_name not in _METHODS[namespace]:
            raise MobileInputError("不支持的操作")
        if namespace in {"health", "files"}:
            self._user({"member"})
        if namespace == "management":
            self._user({"advisor", "operator"})
        if namespace == "commerce" and params.get("image_path") is not None:
            raise MobileInputError("安卓商品不能引用电脑本地图片路径")
        if action == "health.add_life_record" and params.get("source", "manual") != "manual":
            raise MobileInputError("手动界面不能冒充设备或 AI 已确认数据来源")
        method = getattr(getattr(self, namespace), method_name)
        try:
            inspect.signature(method).bind(user.id, **params)
        except TypeError:
            raise MobileInputError("请求参数缺失或包含不支持的字段") from None
        if action == "files.extract_text" and (user.id, params.get("file_id")) in self.ocr_cache:
            return self.attachment_text(
                params["file_id"], self.ocr_cache[(user.id, params["file_id"])]
            )
        return method(user.id, **params)

    def import_attachment(
        self, path: str, filename: str, ocr_text: str | None = None
    ) -> dict[str, Any]:
        user = self._user({"member"})
        with Path(path).open("rb") as stream:
            content = stream.read(MAX_FILE_SIZE + 1)
        file_id = self.files.upload_bytes(user.id, filename, content)
        if ocr_text is not None:
            self.ocr_cache[(user.id, file_id)] = str(ocr_text)[:50000]
        return {
            key: value
            for key, value in self.files.get_file(user.id, file_id).items()
            if key != "content"
        }

    def export_attachment(self, file_id: int, path: str) -> dict[str, Any]:
        user = self._user({"member"})
        stored = self.files.get_file(user.id, file_id)
        self.files.export_file(user.id, file_id, path, overwrite=True)
        return {
            "file_id": file_id,
            "name": stored["original_name"],
            "mime": stored["media_type"],
            "original_name": stored["original_name"],
            "media_type": stored["media_type"],
            "path": str(path),
        }

    def attachment_text(self, file_id: int, ocr_text: str) -> dict[str, Any]:
        user = self._user({"member"})
        if not isinstance(ocr_text, str) or len(ocr_text) > 200000:
            raise MobileInputError("识别文字过长或格式无效")
        files = FileManagementService(self.database, ocr_engine=lambda _: ocr_text)
        result = files.extract_text(user.id, file_id)
        self.ocr_cache[(user.id, file_id)] = ocr_text[:50000]
        return result

    def backup_export(self, path: str):
        if not self._migration_allowed():
            raise MobileInputError(
                "全库备份包含所有账号资料，仅运营员可导出；无运营员时仅限单账号本人。"
            )
        return self.backup.create_archive(path)

    def backup_import(self, path: str):
        if not self._migration_allowed(importing=True):
            raise MobileInputError("不能覆盖其他账号数据，请使用运营账号；首次空库可直接导入。")
        result = asdict(self.backup.import_archive(path))
        self.logout()
        result["state"] = self.state()
        return result


def initialize(data_dir: str) -> str:
    global _APP
    with _LOCK:
        try:
            _APP = MobileApplication(data_dir)
            return _json({"ok": True, "result": _APP.state()})
        except Exception as error:
            _APP = None
            return _json({"ok": False, "error": _safe_error(error)})


def dispatch(request_json: str) -> str:
    request_id: str | int | None = None
    with _LOCK:
        try:
            if not isinstance(request_json, str) or len(request_json) > 256000:
                raise MobileInputError("请求过长或格式无效")
            request = json.loads(
                request_json, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
            )
            if not isinstance(request, dict) or set(request) - {"id", "action", "params"}:
                raise MobileInputError("请求格式无效")
            value = request.get("id")
            if (
                isinstance(value, bool)
                or not isinstance(value, (str, int))
                or len(str(value)) > 128
            ):
                raise MobileInputError("请求编号格式无效")
            request_id = value
            action, params = request.get("action"), request.get("params", {})
            if not isinstance(action, str) or len(action) > 100 or not isinstance(params, dict):
                raise MobileInputError("操作或参数格式无效")
            if _APP is None:
                raise MobileInputError("应用尚未初始化，请重新打开")
            result = _APP.execute(action, params)
            return _json({"id": request_id, "ok": True, "result": result})
        except Exception as error:
            return _json({"id": request_id, "ok": False, "error": _safe_error(error)})


def _native_call(name: str, *args) -> str:
    with _LOCK:
        try:
            if _APP is None:
                raise MobileInputError("应用尚未初始化")
            return _json({"ok": True, "result": getattr(_APP, name)(*args)})
        except Exception as error:
            return _json({"ok": False, "error": _safe_error(error)})


def import_attachment(path: str, filename: str, ocr_text: str | None = None) -> str:
    return _native_call("import_attachment", path, filename, ocr_text)


def export_attachment(file_id: int, absolute_path: str) -> str:
    return _native_call("export_attachment", file_id, absolute_path)


def attachment_export(file_id: int, path: str) -> str:
    return export_attachment(file_id, path)


def attachment_text(file_id: int, ocr_text: str) -> str:
    return _native_call("attachment_text", file_id, ocr_text)


def backup_export(path: str) -> str:
    return _native_call("backup_export", path)


def backup_import(path: str) -> str:
    return _native_call("backup_import", path)
