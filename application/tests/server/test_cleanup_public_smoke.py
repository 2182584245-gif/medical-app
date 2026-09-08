"""Synthetic receipt-only cleanup; live databases/credentials are never fixtures."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import secrets
import time
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from PIL import Image
from psycopg import sql

from ollama_chat_app.data.database import timestamp_to_db
from ollama_chat_app.security import private_payload
from server.platform_transfer import TransferError, _import_order, digest
from tests.server import test_platform_transfer_pg as pg_fixtures
from tools.aliyun_platform import cleanup_public_smoke as cleanup

DEPLOYMENT = "d" * 32
LABEL = "isolated-synthetic-cleanup"
pg_cluster = pg_fixtures.pg_cluster
target_db = pg_fixtures.target_db


def synthetic_receipt(route="aliyun"):
    now = datetime.now(UTC)
    run = uuid4().hex
    receipt = {
        "schema_version": 1,
        "run_id": run,
        "route": route,
        "endpoint": cleanup.ENDPOINTS[route],
        "report_directory": "synthetic-private-run",
        "started_at": timestamp_to_db(now - timedelta(minutes=1)),
        "finished_at": timestamp_to_db(now + timedelta(minutes=1)),
        "status": "passed",
        "checks": list(cleanup.CHECKS),
        "operations": [],
        "accounts": [
            {"username": f"public_smoke_{run}_a", "id": 18, "registration": "confirmed"},
            {"username": f"public_smoke_{run}_b", "id": 19, "registration": "confirmed"},
        ],
        "record_ids": [2],
        "file_ids": [2],
        "cleanup": "not_performed; exact newly generated identities only",
        "limitations": ["Synthetic fixture only; no actual account credentials."],
        "server_instance_id": str(uuid4()),
    }
    uuids = [str(uuid4()) for _ in range(4)]
    request_ids = {5: uuids[0], 6: uuids[0], 7: uuids[1], 8: uuids[2], 9: uuids[3]}
    results = {0: 18, 1: 19, 5: 2, 6: 2, 9: 2}
    for index, name in enumerate(cleanup.OPERATIONS):
        operation = {
            "name": name,
            "actor_id": None if index < 2 else 19 if index in {4, 12} else 18,
            "request_id": request_ids.get(index),
            "status": "confirmed",
            "started_at": timestamp_to_db(now),
        }
        if index in results:
            operation["result_id"] = results[index]
        receipt["operations"].append(operation)
    return receipt


def synthetic_state(receipt):
    rows = {table: [] for table in cleanup.PLATFORM_COLUMNS}
    now = receipt["operations"][0]["started_at"]
    earlier = timestamp_to_db(cleanup.timestamp(now) - timedelta(days=1))

    def add(table, **values):
        rows[table].append({name: values.get(name) for name in cleanup.PLATFORM_COLUMNS[table]})

    # Non-contiguous IDs intentionally overlap across tables: admin17, original
    # conversation18/audit37 must survive users18/19 and record2/file2 removal.
    for identifier, username, role, created in [
        (17, "synthetic-original-admin", "operator", earlier),
        *[(item["id"], item["username"], "member", now) for item in receipt["accounts"]],
    ]:
        add(
            "users",
            id=identifier,
            username=username,
            username_normalized=username,
            password_hash="synthetic-hash-not-a-live-credential",
            created_at=created,
            last_login_at=now,
            role_code=role,
            account_status="active",
            updated_at=created,
        )
    for identifier, user in ((18, 17), (19, 18), (20, 19)):
        add(
            "conversations",
            id=identifier,
            user_id=user,
            title=cleanup.DEFAULT_CONVERSATION_TITLE,
            provider=None,
            model=None,
            created_at=earlier if user == 17 else now,
            updated_at=now,
        )
    add(
        "audit_logs",
        id=37,
        actor_user_id=17,
        action="operator.bootstrapped",
        entity_type="user",
        entity_id=17,
        details_json='{"role_code":"operator"}',
        created_at=earlier,
    )
    add(
        "life_records",
        id=2,
        user_id=18,
        category="water",
        occurred_at=now,
        local_date=now[:10],
        timezone_offset_minutes=0,
        content="合成公网验收饮水记录（非真实健康数据）",
        details_json='{"amount_ml":1}',
        source="manual",
        created_at=now,
        updated_at=now,
    )
    prefs = {**cleanup.DEFAULT_PREFERENCES, "nickname": "合成验收昵称一"}
    add(
        "user_preferences",
        user_id=18,
        preferences_json=json.dumps(prefs, ensure_ascii=False),
        updated_at=now,
    )
    stream = io.BytesIO()
    Image.frombytes("RGB", (192, 128), secrets.token_bytes(192 * 128 * 3)).save(
        stream, format="PNG"
    )
    content = stream.getvalue()
    sha = hashlib.sha256(content).hexdigest()
    add(
        "user_files",
        id=2,
        user_id=18,
        relative_path="embedded/18/" + uuid4().hex + ".png",
        original_name="synthetic-smoke.png",
        media_type="image/png",
        size_bytes=len(content),
        sha256=sha,
        created_at=now,
    )
    add(
        "user_file_contents",
        file_id=2,
        content=content,
        mime_type="image/png",
        sha256=sha,
        created_at=now,
    )
    for identifier, action, entity in (
        (38, "life_record.created", "life_record"),
        (39, "user_file.uploaded", "user_file"),
    ):
        add(
            "audit_logs",
            id=identifier,
            actor_user_id=18,
            action=action,
            entity_type=entity,
            entity_id=2,
            details_json=None,
            created_at=now,
        )
    for user in (18, 18, 19):
        add(
            "platform_sessions",
            token_digest=secrets.token_hex(32),
            user_id=user,
            created_at=now,
            expires_at=timestamp_to_db(cleanup.timestamp(now) + timedelta(hours=1)),
            revoked_at=now,
        )
    for index in (5, 7, 9):
        result = prefs if index == 7 else 2
        add(
            "rpc_requests",
            user_id=18,
            request_id=receipt["operations"][index]["request_id"],
            request_digest=secrets.token_hex(32),
            response_json=json.dumps({"result": result}),
            created_at=now,
        )
    add("auth_rate_buckets", bucket_id=8, window_start=1000000, attempts=7)
    return {
        "identity": {
            "kind": "postgresql",
            "label": LABEL,
            "host": "synthetic-local-only",
            "port": 5432,
            "database": "synthetic",
            "catalog": "synthetic-v2",
            "deployment_marker": "medical-app:aliyun:v1:" + DEPLOYMENT,
        },
        "rows": {
            table: sorted(items, key=lambda row, table=table: cleanup.key(table, row))
            for table, items in rows.items()
        },
    }


def test_plan_contains_exact_noncontiguous_ids_without_credentials():
    receipt = synthetic_receipt()
    state = synthetic_state(receipt)
    before = digest(state)
    plan = cleanup.build_plan(state, receipt)
    assert digest(state) == before
    assert plan["delete_ids"]["users"] == [[18], [19]]
    assert plan["delete_ids"]["conversations"] == [[19], [20]]
    assert plan["delete_ids"]["audit_logs"] == [[38], [39]]
    assert plan["delete_ids"]["life_records"] == [[2]]
    assert plan["delete_ids"]["user_files"] == [[2]]
    encoded = json.dumps(plan)
    for forbidden in ("password_hash", "synthetic-hash", "token_digest", "response_json"):
        assert forbidden not in encoded
    assert not any(row["token_digest"] in encoded for row in state["rows"]["platform_sessions"])
    assert not plan["shared_rate_buckets_modified"] and not plan["sequences_modified"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(status="failed"),
        lambda r: r["accounts"][0].update(registration="attempted_result_unconfirmed"),
        lambda r: r["accounts"][0].update(username="public_smoke_other_a"),
        lambda r: r["accounts"][1].update(id=18),
        lambda r: r["accounts"][0].update(id=True),
        lambda r: r["checks"].pop(),
        lambda r: r["record_ids"].append(3),
        lambda r: r["operations"][6].update(request_id=str(uuid4())),
        lambda r: r["operations"][9].update(result_id=1),
        lambda r: r["operations"][2].update(actor_id=17),
        lambda r: r.update(password="synthetic-blocked"),
        lambda r: r["limitations"].append("postgresql://invalid:never@synthetic.invalid/example"),
        lambda r: r.update(endpoint="https://synthetic.invalid/aliyun"),
        lambda r: r.update(cleanup="already_cleaned"),
    ],
)
def test_invalid_or_uncertain_receipt_refused(mutation):
    receipt = synthetic_receipt()
    mutation(receipt)
    with pytest.raises((TransferError, ValueError, TypeError)):
        cleanup.validate_receipt(receipt)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: s["rows"]["users"][1].update(role_code="operator"),
        lambda s: s["rows"]["life_records"][0].update(content="unexpected actual notes"),
        lambda s: s["rows"]["life_records"].append({**s["rows"]["life_records"][0], "id": 3}),
        lambda s: s["rows"]["platform_sessions"][0].update(revoked_at=None),
        lambda s: s["rows"]["rpc_requests"][0].update(request_id=str(uuid4())),
        lambda s: s["rows"]["user_file_contents"][0].update(content=b"not the synthetic image"),
        lambda s: s["rows"]["conversations"][1].update(title="unreviewed conversation"),
        lambda s: s["rows"]["audit_logs"][0].update(entity_type="member", entity_id=18),
        lambda s: s["rows"]["audit_logs"][0].update(entity_type="unknown_reference", entity_id=18),
        lambda s: s["rows"]["audit_logs"][0].update(details_json='{"member_id":18}'),
        lambda s: s["rows"]["audit_logs"][0].update(details_json='{"unknown_id":18}'),
        lambda s: s["rows"]["member_profiles"].append({"user_id": 18}),
        lambda s: s["rows"]["medical_reports"].append({"id": 6, "user_id": 17, "file_id": 2}),
    ],
)
def test_unexpected_business_external_relationships_refused(mutation):
    receipt = synthetic_receipt()
    state = synthetic_state(receipt)
    mutation(state)
    with pytest.raises(TransferError):
        cleanup.build_plan(state, receipt)


def test_export_reads_only_known_receipt_purpose_and_never_overwrites(tmp_path, monkeypatch):
    receipt = synthetic_receipt()
    calls = []

    def read(path, purpose):
        calls.append((path, purpose))
        return receipt

    monkeypatch.setattr(private_payload, "read_private_json", read)
    destination = tmp_path / "metadata.json"
    args = SimpleNamespace(run_directory=tmp_path / "run", destination=destination)
    result = cleanup.export_receipt(args)
    assert not result["credentials_read"]
    assert calls == [(args.run_directory / "receipt.dpapi", "aliyun-public-smoke/v1/receipt")]
    assert json.loads(destination.read_text(encoding="utf-8")) == receipt
    before = destination.read_bytes()
    with pytest.raises(TransferError):
        cleanup.export_receipt(args)
    assert destination.read_bytes() == before


def test_cli_existing_result_blocks_before_database_connect(tmp_path, monkeypatch, capsys):
    receipt_file, plan_file, result_file = (
        tmp_path / name for name in ("receipt.json", "plan.json", "result.json")
    )
    cleanup.write_json(synthetic_receipt(), receipt_file)
    cleanup.write_json({"existing": True}, result_file)

    def denied(*_args):
        pytest.fail("must refuse occupied output before touching a database")

    monkeypatch.setattr(cleanup, "target_connection", denied)
    assert (
        cleanup.main(
            [
                "apply",
                "--receipt",
                str(receipt_file),
                "--plan-file",
                str(plan_file),
                "--target-profile",
                str(tmp_path / "not-read.json"),
                "--confirm-plan",
                "0" * 64,
                "--result-file",
                str(result_file),
            ]
        )
        == 1
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "cleanup_not_verified"


def seed(connection, receipt):
    state = synthetic_state(receipt)
    with connection.transaction():
        connection.execute(
            sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                sql.Identifier(connection.info.dbname),
                sql.Literal("medical-app:aliyun:v1:" + DEPLOYMENT),
            )
        )
        order = (*_import_order(), "platform_sessions", "auth_rate_buckets", "rpc_requests")
        for table in order:
            for row in state["rows"][table]:
                statement = sql.SQL("INSERT INTO {}.{} ({}) VALUES ({})").format(
                    sql.Identifier(cleanup.PLATFORM_SCHEMA),
                    sql.Identifier(table),
                    sql.SQL(",").join(map(sql.Identifier, row)),
                    sql.SQL(",").join(sql.Placeholder() for _ in row),
                )
                connection.execute(statement, list(row.values()))
        # A used, advanced sequence must not be reset during cleanup.
        connection.execute("SELECT setval('medical_app_platform.users_id_seq', 77, true)")


def pg_state(connection, receipt):
    with connection.transaction():
        return cleanup.read_state(
            connection, label=LABEL, route=receipt["route"], deployment_id=DEPLOYMENT
        )


def test_real_pg_cleanup_keeps_admin_and_shared_state_exactly(target_db):
    receipt = synthetic_receipt()
    seed(target_db, receipt)
    before = pg_state(target_db, receipt)
    plan = cleanup.prepare(target_db, receipt, label=LABEL, deployment_id=DEPLOYMENT)
    assert pg_state(target_db, receipt) == before  # readonly plan
    result = cleanup.apply(
        target_db,
        receipt,
        plan,
        confirm_sha256=plan["plan_sha256"],
        label=LABEL,
        deployment_id=DEPLOYMENT,
    )
    assert result["status"] == "cleanup_committed"
    _selected, chosen = cleanup.selection(before, receipt)
    expected = {
        "identity": before["identity"],
        "rows": {
            table: [row for row in items if cleanup.key(table, row) not in chosen[table]]
            for table, items in before["rows"].items()
        },
    }
    after = pg_state(target_db, receipt)
    assert after == expected
    assert [row["id"] for row in after["rows"]["users"]] == [17]
    assert [row["id"] for row in after["rows"]["audit_logs"]] == [37]
    assert [row["id"] for row in after["rows"]["conversations"]] == [18]
    with target_db.transaction():
        assert target_db.execute(
            "SELECT last_value, is_called FROM medical_app_platform.users_id_seq"
        ).fetchone() == (77, True)
    with pytest.raises(TransferError):
        cleanup.apply(
            target_db,
            receipt,
            plan,
            confirm_sha256=plan["plan_sha256"],
            label=LABEL,
            deployment_id=DEPLOYMENT,
        )
    assert pg_state(target_db, receipt) == after


def test_real_pg_wrong_confirmation_or_changed_row_prevents_delete(target_db):
    receipt = synthetic_receipt()
    seed(target_db, receipt)
    plan = cleanup.prepare(target_db, receipt, label=LABEL, deployment_id=DEPLOYMENT)
    before = pg_state(target_db, receipt)
    with pytest.raises(TransferError, match="SHA256"):
        cleanup.apply(
            target_db, receipt, plan, confirm_sha256="0" * 64, label=LABEL, deployment_id=DEPLOYMENT
        )
    assert pg_state(target_db, receipt) == before
    with target_db.transaction():
        target_db.execute(
            "UPDATE medical_app_platform.users SET updated_at=%s WHERE id=17",
            (timestamp_to_db(datetime.now(UTC)),),
        )
    changed = pg_state(target_db, receipt)
    with pytest.raises(TransferError, match="指纹"):
        cleanup.apply(
            target_db,
            receipt,
            plan,
            confirm_sha256=plan["plan_sha256"],
            label=LABEL,
            deployment_id=DEPLOYMENT,
        )
    assert pg_state(target_db, receipt) == changed


def test_real_pg_post_delete_check_failure_rolls_back_every_row(target_db, monkeypatch):
    receipt = synthetic_receipt()
    seed(target_db, receipt)
    plan = cleanup.prepare(target_db, receipt, label=LABEL, deployment_id=DEPLOYMENT)
    before = pg_state(target_db, receipt)
    original = cleanup.read_state
    count = 0

    def altered(*args, **kwargs):
        nonlocal count
        value = original(*args, **kwargs)
        count += 1
        if count == 2:
            value = copy.deepcopy(value)
            value["rows"]["auth_rate_buckets"][0]["attempts"] += 1
        return value

    monkeypatch.setattr(cleanup, "read_state", altered)
    with pytest.raises(TransferError, match="回滚"):
        cleanup.apply(
            target_db,
            receipt,
            plan,
            confirm_sha256=plan["plan_sha256"],
            label=LABEL,
            deployment_id=DEPLOYMENT,
        )
    monkeypatch.setattr(cleanup, "read_state", original)
    assert pg_state(target_db, receipt) == before


def test_real_pg_external_fk_and_wrong_deployment_refused(target_db):
    receipt = synthetic_receipt()
    seed(target_db, receipt)
    before = pg_state(target_db, receipt)
    with pytest.raises(TransferError, match="身份"):
        cleanup.prepare(target_db, receipt, label=LABEL, deployment_id="e" * 32)
    assert pg_state(target_db, receipt) == before
    with target_db.transaction():
        target_db.execute(
            "CREATE TABLE public.synthetic_external (id BIGINT PRIMARY KEY, "
            "member BIGINT REFERENCES medical_app_platform.users(id) ON DELETE CASCADE)"
        )
        target_db.execute("INSERT INTO public.synthetic_external VALUES (1,18)")
    with pytest.raises(TransferError, match="外部外键"):
        cleanup.prepare(target_db, receipt, label=LABEL, deployment_id=DEPLOYMENT)
    with target_db.transaction():
        assert (
            target_db.execute("SELECT count(*) FROM medical_app_platform.users").fetchone()[0] == 3
        )
        assert target_db.execute("SELECT member FROM public.synthetic_external").fetchone()[0] == 18


def test_real_pg_writer_committing_while_apply_waits_for_lock_is_detected(target_db, pg_cluster):
    receipt = synthetic_receipt()
    seed(target_db, receipt)
    plan = cleanup.prepare(target_db, receipt, label=LABEL, deployment_id=DEPLOYMENT)
    ready, errors = Event(), []

    def writer():
        try:
            with psycopg.connect(**pg_cluster, dbname=target_db.info.dbname) as other:
                other.execute(
                    "UPDATE medical_app_platform.auth_rate_buckets SET attempts=8 WHERE bucket_id=8"
                )
                ready.set()
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    waiting = other.execute(
                        "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=%s AND NOT granted)",
                        (target_db.info.backend_pid,),
                    ).fetchone()[0]
                    if waiting:
                        return  # Commit only after apply is blocked on this writer.
                    time.sleep(0.02)
                raise AssertionError("synthetic apply never reached lock wait")
        except Exception as error:
            errors.append(type(error).__name__)
            ready.set()

    thread = Thread(target=writer, daemon=True)
    thread.start()
    assert ready.wait(8)
    try:
        with pytest.raises((TransferError, psycopg.errors.SerializationFailure)):
            cleanup.apply(
                target_db,
                receipt,
                plan,
                confirm_sha256=plan["plan_sha256"],
                label=LABEL,
                deployment_id=DEPLOYMENT,
            )
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive() and not errors
    after = pg_state(target_db, receipt)
    assert [row["id"] for row in after["rows"]["users"]] == [17, 18, 19]
    assert after["rows"]["auth_rate_buckets"][0]["attempts"] == 8
