"""New schema/category/batch UI behavior, entirely synthetic and network-free."""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PySide6.QtCore import QItemSelectionModel
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QDialogButtonBox, QMessageBox, QPushButton, QScrollArea

from ollama_chat_app.data.database import Database, timestamp_to_db
from ollama_chat_app.data.experience_schema import migrate_experience
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.health import (
    HealthRecordNotFoundError,
    HealthService,
    HealthValidationError,
    ReminderNotFoundError,
)
from ollama_chat_app.services.member_statistics import PARAMETERS, daily_series
from ollama_chat_app.ui.health_records_panel import (
    CATEGORIES,
    HealthRecordsPanel,
    LifeRecordDialog,
    ReminderEditorDialog,
)
from ollama_chat_app.ui.member_today import TodayPage


def legacy_database(path):
    stamp = timestamp_to_db(datetime.now(UTC))
    with sqlite3.connect(path) as connection:
        Database._create_schema_v5(connection)
        migrate_experience(connection)
        connection.execute("PRAGMA user_version=6")
        connection.execute(
            "INSERT INTO users(id,username,username_normalized,password_hash,"
            "created_at,updated_at) "
            "VALUES(17,'synthetic-old','synthetic-old','synthetic-unused-hash',?,?)",
            (stamp, stamp),
        )
        connection.execute(
            "INSERT INTO life_records(id,user_id,category,occurred_at,local_date,"
            "timezone_offset_minutes,content,details_json,created_at,updated_at) "
            "VALUES(41,17,'environment',?,?,480,'合成旧环境事实','{\"temperature_c\":0}',?,?)",
            (stamp, stamp[:10], stamp, stamp),
        )
        connection.execute("CREATE INDEX synthetic_custom_record_index ON life_records(content)")
    return Database(path)


def dump_rows(path):
    with sqlite3.connect(path) as connection:
        return {
            row[0]: connection.execute('SELECT * FROM "' + row[0] + '" ORDER BY 1').fetchall()
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }


def test_v6_upgrade_retains_every_row_and_index_then_accepts_medical(tmp_path):
    database = legacy_database(tmp_path / "legacy.db")
    before = dump_rows(database.path)
    database.initialize()
    assert dump_rows(database.path) == before
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='synthetic_custom_record_index'"
        ).fetchone()
    health = HealthService(database)
    record = health.add_life_record(
        17,
        "medical",
        datetime.now(UTC),
        "合成复查事实",
        details={"event_type": "consultation", "name": "社区门诊"},
    )
    assert record > 41
    database.initialize()
    assert len(health.list_life_records(17)) == 2


def test_v6_upgrade_failure_rolls_back_schema_and_rows(tmp_path, monkeypatch):
    from ollama_chat_app.data import medical_schema

    database = legacy_database(tmp_path / "rollback.db")
    before = dump_rows(database.path)
    original = medical_schema.migrate_medical

    def fail_after_replacement(connection):
        original(connection)
        raise sqlite3.DatabaseError("synthetic failure after replacement")

    monkeypatch.setattr(medical_schema, "migrate_medical", fail_after_replacement)
    with pytest.raises(sqlite3.DatabaseError):
        database.initialize()
    assert dump_rows(database.path) == before
    with sqlite3.connect(database.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert (
            "'medical'"
            not in connection.execute(
                "SELECT sql FROM sqlite_schema WHERE name='life_records'"
            ).fetchone()[0]
        )


@pytest.fixture
def context(tmp_path):
    database = Database(tmp_path / "synthetic.db")
    database.initialize()
    auth = AuthService(database)
    own = auth.register("synthetic-own", "synthetic-only-password-1234").id
    other = auth.register("synthetic-other", "synthetic-only-password-1234").id
    return database, HealthService(database), own, other


def test_medical_and_environment_statistics_are_factual_and_user_scoped(context):
    _db, health, own, other = context
    now = datetime.now().astimezone()
    for kind in ("consultation", "medication"):
        health.add_life_record(
            own,
            "medical",
            now,
            "合成既有事实",
            details={"event_type": kind, "name": "本人记录名称", "description": "仅记录已发生事项"},
        )
    health.add_life_record(own, "environment", now, "合成室温", details={"temperature_c": 0})
    health.add_life_record(other, "medical", now, "不可见他人事实")
    stats = health.get_record_statistics(own)
    assert stats["by_category"]["medical"] == 2 and stats["by_category"]["environment"] == 1
    assert (
        stats["metrics"]["medical_consultation_count"]
        == stats["metrics"]["medical_medication_count"]
        == 1
    )
    assert stats["metrics"]["environment_average_temperature_c"] == 0
    assert set(PARAMETERS) == {code for code, _label in CATEGORIES}
    records = health.list_life_records(own)
    series = daily_series(records, "medical", "medication_count", now.date(), 1)
    assert series[0]["value"] == 1


@pytest.mark.parametrize(
    "details",
    [
        {"event_type": "prescribe"},
        {"event_type": True},
        {"name": 5},
        {"name": "x" * 201},
        {"description": "x" * 2001},
        {"dosage_recommendation": "unsafe"},
    ],
)
def test_medical_details_reject_invalid_or_decision_fields(context, details):
    _db, health, own, _other = context
    with pytest.raises(HealthValidationError):
        health.add_life_record(own, "medical", datetime.now(UTC), "合成", details=details)
    assert health.list_life_records(own) == []


@pytest.mark.parametrize("kind", ["record", "reminder"])
def test_bulk_delete_is_atomic_owned_and_audited(context, kind):
    database, health, own, other = context
    create = (
        (lambda user: health.add_life_record(user, "medical", datetime.now(UTC), "合成事实"))
        if kind == "record"
        else (lambda user: health.add_reminder(user, "合成提醒", datetime.now(UTC)))
    )
    delete = health.delete_life_records if kind == "record" else health.delete_reminders
    listing = health.list_life_records if kind == "record" else health.list_reminders
    ids = [create(own), create(own)]
    outsider = create(other)
    before = dump_rows(database.path)
    with pytest.raises((HealthRecordNotFoundError, ReminderNotFoundError)):
        delete(own, [ids[0], outsider])
    assert dump_rows(database.path) == before
    result = delete(own, ids)
    assert result == {"deleted_ids": ids, "count": 2}
    assert listing(own) == [] and len(listing(other)) == 1
    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM audit_logs WHERE actor_user_id=? AND action LIKE '%.deleted'",
                (own,),
            ).fetchone()[0]
            == 2
        )


@pytest.mark.parametrize("ids", [[], [True], [-1], [0], [1, 1], [2**63], list(range(1, 102)), "1"])
def test_invalid_bulk_selection_refused(context, ids):
    _db, health, own, _other = context
    with pytest.raises(HealthValidationError):
        health.delete_life_records(own, ids)


def test_reminder_repeat_roundtrip_keep_clear_and_unknown_refusal(context):
    _db, health, own, _other = context
    for rule in (None, "none", "daily", "weekly", "monthly"):
        identifier = health.add_reminder(own, "合成提醒", datetime.now(UTC), repeat_rule=rule)
        value = health.update_reminder(own, identifier, title="合成标题改动")
        assert value["repeat_rule"] == (rule or "none")
        assert "paused_until" in value
        assert health.update_reminder(own, identifier, repeat_rule=None)["repeat_rule"] == "none"
    with pytest.raises(HealthValidationError):
        health.add_reminder(own, "合成提醒", datetime.now(UTC), repeat_rule="weekdays")


def test_six_equal_today_buttons_and_medical_dialog_roundtrip(qtbot, context):
    _db, health, own, _other = context
    page = TodayPage(health)
    qtbot.addWidget(page)
    page.set_user(own)
    assert {
        button.objectName().removeprefix("RecordCategory_")
        for button in page.findChildren(QPushButton)
        if button.objectName().startswith("RecordCategory_")
    } == {code for code, _label in CATEGORIES}
    dialog = LifeRecordDialog("medical")
    qtbot.addWidget(dialog)
    dialog.medical_name.setText("合成门诊")
    dialog.medical_description.setText("合成复查")
    dialog.content_input.setPlainText("仅记录既有事项")
    values = dialog.values
    assert values["category"] == "medical" and values["details"]["event_type"] == "consultation"
    health.add_life_record(own, **values)
    page.refresh()
    assert "医疗" in page.records_table.item(0, 1).text()


def test_panel_multiple_selection_delete_and_cancel(qtbot, context, monkeypatch):
    _db, health, own, _other = context
    for _ in range(3):
        health.add_life_record(own, "medical", datetime.now(UTC), "合成事实")
    panel = HealthRecordsPanel(health)
    qtbot.addWidget(panel)
    panel.set_user(own)
    selection = panel.record_table.selectionModel()
    for row in (0, 2):
        selection.select(
            panel.record_table.model().index(row, 0),
            QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
        )
    prompts = []
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.No)
    panel.delete_selected_record()
    assert len(health.list_life_records(own)) == 3

    def confirmed(*args):
        prompts.append(args[2])
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", confirmed)
    panel.delete_selected_record()
    assert "2 条" in prompts[0] and len(health.list_life_records(own)) == 1


def test_reminders_multi_selection_keeps_unselected(qtbot, context, monkeypatch):
    _db, health, own, _other = context
    for index in range(3):
        health.add_reminder(own, f"合成喝水 {index}", datetime.now(UTC))
    panel = HealthRecordsPanel(health)
    qtbot.addWidget(panel)
    panel.set_user(own)
    panel.tabs.setCurrentIndex(2)
    selection = panel.reminder_table.selectionModel()
    for row in (0, 2):
        selection.select(
            panel.reminder_table.model().index(row, 0),
            QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
        )
    kept = panel.reminder_table.item(1, 0).data(256)["id"]
    prompts = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: prompts.append(_args[2]) or QMessageBox.StandardButton.Yes,
    )
    panel.delete_selected_reminder()
    assert "2 条" in prompts[0]
    assert [item["id"] for item in health.list_reminders(own)] == [kept]


def test_medical_and_reminder_dialogs_large_font_short_height(qtbot, qapp, tmp_path):
    # Qt's offscreen plugin has no OS font discovery on Windows; load an installed
    # font for honest readable screenshots instead of passing tofu glyphs as QA.
    font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc"
    if "Microsoft YaHei UI" not in QFontDatabase.families() and font.is_file():
        assert QFontDatabase.addApplicationFont(str(font)) >= 0
    output = Path(os.environ.get("MEDICAL_UI_REVIEW_OUTPUT", str(tmp_path / "renders")))
    output.mkdir(parents=True, exist_ok=True)
    for name, dialog in (
        ("medical", LifeRecordDialog("medical")),
        ("reminder", ReminderEditorDialog()),
    ):
        qtbot.addWidget(dialog)
        dialog.setStyleSheet('QWidget { font-family: "Microsoft YaHei UI"; font-size: 24px; }')
        dialog.resize(760, 460)
        dialog.show()
        qapp.processEvents()
        assert dialog.width() <= 760 and dialog.height() <= 460
        buttons = dialog.findChild(QDialogButtonBox)
        assert buttons.isVisible()
        assert buttons.mapTo(dialog, buttons.rect().bottomRight()).y() < dialog.height()
        scroll = dialog.findChild(QScrollArea)
        assert scroll.verticalScrollBar().maximum() > 0
        assert dialog.grab().save(str(output / f"{name}-top-760x460-font24.png"))
        if name == "medical":
            scroll.ensureWidgetVisible(dialog.medical_description)
        else:
            scroll.ensureWidgetVisible(dialog.repeat_combo)
        qapp.processEvents()
        assert dialog.grab().save(str(output / f"{name}-fields-760x460-font24.png"))
        dialog.close()


@pytest.mark.parametrize("version", range(1, 7))
def test_backup_keeps_exact_legacy_schema_signatures(version):
    from ollama_chat_app.services.backup import _expected_schema_signature, _schema_signature

    with sqlite3.connect(":memory:") as connection:
        getattr(Database, f"_create_schema_v{min(version, 5)}")(connection)
        if version == 6:
            migrate_experience(connection)
        assert _schema_signature(connection) == _expected_schema_signature(version)


def test_backup_current_signature_matches_actual_v7_and_refuses_extra_schema(context):
    from ollama_chat_app.services.backup import BackupValidationError, _validate_database

    database, _health, _own, _other = context
    assert _validate_database(database.path).schema_version == 7
    with database.connect() as connection:
        connection.execute("CREATE TABLE synthetic_unreviewed(id INTEGER)")
        connection.commit()
    with pytest.raises(BackupValidationError, match="结构"):
        _validate_database(database.path)
