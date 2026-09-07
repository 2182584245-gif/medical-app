from __future__ import annotations

import os
from types import SimpleNamespace

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLineEdit, QStackedWidget, QWidget

from ollama_chat_app.ui.auth_pages import LoginPage, RegisterPage
from ollama_chat_app.ui.main_window import MainWindow


@pytest.mark.parametrize(
    ("page_class", "input_name", "button_name", "label"),
    [
        (LoginPage, "password_input", "password_visibility_button", "密码"),
        (RegisterPage, "password_input", "password_visibility_button", "密码"),
        (RegisterPage, "confirm_input", "confirm_visibility_button", "确认密码"),
    ],
)
def test_password_visibility_changes_display_without_editing_password(
    qtbot, page_class, input_name, button_name, label
) -> None:
    page = page_class()
    qtbot.addWidget(page)
    page.show()
    field = getattr(page, input_name)
    button = getattr(page, button_name)
    password = "  密码-Example-🙂-123  "
    field.setText(password)
    field.setCursorPosition(4)

    assert field.echoMode() == QLineEdit.EchoMode.Password
    assert field.displayText() != password
    assert field.accessibleName() == label
    assert button.accessibleName() == f"显示{label}"
    assert button.text() == "显示密码"
    edits: list[str] = []
    field.textChanged.connect(edits.append)

    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)

    assert field.echoMode() == QLineEdit.EchoMode.Normal
    assert field.displayText() == password
    assert field.text() == password
    assert field.cursorPosition() == 4
    assert button.isChecked()
    assert button.text() == "隐藏密码"
    assert button.accessibleName() == f"隐藏{label}"

    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)

    assert field.echoMode() == QLineEdit.EchoMode.Password
    assert field.displayText() != password
    assert field.text() == password
    assert not button.isChecked()
    assert edits == []


def test_keyboard_toggle_and_enter_submit_preserve_credentials(qtbot) -> None:
    page = LoginPage()
    qtbot.addWidget(page)
    page.show()
    page.username_input.setText("member")
    page.password_input.setText("  password-with-spaces  ")
    page.activateWindow()
    page.password_input.setFocus()
    qtbot.waitUntil(page.password_input.hasFocus)
    qtbot.keyClick(page.password_input, Qt.Key.Key_Tab)

    assert page.password_visibility_button.hasFocus()
    qtbot.keyClick(page.password_visibility_button, Qt.Key.Key_Space)
    assert page.password_input.echoMode() == QLineEdit.EchoMode.Normal

    with qtbot.waitSignal(page.login_requested) as request:
        qtbot.keyClick(page.password_input, Qt.Key.Key_Return)
    assert request.args == ["member", "  password-with-spaces  "]


@pytest.mark.parametrize("page_class", [LoginPage, RegisterPage])
def test_busy_form_disables_visibility_controls(qtbot, page_class) -> None:
    page = page_class()
    qtbot.addWidget(page)
    buttons = [page.password_visibility_button]
    if isinstance(page, RegisterPage):
        buttons.append(page.confirm_visibility_button)

    page.set_busy(True)
    for button in buttons:
        assert not button.isEnabled()
        button.click()
        assert not button.isChecked()

    page.set_busy(False)
    for button in buttons:
        assert button.isEnabled()
        button.click()
        assert button.isChecked()


def test_clear_login_password_restores_hidden_state(qtbot) -> None:
    page = LoginPage()
    qtbot.addWidget(page)
    page.password_input.setText("password-123")
    page.password_visibility_button.click()

    page.clear_password()

    assert page.password_input.text() == ""
    assert page.password_input.echoMode() == QLineEdit.EchoMode.Password
    assert not page.password_visibility_button.isChecked()
    assert page.password_visibility_button.text() == "显示密码"


def test_login_and_logout_return_to_a_hidden_empty_password(qtbot) -> None:
    window = MainWindow(
        SimpleNamespace(has_operator=lambda: True),
        SimpleNamespace(list_messages=lambda _user_id: []),
        object(),
    )
    qtbot.addWidget(window)
    window.show()
    page = window.login_page
    page.password_input.setText("password-123")
    page.password_visibility_button.click()

    window._login_succeeded(SimpleNamespace(id=1, username="member", role_code="member"))

    assert window.pages.currentWidget() is window.member_workspace
    assert page.password_input.text() == ""
    assert page.password_input.echoMode() == QLineEdit.EchoMode.Password

    window._logout()

    assert window.pages.currentWidget() is page
    assert page.password_input.text() == ""
    assert page.password_input.echoMode() == QLineEdit.EchoMode.Password
    assert not page.password_visibility_button.isChecked()


def test_register_password_toggles_are_independent_and_reset_hidden(qtbot) -> None:
    page = RegisterPage()
    qtbot.addWidget(page)
    page.password_input.setText("password-123")
    page.confirm_input.setText("password-123")
    page.password_visibility_button.click()

    assert page.password_input.echoMode() == QLineEdit.EchoMode.Normal
    assert page.confirm_input.echoMode() == QLineEdit.EchoMode.Password
    page.confirm_visibility_button.click()

    page.reset()

    for field, button in (
        (page.password_input, page.password_visibility_button),
        (page.confirm_input, page.confirm_visibility_button),
    ):
        assert field.text() == ""
        assert field.echoMode() == QLineEdit.EchoMode.Password
        assert not button.isChecked()
        assert button.text() == "显示密码"


@pytest.mark.parametrize("page_class", [LoginPage, RegisterPage])
def test_leaving_auth_page_restores_hidden_passwords(qtbot, page_class) -> None:
    pages = QStackedWidget()
    qtbot.addWidget(pages)
    page = page_class()
    other_page = QWidget()
    pages.addWidget(page)
    pages.addWidget(other_page)
    pages.show()
    page.password_input.setText("password-123")
    page.password_visibility_button.click()
    if isinstance(page, RegisterPage):
        page.confirm_visibility_button.click()

    pages.setCurrentWidget(other_page)
    pages.setCurrentWidget(page)

    assert page.password_input.text() == "password-123"
    assert page.password_input.echoMode() == QLineEdit.EchoMode.Password
    assert not page.password_visibility_button.isChecked()
    if isinstance(page, RegisterPage):
        assert page.confirm_input.echoMode() == QLineEdit.EchoMode.Password
        assert not page.confirm_visibility_button.isChecked()
