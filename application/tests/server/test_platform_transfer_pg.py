"""Explicit opt-in, owned new LOCAL PostgreSQL container; never accepts cloud URLs."""

from __future__ import annotations

import importlib
import json
import os
import secrets
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import psycopg
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from server import platform_transfer as transfer
from tests.server.test_platform_transfer import synthetic_source

IMAGE = (
    "postgres:17-bookworm@sha256:051f7b7b3abdd564d5d1bd1e8c4b9c1b6e77087d1dd22020ede611c096a272e0"
)
LABEL = "healthlife.synthetic-platform-transfer"


def docker(*args):
    result = subprocess.run(
        [shutil.which("docker"), "--context", "desktop-linux", *args],
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("Synthetic local Docker operation failed; output withheld.")
    return result.stdout.decode("utf-8").strip()


@pytest.fixture(scope="module")
def pg_cluster():
    if os.environ.get("MEDICAL_APP_RUN_TRANSFER_PG_TESTS") != "synthetic-local-only":
        pytest.skip("explicit local synthetic PostgreSQL gate required")
    context = json.loads(docker("context", "inspect", "desktop-linux"))
    assert context[0]["Endpoints"]["docker"]["Host"] == "npipe:////./pipe/dockerDesktopLinuxEngine"
    nonce, password = uuid.uuid4().hex, secrets.token_hex(24)
    name = "medical-transfer-test-" + nonce
    network = name + "-network"
    # Docker's isolated gateway suppresses published ports, so this test-only
    # bridge publishes ONLY loopback. No external endpoints or credentials exist.
    docker("network", "create", "--label", f"{LABEL}={nonce}", network)
    container = None
    try:
        container = docker(
            "run",
            "-d",
            "--pull=never",
            "--name",
            name,
            "--network",
            network,
            "--label",
            f"{LABEL}={nonce}",
            "--log-driver=none",
            "--memory=768m",
            "--tmpfs",
            "/var/lib/postgresql/data:rw,nosuid,size=512m",
            "-p",
            "127.0.0.1::5432",
            "-e",
            f"POSTGRES_PASSWORD={password}",
            IMAGE,
        )
        info = json.loads(docker("inspect", container))[0]
        port = int(info["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"])
        deadline = time.monotonic() + 45
        while True:
            try:
                with psycopg.connect(
                    host="127.0.0.1",
                    port=port,
                    dbname="postgres",
                    user="postgres",
                    password=password,
                    connect_timeout=1,
                ):
                    break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    pytest.fail("new synthetic PostgreSQL did not become ready")
                time.sleep(0.2)
        yield {"host": "127.0.0.1", "port": port, "user": "postgres", "password": password}
    finally:
        if container:
            info = json.loads(docker("inspect", container))[0]
            assert info["Id"] == container and info["Config"]["Labels"].get(LABEL) == nonce
            docker("rm", "-f", container)
        info = json.loads(docker("network", "inspect", network))[0]
        assert info["Labels"].get(LABEL) == nonce
        docker("network", "rm", network)


@pytest.fixture
def target_db(pg_cluster):
    name = "transfer_" + uuid.uuid4().hex
    assert name.startswith("transfer_")
    with psycopg.connect(**pg_cluster, dbname="postgres", autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url = URL.create(
        "postgresql+psycopg",
        database=name,
        username="postgres",
        password=pg_cluster["password"],
        host="127.0.0.1",
        port=pg_cluster["port"],
    )
    engine = create_engine(url, hide_parameters=True)
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        for module in ("platform_0001_full_platform", "platform_0002_desktop_experience"):
            migration = importlib.import_module("server.migrations.versions." + module)
            with patch.object(migration, "op", operations):
                migration.upgrade()
    engine.dispose()
    with psycopg.connect(**pg_cluster, dbname=name) as connection:
        yield connection
    # Database is on this fixture's new tmpfs container; no DROP DATABASE needed.


def copy_to(source, connection, *, mode="empty-only"):
    target = transfer.postgres_snapshot(connection, label="synthetic-pg-target")
    plan, _rows = transfer.plan_transfer(source, target, mode=mode)
    result = transfer.apply_transfer(
        connection, source, target, plan, confirm_sha256=plan["plan_sha256"]
    )
    return target, plan, result


def test_real_pg_all_29_rows_binary_and_credentials_excluded(target_db, tmp_path):
    source = synthetic_source(tmp_path / "source.db")
    _target, _plan, result = copy_to(source, target_db)
    actual = transfer.postgres_snapshot(target_db, label="synthetic-post-commit")
    assert result["status"] == "committed" and result["verified_row_content"]
    assert actual["rows"] == source["rows"]
    with target_db.transaction():
        for table in transfer.EXCLUDED[:3]:
            assert (
                target_db.execute(
                    sql.SQL("SELECT count(*) FROM {}.{}").format(
                        sql.Identifier(transfer.PLATFORM_SCHEMA),
                        sql.Identifier(table),
                    )
                ).fetchone()[0]
                == 0
            )
        assert (
            target_db.execute("SELECT nextval('medical_app_platform.users_id_seq')").fetchone()[0]
            > 3
        )


def test_nonempty_pg_append_preserves_every_existing_row(target_db, tmp_path):
    first = synthetic_source(tmp_path / "a.db", "synthetic-a")
    second = synthetic_source(tmp_path / "b.db", "synthetic-b")
    copy_to(first, target_db)
    before, plan, result = copy_to(second, target_db, mode="append-only")
    after = transfer.postgres_snapshot(target_db, label="after")
    assert result["inserted_counts"]["users"] == 3
    for table, old in before["rows"].items():
        assert all(row in after["rows"][table] for row in old)
        assert len(after["rows"][table]) == len(old) + len(second["rows"][table])
    imported = next(row for row in after["rows"]["orders"] if row["id"] == 2)
    assert imported["member_user_id"] == plan["id_mapping"]["users"]["2"]


def test_pg_mid_import_constraint_failure_rolls_back_every_table(target_db, tmp_path):
    source = synthetic_source(tmp_path / "source.db")
    source["rows"]["visit_task_details"][0]["latitude"] = 999.0
    source["rows"]["visit_task_details"][0]["longitude"] = 1.0
    target = transfer.postgres_snapshot(target_db, label="target")
    plan, _ = transfer.plan_transfer(source, target)
    with pytest.raises(transfer.TransferError, match="未确认完成"):
        transfer.apply_transfer(target_db, source, target, plan, confirm_sha256=plan["plan_sha256"])
    assert transfer.postgres_snapshot(target_db, label="target") == target


def test_pg_changed_target_and_duplicate_apply_refused(target_db, tmp_path):
    source = synthetic_source(tmp_path / "source.db")
    target, plan, _ = copy_to(source, target_db)
    with pytest.raises(transfer.TransferError, match="预检后已改变"):
        transfer.apply_transfer(target_db, source, target, plan, confirm_sha256=plan["plan_sha256"])
    assert len(transfer.postgres_snapshot(target_db, label="after")["rows"]["users"]) == 3


def test_pg_readonly_source_to_second_pg_is_direction_independent(
    pg_cluster, target_db, tmp_path, monkeypatch
):
    from server.platform_transfer_package import generate_key
    from tools.aliyun_platform import transfer as cli

    source = synthetic_source(tmp_path / "source.db")
    copy_to(source, target_db)
    original = transfer.postgres_snapshot(target_db, label="synthetic-cloud-a")
    # A second target must be another physical database, not just another label.
    name = "transfer_" + uuid.uuid4().hex
    with psycopg.connect(**pg_cluster, dbname="postgres", autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url = URL.create(
        "postgresql+psycopg",
        database=name,
        username="postgres",
        password=pg_cluster["password"],
        host="127.0.0.1",
        port=pg_cluster["port"],
    )
    engine = create_engine(url, hide_parameters=True)
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        for module in ("platform_0001_full_platform", "platform_0002_desktop_experience"):
            migration = importlib.import_module("server.migrations.versions." + module)
            with patch.object(migration, "op", operations):
                migration.upgrade()
    engine.dispose()
    with psycopg.connect(**pg_cluster, dbname=name) as destination:
        deployment_id = uuid.uuid4().hex
        with destination.transaction():
            destination.execute(
                sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                    sql.Identifier(name), sql.Literal("medical-app:aliyun:v1:" + deployment_id)
                )
            )

        @contextmanager
        def local_source(_path):
            assert target_db.info.host == "127.0.0.1"
            yield target_db

        @contextmanager
        def local_target(_path):
            assert destination.info.host == "127.0.0.1"
            yield destination, "synthetic-cloud-b"

        monkeypatch.setattr(cli, "connect_windows_management", local_source)
        monkeypatch.setattr(cli, "connect", local_target)
        key = tmp_path / "independent.key"
        generate_key(key)
        args = SimpleNamespace(
            key_file=key,
            source_management_profile=tmp_path / "unused.dpapi",
            source_label="synthetic-cloud-a",
            source_assets=None,
            target_deployment_id="0" * 32,
            directory=tmp_path / "wrong-bound-export",
        )
        cli.export(args)
        prepare_args = SimpleNamespace(
            key_file=key,
            dpapi=False,
            choices=None,
            source_export=args.directory,
            source_sqlite=None,
            source_assets=None,
            target_profile=tmp_path / "unused.json",
            target_assets=None,
            mode="empty-only",
            directory=tmp_path / "review",
        )
        with pytest.raises(transfer.TransferError, match="实际目标部署"):
            cli.prepare(prepare_args)
        assert not prepare_args.directory.exists()
        assert not transfer.postgres_snapshot(destination, label="synthetic-cloud-b")["rows"][
            "users"
        ]
        args.target_deployment_id = deployment_id
        args.directory = tmp_path / "correct-bound-export"
        exported = cli.export(args)
        assert exported["credentials_exported"] is False
        prepare_args.source_export = args.directory
        reviewed = cli.prepare(prepare_args)
        prepare_args.confirm_plan = reviewed["plan_sha256"]
        assert cli.apply(prepare_args)["status"] == "committed"
        assert cli.status(prepare_args)["status"] == "all_planned_rows_present"
        mirrored = transfer.postgres_snapshot(destination, label="synthetic-cloud-b")
        assert mirrored["rows"] == original["rows"]
        assert transfer.postgres_snapshot(target_db, label="synthetic-cloud-a") == original
        with pytest.raises(transfer.TransferError, match="源和目标相同"):
            transfer.plan_transfer(
                mirrored, {**mirrored, "identity": {**mirrored["identity"], "label": "wrong-label"}}
            )


def test_actual_pg_cli_preflight_backup_apply_and_status(target_db, tmp_path, monkeypatch):
    from server.platform_transfer_package import generate_key
    from tools.aliyun_platform import transfer as cli

    source_path = tmp_path / "source.db"
    synthetic_source(source_path)
    key = tmp_path / "synthetic-transfer.key"
    generate_key(key)

    @contextmanager
    def local_only_connect(_path):
        assert target_db.info.host == "127.0.0.1"
        yield target_db, "synthetic-cli-pg"

    monkeypatch.setattr(cli, "connect", local_only_connect)
    args = SimpleNamespace(
        key_file=key,
        dpapi=False,
        choices=None,
        source_sqlite=source_path,
        source_label="synthetic-cli-source",
        source_assets=None,
        target_assets=None,
        target_profile=tmp_path / "unused-profile.json",
        mode="empty-only",
        directory=tmp_path / "review",
    )
    report = cli.prepare(args)
    assert report["status"] == "review_required" and report["target_changed"] is False
    assert cli.status(args)["status"] == "not_applied_target_unchanged"
    assert (args.directory / "source.hltransfer").is_file()
    assert (args.directory / "target-before.hltransfer").is_file()
    args.confirm_plan = report["plan_sha256"]
    assert cli.apply(args)["status"] == "committed"
    assert cli.status(args)["status"] == "all_planned_rows_present"
    with pytest.raises(transfer.TransferError):
        cli.apply(args)


def test_guard_refuses_altered_foreign_keys_before_transfer(target_db):
    with target_db.transaction():
        target_db.execute(
            "ALTER TABLE medical_app_platform.user_preferences "
            "DROP CONSTRAINT user_preferences_user_id_fkey"
        )
    with pytest.raises(transfer.TransferError, match="关系定义"):
        transfer.postgres_snapshot(target_db, label="altered-synthetic")
