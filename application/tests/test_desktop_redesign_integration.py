"""Real desktop construction with fictional local data and network interdiction."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QLineEdit, QToolButton

from ollama_chat_app.data.database import Database, timestamp_from_db
from ollama_chat_app.main import build_desktop_window
from ollama_chat_app.time_utils import display_timezone_name
from ollama_chat_app.ui.health_records_panel import ReminderEditorDialog
from ollama_chat_app.ui.operator_workspace import CreateAdvisorDialog
from ollama_chat_app.ui.voice_input import VoiceInputInstaller


@pytest.fixture
def desktop(qtbot, qapp, tmp_path, monkeypatch):
    from tools.generate_synthetic_demo import build_demo

    calls = []

    def blocked(*args, **kwargs):
        calls.append("network attempt")
        raise AssertionError("Desktop integration tests must stay offline")

    monkeypatch.setattr("httpx.Client.send", blocked)
    monkeypatch.setattr("socket.socket.connect", blocked)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "private"))
    monkeypatch.setenv("OLLAMA_DUAL_CHAT_DATA_DIR", str(tmp_path / "isolated-data"))
    demo = tmp_path / "synthetic-demo"
    build_demo(demo, screenshots=False)
    monkeypatch.chdir(demo)
    database = Database(demo / "data" / "app.db")
    installer = VoiceInputInstaller(qapp)
    window = build_desktop_window("local", local_database=database)
    qtbot.addWidget(window)
    window.show()
    with database.connect() as connection:
        users = {
            row["username"]: window.auth_service.get_user(int(row["id"]))
            for row in connection.execute("SELECT id, username FROM users")
        }
    yield window, users, database, calls
    window._logout()
    QThreadPool.globalInstance().waitForDone(3000)
    qapp.processEvents()
    qapp.removeEventFilter(installer)
    assert not calls, "No integration test may call a network transport"


def test_desktop_roles_major_views_fit_supported_sizes(desktop, qtbot, qapp, tmp_path):
    window, users, _database, _calls = desktop
    root = Path(os.environ.get("DESKTOP_REVIEW_OUTPUT", str(tmp_path / "screenshots")))
    root.mkdir(parents=True, exist_ok=True)
    observations = []
    overflow = []
    for width, height, font in ((1500, 960, 20), (1366, 768, 20), (1500, 960, 24), (1366, 768, 24)):
        for role, account in (
            ("member", "demo-member-1"),
            ("advisor", "demo-advisor-1"),
            ("operator", "demo-operator"),
        ):
            user = users[account]
            window.preferences_service.update(user.id, {"font_size": font, "theme_color": "sage"})
            window._login_succeeded(user)
            workspace = window._active_workspace
            pages = {"member": (0, 1, 2, 3, 4), "advisor": (1, 2, 3), "operator": (0, 1, 4)}[role]
            for page_index in pages:
                if role == "member":
                    workspace.show_page(page_index)
                else:
                    workspace.tabs.setCurrentIndex(page_index)
                window.resize(width, height)
                qapp.processEvents()
                qtbot.wait(15)
                name = f"{role}-page{page_index}-{width}x{height}-font{font}"
                observations.append(
                    {
                        "name": name,
                        "requested": [width, height],
                        "actual": [window.width(), window.height()],
                        "minimum": [window.minimumWidth(), window.minimumHeight()],
                    }
                )
                assert window.grab().save(str(root / f"{name}.png"))
                if window.width() > width or window.height() > height:
                    overflow.append(observations[-1])
            window._logout()
    (root / "layout-observations.json").write_text(
        json.dumps(observations, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Desktop integration screenshots: {root}")
    assert not overflow, json.dumps(overflow, ensure_ascii=False)


def test_desktop_personal_appearance_and_timezone_are_isolated(desktop, qtbot):
    window, users, _database, _calls = desktop
    member = users["demo-member-1"]
    other = users["demo-member-2"]
    window.preferences_service.update(
        member.id,
        {
            "font_size": 24,
            "theme_color": "blue",
            "timezone": "America/New_York",
        },
    )
    window._login_succeeded(member)
    assert "font-size: 24px" in window.styleSheet()
    assert "#365b71" in window.styleSheet()
    assert display_timezone_name() == "America/New_York"
    reminder = window.health_service.list_reminders(member.id)[0]
    instant = (
        timestamp_from_db(reminder["scheduled_at"])
        if isinstance(reminder["scheduled_at"], str)
        else reminder["scheduled_at"]
    )
    dialog = ReminderEditorDialog(reminder, window)
    qtbot.addWidget(dialog)
    original_epoch = dialog.time_input.dateTime().toMSecsSinceEpoch()
    assert original_epoch == int(instant.timestamp() * 1000)
    window.apply_preferences({"font_size": 24, "theme_color": "blue", "timezone": "Asia/Shanghai"})
    assert dialog.time_input.dateTime().toMSecsSinceEpoch() == original_epoch
    actual = window.health_service.list_reminders(member.id)[0]["scheduled_at"]
    assert actual == reminder["scheduled_at"]
    window._logout()
    assert display_timezone_name() == "Asia/Shanghai"
    window._login_succeeded(other)
    assert "font-size: 20px" in window.styleSheet()
    assert "#405b45" in window.styleSheet()
    assert display_timezone_name() == "Asia/Shanghai"
    window._logout()
    window._login_succeeded(member)
    assert "font-size: 24px" in window.styleSheet()
    assert display_timezone_name() == "America/New_York"


def test_desktop_credentials_have_no_voice_control_and_zip_actions_are_hidden(desktop, qtbot, qapp):
    window, users, _database, _calls = desktop
    password_fields = [window.login_page.password_input, window.register_page.password_input]
    advisor = CreateAdvisorDialog(window)
    qtbot.addWidget(advisor)
    advisor.show()
    qapp.processEvents()
    password_fields.append(advisor.password_input)
    for field in password_fields:
        assert field.echoMode() != QLineEdit.EchoMode.Normal
        assert field.findChild(QToolButton, "InlineVoiceInput") is None
    assert advisor.username_input.findChild(QToolButton, "InlineVoiceInput") is not None
    advisor.close()
    for role in ("member", "advisor", "operator"):
        account = "demo-operator" if role == "operator" else f"demo-{role}-1"
        window._login_succeeded(users[account])
        for surface in (window.login_page, window.chat_page, *window._role_workspaces.values()):
            for name in ("backup_button", "export_button", "import_button"):
                control = getattr(surface, name, None)
                if control is not None:
                    assert control.isHidden(), f"Legacy ZIP action visible: {role}/{name}"
        window._logout()
