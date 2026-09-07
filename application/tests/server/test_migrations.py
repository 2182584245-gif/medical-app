from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from server.config import Settings
from server.database import Database, initialize_development_database
from server.migrations.versions import pilot_0001_private_pilot as revision
from server.models import Base
from server.schema import PRIVATE_SCHEMA

PROJECT = Path(__file__).resolve().parents[2]


def offline_sql(operation="upgrade") -> str:
    output = io.StringIO()
    config = Config(str(PROJECT / "server" / "alembic.ini"), output_buffer=output)
    if operation == "upgrade":
        command.upgrade(config, "pilot_0001", sql=True)
    else:
        command.downgrade(config, "pilot_0001:base", sql=True)
    return output.getvalue()


def test_offline_generation_requires_no_secrets_and_never_opens_a_connection(monkeypatch):
    import os

    import sqlalchemy

    for key in tuple(os.environ):
        if key.startswith("SERVER_"):
            monkeypatch.delenv(key)

    def reject_connection(*_args, **_kwargs):
        raise AssertionError("Offline SQL attempted a connection")

    monkeypatch.setattr(sqlalchemy, "create_engine", reject_connection)
    sql = offline_sql()
    assert sql.startswith("BEGIN;") and sql.rstrip().endswith("COMMIT;")
    assert "CREATE SCHEMA medical_app_private;" in sql
    assert "IF NOT EXISTS" not in sql
    created = re.findall(r"CREATE TABLE ([a-z_]+\.[a-z_]+)", sql)
    assert set(created) == {f"{PRIVATE_SCHEMA}.{name}" for name in revision.TABLE_COLUMNS}
    assert len(created) == 5  # Four business tables plus Alembic metadata only.
    assert "CREATE TABLE public." not in sql
    assert "INSERT INTO medical_app_private.pilot_" not in sql


@pytest.mark.parametrize("operation", ["upgrade", "downgrade"])
def test_ownership_and_privilege_guards_are_in_both_sql_directions(operation):
    sql = offline_sql(operation)
    assert "Refusing" in sql and "unowned" in sql
    assert "'anon', 'authenticated', 'service_role'" in sql
    assert "REVOKE ALL ON SCHEMA medical_app_private FROM PUBLIC" in sql
    assert "REVOKE ALL ON ALL TABLES IN SCHEMA medical_app_private FROM PUBLIC" in sql
    assert "ALTER DEFAULT PRIVILEGES IN SCHEMA medical_app_private" in sql
    assert "FROM %I'" in sql and "%%I" not in sql
    assert "SET LOCAL search_path = pg_catalog" in sql
    assert "pg_advisory_xact_lock" in sql
    assert "DROP SCHEMA" not in sql
    assert "service_role" in sql and "GRANT " not in sql


def test_downgrade_guards_every_business_table_before_the_first_drop():
    sql = offline_sql("downgrade")
    first_drop = sql.index("DROP TABLE")
    lock = sql.index("IN ACCESS EXCLUSIVE MODE NOWAIT")
    assert sql.index("SET LOCAL row_security = off") < lock
    assert sql.index("Refusing unexpected row-level security") < first_drop
    for table in revision.TABLE_COLUMNS:
        if table != "alembic_version":
            guard = sql.index(f"Refusing downgrade: business table {table} is not empty")
            assert lock < guard < first_drop
    assert re.findall(r"DROP TABLE ([a-z_.]+) RESTRICT", sql) == [
        f"{PRIVATE_SCHEMA}.{name}"
        for name in ("pilot_messages", "pilot_login_sessions", "pilot_conversations", "pilot_users")
    ]
    assert " CASCADE" not in sql
    assert "DROP TABLE medical_app_private.alembic_version" not in sql
    assert "DELETE FROM medical_app_private.alembic_version" in sql


@pytest.mark.parametrize("operation", ["upgrade", "downgrade"])
def test_unknown_objects_fail_closed_in_emitted_sql(operation):
    sql = offline_sql(operation)
    for text in (
        "Refusing missing or extra relations",
        "Refusing missing or extra pilot constraints",
        "Refusing extra schema objects",
        "Refusing changed or extra columns",
        "Refusing added triggers, policies, rules, defaults or inheritance",
    ):
        assert text in sql
    for catalog in (
        "pg_proc",
        "pg_type",
        "pg_policy",
        "pg_trigger",
        "pg_collation",
        "pg_operator",
        "pg_ts_config",
        "pg_attrdef",
        "pg_inherits",
    ):
        assert f"pg_catalog.{catalog}" in sql
    # PostgreSQL 18 records NOT NULL in pg_constraint; older versions do not.
    assert "c.contype <> 'n'" in sql
    assert "c.relrowsecurity OR c.relforcerowsecurity" in sql
    assert not re.search(r"\nEND\s*\n\$medical_", sql)


def test_frozen_migration_ddl_matches_current_orm_without_a_database(monkeypatch):
    output = io.StringIO()
    migration = MigrationContext.configure(
        dialect=postgresql.dialect(paramstyle="named"),
        opts={"as_sql": True, "output_buffer": output},
    )
    operations = Operations(migration)
    monkeypatch.setattr(revision, "op", operations)
    revision.upgrade()
    sql = output.getvalue()
    for table in Base.metadata.sorted_tables:
        expected = str(CreateTable(table).compile(dialect=postgresql.dialect()))

        # Each full declaration must match. SQLAlchemy stores constraints in a
        # set, so their printed order can differ without changing the schema.
        def normalise(value):
            return sorted(
                re.sub(r"\s+", " ", line).strip().rstrip(",;")
                for line in value.splitlines()
                if line.strip()
            )

        start = sql.index(f"CREATE TABLE {PRIVATE_SCHEMA}.{table.name}")
        end = sql.index(";", start)
        assert normalise(sql[start:end]) == normalise(expected)
        assert tuple(table.columns.keys()) == revision.TABLE_COLUMNS[table.name]
    expected_indexes = {
        constraint.name
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
        if constraint.__class__.__name__ in {"PrimaryKeyConstraint", "UniqueConstraint"}
    } | {index.name for table in Base.metadata.tables.values() for index in table.indexes}
    assert expected_indexes | {"alembic_version_pkc"} == set(revision.INDEXES)
    assert {
        constraint.name
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
    } | {"alembic_version_pkc"} == set(revision.CONSTRAINTS)


def test_private_schema_is_translated_only_for_sqlite_tests(settings):
    database = Database(settings)
    try:
        assert Base.metadata.schema == PRIVATE_SCHEMA
        Base.metadata.create_all(database.engine)
        assert set(inspect(database.engine).get_table_names()) == {
            table.name for table in Base.metadata.tables.values()
        }
        database.check_ready()
        with database.engine.connect() as connection:
            attached = connection.exec_driver_sql("PRAGMA database_list").all()
        assert {row[1] for row in attached} <= {"main", "temp"}
    finally:
        database.engine.dispose()


def test_development_postgresql_cannot_bypass_migrations(settings, monkeypatch):
    values = settings.model_dump()
    values.update(env="development", database_url="postgresql+psycopg://synthetic@db.invalid/test")
    import server.database

    def reject_create_engine(*_args, **_kwargs):
        raise AssertionError("Rejected init-db must not connect")

    monkeypatch.setattr(server.database, "create_engine", reject_create_engine)
    with pytest.raises(RuntimeError, match="requires reviewed Alembic migrations"):
        initialize_development_database(Settings(**values))


@pytest.mark.parametrize(
    "confirm,url",
    [
        (None, None),
        ("wrong-project", "postgresql+psycopg://user:password@db.invalid/db"),
        (PRIVATE_SCHEMA, None),
        (PRIVATE_SCHEMA, "not-a-database-url-sensitive-value"),
        (PRIVATE_SCHEMA, "sqlite+pysqlite:///:memory:"),
        (PRIVATE_SCHEMA, "postgresql+psycopg://user:secret-password@db.invalid/db?sslmode=require"),
        (
            PRIVATE_SCHEMA,
            "postgresql+psycopg://user:secret-password@db.invalid/db?sslmode=verify-full",
        ),
    ],
)
def test_online_requires_separate_confirmation_and_secure_url_before_connection(
    confirm, url, monkeypatch
):
    import sqlalchemy

    monkeypatch.delenv("SERVER_MIGRATION_CONFIRM", raising=False)
    monkeypatch.delenv("SERVER_MIGRATION_DATABASE_URL", raising=False)
    # This runtime URL must never be accepted as a migration fallback.
    monkeypatch.setenv("SERVER_DATABASE_URL", "postgresql+psycopg://runtime:secret@db.invalid/db")
    if confirm:
        monkeypatch.setenv("SERVER_MIGRATION_CONFIRM", confirm)
    if url:
        monkeypatch.setenv("SERVER_MIGRATION_DATABASE_URL", url)

    def reject_connection(*_args, **_kwargs):
        raise AssertionError("Unsafe configuration attempted a connection")

    monkeypatch.setattr(sqlalchemy, "create_engine", reject_connection)
    config = Config(str(PROJECT / "server" / "alembic.ini"))
    with pytest.raises(RuntimeError) as error:
        command.upgrade(config, "pilot_0001")
    assert "secret-password" not in str(error.value)
    assert "not-a-database-url-sensitive-value" not in str(error.value)


@pytest.mark.parametrize("where", ["create", "connect", "transaction"])
def test_online_database_errors_hide_sensitive_details(where, monkeypatch):
    import certifi
    import sqlalchemy
    from sqlalchemy.engine import URL
    from sqlalchemy.exc import OperationalError

    from server.cloud_connection import project_reference, project_url

    url = URL.create(
        "postgresql+psycopg",
        username="migration_owner",
        password="secret-not-for-logs",
        host=f"db.{project_reference(project_url())}.supabase.co",
        port=5432,
        database="postgres",
        query={"sslmode": "verify-full", "sslrootcert": certifi.where()},
    )
    monkeypatch.setenv("SERVER_MIGRATION_CONFIRM", PRIVATE_SCHEMA)
    monkeypatch.setenv("SERVER_MIGRATION_DATABASE_URL", url.render_as_string(hide_password=False))
    failure = OperationalError(
        "secret SQL",
        {"password": "secret-not-for-logs"},
        Exception("secret URL/raw database message"),
    )

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def begin(self):
            raise failure

    class FakeEngine:
        def connect(self):
            if where == "connect":
                raise failure
            return FakeConnection()

        def dispose(self):
            pass

    def fake_engine(*_args, **_kwargs):
        if where == "create":
            raise failure
        return FakeEngine()

    monkeypatch.setattr(sqlalchemy, "create_engine", fake_engine)
    config = Config(str(PROJECT / "server" / "alembic.ini"))
    with pytest.raises(RuntimeError, match="Connection details.*hidden") as error:
        command.upgrade(config, "pilot_0001")
    assert "secret" not in str(error.value)
    assert error.value.__suppress_context__


def test_sql_export_is_review_only_and_never_overwrites(tmp_path):
    from server.migrations.export_sql import export_sql

    destination = tmp_path / "new-output"
    upgrade, downgrade = export_sql(destination)
    before = {path.name: path.read_bytes() for path in (upgrade, downgrade)}
    assert "Generated offline: not executed" in upgrade.read_text(encoding="utf-8")
    assert "DESTRUCTIVE WHEN EXECUTED" in downgrade.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        export_sql(destination)
    assert before == {path.name: path.read_bytes() for path in (upgrade, downgrade)}
