"""Offline Railway deployment-boundary tests; no real credentials or networking."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from platform_test_support import PEPPER, SyntheticDatabase
from pydantic import ValidationError
from starlette.responses import JSONResponse

from server import platform_admin, platform_entrypoint
from server.platform_app import PlatformBoundary, create_app
from server.platform_config import RAILWAY_HEALTHCHECK_HOST, PlatformSettings

HOST = "synthetic-medical-app.up.railway.app"
IDS = {
    "RAILWAY_SERVICE_ID": "11111111-1111-4111-8111-111111111111",
    "RAILWAY_PROJECT_ID": "22222222-2222-4222-8222-222222222222",
    "RAILWAY_ENVIRONMENT_ID": "33333333-3333-4333-8333-333333333333",
}
DATABASE_URL = (
    "postgresql+psycopg://medical_app_platform_runtime:"
    + "synthetic-password-not-used-to-connect" * 2
    + "@example.invalid/postgres?sslmode=verify-full&sslrootcert=synthetic.crt"
)


@pytest.fixture
def railway_environment(monkeypatch):
    for name, value in IDS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", HOST)


def production_settings(**overrides):
    values = dict(
        env="production", database_url=DATABASE_URL, token_pepper=PEPPER,
        railway_proxy=True, railway_edge_only=True, allowed_hosts=[HOST],
    )
    return PlatformSettings(**(values | overrides))


def test_explicit_railway_settings_and_domain_derivation(railway_environment):
    settings = production_settings(allowed_hosts=[])
    assert settings.allowed_hosts == [HOST]
    assert settings.railway_proxy and settings.railway_edge_only and not settings.render_proxy
    assert settings.require_https


def test_platform_variables_do_not_automatically_enable_trust(railway_environment):
    settings = production_settings(railway_proxy=False, railway_edge_only=False)
    assert not settings.railway_proxy and not settings.render_proxy
    with pytest.raises(ValidationError):
        production_settings(railway_proxy=False, railway_edge_only=False, allowed_hosts=[])


@pytest.mark.parametrize("options", [
    {"railway_proxy": True, "railway_edge_only": False},
    {"railway_proxy": False, "railway_edge_only": True},
    {"render_proxy": True},
    {"require_https": False},
])
def test_missing_confirmation_or_conflicting_platforms_fail_closed(railway_environment, options):
    with pytest.raises(ValidationError) as error:
        production_settings(**options)
    assert "synthetic-password" not in str(error.value)


@pytest.mark.parametrize("name", list(IDS))
@pytest.mark.parametrize("value", ["", "not-a-uuid", "11111111111141118111111111111111"])
def test_incomplete_or_noncanonical_metadata_rejected(
    railway_environment, monkeypatch, name, value
):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        production_settings()


@pytest.mark.parametrize("host", [
    "", "*.up.railway.app", "up.railway.app", "a.b.up.railway.app", "-a.up.railway.app",
    "a-.up.railway.app", "a" * 64 + ".up.railway.app", "a.up.railway.app.evil.example",
    "https://a.up.railway.app", "a.up.railway.app/path", "a.up.railway.app:443",
    "a.up.railway.app\n", "a..up.railway.app",
])
def test_generated_domain_must_be_exact_single_label(railway_environment, monkeypatch, host):
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", host)
    with pytest.raises(ValidationError):
        production_settings(allowed_hosts=[])


@pytest.mark.parametrize("hosts", [
    ["other.up.railway.app"], [HOST, "evil.example"], [HOST, "*.up.railway.app"],
])
def test_railway_hosts_are_exact_not_general_custom_hosts(railway_environment, hosts):
    with pytest.raises(ValidationError):
        production_settings(allowed_hosts=hosts)


def test_fixed_health_host_allowed_as_extra_not_as_public_domain(railway_environment):
    assert production_settings(allowed_hosts=[HOST, RAILWAY_HEALTHCHECK_HOST]).allowed_hosts == [
        HOST, RAILWAY_HEALTHCHECK_HOST
    ]


async def _request(*, headers=(), peer="8.8.8.8", path="/v1/auth/me", method="GET",
                   railway_proxy=True, railway_edge_only=True, render_proxy=False,
                   scheme="http", host=HOST):
    sent, observed = [], []
    reads = 0

    async def application(scope, receive, send):
        observed.append(scope)
        await JSONResponse({"scheme": scope["scheme"], "peer": scope["client"][0]})(
            scope, receive, send
        )

    async def receive():
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    settings = SimpleNamespace(
        railway_proxy=railway_proxy, railway_edge_only=railway_edge_only,
        render_proxy=render_proxy, require_https=True, allowed_hosts=[HOST],
        max_body_bytes=1024 * 1024, body_timeout_seconds=30.0,
    )
    boundary = PlatformBoundary(application, settings=settings)
    await boundary(
        {"type": "http", "scheme": scheme, "method": method, "path": path,
         "client": (peer, 10000),
         "headers": ([(b"host", host.encode())] if host is not None else []) + list(headers)},
        receive, send,
    )
    return sent[0]["status"], json.loads(sent[-1]["body"]), reads, observed


def request(**kwargs):
    return asyncio.run(_request(**kwargs))


@pytest.mark.parametrize("peer", ["8.8.8.8", "10.1.2.3", "2606:4700:4700::1111", "::1"])
def test_explicit_isolated_ingress_restores_https_without_inventing_cidr(peer):
    status, result, _reads, observed = request(
        peer=peer, headers=[(b"x-forwarded-proto", b"https")]
    )
    assert status == 200 and result == {"scheme": "https", "peer": peer}
    assert observed[0]["client"] == (peer, 10000)


@pytest.mark.parametrize("headers", [
    [], [(b"x-forwarded-proto", b"http")], [(b"x-forwarded-proto", b"https,http")],
    [(b"x-forwarded-proto", b"HTTPS")], [(b"x-forwarded-proto", b" https")],
])
def test_only_exact_https_forwarded_scheme_is_honored(headers):
    status, _result, reads, _observed = request(headers=headers)
    assert status == 426 and reads == 0


@pytest.mark.parametrize("proxy,edge", [(False, False), (True, False), (False, True)])
def test_missing_runtime_confirmation_never_trusts_headers(proxy, edge):
    status, _result, reads, _observed = request(
        railway_proxy=proxy, railway_edge_only=edge,
        headers=[(b"x-forwarded-proto", b"https"), (b"x-real-ip", b"198.51.100.27")],
    )
    assert status == 426 and reads == 0


@pytest.mark.parametrize("header", [
    b"x-real-ip", b"x-forwarded-for", b"cf-connecting-ip", b"forwarded", b"x-forwarded-host",
])
@pytest.mark.parametrize("value", [
    b"198.51.100.27", b"for=198.51.100.27;proto=https", b"bad,chain",
])
def test_railway_never_restores_client_ip_from_any_header(header, value):
    status, result, _reads, _observed = request(
        headers=[(b"x-forwarded-proto", b"https"), (header, value)]
    )
    assert status == 200 and result["peer"] == "8.8.8.8"


@pytest.mark.parametrize("name", [b"host", b"x-real-ip", b"x-forwarded-proto", b"authorization"])
def test_duplicate_sensitive_headers_case_insensitive_rejected_before_body(name):
    additional = [(name, b"one"), (name.upper(), b"two")]
    status, _result, reads, observed = request(headers=additional)
    assert status == 400 and reads == 0 and observed == []


@pytest.mark.parametrize("host", [
    None, "", "evil.example", HOST + ".evil.example", HOST + "/path", HOST + ":invalid",
    HOST + ":0", HOST + ":65536", HOST + " ", HOST + ":443:443",
])
def test_railway_boundary_requires_exact_host(host):
    status, _result, reads, _observed = request(
        host=host, headers=[(b"x-forwarded-proto", b"https")]
    )
    assert status == 400 and reads == 0


@pytest.mark.parametrize("host", [HOST, HOST.upper(), HOST + ":443"])
def test_standard_host_case_and_numeric_port_are_normalized(host):
    assert request(host=host, headers=[(b"x-forwarded-proto", b"https")])[0] == 200


@pytest.mark.parametrize("path", ["/health/live", "/health/ready"])
def test_railway_health_hostname_allows_only_exact_get(path):
    assert request(host=RAILWAY_HEALTHCHECK_HOST, path=path)[0] == 200
    for method in ("POST", "HEAD", "DELETE"):
        assert request(host=RAILWAY_HEALTHCHECK_HOST, path=path, method=method)[0] == 400


@pytest.mark.parametrize("path", ["/health/other", "/v1/auth/me", "/v1/rpc/files/upload_bytes"])
def test_health_hostname_cannot_address_business_or_unknown_health_routes(path):
    status, _result, reads, observed = request(
        host=RAILWAY_HEALTHCHECK_HOST, path=path, headers=[(b"x-forwarded-proto", b"https")]
    )
    assert status == 400 and reads == 0 and observed == []


def test_health_hostname_unavailable_in_nonrailway_mode():
    assert request(
        host=RAILWAY_HEALTHCHECK_HOST, path="/health/live", scheme="https",
        railway_proxy=False, railway_edge_only=False,
    )[0] == 400


@pytest.mark.parametrize("path,method", [
    ("/health/other", "GET"), ("/health/ready/", "GET"), ("/health/live", "POST"),
    ("/health/live", "HEAD"), ("/v1/auth/me", "GET"),
])
def test_plain_http_health_exemption_is_exact_for_whole_platform(path, method):
    status, _result, reads, observed = request(
        path=path, method=method, railway_proxy=False, railway_edge_only=False
    )
    assert status == 426 and reads == 0 and observed == []


def test_render_scheme_and_client_ip_behavior_preserved():
    status, result, _reads, _observed = request(
        railway_proxy=False, railway_edge_only=False, render_proxy=True, peer="10.1.2.3",
        headers=[(b"x-forwarded-proto", b"https"), (b"cf-connecting-ip", b"1.1.1.1")],
    )
    assert status == 200 and result == {"scheme": "https", "peer": "1.1.1.1"}


def test_health_host_passes_actual_trustedhost_stack_but_cannot_call_business():
    database = SyntheticDatabase()
    settings = PlatformSettings(
        env="test", database_url="sqlite+pysqlite:///:memory:", token_pepper=PEPPER,
        allowed_hosts=[HOST], railway_proxy=True, railway_edge_only=True,
    )
    app = create_app(settings, database=database)
    try:
        with TestClient(app, base_url="http://" + HOST) as client:
            headers = {"Host": RAILWAY_HEALTHCHECK_HOST}
            assert client.get("/health/live", headers=headers).status_code == 200
            assert client.get("/health/ready", headers=headers).status_code == 200
            assert client.get("/v1/auth/me", headers=headers).status_code == 400
            assert client.post("/health/live", headers=headers).status_code == 400
            assert client.get("/v1/auth/me").status_code == 426
            authenticated_path = client.get("/v1/auth/me", headers={"X-Forwarded-Proto": "https"})
            assert authenticated_path.status_code == 401
            assert client.get("/health/live", headers={"Host": "evil.example"}).status_code == 400
    finally:
        database.close()
    assert settings.allowed_hosts == [HOST]


def test_runtime_factory_forwards_flags_without_loading_a_real_profile(
    monkeypatch, railway_environment
):
    monkeypatch.setattr(platform_admin, "load_runtime_payload", lambda _profile: {
        "runtime_database_url": DATABASE_URL, "token_pepper": PEPPER,
    })
    settings = platform_admin.load_runtime_settings(
        railway_proxy=True, railway_edge_only=True, allowed_hosts=[HOST]
    )
    assert settings.railway_proxy and settings.railway_edge_only
    local_test = platform_admin.load_runtime_settings(
        railway_proxy=True, railway_edge_only=True, test_client=True
    )
    assert not local_test.railway_proxy and not local_test.railway_edge_only


def test_entrypoint_keeps_uvicorn_proxy_headers_disabled(monkeypatch, tmp_path):
    calls = []
    certificate = tmp_path / "synthetic-ca.crt"
    monkeypatch.setattr(platform_entrypoint.os, "environ", {
        "PORT": "23456", "PLATFORM_DATABASE_CA_PEM": "synthetic-test-only-certificate",
    })
    monkeypatch.setattr(platform_entrypoint.ssl, "create_default_context", lambda **_kwargs: None)
    monkeypatch.setattr(platform_entrypoint, "Path", lambda _path: certificate)
    monkeypatch.setattr(platform_entrypoint.uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))
    platform_entrypoint.main()
    assert len(calls) == 1
    _args, options = calls[0]
    assert options["proxy_headers"] is False and options["access_log"] is False
    assert options["host"] == "0.0.0.0" and options["port"] == 23456
    assert options["workers"] == 1 and options["limit_concurrency"] == 12
