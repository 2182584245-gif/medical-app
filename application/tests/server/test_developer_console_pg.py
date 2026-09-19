"""Opt-in real PostgreSQL RLS tests on an owned disposable local container only."""

import importlib
import sqlite3
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from platform_test_support import PASSWORD, PEPPER
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from ollama_chat_app.data.database import timestamp_to_db, utc_now
from ollama_chat_app.security.passwords import hash_password
from server.platform_app import create_app
from server.platform_config import PlatformSettings
from server.platform_database import PlatformDatabase
from server.platform_developer import ApplicationSettings
from server.platform_experience_schema import runtime_grant_ddl
from tests.server import test_platform_transfer_pg as fixture_module
from tests.server.test_developer_console import login

pg_cluster = fixture_module.pg_cluster
target_db = fixture_module.target_db


@pytest.fixture
def pg_console(target_db, pg_cluster):
    with target_db.transaction():
        if not target_db.execute(
            "SELECT 1 FROM pg_roles WHERE rolname='medical_app_platform_runtime'"
        ).fetchone():
            target_db.execute(
                "CREATE ROLE medical_app_platform_runtime LOGIN NOINHERIT NOBYPASSRLS "
                "PASSWORD 'synthetic-runtime-v4-only'"
            )
        for name in ("platform_0003_medical_records", "platform_0004_developer_console"):
            migration = importlib.import_module("server.migrations.versions." + name)
            with patch.object(migration, "op", SimpleNamespace(execute=target_db.execute)):
                migration.upgrade()
        for statement in runtime_grant_ddl():
            target_db.execute(statement)
        password_hash = hash_password(PASSWORD)
        now = timestamp_to_db(utc_now())
        for user_id, name, role in (
            (1, "console-member", "member"),
            (2, "console-other", "member"),
            (3, "console-developer", "operator"),
            (4, "console-advisor", "advisor"),
        ):
            target_db.execute(
                "INSERT INTO medical_app_platform.users "
                "(id,username,username_normalized,password_hash,role_code,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (user_id, name, name, password_hash, role, now, now),
            )
            target_db.execute(
                "INSERT INTO medical_app_platform.conversations "
                "(user_id,title,created_at,updated_at) VALUES (%s,'原始对话',%s,%s)",
                (user_id, now, now),
            )
        target_db.execute(
            "INSERT INTO medical_app_platform.developer_grants VALUES(3,1,%s)", (now,)
        )
        target_db.execute(
            "INSERT INTO medical_app_platform.user_preferences VALUES(1,'{}',%s)", (now,)
        )
        target_db.execute(
            "INSERT INTO medical_app_platform.advisor_profiles"
            "(user_id,display_name,created_at,updated_at) VALUES(4,'顾问原名',%s,%s)",
            (now, now),
        )
    url = URL.create(
        "postgresql+psycopg",
        username="medical_app_platform_runtime",
        password="synthetic-runtime-v4-only",
        host=pg_cluster["host"],
        port=pg_cluster["port"],
        database=target_db.info.dbname,
    )
    database = PlatformDatabase(engine=create_engine(url, hide_parameters=True))
    settings = PlatformSettings(
        env="test",
        database_url=url.render_as_string(hide_password=False),
        token_pepper=PEPPER,
        allowed_hosts=["testserver"],
        require_https=True,
    )
    application = create_app(settings, database=database)
    with TestClient(
        application, base_url="https://testserver", raise_server_exceptions=False
    ) as client:
        yield target_db, database, application, client
    database.close()


def test_real_rls_no_runtime_grant_write_and_no_regular_operator_private_preferences(pg_console):
    admin, database, _, client = pg_console
    with admin.transaction():
        for table in (
            "developer_grants",
            "developer_sessions",
            "developer_settings_snapshots",
            "developer_pending_changes",
            "developer_client_sessions",
        ):
            row = admin.execute(
                "SELECT relrowsecurity,relforcerowsecurity FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='medical_app_platform' AND c.relname=%s",
                (table,),
            ).fetchone()
            assert row == (True, True)
    with database.identity(user_id=3, role_code="operator"):
        with database.connect() as connection:
            assert (
                connection.execute("SELECT * FROM user_preferences WHERE user_id=1").fetchall()
                == []
            )
        with pytest.raises(sqlite3.OperationalError), database.transaction() as connection:
            connection.execute("UPDATE developer_grants SET enabled=1 WHERE user_id=3")
    headers = login(client)
    detail = client.get("/v1/developer/users/1", headers=headers)
    assert detail.status_code == 200, detail.text
    assert len(detail.json()["tables"]["user_preferences"]) == 1
    assert "password_hash" not in detail.text


def test_real_rls_settings_and_only_target_start_apply_pending_without_owner_connection(pg_console):
    _, database, _, client = pg_console
    headers = login(client)
    result = client.put(
        "/v1/developer/settings",
        headers=headers,
        json={"expected_revision": 0, "settings": ApplicationSettings().model_dump()},
    )
    assert result.status_code == 200, result.text
    changed = client.patch(
        "/v1/developer/users/4/records/advisor_profiles/4",
        headers=headers,
        json={"changes": {"display_name": "下次启动显示的新名称"}},
    )
    assert changed.status_code == 200, changed.text
    with database.identity(user_id=3, role_code="operator"), database.connect() as connection:
        assert (
            connection.execute(
                "SELECT display_name FROM advisor_profiles WHERE user_id=4"
            ).fetchone()[0]
            == "顾问原名"
        )
    other = login(client, "console-member", developer=False)
    assert client.post("/v1/client-start", headers=other).json()["activation"]["applied"] == 0
    advisor = login(client, "console-advisor", developer=False)
    result = client.post("/v1/client-start", headers=advisor)
    assert result.status_code == 200, result.text
    assert result.json()["activation"] == {"applied": 1, "conflicts": 0}
    with database.identity(user_id=3, role_code="operator"), database.connect() as connection:
        assert (
            connection.execute(
                "SELECT display_name FROM advisor_profiles WHERE user_id=4"
            ).fetchone()[0]
            == "下次启动显示的新名称"
        )


def test_real_rls_revoked_grant_and_session_capture_cannot_select_old_revision(pg_console):
    admin, _, app, client = pg_console
    developer = login(client)
    data = ApplicationSettings().model_dump()
    assert (
        client.put(
            "/v1/developer/settings",
            headers=developer,
            json={"expected_revision": 0, "settings": data},
        ).status_code
        == 200
    )
    member = login(client, "console-member", developer=False)
    assert client.post("/v1/client-start", headers=member).json()["revision"] == 1
    principal = app.state.platform_auth.resolve_token(member["Authorization"][7:])
    assert app.state.developer_service.capture_for_ai(principal)["revision"] == 1
    with pytest.raises(Exception, match="版本不一致"):
        app.state.developer_service.capture_for_ai(principal, 0)
    data["features"]["ai"] = False
    assert (
        client.put(
            "/v1/developer/settings",
            headers=developer,
            json={"expected_revision": 1, "settings": data},
        ).status_code
        == 200
    )
    # Already-open session retains the captured version; new launch updates it.
    assert app.state.developer_service.capture_for_ai(principal)["revision"] == 1
    assert client.post("/v1/client-start", headers=member).json()["revision"] == 2
    assert app.state.developer_service.capture_for_ai(principal)["revision"] == 2
    with admin.transaction():
        admin.execute("UPDATE medical_app_platform.developer_grants SET enabled=0 WHERE user_id=3")
    assert client.get("/v1/developer/settings", headers=developer).status_code == 403


def test_real_expired_advisor_can_sign_in_after_immediate_validity_extension(pg_console):
    admin, database, _, client = pg_console
    now = utc_now()
    text_now = timestamp_to_db(now)
    with admin.transaction():
        admin.execute(
            "INSERT INTO medical_app_platform.staff_account_terms VALUES(4,%s,%s,%s,%s)",
            (
                timestamp_to_db(now - timedelta(days=60)),
                timestamp_to_db(now + timedelta(days=1)),
                text_now,
                text_now,
            ),
        )
    previous = login(client, "console-advisor", developer=False)
    with admin.transaction():
        admin.execute(
            "UPDATE medical_app_platform.staff_account_terms SET ends_at=%s WHERE user_id=4",
            (timestamp_to_db(now - timedelta(days=1)),),
        )
    assert (
        client.post(
            "/v1/auth/login", json={"username": "console-advisor", "password": PASSWORD}
        ).status_code
        == 401
    )
    developer = login(client)
    changed = client.patch(
        "/v1/developer/users/4/records/staff_account_terms/4",
        headers=developer,
        json={"changes": {"ends_at": timestamp_to_db(now + timedelta(days=90))}},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["security_effect"] == "immediate"
    assert client.get("/v1/client-settings", headers=previous).status_code == 401
    assert login(client, "console-advisor", developer=False)
    with database.identity(user_id=3, role_code="operator"), database.connect() as connection:
        assert (
            connection.execute("SELECT count(*) FROM developer_pending_changes").fetchone()[0] == 0
        )
