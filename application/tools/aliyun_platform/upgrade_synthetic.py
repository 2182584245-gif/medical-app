"""Administrator-only, transactional application of one reviewed fictional batch.

Never imported by the public API. No guessed identities, credential resets,
schema changes, delete operations, arbitrary SQL or source-selected table names.
Artifacts must be outside the repository/public bundles, root-owned and private.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

from psycopg import sql
from psycopg.pq import TransactionStatus

from server import platform_transfer as transfer
from server.platform_developer_schema import NEW_TABLES
from server.platform_experience_schema import PLATFORM_SCHEMA, PLATFORM_TABLES
from server.platform_transfer_assets import attach_assets, references
from tools import plan_synthetic_upgrade_v170 as planner

from . import bootstrap
from .guard import ADMIN, MARKER, container_gate, secret
from .upgrade_medical import safe_file, write_report

ALL_TABLES = tuple(sorted((*PLATFORM_TABLES, *NEW_TABLES)))
PROTECTED_TABLES = frozenset(
    {
        "users",
        "platform_sessions",
        "auth_rate_buckets",
        "rpc_requests",
        *NEW_TABLES,
    }
)
INSERT_TABLES = frozenset(
    {
        "life_records",
        "reminders",
        "visit_tasks",
        "visit_records",
        "visit_task_details",
        "messages",
        "conversations",
        "user_preferences",
    }
)
# Deliberately independent of the planner: future planner expansion is not an
# implicit authorization to expand this administrator writer.
PATCH_FIELDS = {
    "member_profiles": {
        "display_name",
        "birth_date",
        "gender",
        "phone",
        "living_situation",
        "emergency_contact_name",
        "emergency_contact_phone",
        "ai_preferred_name",
        "reminder_frequency",
        "height_cm",
        "health_goals",
        "dietary_preferences",
        "medical_notes",
    },
    "advisor_profiles": {"display_name", "organization", "specialty", "bio"},
    "products": {"name", "brand", "description"},
    "product_recommendations": {"reason"},
    "orders": {"product_name_snapshot"},
    "memberships": {"plan_code", "status", "starts_at", "ends_at", "benefits_json"},
    "staff_account_terms": {"starts_at", "ends_at"},
    "advisor_bindings": {"status", "started_at", "ended_at"},
    "user_preferences": set(),
}
SAFE_FAILURE = (
    "Synthetic upgrade not committed or not verified; no raw records or credentials shown."
)


class UpgradeError(RuntimeError):
    """Fixed diagnostics only: callers must not print database exceptions."""


def require(condition, message):
    if not condition:
        raise UpgradeError(message)


def _confirm(plan, confirmation):
    require(isinstance(plan, dict), "A reviewed plan object is required.")
    core = copy.deepcopy(plan)
    supplied = core.pop("plan_sha256", None)
    require(
        isinstance(confirmation, str)
        and re.fullmatch(r"[a-f0-9]{64}", confirmation)
        and supplied == confirmation == transfer.digest(core),
        "The complete unmodified plan SHA256 must be explicitly confirmed.",
    )
    require(
        plan.get("operation") == "upgrade-reviewed-fictional-data-preserve-identities"
        and plan.get("batch") == planner.BATCH
        and plan.get("status") in {"review_required", "already_applied"},
        "Only the reviewed fictional upgrade operation is supported.",
    )


def _check_target(connection, deployment):
    bootstrap.check_target(connection, ADMIN, deployment)
    bootstrap.verify_installation(connection, deployment, revision="platform_0004")
    require(
        connection.execute(
            "SELECT count(*) FROM pg_event_trigger WHERE evtenabled<>'D'"
        ).fetchone()[0]
        == 0,
        "Unknown DDL triggers prohibit this operation.",
    )


def _snapshot(connection, label, asset_root):
    catalog = transfer.catalog_guard(connection)
    rows = transfer._pg_rows(connection)
    transfer.validate_rows(rows)
    return attach_assets(
        transfer.snapshot(rows, transfer._pg_identity(connection, label, catalog)), asset_root
    )


def _protected_fingerprint(connection):
    result = {}
    total_bytes, total_rows = 0, 0
    for table in sorted(PROTECTED_TABLES):
        checksum = hashlib.sha256()
        count = 0
        for row in connection.execute(
            sql.SQL(
                "SELECT to_jsonb(t)::text FROM {} t ORDER BY to_jsonb(t)::text LIMIT 100001"
            ).format(sql.Identifier(PLATFORM_SCHEMA, table))
        ):
            raw = row[0].encode()
            total_bytes += len(raw)
            total_rows += 1
            require(
                total_rows <= 100000 and total_bytes <= 128 * 1024 * 1024,
                "Protected-state verification exceeds the maintenance bound.",
            )
            checksum.update(len(raw).to_bytes(8, "big") + raw)
            count += 1
        result[table] = {"rows": count, "sha256": checksum.hexdigest()}
    return result


def merge_json(current, delta, *, depth=0):
    require(
        isinstance(current, dict) and isinstance(delta, dict) and depth <= 20,
        "Only bounded JSON object leaf merges are allowed.",
    )
    for key, wanted in delta.items():
        require(isinstance(key, str), "JSON field names must be text.")
        if isinstance(wanted, dict) and isinstance(current.get(key), dict):
            merge_json(current[key], wanted, depth=depth + 1)
        else:
            current[key] = copy.deepcopy(wanted)


def expected_rows(target, plan):
    """Independent write allowlist plus expected final full business snapshot."""
    rows = copy.deepcopy(target["rows"])
    require(set(plan["inserts"]) <= INSERT_TABLES, "An unapproved insert table was requested.")
    demo_user_ids = set(plan["id_mapping"]["users"].values())
    demo_advisors = {
        row["id"]
        for row in rows["users"]
        if row["username"] in {"demo-advisor-1", "demo-advisor-2"}
    }
    for table, additions in plan["inserts"].items():
        for row in additions:
            require(
                set(row) == set(transfer.BUSINESS_COLUMNS[table]),
                "Insert columns differ from the fixed business contract.",
            )
            for field, related in transfer.FK.get(table, {}).items():
                if related == "users" and row[field] is not None:
                    require(row[field] in demo_user_ids, "Insert targets a non-fictional account.")
        rows[table].extend(copy.deepcopy(additions))
    for patch in plan["patches"]:
        table = patch["table"]
        require(
            table in PATCH_FIELDS and set(patch["changes"]) <= PATCH_FIELDS[table] | {"updated_at"},
            "An unapproved patch field or table was requested.",
        )
        require(
            patch["changes"].get("updated_at") == plan["prepared_at"],
            "Patch time must match the reviewed plan.",
        )
        matches = [
            row
            for row in rows[table]
            if [row[column] for column in transfer.PK[table]] == patch["target_pk"]
        ]
        require(
            len(matches) == 1 and transfer.digest(matches[0]) == patch["before_sha256"],
            "A patch row changed or is missing.",
        )
        row = matches[0]
        for field, related in transfer.FK.get(table, {}).items():
            if related == "users" and row[field] is not None:
                require(row[field] in demo_user_ids, "Patch targets a non-fictional account.")
        if table == "staff_account_terms":
            require(
                row["user_id"] in demo_advisors,
                "Only the two historically verified fictional advisors may be extended.",
            )
        row.update(copy.deepcopy(patch["changes"]))
        if "json_merge" in patch:
            require(
                table == "user_preferences" and set(patch["json_merge"]) == {"preferences_json"},
                "Only preferences permit a JSON leaf merge.",
            )
            current = json.loads(row["preferences_json"])
            merge_json(current, patch["json_merge"]["preferences_json"])
            row["preferences_json"] = json.dumps(current, ensure_ascii=False, separators=(",", ":"))
    ledger = plan["ledger_insert"]
    require(
        set(ledger) == set(transfer.BUSINESS_COLUMNS["app_settings"])
        and ledger["user_id"] is None
        and ledger["setting_key"] == planner.LEDGER_KEY
        and ledger["created_at"] == ledger["updated_at"] == plan["prepared_at"],
        "The atomic ledger differs from the reviewed fixed key.",
    )
    rows["app_settings"].append(copy.deepcopy(ledger))
    transfer.validate_rows(rows)
    require(
        references({"rows": rows}) == references(target),
        "This update cannot add, remove or change external asset references.",
    )
    return transfer._sort(rows)


def _insert(connection, table, rows):
    if not rows:
        return
    columns = transfer.BUSINESS_COLUMNS[table]
    statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(PLATFORM_SCHEMA, table),
        sql.SQL(",").join(map(sql.Identifier, columns)),
        sql.SQL(",").join(sql.Placeholder() for _ in columns),
    )
    with connection.cursor() as cursor:
        cursor.executemany(statement, [tuple(row[column] for column in columns) for row in rows])


def _patch(connection, patch, expected):
    table = patch["table"]
    pk = transfer.PK[table]
    row = next(
        row for row in expected[table] if [row[column] for column in pk] == patch["target_pk"]
    )
    fields = sorted(set(patch["changes"]) | set(patch.get("json_merge", {})))
    statement = sql.SQL("UPDATE {} SET {} WHERE {}").format(
        sql.Identifier(PLATFORM_SCHEMA, table),
        sql.SQL(",").join(sql.SQL("{}=%s").format(sql.Identifier(field)) for field in fields),
        sql.SQL(" AND ").join(sql.SQL("{}=%s").format(sql.Identifier(field)) for field in pk),
    )
    cursor = connection.execute(
        statement, tuple(row[field] for field in fields) + tuple(patch["target_pk"])
    )
    require(cursor.rowcount == 1, "Patch did not affect exactly one reviewed record.")


def _restart_sequences(connection, inserted_tables, actual_rows):
    for table in sorted(set(inserted_tables) & set(transfer.IDENTITY_TABLES)):
        sequence = connection.execute(
            "SELECT pg_get_serial_sequence(%s,'id')", (f"{PLATFORM_SCHEMA}.{table}",)
        ).fetchone()[0]
        require(sequence == f"{PLATFORM_SCHEMA}.{table}_id_seq", "Sequence ownership differs.")
        identifier = sql.Identifier(PLATFORM_SCHEMA, table + "_id_seq")
        last = connection.execute(
            sql.SQL("SELECT last_value FROM {}").format(identifier)
        ).fetchone()[0]
        next_value = max(last, max((row["id"] for row in actual_rows[table]), default=0)) + 1
        # RESTART (unlike setval) is transactional and never reduces an existing
        # high sequence. A failure after this point rolls the sequence back too.
        connection.execute(
            sql.SQL("ALTER SEQUENCE {} RESTART WITH {}").format(identifier, sql.Literal(next_value))
        )


def plan_upgrade(
    connection,
    deployment,
    *,
    baseline,
    source,
    previous_plan,
    prepared_at,
    target_label,
    target_asset_root=None,
    credentials=None,
):
    require(
        connection.info.transaction_status == TransactionStatus.IDLE,
        "An idle explicitly owned management connection is required.",
    )
    try:
        with connection.transaction():
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            connection.execute("SET LOCAL statement_timeout='120s'")
            _check_target(connection, deployment)
            current = _snapshot(connection, target_label, target_asset_root)
            return planner.plan_upgrade(
                baseline,
                source,
                current,
                previous_plan,
                prepared_at=prepared_at,
                credentials=credentials,
            )
    except UpgradeError:
        raise
    except Exception:
        raise UpgradeError(SAFE_FAILURE) from None


def apply_upgrade(
    connection,
    deployment,
    *,
    baseline,
    source,
    previous_plan,
    reviewed_plan,
    confirm_sha256,
    target_asset_root=None,
    credentials=None,
):
    _confirm(reviewed_plan, confirm_sha256)
    require(
        connection.info.transaction_status == TransactionStatus.IDLE,
        "An idle explicitly owned management connection is required.",
    )
    try:
        with connection.transaction():
            connection.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
            connection.execute("SET LOCAL lock_timeout='5s'")
            connection.execute("SET LOCAL statement_timeout='120s'")
            connection.execute("SET LOCAL idle_in_transaction_session_timeout='180s'")
            # Take table locks before the first SELECT establishes a serializable
            # snapshot. Otherwise a just-finished writer could be invisible to
            # the re-plan even though our locks were acquired afterwards.
            connection.execute(
                sql.SQL("LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE").format(
                    sql.SQL(",").join(
                        sql.Identifier(PLATFORM_SCHEMA, table) for table in ALL_TABLES
                    )
                )
            )
            connection.execute("SELECT pg_advisory_xact_lock(717380026,5)")
            _check_target(connection, deployment)
            current = _snapshot(
                connection, reviewed_plan["target_identity"]["label"], target_asset_root
            )
            rebuilt = planner.plan_upgrade(
                baseline,
                source,
                current,
                previous_plan,
                prepared_at=reviewed_plan["prepared_at"],
                credentials=credentials,
            )
            if rebuilt["status"] == "already_applied":
                if reviewed_plan["status"] == "already_applied":
                    require(rebuilt == reviewed_plan, "No-op target changed; review again.")
                else:
                    ledger = next(
                        row
                        for row in current["rows"]["app_settings"]
                        if row["user_id"] is None and row["setting_key"] == planner.LEDGER_KEY
                    )
                    require(
                        json.loads(ledger["value_json"])
                        == json.loads(reviewed_plan["ledger_insert"]["value_json"]),
                        "Existing ledger does not match this exact reviewed operation.",
                    )
                return {
                    "status": "already_applied",
                    "plan_sha256": confirm_sha256,
                    "inserted_counts": {},
                    "patch_count": 0,
                    "user_edits_and_deletions_preserved": True,
                }
            require(
                rebuilt == reviewed_plan and rebuilt["status"] == "review_required",
                "Target or reviewed inputs changed; a new reviewed plan is required.",
            )
            expected = expected_rows(current, rebuilt)
            protected = _protected_fingerprint(connection)
            catalog_before = bootstrap.catalog_digest(connection)
            for table in transfer._import_order():
                if table in INSERT_TABLES:
                    _insert(connection, table, rebuilt["inserts"].get(table, []))
            for patch in rebuilt["patches"]:
                _patch(connection, patch, expected)
            _insert(connection, "app_settings", [rebuilt["ledger_insert"]])
            actual = transfer._pg_rows(connection)
            require(
                transfer.digest(actual) == transfer.digest(expected),
                "Full expected business row verification failed; transaction rolled back.",
            )
            _restart_sequences(connection, {*rebuilt["inserts"], "app_settings"}, actual)
            require(
                _protected_fingerprint(connection) == protected,
                "Authentication or developer state changed; transaction rolled back.",
            )
            require(
                bootstrap.catalog_digest(connection) == catalog_before,
                "Catalog changed; transaction rolled back.",
            )
            bootstrap.verify_installation(connection, deployment, revision="platform_0004")
            result = {
                "status": "committed",
                "plan_sha256": confirm_sha256,
                "inserted_counts": {table: len(rows) for table, rows in rebuilt["inserts"].items()},
                "patch_count": len(rebuilt["patches"]),
                "ledger_key": planner.LEDGER_KEY,
                "existing_user_rows_unchanged": True,
                "security_rows_unchanged": True,
                "verified_expected_rows": True,
                "data_deleted": False,
                "fictional_advisor_terms_patched": sum(
                    patch["table"] == "staff_account_terms" for patch in rebuilt["patches"]
                ),
            }
        return result
    except UpgradeError:
        raise
    except Exception:
        raise UpgradeError(SAFE_FAILURE) from None


def read_artifact(path):
    path = safe_file(path, existing=True)
    info = path.stat()
    require(info.st_size <= transfer.MAX_BYTES, "Artifact exceeds the bounded size.")
    if os.name == "posix":
        require(
            info.st_uid == 0 and not stat.S_IMODE(info.st_mode) & 0o077,
            "Artifacts must be root-owned and unreadable by group/others.",
        )
    return transfer.deserialize(path.read_bytes())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply"))
    parser.add_argument("--deployment-id", required=True)
    for name in ("baseline", "source", "history", "plan-file"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--prepared-at")
    parser.add_argument("--target-label")
    parser.add_argument("--target-assets", type=Path)
    parser.add_argument("--confirm-plan")
    parser.add_argument("--result-file", type=Path)
    parser.add_argument("--credentials-stdin", action="store_true")
    args = parser.parse_args(argv)
    try:
        deployment = container_gate()
        require(
            os.geteuid() == 0 and args.deployment_id == deployment["id"],
            "Explicit root deployment identity is required.",
        )
        require(deployment.get("kind") == MARKER, "Deployment kind differs.")
        safe_file(args.plan_file, existing=args.action == "apply")
        if args.action == "apply":
            require(args.result_file is not None, "A new private result file is required.")
            safe_file(args.result_file, existing=False)
        credentials = None
        if args.credentials_stdin:
            raw = sys.stdin.buffer.readline(16385)
            require(len(raw) <= 16384, "Credential input exceeds the bound.")
            credentials = json.loads(raw)
            require(
                isinstance(credentials, dict)
                and set(credentials) <= set(planner.ROLES)
                and all(isinstance(v, str) and len(v) <= 1024 for v in credentials.values()),
                "Credential input must contain only documented fictional identities.",
            )
        inputs = {
            name: read_artifact(getattr(args, field))
            for name, field in (
                ("baseline", "baseline"),
                ("source", "source"),
                ("previous_plan", "history"),
            )
        }
        with bootstrap.connect(ADMIN, secret(Path("/run/db-secrets/bootstrap-password"))) as conn:
            if args.action == "plan":
                result = plan_upgrade(
                    conn,
                    deployment,
                    **inputs,
                    prepared_at=args.prepared_at,
                    target_label=args.target_label,
                    target_asset_root=args.target_assets,
                    credentials=credentials,
                )
                write_report(args.plan_file, result)
            else:
                result = apply_upgrade(
                    conn,
                    deployment,
                    **inputs,
                    reviewed_plan=read_artifact(args.plan_file),
                    confirm_sha256=args.confirm_plan,
                    target_asset_root=args.target_assets,
                    credentials=credentials,
                )
                write_report(args.result_file, result)
        # Plans can contain new fictional health text. Emit only a receipt summary.
        print(json.dumps({key: result[key] for key in ("status", "plan_sha256") if key in result}))
        return 0
    except (Exception, KeyboardInterrupt):
        print('{"status":"synthetic_upgrade_not_verified","automatic_retry":false}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
