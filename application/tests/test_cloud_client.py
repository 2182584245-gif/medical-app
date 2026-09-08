from __future__ import annotations

import json
import threading
import time
from uuid import uuid4

import httpx
import pytest

from ollama_chat_app.services.cloud_client import CloudAPIClient, CloudAPIError, validate_base_url

USER = {"id": 3, "username": "synthetic", "username_normalized": "synthetic",
        "created_at": "2026-09-07T00:00:00+00:00", "last_login_at": None,
        "role_code": "member", "account_status": "active", "updated_at": None}
TOKEN = "synthetic-in-memory-bearer-token-123456789"


def login_response():
    return {"access_token": TOKEN, "token_type": "bearer", "expires_at": "2026-09-08T00:00:00Z",
            "user": USER}


@pytest.mark.parametrize("detail,expected", [
    ("offline_base_version_changed", "base_version"),
    ("request_conflict", "idempotency"),
    ("untrusted-private-error", "unknown"),
])
def test_conflict_body_only_exports_fixed_safe_category(make_client, detail, expected):
    client = make_client(lambda request: httpx.Response(409, json={"detail": detail}))
    client._token = TOKEN
    with pytest.raises(CloudAPIError) as caught:
        client.rpc("sync", "apply_queued", [3, {}], {})
    assert caught.value.code == "conflict" and caught.value.conflict_kind == expected
    assert detail not in str(caught.value)


@pytest.fixture
def make_client(monkeypatch):
    monkeypatch.setattr(CloudAPIClient, "_on_gui_thread", staticmethod(lambda: False))
    clients = []

    def create(handler):
        client = CloudAPIClient("https://health.example.com",
                                transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    yield create
    for client in clients:
        client.close()


@pytest.mark.parametrize("value", [
    "http://health.example.com", "https://127.0.0.1", "https://localhost", "https://10.0.0.1",
    "https://host", "https://host.local", "https://u:p@health.example.com",
    "https://health.example.com?secret=value", "https://health.example.com#secret",
    "https://health.example.com/../bad", "https://health.example.com//bad",
    "https://health.example.com/%2f%2fevil", " https://health.example.com",
    "https://health.example.com:99999", "https://health.example.com\\bad",
])
def test_reject_unsafe_base_urls_without_echo(value):
    with pytest.raises(CloudAPIError) as error:
        validate_base_url(value)
    assert value not in str(error.value)


def test_loopback_requires_explicit_development_flag():
    assert validate_base_url("http://127.0.0.1:8000", allow_loopback_http=True).endswith(":8000")
    assert validate_base_url("http://[::1]:8000", allow_loopback_http=True).endswith(":8000")
    with pytest.raises(CloudAPIError):
        validate_base_url("http://192.168.1.2:8000", allow_loopback_http=True)


def test_token_memory_only_login_me_logout_and_password_contract(make_client):
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/v1/auth/login":
            assert "authorization" not in request.headers
            return httpx.Response(200, json=login_response())
        assert request.headers["authorization"] == "Bearer " + TOKEN
        if request.url.path.endswith(("logout", "change-password")):
            return httpx.Response(204)
        return httpx.Response(200, json=USER)

    client = make_client(handler)
    assert client.login("synthetic", "secret-password") == USER
    assert client.is_authenticated
    assert TOKEN not in repr(client)
    assert client.me() == USER
    client.change_password("secret-password", "new-password-123")
    assert json.loads(seen[-1].content) == {
        "current_password": "secret-password", "new_password": "new-password-123",
    }
    assert client.is_authenticated
    client.logout()
    assert not client.is_authenticated
    assert client._http.headers.get("authorization") is None


@pytest.mark.parametrize("status,code", [
    (401, "authentication"), (403, "permission"), (404, "not_found"), (409, "conflict"),
    (422, "validation"), (429, "limited"), (503, "unavailable"), (302, "redirect"),
])
def test_fixed_errors_ignore_server_body_and_never_follow_redirects(make_client, status, code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"detail": "server-secret-password-token"},
                              headers={"Location": "https://evil.example.com/secret"})

    client = make_client(handler)
    with pytest.raises(CloudAPIError) as error:
        client.request("POST", "/v1/auth/login", json={"password": "user-secret"},
                       authenticated=False)
    assert error.value.code == code
    assert "secret" not in str(error.value)
    assert not hasattr(error.value, "request")
    assert not hasattr(error.value, "response")
    assert len(calls) == 1


def test_timeout_no_retry_and_logout_always_clears(make_client):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("login"):
            return httpx.Response(200, json=login_response())
        raise httpx.ReadTimeout("secret-url-query-and-token", request=request)

    client = make_client(handler)
    client.login("synthetic", "synthetic-pass")
    with pytest.raises(CloudAPIError) as error:
        client.logout()
    assert error.value.code == "timeout"
    assert error.value.outcome_uncertain
    assert not client.is_authenticated
    assert len(calls) == 2


@pytest.mark.parametrize("path", ["https://evil.example.com", "//evil.example.com", "/../x",
                                 "/v1/x?token=secret", "/v1/%2f%2fevil", "/v1\\x"])
def test_generic_request_cannot_escape_base(make_client, path):
    def forbidden(_request):
        pytest.fail("Unsafe URL must not reach transport")

    with pytest.raises(CloudAPIError):
        make_client(forbidden).request("GET", path, authenticated=False)


def test_rpc_explicit_id_stays_identical_for_caller_controlled_retry(make_client):
    bodies = []

    def handler(request):
        if request.url.path.endswith("login"):
            return httpx.Response(200, json=login_response())
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"result": 10})

    client = make_client(handler)
    client.login("synthetic", "synthetic-pass")
    operation_id = str(uuid4())
    for _ in range(2):
        assert client.rpc("chat", "create_conversation", [3], {}, request_id=operation_id) == 10
    assert bodies[0] == bodies[1]
    assert bodies[0]["request_id"] == operation_id


def test_pagination_is_bounded_and_does_not_silently_return_partial(make_client):
    calls = []

    def handler(request):
        if request.url.path.endswith("login"):
            return httpx.Response(200, json=login_response())
        calls.append(request)
        return httpx.Response(200, json=[1, 2])

    client = make_client(handler)
    client.login("synthetic", "synthetic-pass")
    with pytest.raises(CloudAPIError, match="上限"):
        client.get_all_pages("/v1/test", page_size=2, max_pages=3)
    assert len(calls) == 3


def test_timeout_tls_and_redirect_settings(make_client):
    client = make_client(lambda _: httpx.Response(204))
    assert client._http.timeout.connect == 10
    assert client._http.timeout.read == 90
    assert client._http.follow_redirects is False
    assert client._http.trust_env is True


def test_gui_bridge_keeps_timer_alive_and_moves_transport_off_gui(qapp, qtbot):
    from PySide6.QtCore import QTimer

    from ollama_chat_app.workers.cloud_bridge import run_cloud_operation

    gui_thread = threading.get_ident()
    worker_threads, ticks, reentrant_errors = [], [], []

    def handler(_request):
        worker_threads.append(threading.get_ident())
        time.sleep(0.08)
        return httpx.Response(200, json={"ok": True})

    def reentrant():
        ticks.append(1)
        try:
            run_cloud_operation(lambda: None)
        except CloudAPIError as error:
            reentrant_errors.append(error.code)

    QTimer.singleShot(10, reentrant)
    transport = httpx.MockTransport(handler)
    with CloudAPIClient("https://health.example.com", transport=transport) as client:
        assert client.request("GET", "/health/ready", authenticated=False) == {"ok": True}
    assert ticks
    assert reentrant_errors == ["limited"]
    assert all(thread != gui_thread for thread in worker_threads)


def test_gui_login_no_cross_thread_lock_deadlock_and_parent_close_guard(qapp, qtbot):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QWidget

    parent = QWidget()
    qtbot.addWidget(parent)
    parent.show()
    parent.activateWindow()
    qapp.processEvents()
    assert qapp.activeWindow() is parent

    def handler(_request):
        time.sleep(0.08)
        return httpx.Response(200, json=login_response())

    QTimer.singleShot(10, parent.close)
    transport = httpx.MockTransport(handler)
    with CloudAPIClient("https://health.example.com", transport=transport) as client:
        assert client.login("synthetic", "synthetic-pass") == USER
        assert parent.isVisible()
    parent.close()
    assert not parent.isVisible()


def test_tls_verification_and_environment_proxy_options_are_explicit(monkeypatch):
    captured = {}
    actual = httpx.Client

    def record(**kwargs):
        captured.update(kwargs)
        return actual(**kwargs)

    monkeypatch.setattr(httpx, "Client", record)
    transport = httpx.MockTransport(lambda _: httpx.Response(204))
    with CloudAPIClient("https://health.example.com", transport=transport):
        pass
    assert captured["verify"] is True
    assert captured["trust_env"] is True
    assert captured["follow_redirects"] is False


def test_invalid_response_and_size_bounds_are_safe(make_client, monkeypatch):
    from ollama_chat_app.services import cloud_client

    client = make_client(lambda _: httpx.Response(200, json={"private": "x" * 100}))
    monkeypatch.setattr(cloud_client, "MAX_RESPONSE_BYTES", 50)
    with pytest.raises(CloudAPIError) as error:
        client.request("GET", "/health/ready", authenticated=False)
    assert error.value.code == "limit"
    assert "private" not in str(error.value)

    monkeypatch.setattr(cloud_client, "MAX_REQUEST_BYTES", 10)
    with pytest.raises(CloudAPIError) as error:
        client.request("POST", "/health/ready", authenticated=False,
                       json={"secret": "this-must-stay-local"})
    assert error.value.code == "validation"


def test_mode_selector_is_explicit_and_cloud_disables_backup_import(qtbot):
    from ollama_chat_app.ui.auth_pages import LoginPage, RegisterPage

    page, register = LoginPage(), RegisterPage()
    qtbot.addWidget(page)
    qtbot.addWidget(register)
    events = []
    page.connection_change_requested.connect(lambda mode, url: events.append((mode, url)))
    page.set_connection_context("cloud", "https://health.example.com")
    assert events == []
    page.connection_apply_button.click()
    assert events == [("cloud", "https://health.example.com")]
    page.set_import_available(False)
    page.set_busy(True)
    page.set_busy(False)
    assert not page.import_button.isEnabled()
    register.set_cloud_mode(True)
    assert register._card.title_label.text() == "注册云端账户"
