from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QDialog, QWidget

from ollama_chat_app.security.secret_store import SecretStore
from ollama_chat_app.ui.main_window import MainWindow
from ollama_chat_app.ui.operator_setup_dialog import OperatorSetupDialog


class FakeAuthService:
    def __init__(self, has_operator: bool = False) -> None:
        self.operator_exists = has_operator
        self.bootstrap_calls: list[tuple[str, str]] = []

    def has_operator(self) -> bool:
        return self.operator_exists

    def bootstrap_operator(self, username: str, password: str) -> object:
        self.bootstrap_calls.append((username, password))
        self.operator_exists = True
        return SimpleNamespace(id=1, username=username, role_code="operator")

    def authenticate(self, username: str, password: str) -> None:
        del username, password
        raise AssertionError("authentication is not part of this focused test")

    def register(self, username: str, password: str) -> None:
        del username, password
        raise AssertionError("registration is not part of this focused test")


class FakeChatService:
    def list_messages(self, user_id: int) -> list[Any]:
        del user_id
        return []


class FakeRoleWorkspace(QWidget):
    logout_requested = Signal()
    export_requested = Signal()
    import_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.started_with: object | None = None
        self.end_count = 0

    def start_session(self, user: object) -> None:
        self.started_with = user

    def end_session(self) -> None:
        self.started_with = None
        self.end_count += 1


def test_login_highlights_setup_only_until_an_operator_exists(qtbot) -> None:
    auth = FakeAuthService(has_operator=False)
    window = MainWindow(auth, FakeChatService(), SecretStore())
    qtbot.addWidget(window)

    assert not window.login_page.operator_setup_frame.isHidden()

    auth.operator_exists = True
    window._refresh_operator_setup_availability()
    assert window.login_page.operator_setup_frame.isHidden()


def test_existing_operator_cannot_open_bootstrap_dialog(qtbot, monkeypatch) -> None:
    auth = FakeAuthService(has_operator=True)
    window = MainWindow(auth, FakeChatService(), SecretStore())
    qtbot.addWidget(window)

    monkeypatch.setattr(
        "ollama_chat_app.ui.main_window.OperatorSetupDialog",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("dialog must not open after bootstrap")
        ),
    )
    window._open_operator_setup()

    assert "不能再次初始化" in window.login_page.error_label.text()
    assert auth.bootstrap_calls == []


def test_operator_setup_dialog_checks_confirmation_and_password_policy(qtbot) -> None:
    dialog = OperatorSetupDialog()
    qtbot.addWidget(dialog)
    dialog.username_input.setText("operator")
    dialog.password_input.setText("short")
    dialog.confirm_input.setText("different")

    dialog._validate_and_accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert "不一致" in dialog.error_label.text()

    dialog.confirm_input.setText("short")
    dialog._validate_and_accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert "至少需要 8 个字符" in dialog.error_label.text()

    dialog.password_input.setText("long-enough-password")
    dialog.confirm_input.setText("long-enough-password")
    dialog._validate_and_accept()
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_bootstrap_form_dispatches_service_call_without_logging_in(qtbot, monkeypatch) -> None:
    auth = FakeAuthService(has_operator=False)
    window = MainWindow(auth, FakeChatService(), SecretStore())
    qtbot.addWidget(window)
    captured: dict[str, object] = {}

    class AcceptedDialog:
        DialogCode = QDialog.DialogCode
        username = "first-operator"
        password = "valid-password"

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def exec(self) -> QDialog.DialogCode:
            return QDialog.DialogCode.Accepted

    def capture_task(task, on_result, on_error, on_finished=None) -> None:
        captured["task"] = task
        captured["on_result"] = on_result
        captured["on_error"] = on_error
        captured["on_finished"] = on_finished

    monkeypatch.setattr("ollama_chat_app.ui.main_window.OperatorSetupDialog", AcceptedDialog)
    monkeypatch.setattr(window, "_start_task", capture_task)

    window._open_operator_setup()

    task = captured["task"]
    assert task.function == auth.bootstrap_operator
    assert task.args == ("first-operator", "valid-password")
    assert window.pages.currentWidget() is window.login_page


def test_login_routes_each_role_to_its_own_workspace(qtbot) -> None:
    advisor_workspace = FakeRoleWorkspace()
    operator_workspace = FakeRoleWorkspace()
    window = MainWindow(
        FakeAuthService(has_operator=True),
        FakeChatService(),
        SecretStore(),
        advisor_workspace=advisor_workspace,
        operator_workspace=operator_workspace,
    )
    qtbot.addWidget(window)

    member = SimpleNamespace(id=1, username="member", role_code="member")
    window._login_succeeded(member)
    assert window.pages.currentWidget() is window.member_workspace
    assert window.chat_page.current_user is member
    window._logout()

    advisor = SimpleNamespace(id=2, username="advisor", role_code="advisor")
    window._login_succeeded(advisor)
    assert window.pages.currentWidget() is advisor_workspace
    assert advisor_workspace.started_with is advisor
    window._logout()
    assert advisor_workspace.end_count == 1

    operator = SimpleNamespace(id=3, username="operator", role_code="operator")
    window._login_succeeded(operator)
    assert window.pages.currentWidget() is operator_workspace
    assert operator_workspace.started_with is operator
    window._logout()
    assert operator_workspace.end_count == 1


def test_unknown_role_never_falls_back_to_member_workspace(qtbot) -> None:
    window = MainWindow(FakeAuthService(has_operator=True), FakeChatService(), SecretStore())
    qtbot.addWidget(window)

    window._login_succeeded(SimpleNamespace(id=9, username="unknown", role_code="root"))

    assert window.pages.currentWidget() is window.login_page
    assert window.chat_page.current_user is None
    assert "角色无效" in window.login_page.error_label.text()
