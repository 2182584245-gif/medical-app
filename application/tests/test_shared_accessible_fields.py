from datetime import datetime

import pytest
from PySide6.QtCore import QDate, QTimeZone
from PySide6.QtWidgets import (
    QDateEdit,
    QDateTimeEdit,
    QDialogButtonBox,
    QPushButton,
    QToolButton,
    QWidget,
)

from ollama_chat_app.ui.address_fields import AddressFields, ServiceTypeCombo
from ollama_chat_app.ui.china_regions import REGIONS, SOURCE_LICENSE
from ollama_chat_app.ui.member_appointment import AppointmentDialog
from ollama_chat_app.ui.operator_workspace import MembershipDialog
from ollama_chat_app.ui.segmented_time import SegmentedDateEdit, SegmentedDateTimeEdit
from ollama_chat_app.ui.theme import PALETTES, RECORD_TONES, build_style
from ollama_chat_app.ui.time_fields import beijing_qdatetime, datetime_iso
from ollama_chat_app.ui.time_phrase import parse_time_phrase
from ollama_chat_app.ui.voice_input import VoiceInputInstaller


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("明天下午三点半", "2026-09-10T15:30"),
        ("二零二六年十月二十一日上午九点五分", "2026-10-21T09:05"),
        ("2026年9月9日星期三18:20", "2026-09-09T18:20"),
        ("下周一", "2026-09-14T08:10"),
    ],
)
def test_local_speech_parser(phrase, expected):
    result = parse_time_phrase(phrase, datetime(2026, 9, 9, 8, 10))
    assert result.isoformat(timespec="minutes") == expected


@pytest.mark.parametrize(
    "phrase", ["2026年2月30日", "上午十三点", "25:30", "明天几点", "2026年9月9日星期一"]
)
def test_speech_rejects_invalid_or_contradictory_values(phrase):
    if phrase == "明天几点":
        # An explicitly supplied relative date is valid; unspecified time is retained.
        assert parse_time_phrase(phrase, datetime(2026, 9, 9, 8, 10)).hour == 8
    else:
        with pytest.raises(ValueError):
            parse_time_phrase(phrase, datetime(2026, 9, 9, 8, 10))


def test_segmented_unfocused_typing_and_derived_weekday(qtbot):
    editor = SegmentedDateTimeEdit("2026-09-09T08:10:00+08:00")
    qtbot.addWidget(editor)
    editor.year.setCurrentText("2028")
    editor.month.setCurrentText("2")
    editor.day.setCurrentText("29")
    editor.hour.setCurrentText("21")
    editor.minute.setCurrentText("45")
    assert datetime_iso(editor.dateTime()) == "2028-02-29T21:45:00+08:00"
    assert editor.weekday.text() == "星期二"
    editor.year.setCurrentText("2027")
    with pytest.raises(ValueError):
        editor.dateTime()


def test_segmented_numeric_fields_do_not_receive_generic_voice_overlay(qtbot, qapp):
    installer = VoiceInputInstaller(qapp)
    try:
        editor = SegmentedDateTimeEdit()
        qtbot.addWidget(editor)
        editor.show()
        qapp.processEvents()
        assert not editor.findChildren(QToolButton, "InlineVoiceInput")
        assert editor.voice_button.isVisible()
    finally:
        qapp.removeEventFilter(installer)


def test_hidden_timezone_updates_preserve_instant_and_birthday(qtbot):
    parent = QWidget()
    qtbot.addWidget(parent)
    editor = SegmentedDateTimeEdit("2026-11-01T06:30:37Z", parent)
    date = SegmentedDateEdit(parent, past_only=True)
    date.set_iso_value("1960-01-01")
    instant = editor.dateTime().toMSecsSinceEpoch()
    zone = QTimeZone(b"America/New_York")
    # Same integration path as MainWindow: only real datetime editors, no civil dates.
    for child in parent.findChildren(QDateTimeEdit):
        if isinstance(child, QDateEdit):
            continue
        value = child.dateTime()
        child.setTimeZone(zone)
        child.setDateTime(value.toTimeZone(zone))
    assert editor.hour.currentText() == "1"
    assert editor.dateTime().toMSecsSinceEpoch() == instant
    assert date.iso_value() == "1960-01-01"


def test_dst_gap_is_rejected_and_voice_only_fills(qtbot):
    editor = SegmentedDateTimeEdit("2026-03-07T12:10:00Z")
    qtbot.addWidget(editor)
    editor.setTimeZone(QTimeZone(b"America/New_York"))
    with pytest.raises(ValueError):
        editor.apply_voice_text("2026年3月8日凌晨两点半")
    editor.setTimeZone(QTimeZone(b"Asia/Shanghai"))
    editor.apply_voice_text("明天下午三点半")
    assert editor.hour.currentText() == "15"
    assert editor.minute.currentText() == "30"
    assert "尚未保存" in editor.notice.text()


def test_optional_date_keeps_empty_real_1900_and_legacy_future(qtbot):
    field = SegmentedDateEdit(past_only=True)
    qtbot.addWidget(field)
    assert field.iso_value() is None
    field.setDate(QDate(1900, 1, 1))
    assert field.iso_value() == "1900-01-01"
    field.clear()
    assert field.iso_value() is None
    field.set_iso_value("2100-01-01")
    assert field.iso_value() == "2100-01-01"


def test_address_requires_corresponding_region_and_free_detail(qtbot):
    field = AddressFields()
    qtbot.addWidget(field)
    assert len(REGIONS) == 34 and SOURCE_LICENSE == "WTFPL-2.0"
    with pytest.raises(ValueError):
        field.validate()
    field.province.setCurrentIndex(field.province.findData("浙江省"))
    assert field.city.findData("杭州市") >= 0
    assert field.city.findData("北京市") < 0
    field.city.setCurrentIndex(field.city.findData("杭州市"))
    with pytest.raises(ValueError):
        field.validate()
    field.detail.setText("家")  # Free text; no invented real-address verification gate.
    assert field.validate() == "浙江省 杭州市 家"
    field.province.setCurrentIndex(field.province.findData("广东省"))
    assert not field.city.currentData()


def test_old_address_is_not_silently_rewritten(qtbot):
    field = AddressFields()
    qtbot.addWidget(field)
    original = "旧地址（待会员核对）"
    field.setText(original)
    assert field.text() == original
    assert field.validate(allow_unchanged_legacy=True) == original
    with pytest.raises(ValueError):
        field.validate()
    field.detail.setText("改动")
    with pytest.raises(ValueError):
        field.validate(allow_unchanged_legacy=True)


def test_custom_service_type_and_appointment_confirmation(qtbot, monkeypatch):
    dialog = AppointmentDialog()
    qtbot.addWidget(dialog)
    assert isinstance(dialog.service_type, ServiceTypeCombo)
    dialog.service_type.setText("到访聊天与整理")
    dialog.address.setText("北京市 海淀区家里")
    dialog.address_confirmed.setChecked(True)
    dialog.scheduled_at.setDateTime(beijing_qdatetime().addDays(2))
    assert dialog.values["service_type"] == "到访聊天与整理"
    assert dialog.map_view is None
    dialog.address.detail.setText("社区活动室")
    assert not dialog.address_confirmed.isChecked()


@pytest.mark.parametrize("kind", ["appointment", "membership"])
def test_short_large_font_dialog_keeps_buttons_reachable(qtbot, qapp, kind):
    old = qapp.styleSheet()
    qapp.setStyleSheet(build_style(24))
    try:
        dialog = AppointmentDialog() if kind == "appointment" else MembershipDialog("合成会员")
        qtbot.addWidget(dialog)
        dialog.resize(760, 460)
        dialog.show()
        qtbot.wait(30)
        assert dialog.height() <= 460
        assert dialog.form_scroll.verticalScrollBar().maximum() > 0
        buttons = dialog.findChild(QDialogButtonBox)
        assert buttons.isVisible()
        assert buttons.mapTo(dialog, buttons.rect().bottomRight()).y() < dialog.height()
        assert dialog.form_scroll.horizontalScrollBar().maximum() == 0
    finally:
        qapp.setStyleSheet(old)


def _luminance(hex_color):
    rgb = [int(hex_color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
    return sum(a * b for a, b in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))


@pytest.mark.parametrize("palette", PALETTES.values())
def test_primary_buttons_and_text_have_readable_contrast(palette):
    accent, _, background, card, _ = palette
    assert 1.05 / (_luminance(accent) + 0.05) >= 4.5
    for surface in (background, card):
        assert (_luminance(surface) + 0.05) / (_luminance("#27362c") + 0.05) >= 7


@pytest.mark.parametrize("colors", RECORD_TONES.values())
def test_six_category_colors_keep_text_high_contrast_even_on_hover(colors):
    surface, ink, _edge, active = colors
    for background in (surface, active):
        assert (_luminance(background) + 0.05) / (_luminance(ink) + 0.05) >= 4.5


def test_six_record_buttons_keep_text_and_distinct_original_icons(qtbot):
    from ollama_chat_app.ui.health_records_panel import CATEGORIES
    from ollama_chat_app.ui.member_today import TodayPage

    page = TodayPage(object())  # Construction only; never requests health/weather data.
    qtbot.addWidget(page)
    art = []
    for code, label in CATEGORIES:
        button = page.findChild(QPushButton, "RecordCategory_" + code)
        assert button.text() == label + "记录"
        assert button.property("recordTone") == code
        assert not button.icon().isNull()
        image = button.icon().pixmap(36, 36).toImage()
        art.append(bytes(image.constBits()))
    assert len(set(art)) == 6 and len(set(v[0] for v in RECORD_TONES.values())) == 6


def test_segmented_accessibility_follows_hidden_timezone_change(qtbot):
    from ollama_chat_app.time_utils import set_display_timezone

    try:
        set_display_timezone("Asia/Shanghai")
        field = SegmentedDateTimeEdit("2026-09-08T12:00:00Z")
        qtbot.addWidget(field)
        original = field.dateTime()
        instant = original.toMSecsSinceEpoch()
        set_display_timezone("America/New_York")
        zone = QTimeZone(b"America/New_York")
        # MainWindow preserves the instant explicitly, as required by Qt's
        # wall-clock-oriented setTimeZone API; the wrapper must track both calls.
        field._backbone.setTimeZone(zone)
        field._backbone.setDateTime(original.toTimeZone(zone))
        assert "纽约时间" in field.accessibleName()
        assert field.timezone_label.text() == "纽约时间"
        assert "纽约时间" in field.hour.accessibleName()
        assert field.dateTime().toMSecsSinceEpoch() == instant
    finally:
        set_display_timezone("Asia/Shanghai")
