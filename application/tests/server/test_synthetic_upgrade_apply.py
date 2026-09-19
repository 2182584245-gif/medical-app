"""Opt-in owned local PG tests; no production target or credentials accepted."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from psycopg import sql
from psycopg.pq import TransactionStatus

from server import platform_transfer as transfer
from server.platform_transfer_assets import stage_assets
from tests.server import test_developer_upgrade as developer_fixture
from tests.server import test_synthetic_upgrade_plan_v170 as plan_fixture
from tools import plan_synthetic_upgrade_v170 as planner
from tools.aliyun_platform import upgrade_developer
from tools.aliyun_platform import upgrade_synthetic as executor

pg_cluster = developer_fixture.pg_cluster
target_db = developer_fixture.target_db
owned_v3 = developer_fixture.owned_v3
corpus = plan_fixture.corpus


def test_confirmation_fails_without_touching_a_connection():
    connection = SimpleNamespace(info=SimpleNamespace(transaction_status=TransactionStatus.IDLE))
    with pytest.raises(executor.UpgradeError, match="SHA256"):
        executor.apply_upgrade(
            connection,
            {},
            baseline={},
            source={},
            previous_plan={},
            reviewed_plan={},
            confirm_sha256="wrong",
        )


def test_json_merge_preserves_private_siblings_and_refuses_excess_depth():
    value = {"appearance": {"theme": "old", "private": "keep"}, "unrelated": [1, 2]}
    executor.merge_json(value, {"appearance": {"theme": "new"}})
    assert value == {"appearance": {"theme": "new", "private": "keep"}, "unrelated": [1, 2]}
    with pytest.raises(executor.UpgradeError):
        executor.merge_json({}, {}, depth=21)


def test_explicit_patch_allowlist_matches_reviewed_planner_but_excludes_credentials():
    for table, fields in planner.DISPLAY_PATCHES.items():
        assert executor.PATCH_FIELDS[table] == fields
    assert "users" not in executor.INSERT_TABLES | set(executor.PATCH_FIELDS)
    assert not executor.PROTECTED_TABLES & executor.INSERT_TABLES
    assert not executor.PROTECTED_TABLES & set(executor.PATCH_FIELDS)


@pytest.fixture
def prepared(owned_v3, corpus, tmp_path, monkeypatch):
    connection, deployment = owned_v3
    marker = executor.MARKER + ":" + deployment["id"]
    with connection.transaction():
        connection.execute(
            sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                sql.Identifier(connection.info.dbname), sql.Literal(marker)
            )
        )

    # The inherited fixture connects only to its independently labelled new
    # tmpfs PG17 on 127.0.0.1; only the TLS/host gate is replaced here. The full
    # role/catalog/migration checks below are real PostgreSQL checks unchanged.
    def local_gate(conn, _user, selected):
        assert conn is connection and conn.info.host == "127.0.0.1"
        assert selected == deployment
        assert (
            conn.execute(
                "SELECT shobj_description(oid,'pg_database') FROM pg_database "
                "WHERE datname=current_database()"
            ).fetchone()[0]
            == marker
        )

    monkeypatch.setattr(executor.bootstrap, "check_target", local_gate)
    baseline = copy.deepcopy(corpus["baseline"])
    source = copy.deepcopy(corpus["source"])
    target_root = tmp_path / "target-assets"
    target_root.mkdir()
    empty = transfer.postgres_snapshot(connection, label="owned-synthetic-target")
    history, _ = transfer.plan_transfer(
        baseline, empty, mode="append-only", entity_types={"member_profile": "users"}
    )
    stage_assets(baseline, history, target_root)
    transfer.apply_transfer(
        connection,
        baseline,
        empty,
        history,
        confirm_sha256=history["plan_sha256"],
        target_asset_root=target_root,
    )
    v4_plan = upgrade_developer.plan_upgrade(connection, deployment)
    upgrade_developer.apply_upgrade(connection, deployment, v4_plan, v4_plan["plan_sha256"])
    with connection.transaction():
        # Existing private fields and protected runtime state must survive.
        connection.execute(
            "INSERT INTO medical_app_platform.auth_rate_buckets "
            "(bucket_id,window_start,attempts) VALUES(101,0,1)"
        )
        connection.execute(
            "INSERT INTO medical_app_platform.developer_grants "
            "VALUES(1,1,'2026-09-19T00:00:00.000000Z')"
        )
        row = connection.execute(
            "SELECT user_id,preferences_json FROM medical_app_platform."
            "user_preferences ORDER BY user_id LIMIT 1"
        ).fetchone()
        value = json.loads(row[1])
        value["private_owner_note"] = "Synthetic private note to preserve"
        connection.execute(
            "UPDATE medical_app_platform.user_preferences SET preferences_json=%s WHERE user_id=%s",
            (json.dumps(value), row[0]),
        )
    plan = executor.plan_upgrade(
        connection,
        deployment,
        baseline=baseline,
        source=source,
        previous_plan=history,
        prepared_at=plan_fixture.PREPARED,
        target_label="owned-synthetic-target",
        target_asset_root=target_root,
    )
    assert plan["status"] == "review_required"
    return {
        "connection": connection,
        "deployment": deployment,
        "baseline": baseline,
        "source": source,
        "previous_plan": history,
        "reviewed_plan": plan,
        "target_asset_root": target_root,
        "confirm_sha256": plan["plan_sha256"],
    }


def snapshot(prepared):
    return transfer.postgres_snapshot(
        prepared["connection"],
        label="owned-synthetic-target",
        asset_root=prepared["target_asset_root"],
    )


def sequence_state(prepared):
    with prepared["connection"].transaction():
        return (
            prepared["connection"]
            .execute("SELECT last_value,is_called FROM medical_app_platform.life_records_id_seq")
            .fetchone()
        )


def test_real_pg_expanded_batch_preserves_identities_private_fields_and_idempotency(prepared):
    before = snapshot(prepared)
    with prepared["connection"].transaction():
        protected = executor._protected_fingerprint(prepared["connection"])
    result = executor.apply_upgrade(**prepared)
    assert result["status"] == "committed"
    assert result["inserted_counts"]["life_records"] == 4632
    assert result["existing_user_rows_unchanged"] and result["security_rows_unchanged"]
    assert result["fictional_advisor_terms_patched"] <= 2
    after = snapshot(prepared)
    assert transfer.digest(after["rows"]["users"]) == transfer.digest(before["rows"]["users"])
    assert len(after["rows"]["life_records"]) == len(before["rows"]["life_records"]) + 4632
    assert "Synthetic private note to preserve" in json.dumps(after["rows"]["user_preferences"])
    with prepared["connection"].transaction():
        assert executor._protected_fingerprint(prepared["connection"]) == protected
        maximum = (
            prepared["connection"]
            .execute("SELECT max(id) FROM medical_app_platform.life_records")
            .fetchone()[0]
        )
        assert (
            prepared["connection"]
            .execute("SELECT nextval('medical_app_platform.life_records_id_seq')")
            .fetchone()[0]
            > maximum
        )
        prepared["connection"].execute(
            "DELETE FROM medical_app_platform.life_records WHERE id=%s", (maximum,)
        )
    replay_before = snapshot(prepared)
    assert executor.apply_upgrade(**prepared)["status"] == "already_applied"
    assert transfer.digest(snapshot(prepared)) == transfer.digest(replay_before)


def test_real_pg_changed_target_refuses_before_writes(prepared):
    with prepared["connection"].transaction():
        prepared["connection"].execute(
            "UPDATE medical_app_platform.member_profiles "
            "SET display_name='Synthetic owner correction' WHERE user_id=4"
        )
    before = transfer.digest(snapshot(prepared))
    with pytest.raises(executor.UpgradeError, match="changed"):
        executor.apply_upgrade(**prepared)
    assert transfer.digest(snapshot(prepared)) == before


def test_real_pg_injected_late_failure_rolls_back_rows_ledger_and_sequences(prepared, monkeypatch):
    before = transfer.digest(snapshot(prepared))
    sequence_before = sequence_state(prepared)
    original = executor._restart_sequences

    def fail_after_sequences(*args):
        original(*args)
        raise RuntimeError("synthetic injected failure after transactional sequence restart")

    monkeypatch.setattr(executor, "_restart_sequences", fail_after_sequences)
    with pytest.raises(executor.UpgradeError, match="not committed"):
        executor.apply_upgrade(**prepared)
    assert transfer.digest(snapshot(prepared)) == before
    assert sequence_state(prepared) == sequence_before


def test_real_pg_modified_plan_digest_cannot_authorize_extra_user_write(prepared):
    bad = copy.deepcopy(prepared["reviewed_plan"])
    bad["inserts"]["users"] = [copy.deepcopy(prepared["source"]["rows"]["users"][0])]
    bad.pop("plan_sha256")
    bad["plan_sha256"] = transfer.digest(bad)
    inputs = {**prepared, "reviewed_plan": bad, "confirm_sha256": bad["plan_sha256"]}
    before = transfer.digest(snapshot(prepared))
    with pytest.raises(executor.UpgradeError, match="changed"):
        executor.apply_upgrade(**inputs)
    assert transfer.digest(snapshot(prepared)) == before


def test_real_pg_protected_state_mutation_is_detected_and_rolled_back(prepared, monkeypatch):
    before = transfer.digest(snapshot(prepared))
    with prepared["connection"].transaction():
        protected = executor._protected_fingerprint(prepared["connection"])
    original = executor._restart_sequences

    def inject_protected_change(connection, *args):
        original(connection, *args)
        connection.execute("UPDATE medical_app_platform.developer_grants SET enabled=0")

    monkeypatch.setattr(executor, "_restart_sequences", inject_protected_change)
    with pytest.raises(executor.UpgradeError, match="developer state changed"):
        executor.apply_upgrade(**prepared)
    assert transfer.digest(snapshot(prepared)) == before
    with prepared["connection"].transaction():
        assert executor._protected_fingerprint(prepared["connection"]) == protected


def test_real_pg_existing_high_sequence_is_not_lowered(prepared):
    with prepared["connection"].transaction():
        prepared["connection"].execute(
            "ALTER SEQUENCE medical_app_platform.life_records_id_seq RESTART WITH 1000000"
        )
    assert executor.apply_upgrade(**prepared)["status"] == "committed"
    with prepared["connection"].transaction():
        value = (
            prepared["connection"]
            .execute("SELECT nextval('medical_app_platform.life_records_id_seq')")
            .fetchone()[0]
        )
        assert value >= 1000000


def test_cli_wrong_identity_fails_before_reading_artifacts(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        executor, "container_gate", lambda: {"kind": executor.MARKER, "id": "a" * 32}
    )
    monkeypatch.setattr(executor.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(executor, "read_artifact", lambda _p: pytest.fail("no artifact read"))
    args = ["plan", "--deployment-id", "b" * 32]
    for name in ("baseline", "source", "history", "plan-file"):
        args += ["--" + name, str(Path(tmp_path) / name)]
    assert executor.main(args) == 1
    assert "synthetic_upgrade_not_verified" in capsys.readouterr().out
