from __future__ import annotations

import sys
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QDateTime
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QDialog

from ollama_chat_app.data.database import Database
from ollama_chat_app.security.secret_store import SecretStore
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.chat import ChatService
from ollama_chat_app.time_utils import display_timezone_name, set_display_timezone
from ollama_chat_app.ui.advisor_ai_panel import AdvisorAiPanel
from ollama_chat_app.ui.chat_page import ChatPage
from ollama_chat_app.ui.deepseek_key_dialog import DeepSeekKeyDialog
from ollama_chat_app.ui.meal_capture_dialog import MealCaptureDialog
from ollama_chat_app.ui.segmented_time import SegmentedDateTimeEdit
from ollama_chat_app.ui.sleep_check_dialog import SleepCheckDialog
from ollama_chat_app.ui.theme import build_style


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    import httpx

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Focused UI tests must not contact any external service")

    monkeypatch.setattr(httpx.Client, "send", forbidden)


@pytest.fixture
def timezone_scope():
    original = display_timezone_name()
    yield
    set_display_timezone(original)


@pytest.mark.parametrize(
    "zone,offset,label",
    [
        ("Asia/Shanghai", "+08:00", "北京时间"),
        ("America/New_York", "-04:00", "纽约时间"),
        ("UTC", "+00:00", "世界标准时间"),
    ],
)
def test_sleep_segmented_times_are_user_zoned(
    qtbot, timezone_scope, monkeypatch, zone, offset, label
):
    set_display_timezone(zone)
    now = datetime(2026, 9, 14, 9, 30, tzinfo=ZoneInfo(zone))
    monkeypatch.setattr("ollama_chat_app.ui.sleep_check_dialog.beijing_now", lambda: now)
    dialog = SleepCheckDialog()
    qtbot.addWidget(dialog)
    assert isinstance(dialog.start, SegmentedDateTimeEdit)
    assert isinstance(dialog.end, SegmentedDateTimeEdit)
    assert bytes(dialog.start.timeZone().id()).decode() == zone
    assert dialog.start.hour.currentText() == "22"
    assert dialog.end.hour.currentText() == "7"
    assert f"2026-09-13T22:00:00{offset}" in dialog.prompt()
    assert f"2026-09-14T07:00:00{offset}" in dialog.prompt()
    assert label in dialog.prompt() and zone in dialog.prompt()
    assert dialog.start.voice_button.isEnabled()
    assert dialog.scroll.widgetResizable()
    dialog.validate()
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_sleep_invalid_field_does_not_accept_or_save(qtbot, timezone_scope):
    set_display_timezone("Asia/Shanghai")
    dialog = SleepCheckDialog()
    qtbot.addWidget(dialog)
    dialog.start.day.setCurrentText("99")
    dialog.validate()
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert "日期无效" in dialog.status.text()


@pytest.mark.parametrize("hours", [0, -1, 25])
def test_sleep_duration_validation(qtbot, timezone_scope, hours):
    dialog = SleepCheckDialog()
    qtbot.addWidget(dialog)
    start = dialog.start.dateTime()
    dialog.end.setDateTime(start.addSecs(hours * 3600))
    dialog.validate()
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert "24 小时" in dialog.status.text()


def test_sleep_future_time_rejected(qtbot):
    dialog = SleepCheckDialog()
    qtbot.addWidget(dialog)
    start = QDateTime.currentDateTimeUtc()
    dialog.start.setDateTime(start)
    dialog.end.setDateTime(start.addSecs(3600))
    dialog.validate()
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert "还没到" in dialog.status.text()


def test_camera_is_opt_in_and_synthetic_capture_kept_in_memory(qtbot, monkeypatch):
    def forbidden():
        raise AssertionError("Opening the dialog must not turn on a camera")

    monkeypatch.setattr("ollama_chat_app.ui.meal_capture_dialog.QCamera", forbidden)
    dialog = MealCaptureDialog()
    qtbot.addWidget(dialog)
    assert dialog.camera is None
    assert dialog.image_bytes is None
    image = QImage(16, 16, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFF)
    dialog.image_captured(1, image)
    assert dialog.image_bytes.startswith(b"\xff\xd8")
    assert dialog.file_path is None
    assert dialog.result() == QDialog.DialogCode.Accepted


def account(identifier=1, created_at="2026-09-14T00:00:00+00:00"):
    return SimpleNamespace(id=identifier, username="synthetic-advisor", created_at=created_at)


def fake_key_dialog(monkeypatch, *, persist=True, remove=False):
    class Dialog:
        api_key = "synthetic-advisor-key-only"
        persist_key = persist
        remove_requested = remove

        def __init__(self, current_key, parent):
            self.current_key = current_key

        def exec(self):
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr("ollama_chat_app.ui.advisor_ai_panel.DeepSeekKeyDialog", Dialog)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI test")
@pytest.mark.parametrize("persist", [True, False])
def test_advisor_remember_key_honors_explicit_choice(qtbot, tmp_path, monkeypatch, persist):
    store = SecretStore(namespace="synthetic-ui-test", storage_dir=tmp_path)
    panel = AdvisorAiPanel(None, store)
    qtbot.addWidget(panel)
    user = account()
    panel.start_session(user.id, [], account=user)
    panel.mode_combo.setCurrentIndex(panel.mode_combo.findData("deepseek_cloud"))
    fake_key_dialog(monkeypatch, persist=persist)
    assert panel._manage_key()
    identity = panel._persistent_identity
    stored = store.get_persistent_api_key(identity, "deepseek_cloud")
    assert (stored is not None) == persist
    assert panel._read_key("deepseek_cloud") == "synthetic-advisor-key-only"
    assert all(
        b"synthetic-advisor-key-only" not in item.read_bytes() for item in tmp_path.iterdir()
    )
    panel.end_session()
    restarted = SecretStore(namespace="synthetic-ui-test", storage_dir=tmp_path)
    panel.secret_store = restarted
    panel.start_session(user.id, [], account=user)
    assert (panel._read_key("deepseek_cloud") is not None) == persist
    replacement = account(created_at="2026-09-15T00:00:00+00:00")
    panel.start_session(replacement.id, [], account=replacement)
    assert panel._read_key("deepseek_cloud") is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI test")
def test_advisor_removing_key_removes_session_and_encrypted_copy(qtbot, tmp_path, monkeypatch):
    store = SecretStore(storage_dir=tmp_path)
    panel = AdvisorAiPanel(None, store)
    qtbot.addWidget(panel)
    user = account()
    panel.start_session(user.id, [], account=user)
    panel.mode_combo.setCurrentIndex(panel.mode_combo.findData("deepseek_cloud"))
    store.save_persistent_api_key(panel._persistent_identity, "synthetic-only", "deepseek_cloud")
    store.set_api_key(user.id, "synthetic-only", "deepseek_cloud")
    fake_key_dialog(monkeypatch, remove=True)
    assert not panel._manage_key()
    assert panel._read_key("deepseek_cloud") is None
    assert list(tmp_path.iterdir()) == []


def test_non_windows_dialog_cannot_request_persistence(qtbot, monkeypatch):
    monkeypatch.setattr("ollama_chat_app.ui.deepseek_key_dialog.sys.platform", "linux")
    dialog = DeepSeekKeyDialog()
    qtbot.addWidget(dialog)
    assert not dialog.persist_checkbox.isEnabled()
    dialog.persist_checkbox.setChecked(True)
    assert not dialog.persist_key


@pytest.mark.parametrize("font_size", [20, 30])
def test_chat_small_screen_controls_remain_scroll_accessible(qtbot, tmp_path, font_size):
    database = Database(tmp_path / "layout.db")
    page = ChatPage(ChatService(database), SecretStore(storage_dir=tmp_path / "credentials"))
    qtbot.addWidget(page)
    page.setStyleSheet(build_style(font_size))
    page.resize(1000, 600)
    page.show()
    qtbot.wait(20)
    assert page.width() == 1000 and page.height() == 600
    assert page.page_scroll.widgetResizable()
    page.page_scroll.ensureWidgetVisible(page.send_button)
    viewport = page.page_scroll.viewport()
    center = page.send_button.mapTo(viewport, page.send_button.rect().center())
    assert viewport.rect().contains(center)
    assert page.delete_conversations_button.text() == "删除勾选对话"


def test_audio_opt_in_and_demo_do_not_carry_to_another_account(qtbot, tmp_path):
    database = Database(tmp_path / "session.db")
    user = AuthService(database).register("synthetic-session", "synthetic-password-only")
    page = ChatPage(ChatService(database), SecretStore(storage_dir=tmp_path / "credentials"))
    qtbot.addWidget(page)
    page.start_session(user)
    page.speak_checkbox.setChecked(True)
    page.demo_checkbox.setChecked(True)
    page._last_voice_text = "previous synthetic speech"
    page.end_session()
    assert not page.speak_checkbox.isChecked()
    assert not page.demo_checkbox.isChecked()
    assert page._last_voice_text == ""
