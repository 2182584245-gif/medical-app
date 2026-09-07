from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QHideEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class _PasswordField(QWidget):
    def __init__(self, placeholder: str, label: str = "密码") -> None:
        super().__init__()
        self._label = label
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.line_edit = QLineEdit()
        self.line_edit.setPlaceholderText(placeholder)
        self.line_edit.setAccessibleName(label)
        self.line_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.line_edit.setMaxLength(256)
        layout.addWidget(self.line_edit, 1)

        self.visibility_button = QPushButton("显示密码")
        self.visibility_button.setCheckable(True)
        self.visibility_button.setAutoDefault(False)
        self.visibility_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.visibility_button.toggled.connect(self._set_password_visible)
        layout.addWidget(self.visibility_button)
        self._set_password_visible(False)

    def _set_password_visible(self, visible: bool) -> None:
        self.line_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        )
        action = "隐藏" if visible else "显示"
        self.visibility_button.setText(f"{action}密码")
        self.visibility_button.setAccessibleName(f"{action}{self._label}")
        self.visibility_button.setToolTip(f"{action}{self._label}")

    def hide_password(self) -> None:
        self.visibility_button.setChecked(False)

    def hideEvent(self, event: QHideEvent) -> None:
        self.hide_password()
        super().hideEvent(event)


def _card_page(title: str, subtitle: str) -> tuple[QWidget, QVBoxLayout]:
    page = QWidget()
    outer = QHBoxLayout(page)
    outer.setContentsMargins(32, 32, 32, 32)
    outer.addStretch(1)

    card = QFrame()
    card.setObjectName("Card")
    card.setMaximumWidth(460)
    card.setMinimumWidth(380)
    layout = QVBoxLayout(card)
    layout.setContentsMargins(36, 34, 36, 34)
    layout.setSpacing(14)

    title_label = QLabel(title)
    title_label.setObjectName("Title")
    title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(title_label)

    subtitle_label = QLabel(subtitle)
    subtitle_label.setObjectName("Subtitle")
    subtitle_label.setWordWrap(True)
    subtitle_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(subtitle_label)
    page.title_label = title_label
    page.subtitle_label = subtitle_label
    layout.addSpacing(10)

    outer.addWidget(card)
    outer.addStretch(1)
    return page, layout


class LoginPage(QWidget):
    login_requested = Signal(str, str)
    register_requested = Signal()
    import_requested = Signal()
    operator_setup_requested = Signal()
    connection_change_requested = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        page, layout = _card_page(
            "健康生活服务平台",
            "使用本机账户登录，查看生活记录、档案、服务与 AI 助手",
        )
        self._card = page
        self._import_available = True
        host_layout = QVBoxLayout(self)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.addWidget(page)

        self.connection_mode = QComboBox()
        self.connection_mode.addItem("本地模式：数据保存在此设备", "local")
        self.connection_mode.addItem("云端模式：使用云端账户及数据", "cloud")
        self.connection_mode.setAccessibleName("数据模式")
        layout.addWidget(self.connection_mode)
        self.cloud_url_input = QLineEdit()
        self.cloud_url_input.setPlaceholderText("https://你的云端服务域名")
        self.cloud_url_input.setMaxLength(2048)
        self.cloud_url_input.setAccessibleName("云端服务地址")
        layout.addWidget(self.cloud_url_input)
        self.connection_apply_button = QPushButton("应用所选模式")
        self.connection_apply_button.clicked.connect(lambda: self.connection_change_requested.emit(
            str(self.connection_mode.currentData()), self.cloud_url_input.text().strip()
        ))
        layout.addWidget(self.connection_apply_button)
        self.connection_mode.currentIndexChanged.connect(self._connection_selection_changed)
        self._connection_selection_changed()

        layout.addWidget(QLabel("用户名"))
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("请输入用户名")
        self.username_input.setMaxLength(50)
        layout.addWidget(self.username_input)

        layout.addWidget(QLabel("密码"))
        self._password_field = _PasswordField("请输入密码")
        self.password_input = self._password_field.line_edit
        self.password_visibility_button = self._password_field.visibility_button
        self.password_input.returnPressed.connect(self._submit)
        layout.addWidget(self._password_field)

        self.error_label = QLabel()
        self.error_label.setObjectName("Error")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

        self.login_button = QPushButton("登录")
        self.login_button.setObjectName("PrimaryButton")
        self.login_button.clicked.connect(self._submit)
        layout.addWidget(self.login_button)

        self.register_button = QPushButton("注册新用户")
        self.register_button.clicked.connect(self.register_requested)
        layout.addWidget(self.register_button)

        self.import_button = QPushButton("从备份导入数据")
        self.import_button.setToolTip("恢复由本应用“导出数据”生成的全部本地业务数据。")
        self.import_button.clicked.connect(self.import_requested)
        layout.addWidget(self.import_button)

        self.operator_setup_frame = QFrame()
        self.operator_setup_frame.setObjectName("OperatorSetupCard")
        setup_layout = QVBoxLayout(self.operator_setup_frame)
        setup_layout.setContentsMargins(14, 12, 14, 12)
        setup_layout.setSpacing(8)
        setup_hint = QLabel("首次使用：请先创建一个运营账户，用于管理顾问和服务。")
        setup_hint.setWordWrap(True)
        setup_layout.addWidget(setup_hint)
        self.operator_setup_button = QPushButton("首次配置运营账户")
        self.operator_setup_button.setObjectName("PrimaryButton")
        self.operator_setup_button.clicked.connect(self.operator_setup_requested)
        setup_layout.addWidget(self.operator_setup_button)
        self.operator_setup_frame.setStyleSheet(
            "QFrame#OperatorSetupCard { background: #fff8e1; border: 1px solid #e8b949; "
            "border-radius: 10px; }"
        )
        self.operator_setup_frame.hide()
        layout.addWidget(self.operator_setup_frame)

    def _submit(self) -> None:
        self.clear_error()
        self.login_requested.emit(self.username_input.text(), self.password_input.text())

    def set_busy(self, busy: bool) -> None:
        self.login_button.setDisabled(busy)
        self.register_button.setDisabled(busy)
        self.import_button.setEnabled(not busy and self._import_available)
        self.operator_setup_button.setDisabled(busy)
        self.username_input.setDisabled(busy)
        self.password_input.setDisabled(busy)
        self.password_visibility_button.setDisabled(busy)
        self.connection_mode.setDisabled(busy)
        self.cloud_url_input.setDisabled(busy)
        self.connection_apply_button.setDisabled(busy)
        self.login_button.setText("正在登录…" if busy else "登录")

    def show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()

    def clear_error(self) -> None:
        self.error_label.clear()
        self.error_label.hide()

    def clear_password(self) -> None:
        self.password_input.clear()
        self._password_field.hide_password()

    def focus_username(self) -> None:
        self.username_input.setFocus()

    def set_operator_setup_available(self, available: bool) -> None:
        self.operator_setup_frame.setVisible(available)

    def set_import_available(self, available: bool) -> None:
        self._import_available = available
        self.import_button.setEnabled(available)

    def _connection_selection_changed(self) -> None:
        self.cloud_url_input.setVisible(self.connection_mode.currentData() == "cloud")

    def set_connection_context(self, mode: str, base_url: str = "") -> None:
        self.connection_mode.setCurrentIndex(1 if mode == "cloud" else 0)
        self.cloud_url_input.setText(base_url)
        self._card.subtitle_label.setText(
            "云端模式：账号、健康记录、聊天和上传附件保存在应用服务方云端数据库。"
            "AI 提供商是独立服务，仅在使用 AI 并确认后发送相应内容；不会自动迁移本地历史。"
            if mode == "cloud" else
            "当前为本地模式：使用本机账户，数据保存在本地便携数据库中。"
        )


class RegisterPage(QWidget):
    create_requested = Signal(str, str, str)
    back_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        page, layout = _card_page(
            "注册本机账户",
            "账户和个人数据保存在本地便携数据库中；当前版本没有忘记密码功能",
        )
        self._card = page
        host_layout = QVBoxLayout(self)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.addWidget(page)

        layout.addWidget(QLabel("用户名"))
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("2–50 个字符")
        self.username_input.setMaxLength(50)
        layout.addWidget(self.username_input)

        layout.addWidget(QLabel("密码"))
        self._password_field = _PasswordField("至少 8 个字符")
        self.password_input = self._password_field.line_edit
        self.password_visibility_button = self._password_field.visibility_button
        layout.addWidget(self._password_field)

        layout.addWidget(QLabel("确认密码"))
        self._confirm_field = _PasswordField("再次输入密码", "确认密码")
        self.confirm_input = self._confirm_field.line_edit
        self.confirm_visibility_button = self._confirm_field.visibility_button
        self.confirm_input.returnPressed.connect(self._submit)
        layout.addWidget(self._confirm_field)

        self.error_label = QLabel()
        self.error_label.setObjectName("Error")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

        self.create_button = QPushButton("创建账户")
        self.create_button.setObjectName("PrimaryButton")
        self.create_button.clicked.connect(self._submit)
        layout.addWidget(self.create_button)

        self.back_button = QPushButton("返回登录")
        self.back_button.clicked.connect(self.back_requested)
        layout.addWidget(self.back_button)

    def _submit(self) -> None:
        self.clear_error()
        self.create_requested.emit(
            self.username_input.text(),
            self.password_input.text(),
            self.confirm_input.text(),
        )

    def set_busy(self, busy: bool) -> None:
        for widget in (
            self.username_input,
            self.password_input,
            self.password_visibility_button,
            self.confirm_input,
            self.confirm_visibility_button,
            self.create_button,
            self.back_button,
        ):
            widget.setDisabled(busy)
        self.create_button.setText("正在创建…" if busy else "创建账户")

    def show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()

    def clear_error(self) -> None:
        self.error_label.clear()
        self.error_label.hide()

    def reset(self) -> None:
        self.username_input.clear()
        self.password_input.clear()
        self.confirm_input.clear()
        self._password_field.hide_password()
        self._confirm_field.hide_password()
        self.clear_error()

    def set_cloud_mode(self, enabled: bool) -> None:
        self._card.title_label.setText("注册云端账户" if enabled else "注册本机账户")
        self._card.subtitle_label.setText(
            "将创建独立云端账户；账号、健康记录、聊天和上传附件保存在应用服务方云端。"
            "AI 提供商是另一独立服务；本机历史不自动迁移，暂不提供忘记密码功能。"
            if enabled else
            "账户和个人数据保存在本地便携数据库中；当前版本没有忘记密码功能"
        )
