from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFrame, QLabel, QScrollArea, QVBoxLayout, QWidget


class MessageBubble(QFrame):
    def __init__(self, role: str, content: str, status: str = "complete") -> None:
        super().__init__()
        self.role = role
        self.setObjectName("ChatMessageBubble")
        self.setMaximumWidth(720)
        self.setProperty("role", role)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(5)

        role_label = QLabel("你" if role == "user" else "AI")
        role_label.setStyleSheet("font-weight: 600; background: transparent;")
        layout.addWidget(role_label)

        self.content_label = QLabel(content)
        self.content_label.setWordWrap(True)
        self.content_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.content_label.setTextFormat(Qt.TextFormat.PlainText)
        self.content_label.setStyleSheet("background: transparent;")
        layout.addWidget(self.content_label)

        self.status_label = QLabel()
        self.status_label.setObjectName("Hint")
        self.status_label.setStyleSheet("background: transparent;")
        layout.addWidget(self.status_label)
        self.set_status(status)

        if role == "user":
            self.setStyleSheet(
                "QFrame#ChatMessageBubble { background: #e8f1ff; border: 1px solid #c6dcff; "
                "border-radius: 12px; }"
            )
        else:
            self.setStyleSheet(
                "QFrame#ChatMessageBubble { background: #ffffff; border: 1px solid #dce2eb; "
                "border-radius: 12px; }"
            )

    def set_content(self, content: str) -> None:
        self.content_label.setText(content)

    def set_status(self, status: str) -> None:
        labels = {
            "pending": "正在生成…",
            "error": "生成失败",
            "complete": "",
        }
        self.status_label.setText(labels.get(status, status))
        self.status_label.setVisible(bool(self.status_label.text()))


class MessageList(QScrollArea):
    def __init__(self) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.container = QWidget()
        self.layout = QVBoxLayout(self.container)
        self.layout.setContentsMargins(18, 18, 18, 18)
        self.layout.setSpacing(12)
        self.layout.addStretch(1)
        self.setWidget(self.container)
        self._bubbles: dict[int, MessageBubble] = {}

    def clear_messages(self) -> None:
        while self.layout.count() > 1:
            item = self.layout.takeAt(0)
            if widget := item.widget():
                widget.deleteLater()
        self._bubbles.clear()

    def add_message(
        self,
        message_id: int,
        role: str,
        content: str,
        status: str = "complete",
    ) -> MessageBubble:
        bubble = MessageBubble(role, content, status)
        alignment = Qt.AlignmentFlag.AlignRight if role == "user" else Qt.AlignmentFlag.AlignLeft
        self.layout.insertWidget(self.layout.count() - 1, bubble, 0, alignment)
        self._bubbles[message_id] = bubble
        self._resize_bubbles()
        self._scroll_to_bottom()
        return bubble

    def update_message(self, message_id: int, content: str, status: str) -> None:
        bubble = self._bubbles.get(message_id)
        if not bubble:
            return
        bubble.set_content(content)
        bubble.set_status(status)
        self._resize_bubbles()
        self._scroll_to_bottom()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._resize_bubbles()

    def _resize_bubbles(self) -> None:
        width = max(140, min(720, self.viewport().width() - 56))
        for bubble in self._bubbles.values():
            bubble.setFixedWidth(width)
            margins = bubble.layout().contentsMargins()
            text_width = max(80, width - margins.left() - margins.right() - 4)
            bubble.content_label.setMinimumHeight(
                max(
                    bubble.content_label.fontMetrics().height(),
                    bubble.content_label.heightForWidth(text_width),
                )
            )
            bubble.setMinimumHeight(bubble.layout().minimumSize().height())

    def _scroll_to_bottom(self) -> None:
        QTimer.singleShot(
            0,
            lambda: self.verticalScrollBar().setValue(self.verticalScrollBar().maximum()),
        )
