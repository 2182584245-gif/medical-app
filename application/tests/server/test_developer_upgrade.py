"""Owned v3/v4 upgrade gates and opt-in disposable PostgreSQL evidence."""

import importlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from psycopg import sql
from psycopg.pq import TransactionStatus

from server import platform_developer_catalog as catalog
from server import platform_developer_schema as v4
from server import platform_transfer
from server.platform_experience_schema import PLATFORM_MARKER, PLATFORM_SCHEMA, runtime_grant_ddl
from tests.server import test_platform_transfer_pg as fixture_module
from tools.aliyun_platform import bootstrap, maintenance
from tools.aliyun_platform import upgrade_developer as upgrade

pg_cluster = fixture_module.pg_cluster
target_db = fixture_module.target_db


def test_frozen_v3_ddl_preserved_and_v4_strictly_additive():
    v3 = bootstrap.ddl(revision="platform_0003")
    statements = bootstrap.ddl(revision="platform_0004")
    assert statements[: len(v3)] == v3
    assert len([s for s in statements if s.startswith("CREATE TABLE ")]) == 37
    assert set(catalog.NEW_COLUMNS) == set(v4.NEW_TABLES)
    assert catalog.POLICY_KEYS
    with pytest.raises(bootstrap.DeploymentError):
        bootstrap.ddl(revision="platform_9999")


@pytest.mark.parametrize("confirmation", [None, "", "a" * 63, "b" * 64])
def test_upgrade_requires_complete_plan_confirmation(confirmation):
    connection = SimpleNamespace(info=SimpleNamespace(transaction_status=TransactionStatus.IDLE))
    with pytest.raises(bootstrap.DeploymentError, match="SHA256"):
        upgrade.apply_upgrade(connection, {}, {"plan_sha256": "a" * 64}, confirmation)


@pytest.fixture
def owned_v3(target_db, monkeypatch):
    monkeypatch.setattr(bootstrap, "ADMIN", "postgres")
    deployment = {"id": "b" * 32}
    with target_db.transaction():
        if not target_db.execute(
            "SELECT 1 FROM pg_roles WHERE rolname=%s", (bootstrap.RUNTIME,)
        ).fetchone():
            target_db.execute(
                sql.SQL("CREATE ROLE {} LOGIN NOINHERIT NOBYPASSRLS CONNECTION LIMIT 5").format(
                    sql.Identifier(bootstrap.RUNTIME)
                )
            )
        migration = importlib.import_module(
            "server.migrations.versions.platform_0003_medical_records"
        )
        with patch.object(migration, "op", SimpleNamespace(execute=target_db.execute)):
            migration.upgrade()
        for statement in runtime_grant_ddl():
            target_db.execute(statement)
        target_db.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(target_db.info.dbname)
            )
        )
        target_db.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(target_db.info.dbname), sql.Identifier(bootstrap.RUNTIME)
            )
        )
        target_db.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        marker = (
            f"{bootstrap.MARKER}:{deployment['id']}:"
            f"{bootstrap.ddl_digest(revision='platform_0003')}:"
            f"{bootstrap.catalog_digest(target_db)}"
        )
        target_db.execute(
            sql.SQL("COMMENT ON ROLE {} IS {}").format(
                sql.Identifier(bootstrap.RUNTIME), sql.Literal(marker)
            )
        )
        bootstrap.verify_installation(target_db, deployment)
    return target_db, deployment


def install_frozen_v4(connection):
    with connection.transaction():
        for statement in (*v4.TABLE_DDL, *v4.policy_ddl(), *v4.grant_ddl()):
            connection.execute(statement)
        for table in v4.NEW_TABLES:
            connection.execute(
                sql.SQL("COMMENT ON TABLE {} IS {}").format(
                    sql.Identifier(PLATFORM_SCHEMA, table), sql.Literal(PLATFORM_MARKER)
                )
            )


def test_real_pg_frozen_catalog_digest(owned_v3):
    connection, _ = owned_v3
    install_frozen_v4(connection)
    with connection.transaction():
        actual = catalog.inventory_digest(catalog.inventory(connection))
        assert actual == catalog.REVIEWED_V4_SHA256
        catalog.require_reviewed_v4(connection)


def test_real_pg_upgrade_preserves_rows_old_catalog_and_refuses_replay(owned_v3, tmp_path):
    connection, deployment = owned_v3
    source = fixture_module.synthetic_source(tmp_path / "synthetic.db")
    fixture_module.copy_to(source, connection)
    plan = upgrade.plan_upgrade(connection, deployment)
    assert plan["data"]["counts"]["users"] == 3
    result = upgrade.apply_upgrade(connection, deployment, plan, plan["plan_sha256"])
    assert result["all_existing_rows_identical"] and not result["developer_grant_created"]
    with connection.transaction():
        bootstrap.verify_installation(connection, deployment, revision="platform_0004")
        assert (
            bootstrap.catalog_digest(connection, exclude_developer=True)
            == plan["old_catalog_sha256"]
        )
    snapshot = platform_transfer.postgres_snapshot(connection, label="v4-test")
    assert set(snapshot["rows"]).isdisjoint(v4.NEW_TABLES)
    assert snapshot["rows"] == source["rows"]
    with pytest.raises(bootstrap.DeploymentError):
        upgrade.apply_upgrade(connection, deployment, plan, plan["plan_sha256"])


def test_real_pg_altered_v3_policy_refuses_before_upgrade(owned_v3):
    connection, deployment = owned_v3
    plan = upgrade.plan_upgrade(connection, deployment)
    with connection.transaction():
        connection.execute(
            "CREATE POLICY unreviewed_extra ON medical_app_platform.users USING(true)"
        )
    with pytest.raises(bootstrap.DeploymentError, match="fingerprint"):
        upgrade.apply_upgrade(connection, deployment, plan, plan["plan_sha256"])
    with connection.transaction():
        assert (
            connection.execute(
                "SELECT to_regclass('medical_app_platform.developer_grants')"
            ).fetchone()[0]
            is None
        )


def test_real_pg_v4_policy_drift_refuses_business_transfer(owned_v3):
    connection, deployment = owned_v3
    plan = upgrade.plan_upgrade(connection, deployment)
    upgrade.apply_upgrade(connection, deployment, plan, plan["plan_sha256"])
    with connection.transaction():
        connection.execute(
            "ALTER POLICY developer_read ON medical_app_platform.developer_grants USING(true)"
        )
    with pytest.raises(platform_transfer.TransferError, match="安全结构"):
        platform_transfer.postgres_snapshot(connection, label="refused-v4")


def test_real_pg_changed_rows_refuse_v4_before_any_schema_write(owned_v3):
    connection, deployment = owned_v3
    plan = upgrade.plan_upgrade(connection, deployment)
    with connection.transaction():
        connection.execute(
            "INSERT INTO medical_app_platform.auth_rate_buckets "
            "(bucket_id,window_start,attempts) VALUES(123,0,1)"
        )
    with pytest.raises(bootstrap.DeploymentError, match="rows changed"):
        upgrade.apply_upgrade(connection, deployment, plan, plan["plan_sha256"])
    with connection.transaction():
        assert (
            connection.execute(
                "SELECT to_regclass('medical_app_platform.developer_grants')"
            ).fetchone()[0]
            is None
        )


def test_real_pg_backup_catalog_tracks_v4_columns_policies_and_counts(owned_v3, monkeypatch):
    import json

    connection, deployment = owned_v3
    monkeypatch.setattr(maintenance, "ADMIN", "postgres")

    def query(_database, statement):
        with connection.transaction():
            return json.dumps(connection.execute(statement).fetchone()[0])

    monkeypatch.setattr(maintenance, "_pg", query)
    before = maintenance._catalog("synthetic")
    assert len([r for r in before["relations"] if r["relkind"] == "r"]) == 32
    plan = upgrade.plan_upgrade(connection, deployment)
    upgrade.apply_upgrade(connection, deployment, plan, plan["plan_sha256"])
    after = maintenance._catalog("synthetic")
    assert len([r for r in after["relations"] if r["relkind"] == "r"]) == 37
    assert after["constraints"] and after["policies"] and after["columns"]
    with connection.transaction():
        connection.execute(
            "ALTER POLICY developer_read ON medical_app_platform.developer_grants USING(true)"
        )
    assert maintenance._catalog("synthetic") != after


def test_v4_images_include_developer_modules_and_upgrade_entrypoint():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    allowlist = (root / "tools/aliyun_platform/Dockerfile.dockerignore").read_text().splitlines()
    for path in (
        "server/platform_developer.py",
        "server/platform_developer_schema.py",
        "server/platform_developer_catalog.py",
        "tools/aliyun_platform/upgrade_developer.py",
    ):
        assert "!" + path in allowlist
    assert (
        "FROM medical-app-aliyun-api:v4"
        in (root / "tools/aliyun_platform/Transfer.Dockerfile").read_text()
    )
