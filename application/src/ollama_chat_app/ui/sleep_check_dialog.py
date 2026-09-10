from __future__ import annotations

from datetime import timedelta

from PySide6.QtCore import QDateTime
from PySide6.QtWidgets import (
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
)

from ..time_utils import beijing_now


class SleepCheckDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("早上核对一下睡眠")
        self.setMinimumWidth(500)
        layout = QVBoxLayout(self)
        label = QLabel("昨晚大概几点睡，今天几点起？请改成实际时间。也可以返回后直接说给我听。")
        label.setWordWrap(True)
        layout.addWidget(label)
        now = beijing_now()
        form = QFormLayout()
        self.start = QDateTimeEdit(QDateTime((now - timedelta(days=1)).replace(hour=22, minute=0)))
        self.end = QDateTimeEdit(QDateTime(now.replace(hour=7, minute=0)))
        for control in (self.start, self.end):
            control.setCalendarPopup(True)
            control.setDisplayFormat("yyyy-MM-dd HH:mm")
            control.setMinimumHeight(48)
        form.addRow("大约入睡时间", self.start)
        form.addRow("起床时间", self.end)
        layout.addLayout(form)
        self.status = QLabel("这两项是用户自报，不是设备检测；默认示例尚未保存。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("用这两个时间")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("返回")
        buttons.accepted.connect(self.validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def validate(self):
        seconds = self.start.dateTime().secsTo(self.end.dateTime())
        if not 0 < seconds <= 86400:
            self.status.setText("请核对时间：起床应晚于入睡，区间不超过 24 小时。")
            return
        if self.end.dateTime().toSecsSinceEpoch() > beijing_now().timestamp():
            self.status.setText("起床时间还没到，请填写已经发生的时间。")
            return
        self.accept()

    def prompt(self) -> str:
        return (
            "我自报的作息：入睡 "
            + self.start.dateTime().toString("yyyy-MM-dd HH:mm")
            + "，起床 "
            + self.end.dateTime().toString("yyyy-MM-dd HH:mm")
            + "（北京时间）。请整理为待确认的作息记录。"
        )
