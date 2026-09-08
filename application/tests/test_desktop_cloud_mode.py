from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import httpx
import pytest

from ollama_chat_app.cloud_config import configured_cloud_base_url
from ollama_chat_app.data.database import Database
from ollama_chat_app.endpoint_settings import (
    EndpointSettings,
    EndpointSettingsError,
    EndpointSettingsStore,
)
from ollama_chat_app.main import DesktopWindowController, build_desktop_window
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.cloud_client import CloudAPIClient


@pytest.fixture(autouse=True)
def isolated_connection_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "private-local-data"))
    monkeypatch.delenv("HEALTHLIFE_CLOUD_BASE_URL", raising=False)


@pytest.fixture
def local_endpoint_store(tmp_path):
    store = EndpointSettingsStore(tmp_path / "endpoint-settings.json")
    store.save(EndpointSettings(mode="local"))
    return store


@pytest.fixture
def synthetic_database(tmp_path):
    database = Database(tmp_path / "synthetic-local.db")
    database.initialize()
    AuthService(database).register("synthetic-local-member", "synthetic-local-password")
    return database


def _logical_snapshot(database):
    with database.connect() as connection:
        return tuple(connection.iterdump())


def _user(name):
    now = datetime.now(UTC).isoformat()
    return {"id": 1 if name == "synthetic-first" else 2, "username": name,
            "username_normalized": name, "created_at": now, "last_login_at": None,
            "role_code": "member", "account_status": "active", "updated_at": None}


@pytest.fixture
def mock_cloud(monkeypatch):
    monkeypatch.setattr(CloudAPIClient, "_on_gui_thread", staticmethod(lambda: False))
    clients, requests = [], []

    def handler(request):
        requests.append(request)
        if request.url.path == "/v1/auth/login":
            name = json.loads(request.content)["username"]
            return httpx.Response(200, json={
                "access_token": "synthetic-token-only-in-memory-" + name,
                "token_type": "bearer", "expires_at": "2026-09-08T00:00:00Z",
                "user": _user(name),
            })
        if request.url.path == "/v1/auth/logout":
            return httpx.Response(204)
        pytest.fail("Construction/switching must not query cloud data")

    def factory(url):
        client = CloudAPIClient(url, transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    yield factory, clients, requests
    for client in clients:
        client.close()


def test_cloud_window_builds_remote_services_without_local_data_and_leaves_commerce_disconnected(
    qapp, qtbot, mock_cloud
):
    factory, clients, requests = mock_cloud

    class ForbiddenDatabase:
        def initialize(self):
            pytest.fail("Cloud construction must not initialize local data")

        def connect(self):
            pytest.fail("Cloud construction must not read local data")

    window = build_desktop_window("cloud", local_database=ForbiddenDatabase(),
                                   cloud_base_url="https://health.example.com",
                                   client_factory=factory)
    qtbot.addWidget(window)
    for service in (window.auth_service, window.chat_service, window.health_service,
                    window.service_management_service, window.preferences_service,
                    window.file_management_service, window.ai_assistant_service):
        assert service.uses_network
        assert service.client is clients[0]
        assert not hasattr(service, "database")
    assert requests == []
    assert window.commerce_service is None  # Virtual shopping is explicitly local-only.
    assert window.backup_service is None
    assert not window.login_page.import_button.isEnabled()
    assert window.cloud_mode
    assert "云端模式" in window.windowTitle()
    assert not window.login_page.operator_setup_frame.isVisible()
    assert window.login_page.import_button.isHidden()
    for surface in window._role_workspaces.values():
        for name in ("export_button", "import_button"):
            button = getattr(surface, name, None)
            if button is not None:
                assert button.isHidden()
    assert "健康记录" in window.login_page._card.subtitle_label.text()
    assert "上传附件" in window.login_page._card.subtitle_label.text()
    assert "独立服务" in window.login_page._card.subtitle_label.text()
    window.chat_page.current_user = object()
    window.chat_page._refresh_busy_controls()
    assert window.chat_page.image_button.isEnabled()
    clients[0].close()


def test_mode_switch_rebuilds_every_service_and_preserves_local_data(
    qapp, qtbot, synthetic_database, mock_cloud, local_endpoint_store
):
    factory, clients, requests = mock_cloud
    before = _logical_snapshot(synthetic_database)
    controller = DesktopWindowController(synthetic_database,
        cloud_base_url="https://health.example.com", client_factory=factory,
        endpoint_store=local_endpoint_store)
    local = controller.start()
    qtbot.addWidget(local)
    assert not local.cloud_mode
    assert requests == []
    local.login_page.password_input.setText("must-clear-on-mode-change")
    local.secret_store.set_api_key(1, "synthetic-private-key", "deepseek_cloud")
    old_store = local.secret_store
    # Exercise the actual signal that the login page emits.
    local.connection_change_requested.emit("cloud", "https://health.example.com")
    cloud = controller.window
    qtbot.addWidget(cloud)
    assert cloud is not local and cloud.cloud_mode
    assert old_store.get_api_key(1, "deepseek_cloud") is None
    assert cloud.secret_store.get_api_key(1, "deepseek_cloud") is None
    assert cloud.login_page.password_input.text() == ""
    assert requests == []
    assert _logical_snapshot(synthetic_database) == before

    cloud.auth_service.authenticate("synthetic-first", "synthetic-first-password")
    assert clients[0].is_authenticated
    cloud.auth_service.logout()
    assert not clients[0].is_authenticated
    assert controller.switch_mode("local")
    returned = controller.window
    qtbot.addWidget(returned)
    assert not returned.cloud_mode
    assert returned.chat_service is not local.chat_service
    assert clients[0]._closed
    assert _logical_snapshot(synthetic_database) == before
    controller.shutdown()
    returned.close()


def test_cloud_user_change_and_restart_do_not_reuse_tokens_or_keys(
    qapp, qtbot, synthetic_database, mock_cloud, local_endpoint_store
):
    factory, clients, requests = mock_cloud
    controller = DesktopWindowController(synthetic_database, client_factory=factory,
                                         endpoint_store=local_endpoint_store)
    first_window = controller.start()
    qtbot.addWidget(first_window)
    assert controller.switch_mode("cloud", "https://health.example.com")
    cloud = controller.window
    qtbot.addWidget(cloud)
    first = cloud.auth_service.authenticate("synthetic-first", "synthetic-password")
    cloud.secret_store.set_api_key(first.id, "first-private-ai-key")
    cloud.auth_service.logout()
    second = cloud.auth_service.authenticate("synthetic-second", "synthetic-password")
    assert first.id != second.id
    assert "synthetic-second" in clients[0]._token
    assert clients[0]._user["id"] == second.id
    assert cloud.secret_store.get_api_key(second.id) is None
    controller.shutdown()
    cloud.close()
    assert not clients[0].is_authenticated
    assert clients[0]._closed
    assert cloud.secret_store.get_api_key(first.id) is None

    count_before_restart = len(requests)
    restarted = DesktopWindowController(synthetic_database, client_factory=factory,
                                        endpoint_store=local_endpoint_store)
    fresh = restarted.start()
    qtbot.addWidget(fresh)
    assert fresh.cloud_mode  # Explicitly selected cloud mode survives restart, credentials do not.
    assert not clients[-1].is_authenticated
    assert fresh.secret_store.get_api_key(first.id) is None
    assert len(requests) == count_before_restart
    restarted.shutdown()
    fresh.close()


def test_active_session_and_pending_work_block_switch_and_close(
    qapp, qtbot, synthetic_database, mock_cloud, local_endpoint_store
):
    factory, _, requests = mock_cloud
    controller = DesktopWindowController(synthetic_database, client_factory=factory,
                                         endpoint_store=local_endpoint_store)
    window = controller.start()
    qtbot.addWidget(window)
    window._tasks.append(object())
    assert not controller.switch_mode("cloud", "https://health.example.com")
    window.close()
    assert window.isVisible()
    assert requests == []
    window._tasks.clear()
    window._active_workspace = object()
    assert not controller.switch_mode("cloud", "https://health.example.com")
    window._active_workspace = None
    controller.shutdown()
    window.close()


def test_invalid_cloud_setting_keeps_existing_local_window_and_data(
    qapp, qtbot, synthetic_database, mock_cloud, local_endpoint_store
):
    factory, _, requests = mock_cloud
    before = _logical_snapshot(synthetic_database)
    controller = DesktopWindowController(synthetic_database, client_factory=factory,
                                         endpoint_store=local_endpoint_store)
    window = controller.start()
    qtbot.addWidget(window)
    assert not controller.switch_mode("cloud", "https://username:private-secret@health.example.com")
    assert controller.window is window and not window.cloud_mode
    assert "private-secret" not in window.login_page.error_label.text()
    assert _logical_snapshot(synthetic_database) == before
    assert requests == []
    controller.shutdown()
    window.close()


def test_public_configuration_cannot_expose_credentials(monkeypatch):
    from ollama_chat_app import cloud_config

    monkeypatch.setattr(cloud_config, "DEFAULT_CLOUD_BASE_URL", "")
    monkeypatch.delenv("HEALTHLIFE_CLOUD_BASE_URL", raising=False)
    assert configured_cloud_base_url() == ""
    monkeypatch.setenv("HEALTHLIFE_CLOUD_BASE_URL", "https://health.example.com/")
    assert configured_cloud_base_url() == "https://health.example.com"
    monkeypatch.setenv("HEALTHLIFE_CLOUD_BASE_URL", "https://user:secret@health.example.com")
    assert configured_cloud_base_url() == ""


def test_startup_sets_http_logging_to_warning(monkeypatch, tmp_path):
    from ollama_chat_app import main

    monkeypatch.setattr(main, "log_path", lambda: tmp_path / "synthetic-log.txt")
    previous = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    try:
        main.configure_logging()
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
    finally:
        for name, level in previous.items():
            logging.getLogger(name).setLevel(level)


def test_fresh_install_starts_default_aliyun_without_network_or_config_write(
    qtbot, synthetic_database, mock_cloud, tmp_path,
):
    factory, clients, requests = mock_cloud
    store = EndpointSettingsStore(tmp_path / "fresh-connection.json")
    controller = DesktopWindowController(synthetic_database, endpoint_store=store,
                                         client_factory=factory)
    window = controller.start()
    qtbot.addWidget(window)
    assert window.cloud_mode and clients[-1].base_url == "https://39.106.166.15/aliyun"
    assert not requests and not store.path.exists()
    controller.shutdown()
    window.close()


def test_saved_local_mode_starts_local_without_any_cloud_client(
    qtbot, synthetic_database, mock_cloud, local_endpoint_store,
):
    factory, clients, requests = mock_cloud
    before = local_endpoint_store.path.read_bytes()
    controller = DesktopWindowController(synthetic_database, endpoint_store=local_endpoint_store,
                                         client_factory=factory)
    window = controller.start()
    qtbot.addWidget(window)
    assert not window.cloud_mode and not clients and not requests
    assert local_endpoint_store.path.read_bytes() == before
    controller.shutdown()
    window.close()


def test_custom_supabase_route_survives_restart_and_reaches_the_same_dialog_store(
    qtbot, synthetic_database, mock_cloud, tmp_path,
):
    factory, clients, requests = mock_cloud
    store = EndpointSettingsStore(tmp_path / "custom-settings.json")
    saved = EndpointSettings("cloud", "supabase", "https://first.example.com/api",
                             "https://second.example.com/business")
    store.save(saved)
    before = store.path.read_bytes()
    for _ in range(2):
        controller = DesktopWindowController(synthetic_database, endpoint_store=store,
                                             client_factory=factory)
        window = controller.start()
        qtbot.addWidget(window)
        assert clients[-1].base_url == saved.supabase_url
        assert window.login_page.endpoint_store is store
        assert "Supabase" in window.login_page.cloud_target_label.text()
        assert store.path.read_bytes() == before
        controller.shutdown()
        window.close()
    assert not requests


def test_malformed_settings_fall_back_local_with_notice_and_no_overwrite(
    qtbot, synthetic_database, mock_cloud, tmp_path,
):
    factory, clients, requests = mock_cloud
    store = EndpointSettingsStore(tmp_path / "damaged-settings.json")
    store.path.write_text("invalid-private-marker", encoding="utf-8")
    controller = DesktopWindowController(synthetic_database, endpoint_store=store,
                                         client_factory=factory)
    window = controller.start()
    qtbot.addWidget(window)
    assert not window.cloud_mode and not clients and not requests
    assert "已保留" in window.login_page.error_label.text()
    assert "invalid-private-marker" not in window.login_page.error_label.text()
    assert store.path.read_text() == "invalid-private-marker"
    controller.shutdown()
    window.close()


def test_environment_override_is_session_only_and_preserves_both_custom_routes(
    qtbot, synthetic_database, mock_cloud, tmp_path, monkeypatch,
):
    factory, clients, requests = mock_cloud
    store = EndpointSettingsStore(tmp_path / "custom-settings.json")
    saved = EndpointSettings("cloud", "supabase", "https://first.example.com/a",
                             "https://second.example.com/b")
    store.save(saved)
    before = store.path.read_bytes()
    monkeypatch.setenv("HEALTHLIFE_CLOUD_BASE_URL", "https://override.example.com/session")
    controller = DesktopWindowController(synthetic_database, endpoint_store=store,
                                         client_factory=factory)
    window = controller.start()
    qtbot.addWidget(window)
    assert clients[-1].base_url == "https://override.example.com/session"
    assert store.path.read_bytes() == before
    assert not requests
    controller.shutdown()
    window.close()


def test_persistence_failure_keeps_previous_window_and_closes_unused_replacement(
    qtbot, synthetic_database, mock_cloud, local_endpoint_store, monkeypatch,
):
    factory, clients, requests = mock_cloud
    controller = DesktopWindowController(synthetic_database, endpoint_store=local_endpoint_store,
                                         client_factory=factory)
    previous = controller.start()
    qtbot.addWidget(previous)
    before = local_endpoint_store.path.read_bytes()

    def fail(*_args):
        raise EndpointSettingsError("synthetic failure")

    monkeypatch.setattr(local_endpoint_store, "save_connection", fail)
    assert not controller.switch_mode("cloud", "https://next.example.com/api")
    assert controller.window is previous and previous.isVisible()
    assert not previous.cloud_mode and clients[-1]._closed
    assert local_endpoint_store.path.read_bytes() == before
    assert not requests
    controller.shutdown()
    previous.close()
