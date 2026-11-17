"""Reusable warm-card components for the member experience.

The components intentionally contain presentation only.  They do not know
about the cloud client or health-service schema, which keeps the redesign
safe to apply to the existing PySide6 pages.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from .art_icons import rounded_icon


class WarmCard(QFrame):
    """A soft, rounded surface used by member pages."""

    def __init__(self, title: str = "", subtitle: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("WarmCard")
        self.setProperty("warmTone", "neutral")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(20, 18, 20, 18)
        self.body.setSpacing(10)
        if title:
            self.title_label = QLabel(title)
            self.title_label.setObjectName("WarmCardTitle")
            self.body.addWidget(self.title_label)
        else:
            self.title_label = None
        if subtitle:
            self.subtitle_label = QLabel(subtitle)
            self.subtitle_label.setObjectName("WarmCardSubtitle")
            self.subtitle_label.setWordWrap(True)
            self.body.addWidget(self.subtitle_label)
        else:
            self.subtitle_label = None

    def set_tone(self, tone: str) -> None:
        self.setProperty("warmTone", tone)
        self.style().unpolish(self)
        self.style().polish(self)


class ReminderBanner(WarmCard):
    """Warm-orange, single-action reminder banner."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent=parent)
        self.setObjectName("ReminderBanner")
        self.set_tone("reminder")
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self.icon_label = QLabel()
        self.icon_label.setPixmap(rounded_icon("reminder", size=30, color="#6c3c27", background="#fff0df").pixmap(30, 30))
        self.icon_label.setFixedSize(30, 30)
        self.icon_label.setObjectName("WarmReminderIcon")
        row.addWidget(self.icon_label)
        self.text_label = QLabel(text)
        self.text_label.setObjectName("WarmReminderText")
        self.text_label.setWordWrap(True)
        row.addWidget(self.text_label, 1)
        self.action_button = QPushButton("去填写")
        self.action_button.setObjectName("WarmReminderAction")
        self.action_button.setMinimumHeight(42)
        row.addWidget(self.action_button)
        self.body.addLayout(row)

    def set_text(self, text: str) -> None:
        self.text_label.setText(text)


class DailyReadingCard(WarmCard):
    """A compact article teaser with topic, title, summary and action."""

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setObjectName("DailyReadingCard")
        self.set_tone("reading")
        top = QHBoxLayout()
        self.topic_label = QLabel("今日阅读")
        self.topic_label.setObjectName("WarmReadingTopic")
        top.addWidget(self.topic_label)
        top.addStretch(1)
        self.action_button = QPushButton("去阅读")
        self.action_button.setObjectName("WarmSecondaryAction")
        top.addWidget(self.action_button)
        self.body.addLayout(top)
        self.title_label = QLabel("每天读一点可靠资料")
        self.title_label.setObjectName("WarmReadingTitle")
        self.title_label.setWordWrap(True)
        self.body.addWidget(self.title_label)
        self.summary_label = QLabel("了解一个小知识，让今天的生活更安心。")
        self.summary_label.setObjectName("WarmReadingSummary")
        self.summary_label.setWordWrap(True)
        self.body.addWidget(self.summary_label)


class VoiceRecordCard(WarmCard):
    """Large, friendly entry point for voice-first recording."""

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setObjectName("VoiceRecordCard")
        self.set_tone("voice")
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self.icon_button = QPushButton()
        self.icon_button.setObjectName("WarmVoiceButton")
        self.icon_button.setIcon(rounded_icon("mic", size=42, color="#5c4f88", background="#e8ddfa"))
        self.icon_button.setIconSize(QSize(42, 42))
        self.icon_button.setFixedSize(68, 68)
        row.addWidget(self.icon_button)
        copy = QVBoxLayout()
        self.title_label = QLabel("语音智能记录")
        self.title_label.setObjectName("WarmVoiceTitle")
        copy.addWidget(self.title_label)
        self.subtitle_label = QLabel("说出今日所做的事情，系统自动记录")
        self.subtitle_label.setObjectName("WarmVoiceSubtitle")
        self.subtitle_label.setWordWrap(True)
        copy.addWidget(self.subtitle_label)
        row.addLayout(copy, 1)
        self.body.addLayout(row)


class RecordMetricCard(WarmCard):
    """Metric card used for diet/activity/sleep quick entry."""

    def __init__(self, title: str, hint: str, icon: str, tone: str, parent=None):
        super().__init__(parent=parent)
        self.setObjectName("RecordMetricCard")
        self.set_tone(tone)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self.icon_label = QLabel()
        self.icon_label.setPixmap(rounded_icon(icon, size=42).pixmap(42, 42))
        row.addWidget(self.icon_label)
        copy = QVBoxLayout()
        self.metric_title = QLabel(title)
        self.metric_title.setObjectName("RecordMetricTitle")
        copy.addWidget(self.metric_title)
        self.metric_hint = QLabel(hint)
        self.metric_hint.setObjectName("RecordMetricHint")
        self.metric_hint.setWordWrap(True)
        copy.addWidget(self.metric_hint)
        row.addLayout(copy, 1)
        self.action_button = QPushButton("记录")
        self.action_button.setObjectName("WarmSecondaryAction")
        self.action_button.setMinimumHeight(42)
        row.addWidget(self.action_button)
        self.body.addLayout(row)


class TimelineCard(WarmCard):
    """Heading surface for the compact today's dynamic timeline."""

    def __init__(self, title: str = "今日动态", parent=None):
        super().__init__(title, parent=parent)
        self.setObjectName("TimelineCard")
        self.set_tone("timeline")


class ProfileCard(WarmCard):
    """Visual profile surface for member/advisor information."""

    def __init__(self, name: str, detail: str = "", parent=None):
        super().__init__(parent=parent)
        self.setObjectName("ProfileCard")
        self.set_tone("profile")
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        avatar = QLabel("●")
        avatar.setObjectName("WarmAvatar")
        row.addWidget(avatar)
        copy = QVBoxLayout()
        self.name_label = QLabel(name)
        self.name_label.setObjectName("ProfileName")
        copy.addWidget(self.name_label)
        self.detail_label = QLabel(detail)
        self.detail_label.setObjectName("ProfileDetail")
        self.detail_label.setWordWrap(True)
        copy.addWidget(self.detail_label)
        row.addLayout(copy, 1)
        self.body.addLayout(row)


class StatusBadge(QLabel):
    def __init__(self, text: str = "", tone: str = "success", parent=None):
        super().__init__(text, parent)
        self.setObjectName("StatusBadge")
        self.setProperty("badgeTone", tone)


class LoadingPanel(WarmCard):
    def __init__(self, text: str = "正在准备…", parent=None):
        super().__init__(parent=parent)
        self.setObjectName("LoadingPanel")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.body.addWidget(self.progress)
        self.label = QLabel(text)
        self.label.setObjectName("LoadingLabel")
        self.body.addWidget(self.label)

    def set_progress(self, value: int, text: str | None = None) -> None:
        self.progress.setValue(max(0, min(100, int(value))))
        if text is not None:
            self.label.setText(text)


class EmptyState(WarmCard):
    def __init__(self, text: str = "还没有内容", action_text: str = "", parent=None):
        super().__init__(parent=parent)
        self.setObjectName("EmptyState")
        self.label = QLabel(text)
        self.label.setObjectName("EmptyStateText")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body.addWidget(self.label)
        self.action_button = QPushButton(action_text) if action_text else None
        if self.action_button:
            self.action_button.setObjectName("WarmSecondaryAction")
            self.body.addWidget(self.action_button, 0, Qt.AlignmentFlag.AlignCenter)


class WarmSearchBar(QLineEdit):
    def __init__(self, placeholder: str = "搜索", parent=None):
        super().__init__(parent)
        self.setObjectName("WarmSearchBar")
        self.setPlaceholderText(placeholder)
        self.setMinimumHeight(44)


class WarmDialog(QFrame):
    """Small helper surface for custom dialogs; no business behaviour."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("WarmDialog")
        self.setProperty("warmTone", "neutral")
