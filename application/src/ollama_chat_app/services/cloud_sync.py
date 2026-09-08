"""Account-isolated encrypted mirrors and opt-in, conflict-checked offline intents."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThreadPool, QTimer, Signal, Slot

from ..security.private_payload import protect_payload, read_private_json, write_private_json
from ..workers.task import FunctionTask
from .cloud_client import CloudAPIError, validate_base_url
from .cloud_rpc_codec import decode_rpc, encode_rpc
from .offline_access import OfflineAccessError, utc_timestamp, validate_lease
from .offline_outbox import (
    OfflineOutbox,
    QueuedOperation,
    canonical_method,
    content_version,
    version_resource,
)
from .sync_files import SyncFileCache
from .sync_protocol import (
    RESOURCE_KINDS,
    decode_large_snapshot,
    encode_large_snapshot,
    fetch_paged_snapshot,
    json_bytes,
)

MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
RESOURCE_TYPES = {
    "profile": dict,
    "preferences": dict,
    "life_records": list,
    "reminders": list,
    "conversations": list,
    "messages": dict,
    "service_summary": dict,
    "appointments": list,
    "members": list,
    "advisors": list,
    "work_statistics": dict,
}
EXCLUDED_RESOURCES = frozenset({"chat_attachments", "user_files", "report_files"})
_SECRET_FIELDS = frozenset(
    {
        "password",
        "password_hash",
        "api_key",
        "access_token",
        "refresh_token",
        "bearer_token",
        "secret",
        "token",
    }
)


class SyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class SyncIdentity:
    endpoint: str
    server_instance_id: str
    actor_id: int

    def __post_init__(self):
        validate_base_url(self.endpoint, allow_loopback_http=True)
        if type(self.actor_id) is not int or self.actor_id <= 0:
            raise SyncError("同步账户编号无效")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", self.server_instance_id):
            raise SyncError("云端实例标识无效")

    @property
    def cache_key(self):
        text = f"{self.endpoint.rstrip('/')}\0{self.server_instance_id}\0{self.actor_id}"
        return hashlib.sha256(text.encode()).hexdigest()


def _reject_secrets(value):
    if isinstance(value, dict):
        if value.get("$__type") == "bytes":
            raise SyncError("快照暂不支持缓存文件原始内容")
        if any(str(key).lower() in _SECRET_FIELDS for key in value):
            raise SyncError("云端快照包含禁止缓存的认证字段")
        for child in value.values():
            _reject_secrets(child)
    elif isinstance(value, list):
        for child in value:
            _reject_secrets(child)
    elif isinstance(value, bytes):
        raise SyncError("快照暂不支持缓存文件原始内容")


def validate_snapshot(snapshot, actor_id):
    required = {
        "schema_version",
        "server_instance_id",
        "actor_id",
        "revision",
        "complete",
        "resources",
        "excluded",
    }
    if type(snapshot) is not dict or set(snapshot) != required:
        raise SyncError("云端快照字段不完整，请更新应用或服务端")
    if (
        type(snapshot["schema_version"]) is not int
        or snapshot["schema_version"] not in {1, 2}
        or snapshot["complete"] is not True
    ):
        raise SyncError("云端快照不完整，已保留上次完整数据")
    if type(snapshot["actor_id"]) is not int or snapshot["actor_id"] != actor_id:
        raise SyncError("云端快照所属账户不匹配")
    if type(snapshot["revision"]) not in (str, int) or len(str(snapshot["revision"])) > 200:
        raise SyncError("云端快照版本无效")
    resources = snapshot["resources"]
    expected = set(RESOURCE_TYPES) if snapshot["schema_version"] == 1 else set(RESOURCE_KINDS)
    if type(resources) is not dict or set(resources) != expected:
        raise SyncError("云端快照资源不完整，未更新本机镜像")
    for name, kind in RESOURCE_TYPES.items():
        if type(resources[name]) is not kind:
            raise SyncError("云端快照资源格式无效")
    for name in expected - set(RESOURCE_TYPES):
        if type(resources[name]) is not list:
            raise SyncError("云端分页资源格式无效")
    if any(
        type(key) is not str or not key.isdecimal() or type(value) is not list
        for key, value in resources["messages"].items()
    ):
        raise SyncError("云端聊天镜像格式无效")
    excluded = snapshot["excluded"]
    expected_excluded = EXCLUDED_RESOURCES if snapshot["schema_version"] == 1 else set()
    if type(excluded) is not dict or set(excluded) != expected_excluded:
        raise SyncError("云端未准确说明未镜像文件的范围")
    if any(type(count) is not int or count < 0 for count in excluded.values()):
        raise SyncError("未镜像文件计数无效")
    if snapshot["schema_version"] == 2:
        encoded = encode_large_snapshot(snapshot)
        _reject_secrets(encoded)
        return json_bytes(encoded).decode()
    encoded = encode_rpc(snapshot)
    _reject_secrets(encoded)
    raw = json.dumps(encoded, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(raw.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        raise SyncError("快照超过 8 MiB 上限，本次未缓存；请缩小同步范围")
    return raw


class CloudMirrorStore:
    """A separate JSON mirror. No local users, passwords, or mutable domain services."""

    def __init__(self, cache_root: Path):
        self.cache_root = Path(cache_root).resolve()
        self.identity: SyncIdentity | None = None
        self.path: Path | None = None
        self.authorized = False
        self._fresh = False
        self._lease = None
        self._expires_at = None
        self._last_clock = datetime.now(UTC)

    def activate(self, identity: SyncIdentity, lease=None):
        """Call only after a successful authenticated, identity-checked snapshot response."""
        if identity != self.identity:
            self._fresh = False
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.identity = identity
        if lease is not None:
            try:
                self._lease = validate_lease(lease, actor_id=identity.actor_id)
            except OfflineAccessError:
                raise SyncError("离线授权已到期，请重新在线登录。") from None
            if self._lease["server_instance_id"] != identity.server_instance_id:
                raise SyncError("离线授权与同步云端实例不匹配。")
            self._expires_at = utc_timestamp(lease["expires_at"])
        else:
            # Compatibility with old servers/tests remains strictly bounded and
            # can never authorize a cross-process offline sign-in.
            self._lease = None
            self._expires_at = datetime.now(UTC) + timedelta(minutes=15)
        self.path = self.cache_root / f"cloud-{identity.cache_key}.sqlite"
        checkpoint = read_private_json(
            self.cache_root / f"clock-{identity.cache_key}.private", "clock/" + identity.cache_key)
        if checkpoint is not None:
            self._last_clock = max(self._last_clock, utc_timestamp(checkpoint["last_clock"]))
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS mirror_snapshot "
                "(singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                "payload TEXT NOT NULL,last_success TEXT NOT NULL)"
            )
        self.authorized = True

    @property
    def expires_at(self):
        return self._expires_at.isoformat() if self._expires_at else None

    def _check_deadline(self):
        now = datetime.now(UTC)
        if self._expires_at is None or now >= self._expires_at or now < self._last_clock:
            self.revoke()
            raise SyncError("离线镜像授权已到期或系统时间回退，请重新在线登录。")
        self._last_clock = now
        if self.identity is not None:
            write_private_json(
                self.cache_root / f"clock-{self.identity.cache_key}.private",
                "clock/" + self.identity.cache_key, {"last_clock": now.isoformat()},
            )

    def _payload(self):
        if self.path is None or self.identity is None:
            raise SyncError("没有当前账户的镜像。")
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT payload FROM mirror_snapshot WHERE singleton=1"
            ).fetchone()
        if row is None or not row[0].startswith("dpapi-v1:"):
            raise SyncError("镜像尚未安全缓存，请先在线同步。")
        try:
            return json.loads(protect_payload(
                base64.b64decode(row[0][9:], validate=True), "mirror/" + self.identity.cache_key,
                decrypt=True,
            ))
        except Exception:
            self.revoke()
            raise SyncError("镜像完整性或账户保护校验失败。") from None

    def authorize_offline(self, identity: SyncIdentity, lease):
        self.activate(identity, lease)
        payload = self._payload()
        if payload.get("lease") != lease:
            self.revoke()
            raise SyncError("本机没有与本次离线授权对应的完整镜像，请在线同步。")
        self._check_deadline()
        self._fresh = True
        self.snapshot()

    def apply_snapshot(self, snapshot):
        if not self.authorized or self.identity is None or self.path is None:
            raise SyncError("当前镜像尚未通过云端身份验证")
        if snapshot.get("server_instance_id") != self.identity.server_instance_id:
            raise SyncError("云端实例已变化，请重新确认连接目标")
        raw = validate_snapshot(snapshot, self.identity.actor_id)
        self._check_deadline()
        stamp = datetime.now(UTC).isoformat()
        private = json.dumps(
            {"snapshot": json.loads(raw), "lease": self._lease, "expires_at": self.expires_at},
            ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        ).encode()
        raw = "dpapi-v1:" + base64.b64encode(protect_payload(
            private, "mirror/" + self.identity.cache_key
        )).decode("ascii")
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA secure_delete=ON")
            connection.execute(
                "INSERT INTO mirror_snapshot(singleton,payload,last_success) "
                "VALUES(1,?,?) ON CONFLICT(singleton) DO UPDATE SET "
                "payload=excluded.payload,last_success=excluded.last_success",
                (raw, stamp),
            )
        self._fresh = True

    def snapshot(self):
        if not self.authorized or self.path is None:
            raise SyncError("请重新登录云端后查看同步数据")
        if not self._fresh:
            raise SyncError("镜像正在刷新，请联网获取写入后的最新数据")
        self._check_deadline()
        payload = self._payload()
        if utc_timestamp(payload["expires_at"]) <= datetime.now(UTC):
            self.revoke()
            raise SyncError("镜像已超过绝对有效期，请在线同步。")
        encoded = payload["snapshot"]
        return decode_large_snapshot(encoded) if encoded.get("schema_version") == 2 else decode_rpc(
            encoded)

    def read_resource(self, name):
        if name not in RESOURCE_KINDS:
            raise SyncError("未同步此类资源")
        return self.snapshot()["resources"][name]

    @property
    def last_success(self):
        if self.path is None:
            return None
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT last_success FROM mirror_snapshot WHERE singleton=1"
            ).fetchone()
        return row[0] if row else None

    def revoke(self):
        self.authorized = False
        self._fresh = False

    def mark_stale(self):
        """Keep identity authorization, but never serve pre-write records as current."""
        self._fresh = False


class CloudSyncService(QObject):
    """Single-flight Qt worker polling. Offline state exposes only the last authorized mirror."""

    status_changed = Signal(dict)
    snapshot_changed = Signal(dict)
    authorization_lost = Signal()
    reauthentication_required = Signal(str)
    outbox_changed = Signal(dict)
    _write_refresh_requested = Signal(int)
    _authorization_rejection_requested = Signal(int, object)

    def __init__(self, client, mirror: CloudMirrorStore, parent=None, *, interval_ms=10_000):
        super().__init__(parent)
        self.client, self.mirror = client, mirror
        self.outbox = OfflineOutbox(mirror.cache_root / "outbox")
        self.files = SyncFileCache(mirror.cache_root / "attachments")
        self.actor_id = None
        self.actor_role = None
        self.identity = None
        self._generation = 0
        self._task = None
        self._resync = False
        self._state = "stopped"
        self._message = "尚未连接云端同步"
        self._lock = threading.RLock()
        self._intent_lock = threading.RLock()
        self._authorization_blocked = False
        self.timer = QTimer(self)
        self.timer.setInterval(max(1000, interval_ms))
        self.timer.timeout.connect(self.sync_now)
        self._write_refresh_requested.connect(
            self._refresh_after_write, Qt.ConnectionType.QueuedConnection
        )
        self._authorization_rejection_requested.connect(
            self._failed, Qt.ConnectionType.QueuedConnection
        )

    def start(
        self, actor_id: int, expected_server_instance_id: str | None = None, *, role_code=None
    ):
        if not getattr(self.client, "is_authenticated", False):
            raise SyncError("云端同步需要先完成在线登录")
        if type(actor_id) is not int or actor_id <= 0:
            raise SyncError("同步账户无效")
        if role_code not in {None, "member", "advisor", "operator"}:
            raise SyncError("同步账户角色无效")
        self.stop()
        self.actor_id = actor_id
        self.actor_role = role_code
        self._authorization_blocked = False
        self.identity = (
            SyncIdentity(self.client.base_url, expected_server_instance_id, actor_id)
            if expected_server_instance_id
            else None
        )
        self._state = "connecting"
        if getattr(self.client, "is_offline_session", False):
            lease = self.client.offline_lease
            self.identity = SyncIdentity(
                self.client.base_url, lease["server_instance_id"], actor_id
            )
            self.mirror.authorize_offline(self.identity, lease)
            self._state = "offline"
            self.snapshot_changed.emit(self.mirror.snapshot())
        self.timer.start()
        self.sync_now()

    def stop(self):
        self.timer.stop()
        with self._lock:
            self._generation += 1
            self.actor_id = None
            self.actor_role = None
            self.identity = None
            self._resync = False
            self.mirror.revoke()
        self._set_state("stopped", "同步已停止，镜像需重新在线验证后才能查看")

    @property
    def offline_enabled(self):
        return (getattr(self.client, "offline_store", None) is not None
                and getattr(self.client, "offline_opt_in", False))

    @property
    def is_syncing(self):
        return self._task is not None

    @property
    def cache_generation(self):
        with self._lock:
            return self._generation

    @property
    def authorization_blocked(self):
        with self._lock:
            return self._authorization_blocked

    def snapshot_generation_is_current(self, generation):
        with self._lock:
            return (
                generation == self._generation
                and not self._authorization_blocked
                and self.mirror.authorized
                and self.mirror._fresh
            )

    def authorized_snapshot(self, actor_id: int, endpoint: str):
        """Read only the current complete, unmodified, identity-matched generation."""
        with self._lock:
            if self._authorization_blocked:
                raise CloudAPIError("permission")
            if actor_id != self.actor_id or endpoint.rstrip("/") != self.client.base_url.rstrip(
                "/"
            ):
                raise CloudAPIError("permission")
            if self.identity is None or self.mirror.identity != self.identity:
                raise SyncError("尚未取得当前身份的完整镜像")
            if self.identity.actor_id != actor_id or self.identity.endpoint.rstrip(
                "/"
            ) != endpoint.rstrip("/"):
                raise CloudAPIError("permission")
            snapshot = self.mirror.snapshot()
            if snapshot.get("actor_id") != actor_id or snapshot.get("complete") is not True:
                raise SyncError("镜像账户或完整性无效")
            if snapshot.get("server_instance_id") != self.identity.server_instance_id:
                raise CloudAPIError("permission")
            return snapshot

    def invalidate_after_write(self):
        """Callable from workers; revoke freshness before queuing owner-thread resync."""
        with self._lock:
            self._generation += 1
            generation = self._generation
            self.mirror.mark_stale()
        self._write_refresh_requested.emit(generation)

    @Slot(int)
    def _refresh_after_write(self, generation):
        if generation != self._generation or self.actor_id is None or self._authorization_blocked:
            return
        self.sync_now()

    def reject_authorization(self, error):
        """Revoke synchronously even when the network operation runs in a worker."""
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._authorization_blocked = True
            self.mirror.revoke()
        self._authorization_rejection_requested.emit(generation, error)

    def state(self):
        same_actor = (
            self.mirror.identity is not None
            and self.mirror.identity.actor_id == self.actor_id
            and self.mirror.identity.endpoint.rstrip("/") == self.client.base_url.rstrip("/")
        )
        pending = self._outbox_summary() if self.offline_enabled and same_actor else []
        return {
            "state": self._state,
            "message": self._message,
            "last_success": self.mirror.last_success if same_actor else None,
            "read_only": self._state != "online",
            "authorized": self.mirror.authorized,
            "fresh": self.mirror._fresh,
            "actor_id": self.actor_id,
            "offline_expires_at": self.mirror.expires_at,
            "pending_count": len(pending),
            "conflict_count": sum(row["status"] in {"conflict", "rejected"} for row in pending),
            "offline_editable": self.offline_enabled and self.mirror.authorized,
        }

    def _outbox_identity(self):
        if (
            not self.offline_enabled or self.identity is None or self._authorization_blocked
            or self.actor_id != self.identity.actor_id or not self.client.is_authenticated
        ):
            raise CloudAPIError("permission")
        self.mirror._check_deadline()
        return self.identity

    def _outbox_summary(self):
        if self.identity is None or self._authorization_blocked or not self.mirror.authorized:
            return []
        return self.outbox.list(self.identity)

    def _notify_outbox(self):
        rows = self._outbox_summary()
        self.outbox_changed.emit({"pending_count": len(rows), "conflict_count": sum(
            row["status"] in {"conflict", "rejected"} for row in rows)})

    def pending_operations(self):
        return self.outbox.list(self._outbox_identity())

    def get_pending(self, identifier):
        return next((row for row in self.outbox.list(self._outbox_identity(), include_payload=True)
                     if row["operation_id"] == identifier), None)

    def discard_pending(self, identifier):
        self.outbox.discard(self._outbox_identity(), identifier)
        self._notify_outbox()

    def _base_version(self, service, method):
        resource = version_resource(service, method)
        if resource is None:
            return None
        snapshot = self.authorized_snapshot(self.actor_id, self.client.base_url)
        return content_version(snapshot["resources"][resource])

    def rebase_pending(self, identifier, patch=None):
        """Only a user-confirmed conflict resolution creates a new operation UUID."""
        if self._state != "online" or not self.client.is_online_authenticated:
            raise CloudAPIError("offline")
        identity = self._outbox_identity()
        item = self.get_pending(identifier)
        if item is None or item["status"] not in {"conflict", "rejected"}:
            raise CloudAPIError("conflict")
        kwargs = dict(item["kwargs"])
        if patch is not None:
            if type(patch) is not dict:
                raise CloudAPIError("validation")
            kwargs.update(patch)
        queued = self.outbox.enqueue(identity, item["service"], item["method"], item["args"],
                                     kwargs, base_version=self._base_version(
                                         item["service"], item["method"]))
        self.outbox.discard(identity, identifier)
        self._notify_outbox()
        self._write_refresh_requested.emit(self._generation)
        return queued

    def submit_intent(self, service, method, args, kwargs):
        identity = self._outbox_identity()
        method = canonical_method(service, method)
        if service != "preferences" and self.actor_role != "member":
            raise CloudAPIError("permission")
        queued = self.outbox.enqueue(identity, service, method, args, kwargs,
                                     base_version=self._base_version(service, method))
        self._notify_outbox()
        if self._state == "offline" or not self.client.is_online_authenticated:
            return queued
        item = self.get_pending(queued.operation_id)
        try:
            result = self._send_intent(identity, item)
            self.invalidate_after_write()
            return result
        except CloudAPIError as error:
            if error.code in {"network", "timeout", "unavailable", "offline"}:
                return QueuedOperation(queued.operation_id, service, method, queued.created_at,
                                       "uncertain" if error.outcome_uncertain else "queued")
            raise

    def _send_intent(self, identity, item):
        with self._intent_lock:
            return self._send_intent_locked(identity, item)

    def _send_intent_locked(self, identity, item):
        if identity != self.identity or identity.actor_id != self.actor_id:
            raise CloudAPIError("permission")
        try:
            user = self.client.me()
            if user["id"] != identity.actor_id or user["role_code"] != self.actor_role:
                raise CloudAPIError("permission")
            intent = {key: item[key] for key in
                      ("service", "method", "args", "kwargs", "base_version")}
            result = decode_rpc(self.client.rpc(
                "sync", "apply_queued", encode_rpc([identity.actor_id, intent]), encode_rpc({}),
                request_id=item["operation_id"],
            ))
        except CloudAPIError as error:
            if (item["status"] == "uncertain" or error.outcome_uncertain
                    or (error.code == "conflict" and error.conflict_kind != "base_version")):
                # Scope/journal conflicts do NOT prove that the original write
                # failed. Neither do later 422/404 responses after a lost ACK.
                status = "uncertain"
            elif error.code == "conflict":
                status = "conflict"
            elif error.code in {"validation", "not_found"}:
                status = "rejected"
            else:
                status = "queued"
            self.outbox.set_status(identity, item["operation_id"], status, error.code)
            self._notify_outbox()
            if (identity == self.identity and identity.actor_id == self.actor_id
                    and error.code in {"authentication", "not_authenticated", "permission"}):
                self.reject_authorization(error)
            raise
        self.outbox.set_status(identity, item["operation_id"], "applied")
        with self._lock:
            self.mirror.mark_stale()
        self._notify_outbox()
        return result

    def _replay_pending(self, actor_id):
        if not self.offline_enabled or self.identity is None:
            return
        identity = self._outbox_identity()
        if identity.actor_id != actor_id:
            raise CloudAPIError("permission")
        for item in self.outbox.list(identity, include_payload=True):
            if item["status"] not in {"queued", "uncertain"}:
                continue
            try:
                self._send_intent(identity, item)
            except CloudAPIError as error:
                if error.code not in {"conflict", "validation", "not_found"}:
                    raise

    def sync_now(self):
        if (
            self.actor_id is None
            or self._authorization_blocked
            or self._state in {"unauthorized", "blocked"}
        ):
            return False
        if self._task is not None:
            self._resync = True
            return False
        generation, actor_id = self._generation, self.actor_id
        task = FunctionTask(self._fetch_snapshot, actor_id)
        self._task = task
        task.signals.result.connect(lambda result: self._accept(generation, actor_id, result))
        task.signals.error.connect(lambda error: self._failed(generation, error))
        task.signals.finished.connect(lambda: self._finished(task))
        self._set_state("syncing", "正在同步云端完整记录…")
        QThreadPool.globalInstance().start(task)
        return True

    def _fetch_snapshot(self, actor_id):
        if self.mirror.authorized:
            self.mirror._check_deadline()
        if getattr(self.client, "is_offline_session", False):
            self.client.probe_online()
            # A connectivity probe is NOT authentication. Never persist or turn
            # the offline verifier into an online bearer token.
            raise CloudAPIError("authentication")
        if hasattr(self.client, "me"):
            user = self.client.me()
            if user["id"] != actor_id or (self.actor_role and user["role_code"] != self.actor_role):
                raise CloudAPIError("permission")
        self._replay_pending(actor_id)
        if getattr(self.client, "sync_protocol", 1) == 2:
            try:
                snapshot = fetch_paged_snapshot(self.client, actor_id)
                identity = SyncIdentity(
                    self.client.base_url, snapshot["server_instance_id"], actor_id)
                self.files.fetch(self.client, identity, snapshot["resources"])
                final = decode_rpc(self.client.rpc(
                    "sync", "get_sync_manifest", encode_rpc([actor_id]), {}))
                if (final["revision"] != snapshot["revision"]
                        or final["actor_id"] != actor_id
                        or final["server_instance_id"] != snapshot["server_instance_id"]):
                    raise CloudAPIError("conflict")
                validate_snapshot(snapshot, actor_id)
                return snapshot
            except ValueError:
                raise SyncError("分页镜像或附件校验未通过，未发布不完整数据。") from None
        result = self.client.rpc("sync", "get_snapshot", encode_rpc([actor_id]), encode_rpc({}))
        snapshot = decode_rpc(result)
        validate_snapshot(snapshot, actor_id)
        return snapshot

    def _accept(self, generation, actor_id, snapshot):
        try:
            with self._lock:
                if generation != self._generation or actor_id != self.actor_id:
                    return
                identity = SyncIdentity(
                    self.client.base_url, snapshot["server_instance_id"], actor_id
                )
                if self.identity is not None and identity != self.identity:
                    raise SyncError("云端实例或账户已变化，请重新确认连接目标")
                self.identity = identity
                self.mirror.activate(identity, getattr(self.client, "offline_lease", None))
                self.mirror.apply_snapshot(snapshot)
            self._set_state("online", "云端身份已验证，记录已同步；"
                            "待提交内容会逐项核验后提交。")
            self.snapshot_changed.emit(snapshot)
        except Exception as error:
            self._failed(generation, error)

    @Slot(int, object)
    def _failed(self, generation, error):
        if generation != self._generation:
            return
        auth_error = isinstance(error, CloudAPIError) and error.code in {
            "authentication",
            "not_authenticated",
            "permission",
        }
        # Only an actual unavailable transport may preserve the last authorized
        # generation. A 409/422 page failure can mean a binding was removed;
        # presenting that older scope as an offline snapshot would leak access.
        transport_error = isinstance(error, CloudAPIError) and error.code in {
            "network", "timeout", "unavailable",
        }
        unsafe_error = not auth_error and not transport_error
        if auth_error or unsafe_error:
            self._authorization_blocked = True
            self._resync = False
            self.mirror.revoke()
            self.timer.stop()
            self._set_state("unauthorized" if auth_error else "blocked", str(error))
            self.authorization_lost.emit()
            if auth_error:
                if hasattr(self.client, "revoke_offline_access"):
                    self.client.revoke_offline_access()
                self.reauthentication_required.emit("网络已恢复或授权失效，请在线输入密码重新验证。")
        else:
            self._set_state("offline", "网络不可用；限期显示已验证镜像，"
                            "支持的修改仅保存在本机待提交。")

    def _finished(self, task):
        if self._task is task:
            self._task = None
        if self._resync and self.actor_id is not None:
            self._resync = False
            self.sync_now()

    def _set_state(self, state, message):
        self._state, self._message = state, message
        self.status_changed.emit(self.state())
