from __future__ import annotations

from collections.abc import Mapping

from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QVBoxLayout

from ..config import DEEPSEEK_DEFAULT_MODEL, DEEPSEEK_VISION_MODEL


class ChatSettingsDialog(QDialog):
    def __init__(self, preferences: Mapping[str, object], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI 设置")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        title = QLabel("让聊天更合您的习惯")
        title.setObjectName("Title")
        layout.addWidget(title)
        form = QFormLayout()
        form.setSpacing(16)
        layout.addLayout(form)
        self.controls: dict[str, QComboBox] = {}
        choices = {
            "ai_provider": (
                "AI 模型",
                [("DeepSeek", "deepseek_cloud")],
            ),
            "font_size": (
                "字号",
                [("大字 · 18", 18), ("舒适 · 20", 20), ("更大 · 22", 22), ("特大 · 24", 24)],
            ),
            "ai_tone": (
                "语气",
                [("温和关怀", "warm"), ("简洁直接", "concise"), ("清楚正式", "formal")],
            ),
            "ai_complexity": (
                "回答复杂度",
                [("简单易懂", "simple"), ("适中", "standard"), ("详细说明", "detailed")],
            ),
            "theme_color": (
                "主题颜色",
                [("草木绿", "sage"), ("晴空蓝", "blue"), ("暖玫瑰", "rose")],
            ),
            "brightness": (
                "亮度",
                [("柔和", 90), ("标准", 100), ("明亮", 110)],
            ),
            "ai_voice_output": (
                "AI 语音播报",
                [("开启（功能准备中）", True), ("关闭", False)],
            ),
            "ai_model": (
                "模型版本",
                [
                    (DEEPSEEK_DEFAULT_MODEL, DEEPSEEK_DEFAULT_MODEL),
                    (DEEPSEEK_VISION_MODEL + "（图片实验版）", DEEPSEEK_VISION_MODEL),
                ],
            ),
        }
        for key, (label, values) in choices.items():
            control = QComboBox()
            if key == "ai_model":
                control.setEditable(True)
            for text, value in values:
                control.addItem(text, value)
            selected = preferences.get(key)
            index = control.findData(selected)
            if index >= 0:
                control.setCurrentIndex(index)
            elif selected and key == "ai_model":
                control.setEditText(str(selected))
            self.controls[key] = control
            form.addRow(label, control)
            if key == "ai_provider":
                control.hide()
                form.labelForField(control).hide()
        self._populate_models(str(preferences.get("ai_model") or ""))
        self.controls["ai_provider"].currentIndexChanged.connect(
            lambda _index: self._populate_models()
        )
        hint = QLabel(
            "AI 助手和语音智能记录统一使用 DeepSeek。语音播报开关先保存偏好，"
            "当前版本仅保留文字回答。含图片的对话会按云端视觉能力处理。"
        )
        hint.setWordWrap(True)
        hint.setObjectName("Hint")
        layout.addWidget(hint)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存设置")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _populate_models(self, selected: str = "") -> None:
        provider = "deepseek_cloud"
        model = self.controls["ai_model"]
        model.clear()
        model.setEditable(False)
        model.addItem(DEEPSEEK_DEFAULT_MODEL, DEEPSEEK_DEFAULT_MODEL)
        model.addItem(DEEPSEEK_VISION_MODEL + "（图片）", DEEPSEEK_VISION_MODEL)
        model.setCurrentIndex(max(0, model.findData(selected)))

    def values(self) -> dict[str, object]:
        result = {key: control.currentData() for key, control in self.controls.items()}
        model = self.controls["ai_model"]
        index = model.currentIndex()
        result["ai_model"] = (
            model.currentData()
            if (index >= 0 and model.currentText() == model.itemText(index))
            else model.currentText().strip()
        )
        return result
