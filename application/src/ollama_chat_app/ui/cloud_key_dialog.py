from __future__ import annotations

from PySide6.QtCore import QThreadPool, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ..config import OLLAMA_API_KEY_URL
from ..providers.ollama_cloud import OllamaCloudProvider
from ..workers.task import FunctionTask


class CloudKeyDialog(QDialog):
    """Collect and verify a user-owned Ollama Cloud API key."""

    def __init__(self, current_key: str | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Ollama Cloud API Key")
        self.setModal(True)
        self.setMinimumWidth(520)

        self._current_key = current_key
        self._verified_key: str | None = None
        self._models: list[str] = []
        self.remove_requested = False
        self._tasks: list[FunctionTask] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)

        title = QLabel("使用你自己的 Ollama API Key")
        title.setObjectName("Title")
        layout.addWidget(title)

        description = QLabel(
            "应用不会获取你的 Ollama 网站密码。请在官方网页创建 Key，复制后粘贴到这里。\n"
            "Key 只保留在本次运行的内存中；关闭应用后会自动清除。"
        )
        description.setObjectName("Subtitle")
        description.setWordWrap(True)
        layout.addWidget(description)

        open_button = QPushButton("打开 Ollama API Key 官方页面")
        open_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(OLLAMA_API_KEY_URL)))
        layout.addWidget(open_button)

        layout.addWidget(QLabel("API Key"))
        self.key_input = QLineEdit()
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_input.setPlaceholderText(
            "当前运行会话已有 Key；可留空继续使用" if current_key else "粘贴你的 API Key"
        )
        self.key_input.setClearButtonEnabled(True)
        layout.addWidget(self.key_input)

        self.status_label = QLabel("验证成功后才能使用云端模式。")
        self.status_label.setObjectName("Hint")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        if current_key:
            remove_button = QPushButton("清除当前会话的 Key")
            remove_button.setObjectName("DangerButton")
            remove_button.clicked.connect(self._request_remove)
            buttons.addWidget(remove_button)
        buttons.addStretch(1)

        cancel_button = QPushButton("取消")
        cancel_button.clicked.connect(self.reject)
        buttons.addWidget(cancel_button)

        self.verify_button = QPushButton("检测并使用")
        self.verify_button.setObjectName("PrimaryButton")
        self.verify_button.clicked.connect(self._verify)
        buttons.addWidget(self.verify_button)
        layout.addLayout(buttons)

    @property
    def api_key(self) -> str | None:
        return self._verified_key

    @property
    def models(self) -> list[str]:
        return list(self._models)

    def _request_remove(self) -> None:
        answer = QMessageBox.question(
            self,
            "删除 API Key",
            "清除当前会话中的这个 Key？这不会撤销 Ollama 网站上的 Key。",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.remove_requested = True
            self.accept()

    def _verify(self) -> None:
        key = self.key_input.text().strip() or self._current_key
        if not key:
            self._show_error("请先粘贴 API Key。")
            return

        self.verify_button.setDisabled(True)
        self.key_input.setDisabled(True)
        self.status_label.setObjectName("StatusBusy")
        self.status_label.setText("正在验证 API Key 并读取云端模型…")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

        task = FunctionTask(OllamaCloudProvider(key).list_models)
        self._tasks.append(task)
        task.signals.result.connect(lambda models: self._verification_succeeded(key, models))
        task.signals.error.connect(self._verification_failed)
        task.signals.finished.connect(lambda: self._task_finished(task))
        QThreadPool.globalInstance().start(task)

    def _verification_succeeded(self, key: str, models: object) -> None:
        model_list = [str(item) for item in models] if isinstance(models, list) else []
        if not model_list:
            self._verification_failed(RuntimeError("当前账户没有返回可用的云端模型。"))
            return
        self._verified_key = key
        self._models = model_list
        self.accept()

    def _verification_failed(self, error: object) -> None:
        self.verify_button.setDisabled(False)
        self.key_input.setDisabled(False)
        self._show_error(str(error) or "API Key 验证失败，请检查网络和 Key。")

    def _show_error(self, message: str) -> None:
        self.status_label.setObjectName("StatusError")
        self.status_label.setText(message)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _task_finished(self, task: FunctionTask) -> None:
        if task in self._tasks:
            self._tasks.remove(task)
