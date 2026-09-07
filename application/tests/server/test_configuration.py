from __future__ import annotations

import secrets

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from server.app import create_app
from server.config import Settings
from server.database import Database, initialize_development_database
from server.models import Base, LoginSession, User


def production_settings(**changes):
    values = {
        "env": "production",
        "database_url": (
            "postgresql+psycopg://pilot:synthetic-secure-password-123@db.invalid/pilot"
            "?sslmode=verify-full"
        ),
        "token_pepper": secrets.token_hex(32),
        "allowed_hosts": ["api.invalid"],
        "external_auth_limits_configured": True,
    }
    values.update(changes)
    return Settings(**values)


@pytest.mark.parametrize(
    "changes",
    [
        {"database_url": "sqlite+pysqlite:///:memory:"},
        {
            "database_url": "postgresql+psycopg://pilot:changeme@db.invalid/pilot?sslmode=verify-full"
        },
        {"database_url": "postgresql+psycopg://pilot:synthetic-secure-password@db.invalid/pilot"},
        {"token_pepper": "changeme"},
        {"token_pepper": "a" * 64},
        {"require_https": False},
        {"allowed_hosts": ["*"]},
        {"allowed_hosts": ["localhost"]},
        {"external_auth_limits_configured": False},
    ],
)
def test_unsafe_production_configuration_is_rejected(changes):
    with pytest.raises(ValidationError):
        production_settings(**changes)


def test_env_configuration_has_no_secret_defaults(monkeypatch):
    import os

    for name in list(os.environ):
        if name.startswith("SERVER_"):
            monkeypatch.delenv(name)
    with pytest.raises(ValidationError):
        Settings()


def test_startup_never_creates_tables_and_readiness_detects_missing_schema(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503
        assert inspect(app.state.database.engine).get_table_names() == []


def test_readiness_requires_all_expected_columns(client, app):
    assert client.get("/health/ready").status_code == 200
    LoginSession.__table__.drop(app.state.database.engine)
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert "pilot_login_sessions" not in response.text


def test_development_initialization_is_explicit_idempotent_and_inserts_no_demo_rows(settings):
    development = Settings(**{**settings.model_dump(), "env": "development"})
    initialize_development_database(development)
    initialize_development_database(development)
    database = Database(development)
    try:
        with database.sessions() as db:
            assert db.query(User).count() == 0
        assert set(inspect(database.engine).get_table_names()) == {
            table.name for table in Base.metadata.tables.values()
        }
    finally:
        database.engine.dispose()
    with pytest.raises(RuntimeError, match="development"):
        initialize_development_database(production_settings())


def test_postgresql_ddl_uses_native_uuid_and_timezone_aware_timestamp_without_network():
    for table in Base.metadata.sorted_tables:
        sql = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        assert "UUID" in sql
        assert "TIMESTAMP WITH TIME ZONE" in sql
        assert "api_key" not in sql.casefold()


def test_host_and_https_boundaries(settings):
    secure = Settings(**{**settings.model_dump(), "require_https": True})
    with TestClient(create_app(secure)) as client:
        assert client.post("/auth/login", json={}).status_code == 426
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/live", headers={"Host": "evil.invalid"}).status_code == 400


@pytest.mark.parametrize("streamed", [True, False])
def test_request_body_limits_cover_declared_and_chunked_bodies(client, settings, streamed):
    payload = b"x" * (settings.max_body_bytes + 1)
    content = iter([payload[:1000], payload[1000:]]) if streamed else payload
    response = client.post(
        "/auth/login", content=content, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413


def test_invalid_content_length_is_rejected(client):
    response = client.post("/auth/login", content=b"{}", headers={"Content-Length": "invalid"})
    assert response.status_code == 400
