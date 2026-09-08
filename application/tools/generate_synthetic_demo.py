"""Build an isolated, clearly fictional offline demo. Refuse existing output paths."""

from __future__ import annotations

import argparse
import json
import os
import secrets
from datetime import datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo


def build_demo(output: Path, *, screenshots: bool = True) -> dict:
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"拒绝覆盖既有目录：{output}")
    output.mkdir(parents=True, exist_ok=False)
    data_dir = output / "data"
    data_dir.mkdir()
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["OLLAMA_DUAL_CHAT_DATA_DIR"] = str(data_dir)

    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtWidgets import QApplication

    from ollama_chat_app.data.database import Database
    from ollama_chat_app.services.appointment_service import AppointmentService
    from ollama_chat_app.services.auth import AuthService
    from ollama_chat_app.services.commerce import CommerceService
    from ollama_chat_app.services.health import HealthService
    from ollama_chat_app.services.preferences import PreferencesService
    from ollama_chat_app.services.service_management import ServiceManagementService
    from ollama_chat_app.ui.member_commerce import product_illustration
    from ollama_chat_app.ui.theme import build_style

    app = QApplication.instance() or QApplication([])
    # Qt's offscreen platform does not discover Windows system fonts itself.
    font_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for filename in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf"):
        font_path = font_dir / filename
        if font_path.is_file():
            QFontDatabase.addApplicationFont(str(font_path))
    app.setFont(QFont("Microsoft YaHei UI", 15))
    app.setStyleSheet(build_style())
    database = Database(data_dir / "app.db")
    auth = AuthService(database)
    management = ServiceManagementService(database)
    health = HealthService(database)
    commerce = CommerceService(database)
    preferences = PreferencesService(database)
    appointments = AppointmentService(database)
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    today = now.date()
    accounts = []

    def credentials(role: str, username: str, name: str) -> str:
        password = secrets.token_urlsafe(15)
        accounts.append({"role": role, "username": username, "name": name, "password": password})
        return password

    operator = auth.bootstrap_operator(
        "demo-operator", credentials("运营", "demo-operator", "示例运营员")
    )
    advisors = [
        management.create_advisor(
            operator.id,
            f"demo-advisor-{index + 1}",
            credentials("顾问", f"demo-advisor-{index + 1}", name),
            display_name=name,
            valid_until=now + timedelta(days=365 * 3),
            organization="示例社区生活服务站",
            specialty="日常生活安排与居家协助",
            bio="本账号与所有服务记录均为虚构演示资料。",
        )
        for index, name in enumerate(("示例林顾问", "示例周顾问"))
    ]
    members = []
    for index, name in enumerate(("示例陈奶奶", "示例吴爷爷")):
        username = f"demo-member-{index + 1}"
        member = auth.register(username, credentials("会员", username, name))
        members.append(member)
        health.save_profile(
            member.id,
            {
                "display_name": name,
                "birth_date": f"{1955 + index * 3}-05-12",
                "gender": "female" if index == 0 else "male",
                "phone": "00000000000",
                "living_situation": "示例：与家人同住，日常喜欢散步和整理花草",
                "emergency_contact_name": "示例家属",
                "emergency_contact_phone": "00000000000",
                "ai_preferred_name": "示例陈阿姨" if index == 0 else "示例吴叔叔",
                "reminder_frequency": "normal",
                "height_cm": 160 + index * 10,
                "health_goals": "示例：记录每日饮食、饮水、活动、睡眠及居住环境",
                "dietary_preferences": "示例：偏好清淡家常菜",
                "medical_notes": "全部为虚构演示资料，不代表任何真实人士的健康情况。",
            },
        )
        preferences.update(
            member.id,
            {
                "nickname": name,
                "preferred_name": name,
                "city": "上海（演示）",
                "font_size": 20,
                "theme_color": "sage",
                "timezone": "Asia/Shanghai",
                "ai_context_consent": False,
                "weather_consent": False,
            },
        )
        management.save_membership(
            operator.id,
            member.id,
            plan_code="示例家庭关怀年卡" if index == 0 else "示例基础会员",
            starts_at=now - timedelta(days=90),
            ends_at=now + timedelta(days=275) if index == 0 else now - timedelta(days=1),
            status="active" if index == 0 else "expired",
            benefits={
                "visit_total": 12,
                "visit_used": 3,
                "first_filing": True,
                "gift": "示例生活礼盒（演示，不发货）",
            },
        )
        management.bind_advisor(operator.id, member.id, advisors[index].id)
        for offset in range(14):
            day = today - timedelta(days=13 - offset)
            variation = (offset + index) % 4
            records = (
                (
                    "diet",
                    "午餐：杂粮饭、蔬菜和豆制品",
                    {
                        "meal_type": "lunch",
                        "amount_g": 450 + variation * 20,
                        "calories_kcal": 520 + variation * 30,
                    },
                ),
                ("water", "全天分次饮水", {"amount_ml": 1400 + variation * 100}),
                (
                    "activity",
                    "小区散步与舒展活动",
                    {
                        "duration_minutes": 25 + variation * 5,
                        "steps": 3600 + variation * 450,
                        "energy_kcal": 100 + variation * 15,
                    },
                ),
                ("sleep", "前夜睡眠记录", {"duration_hours": 7 + variation * 0.3, "quality": 4}),
                (
                    "environment",
                    "开窗通风并整理客厅",
                    {
                        "temperature_c": 24 + variation * 0.5,
                        "humidity_percent": 48 + variation * 3,
                        "ventilation_minutes": 30,
                    },
                ),
            )
            for category, content, values in records:
                health.add_life_record(
                    member.id,
                    category,
                    datetime.combine(day, time(7, 30), now.tzinfo),
                    f"【虚构演示】{content}；所有数值为预设示例，非实际测量或 AI 分析。",
                    source="import",
                    details={**values, "synthetic_demo": True},
                )
        for days_ago in (12, 9, 6, 3):
            visited = now - timedelta(days=days_ago, hours=2)
            task = appointments.create_request(
                member.id,
                "示例居家关怀",
                visited,
                "【虚构演示】预约备注",
                "示例地址：演示社区 1 号楼（非真实住址）",
            )
            management.complete_visit_task(
                advisors[index].id,
                task,
                visited_at=visited,
                summary="【虚构演示】记录生活安排与居家需求",
                details={
                    "duration_minutes": 40 + index * 10,
                    "inspected_areas": "示例：居家环境与生活用品摆放",
                    "user_questions": "示例：如何安排日常散步",
                    "follow_ups": "示例：下次共同核对生活记录",
                    "synthetic_demo": True,
                },
            )
        for state in ("pending", "in_progress", "incomplete", "disabled"):
            task = appointments.create_request(
                member.id,
                "示例生活协助",
                now + timedelta(days=2 + index),
                "【虚构演示】请按约定时间联系",
                "示例地址：演示社区 1 号楼（非真实住址）",
            )
            if state == "in_progress":
                management.start_visit_task(advisors[index].id, task)
            elif state != "pending":
                appointments.update_request(task, operator.id, status=state)
        for kind, title in (
            ("water", "示例：记得记录今天的饮水"),
            ("activity", "示例：安排一次轻松散步"),
        ):
            health.add_reminder(member.id, title, now + timedelta(hours=2), kind)

    image_dir = output / "assets" / "products"
    image_dir.mkdir(parents=True)
    products = []
    catalog = (
        ("示例随行饮水杯", "daily", "500 毫升", 3900),
        ("示例杂粮燕麦", "food", "500 克", 2600),
        ("示例纯棉毛巾", "daily", "两条装", 2200),
        ("示例午休靠枕", "home", "40 × 40 厘米", 5900),
        ("示例轻便收纳盒", "home", "三件套", 3500),
        ("示例运动弹力带", "other", "轻阻力", 2900),
    )
    for index, (name, category, specification, price) in enumerate(catalog, 1):
        relative = f"assets/products/demo-{index}.png"
        if not product_illustration({"name": name, "category": category}, 320).save(
            str(output / relative)
        ):
            raise RuntimeError("无法保存商品示意图")
        product = commerce.create_product(
            operator.id,
            sku=f"DEMO-{index:03d}",
            name=name,
            category=category,
            brand="示例生活",
            specification=specification,
            price_cents=price,
            unit="件",
            image_path=relative,
            description="虚构演示商品，用于展示日常生活用品信息和购物流程。",
            is_active=True,
        )
        products.append(product)
    for index, member in enumerate(members):
        commerce.set_favorite(member.id, products[index]["id"], True)
        commerce.set_favorite(member.id, products[index + 2]["id"], True)
        commerce.add_to_cart(member.id, products[index]["id"], 1)
        commerce.add_to_cart(member.id, products[index + 3]["id"], 2)
        commerce.recommend_product(
            advisors[index].id,
            member.id,
            products[index]["id"],
            "示例：方便安排日常用品；仅演示生活用途推荐。",
        )
        commerce.simulate_purchase(member.id, products[index + 2]["id"])

    with database.connect() as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("users", "life_records", "visit_tasks", "visit_records", "products")
        }
    if integrity != "ok" or foreign_keys:
        raise RuntimeError("演示数据库完整性检查失败")
    for account in accounts:
        auth.authenticate(account["username"], account["password"])
    manifest = {
        "synthetic_demo": True,
        "generated_at": now.isoformat(),
        "database": "data/app.db",
        "counts": counts,
        "integrity_check": integrity,
        "foreign_key_violations": 0,
        "record_days": 14,
        "record_categories": 5,
        "ai_calls": 0,
        "notice": "所有身份、账号、地址、健康记录、服务和商品均为虚构演示资料。",
    }
    (output / "demo_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    credential_lines = [
        "# 独立演示账号",
        "",
        manifest["notice"],
        "",
        "每个密码独立随机生成，仅属于本演示库；不包含任何真实账号密码。",
        "",
        "| 角色 | 昵称 | 账号 | 初始密码 |",
        "|---|---|---|---|",
    ]
    credential_lines.extend(
        f"| {item['role']} | {item['name']} | {item['username']} | {item['password']} |"
        for item in accounts
    )
    credential_lines += [
        "",
        "顾问账号有效期为生成日起三年。会员 1 为有效会员，会员 2 展示已到期状态。",
        "",
        "演示数据位于 data/app.db；配合演示程序完整文件夹使用，不覆盖既有 data 目录。",
        "",
        "14 天 × 5 类 × 2 位会员，共 140 条预设记录，均标为虚构导入示例。",
        "能量、饮水、步数、睡眠和环境数值不是实际测量，也不是 AI 分析结果。",
        "商品插画为本地绘制的类别示意图；购物订单没有支付、扣款或物流。",
        "",
        "screenshots 内为同一合成数据库渲染的界面，展示时不访问云端或 AI。",
    ]
    (output / "DEMO_ACCOUNTS.md").write_text("\n".join(credential_lines) + "\n", encoding="utf-8")
    if screenshots:
        render_screenshots(
            output,
            app,
            management,
            health,
            commerce,
            preferences,
            appointments,
            operator,
            advisors[0],
            members[0],
        )
    with database.connect() as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return manifest


def _network_blocked(*args, **kwargs):
    raise RuntimeError("离线演示生成禁止发起网络请求")


@patch("httpx.Client.send", new=_network_blocked)
@patch("socket.socket.connect", new=_network_blocked)
def render_screenshots(
    output, app, management, health, commerce, preferences, appointments, operator, advisor, member
):
    from PySide6.QtCore import Qt, QThreadPool
    from PySide6.QtWidgets import QLabel

    from ollama_chat_app.services.member_weather import WeatherUnavailable
    from ollama_chat_app.ui.advisor_workspace import AdvisorWorkspace
    from ollama_chat_app.ui.health_workspace import HealthWorkspace
    from ollama_chat_app.ui.operator_workspace import OperatorWorkspace

    directory = output / "screenshots"
    directory.mkdir()
    previous_cwd = Path.cwd()
    os.chdir(output)  # Resolve the demo's relative artwork without reading another data tree.
    try:
        workspace = OperatorWorkspace(management, commerce_service=commerce)
        workspace.resize(1600, 1000)
        workspace.start_session(operator)
        workspace.show()

        def capture(widget, name):
            QThreadPool.globalInstance().waitForDone(1000)
            app.processEvents()
            if not widget.grab().save(str(directory / f"{name}.png")):
                raise RuntimeError(f"无法保存截图：{name}")

        capture(workspace, "operator-members")
        workspace.view_members_button.click()
        capture(workspace, "operator-member-table")
        workspace.tabs.setCurrentIndex(1)
        capture(workspace, "operator-advisors-and-visits")
        workspace.tabs.setCurrentWidget(workspace.performance_panel)
        for mode in ("table", "bar", "line", "pie"):
            panel = workspace.performance_panel
            panel.view_filter.setCurrentIndex(panel.view_filter.findData(mode))
            capture(workspace, f"operator-statistics-{mode}")
        workspace.close()
        advisor_workspace = AdvisorWorkspace(management, commerce_service=commerce)
        advisor_workspace.resize(1600, 1000)
        advisor_workspace.start_session(advisor)
        advisor_workspace.show()
        advisor_workspace.tabs.setCurrentIndex(1)
        capture(advisor_workspace, "advisor-bound-members")
        advisor_workspace.tabs.setCurrentIndex(3)
        capture(advisor_workspace, "advisor-work-records")
        advisor_workspace.close()
        chat = QLabel("离线演示：未连接 AI，未发送个人资料。")
        chat.setAlignment(Qt.AlignmentFlag.AlignCenter)
        member_workspace = HealthWorkspace(
            health,
            chat,
            commerce_service=commerce,
            preferences_service=preferences,
            appointment_service=appointments,
        )

        def offline_weather(location):
            raise WeatherUnavailable("离线演示：未查询天气")

        member_workspace.today_page.weather_service = SimpleNamespace(current=offline_weather)
        member_workspace.resize(1600, 1000)
        member_workspace.start_session(member)
        member_workspace.show()
        for index, name in (
            (0, "member-today"),
            (1, "member-profile-and-statistics"),
            (2, "member-services"),
            (4, "member-my-platform"),
        ):
            member_workspace.show_page(index)
            capture(member_workspace, name)
        member_workspace.show_page(2)
        member_workspace.service_page.section_tabs.setCurrentIndex(1)
        capture(member_workspace, "member-products")
        member_workspace.service_page.commerce_panel.product_tabs.setCurrentIndex(1)
        capture(member_workspace, "member-all-products")
        member_workspace.show_page(1)
        statistics = member_workspace.profile_page
        statistics.category_combo.setCurrentIndex(statistics.category_combo.findData("water"))
        statistics.parameter_combo.setCurrentIndex(statistics.parameter_combo.findData("amount_ml"))
        statistics.view_combo.setCurrentIndex(statistics.view_combo.findData("line"))
        capture(member_workspace, "member-water-trend")
        member_workspace.close()
    finally:
        os.chdir(previous_cwd)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--no-screenshots", action="store_true")
    args = parser.parse_args()
    manifest = build_demo(args.output, screenshots=not args.no_screenshots)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "counts": manifest["counts"],
                "integrity_check": manifest["integrity_check"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
