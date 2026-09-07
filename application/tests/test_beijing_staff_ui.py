from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QLocale
from PySide6.QtWidgets import QDateTimeEdit, QDialog, QMessageBox

from ollama_chat_app.ui import advisor_workspace, operator_workspace
from ollama_chat_app.ui.advisor_workspace import AdvisorVisitCompletionDialog
from ollama_chat_app.ui.ai_proposals_panel import _proposal_summary
from ollama_chat_app.ui.operator_workspace import (
    BindAdvisorDialog,
    MembershipDialog,
    VisitTaskDialog,
)
from ollama_chat_app.ui.time_fields import (
    BeijingDateTimeEdit,
    beijing_qdatetime,
    set_editor_datetime,
)


@pytest.mark.parametrize(
    "source",
    [
        datetime(2032, 10, 24, 18, 15, tzinfo=UTC),
        "2032-10-24T18:15:00Z",
        "2032-10-24T18:15:00+00:00",
        "2032-10-25T02:15:00+08:00",
    ],
)
def test_membership_load_and_save_preserves_beijing_instant(qtbot, source) -> None:
    dialog = MembershipDialog(
        "会员",
        {
            "membership_id": 31,
            "membership_starts_at": source,
            "membership_ends_at": "2033-10-24T18:15:00Z",
        },
    )
    qtbot.addWidget(dialog)

    assert dialog.start_input.text() == "2032-10-25 02:15"
    assert dialog.end_input.text() == "2033-10-25 02:15"
    assert dialog.values["starts_at"] == "2032-10-25T02:15:00+08:00"
    assert dialog.values["ends_at"] == "2033-10-25T02:15:00+08:00"


@pytest.mark.parametrize(
    ("dialog_kind", "editor_name", "value_key"),
    [
        ("membership", "start_input", "starts_at"),
        ("membership", "end_input", "ends_at"),
        ("binding", "start_input", "started_at"),
        ("visit", "time_input", "scheduled_at"),
        ("completion", "next_visit_input", "next_visit_at"),
    ],
)
def test_every_staff_time_field_supports_keyboard_edit_and_beijing_save(
    qtbot, dialog_kind: str, editor_name: str, value_key: str
) -> None:
    if dialog_kind == "membership":
        dialog = MembershipDialog("会员")
    elif dialog_kind == "binding":
        dialog = BindAdvisorDialog("会员", [{"id": 7, "display_name": "顾问"}])
    elif dialog_kind == "visit":
        dialog = VisitTaskDialog("会员")
    else:
        dialog = AdvisorVisitCompletionDialog({"id": 1, "title": "上门"})
        dialog.create_next_visit_input.setChecked(True)
    qtbot.addWidget(dialog)
    editor = getattr(dialog, editor_name)
    assert isinstance(editor, BeijingDateTimeEdit)
    assert editor.calendarPopup()
    assert editor.locale().language() == QLocale.Language.Chinese
    assert editor.dateTime().offsetFromUtc() == 8 * 60 * 60
    set_editor_datetime(editor, "2030-01-20T09:00:00+08:00")
    dialog.show()
    editor.setFocus()

    for section, text in (
        (QDateTimeEdit.Section.YearSection, "2032"),
        (QDateTimeEdit.Section.MonthSection, "10"),
        (QDateTimeEdit.Section.DaySection, "25"),
        (QDateTimeEdit.Section.HourSection, "16"),
        (QDateTimeEdit.Section.MinuteSection, "45"),
    ):
        editor.setSelectedSection(section)
        qtbot.keyClicks(editor, text)

    # Read values while focus is still in the editor, as when saving with a shortcut.
    assert dialog.values[value_key] == "2032-10-25T16:45:00+08:00"


@pytest.mark.parametrize(
    "display_time", [operator_workspace._display_time, advisor_workspace._display_time]
)
@pytest.mark.parametrize(
    "source",
    [datetime(2032, 10, 24, 18, 15, tzinfo=UTC), "2032-10-24T18:15:00Z"],
)
def test_staff_history_displays_utc_sources_in_beijing(display_time, source) -> None:
    assert display_time(source) == "2032-10-25 02:15"
    assert display_time(None) == "未安排"


@pytest.mark.parametrize(
    ("proposal", "expected"),
    [
        (
            {
                "type": "life_record",
                "category": "water",
                "occurred_at": "2032-10-24T18:15:00Z",
                "content": "喝水",
            },
            "饮水 · 2032-10-25 02:15（北京时间）\n喝水",
        ),
        (
            {"type": "reminder", "title": "复访", "scheduled_at": "2032-10-24T18:15:00Z"},
            "复访 · 2032-10-25 02:15（北京时间）",
        ),
        (
            {
                "type": "advisor_summary",
                "period_start": "2032-10-24T18:15:00Z",
                "period_end": "2032-10-25T18:15:00Z",
                "content": "服务摘要",
            },
            "2032-10-25 02:15 至 2032-10-26 02:15（北京时间）\n服务摘要",
        ),
    ],
)
def test_ai_proposal_times_match_the_beijing_time_to_be_confirmed(proposal, expected) -> None:
    assert _proposal_summary(proposal) == expected


def test_membership_rejects_equal_or_reversed_times(qtbot, monkeypatch) -> None:
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    dialog = MembershipDialog("会员")
    qtbot.addWidget(dialog)
    set_editor_datetime(dialog.start_input, "2032-10-25T16:45:00+08:00")
    for ends_at in ("2032-10-25T16:45:00+08:00", "2032-10-25T16:44:00+08:00"):
        set_editor_datetime(dialog.end_input, ends_at)
        dialog._validate()
        assert dialog.result() == QDialog.DialogCode.Rejected
    assert len(warnings) == 2

    set_editor_datetime(dialog.end_input, "2032-10-25T16:46:00+08:00")
    dialog._validate()
    assert dialog.result() == QDialog.DialogCode.Accepted


@pytest.mark.parametrize("kind", ["visit", "completion"])
def test_visit_validation_compares_future_beijing_instants(qtbot, monkeypatch, kind) -> None:
    current = beijing_qdatetime("2032-10-25T16:45:00+08:00")
    target_module = operator_workspace if kind == "visit" else advisor_workspace
    monkeypatch.setattr(target_module, "beijing_qdatetime", lambda: current)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    if kind == "visit":
        dialog = VisitTaskDialog("会员")
        editor = dialog.time_input
    else:
        dialog = AdvisorVisitCompletionDialog({"id": 1, "title": "上门"})
        dialog.summary_input.setPlainText("已完成上门")
        dialog.create_next_visit_input.setChecked(True)
        editor = dialog.next_visit_input
    qtbot.addWidget(dialog)

    set_editor_datetime(editor, "2032-10-25T08:45:00Z")
    dialog._validate()
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert len(warnings) == 1

    set_editor_datetime(editor, "2032-10-25T08:46:00Z")
    dialog._validate()
    assert dialog.result() == QDialog.DialogCode.Accepted
