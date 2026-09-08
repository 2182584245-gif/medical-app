"""Offline contract tests only: no Docker daemon, cloud, network or real data."""

from __future__ import annotations

import json
import os
import ssl
from contextlib import ExitStack
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from platform_test_support import PEPPER, SyntheticDatabase

from server.platform_app import create_app
from server.platform_config import PlatformSettings
from tools.local_platform import smoke


@pytest.fixture
def local_state(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CONTAINER_ONLY", "1")
    path = tmp_path / "state.json"
    monkeypatch.setattr(smoke, "STATE_FILE", path)
    return path


@pytest.fixture
def app_factory():
    database = SyntheticDatabase()
    settings = PlatformSettings(
        env="test", database_url="sqlite+pysqlite:///:memory:", token_pepper=PEPPER,
        allowed_hosts=["api"], require_https=True,
    )
    application = create_app(settings, database=database)
    with ExitStack():
        yield database, lambda: TestClient(
            application, base_url=smoke.API_ORIGIN, raise_server_exceptions=False,
        )
    database.close()


def test_full_business_contract_and_persistence_readback(local_state, app_factory):
    database, factory = app_factory
    first = smoke.run(_client_factory=factory)
    assert first["status"] == "passed", first
    assert first["mode"] == "offline_contract_test"
    assert first["cloud_connected"] is first["ai_called"] is False
    assert first["counts"]["accounts"] == 2
    assert first["counts"]["verified_conversations"] == 2
    assert database.scalar("SELECT COUNT(*) FROM users") == 2
    assert database.scalar("SELECT COUNT(*) FROM messages") == 2
    assert database.scalar("SELECT COUNT(*) FROM platform_sessions WHERE revoked_at IS NULL") == 0
    before = local_state.read_bytes()
    second = smoke.run(verify_persistence=True, _client_factory=factory)
    assert second["status"] == "passed", second
    assert second["operation"] == "verify_persistence"
    assert local_state.read_bytes() == before
    assert database.scalar("SELECT COUNT(*) FROM users") == 2
    assert database.scalar("SELECT COUNT(*) FROM messages") == 2
    assert database.scalar("SELECT COUNT(*) FROM platform_sessions WHERE revoked_at IS NULL") == 0
    for account in json.loads(before)["accounts"]:
        for report in (first, second):
            output = json.dumps(report)
            assert account["password"] not in output
            assert account["username"] not in output
    assert "access_token" not in json.dumps(first)


@pytest.mark.parametrize("guard", [None, "", "0", "true", "yes"])
def test_missing_local_guard_refuses_before_network(local_state, monkeypatch, guard):
    if guard is None:
        monkeypatch.delenv("LOCAL_CONTAINER_ONLY")
    else:
        monkeypatch.setenv("LOCAL_CONTAINER_ONLY", guard)

    def forbidden():
        pytest.fail("client must not be created")

    report = smoke.run(_client_factory=forbidden)
    assert report["status"] == "failed"
    assert report["code"] == "configuration"
    assert not local_state.exists()


def test_existing_state_is_never_overwritten(local_state):
    local_state.write_text("synthetic existing state", encoding="utf-8")

    def forbidden():
        pytest.fail("client must not be created")

    report = smoke.run(_client_factory=forbidden)
    assert report["code"] == "state_exists"
    assert local_state.read_text(encoding="utf-8") == "synthetic existing state"


def test_missing_persistence_state_refuses_before_network(local_state):
    report = smoke.run(verify_persistence=True, _client_factory=lambda: pytest.fail("network"))
    assert report["code"] == "state_missing"


@pytest.mark.parametrize("content", ["not JSON", "{}", "[]", '"secret"', "x" * 8193])
def test_invalid_state_refused_without_echo(local_state, content):
    local_state.write_text(content, encoding="utf-8")
    local_state.chmod(0o600)
    report = smoke.run(verify_persistence=True, _client_factory=lambda: pytest.fail("network"))
    assert report["code"] == "state_invalid"
    assert "secret" not in json.dumps(report)


@pytest.mark.parametrize("mutation", ["username", "password", "role", "host", "negative_id"])
def test_saved_state_rejects_non_synthetic_fields(local_state, app_factory, mutation):
    _, factory = app_factory
    assert smoke.run(_client_factory=factory)["status"] == "passed"
    state = json.loads(local_state.read_text(encoding="utf-8"))
    if mutation in {"username", "password"}:
        state["accounts"][0][mutation] = "not_a_synthetic_credential"
    elif mutation == "role":
        state["accounts"][0]["role"] = "operator"
    elif mutation == "host":
        state["base_url"] = "https://cloud.example.invalid"
    else:
        state["file_id"] = -1
    local_state.write_text(json.dumps(state), encoding="utf-8")
    report = smoke.run(verify_persistence=True, _client_factory=lambda: pytest.fail("network"))
    assert report["code"] == "state_invalid"


def test_http_client_pins_origin_ca_no_proxy_or_redirects(monkeypatch):
    captured = {}
    context = ssl.create_default_context()

    def fake_context(*, cafile):
        captured["cafile"] = cafile
        return context

    def fake_client(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(smoke.ssl, "create_default_context", fake_context)
    monkeypatch.setattr(smoke.httpx, "Client", fake_client)
    smoke._create_client()
    assert captured["base_url"] == "https://api:8443"
    assert captured["cafile"] == str(Path("/run/api-secrets/ca.crt"))
    assert captured["verify"] is context
    assert context.check_hostname is True and context.verify_mode == ssl.CERT_REQUIRED
    assert captured["trust_env"] is captured["follow_redirects"] is False


@pytest.mark.parametrize("exception,code", [
    (httpx.ConnectError("SECRET connection string"), "network"),
    (httpx.ReadTimeout("SECRET password"), "timeout"),
    (ssl.SSLError("SECRET certificate path"), "tls"),
    (RuntimeError("SECRET response body"), "check_failed"),
])
def test_failures_are_sanitized(local_state, exception, code):
    def failed():
        raise exception

    report = smoke.run(_client_factory=failed)
    assert report["status"] == "failed" and report["code"] == code
    assert "SECRET" not in json.dumps(report)
    assert not local_state.exists()


def test_cli_accepts_no_origin_or_secret_arguments():
    with pytest.raises(SystemExit) as error:
        smoke.main(["--base-url", "https://cloud.example.invalid"])
    assert error.value.code == 2


def test_cli_prints_only_safe_json(monkeypatch, capsys):
    expected = {"status": "passed", "check_count": 14}
    monkeypatch.setattr(smoke, "run", lambda **_kwargs: expected)
    assert smoke.main([]) == 0
    assert json.loads(capsys.readouterr().out) == expected


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only private Docker volume permissions")
def test_state_has_private_mode_and_public_mode_refused(local_state, app_factory):
    _, factory = app_factory
    assert smoke.run(_client_factory=factory)["status"] == "passed"
    assert local_state.stat().st_mode & 0o777 == 0o600
    local_state.chmod(0o644)
    report = smoke.run(verify_persistence=True, _client_factory=lambda: pytest.fail("network"))
    assert report["code"] == "state_invalid"
