"""Offline Qt acceptance using a copy of the reviewed fictional release corpus.

No production credential, user database, system secret store or network is used.
Optional evidence is written only to V170_UI_EVIDENCE; source seed stays immutable.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt, QThreadPool, QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
)

from ollama_chat_app.data.database import Database
from ollama_chat_app.main import build_desktop_window
from ollama_chat_app.security.passwords import hash_password
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.developer import LocalDeveloperService, provision_local_developer
from ollama_chat_app.services.developer_settings import DeveloperError
from ollama_chat_app.services.health import HealthService
from ollama_chat_app.ui.developer_workspace import (
    DeveloperLoginDialog,
    DeveloperWorkspaceDialog,
    RecordEditDialog,
    UserDataDialog,
)
from ollama_chat_app.ui.theme import build_style
from tools.generate_synthetic_demo import EXPANDED_PROFILE, build_demo
from tools.upgrade_synthetic_demo_v7 import contract

DEVELOPER_PASSWORD = "OfflineJourneyOnly@1726"
MEMBER_PASSWORD = "MemberJourneyOnly@1726"
INITIAL_FAKE_KEY = "sk-offline-fixture-never-used"
NEXT_FAKE_KEY = "sk-offline-replacement-never-used"


class JourneySecrets:
    def __init__(self):
        self.defaults = {"deepseek_cloud": INITIAL_FAKE_KEY}
        self.persistent = {}
        self.session = {}

    def get_default_api_key(self, provider="deepseek_cloud"):
        return self.defaults.get(provider)

    def set_default_api_key(self, key, provider="deepseek_cloud"):
        self.defaults[provider] = key

    def get_api_key(self, user, provider="ollama_cloud"):
        return self.session.get((user, provider))

    def set_api_key(self, user, key, provider="ollama_cloud"):
        self.session[(user, provider)] = key

    def get_persistent_api_key(self, user, provider="deepseek_cloud"):
        return self.persistent.get((user, provider))

    def save_persistent_api_key(self, user, key, provider="deepseek_cloud"):
        self.persistent[(user, provider)] = key

    def get_effective_api_key(self, user, provider="deepseek_cloud"):
        return (
            self.get_api_key(user, provider)
            or self.get_persistent_api_key(user, provider)
            or self.get_default_api_key(provider)
        )

    def clear_api_keys(self):
        self.session.clear()


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _capture(widget, name, qapp):
    qapp.processEvents()
    supplied = os.environ.get("V170_UI_EVIDENCE")
    if not supplied:
        return
    directory = Path(supplied)
    directory.mkdir(parents=True, exist_ok=True)
    assert widget.grab().save(str(directory / f"{name}.png"))


def _visible_text(widget):
    text = [child.text() for child in widget.findChildren(QLabel)]
    text += [child.text() for child in widget.findChildren(QLineEdit)]
    text += [child.toPlainText() for child in widget.findChildren(QPlainTextEdit)]
    return "\n".join(text)


@pytest.fixture(scope="module")
def verified_seed(tmp_path_factory):
    supplied = os.environ.get("V170_ACCEPTANCE_SEED")
    source = Path(supplied) if supplied else tmp_path_factory.mktemp("journey-seed") / "seed"
    if not supplied:
        old_environment = dict(os.environ)
        try:
            build_demo(source, profile=EXPANDED_PROFILE, screenshots=False)
        finally:
            os.environ.clear()
            os.environ.update(old_environment)
    contract().sibling("_demo_v170").verify_payload(source)
    before = _hash(source / "data/app.db")
    yield source
    assert _hash(source / "data/app.db") == before


@pytest.fixture
def journey(verified_seed, tmp_path, monkeypatch, qapp, qtbot):
    calls, warnings, windows = [], [], []

    def forbidden(*_args, **_kwargs):
        calls.append("network attempt")
        raise AssertionError("Offline developer journey must not access a network")

    monkeypatch.setattr("httpx.Client.send", forbidden)
    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "private"))
    target = tmp_path / "working-copy"
    shutil.copytree(verified_seed, target)
    monkeypatch.chdir(target)
    monkeypatch.setenv("OLLAMA_DUAL_CHAT_DATA_DIR", str(target / "data"))
    database = Database(target / "data/app.db")
    auth = AuthService(database)
    with database.transaction() as connection:
        users = {
            row["username"]: row["id"]
            for row in connection.execute("SELECT id,username FROM users")
        }
        for username, password in (
            ("demo-operator", DEVELOPER_PASSWORD),
            ("demo-member-1", MEMBER_PASSWORD),
        ):
            connection.execute(
                "UPDATE users SET password_hash=? WHERE username=?",
                (hash_password(password), username),
            )
    provision_local_developer(database, users["demo-operator"])
    secrets = JourneySecrets()
    monkeypatch.setattr("ollama_chat_app.main.SecretStore", lambda **_kw: secrets)
    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_kw: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(
        QMessageBox, "information", lambda *_a, **_kw: QMessageBox.StandardButton.Ok
    )
    monkeypatch.setattr(QMessageBox, "warning", lambda *_a, **_kw: warnings.append(_a[2]))
    previous_font, previous_style = qapp.font(), qapp.styleSheet()
    font_directory = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for filename in ("msyh.ttc", "msyhbd.ttc"):
        if (font_directory / filename).is_file():
            QFontDatabase.addApplicationFont(str(font_directory / filename))
    qapp.setFont(QFont("Microsoft YaHei UI", 15))
    qapp.setStyleSheet(build_style(20))

    def build_window():
        window = build_desktop_window("local", local_database=database)
        qtbot.addWidget(window)
        window.resize(1500, 960)
        window.show()
        window.network_monitor.stop()
        windows.append(window)
        return window

    window = build_window()
    assert window.developer_service is not None, window._developer_initialization_error
    fixture = SimpleNamespace(
        window=window,
        database=database,
        auth=auth,
        users=users,
        secrets=secrets,
        warnings=warnings,
        windows=windows,
        build_window=build_window,
    )
    yield fixture
    for item in windows:
        item._logout()
        item.close()
    QThreadPool.globalInstance().waitForDone(3000)
    qapp.processEvents()
    qapp.setFont(previous_font)
    qapp.setStyleSheet(previous_style)
    assert not calls


def _ready(dialog, qtbot):
    qtbot.waitUntil(lambda: not dialog._tasks and dialog.isEnabled(), timeout=10000)


def _workspace(journey, qtbot):
    service = journey.window.developer_service
    service.authenticate("demo-operator", DEVELOPER_PASSWORD)
    dialog = DeveloperWorkspaceDialog(service, journey.window)
    qtbot.addWidget(dialog)
    dialog.resize(1300, 900)
    dialog.show()
    dialog.reload()
    _ready(dialog, qtbot)
    assert dialog._snapshot is not None
    return dialog


def test_entry_login_authentication_and_three_role_double_click(journey, qtbot, qapp, monkeypatch):
    window = journey.window
    assert window.developer_button.isVisible()
    assert window.developer_button.text() == "开发者模式"
    assert window.developer_button.geometry().left() < window.network_button.geometry().left()
    _capture(window, "01-login-entry", qapp)
    observed = []

    def capture_entry(self):
        self.show()
        assert self.password_field.line_edit.echoMode() == QLineEdit.EchoMode.Password
        observed.append(self.windowTitle())
        _capture(self, "02-developer-authentication", qapp)
        self.reject()
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(DeveloperLoginDialog, "exec", capture_entry)
    qtbot.mouseClick(window.developer_button, Qt.MouseButton.LeftButton)
    assert observed == ["开发者模式 · 身份验证"]
    login = DeveloperLoginDialog(window.developer_service, window)
    qtbot.addWidget(login)
    login.show()
    login.username_input.setText("demo-operator")
    login.password_field.line_edit.setText(DEVELOPER_PASSWORD)
    qtbot.mouseClick(
        login.buttons.button(QDialogButtonBox.StandardButton.Ok), Qt.MouseButton.LeftButton
    )
    qtbot.waitUntil(lambda: login.result() == QDialog.DialogCode.Accepted, timeout=10000)
    _ready(login, qtbot)
    assert login.password_field.line_edit.text() == ""
    dialog = _workspace(journey, qtbot)
    assert dialog.tabs.count() == 4
    assert dialog.role_tabs.count() == 3
    assert {role: table.rowCount() for role, table in dialog.user_tables.items()} == {
        "operator": 1,
        "advisor": 2,
        "member": 2,
    }
    displayed = []

    def capture_details(self):
        self.resize(1150, 820)
        self.show()
        displayed.append(self.details["user"]["role_code"])
        assert "password_hash" not in json.dumps(self.details)
        assert "$argon2" not in _visible_text(self)
        assert INITIAL_FAKE_KEY not in _visible_text(self)
        self.tabs.setCurrentIndex(1)
        _capture(self, "03-user-details-" + displayed[-1], qapp)
        self.reject()
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(UserDataDialog, "exec", capture_details)
    for index, role in enumerate(("operator", "advisor", "member")):
        dialog.role_tabs.setCurrentIndex(index)
        qapp.processEvents()
        table = dialog.user_tables[role]
        qtbot.mouseClick(
            table.viewport(),
            Qt.MouseButton.LeftButton,
            pos=table.visualItemRect(table.item(0, 1)).center(),
        )
        qtbot.mouseDClick(
            table.viewport(),
            Qt.MouseButton.LeftButton,
            pos=table.visualItemRect(table.item(0, 1)).center(),
        )
        qtbot.waitUntil(lambda r=role: r in displayed, timeout=10000)
        _ready(dialog, qtbot)
    assert displayed == ["operator", "advisor", "member"]
    assert not journey.warnings


def test_ordinary_member_cannot_enter_developer_workspace(journey, qtbot):
    service = journey.window.developer_service
    login = DeveloperLoginDialog(service, journey.window)
    qtbot.addWidget(login)
    login.show()
    login.username_input.setText("demo-member-1")
    login.password_field.line_edit.setText(MEMBER_PASSWORD)
    qtbot.mouseClick(
        login.buttons.button(QDialogButtonBox.StandardButton.Ok), Qt.MouseButton.LeftButton
    )
    qtbot.waitUntil(lambda: bool(journey.warnings), timeout=10000)
    _ready(login, qtbot)
    assert login.result() != QDialog.DialogCode.Accepted
    with pytest.raises(DeveloperError):
        service.list_users("member")
    assert "权限" in journey.warnings[0]


def test_profile_and_chat_edits_stay_queued_until_new_service(journey, qtbot, qapp, monkeypatch):
    service = journey.window.developer_service
    service.authenticate("demo-operator", DEVELOPER_PASSWORD)
    member_id = journey.users["demo-member-1"]
    initial = service.user_details(member_id)
    dialog = UserDataDialog(service, initial, journey.window)
    qtbot.addWidget(dialog)
    dialog.resize(1150, 820)
    dialog.show()
    dialog.tabs.setCurrentIndex(1)
    edits = [
        ("member_profiles", "display_name", "陈淑兰（已调整）"),
        ("messages", "content", "这是一条已核对的生活记录说明。"),
    ]
    chosen = {}
    original_exec = RecordEditDialog.exec

    def edit_and_accept(self):
        field, value = chosen["field"], chosen["value"]
        editor = self.inputs[field]
        if isinstance(editor, QPlainTextEdit):
            editor.setPlainText(value)
        else:
            editor.setText(value)
        buttons = self.findChild(QDialogButtonBox)
        QTimer.singleShot(0, lambda: buttons.button(QDialogButtonBox.StandardButton.Save).click())
        return original_exec(self)

    monkeypatch.setattr(RecordEditDialog, "exec", edit_and_accept)
    for table, field, value in edits:
        chosen.update(field=field, value=value)
        dialog.table_selector.setCurrentIndex(dialog.table_selector.findData(table))
        qapp.processEvents()
        qtbot.mouseClick(
            dialog.records.viewport(),
            Qt.MouseButton.LeftButton,
            pos=dialog.records.visualItemRect(dialog.records.item(0, 1)).center(),
        )
        qtbot.mouseDClick(
            dialog.records.viewport(),
            Qt.MouseButton.LeftButton,
            pos=dialog.records.visualItemRect(dialog.records.item(0, 1)).center(),
        )
        qtbot.waitUntil(
            lambda t=table: any(
                x["table"] == t for x in service.user_details(member_id)["pending_changes"]
            ),
            timeout=10000,
        )
        _ready(dialog, qtbot)
    before = service.user_details(member_id)
    assert len(before["pending_changes"]) == 2
    assert before["tables"]["member_profiles"][0]["display_name"] == "陈淑兰"
    assert before["tables"]["messages"][0]["content"] == initial["tables"]["messages"][0]["content"]
    assert "2 条" in dialog.pending_label.text()
    _capture(dialog, "08-records-queued-until-reopen", qapp)
    reopened = LocalDeveloperService(journey.database, journey.secrets)
    reopened.authenticate("demo-operator", DEVELOPER_PASSWORD)
    after = reopened.user_details(member_id)
    assert after["tables"]["member_profiles"][0]["display_name"] == edits[0][2]
    assert after["tables"]["messages"][0]["content"] == edits[1][2]
    assert after["pending_changes"] == []
    assert not journey.warnings


def test_ui_publication_freezes_key_skills_knowledge_and_features_until_reopen(
    journey, qtbot, qapp
):
    window = journey.window
    member = journey.auth.get_user(journey.users["demo-member-1"])
    window._login_succeeded(member)
    dialog = _workspace(journey, qtbot)
    original = window.developer_service.startup_snapshot
    assert window.health_workspace.navigation_buttons[3].isEnabled()
    dialog.complexity.setCurrentIndex(dialog.complexity.findData("detailed"))
    dialog.prompt.setPlainText("先了解生活情境，用通俗话解释，不猜测没有记录的事实。")
    dialog.key.line_edit.setText(NEXT_FAKE_KEY)
    dialog.skills.set_entries(
        [
            {
                "name": "整理已发生的生活事项",
                "instructions": "只整理已提供的日期和事实。",
                "triggers": ["整理"],
                "enabled": True,
            }
        ]
    )
    dialog.knowledge.set_entries(
        [
            {
                "title": "生活记录方法",
                "content": "想不起来的测量值可以先留空。",
                "source": "测试团队人工编写的记录指南",
                "reviewed": True,
                "keywords": ["记录"],
                "enabled": True,
            }
        ]
    )
    dialog.tips.setPlainText("慢慢记录，不必追求每条都齐全。\n不清楚的地方可以先留空。")
    dialog.features["ai"].setChecked(False)
    dialog.features["commerce"].setChecked(False)
    dialog.announcement.setPlainText("今晚可以回看本周的生活记录。")
    dialog.timeout.setValue(90)
    qtbot.mouseClick(dialog.publish_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: dialog._snapshot["revision"] == 1, timeout=10000)
    _ready(dialog, qtbot)
    assert dialog.key.line_edit.text() == ""
    assert NEXT_FAKE_KEY not in _visible_text(dialog)
    assert "已安全配置" in dialog.key_state.text()
    assert window.developer_service.startup_snapshot == original
    assert window.secret_store.get_default_api_key() == INITIAL_FAKE_KEY
    assert window.health_workspace.navigation_buttons[3].isEnabled()
    for index, name in enumerate(
        (
            "04-developer-users",
            "05-developer-ai",
            "06-developer-open-features",
            "07-developer-runtime",
        )
    ):
        dialog.tabs.setCurrentIndex(index)
        _capture(dialog, name, qapp)
        if index == 1:
            scroll = dialog.tabs.currentWidget()
            assert isinstance(scroll, QScrollArea)
            scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
            _capture(dialog, "05b-developer-knowledge-and-daily-tips", qapp)
    with journey.database.connect() as connection:
        assert NEXT_FAKE_KEY not in "\n".join(
            str(tuple(row)) for row in connection.execute("SELECT * FROM app_settings")
        )
    dialog.reject()
    window._logout()
    window.close()
    reopened = journey.build_window()
    reopened._login_succeeded(member)
    new_snapshot = reopened.developer_service.startup_snapshot
    assert new_snapshot["revision"] == 1
    assert new_snapshot["settings"]["ai"]["skills"][0]["name"] == "整理已发生的生活事项"
    assert new_snapshot["settings"]["ai"]["knowledge"][0]["reviewed"] is True
    assert reopened.secret_store.get_default_api_key() == NEXT_FAKE_KEY
    assert not reopened.health_workspace.navigation_buttons[3].isEnabled()
    assert not reopened.health_workspace.service_page.section_tabs.isTabEnabled(1)
    assert reopened.health_workspace.navigation_buttons[4].isEnabled()
    assert HealthService(journey.database).get_profile(member.id)["display_name"] == "陈淑兰"
    _capture(reopened, "09-member-next-launch-feature-policy", qapp)
    assert not journey.warnings


@pytest.mark.parametrize("font_size", (20, 24))
def test_four_sections_remain_readable_with_enlarged_fonts(journey, qtbot, qapp, font_size):
    qapp.setStyleSheet(build_style(font_size))
    dialog = _workspace(journey, qtbot)
    for index in range(4):
        dialog.tabs.setCurrentIndex(index)
        qapp.processEvents()
        assert dialog.tabs.tabBar().tabRect(index).width() > 100
        assert dialog.publish_button.isVisible()
        assert dialog.publish_button.geometry().height() >= 30
        assert dialog.rect().contains(
            dialog.publish_button.mapTo(dialog, dialog.publish_button.rect().center())
        )
        _capture(dialog, f"10-font-{font_size}-section-{index + 1}", qapp)
    assert "$argon2" not in _visible_text(dialog)
    assert not journey.warnings
