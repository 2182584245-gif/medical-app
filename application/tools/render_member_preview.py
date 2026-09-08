"""Render synthetic member screens in isolation; never opens the user's database."""

from __future__ import annotations

import os
import tempfile
from datetime import timedelta
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QWidget

from ollama_chat_app.data.database import Database
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.commerce import CommerceService
from ollama_chat_app.services.health import HealthService
from ollama_chat_app.services.preferences import PreferencesService
from ollama_chat_app.time_utils import beijing_now
from ollama_chat_app.ui.health_workspace import HealthWorkspace
from ollama_chat_app.ui.theme import APP_STYLE


def main():
    output = Path(__file__).resolve().parents[1] / "outputs" / "member-preview"
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    # Windows offscreen Qt does not enumerate system fonts automatically.
    QFontDatabase.addApplicationFont("C:/Windows/Fonts/msyh.ttc")
    QFontDatabase.addApplicationFont("C:/Windows/Fonts/msyhbd.ttc")
    app.setStyleSheet(APP_STYLE)
    with tempfile.TemporaryDirectory(prefix="member-preview-") as scratch:
        db = Database(Path(scratch) / "synthetic.db")
        db.initialize()
        auth = AuthService(db)
        operator = auth.bootstrap_operator("preview-operator", "preview-password-123")
        member = auth.register("演示会员", "preview-password-123")
        health = HealthService(db)
        commerce = CommerceService(db)
        now = beijing_now()
        health.save_profile(member.id, {"display_name": "演示会员"})
        for day in range(7):
            for category, text, details in [
                ("diet", "午餐：米饭、清炒时蔬和鸡蛋", {"calories_kcal": 480 + day * 20}),
                ("water", "上午喝了一杯温水", {"amount_ml": 250 + day * 50}),
                ("activity", "下午在社区公园散步", {"duration_minutes": 30, "energy_kcal": 100}),
                ("sleep", "昨晚睡眠", {"duration_hours": 7.5}),
            ]:
                health.add_life_record(
                    member.id,
                    category=category,
                    content=text,
                    occurred_at=now - timedelta(days=day),
                    details=details,
                )
        health.add_reminder(member.id, "散步后记得补充水分", now + timedelta(minutes=5))
        for sku, name, category, price, specification in [
            ("CUP", "便携刻度水杯", "饮水用品", 3900, "500 毫升 · 商品示例"),
            ("OAT", "原味燕麦片", "日常食品", 2800, "500 克 · 商品示例"),
            ("PILLOW", "柔软午休枕", "家居睡眠", 5900, "草木绿 · 商品示例"),
            ("BAND", "轻便弹力带", "运动用品", 1900, "轻阻力 · 商品示例"),
        ]:
            commerce.create_product(
                operator.id,
                sku=sku,
                name=name,
                category=category,
                price_cents=price,
                specification=specification,
                description="仅为本地界面预览的合成商品。",
                is_active=True,
            )
        window = HealthWorkspace(
            health, QWidget(), commerce_service=commerce, preferences_service=PreferencesService(db)
        )
        window.resize(1480, 1000)
        window.start_session(member)
        window.show()
        app.processEvents()
        for index, name in [(0, "today"), (1, "statistics"), (2, "products"), (4, "my-platform")]:
            window.show_page(index)
            if name == "statistics":
                window.profile_page.category_combo.setCurrentIndex(1)
                window.profile_page.view_combo.setCurrentIndex(2)
            elif name == "products":
                window.service_page.section_tabs.setCurrentIndex(1)
                window.service_page.commerce_panel.product_tabs.setCurrentIndex(1)
            app.processEvents()
            destination = output / f"synthetic-{name}.png"
            if not window.grab().save(str(destination)):
                raise RuntimeError(f"Failed to save preview: {destination}")
            print(destination)
        window.close()
        app.processEvents()


if __name__ == "__main__":
    main()
