from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from ollama_chat_app.data.database import Database
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.health import (
    HealthService,
    HealthValidationError,
    ReminderNotFoundError,
)
from ollama_chat_app.ui.health_records_panel import HealthRecordsPanel


@pytest.fixture
def services(tmp_path: Path) -> tuple[Database, AuthService, HealthService]:
    database = Database(tmp_path / "health-v1.db")
    database.initialize()
    return database, AuthService(database), HealthService(database)


def test_all_five_record_categories_validate_persist_and_aggregate(
    services: tuple[Database, AuthService, HealthService],
) -> None:
    _database, auth, health = services
    user = auth.register("records-user", "correct-horse-battery-staple")
    china = timezone(timedelta(hours=8))
    values = (
        ("diet", {"meal_type": "lunch", "calories_kcal": 520}),
        ("water", {"amount_ml": 350}),
        ("activity", {"activity_type": "散步", "duration_minutes": 30, "steps": 4200}),
        ("sleep", {"duration_hours": 7.5, "quality": 4}),
        (
            "environment",
            {"temperature_c": 24.5, "humidity_percent": 51, "air_quality": "通风良好"},
        ),
    )
    for offset, (category, details) in enumerate(values):
        health.add_life_record(
            user.id,
            category,
            datetime(2026, 9, 1 + offset, 9, tzinfo=china),
            f"{category} 的已发生事实",
            details=details,
        )

    records = health.list_life_records(
        user.id, start_date="2026-09-01", end_date=date(2026, 9, 5), limit=20
    )
    assert {record["category"] for record in records} == {
        "diet",
        "water",
        "activity",
        "sleep",
        "environment",
    }
    assert next(record for record in records if record["category"] == "water")["details"] == {
        "amount_ml": 350
    }

    statistics = health.get_record_statistics(user.id, 7, as_of="2026-09-05")
    assert statistics["total_records"] == 5
    assert statistics["by_category"] == {
        "activity": 1,
        "diet": 1,
        "environment": 1,
        "sleep": 1,
        "water": 1,
    }
    assert statistics["metrics"] == {
        "water_amount_ml": 350,
        "activity_minutes": 30,
        "activity_steps": 4200,
        "sleep_average_hours": 7.5,
        "sleep_average_quality": 4.0,
        "diet_calories_kcal": 520,
        "environment_average_temperature_c": 24.5,
        "environment_average_humidity_percent": 51.0,
    }
    assert "不构成" in statistics["notice"]


@pytest.mark.parametrize(
    ("category", "details", "message"),
    [
        ("water", {"amount_ml": 0}, "饮水量"),
        ("activity", {"duration_minutes": 1441}, "活动时长"),
        ("activity", {"steps": -1}, "步数"),
        ("sleep", {"duration_hours": 25}, "睡眠时长"),
        ("sleep", {"quality": 6}, "睡眠质量"),
        ("environment", {"humidity_percent": 101}, "环境湿度"),
        ("diet", {"calories_kcal": 20_001}, "热量"),
    ],
)
def test_record_details_have_plain_language_numeric_ranges(
    services: tuple[Database, AuthService, HealthService],
    category: str,
    details: dict[str, object],
    message: str,
) -> None:
    _database, auth, health = services
    user = auth.register(f"range-{category}-{message}", "correct-horse-battery-staple")
    with pytest.raises(HealthValidationError, match=message):
        health.add_life_record(
            user.id,
            category,
            datetime.now().astimezone(),
            "事实",
            details=details,
        )


def test_record_date_filters_and_period_validation_are_scoped_to_user(
    services: tuple[Database, AuthService, HealthService],
) -> None:
    _database, auth, health = services
    alice = auth.register("date-alice", "correct-horse-battery-staple")
    bob = auth.register("date-bob", "correct-horse-battery-staple")
    china = timezone(timedelta(hours=8))
    health.add_life_record(alice.id, "water", datetime(2026, 9, 2, tzinfo=china), "甲")
    health.add_life_record(alice.id, "water", datetime(2026, 9, 4, tzinfo=china), "乙")
    health.add_life_record(bob.id, "water", datetime(2026, 9, 4, tzinfo=china), "他人")

    assert [
        record["content"]
        for record in health.list_life_records(
            alice.id, start_date="2026-09-03", end_date="2026-09-05"
        )
    ] == ["乙"]
    assert health.get_record_statistics(alice.id, 7, as_of=date(2026, 9, 5))["total_records"] == 2
    with pytest.raises(HealthValidationError, match="开始日期"):
        health.list_life_records(alice.id, start_date="2026-09-06", end_date="2026-09-05")
    with pytest.raises(HealthValidationError, match="7 天或近 30 天"):
        health.get_record_statistics(alice.id, 14)


def test_reminders_can_be_edited_paused_resumed_and_never_be_medication_reminders(
    services: tuple[Database, AuthService, HealthService],
) -> None:
    _database, auth, health = services
    alice = auth.register("reminder-alice", "correct-horse-battery-staple")
    bob = auth.register("reminder-bob", "correct-horse-battery-staple")
    initial = datetime.now(UTC) + timedelta(days=1)
    reminder_id = health.add_reminder(alice.id, "记录饮水", initial, "water")

    edited = health.update_reminder(
        alice.id,
        reminder_id,
        title="晚间散步",
        scheduled_at=initial + timedelta(hours=2),
        reminder_type="activity",
    )
    assert edited["title"] == "晚间散步"
    assert edited["reminder_type"] == "activity"
    assert health.pause_reminder(alice.id, reminder_id)["status"] == "disabled"
    assert health.resume_reminder(alice.id, reminder_id)["status"] == "active"

    with pytest.raises(ReminderNotFoundError, match="无权访问"):
        health.update_reminder(bob.id, reminder_id, title="越权编辑")
    with pytest.raises(HealthValidationError, match="药物提醒"):
        health.add_reminder(alice.id, "晚上吃药", initial, "custom")
    with pytest.raises(HealthValidationError, match="药物提醒"):
        health.update_reminder(alice.id, reminder_id, title="服药")
    with pytest.raises(HealthValidationError, match="提醒类型无效"):
        health.add_reminder(alice.id, "不支持的类型", initial, "medication")


def test_health_records_panel_loads_records_statistics_and_reminders(
    qtbot: object,
    services: tuple[Database, AuthService, HealthService],
) -> None:
    _database, auth, health = services
    user = auth.register("panel-user", "correct-horse-battery-staple")
    health.add_life_record(
        user.id,
        "water",
        datetime.now().astimezone(),
        "喝水 250 毫升",
        details={"amount_ml": 250},
    )
    health.add_reminder(
        user.id,
        "午后喝水",
        datetime.now(UTC) + timedelta(days=1),
        "water",
    )

    panel = HealthRecordsPanel(health)
    qtbot.addWidget(panel)  # type: ignore[attr-defined]
    panel.set_user(user.id)

    assert panel.record_table.rowCount() == 1
    assert panel.record_table.item(0, 2).text() == "喝水 250 毫升"
    assert panel.reminder_table.rowCount() == 1
    assert panel.reminder_table.item(0, 3).text() == "启用"
    assert "250 毫升" in panel.statistics_summary.text()
