"""Only newly generated fictional SQLite inputs; no real userdata/cloud access."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from copy import deepcopy

import pytest
from argon2 import PasswordHasher

from ollama_chat_app.data.database import Database
from server import platform_transfer as transfer
from server.platform_transfer_package import load_package, package_context, save_package

STAMP = "2026-09-08T00:00:00.000000Z"


def synthetic_source(path, prefix="synthetic-a"):
    Database(path).initialize()
    hashed = PasswordHasher(time_cost=1, memory_cost=8192).hash("SYNTHETIC ONLY NOT AN ACCOUNT")
    content = b"SYNTHETIC REPORT ORIGINAL FILE"
    checksum = hashlib.sha256(content).hexdigest()
    data = {
        "users": [
            {
                "id": index,
                "username": f"{prefix}-{role}",
                "username_normalized": f"{prefix}-{role}",
                "password_hash": hashed,
                "role_code": role,
            }
            for index, role in ((1, "operator"), (2, "member"), (3, "advisor"))
        ],
        "member_profiles": [{"user_id": 2, "display_name": "示例会员"}],
        "profile_facts": [{"id": 1, "user_id": 2, "fact_key": "test", "value_json": "{}"}],
        "life_records": [
            {
                "id": 1,
                "user_id": 2,
                "category": "diet",
                "occurred_at": STAMP,
                "local_date": "2026-09-08",
                "timezone_offset_minutes": 0,
                "content": "示例非真实健康记录",
            }
        ],
        "reminders": [{"id": 1, "user_id": 2, "title": "示例提醒", "scheduled_at": STAMP}],
        "advisor_profiles": [{"user_id": 3, "display_name": "示例顾问"}],
        "advisor_bindings": [{"id": 1, "member_user_id": 2, "advisor_user_id": 3}],
        "memberships": [{"id": 1, "user_id": 2, "plan_code": "synthetic", "starts_at": STAMP}],
        "visit_tasks": [{"id": 1, "member_user_id": 2, "advisor_user_id": 3, "title": "示例任务"}],
        "visit_records": [
            {
                "id": 1,
                "member_user_id": 2,
                "advisor_user_id": 3,
                "task_id": 1,
                "visited_at": STAMP,
                "summary": "示例服务记录",
            }
        ],
        "user_files": [
            {
                "id": 1,
                "user_id": 2,
                "relative_path": "files/synthetic.txt",
                "original_name": "synthetic.txt",
                "media_type": "text/plain",
                "size_bytes": len(content),
                "sha256": checksum,
            }
        ],
        "medical_reports": [{"id": 1, "user_id": 2, "file_id": 1}],
        "report_items": [{"id": 1, "report_id": 1, "item_name": "示例项目"}],
        "ai_insights": [
            {
                "id": 1,
                "user_id": 2,
                "insight_type": "synthetic",
                "content": "示例非AI结论",
                "evidence_json": '{"record_id":1}',
            }
        ],
        "advisor_summaries": [
            {
                "id": 1,
                "member_user_id": 2,
                "advisor_user_id": 3,
                "period_start": "2026-09-01",
                "period_end": "2026-09-08",
                "content": "示例顾问摘要",
            }
        ],
        "app_settings": [
            {
                "id": 1,
                "user_id": None,
                "setting_key": prefix + "-visual",
                "value_json": '{"font_size":20}',
            }
        ],
        "audit_logs": [
            {
                "id": 1,
                "actor_user_id": 1,
                "action": "synthetic.created",
                "entity_type": "life_record",
                "entity_id": 1,
                "details_json": '{"member_user_id":2,"record_id":1}',
            }
        ],
        "products": [
            {
                "id": 1,
                "sku": prefix + "-sku",
                "name": "示例商品",
                "category": "daily",
                "price_cents": 100,
                "created_by_user_id": 1,
            }
        ],
        "product_recommendations": [
            {
                "id": 1,
                "product_id": 1,
                "member_user_id": 2,
                "advisor_user_id": 3,
                "reason": "示例推荐",
            }
        ],
        "orders": [
            {
                "id": 1,
                "order_no": prefix + "-order",
                "member_user_id": 2,
                "product_id": 1,
                "recommendation_id": 1,
                "reordered_from_order_id": 1,
                "product_name_snapshot": "示例商品",
                "quantity": 2,
                "unit_price_cents": 100,
                "total_amount_cents": 200,
            }
        ],
        "user_file_contents": [
            {"file_id": 1, "content": content, "mime_type": "text/plain", "sha256": checksum}
        ],
        "conversations": [{"id": 1, "user_id": 2, "title": "示例会话"}],
        "messages": [
            {
                "id": 1,
                "conversation_id": 1,
                "sequence_no": 1,
                "role": "user",
                "content": "示例消息",
                "status": "complete",
            }
        ],
        "chat_attachments": [
            {
                "id": 1,
                "message_id": 1,
                "original_name": "synthetic.txt",
                "media_type": "text/plain",
                "content": content,
                "sha256": checksum,
                "extraction_method": "text",
            }
        ],
        "user_preferences": [{"user_id": 2, "preferences_json": '{"nickname":"示例会员"}'}],
        "member_cart": [{"user_id": 2, "product_id": 1, "quantity": 2}],
        "member_favorites": [{"user_id": 2, "product_id": 1}],
        "staff_account_terms": [
            {"user_id": 3, "starts_at": STAMP, "ends_at": "2029-09-08T00:00:00.000000Z"}
        ],
        "visit_task_details": [
            {"task_id": 1, "service_type": "示例上门服务", "address": "示例地址", "requested_by": 2}
        ],
    }
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for table in transfer._import_order():
            for original in data[table]:
                row = dict(original)
                for column in transfer.BUSINESS_COLUMNS[table]:
                    if column in {"created_at", "updated_at"} and column not in row:
                        row[column] = STAMP
                columns = ",".join(row)
                connection.execute(
                    f'INSERT INTO "{table}" ({columns}) VALUES ({",".join("?" for _ in row)})',
                    tuple(row.values()),
                )
    return transfer.sqlite_snapshot(path, label=prefix)


@pytest.fixture
def source(tmp_path):
    return synthetic_source(tmp_path / "synthetic.db")


def empty(label="synthetic-target"):
    return transfer.snapshot(
        {table: [] for table in transfer.BUSINESS_TABLES},
        {"kind": "postgresql", "label": label, "locator": label},
    )


def test_full_schema_all_29_tables_and_binary_roundtrip(source):
    assert len(source["rows"]) == 29
    assert all(source["rows"].values())
    transfer.validate_rows(source["rows"])
    assert transfer.deserialize(transfer.serialize(source)) == source
    plan, rows = transfer.plan_transfer(source, empty())
    assert plan["status"] == "review_required"
    assert rows == source["rows"]
    assert plan["excluded"] == list(transfer.EXCLUDED)


def test_nonempty_requires_explicit_append_and_remaps_all_foreign_keys(source, tmp_path):
    target = synthetic_source(tmp_path / "target.db", "synthetic-b")
    assert transfer.plan_transfer(source, target)[0]["status"] == "blocked"
    plan, rows = transfer.plan_transfer(source, target, mode="append-only")
    assert plan["status"] == "review_required"
    transfer.validate_rows(rows)
    assert rows["users"][0]["id"] == 4
    assert rows["orders"][0]["member_user_id"] == 5
    assert rows["orders"][0]["reordered_from_order_id"] == 2
    assert rows["user_file_contents"][0]["file_id"] == 2
    assert rows["audit_logs"][0]["entity_id"] == 2
    assert json.loads(rows["audit_logs"][0]["details_json"])["member_user_id"] == 5
    assert rows["users"][0]["password_hash"] == source["rows"]["users"][0]["password_hash"]


def test_unique_conflict_is_not_identity_merge(source):
    target = deepcopy(source)
    target["identity"]["label"] = "another-target"
    target["identity"]["locator"] = "another-file"
    plan, _ = transfer.plan_transfer(source, target, mode="append-only")
    assert plan["status"] == "blocked"
    assert any(item["code"] == "unique_value_conflict" for item in plan["conflicts"])
    assert any(item["code"] == "global_settings_conflict" for item in plan["conflicts"])


def test_explicit_renames_create_separate_identities(source):
    target = deepcopy(source)
    target["identity"]["label"] = "another-target"
    target["identity"]["locator"] = "another-file"
    target["rows"]["app_settings"] = []
    choices = {
        "users": {str(i): f"renamed-synthetic-{i}" for i in (1, 2, 3)},
        "products": {"1": "renamed-sku"},
        "orders": {"1": "renamed-order"},
    }
    plan, rows = transfer.plan_transfer(source, target, mode="append-only", renames=choices)
    assert plan["status"] == "review_required"
    assert {row["id"] for row in rows["users"]} == {4, 5, 6}
    assert {row["role_code"] for row in rows["users"]} == {"operator", "member", "advisor"}


def test_unknown_json_id_is_review_blocker_not_guessed(source, tmp_path):
    target = synthetic_source(tmp_path / "target.db", "synthetic-b")
    source["rows"]["ai_insights"][0]["evidence_json"] = '{"unknown_id":1}'
    assert transfer.plan_transfer(source, target, mode="append-only")[0]["status"] == "blocked"
    plan, rows = transfer.plan_transfer(
        source, target, mode="append-only", json_id_fields={"unknown_id": "life_records"}
    )
    assert plan["status"] == "review_required"
    assert json.loads(rows["ai_insights"][0]["evidence_json"])["unknown_id"] == 2


@pytest.mark.parametrize(
    "mutation",
    ["setting_key", "nested_key", "text_key", "binary_key", "password", "fk", "blob_hash"],
)
def test_secret_or_invalid_source_is_rejected(source, mutation):
    rows = source["rows"]
    if mutation == "setting_key":
        rows["app_settings"][0]["setting_key"] = "deepseek_api_key"
    elif mutation == "nested_key":
        rows["app_settings"][0]["value_json"] = '{"deepseek":{"api_key":"SYNTHETIC ONLY"}}'
    elif mutation == "text_key":
        rows["messages"][0]["content"] = "sk-synthetic-secret-test-12345678"
    elif mutation == "binary_key":
        content = b"Bearer synthetic-token-1234567890"
        rows["chat_attachments"][0].update(
            content=content, sha256=hashlib.sha256(content).hexdigest()
        )
    elif mutation == "password":
        rows["users"][0]["password_hash"] = "SYNTHETIC PLAINTEXT MUST BE REJECTED"
    elif mutation == "fk":
        rows["orders"][0]["member_user_id"] = 999
    elif mutation == "blob_hash":
        rows["chat_attachments"][0]["sha256"] = "0" * 64
    with pytest.raises(transfer.TransferError):
        transfer.validate_rows(rows)


def test_encrypted_package_aead_identity_key_tamper_and_nonce(source, tmp_path):
    plan, _ = transfer.plan_transfer(source, empty())
    context = package_context(plan, "source")
    key = os.urandom(32)
    first, second = tmp_path / "first.enc", tmp_path / "second.enc"
    save_package(source, first, context=context, key=key)
    save_package(source, second, context=context, key=key)
    assert first.read_bytes() != second.read_bytes()
    assert b"password_hash" not in first.read_bytes()
    assert load_package(first, context=context, key=key) == source
    with pytest.raises(transfer.TransferError):
        load_package(first, context=context, key=os.urandom(32))
    with pytest.raises(transfer.TransferError):
        load_package(first, context={**context, "target_sha256": "0" * 64}, key=key)
    damaged = bytearray(first.read_bytes())
    damaged[-20] ^= 1
    (tmp_path / "damaged.enc").write_bytes(damaged)
    with pytest.raises(transfer.TransferError):
        load_package(tmp_path / "damaged.enc", context=context, key=key)
    with pytest.raises(transfer.TransferError):
        save_package(source, first, context=context, key=key)


def test_sqlite_source_is_unchanged_and_missing_original_requires_root(tmp_path):
    path = tmp_path / "synthetic.db"
    source = synthetic_source(path)
    before = path.read_bytes()
    transfer.sqlite_snapshot(path, label="readonly")
    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM user_file_contents")
    with pytest.raises(transfer.TransferError, match="源资产目录"):
        transfer.sqlite_snapshot(path, label="missing-original")
    files = tmp_path / "files"
    files.mkdir()
    (files / "synthetic.txt").write_bytes(source["rows"]["user_file_contents"][0]["content"])
    hydrated = transfer.sqlite_snapshot(path, label="hydrated", asset_root=tmp_path)
    assert hydrated["rows"] == source["rows"]


def test_guard_rejects_extra_tables_fields_tokens(source):
    source["rows"]["platform_sessions"] = []
    with pytest.raises(transfer.TransferError):
        transfer.validate_rows(source["rows"])


def test_assets_are_complete_content_addressed_and_never_overwritten(tmp_path):
    from PIL import Image

    from server.platform_transfer_assets import stage_assets, verify_staged_assets

    path = tmp_path / "source.db"
    synthetic_source(path)
    assets = tmp_path / "source-assets"
    assets.mkdir()
    Image.new("RGB", (2, 2), (30, 80, 40)).save(assets / "synthetic.png")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE products SET image_path='synthetic.png'")
    with pytest.raises(transfer.TransferError, match="原图"):
        transfer.sqlite_snapshot(path, label="synthetic-images")
    source = transfer.sqlite_snapshot(path, label="synthetic-images", asset_root=assets)
    plan, rows = transfer.plan_transfer(source, empty())
    assert len(plan["external_assets"]) == 1
    assert rows["products"][0]["image_path"].startswith("transfer-assets/")
    target_assets = tmp_path / "target-assets"
    target_assets.mkdir()
    stage_assets(source, plan, target_assets)
    verify_staged_assets(plan, target_assets)
    assert stage_assets(source, plan, target_assets)["reused_verified_staging"] is True
    output = target_assets / plan["external_assets"][0]["destination"]
    output.write_bytes(b"SYNTHETIC TAMPER")
    with pytest.raises(transfer.TransferError):
        stage_assets(source, plan, target_assets)
    assert output.read_bytes() == b"SYNTHETIC TAMPER"


def test_linux_key_must_be_private_independent_and_existing_key_is_preserved(tmp_path):
    from server.platform_transfer_package import generate_key, read_key

    directory = tmp_path / "private-key"
    directory.mkdir(mode=0o700)
    path = directory / "transfer.key"
    generate_key(path)
    key = read_key(path)
    assert len(key) == 32
    with pytest.raises(transfer.TransferError):
        generate_key(path)
    assert read_key(path) == key
    if os.name == "posix":
        path.chmod(0o644)
        with pytest.raises(transfer.TransferError, match="root"):
            read_key(path)


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only; AES tested on Linux")
def test_windows_dpapi_package_optional(source, tmp_path):
    plan, _ = transfer.plan_transfer(source, empty())
    context = package_context(plan, "source")
    path = tmp_path / "synthetic.dpapi-transfer"
    save_package(source, path, context=context)
    assert load_package(path, context=context) == source


def test_admin_connection_rejects_url_plaintext_tx_pool_and_unknown_target_before_network():
    from tools.aliyun_platform.transfer import connection_options

    with pytest.raises(transfer.TransferError):
        connection_options({"url": "postgresql://SYNTHETIC:NEVER@invalid/db"})
    sample = {
        "kind": "supabase",
        "label": "synthetic",
        "host": "not-supabase.example",
        "port": 6543,
        "database": "postgres",
        "user": "postgres",
        "password_file": "not-read",
        "sslrootcert": "not-read",
    }
    with pytest.raises(transfer.TransferError, match="5432"):
        connection_options(sample)
    sample["port"] = 5432
    with pytest.raises(transfer.TransferError, match="Supabase"):
        connection_options(sample)


def test_self_copy_is_rejected_even_with_different_label(source):
    changed = deepcopy(source)
    changed["identity"]["label"] = "different-name-same-file"
    with pytest.raises(transfer.TransferError, match="源和目标相同"):
        transfer.plan_transfer(source, changed)


def test_source_export_manifest_target_and_snapshot_are_authenticated(source, tmp_path):
    from tools.aliyun_platform import transfer as cli

    manifest = {
        "version": 1,
        "operation": "export-platform-snapshot",
        "source": source["identity"],
        "source_counts": {table: len(rows) for table, rows in source["rows"].items()},
        "source_sha256": transfer.digest(source),
        "expected_target": {"kind": "aliyun", "deployment_id": "a" * 32},
    }
    manifest["export_sha256"] = transfer.digest(manifest)
    key = os.urandom(32)
    save_package(
        source, tmp_path / "source.hltransfer", context=cli.export_context(manifest), key=key
    )
    cli.write_json(manifest, tmp_path / "export.json")
    assert cli.load_export(tmp_path, key)[0] == source
    manifest["expected_target"]["deployment_id"] = "b" * 32
    manifest["export_sha256"] = transfer.digest(
        {k: v for k, v in manifest.items() if k != "export_sha256"}
    )
    (tmp_path / "export.json").write_bytes(transfer.serialize(manifest))
    with pytest.raises(transfer.TransferError):
        cli.load_export(tmp_path, key)


@pytest.mark.parametrize(
    "key", ["api_key_encrypted", "apiKeyDpapi", "stored_credentials", "refresh_token_ciphertext"]
)
def test_encrypted_credential_forms_are_not_transferable(source, key):
    source["rows"]["app_settings"][0]["value_json"] = json.dumps({key: "SYNTHETIC ENCRYPTED BYTES"})
    with pytest.raises(transfer.TransferError, match="凭据"):
        transfer.validate_rows(source["rows"])
