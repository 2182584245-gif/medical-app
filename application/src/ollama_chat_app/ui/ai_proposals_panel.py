from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..time_utils import display_timezone_label, format_beijing

_TYPE_LABELS = {
    "life_record": "生活记录",
    "profile_fact": "档案信息",
    "reminder": "生活提醒",
    "advisor_summary": "顾问摘要",
    "insight": "一般生活提示",
}
_CATEGORY_LABELS = {
    "diet": "饮食",
    "water": "饮水",
    "activity": "活动",
    "sleep": "睡眠",
    "environment": "环境",
}
_PROFILE_LABELS = {
    "display_name": "称呼",
    "birth_date": "出生日期",
    "gender": "性别",
    "phone": "联系电话",
    "living_situation": "居住情况",
    "emergency_contact_name": "紧急联系人",
    "emergency_contact_phone": "紧急联系人电话",
    "ai_preferred_name": "AI 称呼",
    "reminder_frequency": "提醒频率",
    "height_cm": "身高",
    "health_goals": "生活目标",
    "dietary_preferences": "饮食偏好",
}


class AiProposalsPanel(QWidget):
    """Reusable review panel; the caller performs service calls after signals fire."""

    confirm_requested = Signal(int, int)
    dismiss_requested = Signal(int, int)
    refresh_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("AiProposalsPanel")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        heading_row = QHBoxLayout()
        title = QLabel("AI 待确认内容")
        title.setObjectName("Title")
        heading_row.addWidget(title)
        heading_row.addStretch(1)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setObjectName("AiDraftRefreshButton")
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        heading_row.addWidget(self.refresh_button)
        root.addLayout(heading_row)

        hint = QLabel(
            "AI 只能提出草稿。请逐项核对；只有点击“确认保存”后，内容才会进入档案、记录或提醒。"
        )
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        root.addWidget(hint)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        root.addWidget(self.status_label)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.container = QWidget()
        self.cards_layout = QVBoxLayout(self.container)
        self.cards_layout.setContentsMargins(0, 0, 4, 0)
        self.cards_layout.setSpacing(12)
        self.scroll.setWidget(self.container)
        root.addWidget(self.scroll, 1)
        self.set_drafts([])

    def set_drafts(self, drafts: Sequence[Mapping[str, Any]]) -> None:
        self._clear_cards()
        pending_count = 0
        for draft in drafts:
            card, card_pending = self._draft_card(draft)
            pending_count += card_pending
            self.cards_layout.addWidget(card)
        if not drafts:
            empty = QLabel("目前没有需要确认的 AI 草稿。")
            empty.setObjectName("AiDraftEmptyLabel")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setMinimumHeight(120)
            self.cards_layout.addWidget(empty)
        self.cards_layout.addStretch(1)
        if drafts:
            self.set_status(
                f"共 {len(drafts)} 份草稿，{pending_count} 项等待确认。",
                kind="ok" if pending_count == 0 else "busy",
            )
        else:
            self.clear_status()

    def set_busy(self, message: str = "正在读取 AI 草稿…") -> None:
        self.refresh_button.setEnabled(False)
        self.set_status(message, kind="busy")

    def set_error(self, message: str) -> None:
        self.refresh_button.setEnabled(True)
        self.set_status(message, kind="error")

    def set_status(self, message: str, *, kind: str = "ok") -> None:
        self.refresh_button.setEnabled(True)
        object_names = {"ok": "StatusOk", "busy": "StatusBusy", "error": "StatusError"}
        self.status_label.setObjectName(object_names.get(kind, "StatusOk"))
        self.status_label.setText(message)
        self.status_label.show()
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def clear_status(self) -> None:
        self.refresh_button.setEnabled(True)
        self.status_label.clear()
        self.status_label.hide()

    def _draft_card(self, draft: Mapping[str, Any]) -> tuple[QFrame, int]:
        insight_id = int(draft["id"])
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(9)

        source = " · ".join(
            value
            for value in (
                None if draft.get("provider") is None else str(draft["provider"]),
                None if draft.get("model") is None else str(draft["model"]),
            )
            if value
        )
        heading = QLabel("AI 回答" + (f"　{source}" if source else ""))
        heading.setStyleSheet("font-weight: 600; color: #334155;")
        layout.addWidget(heading)
        answer = QLabel(str(draft.get("answer") or ""))
        answer.setWordWrap(True)
        answer.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(answer)

        pending = 0
        proposals = draft.get("proposals")
        if not isinstance(proposals, list) or not proposals:
            no_change = QLabel("这次回答没有提出需要写入本地数据的内容。")
            no_change.setObjectName("Hint")
            no_change.setWordWrap(True)
            layout.addWidget(no_change)
            return card, pending
        for proposal in proposals:
            if not isinstance(proposal, Mapping):
                continue
            proposal_widget = self._proposal_row(insight_id, proposal)
            layout.addWidget(proposal_widget)
            if proposal.get("status") == "pending":
                pending += 1
        return card, pending

    def _proposal_row(self, insight_id: int, proposal: Mapping[str, Any]) -> QFrame:
        proposal_id = int(proposal["id"])
        status = str(proposal.get("status", "pending"))
        frame = QFrame()
        frame.setObjectName("AiProposalRow")
        frame.setStyleSheet(
            "QFrame#AiProposalRow { background: #f8fafc; border: 1px solid #e2e8f0; "
            "border-radius: 10px; }"
        )
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(7)

        proposal_type = str(proposal.get("type", ""))
        title = QLabel(_TYPE_LABELS.get(proposal_type, "待确认内容"))
        title.setStyleSheet("font-weight: 600;")
        layout.addWidget(title)
        summary = QLabel(_proposal_summary(proposal))
        summary.setWordWrap(True)
        summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(summary)

        if status == "pending":
            button_row = QHBoxLayout()
            button_row.addStretch(1)
            dismiss = QPushButton("忽略")
            dismiss.setObjectName(f"AiDismissButton-{insight_id}-{proposal_id}")
            dismiss.clicked.connect(
                lambda _checked=False, did=insight_id, pid=proposal_id: self.dismiss_requested.emit(
                    did, pid
                )
            )
            confirm = QPushButton("确认保存")
            confirm.setObjectName(f"AiConfirmButton-{insight_id}-{proposal_id}")
            confirm.setProperty("class", "primary")
            confirm.setStyleSheet(
                "background: #2563eb; color: white; border-color: #2563eb; font-weight: 600;"
            )
            confirm.clicked.connect(
                lambda _checked=False, did=insight_id, pid=proposal_id: self.confirm_requested.emit(
                    did, pid
                )
            )
            button_row.addWidget(dismiss)
            button_row.addWidget(confirm)
            layout.addLayout(button_row)
        else:
            label = QLabel("已确认保存" if status == "accepted" else "已忽略")
            label.setObjectName("StatusOk" if status == "accepted" else "Hint")
            layout.addWidget(label)
        return frame

    def _clear_cards(self) -> None:
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()


def _proposal_summary(proposal: Mapping[str, Any]) -> str:
    proposal_type = str(proposal.get("type", ""))
    if proposal_type == "life_record":
        category = _CATEGORY_LABELS.get(str(proposal.get("category")), "生活")
        occurred_at = format_beijing(proposal.get("occurred_at"), empty="未填写时间")
        return (
            f"{category} · {occurred_at}（{display_timezone_label()}）\n"
            f"{proposal.get('content', '')}"
        )
    if proposal_type == "profile_fact":
        field = str(proposal.get("fact_key", ""))
        return f"{_PROFILE_LABELS.get(field, field)}：{proposal.get('value', '')}"
    if proposal_type == "reminder":
        scheduled_at = format_beijing(proposal.get("scheduled_at"), empty="未填写时间")
        return f"{proposal.get('title', '')} · {scheduled_at}（{display_timezone_label()}）"
    if proposal_type == "advisor_summary":
        period_start = format_beijing(proposal.get("period_start"), empty="未填写开始时间")
        period_end = format_beijing(proposal.get("period_end"), empty="未填写结束时间")
        return (
            f"{period_start} 至 {period_end}（{display_timezone_label()}）\n"
            f"{proposal.get('content', '')}"
        )
    return str(proposal.get("content", ""))


__all__ = ["AiProposalsPanel"]
