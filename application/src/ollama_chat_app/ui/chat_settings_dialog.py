from __future__ import annotations

from collections.abc import Mapping

from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QVBoxLayout

from ..config import DEEPSEEK_DEFAULT_MODEL, DEEPSEEK_VISION_MODEL, DEFAULT_LOCAL_MODEL


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
                [
                    ("DeepSeek", "deepseek_cloud"),
                    ("本地 Ollama", "local"),
                    ("Ollama Cloud", "ollama_cloud"),
                ],
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
            "ai_model": (
                "模型版本",
                [
                    (DEEPSEEK_DEFAULT_MODEL, DEEPSEEK_DEFAULT_MODEL),
                    (DEEPSEEK_VISION_MODEL + "（图片实验版）", DEEPSEEK_VISION_MODEL),
                    (DEFAULT_LOCAL_MODEL, DEFAULT_LOCAL_MODEL),
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
        self._populate_models(str(preferences.get("ai_model") or ""))
        self.controls["ai_provider"].currentIndexChanged.connect(
            lambda _index: self._populate_models()
        )
        hint = QLabel(
            "含图片的 DeepSeek 对话将使用图片实验版；发送前会明确提示。其他服务请填写可用模型名称。"
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
        provider = self.controls["ai_provider"].currentData()
        model = self.controls["ai_model"]
        model.clear()
        model.setEditable(provider != "deepseek_cloud")
        if provider == "deepseek_cloud":
            model.addItem(DEEPSEEK_DEFAULT_MODEL, DEEPSEEK_DEFAULT_MODEL)
            model.addItem(DEEPSEEK_VISION_MODEL + "（图片实验版）", DEEPSEEK_VISION_MODEL)
            model.setCurrentIndex(max(0, model.findData(selected)))
        elif provider == "local":
            model.addItem(DEFAULT_LOCAL_MODEL, DEFAULT_LOCAL_MODEL)
            if selected and not selected.startswith("deepseek-"):
                model.setEditText(selected)
        else:
            # Cloud models are account-specific; opening settings performs no
            # network call. A verified model can be entered or loaded with Key.
            model.setEditText(selected if selected and not selected.startswith("deepseek-") else "")
            model.lineEdit().setPlaceholderText("填写当前 Ollama 账户可用的模型")

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
