from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QDialog, QLabel, QMessageBox

from ollama_chat_app.services.offline_outbox import QueuedOperation
from ollama_chat_app.services.preferences import DEFAULT_PREFERENCES
from ollama_chat_app.ui.health_records_panel import HealthRecordsPanel
from ollama_chat_app.ui.health_workspace import ProfilePage, ServicePage
from ollama_chat_app.ui.member_statistics import ProfileStatisticsPage
from ollama_chat_app.ui.member_today import TodayPage
from ollama_chat_app.ui.my_platform import MyPlatformPanel
from ollama_chat_app.ui.offline_feedback import QUEUED_MESSAGE, show_queued_result
from ollama_chat_app.ui.outbox_dialog import OutboxDialog

QUEUED = QueuedOperation("synthetic-request-uuid", "health", "add_life_record",
                         "2026-09-08T00:00:00+00:00")


def _accepted(**values):
    return SimpleNamespace(values=values, exec=lambda: QDialog.DialogCode.Accepted,
                           set_proposal=lambda _proposal: None)


def _forbidden(*_args, **_kwargs):
    pytest.fail("Queued work must not be shown as cloud-confirmed or trigger a cloud refresh")


def test_queued_feedback_is_strict_type_not_fake_cloud_id(qtbot):
    label = QLabel()
    qtbot.addWidget(label)
    assert not show_queued_result({"id": 123, "status": "queued"}, label, label=label)
    assert not show_queued_result(123, label, label=label)
    assert show_queued_result(QUEUED, label, label=label)
    assert label.text() == QUEUED_MESSAGE and label.objectName() == "HealthPending"
    assert "synthetic-request-uuid" not in label.text()


def test_today_queue_preserves_cloud_lists_and_does_not_emit_cloud_success(qtbot, monkeypatch):
    page = TodayPage(SimpleNamespace())
    qtbot.addWidget(page)
    page.user_id = 1
    monkeypatch.setattr(page, "refresh", _forbidden)
    page.record_changed.connect(_forbidden)
    page.reminder_changed.connect(_forbidden)
    page._mutate(lambda: QUEUED, True)
    assert page.status_label.text() == QUEUED_MESSAGE
    page._mutate(lambda: QUEUED, False)
    assert page.status_label.text() == QUEUED_MESSAGE


def test_confirmed_ai_record_queue_is_not_reported_as_saved(qtbot, monkeypatch):
    page = TodayPage(SimpleNamespace(add_life_record=lambda *_a, **_kw: QUEUED))
    qtbot.addWidget(page)
    page.user_id = 1
    monkeypatch.setattr("ollama_chat_app.ui.member_today.LifeRecordDialog",
                        lambda *_a, **_kw: _accepted(content="synthetic"))
    monkeypatch.setattr(page, "refresh", _forbidden)
    page.record_changed.connect(_forbidden)
    page._confirm_proposals(1, [{"category": "water"}])
    assert page.status_label.text() == QUEUED_MESSAGE


@pytest.mark.parametrize("action", ["add_record", "add_reminder"])
def test_legacy_record_forms_preserve_queued_status(qtbot, monkeypatch, action):
    health = SimpleNamespace(add_life_record=lambda *_a, **_kw: QUEUED,
                             add_reminder=lambda *_a, **_kw: QUEUED)
    page = HealthRecordsPanel(health)
    qtbot.addWidget(page)
    page.user_id = 1
    for name in ("LifeRecordDialog", "ReminderEditorDialog"):
        monkeypatch.setattr("ollama_chat_app.ui.health_records_panel." + name,
                            lambda *_a, **_kw: _accepted(content="synthetic"))
    for name in ("refresh_records", "refresh_reminders", "refresh_statistics"):
        monkeypatch.setattr(page, name, _forbidden)
    page.record_changed.connect(_forbidden)
    page.reminder_changed.connect(_forbidden)
    getattr(page, action)()
    assert page.status_label.text() == QUEUED_MESSAGE


def test_statistics_record_queue_does_not_refresh_or_emit(qtbot, monkeypatch):
    editor = ProfilePage(SimpleNamespace())
    qtbot.addWidget(editor)
    page = ProfileStatisticsPage(
        SimpleNamespace(add_life_record=lambda *_a, **_kw: QUEUED), editor
    )
    qtbot.addWidget(page)
    page.user_id = 1
    monkeypatch.setattr("ollama_chat_app.ui.member_statistics.LifeRecordDialog",
                        lambda *_a, **_kw: _accepted(content="synthetic"))
    monkeypatch.setattr(page, "refresh", _forbidden)
    page.record_changed.connect(_forbidden)
    page.add_record()
    assert page.status_label.text() == QUEUED_MESSAGE


def test_profile_queue_retains_editor_without_claiming_cloud_save(qtbot):
    page = ProfilePage(SimpleNamespace(save_profile=lambda *_a, **_kw: QUEUED))
    qtbot.addWidget(page)
    page.user_id = 1
    page.full_name_input.setText("synthetic-member")
    page.profile_changed.connect(_forbidden)
    page.save()
    assert page.status_label.text() == QUEUED_MESSAGE
    assert page.full_name_input.text() == "synthetic-member"


def test_appointment_queue_never_claims_operator_received_request(qtbot, monkeypatch):
    page = ServicePage(SimpleNamespace(), appointment_service=SimpleNamespace(
        create_request=lambda *_a, **_kw: QUEUED))
    qtbot.addWidget(page)
    page.user_id = 1
    monkeypatch.setattr("ollama_chat_app.ui.health_workspace.AppointmentDialog",
                        lambda *_a, **_kw: _accepted(address="synthetic-address"))
    monkeypatch.setattr(page, "refresh", _forbidden)
    messages = []
    monkeypatch.setattr(QMessageBox, "information", lambda _p, title, text: messages.append(
        (title, text)))
    page._request_appointment()
    assert messages == [("已暂存待同步", QUEUED_MESSAGE)]


def test_preference_queue_never_emits_a_fake_preferences_dictionary(qtbot):
    previous = {**DEFAULT_PREFERENCES, "weather_consent": True,
                "ai_context_consent": True, "ai_context_consent_decided": True}
    patches = []
    prefs = SimpleNamespace(get=lambda _actor: previous,
                            update=lambda _actor, patch: patches.append(patch) or QUEUED)
    page = MyPlatformPanel(SimpleNamespace(get_service_summary=lambda _actor: {}), prefs)
    qtbot.addWidget(page)
    page.set_user(1)
    page.font_size.setValue(previous["font_size"] + 2)
    page.preferences_changed.connect(_forbidden)
    page._save()
    assert page.status.text() == QUEUED_MESSAGE
    assert patches == [{"font_size": previous["font_size"] + 2}]


class SyntheticSync(QObject):
    outbox_changed = Signal(dict)
    status_changed = Signal(dict)
    authorization_lost = Signal(object)
    reauthentication_required = Signal(str)

    def __init__(self):
        super().__init__()
        self.identity = ("synthetic-endpoint", "synthetic-instance", 1)
        self.authorized = True
        self.online = True
        self.fresh = True
        self.calls = []
        self.rows = [{"operation_id": status, "service": "health", "method": "save_profile",
                      "created_at": "2026-09-08T00:00:00+00:00", "status": status,
                      "error_code": "conflict" if status == "conflict" else ""}
                     for status in ("queued", "uncertain", "conflict", "rejected")]

    def state(self):
        return {"authorized": self.authorized, "state": "online" if self.online else "offline",
                "fresh": self.fresh}

    def pending_operations(self):
        return self.rows

    def get_pending(self, identifier):
        self.calls.append(("view", identifier))
        row = next(item for item in self.rows if item["operation_id"] == identifier)
        return {**row, "args": [1], "kwargs": {"display_name": "synthetic-local-name"}}

    def discard_pending(self, identifier):
        self.calls.append(("discard", identifier))
        self.rows = [row for row in self.rows if row["operation_id"] != identifier]

    def rebase_pending(self, identifier):
        self.calls.append(("rebase", identifier))

    def sync_now(self):
        self.calls.append(("continue",))


def test_outbox_uncertain_keeps_original_request_and_cannot_discard(qtbot):
    sync = SyntheticSync()
    dialog = OutboxDialog(sync)
    qtbot.addWidget(dialog)
    dialog.table.selectRow(1)
    assert dialog.continue_button.isEnabled()
    assert not dialog.rebase_button.isEnabled()
    assert not dialog.discard_button.isEnabled()
    assert not dialog.keep_cloud_button.isEnabled()
    dialog.continue_button.click()
    dialog._discard(keep_cloud=False)
    assert sync.calls == [("continue",)]


def test_outbox_rebase_requires_fresh_online_state_and_explicit_confirmation(qtbot, monkeypatch):
    sync = SyntheticSync()
    dialog = OutboxDialog(sync)
    qtbot.addWidget(dialog)
    dialog.table.selectRow(2)
    sync.fresh = False
    dialog._update_actions()
    assert not dialog.rebase_button.isEnabled()
    sync.fresh = True
    dialog._update_actions()
    assert dialog.rebase_button.isEnabled()
    monkeypatch.setattr(QMessageBox, "question", lambda *_a: QMessageBox.StandardButton.No)
    dialog.rebase_button.click()
    assert sync.calls == [("view", "conflict")]
    monkeypatch.setattr(QMessageBox, "question", lambda *_a: QMessageBox.StandardButton.Yes)
    dialog.rebase_button.click()
    qtbot.waitUntil(lambda: dialog._task is None)
    assert ("rebase", "conflict") in sync.calls


def test_keep_cloud_only_discards_selected_local_intent_after_confirmation(qtbot, monkeypatch):
    sync = SyntheticSync()
    dialog = OutboxDialog(sync)
    qtbot.addWidget(dialog)
    dialog.table.selectRow(2)
    monkeypatch.setattr(QMessageBox, "question", lambda *_a: QMessageBox.StandardButton.Yes)
    dialog.keep_cloud_button.click()
    qtbot.waitUntil(lambda: dialog._task is None)
    assert sync.calls == [("discard", "conflict")]
    assert len(sync.rows) == 3


@pytest.mark.parametrize("revoke", ["authorization", "identity", "reauthentication"])
def test_outbox_hides_existing_private_content_when_session_changes(qtbot, revoke):
    sync = SyntheticSync()
    dialog = OutboxDialog(sync)
    qtbot.addWidget(dialog)
    dialog.table.selectRow(0)
    dialog.view_button.click()
    assert "synthetic-local-name" in dialog.details.toPlainText()
    if revoke == "authorization":
        sync.authorized = False
        sync.authorization_lost.emit("expired")
    elif revoke == "identity":
        sync.identity = ("synthetic-other", "synthetic-instance", 2)
        sync.outbox_changed.emit({})
    else:
        sync.reauthentication_required.emit("online again")
    assert dialog.table.rowCount() == 0 and not dialog.details.toPlainText()
    assert not dialog.view_button.isEnabled()
    sync.authorized = True
    dialog.refresh()
    assert dialog.table.rowCount() == 0  # An old dialog cannot revive after new login.
