"""Build an isolated, clearly fictional offline demo. Refuse existing output paths."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import secrets
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo


def build_demo(
    output: Path,
    *,
    screenshots: bool = True,
    profile: str = "legacy",
    anchor: date | None = None,
    credential_seed: Path | None = None,
) -> dict:
    if profile == "expanded-v170":
        return build_expanded_demo(output, anchor=anchor, credential_seed=credential_seed)
    if profile != "legacy" or anchor is not None or credential_seed is not None:
        raise ValueError("扩充资料请显式选择 expanded-v170；旧版固定样本不接受其他参数。")
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


EXPANDED_PROFILE = "expanded-v170"
EXPANDED_ANCHOR = date(2026, 9, 19)
EXPANDED_DAYS = 210
EXPANDED_RANDOM_SEED = 170019
EXPANDED_ACCOUNTS = {
    "demo-operator": ("operator", "许静"),
    "demo-advisor-1": ("advisor", "林文静"),
    "demo-advisor-2": ("advisor", "周明远"),
    "demo-member-1": ("member", "陈淑兰"),
    "demo-member-2": ("member", "吴建国"),
}


def expanded_life_records(anchor: date):
    """Stable authored fixtures; never sensor readings, prescriptions or model output."""
    rng = random.Random(EXPANDED_RANDOM_SEED)
    breakfasts = (
        "小米粥、鸡蛋和凉拌黄瓜",
        "燕麦粥、全麦面包和牛奶",
        "杂粮馒头、豆浆和小番茄",
        "南瓜粥、鸡蛋和一小碟青菜",
        "玉米、酸奶和煮鸡蛋",
        "山药粥、豆腐干和苹果",
        "面条、荷包蛋和菠菜",
        "红薯、牛奶和小馒头",
    )
    lunches = (
        "杂粮饭、清蒸鱼和西兰花",
        "米饭、香菇鸡丁和炒油麦菜",
        "番茄鸡蛋面配一碗青菜",
        "二米饭、冬瓜虾仁和炒豆芽",
        "荞麦面、卤牛肉和黄瓜",
        "米饭、白菜炖豆腐和蒸南瓜",
        "杂粮饭、芹菜肉丝和紫菜汤",
        "土豆炖鸡、米饭和凉拌木耳",
        "饺子配烫青菜",
        "米饭、番茄炒蛋和香菇青菜",
        "家人做的牛肉蔬菜面",
        "社区食堂的米饭、蒸蛋和两样蔬菜",
    )
    dinners = (
        "小米饭、蒸蛋和炒青菜",
        "冬瓜豆腐汤、杂粮馒头和胡萝卜",
        "青菜鸡丝面",
        "米饭、清蒸鲈鱼和炒丝瓜",
        "山药粥、豆腐和西红柿",
        "南瓜饭、蘑菇鸡肉和烫生菜",
        "菠菜鸡蛋汤配杂粮包",
        "白菜肉末面和黄瓜",
    )
    places = ("家中餐桌", "家中餐桌", "家中餐桌", "社区食堂", "女儿家")
    for member_index in range(2):
        for offset in range(EXPANDED_DAYS):
            day = anchor - timedelta(days=EXPANDED_DAYS - 1 - offset)
            shift = offset + member_index * 7
            meal_choices = (breakfasts, lunches, dinners)
            for meal_index, (meal, hour, minute, base_kcal, base_weight) in enumerate(
                (
                    ("breakfast", 7, 40, 380, 360),
                    ("lunch", 12, 10, 620, 520),
                    ("dinner", 18, 20, 510, 440),
                )
            ):
                food = meal_choices[meal_index][shift % len(meal_choices[meal_index])]
                location = places[(offset // 3 + member_index) % len(places)]
                if member_index == 1 and location == "女儿家":
                    location = "儿子家"
                yield (
                    member_index,
                    day,
                    time(hour, minute),
                    "diet",
                    (
                        f"在{location}吃了{food}，"
                        f"食量{'比平时略少' if shift % 11 == 0 else '和平时接近'}。"
                    ),
                    {
                        "meal_type": meal,
                        "amount_g": base_weight + rng.randrange(-3, 4) * 15,
                        "calories_kcal": base_kcal + member_index * 45 + rng.randrange(-4, 5) * 15,
                        "location": location,
                    },
                )
            # Individual drinks, not one day's total repeated for every cup.
            for hour, minute in ((8, 30), (10, 30), (13, 30), (16, 0), (19, 45)):
                if hour == 16 and shift % 9 == 0:
                    continue  # A plausible unrecorded afternoon, not fabricated completeness.
                amount = rng.choice((200, 250, 300, 350))
                yield (
                    member_index,
                    day,
                    time(hour, minute),
                    "water",
                    (
                        f"{'整理完花草' if hour == 10 else '坐下休息时'}喝了一杯温水，"
                        f"约{amount}毫升。"
                    ),
                    {"amount_ml": amount},
                )
            indoor = shift % 9 in (0, 1)
            duration = rng.choice((15, 20, 25, 30, 35, 40))
            activity = (
                "在家舒展身体和慢走"
                if indoor
                else ("和老伴在小区花园散步" if member_index == 0 else "在河边步道散步")
            )
            yield (
                member_index,
                day,
                time(17, 0),
                "activity",
                (f"{activity}，持续{duration}分钟，结束后坐着休息了一会儿。"),
                {
                    "duration_minutes": duration,
                    "steps": duration * rng.choice((65, 70, 75)),
                    "energy_kcal": duration * rng.choice((2, 3)),
                    "activity_type": "walk",
                },
            )
            hours = round(
                6.6
                + rng.choice((-0.8, -0.3, 0, 0.4, 0.8, 1.0))
                + (0.2 if member_index == 0 else 0),
                1,
            )
            quality = 2 if hours < 6 else (4 if hours >= 7 else 3)
            yield (
                member_index,
                day,
                time(7, 20),
                "sleep",
                (
                    f"昨夜睡了约{hours}小时，"
                    f"{'夜里醒来两次' if quality == 2 else '醒来后精神尚可'}。"
                ),
                {"duration_hours": hours, "quality": quality},
            )
            season = math.sin((day.timetuple().tm_yday - 100) * math.tau / 365)
            temperature = round(23 + 5 * season + rng.uniform(-1.5, 1.5), 1)
            ventilation = 15 if indoor else rng.choice((20, 25, 30, 40))
            yield (
                member_index,
                day,
                time(9, 0),
                "environment",
                (
                    f"{'下雨后擦干阳台地面，' if indoor else '整理客厅，'}"
                    f"开窗通风{ventilation}分钟。"
                ),
                {
                    "temperature_c": temperature,
                    "humidity_percent": rng.randrange(42, 68),
                    "ventilation_minutes": ventilation,
                    "location": "家中客厅",
                },
            )
            if offset % 7 == 0:
                week = offset // 7
                kind, title, description = (
                    ("consultation", "社区门诊复查", "已到门诊复查，将纸质记录放入资料袋。"),
                    ("other", "整理体检资料", "按日期整理已有报告，记录下次想咨询的问题。"),
                    (
                        "medication",
                        "完成既有用药事项",
                        "本人记录已按既有医嘱完成事项，未录入药名和剂量。",
                    ),
                )[week % 3]
                yield (
                    member_index,
                    day,
                    time(10, 15),
                    "medical",
                    description,
                    {
                        "event_type": kind,
                        "name": title,
                        "description": description,
                        "location": "社区门诊" if kind == "consultation" else "家中",
                    },
                )


def _expanded_credential_input(source: Path | None) -> dict[str, str]:
    if source is None:
        return {}
    from tools.upgrade_synthetic_demo_v7 import checked_path, contract

    source = checked_path(source)
    common = contract()
    # This gate intentionally remains the old fixed fictional corpus contract.
    # A real personal database with hand-written synthetic_demo=true is not enough.
    common.verify_payload(source)
    return common.credentials(source)


def build_expanded_demo(
    output: Path, *, anchor: date | None = None, credential_seed: Path | None = None
) -> dict:
    """Make a NEW 210-day fictional fixture without touching an installed/user/cloud DB.

    Passwords remain random (or preserved from a verified old fictional payload).
    Only the authored content and timeline are deterministic. Public-facing names
    omit fixture labels; the manifest, record metadata and account guide do not.
    """
    from ollama_chat_app.data.database import SCHEMA_VERSION, Database, timestamp_to_db
    from ollama_chat_app.security.passwords import hash_password, verify_password
    from ollama_chat_app.services.appointment_service import AppointmentService
    from ollama_chat_app.services.health import HealthService
    from ollama_chat_app.services.preferences import PreferencesService
    from ollama_chat_app.services.service_management import ServiceManagementService
    from tools.upgrade_synthetic_demo_v7 import checked_path, contract

    anchor = anchor or EXPANDED_ANCHOR
    if type(anchor) is not date or not date(2020, 1, 1) <= anchor <= date(2100, 1, 1):
        raise ValueError("资料基准日期格式不正确。")
    output = checked_path(output)
    if output.exists():
        raise FileExistsError(f"拒绝覆盖既有目录：{output}")
    old_passwords = _expanded_credential_input(credential_seed)
    with (
        patch("httpx.Client.send", new=_network_blocked),
        patch("socket.socket.connect", new=_network_blocked),
    ):
        build_demo(output, screenshots=False)
    passwords = old_passwords or contract().credentials(output)
    database = Database(output / "data/app.db")
    health, preferences = HealthService(database), PreferencesService(database)
    management, appointments = ServiceManagementService(database), AppointmentService(database)
    zone = ZoneInfo("Asia/Shanghai")
    cutoff = datetime.combine(anchor, time(20), zone)
    first_day = anchor - timedelta(days=EXPANDED_DAYS - 1)
    joined = datetime.combine(first_day - timedelta(days=7), time(9), zone)
    stamp = timestamp_to_db(cutoff)
    joined_stamp = timestamp_to_db(joined)
    provenance = {
        "synthetic_demo": True,
        "synthetic_profile": EXPANDED_PROFILE,
        "measured": False,
        "generated_by_ai": False,
    }
    with database.transaction() as connection:
        users = {row["username"]: dict(row) for row in connection.execute("SELECT * FROM users")}
        if set(users) != set(EXPANDED_ACCOUNTS):
            raise RuntimeError("拒绝修改不是刚创建的固定五账号资料库。")
        # Only this newly-created private fixture is rewritten, never a user's source.
        for table in (
            "life_records",
            "reminders",
            "visit_records",
            "visit_task_details",
            "visit_tasks",
            "audit_logs",
        ):
            connection.execute(f'DELETE FROM "{table}"')
        for username, (_role, _name) in EXPANDED_ACCOUNTS.items():
            connection.execute(
                "UPDATE users SET password_hash=?,created_at=?,updated_at=?,last_login_at=NULL "
                "WHERE id=?",
                (hash_password(passwords[username]), joined_stamp, stamp, users[username]["id"]),
            )
        connection.execute(
            "UPDATE conversations SET created_at=?,updated_at=?", (joined_stamp, joined_stamp)
        )
        connection.execute(
            "UPDATE staff_account_terms SET starts_at=?,ends_at=?",
            (joined_stamp, timestamp_to_db(cutoff + timedelta(days=1095))),
        )
        connection.execute(
            "UPDATE advisor_bindings SET started_at=?,created_at=?,updated_at=?",
            (joined_stamp, joined_stamp, stamp),
        )
        for index in range(2):
            identifier = users[f"demo-advisor-{index + 1}"]["id"]
            connection.execute(
                "UPDATE advisor_profiles SET display_name=?,organization=?,specialty=?,bio=?,"
                "created_at=?,updated_at=? WHERE user_id=?",
                (
                    EXPANDED_ACCOUNTS[f"demo-advisor-{index + 1}"][1],
                    "和悦社区生活服务站",
                    ("居家整理、日常活动陪伴" if index == 0 else "生活记录梳理、社区服务协调"),
                    (
                        "喜欢听长辈讲家常，帮助整理生活安排，沟通时放慢语速。"
                        if index == 0
                        else "习惯先了解每户家庭的生活节奏，再一起安排上门服务和后续联系。"
                    ),
                    joined_stamp,
                    stamp,
                    identifier,
                ),
            )
        for username, (role, name) in EXPANDED_ACCOUNTS.items():
            identifier = users[username]["id"]
            connection.execute(
                "INSERT INTO audit_logs(actor_user_id,action,entity_type,entity_id,details_json,"
                "created_at) VALUES(?,'synthetic.persona_created','user',?,?,?)",
                (
                    users["demo-operator"]["id"],
                    identifier,
                    json.dumps({**provenance, "role": role, "name": name}, ensure_ascii=False),
                    joined_stamp,
                ),
            )
        member_ids = [users[f"demo-member-{index + 1}"]["id"] for index in range(2)]
        for index, day, clock, category, content, details in expanded_life_records(anchor):
            validated = HealthService._validate_record_details(category, details)
            validated.update(provenance)  # Attach provenance after strict medical validation.
            occurred = timestamp_to_db(datetime.combine(day, clock, zone))
            connection.execute(
                "INSERT INTO life_records(user_id,category,occurred_at,local_date,"
                "timezone_offset_minutes,content,details_json,source,created_at,updated_at) "
                "VALUES(?,?,?,?,480,?,?,'import',?,?)",
                (
                    member_ids[index],
                    category,
                    occurred,
                    day.isoformat(),
                    content,
                    json.dumps(validated, ensure_ascii=False, sort_keys=True),
                    occurred,
                    occurred,
                ),
            )
        # A single import audit references the deterministic corpus, not invented manual actions.
        connection.execute(
            "INSERT INTO audit_logs(actor_user_id,action,entity_type,details_json,created_at) "
            "VALUES(?,'synthetic.history_imported','life_records',?,?)",
            (users["demo-operator"]["id"], json.dumps(provenance), stamp),
        )

    profile_values = (
        {
            "birth_date": "1955-05-12",
            "gender": "female",
            "height_cm": 160,
            "living_situation": "和老伴同住，周末常去女儿家，平时喜欢种花、听戏和散步。",
            "emergency_contact_name": "女儿小陈",
            "ai_preferred_name": "陈阿姨",
            "health_goals": "按日期记清三餐和饮水；天气允许时陪老伴散步；整理复查资料。",
            "dietary_preferences": "喜欢米饭、鱼和时令蔬菜；早餐常吃粥，不喜欢太硬的食物。",
            "medical_notes": "有定期社区复查习惯；未录入明确诊断、药名和剂量。",
        },
        {
            "birth_date": "1958-10-23",
            "gender": "male",
            "height_cm": 170,
            "living_situation": "自己居住，儿子每周来看望；喜欢下棋、听广播和沿河散步。",
            "emergency_contact_name": "儿子小吴",
            "ai_preferred_name": "吴叔叔",
            "health_goals": "让喝水和用餐时间更规律；留意夜间睡眠；每月整理生活和门诊记录。",
            "dietary_preferences": "喜欢面食和家常炖菜；午餐有时在社区食堂解决。",
            "medical_notes": "保存了过去的门诊记录；用药事项只记是否已完成，不提供处方建议。",
        },
    )
    for username, (role, name) in EXPANDED_ACCOUNTS.items():
        preferences.update(
            users[username]["id"],
            {
                "nickname": name,
                "preferred_name": name,
                "city": "上海" if role != "operator" else "北京",
                "font_size": 20 if role == "member" else 18,
                "timezone": "Asia/Shanghai",
                "ai_context_consent": False,
                "weather_consent": False,
            },
        )
    for index, values in enumerate(profile_values):
        identifier = member_ids[index]
        name = EXPANDED_ACCOUNTS[f"demo-member-{index + 1}"][1]
        health.save_profile(
            identifier,
            {
                **values,
                "display_name": name,
                "phone": "",
                "emergency_contact_phone": "",
                "reminder_frequency": "normal",
            },
        )
        preferences.update(
            identifier,
            {
                "preferred_name": values["ai_preferred_name"],
                "theme_color": "sage" if index == 0 else "blue",
            },
        )
        membership = "家庭关怀年卡" if index == 0 else "基础生活会员"
        with database.transaction() as connection:
            connection.execute(
                "UPDATE memberships SET plan_code=?,starts_at=?,ends_at=?,status=?,benefits_json=? "
                "WHERE user_id=?",
                (
                    membership,
                    joined_stamp,
                    timestamp_to_db(cutoff + timedelta(days=150 if index == 0 else -10)),
                    "active" if index == 0 else "expired",
                    json.dumps(
                        {
                            "visit_total": 12,
                            "visit_used": 7,
                            "first_filing": True,
                            "gift": "日常生活礼盒",
                            **provenance,
                        },
                        ensure_ascii=False,
                    ),
                    identifier,
                ),
            )
        for reminder_index, (kind, title, hour, repeat) in enumerate(
            (
                ("water", "坐下歇一歇，记下今天喝的水", 10, "daily"),
                ("activity", "按自己的状态安排轻松活动", 17, "daily"),
                ("custom", "和家人通个电话", 19, "weekly"),
                ("visit", "提前整理下次上门想聊的事情", 9, "none"),
                ("custom", "整理上个月的门诊资料", 14, "monthly"),
            )
        ):
            reminder = health.add_reminder(
                identifier,
                title,
                datetime.combine(anchor + timedelta(days=1 + reminder_index % 2), time(hour), zone),
                kind,
                repeat_rule=repeat,
            )
            if reminder_index == 4 and index == 1:
                health.toggle_reminder(identifier, reminder, enabled=False)
        for past_offset in (180, 90, 30):
            reminder = health.add_reminder(
                identifier, "已经整理好生活记录", cutoff - timedelta(days=past_offset), "custom"
            )
            with database.transaction() as connection:
                connection.execute(
                    "UPDATE reminders SET status='completed' WHERE id=?", (reminder,)
                )
        advisor_id = users[f"demo-advisor-{index + 1}"]["id"]
        for visit_index, days_ago in enumerate((196, 168, 140, 112, 84, 56, 28)):
            visited = datetime.combine(anchor - timedelta(days=days_ago), time(9 + index, 30), zone)
            task = appointments.create_request(
                identifier,
                ("居家整理协助", "生活记录交流", "社区事项协调")[visit_index % 3],
                visited,
                "到达前请先通过平台联系。",
                "上海市 / 上海市 / 和悦生活苑公共会客区",
            )
            management.complete_visit_task(
                advisor_id,
                task,
                visited_at=visited,
                summary=(
                    "一起整理生活记录，约定下次再交流。"
                    if visit_index % 2
                    else "检查家中常用物品的摆放，交流近期生活安排。"
                ),
                details={
                    "duration_minutes": 30 + (visit_index % 4) * 10,
                    "inspected_areas": "客厅、阳台和常用物品摆放处",
                    "user_questions": "怎样让记录更方便，下一次上门什么时候安排",
                    "follow_ups": "下次共同回看记录，不代替医疗判断",
                    **provenance,
                },
            )
        for state, day_offset in (
            ("pending", 3),
            ("in_progress", 0),
            ("incomplete", -14),
            ("disabled", -7),
        ):
            task = appointments.create_request(
                identifier,
                "生活协助与近况交流",
                cutoff + timedelta(days=day_offset),
                "时间如需变动，请提前在平台留言。",
                "上海市 / 上海市 / 和悦生活苑公共会客区",
            )
            if state == "in_progress":
                management.start_visit_task(advisor_id, task)
            elif state != "pending":
                appointments.update_request(task, users["demo-operator"]["id"], status=state)

    _expanded_chats(database, member_ids, anchor, provenance)
    with database.transaction() as connection:
        for index, name in enumerate(
            ("随行饮水杯", "杂粮燕麦", "纯棉毛巾", "午休靠枕", "轻便收纳盒", "运动弹力带"), 1
        ):
            connection.execute(
                "UPDATE products SET name=?,brand='和悦生活',description=? WHERE sku=?",
                (
                    name,
                    "用于日常生活的实用物品，具体规格见商品信息。仅体验选购流程，不实际发货。",
                    f"DEMO-{index:03d}",
                ),
            )
        connection.execute(
            "UPDATE product_recommendations SET reason=?",
            ("结合日常生活需要，可按个人喜好选择，不代表医疗建议。",),
        )
        # The old order snapshot contains the old label; rebuild it from the newly named product.
        connection.execute(
            "UPDATE orders SET product_name_snapshot=(SELECT name FROM products "
            "WHERE products.id=orders.product_id)"
        )
        # Fill deterministic creation times without changing event-specific occurrence times.
        for table in (
            "advisor_profiles",
            "member_profiles",
            "memberships",
            "user_preferences",
            "products",
            "product_recommendations",
            "orders",
            "member_cart",
            "member_favorites",
            "staff_account_terms",
        ):
            columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            if "created_at" in columns:
                connection.execute(f'UPDATE "{table}" SET created_at=?', (joined_stamp,))
            if "updated_at" in columns:
                connection.execute(f'UPDATE "{table}" SET updated_at=?', (stamp,))
        for table in ("reminders", "visit_tasks", "visit_task_details", "visit_records"):
            connection.execute(
                f'UPDATE "{table}" SET created_at=?,updated_at=?', (joined_stamp, stamp)
            )
        connection.execute("UPDATE audit_logs SET created_at=? WHERE created_at>?", (stamp, stamp))
        all_tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        counts = {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in all_tables
        }
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok" or connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("扩充资料完整性校验失败。")
        for username, password in passwords.items():
            stored = connection.execute(
                "SELECT password_hash FROM users WHERE username=?", (username,)
            ).fetchone()[0]
            if not verify_password(stored, password):
                raise RuntimeError("虚构账号密码校验失败。")
        payload_hash = hashlib.sha256(
            json.dumps(
                [
                    tuple(row)
                    for row in connection.execute(
                        "SELECT user_id,category,occurred_at,content,details_json "
                        "FROM life_records "
                        "ORDER BY id"
                    )
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    manifest = {
        "synthetic_demo": True,
        "profile": EXPANDED_PROFILE,
        "schema_version": SCHEMA_VERSION,
        "generated_at": cutoff.isoformat(),
        "experience_day": anchor.isoformat(),
        "first_record_day": first_day.isoformat(),
        "record_days": EXPANDED_DAYS,
        "record_categories": 6,
        "database": "data/app.db",
        "counts": counts,
        "integrity_check": "ok",
        "foreign_key_violations": 0,
        "ai_calls": 0,
        "cloud_imported": False,
        "deterministic_content_seed": EXPANDED_RANDOM_SEED,
        "content_sha256": payload_hash,
        "passwords_preserved": bool(old_passwords),
        "account_provenance": {name: "fictional-authored-persona" for name in EXPANDED_ACCOUNTS},
        "message_provenance": "authored-dialogue-not-live-model-output",
        "notice": "所有身份、账号、地址、健康与服务记录、聊天和商品均为虚构测试资料。"
        "界面不逐条标注，但不能当作真实人士记录、实测指标或医疗证据。",
    }
    (output / "demo_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    account_lines = [
        "# 体验账号与使用说明",
        "",
        manifest["notice"],
        "",
        "密码独立随机生成；已有五个虚构账号可沿用旧密码。不含真实管理员凭据。",
        "",
        "| 角色 | 昵称 | 账号 | 初始密码 |",
        "|---|---|---|---|",
    ]
    role_labels = {"operator": "管理者", "advisor": "顾问", "member": "会员"}
    account_lines.extend(
        f"| {role_labels[role]} | {name} | {username} | {passwords[username]} |"
        for username, (role, name) in EXPANDED_ACCOUNTS.items()
    )
    account_lines += [
        "",
        f"生活资料覆盖 {first_day} 至 {anchor}，共 {EXPANDED_DAYS} 天。",
        "林文静负责陈淑兰，周明远负责吴建国；第二位会员的年卡已到期，账号仍可登录。",
        "顾问有效期三年。包括六类生活记录、历史/待办提醒、上门安排、多主题聊天与购物。",
        "聊天是人工编写的虚构对话，不是现场调用 DeepSeek 的结果。",
        "手机号留空；未复制真实个人信息、处方或剂量；不自动上传到云端。",
        "首次使用先选择本地模式；不要用这些公开体验密码保护真实资料。",
    ]
    (output / "DEMO_ACCOUNTS.md").write_text("\n".join(account_lines) + "\n", encoding="utf-8")
    (output / "DEMO_ACCOUNTS.txt").write_text("\n".join(account_lines) + "\n", encoding="utf-8-sig")
    with database.connect() as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return manifest


def _expanded_chats(database, member_ids, anchor, provenance):
    from ollama_chat_app.data.database import timestamp_to_db

    topics = (
        (
            "三餐与买菜",
            180,
            (
                (
                    "我想把三餐记得清楚一点，从哪里开始？",
                    "先记今天吃了什么、在什么时间吃就好。想不起来的分量可以先留空。",
                ),
                (
                    "我中午在社区食堂吃了米饭和两样菜。",
                    "记成“午餐，在社区食堂吃米饭和两样菜”就很清楚。下次记得菜名时，再补进去也可以。",
                ),
                (
                    "这样不需要每顿都称重吧？",
                    "不用为了做生活记录增加负担。记不清的重量不必猜；以后有包装或实际称量时，再填写数值。",
                ),
            ),
        ),
        (
            "饮水小习惯",
            150,
            (
                (
                    "我总是忘了记录喝水。",
                    "可以把记录按钮放在容易找到的地方，喝完一杯顺手记一下，不用一次补齐整天。",
                ),
                (
                    "我的杯子大概250毫升。",
                    "如果杯子刻度写着250毫升，喝满一杯后可以这样记；只喝半杯时就按实际情况记。",
                ),
                (
                    "昨天漏记了一次怎么办？",
                    "记得就补上，不记得就留空。漏了一条记录，不等于昨天没喝水。",
                ),
            ),
        ),
        (
            "睡眠与生活节奏",
            90,
            (
                (
                    "早上起来怎么记昨晚的睡眠？",
                    "可以先写大约睡了多久、夜里醒了几次，再写醒来后的感受。想不准的地方用“大约”就好。",
                ),
                (
                    "昨晚醒来两次，上午有点困。",
                    "可以原样记下来。我们看一段时间的变化，不凭一晚的记录判断是什么病。",
                ),
                (
                    "我把白天午睡也写进去吗？",
                    "可以另写清楚午睡的时间和大约时长，和夜间睡眠分开，回看时更容易分辨。",
                ),
            ),
        ),
        (
            "散步和天气",
            30,
            (
                (
                    "下雨没出去走路，要怎么记录？",
                    "写清楚今天下雨、没外出就可以。如果在家活动过，也可以另外记实际做了什么。",
                ),
                (
                    "我在客厅来回走了十五分钟。",
                    "这条记录可以写“在客厅慢走约15分钟”。步数和耗能没测量时，不必随便填。",
                ),
                (
                    "明天再看状态决定。",
                    "好的，按自己的状态安排就行。需要调整活动强度时，也可以向熟悉您情况的专业人员咨询。",
                ),
            ),
        ),
        (
            "复查资料整理",
            7,
            (
                (
                    "我想把上次看病的事情记下来。",
                    "可以记录看病日期、地点、当时想咨询的问题和已有资料，先把发生过的事情记清楚。",
                ),
                (
                    "报告放在家里的蓝色文件夹。",
                    "这个位置说明很实用。可以写进备注，下次找资料时会方便一些。",
                ),
                (
                    "药名和剂量不记得了。",
                    "先留空，不要凭记忆猜。需要时核对原来的药盒、处方或向医生药师确认。",
                ),
            ),
        ),
        (
            "和顾问约时间",
            0,
            (
                (
                    "下次顾问来之前，我要准备什么？",
                    "把最近想聊的生活问题列两三条就够了，比如记录有没有困难、常用物品是否好拿。",
                ),
                (
                    "我想问问怎么整理这几个月的记录。",
                    "可以把这个问题写进预约备注。记录有漏缺也没关系，按已有资料一起回看。",
                ),
                (
                    "我先写好，到时候一起看。",
                    "好的。提交预约前再核对时间和地点，有变化时在平台里及时说明就行。",
                ),
            ),
        ),
    )
    with database.transaction() as connection:
        for index, member_id in enumerate(member_ids):
            preferred = "陈阿姨" if index == 0 else "吴叔叔"
            for title, offset, pairs in topics:
                started = datetime.combine(
                    anchor - timedelta(days=offset), time(19), ZoneInfo("Asia/Shanghai")
                )
                created = timestamp_to_db(started)
                cursor = connection.execute(
                    "INSERT INTO conversations(user_id,title,provider,model,created_at,updated_at) "
                    "VALUES(?,?,'fixture','synthetic-authored',?,?)",
                    (member_id, title, created, timestamp_to_db(started + timedelta(minutes=11))),
                )
                conversation = cursor.lastrowid
                for pair_index, pair in enumerate(pairs):
                    for role_index, (role, content) in enumerate(
                        zip(("user", "assistant"), pair, strict=True)
                    ):
                        at = timestamp_to_db(
                            started + timedelta(minutes=pair_index * 4 + role_index)
                        )
                        if role == "assistant" and pair_index == 0:
                            content = f"{preferred}，{content}"
                        connection.execute(
                            "INSERT INTO messages(conversation_id,sequence_no,role,content,status,"
                            "provider,model,created_at,updated_at) "
                            "VALUES(?,?,?,?,'complete','fixture','synthetic-authored',?,?)",
                            (conversation, pair_index * 2 + role_index + 1, role, content, at, at),
                        )
                connection.execute(
                    "INSERT INTO audit_logs(actor_user_id,action,entity_type,entity_id,"
                    "details_json,"
                    "created_at) VALUES(?,'synthetic.dialogue_imported','conversation',?,?,?)",
                    (member_id, conversation, json.dumps(provenance), created),
                )


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
    parser.add_argument("--profile", choices=("legacy", EXPANDED_PROFILE), default="legacy")
    parser.add_argument("--anchor", type=date.fromisoformat)
    parser.add_argument("--credential-seed", type=Path)
    args = parser.parse_args()
    manifest = build_demo(
        args.output,
        screenshots=not args.no_screenshots,
        profile=args.profile,
        anchor=args.anchor,
        credential_seed=args.credential_seed,
    )
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
