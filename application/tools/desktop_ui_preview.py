"""Render desktop-only QA screenshots using synthetic, temporary local data.

Never opens a real user database and never invokes an AI provider. Screenshots
are evidence of layout only, not evidence of online model or cloud operation.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

from ollama_chat_app.data.database import Database
from ollama_chat_app.security.secret_store import SecretStore
from ollama_chat_app.services.ai_assistant import AiAssistantService
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.backup import PortableBackupService
from ollama_chat_app.services.chat import ChatService
from ollama_chat_app.services.commerce import CommerceService
from ollama_chat_app.services.file_management import FileManagementService
from ollama_chat_app.services.health import HealthService
from ollama_chat_app.services.service_management import ServiceManagementService
from ollama_chat_app.ui.main_window import MainWindow
from ollama_chat_app.ui.theme import APP_STYLE


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    # Qt's offscreen plugin does not enumerate Windows fallback fonts reliably.
    for font_name in ("msyh.ttc", "msyhbd.ttc"):
        font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / font_name
        if font_path.is_file():
            QFontDatabase.addApplicationFont(str(font_path))
    app.setStyleSheet(APP_STYLE)
    with tempfile.TemporaryDirectory(prefix="healthlife-desktop-preview-") as temporary:
        database = Database(Path(temporary) / "synthetic.db")
        database.initialize()
        auth = AuthService(database)
        user = auth.register("界面测试用户", "Synthetic-preview-only-123!")
        chat = ChatService(database)
        backup = PortableBackupService(database)
        health = HealthService(database)
        files = FileManagementService(database)
        window = MainWindow(
            auth,
            chat,
            SecretStore(),
            backup,
            health_service=health,
            service_management_service=ServiceManagementService(database),
            commerce_service=CommerceService(database),
            file_management_service=files,
            ai_assistant_service=AiAssistantService(database, health),
        )
        window.resize(1240, 820)
        window.show()
        app.processEvents()
        window.login_page.username_input.setText("界面测试用户")
        window.login_page.password_input.setText("Synthetic-preview-only-123!")
        app.processEvents()
        window.grab().save(str(output / "login-password-hidden.png"))

        # These synthetic turns are never sent to any model or network service.
        first = chat.get_default_conversation(user.id)
        second = chat.create_conversation(user.id, "报告与文件讨论")
        chat.rename_conversation(user.id, first.id, "日常生活交流")
        exchange = chat.begin_message(
            user.id,
            "这是界面验收用的测试消息，不是真实健康资料。",
            provider="deepseek_cloud",
            model="deepseek-v4-flash",
            conversation_id=second.id,
        )
        chat.complete_message(
            user.id,
            exchange.assistant_message.id,
            "界面测试回复：本截图未调用 AI。新对话的上下文与其他对话分开保存。",
        )
        page = window.chat_page
        window._login_succeeded(user)
        # Select the new chat without any model/key lookup.
        for index in range(page.conversation_list.count()):
            item = page.conversation_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == second.id:
                page.conversation_list.setCurrentItem(item)
                break
        assert page.current_conversation_id == second.id
        page.mode_combo.blockSignals(True)
        page.mode_combo.setCurrentIndex(2)
        page.mode_combo.blockSignals(False)
        page._previous_mode_index = 2
        page._show_mode_controls("deepseek_cloud")
        page.status_label.setText(
            "DeepSeek：纯文字使用 Flash；含图片时使用视觉实验模型。此截图没有发起请求。"
        )
        page.message_input.setPlainText("可以附加图片或文件后再点击发送。")
        window.health_workspace.show_page(4)
        app.processEvents()
        app.processEvents()
        window.grab().save(str(output / "member-workspace-chat.png"))
        window.setCentralWidget(page)
        page.show()
        app.processEvents()
        measurements = []
        for width, height in ((1240, 820), (980, 680)):
            window.resize(width, height)
            app.processEvents()
            app.processEvents()
            window.grab().save(str(output / f"chat-{width}x{height}.png"))
            measurements.append(
                {
                    "requested": [width, height],
                    "actual": [window.width(), window.height()],
                    "page_minimum_hint": [
                        page.minimumSizeHint().width(),
                        page.minimumSizeHint().height(),
                    ],
                }
            )
        window.close()
        app.processEvents()
    (output / "layout-check.json").write_text(
        json.dumps(
            {"synthetic_data": True, "network_calls": 0, "measurements": measurements},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps(measurements, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
