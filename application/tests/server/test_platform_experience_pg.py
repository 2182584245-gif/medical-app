"""Opt-in PostgreSQL 17 verification; requires an empty disposable local database.

Never accepts a remote server, default database, or pre-existing platform schema.
The calling harness owns and removes the disposable container after these tests.
"""

from __future__ import annotations

import importlib
import os
import sqlite3
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from ollama_chat_app.data.database import timestamp_to_db, utc_now
from ollama_chat_app.services.appointment_service import AppointmentService
from ollama_chat_app.services.cloud_rpc_codec import encode_rpc
from ollama_chat_app.services.commerce import CommerceService, CommerceValidationError
from ollama_chat_app.services.preferences import PreferencesService
from ollama_chat_app.services.service_management import ServiceManagementService
from server.platform_database import PlatformDatabase
from server.platform_experience_schema import PLATFORM_COLUMNS, runtime_grant_ddl
from server.platform_rpc import RpcDispatcher


@pytest.fixture(scope="module")
def pg_runtime():
    raw = os.environ.get("MEDICAL_APP_V2_TEST_DATABASE_URL")
    if not raw:
        pytest.skip("requires an explicitly selected disposable local PostgreSQL container")
    url = make_url(raw)
    if url.host != "127.0.0.1" or url.database != "medical_app_v2_test":
        pytest.fail("refusing a non-disposable or nonlocal database")
    admin = create_engine(url, hide_parameters=True)
    now = timestamp_to_db(utc_now())
    with admin.begin() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT pg_catalog.to_regnamespace('medical_app_platform')"
            ).scalar()
            is None
        ), "refusing to adopt an existing schema"
        for role in ("anon", "authenticated", "service_role"):
            connection.exec_driver_sql(f"CREATE ROLE {role} NOLOGIN")
        operations = Operations(MigrationContext.configure(connection))
        v1 = importlib.import_module("server.migrations.versions.platform_0001_full_platform")
        v2 = importlib.import_module("server.migrations.versions.platform_0002_desktop_experience")
        with patch.object(v1, "op", operations):
            v1.upgrade()
        # Existing synthetic rows precede v2 and must survive without rewriting.
        for identifier, role in ((1, "member"), (2, "member"), (3, "advisor"), (4, "operator")):
            connection.exec_driver_sql(
                "INSERT INTO medical_app_platform.users "
                "(id,username,username_normalized,password_hash,role_code,created_at,updated_at) "
                "VALUES(%s,%s,%s,'synthetic-unused-hash',%s,%s,%s)",
                (identifier, f"synthetic-{identifier}", f"synthetic-{identifier}", role, now, now),
            )
        with patch.object(v2, "op", operations):
            v2.upgrade()
        assert (
            connection.exec_driver_sql("SELECT COUNT(*) FROM medical_app_platform.users").scalar()
            == 4
        )
        connection.exec_driver_sql(
            "CREATE ROLE medical_app_platform_runtime LOGIN NOINHERIT "
            "PASSWORD 'synthetic-runtime-only'"
        )
        for statement in runtime_grant_ddl():
            connection.exec_driver_sql(statement)
        connection.exec_driver_sql(
            "INSERT INTO medical_app_platform.advisor_bindings "
            "(member_user_id,advisor_user_id,status,created_at,updated_at) "
            "VALUES(1,3,'active',%s,%s)",
            (now, now),
        )
        connection.exec_driver_sql(
            "INSERT INTO medical_app_platform.staff_account_terms VALUES(3,%s,%s,%s,%s)",
            (
                timestamp_to_db(utc_now() - timedelta(days=1)),
                timestamp_to_db(utc_now() + timedelta(days=1)),
                now,
                now,
            ),
        )
    runtime_engine = create_engine(
        url.set(username="medical_app_platform_runtime", password="synthetic-runtime-only"),
        hide_parameters=True,
    )
    database = PlatformDatabase(engine=runtime_engine)
    database.initialize()
    yield admin, database
    database.close()
    admin.dispose()


def test_postgres_v2_metadata_and_data_api_roles_have_no_access(pg_runtime):
    admin, _database = pg_runtime
    with admin.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='medical_app_platform' AND relkind='r'"
        ).fetchall()
        assert {row[0] for row in rows} == set(PLATFORM_COLUMNS)
        assert all(row[1] and row[2] for row in rows)
        for role in ("anon", "authenticated", "service_role"):
            assert not connection.exec_driver_sql(
                "SELECT has_schema_privilege(%s,'medical_app_platform','USAGE')", (role,)
            ).scalar()


def test_postgres_preferences_isolate_accounts_and_reject_ownership_transfer(pg_runtime):
    _admin, database = pg_runtime
    preferences = PreferencesService(database)
    with database.identity(user_id=1, role_code="member"):
        preferences.update(1, {"preferred_name": "合成甲"})
        with pytest.raises(sqlite3.OperationalError), database.transaction() as connection:
            connection.execute("UPDATE user_preferences SET user_id=2 WHERE user_id=1")
    with database.identity(user_id=2, role_code="member"):
        with database.connect() as connection:
            assert (
                connection.execute("SELECT * FROM user_preferences WHERE user_id=1").fetchall()
                == []
            )
        assert preferences.get(2)["preferred_name"] == ""


def test_postgres_member_can_request_only_own_appointment(pg_runtime):
    _admin, database = pg_runtime
    service = AppointmentService(database)
    with database.identity(user_id=1, role_code="member"):
        identifier = service.create_request(1, "合成上门服务", notes="合成预约")
        assert any(row["id"] == identifier for row in service.list_for_member(1))
    with database.identity(user_id=2, role_code="member"):
        assert service.list_for_member(2) == []


def test_postgres_cart_and_favorites_isolate_and_retain_inactive_product_visibility(pg_runtime):
    _admin, database = pg_runtime
    commerce = CommerceService(database)
    with database.identity(user_id=4, role_code="operator"):
        product = commerce.create_product(
            4,
            sku="synthetic-v2",
            name="合成商品",
            category="daily",
            price_cents=100,
            is_active=True,
        )["id"]
    with database.identity(user_id=1, role_code="member"):
        commerce.add_to_cart(1, product, 2)
        commerce.set_favorite(1, product, True)
        commerce.set_favorite(1, product, True)
        assert len(commerce.list_cart(1)) == len(commerce.list_favorites(1)) == 1
    with database.identity(user_id=2, role_code="member"):
        assert commerce.list_cart(2) == commerce.list_favorites(2) == []
    with database.identity(user_id=4, role_code="operator"):
        commerce.set_product_active(4, product, False)
    with database.identity(user_id=1, role_code="member"):
        assert len(commerce.list_cart(1)) == 1
        with pytest.raises(CommerceValidationError):
            commerce.checkout_cart(1)
    with database.identity(user_id=4, role_code="operator"):
        commerce.set_product_active(4, product, True)
    with database.identity(user_id=1, role_code="member"):
        assert len(commerce.checkout_cart(1)) == 1
        assert commerce.list_cart(1) == []


def test_postgres_operator_password_reset_revokes_target_sessions(pg_runtime):
    admin, database = pg_runtime
    now = timestamp_to_db(utc_now())
    with admin.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO medical_app_platform.platform_sessions "
            "(token_digest,user_id,created_at,expires_at) VALUES(%s,1,%s,%s)",
            ("a" * 64, now, timestamp_to_db(utc_now() + timedelta(days=1))),
        )
    dispatcher = RpcDispatcher(database, {"service_management": ServiceManagementService(database)})
    payload = {
        "args": encode_rpc([4, 1, "synthetic-reset-password-only"]),
        "kwargs": encode_rpc({}),
        "request_id": str(uuid4()),
    }
    with database.identity(user_id=4, role_code="operator"):
        dispatcher.invoke(4, "service_management", "reset_account_password", payload)
    with admin.connect() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT revoked_at FROM medical_app_platform.platform_sessions "
                "WHERE token_digest=%s",
                ("a" * 64,),
            ).scalar()
            is not None
        )


def test_postgres_expired_advisor_rls_denies_business_rows_even_with_stale_identity(pg_runtime):
    admin, database = pg_runtime
    with database.identity(user_id=3, role_code="advisor"), database.connect() as connection:
        assert connection.execute("SELECT * FROM visit_tasks").fetchall()
    with admin.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE medical_app_platform.staff_account_terms SET ends_at=%s WHERE user_id=3",
            (timestamp_to_db(utc_now() - timedelta(seconds=1)),),
        )
    with database.identity(user_id=3, role_code="advisor"), database.connect() as connection:
        assert connection.execute("SELECT * FROM visit_tasks").fetchall() == []
        assert connection.execute("SELECT * FROM member_profiles").fetchall() == []
