from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from server import cloud_connection as cloud

REF = "rvtjveplqnelgwnpuqtn"
PUBLIC_URL = f"https://{REF}.supabase.co"
URI = f"postgresql://postgres.{REF}:[YOUR-PASSWORD]@aws-0-test.pooler.supabase.com:5432/postgres"


def test_public_project_metadata_has_no_secret_or_enabled_sync():
    data = json.loads(cloud.PROJECT_FILE.read_text(encoding="utf-8"))
    assert data["project_url"] == PUBLIC_URL
    assert data["desktop_sync_enabled"] is False
    assert set(data) == {"project_url", "purpose", "desktop_sync_enabled"}


@pytest.mark.parametrize(
    "uri",
    [
        "not-a-connection-string",
        "https://example.com",
        URI.replace(REF, "a" * 20),
        URI.replace("5432", "6543"),
        URI.replace("pooler.supabase.com", "pooler.supabase.com.attacker.invalid"),
        URI + "?host=attacker.invalid",
        URI + "?hostaddr=127.0.0.1",
        URI + "?service=other",
        URI + "?options=-c%20search_path=public",
        URI + "?passfile=private",
        URI + "?sslmode=require&sslmode=disable",
        URI.replace("/postgres", "/other_database"),
    ],
)
def test_invalid_or_redirected_uri_is_rejected_without_input_echo(uri):
    with pytest.raises(cloud.CloudConfigurationError) as error:
        cloud.validated_url(uri, PUBLIC_URL)
    assert uri not in str(error.value)
    assert "YOUR-PASSWORD" not in str(error.value)


@pytest.mark.parametrize(
    "url",
    [
        PUBLIC_URL.replace("https", "http"),
        PUBLIC_URL + "/rest/v1/",
        PUBLIC_URL.replace("https://", "https://@"),
        PUBLIC_URL + "?key=secret",
    ],
)
def test_project_url_is_exact_https_entrypoint(url):
    with pytest.raises(cloud.CloudConfigurationError):
        cloud.project_reference(url)


def test_password_special_characters_and_spaces_are_preserved():
    password = "  synthetic@:#/%密码\\not-real  "
    payload = cloud.connection_payload(URI + "?sslmode=require", password, None)
    parsed = cloud.validated_url(payload["migration_database_url"], PUBLIC_URL)
    assert parsed.password == password
    assert parsed.query["sslmode"] == "verify-full"
    assert Path(parsed.query["sslrootcert"]).is_file()
    assert "runtime_database_url" not in payload


def test_no_tty_does_not_read_or_echo_secrets(monkeypatch):
    monkeypatch.setattr(cloud.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cloud.getpass, "getpass", lambda *_a: pytest.fail("must not read"))
    with pytest.raises(cloud.CloudConfigurationError, match="交互终端"):
        cloud.hidden_input("hidden")


class FakeConnection:
    def __init__(self):
        self.read_only = False
        self.pgconn = SimpleNamespace(ssl_in_use=True)
        self.queries = []
        self.rollbacks = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params=None):
        assert self.read_only
        self.queries.append((statement, params))
        return SimpleNamespace(fetchone=lambda: ("18.4", "postgres", "postgres", False))

    def rollback(self):
        self.rollbacks += 1


def test_probe_only_metadata_readonly_tls_and_forced_rollback(monkeypatch, tmp_path):
    password = "synthetic@pass:word/123"
    payload = cloud.connection_payload(URI, password, None)
    monkeypatch.setattr(cloud, "load_secret_payload", lambda _path: payload)
    connection = FakeConnection()
    calls = []

    def connect(**kwargs):
        calls.append(kwargs)
        return connection

    monkeypatch.setattr(cloud.psycopg, "connect", connect)
    report = cloud.probe_profile(tmp_path / "fake.json")
    assert calls[0]["password"] == password
    assert calls[0]["sslmode"] == "verify-full"
    assert calls[0]["connect_timeout"] == 8
    assert "hostaddr" not in calls[0]
    assert len(connection.queries) == 2
    assert connection.queries[0][0].startswith("SET LOCAL statement_timeout")
    assert "pg_catalog.pg_namespace" in connection.queries[1][0]
    assert connection.rollbacks == 1
    assert report["tables_changed"] is False
    assert report["health_records_read"] == 0
    assert report["desktop_sync_enabled"] is False
    assert password not in json.dumps(report)


def test_probe_exception_rolls_back_and_cli_does_not_leak(monkeypatch, tmp_path, capsys):
    payload = cloud.connection_payload(URI, "synthetic-sensitive-password", None)
    monkeypatch.setattr(cloud, "load_secret_payload", lambda _path: payload)
    connection = FakeConnection()

    def failed_query(*_args, **_kwargs):
        raise psycopg.OperationalError("connection password=synthetic-sensitive-password failed")

    connection.execute = failed_query
    monkeypatch.setattr(cloud.psycopg, "connect", lambda **_kwargs: connection)
    monkeypatch.setattr(
        cloud.sys, "argv", ["cloud_connection", "check", "--profile", str(tmp_path / "fake.json")]
    )
    assert cloud.main() == 3
    assert connection.rollbacks == 1
    assert "synthetic-sensitive-password" not in capsys.readouterr().out


def test_status_does_not_read_secret_or_connect(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cloud, "load_secret_payload", lambda _p: pytest.fail("must not decrypt"))
    monkeypatch.setattr(cloud.psycopg, "connect", lambda **_k: pytest.fail("must not connect"))
    monkeypatch.setattr(
        cloud.sys,
        "argv",
        ["cloud_connection", "status", "--profile", str(tmp_path / "absent.json")],
    )
    assert cloud.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert not result["profile_exists"]
    assert result["cloud_connected"] == "not_checked"
