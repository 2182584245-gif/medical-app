"""Opt-in acceptance of exactly five imported synthetic demo accounts.

Default execution is a no-network plan. With --run, read the explicitly supplied
DEMO_ACCOUNTS.md privately, authenticate its five allowlisted accounts, and create
then delete exactly one marked record belonging to demo-member-1. Never register,
change preferences, upload, call AI/admin APIs, or clean up by username prefix.
Interrupted writes are retained in a new encrypted receipt; there is no automatic
retry/resume. The operator must inspect that receipt before running again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from ollama_chat_app.security.private_payload import _safe_path
from ollama_chat_app.services.cloud_client import CloudAPIClient
from ollama_chat_app.services.cloud_rpc_codec import encode_rpc
from ollama_chat_app.services.cloud_sync import CloudMirrorStore, SyncIdentity
from ollama_chat_app.services.sqlite_outbox import SqliteOfflineOutbox
from ollama_chat_app.services.sync_protocol import RESOURCE_KINDS, fetch_paged_snapshot

from .public_smoke import (
    _SAFE_ERRORS,
    ENDPOINTS,
    SmokeError,
    _denied,
    _new_directory,
    _now,
    _positive_id,
    _Receipt,
    _require,
    _rpc,
    inspect_receipt,
)

ENDPOINT = ENDPOINTS["aliyun"]
ACCOUNTS = {
    "demo-member-1": "member",
    "demo-member-2": "member",
    "demo-advisor-1": "advisor",
    "demo-advisor-2": "advisor",
    "demo-operator": "operator",
}
CATEGORIES = {"diet", "water", "activity", "sleep", "environment", "medical"}
ROLE_LABELS = {"会员": "member", "顾问": "advisor", "运营": "operator"}


def read_accounts(path):
    """Strict generated-table parser; never prints or returns arbitrary accounts."""
    try:
        path = Path(path)
        _require(path.is_absolute() and path.name == "DEMO_ACCOUNTS.md")
        path = _safe_path(path)
        _require(0 < path.stat().st_size <= 65536)
        result = {}
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip().startswith("|"):
                continue
            fields = [item.strip() for item in line.strip().strip("|").split("|")]
            if fields == ["角色", "昵称", "账号", "初始密码"] or all(
                set(item) <= {"-", ":", " "} for item in fields
            ):
                continue
            _require(len(fields) == 4)
            role, _name, username, password = fields
            _require(username in ACCOUNTS and username not in result)
            _require(ROLE_LABELS.get(role) == ACCOUNTS[username])
            _require(8 <= len(password) <= 256 and not any(ord(c) < 32 for c in password))
            result[username] = password
        _require(set(result) == set(ACCOUNTS))
        return result
    except Exception:
        raise SmokeError("configuration") from None


def _record_hash(records):
    ordered = sorted(records, key=lambda row: row["id"])
    raw = json.dumps(
        encode_rpc(ordered),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _member_snapshot(client, actor_id, expected_instance=None):
    value = fetch_paged_snapshot(client, actor_id)
    _require(value["schema_version"] == 2 and value["complete"] is True)
    _require(set(value["resources"]) == set(RESOURCE_KINDS))
    if expected_instance is not None:
        _require(value["server_instance_id"] == expected_instance)
    records = value["resources"]["life_records"]
    _require({row["category"] for row in records} == CATEGORIES)
    _require(len({row["id"] for row in records}) == len(records))
    for row in records:
        _positive_id(row["id"])
        _require(row["user_id"] == actor_id and isinstance(row["details"], dict))
        if row["category"] == "medical":
            _require(row["details"].get("event_type") in {"consultation", "medication", "other"})
    return value


def _acceptance(clients, receipt, credentials, client_factory):
    for username, role in ACCOUNTS.items():
        client = client_factory(ENDPOINT)
        clients[username] = client
        item = receipt.begin("login_" + username)
        user = client.login(username, credentials[username], remember_offline=False)
        _require(user["username"] == username and user["role_code"] == role)
        _require(client.sync_protocol == 2 and client.is_online_authenticated)
        identifier = _positive_id(user["id"])
        _require(identifier not in {row["id"] for row in receipt.data["accounts"]})
        receipt.data["accounts"].append({"username": username, "role": role, "id": identifier})
        receipt.done(item, result_id=identifier)
    receipt.check("five_exact_demo_identities_login_and_protocol_2")
    actor = receipt.data["accounts"][0]["id"]
    other_actor = receipt.data["accounts"][1]["id"]
    first = clients["demo-member-1"]
    second = client_factory(ENDPOINT)
    clients["demo-member-1-second-session"] = second
    item = receipt.begin("independent_second_login", actor_id=actor)
    user = second.login("demo-member-1", credentials["demo-member-1"], remember_offline=False)
    _require(user["id"] == actor and user["username"] == "demo-member-1")
    _require(user["role_code"] == "member" and second.sync_protocol == 2)
    receipt.done(item)
    initial = _member_snapshot(first, actor)
    instance = initial["server_instance_id"]
    other_initial = _member_snapshot(clients["demo-member-2"], other_actor, instance)
    baseline = initial["resources"]["life_records"]
    baseline_ids = {row["id"] for row in baseline}
    receipt.data["server_instance_id"] = instance
    receipt.data["snapshot_schema_version"] = 2
    receipt.data["baseline_record_counts"] = [
        len(baseline),
        len(other_initial["resources"]["life_records"]),
    ]
    receipt.check("two_members_complete_six_category_snapshot_and_medical_fact_schema")

    # No worker, timer, socket call or offline sign-in here: current authenticated
    # lease authorizes a simulated disconnected mirror/queue recovery stage.
    _require(first.offline_lease is not None)
    identity = SyncIdentity(ENDPOINT, instance, actor)
    cache_root = receipt.directory / "mirror"
    mirror = CloudMirrorStore(cache_root)
    mirror.activate(identity, first.offline_lease)
    mirror.apply_snapshot(initial)
    reopened = CloudMirrorStore(cache_root)
    reopened.authorize_offline(identity, first.offline_lease)
    _require(
        _record_hash(reopened.snapshot()["resources"]["life_records"]) == _record_hash(baseline)
    )
    marker = "v150-demo-acceptance-" + receipt.data["run_id"]
    content = "【合成验收；非真实健康事实】" + marker
    queue = SqliteOfflineOutbox(cache_root)
    queued = queue.enqueue(
        identity,
        "health",
        "add_life_record",
        [actor, "water", _now(), content],
        {"details": {"amount_ml": 1}},
        base_version=None,
    )
    pending = SqliteOfflineOutbox(cache_root).list(identity, include_payload=True)
    _require(len(pending) == 1 and pending[0]["operation_id"] == queued.operation_id)
    receipt.check("encrypted_mirror_and_sqlite_queue_reopened_without_network")
    operation = receipt.begin(
        "create_only_this_record", actor_id=actor, request_id=queued.operation_id
    )
    pending = queue.mark_attempted(identity, queued.operation_id)
    intent = {key: pending[key] for key in ("service", "method", "args", "kwargs", "base_version")}
    identifier = _positive_id(
        _rpc(first, "sync", "apply_queued", actor, intent, request_id=queued.operation_id)
    )
    _require(identifier not in baseline_ids)
    receipt.data["record_ids"].append(identifier)
    receipt.done(operation, result_id=identifier)
    item = receipt.begin("replay_exact_same_uuid", actor_id=actor, request_id=queued.operation_id)
    _require(
        _rpc(second, "sync", "apply_queued", actor, intent, request_id=queued.operation_id)
        == identifier
    )
    receipt.done(item, result_id=identifier)
    changed = _member_snapshot(second, actor, instance)
    rows = changed["resources"]["life_records"]
    added = [row for row in rows if row["id"] == identifier]
    _require(len(added) == 1 and added[0]["content"] == content)
    _require(added[0]["details"]["amount_ml"] == 1 and added[0]["user_id"] == actor)
    _require(
        _record_hash([row for row in rows if row["id"] != identifier]) == _record_hash(baseline)
    )
    queue.set_status(identity, queued.operation_id, "applied")
    reopened.apply_snapshot(changed)
    _require(len(reopened.snapshot()["resources"]["life_records"]) == len(baseline) + 1)
    _require(not SqliteOfflineOutbox(cache_root).list(identity))
    receipt.check("same_uuid_single_record_second_client_visible_and_mirror_updated")
    _denied({403}, _rpc, clients["demo-member-2"], "sync", "get_sync_manifest", actor)
    receipt.check("demo_member_cross_account_snapshot_denied")

    # Sole deletion target: the exact confirmed return ID, absent from baseline,
    # now independently checked to contain our unique marker and expected owner.
    item = receipt.begin("delete_only_returned_id", actor_id=actor, request_id=str(uuid4()))
    _rpc(first, "health", "delete_life_record", actor, identifier, request_id=item["request_id"])
    receipt.done(item, result_id=identifier)
    receipt.data["cleanup"] = "exact_returned_record_id_deleted; verification_pending"
    final = _member_snapshot(second, actor, instance)
    _require(_record_hash(final["resources"]["life_records"]) == _record_hash(baseline))
    final_other = _member_snapshot(clients["demo-member-2"], other_actor, instance)
    _require(
        _record_hash(final_other["resources"]["life_records"])
        == _record_hash(other_initial["resources"]["life_records"])
    )
    reopened.apply_snapshot(final)
    receipt.data["cleanup"] = "exact_returned_record_id_deleted; all_original_records_preserved"
    receipt.check("only_test_record_deleted_both_members_original_records_unchanged")
    for name, client in clients.items():
        item = receipt.begin("logout_" + name)
        client.logout()
        receipt.done(item)
    receipt.check("all_six_test_sessions_logged_out")


def run(
    *,
    accounts_file=None,
    endpoint=ENDPOINT,
    execute=False,
    report_directory=None,
    client_factory=CloudAPIClient,
):
    if endpoint != ENDPOINT or execute is not True:
        raise SmokeError("configuration")
    credentials = read_accounts(accounts_file)
    directory = _new_directory(report_directory)
    receipt = _Receipt(directory, "aliyun", uuid4().hex)
    receipt.data.update(
        {
            "acceptance": "imported-demo-v150",
            "cleanup": "not_yet_performed",
            "limitations": [
                "Staff login only; no staff business snapshot or existing-user lists fetched.",
                "Snapshot schema 2 and medical facts checked; no database DDL/admin introspection.",
                "Offline stage reopens mirror/queue without network, not physical network loss.",
                "No AI, registration, preference update, upload, or automatic failure cleanup.",
                "Login/audit/idempotency metadata remains; only the test record is deleted.",
            ],
        }
    )
    receipt.save()
    clients = {}
    try:
        receipt.data["status"] = "running"
        receipt.save()
        _acceptance(clients, receipt, credentials, client_factory)
        receipt.data["status"] = "passed"
    except BaseException as error:
        receipt.data["status"] = "failed"
        code = getattr(error, "code", "check_failed")
        receipt.data["error_code"] = code if code in _SAFE_ERRORS else "check_failed"
    finally:
        for client in clients.values():
            with suppress(Exception):
                client.close()  # No hidden logout/retry/deletion when the run failed.
        credentials.clear()
        receipt.data["finished_at"] = _now()
        receipt.save()
    return receipt.data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run", action="store_true", help="Explicitly run the bounded public test."
    )
    parser.add_argument("--endpoint", choices=[ENDPOINT], default=ENDPOINT)
    parser.add_argument("--accounts-file", type=Path)
    parser.add_argument("--report-directory", type=Path)
    parser.add_argument("--inspect", type=Path, help="Read only the non-secret encrypted receipt.")
    args = parser.parse_args(argv)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        if args.inspect:
            _require(not args.run and not args.accounts_file and not args.report_directory)
            result = inspect_receipt(args.inspect)
            _require(result.get("acceptance") == "imported-demo-v150")
        elif not args.run:
            result = {
                "status": "not_run",
                "network_requests": 0,
                "endpoint": ENDPOINT,
                "accounts": list(ACCOUNTS),
                "requires": "--run --accounts-file ABSOLUTE_PATH",
            }
        else:
            result = run(
                accounts_file=args.accounts_file,
                endpoint=args.endpoint,
                execute=True,
                report_directory=args.report_directory,
            )
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0 if result["status"] in {"passed", "not_run"} else 1
    except Exception:
        print('{"status":"failed","error_code":"configuration_or_receipt_unavailable"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
