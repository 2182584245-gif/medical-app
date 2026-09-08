from __future__ import annotations

import base64
import hashlib
import sqlite3

import pytest

from ollama_chat_app.data.database import Database
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.preferences import PreferencesService
from ollama_chat_app.ui.local_migration_auth import verify_local_source


def test_source_verification_does_not_mutate_and_rejects_operator(tmp_path):
    path = tmp_path / "synthetic-source.db"
    database = Database(path)
    database.initialize()
    auth = AuthService(database)
    member = auth.register("sample-member", "Sample-pass-1234")
    auth.bootstrap_operator("sample-operator", "Sample-pass-1234")
    # Checkpoint before hashing so the evidence covers the complete source file.
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = hashlib.sha256(path.read_bytes()).digest()
    assert verify_local_source(path, "sample-member", "Sample-pass-1234") == member.id
    for username, password in (
        ("sample-member", "incorrect-pass-1234"),
        ("unknown-member", "Sample-pass-1234"),
        ("sample-operator", "Sample-pass-1234"),
    ):
        with pytest.raises(ValueError):
            verify_local_source(path, username, password)
    assert hashlib.sha256(path.read_bytes()).digest() == before


def test_preference_storage_rejects_keys_and_huge_avatar(tmp_path):
    database = Database(tmp_path / "synthetic-preferences.db")
    database.initialize()
    member = AuthService(database).register("preference-member", "Sample-pass-1234")
    preferences = PreferencesService(database)
    initial = preferences.get(member.id)
    assert initial["timezone"] == "Asia/Shanghai"
    assert initial["ai_context_consent"] is False
    assert initial["weather_consent"] is False
    for patch in (
        {"api_key": "synthetic-not-a-real-secret"},
        {"weather_consent": "yes"},
        {"timezone": "not/a-real-zone"},
        {"latitude": float("nan")},
        {"avatar_data": "data:image/png;base64," + base64.b64encode(b"invalid").decode()},
    ):
        with pytest.raises(ValueError):
            preferences.update(member.id, patch)
    assert preferences.get(member.id) == initial


def test_preferences_partial_updates_preserve_unrelated_settings(tmp_path):
    database = Database(tmp_path / "synthetic-preferences.db")
    database.initialize()
    member = AuthService(database).register("preference-member", "Sample-pass-1234")
    preferences = PreferencesService(database)
    preferences.update(member.id, {"preferred_name": "示例王阿姨", "font_size": 24})
    preferences.update(member.id, {"timezone": "America/New_York"})
    result = preferences.get(member.id)
    assert result["preferred_name"] == "示例王阿姨"
    assert result["font_size"] == 24
    assert result["timezone"] == "America/New_York"


def test_time_editors_label_actual_selected_zone_and_keep_instants(qtbot):
    from PySide6.QtWidgets import QLabel

    from ollama_chat_app.time_utils import set_display_timezone
    from ollama_chat_app.ui.health_records_panel import ReminderEditorDialog

    try:
        set_display_timezone("America/New_York")
        dialog = ReminderEditorDialog({"title": "示例提醒", "scheduled_at": "2026-09-08T12:00:00Z"})
        qtbot.addWidget(dialog)
        assert dialog.values["scheduled_at"] == "2026-09-08T08:00:00-04:00"
        assert any("纽约时间" in label.text() for label in dialog.findChildren(QLabel))
        assert "纽约时间" in dialog.time_input.accessibleName()
    finally:
        set_display_timezone("Asia/Shanghai")
