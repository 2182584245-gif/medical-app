from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from PySide6.QtCore import QDate, QPoint, Qt
from PySide6.QtWidgets import QLabel

from ollama_chat_app.data.database import Database
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.health import HealthService
from ollama_chat_app.time_utils import as_beijing, beijing_now, beijing_today, format_beijing
from ollama_chat_app.ui.files_reports_panel import ReportDraftDialog
from ollama_chat_app.ui.health_records_panel import LifeRecordDialog, ReminderEditorDialog
from ollama_chat_app.ui.health_workspace import ProfilePage, RecordDialog, ReminderDialog
from ollama_chat_app.ui.theme import APP_STYLE
from ollama_chat_app.ui.time_fields import BeijingDateTimeEdit, OptionalDateEdit, datetime_iso


@pytest.fixture
def health_services(tmp_path):
    database = Database(tmp_path / "time-regression.db")
    database.initialize()
    user = AuthService(database).register("beijing-user", "correct-horse-battery-staple")
    return database, user, HealthService(database)


@pytest.mark.parametrize("dialog_type", [RecordDialog, LifeRecordDialog])
def test_both_record_entries_save_selected_beijing_time(qtbot, health_services, dialog_type):
    database, user, health = health_services
    dialog = dialog_type("water")
    qtbot.addWidget(dialog)
    dialog.content_input.setPlainText("下午喝了一杯水")
    yesterday = beijing_today() - timedelta(days=1)
    chosen = f"{yesterday.isoformat()} 15:45"
    dialog.show()
    dialog.time_input.setFocus()
    dialog.time_input.selectAll()
    qtbot.keyClicks(dialog.time_input, chosen)
    values = dialog.values
    assert values["occurred_at"][:16] == chosen.replace(" ", "T")
    assert values["occurred_at"].endswith("+08:00")
    health.add_life_record(user.id, **values)
    record = health.list_life_records(user.id)[0]
    assert as_beijing(record["occurred_at"]).hour == 15
    assert record["local_date"] == yesterday.isoformat()
    assert record["timezone_offset_minutes"] == 480
    reopened = HealthService(Database(database.path))
    saved = reopened.list_life_records(user.id)[0]
    assert as_beijing(saved["occurred_at"]).minute == 45
    assert any("北京时间" in label.text() for label in dialog.findChildren(QLabel))


@pytest.mark.parametrize("dialog_type", [ReminderDialog, ReminderEditorDialog])
def test_both_reminder_entries_save_and_reload_without_timezone_prompt(
    qtbot, health_services, dialog_type,
):
    _database, user, health = health_services
    dialog = dialog_type()
    qtbot.addWidget(dialog)
    dialog.title_input.setText("晚间散步")
    tomorrow = beijing_today() + timedelta(days=1)
    chosen = f"{tomorrow.isoformat()} 21:30"
    dialog.show()
    dialog.time_input.setFocus()
    dialog.time_input.selectAll()
    qtbot.keyClicks(dialog.time_input, chosen)
    values = dialog.values
    assert values["scheduled_at"][:16] == chosen.replace(" ", "T")
    assert values["scheduled_at"].endswith("+08:00")
    health.add_reminder(user.id, **values)
    reminder = health.list_reminders(user.id)[0]
    loaded = ReminderEditorDialog(reminder)
    qtbot.addWidget(loaded)
    assert loaded.time_input.text() == chosen
    health.update_reminder(user.id, reminder["id"], **loaded.values)
    saved = health.list_reminders(user.id)[0]
    assert as_beijing(saved["scheduled_at"]).hour == 21
    assert as_beijing(saved["scheduled_at"]).minute == 30


@pytest.mark.parametrize(
    "stored", ["2026-01-01T23:30:00Z", datetime(2026, 1, 1, 23, 30, tzinfo=UTC)]
)
def test_existing_utc_reminder_retains_instant_and_displays_beijing(qtbot, stored):
    dialog = ReminderEditorDialog({"title": "早餐", "scheduled_at": stored})
    qtbot.addWidget(dialog)
    assert dialog.time_input.text() == "2026-01-02 07:30"
    assert as_beijing(dialog.values["scheduled_at"]) == as_beijing(stored)


def test_datetime_full_keyboard_entry_and_unfocused_save(qtbot):
    editor = BeijingDateTimeEdit("2026-09-05T01:30:00Z")
    qtbot.addWidget(editor)
    editor.show()
    editor.setFocus()
    editor.selectAll()
    qtbot.keyClicks(editor, "2027-01-02 21:45")
    assert datetime_iso(editor.dateTime()) == "2027-01-02T21:45:00+08:00"


def test_optional_date_typing_blank_clear_and_real_1900_birthday(qtbot):
    editor = OptionalDateEdit(past_only=True)
    qtbot.addWidget(editor)
    assert editor.iso_value() is None
    editor.show()
    editor.setFocus()
    editor.selectAll()
    qtbot.keyClicks(editor, "1960-01-02")
    assert editor.iso_value() == "1960-01-02"
    editor.selectAll()
    qtbot.keyClick(editor, Qt.Key.Key_Delete)
    assert editor.iso_value() is None
    editor.selectAll()
    qtbot.keyClicks(editor, "1900-01-01")
    assert editor.iso_value() == "1900-01-01"


def test_empty_optional_calendar_opens_at_current_beijing_month(qtbot):
    editor = OptionalDateEdit()
    qtbot.addWidget(editor)
    editor.resize(320, 44)
    editor.show()
    qtbot.mouseClick(editor, Qt.MouseButton.LeftButton, pos=QPoint(editor.width() - 9, 20))
    calendar = editor.calendarWidget()
    qtbot.waitUntil(calendar.isVisible)
    today = beijing_today()
    assert (calendar.yearShown(), calendar.monthShown()) == (today.year, today.month)
    assert editor.iso_value() is None
    qtbot.keyClick(calendar, Qt.Key.Key_Escape)


def test_profile_birth_date_saves_1900_and_can_be_cleared(qtbot, health_services):
    _database, user, health = health_services
    page = ProfilePage(health)
    qtbot.addWidget(page)
    page.set_user(user.id)
    assert page.birth_date_input.iso_value() is None
    page.birth_date_input.setDate(QDate(1900, 1, 1))
    page.save()
    assert health.get_profile(user.id)["birth_date"] == "1900-01-01"
    page.birth_date_input.clear()
    page.save()
    assert not health.get_profile(user.id)["birth_date"]


def test_report_date_calendar_and_keyboard_produce_civil_date(qtbot):
    dialog = ReportDraftDialog({"text": "测试报告", "candidate_items": []})
    qtbot.addWidget(dialog)
    assert dialog.values["report_date"] is None
    dialog.show()
    dialog.report_date.setFocus()
    dialog.report_date.selectAll()
    qtbot.keyClicks(dialog.report_date, "2026-08-31")
    assert dialog.values["report_date"] == "2026-08-31"
    assert dialog.report_date.calendarPopup()


def test_existing_invalid_future_birthday_is_not_silently_changed(qtbot):
    editor = OptionalDateEdit(past_only=True)
    qtbot.addWidget(editor)
    future = (beijing_today() + timedelta(days=20)).isoformat()
    editor.set_iso_value(future)
    assert editor.iso_value() == future


def test_default_editor_is_beijing_and_style_leaves_text_editable(qtbot):
    editor = BeijingDateTimeEdit()
    qtbot.addWidget(editor)
    editor.setStyleSheet(APP_STYLE)
    editor.show()
    assert editor.dateTime().offsetFromUtc() == 8 * 3600
    assert abs((as_beijing(datetime_iso(editor.dateTime())) - beijing_now()).total_seconds()) < 10
    assert not editor.isReadOnly()
    assert editor.lineEdit().height() >= editor.fontMetrics().height()
    assert editor.calendarPopup()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-01-01T23:30:00Z", "2026-01-02 07:30"),
        ("2026-01-01T21:30:00-05:00", "2026-01-02 10:30"),
        (datetime(2026, 1, 1, 23, 30, tzinfo=UTC), "2026-01-02 07:30"),
        ("2026-01-01 21:30", "2026-01-01 21:30"),
    ],
)
def test_saved_time_display_is_consistent(value, expected):
    assert format_beijing(value) == expected
