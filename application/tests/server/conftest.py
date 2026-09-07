from __future__ import annotations

import secrets

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.config import Settings
from server.models import Base


@pytest.fixture
def settings(tmp_path):
    return Settings(
        env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'pilot.db'}",
        token_pepper=secrets.token_hex(32),
        allowed_hosts=["testserver"],
        require_https=False,
    )


@pytest.fixture
def app(settings):
    application = create_app(settings)
    # Tests alone prepare this synthetic schema. Application startup never does so.
    Base.metadata.create_all(application.state.database.engine)
    yield application
    application.state.database.engine.dispose()


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def _account(client, username="synthetic-member", password="  synthetic-password-123  "):
    registration = client.post("/auth/register", json={"username": username, "password": password})
    assert registration.status_code == 201, registration.text
    login = client.post("/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200, login.text
    return registration.json(), {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest.fixture
def account():
    return _account
