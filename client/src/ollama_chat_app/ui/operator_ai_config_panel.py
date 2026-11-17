from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


class OperatorAiConfigPanel(QWidget):
    """Operator-facing controls for non-secret, global structured-AI settings."""

    config_saved = Signal(dict)

    def __init__(self, ai_service: object, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("OperatorAiConfigPanel")
        self.ai_service = ai_service
        self.operator_user_id: int | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 4, 8, 8)
        root.setSpacing(12)
        title = QLabel("结构化 AI 功能配置")
        title.setObjectName("PageTitle")
        root.addWidget(title)
        description = QLabel(
            "这里控制 AI 是否可以读取已确认的本地生活数据并生成待确认草稿。"
            "所有 AI 建议仍须会员或当前顾问逐项确认。"
        )
        description.setWordWrap(True)
        description.setObjectName("HealthHint")
        root.addWidget(description)

        self.security_notice = QLabel(
            "安全说明：此页面只保存功能开关和上下文天数，不接收、不显示、也不保存任何 API Key。"
        )
        self.security_notice.setWordWrap(True)
        self.security_notice.setObjectName("CommerceNotice")
        root.addWidget(self.security_notice)

        card = QFrame()
        card.setObjectName("HealthCard")
        form = QFormLayout(card)
        form.setContentsMargins(18, 16, 18, 16)
        form.setSpacing(14)
        self.enabled_checkbox = QCheckBox("启用结构化 AI")
        self.enabled_checkbox.setObjectName("StructuredAiEnabled")
        self.enabled_checkbox.toggled.connect(self._update_feature_controls)
        form.addRow("总开关", self.enabled_checkbox)
        self.member_checkbox = QCheckBox("允许会员 AI 助手生成待确认内容")
        self.member_checkbox.setObjectName("MemberAiEnabled")
        form.addRow("会员助手", self.member_checkbox)
        self.advisor_checkbox = QCheckBox("允许顾问生成 AI 工作摘要草稿")
        self.advisor_checkbox.setObjectName("AdvisorAiEnabled")
        form.addRow("顾问摘要", self.advisor_checkbox)
        self.context_days_input = QSpinBox()
        self.context_days_input.setObjectName("AiContextDays")
        self.context_days_input.setRange(1, 90)
        self.context_days_input.setSuffix(" 天")
        self.context_days_input.setToolTip(
            "AI 最多读取最近多少天的生活记录；调用方只能主动选择更短范围。"
        )
        form.addRow("数据上下文范围", self.context_days_input)
        root.addWidget(card)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        root.addWidget(self.status_label)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.refresh_button = QPushButton("重新读取")
        self.refresh_button.setObjectName("AiConfigRefreshButton")
        self.refresh_button.clicked.connect(self.refresh)
        actions.addWidget(self.refresh_button)
        self.save_button = QPushButton("保存配置")
        self.save_button.setObjectName("PrimaryButton")
        self.save_button.clicked.connect(self.save)
        actions.addWidget(self.save_button)
        root.addLayout(actions)
        root.addStretch(1)
        self._set_form_enabled(False)

    def set_operator(self, operator_user_id: int | None) -> None:
        self.operator_user_id = operator_user_id
        self._set_form_enabled(operator_user_id is not None)
        if operator_user_id is None:
            self.clear_status()
            return
        self.refresh()

    def refresh(self) -> None:
        if self.operator_user_id is None:
            self.set_error("请先登录运营员账号。")
            return
        try:
            config = self.ai_service.get_ai_config(self.operator_user_id)
            self.set_config(config)
            self.set_status("已读取当前全局配置。")
        except Exception as exc:
            self.set_error(f"读取 AI 配置失败：{exc}")

    def save(self) -> None:
        if self.operator_user_id is None:
            self.set_error("请先登录运营员账号。")
            return
        values = {
            "enabled": self.enabled_checkbox.isChecked(),
            "member_assistant_enabled": self.member_checkbox.isChecked(),
            "advisor_summary_enabled": self.advisor_checkbox.isChecked(),
            "context_days": self.context_days_input.value(),
        }
        try:
            saved = self.ai_service.update_ai_config(self.operator_user_id, values)
            self.set_config(saved)
            self.set_status("结构化 AI 配置已保存在本地数据库中。")
            self.config_saved.emit(dict(saved))
        except Exception as exc:
            self.set_error(f"保存 AI 配置失败：{exc}")

    def set_config(self, config: Mapping[str, Any]) -> None:
        self.enabled_checkbox.setChecked(bool(config.get("enabled", True)))
        self.member_checkbox.setChecked(bool(config.get("member_assistant_enabled", True)))
        self.advisor_checkbox.setChecked(bool(config.get("advisor_summary_enabled", True)))
        try:
            days = int(config.get("context_days", 14))
        except (TypeError, ValueError):
            days = 14
        self.context_days_input.setValue(max(1, min(90, days)))
        self._update_feature_controls(self.enabled_checkbox.isChecked())

    def set_status(self, message: str) -> None:
        self.status_label.setObjectName("StatusOk")
        self.status_label.setText(message)
        self.status_label.show()
        self._repolish_status()

    def set_error(self, message: str) -> None:
        self.status_label.setObjectName("StatusError")
        self.status_label.setText(message)
        self.status_label.show()
        self._repolish_status()

    def clear_status(self) -> None:
        self.status_label.clear()
        self.status_label.hide()

    def _update_feature_controls(self, enabled: bool) -> None:
        has_operator = self.operator_user_id is not None
        self.member_checkbox.setEnabled(has_operator and enabled)
        self.advisor_checkbox.setEnabled(has_operator and enabled)
        self.context_days_input.setEnabled(has_operator and enabled)

    def _set_form_enabled(self, enabled: bool) -> None:
        self.enabled_checkbox.setEnabled(enabled)
        self.save_button.setEnabled(enabled)
        self.refresh_button.setEnabled(enabled)
        self._update_feature_controls(enabled and self.enabled_checkbox.isChecked())

    def _repolish_status(self) -> None:
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)


__all__ = ["OperatorAiConfigPanel"]
