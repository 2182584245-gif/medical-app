"""Read-only, identity-preserving plan for the reviewed expanded fictional corpus.

This module has NO apply function, database connection, network call or file
writer. A separately reviewed executor must take an authenticated full target
snapshot, confirm the plan digest, recheck it under transaction locks, apply only
these inserts/patches and atomically insert the ledger. Source/target snapshots
stay in memory; returned plans contain new fictional values, IDs and digests, not
target personal values, password hashes, login tokens or API keys.

Ledger uses existing app_settings (user_id NULL + fixed key), not an extra table.
It is intentionally NOT secret: existing RLS permits signed-in global reads, but
only operators can write it and public RPC has no arbitrary-key setter. Keep the
full reviewed plan/ID maps in a protected external deployment receipt. Never
make a global ledger contain prior personal values or credentials.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from pathlib import Path

from ollama_chat_app.security.passwords import verify_password
from server.platform_transfer import (
    BUSINESS_COLUMNS,
    BUSINESS_TABLES,
    FK,
    IDENTITY_TABLES,
    PK,
    digest,
    sqlite_snapshot,
    validate_rows,
)

PROFILE = "expanded-v170"
BUSINESS_SHA = "8acb784c4a2e9239f28aae3e2d2695311b49559b9ed34e7b13658cf8268f272c"
BATCH = PROFILE + ":" + BUSINESS_SHA
LEDGER_KEY = "seed.expanded-v170." + BUSINESS_SHA[:12] + ".ledger.v1"
ROLES = {
    "demo-operator": "operator",
    "demo-advisor-1": "advisor",
    "demo-advisor-2": "advisor",
    "demo-member-1": "member",
    "demo-member-2": "member",
}
NEW_FACT_TABLES = (
    "life_records", "reminders", "visit_tasks", "visit_records", "visit_task_details",
)
DISPLAY_PATCHES = {
    "member_profiles": set(BUSINESS_COLUMNS["member_profiles"])
    - {"user_id", "created_at", "updated_at"},
    "advisor_profiles": {"display_name", "organization", "specialty", "bio"},
    "products": {"name", "brand", "description"},
    "product_recommendations": {"reason"},
    # Preserve historic order numbers, prices, quantities and payment/state facts.
    "orders": {"product_name_snapshot"},
    "memberships": {"plan_code", "status", "starts_at", "ends_at", "benefits_json"},
    "staff_account_terms": {"starts_at", "ends_at"},
    "advisor_bindings": {"status", "started_at", "ended_at"},
}
EMPTY_SOURCE_TABLES = {
    "profile_facts", "user_files", "medical_reports", "report_items", "ai_insights",
    "advisor_summaries", "app_settings", "user_file_contents", "chat_attachments",
}


class PlanError(RuntimeError):
    """Only fixed, non-sensitive diagnostic messages."""


def _require(condition, message):
    if not condition:
        raise PlanError(message)


def _business_digest(rows):
    ignored = {"created_at", "updated_at", "last_login_at", "password_hash", "order_no"}
    return digest({
        table: [{key: value for key, value in row.items() if key not in ignored}
                for row in sorted(rows[table], key=lambda row, t=table: _key(t, row))]
        for table in sorted(rows)
    })


def _key(table, row):
    return tuple(row[column] for column in PK[table])


def _index(rows):
    return {table: {_key(table, row): row for row in rows[table]} for table in BUSINESS_TABLES}


def _row_id(table, row):
    return str(row["id"]) if table in IDENTITY_TABLES else ":".join(map(str, _key(table, row)))


def _patch_json(before, new, current, *, table, identity, conflicts, path=()):
    """Three-way merge object leaves; preserve unknown/current-user-added keys."""
    result = copy.deepcopy(current)
    for key, wanted in new.items():
        field = (*path, key)
        had_old, has_current = key in before, key in current
        old = before.get(key)
        actual = current.get(key)
        if has_current and wanted == actual:
            continue
        if had_old and old == wanted:
            continue
        if all(isinstance(item, dict) for item in (old, wanted, actual)):
            result[key] = _patch_json(
                old, wanted, actual, table=table, identity=identity,
                conflicts=conflicts, path=field,
            )
        elif (had_old and has_current and old == actual) or (not had_old and not has_current):
            result[key] = copy.deepcopy(wanted)
        else:
            conflicts.append({
                "table": table, "target_pk": list(identity), "field": ".".join(field),
                "code": "user_value_preserved",
            })
    return result


def _json_delta(current, merged):
    result = {}
    for key, value in merged.items():
        if key not in current:
            result[key] = value
        elif isinstance(value, dict) and isinstance(current[key], dict):
            nested = _json_delta(current[key], value)
            if nested:
                result[key] = nested
        elif value != current[key]:
            result[key] = value
    return result


def _validated_inputs(baseline, source, target, previous_plan):
    for item in (baseline, source, target):
        _require(item.get("version") == 1, "Unsupported snapshot format.")
        validate_rows(item["rows"])
    _require(_business_digest(source["rows"]) == BUSINESS_SHA,
             "Only the strictly reviewed expanded-v170 business corpus is supported.")
    _require(all(not source["rows"][table] for table in EMPTY_SOURCE_TABLES),
             "Unexpected additional source data is not supported.")
    historic = copy.deepcopy(previous_plan)
    historic_sha = historic.pop("plan_sha256", None)
    _require(historic_sha == digest(historic), "Historical import plan digest is invalid.")
    _require(previous_plan.get("status") == "review_required"
             and previous_plan.get("mode") == "append-only"
             and previous_plan.get("choices", {}).get("renames", {}) == {},
             "Require the reviewed, unrenamed historical demo import plan.")
    old_source = copy.deepcopy(baseline)
    old_source["identity"] = previous_plan["source"]
    _require(digest(old_source) == previous_plan["source_sha256"],
             "Baseline does not exactly match the historically imported source.")
    identity, old_target = target["identity"], previous_plan["target"]
    _require(identity.get("kind") == "postgresql" and identity.get("record_schema") == 7,
             "Require a reviewed PostgreSQL target supporting schema7 records.")
    marker = identity.get("deployment_marker", "")
    _require(isinstance(marker, str) and re.fullmatch("medical-app:aliyun:v1:[a-f0-9]{32}", marker)
             and all(identity.get(k) == old_target.get(k)
                     for k in ("kind", "host", "port", "database", "deployment_marker")),
             "Target ownership does not match the historical deployment.")
    _require(set(row["username"] for row in baseline["rows"]["users"]) == set(ROLES)
             and set(row["username"] for row in source["rows"]["users"]) == set(ROLES),
             "Only the five reviewed fictional identities may be imported.")


def plan_upgrade(
    baseline: dict,
    source: dict,
    target: dict,
    previous_plan: dict,
    *,
    prepared_at: str,
    credentials: dict[str, str] | None = None,
) -> dict:
    """Return a deterministic plan or a blocked/no-op result, never execute it.

    credentials is optional and remains memory-only. Equal historical/current
    Argon2 hashes establish unchanged credentials without plaintext. Otherwise a
    caller may supply the reviewed documented password for verification only;
    failed verification blocks first import rather than resetting a cloud login.
    """
    _require(isinstance(prepared_at, str)
             and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", prepared_at),
             "Supply one explicit canonical UTC plan timestamp for deterministic replay.")
    try:
        datetime.fromisoformat(prepared_at)
    except ValueError:
        raise PlanError("Invalid UTC plan timestamp.") from None
    _validated_inputs(baseline, source, target, previous_plan)
    original_target_sha = digest(target)
    plan = {
        "version": 1, "operation": "upgrade-reviewed-fictional-data-preserve-identities",
        "batch": BATCH, "prepared_at": prepared_at, "source_business_sha256": BUSINESS_SHA,
        "historical_plan_sha256": previous_plan["plan_sha256"],
        "baseline_source_sha256": previous_plan["source_sha256"],
        "target_sha256": original_target_sha, "target_identity": copy.deepcopy(target["identity"]),
        "status": "review_required", "account_checks": [], "conflicts": [],
        "preserved_conflicts": [], "inserts": {}, "patches": [], "id_mapping": {},
        "existing_users_preserved": True, "passwords_reset": False,
        "deleted_records_restored": False, "cloud_connected_by_planner": False,
        "schema_changes": [],
        "excluded_source_tables": sorted(EMPTY_SOURCE_TABLES | {"audit_logs"}),
    }
    current = _index(target["rows"])
    old_users = {row["username"]: row for row in baseline["rows"]["users"]}
    new_users = {row["username"]: row for row in source["rows"]["users"]}
    mapping = {table: {} for table in IDENTITY_TABLES}
    for username, role in ROLES.items():
        old = old_users[username]
        expected_id = previous_plan["id_mapping"]["users"].get(str(old["id"]))
        matches = [row for row in target["rows"]["users"]
                   if row["username_normalized"] == username]
        actual = matches[0] if len(matches) == 1 else None
        identity_ok = bool(actual and actual["id"] == expected_id
                           and actual["role_code"] == role and actual["username"] == username)
        password_ok = bool(identity_ok and actual["password_hash"] == old["password_hash"])
        if identity_ok and not password_ok and credentials and username in credentials:
            password_ok = verify_password(actual["password_hash"], credentials[username])
        account = {
            "username": username, "role": role, "identity_matches_history": identity_ok,
            "credential_compatible": password_ok,
            "account_active": bool(actual and actual["account_status"] == "active"),
        }
        plan["account_checks"].append(account)
        if identity_ok:
            mapping["users"][str(new_users[username]["id"])] = expected_id
        else:
            plan["conflicts"].append({"code": "account_identity_conflict", "username": username})
        if not password_ok:
            plan["conflicts"].append({"code": "credential_conflict_no_reset", "username": username})
        if not account["account_active"]:
            plan["conflicts"].append({
                "code": "account_disabled_no_reactivation", "username": username,
            })
    ledger_rows = [row for row in target["rows"]["app_settings"]
                   if row["user_id"] is None and row["setting_key"] == LEDGER_KEY]
    _require(len(ledger_rows) <= 1, "Duplicate global ledger rows are invalid.")
    if ledger_rows:
        try:
            ledger = json.loads(ledger_rows[0]["value_json"])
        except (TypeError, ValueError):
            raise PlanError("Existing ledger is malformed; do not reimport.") from None
        _require(isinstance(ledger, dict) and ledger.get("version") == 1
                 and set(ledger) == {
                     "version", "batch", "status", "source_business_sha256",
                     "historical_plan_sha256", "operation_sha256", "inserted_counts", "patch_count",
                 }
                 and ledger.get("batch") == BATCH and ledger.get("status") == "applied"
                 and ledger.get("historical_plan_sha256") == previous_plan["plan_sha256"]
                 and ledger.get("source_business_sha256") == BUSINESS_SHA
                 and re.fullmatch(r"[0-9a-f]{64}", str(ledger.get("operation_sha256", ""))),
                 "Existing ledger does not identify this reviewed batch; do not reimport.")
        plan["status"] = "already_applied"
        plan["ledger_key"] = LEDGER_KEY
        plan["user_edits_and_deletions_preserved"] = True
        # Do not recreate edited/deleted imported rows or reset subsequently changed passwords.
        plan["plan_sha256"] = digest(plan)
        return plan
    details = [json.loads(row["details_json"] or "{}")
               for row in target["rows"]["life_records"]]
    if any(isinstance(item, dict) and item.get("synthetic_profile") == PROFILE for item in details):
        plan["conflicts"].append({"code": "expanded_rows_without_ledger_require_recovery"})
    if plan["conflicts"]:
        plan["status"] = "blocked"
        plan["plan_sha256"] = digest(plan)
        return plan
    counters = {
        table: max((row["id"] for row in target["rows"][table]), default=0)
        for table in IDENTITY_TABLES
    }
    inserts = {table: [] for table in BUSINESS_TABLES}

    def allocate(table, row):
        counters[table] += 1
        mapping[table][str(row["id"])] = counters[table]

    old_index = _index(baseline["rows"])
    # Reuse old entities only where source identity semantics were retained.
    reused = {"products", "product_recommendations", "orders", "memberships", "advisor_bindings"}
    for table in sorted(reused):
        for row in source["rows"][table]:
            mapped = previous_plan["id_mapping"][table].get(str(row["id"]))
            if (mapped,) not in current[table] or (row["id"],) not in old_index[table]:
                plan["conflicts"].append({
                    "code": "historic_entity_missing_do_not_restore", "table": table,
                    "source_pk": list(_key(table, row)),
                })
            else:
                mapping[table][str(row["id"])] = mapped
    for row in source["rows"]["conversations"]:
        if (row["id"],) in old_index["conversations"]:
            mapped = previous_plan["id_mapping"]["conversations"].get(str(row["id"]))
            if (mapped,) not in current["conversations"]:
                plan["conflicts"].append({
                    "code": "historic_conversation_missing_do_not_restore",
                    "source_pk": [row["id"]],
                })
            else:
                mapping["conversations"][str(row["id"])] = mapped
        else:
            allocate("conversations", row)
    if plan["conflicts"]:
        plan["status"] = "blocked"
        plan["plan_sha256"] = digest(plan)
        return plan
    for table in (*sorted(reused), "conversations"):
        for old in baseline["rows"][table]:
            mapped = mapping[table].get(str(old["id"]))
            if mapped is None:
                continue
            actual = current[table][(mapped,)]
            for field, related in FK.get(table, {}).items():
                old_value = old[field]
                expected = None if old_value is None else previous_plan["id_mapping"][related].get(
                    str(old_value)
                )
                if actual[field] != expected:
                    plan["conflicts"].append({
                        "code": "historic_relationship_changed_preserved", "table": table,
                        "target_pk": [mapped], "field": field,
                    })
    if plan["conflicts"]:
        plan["status"] = "blocked"
        plan["plan_sha256"] = digest(plan)
        return plan
    for table in (*NEW_FACT_TABLES, "messages"):
        if table in IDENTITY_TABLES:
            for row in source["rows"][table]:
                allocate(table, row)

    def remap(table, row):
        row = copy.deepcopy(row)
        if table in IDENTITY_TABLES:
            row["id"] = mapping[table][str(row["id"])]
        for field, related in FK.get(table, {}).items():
            if row[field] is not None:
                row[field] = mapping[related][str(row[field])]
        return row

    for table in (*NEW_FACT_TABLES, "messages"):
        inserts[table] = [remap(table, row) for row in source["rows"][table]]
    inserts["conversations"] = [
        remap("conversations", row) for row in source["rows"]["conversations"]
        if (row["id"],) not in old_index["conversations"]
    ]
    for table, fields in DISPLAY_PATCHES.items():
        for row in source["rows"][table]:
            wanted = remap(table, row)
            identity = _key(table, wanted)
            before = old_index[table].get(_key(table, row))
            actual = current[table].get(identity)
            if before is None or actual is None:
                plan["preserved_conflicts"].append({
                    "code": "missing_baseline_or_deleted_singleton_preserved", "table": table,
                    "target_pk": list(identity),
                })
                continue
            changes = {}
            for field in sorted(fields):
                if wanted[field] == before[field] or wanted[field] == actual[field]:
                    continue
                if actual[field] == before[field]:
                    changes[field] = wanted[field]
                else:
                    plan["preserved_conflicts"].append({
                        "code": "user_value_preserved", "table": table,
                        "target_pk": list(identity), "field": field,
                    })
            if changes:
                changes["updated_at"] = prepared_at
                plan["patches"].append({
                    "table": table, "target_pk": list(identity),
                    "before_sha256": digest(actual), "changes": changes,
                })
    for row in source["rows"]["user_preferences"]:
        wanted = remap("user_preferences", row)
        identity = _key("user_preferences", wanted)
        actual = current["user_preferences"].get(identity)
        old = old_index["user_preferences"].get(_key("user_preferences", row))
        if actual is None:
            if old is None:
                inserts["user_preferences"].append(wanted)
            else:
                plan["preserved_conflicts"].append({
                    "code": "deleted_preferences_preserved", "table": "user_preferences",
                    "target_pk": list(identity),
                })
            continue
        before_json = json.loads(old["preferences_json"]) if old else {}
        wanted_json, actual_json = (
            json.loads(item["preferences_json"]) for item in (wanted, actual)
        )
        merged = _patch_json(
            before_json, wanted_json, actual_json, table="user_preferences", identity=identity,
            conflicts=plan["preserved_conflicts"],
        )
        if merged != actual_json:
            # Emit changed leaves only, never copy private current values into the plan.
            patch_json = _json_delta(actual_json, merged)
            plan["patches"].append({
                "table": "user_preferences", "target_pk": list(identity),
                "before_sha256": digest(actual),
                "changes": {"updated_at": prepared_at},
                "json_merge": {"preferences_json": patch_json},
            })
    # Never replenish shopping carts/favorites or copy newly authored audit events.
    plan["preserved_tables_without_writes"] = [
        "users", "member_cart", "member_favorites", "audit_logs",
        *sorted(EMPTY_SOURCE_TABLES - {"app_settings"}),
    ]
    plan["inserts"] = {table: rows for table, rows in inserts.items() if rows}
    plan["id_mapping"] = {table: values for table, values in mapping.items() if values}
    counts = {table: len(rows) for table, rows in plan["inserts"].items()}
    operation_sha = digest({
        "batch": BATCH, "target_sha256": original_target_sha,
        "inserts": plan["inserts"], "patches": plan["patches"],
    })
    ledger = {
        "version": 1, "batch": BATCH, "status": "applied",
        "source_business_sha256": BUSINESS_SHA,
        "historical_plan_sha256": previous_plan["plan_sha256"],
        "operation_sha256": operation_sha, "inserted_counts": counts,
        "patch_count": len(plan["patches"]),
    }
    counters["app_settings"] += 1
    plan["ledger_insert"] = {
        "id": counters["app_settings"], "user_id": None, "setting_key": LEDGER_KEY,
        "value_json": json.dumps(ledger, sort_keys=True, separators=(",", ":")),
        "created_at": prepared_at, "updated_at": prepared_at,
    }
    plan["atomic_ledger_required"] = True
    plan["no_new_external_assets"] = True
    plan["public_distribution_allowed"] = False
    plan["plan_sha256"] = digest(plan)
    return plan


def prepare_reviewed(
    baseline_root: Path, source_root: Path, target_snapshot: dict, previous_plan: dict,
    *, prepared_at: str,
) -> dict:
    """Read reviewed local seeds and return a plan; caller supplies target read-only snapshot."""
    from tools.upgrade_synthetic_demo_v7 import contract

    common = contract()
    common.verify_payload(baseline_root)
    common.sibling("_demo_v170").verify_payload(source_root)
    baseline = sqlite_snapshot(
        baseline_root / "data/app.db", label="old-demo", asset_root=baseline_root
    )
    source = sqlite_snapshot(source_root / "data/app.db", label=PROFILE, asset_root=source_root)
    return plan_upgrade(
        baseline, source, target_snapshot, previous_plan, prepared_at=prepared_at,
        credentials=common.sibling("_demo_v170").credentials(source_root),
    )
