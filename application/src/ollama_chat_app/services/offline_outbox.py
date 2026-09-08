"""Encrypted intent queue; never fabricates a cloud identifier or business row."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from ..security.private_payload import read_private_json, write_private_json
from .cloud_client import CloudAPIError
from .cloud_rpc_codec import decode_rpc, encode_rpc

OFFLINE_METHODS = {
    "health": frozenset({"add_life_record", "add_reminder", "save_profile", "update_reminder",
                         "pause_reminder", "resume_reminder"}),
    "service_management": frozenset({"request_appointment"}),
    "preferences": frozenset({"update_preferences"}),
}
APPEND_METHODS = frozenset({"add_life_record", "add_reminder", "request_appointment"})
MAX_QUEUED_OPERATIONS = 200
MAX_OPERATION_BYTES = 256 * 1024
_FORBIDDEN = frozenset({"password", "password_hash", "access_token", "refresh_token", "token",
                        "api_key", "secret", "avatar_path"})


@dataclass(frozen=True)
class QueuedOperation:
    operation_id: str
    service: str
    method: str
    created_at: str
    status: str = "queued"


def content_version(value):
    raw = json.dumps(encode_rpc(value), ensure_ascii=False, sort_keys=True,
                     allow_nan=False, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def canonical_method(service, method):
    return "update_preferences" if service == "preferences" and method == "update" else method


def version_resource(service, method):
    if method in APPEND_METHODS:
        return None
    if service == "preferences":
        return "preferences"
    return "profile" if method == "save_profile" else "reminders"


def _check_private_fields(value):
    if type(value) is dict:
        if any(str(key).lower() in _FORBIDDEN for key in value):
            raise CloudAPIError("validation")
        # An offline queue cannot broaden external data sharing before the
        # server and other clients have acknowledged the explicit decision.
        if any(key.endswith("_consent") and item is True for key, item in value.items()):
            raise CloudAPIError("offline")
        for item in value.values():
            _check_private_fields(item)
    elif type(value) in (list, tuple):
        for item in value:
            _check_private_fields(item)
    elif isinstance(value, bytes):
        raise CloudAPIError("validation")  # File upload queue is a separate future capability.


def queue_payload_allowed(args, kwargs):
    try:
        _check_private_fields(args)
        _check_private_fields(kwargs)
        return True
    except CloudAPIError:
        return False


def validate_intent(service, method, args, kwargs, actor_id, base_version):
    if method not in OFFLINE_METHODS.get(service, ()):
        raise CloudAPIError("offline")
    if not args or type(args[0]) is not int or args[0] != actor_id or type(kwargs) is not dict:
        raise CloudAPIError("permission")
    if method not in APPEND_METHODS and (
        type(base_version) is not str or len(base_version) != 64
        or any(char not in "0123456789abcdef" for char in base_version)
    ):
        raise CloudAPIError("conflict")
    _check_private_fields(args)
    _check_private_fields(kwargs)
    encoded_size = len(json.dumps(encode_rpc([args, kwargs]), ensure_ascii=False).encode())
    if encoded_size > MAX_OPERATION_BYTES:
        raise CloudAPIError("validation")


class OfflineOutbox:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.RLock()

    def _load(self, identity):
        key = identity.cache_key
        value = read_private_json(self.root / (key + ".outbox"), "outbox/" + key)
        if value is None:
            return []
        if value.get("identity") != [identity.endpoint.rstrip("/"), identity.server_instance_id,
                                     identity.actor_id]:
            raise CloudAPIError("permission")
        return value["operations"]

    def _save(self, identity, operations):
        key = identity.cache_key
        write_private_json(self.root / (key + ".outbox"), "outbox/" + key,
                           {"identity": [identity.endpoint.rstrip("/"), identity.server_instance_id,
                                         identity.actor_id], "operations": operations})

    def enqueue(self, identity, service, method, args, kwargs, *, base_version):
        method = canonical_method(service, method)
        validate_intent(service, method, args, kwargs, identity.actor_id, base_version)
        with self._lock:
            rows = self._load(identity)
            if len(rows) >= MAX_QUEUED_OPERATIONS:
                raise CloudAPIError("limit")
            now = datetime.now(UTC).isoformat()
            identifier = str(uuid4())
            rows.append({"operation_id": identifier, "service": service, "method": method,
                         "created_at": now, "status": "queued", "error_code": None,
                         "args": encode_rpc(list(args)), "kwargs": encode_rpc(kwargs),
                         "base_version": base_version})
            self._save(identity, rows)
            return QueuedOperation(identifier, service, method, now)

    def list(self, identity, *, include_payload=False):
        with self._lock:
            rows = self._load(identity)
            if include_payload:
                return [dict(row, args=decode_rpc(row["args"]), kwargs=decode_rpc(row["kwargs"]))
                        for row in rows]
            fields = {"operation_id", "service", "method", "created_at", "status", "error_code"}
            return [{key: value for key, value in row.items() if key in fields} for row in rows]

    def set_status(self, identity, identifier, status, error_code=None):
        if status not in {"queued", "uncertain", "conflict", "rejected", "applied"}:
            raise CloudAPIError("validation")
        with self._lock:
            rows = self._load(identity)
            normalized = str(UUID(identifier))
            target = next((row for row in rows if row["operation_id"] == normalized), None)
            if target is None:
                if status == "applied":
                    return  # Another idempotent replay already removed this acknowledgement.
                raise CloudAPIError("not_found")
            if status == "applied":
                rows.remove(target)
            else:
                target["status"], target["error_code"] = status, error_code
            self._save(identity, rows)

    def discard(self, identity, identifier):
        with self._lock:
            rows = self._load(identity)
            target = next((row for row in rows if row["operation_id"] == identifier), None)
            if target is None:
                raise CloudAPIError("not_found")
            if target["status"] == "uncertain":
                raise CloudAPIError("conflict")
            rows.remove(target)
            self._save(identity, rows)
