"""Only synthetic snapshots: no PostgreSQL connection, network or actual executor."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest

from ollama_chat_app.security.passwords import hash_password
from server import platform_transfer as transfer
from tools import plan_synthetic_upgrade_v170 as planner
from tools.generate_synthetic_demo import build_demo
from tools.upgrade_synthetic_demo_v7 import contract, upgrade_copy

PREPARED = "2026-09-19T15:00:00.000000Z"


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    directory = tmp_path_factory.mktemp("fictional-upgrade-plan")
    baseline_source = os.environ.get("V170_PLAN_BASELINE")
    expanded_source = os.environ.get("V170_PLAN_SOURCE")
    if baseline_source and expanded_source:
        common = contract()
        for supplied, name in ((baseline_source, "baseline"), (expanded_source, "expanded")):
            source_path = Path(supplied)
            common.verify_payload(source_path)
            for relative in common.PAYLOAD_FILES:
                target = directory / name / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path / relative, target)
    else:
        if importlib.util.find_spec("PySide6") is None:
            pytest.skip("Supply V170_PLAN_BASELINE/V170_PLAN_SOURCE or desktop Qt generator")
        original = dict(os.environ)
        try:
            build_demo(directory / "old-v6", screenshots=False)
            upgrade_copy(directory / "old-v6", directory / "baseline")
            build_demo(
                directory / "expanded", screenshots=False, profile="expanded-v170",
                credential_seed=directory / "baseline",
            )
        finally:
            os.environ.clear()
            os.environ.update(original)
    baseline = transfer.sqlite_snapshot(
        directory / "baseline/data/app.db", label="old-demo", asset_root=directory / "baseline"
    )
    source = transfer.sqlite_snapshot(
        directory / "expanded/data/app.db", label="new-demo", asset_root=directory / "expanded"
    )
    identity = {
        "kind": "postgresql", "label": "synthetic-target-only", "host": "postgres",
        "port": 5432, "database": "medical_app_aliyun", "catalog": "synthetic-catalog",
        "deployment_marker": "medical-app:aliyun:v1:" + "a" * 32, "record_schema": 7,
    }
    empty = {table: [] for table in transfer.BUSINESS_TABLES}
    owner = copy.deepcopy(baseline["rows"]["users"][0])
    owner.update(id=23, username="fictional-existing-operator",
                 username_normalized="fictional-existing-operator")
    empty["users"] = [owner]
    original_target = transfer.snapshot(empty, identity)
    previous_plan, transformed = transfer.plan_transfer(
        baseline, original_target, mode="append-only", entity_types={"member_profile": "users"}
    )
    assert previous_plan["status"] == "review_required"
    rows = {table: empty[table] + transformed[table] for table in transfer.BUSINESS_TABLES}
    assets = {
        item["destination"]: baseline["assets"][item["source_path"]]
        for item in previous_plan["external_assets"]
    }
    target = transfer.snapshot(rows, identity, assets=assets)
    return {
        "baseline": baseline, "source": source, "target": target, "previous_plan": previous_plan,
        "directory": directory,
    }


@pytest.fixture
def snapshots(corpus):
    return {name: copy.deepcopy(value) for name, value in corpus.items() if name != "directory"}


def make_plan(snapshots, **kwargs):
    return planner.plan_upgrade(**snapshots, prepared_at=PREPARED, **kwargs)


def merge_json(current, patch):
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(current.get(key), dict):
            merge_json(current[key], value)
        else:
            current[key] = value


def simulated_result(target, plan):
    """Test-only dictionary simulation; intentionally NOT a database executor."""
    result = copy.deepcopy(target)
    for table, rows in plan["inserts"].items():
        result["rows"][table].extend(copy.deepcopy(rows))
    for patch in plan["patches"]:
        row = next(row for row in result["rows"][patch["table"]]
                   if list(planner._key(patch["table"], row)) == patch["target_pk"])
        assert transfer.digest(row) == patch["before_sha256"]
        row.update(copy.deepcopy(patch["changes"]))
        for column, value in patch.get("json_merge", {}).items():
            current = json.loads(row[column])
            merge_json(current, value)
            row[column] = json.dumps(current, ensure_ascii=False)
    result["rows"]["app_settings"].append(copy.deepcopy(plan["ledger_insert"]))
    transfer.validate_rows(result["rows"])
    return result


def test_plan_preserves_identity_and_appends_separate_fact_batch(snapshots):
    before = transfer.digest(snapshots)
    plan = make_plan(snapshots)
    assert plan["status"] == "review_required"
    assert transfer.digest(snapshots) == before
    assert plan["id_mapping"]["users"] == {str(i): i + 23 for i in range(1, 6)}
    expected = {
        "life_records": 4632, "reminders": 16, "visit_tasks": 22, "visit_records": 14,
        "visit_task_details": 22, "messages": 72, "conversations": 12, "user_preferences": 3,
    }
    assert {name: len(rows) for name, rows in plan["inserts"].items()} == expected
    assert all(item["identity_matches_history"] and item["credential_compatible"]
               for item in plan["account_checks"])
    assert all(item["table"] != "users" for item in plan["patches"])
    assert plan["schema_changes"] == []
    result = simulated_result(snapshots["target"], plan)
    assert result["rows"]["users"] == snapshots["target"]["rows"]["users"]
    assert result["rows"]["audit_logs"] == snapshots["target"]["rows"]["audit_logs"]
    for table in planner.NEW_FACT_TABLES:
        assert all(row in result["rows"][table] for row in snapshots["target"]["rows"][table])
    assert len(result["rows"]["life_records"]) == 156 + 4632
    assert result["assets"] == snapshots["target"]["assets"]


def test_second_run_is_noop_even_after_user_edits_and_deletes(snapshots):
    first = make_plan(snapshots)
    result = simulated_result(snapshots["target"], first)
    last = result["rows"]["life_records"].pop()
    assert last["details_json"]
    result["rows"]["life_records"][-1]["content"] = "A fictional user corrected their own record"
    snapshots["target"] = result
    second = make_plan(snapshots)
    assert second["status"] == "already_applied"
    assert second["inserts"] == {} and second["patches"] == []
    assert second["user_edits_and_deletions_preserved"] is True
    assert "ledger_insert" not in second


def test_three_way_changes_preserve_user_fields_and_unrelated_rows(snapshots):
    target = snapshots["target"]
    profile = target["rows"]["member_profiles"][0]
    profile["display_name"] = "Private owner-edited name never written to plan"
    product = target["rows"]["products"][0]
    product["description"] = "Private owner-edited product text never written to plan"
    product["price_cents"] += 100
    profile_before, product_before = copy.deepcopy(profile), copy.deepcopy(product)
    plan = make_plan(snapshots)
    assert plan["status"] == "review_required"
    assert any(item["table"] == "member_profiles" and item["field"] == "display_name"
               for item in plan["preserved_conflicts"])
    result = simulated_result(target, plan)
    changed_profile = next(row for row in result["rows"]["member_profiles"]
                           if row["user_id"] == profile_before["user_id"])
    changed_product = next(row for row in result["rows"]["products"]
                           if row["id"] == product_before["id"])
    assert changed_profile["display_name"] == profile_before["display_name"]
    assert changed_product["description"] == product_before["description"]
    assert changed_product["price_cents"] == product_before["price_cents"]
    serialized = json.dumps(plan, ensure_ascii=False)
    assert "Private owner-edited" not in serialized
    assert "$argon2" not in serialized
    assert "password_hash" not in serialized


def test_unknown_preferences_preserved_without_leaking_into_plan(snapshots):
    row = snapshots["target"]["rows"]["user_preferences"][0]
    value = json.loads(row["preferences_json"])
    value.update(nickname="Owner-kept nickname", private_note="PRIVATE-NOT-IN-PLAN", font_size=29)
    row["preferences_json"] = json.dumps(value)
    plan = make_plan(snapshots)
    assert "PRIVATE-NOT-IN-PLAN" not in json.dumps(plan)
    result = simulated_result(snapshots["target"], plan)
    updated = next(item for item in result["rows"]["user_preferences"]
                   if item["user_id"] == row["user_id"])
    preferences = json.loads(updated["preferences_json"])
    assert preferences["private_note"] == "PRIVATE-NOT-IN-PLAN"
    assert preferences["nickname"] == "Owner-kept nickname"
    assert preferences["font_size"] == 29


def test_nested_json_delta_contains_only_source_changes():
    before = {"appearance": {"theme": "old"}}
    new = {"appearance": {"theme": "new"}}
    current = {"appearance": {"theme": "old", "private_note": "never reveal"}}
    merged = planner._patch_json(before, new, current, table="user_preferences", identity=(27,),
                                 conflicts=[])
    delta = planner._json_delta(current, merged)
    assert delta == {"appearance": {"theme": "new"}}


def test_changed_password_blocks_without_reset(snapshots):
    account = snapshots["target"]["rows"]["users"][1]
    account["password_hash"] = hash_password("FictionalNewPasswordOnly@2026")
    plan = make_plan(snapshots)
    assert plan["status"] == "blocked"
    assert any(item["code"] == "credential_conflict_no_reset" for item in plan["conflicts"])
    assert not plan["inserts"] and not plan["patches"]


def test_rehashed_same_password_can_be_verified_without_output(snapshots, corpus):
    credentials = contract().sibling("_demo_v170").credentials(corpus["directory"] / "expanded")
    for account in snapshots["target"]["rows"]["users"]:
        if account["username"] in credentials:
            account["password_hash"] = hash_password(credentials[account["username"]])
    plan = make_plan(snapshots, credentials=credentials)
    assert plan["status"] == "review_required"
    assert all(value not in json.dumps(plan) for value in credentials.values())


@pytest.mark.parametrize("field,value", [("account_status", "disabled"), ("role_code", "member")])
def test_account_status_or_role_drift_is_not_overwritten(snapshots, field, value):
    snapshots["target"]["rows"]["users"][1][field] = value
    plan = make_plan(snapshots)
    assert plan["status"] == "blocked"
    assert not plan["inserts"] and not plan["patches"]


def test_reassigned_conversation_cannot_receive_other_users_messages(snapshots):
    snapshots["target"]["rows"]["conversations"][0]["user_id"] = 23
    plan = make_plan(snapshots)
    assert plan["status"] == "blocked"
    assert any(item["code"] == "historic_relationship_changed_preserved"
               for item in plan["conflicts"])


def test_untracked_expanded_facts_stop_instead_of_guessing_dedup(snapshots):
    row = copy.deepcopy(snapshots["source"]["rows"]["life_records"][0])
    row.update(id=9000, user_id=27)
    snapshots["target"]["rows"]["life_records"].append(row)
    plan = make_plan(snapshots)
    assert plan["status"] == "blocked"
    assert any(item["code"] == "expanded_rows_without_ledger_require_recovery"
               for item in plan["conflicts"])


def test_ledger_minimal_non_secret_and_global_unique_contract(snapshots):
    plan = make_plan(snapshots)
    row = plan["ledger_insert"]
    value = json.loads(row["value_json"])
    assert row["user_id"] is None and row["setting_key"] == planner.LEDGER_KEY
    assert len(row["value_json"].encode()) < 2000
    assert set(value) == {
        "version", "batch", "status", "source_business_sha256", "historical_plan_sha256",
        "operation_sha256", "inserted_counts", "patch_count",
    }
    assert "id_mapping" not in value
    assert plan["atomic_ledger_required"] is True


def test_malformed_ledger_never_reimports(snapshots):
    first = make_plan(snapshots)
    snapshots["target"] = simulated_result(snapshots["target"], first)
    snapshots["target"]["rows"]["app_settings"][-1]["value_json"] = '{"version":99}'
    with pytest.raises(planner.PlanError, match="ledger"):
        make_plan(snapshots)


def test_changed_source_or_historic_plan_is_rejected(snapshots):
    snapshots["source"]["rows"]["life_records"][0]["content"] = "unaudited changes"
    with pytest.raises(planner.PlanError, match="strictly reviewed"):
        make_plan(snapshots)


def test_changed_historical_plan_digest_is_rejected(snapshots):
    snapshots["previous_plan"]["id_mapping"]["users"]["1"] = 23
    with pytest.raises(planner.PlanError, match="digest"):
        make_plan(snapshots)


def test_wrong_deployment_is_rejected(snapshots):
    snapshots["target"]["identity"]["deployment_marker"] = "medical-app:aliyun:v1:" + "b" * 32
    with pytest.raises(planner.PlanError, match="ownership"):
        make_plan(snapshots)


def test_reviewed_local_seed_wrapper_matches_pure_plan(corpus, snapshots):
    directory: Path = corpus["directory"]
    result = planner.prepare_reviewed(
        directory / "baseline", directory / "expanded", snapshots["target"],
        snapshots["previous_plan"], prepared_at=PREPARED,
    )
    assert result["status"] == "review_required"
    assert result["inserts"] == make_plan(snapshots)["inserts"]


def test_plan_hash_is_replayable_and_target_bound(snapshots):
    first, second = make_plan(snapshots), make_plan(snapshots)
    assert first == second
    checked = copy.deepcopy(first)
    supplied_hash = checked.pop("plan_sha256")
    assert supplied_hash == transfer.digest(checked)
    snapshots["target"]["rows"]["users"][0]["last_login_at"] = PREPARED
    changed = make_plan(snapshots)
    assert changed["target_sha256"] != first["target_sha256"]
    assert changed["plan_sha256"] != first["plan_sha256"]


def test_reviewed_v4_catalog_change_does_not_rebind_deployment(snapshots):
    snapshots["target"]["identity"]["catalog"] = "reviewed-v4-37-table-catalog"
    assert make_plan(snapshots)["status"] == "review_required"


def test_deleted_historic_conversation_stops_without_restoring_it(snapshots):
    snapshots["target"]["rows"]["conversations"].pop()
    result = make_plan(snapshots)
    assert result["status"] == "blocked"
    assert result["inserts"] == {}
    assert any(item["code"] == "historic_conversation_missing_do_not_restore"
               for item in result["conflicts"])


def test_deleted_profile_is_preserved_while_new_facts_can_be_added(snapshots):
    snapshots["target"]["rows"]["member_profiles"].pop()
    result = make_plan(snapshots)
    assert result["status"] == "review_required"
    assert "member_profiles" not in result["inserts"]
    assert any(item["code"] == "missing_baseline_or_deleted_singleton_preserved"
               for item in result["preserved_conflicts"])


def test_invalid_calendar_timestamp_is_rejected(snapshots):
    with pytest.raises(planner.PlanError, match="timestamp"):
        planner.plan_upgrade(**snapshots, prepared_at="2026-99-19T15:00:00.000000Z")
