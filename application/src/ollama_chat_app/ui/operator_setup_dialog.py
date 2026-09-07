from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from ..security.passwords import PasswordPolicyError, validate_new_password


class OperatorSetupDialog(QDialog):
    """Collect and locally validate credentials for the first operator."""

    def __init__(self, suggested_username: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("首次配置运营账户")
        self.setModal(True)
        self.setMinimumWidth(500)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)

        title = QLabel("创建首个运营账户")
        title.setObjectName("Title")
        layout.addWidget(title)
        description = QLabel(
            "该账户可以管理顾问、会员和服务数据。系统只允许在尚无运营账户时完成首次配置。"
        )
        description.setObjectName("Subtitle")
        description.setWordWrap(True)
        layout.addWidget(description)

        layout.addWidget(QLabel("运营用户名"))
        self.username_input = QLineEdit(suggested_username)
        self.username_input.setPlaceholderText("2–64 个字符")
        self.username_input.setMaxLength(64)
        layout.addWidget(self.username_input)

        layout.addWidget(QLabel("密码"))
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setPlaceholderText("至少 8 个字符")
        self.password_input.setMaxLength(1024)
        layout.addWidget(self.password_input)

        layout.addWidget(QLabel("确认密码"))
        self.confirm_input = QLineEdit()
        self.confirm_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm_input.setPlaceholderText("再次输入密码")
        self.confirm_input.setMaxLength(1024)
        self.confirm_input.returnPressed.connect(self._validate_and_accept)
        layout.addWidget(self.confirm_input)

        self.error_label = QLabel()
        self.error_label.setObjectName("Error")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("创建运营账户")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self._validate_and_accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    @property
    def username(self) -> str:
        return self.username_input.text().strip()

    @property
    def password(self) -> str:
        return self.password_input.text()

    def _validate_and_accept(self) -> None:
        self._show_error("")
        if not self.username:
            self._show_error("运营用户名不能为空。")
            return
        if self.password != self.confirm_input.text():
            self._show_error("两次输入的密码不一致。")
            return
        try:
            validate_new_password(self.password)
        except PasswordPolicyError as exc:
            self._show_error(f"{exc}。")
            return
        self.accept()

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))


__all__ = ["OperatorSetupDialog"]
