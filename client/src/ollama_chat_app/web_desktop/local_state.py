"""Small encrypted member-owned state that survives changing loopback ports.

Only appointment drafts and four daily focus selections are supported. No
caller-supplied key, filename, path, SQL or cloud identity is accepted.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import UTC, date, datetime, time
from pathlib import Path
from threading import RLock

from ..security.windows_dpapi import protect_secret

LOCAL_MEMBER_ACTIONS = frozenset({"local.booking-draft.get", "local.booking-draft.save", "local.booking-draft.delete", "local.member-preferences.get", "local.member-preferences.save"})
_FIELDS = {"service_type": 200, "date": 10, "time": 5, "address": 2000, "notes": 10000}
_FOCUS = ("diet", "exercise", "sleep", "health")
_MAX_FILE_BYTES = 64 * 1024


class LocalStateError(RuntimeError):
    def __init__(self, message: str, code: str = "local_storage"):
        self.code = code
        super().__init__(message)


class MemberLocalState:
    def __init__(self, root: Path, *, protector=protect_secret):
        self.root = Path(root).absolute()
        self._protector = protector
        self._lock = RLock()
        self._safe_path(self.root)
        if self.root.exists() and not self.root.is_dir():
            raise LocalStateError("本机资料目录不是文件夹。")

    @staticmethod
    def _safe_path(path: Path):
        for item in (path, *path.parents):
            if item.exists() or item.is_symlink():
                info = item.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise LocalStateError("本机资料目录不可使用链接，请检查新应用数据目录。")
        if path.exists() and not (path.is_dir() or path.is_file()):
            raise LocalStateError("本机资料存储位置无效。")

    @staticmethod
    def _owner(endpoint: str, actor: int) -> str:
        if not isinstance(endpoint, str) or not endpoint or type(actor) is not int or actor <= 0:
            raise LocalStateError("请先登录会员账号。", "not_authenticated")
        return hashlib.sha256(json.dumps(["FAWNSEEv1.0.0", endpoint.rstrip("/"), actor], separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _appointment_id(value) -> int:
        if type(value) is not int or not 1 <= value < 2**63:
            raise LocalStateError("预约编号格式不正确。", "validation")
        return value

    @staticmethod
    def _fields(value) -> dict:
        if not isinstance(value, dict) or set(value) != set(_FIELDS):
            raise LocalStateError("本机预约草稿仅支持服务、日期、时间、地址和备注。", "validation")
        fields = {}
        for name, maximum in _FIELDS.items():
            item = value[name]
            if not isinstance(item, str) or len(item) > maximum or any(ord(c) < 32 and c not in "\n\r\t" for c in item):
                raise LocalStateError("本机预约草稿内容格式或长度不正确。", "validation")
            fields[name] = item.strip()
        try:
            if fields["date"]:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", fields["date"]):
                    raise ValueError
                date.fromisoformat(fields["date"])
            if fields["time"]:
                if not re.fullmatch(r"\d{2}:\d{2}", fields["time"]):
                    raise ValueError
                time.fromisoformat(fields["time"])
        except ValueError:
            raise LocalStateError("草稿日期或时间无效，请检查后再保存。", "validation") from None
        return fields

    @staticmethod
    def _focus(value) -> list[str]:
        if not isinstance(value, list) or len(value) > 16 or any(not isinstance(item, str) or item not in _FOCUS for item in value):
            raise LocalStateError("日常关注仅支持饮食、运动、睡眠和健康四项。", "validation")
        return [item for item in _FOCUS if item in value]

    def _path(self, owner, name):
        path = self.root / owner / (name + ".dpapi")
        self._safe_path(path)
        return path

    def _read(self, owner, name):
        path = self._path(owner, name)
        if not path.exists():
            return None
        try:
            if not path.is_file() or not 0 < path.stat().st_size <= _MAX_FILE_BYTES:
                raise ValueError
            value = json.loads(self._protector(path.read_bytes(), decrypt=True))
            if not isinstance(value, dict) or set(value) != {"version", "owner", "name", "data"} or value["version"] != 1 or value["owner"] != owner or value["name"] != name:
                raise ValueError
            return value["data"]
        except Exception:
            raise LocalStateError("本机资料无法安全读取，原文件已保留。请不要反复覆盖保存。") from None

    def _write(self, owner, name, data):
        path = self._path(owner, name)
        temporary = None
        try:
            raw = json.dumps({"version": 1, "owner": owner, "name": name, "data": data}, ensure_ascii=False, allow_nan=False).encode()
            encrypted = self._protector(raw)
            if not isinstance(encrypted, bytes) or not 0 < len(encrypted) <= _MAX_FILE_BYTES:
                raise ValueError
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._safe_path(path)
            descriptor, temporary = tempfile.mkstemp(prefix="local-", suffix=".tmp", dir=path.parent)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            self._safe_path(path)
            os.replace(temporary, path)
            temporary = None
        except Exception:
            raise LocalStateError("本机资料保存失败，未覆盖原草稿。请保留当前输入后重试。") from None
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    pass

    def request(self, endpoint: str, actor: int, action: str, body: dict):
        with self._lock:
            owner = self._owner(endpoint, actor)
            if action.startswith("local.booking-draft."):
                allowed = {"appointment_id", "fields"} if action.endswith(".save") else {"appointment_id"}
                if set(body) != allowed:
                    raise LocalStateError("本机预约草稿参数格式不正确。", "validation")
                name = "booking-draft-" + str(self._appointment_id(body["appointment_id"]))
                if action.endswith(".get"):
                    data = self._read(owner, name)
                    if data is None:
                        return None
                    try:
                        if not isinstance(data, dict) or set(data) != {"fields", "savedAt"} or not isinstance(data["savedAt"], str) or datetime.fromisoformat(data["savedAt"]).tzinfo is None:
                            raise ValueError
                        return {"fields": self._fields(data["fields"]), "savedAt": data["savedAt"]}
                    except Exception:
                        raise LocalStateError("本机预约草稿格式无效，原文件已保留。") from None
                if action.endswith(".save"):
                    data = {"fields": self._fields(body["fields"]), "savedAt": datetime.now(UTC).isoformat()}
                    self._write(owner, name, data)
                    return data
                if action == "local.booking-draft.delete":
                    try:
                        self._path(owner, name).unlink(missing_ok=True)
                    except OSError:
                        raise LocalStateError("本机预约草稿未能删除，请稍后重试。") from None
                    return {"deleted": True}
            if action == "local.member-preferences.get":
                if body:
                    raise LocalStateError("本机偏好读取参数无效。", "validation")
                data = self._read(owner, "member-preferences")
                if data is None:
                    return {"health_focus": []}
                try:
                    if not isinstance(data, dict) or set(data) != {"health_focus"}:
                        raise ValueError
                    return {"health_focus": self._focus(data["health_focus"])}
                except Exception:
                    raise LocalStateError("本机日常偏好格式无效，原文件已保留。") from None
            if action == "local.member-preferences.save":
                if set(body) != {"health_focus"}:
                    raise LocalStateError("本机偏好保存参数无效。", "validation")
                data = {"health_focus": self._focus(body["health_focus"])}
                self._write(owner, "member-preferences", data)
                return data
            raise LocalStateError("本机操作未开放。", "permission")
