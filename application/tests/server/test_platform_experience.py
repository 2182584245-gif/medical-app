"""Synthetic regression coverage for v2 RPC, term expiry and migration contracts."""

from __future__ import annotations

import importlib
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from platform_test_support import PASSWORD, PEPPER, SyntheticDatabase

from ollama_chat_app.data.database import timestamp_to_db, utc_now
from ollama_chat_app.services.cloud_rpc_codec import decode_rpc, encode_rpc
from ollama_chat_app.services.preferences import PreferencesService
from server.platform_app import create_app
from server.platform_auth import PlatformAuth, PlatformAuthError
from server.platform_config import PlatformSettings
from server.platform_experience_schema import (
    EXPIRY_TABLES,
    NEW_COLUMNS,
    PLATFORM_COLUMNS,
    incremental_policy_ddl,
    policy_rules,
)
from server.platform_rpc import RpcDispatcher, RpcError, is_mutation
from server.platform_schema import PLATFORM_COLUMNS as V1_COLUMNS
from server.platform_security import PlatformAuthSettings


def _payload(*args, request_id=None, **kwargs):
    return {
        "args": encode_rpc(list(args)),
        "kwargs": encode_rpc(kwargs),
        "request_id": request_id or str(uuid4()),
    }


@pytest.fixture
def synthetic():
    database = SyntheticDatabase()
    member = database.account("experience-member")
    advisor = database.account("experience-advisor", "advisor")
    now = utc_now()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO staff_account_terms VALUES (?,?,?,?,?)",
            (
                advisor,
                timestamp_to_db(now - timedelta(days=1)),
                timestamp_to_db(now + timedelta(days=1)),
                timestamp_to_db(now),
                timestamp_to_db(now),
            ),
        )
    yield database, member, advisor
    database.close()


def _expire(database, advisor):
    with database.transaction() as connection:
        connection.execute(
            "UPDATE staff_account_terms SET ends_at=? WHERE user_id=?",
            (timestamp_to_db(utc_now() - timedelta(seconds=1)), advisor),
        )


def test_preferences_aliases_are_actor_bound_reads_and_idempotent_mutations(synthetic):
    database, member, advisor = synthetic
    dispatcher = RpcDispatcher(database, {"preferences": PreferencesService(database)})
    assert not is_mutation("get_preferences")
    assert is_mutation("update_preferences")
    request = _payload(member, {"preferred_name": "合成称呼"})
    first = dispatcher.invoke(member, "preferences", "update_preferences", request)
    assert first == dispatcher.invoke(member, "preferences", "update_preferences", request)
    assert decode_rpc(first["result"])["preferred_name"] == "合成称呼"
    with pytest.raises(RpcError) as error:
        dispatcher.invoke(advisor, "preferences", "get_preferences", _payload(member))
    assert error.value.status == 403


def test_expired_advisor_cannot_replay_cached_mutation_or_read(synthetic):
    database, _member, advisor = synthetic
    dispatcher = RpcDispatcher(database, {"preferences": PreferencesService(database)})
    request = _payload(advisor, {"nickname": "合成顾问"})
    dispatcher.invoke(advisor, "preferences", "update_preferences", request)
    _expire(database, advisor)
    for method, value in (
        ("update_preferences", request),
        ("get_preferences", _payload(advisor)),
    ):
        with pytest.raises(RpcError) as caught:
            dispatcher.invoke(advisor, "preferences", method, value)
        assert caught.value.code == "identity_unavailable"


def test_existing_token_and_new_login_both_stop_at_staff_expiry(synthetic):
    database, _member, advisor = synthetic
    auth = PlatformAuth(
        database,
        PlatformAuthSettings(
            token_pepper=PEPPER,
            production=False,
            auth_ip_limit=100,
            auth_username_limit=100,
        ),
    )
    response = auth.login("experience-advisor", PASSWORD, client_ip="127.0.0.1")
    assert auth.resolve_token(response["access_token"]).user.id == advisor
    _expire(database, advisor)
    with pytest.raises(PlatformAuthError) as token_error:
        auth.resolve_token(response["access_token"])
    assert token_error.value.status_code == 401
    with pytest.raises(PlatformAuthError) as login_error:
        auth.login("experience-advisor", PASSWORD, client_ip="127.0.0.1")
    assert login_error.value.status_code == 401


def test_new_migration_is_additive_and_forces_all_new_table_policies(monkeypatch):
    module = importlib.import_module("server.migrations.versions.platform_0002_desktop_experience")
    statements = []
    monkeypatch.setattr(module.op, "execute", lambda sql: statements.append(str(sql)))
    module.upgrade()
    assert module.down_revision == "platform_0001"
    assert len(V1_COLUMNS) == 27 and len(PLATFORM_COLUMNS) == 32
    sql = "\n".join(statements)
    assert "CREATE SCHEMA" not in sql
    assert "UPDATE medical_app_platform.users" not in sql
    assert "DELETE FROM" not in sql
    for table in NEW_COLUMNS:
        assert f"ALTER TABLE medical_app_platform.{table} FORCE ROW LEVEL SECURITY" in sql
    assert "platform_staff_term" in sql and "AS RESTRICTIVE" in sql
    assert "staff_account_terms" not in EXPIRY_TABLES
    assert set(policy_rules()) == set(PLATFORM_COLUMNS)
    assert not any("SECURITY DEFINER" in statement for statement in incremental_policy_ddl())


def test_sync_wire_envelope_included_in_eight_mib_limit(synthetic, monkeypatch):
    database, member, _advisor = synthetic
    app = create_app(
        PlatformSettings(
            env="test",
            database_url="sqlite+pysqlite:///:memory:",
            token_pepper=PEPPER,
            allowed_hosts=["testserver"],
            require_https=True,
        ),
        database=database,
    )
    login = app.state.platform_auth.login("experience-member", PASSWORD, client_ip="127.0.0.1")
    monkeypatch.setattr(
        RpcDispatcher, "invoke", lambda *_a, **_kw: {"result": "x" * (8 * 1024 * 1024)}
    )
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/v1/rpc/sync/get_snapshot",
            json=_payload(member),
            headers={"Authorization": "Bearer " + login["access_token"]},
        )
        assert response.status_code == 413
        assert response.json() == {"detail": "result_too_large"}
