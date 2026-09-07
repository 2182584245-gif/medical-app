from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

# This must be set before importing PySide6 or any application UI module.
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from ollama_chat_app.ui.auth_pages import LoginPage, RegisterPage
from ollama_chat_app.ui.chat_page import ChatPage
from ollama_chat_app.ui.main_window import MainWindow


class FakeAuthService:
    """The smoke tests build the window but deliberately perform no authentication."""

    def authenticate(self, username: str, password: str) -> None:
        raise AssertionError("authentication must not run while constructing MainWindow")

    def register(self, username: str, password: str) -> None:
        raise AssertionError("registration must not run while constructing MainWindow")


class FakeChatService:
    """Minimal chat dependency whose methods must not run during page construction."""

    def list_messages(self, user_id: int) -> list[Any]:
        return []

    def begin_message(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("a chat request must not start during this smoke test")


class FakeSecretStore:
    """Fail loudly if local-mode construction unexpectedly touches credentials."""

    def get_api_key(self, user_id: int) -> None:
        raise AssertionError("local mode must not read an Ollama Cloud API key")

    def set_api_key(self, user_id: int, api_key: str) -> None:
        raise AssertionError("local mode must not save an Ollama Cloud API key")

    def delete_api_key(self, user_id: int) -> None:
        raise AssertionError("local mode must not delete an Ollama Cloud API key")

    def clear_api_keys(self) -> None:
        raise AssertionError("local mode must not clear Ollama Cloud API keys")


def test_main_window_can_create_login_and_register_pages(qtbot) -> None:
    window = MainWindow(FakeAuthService(), FakeChatService(), FakeSecretStore())
    qtbot.addWidget(window)

    assert isinstance(window.login_page, LoginPage)
    assert isinstance(window.register_page, RegisterPage)
    assert window.pages.indexOf(window.login_page) >= 0
    assert window.pages.indexOf(window.register_page) >= 0
    assert window.pages.currentWidget() is window.login_page
    assert not window.login_page.import_button.isEnabled()


def test_chat_page_defaults_and_switches_to_local_without_ollama_access(
    qtbot,
    monkeypatch,
) -> None:
    unexpected_calls: list[str] = []

    def fail_provider_construction(*args: Any, **kwargs: Any) -> None:
        unexpected_calls.append("LocalOllamaProvider")
        raise AssertionError("local provider must only be created when a message is sent")

    def fail_client_construction(*args: Any, **kwargs: Any) -> None:
        unexpected_calls.append("ollama.Client")
        raise AssertionError("constructing or selecting local mode must not access Ollama")

    # ChatPage imports the provider class directly, while the provider module owns
    # the Ollama SDK reference. Guard both boundaries so this test catches either
    # eager provider creation or an eager SDK/network client probe.
    monkeypatch.setattr(
        "ollama_chat_app.ui.chat_page.LocalOllamaProvider",
        fail_provider_construction,
    )
    monkeypatch.setattr(
        "ollama_chat_app.providers.local_ollama.ollama.Client",
        fail_client_construction,
    )

    page = ChatPage(FakeChatService(), FakeSecretStore())
    qtbot.addWidget(page)

    assert page.mode_combo.currentData() == "local"
    assert [page.mode_combo.itemData(index) for index in range(page.mode_combo.count())] == [
        "local",
        "ollama_cloud",
        "deepseek_cloud",
    ]
    assert page.deepseek_model_combo.currentData() == "deepseek-v4-flash"
    assert page.model_stack.currentWidget() is page.local_model_combo
    assert not page.key_button.isVisible()
    assert not page.backup_button.isEnabled()
    assert "发送消息时才会连接" in page.status_label.text()

    # Simulate returning from the cloud selector without first executing the cloud
    # branch. The real local-mode change handler must remain a UI-only operation.
    page.mode_combo.blockSignals(True)
    page.mode_combo.setCurrentIndex(1)
    page.model_stack.setCurrentIndex(1)
    page.key_button.show()
    page.mode_combo.blockSignals(False)
    page.mode_combo.setCurrentIndex(0)

    assert page.mode_combo.currentData() == "local"
    assert page.model_stack.currentWidget() is page.local_model_combo
    assert not page.key_button.isVisible()
    assert "发送消息时才会连接" in page.status_label.text()
    assert unexpected_calls == []


def test_portable_backup_button_is_disabled_during_model_request(qtbot) -> None:
    fake_backup_service = object()
    page = ChatPage(FakeChatService(), FakeSecretStore(), fake_backup_service)
    qtbot.addWidget(page)
    page.start_session(SimpleNamespace(id=1, username="alice"))

    assert page.backup_button.isEnabled()
    assert "全部本地账户和聊天记录" in page.backup_button.toolTip()

    page._set_request_running(True)
    assert not page.backup_button.isEnabled()

    page._set_request_running(False)
    assert page.backup_button.isEnabled()
