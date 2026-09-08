"""Shared editable date controls with an explicit Beijing timezone."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QDate, QDateTime, QEvent, QLocale, QObject, Qt, QTimeZone
from PySide6.QtGui import QKeyEvent, QKeySequence
from PySide6.QtWidgets import QDateEdit, QDateTimeEdit, QWidget

from ..time_utils import (
    as_beijing,
    beijing_now,
    beijing_today,
    display_timezone,
    display_timezone_label,
    display_timezone_name,
)

BEIJING_QTIMEZONE = QTimeZone.fromSecondsAheadOfUtc(8 * 60 * 60)
CHINESE_LOCALE = QLocale("zh_CN")
EMPTY_DATE = QDate(1899, 12, 31)


def current_qtimezone() -> QTimeZone:
    return QTimeZone(display_timezone_name().encode("utf-8"))


def beijing_qdatetime(value: datetime | str | QDateTime | None = None) -> QDateTime:
    if isinstance(value, QDateTime):
        if not value.isValid():
            raise ValueError("请选择有效日期和时间")
        return value.toTimeZone(current_qtimezone())
    parsed = beijing_now() if value is None else as_beijing(value)
    return QDateTime.fromMSecsSinceEpoch(int(parsed.timestamp() * 1000), current_qtimezone())


def datetime_iso(value: QDateTime) -> str:
    """Avoid Qt local ISO strings/toPython(), which can omit timezone information."""
    if not value.isValid():
        raise ValueError("请选择有效日期和时间")
    parsed = datetime.fromtimestamp(value.toMSecsSinceEpoch() / 1000, display_timezone())
    return parsed.isoformat(timespec="seconds")


def set_editor_datetime(editor: QDateTimeEdit, value: datetime | str | QDateTime) -> None:
    editor.setTimeZone(current_qtimezone())
    editor.setDateTime(beijing_qdatetime(value))


class _DateEntryMixin:
    """Allow normal yyyy-MM-dd HH:mm typing despite Qt's automatic section advance."""

    _pending_separator = ""

    def keyPressEvent(self, event: QKeyEvent) -> None:
        text = event.text()
        if self._pending_separator and text == self._pending_separator:
            self._pending_separator = ""
            event.accept()
            return
        self._pending_separator = ""
        if text.isdigit() and self.lineEdit().selectedText() == self.lineEdit().text():
            self.setSelectedSection(QDateTimeEdit.Section.YearSection)
        before = self.currentSection()
        super().keyPressEvent(event)
        if text.isdigit() and before != self.currentSection():
            self._pending_separator = {
                QDateTimeEdit.Section.YearSection: "-",
                QDateTimeEdit.Section.MonthSection: "-",
                QDateTimeEdit.Section.DaySection: " ",
                QDateTimeEdit.Section.HourSection: ":",
            }.get(before, "")


class BeijingDateTimeEdit(_DateEntryMixin, QDateTimeEdit):
    def __init__(
        self,
        value: datetime | str | QDateTime | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setLocale(CHINESE_LOCALE)
        self.setTimeZone(current_qtimezone())
        self.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.setCalendarPopup(True)
        self.setKeyboardTracking(False)
        self.setMinimumDate(QDate(1900, 1, 1))
        self.setMinimumWidth(260)
        self.setMinimumHeight(40)
        self.setToolTip("时间按右上角所选时区显示，默认北京时间；可输入或点击日历选择日期。")
        self.setAccessibleName(f"日期和时间（{display_timezone_label()}）")
        self.setDateTime(beijing_qdatetime(value))
        self.calendarWidget().setLocale(CHINESE_LOCALE)

    def dateTime(self) -> QDateTime:
        # Save actions also work when the user has not tabbed out of the last field.
        self.interpretText()
        return super().dateTime()


class OptionalDateEdit(_DateEntryMixin, QDateEdit):
    """Calendar-backed optional civil date; an unset value never invents a birthday."""

    def __init__(self, parent: QWidget | None = None, *, past_only: bool = False) -> None:
        super().__init__(parent)
        self.setLocale(CHINESE_LOCALE)
        self.setDisplayFormat("yyyy-MM-dd")
        self.setCalendarPopup(True)
        self.setKeyboardTracking(False)
        self.setMinimumDate(EMPTY_DATE)
        if past_only:
            self.setMaximumDate(QDate(beijing_today()))
        self.setSpecialValueText("未填写")
        self.setDate(EMPTY_DATE)
        self.setMinimumWidth(260)
        self.setMinimumHeight(40)
        self.setToolTip("选填；可输入年月日或打开日历选择日期，删除文字可清空。")
        calendar = self.calendarWidget()
        calendar.installEventFilter(self)
        calendar.setLocale(CHINESE_LOCALE)
        today = beijing_today()
        calendar.setCurrentPage(today.year, today.month)

    def iso_value(self) -> str | None:
        if not self.lineEdit().text().strip():
            self.setDate(EMPTY_DATE)
        self.interpretText()
        return None if self.date() == EMPTY_DATE else self.date().toString(Qt.DateFormat.ISODate)

    def set_iso_value(self, value: str | None) -> None:
        parsed = QDate.fromString(str(value or ""), Qt.DateFormat.ISODate)
        if parsed.isValid() and parsed > self.maximumDate():
            # Preserve any legacy out-of-range value on load; normal validation
            # will ask for a correction instead of silently saving today's date.
            self.setMaximumDate(parsed)
        self.setDate(parsed if parsed.isValid() else EMPTY_DATE)

    def clear(self) -> None:
        self.setDate(EMPTY_DATE)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            watched is self.calendarWidget()
            and event.type() == QEvent.Type.Show
            and self.date() == EMPTY_DATE
        ):
            today = beijing_today()
            self.calendarWidget().setCurrentPage(today.year, today.month)
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self.date() == EMPTY_DATE and (
            event.text().isdigit() or event.matches(QKeySequence.StandardKey.Paste)
        ):
            # Qt's special-value label cannot accept a typed year directly.
            self.setDate(QDate(beijing_today()))
            self.setSelectedSection(QDateTimeEdit.Section.YearSection)
        if event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete) and (
            self.lineEdit().selectedText() == self.lineEdit().text()
        ):
            self.clear()
            event.accept()
            return
        super().keyPressEvent(event)
