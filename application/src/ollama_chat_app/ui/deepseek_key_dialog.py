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

from ..config import DEEPSEEK_API_KEY_URL, DEEPSEEK_DOCS_URL, DEEPSEEK_USAGE_URL
from ..providers.deepseek_cloud import DeepSeekCloudProvider
from ..workers.task import FunctionTask


class DeepSeekKeyDialog(QDialog):
    """Collect and minimally verify a user-owned DeepSeek API key."""

    def __init__(self, current_key: str | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("DeepSeek API Key")
        self.setModal(True)
        self.setMinimumWidth(560)

        self._current_key = current_key
        self._verified_key: str | None = None
        self.remove_requested = False
        self._tasks: list[FunctionTask] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)

        title = QLabel("使用你自己的 DeepSeek API Key")
        title.setObjectName("Title")
        layout.addWidget(title)

        description = QLabel(
            "请在 DeepSeek 官方平台创建 Key 后粘贴到这里。应用不会获取你的官网密码，"
            "Key 只保留在本次运行的内存中，关闭应用后自动清除。\n"
            "点击“极小请求验证并使用”会调用一次 deepseek-v4-flash，并限制为 2 个输出 token，"
            "因此可能产生极少量用量。"
        )
        description.setObjectName("Subtitle")
        description.setWordWrap(True)
        layout.addWidget(description)

        links = QHBoxLayout()
        for label, url in (
            ("获取 API Key", DEEPSEEK_API_KEY_URL),
            ("查看用量", DEEPSEEK_USAGE_URL),
            ("官方接口文档", DEEPSEEK_DOCS_URL),
        ):
            button = QPushButton(label)
            button.clicked.connect(
                lambda _checked=False, target=url: QDesktopServices.openUrl(QUrl(target))
            )
            links.addWidget(button)
        layout.addLayout(links)

        layout.addWidget(QLabel("API Key"))
        self.key_input = QLineEdit()
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_input.setPlaceholderText(
            "当前运行会话已有 Key；可留空继续使用" if current_key else "粘贴你的 API Key"
        )
        self.key_input.setClearButtonEnabled(True)
        layout.addWidget(self.key_input)

        self.status_label = QLabel("应用不会把 Key 写入数据库、日志或数据备份。")
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

        self.verify_button = QPushButton("极小请求验证并使用")
        self.verify_button.setObjectName("PrimaryButton")
        self.verify_button.clicked.connect(self._verify)
        buttons.addWidget(self.verify_button)
        layout.addLayout(buttons)

    @property
    def api_key(self) -> str | None:
        return self._verified_key

    def _request_remove(self) -> None:
        answer = QMessageBox.question(
            self,
            "删除 API Key",
            "清除当前会话中的这个 Key？这不会撤销 DeepSeek 官方平台上的 Key。",
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
        self._set_status("正在发送极小验证请求…", "StatusBusy")

        task = FunctionTask(DeepSeekCloudProvider(key).verify_key)
        self._tasks.append(task)
        task.signals.result.connect(lambda _answer: self._verification_succeeded(key))
        task.signals.error.connect(self._verification_failed)
        task.signals.finished.connect(lambda: self._task_finished(task))
        QThreadPool.globalInstance().start(task)

    def _verification_succeeded(self, key: str) -> None:
        self._verified_key = key
        self.accept()

    def _verification_failed(self, error: object) -> None:
        self.verify_button.setDisabled(False)
        self.key_input.setDisabled(False)
        self._show_error(str(error) or "DeepSeek API Key 验证失败，请检查网络和 Key。")

    def _show_error(self, message: str) -> None:
        self._set_status(message, "StatusError")

    def _set_status(self, message: str, object_name: str) -> None:
        self.status_label.setObjectName(object_name)
        self.status_label.setText(message)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _task_finished(self, task: FunctionTask) -> None:
        if task in self._tasks:
            self._tasks.remove(task)
