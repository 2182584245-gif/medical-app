"""Opt-in offline reauthentication with an absolute server-bounded expiry.

Never grants online authentication. A lease is metadata, not a bearer token, and
is accepted only after HTTPS online login or current-user DPAPI verification.
"""

from __future__ import annotations

import hashlib
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..security.passwords import hash_password, normalize_username, verify_password
from ..security.private_payload import read_private_json, write_private_json

MAX_OFFLINE_SECONDS = 12 * 3600


class OfflineAccessError(RuntimeError):
    pass


def utc_timestamp(value):
    if type(value) is not str or len(value) > 64:
        raise OfflineAccessError("离线授权时间无效。")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise OfflineAccessError("离线授权时间无效。") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise OfflineAccessError("离线授权时间必须包含时区。")
    return result.astimezone(UTC)


def validate_lease(lease, *, actor_id=None, now=None):
    keys = {"version", "actor_id", "role_code", "server_instance_id", "issued_at", "expires_at"}
    if (type(lease) is not dict or set(lease) != keys
            or type(lease["version"]) is not int or lease["version"] != 1):
        raise OfflineAccessError("服务端未提供有效的离线授权。")
    if type(lease["actor_id"]) is not int or lease["actor_id"] <= 0:
        raise OfflineAccessError("离线授权账户无效。")
    if actor_id is not None and lease["actor_id"] != actor_id:
        raise OfflineAccessError("离线授权账户不匹配。")
    if lease["role_code"] not in {"member", "advisor", "operator"}:
        raise OfflineAccessError("离线授权角色无效。")
    if (
        type(lease["server_instance_id"]) is not str
        or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", lease["server_instance_id"])
    ):
        raise OfflineAccessError("离线授权云端标识无效。")
    issued, expiry = utc_timestamp(lease["issued_at"]), utc_timestamp(lease["expires_at"])
    current = now or datetime.now(UTC)
    if not issued <= current < expiry or not timedelta(0) < expiry-issued <= timedelta(
        seconds=MAX_OFFLINE_SECONDS
    ):
        raise OfflineAccessError("离线授权已到期或系统时间异常，请在线登录。")
    return dict(lease)


class OfflineAccessStore:
    def __init__(self, root: Path, *, clock=None):
        self.root = Path(root)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()

    @staticmethod
    def _key(endpoint, username):
        normalized = normalize_username(username)
        return hashlib.sha256((endpoint.rstrip("/") + "\0" + normalized).encode()).hexdigest()

    def _load(self, endpoint, username):
        key = self._key(endpoint, username)
        return key, read_private_json(self.root / (key + ".offline"), "signin/" + key)

    def _save(self, key, value):
        write_private_json(self.root / (key + ".offline"), "signin/" + key, value)

    def enroll(self, endpoint, user, password, lease):
        with self._lock:
            now = self._clock()
            lease = validate_lease(lease, actor_id=user["id"], now=now)
            if lease["role_code"] != user["role_code"] or user["account_status"] != "active":
                raise OfflineAccessError("账号当前不能启用离线访问。")
            key = self._key(endpoint, user["username"])
            self._save(key, {"version": 1, "endpoint": endpoint.rstrip("/"), "user": dict(user),
                             "lease": lease, "verifier": hash_password(password),
                             "last_clock": now.isoformat(), "failures": 0, "locked_until": None,
                             "revoked": False})

    def authenticate(self, endpoint, username, password):
        with self._lock:
            key, value = self._load(endpoint, username)
            now = self._clock()
            if (
                value is None or value.get("revoked")
                or value.get("endpoint") != endpoint.rstrip("/")
            ):
                raise OfflineAccessError("尚未启用此云端账号的离线登录，请先在线登录。")
            if now < utc_timestamp(value["last_clock"]):
                raise OfflineAccessError("检测到系统时间回退，请在线重新验证。")
            lease = validate_lease(value["lease"], actor_id=value["user"]["id"], now=now)
            if value.get("locked_until") and now < utc_timestamp(value["locked_until"]):
                raise OfflineAccessError("离线密码尝试过多，请稍后再试。")
            value["last_clock"] = now.isoformat()
            if not verify_password(value["verifier"], password):
                value["failures"] += 1
                if value["failures"] >= 5:
                    value["locked_until"] = (now + timedelta(minutes=5)).isoformat()
                self._save(key, value)
                raise OfflineAccessError("离线用户名或密码错误。")
            value["failures"], value["locked_until"] = 0, None
            self._save(key, value)
            return dict(value["user"]), lease

    def revoke(self, endpoint, username):
        with self._lock:
            key = self._key(endpoint, username)
            if (self.root / (key + ".offline")).exists():
                # Keep an encrypted tombstone, so failed/expired authorization is
                # not silently reactivated by the next offline login.
                self._save(key, {"version": 1, "revoked": True})
