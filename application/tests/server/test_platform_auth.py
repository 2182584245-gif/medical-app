from __future__ import annotations

import json
import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ollama_chat_app.data.database import Database, timestamp_to_db, utc_now
from server.platform_auth import (
    BUSINESS_TABLES,
    PlatformAuth,
    PlatformAuthError,
    bootstrap_operator,
    create_auth_router,
)
from server.platform_security import (
    PLATFORM_RUNTIME_ROLE,
    PlatformAuthSettings,
    PlatformAuthUnavailable,
    SharedAuthLimiter,
)
from server.security import AuthCapacityError

PASSWORD = "synthetic-password-only-123"


@pytest.fixture
def platform_database(tmp_path):
    database = Database(tmp_path / "synthetic-platform.sqlite")
    database.initialize()
    with database.transaction() as connection:
        connection.execute(
            "CREATE TABLE platform_sessions "
            "(token_digest TEXT PRIMARY KEY CHECK(length(token_digest)=64), "
            "user_id INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, "
            "expires_at TEXT NOT NULL, revoked_at TEXT)"
        )
        connection.execute(
            "CREATE TABLE auth_rate_buckets "
            "(bucket_id INTEGER PRIMARY KEY CHECK(bucket_id BETWEEN 0 AND 16384), "
            "window_start INTEGER NOT NULL, "
            "attempts INTEGER NOT NULL CHECK(attempts BETWEEN 0 AND 1000000))"
        )
        connection.execute("CREATE INDEX synthetic_bucket_age ON auth_rate_buckets(window_start)")
    return database


@pytest.fixture
def platform_settings():
    return PlatformAuthSettings(
        token_pepper=secrets.token_hex(32),
        production=False,
        auth_ip_limit=100,
        auth_username_limit=100,
        auth_global_limit=1000,
    )


@pytest.fixture
def platform_auth(platform_database, platform_settings):
    return PlatformAuth(platform_database, platform_settings)


@pytest.fixture
def platform_client(platform_auth):
    app = FastAPI()
    app.include_router(create_auth_router(platform_auth))
    with TestClient(app) as client:
        yield client


def account(client, name="synthetic-member"):
    registration = client.post("/v1/auth/register", json={"username": name, "password": PASSWORD})
    assert registration.status_code == 201, registration.text
    login = client.post("/v1/auth/login", json={"username": name, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return registration.json(), login.json()


def test_login_offline_lease_bound_to_current_session(platform_client):
    from ollama_chat_app.services.offline_access import utc_timestamp, validate_lease

    user, response = account(platform_client, "offline-lease-member")
    lease = validate_lease(response["offline_lease"], actor_id=user["id"])
    assert response["sync_protocol"] == 2
    assert utc_timestamp(lease["expires_at"]) <= utc_timestamp(response["expires_at"])
    assert "access_token" not in lease and "password" not in lease


def test_offline_lease_cannot_outlive_advisor_term(platform_database):
    from ollama_chat_app.services.auth import AuthService
    from ollama_chat_app.services.offline_access import utc_timestamp
    from server.platform_leases import make_offline_lease

    now = utc_now()
    actor = AuthService(platform_database).register("lease-synthetic-advisor", PASSWORD)
    with platform_database.transaction() as connection:
        connection.execute("UPDATE users SET role_code='advisor' WHERE id=?", (actor.id,))
        connection.execute(
            "INSERT INTO staff_account_terms(user_id,starts_at,ends_at,updated_at,created_at) "
            "VALUES(?,?,?,?,?)",
            (actor.id, timestamp_to_db(now-timedelta(hours=1)),
             timestamp_to_db(now+timedelta(minutes=15)),
             timestamp_to_db(now), timestamp_to_db(now)),
        )
        value = make_offline_lease(connection, actor.id, "advisor", now,
                                   now+timedelta(hours=2), bytes(range(32)).hex())
    assert utc_timestamp(value["expires_at"]) == now+timedelta(minutes=15)


def headers(login):
    return {"Authorization": "Bearer " + login["access_token"]}


def test_full_user_view_opaque_session_and_default_conversation(platform_client, platform_database):
    user, login = account(platform_client)
    assert user["role_code"] == "member"
    assert set(user) == {
        "id",
        "username",
        "username_normalized",
        "created_at",
        "last_login_at",
        "role_code",
        "account_status",
        "updated_at",
    }
    assert len(login["access_token"]) == 43
    assert login["user"]["id"] == user["id"]
    assert PASSWORD not in json.dumps(login)
    response = platform_client.get("/v1/auth/me", headers=headers(login))
    assert response.status_code == 200
    assert response.json()["username"] == user["username"]
    with platform_database.connect() as connection:
        digest = connection.execute("SELECT token_digest FROM platform_sessions").fetchone()[0]
        assert digest != login["access_token"] and len(digest) == 64
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM conversations WHERE user_id=?", (user["id"],)
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("field", ["role_code", "actor_id", "id", "account_status"])
def test_public_registration_cannot_self_elevate(platform_client, platform_database, field):
    result = platform_client.post(
        "/v1/auth/register",
        json={"username": "attempt-elevation", "password": PASSWORD, field: "operator"},
    )
    assert result.status_code == 422
    assert PASSWORD not in result.text
    with platform_database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_validation_never_echoes_submitted_password(platform_client):
    candidate = "secret-overlong-" * 100
    result = platform_client.post(
        "/v1/auth/register", json={"username": "safe", "password": candidate}
    )
    assert result.status_code == 422
    assert "secret-overlong" not in result.text


def test_change_password_revokes_other_tokens_and_old_password(platform_client):
    _, first = account(platform_client)
    second = platform_client.post(
        "/v1/auth/login", json={"username": "synthetic-member", "password": PASSWORD}
    ).json()
    changed = platform_client.post(
        "/v1/auth/change-password",
        headers=headers(first),
        json={"current_password": PASSWORD, "new_password": "replacement-synthetic-456"},
    )
    assert changed.status_code == 204
    assert platform_client.get("/v1/auth/me", headers=headers(first)).status_code == 200
    assert platform_client.get("/v1/auth/me", headers=headers(second)).status_code == 401
    assert (
        platform_client.post(
            "/v1/auth/login", json={"username": "synthetic-member", "password": PASSWORD}
        ).status_code
        == 401
    )
    assert (
        platform_client.post(
            "/v1/auth/login",
            json={"username": "synthetic-member", "password": "replacement-synthetic-456"},
        ).status_code
        == 200
    )


def test_wrong_password_does_not_revoke_or_change(platform_client):
    _, login = account(platform_client)
    result = platform_client.post(
        "/v1/auth/change-password",
        headers=headers(login),
        json={"current_password": "wrong-value", "new_password": "not-to-be-saved-234"},
    )
    assert result.status_code == 401
    assert platform_client.get("/v1/auth/me", headers=headers(login)).status_code == 200
    assert (
        platform_client.post(
            "/v1/auth/login", json={"username": "synthetic-member", "password": PASSWORD}
        ).status_code
        == 200
    )


def test_logout_expiry_and_disabled_user_fail_closed(platform_client, platform_database):
    user, login = account(platform_client)
    assert platform_client.post("/v1/auth/logout", headers=headers(login)).status_code == 204
    assert platform_client.get("/v1/auth/me", headers=headers(login)).status_code == 401
    fresh = platform_client.post(
        "/v1/auth/login", json={"username": "synthetic-member", "password": PASSWORD}
    ).json()
    with platform_database.transaction() as connection:
        connection.execute(
            "UPDATE platform_sessions SET expires_at=?",
            (timestamp_to_db(utc_now() - timedelta(minutes=1)),),
        )
    assert platform_client.get("/v1/auth/me", headers=headers(fresh)).status_code == 401
    newest = platform_client.post(
        "/v1/auth/login", json={"username": "synthetic-member", "password": PASSWORD}
    ).json()
    with platform_database.transaction() as connection:
        connection.execute("UPDATE users SET account_status='disabled' WHERE id=?", (user["id"],))
    assert platform_client.get("/v1/auth/me", headers=headers(newest)).status_code == 401


def test_bootstrap_requires_confirmation_and_never_overwrites(platform_database, platform_settings):
    with pytest.raises(PlatformAuthError) as error:
        bootstrap_operator(platform_database, platform_settings, "owner", PASSWORD, confirm="yes")
    assert error.value.status_code == 400
    user = bootstrap_operator(
        platform_database, platform_settings, "owner", PASSWORD, confirm="medical_app_platform"
    )
    assert user.role_code == "operator"
    with pytest.raises(PlatformAuthError) as error:
        bootstrap_operator(
            platform_database,
            platform_settings,
            "second-owner",
            PASSWORD,
            confirm="medical_app_platform",
        )
    assert error.value.status_code == 409
    with platform_database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0] == 1


def test_bootstrap_refuses_existing_member_name(
    platform_auth, platform_database, platform_settings
):
    user = platform_auth.register("same-name", PASSWORD, client_ip="127.0.0.1")
    with pytest.raises(PlatformAuthError) as error:
        bootstrap_operator(
            platform_database,
            platform_settings,
            "same-name",
            PASSWORD,
            confirm="medical_app_platform",
        )
    assert error.value.status_code == 409
    with platform_database.connect() as connection:
        assert (
            connection.execute("SELECT role_code FROM users WHERE id=?", (user.id,)).fetchone()[0]
            == "member"
        )


def test_no_public_bootstrap_route(platform_client):
    assert platform_client.post("/v1/auth/bootstrap", json={}).status_code == 404


def test_shared_limits_across_independent_instances(platform_database, platform_settings):
    settings = platform_settings.model_copy(update={"auth_username_limit": 2})
    first = SharedAuthLimiter(platform_database, settings, clock=lambda: 1000)
    second = SharedAuthLimiter(platform_database, settings, clock=lambda: 1000)
    first.check("ip-one", "same-name")
    second.check("ip-two", "same-name")
    with pytest.raises(AuthCapacityError):
        first.check("ip-three", "same-name")
    with platform_database.connect() as connection:
        rows = connection.execute("SELECT * FROM auth_rate_buckets").fetchall()
    assert "same-name" not in repr(rows) and "ip-one" not in repr(rows)
    assert max(row["attempts"] for row in rows) == 3  # rejected attempt committed


def test_ip_and_global_limits_and_expiry(platform_database, platform_settings):
    settings = platform_settings.model_copy(update={"auth_ip_limit": 1, "auth_global_limit": 3})
    limiter = SharedAuthLimiter(platform_database, settings, clock=lambda: 1000)
    limiter.check("ip-one", "name-one")
    with pytest.raises(AuthCapacityError):
        limiter.check("ip-one", "name-two")
    limiter.check("ip-two", "name-three")
    with pytest.raises(AuthCapacityError):
        limiter.check("ip-three", "name-four")
    later = SharedAuthLimiter(platform_database, settings, clock=lambda: 1200)
    later.check("new-ip", "new-name")
    with platform_database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM auth_rate_buckets").fetchone()[0] == 3


def test_concurrent_requests_share_atomic_global_budget(platform_database, platform_settings):
    settings = platform_settings.model_copy(update={"auth_global_limit": 5})

    def attempt(index):
        limiter = SharedAuthLimiter(platform_database, settings, clock=lambda: 1000)
        try:
            limiter.check(f"synthetic-ip-{index}", f"synthetic-name-{index}")
            return True
        except AuthCapacityError:
            return False

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(attempt, range(12)))
    assert sum(results) == 5


def test_bucket_space_is_keyed_and_strictly_bounded(platform_database, platform_settings):
    first = SharedAuthLimiter(platform_database, platform_settings)
    second = SharedAuthLimiter(
        platform_database,
        platform_settings.model_copy(
            update={
                "token_pepper": PlatformAuthSettings(
                    token_pepper=secrets.token_hex(32)
                ).token_pepper
            }
        ),
    )
    assert first.bucket_ids("client", "name") != second.bucket_ids("client", "name")
    for index in range(1000):
        global_id, ip_id, name_id = first.bucket_ids(str(index), str(index))
        assert global_id == 0 and 1 <= ip_id <= 8192 and 8193 <= name_id <= 16384
    with platform_database.transaction() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO auth_rate_buckets VALUES (16385,0,1)")


def test_database_failure_denies_auth_and_hides_error(platform_auth, monkeypatch):
    @contextmanager
    def failed():
        raise RuntimeError("password=private-do-not-echo")
        yield

    monkeypatch.setattr(platform_auth.database, "transaction", failed)
    with pytest.raises(PlatformAuthError) as error:
        platform_auth.login("someone", PASSWORD, client_ip="test")
    assert error.value.status_code == 503
    assert "private-do-not-echo" not in str(error.value)


def test_production_rejects_sqlite_without_boolean_bypass(platform_database):
    settings = PlatformAuthSettings(token_pepper=secrets.token_hex(32))
    with pytest.raises(PlatformAuthUnavailable):
        PlatformAuth(platform_database, settings)


def test_postgres_sql_uses_database_clock_and_transaction_lock(platform_settings):
    statements = []

    class Connection:
        def execute(self, statement, parameters=None):
            statements.append((statement, parameters))
            value = 1000 if "extract(epoch" in statement else 1
            return SimpleNamespace(fetchone=lambda: (value,))

    @contextmanager
    def transaction():
        yield Connection()

    database = SimpleNamespace(dialect="postgresql", transaction=transaction)
    SharedAuthLimiter(
        database, platform_settings, clock=lambda: pytest.fail("must use DB time")
    ).check("sensitive-ip", "sensitive-name")
    assert any("pg_advisory_xact_lock" in sql for sql, _ in statements)
    assert any("lock_timeout" in sql for sql, _ in statements)
    assert "sensitive-ip" not in repr(statements) and "sensitive-name" not in repr(statements)


def test_sqlite_readiness_is_never_marked_production_verified(platform_auth):
    report = platform_auth.check_ready()
    assert report["production_verified"] is False


def test_postgres_readiness_rejects_administrative_role(platform_auth):
    platform_auth.postgres = True

    @contextmanager
    def connect():
        yield SimpleNamespace(
            execute=lambda *_a, **_k: SimpleNamespace(
                fetchone=lambda: ("postgres", False, True, True, True, False, False)
            )
        )

    platform_auth.database = SimpleNamespace(connect=connect)
    with pytest.raises(PlatformAuthError) as error:
        platform_auth.check_ready()
    assert error.value.status_code == 503
    assert PLATFORM_RUNTIME_ROLE not in str(error.value)


@pytest.mark.parametrize("unsafe", [None, "rls", "missing_table", "permission", "probe"])
def test_postgres_readiness_probes_shared_write_and_forces_rollback(platform_auth, unsafe):
    statements = []
    rolled_back = []

    class Connection:
        def execute(self, sql, params=None):
            statements.append(sql)
            if "FROM pg_catalog.pg_roles" in sql:
                row = (PLATFORM_RUNTIME_ROLE, False, False, False, False, False, False)
            elif "has_schema_privilege" in sql:
                row = (True, False)
            elif "INSERT INTO auth_rate_buckets" in sql:
                if unsafe == "probe":
                    raise sqlite3.OperationalError("sensitive-driver-details")
                row = (0,)
            else:
                row = (unsafe != "permission",)
            tables = [
                (name, unsafe != "rls", True)
                for name in (*BUSINESS_TABLES, "platform_sessions", "auth_rate_buckets")
            ]
            if unsafe == "missing_table":
                tables = tables[1:]
            return SimpleNamespace(fetchone=lambda: row, fetchall=lambda: tables)

    @contextmanager
    def connect():
        yield Connection()

    @contextmanager
    def transaction():
        try:
            yield Connection()
        except Exception:
            rolled_back.append(True)
            raise
        else:
            pytest.fail("readiness must never commit a counter mutation")

    platform_auth.database = SimpleNamespace(connect=connect, transaction=transaction)
    platform_auth.postgres = True
    if unsafe:
        with pytest.raises(PlatformAuthError) as error:
            platform_auth.check_ready()
        assert error.value.status_code == 503
        assert "sensitive-driver-details" not in str(error.value)
    else:
        report = platform_auth.check_ready()
        assert report["production_verified"] is True
        assert report["rls_tables"] == len(BUSINESS_TABLES) + 2
        assert rolled_back == [True]
        assert any("ON CONFLICT" in sql for sql in statements)


def test_login_race_with_password_change_does_not_issue_stale_session(platform_auth, monkeypatch):
    user = platform_auth.register("race-user", PASSWORD, client_ip="test")
    original_verify = platform_auth.security.verify_password
    replacement = platform_auth.security.hash_password("already-changed-456")

    def verify_then_change(candidate, stored):
        verified = original_verify(candidate, stored)
        with platform_auth.database.transaction() as connection:
            connection.execute(
                "UPDATE users SET password_hash=? WHERE id=?", (replacement, user.id)
            )
        return verified

    monkeypatch.setattr(platform_auth.security, "verify_password", verify_then_change)
    with pytest.raises(PlatformAuthError) as error:
        platform_auth.login("race-user", PASSWORD, client_ip="test")
    assert error.value.status_code == 401
    with platform_auth.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM platform_sessions").fetchone()[0] == 0


def test_current_user_role_is_reloaded_not_claimed_from_old_token(
    platform_client, platform_database
):
    user, login = account(platform_client)
    with platform_database.transaction() as connection:
        connection.execute("UPDATE users SET role_code='advisor' WHERE id=?", (user["id"],))
    response = platform_client.get("/v1/auth/me", headers=headers(login))
    assert response.status_code == 200
    assert response.json()["role_code"] == "advisor"


def test_auth_sql_is_accepted_by_production_adapter(platform_auth, monkeypatch):
    from server.platform_database import compile_sql

    database = platform_auth.database
    original_connect = database.connect
    checked = []

    class CompiledCheckConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, statement, params=()):
            if not statement.startswith("BEGIN"):
                compile_sql(statement, params)
                checked.append(statement)
            return self.connection.execute(statement, params)

        def commit(self):
            return self.connection.commit()

        def rollback(self):
            return self.connection.rollback()

    @contextmanager
    def connect():
        with original_connect() as connection:
            yield CompiledCheckConnection(connection)

    monkeypatch.setattr(database, "connect", connect)
    platform_auth.register("compatible-user", PASSWORD, client_ip="test")
    login = platform_auth.login("compatible-user", PASSWORD, client_ip="test")
    principal = platform_auth.resolve_token(login["access_token"])
    platform_auth.change_password(principal, PASSWORD, "changed-compatible-456", client_ip="test")
    platform_auth.logout(principal)
    assert len(checked) > 15
