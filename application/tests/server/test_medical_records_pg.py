"""Opt-in PostgreSQL 17 on a newly owned local tmpfs container, never cloud."""

import importlib
import secrets
import sqlite3
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from ollama_chat_app.services.health import HealthRecordNotFoundError, HealthService
from server import platform_transfer as transfer
from server.platform_database import PlatformDatabase
from server.platform_experience_schema import runtime_grant_ddl
from server.platform_medical_schema import CATEGORY_CHECK, OLD_CATEGORY_CHECK, category_check
from server.platform_rpc import RpcDispatcher, RpcError
from tests.server import test_platform_transfer_pg as fixture_module
from tests.server.test_medical_records_rpc import payload
from tests.server.test_platform_transfer import synthetic_source
from tools.aliyun_platform import bootstrap
from tools.aliyun_platform import upgrade_medical as upgrade

pg_cluster = fixture_module.pg_cluster
target_db = fixture_module.target_db


def migrate_v3(connection):
    migration = importlib.import_module("server.migrations.versions.platform_0003_medical_records")
    with (
        connection.transaction(),
        patch.object(migration, "op", SimpleNamespace(execute=connection.execute)),
    ):
        migration.upgrade()


def test_v3_preserves_all_29_tables_binary_and_readiness_requires_upgrade(
    target_db, pg_cluster, tmp_path
):
    source = synthetic_source(tmp_path / "synthetic.db")
    fixture_module.copy_to(source, target_db)
    before = transfer.postgres_snapshot(target_db, label="before")
    url = URL.create(
        "postgresql+psycopg",
        username="postgres",
        password=pg_cluster["password"],
        host=pg_cluster["host"],
        port=pg_cluster["port"],
        database=target_db.info.dbname,
    )
    database = PlatformDatabase(engine=create_engine(url, hide_parameters=True))
    try:
        with pytest.raises(sqlite3.OperationalError, match="升级"):
            database.initialize()
        migrate_v3(target_db)
        after = transfer.postgres_snapshot(target_db, label="after")
        assert before["rows"] == after["rows"] == source["rows"]
        assert before["identity"]["record_schema"] == 6
        assert after["identity"]["record_schema"] == 7
        with target_db.transaction():
            assert category_check(target_db) == (CATEGORY_CHECK, True)
        database.initialize()
        # The raw migration refuses repetition; the manager detects version first.
        with pytest.raises(Exception, match="unknown previous"):
            migrate_v3(target_db)
        assert transfer.postgres_snapshot(target_db, label="after") == after
    finally:
        database.close()


def test_transfer_old_schema6_allowed_but_medical_requires_verified_v3(target_db, tmp_path):
    source = synthetic_source(tmp_path / "synthetic.db")
    with sqlite3.connect(tmp_path / "synthetic.db") as local:
        # Build a genuine legacy category constraint, not merely downgrade a version number.
        local.execute("PRAGMA foreign_keys=OFF")
        create_sql = local.execute(
            "SELECT sql FROM sqlite_schema WHERE name='life_records'"
        ).fetchone()[0]
        local.execute(
            create_sql.replace('"life_records"', '"legacy_records"', 1)
            .replace("life_records (", "legacy_records (", 1)
            .replace(", 'medical'", "")
        )
        local.execute("INSERT INTO legacy_records SELECT * FROM life_records")
        local.execute("DROP TABLE life_records")
        local.execute("ALTER TABLE legacy_records RENAME TO life_records")
        local.execute("PRAGMA user_version=6")
    legacy = transfer.sqlite_snapshot(tmp_path / "synthetic.db", label="legacy-six")
    assert legacy["rows"] == source["rows"]
    target = transfer.postgres_snapshot(target_db, label="old-target")
    transfer.plan_transfer(legacy, target)
    medical = deepcopy(source)
    medical["rows"]["life_records"][0]["category"] = "medical"
    with pytest.raises(transfer.TransferError, match="升级"):
        transfer.plan_transfer(medical, target)
    migrate_v3(target_db)
    fixture_module.copy_to(medical, target_db)
    assert transfer.postgres_snapshot(target_db, label="new")["rows"] == medical["rows"]


def own_v2_runtime(connection, deployment, monkeypatch):
    monkeypatch.setattr(bootstrap, "ADMIN", "postgres")
    with connection.transaction():
        connection.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS NOINHERIT CONNECTION LIMIT 5"
            ).format(sql.Identifier(bootstrap.RUNTIME))
        )
        for statement in runtime_grant_ddl():
            connection.execute(statement)
        connection.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(connection.info.dbname)
            )
        )
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(connection.info.dbname), sql.Identifier(bootstrap.RUNTIME)
            )
        )
        marker = (
            f"{bootstrap.MARKER}:{deployment['id']}:"
            f"{bootstrap.ddl_digest(revision='platform_0002')}:{bootstrap.catalog_digest(connection)}"
        )
        connection.execute(
            sql.SQL("COMMENT ON ROLE {} IS {}").format(
                sql.Identifier(bootstrap.RUNTIME), sql.Literal(marker)
            )
        )
        bootstrap.verify_installation(connection, deployment, revision="platform_0002")


def test_aliyun_plan_apply_exact_old_marker_no_row_or_sequence_changes(
    target_db, tmp_path, monkeypatch
):
    source = synthetic_source(tmp_path / "source.db")
    fixture_module.copy_to(source, target_db)
    deployment = {"id": uuid4().hex}
    own_v2_runtime(target_db, deployment, monkeypatch)
    plan = upgrade.plan_upgrade(target_db, deployment)
    assert upgrade.plan_upgrade(target_db, deployment) == plan
    with target_db.transaction():
        sequences = target_db.execute(
            "SELECT sequencename,last_value FROM pg_sequences "
            "WHERE schemaname='medical_app_platform' ORDER BY 1"
        ).fetchall()
        assert category_check(target_db) == (OLD_CATEGORY_CHECK, True)
    with pytest.raises(upgrade.DeploymentError, match="SHA256"):
        upgrade.apply_upgrade(target_db, deployment, plan, "0" * 64)
    changed = deepcopy(plan)
    changed["data"]["counts"]["users"] = 999
    with pytest.raises(upgrade.DeploymentError, match="changed"):
        upgrade.apply_upgrade(target_db, deployment, changed, plan["plan_sha256"])
    # Simulate failure after DDL: PostgreSQL rolls the constraint and marker back.
    original = upgrade.data_fingerprint
    count = 0

    def interrupted(connection):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("synthetic interrupted verification")
        return original(connection)

    with monkeypatch.context() as isolated:
        isolated.setattr(upgrade, "data_fingerprint", interrupted)
        with pytest.raises(RuntimeError, match="synthetic interrupted"):
            upgrade.apply_upgrade(target_db, deployment, plan, plan["plan_sha256"])
    assert upgrade.plan_upgrade(target_db, deployment) == plan
    result = upgrade.apply_upgrade(target_db, deployment, plan, plan["plan_sha256"])
    assert result["all_existing_rows_identical"] and not result["runtime_password_rotated"]
    with target_db.transaction():
        bootstrap.verify_installation(target_db, deployment)
        assert upgrade.data_fingerprint(target_db) == plan["data"]
        assert (
            target_db.execute(
                "SELECT sequencename,last_value FROM pg_sequences "
                "WHERE schemaname='medical_app_platform' ORDER BY 1"
            ).fetchall()
            == sequences
        )
    # Reusing an old plan refuses without re-running DDL or changing its marker.
    with pytest.raises(upgrade.DeploymentError, match="fingerprint"):
        upgrade.apply_upgrade(target_db, deployment, plan, plan["plan_sha256"])
    with target_db.transaction():
        bootstrap.verify_installation(target_db, deployment)


def test_v3_runtime_rls_and_batch_delete_rpc(target_db, pg_cluster):
    migrate_v3(target_db)
    password = secrets.token_hex(32)
    with target_db.transaction():
        exists = target_db.execute(
            "SELECT 1 FROM pg_roles WHERE rolname=%s", (bootstrap.RUNTIME,)
        ).fetchone()
        if not exists:
            target_db.execute(
                sql.SQL("CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS").format(
                    sql.Identifier(bootstrap.RUNTIME)
                )
            )
        target_db.execute(
            sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                sql.Identifier(bootstrap.RUNTIME), sql.Literal(password)
            )
        )
        for statement in runtime_grant_ddl():
            target_db.execute(statement)
        for user in (17, 29):
            target_db.execute(
                "INSERT INTO medical_app_platform.users "
                "(id,username,username_normalized,password_hash,role_code,created_at,updated_at) "
                "VALUES(%s,%s,%s,'synthetic-unused-hash','member',%s,%s)",
                (
                    user,
                    f"synthetic-rpc-{user}",
                    f"synthetic-rpc-{user}",
                    "2026-09-09T00:00:00.000000Z",
                    "2026-09-09T00:00:00.000000Z",
                ),
            )
    url = URL.create(
        "postgresql+psycopg",
        username=bootstrap.RUNTIME,
        password=password,
        host=pg_cluster["host"],
        port=pg_cluster["port"],
        database=target_db.info.dbname,
    )
    database = PlatformDatabase(engine=create_engine(url, hide_parameters=True))
    database.initialize()
    health = HealthService(database)
    rpc = RpcDispatcher(database, {"health": health})
    try:
        with database.identity(user_id=29, role_code="member"):
            foreign = health.add_life_record(29, "medical", datetime.now(UTC), "合成他人事实")
        with database.identity(user_id=17, role_code="member"):
            own = health.add_life_record(17, "medical", datetime.now(UTC), "合成本人事实")
            with database.connect() as connection:
                assert (
                    connection.execute("SELECT id FROM life_records WHERE user_id=29").fetchall()
                    == []
                )
            with pytest.raises(HealthRecordNotFoundError):
                rpc.invoke(17, "health", "delete_life_records", payload(17, [own, foreign]))
            assert [row["id"] for row in health.list_life_records(17)] == [own]
            request = payload(17, [own])
            first = rpc.invoke(17, "health", "delete_life_records", request)
            assert rpc.invoke(17, "health", "delete_life_records", request) == first
            assert health.list_life_records(17) == []
            with pytest.raises(RpcError) as error:
                rpc.invoke(17, "health", "delete_life_records", payload(29, [foreign]))
            assert error.value.status == 403
        with database.identity(user_id=29, role_code="member"):
            assert [row["id"] for row in health.list_life_records(29)] == [foreign]
        with target_db.transaction():
            assert target_db.execute(
                "SELECT count(*) FROM medical_app_platform.audit_logs "
                "WHERE action='life_record.deleted' AND actor_user_id=17"
            ).fetchone() == (1,)
    finally:
        database.close()
