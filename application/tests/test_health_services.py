from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from ollama_chat_app.data.database import SCHEMA_VERSION, Database
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.health import (
    HealthRecordNotFoundError,
    HealthService,
    HealthValidationError,
    ReminderNotFoundError,
)


@pytest.fixture
def health_services(tmp_path: Path) -> tuple[Database, AuthService, HealthService]:
    database = Database(tmp_path / "app.db")
    database.initialize()
    return database, AuthService(database), HealthService(database)


def test_empty_database_is_created_at_current_schema_with_health_and_commerce_tables(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()

    expected_tables = {
        "users",
        "conversations",
        "messages",
        "chat_attachments",
        "user_file_contents",
        "member_profiles",
        "profile_facts",
        "life_records",
        "reminders",
        "advisor_profiles",
        "advisor_bindings",
        "memberships",
        "visit_tasks",
        "visit_records",
        "user_files",
        "medical_reports",
        "report_items",
        "ai_insights",
        "advisor_summaries",
        "app_settings",
        "audit_logs",
        "products",
        "product_recommendations",
        "orders",
        "user_preferences",
        "member_cart",
        "member_favorites",
        "staff_account_terms",
        "visit_task_details",
    }
    with database.connect() as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION == 6
        table_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            ).fetchall()
        }
        user_columns = {
            str(row["name"]): row for row in connection.execute("PRAGMA table_info(users)")
        }

    assert expected_tables == table_names
    assert {"role_code", "account_status", "updated_at"} <= set(user_columns)
    assert user_columns["role_code"]["dflt_value"] == "'member'"


def test_v1_migration_preserves_existing_account_conversation_and_messages(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-v1.db"
    created_at = "2026-01-02T03:04:05.000000Z"
    with sqlite3.connect(path) as connection:
        Database._create_schema_v1(connection)
        connection.execute(
            """
            INSERT INTO users (
                id, username, username_normalized, password_hash, created_at, last_login_at
            ) VALUES (7, '旧用户', '旧用户', 'legacy-hash', ?, NULL)
            """,
            (created_at,),
        )
        connection.execute(
            """
            INSERT INTO conversations (
                id, user_id, title, provider, model, created_at, updated_at
            ) VALUES (11, 7, '保留的对话', 'local', 'qwen3:4b', ?, ?)
            """,
            (created_at, created_at),
        )
        connection.execute(
            """
            INSERT INTO messages (
                id, conversation_id, sequence_no, role, content, status,
                error_message, provider, model, created_at, updated_at
            ) VALUES (13, 11, 1, 'user', '请保留我', 'complete',
                      NULL, 'local', 'qwen3:4b', ?, ?)
            """,
            (created_at, created_at),
        )
        connection.execute("PRAGMA user_version = 1")
        connection.commit()

    database = Database(path)
    database.initialize()
    database.initialize()  # Migration and current-version initialization are idempotent.

    with database.connect() as connection:
        user = connection.execute(
            "SELECT username, role_code, account_status, updated_at FROM users WHERE id = 7"
        ).fetchone()
        conversation = connection.execute(
            "SELECT title, provider, model FROM conversations WHERE id = 11"
        ).fetchone()
        message = connection.execute(
            "SELECT content, status FROM messages WHERE id = 13"
        ).fetchone()
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION

    assert tuple(user) == ("旧用户", "member", "active", created_at)
    assert tuple(conversation) == ("保留的对话", "local", "qwen3:4b")
    assert tuple(message) == ("请保留我", "complete")


def test_life_record_crud_preserves_local_date_details_and_validates_ai_source(
    health_services: tuple[Database, AuthService, HealthService],
) -> None:
    _database, auth, health = health_services
    user = auth.register("alice", "correct-horse-battery-staple")
    china_time = timezone(timedelta(hours=8))
    occurred_at = datetime(2026, 9, 4, 23, 30, tzinfo=china_time)

    record_id = health.add_life_record(
        user.id,
        "water",
        occurred_at,
        " 饮水 300 毫升 ",
        details={"amount_ml": 300, "temperature": "warm"},
    )

    records = health.list_life_records(user.id, category="water")
    assert len(records) == 1
    assert records[0] == {
        "id": record_id,
        "user_id": user.id,
        "category": "water",
        "occurred_at": datetime(2026, 9, 4, 15, 30, tzinfo=UTC),
        "local_date": "2026-09-04",
        "timezone_offset_minutes": 480,
        "content": "饮水 300 毫升",
        "details": {"amount_ml": 300, "temperature": "warm"},
        "source": "manual",
        "created_at": records[0]["created_at"],
        "updated_at": records[0]["updated_at"],
    }

    with pytest.raises(HealthValidationError, match="用户确认"):
        health.add_life_record(user.id, "diet", occurred_at, "AI 猜测", source="ai")
    naive_id = health.add_life_record(
        user.id, "sleep", datetime(2026, 9, 4, 8, 0), "默认北京时间"
    )
    naive_record = health.list_life_records(user.id, category="sleep")[0]
    assert naive_record["occurred_at"] == datetime(2026, 9, 4, tzinfo=UTC)
    assert naive_record["timezone_offset_minutes"] == 480
    health.delete_life_record(user.id, naive_id)

    health.delete_life_record(user.id, record_id)
    assert health.list_life_records(user.id) == []
    with pytest.raises(HealthRecordNotFoundError):
        health.delete_life_record(user.id, record_id)


def test_profile_partial_updates_validation_and_dashboard(
    health_services: tuple[Database, AuthService, HealthService],
) -> None:
    _database, auth, health = health_services
    user = auth.register("alice", "correct-horse-battery-staple")

    assert health.get_profile(user.id)["display_name"] is None
    health.save_profile(
        user.id,
        {
            "display_name": " 小爱 ",
            "birth_date": "1995-02-03",
            "gender": "female",
            "height_cm": 165,
            "health_goals": "规律运动",
        },
    )
    health.save_profile(user.id, {"dietary_preferences": "少盐"})
    profile = health.get_profile(user.id)

    assert profile["display_name"] == "小爱"
    assert profile["birth_date"] == "1995-02-03"
    assert profile["height_cm"] == 165.0
    assert profile["health_goals"] == "规律运动"
    assert profile["dietary_preferences"] == "少盐"
    assert profile["medical_notes"] is None
    assert health.get_dashboard(user.id)["profile_completion_percent"] == 46

    with pytest.raises(HealthValidationError, match="不支持的字段"):
        health.save_profile(user.id, {"role_code": "operator"})
    with pytest.raises(HealthValidationError, match="身高"):
        health.save_profile(user.id, {"height_cm": 500})


def test_member_health_service_rejects_staff_and_disabled_accounts(
    health_services: tuple[Database, AuthService, HealthService],
) -> None:
    database, auth, health = health_services
    member = auth.register("alice", "correct-horse-battery-staple")
    operator = auth.bootstrap_operator("operator", "operator-password-123")

    with pytest.raises(HealthValidationError, match="仅会员账号"):
        health.get_dashboard(operator.id)

    with database.transaction() as connection:
        connection.execute(
            "UPDATE users SET account_status = 'disabled' WHERE id = ?", (member.id,)
        )
    with pytest.raises(HealthValidationError, match="账号已停用"):
        health.get_profile(member.id)


def test_reminder_crud_and_toggle(
    health_services: tuple[Database, AuthService, HealthService],
) -> None:
    _database, auth, health = health_services
    user = auth.register("alice", "correct-horse-battery-staple")
    scheduled_at = datetime(2030, 1, 2, 9, 15, tzinfo=timezone(timedelta(hours=8)))

    reminder_id = health.add_reminder(user.id, " 饮水提醒 ", scheduled_at.isoformat(), "water")
    reminders = health.list_reminders(user.id)
    assert [(item["id"], item["title"], item["status"]) for item in reminders] == [
        (reminder_id, "饮水提醒", "active")
    ]
    assert reminders[0]["scheduled_at"] == datetime(2030, 1, 2, 1, 15, tzinfo=UTC)

    assert health.toggle_reminder(user.id, reminder_id)["status"] == "disabled"
    assert health.toggle_reminder(user.id, reminder_id, enabled=True)["status"] == "active"
    health.delete_reminder(user.id, reminder_id)
    assert health.list_reminders(user.id) == []
    with pytest.raises(ReminderNotFoundError):
        health.toggle_reminder(user.id, reminder_id)


def test_iso_datetime_strings_preserve_explicit_zones_and_default_to_beijing(
    health_services: tuple[Database, AuthService, HealthService],
) -> None:
    _database, auth, health = health_services
    user = auth.register("alice", "correct-horse-battery-staple")

    record_id = health.add_life_record(
        user.id,
        "sleep",
        "2026-09-04T23:15:00+08:00",
        "准备睡觉",
    )
    reminder_id = health.add_reminder(user.id, "起床", "2026-09-05T00:00:00Z")

    assert health.list_life_records(user.id)[0]["id"] == record_id
    assert health.list_life_records(user.id)[0]["local_date"] == "2026-09-04"
    assert health.list_life_records(user.id)[0]["timezone_offset_minutes"] == 480
    assert health.list_reminders(user.id)[0]["id"] == reminder_id
    naive_reminder_id = health.add_reminder(user.id, "默认北京时间", "2026-09-05T08:00:00")
    naive_reminder = next(
        item for item in health.list_reminders(user.id) if item["id"] == naive_reminder_id
    )
    assert naive_reminder["scheduled_at"] == datetime(2026, 9, 5, tzinfo=UTC)
    with pytest.raises(HealthValidationError, match="ISO 8601"):
        health.add_life_record(user.id, "sleep", "明天晚上", "无效时间")


def test_record_profile_and_reminder_operations_are_isolated_between_users(
    health_services: tuple[Database, AuthService, HealthService],
) -> None:
    _database, auth, health = health_services
    alice = auth.register("alice", "correct-horse-battery-staple")
    bob = auth.register("bob", "another-correct-password")
    now = datetime.now(UTC)
    record_id = health.add_life_record(alice.id, "activity", now, "散步 20 分钟")
    reminder_id = health.add_reminder(alice.id, "散步", now + timedelta(hours=1))
    health.save_profile(alice.id, {"display_name": "Alice"})

    assert health.list_life_records(bob.id) == []
    assert health.list_reminders(bob.id) == []
    assert health.get_profile(bob.id)["display_name"] is None
    with pytest.raises(HealthRecordNotFoundError):
        health.delete_life_record(bob.id, record_id)
    with pytest.raises(ReminderNotFoundError):
        health.toggle_reminder(bob.id, reminder_id)
    with pytest.raises(ReminderNotFoundError):
        health.delete_reminder(bob.id, reminder_id)

    assert health.list_life_records(alice.id)[0]["content"] == "散步 20 分钟"
    assert health.list_reminders(alice.id)[0]["title"] == "散步"
    assert health.get_profile(alice.id)["display_name"] == "Alice"
    assert health.get_service_summary(alice.id) == {
        "user_id": alice.id,
        "membership": None,
        "advisor": None,
        "pending_visit_tasks": [],
        "next_visit": None,
        "visit_records": [],
        "completed_visit_count": 0,
    }
