from __future__ import annotations

import asyncio

import pytest

from server.app import RequestBoundary
from server.config import Settings
from server.security import AuthCapacityError, AuthRateLimiter


def test_client_global_and_storage_limits_are_bounded_without_sleeping():
    clock = [100.0]
    limiter = AuthRateLimiter(2, 3, max_clients=2, clock=lambda: clock[0])
    limiter.check("first")
    limiter.check("first")
    with pytest.raises(AuthCapacityError):
        limiter.check("first")
    limiter.check("second")
    with pytest.raises(AuthCapacityError):
        limiter.check("second")
    assert len(limiter._global) == 3
    assert len(limiter._clients) == 2
    clock[0] += 60
    limiter.check("third")
    assert len(limiter._clients) == 1
    full = AuthRateLimiter(2, 100, max_clients=2, clock=lambda: clock[0])
    full.check("first")
    full.check("second")
    with pytest.raises(AuthCapacityError):
        full.check("new-address")
    assert set(full._clients) == {"first", "second"}


def test_auth_limiter_rejects_before_hashing_and_ignores_spoofed_forwarding_headers(
    client, app, monkeypatch
):
    app.state.auth_limiter = AuthRateLimiter(1, 10)
    called = []

    def reject_password(*args):
        called.append(args)
        return False

    monkeypatch.setattr(app.state.security, "verify_password", reject_password)
    payload = {"username": "synthetic-missing", "password": "synthetic-password"}
    assert client.post("/auth/login", json=payload).status_code == 401
    blocked = client.post("/auth/login", json=payload, headers={"X-Forwarded-For": "203.0.113.1"})
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "60"
    assert len(called) == 1
    assert client.post("/auth/register", json=payload).status_code == 429


def test_argon2_semaphore_rejects_excess_work_and_releases_after_failure(app):
    security = app.state.security
    with security.hash_slot(), security.hash_slot(), pytest.raises(AuthCapacityError):
        security.hash_password("synthetic-password")
    with pytest.raises(RuntimeError), security.hash_slot():
        raise RuntimeError("simulated hasher failure")
    assert security.verify_password("incorrect-synthetic-password", security.dummy_hash) is False


def test_slow_body_is_stopped_before_entering_the_application(settings):
    settings = Settings(**{**settings.model_dump(), "body_timeout_seconds": 0.05})
    sent = []

    async def unexpected_app(*_args):
        raise AssertionError("an incomplete timed-out body must not reach application code")

    async def receive():
        await asyncio.sleep(1)
        return {"type": "http.request", "body": b"", "more_body": True}

    async def send(message):
        sent.append(message)

    boundary = RequestBoundary(unexpected_app, settings=settings)
    asyncio.run(
        boundary(
            {"type": "http", "scheme": "http", "path": "/auth/login", "headers": []},
            receive,
            send,
        )
    )
    assert sent[0]["status"] == 408
