from __future__ import annotations

import asyncio
import io
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from platform_test_support import PASSWORD, PEPPER, SyntheticDatabase
from pydantic import ValidationError
from starlette.responses import JSONResponse
from test_platform_rpc import payload

from ollama_chat_app.services.cloud_rpc_codec import decode_rpc
from server.platform_app import PlatformBoundary, create_app
from server.platform_auth import PlatformAuthError
from server.platform_config import PlatformSettings


@pytest.fixture
def context():
    database = SyntheticDatabase()
    settings = PlatformSettings(
        env="test",
        database_url="sqlite+pysqlite:///:memory:",
        token_pepper=PEPPER,
        allowed_hosts=["testserver"],
        require_https=True,
    )
    app = create_app(settings, database=database)
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as client:
        yield database, app, client
    database.close()


def account(client, username="app-synthetic"):
    response = client.post("/v1/auth/register", json={"username": username, "password": PASSWORD})
    assert response.status_code == 201, response.text
    user_id = response.json()["id"]
    login = client.post("/v1/auth/login", json={"username": username, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return user_id, {"Authorization": "Bearer " + login.json()["access_token"]}


def test_end_to_end_registration_chat_multiple_conversations_and_retry(context):
    database, _, client = context
    actor, headers = account(client)
    request = payload(actor, "新的会话")
    first = client.post("/v1/rpc/chat/create_conversation", json=request, headers=headers)
    second = client.post("/v1/rpc/chat/create_conversation", json=request, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    conversation = decode_rpc(first.json()["result"])
    message = client.post(
        "/v1/rpc/chat/begin_message",
        json=payload(actor, "你好", "deepseek", "model", conversation_id=conversation.id),
        headers=headers,
    )
    assert message.status_code == 200, message.text
    assert database.scalar("SELECT COUNT(*) FROM messages") == 2
    listed = client.post("/v1/rpc/chat/list_conversations", json=payload(actor), headers=headers)
    assert len(decode_rpc(listed.json()["result"])) == 2
    assert listed.headers["cache-control"] == "no-store"
    assert listed.headers["x-content-type-options"] == "nosniff"


def test_invalid_or_revoked_session_keeps_401_not_422(context):
    _, _, client = context
    actor, headers = account(client)
    invalid = client.post(
        "/v1/rpc/health/get_profile",
        json=payload(actor),
        headers={"Authorization": "Bearer " + "A" * 43},
    )
    assert invalid.status_code == 401
    assert invalid.headers["www-authenticate"] == "Bearer"
    assert client.post("/v1/auth/logout", headers=headers).status_code == 204
    revoked = client.post("/v1/rpc/health/get_profile", json=payload(actor), headers=headers)
    assert revoked.status_code == 401


@pytest.mark.parametrize("status", [401, 429, 503])
def test_auth_failure_retains_authoritative_status(context, monkeypatch, status):
    _, app, client = context

    def rejected(_token):
        raise PlatformAuthError(status, "安全错误")

    monkeypatch.setattr(app.state.platform_auth, "resolve_token", rejected)
    response = client.post(
        "/v1/rpc/chat/list_conversations",
        json=payload(1),
        headers={"Authorization": "Bearer " + "A" * 43},
    )
    assert response.status_code == status
    if status == 429:
        assert response.headers["retry-after"] == "60"


def test_resource_isolation_and_disabled_account_after_login(context):
    database, _, client = context
    actor, headers = account(client)
    other, _ = account(client, "another-account")
    forged = client.post("/v1/rpc/health/get_profile", json=payload(other), headers=headers)
    assert forged.status_code == 403
    own = client.post("/v1/rpc/health/get_profile", json=payload(actor), headers=headers)
    assert own.status_code == 200
    database.connection.execute("UPDATE users SET account_status='disabled' WHERE id=?", (actor,))
    blocked = client.post("/v1/rpc/health/get_profile", json=payload(actor), headers=headers)
    assert blocked.status_code == 401


def test_health_rechecks_shape_and_returns_safe_failure(context, monkeypatch):
    database, _, client = context
    assert client.get("/health/ready").status_code == 200
    assert database.initialize_calls[-1] is True

    def fail(*, force=False):
        assert force is True
        raise RuntimeError("SECRET database path and password")

    monkeypatch.setattr(database, "initialize", fail)
    result = client.get("/health/ready")
    assert result.status_code == 503
    assert result.json() == {"status": "not_ready"}
    assert "SECRET" not in result.text
    assert client.get("/health/live").status_code == 200


def test_validation_private_errors_and_disabled_api_docs(context):
    _, _, client = context
    response = client.post(
        "/v1/auth/register",
        json={"username": "safe", "password": PASSWORD, "role_code": "operator"},
    )
    assert response.status_code == 422 and PASSWORD not in response.text
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
    assert client.get("/health/live", headers={"Host": "untrusted.example"}).status_code == 400


def test_business_error_never_echoes_raw_database_or_input(context, monkeypatch):
    _, app, client = context
    actor, headers = account(client)

    def fail(_token):
        raise sqlite3.OperationalError("SECRET db://user:password@host private.sql")

    monkeypatch.setattr(app.state.platform_auth, "resolve_token", fail)
    response = client.post("/v1/rpc/chat/list_conversations", json=payload(actor), headers=headers)
    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}
    assert "SECRET" not in response.text


def test_upload_checks_real_bearer_before_reading_large_body(context):
    _, _, client = context
    response = client.post(
        "/v1/rpc/files/upload_bytes",
        content=b"do not parse",
        headers={"Authorization": "Bearer " + "A" * 43, "Content-Length": str(30 * 1024 * 1024)},
    )
    assert response.status_code == 401


def test_real_file_upload_and_download_for_owner(context):
    _, _, client = context
    actor, headers = account(client)
    output = io.BytesIO()
    Image.new("RGB", (2, 2), color="white").save(output, format="PNG")
    content = output.getvalue()
    upload = client.post(
        "/v1/rpc/files/upload_bytes", json=payload(actor, "synthetic.png", content), headers=headers
    )
    assert upload.status_code == 200, upload.text
    file_id = decode_rpc(upload.json()["result"])
    downloaded = client.post(
        "/v1/rpc/files/get_file", json=payload(actor, file_id), headers=headers
    )
    assert downloaded.status_code == 200, downloaded.text
    assert content in decode_rpc(downloaded.json()["result"]).values()


def test_upload_revalidates_session_after_body_arrives(context, monkeypatch):
    database, app, client = context
    actor, headers = account(client)
    original = app.state.platform_auth.resolve_token
    calls = []

    def expires_during_upload(token):
        calls.append(True)
        if len(calls) == 1:
            return original(token)
        raise PlatformAuthError(401, "需要重新登录")

    monkeypatch.setattr(app.state.platform_auth, "resolve_token", expires_during_upload)
    result = client.post(
        "/v1/rpc/files/upload_bytes",
        json=payload(actor, "synthetic.pdf", b"%PDF-1.4\n%%EOF"),
        headers=headers,
    )
    assert result.status_code == 401
    assert len(calls) == 2
    assert database.scalar("SELECT COUNT(*) FROM user_files") == 0


async def _boundary_request(
    *,
    headers=(),
    peer="8.8.8.8",
    scheme="https",
    path="/v1/rpc/health/get_profile",
    render_proxy=False,
    chunks=(b"{}",),
    auth=None,
    timeout=30,
    busy=False,
    delay=0,
):
    observed, sent = [], []
    reads = 0
    messages = [
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1}
        for i, chunk in enumerate(chunks)
    ]

    async def app(scope, receive, send):
        observed.append(scope)
        await JSONResponse({"scheme": scope["scheme"], "client": scope["client"][0]})(
            scope, receive, send
        )

    async def receive():
        nonlocal reads
        reads += 1
        if delay:
            await asyncio.sleep(delay)
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    settings = SimpleNamespace(
        require_https=True,
        render_proxy=render_proxy,
        max_body_bytes=1024 * 1024,
        body_timeout_seconds=timeout,
    )
    boundary = PlatformBoundary(app, settings=settings, auth=auth)
    if busy:
        await boundary._large.acquire()
    await boundary(
        {
            "type": "http",
            "scheme": scheme,
            "path": path,
            "client": (peer, 1234),
            "headers": list(headers),
        },
        receive,
        send,
    )
    return sent[0]["status"], json.loads(sent[-1]["body"]), reads, observed


def boundary_request(**kwargs):
    return asyncio.run(_boundary_request(**kwargs))


@pytest.mark.parametrize("proxy,peer", [(False, "10.1.2.3"), (True, "8.8.8.8"), (True, "0.0.0.0")])
def test_proxy_headers_cannot_upgrade_untrusted_http(proxy, peer):
    status, _, reads, _ = boundary_request(
        render_proxy=proxy,
        peer=peer,
        scheme="http",
        headers=[(b"x-forwarded-proto", b"https"), (b"cf-connecting-ip", b"1.1.1.1")],
    )
    assert status == 426 and reads == 0


@pytest.mark.parametrize("cf", [None, b"not-an-ip", b"1.1.1.1"])
def test_verified_render_scheme_is_independent_of_optional_client_ip(cf):
    headers = [(b"x-forwarded-proto", b"https"), (b"x-forwarded-for", b"spoofed-client, 1.1.1.1")]
    if cf is not None:
        headers.append((b"cf-connecting-ip", cf))
    status, body, _, _ = boundary_request(
        render_proxy=True, peer="10.1.2.3", scheme="http", headers=headers
    )
    assert status == 200 and body["scheme"] == "https"
    assert body["client"] == ("1.1.1.1" if cf == b"1.1.1.1" else "10.1.2.3")


def test_duplicate_auth_or_forwarding_headers_rejected():
    for name in (b"authorization", b"content-length", b"x-forwarded-proto", b"cf-connecting-ip"):
        status, _, reads, _ = boundary_request(headers=[(name, b"one"), (name, b"two")])
        assert status == 400 and reads == 0


@pytest.mark.parametrize("declared,status", [(b"-1", 413), (b"999999999", 413), (b"abc", 400)])
def test_declared_request_limits_are_checked_before_buffering(declared, status):
    actual, _, reads, _ = boundary_request(headers=[(b"content-length", declared)])
    assert actual == status and reads == 0


def test_chunked_actual_size_cannot_bypass_content_length():
    status, _, reads, _ = boundary_request(
        headers=[(b"content-length", b"1")], chunks=(b"a" * 600000, b"b" * 600000)
    )
    assert status == 413 and reads == 2


def test_slow_body_is_bounded_before_json_parser_runs():
    status, _, reads, observed = boundary_request(timeout=0.01, delay=0.1)
    assert status == 408 and reads == 1 and observed == []


def test_large_upload_requires_real_auth_and_capacity_before_buffering():
    class Reject:
        def resolve_token(self, _token):
            raise PlatformAuthError(401, "invalid")

    options = {
        "path": "/v1/rpc/files/upload_bytes",
        "headers": [(b"authorization", b"Bearer " + b"A" * 43)],
    }
    status, _, reads, _ = boundary_request(auth=Reject(), **options)
    assert status == 401 and reads == 0
    valid = SimpleNamespace(resolve_token=lambda _token: object())
    status, _, reads, _ = boundary_request(auth=valid, busy=True, **options)
    assert status == 429 and reads == 0
    status, _, reads, _ = boundary_request(
        auth=valid,
        **{
            **options,
            "headers": options["headers"]
            + [(b"content-length", str(32 * 1024 * 1024 + 1).encode())],
        },
    )
    assert status == 413 and reads == 0


def test_production_config_rejects_impostor_runtime_bad_host_or_unbounded_body(monkeypatch):
    kwargs = dict(
        env="production",
        token_pepper=PEPPER,
        allowed_hosts=["synthetic.onrender.com"],
        database_url="postgresql+psycopg://medical_app_platform_runtime:"
        + "a" * 40
        + "@example.invalid/db?sslmode=verify-full&sslrootcert=synthetic.crt",
    )
    assert PlatformSettings(**kwargs).production
    for field, value in (
        ("allowed_hosts", [""]),
        ("allowed_hosts", ["a..onrender.com"]),
        ("require_https", False),
        ("max_body_bytes", 10**10),
        ("body_timeout_seconds", 0),
        (
            "database_url",
            str(kwargs["database_url"]).replace(
                "medical_app_platform_runtime:", "medical_app_platform_runtime_evil:"
            ),
        ),
    ):
        with pytest.raises(ValidationError) as error:
            PlatformSettings(**{**kwargs, field: value})
        assert "a" * 40 not in str(error.value)
    monkeypatch.delenv("RENDER_SERVICE_ID", raising=False)
    with pytest.raises(ValidationError):
        PlatformSettings(**kwargs, render_proxy=True)
