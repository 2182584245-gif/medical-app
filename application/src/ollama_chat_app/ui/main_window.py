from __future__ import annotations

from importlib import import_module
from pathlib import Path

from PySide6.QtCore import QStandardPaths, QThreadPool, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDateEdit,
    QDateTimeEdit,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..config import APP_NAME
from ..security.secret_store import SecretStore
from ..services.auth import AuthService
from ..services.backup import PortableBackupService
from ..services.chat import ChatService
from ..services.health import HealthService
from ..workers.task import FunctionTask
from .auth_pages import LoginPage, RegisterPage
from .chat_page import ChatPage
from .health_workspace import HealthWorkspace
from .operator_setup_dialog import OperatorSetupDialog


class _UnavailableRoleWorkspace(QWidget):
    """Safe placeholder used only while a role-specific UI module is absent."""

    logout_requested = Signal()
    export_requested = Signal()
    import_requested = Signal()

    def __init__(self, role_label: str) -> None:
        super().__init__()
        self.current_user: object | None = None
        outer = QHBoxLayout(self)
        outer.addStretch(1)
        card = QFrame()
        card.setObjectName("Card")
        card.setMaximumWidth(620)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(36, 34, 36, 34)
        title = QLabel(f"{role_label}工作台尚未加载")
        title.setObjectName("Title")
        layout.addWidget(title)
        message = QLabel("当前构建缺少该角色的工作台模块，请更新到完整版本后再使用。")
        message.setObjectName("Subtitle")
        message.setWordWrap(True)
        layout.addWidget(message)
        logout_button = QPushButton("退出登录")
        logout_button.setObjectName("PrimaryButton")
        logout_button.clicked.connect(self.logout_requested)
        layout.addWidget(logout_button)
        outer.addWidget(card)
        outer.addStretch(1)

    def start_session(self, user: object) -> None:
        self.current_user = user

    def end_session(self) -> None:
        self.current_user = None


class MainWindow(QMainWindow):
    connection_change_requested = Signal(str, str)

    def __init__(
        self,
        auth_service: AuthService,
        chat_service: ChatService,
        secret_store: SecretStore,
        backup_service: PortableBackupService | None = None,
        health_service: HealthService | None = None,
        service_management_service: object | None = None,
        advisor_workspace: QWidget | None = None,
        operator_workspace: QWidget | None = None,
        commerce_service: object | None = None,
        file_management_service: object | None = None,
        ai_assistant_service: object | None = None,
        cloud_base_url: str = "",
        preferences_service: object | None = None,
        appointment_service: object | None = None,
        endpoint_store=None,
    ) -> None:
        super().__init__()
        self.auth_service = auth_service
        self.chat_service = chat_service
        self.secret_store = secret_store
        self.backup_service = backup_service
        self.health_service = health_service
        self.service_management_service = service_management_service
        self.commerce_service = commerce_service
        self.file_management_service = file_management_service
        self.ai_assistant_service = ai_assistant_service
        self.preferences_service = preferences_service
        self._current_user = None
        self.cloud_sync_service = None
        self.local_migration_source_path = None
        self._last_snapshot_revision = None
        self.cloud_mode = bool(getattr(auth_service, "uses_network", False))
        self._tasks: list[FunctionTask] = []
        self._active_workspace: QWidget | None = None

        self.setWindowTitle(APP_NAME)
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else None
        self.resize(
            min(1500, available.width() - 40) if available else 1500,
            min(960, available.height() - 60) if available else 960,
        )
        self.setMinimumSize(980, 680)

        self.pages = QStackedWidget()
        self.setCentralWidget(self.pages)

        self.login_page = LoginPage(endpoint_store=endpoint_store)
        self.login_page.set_import_available(False)
        self.login_page.set_connection_context(
            "cloud" if self.cloud_mode else "local", cloud_base_url
        )
        self.register_page = RegisterPage()
        self.register_page.set_cloud_mode(self.cloud_mode)
        self.chat_page = ChatPage(
            chat_service,
            secret_store,
            backup_service,
            ai_assistant_service,
            file_management_service,
            preferences_service=preferences_service,
        )
        self.health_workspace = (
            HealthWorkspace(
                health_service,
                self.chat_page,
                backup_service,
                commerce_service=commerce_service,
                file_management_service=file_management_service,
                preferences_service=preferences_service,
                ai_service=ai_assistant_service,
                appointment_service=appointment_service,
            )
            if health_service is not None
            else None
        )
        self.member_workspace = self.health_workspace or self.chat_page
        self.advisor_workspace = (
            advisor_workspace
            if advisor_workspace is not None
            else self._create_role_workspace("advisor")
        )
        self.operator_workspace = (
            operator_workspace
            if operator_workspace is not None
            else self._create_role_workspace("operator")
        )
        self._role_workspaces: dict[str, QWidget] = {
            "member": self.member_workspace,
            "advisor": self.advisor_workspace,
            "operator": self.operator_workspace,
        }
        self.pages.addWidget(self.login_page)
        self.pages.addWidget(self.register_page)
        for workspace in self._role_workspaces.values():
            if self.pages.indexOf(workspace) < 0:
                self.pages.addWidget(workspace)

        self.login_page.login_requested.connect(self._login)
        self.login_page.set_login_options_enabled(self.cloud_mode)
        self.login_page.login_options_requested.connect(self._login_with_options)
        self.login_page.register_requested.connect(self._show_register)
        self.login_page.import_requested.connect(self._import_data_from_login)
        self.login_page.operator_setup_requested.connect(self._open_operator_setup)
        self.login_page.connection_change_requested.connect(self._request_connection_change)
        self.register_page.create_requested.connect(self._register)
        self.register_page.back_requested.connect(self._show_login)
        connected: set[int] = set()
        for workspace in self._role_workspaces.values():
            if id(workspace) in connected:
                continue
            connected.add(id(workspace))
            self._connect_workspace_signals(workspace)
        # ChatPage emits this after a successful data import. The idempotent
        # logout method safely handles the member workspace forwarding it too.
        self.chat_page.logout_requested.connect(self._logout)

        # The old whole-database ZIP import/export UI has been retired.
        # Internal safety snapshots remain available to migration tooling.
        for surface in (self.login_page, self.chat_page, *self._role_workspaces.values()):
            for name in ("backup_button", "export_button", "import_button"):
                button = getattr(surface, name, None)
                if button is not None:
                    button.hide()

        toolbar = QToolBar("应用设置", self)
        toolbar.setMovable(False)
        self.offline_status_label = QLabel()
        self.offline_status_label.setWordWrap(True)
        self.offline_status_label.setMaximumWidth(620)
        self.offline_status_label.setAccessibleName("离线授权与待提交状态")
        self.offline_status_label.setStyleSheet(
            "QLabel { color: #654c22; background: #fff1cd; padding: 8px; border-radius: 8px; }"
        )
        self.offline_status_label.hide()
        toolbar.addWidget(self.offline_status_label)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)
        self.sync_button = QPushButton("数据与同步")
        self.sync_button.clicked.connect(self._open_data_sync)
        toolbar.addWidget(self.sync_button)
        self.timezone_button = QPushButton("时区 · 北京")
        self.timezone_button.setAccessibleName("设置时区，默认北京时间")
        self.timezone_button.clicked.connect(self._choose_timezone)
        toolbar.addWidget(self.timezone_button)
        self.addToolBar(toolbar)
        if hasattr(self.chat_page, "preferences_changed"):
            self.chat_page.preferences_changed.connect(self.apply_preferences)
        if self.health_workspace is not None:
            self.health_workspace.my_platform_page.preferences_changed.connect(
                self.apply_preferences
            )

        self._show_login()

    def configure_cloud_sync(self, client, *, sync_service=None):
        from ..paths import user_data_dir
        from ..services.cloud_sync import CloudMirrorStore, CloudSyncService

        self.cloud_sync_service = sync_service or CloudSyncService(
            client, CloudMirrorStore(user_data_dir() / "cloud-mirrors"), self
        )
        self.cloud_sync_service.setParent(self)
        self.cloud_sync_service.status_changed.connect(self._sync_status_changed)
        self.cloud_sync_service.authorization_lost.connect(self._cloud_authorization_lost)
        self.cloud_sync_service.snapshot_changed.connect(self._snapshot_arrived)
        self.cloud_sync_service.reauthentication_required.connect(self._reauthentication_required)
        self.cloud_sync_service.outbox_changed.connect(self._outbox_changed)
        self.chat_page.data_changed.connect(self.cloud_sync_service.sync_now)

    def _sync_status_changed(self, state):
        labels = {
            "online": "已同步",
            "syncing": "同步中",
            "connecting": "连接中",
            "offline": "离线镜像",
            "unauthorized": "需登录",
            "blocked": "需检查",
        }
        self.sync_button.setText("数据与同步 · " + labels.get(state["state"], "未连接"))
        self.sync_button.setToolTip(state["message"])
        pending, conflicts = state.get("pending_count", 0), state.get("conflict_count", 0)
        parts = []
        if state["state"] == "offline":
            from ..time_utils import format_beijing

            parts.append("当前离线，显示上次同步的记录")
            expiry = state.get("offline_expires_at")
            if expiry:
                parts.append("离线查看到期：" + format_beijing(expiry))
        if pending:
            parts.append(f"{pending} 项尚未提交云端（其中 {conflicts} 项需处理）")
        self.offline_status_label.setText("\n".join(parts))
        self.offline_status_label.setVisible(bool(parts))

    def _outbox_changed(self, _summary):
        if self.cloud_sync_service is not None:
            self._sync_status_changed(self.cloud_sync_service.state())

    def _reauthentication_required(self, message):
        if self._active_workspace is not None:
            self._logout()
        self.login_page.allow_offline_checkbox.setChecked(False)
        self.login_page.clear_password()
        self.login_page.show_error(message)

    def _snapshot_arrived(self, snapshot):
        from .snapshot_refresh import refresh_current_workspace

        refresh_current_workspace(self, snapshot)
        if snapshot["revision"] == self._last_snapshot_revision:
            return
        self._last_snapshot_revision = snapshot["revision"]
        self.statusBar().showMessage("已载入完整云端镜像；离线修改会单独列为待提交。", 6000)

    def _cloud_authorization_lost(self):
        if self._active_workspace is not None:
            self._logout()
        self.login_page.show_error("云端身份或权限已失效。已锁定镜像，请重新在线登录。")

    def _open_data_sync(self):
        if not self.cloud_mode:
            QMessageBox.information(
                self,
                "本地数据",
                "当前使用本机数据库。\n\n"
                "要连接云端，请退出登录，在登录页选择阿里云或 Supabase 数据源并登录云端账户。"
                "之后可在这里验证本机源账户、预览首次迁移；不会自动上传历史数据。\n\n"
                "原一键打包功能已移除，数据迁移不会删除本机原件。",
            )
            return
        if self._current_user is None or self.cloud_sync_service is None:
            QMessageBox.information(self, "数据与同步", "请先在线登录云端账户。")
            return
        from .data_sync_dialog import DataSyncDialog

        options = {}
        source_path = self.local_migration_source_path
        if source_path is not None and self._current_user.role_code == "member":
            from ..paths import user_data_dir
            from ..services.cloud_migration import (
                MemberMigrationPlanner,
                MemberMigrationService,
                MigrationJournal,
            )
            from .local_migration_auth import authenticate_local_migration

            journal = MigrationJournal(
                user_data_dir() / "cloud-mirrors" / "migration-journal.sqlite"
            )
            options = {
                "planner": MemberMigrationPlanner(source_path, journal),
                "migration_service": MemberMigrationService(
                    self.cloud_client, journal, sync_service=self.cloud_sync_service
                ),
                "source_authenticator": lambda: authenticate_local_migration(source_path, self),
            }
        DataSyncDialog(self.cloud_sync_service, self, **options).exec()

    def apply_preferences(self, preferences: dict) -> None:
        from ..time_utils import set_display_timezone
        from .theme import build_style
        from .time_fields import current_qtimezone

        name = str(preferences.get("timezone", "Asia/Shanghai"))
        set_display_timezone(name)
        self.timezone_button.setText("时区 · " + ("北京" if name == "Asia/Shanghai" else name))
        self.setStyleSheet(
            build_style(
                preferences.get("font_size", 20),
                preferences.get("theme_color", "sage"),
                preferences.get("brightness", 100),
            )
        )
        for editor in self.findChildren(QDateTimeEdit):
            if isinstance(editor, QDateEdit):
                # Birthdays and other civil dates are not instants.
                continue
            # A timezone change relabels an instant; it must not reschedule it.
            instant = editor.dateTime()
            editor.setTimeZone(current_qtimezone())
            editor.setDateTime(instant.toTimeZone(current_qtimezone()))
        for workspace in self._role_workspaces.values():
            apply = getattr(workspace, "apply_preferences", None)
            if callable(apply):
                apply(preferences)
        self.statusBar().showMessage("时间按所选时区显示；数据库统一保存标准时间。", 6000)

    def _choose_timezone(self) -> None:
        from ..time_utils import display_timezone_name
        from .my_platform import TIMEZONES

        labels = [label for label, _ in TIMEZONES]
        names = [name for _, name in TIMEZONES]
        current = names.index(display_timezone_name()) if display_timezone_name() in names else 0
        chosen, accepted = QInputDialog.getItem(
            self, "时区", "请选择显示与录入时间使用的时区：", labels, current, False
        )
        if not accepted:
            return
        patch = {"timezone": names[labels.index(chosen)]}
        try:
            if self._current_user is not None and self.preferences_service is not None:
                patch = self.preferences_service.update(self._current_user.id, patch)
                from .offline_feedback import show_queued_result

                if show_queued_result(patch, self):
                    return
            self.apply_preferences(patch)
            if self.health_workspace is self._active_workspace:
                self.health_workspace.today_page.refresh()
                self.health_workspace.my_platform_page.refresh()
        except Exception:
            QMessageBox.warning(self, "时区未保存", "无法保存时区，请检查连接后重试。")

    def _request_connection_change(self, mode: str, base_url: str) -> None:
        if self._tasks or self._active_workspace is not None:
            self.login_page.show_error("请先等待当前操作结束并退出登录，再切换数据模式。")
            return
        if mode not in {"local", "cloud"}:
            return
        if mode == "cloud":
            from ..services.cloud_client import CloudAPIError, validate_base_url

            try:
                base_url = validate_base_url(base_url)
            except CloudAPIError as error:
                self.login_page.show_error(str(error))
                return
        self.login_page.clear_password()
        self.register_page.reset()
        self.secret_store.clear_api_keys()
        self.connection_change_requested.emit(mode, base_url)

    def _create_role_workspace(self, role_code: str) -> QWidget:
        role_labels = {"advisor": "家庭生活顾问", "operator": "运营"}
        if self.service_management_service is None:
            return _UnavailableRoleWorkspace(role_labels[role_code])
        workspace_class = self._load_role_workspace_class(role_code)
        if workspace_class is None:
            return _UnavailableRoleWorkspace(role_labels[role_code])
        try:
            workspace = workspace_class(
                self.service_management_service,
                self.backup_service,
                commerce_service=self.commerce_service,
            )
            if not isinstance(workspace, QWidget):
                raise TypeError("角色工作台必须是 QWidget")
            return workspace
        except Exception:
            # A partially installed role module must not break member login or
            # accidentally expose the member workspace to staff accounts.
            return _UnavailableRoleWorkspace(role_labels[role_code])

    @staticmethod
    def _load_role_workspace_class(role_code: str) -> type[QWidget] | None:
        class_name = "AdvisorWorkspace" if role_code == "advisor" else "OperatorWorkspace"
        module_name = f"ollama_chat_app.ui.{role_code}_workspace"
        try:
            module = import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name == module_name:
                return None
            raise
        workspace_class = getattr(module, class_name, None)
        return workspace_class if isinstance(workspace_class, type) else None

    def _connect_workspace_signals(self, workspace: QWidget) -> None:
        if hasattr(workspace, "logout_requested"):
            workspace.logout_requested.connect(self._logout)
        if hasattr(workspace, "export_requested"):
            workspace.export_requested.connect(self.chat_page._export_data_backup)
        if hasattr(workspace, "import_requested"):
            workspace.import_requested.connect(self.chat_page._import_data_backup)

    def _show_login(self) -> None:
        self.register_page.reset()
        self.login_page.clear_error()
        self.login_page.clear_password()
        self.pages.setCurrentWidget(self.login_page)
        self._refresh_operator_setup_availability()
        self.login_page.focus_username()

    def _show_register(self) -> None:
        self.login_page.clear_error()
        self.register_page.reset()
        self.register_page.username_input.setText(self.login_page.username_input.text().strip())
        self.pages.setCurrentWidget(self.register_page)
        self.register_page.username_input.setFocus()

    def _login_with_options(self, username: str, password: str, options: dict) -> None:
        self._login(
            username, password,
            allow_offline=options.get("allow_offline") is True,
            remember_offline=options.get("remember_offline") is True,
        )

    def _login(
        self, username: str, password: str, *, allow_offline=False, remember_offline=False
    ) -> None:
        if not username.strip() or not password:
            self.login_page.show_error("用户名和密码不能为空。")
            return
        self.login_page.set_busy(True)
        options = (
            {"allow_offline": allow_offline, "remember_offline": remember_offline}
            if self.cloud_mode else {}
        )
        task = FunctionTask(self.auth_service.authenticate, username, password, **options)
        self._start_task(
            task,
            self._login_succeeded,
            lambda error: self.login_page.show_error(str(error) or "登录失败。"),
            lambda: self.login_page.set_busy(False),
        )

    def _login_succeeded(self, user: object) -> None:
        self.login_page.clear_password()
        role_code = str(getattr(user, "role_code", "member"))
        workspace = self._role_workspaces.get(role_code)
        if workspace is None:
            self.login_page.show_error("账户角色无效，请联系运营人员检查账户。")
            self.pages.setCurrentWidget(self.login_page)
            return
        if self.cloud_sync_service is not None:
            self._last_snapshot_revision = None
            try:
                self.cloud_sync_service.start(user.id, role_code=user.role_code)
            except Exception:
                self.cloud_sync_service.stop()
                self.auth_service.clear_session()
                self.login_page.show_error("离线资料尚未准备好或授权已失效，请连接网络后登录。")
                self.pages.setCurrentWidget(self.login_page)
                return
        self._active_workspace = workspace
        self._current_user = user
        if self.preferences_service is not None:
            try:
                self.apply_preferences(self.preferences_service.get(user.id))
            except Exception:
                self.statusBar().showMessage("个人设置暂时未能读取，使用默认北京时间。")
        # Staff workspaces do not embed ChatPage, but its existing portable
        # backup handlers require an authenticated in-memory user marker.
        if role_code != "member":
            self.chat_page.current_user = user
        workspace.start_session(user)
        self.pages.setCurrentWidget(workspace)

    def _refresh_operator_setup_availability(self) -> None:
        has_operator = getattr(self.auth_service, "has_operator", None)
        if not callable(has_operator):
            self.login_page.set_operator_setup_available(False)
            return
        try:
            available = not bool(has_operator())
        except Exception as exc:
            self.login_page.set_operator_setup_available(False)
            self.login_page.show_error(f"无法检查首次配置状态：{exc}")
            return
        self.login_page.set_operator_setup_available(available)

    def _open_operator_setup(self) -> None:
        has_operator = getattr(self.auth_service, "has_operator", None)
        bootstrap_operator = getattr(self.auth_service, "bootstrap_operator", None)
        if not callable(has_operator) or not callable(bootstrap_operator):
            self.login_page.show_error("当前版本没有启用运营账户初始化功能。")
            return
        try:
            if has_operator():
                self._refresh_operator_setup_availability()
                self.login_page.show_error("运营账户已经完成首次配置，不能再次初始化。")
                return
        except Exception as exc:
            self.login_page.show_error(f"无法检查首次配置状态：{exc}")
            return

        dialog = OperatorSetupDialog(self.login_page.username_input.text().strip(), self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return

        self.login_page.clear_error()
        self.login_page.set_busy(True)
        task = FunctionTask(bootstrap_operator, dialog.username, dialog.password)
        self._start_task(
            task,
            self._operator_setup_succeeded,
            self._operator_setup_failed,
            lambda: self.login_page.set_busy(False),
        )

    def _operator_setup_succeeded(self, user: object) -> None:
        self._refresh_operator_setup_availability()
        username = str(getattr(user, "username", ""))
        QMessageBox.information(
            self,
            "首次配置完成",
            "首个运营账户已经创建，请使用该账户登录运营工作台。",
        )
        self._show_login()
        self.login_page.username_input.setText(username)
        self.login_page.password_input.setFocus()

    def _operator_setup_failed(self, error: object) -> None:
        self._refresh_operator_setup_availability()
        self.login_page.show_error(str(error) or "创建运营账户失败。")

    def _register(self, username: str, password: str, confirmation: str) -> None:
        if password != confirmation:
            self.register_page.show_error("两次输入的密码不一致。")
            return
        self.register_page.set_busy(True)
        task = FunctionTask(self.auth_service.register, username, password)
        self._start_task(
            task,
            lambda user: self._registration_succeeded(user),
            lambda error: self.register_page.show_error(str(error) or "注册失败。"),
            lambda: self.register_page.set_busy(False),
        )

    def _registration_succeeded(self, user: object) -> None:
        username = getattr(user, "username", "")
        QMessageBox.information(self, "注册成功", "账户已经创建，请使用新账户登录。")
        self._show_login()
        self.login_page.username_input.setText(username)
        self.login_page.password_input.setFocus()

    def _import_data_from_login(self) -> None:
        if self.backup_service is None:
            self.login_page.show_error("当前运行方式没有启用数据导入功能。")
            return

        download_dir = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DownloadLocation
        )
        start_dir = Path(download_dir) if download_dir else Path.home()
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "选择数据备份",
            str(start_dir),
            "ZIP 压缩包 (*.zip)",
        )
        if not selected:
            return

        answer = QMessageBox.warning(
            self,
            "确认导入数据",
            "导入后，当前应用中的全部账户、聊天与健康生活数据会被所选备份替换。\n\n"
            "应用会先自动保存一份当前数据备份；如果检查或导入失败，当前数据"
            "不会被替换。所选 ZIP 必须由本应用的“导出数据”功能生成。\n\n"
            "云端 API Key 不会随备份迁移。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.login_page.clear_error()
        self.login_page.set_busy(True)
        task = FunctionTask(self.backup_service.import_archive, Path(selected).expanduser())
        self._start_task(
            task,
            self._login_import_succeeded,
            self._login_import_failed,
            lambda: self.login_page.set_busy(False),
        )

    def _login_import_succeeded(self, result: object) -> None:
        safety_backup = Path(getattr(result, "safety_backup_path", ""))
        user_count = int(getattr(result, "user_count", 0))
        message_count = int(getattr(result, "message_count", 0))
        self.secret_store.clear_api_keys()
        self.login_page.clear_password()
        QMessageBox.information(
            self,
            "导入完成",
            f"已导入 {user_count} 个账户、{message_count} 条聊天消息。\n\n"
            f"导入前的当前数据已自动保存到：\n{safety_backup}\n\n"
            "请使用备份中的账户登录；云端 API Key 需要重新输入。",
        )
        self._show_login()

    def _login_import_failed(self, error: object) -> None:
        message = str(error) or "导入数据备份失败，当前数据没有被替换。"
        self.login_page.show_error(message)
        QMessageBox.warning(self, "导入失败", message)

    def _logout(self) -> None:
        workspace = self._active_workspace
        if workspace is None:
            return
        if self.cloud_sync_service is not None:
            self.cloud_sync_service.stop()
        self._active_workspace = None
        self._current_user = None
        self.apply_preferences({})
        workspace.end_session()
        if workspace is not self.member_workspace:
            self.chat_page.end_session()
        self._show_login()
        if self.cloud_mode:
            # Login stays disabled until the old bearer has been revoked/cleared.
            # Network failure still clears local credentials in the client finally.
            self.login_page.set_busy(True)
            task = FunctionTask(self.auth_service.logout)
            self._start_task(
                task,
                lambda _value: None,
                lambda _error: self.login_page.show_error(
                    "本机登录已清除，但云端注销未获确认；原令牌将在到期后失效。"
                ),
                lambda: self.login_page.set_busy(False),
            )

    def _start_task(
        self,
        task: FunctionTask,
        on_result,
        on_error,
        on_finished=None,
    ) -> None:
        self._tasks.append(task)
        task.signals.result.connect(on_result)
        task.signals.error.connect(on_error)
        if on_finished:
            task.signals.finished.connect(on_finished)
        task.signals.finished.connect(lambda: self._task_finished(task))
        QThreadPool.globalInstance().start(task)

    def _task_finished(self, task: FunctionTask) -> None:
        if task in self._tasks:
            self._tasks.remove(task)
