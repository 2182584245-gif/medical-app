"""Synthetic-only authenticated AI proxy; upstream never accesses the network."""

import io
import json
import os
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from platform_test_support import PASSWORD, PEPPER, SyntheticDatabase

from server.platform_ai_proxy import (
    ENVELOPE_AAD,
    ENVELOPE_MAGIC,
    AIProxyError,
    AIProxySettings,
    AIQuota,
    AIRequest,
    load_encrypted_key,
    request_payload,
)
from server.platform_app import create_app
from server.platform_auth import PlatformAuth
from server.platform_config import PlatformSettings

MODEL = "deepseek-v4-flash"
SYNTHETIC_KEY = "synthetic-server-ai-never-real-123456"


def settings():
    return PlatformSettings(
        env="test",
        database_url="sqlite+pysqlite:///:memory:",
        token_pepper=PEPPER,
        allowed_hosts=["testserver"],
        require_https=True,
    )


@pytest.fixture
def context():
    database, calls = SyntheticDatabase(), []

    def upstream(request):
        calls.append(request)
        payload = json.loads(request.content)
        if payload.get("stream"):
            events = [
                {
                    "model": payload["model"],
                    "choices": [{"delta": {"content": "合成回答"}, "finish_reason": None}],
                },
                {"model": payload["model"], "choices": [{"delta": {}, "finish_reason": "stop"}]},
            ]
            data = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
            return httpx.Response(
                200, text=data + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(
            200,
            json={
                "model": payload["model"],
                "choices": [{"message": {"content": "合成回答"}, "finish_reason": "stop"}],
            },
        )

    app = create_app(
        settings(),
        database=database,
        ai_transport=httpx.MockTransport(upstream),
        ai_proxy_settings=AIProxySettings(envelope_path="synthetic", key_path="synthetic-key"),
    )
    app.state.ai_proxy.key_loader = lambda *_: SYNTHETIC_KEY
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as client:
        client.post("/v1/auth/register", json={"username": "proxy-synthetic", "password": PASSWORD})
        login = client.post(
            "/v1/auth/login", json={"username": "proxy-synthetic", "password": PASSWORD}
        )
        token = login.json()["access_token"]
        yield database, app, client, {"Authorization": "Bearer " + token}, calls
    database.close()


def body():
    return {"model": MODEL, "messages": [{"role": "user", "content": "合成普通问题"}]}


def test_complete_authenticated_fixed_target_private_key_and_no_secret_response(context):
    db, app, client, headers, calls = context
    result = client.post("/v1/ai/chat", json=body(), headers=headers)
    assert result.status_code == 200
    assert result.json() == {"content": "合成回答", "model": MODEL}
    assert result.headers["cache-control"] == "no-store"
    assert SYNTHETIC_KEY not in result.text
    assert len(calls) == 1 and str(calls[0].url) == "https://api.deepseek.com/chat/completions"
    assert calls[0].headers["authorization"] == "Bearer " + SYNTHETIC_KEY
    assert calls[0].headers["authorization"] != headers["Authorization"]
    payload = json.loads(calls[0].content)
    assert payload["max_tokens"] == 2048 and "诊断" in payload["messages"][0]["content"]
    assert len(payload["user_id"]) == 64 and "proxy-synthetic" not in payload["user_id"]
    assert db.scalar("SELECT COUNT(*) FROM life_records") == 0


def test_stream_real_route_returns_deltas_and_completion(context):
    _, _, client, headers, calls = context
    result = client.post("/v1/ai/chat/stream", json=body(), headers=headers)
    assert result.status_code == 200
    assert '"delta":"合成回答"' in result.text and '"done":true' in result.text
    assert SYNTHETIC_KEY not in result.text and len(calls) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        lambda b: b.update(api_key="synthetic-client-key"),
        lambda b: b.update(user_id=999),
        lambda b: b.update(url="https://evil.invalid"),
        lambda b: b.update(model="arbitrary-expensive-model"),
        lambda b: b.update(max_tokens=2049),
        lambda b: b["messages"][0].update(role="tool"),
        lambda b: b["messages"][0].update(role=[]),
        lambda b: b["messages"][0].update(role={"unexpected": "shape"}),
        lambda b: b["messages"][0].update(content="x" * 32001),
        lambda b: b["messages"][0].update(
            content=[{"type": "image_url", "image_url": {"url": "https://evil.invalid/secret"}}]
        ),
    ],
)
def test_untrusted_routing_and_oversize_content_rejected_before_upstream(context, mutation):
    _, _, client, headers, calls = context
    payload = body()
    mutation(payload)
    response = client.post("/v1/ai/chat", json=payload, headers=headers)
    assert response.status_code in {413, 422} and calls == []
    assert "synthetic-client-key" not in response.text


def test_revoked_disabled_and_missing_sessions_never_call_upstream(context):
    db, _, client, headers, calls = context
    assert client.post("/v1/ai/chat", json=body()).status_code == 401
    with db.transaction() as connection:
        connection.execute("UPDATE users SET account_status='disabled'")
    assert client.post("/v1/ai/chat", json=body(), headers=headers).status_code == 401
    assert calls == []


def test_upstream_error_is_sanitized_and_not_retried(context):
    _, app, client, headers, calls = context

    def rejected(request):
        calls.append(request)
        return httpx.Response(401, text=SYNTHETIC_KEY + " private upstream error")

    app.state.ai_proxy.transport = httpx.MockTransport(rejected)
    result = client.post("/v1/ai/chat", json=body(), headers=headers)
    assert result.status_code == 502 and len(calls) == 1
    assert SYNTHETIC_KEY not in result.text and "private upstream" not in result.text
    # Failed upstream attempts still consumed quota; there is no free auto retry.
    client.post("/v1/ai/chat", json=body(), headers=headers)
    assert client.post("/v1/ai/chat", json=body(), headers=headers).status_code == 429
    assert len(calls) == 2


def test_proxy_not_configured_fails_without_charging_or_network(context):
    db, app, client, headers, calls = context
    app.state.ai_proxy.settings = AIProxySettings()
    result = client.post("/v1/ai/chat", json=body(), headers=headers)
    assert result.status_code == 503 and not calls
    assert db.scalar("SELECT COUNT(*) FROM auth_rate_buckets WHERE bucket_id >= 8191") == 0


def test_aes_envelope_authentication_and_separate_private_key(tmp_path):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key, nonce = bytes(range(32)), bytes(range(12))
    encrypted = (
        ENVELOPE_MAGIC + nonce + AESGCM(key).encrypt(nonce, SYNTHETIC_KEY.encode(), ENVELOPE_AAD)
    )
    envelope, key_file = tmp_path / "envelope.bin", tmp_path / "key.bin"
    envelope.write_bytes(encrypted)
    key_file.write_bytes(key)
    envelope.chmod(0o600)
    key_file.chmod(0o600)
    assert SYNTHETIC_KEY.encode() not in envelope.read_bytes()
    assert load_encrypted_key(str(envelope), str(key_file)) == SYNTHETIC_KEY
    envelope.write_bytes(encrypted[:-1] + bytes([encrypted[-1] ^ 1]))
    with pytest.raises(AIProxyError) as error:
        load_encrypted_key(str(envelope), str(key_file))
    assert SYNTHETIC_KEY not in str(error.value)


@pytest.fixture
def quota():
    database = SyntheticDatabase()
    auth = PlatformAuth(database, settings())
    moment = [86400 * 20000 + 3600]
    limiter = AIQuota(auth, AIProxySettings(), clock=lambda: moment[0])
    yield database, limiter, moment
    database.close()


def test_ai_domains_are_disjoint_from_auth_and_each_other(quota):
    _, limiter, _ = quota
    from server.platform_security import SharedAuthLimiter

    auth = SharedAuthLimiter(limiter.auth.database, settings())
    for identifier in range(15000):
        assert max(auth.bucket_ids(str(identifier), str(identifier))) <= 8190
        values = limiter.buckets(identifier)
        assert [item[0] for item in values[:2]] == [8191, 8192]
        assert 8193 <= values[2][0] <= 12288 and 12289 <= values[3][0] <= 16384


def test_concurrent_replicas_share_minute_quota_and_rejections_do_not_burn_global(quota):
    database, limiter, moment = quota
    other = AIQuota(limiter.auth, AIProxySettings(), clock=lambda: moment[0])

    def reserve(index):
        try:
            (limiter if index % 2 else other).reserve(1)
            return True
        except AIProxyError as error:
            assert error.status == 429
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(30))) == 2
    assert database.scalar("SELECT attempts FROM auth_rate_buckets WHERE bucket_id=8191") == 2


def test_global_day_cannot_be_bypassed_with_multiple_users_and_restarts(quota):
    database, limiter, moment = quota
    for index in range(100):
        moment[0] += 61
        AIQuota(limiter.auth, AIProxySettings(), clock=lambda: moment[0]).reserve(index + 1)
    with pytest.raises(AIProxyError):
        limiter.reserve(500)
    assert database.scalar("SELECT attempts FROM auth_rate_buckets WHERE bucket_id=8191") == 100
    moment[0] = (moment[0] // 86400 + 1) * 86400
    limiter.reserve(500)
    assert database.scalar("SELECT attempts FROM auth_rate_buckets WHERE bucket_id=8191") == 1
    moment[0] -= 86400
    with pytest.raises(AIProxyError):
        limiter.reserve(501)


def test_user_daily_limit_survives_minute_windows(quota):
    _, limiter, moment = quota
    for _ in range(20):
        limiter.reserve(42)
        moment[0] += 61
    with pytest.raises(AIProxyError):
        limiter.reserve(42)


def test_legacy_auth_daily_slot_and_cleanup_never_reset_ai_quota(quota):
    database, limiter, moment = quota
    with database.transaction() as connection:
        connection.execute("INSERT INTO auth_rate_buckets VALUES (8191,?,7)", (moment[0] + 60,))
    limiter.reserve(1)  # Legacy minute slot is not a future AI midnight window.
    day_before = database.scalar("SELECT attempts FROM auth_rate_buckets WHERE bucket_id=8191")
    moment[0] += 600
    from server.platform_security import SharedAuthLimiter

    SharedAuthLimiter(database, settings(), clock=lambda: moment[0]).check("synthetic", "account")
    assert (
        database.scalar("SELECT attempts FROM auth_rate_buckets WHERE bucket_id=8191") == day_before
    )


def test_vision_requires_exact_model_and_valid_local_image():
    import base64

    stream = io.BytesIO()
    Image.new("RGB", (3, 3), "red").save(stream, format="PNG")
    payload = body()
    payload["messages"][0]["content"] = [
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()
            },
        }
    ]
    with pytest.raises(AIProxyError):
        request_payload(AIRequest(**payload))
    payload["model"] = "deepseek-v4-flash-vision-exp"
    assert request_payload(AIRequest(**payload))["model"] == payload["model"]


def test_quota_environment_can_reduce_but_never_raise_safety_ceiling(monkeypatch):
    monkeypatch.setenv("HEALTHLIFE_AI_GLOBAL_DAY_LIMIT", "50")
    assert AIProxySettings.from_environment().global_day == 50
    for bad in ("0", "101", "-1", "unlimited"):
        monkeypatch.setenv("HEALTHLIFE_AI_GLOBAL_DAY_LIMIT", bad)
        with pytest.raises(ValueError):
            AIProxySettings.from_environment()
    with pytest.raises(ValueError):
        AIProxySettings(user_day=True)


@pytest.mark.parametrize(
    "failure", ["missing_done", "wrong_model", "length", "redirect", "content_type", "long_line"]
)
def test_stream_never_claims_completion_for_truncated_wrong_model_or_redirect(context, failure):
    _, app, client, headers, calls = context

    def broken(request):
        calls.append(request)
        if failure == "redirect":
            return httpx.Response(307, headers={"location": "https://evil.invalid/key"})
        chunk = {
            "model": "not-requested" if failure == "wrong_model" else MODEL,
            "choices": [
                {
                    "delta": {"content": "模拟"},
                    "finish_reason": "length" if failure == "length" else "stop",
                }
            ],
        }
        value = "data: " + json.dumps(chunk) + "\n\n"
        if failure == "long_line":
            value = ":" + "x" * 65537 + "\n" + value
        if failure != "missing_done":
            value += "data: [DONE]\n\n"
        return httpx.Response(
            200,
            text=value,
            headers={
                "content-type": "text/plain" if failure == "content_type" else "text/event-stream"
            },
        )

    app.state.ai_proxy.transport = httpx.MockTransport(broken)
    result = client.post("/v1/ai/chat/stream", json=body(), headers=headers)
    assert result.status_code == 200
    assert '"error":"default_ai_incomplete"' in result.text and '"done":true' not in result.text
    assert len(calls) == 1 and SYNTHETIC_KEY not in result.text


def test_revocation_during_upstream_completion_hides_result(context):
    database, app, client, headers, calls = context

    def revoked(request):
        calls.append(request)
        with database.transaction() as connection:
            connection.execute("UPDATE platform_sessions SET revoked_at=created_at")
        return httpx.Response(
            200,
            json={
                "model": MODEL,
                "choices": [
                    {"message": {"content": "不应返回的合成资料"}, "finish_reason": "stop"}
                ],
            },
        )

    app.state.ai_proxy.transport = httpx.MockTransport(revoked)
    result = client.post("/v1/ai/chat", json=body(), headers=headers)
    assert result.status_code == 401 and "不应返回" not in result.text and len(calls) == 1


def test_json_bom_bad_shape_and_key_file_aliases_fail_closed(context, tmp_path):
    _, app, client, headers, calls = context
    app.state.ai_proxy.transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text='{"choices":[]}')
    )
    result = client.post("/v1/ai/chat", json=body(), headers=headers)
    assert result.status_code == 502 and SYNTHETIC_KEY not in result.text
    with pytest.raises(AIProxyError):
        load_encrypted_key(str(tmp_path / "same"), str(tmp_path / "same"))
    with pytest.raises(AIProxyError):
        load_encrypted_key("relative", "also-relative")


@pytest.mark.parametrize("stream", [False, True])
def test_body_size_limit_authenticates_before_buffering_and_never_charges(context, stream):
    database, _app, client, headers, calls = context
    path = "/v1/ai/chat/stream" if stream else "/v1/ai/chat"
    huge = "x" * (4 * 1024 * 1024 + 1)
    assert client.post(path, content=huge).status_code == 401
    assert client.post(path, content=huge, headers=headers).status_code == 413
    assert calls == []
    assert database.scalar("SELECT count(*) FROM auth_rate_buckets WHERE bucket_id>=8191") == 0


@pytest.mark.parametrize("stage", ["before", "during"])
def test_expired_session_never_finishes_stream(context, stage):
    database, app, client, headers, calls = context

    def expire():
        with database.transaction() as connection:
            connection.execute(
                "UPDATE platform_sessions SET expires_at='2000-01-01T00:00:00+00:00'"
            )

    original = app.state.ai_proxy.transport

    def upstream(request):
        expire()
        return original.handler(request)

    if stage == "before":
        expire()
    else:
        app.state.ai_proxy.transport = httpx.MockTransport(upstream)
    response = client.post("/v1/ai/chat/stream", json=body(), headers=headers)
    if stage == "before":
        assert response.status_code == 401 and not calls
    else:
        assert response.status_code == 200 and len(calls) == 1
        assert '"delta"' not in response.text
        assert (
            '"done":true' not in response.text
            and '"error":"default_ai_incomplete"' in response.text
        )


def test_key_loader_rejects_hardlinks_and_permissions_without_disclosing_contents(
    tmp_path, monkeypatch
):
    import server.platform_ai_proxy as module

    path, linked = tmp_path / "private", tmp_path / "linked"
    path.write_bytes(b"synthetic-do-not-echo")
    os.link(path, linked)
    with pytest.raises(AIProxyError):
        module._private_file(str(path), 100)
    linked.unlink()
    real_info = path.stat()
    proxy_os = SimpleNamespace(**{name: getattr(os, name) for name in dir(os)})
    proxy_os.name = "posix"
    proxy_os.geteuid = lambda: real_info.st_uid
    monkeypatch.setattr(module, "os", proxy_os)
    original_stat = module.Path.stat

    def unsafe_mode(candidate, **kwargs):
        info = original_stat(candidate, **kwargs)
        if candidate != path or not kwargs.get("follow_symlinks", True):
            return info
        values = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
        values["st_mode"] = (info.st_mode & ~0o777) | 0o604
        return SimpleNamespace(**values)

    monkeypatch.setattr(module.Path, "stat", unsafe_mode)
    with pytest.raises(AIProxyError):
        module._private_file(str(path), 100)
