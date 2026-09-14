"""Accessible segmented civil date/time editors with explicit local speech entry.

The hidden Qt editor preserves existing instant/timezone integration. The visible
combos never create a separate weekday value or save a record on their own.
"""

from datetime import datetime

from PySide6.QtCore import QDate, QDateTime, QLocale, Qt, QTime, Signal
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QGridLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QWidget,
)

from ..time_utils import beijing_today
from .time_fields import (
    EMPTY_DATE,
    BeijingDateTimeEdit,
    OptionalDateEdit,
    editor_timezone_label,
)
from .time_phrase import parse_time_phrase


class SegmentedDateTimeEdit(QWidget):
    dateTimeChanged = Signal(QDateTime)
    dateChanged = Signal(QDate)
    timeChanged = Signal(QTime)

    def __init__(self, value=None, parent=None, *, date_only=False, _backbone=None):
        super().__init__(parent)
        self._date_only = date_only
        self._loading = False
        self._backbone = _backbone or BeijingDateTimeEdit(value, self)
        self._backbone.setParent(self)
        self._backbone.hide()
        self.setLocale(QLocale("zh_CN"))
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)
        self.year = self._combo("年", 1900, 9999, range(1900, beijing_today().year + 81))
        self.month = self._combo("月", 1, 12, range(1, 13))
        self.day = self._combo("日", 1, 31, range(1, 32))
        self.weekday = QLabel()
        self.weekday.setAccessibleName("星期，由日期自动计算")
        self.weekday.setObjectName("DerivedWeekday")
        self.weekday.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for column, (label, field) in enumerate(
            (("年", self.year), ("月", self.month), ("日", self.day))
        ):
            grid.addWidget(QLabel(label), 0, column)
            grid.addWidget(field, 1, column)
        grid.addWidget(QLabel("星期（自动）"), 0, 3)
        grid.addWidget(self.weekday, 1, 3)
        self.hour = self._combo("时，24小时制", 0, 23, range(24))
        self.minute = self._combo("分", 0, 59, range(60))
        self.voice_button = QPushButton("语音填时间" if not date_only else "语音填日期")
        self.voice_button.setToolTip("本机识别，仅填写；未提及的部分保留原值，请核对后保存。")
        self.voice_button.clicked.connect(self._voice)
        self._time_labels = [QLabel("时（24小时）"), QLabel("分")]
        for column, field in enumerate((self.hour, self.minute)):
            grid.addWidget(self._time_labels[column], 2, column)
            grid.addWidget(field, 3, column)
            field.setVisible(not date_only)
            self._time_labels[column].setVisible(not date_only)
        grid.addWidget(self.voice_button, 3, 2, 1, 2)
        self.timezone_label = QLabel()
        self.timezone_label.setObjectName("Subtitle")
        self.timezone_label.setWordWrap(True)
        self.timezone_label.setVisible(not date_only)
        grid.addWidget(self.timezone_label, 2, 2, 1, 2)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setObjectName("Subtitle")
        self.notice.hide()
        grid.addWidget(self.notice, 4, 0, 1, 4)
        grid.setColumnStretch(0, 2)
        for column in range(1, 4):
            grid.setColumnStretch(column, 1)
        self._backbone.dateTimeChanged.connect(self._from_backbone)
        if hasattr(self._backbone, "display_timezone_changed"):
            self._backbone.display_timezone_changed.connect(
                lambda: self._from_backbone(self._backbone.dateTime())
            )
        self._from_backbone(self._backbone.dateTime())

    def _combo(self, name, minimum, maximum, values):
        field = QComboBox(self)
        # Install an already marked editor: setEditable(True) can polish Qt's
        # default editor synchronously, before we could opt out of generic voice.
        entry = QLineEdit()
        entry.setProperty("voice_input_attached", True)
        field.setLineEdit(entry)
        field.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        field.addItems([str(value) for value in values])
        field.lineEdit().setValidator(QIntValidator(minimum, maximum, field))
        field.lineEdit().setMaxLength(4 if maximum > 100 else 2)
        # One explicit voice entry covers the entire value; do not attach a
        # second generic dictation action to each number box.
        field.lineEdit().setProperty("voice_input_attached", True)
        field.setAccessibleName(name)
        field.setMinimumWidth(0)
        field.setMinimumContentsLength(4 if maximum > 100 else 2)
        field.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        field.setMaxVisibleItems(8)
        field.activated.connect(lambda *_: self._try_commit())
        field.lineEdit().editingFinished.connect(self._try_commit)
        return field

    def _from_backbone(self, value):
        if self._loading:
            return
        self._loading = True
        date = value.date()
        if date == EMPTY_DATE:
            date = QDate(beijing_today())
        for field, number in (
            (self.year, date.year()),
            (self.month, date.month()),
            (self.day, date.day()),
            (self.hour, value.time().hour()),
            (self.minute, value.time().minute()),
        ):
            field.setCurrentText(str(number))
        self.weekday.setText("星期" + "一二三四五六日"[date.dayOfWeek() - 1])
        if self._date_only:
            self.setAccessibleName("日期（年、月、日；星期自动计算）")
        else:
            zone = editor_timezone_label(self._backbone.timeZone())
            self.setAccessibleName(f"日期和时间（{zone}）")
            self.timezone_label.setText(zone)
            for field, name in ((self.hour, "时，24小时制"), (self.minute, "分")):
                field.setAccessibleName(f"{name}（{zone}）")
        self._loading = False
        self.dateTimeChanged.emit(value)
        self.dateChanged.emit(value.date())
        self.timeChanged.emit(value.time())

    def _read_date(self):
        try:
            values = [int(field.currentText()) for field in (self.year, self.month, self.day)]
        except ValueError as error:
            raise ValueError("请填写完整的年、月、日。") from error
        date = QDate(*values)
        if not date.isValid() or not self.minimumDate() <= date <= self.maximumDate():
            raise ValueError("日期无效或超出可选范围，请核对年月日。")
        return date

    def _commit(self):
        if self._loading:
            return
        date = self._read_date()
        if self._date_only:
            self._backbone.setDate(date)
        else:
            try:
                time = QTime(int(self.hour.currentText()), int(self.minute.currentText()))
            except ValueError as error:
                raise ValueError("请填写完整的时、分。") from error
            if not time.isValid():
                raise ValueError("时间无效，请使用 0—23 时、0—59 分。")
            previous = self._backbone.dateTime()
            if (
                previous.date() == date
                and previous.time().hour() == time.hour()
                and previous.time().minute() == time.minute()
            ):
                # Merely reading the form must preserve seconds and the chosen
                # offset of an existing DST overlap, not reconstruct an instant.
                self.weekday.setText("星期" + "一二三四五六日"[date.dayOfWeek() - 1])
                return
            value = QDateTime(date, time, self.timeZone())
            if not value.isValid() or value.date() != date or value.time() != time:
                raise ValueError("该时刻在当前时区不存在（可能为夏令时跳转），请重新选择。")
            self._backbone.setDateTime(value)
        self.weekday.setText("星期" + "一二三四五六日"[date.dayOfWeek() - 1])

    def _try_commit(self):
        try:
            self._commit()
            self.notice.hide()
        except ValueError as error:
            self.notice.setText(str(error))
            self.notice.show()

    def apply_voice_text(self, text):
        # Parse before changing controls: invalid speech leaves the old value.
        value = self.dateTime()
        base = datetime(
            value.date().year(),
            value.date().month(),
            value.date().day(),
            value.time().hour(),
            value.time().minute(),
        )
        parsed = parse_time_phrase(text, base, date_only=self._date_only)
        date, time = QDate(parsed.year, parsed.month, parsed.day), QTime(parsed.hour, parsed.minute)
        if not self.minimumDate() <= date <= self.maximumDate():
            raise ValueError("所说的日期超出可选范围，请核对。")
        target = QDateTime(date, time, self.timeZone())
        if not target.isValid() or target.date() != date or target.time() != time:
            raise ValueError("所说时刻在当前时区不存在，请重新选择。")
        self.setDateTime(target)
        self.notice.setText("语音已填写，尚未保存。未提及的部分保留原值，请核对。")
        self.notice.show()

    def _voice(self):
        from .voice_input import VoiceTextDialog

        dialog = VoiceTextDialog(
            self,
            prompt="请说年月日"
            + (
                "，例如一九六零年五月二日。"
                if self._date_only
                else "和时分，例如明天下午三点半；仅填写，最后仍需您确认。"
            ),
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                self.apply_voice_text(dialog.transcript_text)
            except ValueError as error:
                QMessageBox.information(self, "请核对时间", str(error))

    def dateTime(self):
        self._commit()
        return self._backbone.dateTime()

    def setDateTime(self, value):
        self._backbone.setDateTime(value)
        self._from_backbone(self._backbone.dateTime())

    def date(self):
        return self.dateTime().date()

    def setDate(self, value):
        self._backbone.setDate(value)
        self._from_backbone(self._backbone.dateTime())

    def time(self):
        return self.dateTime().time()

    def setTime(self, value):
        self._backbone.setTime(value)
        self._from_backbone(self._backbone.dateTime())

    def setTimeZone(self, value):
        self._backbone.setTimeZone(value)
        self._from_backbone(self._backbone.dateTime())

    def timeZone(self):
        return self._backbone.timeZone()

    def minimumDate(self):
        return self._backbone.minimumDate()

    def maximumDate(self):
        return self._backbone.maximumDate()

    def setMinimumDate(self, value):
        self._backbone.setMinimumDate(value)

    def setMaximumDate(self, value):
        self._backbone.setMaximumDate(value)

    def setDateRange(self, minimum, maximum):
        self._backbone.setDateRange(minimum, maximum)

    def calendarPopup(self):
        return self._backbone.calendarPopup()

    def calendarWidget(self):
        return self._backbone.calendarWidget()

    def interpretText(self):
        self._commit()

    def text(self):
        value = self.dateTime()
        return value.toString("yyyy-MM-dd" if self._date_only else "yyyy-MM-dd HH:mm")


class SegmentedDateEdit(SegmentedDateTimeEdit):
    def __init__(self, parent=None, *, past_only=False, optional=True):
        backbone = OptionalDateEdit(past_only=past_only)
        super().__init__(parent=parent, date_only=True, _backbone=backbone)
        self.optional = optional
        self.enabled_date = QCheckBox("填写日期（可不填）", self)
        self.enabled_date.setChecked(not optional)
        self.enabled_date.setVisible(optional)
        self.layout().addWidget(self.enabled_date, 2, 0, 1, 4)
        self.enabled_date.toggled.connect(self._toggle_date)
        self._toggle_date(not optional)

    def _toggle_date(self, checked):
        for field in (self.year, self.month, self.day, self.weekday):
            field.setEnabled(checked)
        if not checked:
            self._backbone.clear()

    def _commit(self):
        if hasattr(self, "enabled_date") and not self.enabled_date.isChecked():
            return
        super()._commit()

    def setDate(self, value):
        if hasattr(self, "enabled_date"):
            self.enabled_date.setChecked(value.isValid() and value != EMPTY_DATE)
        super().setDate(value)

    def setDateTime(self, value):
        self.setDate(value.date())

    def iso_value(self):
        if not self.enabled_date.isChecked():
            return None
        return self.date().toString(Qt.DateFormat.ISODate)

    def set_iso_value(self, value):
        parsed = QDate.fromString(str(value or ""), Qt.DateFormat.ISODate)
        if parsed.isValid() and parsed > self.maximumDate():
            self.setMaximumDate(parsed)
        self.setDate(parsed if parsed.isValid() else EMPTY_DATE)

    def clear(self):
        self.enabled_date.setChecked(False)

    def apply_voice_text(self, text):
        # An optional empty date uses today's date as the explicit parser base,
        # but becomes filled only after a complete successful parse.
        old = self.enabled_date.isChecked()
        self.enabled_date.setChecked(True)
        try:
            super().apply_voice_text(text)
        except ValueError:
            self.enabled_date.setChecked(old)
            raise
