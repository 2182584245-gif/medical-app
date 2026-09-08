from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QPlainTextEdit, QSpinBox, QTextEdit

from ollama_chat_app.data.database import Database
from ollama_chat_app.services.ai_assistant import AiAssistantService
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.health import HealthService
from ollama_chat_app.time_utils import as_beijing
from ollama_chat_app.ui.health_records_panel import LifeRecordDialog
from ollama_chat_app.ui.member_appointment import AppointmentDialog
from ollama_chat_app.ui.member_today import TodayPage
from ollama_chat_app.ui.voice_input import VoiceInputInstaller, VoiceTextDialog


@pytest.fixture
def voice_context(tmp_path):
    database = Database(tmp_path / "synthetic-voice.db")
    auth = AuthService(database)
    member = auth.register("synthetic-speaker", "test-password-123")
    health = HealthService(database)
    return SimpleNamespace(
        db=database, member=member, health=health, ai=AiAssistantService(database, health)
    )


def test_four_category_ai_proposals_keep_units_time_location_and_require_confirmation(
    qtbot,
    voice_context,
    monkeypatch,
):
    context = voice_context
    stamp = "2026-09-08T10:30:00+08:00"
    proposals = [
        {
            "type": "life_record",
            "category": "diet",
            "occurred_at": stamp,
            "content": "午餐吃米饭150克",
            "details": {"amount_g": 150, "calories_kcal": 200, "location": "家中"},
        },
        {
            "type": "life_record",
            "category": "water",
            "occurred_at": stamp,
            "content": "喝水300毫升",
            "details": {"amount_ml": 300, "location": "家中"},
        },
        {
            "type": "life_record",
            "category": "activity",
            "occurred_at": stamp,
            "content": "在公园散步30分钟",
            "details": {"duration_minutes": 30, "energy_kcal": 100, "location": "公园"},
        },
        {
            "type": "life_record",
            "category": "sleep",
            "occurred_at": stamp,
            "content": "昨晚睡了7.5小时",
            "details": {"duration_hours": 7.5, "location": "家中"},
        },
    ]
    captured = []

    class Provider:
        def chat(self, model, messages):
            captured.extend(messages)
            return json.dumps({"answer": "请逐项核对", "proposals": proposals}, ensure_ascii=False)

    suggestions = context.ai.propose_life_records(
        context.member.id,
        Provider(),
        "synthetic",
        "synthetic-model",
        "合成描述，按测试数据填写四项",
    )
    assert len(suggestions) == 4
    assert context.health.list_life_records(context.member.id) == []
    with context.db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM ai_insights").fetchone()[0] == 0
    assert len(captured) == 2

    def accept_proposal(dialog):
        assert as_beijing(dialog.values["occurred_at"]).hour == 10
        assert dialog.location_input.text() in {"公园", "家中"}
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(LifeRecordDialog, "exec", accept_proposal)
    page = TodayPage(context.health)
    qtbot.addWidget(page)
    page.set_user(context.member.id)
    page._confirm_proposals(context.member.id, suggestions)
    rows = context.health.list_life_records(context.member.id)
    assert {row["category"] for row in rows} == {"diet", "water", "activity", "sleep"}
    assert all(row["source"] == "ai_confirmed" for row in rows)
    values = {row["category"]: row["details"] for row in rows}
    assert values["water"]["amount_ml"] == 300
    assert values["activity"]["duration_minutes"] == 30
    assert values["activity"]["energy_kcal"] == 100
    assert values["activity"]["location"] == "公园"
    assert values["sleep"]["duration_hours"] == 7.5
    assert values["diet"]["amount_g"] == 150


def test_voice_controls_exclude_credentials_and_fill_only_confirmed_text(qtbot, monkeypatch):
    installer = VoiceInputInstaller()
    try:
        text_fields = [QLineEdit(), QTextEdit(), QPlainTextEdit()]
        for field in text_fields:
            qtbot.addWidget(field)
            installer._attach(field)
            assert field.property("voice_input_attached") is True
        password = QLineEdit()
        password.setEchoMode(QLineEdit.EchoMode.Password)
        qtbot.addWidget(password)
        installer._attach(password)
        assert password.property("sensitive_input") is True
        assert not password.property("voice_input_attached")
        secret = QLineEdit()
        secret.setProperty("sensitive_input", True)
        qtbot.addWidget(secret)
        installer._attach(secret)
        assert not secret.property("voice_input_attached")
        spin = QSpinBox()
        qtbot.addWidget(spin)
        installer._attach(spin.lineEdit())
        assert not spin.lineEdit().property("voice_input_attached")
        field = text_fields[0]
        field.setText("原文")
        monkeypatch.setattr(VoiceTextDialog, "exec", lambda dialog: QDialog.DialogCode.Rejected)
        installer._fill(field)
        assert field.text() == "原文"

        def accepted(dialog):
            dialog.text.setPlainText("合成转写")
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(VoiceTextDialog, "exec", accepted)
        installer._fill(field)
        assert field.text() == "原文合成转写"
    finally:
        QApplication.instance().removeEventFilter(installer)


def test_map_failure_keeps_explicit_address_confirmation_available(qtbot):
    dialog = AppointmentDialog()
    qtbot.addWidget(dialog)
    dialog.address.setText("上海市合成测试街道100号")
    dialog._map_loaded(False)
    assert "地图暂不可用" in dialog.map_status.text()
    dialog.address_confirmed.setChecked(True)
    dialog.validate()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.values["address"] == "上海市合成测试街道100号"
    assert dialog.values["latitude"] is None
    dialog.address.setText("上海市合成测试街道200号")
    assert not dialog.address_confirmed.isChecked()
