from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from ..data.database import Database, timestamp_from_db, timestamp_to_db, utc_now
from ..time_utils import BEIJING_TIMEZONE, as_beijing, beijing_today

LIFE_RECORD_CATEGORIES = frozenset({"diet", "water", "activity", "sleep", "environment"})
LIFE_RECORD_SOURCES = frozenset({"manual", "import", "device", "ai_confirmed"})
REMINDER_TYPES = frozenset(
    {"water", "diet", "sleep", "activity", "environment", "visit", "membership", "custom"}
)
PROFILE_FIELDS = frozenset(
    {
        "display_name",
        "birth_date",
        "gender",
        "phone",
        "living_situation",
        "emergency_contact_name",
        "emergency_contact_phone",
        "ai_preferred_name",
        "reminder_frequency",
        "height_cm",
        "health_goals",
        "dietary_preferences",
        "medical_notes",
    }
)
GENDERS = frozenset({"female", "male", "other", "unspecified"})
MAX_CONTENT_LENGTH = 10_000
MAX_TITLE_LENGTH = 200
MAX_PROFILE_TEXT_LENGTH = 4_000
MIN_RECORD_DATE = date(1900, 1, 1)
MEDICATION_REMINDER_TERMS = frozenset(
    {"medicine", "medication", "drug", "pill", "服药", "吃药", "用药", "药物", "药品"}
)


class HealthServiceError(RuntimeError):
    """Base error for local health-record operations."""


class HealthValidationError(HealthServiceError, ValueError):
    """Raised when UI input does not satisfy a documented business rule."""


class HealthRecordNotFoundError(HealthServiceError, LookupError):
    """Raised when a record does not exist or belongs to another user."""


class ReminderNotFoundError(HealthServiceError, LookupError):
    """Raised when a reminder does not exist or belongs to another user."""


class HealthService:
    """User-scoped APIs for the first local health-platform milestone.

    Every public operation requires a user id and every mutable query includes
    that user id in its predicate. This makes accidental cross-account access
    difficult even when a UI passes an id copied from another screen.
    """

    def __init__(self, database: Database | None = None) -> None:
        self.database = database or Database()
        self.database.initialize()

    def get_dashboard(self, user_id: int) -> dict[str, Any]:
        user_id = self._require_user(user_id)
        today_date = beijing_today()
        today = today_date.isoformat()
        day_start = self._beijing_day_start(today_date)
        day_end = self._beijing_day_start(today_date + timedelta(days=1))
        now = timestamp_to_db(utc_now())
        with self.database.connect() as connection:
            category_rows = connection.execute(
                """
                SELECT category, COUNT(*) AS record_count
                FROM life_records
                WHERE user_id = ? AND occurred_at >= ? AND occurred_at < ?
                GROUP BY category
                """,
                (user_id, day_start, day_end),
            ).fetchall()
            reminder_rows = connection.execute(
                """
                SELECT id, user_id, title, reminder_type, scheduled_at, status,
                       created_at, updated_at
                FROM reminders
                WHERE user_id = ? AND status = 'active' AND scheduled_at >= ?
                ORDER BY scheduled_at, id
                LIMIT 5
                """,
                (user_id, now),
            ).fetchall()
            today_rows = connection.execute(
                """
                SELECT id, user_id, category, occurred_at, local_date,
                       timezone_offset_minutes, content, details_json, source,
                       created_at, updated_at
                FROM life_records
                WHERE user_id = ? AND occurred_at >= ? AND occurred_at < ?
                ORDER BY occurred_at DESC, id DESC
                LIMIT 8
                """,
                (user_id, day_start, day_end),
            ).fetchall()
            total_today = int(
                connection.execute(
                    "SELECT COUNT(*) FROM life_records "
                    "WHERE user_id = ? AND occurred_at >= ? AND occurred_at < ?",
                    (user_id, day_start, day_end),
                ).fetchone()[0]
            )

        by_category = {category: 0 for category in sorted(LIFE_RECORD_CATEGORIES)}
        by_category.update(
            {str(row["category"]): int(row["record_count"]) for row in category_rows}
        )
        profile = self.get_profile(user_id)
        populated_profile_fields = sum(profile[field] not in (None, "") for field in PROFILE_FIELDS)
        fact_labels = {
            "display_name": "姓名",
            "birth_date": "出生日期",
            "gender": "性别",
            "living_situation": "居住情况",
            "ai_preferred_name": "AI 称呼",
            "reminder_frequency": "提醒频率",
        }
        display_values = {
            "female": "女",
            "male": "男",
            "other": "其他 / 不便说明",
            "normal": "按每条提醒执行",
            "low": "适当减少",
            "off": "暂不主动提醒",
        }
        facts = [
            {"name": label, "value": display_values.get(profile[field], profile[field])}
            for field, label in fact_labels.items()
            if profile.get(field) not in (None, "")
        ]
        today_records = [self._life_record_from_row(row) for row in today_rows]
        reminders = [self._reminder_from_row(row) for row in reminder_rows]
        return {
            "user_id": user_id,
            "local_date": today,
            "today_record_count": total_today,
            "today_records_by_category": by_category,
            "facts": facts,
            "today_records": today_records,
            "reminders": reminders,
            "upcoming_reminders": reminders,
            "profile_completion_percent": round(
                populated_profile_fields * 100 / len(PROFILE_FIELDS)
            ),
            "recent_records": self.list_life_records(user_id, limit=5),
        }

    def list_life_records(
        self,
        user_id: int,
        category: str | None = None,
        limit: int = 100,
        *,
        start_date: date | str | None = None,
        end_date: date | str | None = None,
    ) -> list[dict[str, Any]]:
        user_id = self._require_user(user_id)
        category_value = None if category is None else self._validate_category(category)
        limit_value = self._validate_limit(limit)
        query = (
            "SELECT id, user_id, category, occurred_at, local_date, "
            "timezone_offset_minutes, content, details_json, source, created_at, updated_at "
            "FROM life_records WHERE user_id = ?"
        )
        parameters: list[object] = [user_id]
        if category_value is not None:
            query += " AND category = ?"
            parameters.append(category_value)
        start_value = None if start_date is None else self._parse_date_input(start_date, "开始日期")
        end_value = None if end_date is None else self._parse_date_input(end_date, "结束日期")
        if start_value is not None and end_value is not None and start_value > end_value:
            raise HealthValidationError("开始日期不能晚于结束日期")
        if start_value is not None:
            query += " AND occurred_at >= ?"
            parameters.append(self._beijing_day_start(start_value))
        if end_value is not None and end_value < date.max:
            query += " AND occurred_at < ?"
            parameters.append(self._beijing_day_start(end_value + timedelta(days=1)))
        query += " ORDER BY occurred_at DESC, id DESC LIMIT ?"
        parameters.append(limit_value)
        with self.database.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._life_record_from_row(row) for row in rows]

    def add_life_record(
        self,
        user_id: int,
        category: str,
        occurred_at: datetime | str,
        content: str,
        source: str = "manual",
        details: Mapping[str, Any] | None = None,
    ) -> int:
        user_id = self._require_user(user_id)
        category_value = self._validate_category(category)
        occurred_at_utc, local_date, offset_minutes = self._normalize_local_timestamp(occurred_at)
        content_value = self._clean_required_text(content, "记录内容", maximum=MAX_CONTENT_LENGTH)
        source_value = self._clean_required_text(source, "记录来源", maximum=50)
        if source_value not in LIFE_RECORD_SOURCES:
            raise HealthValidationError("记录来源无效；AI 内容必须经用户确认后再保存")
        details_json = self._mapping_to_json(
            self._validate_record_details(category_value, details), "记录详情"
        )
        now = timestamp_to_db(utc_now())
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO life_records (
                    user_id, category, occurred_at, local_date,
                    timezone_offset_minutes, content, details_json, source,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    category_value,
                    occurred_at_utc,
                    local_date,
                    offset_minutes,
                    content_value,
                    details_json,
                    source_value,
                    now,
                    now,
                ),
            )
            record_id = int(cursor.lastrowid)
            self._add_audit_log(
                connection, user_id, "life_record.created", "life_record", record_id
            )
        return record_id

    def get_record_statistics(
        self,
        user_id: int,
        days: int = 7,
        *,
        as_of: date | datetime | str | None = None,
    ) -> dict[str, Any]:
        """Return factual 7/30-day summaries without health scores or diagnosis."""
        user_id = self._require_user(user_id)
        if isinstance(days, bool) or days not in {7, 30}:
            raise HealthValidationError("统计周期只能选择近 7 天或近 30 天")
        end_date = self._statistics_date(as_of)
        start_date = end_date - timedelta(days=days - 1)
        records = self.list_life_records(
            user_id,
            limit=500,
            start_date=start_date,
            end_date=end_date,
        )

        category_counts = {category: 0 for category in sorted(LIFE_RECORD_CATEGORIES)}
        daily_counts = {
            (start_date + timedelta(days=offset)).isoformat(): 0 for offset in range(days)
        }
        metrics: dict[str, float | int | None] = {
            "water_amount_ml": 0,
            "activity_minutes": 0,
            "activity_steps": 0,
            "sleep_average_hours": None,
            "sleep_average_quality": None,
            "diet_calories_kcal": 0,
            "environment_average_temperature_c": None,
            "environment_average_humidity_percent": None,
        }
        sleep_hours: list[float] = []
        sleep_quality: list[float] = []
        temperatures: list[float] = []
        humidities: list[float] = []
        for record in records:
            category = str(record["category"])
            category_counts[category] += 1
            record_day = as_beijing(record["occurred_at"]).date().isoformat()
            daily_counts[record_day] += 1
            details = record.get("details") or {}
            if category == "water":
                metrics["water_amount_ml"] = int(metrics["water_amount_ml"] or 0) + int(
                    details.get("amount_ml", 0)
                )
            elif category == "activity":
                metrics["activity_minutes"] = int(metrics["activity_minutes"] or 0) + int(
                    details.get("duration_minutes", 0)
                )
                metrics["activity_steps"] = int(metrics["activity_steps"] or 0) + int(
                    details.get("steps", 0)
                )
            elif category == "sleep":
                if "duration_hours" in details:
                    sleep_hours.append(float(details["duration_hours"]))
                if "quality" in details:
                    sleep_quality.append(float(details["quality"]))
            elif category == "diet":
                metrics["diet_calories_kcal"] = int(metrics["diet_calories_kcal"] or 0) + int(
                    details.get("calories_kcal", 0)
                )
            elif category == "environment":
                if "temperature_c" in details:
                    temperatures.append(float(details["temperature_c"]))
                if "humidity_percent" in details:
                    humidities.append(float(details["humidity_percent"]))
        metrics["sleep_average_hours"] = self._average(sleep_hours)
        metrics["sleep_average_quality"] = self._average(sleep_quality)
        metrics["environment_average_temperature_c"] = self._average(temperatures)
        metrics["environment_average_humidity_percent"] = self._average(humidities)
        return {
            "user_id": user_id,
            "period_days": days,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "total_records": len(records),
            "by_category": category_counts,
            "by_date": daily_counts,
            "metrics": metrics,
            "notice": "仅汇总用户确认或录入的生活事实，不构成健康评分、诊断或治疗建议。",
        }

    def delete_life_record(self, user_id: int, record_id: int) -> None:
        user_id = self._require_user(user_id)
        record_id = self._positive_integer(record_id, "记录编号")
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM life_records WHERE id = ? AND user_id = ?",
                (record_id, user_id),
            )
            if cursor.rowcount != 1:
                raise HealthRecordNotFoundError("记录不存在或无权访问")
            self._add_audit_log(
                connection, user_id, "life_record.deleted", "life_record", record_id
            )

    def get_profile(self, user_id: int) -> dict[str, Any]:
        user_id = self._require_user(user_id)
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT user_id, display_name, birth_date, gender, phone,
                       living_situation, emergency_contact_name,
                       emergency_contact_phone, ai_preferred_name,
                       reminder_frequency, height_cm,
                       health_goals, dietary_preferences, medical_notes,
                       created_at, updated_at
                FROM member_profiles
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        if row is None:
            return {
                "user_id": user_id,
                **{field: None for field in PROFILE_FIELDS},
                "created_at": None,
                "updated_at": None,
            }
        return {
            "user_id": int(row["user_id"]),
            "display_name": _optional_text(row["display_name"]),
            "birth_date": _optional_text(row["birth_date"]),
            "gender": _optional_text(row["gender"]),
            "phone": _optional_text(row["phone"]),
            "living_situation": _optional_text(row["living_situation"]),
            "emergency_contact_name": _optional_text(row["emergency_contact_name"]),
            "emergency_contact_phone": _optional_text(row["emergency_contact_phone"]),
            "ai_preferred_name": _optional_text(row["ai_preferred_name"]),
            "reminder_frequency": _optional_text(row["reminder_frequency"]),
            "height_cm": None if row["height_cm"] is None else float(row["height_cm"]),
            "health_goals": _optional_text(row["health_goals"]),
            "dietary_preferences": _optional_text(row["dietary_preferences"]),
            "medical_notes": _optional_text(row["medical_notes"]),
            "created_at": timestamp_from_db(row["created_at"]),
            "updated_at": timestamp_from_db(row["updated_at"]),
        }

    def save_profile(self, user_id: int, values: dict[str, Any]) -> None:
        user_id = self._require_user(user_id)
        if not isinstance(values, dict):
            raise HealthValidationError("档案内容格式无效")
        unknown = set(values) - PROFILE_FIELDS
        if unknown:
            raise HealthValidationError(f"档案包含不支持的字段：{', '.join(sorted(unknown))}")
        current = self.get_profile(user_id)
        merged = {field: current[field] for field in PROFILE_FIELDS}
        for field, value in values.items():
            merged[field] = self._validate_profile_value(field, value)
        now = timestamp_to_db(utc_now())
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO member_profiles (
                    user_id, display_name, birth_date, gender, phone,
                    living_situation, emergency_contact_name,
                    emergency_contact_phone, ai_preferred_name,
                    reminder_frequency, height_cm,
                    health_goals, dietary_preferences, medical_notes,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    birth_date = excluded.birth_date,
                    gender = excluded.gender,
                    phone = excluded.phone,
                    living_situation = excluded.living_situation,
                    emergency_contact_name = excluded.emergency_contact_name,
                    emergency_contact_phone = excluded.emergency_contact_phone,
                    ai_preferred_name = excluded.ai_preferred_name,
                    reminder_frequency = excluded.reminder_frequency,
                    height_cm = excluded.height_cm,
                    health_goals = excluded.health_goals,
                    dietary_preferences = excluded.dietary_preferences,
                    medical_notes = excluded.medical_notes,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    merged["display_name"],
                    merged["birth_date"],
                    merged["gender"],
                    merged["phone"],
                    merged["living_situation"],
                    merged["emergency_contact_name"],
                    merged["emergency_contact_phone"],
                    merged["ai_preferred_name"],
                    merged["reminder_frequency"],
                    merged["height_cm"],
                    merged["health_goals"],
                    merged["dietary_preferences"],
                    merged["medical_notes"],
                    now,
                    now,
                ),
            )
            self._add_audit_log(
                connection, user_id, "member_profile.saved", "member_profile", user_id
            )

    def list_reminders(self, user_id: int) -> list[dict[str, Any]]:
        user_id = self._require_user(user_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, title, reminder_type, scheduled_at, status,
                       created_at, updated_at
                FROM reminders
                WHERE user_id = ?
                ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,
                         scheduled_at, id
                """,
                (user_id,),
            ).fetchall()
        return [self._reminder_from_row(row) for row in rows]

    def add_reminder(
        self,
        user_id: int,
        title: str,
        scheduled_at: datetime | str,
        reminder_type: str = "custom",
    ) -> int:
        user_id = self._require_user(user_id)
        title_value = self._clean_required_text(title, "提醒标题", maximum=MAX_TITLE_LENGTH)
        reminder_type_value = self._validate_reminder(title_value, reminder_type)
        scheduled_value = timestamp_to_db(self._reminder_datetime(scheduled_at))
        now = timestamp_to_db(utc_now())
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO reminders (
                    user_id, title, reminder_type, scheduled_at,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?)
                """,
                (user_id, title_value, reminder_type_value, scheduled_value, now, now),
            )
            reminder_id = int(cursor.lastrowid)
            self._add_audit_log(connection, user_id, "reminder.created", "reminder", reminder_id)
        return reminder_id

    def update_reminder(
        self,
        user_id: int,
        reminder_id: int,
        *,
        title: str | None = None,
        scheduled_at: datetime | str | None = None,
        reminder_type: str | None = None,
    ) -> dict[str, Any]:
        """Edit a reminder while retaining its enabled/paused state."""
        user_id = self._require_user(user_id)
        reminder_id = self._positive_integer(reminder_id, "提醒编号")
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT id, title, reminder_type, scheduled_at
                FROM reminders WHERE id = ? AND user_id = ?
                """,
                (reminder_id, user_id),
            ).fetchone()
            if row is None:
                raise ReminderNotFoundError("提醒不存在或无权访问")
            title_value = (
                str(row["title"])
                if title is None
                else self._clean_required_text(title, "提醒标题", maximum=MAX_TITLE_LENGTH)
            )
            type_value = str(row["reminder_type"]) if reminder_type is None else reminder_type
            type_value = self._validate_reminder(title_value, type_value)
            scheduled_value = (
                str(row["scheduled_at"])
                if scheduled_at is None
                else timestamp_to_db(self._reminder_datetime(scheduled_at))
            )
            now = timestamp_to_db(utc_now())
            connection.execute(
                """
                UPDATE reminders
                SET title = ?, reminder_type = ?, scheduled_at = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (title_value, type_value, scheduled_value, now, reminder_id, user_id),
            )
            updated = connection.execute(
                """
                SELECT id, user_id, title, reminder_type, scheduled_at, status,
                       created_at, updated_at
                FROM reminders WHERE id = ? AND user_id = ?
                """,
                (reminder_id, user_id),
            ).fetchone()
            self._add_audit_log(connection, user_id, "reminder.updated", "reminder", reminder_id)
        if updated is None:  # pragma: no cover - same transaction and guarded id
            raise ReminderNotFoundError("提醒不存在或无权访问")
        return self._reminder_from_row(updated)

    def pause_reminder(self, user_id: int, reminder_id: int) -> dict[str, Any]:
        """Pause a reminder; schema status ``disabled`` is presented as paused in the UI."""
        return self.toggle_reminder(user_id, reminder_id, enabled=False)

    def resume_reminder(self, user_id: int, reminder_id: int) -> dict[str, Any]:
        return self.toggle_reminder(user_id, reminder_id, enabled=True)

    def toggle_reminder(
        self, user_id: int, reminder_id: int, enabled: bool | None = None
    ) -> dict[str, Any]:
        user_id = self._require_user(user_id)
        reminder_id = self._positive_integer(reminder_id, "提醒编号")
        if enabled is not None and not isinstance(enabled, bool):
            raise HealthValidationError("提醒开关必须是布尔值")
        now = timestamp_to_db(utc_now())
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM reminders WHERE id = ? AND user_id = ?",
                (reminder_id, user_id),
            ).fetchone()
            if row is None:
                raise ReminderNotFoundError("提醒不存在或无权访问")
            current_status = str(row["status"])
            new_status = (
                "active"
                if enabled
                else "disabled"
                if enabled is not None
                else "disabled"
                if current_status == "active"
                else "active"
            )
            connection.execute(
                "UPDATE reminders SET status = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (new_status, now, reminder_id, user_id),
            )
            updated = connection.execute(
                """
                SELECT id, user_id, title, reminder_type, scheduled_at, status,
                       created_at, updated_at
                FROM reminders WHERE id = ? AND user_id = ?
                """,
                (reminder_id, user_id),
            ).fetchone()
            self._add_audit_log(connection, user_id, "reminder.toggled", "reminder", reminder_id)
        if updated is None:  # pragma: no cover - same transaction and guarded id
            raise ReminderNotFoundError("提醒不存在或无权访问")
        return self._reminder_from_row(updated)

    def delete_reminder(self, user_id: int, reminder_id: int) -> None:
        user_id = self._require_user(user_id)
        reminder_id = self._positive_integer(reminder_id, "提醒编号")
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM reminders WHERE id = ? AND user_id = ?",
                (reminder_id, user_id),
            )
            if cursor.rowcount != 1:
                raise ReminderNotFoundError("提醒不存在或无权访问")
            self._add_audit_log(connection, user_id, "reminder.deleted", "reminder", reminder_id)

    def get_service_summary(self, user_id: int) -> dict[str, Any]:
        user_id = self._require_user(user_id)
        with self.database.connect() as connection:
            membership = connection.execute(
                """
                SELECT id, plan_code, status, starts_at, ends_at, benefits_json
                FROM memberships
                WHERE user_id = ? AND status = 'active'
                ORDER BY starts_at DESC, id DESC LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            advisor = connection.execute(
                """
                SELECT b.id AS binding_id, b.advisor_user_id, b.started_at,
                       COALESCE(p.display_name, u.username) AS display_name,
                       p.organization, p.specialty, p.bio
                FROM advisor_bindings b
                JOIN users u ON u.id = b.advisor_user_id
                LEFT JOIN advisor_profiles p ON p.user_id = b.advisor_user_id
                WHERE b.member_user_id = ? AND b.status = 'active'
                ORDER BY b.created_at DESC, b.id DESC LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            task_rows = connection.execute(
                """
                SELECT t.id, t.title, t.scheduled_at, t.status,
                       COALESCE(p.display_name, u.username) AS advisor_name
                FROM visit_tasks t
                LEFT JOIN users u ON u.id = t.advisor_user_id
                LEFT JOIN advisor_profiles p ON p.user_id = t.advisor_user_id
                WHERE t.member_user_id = ? AND t.status IN ('pending', 'in_progress')
                ORDER BY CASE WHEN t.scheduled_at IS NULL THEN 1 ELSE 0 END,
                         t.scheduled_at, t.id
                LIMIT 10
                """,
                (user_id,),
            ).fetchall()
            visit_rows = connection.execute(
                """
                SELECT r.id, r.task_id, r.visited_at, r.summary, r.details_json,
                       COALESCE(p.display_name, u.username) AS advisor_name
                FROM visit_records r
                LEFT JOIN users u ON u.id = r.advisor_user_id
                LEFT JOIN advisor_profiles p ON p.user_id = r.advisor_user_id
                WHERE r.member_user_id = ?
                ORDER BY r.visited_at DESC, r.id DESC
                LIMIT 20
                """,
                (user_id,),
            ).fetchall()
            completed_visits = int(
                connection.execute(
                    "SELECT COUNT(*) FROM visit_records WHERE member_user_id = ?",
                    (user_id,),
                ).fetchone()[0]
            )
        return {
            "user_id": user_id,
            "membership": None if membership is None else self._membership_from_row(membership),
            "advisor": None if advisor is None else dict(advisor),
            "pending_visit_tasks": [dict(row) for row in task_rows],
            "next_visit": None if not task_rows else dict(task_rows[0]),
            "visit_records": [
                {
                    **dict(row),
                    "details": _json_object(row["details_json"]),
                }
                for row in visit_rows
            ],
            "completed_visit_count": completed_visits,
        }

    def _require_user(self, user_id: int) -> int:
        value = self._positive_integer(user_id, "用户编号")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT role_code, account_status FROM users WHERE id = ?", (value,)
            ).fetchone()
        if row is None:
            raise HealthValidationError("用户不存在")
        if str(row["role_code"]) != "member":
            raise HealthValidationError("仅会员账号可以使用个人生活记录功能")
        if str(row["account_status"]) != "active":
            raise HealthValidationError("账号已停用，不能读写个人生活记录")
        return value

    @staticmethod
    def _positive_integer(value: Any, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise HealthValidationError(f"{label}无效")
        return value

    @staticmethod
    def _validate_category(category: str) -> str:
        value = HealthService._clean_required_text(category, "记录分类", maximum=50)
        if value not in LIFE_RECORD_CATEGORIES:
            raise HealthValidationError("记录分类无效")
        return value

    @staticmethod
    def _validate_limit(limit: int) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise HealthValidationError("读取数量必须在 1 到 10000 之间")
        return limit

    @staticmethod
    def _parse_date_input(value: date | str, label: str) -> date:
        if isinstance(value, datetime):
            parsed = as_beijing(value).date()
        elif isinstance(value, date):
            parsed = value
        elif isinstance(value, str):
            try:
                parsed = date.fromisoformat(value.strip())
            except ValueError as error:
                raise HealthValidationError(f"{label}必须使用 YYYY-MM-DD 格式") from error
        else:
            raise HealthValidationError(f"{label}格式无效")
        if parsed < MIN_RECORD_DATE:
            raise HealthValidationError(f"{label}不能早于 {MIN_RECORD_DATE.isoformat()}")
        return parsed

    @classmethod
    def _statistics_date(cls, value: date | datetime | str | None) -> date:
        if value is None:
            return beijing_today()
        if isinstance(value, datetime):
            return as_beijing(value).date()
        return cls._parse_date_input(value, "统计截止日期")

    @staticmethod
    def _beijing_day_start(value: date) -> str:
        return timestamp_to_db(datetime.combine(value, time.min, tzinfo=BEIJING_TIMEZONE))

    @staticmethod
    def _average(values: list[float]) -> float | None:
        return None if not values else round(sum(values) / len(values), 2)

    @classmethod
    def _validate_record_details(
        cls, category: str, details: Mapping[str, Any] | None
    ) -> dict[str, Any] | None:
        if details is None:
            return None
        if not isinstance(details, Mapping):
            raise HealthValidationError("记录详情必须是键值对象")
        normalized = dict(details)
        if len(normalized) > 50:
            raise HealthValidationError("记录详情字段不能超过 50 个")
        numeric_rules: dict[str, dict[str, tuple[float, float, str]]] = {
            "diet": {"calories_kcal": (0, 20_000, "热量")},
            "water": {"amount_ml": (1, 10_000, "饮水量")},
            "activity": {
                "duration_minutes": (0, 1_440, "活动时长"),
                "steps": (0, 200_000, "步数"),
            },
            "sleep": {
                "duration_hours": (0, 24, "睡眠时长"),
                "quality": (1, 5, "睡眠质量"),
            },
            "environment": {
                "temperature_c": (-80, 80, "环境温度"),
                "humidity_percent": (0, 100, "环境湿度"),
            },
        }
        for field, (minimum, maximum, label) in numeric_rules[category].items():
            if field not in normalized or normalized[field] in (None, ""):
                normalized.pop(field, None)
                continue
            value = normalized[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise HealthValidationError(f"{label}必须是数字")
            numeric = float(value)
            if not minimum <= numeric <= maximum:
                raise HealthValidationError(f"{label}必须在 {minimum:g} 到 {maximum:g} 之间")
            if field in {"amount_ml", "duration_minutes", "steps", "quality", "calories_kcal"}:
                if not numeric.is_integer():
                    raise HealthValidationError(f"{label}必须是整数")
                normalized[field] = int(numeric)
            else:
                normalized[field] = numeric
        return normalized

    @classmethod
    def _validate_reminder(cls, title: str, reminder_type: str) -> str:
        reminder_type_value = cls._clean_required_text(reminder_type, "提醒类型", maximum=50)
        if reminder_type_value not in REMINDER_TYPES:
            raise HealthValidationError(
                "提醒类型无效；请选择饮水、饮食、睡眠、活动、环境、回访、会员或自定义"
            )
        searchable = f"{title} {reminder_type_value}".casefold().replace(" ", "")
        if any(term in searchable for term in MEDICATION_REMINDER_TERMS):
            raise HealthValidationError("本应用不提供服药或药物提醒，请改为一般生活提醒")
        return reminder_type_value

    @classmethod
    def _reminder_datetime(cls, value: datetime | str) -> datetime:
        parsed = cls._aware_datetime(value, "提醒时间")
        local_day = as_beijing(parsed).date()
        if local_day < MIN_RECORD_DATE:
            raise HealthValidationError(f"提醒时间不能早于 {MIN_RECORD_DATE.isoformat()}")
        if local_day > beijing_today() + timedelta(days=366 * 20):
            raise HealthValidationError("提醒时间不能晚于 20 年后")
        return parsed

    @staticmethod
    def _aware_datetime(value: datetime | str, label: str) -> datetime:
        parsed = HealthService._parse_datetime_input(value, label)
        return as_beijing(parsed).astimezone(UTC)

    @classmethod
    def _normalize_local_timestamp(cls, value: datetime | str) -> tuple[str, str, int]:
        parsed = cls._parse_datetime_input(value, "发生时间")
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=BEIJING_TIMEZONE)
        offset_seconds = parsed.utcoffset().total_seconds()
        if offset_seconds % 60:
            raise HealthValidationError("时区偏移必须精确到分钟")
        offset_minutes = int(offset_seconds // 60)
        if not -840 <= offset_minutes <= 840:
            raise HealthValidationError("时区偏移超出支持范围")
        local_day = as_beijing(parsed).date()
        if local_day < MIN_RECORD_DATE:
            raise HealthValidationError(f"发生时间不能早于 {MIN_RECORD_DATE.isoformat()}")
        if local_day > beijing_today() + timedelta(days=1):
            raise HealthValidationError("发生时间不能晚于明天")
        return timestamp_to_db(parsed), parsed.date().isoformat(), offset_minutes

    @staticmethod
    def _parse_datetime_input(value: datetime | str, label: str) -> datetime:
        if isinstance(value, datetime):
            return value
        if not isinstance(value, str):
            raise HealthValidationError(f"{label}格式无效")
        cleaned = value.strip()
        if not cleaned:
            raise HealthValidationError(f"{label}不能为空")
        normalized = cleaned[:-1] + "+00:00" if cleaned.endswith(("Z", "z")) else cleaned
        try:
            return datetime.fromisoformat(normalized)
        except ValueError as error:
            raise HealthValidationError(f"{label}必须是有效的 ISO 8601 日期时间") from error

    @staticmethod
    def _clean_required_text(value: Any, label: str, *, maximum: int) -> str:
        if not isinstance(value, str):
            raise HealthValidationError(f"{label}格式无效")
        cleaned = " ".join(value.split())
        if not cleaned:
            raise HealthValidationError(f"{label}不能为空")
        if len(cleaned) > maximum:
            raise HealthValidationError(f"{label}不能超过 {maximum} 个字符")
        return cleaned

    @staticmethod
    def _mapping_to_json(value: Mapping[str, Any] | None, label: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise HealthValidationError(f"{label}必须是键值对象")
        try:
            return json.dumps(
                dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError) as error:
            raise HealthValidationError(f"{label}包含无法保存的内容") from error

    @staticmethod
    def _validate_profile_value(field: str, value: Any) -> Any:
        if value is None or value == "":
            return None
        if field == "height_cm":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise HealthValidationError("身高必须是数字")
            numeric = float(value)
            if not 30 <= numeric <= 300:
                raise HealthValidationError("身高必须在 30 到 300 厘米之间")
            return numeric
        if field == "gender":
            if not isinstance(value, str) or value not in GENDERS:
                raise HealthValidationError("性别字段无效")
            return value
        if field == "reminder_frequency":
            if not isinstance(value, str) or value not in {"normal", "low", "off"}:
                raise HealthValidationError("提醒频率字段无效")
            return value
        if field == "birth_date":
            if not isinstance(value, str):
                raise HealthValidationError("出生日期格式无效")
            try:
                parsed = date.fromisoformat(value)
            except ValueError as error:
                raise HealthValidationError("出生日期必须使用 YYYY-MM-DD 格式") from error
            if parsed > beijing_today():
                raise HealthValidationError("出生日期不能晚于今天")
            return parsed.isoformat()
        return HealthService._clean_required_text(
            value,
            {
                "display_name": "称呼",
                "phone": "联系电话",
                "living_situation": "居住情况",
                "emergency_contact_name": "紧急联系人姓名",
                "emergency_contact_phone": "紧急联系人电话",
                "ai_preferred_name": "AI 称呼",
                "health_goals": "健康目标",
                "dietary_preferences": "饮食偏好",
                "medical_notes": "健康备注",
            }[field],
            maximum=MAX_PROFILE_TEXT_LENGTH,
        )

    @staticmethod
    def _life_record_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "user_id": int(row["user_id"]),
            "category": str(row["category"]),
            "occurred_at": timestamp_from_db(row["occurred_at"]),
            "local_date": str(row["local_date"]),
            "timezone_offset_minutes": int(row["timezone_offset_minutes"]),
            "content": str(row["content"]),
            "details": _json_object(row["details_json"]),
            "source": str(row["source"]),
            "created_at": timestamp_from_db(row["created_at"]),
            "updated_at": timestamp_from_db(row["updated_at"]),
        }

    @staticmethod
    def _reminder_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "user_id": int(row["user_id"]),
            "title": str(row["title"]),
            "reminder_type": str(row["reminder_type"]),
            "scheduled_at": timestamp_from_db(row["scheduled_at"]),
            "status": str(row["status"]),
            "enabled": str(row["status"]) == "active",
            "created_at": timestamp_from_db(row["created_at"]),
            "updated_at": timestamp_from_db(row["updated_at"]),
        }

    @staticmethod
    def _membership_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "plan_code": str(row["plan_code"]),
            "status": str(row["status"]),
            "starts_at": timestamp_from_db(row["starts_at"]),
            "ends_at": timestamp_from_db(row["ends_at"]),
            "benefits": _json_object(row["benefits_json"]),
        }

    @staticmethod
    def _add_audit_log(
        connection: sqlite3.Connection,
        actor_user_id: int,
        action: str,
        entity_type: str,
        entity_id: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_logs (
                actor_user_id, action, entity_type, entity_id, details_json, created_at
            ) VALUES (?, ?, ?, ?, NULL, ?)
            """,
            (actor_user_id, action, entity_type, entity_id, timestamp_to_db(utc_now())),
        )


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _json_object(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    parsed = json.loads(str(value))
    return parsed if isinstance(parsed, dict) else None


__all__ = [
    "GENDERS",
    "HealthRecordNotFoundError",
    "HealthService",
    "HealthServiceError",
    "HealthValidationError",
    "LIFE_RECORD_CATEGORIES",
    "LIFE_RECORD_SOURCES",
    "PROFILE_FIELDS",
    "REMINDER_TYPES",
    "ReminderNotFoundError",
]
