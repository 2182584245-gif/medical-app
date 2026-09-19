"""No real endpoints, credentials or databases: exercise the login worker boundary."""

from types import SimpleNamespace

import httpx
import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QDialog, QMessageBox

from ollama_chat_app.endpoint_settings import EndpointSettings
from ollama_chat_app.services.cloud_client import CloudAPIClient, CloudAPIError
from ollama_chat_app.services.developer import RemoteDeveloperService
from ollama_chat_app.services.developer_settings import default_snapshot
from ollama_chat_app.services.remote_services import RemoteAuthService
from ollama_chat_app.ui.auth_pages import LoginPage
from ollama_chat_app.ui.developer_workspace import DeveloperLoginDialog
from ollama_chat_app.ui.main_window import MainWindow

ENDPOINT = "https://synthetic.example.com/aliyun"


@pytest.fixture
def login_harness(qtbot):
    clients = []
    tasks = []

    def build(role, startup_status):
        user = {
            "id": 7, "username": "synthetic-" + role,
            "username_normalized": "synthetic-" + role,
            "created_at": "2026-09-19T00:00:00Z", "last_login_at": None,
            "role_code": role, "account_status": "active", "updated_at": None,
        }
        requests = []

        def handler(request):
            requests.append(request.url.path)
            if request.url.path == "/aliyun/v1/auth/login":
                return httpx.Response(200, json={
                    "access_token": "synthetic-never-persisted-token", "token_type": "bearer",
                    "user": user,
                })
            if request.url.path == "/aliyun/v1/client-start":
                assert request.headers["Authorization"] == (
                    "Bearer synthetic-never-persisted-token")
                return httpx.Response(startup_status, json={"detail": "synthetic"})
            raise AssertionError("Unexpected mock endpoint")

        def make_client(url):
            client = CloudAPIClient(url, transport=httpx.MockTransport(handler))
            clients.append(client)
            return client

        client = make_client(ENDPOINT)
        developer = RemoteDeveloperService(client, client_factory=make_client)
        store = SimpleNamespace(load=lambda: EndpointSettings(aliyun_url=ENDPOINT))
        page = LoginPage(endpoint_store=store)
        qtbot.addWidget(page)
        page.set_connection_context("cloud", ENDPOINT)
        completed = []
        window = SimpleNamespace(
            cloud_mode=True, auth_service=RemoteAuthService(client), developer_service=developer,
            login_page=page, _login_succeeded=completed.append,
            _pending_developer_snapshot={"old_identity": True},
            _pending_developer_warning="old identity warning",
        )

        def start(task, success, error, finished):
            tasks.append(task)
            task.signals.result.connect(success)
            task.signals.error.connect(error)
            task.signals.finished.connect(finished)
            task.signals.finished.connect(lambda: tasks.remove(task))
            QThreadPool.globalInstance().start(task)

        window._start_task = start
        return SimpleNamespace(window=window, client=client, developer=developer,
                               page=page, completed=completed, requests=requests, user=user)

    yield build
    qtbot.waitUntil(lambda: not tasks, timeout=5000)
    assert QThreadPool.globalInstance().waitForDone(3000)
    for client in clients:
        client.close()


@pytest.mark.parametrize("role", ["member", "advisor", "operator"])
@pytest.mark.parametrize("startup_status", [404, 500])
def test_optional_startup_config_failure_keeps_all_three_valid_cloud_roles_usable(
    login_harness, qtbot, role, startup_status,
):
    case = login_harness(role, startup_status)
    MainWindow._login(case.window, case.user["username"], "Synthetic-Only-2026!")
    qtbot.waitUntil(lambda: len(case.completed) == 1 and not case.page._busy, timeout=5000)
    assert case.completed[0].role_code == role
    assert case.client.is_online_authenticated
    assert case.page.login_button.isEnabled()
    assert not case.page.error_label.text()
    assert case.page._active_connection_mode == "cloud"
    assert case.requests == ["/aliyun/v1/auth/login", "/aliyun/v1/client-start"]
    snapshot = case.window._pending_developer_snapshot
    assert snapshot["settings"]["features"]["today"] is True
    assert snapshot["settings"]["ai"]["enabled"] is (startup_status == 404)
    if startup_status == 404:
        assert snapshot == default_snapshot()
        assert "尚未开放" in case.window._pending_developer_warning


@pytest.mark.parametrize("role", ["member", "advisor", "operator"])
def test_rejected_startup_session_never_uses_cached_config_or_login_success(
    login_harness, qtbot, role,
):
    case = login_harness(role, 401)
    case.developer._trusted_user_snapshots[case.user["id"]] = default_snapshot()
    MainWindow._login(case.window, case.user["username"], "Synthetic-Only-2026!")
    qtbot.waitUntil(lambda: not case.page._busy, timeout=5000)
    assert case.completed == []
    assert not case.client.is_authenticated
    assert case.window._pending_developer_snapshot is None
    assert case.window._pending_developer_warning == ""
    assert "云端登录状态已失效，请重新登录" in case.page.error_label.text()
    assert "离线资料尚未准备好" not in case.page.error_label.text()
    assert case.page.login_button.isEnabled()
    assert case.page._active_connection_mode == "cloud"
    assert case.requests == ["/aliyun/v1/auth/login", "/aliyun/v1/client-start"]


@pytest.mark.parametrize("code", ["not_found", "authentication", "permission"])
def test_developer_login_reports_missing_cloud_interface_without_claiming_bad_password(
    qtbot, monkeypatch, code,
):
    messages = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _parent, _title, text: messages.append(text))

    def authenticate(_username, _password):
        raise CloudAPIError(code)

    service = SimpleNamespace(uses_network=True, authenticate=authenticate)
    dialog = DeveloperLoginDialog(service)
    qtbot.addWidget(dialog)
    dialog.username_input.setText("synthetic-developer")
    dialog.password_field.line_edit.setText("Synthetic-Only-2026!")
    dialog._submit()
    qtbot.waitUntil(lambda: len(messages) == 1 and not dialog._tasks, timeout=5000)
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert dialog.password_field.line_edit.text() == ""
    assert ("云端开发者接口尚未升级（不是密码错误）" in messages[0]) is (code == "not_found")
    if code != "not_found":
        assert messages == [str(CloudAPIError(code))]


def test_pending_mode_selection_cannot_open_developer_service_for_previous_mode(qtbot, monkeypatch):
    store = SimpleNamespace(load=lambda: EndpointSettings(aliyun_url=ENDPOINT))
    page = LoginPage(endpoint_store=store)
    qtbot.addWidget(page)
    page.set_connection_context("cloud", ENDPOINT)
    page.connection_mode.setCurrentIndex(page.connection_mode.findData("local"))
    assert page._connection_pending()
    assert not page.login_button.isEnabled()
    messages = []
    monkeypatch.setattr(
        QMessageBox, "information", lambda _parent, _title, text: messages.append(text))
    # No developer_service attribute: trying to access the old service would fail.
    MainWindow._open_developer_mode(SimpleNamespace(login_page=page))
    assert len(messages) == 1 and "应用所选模式" in messages[0]
    assert page._active_connection_mode == "cloud"


@pytest.mark.parametrize("mode,label", [("local", "本地模式"), ("cloud", "阿里云模式")])
def test_login_explains_independent_account_domains_without_implicit_fallback(qtbot, mode, label):
    store = SimpleNamespace(load=lambda: EndpointSettings(aliyun_url=ENDPOINT))
    page = LoginPage(endpoint_store=store)
    qtbot.addWidget(page)
    page.set_connection_context(mode, ENDPOINT)
    text = page._card.subtitle_label.text()
    assert label in text
    assert "本地账号与阿里云账号相互独立" in text
    assert "不会自动切换" in text
    assert page._active_connection_mode == mode
    assert page.login_button.isEnabled()
