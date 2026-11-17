from __future__ import annotations

from datetime import timedelta

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..time_utils import beijing_now, display_timezone_label, display_timezone_name
from .segmented_time import SegmentedDateTimeEdit
from .time_fields import datetime_iso


class SleepCheckDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("早上核对一下睡眠")
        self.resize(640, 630)
        layout = QVBoxLayout(self)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMinimumWidth(0)
        self.scroll = scroll
        contents = QWidget()
        content_layout = QVBoxLayout(contents)
        label = QLabel("昨晚大概几点睡，今天几点起？请改成实际时间。也可以返回后直接说给我听。")
        label.setWordWrap(True)
        content_layout.addWidget(label)
        now = beijing_now()
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.start = SegmentedDateTimeEdit(
            (now - timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0)
        )
        self.end = SegmentedDateTimeEdit(now.replace(hour=7, minute=0, second=0, microsecond=0))
        form.addRow("大约入睡时间", self.start)
        form.addRow("起床时间", self.end)
        content_layout.addLayout(form)
        self.status = QLabel("这两项是用户自报，不是设备检测；默认示例尚未保存。")
        self.status.setWordWrap(True)
        content_layout.addWidget(self.status)
        content_layout.addStretch(1)
        scroll.setWidget(contents)
        layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("用这两个时间")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("返回")
        buttons.accepted.connect(self.validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def validate(self):
        try:
            start, end = self.start.dateTime(), self.end.dateTime()
        except ValueError as error:
            self.status.setText(str(error))
            return
        seconds = start.secsTo(end)
        if not 0 < seconds <= 86400:
            self.status.setText("请核对时间：起床应晚于入睡，区间不超过 24 小时。")
            return
        if end.toSecsSinceEpoch() > beijing_now().timestamp():
            self.status.setText("起床时间还没到，请填写已经发生的时间。")
            return
        self.accept()

    def prompt(self) -> str:
        return (
            "我自报的作息：入睡 "
            + datetime_iso(self.start.dateTime())
            + "，起床 "
            + datetime_iso(self.end.dateTime())
            + f"（{display_timezone_label()}；时区 {display_timezone_name()}）。"
            + "请整理为待确认的作息记录。"
        )
