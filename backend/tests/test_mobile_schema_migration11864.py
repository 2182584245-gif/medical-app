"""Smoke test the SQL that operators apply to a new platform database."""

import io
import os
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg.conninfo import conninfo_to_dict

from server.platform_schema import PLATFORM_RUNTIME_ROLE, PLATFORM_SCHEMA


@pytest.fixture(scope="module")
def fresh_install_sql():
    buffer = io.StringIO()
    config = Config(str(Path(__file__).resolve().parents[1] / "server" / "alembic.ini"))
    config.output_buffer = buffer
    command.upgrade(config, "head", sql=True)
    return buffer.getvalue()


def test_mobile_migration_can_render_offline(fresh_install_sql):
    assert "platform_0007_mobile_11864" in fresh_install_sql
    assert "INSERT INTO medical_app_platform.expansion_foods(" in fresh_install_sql
    for table in (
        "member_memory_controls",
        "member_ai_preferences",
        "member_profile_vectors11841",
        "member_multimodal_vectors11842",
        "member_conversation_memory11844",
        "phone_auth_credentials11838",
    ):
        assert f"CREATE TABLE {PLATFORM_SCHEMA}.{table}" in fresh_install_sql.replace(
            "CREATE TABLE IF NOT EXISTS", "CREATE TABLE"
        )


def test_mobile_schema_on_empty_postgres(fresh_install_sql):
    dsn = os.environ.get("TEST_MOBILE_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set TEST_MOBILE_POSTGRES_DSN for the isolated PostgreSQL test")
    options = conninfo_to_dict(dsn)
    if (
        options.get("host") not in {"localhost", "127.0.0.1", "::1"}
        or not options.get("dbname", "").startswith("fawnsee_test_")
    ):
        pytest.fail("Use a local scratch database with a name starting with fawnsee_test_")
    with psycopg.connect(dsn, autocommit=True) as conn:
        already_initialized = conn.execute(
            "SELECT nspname FROM pg_namespace WHERE nspname IN "
            "('medical_app_platform', 'medical_app_private', 'medical_app_extensions')"
        ).fetchall()
        assert not already_initialized, "The integration test requires an empty disposable database"
        conn.execute(fresh_install_sql, prepare=False)
        assert conn.execute(
            "SELECT version_num FROM medical_app_private.alembic_version"
        ).fetchone() == ("platform_0007_mobile_11864",)
        assert conn.execute(
            "SELECT count(*) FROM medical_app_platform.expansion_foods"
        ).fetchone()[0] > 0

        with conn.transaction(force_rollback=True):
            stamp = "2026-01-01T00:00:00.000000Z"
            member_ids = [
                conn.execute(
                    "INSERT INTO medical_app_platform.users "
                    "(username, username_normalized, password_hash, created_at) "
                    "VALUES (%s, %s, 'synthetic-test-row', %s) RETURNING id",
                    (name, name, stamp),
                ).fetchone()[0]
                for name in ("migration_member_a", "migration_member_b")
            ]
            conn.execute(f"SET LOCAL ROLE {PLATFORM_RUNTIME_ROLE}")
            conn.execute("SELECT set_config('medical_app.role_code', 'member', true)")
            conn.execute("SELECT set_config('medical_app.user_id', %s, true)", (str(member_ids[0]),))
            insert_control = (
                "INSERT INTO medical_app_platform.member_memory_controls (user_id, domain, updated_at) "
                "VALUES (%s, 'profile', %s)"
            )
            conn.execute(insert_control, (member_ids[0], stamp))
            assert conn.execute(
                "SELECT user_id FROM medical_app_platform.member_memory_controls"
            ).fetchall() == [(member_ids[0],)]
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute(insert_control, (member_ids[1], stamp))
            conn.execute("SELECT set_config('medical_app.user_id', %s, true)", (str(member_ids[1]),))
            assert not conn.execute(
                "SELECT user_id FROM medical_app_platform.member_memory_controls"
            ).fetchall()
