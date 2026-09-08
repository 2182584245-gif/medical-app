"""Explicit, fixed-target synthetic HTTPS acceptance; never automatic cleanup.

Run with the desktop Python, from the application directory::

    .venv/Scripts/python.exe -m tools.aliyun_platform.public_smoke --route aliyun --confirm

Each invocation creates a NEW current-user DPAPI receipt and credential directory.
An interrupted registration is not retried, resumed, or assumed to have failed.
Inspect the retained, non-secret receipt with ``--inspect ABSOLUTE_RUN_DIRECTORY``
before deciding whether another run is appropriate. Exact usernames, known IDs and
RPC UUIDs support separately authorized cleanup; this tool performs no deletion.
No existing account credentials, local health database, AI, or admin APIs are used.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import secrets
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from PIL import Image

from ollama_chat_app.security.private_payload import (
    _safe_path,
    read_private_json,
    write_private_json,
)
from ollama_chat_app.services.cloud_client import CloudAPIClient, CloudAPIError
from ollama_chat_app.services.cloud_rpc_codec import decode_rpc, encode_rpc
from ollama_chat_app.services.cloud_sync import SyncIdentity
from ollama_chat_app.services.offline_outbox import OfflineOutbox, content_version
from ollama_chat_app.services.sync_files import SyncFileCache
from ollama_chat_app.services.sync_protocol import fetch_paged_snapshot

ENDPOINTS = {
    "aliyun": "https://39.106.166.15/aliyun",
    "supabase": "https://39.106.166.15/supabase",
}
SOURCE = Path(__file__).resolve().parents[2]
_PURPOSE = "aliyun-public-smoke/v1/"
_SAFE_ERRORS = frozenset({
    "configuration", "authentication", "permission", "not_found", "conflict", "validation",
    "limited", "unavailable", "timeout", "network", "redirect", "protocol", "limit",
    "closed", "not_authenticated", "offline", "check_failed", "local_receipt_failed",
})


class SmokeError(RuntimeError):
    def __init__(self, code="check_failed"):
        self.code = code if code in _SAFE_ERRORS else "check_failed"
        super().__init__(self.code)  # Never expose arbitrary HTTP/OS exception text.


def _require(condition):
    if not condition:
        raise SmokeError()


def _now():
    return datetime.now(UTC).isoformat()


def _new_directory(directory):
    if directory is None:
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            raise SmokeError("configuration")
        directory = Path(base) / "HealthLifePublicAcceptance" / uuid4().hex
    path = Path(directory)
    # New-only, no repository artifacts, relative/UNC/linked directories or overwrite.
    if not path.is_absolute() or path == SOURCE or SOURCE in path.parents:
        raise SmokeError("configuration")
    try:
        path = _safe_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path = _safe_path(path)
        path.mkdir(mode=0o700, exist_ok=False)
        return _safe_path(path)
    except Exception:
        raise SmokeError("configuration") from None


def inspect_receipt(directory):
    """Read only metadata, never the encrypted password bundle or queue payload."""
    try:
        result = read_private_json(Path(directory) / "receipt.dpapi", _PURPOSE + "receipt")
        if not isinstance(result, dict) or result.get("schema_version") != 1:
            raise ValueError
        return result
    except Exception:
        raise SmokeError("local_receipt_failed") from None


class _Receipt:
    def __init__(self, directory, route, marker):
        self.directory = directory
        self.data = {
            "schema_version": 1, "run_id": marker, "route": route,
            "endpoint": ENDPOINTS[route], "report_directory": str(directory),
            "started_at": _now(), "status": "prepared", "checks": [], "operations": [],
            "accounts": [], "record_ids": [], "file_ids": [],
            "cleanup": "not_performed; exact newly generated identities only",
            "limitations": [
                "No real AI calls or existing-user data are tested.",
                "Queue persistence/replay is tested; offline login, lease expiry and Qt UI "
                "remain covered by isolated automated tests, not this public run.",
                "An attempted registration with no ID requires exact-username resolution; "
                "never automatically register again or delete by prefix.",
            ],
        }

    def save(self):
        try:
            write_private_json(self.directory / "receipt.dpapi", _PURPOSE + "receipt", self.data)
        except Exception:
            raise SmokeError("local_receipt_failed") from None

    def begin(self, name, *, actor_id=None, request_id=None):
        item = {"name": name, "actor_id": actor_id, "request_id": request_id,
                "status": "attempted_result_unconfirmed", "started_at": _now()}
        self.data["operations"].append(item)
        self.save()  # Durable BEFORE network; failure here prevents the mutation.
        return item

    def done(self, item, *, result_id=None):
        item["status"] = "confirmed"
        if result_id is not None:
            item["result_id"] = result_id
        self.save()

    def check(self, name):
        self.data["checks"].append(name)
        self.save()


def _rpc(client, service, method, *args, request_id=None, **kwargs):
    # This closed helper is not an arbitrary endpoint or admin dispatch interface.
    return decode_rpc(client.rpc(service, method, encode_rpc(list(args)), encode_rpc(kwargs),
                                 request_id=request_id))


def _denied(statuses, function, *args, **kwargs):
    try:
        function(*args, **kwargs)
    except CloudAPIError as error:
        _require(error.status_code in statuses)
    else:
        raise SmokeError()


def _positive_id(value):
    _require(type(value) is int and value > 0)
    return value


def _register(client, receipt, credentials, index):
    account = receipt.data["accounts"][index]
    operation = receipt.begin("register_" + str(index))
    account["registration"] = "attempted_result_unconfirmed"
    receipt.save()
    user = client.register(account["username"], credentials[index]["password"])
    # Random collision or ambiguous response is not permission to use an existing account.
    _require(user["username"] == account["username"] and user["role_code"] == "member")
    account["id"] = _positive_id(user["id"])
    account["registration"] = "confirmed"
    receipt.done(operation, result_id=user["id"])
    return user["id"]


def _checks(clients, receipt, credentials):
    first, second, other = clients
    _require(first.request("GET", "/health/ready", authenticated=False)["status"] == "ready")
    _denied({401}, first.request, "GET", "/v1/auth/me", authenticated=False)
    receipt.check("ready_and_unauthenticated_denial")
    actor = _register(first, receipt, credentials, 0)
    outsider = _register(other, receipt, credentials, 1)
    _require(actor != outsider)
    receipt.check("register_two_fresh_members")
    for index, client, expected in ((0, first, actor), (0, second, actor), (1, other, outsider)):
        operation = receipt.begin("login", actor_id=expected)
        user = client.login(credentials[index]["username"], credentials[index]["password"])
        _require(user["id"] == expected and user["role_code"] == "member")
        _require(client.sync_protocol == 2)
        receipt.done(operation)
    initial = fetch_paged_snapshot(first, actor)
    _require(initial["complete"] and not initial["resources"]["life_records"])
    receipt.data["server_instance_id"] = initial["server_instance_id"]
    receipt.check("login_and_protocol_2_complete_manifest")

    # Enqueue locally WITHOUT contacting the server, then reopen the DPAPI queue.
    identity = SyncIdentity(first.base_url, initial["server_instance_id"], actor)
    outbox = OfflineOutbox(receipt.directory / "outbox")
    queued = outbox.enqueue(identity, "health", "add_life_record",
                            [actor, "water", _now(), "合成公网验收饮水记录（非真实健康数据）"],
                            {"details": {"amount_ml": 1}}, base_version=None)
    item = OfflineOutbox(outbox.root).list(identity, include_payload=True)[0]
    _require(item["operation_id"] == queued.operation_id)
    intent = {key: item[key] for key in ("service", "method", "args", "kwargs", "base_version")}
    operation = receipt.begin("queue_water_record", actor_id=actor, request_id=queued.operation_id)
    record_id = _positive_id(_rpc(first, "sync", "apply_queued", actor, intent,
                                  request_id=queued.operation_id))
    receipt.data["record_ids"].append(record_id)
    receipt.done(operation, result_id=record_id)
    # Deliberate duplicate replay, never a new UUID. This is not an automatic error retry.
    operation = receipt.begin("replay_same_uuid", actor_id=actor, request_id=queued.operation_id)
    _require(_rpc(second, "sync", "apply_queued", actor, intent,
                  request_id=queued.operation_id) == record_id)
    receipt.done(operation, result_id=record_id)
    outbox.set_status(identity, queued.operation_id, "applied")
    snapshot = fetch_paged_snapshot(second, actor)
    records = snapshot["resources"]["life_records"]
    _require(len(records) == 1 and records[0]["id"] == record_id)
    _require(records[0]["category"] == "water" and records[0]["details"]["amount_ml"] == 1)
    _require(snapshot["server_instance_id"] == initial["server_instance_id"])
    receipt.check("encrypted_outbox_same_uuid_exactly_one_record_and_second_client_sync")

    # A real old version, not a fabricated hash: first write succeeds; stale write must fail.
    old = content_version(snapshot["resources"]["preferences"])
    pref_intent = {"service": "preferences", "method": "update_preferences",
                   "args": [actor, {"nickname": "合成验收昵称一"}], "kwargs": {},
                   "base_version": old}
    request_id = str(uuid4())
    operation = receipt.begin("preference_version_advance", actor_id=actor, request_id=request_id)
    _rpc(first, "sync", "apply_queued", actor, pref_intent, request_id=request_id)
    receipt.done(operation)
    pref_intent["args"] = [actor, {"nickname": "合成验收昵称二"}]
    request_id = str(uuid4())
    operation = receipt.begin("stale_preference_rejected", actor_id=actor, request_id=request_id)
    _denied({409}, _rpc, second, "sync", "apply_queued", actor, pref_intent,
            request_id=request_id)
    receipt.done(operation)
    current = fetch_paged_snapshot(second, actor)
    _require(current["resources"]["preferences"]["nickname"] == "合成验收昵称一")
    receipt.check("stale_queue_version_conflict_preserves_cloud_value")

    picture = io.BytesIO()
    # Random synthetic pixels avoid compressing below 64 KiB: exercise TWO chunk offsets.
    Image.frombytes("RGB", (192, 128), secrets.token_bytes(192 * 128 * 3)).save(
        picture, format="PNG")
    payload = picture.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    request_id = str(uuid4())
    operation = receipt.begin("upload_synthetic_png", actor_id=actor, request_id=request_id)
    file_id = _positive_id(_rpc(first, "files", "upload_bytes", actor, "synthetic-smoke.png",
                                payload, request_id=request_id))
    receipt.data["file_ids"].append(file_id)
    receipt.done(operation, result_id=file_id)
    downloaded = _rpc(second, "files", "get_file", actor, file_id)
    _require(hashlib.sha256(downloaded["content"]).hexdigest() == digest)
    _require(downloaded["sha256"] == digest)
    receipt.check("file_upload_second_client_download_sha256")
    with_file = fetch_paged_snapshot(second, actor)
    metadata = with_file["resources"]["user_files"]
    _require(len(metadata) == 1 and metadata[0]["id"] == file_id)
    _require(metadata[0]["sha256"] == digest and metadata[0]["size_bytes"] == len(payload))
    file_cache = SyncFileCache(receipt.directory / "attachments")
    # Supply only the one verified synthetic file, not any unexpected server attachment.
    file_cache.fetch(second, identity, {"user_files": metadata, "chat_attachments": []})
    cached = SyncFileCache(file_cache.root).read(identity, "user_files", metadata[0])
    _require(cached == payload)
    receipt.check("protocol_2_file_chunks_sha256_and_reopened_encrypted_cache")

    foreign = fetch_paged_snapshot(other, outsider)
    _require(not foreign["resources"]["life_records"] and not foreign["resources"]["user_files"])
    _denied({403}, _rpc, other, "health", "get_profile", actor)
    _denied({403}, _rpc, other, "sync", "get_sync_manifest", actor)
    _denied({404, 422}, _rpc, other, "files", "get_file", outsider, file_id)
    _denied({404}, _rpc, other, "sync", "get_file_chunk", outsider,
            "user_files", file_id, digest, 0)
    receipt.check("cross_account_snapshot_forgery_and_file_access_denied")
    for client, user_id in ((first, actor), (second, actor), (other, outsider)):
        operation = receipt.begin("logout_own_session", actor_id=user_id)
        client.logout()
        receipt.done(operation)
    receipt.check("all_three_synthetic_sessions_logged_out")


def run(*, route, confirm=False, report_directory=None, client_factory=CloudAPIClient):
    """Test injection is API-only; CLI has no custom transport/URL/TLS override."""
    if confirm is not True or route not in ENDPOINTS:
        raise SmokeError("configuration")
    directory = _new_directory(report_directory)
    marker = uuid4().hex
    credentials = [{"username": f"public_smoke_{marker}_{suffix}",
                    "password": secrets.token_urlsafe(36)} for suffix in ("a", "b")]
    receipt = _Receipt(directory, route, marker)
    receipt.data["accounts"] = [{"username": item["username"], "id": None,
                                 "registration": "not_attempted"} for item in credentials]
    receipt.save()
    clients = []
    try:
        # Passwords only in this current-user encrypted file; tokens remain memory-only.
        write_private_json(directory / "credentials.dpapi", _PURPOSE + "credentials",
                           {"endpoint": ENDPOINTS[route], "accounts": credentials})
        for _index in range(3):
            clients.append(client_factory(ENDPOINTS[route]))
        receipt.data["status"] = "running"
        receipt.save()
        _checks(clients, receipt, credentials)
        receipt.data["status"] = "passed"
    except BaseException as error:
        # Includes Ctrl-C: preserve the prior attempted/confirmed operations, no registration retry.
        receipt.data["status"] = "failed"
        code = getattr(error, "code", "check_failed")
        receipt.data["error_code"] = code if code in _SAFE_ERRORS else "check_failed"
    finally:
        for client in clients:
            with suppress(Exception):
                client.close()  # Clears memory only; never hidden cleanup/retry network traffic.
        credentials.clear()
        receipt.data["finished_at"] = _now()
        receipt.save()
    return receipt.data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route", choices=tuple(ENDPOINTS))
    parser.add_argument("--confirm", action="store_true",
                        help="Explicitly create two new synthetic members on the selected route.")
    parser.add_argument("--report-directory", type=Path,
                        help="New absolute directory outside source.")
    parser.add_argument("--inspect", type=Path, help="Read the non-secret receipt; no network.")
    args = parser.parse_args(argv)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        if args.inspect:
            if args.route or args.confirm or args.report_directory:
                raise SmokeError("configuration")
            result = inspect_receipt(args.inspect)
        else:
            result = run(route=args.route, confirm=args.confirm,
                         report_directory=args.report_directory)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0 if result["status"] == "passed" else 1
    except Exception:
        print('{"status":"failed","error_code":"configuration_or_receipt_unavailable"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
