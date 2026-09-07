"""Independent cloud configuration review; no credentials or real network access."""

from __future__ import annotations

import io
import warnings
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from sqlalchemy.engine import make_url

from server import cloud_connection as cloud

PROJECT_REF = "rvtjveplqnelgwnpuqtn"
PROJECT = f"https://{PROJECT_REF}.supabase.co"
POOL_HOST = "aws-0-ap-southeast-1.pooler.supabase.com"
POOL_URI = f"postgresql://postgres.{PROJECT_REF}:[YOUR-PASSWORD]@{POOL_HOST}:5432/postgres"


@pytest.mark.parametrize(
    "uri",
    [
        POOL_URI.replace(POOL_HOST, "evil.example"),
        POOL_URI.replace(POOL_HOST, f"{POOL_HOST}.evil.example"),
        POOL_URI.replace(POOL_HOST, "127.0.0.1"),
        POOL_URI.replace(POOL_HOST, "[::1]"),
        POOL_URI.replace(POOL_HOST, f"{POOL_HOST},evil.example"),
        POOL_URI.replace(POOL_HOST, f"db.{'a' * 20}.supabase.co"),
        POOL_URI.replace(PROJECT_REF, "a" * 20),
        POOL_URI.replace(f"postgres.{PROJECT_REF}", "postgres"),
        POOL_URI.replace("postgres.", "postgres%0A."),
        POOL_URI.replace(":5432/", ":6543/"),
        POOL_URI.replace(":5432/", "/"),
        POOL_URI.replace("/postgres", "/another_database"),
        POOL_URI.replace("postgresql://", "https://"),
        POOL_URI + "?host=evil.example",
        POOL_URI + "?hostaddr=127.0.0.1",
        POOL_URI + "?service=other",
        POOL_URI + "?passfile=C%3A%2Fother",
        POOL_URI + "?options=-c%20search_path%3Dpublic",
        POOL_URI + "?sslmode=verify-full&sslmode=disable",
        POOL_URI + "?sslrootcert=first&sslrootcert=second",
        "\n" + POOL_URI,
        POOL_URI + "\x00",
        "not a connection string",
        "postgresql://host:not-an-integer/postgres",
    ],
)
def test_rejects_connection_target_and_parameter_bypasses(uri):
    with pytest.raises(cloud.CloudConfigurationError):
        cloud.validated_url(uri, PROJECT)


@pytest.mark.parametrize("role", ["postgres", "medical_runtime", "medical_migrator"])
def test_accepts_project_bound_pooler_and_direct_roles(role):
    pool = cloud.validated_url(POOL_URI.replace("postgres.", f"{role}."), PROJECT)
    assert pool.username == f"{role}.{PROJECT_REF}"
    assert pool.host == POOL_HOST
    direct = cloud.validated_url(
        f"postgresql://{role}:placeholder@db.{PROJECT_REF}.supabase.co:5432/postgres", PROJECT
    )
    assert direct.username == role


@pytest.mark.parametrize(
    "value",
    [
        PROJECT.replace("https://", "http://"),
        PROJECT + "/rest/v1/",
        PROJECT + "?other=true",
        PROJECT + "#fragment",
        PROJECT + ":443",
        PROJECT.replace("https://", "https://@"),
        PROJECT.replace("https://", "https://:@"),
        PROJECT + ".evil.example",
    ],
)
def test_project_url_rejects_noncanonical_or_userinfo_variants(value):
    with pytest.raises(cloud.CloudConfigurationError):
        cloud.project_reference(value)


@pytest.mark.parametrize(
    "password", ["  synthetic @ : / ? # % + \\ 密码  ", "'quoted\"synthetic'", "空格 密码"]
)
def test_separate_password_round_trip_and_forced_certificate_options(monkeypatch, password):
    monkeypatch.setattr(cloud, "project_url", lambda: PROJECT)
    monkeypatch.setattr(cloud, "ca_file", lambda _value: "C:/Synthetic Folder/root.crt")
    payload = cloud.connection_payload(POOL_URI + "?sslmode=disable", password, None)
    result = make_url(payload["migration_database_url"])
    assert result.password == password
    assert result.query == {
        "sslmode": "verify-full",
        "sslrootcert": "C:/Synthetic Folder/root.crt",
    }
    assert payload["purpose"] == "migration-readonly-preflight"


@pytest.mark.parametrize("password", ["", "\x00bad", "[YOUR-PASSWORD]", "YOUR-PASSWORD"])
def test_rejects_empty_nul_and_placeholder_passwords(monkeypatch, password):
    monkeypatch.setattr(cloud, "project_url", lambda: PROJECT)
    with pytest.raises(cloud.CloudConfigurationError):
        cloud.connection_payload(POOL_URI, password, None)


def test_redirected_input_is_rejected_before_getpass(monkeypatch):
    monkeypatch.setattr(cloud.sys, "stdin", io.StringIO("never read synthetic password"))

    def forbidden_getpass(_prompt):
        pytest.fail("getpass must not run without an interactive terminal")

    monkeypatch.setattr(cloud.getpass, "getpass", forbidden_getpass)
    with pytest.raises(cloud.CloudConfigurationError):
        cloud.hidden_input("Synthetic prompt")


def test_getpass_echo_warning_fails_closed(monkeypatch):
    monkeypatch.setattr(cloud.sys, "stdin", SimpleNamespace(isatty=lambda: True))

    def unsafe_getpass(_prompt):
        warnings.warn("Password input may be echoed", cloud.getpass.GetPassWarning, stacklevel=2)
        pytest.fail("warning must be raised before input fallback")

    monkeypatch.setattr(cloud.getpass, "getpass", unsafe_getpass)
    with pytest.raises(cloud.CloudConfigurationError):
        cloud.hidden_input("Synthetic prompt")


class _FakeConnection:
    def __init__(self, *, tls=True, fail_query=False):
        self.pgconn = SimpleNamespace(ssl_in_use=tls)
        self.events = []
        self._read_only = False
        self.fail_query = fail_query

    def __enter__(self):
        self.events.append("enter")
        return self

    def __exit__(self, exc_type, _error, _traceback):
        self.events.append(("exit", exc_type))

    @property
    def read_only(self):
        return self._read_only

    @read_only.setter
    def read_only(self, value):
        self.events.append(("read_only", value))
        self._read_only = value

    def execute(self, statement, params=None):
        assert self.read_only is True, "read-only mode must precede every statement"
        self.events.append(("execute", statement, params))
        assert statement.startswith(("SET LOCAL ", "SELECT "))
        if self.fail_query and statement.startswith("SELECT "):
            raise psycopg.OperationalError("synthetic database failure")
        return self

    def fetchone(self):
        return "18.synthetic", "postgres", "synthetic_migrator", False

    def rollback(self):
        self.events.append("rollback")


def _mock_probe(monkeypatch, *, tls=True, fail_query=False):
    monkeypatch.setattr(cloud, "project_url", lambda: PROJECT)
    monkeypatch.setattr(cloud, "ca_file", lambda _value: "C:/synthetic-root.crt")
    password = " synthetic @ : % / # + password "
    payload = cloud.connection_payload(POOL_URI, password, None)
    monkeypatch.setattr(cloud, "load_secret_payload", lambda _path: payload)
    connection = _FakeConnection(tls=tls, fail_query=fail_query)
    captured = {}

    def connect(**kwargs):
        captured.update(kwargs)
        return connection

    monkeypatch.setattr(cloud.psycopg, "connect", connect)
    return connection, captured, payload, password


def test_probe_uses_exact_kwargs_readonly_first_and_explicit_rollback(monkeypatch):
    connection, captured, _payload, password = _mock_probe(monkeypatch)
    result = cloud.probe_profile(Path("synthetic-profile-not-read.json"))
    assert captured["host"] == POOL_HOST
    assert captured["port"] == 5432
    assert captured["dbname"] == "postgres"
    assert captured["user"] == f"postgres.{PROJECT_REF}"
    assert captured["password"] == password
    assert captured["sslmode"] == "verify-full"
    assert captured["sslrootcert"] == "C:/synthetic-root.crt"
    assert 1 <= captured["connect_timeout"] <= 10
    assert connection.events[:2] == ["enter", ("read_only", True)]
    assert connection.events[-2:] == ["rollback", ("exit", None)]
    assert result["tables_changed"] is False
    assert result["health_records_read"] == 0
    assert result["desktop_sync_enabled"] is False
    assert password not in str(result)
    assert "migration_database_url" not in result


def test_probe_rolls_back_after_database_query_failure(monkeypatch):
    connection, _captured, _payload, _password = _mock_probe(monkeypatch, fail_query=True)
    with pytest.raises(psycopg.OperationalError):
        cloud.probe_profile(Path("synthetic-profile-not-read.json"))
    assert connection.events[-2:] == ["rollback", ("exit", psycopg.OperationalError)]


def test_probe_no_tls_rejects_before_any_sql(monkeypatch):
    connection, _captured, _payload, _password = _mock_probe(monkeypatch, tls=False)
    with pytest.raises(cloud.CloudConfigurationError):
        cloud.probe_profile(Path("synthetic-profile-not-read.json"))
    assert not any(
        isinstance(event, tuple) and event[0] == "execute" for event in connection.events
    )


@pytest.mark.parametrize(
    "change",
    [
        {"project_url": "https://" + "a" * 20 + ".supabase.co"},
        {"purpose": "runtime"},
        {"unexpected": "synthetic-secret"},
    ],
)
def test_wrong_project_purpose_or_extra_profile_keys_prevent_connection(monkeypatch, change):
    _connection, captured, payload, _password = _mock_probe(monkeypatch)
    payload.update(change)
    with pytest.raises(cloud.CloudConfigurationError):
        cloud.probe_profile(Path("synthetic-profile-not-read.json"))
    assert captured == {}


@pytest.mark.parametrize(
    "error_type, prefix",
    [
        (psycopg.errors.InvalidPassword, "password authentication failed"),
        (psycopg.OperationalError, "TLS certificate verify failed"),
        (psycopg.OperationalError, "Tenant or user not found"),
        (psycopg.OperationalError, "unknown connection failure"),
        (ValueError, "unexpected failure"),
    ],
)
def test_cli_error_output_never_contains_raw_driver_text_or_secret(
    monkeypatch, capsys, error_type, prefix
):
    raw = f"{prefix} PRIVATE-ERROR-MARKER DSN={POOL_URI} password=synthetic-super-secret"

    def fail(_path):
        raise error_type(raw)

    monkeypatch.setattr(cloud, "probe_profile", fail)
    monkeypatch.setattr(cloud.sys, "argv", ["cloud_connection", "check"])
    assert cloud.main() in (2, 3)
    result = capsys.readouterr()
    assert "synthetic-super-secret" not in result.out + result.err
    assert "PRIVATE-ERROR-MARKER" not in result.out + result.err
    assert POOL_URI not in result.out + result.err
    assert result.err == ""


def test_status_does_not_read_decrypt_or_connect(monkeypatch, tmp_path, capsys):
    def forbidden(*_args, **_kwargs):
        pytest.fail("status must not decrypt or connect")

    monkeypatch.setattr(cloud, "load_secret_payload", forbidden)
    monkeypatch.setattr(cloud.psycopg, "connect", forbidden)
    monkeypatch.setattr(cloud, "project_url", lambda: PROJECT)
    monkeypatch.setattr(
        cloud.sys,
        "argv",
        ["cloud_connection", "status", "--profile", str(tmp_path / "absent.json")],
    )
    assert cloud.main() == 0
    output = capsys.readouterr().out
    assert '"profile_exists": false' in output
    assert '"cloud_connected": "not_checked"' in output


def test_configure_existing_profile_is_not_overwritten_or_prompted(monkeypatch, tmp_path):
    path = tmp_path / "synthetic-existing-profile.json"
    path.write_text("synthetic old encrypted payload", encoding="utf-8")
    monkeypatch.setattr(cloud, "project_url", lambda: PROJECT)

    def forbidden(*_args, **_kwargs):
        pytest.fail("existing profile must not be changed without --replace")

    monkeypatch.setattr(cloud, "hidden_input", forbidden)
    monkeypatch.setattr(cloud, "save_secret_payload", forbidden)
    with pytest.raises(cloud.CloudConfigurationError):
        cloud.configure(path)
    assert path.read_text(encoding="utf-8") == "synthetic old encrypted payload"
