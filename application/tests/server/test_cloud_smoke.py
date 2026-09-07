from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from server import cloud_smoke
from server.database import Database
from server.models import Base, User


def _counts(settings):
    database = Database(settings)
    try:
        with database.engine.connect() as connection:
            return {
                table.name: connection.scalar(select(func.count()).select_from(table))
                for table in Base.metadata.sorted_tables
            }
    finally:
        database.engine.dispose()


def test_success_rolls_back_all_api_commits_and_reports_only_whitelist(settings, app, capsys):
    result = cloud_smoke.run_smoke(settings)
    assert result["status"] == "passed_rolled_back"
    assert result["rollback_completed"] is True
    assert result["synthetic_records_persisted"] == 0
    assert result["network_https_tested"] is False
    assert result["concurrent_requests_tested"] is False
    assert result["ai_calls"] == 0
    assert len(result["checks"]) == 15
    assert all(count == 0 for count in _counts(settings).values())
    assert capsys.readouterr().out == ""
    serialized = json.dumps(result)
    for value in ("password", "access_token", "合成验收消息", "SELECT", "INSERT", "Bearer"):
        assert value not in serialized


def test_existing_records_untouched(settings, app):
    with app.state.database.sessions.begin() as session:
        session.add(
            User(
                username="existing",
                username_normalized="existing",
                password_hash="pre-existing-synthetic-hash",
            )
        )
    before = _counts(settings)
    cloud_smoke.run_smoke(settings)
    assert _counts(settings) == before


def test_mid_run_exception_rolls_back_committed_synthetic_registration(
    settings, app, monkeypatch, capsys
):
    secret = "exception-must-not-leak-password-token-SQL"

    def broken(first, second, usernames):
        response = first.post(
            "/auth/register",
            json={
                "username": usernames[0],
                "password": "synthetic-password-123",
            },
        )
        assert response.status_code == 201
        raise RuntimeError(secret)

    monkeypatch.setattr(cloud_smoke, "_exercise", broken)
    with pytest.raises(cloud_smoke.CloudSmokeError) as error:
        cloud_smoke.run_smoke(settings)
    assert str(error.value) == "database_or_test_unavailable"
    assert secret not in str(error.value)
    assert all(count == 0 for count in _counts(settings).values())
    assert secret not in capsys.readouterr().out


def test_api_assertion_failure_rolls_back(settings, app, monkeypatch):
    def broken(first, second, usernames):
        response = first.post(
            "/auth/register",
            json={
                "username": usernames[0],
                "password": "synthetic-password-123",
            },
        )
        assert response.status_code == 201
        cloud_smoke._require(False)

    monkeypatch.setattr(cloud_smoke, "_exercise", broken)
    with pytest.raises(cloud_smoke.CloudSmokeError, match="^api_check_failed$"):
        cloud_smoke.run_smoke(settings)
    assert all(count == 0 for count in _counts(settings).values())


def test_missing_schema_fails_closed_without_initializing(settings):
    with pytest.raises(cloud_smoke.CloudSmokeError, match="^readiness_failed$"):
        cloud_smoke.run_smoke(settings)
    database = Database(settings)
    try:
        from sqlalchemy import inspect

        assert inspect(database.engine).get_table_names() == []
    finally:
        database.engine.dispose()


@pytest.mark.parametrize("arguments", [[], ["--confirm", "wrong"]])
def test_cli_requires_confirmation_before_loading_config(arguments, monkeypatch, capsys):
    def forbidden(_profile):
        pytest.fail("Must not read configuration without confirmation")

    monkeypatch.setattr(cloud_smoke, "_load_settings", forbidden)
    assert cloud_smoke.main(arguments) == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "not_run",
        "error": "confirmation_required",
    }


def test_cli_configuration_failure_never_echoes_secrets(monkeypatch, capsys):
    def broken(_profile):
        raise RuntimeError("postgresql://password-must-not-leak")

    monkeypatch.setattr(cloud_smoke, "_load_settings", broken)
    assert cloud_smoke.main(["--confirm", "medical_app_private"]) == 3
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "error": "configuration_unavailable",
    }


def test_cli_success_uses_injected_configuration(settings, app, monkeypatch, capsys):
    monkeypatch.setattr(cloud_smoke, "_load_settings", lambda _profile: settings)
    assert cloud_smoke.main(["--confirm", "medical_app_private"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "passed_rolled_back"
    assert all(count == 0 for count in _counts(settings).values())


def test_error_constructor_has_fixed_whitelist():
    error = cloud_smoke.CloudSmokeError("secret-token-unknown")
    assert str(error) == "database_or_test_unavailable"


@pytest.mark.parametrize(
    "sslmode,tls,role",
    [
        ("require", True, "medical_app_runtime"),
        ("verify-full", False, "medical_app_runtime"),
        ("verify-full", True, "postgres"),
    ],
)
def test_postgres_smoke_rejects_weak_tls_or_administrator(settings, sslmode, tls, role):
    connection = Mock()
    connection.dialect = SimpleNamespace(name="postgresql")
    connection.connection.driver_connection.pgconn.ssl_in_use = tls
    connection.exec_driver_sql.return_value.scalar_one.return_value = role
    settings = settings.model_copy(
        update={
            "database_url": SecretStr(
                "postgresql+psycopg://synthetic@db.invalid/postgres?sslmode="
                + sslmode
                + "&sslrootcert=synthetic.crt"
            )
        }
    )
    with pytest.raises(cloud_smoke.CloudSmokeError, match="runtime_security_failed"):
        cloud_smoke._postgres_checks(connection, settings)
