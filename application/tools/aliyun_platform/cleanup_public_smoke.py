"""Explicit, receipt-bound removal of exactly two confirmed public-smoke members.

Never used by the API. No prefix deletes, guessed IDs, credentials in plans,
sequence resets, or cleanup of failed/uncertain receipts. All remaining rows are
verified byte-for-byte in the deletion transaction, including shared rate state.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import psycopg
from PIL import Image
from psycopg import sql

from ollama_chat_app.data.database import DEFAULT_CONVERSATION_TITLE
from ollama_chat_app.services.preferences import DEFAULT_PREFERENCES
from server.platform_experience_schema import PLATFORM_COLUMNS, PLATFORM_SCHEMA
from server.platform_transfer import (
    ENTITY_TYPES,
    FK,
    JSON_IDS,
    PK,
    TransferError,
    _no_secrets,
    _pg_identity,
    catalog_guard,
    digest,
)
from server.platform_transfer_package import safe_path

from .transfer import connect, connect_windows_management, read_json, write_json

ENDPOINTS = {"aliyun": "https://39.106.166.15/aliyun", "supabase": "https://39.106.166.15/supabase"}
RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "route",
        "endpoint",
        "report_directory",
        "started_at",
        "finished_at",
        "status",
        "checks",
        "operations",
        "accounts",
        "record_ids",
        "file_ids",
        "cleanup",
        "limitations",
        "server_instance_id",
    }
)
CHECKS = (
    "ready_and_unauthenticated_denial",
    "register_two_fresh_members",
    "login_and_protocol_2_complete_manifest",
    "encrypted_outbox_same_uuid_exactly_one_record_and_second_client_sync",
    "stale_queue_version_conflict_preserves_cloud_value",
    "file_upload_second_client_download_sha256",
    "protocol_2_file_chunks_sha256_and_reopened_encrypted_cache",
    "cross_account_snapshot_forgery_and_file_access_denied",
    "all_three_synthetic_sessions_logged_out",
)
OPERATIONS = (
    "register_0",
    "register_1",
    "login",
    "login",
    "login",
    "queue_water_record",
    "replay_same_uuid",
    "preference_version_advance",
    "stale_preference_rejected",
    "upload_synthetic_png",
    "logout_own_session",
    "logout_own_session",
    "logout_own_session",
)
KEYS = {
    **PK,
    "platform_sessions": ("token_digest",),
    "rpc_requests": ("user_id", "request_id"),
    "auth_rate_buckets": ("bucket_id",),
}
RELATIONS = {**FK, "platform_sessions": {"user_id": "users"}, "rpc_requests": {"user_id": "users"}}
DELETE_ORDER = (
    "audit_logs",
    "rpc_requests",
    "platform_sessions",
    "user_file_contents",
    "user_files",
    "life_records",
    "user_preferences",
    "conversations",
    "users",
)


def require(condition, message="验收清理预检不匹配；未执行删除。"):
    if not condition:
        raise TransferError(message)


def timestamp(value):
    require(type(value) is str and len(value) <= 64)
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.tzinfo is not None and result.utcoffset() is not None)
    return result.astimezone(UTC)


def validate_receipt(receipt):
    require(type(receipt) is dict and set(receipt) == RECEIPT_FIELDS)
    _no_secrets(receipt)
    require(
        type(receipt["schema_version"]) is int
        and receipt["schema_version"] == 1
        and receipt["status"] == "passed"
    )
    run = receipt["run_id"]
    require(type(run) is str and re.fullmatch(r"[a-f0-9]{32}", run))
    require(receipt["route"] in ENDPOINTS and receipt["endpoint"] == ENDPOINTS[receipt["route"]])
    require(str(UUID(receipt["server_instance_id"])) == receipt["server_instance_id"])
    started, finished = timestamp(receipt["started_at"]), timestamp(receipt["finished_at"])
    require(started <= finished <= datetime.now(UTC) + timedelta(minutes=5))
    require(receipt["checks"] == list(CHECKS))
    require(
        type(receipt["report_directory"]) is str
        and len(receipt["report_directory"]) <= 4096
        and receipt["cleanup"] == "not_performed; exact newly generated identities only"
        and type(receipt["limitations"]) is list
        and len(receipt["limitations"]) <= 10
        and all(type(value) is str and len(value) <= 1000 for value in receipt["limitations"])
    )
    accounts = receipt["accounts"]
    require(type(accounts) is list and len(accounts) == 2)
    for index, suffix in enumerate(("a", "b")):
        account = accounts[index]
        require(type(account) is dict and set(account) == {"username", "id", "registration"})
        require(
            account["username"] == f"public_smoke_{run}_{suffix}"
            and account["registration"] == "confirmed"
            and type(account["id"]) is int
            and 0 < account["id"] < 2**63
        )
    ids = [item["id"] for item in accounts]
    require(len(set(ids)) == 2)
    for name in ("record_ids", "file_ids"):
        values = receipt[name]
        require(
            type(values) is list
            and len(values) == 1
            and type(values[0]) is int
            and 0 < values[0] < 2**63
        )
    operations = receipt["operations"]
    require(type(operations) is list and len(operations) == len(OPERATIONS))
    request_ids = []
    for index, name in enumerate(OPERATIONS):
        item = operations[index]
        expected_fields = {"name", "actor_id", "request_id", "status", "started_at"}
        result = {
            0: ids[0],
            1: ids[1],
            5: receipt["record_ids"][0],
            6: receipt["record_ids"][0],
            9: receipt["file_ids"][0],
        }.get(index)
        if result is not None:
            expected_fields.add("result_id")
        require(type(item) is dict and set(item) == expected_fields)
        require(
            item["name"] == name
            and item["status"] == "confirmed"
            and started <= timestamp(item["started_at"]) <= finished
        )
        if result is not None:
            require(item["result_id"] == result)
        expected_actor = None if index < 2 else ids[1] if index in {4, 12} else ids[0]
        require(item["actor_id"] == expected_actor)
        if index in {5, 6, 7, 8, 9}:
            require(
                type(item["request_id"]) is str
                and str(UUID(item["request_id"])) == item["request_id"]
            )
            request_ids.append(item["request_id"])
        else:
            require(item["request_id"] is None)
    require(request_ids[0] == request_ids[1] and len(set(request_ids)) == 4)
    return receipt


def export_receipt(args):
    # Do not import the GUI/network smoke runner or inspect credentials.dpapi.
    from ollama_chat_app.security.private_payload import read_private_json

    receipt = read_private_json(
        args.run_directory / "receipt.dpapi", "aliyun-public-smoke/v1/receipt"
    )
    validate_receipt(receipt)
    write_json(receipt, args.destination)
    return {
        "status": "receipt_exported",
        "run_id": receipt["run_id"],
        "receipt_sha256": digest(receipt),
        "credentials_read": False,
    }


def key(table, row):
    return tuple(row[column] for column in KEYS[table])


def read_state(connection, *, label, route, deployment_id=None):
    catalog = catalog_guard(connection)
    # An external FK could cascade/SET NULL outside the reviewed platform.
    external = connection.execute(
        "SELECT count(*) FROM pg_constraint k JOIN pg_class src ON src.oid=k.conrelid "
        "JOIN pg_namespace sn ON sn.oid=src.relnamespace JOIN pg_class dst ON dst.oid=k.confrelid "
        "JOIN pg_namespace dn ON dn.oid=dst.relnamespace WHERE k.contype='f' "
        "AND dn.nspname=%s AND sn.nspname<>%s",
        (PLATFORM_SCHEMA, PLATFORM_SCHEMA),
    ).fetchone()[0]
    require(external == 0, "存在平台外部外键，禁止触发未审核的级联清理。")
    rules = connection.execute(
        "SELECT count(*) FROM pg_rewrite r JOIN pg_class c ON c.oid=r.ev_class "
        "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s "
        "AND r.rulename <> '_RETURN'",
        (PLATFORM_SCHEMA,),
    ).fetchone()[0]
    require(rules == 0, "存在未审核重写规则，禁止清理。")
    identity = _pg_identity(connection, label, catalog)
    if route == "aliyun":
        require(type(deployment_id) is str and re.fullmatch(r"[a-f0-9]{32}", deployment_id))
        require(
            identity["deployment_marker"] == "medical-app:aliyun:v1:" + deployment_id,
            "清理目标阿里云部署身份与确认值不匹配。",
        )
    rows, total, raw_bytes = {}, 0, 0
    for table, columns in PLATFORM_COLUMNS.items():
        cursor = connection.execute(
            sql.SQL("SELECT {} FROM {}.{} LIMIT 100001").format(
                sql.SQL(",").join(map(sql.Identifier, columns)),
                sql.Identifier(PLATFORM_SCHEMA),
                sql.Identifier(table),
            )
        )
        values = []
        for item in cursor:
            row = {
                name: str(value)
                if isinstance(value, UUID)
                else bytes(value)
                if isinstance(value, memoryview)
                else value
                for name, value in zip(columns, item, strict=True)
            }
            total += 1
            require(total <= 100000, "清理核验超过十万行安全上限；未执行删除。")
            raw_bytes += sum(
                len(value.encode("utf-8"))
                if isinstance(value, str)
                else len(value)
                if isinstance(value, bytes)
                else 8
                for value in row.values()
            )
            require(raw_bytes <= 128 * 1024 * 1024, "清理核验超过 128 MiB 安全上限；未执行删除。")
            values.append(row)
        rows[table] = sorted(values, key=lambda row, table=table: key(table, row))
    return {"identity": identity, "rows": rows}


def selection(state, receipt):
    rows = state["rows"]
    wanted = {item["id"]: item["username"] for item in receipt["accounts"]}
    actor = receipt["accounts"][0]["id"]
    chosen = {table: set() for table in rows}
    chosen["users"] = {(identifier,) for identifier in wanted}
    users = [row for row in rows["users"] if key("users", row) in chosen["users"]]
    require(len(users) == 2)
    begin, end = timestamp(receipt["started_at"]), timestamp(receipt["finished_at"])
    for row in users:
        require(
            row["username"] == row["username_normalized"] == wanted[row["id"]]
            and row["role_code"] == "member"
            and row["account_status"] == "active"
            and begin <= timestamp(row["created_at"]) <= end
        )
    changed = True
    while changed:
        changed = False
        for table, columns in RELATIONS.items():
            for row in rows[table]:
                if key(table, row) not in chosen[table] and any(
                    (row[column],) in chosen[target] for column, target in columns.items()
                ):
                    chosen[table].add(key(table, row))
                    changed = True
    require(
        not any(values for table, values in chosen.items() if table not in DELETE_ORDER),
        "验收账号存在未预期的会员/顾问/订单/资料等业务关联，禁止清理。",
    )
    selected = {
        table: [row for row in items if key(table, row) in chosen[table]]
        for table, items in rows.items()
    }
    expected_counts = {
        "users": 2,
        "conversations": 2,
        "life_records": 1,
        "user_files": 1,
        "user_file_contents": 1,
        "user_preferences": 1,
        "audit_logs": 2,
        "platform_sessions": 3,
        "rpc_requests": 3,
    }
    require(
        {table: len(items) for table, items in selected.items() if items} == expected_counts,
        "关联行数量不符合已通过验收的固定流程，禁止扩大删除范围。",
    )
    for items in selected.values():
        for row in items:
            if "created_at" in row:
                require(begin <= timestamp(row["created_at"]) <= end)
    require(
        chosen["life_records"] == {(receipt["record_ids"][0],)}
        and chosen["user_files"] == {(receipt["file_ids"][0],)}
    )
    record = selected["life_records"][0]
    require(
        record["user_id"] == actor
        and record["category"] == "water"
        and record["content"] == "合成公网验收饮水记录（非真实健康数据）"
        and json.loads(record["details_json"]) == {"amount_ml": 1}
    )
    for row in selected["conversations"]:
        require(
            row["title"] == DEFAULT_CONVERSATION_TITLE
            and row["provider"] is None
            and row["model"] is None
        )
    require({row["user_id"] for row in selected["conversations"]} == set(wanted))
    prefs = selected["user_preferences"][0]
    require(
        prefs["user_id"] == actor
        and json.loads(prefs["preferences_json"])
        == {**DEFAULT_PREFERENCES, "nickname": "合成验收昵称一"}
    )
    file, content = selected["user_files"][0], selected["user_file_contents"][0]
    require(
        file["user_id"] == actor
        and file["original_name"] == "synthetic-smoke.png"
        and file["media_type"] == content["mime_type"] == "image/png"
        and content["file_id"] == file["id"]
        and 65536 < len(content["content"]) == file["size_bytes"] < 100000
        and file["sha256"] == content["sha256"] == hashlib.sha256(content["content"]).hexdigest()
    )
    with Image.open(io.BytesIO(content["content"])) as picture:
        require(picture.format == "PNG" and picture.size == (192, 128))
        picture.verify()
    expected_audits = {
        ("life_record.created", "life_record", record["id"]),
        ("user_file.uploaded", "user_file", file["id"]),
    }
    require(
        {(row["action"], row["entity_type"], row["entity_id"]) for row in selected["audit_logs"]}
        == expected_audits
    )
    require(
        all(
            row["actor_user_id"] == actor and row["details_json"] is None
            for row in selected["audit_logs"]
        )
    )
    sessions = selected["platform_sessions"]
    require(
        sum(row["user_id"] == actor for row in sessions) == 2
        and all(
            row["revoked_at"] is not None and begin <= timestamp(row["revoked_at"]) <= end
            for row in sessions
        )
    )
    operations = receipt["operations"]
    expected_requests = {operations[index]["request_id"] for index in (5, 7, 9)}
    require(
        {row["request_id"] for row in selected["rpc_requests"]} == expected_requests
        and all(row["user_id"] == actor for row in selected["rpc_requests"])
    )
    # Every outbound/inbound FK is in the selected closure; non-FK audit and JSON
    # references must also not strand another account's retained private history.
    selected_integers = {
        value[0]
        for identities in chosen.values()
        for value in identities
        if len(value) == 1 and type(value[0]) is int
    }

    def json_reference(value):
        if isinstance(value, dict):
            for field, item in value.items():
                target = JSON_IDS.get(field)
                candidates = item if type(item) is list else [item]
                if target and any(
                    type(number) is int and (number,) in chosen[target] for number in candidates
                ):
                    return True
                if (
                    not target
                    and (field.endswith("_id") or field.endswith("_ids"))
                    and any(
                        type(number) is int and number in selected_integers for number in candidates
                    )
                ):
                    return True
                if json_reference(item):
                    return True
        elif isinstance(value, list):
            return any(json_reference(item) for item in value)
        return False

    for table, items in rows.items():
        for row in items:
            if key(table, row) in chosen[table]:
                continue
            if table == "audit_logs" and row["entity_id"] is not None:
                target = ENTITY_TYPES.get(row["entity_type"])
                require(
                    not (
                        (target and (row["entity_id"],) in chosen[target])
                        or (not target and row["entity_id"] in selected_integers)
                    ),
                    "其他操作者的审计记录引用验收对象，禁止删除其历史。",
                )
            for name, value in row.items():
                if name.endswith("_json") and value:
                    require(
                        not json_reference(json.loads(value)),
                        "保留行含验收对象的结构化引用，禁止留下悬空历史。",
                    )
    return selected, chosen


def build_plan(state, receipt):
    validate_receipt(receipt)
    selected, _chosen = selection(state, receipt)
    identifiers = {}
    for table, items in selected.items():
        if not items:
            continue
        identifiers[table] = (
            [
                {
                    "user_id": row["user_id"],
                    "created_at": row["created_at"],
                    "row_sha256": digest(row),
                }
                for row in items
            ]
            if table == "platform_sessions"
            else [list(key(table, row)) for row in items]
        )
    plan = {
        "version": 1,
        "operation": "delete-confirmed-public-smoke-only",
        "status": "review_required",
        "run_id": receipt["run_id"],
        "route": receipt["route"],
        "target": state["identity"],
        "receipt_sha256": digest(receipt),
        "before_sha256": digest(state),
        "delete_ids": identifiers,
        "delete_counts": {table: len(items) for table, items in selected.items()},
        "all_table_counts": {table: len(items) for table, items in state["rows"].items()},
        "other_rows_preserved": True,
        "shared_rate_buckets_modified": False,
        "sequences_modified": False,
        "credentials_in_report": False,
    }
    plan["plan_sha256"] = digest(plan)
    return plan


def prepare(connection, receipt, *, label, deployment_id=None):
    validate_receipt(receipt)
    require(connection.info.transaction_status == psycopg.pq.TransactionStatus.IDLE)
    with connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        connection.execute("SET LOCAL statement_timeout='30s'")
        return build_plan(
            read_state(
                connection, label=label, route=receipt["route"], deployment_id=deployment_id
            ),
            receipt,
        )


def apply(connection, receipt, plan, *, confirm_sha256, label, deployment_id=None):
    validate_receipt(receipt)
    require(confirm_sha256 == plan.get("plan_sha256"), "必须明确确认完整计划 SHA256。")
    require(connection.info.transaction_status == psycopg.pq.TransactionStatus.IDLE)
    with connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        connection.execute("SET LOCAL lock_timeout='5s'")
        connection.execute("SET LOCAL statement_timeout='30s'")
        connection.execute(
            sql.SQL("LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE").format(
                sql.SQL(",").join(
                    sql.Identifier(PLATFORM_SCHEMA, table) for table in sorted(PLATFORM_COLUMNS)
                )
            )
        )
        before = read_state(
            connection, label=label, route=receipt["route"], deployment_id=deployment_id
        )
        require(build_plan(before, receipt) == plan, "目标、回执或行指纹已改变；需重新只读预检。")
        selected, chosen = selection(before, receipt)
        for table in DELETE_ORDER:
            statement = sql.SQL("DELETE FROM {}.{} WHERE {}").format(
                sql.Identifier(PLATFORM_SCHEMA),
                sql.Identifier(table),
                sql.SQL(" AND ").join(
                    sql.SQL("{}=%s").format(sql.Identifier(column)) for column in KEYS[table]
                ),
            )
            for row in selected[table]:
                require(connection.execute(statement, key(table, row)).rowcount == 1)
        after = read_state(
            connection, label=label, route=receipt["route"], deployment_id=deployment_id
        )
        expected = {
            "identity": before["identity"],
            "rows": {
                table: [row for row in items if key(table, row) not in chosen[table]]
                for table, items in before["rows"].items()
            },
        }
        require(digest(after) == digest(expected), "保留行或清理结果校验失败，整笔回滚。")
    return {
        "status": "cleanup_committed",
        "run_id": receipt["run_id"],
        "plan_sha256": plan["plan_sha256"],
        "removed_counts": plan["delete_counts"],
        "other_rows_byte_identical": True,
        "shared_rate_buckets_modified": False,
        "sequences_modified": False,
        "automatic_retry_performed": False,
    }


@contextmanager
def target_connection(args, receipt):
    if receipt["route"] == "supabase":
        require(args.management_profile is not None and args.target_profile is None)
        with connect_windows_management(args.management_profile) as connection:
            yield connection, "confirmed-supabase-public-smoke-cleanup"
    else:
        require(args.target_profile is not None and args.management_profile is None)
        profile = read_json(args.target_profile, limit=16384)
        require(profile["kind"] == "aliyun")
        with connect(args.target_profile) as context:
            yield context


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    exporter = commands.add_parser("export-receipt")
    exporter.add_argument("--run-directory", type=Path, required=True)
    exporter.add_argument("--destination", type=Path, required=True)
    for name in ("plan", "apply"):
        command = commands.add_parser(name)
        command.add_argument("--receipt", type=Path, required=True)
        command.add_argument("--plan-file", type=Path, required=True)
        target = command.add_mutually_exclusive_group(required=True)
        target.add_argument("--management-profile", type=Path)
        target.add_argument("--target-profile", type=Path)
        command.add_argument("--deployment-id")
        if name == "apply":
            command.add_argument("--confirm-plan", required=True)
            command.add_argument("--result-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "export-receipt":
            result = export_receipt(args)
        else:
            receipt = validate_receipt(read_json(args.receipt, limit=65536))
            # Fail before connecting/deleting if a report destination is already
            # occupied; an uncertain commit never triggers an automatic retry.
            safe_path(args.plan_file if args.action == "plan" else args.result_file, exists=False)
            with target_connection(args, receipt) as (connection, label):
                if args.action == "plan":
                    result = prepare(
                        connection, receipt, label=label, deployment_id=args.deployment_id
                    )
                    write_json(result, args.plan_file)
                else:
                    result = apply(
                        connection,
                        receipt,
                        read_json(args.plan_file, limit=65536),
                        confirm_sha256=args.confirm_plan,
                        label=label,
                        deployment_id=args.deployment_id,
                    )
                    write_json(result, args.result_file)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as error:
        result = {"status": "cleanup_not_verified", "automatic_retry_performed": False}
        if isinstance(error, TransferError):
            result["reason"] = str(error)  # TransferError contains fixed messages only.
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
