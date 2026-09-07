from __future__ import annotations

import logging
import sys
from importlib import import_module
from logging.handlers import RotatingFileHandler

from PySide6.QtCore import QEvent, QLockFile, QObject
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from .cloud_config import configured_cloud_base_url
from .config import APP_NAME, APP_VERSION
from .data.database import Database
from .paths import (
    database_path,
    is_frozen_app,
    legacy_database_path,
    log_path,
    user_data_dir,
)
from .security.secret_store import SecretStore
from .services.ai_assistant import AiAssistantService
from .services.auth import AuthService
from .services.backup import PortableBackupService, migrate_legacy_database
from .services.chat import ChatService
from .services.cloud_client import CloudAPIClient, CloudAPIError, validate_base_url
from .services.commerce import CommerceService
from .services.file_management import FileManagementService
from .services.health import HealthService
from .services.remote_services import (
    RemoteAiAssistantService,
    RemoteAuthService,
    RemoteChatService,
    RemoteCommerceService,
    RemoteFileManagementService,
    RemoteHealthService,
    RemoteServiceManagementService,
)
from .services.service_management import ServiceManagementService
from .ui.main_window import MainWindow
from .ui.theme import APP_STYLE


def configure_logging() -> None:
    handler = RotatingFileHandler(
        log_path(),
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    # HTTPX INFO logs contain URLs; query parameters may contain health filters.
    # Do not persist transport request/response details or headers in app logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _optional_role_workspace(
    role_code: str,
    service_management: ServiceManagementService,
    backup_service: PortableBackupService | None,
    commerce_service: CommerceService,
    ai_assistant_service: AiAssistantService | None = None,
    secret_store: SecretStore | None = None,
) -> object | None:
    """Build a role UI from its fixed module path when that module is installed."""

    class_name = "AdvisorWorkspace" if role_code == "advisor" else "OperatorWorkspace"
    module_name = f"ollama_chat_app.ui.{role_code}_workspace"
    try:
        module = import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            return None
        raise
    workspace_class = getattr(module, class_name, None)
    if not isinstance(workspace_class, type):
        return None
    keyword_arguments: dict[str, object] = {"commerce_service": commerce_service}
    if role_code == "advisor":
        keyword_arguments.update(
            ai_assistant_service=ai_assistant_service,
            secret_store=secret_store,
        )
    elif role_code == "operator":
        keyword_arguments.update(ai_assistant_service=ai_assistant_service)
    return workspace_class(service_management, backup_service, **keyword_arguments)


def build_desktop_window(
    mode: str,
    *,
    local_database: Database,
    cloud_base_url: str = "",
    client_factory=None,
) -> MainWindow:
    """Construct seven mutually consistent services without contacting the cloud.

    Cloud mode never receives the local database or backup service. Rebuilding
    the whole UI on an explicit mode switch prevents stale service references
    from mixing local integer IDs and independent cloud integer IDs.
    """
    client = None
    if mode not in {"local", "cloud"}:
        raise CloudAPIError("configuration")
    try:
        if mode == "cloud":
            cloud_base_url = validate_base_url(cloud_base_url)
            client = (client_factory or CloudAPIClient)(cloud_base_url)
            auth = RemoteAuthService(client)
            chat = RemoteChatService(client)
            health = RemoteHealthService(client)
            management = RemoteServiceManagementService(client)
            commerce = RemoteCommerceService(client)
            files = RemoteFileManagementService(client)
            ai = RemoteAiAssistantService(client)
            backup = None
        else:
            auth = AuthService(local_database)
            chat = ChatService(local_database)
            health = HealthService(local_database)
            management = ServiceManagementService(local_database)
            commerce = CommerceService(local_database)
            files = FileManagementService(local_database)
            ai = AiAssistantService(local_database, health)
            backup = PortableBackupService(local_database)
        secrets = SecretStore()
        advisor = _optional_role_workspace("advisor", management, backup, commerce, ai, secrets)
        operator = _optional_role_workspace("operator", management, backup, commerce, ai)
        window = MainWindow(
            auth_service=auth, chat_service=chat, secret_store=secrets,
            backup_service=backup, health_service=health,
            service_management_service=management, advisor_workspace=advisor,
            operator_workspace=operator, commerce_service=commerce,
            file_management_service=files, ai_assistant_service=ai,
            cloud_base_url=cloud_base_url,
        )
        window.cloud_client = client
        window.setWindowTitle(APP_NAME + (" — 云端模式" if mode == "cloud" else " — 本地模式"))
        return window
    except Exception:
        if client is not None:
            client.close()
        raise


def _window_has_pending_work(window: MainWindow) -> bool:
    # Only app-created QWidget state is inspected, never payload-supplied names.
    for widget in (window, *window.findChildren(QWidget)):
        if (getattr(widget, "_tasks", None)
                or getattr(widget, "_request_running", False)
                or getattr(widget, "_backup_running", False)):
            return True
    return False


class DesktopWindowController(QObject):
    """Own the active window and close its memory-only cloud client on replacement/exit."""

    def __init__(self, local_database: Database, *, cloud_base_url: str | None = None,
                 client_factory=None) -> None:
        super().__init__()
        self.local_database = local_database
        self.cloud_base_url = (
            configured_cloud_base_url() if cloud_base_url is None else cloud_base_url
        )
        self.client_factory = client_factory
        self.window: MainWindow | None = None
        self._switching = False

    def start(self) -> MainWindow:
        if self.window is not None:
            return self.window
        # Every process starts local, even when a public cloud URL is configured.
        self.window = self._build("local", self.cloud_base_url)
        self.window.show()
        return self.window

    def _build(self, mode: str, url: str) -> MainWindow:
        window = build_desktop_window(mode, local_database=self.local_database,
                                       cloud_base_url=url, client_factory=self.client_factory)
        window.connection_change_requested.connect(self.switch_mode)
        window.installEventFilter(self)
        return window

    def switch_mode(self, mode: str, url: str = "") -> bool:
        previous = self.window
        if (previous is None or self._switching or _window_has_pending_work(previous)
                or previous._active_workspace is not None):
            if previous is not None:
                previous.login_page.show_error("请先等待当前操作完成并退出登录，再切换模式。")
            return False
        self._switching = True
        try:
            requested_url = validate_base_url(url) if mode == "cloud" else self.cloud_base_url
            replacement = self._build(mode, requested_url)
            if mode == "cloud":
                self.cloud_base_url = requested_url
            replacement.setGeometry(previous.geometry())
            # Show the new window before closing the old one so Qt never sees
            # zero top-level windows and never exits during a mode switch.
            replacement.show()
            self.window = replacement
            self._release(previous)
            previous.removeEventFilter(self)
            previous.close()
            previous.deleteLater()
            return True
        except CloudAPIError as error:
            previous.login_page.show_error(str(error))
            return False
        except Exception:
            logging.getLogger(__name__).error("Desktop mode switch failed; details withheld")
            previous.login_page.show_error("无法切换数据模式，原窗口和数据已保留，请稍后重试。")
            return False
        finally:
            self._switching = False

    @staticmethod
    def _release(window: MainWindow) -> None:
        if getattr(window, "_desktop_resources_released", False):
            return
        window._desktop_resources_released = True
        window.login_page.clear_password()
        window.register_page.reset()
        window.secret_store.clear_api_keys()
        if window._active_workspace is not None:
            window._active_workspace.end_session()
            window._active_workspace = None
        window.chat_page.end_session()
        client = getattr(window, "cloud_client", None)
        if client is not None:
            try:
                client.clear_session()
                client.close()
            except Exception:
                logging.getLogger(__name__).warning(
                    "Cloud connection cleanup failed; details withheld"
                )
            finally:
                window.cloud_client = None

    def eventFilter(self, watched, event):
        if watched is self.window and event.type() == QEvent.Type.Close:
            if _window_has_pending_work(self.window):
                event.ignore()
                self.window.statusBar().showMessage("当前后台操作尚未结束，请稍候再关闭应用。")
                return True
            self._release(self.window)
        return False

    def shutdown(self) -> None:
        if self.window is not None:
            self._release(self.window)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("Local")
    app.setStyleSheet(APP_STYLE)

    instance_lock: QLockFile | None = None
    controller: DesktopWindowController | None = None
    try:
        # Database replacement during an import is only safe when no second copy
        # of this application can still be writing the same portable data file.
        instance_lock = QLockFile(str(user_data_dir() / ".app-instance.lock"))
        instance_lock.setStaleLockTime(0)
        if not instance_lock.tryLock(100):
            QMessageBox.warning(
                None,
                "应用已在运行",
                "检测到同一份应用已经打开，或数据目录暂时被占用。\n\n请关闭另一窗口后再试。",
            )
            return 2

        configure_logging()
        current_database_path = database_path()
        if is_frozen_app():
            migrate_legacy_database(legacy_database_path(), current_database_path)
        database = Database(current_database_path)
        database.initialize()
        controller = DesktopWindowController(database)
        controller.start()
    except Exception as exc:
        logging.getLogger(__name__).exception("Application startup failed")
        QMessageBox.critical(
            None,
            "启动失败",
            f"应用无法初始化本地数据。\n\n{exc}",
        )
        if instance_lock is not None:
            instance_lock.unlock()
        return 1

    try:
        return app.exec()
    finally:
        if controller is not None:
            controller.shutdown()
        if instance_lock is not None:
            instance_lock.unlock()


if __name__ == "__main__":
    raise SystemExit(main())
