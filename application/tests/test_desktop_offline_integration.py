"""Synthetic Qt + actual desktop wiring; never contacts a real service or account."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from PySide6.QtWidgets import QInputDialog, QMessageBox

from ollama_chat_app.data.database import Database
from ollama_chat_app.endpoint_settings import EndpointSettings, EndpointSettingsStore
from ollama_chat_app.main import build_desktop_window
from ollama_chat_app.services.cloud_client import CloudAPIClient
from ollama_chat_app.services.offline_access import OfflineAccessStore
from ollama_chat_app.services.offline_outbox import QueuedOperation
from ollama_chat_app.time_utils import display_timezone_name, set_display_timezone
from ollama_chat_app.ui.offline_feedback import QUEUED_MESSAGE

ENDPOINT = "https://synthetic.example.com/aliyun"
PASSWORD = "Synthetic-Offline-Integration-2026!"
USER = {
    "id": 1, "username": "synthetic-member", "username_normalized": "synthetic-member",
    "created_at": "2026-09-08T00:00:00Z", "last_login_at": None, "role_code": "member",
    "account_status": "active", "updated_at": None,
}


def _lease():
    now = datetime.now(UTC)
    return {"version": 1, "actor_id": 1, "role_code": "member",
            "server_instance_id": "synthetic-offline-instance-0001", "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=2)).isoformat()}


def _forbidden(*_args, **_kwargs):
    pytest.fail("This integration branch must not read/refresh cloud business data")


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "private-app-data"))
    monkeypatch.setenv("OLLAMA_DUAL_CHAT_DATA_DIR", str(tmp_path / "portable-data"))
    monkeypatch.delenv("HEALTHLIFE_CLOUD_BASE_URL", raising=False)
    previous_timezone = display_timezone_name()
    set_display_timezone("Asia/Shanghai")
    yield
    set_display_timezone(previous_timezone)


@pytest.fixture
def cloud_window(qtbot, tmp_path):
    requests = []
    connection = {"online": True}
    lease = _lease()
    offline_store = OfflineAccessStore(tmp_path / "offline-verifier")

    def handler(request):
        requests.append(request)
        if not connection["online"]:
            raise httpx.ConnectError("synthetic network is offline", request=request)
        if request.url.path == "/aliyun/v1/auth/login":
            return httpx.Response(200, json={
                "access_token": "synthetic-token-never-persisted-1234",
                "token_type": "bearer", "user": USER, "offline_lease": lease, "sync_protocol": 2,
            })
        if request.url.path == "/aliyun/v1/auth/logout":
            return httpx.Response(204)
        pytest.fail("Unexpected network operation; only synthetic login/logout are allowed")

    client = CloudAPIClient(ENDPOINT, transport=httpx.MockTransport(handler),
                            offline_store=offline_store)
    settings = EndpointSettingsStore(tmp_path / "endpoint-settings.json")
    settings.save(EndpointSettings(aliyun_url=ENDPOINT))
    window = build_desktop_window(
        "cloud", local_database=SimpleNamespace(), cloud_base_url=ENDPOINT,
        client_factory=lambda _url: client, endpoint_store=settings,
    )
    qtbot.addWidget(window)
    assert not requests
    yield SimpleNamespace(window=window, client=client, requests=requests,
                          connection=connection, offline_store=offline_store, lease=lease)
    qtbot.waitUntil(lambda: not window._tasks, timeout=5000)
    window._active_workspace = None
    window.cloud_sync_service.stop()
    client.close()
    window.close()


def test_extended_cloud_login_submits_once_and_forwards_explicit_options(
    qtbot, cloud_window, monkeypatch,
):
    context = cloud_window
    window, page = context.window, context.window.login_page
    completed, calls = [], []
    original = window.auth_service.authenticate

    def authenticate(username, password, **options):
        calls.append((username, password, options))
        return original(username, password, **options)

    monkeypatch.setattr(window.auth_service, "authenticate", authenticate)
    monkeypatch.setattr(window, "_login_succeeded", completed.append)
    legacy, extended = [], []
    page.login_requested.connect(lambda *_args: legacy.append(True))
    page.login_options_requested.connect(lambda *_args: extended.append(True))
    page.username_input.setText(USER["username"])
    page.password_input.setText(PASSWORD)
    page.allow_offline_checkbox.setChecked(True)
    page.remember_offline_checkbox.setChecked(True)
    page.login_button.click()
    qtbot.waitUntil(lambda: len(completed) == 1 and not window._tasks, timeout=5000)
    assert not legacy and extended == [True]
    assert calls == [(USER["username"], PASSWORD,
                      {"allow_offline": True, "remember_offline": True})]
    assert [request.url.path for request in context.requests] == ["/aliyun/v1/auth/login"]
    assert context.client.is_online_authenticated
    assert context.client.offline_opt_in


@pytest.mark.parametrize("use_extended_handler", [False, True])
def test_local_authentication_never_receives_cloud_offline_keywords(
    qtbot, tmp_path, monkeypatch, use_extended_handler,
):
    database = Database(tmp_path / "synthetic-local.db")
    database.initialize()
    store = EndpointSettingsStore(tmp_path / "local-endpoint-settings.json")
    store.save(EndpointSettings(mode="local"))
    window = build_desktop_window("local", local_database=database, endpoint_store=store)
    qtbot.addWidget(window)
    calls, completed = [], []

    def strict_local_authenticate(username, password):
        calls.append((username, password))
        return SimpleNamespace(**USER)

    monkeypatch.setattr(window.auth_service, "authenticate", strict_local_authenticate)
    monkeypatch.setattr(window, "_login_succeeded", completed.append)
    if use_extended_handler:
        window._login_with_options(USER["username"], PASSWORD,
                                   {"allow_offline": True, "remember_offline": True})
    else:
        page = window.login_page
        assert page.offline_options_frame.isHidden()
        page.username_input.setText(USER["username"])
        page.password_input.setText(PASSWORD)
        page.login_button.click()
    qtbot.waitUntil(lambda: len(completed) == 1 and not window._tasks, timeout=5000)
    assert calls == [(USER["username"], PASSWORD)]
    window.close()


def test_offline_login_without_matching_mirror_never_enters_workspace(
    qtbot, cloud_window, monkeypatch,
):
    context = cloud_window
    context.offline_store.enroll(ENDPOINT, USER, PASSWORD, context.lease)
    context.connection["online"] = False
    window, page = context.window, context.window.login_page
    monkeypatch.setattr(window.member_workspace, "start_session", _forbidden)
    monkeypatch.setattr(window.preferences_service, "get", _forbidden)
    page.username_input.setText(USER["username"])
    page.password_input.setText(PASSWORD)
    page.allow_offline_checkbox.setChecked(True)
    page.login_button.click()
    qtbot.waitUntil(lambda: not window._tasks, timeout=5000)
    assert window._active_workspace is None and window._current_user is None
    assert window.pages.currentWidget() is page
    assert not context.client.is_authenticated and not context.client.is_offline_session
    assert not window.cloud_sync_service.timer.isActive()
    assert not window.cloud_sync_service.mirror.authorized
    assert "离线资料尚未准备好" in page.error_label.text()
    assert not page.password_input.text()
    assert len(context.requests) == 1


def test_banner_follows_sync_signals_and_outbox_count_changes(cloud_window, monkeypatch):
    window = cloud_window.window
    sync = window.cloud_sync_service
    state = {"state": "offline", "message": "synthetic offline status", "pending_count": 2,
             "conflict_count": 1, "offline_expires_at": cloud_window.lease["expires_at"]}
    monkeypatch.setattr(sync, "state", lambda: state)
    sync.status_changed.emit(state)
    assert not window.offline_status_label.isHidden()
    assert "当前离线" in window.offline_status_label.text()
    assert "离线查看到期" in window.offline_status_label.text()
    assert "2 项尚未提交云端" in window.offline_status_label.text()
    assert "1 项需处理" in window.offline_status_label.text()
    state.update(state="online", pending_count=1, conflict_count=0)
    sync.outbox_changed.emit({"pending_count": 1, "conflict_count": 0})
    assert "1 项尚未提交云端" in window.offline_status_label.text()
    assert "当前离线" not in window.offline_status_label.text()
    state.update(pending_count=0)
    sync.outbox_changed.emit({"pending_count": 0, "conflict_count": 0})
    assert window.offline_status_label.isHidden()


def test_reauthentication_signal_exits_workspace_and_clears_offline_login_choices(
    qtbot, cloud_window, monkeypatch,
):
    window = cloud_window.window
    ended, logouts = [], []
    window._active_workspace = window.member_workspace
    window._current_user = SimpleNamespace(**USER)
    window.pages.setCurrentWidget(window.member_workspace)
    monkeypatch.setattr(window.member_workspace, "end_session", lambda: ended.append(True))
    monkeypatch.setattr(window.auth_service, "logout", lambda: logouts.append(True))
    window.login_page.password_input.setText(PASSWORD)
    window.login_page.allow_offline_checkbox.setChecked(True)
    window.login_page.remember_offline_checkbox.setChecked(True)
    window.cloud_sync_service.reauthentication_required.emit("网络已恢复，请重新在线登录后同步。")
    qtbot.waitUntil(lambda: not window._tasks, timeout=5000)
    assert ended == [True] and logouts == [True]
    assert window._active_workspace is None and window._current_user is None
    assert window.pages.currentWidget() is window.login_page
    assert not window.login_page.password_input.text()
    assert not window.login_page.allow_offline_checkbox.isChecked()
    assert not window.login_page.remember_offline_checkbox.isChecked()
    assert "重新在线登录" in window.login_page.error_label.text()
    assert window.offline_status_label.isHidden()
    assert not cloud_window.requests


def test_queued_timezone_change_does_not_apply_unconfirmed_settings_or_refresh(
    cloud_window, monkeypatch,
):
    window = cloud_window.window
    window._current_user = SimpleNamespace(**USER)
    window._active_workspace = window.health_workspace
    calls, notices = [], []
    queued = QueuedOperation("synthetic-timezone-request", "preferences", "update_preferences",
                             "2026-09-08T00:00:00+00:00")
    monkeypatch.setattr(QInputDialog, "getItem", lambda *_args: ("东京", True))
    monkeypatch.setattr(window.preferences_service, "update",
                        lambda actor, patch: calls.append((actor, patch)) or queued)
    monkeypatch.setattr(window, "apply_preferences", _forbidden)
    monkeypatch.setattr(window.health_workspace.today_page, "refresh", _forbidden)
    monkeypatch.setattr(window.health_workspace.my_platform_page, "refresh", _forbidden)
    monkeypatch.setattr(QMessageBox, "information",
                        lambda _parent, title, text: notices.append((title, text)))
    monkeypatch.setattr(QMessageBox, "warning", _forbidden)
    window._choose_timezone()
    assert calls == [(1, {"timezone": "Asia/Tokyo"})]
    assert notices == [("已暂存待同步", QUEUED_MESSAGE)]
    assert display_timezone_name() == "Asia/Shanghai"
    assert not cloud_window.requests


def test_default_cloud_client_receives_private_offline_store_without_network(
    qtbot, tmp_path, monkeypatch,
):
    from ollama_chat_app import main

    clients = []

    def factory(url, *, offline_store):
        assert isinstance(offline_store, OfflineAccessStore)
        assert offline_store.root.is_relative_to(tmp_path)
        client = CloudAPIClient(url, offline_store=offline_store,
                                transport=httpx.MockTransport(_forbidden))
        clients.append(client)
        return client

    monkeypatch.setattr(main, "CloudAPIClient", factory)
    window = build_desktop_window("cloud", local_database=SimpleNamespace(),
                                   cloud_base_url=ENDPOINT,
                                   endpoint_store=EndpointSettingsStore(tmp_path / "settings.json"))
    qtbot.addWidget(window)
    assert len(clients) == 1 and window.cloud_client is clients[0]
    window.cloud_sync_service.stop()
    clients[0].close()
    window.close()
