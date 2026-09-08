"""Render current member layout with temporary synthetic data; no AI or cloud calls."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

from ollama_chat_app.data.database import Database
from ollama_chat_app.security.secret_store import SecretStore
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.chat import ChatService
from ollama_chat_app.services.health import HealthService
from ollama_chat_app.services.preferences import PreferencesService
from ollama_chat_app.time_utils import beijing_now
from ollama_chat_app.ui.main_window import MainWindow


def main():
    output = Path(__file__).resolve().parents[1] / "outputs" / "member-small-screen-20260908"
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    for name in ("msyh.ttc", "msyhbd.ttc"):
        QFontDatabase.addApplicationFont(str(Path("C:/Windows/Fonts") / name))
    with tempfile.TemporaryDirectory(prefix="synthetic-layout-") as scratch:
        database = Database(Path(scratch) / "synthetic.db")
        database.initialize()
        auth = AuthService(database)
        user = auth.register("合成界面验收用户", "Synthetic-layout-123!")
        preferences = PreferencesService(database)
        preferences.update(user.id, {"font_size": 24, "weather_consent": False})
        health = HealthService(database)
        for category, detail in (
            ("water", {"amount_ml": 300}),
            ("activity", {"duration_minutes": 30}),
        ):
            health.add_life_record(
                user.id,
                category=category,
                content="合成界面验收记录，非真实资料。",
                occurred_at=beijing_now(),
                details=detail,
            )
        for title in ("合成提醒：喝水后记录", "合成提醒：散步完成后记录"):
            health.add_reminder(user.id, title, beijing_now())
        chat = ChatService(database)
        for title in ("合成日常聊天", "合成购物讨论", "合成生活记录"):
            chat.create_conversation(user.id, title)
        window = MainWindow(
            auth, chat, SecretStore(), health_service=health, preferences_service=preferences
        )
        window.resize(1366, 768)
        window.show()
        window._login_succeeded(user)
        workspace = window.health_workspace
        window.apply_preferences(preferences.get(user.id))
        for index, name in ((0, "today"), (1, "statistics"), (3, "chat")):
            workspace.show_page(index)
            app.processEvents()
            app.processEvents()
            window.grab().save(str(output / f"{name}-1366x768-font24.png"))
            if index == 0:
                bar = workspace.today_page.content_scroll.verticalScrollBar()
                bar.setValue(bar.maximum())
                app.processEvents()
                window.grab().save(str(output / "today-scrolled-1366x768-font24.png"))
                bar.setValue(0)
        print(f"synthetic only; actual size={window.width()}x{window.height()}; output={output}")
        window.close()
        app.processEvents()


if __name__ == "__main__":
    main()
