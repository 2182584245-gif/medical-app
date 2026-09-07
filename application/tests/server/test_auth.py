from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from server.models import LoginSession, User, utc_now


def test_registration_login_password_bytes_and_hashed_revocable_tokens(client, app, account):
    password = "  synthetic-password-123  "
    user, headers = account(client, "  Ｍｅｍｂｅｒ  ", password)
    assert UUID(user["id"])
    assert user["username"] == "Member"
    assert user["created_at"].endswith("Z")
    assert set(user) == {"id", "username", "created_at"}
    token = headers["Authorization"].split()[1]
    assert len(token) == 43
    with app.state.database.sessions() as db:
        stored = db.scalar(select(User))
        session = db.scalar(select(LoginSession))
        assert stored.password_hash.startswith("$argon2id$")
        assert 90 <= len(stored.password_hash) <= 512
        assert session.token_digest == app.state.security.token_digest(token)
        assert session.token_digest != token
        assert session.expires_at.utcoffset().total_seconds() == 0
    response = client.get("/auth/me", headers=headers)
    assert response.status_code == 200
    assert response.json() == user
    assert response.headers["cache-control"] == "no-store"
    failed = client.post("/auth/login", json={"username": "member", "password": password.strip()})
    assert failed.status_code == 401
    successful = client.post("/auth/login", json={"username": "member", "password": password})
    assert successful.status_code == 200
    assert successful.json()["access_token"] != token
    assert client.post("/auth/logout", headers=headers).status_code == 204
    assert client.get("/auth/me", headers=headers).status_code == 401
    second_header = {"Authorization": f"Bearer {successful.json()['access_token']}"}
    assert client.get("/auth/me", headers=second_header).status_code == 200
    database_bytes = app.state.database.engine.url.database
    assert token.encode() not in Path(database_bytes).read_bytes()
    assert password.encode() not in Path(database_bytes).read_bytes()


def test_unknown_account_uses_dummy_hash_and_has_same_401_as_wrong_password(
    client, app, monkeypatch, account
):
    account(client)
    verified_hashes = []
    verify = app.state.security.verify_password

    def track(password, digest):
        verified_hashes.append(digest)
        return verify(password, digest)

    monkeypatch.setattr(app.state.security, "verify_password", track)
    unknown = client.post(
        "/auth/login", json={"username": "missing-member", "password": "bad-pass-1"}
    )
    wrong = client.post(
        "/auth/login", json={"username": "synthetic-member", "password": "bad-pass-1"}
    )
    missing = client.get("/auth/me")
    assert unknown.status_code == wrong.status_code == missing.status_code == 401
    assert unknown.json() == wrong.json() == missing.json()
    assert unknown.headers["www-authenticate"] == "Bearer"
    assert verified_hashes[0] == app.state.security.dummy_hash
    assert verified_hashes[1] != app.state.security.dummy_hash


@pytest.mark.parametrize("invalid_state", ["expired", "revoked", "disabled"])
def test_expired_revoked_or_disabled_sessions_are_unauthorized(client, app, invalid_state, account):
    _user, headers = account(client)
    with app.state.database.sessions.begin() as db:
        session = db.scalar(select(LoginSession))
        if invalid_state == "expired":
            session.expires_at = utc_now() - timedelta(seconds=1)
        elif invalid_state == "revoked":
            session.revoked_at = utc_now()
        else:
            db.scalar(select(User)).active = False
    response = client.get("/auth/me", headers=headers)
    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required"}


@pytest.mark.parametrize("authorization", ["Basic abc", "Bearer bad", "Bearer " + "x" * 1000])
def test_malformed_authorization_has_uniform_401(client, authorization):
    response = client.get("/auth/me", headers={"Authorization": authorization})
    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required"}


def test_normalized_username_is_unique_and_validation_never_echoes_secrets(client, account):
    account(client, "Member")
    duplicate = client.post(
        "/auth/register", json={"username": "ＭＥＭＢＥＲ", "password": "secret-pass"}
    )
    assert duplicate.status_code == 409
    secret = "not-an-accepted-api-key"
    response = client.post(
        "/auth/register",
        json={
            "username": "other-member",
            "password": "x" * 257,
            "api_key": secret,
        },
    )
    assert response.status_code == 422
    assert secret not in response.text
    assert "x" * 257 not in response.text
