from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from ..data.database import Database, User, timestamp_from_db, timestamp_to_db, utc_now
from ..time_utils import as_beijing
from .auth import AuthService

ROLE_MEMBER = "member"
ROLE_ADVISOR = "advisor"
ROLE_OPERATOR = "operator"
ACCOUNT_ACTIVE = "active"
VISIT_TASK_STATUSES = frozenset({"pending", "in_progress", "completed", "cancelled"})
MEMBERSHIP_STATUSES = frozenset({"pending", "active", "expired", "cancelled"})


class ServiceManagementError(RuntimeError):
    """Base error for the role-scoped service-management layer."""


class PermissionDeniedError(ServiceManagementError):
    """Raised when an actor is inactive or lacks access to a requested resource."""

    def __init__(self) -> None:
        super().__init__("无权执行此操作")


class ManagementValidationError(ServiceManagementError, ValueError):
    """Raised when a management form contains invalid input."""


class ManagementRecordNotFoundError(ServiceManagementError, LookupError):
    """Raised when an operator requests a resource that does not exist."""


class InvalidVisitStateError(ServiceManagementError):
    """Raised when a visit task cannot make the requested state transition."""


class ServiceManagementService:
    """Strict backend boundary for operator, advisor and member service workflows.

    UI visibility is never treated as authorization. Every method reloads the
    actor and target relationships inside its own connection or transaction.
    """

    def __init__(self, database: Database | None = None) -> None:
        self.database = database or Database()
        self.database.initialize()

    def get_dashboard(self, actor_user_id: int) -> dict[str, Any]:
        """Return compact role-scoped counts and upcoming work for a workspace home page."""

        with self.database.connect() as connection:
            actor = self._active_actor(connection, actor_user_id)
            actor_id = int(actor["id"])
            role = str(actor["role_code"])
            if role == ROLE_OPERATOR:
                counts = {
                    "member_count": self._scalar_count(
                        connection, "SELECT COUNT(*) FROM users WHERE role_code = 'member'"
                    ),
                    "advisor_count": self._scalar_count(
                        connection, "SELECT COUNT(*) FROM users WHERE role_code = 'advisor'"
                    ),
                    "active_membership_count": self._scalar_count(
                        connection,
                        "SELECT COUNT(DISTINCT user_id) FROM memberships WHERE status = 'active'",
                    ),
                    "pending_visit_count": self._scalar_count(
                        connection,
                        "SELECT COUNT(*) FROM visit_tasks "
                        "WHERE status IN ('pending', 'in_progress')",
                    ),
                    "completed_visit_count": self._scalar_count(
                        connection, "SELECT COUNT(*) FROM visit_records"
                    ),
                }
            elif role == ROLE_ADVISOR:
                counts = {
                    "active_member_count": self._scalar_count(
                        connection,
                        "SELECT COUNT(*) FROM advisor_bindings "
                        "WHERE advisor_user_id = ? AND status = 'active'",
                        (actor_id,),
                    ),
                    "pending_visit_count": self._scalar_count(
                        connection,
                        "SELECT COUNT(*) FROM visit_tasks t "
                        "WHERE t.advisor_user_id = ? "
                        "AND t.status IN ('pending', 'in_progress') "
                        "AND EXISTS (SELECT 1 FROM advisor_bindings b "
                        "WHERE b.member_user_id = t.member_user_id "
                        "AND b.advisor_user_id = ? AND b.status = 'active')",
                        (actor_id, actor_id),
                    ),
                    "completed_visit_count": self._scalar_count(
                        connection,
                        "SELECT COUNT(*) FROM visit_records WHERE advisor_user_id = ?",
                        (actor_id,),
                    ),
                }
            else:
                counts = {
                    "pending_visit_count": self._scalar_count(
                        connection,
                        "SELECT COUNT(*) FROM visit_tasks WHERE member_user_id = ? "
                        "AND status IN ('pending', 'in_progress')",
                        (actor_id,),
                    ),
                    "completed_visit_count": self._scalar_count(
                        connection,
                        "SELECT COUNT(*) FROM visit_records WHERE member_user_id = ?",
                        (actor_id,),
                    ),
                }
        return {
            "actor_user_id": actor_id,
            "role_code": role,
            **counts,
        }

    def create_advisor(
        self,
        actor_user_id: int,
        username: str,
        password: str,
        *,
        display_name: str,
        organization: str | None = None,
        specialty: str | None = None,
        bio: str | None = None,
    ) -> User:
        """UI facade: create an advisor account, then its validated display profile."""

        display = self._required_text(display_name, "顾问姓名", 100)
        organization_value = self._optional_text(organization, "机构", 200)
        specialty_value = self._optional_text(specialty, "专长", 200)
        bio_value = self._optional_text(bio, "顾问简介", 4_000)
        user = AuthService(self.database).create_staff(
            actor_user_id, username, password, ROLE_ADVISOR
        )
        self.save_advisor_profile(
            actor_user_id,
            user.id,
            display_name=display,
            organization=organization_value,
            specialty=specialty_value,
            bio=bio_value,
        )
        return user

    def list_members(self, actor_user_id: int) -> list[dict[str, Any]]:
        """Return all permitted members: all for operators, bound for advisors, self for members."""

        with self.database.connect() as connection:
            actor = self._active_actor(connection, actor_user_id)
            role = str(actor["role_code"])
            parameters: tuple[object, ...]
            access_clause: str
            if role == ROLE_OPERATOR:
                access_clause = ""
                parameters = ()
            elif role == ROLE_ADVISOR:
                access_clause = (
                    "AND EXISTS (SELECT 1 FROM advisor_bindings access_binding "
                    "WHERE access_binding.member_user_id = u.id "
                    "AND access_binding.advisor_user_id = ? "
                    "AND access_binding.status = 'active')"
                )
                parameters = (int(actor["id"]),)
            else:
                access_clause = "AND u.id = ?"
                parameters = (int(actor["id"]),)

            rows = connection.execute(
                f"""
                SELECT u.id, u.username, u.account_status,
                       p.display_name, p.phone, p.living_situation,
                       m.id AS membership_id, m.plan_code, m.status AS membership_status,
                       m.starts_at AS membership_starts_at, m.ends_at AS membership_ends_at,
                       m.benefits_json AS membership_benefits_json,
                       b.advisor_user_id,
                       COALESCE(ap.display_name, au.username) AS advisor_name,
                       (
                           SELECT vt.scheduled_at FROM visit_tasks vt
                           WHERE vt.member_user_id = u.id
                             AND vt.status IN ('pending', 'in_progress')
                           ORDER BY CASE WHEN vt.scheduled_at IS NULL THEN 1 ELSE 0 END,
                                    vt.scheduled_at, vt.id
                           LIMIT 1
                       ) AS next_visit_at
                FROM users u
                LEFT JOIN member_profiles p ON p.user_id = u.id
                LEFT JOIN memberships m ON m.id = (
                    SELECT selected_membership.id FROM memberships selected_membership
                    WHERE selected_membership.user_id = u.id
                    ORDER BY CASE selected_membership.status WHEN 'active' THEN 0 ELSE 1 END,
                             selected_membership.starts_at DESC, selected_membership.id DESC
                    LIMIT 1
                )
                LEFT JOIN advisor_bindings b
                  ON b.member_user_id = u.id AND b.status = 'active'
                LEFT JOIN users au ON au.id = b.advisor_user_id
                LEFT JOIN advisor_profiles ap ON ap.user_id = b.advisor_user_id
                WHERE u.role_code = 'member' {access_clause}
                ORDER BY COALESCE(p.display_name, u.username), u.id
                """,
                parameters,
            ).fetchall()
        return [self._member_summary(row) for row in rows]

    def get_member_overview(
        self, actor_user_id: int, member_user_id: int, *, recent_record_limit: int = 20
    ) -> dict[str, Any]:
        """Read profile and recent life facts after checking role/binding access."""

        limit = self._validate_limit(recent_record_limit)
        with self.database.connect() as connection:
            actor = self._active_actor(connection, actor_user_id)
            member_id = self._accessible_member_id(connection, actor, member_user_id)
            user = connection.execute(
                """
                SELECT u.id, u.username, u.account_status,
                       p.display_name, p.birth_date, p.gender, p.phone,
                       p.living_situation, p.emergency_contact_name,
                       p.emergency_contact_phone, p.ai_preferred_name,
                       p.reminder_frequency, p.height_cm, p.health_goals,
                       p.dietary_preferences, p.medical_notes
                FROM users u
                LEFT JOIN member_profiles p ON p.user_id = u.id
                WHERE u.id = ? AND u.role_code = 'member'
                """,
                (member_id,),
            ).fetchone()
            if user is None:  # pragma: no cover - guarded by _accessible_member_id
                raise ManagementRecordNotFoundError("会员不存在")
            records = connection.execute(
                """
                SELECT id, category, occurred_at, local_date,
                       timezone_offset_minutes, content, details_json, source
                FROM life_records
                WHERE user_id = ?
                ORDER BY occurred_at DESC, id DESC LIMIT ?
                """,
                (member_id, limit),
            ).fetchall()
            facts = connection.execute(
                """
                SELECT id, fact_key, value_json, source, status, effective_at,
                       created_at, updated_at
                FROM profile_facts
                WHERE user_id = ? AND status = 'confirmed'
                ORDER BY fact_key, id DESC
                """,
                (member_id,),
            ).fetchall()
        return {
            "profile": dict(user),
            "confirmed_facts": [self._fact_from_row(row) for row in facts],
            "recent_life_records": [self._life_record_from_row(row) for row in records],
        }

    def list_advisors(self, actor_user_id: int) -> list[dict[str, Any]]:
        """List advisor accounts and active workload; operator only."""

        with self.database.connect() as connection:
            self._require_operator(connection, actor_user_id)
            rows = connection.execute(
                """
                SELECT u.id, u.username, u.account_status,
                       p.display_name, p.organization, p.specialty, p.bio,
                       COUNT(b.id) AS active_member_count
                FROM users u
                LEFT JOIN advisor_profiles p ON p.user_id = u.id
                LEFT JOIN advisor_bindings b
                  ON b.advisor_user_id = u.id AND b.status = 'active'
                WHERE u.role_code = 'advisor'
                GROUP BY u.id, u.username, u.account_status, p.display_name,
                         p.organization, p.specialty, p.bio
                ORDER BY COALESCE(p.display_name, u.username), u.id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_advisor_work_statistics(self, actor_user_id: int) -> dict[str, Any]:
        """Return auditable advisor workload facts to an active operator only.

        The result intentionally has no performance score, ranking, health conclusion,
        or medical evaluation. Visit minutes are counted only when a completed visit
        record explicitly contains a valid ``duration_minutes`` value.
        """

        with self.database.connect() as connection:
            self._require_operator(connection, actor_user_id)
            rows = connection.execute(
                """
                SELECT u.id AS advisor_user_id, u.username, u.account_status,
                       COALESCE(p.display_name, u.username) AS display_name,
                       p.organization, p.specialty,
                       (
                           SELECT COUNT(DISTINCT b.member_user_id)
                           FROM advisor_bindings b
                           WHERE b.advisor_user_id = u.id AND b.status = 'active'
                       ) AS active_member_count,
                       (
                           SELECT COUNT(*) FROM visit_tasks pending_task
                           WHERE pending_task.advisor_user_id = u.id
                             AND pending_task.status IN ('pending', 'in_progress')
                       ) AS pending_task_count,
                       (
                           SELECT COUNT(*) FROM visit_tasks completed_task
                           WHERE completed_task.advisor_user_id = u.id
                             AND completed_task.status = 'completed'
                       ) AS completed_task_count,
                       (
                           SELECT COUNT(*) FROM visit_records completed_visit
                           WHERE completed_visit.advisor_user_id = u.id
                       ) AS completed_visit_record_count
                FROM users u
                LEFT JOIN advisor_profiles p ON p.user_id = u.id
                WHERE u.role_code = 'advisor'
                ORDER BY COALESCE(p.display_name, u.username), u.id
                """
            ).fetchall()
            duration_rows = connection.execute(
                """
                SELECT advisor_user_id, details_json
                FROM visit_records
                WHERE advisor_user_id IS NOT NULL
                """
            ).fetchall()

        duration_totals: dict[int, int] = {}
        duration_counts: dict[int, int] = {}
        for row in duration_rows:
            advisor_id = int(row["advisor_user_id"])
            minutes = self._recorded_visit_minutes(row["details_json"])
            if minutes is None:
                continue
            duration_totals[advisor_id] = duration_totals.get(advisor_id, 0) + minutes
            duration_counts[advisor_id] = duration_counts.get(advisor_id, 0) + 1

        advisors: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            advisor_id = int(item["advisor_user_id"])
            visit_count = int(item["completed_visit_record_count"])
            with_duration = duration_counts.get(advisor_id, 0)
            item.update(
                {
                    "active_member_count": int(item["active_member_count"]),
                    "pending_task_count": int(item["pending_task_count"]),
                    "completed_task_count": int(item["completed_task_count"]),
                    "completed_visit_record_count": visit_count,
                    "recorded_visit_minutes": duration_totals.get(advisor_id, 0),
                    "records_with_duration_count": with_duration,
                    "records_without_duration_count": visit_count - with_duration,
                }
            )
            advisors.append(item)
        return {
            "advisors": advisors,
            "notice": (
                "仅展示数据库中已记录的顾问工作事实；未生成自动绩效分数、排名、"
                "医疗评价或健康结论。总分钟数只累计明确填写服务时长的上门记录。"
            ),
        }

    def save_advisor_profile(
        self,
        actor_user_id: int,
        advisor_user_id: int,
        *,
        display_name: str,
        organization: str | None = None,
        specialty: str | None = None,
        bio: str | None = None,
    ) -> None:
        """Create or update an advisor profile; operator only."""

        display = self._required_text(display_name, "顾问姓名", 100)
        organization_value = self._optional_text(organization, "机构", 200)
        specialty_value = self._optional_text(specialty, "专长", 200)
        bio_value = self._optional_text(bio, "顾问简介", 4_000)
        with self.database.transaction() as connection:
            operator = self._require_operator(connection, actor_user_id)
            advisor_id = self._require_target_role(
                connection, advisor_user_id, ROLE_ADVISOR, require_active=False
            )
            now = timestamp_to_db(utc_now())
            connection.execute(
                """
                INSERT INTO advisor_profiles (
                    user_id, display_name, organization, specialty, bio,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    organization = excluded.organization,
                    specialty = excluded.specialty,
                    bio = excluded.bio,
                    updated_at = excluded.updated_at
                """,
                (
                    advisor_id,
                    display,
                    organization_value,
                    specialty_value,
                    bio_value,
                    now,
                    now,
                ),
            )
            self._audit(
                connection,
                int(operator["id"]),
                "advisor_profile.saved",
                "advisor_profile",
                advisor_id,
            )

    def save_membership(
        self,
        actor_user_id: int,
        member_user_id: int,
        *,
        plan_code: str,
        starts_at: datetime | str,
        ends_at: datetime | str | None = None,
        status: str = "active",
        benefits: Mapping[str, Any] | None = None,
        membership_id: int | None = None,
    ) -> int:
        """Create a membership or update one explicitly identified membership."""

        plan = self._required_text(plan_code, "会员方案", 100)
        status_value = self._membership_status(status)
        starts = self._datetime_to_db(starts_at, "会员开始时间")
        ends = None if ends_at is None else self._datetime_to_db(ends_at, "会员结束时间")
        if ends is not None and ends <= starts:
            raise ManagementValidationError("会员结束时间必须晚于开始时间")
        benefits_json = self._mapping_json(benefits, "会员权益")
        with self.database.transaction() as connection:
            operator = self._require_operator(connection, actor_user_id)
            member_id = self._require_target_role(connection, member_user_id, ROLE_MEMBER)
            now = timestamp_to_db(utc_now())
            other_active_memberships = 0
            if membership_id is None:
                cursor = connection.execute(
                    """
                    INSERT INTO memberships (
                        user_id, plan_code, status, starts_at, ends_at,
                        benefits_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (member_id, plan, status_value, starts, ends, benefits_json, now, now),
                )
                saved_id = int(cursor.lastrowid)
                action = "membership.created"
            else:
                saved_id = self._positive_id(membership_id, "会员记录编号")
                cursor = connection.execute(
                    """
                    UPDATE memberships
                    SET plan_code = ?, status = ?, starts_at = ?, ends_at = ?,
                        benefits_json = ?, updated_at = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (plan, status_value, starts, ends, benefits_json, now, saved_id, member_id),
                )
                if cursor.rowcount != 1:
                    raise ManagementRecordNotFoundError("会员记录不存在")
                action = "membership.updated"
            if status_value == "active":
                other_active_memberships = connection.execute(
                    """
                    UPDATE memberships
                    SET status = 'expired', updated_at = ?
                    WHERE user_id = ? AND status = 'active' AND id <> ?
                    """,
                    (now, member_id, saved_id),
                ).rowcount
            self._audit(
                connection,
                int(operator["id"]),
                action,
                "membership",
                saved_id,
                {"expired_previous_active_count": other_active_memberships},
            )
        return saved_id

    def bind_advisor(
        self,
        actor_user_id: int,
        member_user_id: int,
        advisor_user_id: int,
        *,
        started_at: datetime | str | None = None,
    ) -> int:
        """Bind or atomically rebind one member to one active advisor."""

        started = (
            timestamp_to_db(utc_now())
            if started_at is None
            else self._datetime_to_db(started_at, "服务开始时间")
        )
        with self.database.transaction() as connection:
            operator = self._require_operator(connection, actor_user_id)
            member_id = self._require_target_role(connection, member_user_id, ROLE_MEMBER)
            advisor_id = self._require_target_role(connection, advisor_user_id, ROLE_ADVISOR)
            current = connection.execute(
                """
                SELECT id, advisor_user_id FROM advisor_bindings
                WHERE member_user_id = ? AND status = 'active'
                """,
                (member_id,),
            ).fetchone()
            now = timestamp_to_db(utc_now())
            if current is not None and int(current["advisor_user_id"]) == advisor_id:
                binding_id = int(current["id"])
                connection.execute(
                    "UPDATE advisor_bindings SET started_at = ?, updated_at = ? WHERE id = ?",
                    (started, now, binding_id),
                )
                action = "advisor_binding.updated"
            else:
                reassigned_task_count = 0
                if current is not None:
                    connection.execute(
                        """
                        UPDATE advisor_bindings
                        SET status = 'ended', ended_at = ?, updated_at = ?
                        WHERE id = ? AND status = 'active'
                        """,
                        (now, now, int(current["id"])),
                    )
                reassigned_task_count = connection.execute(
                    """
                    UPDATE visit_tasks
                    SET advisor_user_id = ?, updated_at = ?
                    WHERE member_user_id = ?
                      AND status IN ('pending', 'in_progress')
                      AND (advisor_user_id IS NULL OR advisor_user_id <> ?)
                    """,
                    (advisor_id, now, member_id, advisor_id),
                ).rowcount
                cursor = connection.execute(
                    """
                    INSERT INTO advisor_bindings (
                        member_user_id, advisor_user_id, status, started_at,
                        ended_at, created_at, updated_at
                    ) VALUES (?, ?, 'active', ?, NULL, ?, ?)
                    """,
                    (member_id, advisor_id, started, now, now),
                )
                binding_id = int(cursor.lastrowid)
                action = (
                    "advisor_binding.created" if current is None else "advisor_binding.replaced"
                )
            self._audit(
                connection,
                int(operator["id"]),
                action,
                "advisor_binding",
                binding_id,
                {
                    "member_user_id": member_id,
                    "advisor_user_id": advisor_id,
                    "reassigned_open_task_count": (
                        reassigned_task_count if action != "advisor_binding.updated" else 0
                    ),
                },
            )
        return binding_id

    def create_visit_task(
        self,
        actor_user_id: int,
        member_user_id: int,
        *,
        title: str,
        scheduled_at: datetime | str | None,
        advisor_user_id: int | None = None,
        notes: str | None = None,
    ) -> int:
        """Create a task as operator or as the member's active advisor."""

        title_value = self._required_text(title, "上门任务标题", 200)
        scheduled = (
            None if scheduled_at is None else self._datetime_to_db(scheduled_at, "计划上门时间")
        )
        notes_value = self._optional_text(notes, "上门任务备注", 10_000)
        with self.database.transaction() as connection:
            actor = self._active_actor(connection, actor_user_id)
            actor_id = int(actor["id"])
            role = str(actor["role_code"])
            if role == ROLE_OPERATOR:
                member_id = self._require_target_role(connection, member_user_id, ROLE_MEMBER)
                assigned_advisor_id = (
                    self._active_binding_advisor(connection, member_id)
                    if advisor_user_id is None
                    else self._require_target_role(connection, advisor_user_id, ROLE_ADVISOR)
                )
                if assigned_advisor_id is not None:
                    self._require_active_binding(connection, member_id, assigned_advisor_id)
            elif role == ROLE_ADVISOR:
                member_id = self._accessible_member_id(connection, actor, member_user_id)
                if (
                    advisor_user_id is not None
                    and self._positive_id(advisor_user_id, "顾问编号") != actor_id
                ):
                    raise PermissionDeniedError
                assigned_advisor_id = actor_id
            else:
                raise PermissionDeniedError

            now = timestamp_to_db(utc_now())
            cursor = connection.execute(
                """
                INSERT INTO visit_tasks (
                    member_user_id, advisor_user_id, title, scheduled_at,
                    status, notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    member_id,
                    assigned_advisor_id,
                    title_value,
                    scheduled,
                    notes_value,
                    now,
                    now,
                ),
            )
            task_id = int(cursor.lastrowid)
            self._audit(
                connection,
                actor_id,
                "visit_task.created",
                "visit_task",
                task_id,
                {"member_user_id": member_id, "advisor_user_id": assigned_advisor_id},
            )
        return task_id

    def start_visit_task(self, actor_user_id: int, task_id: int) -> None:
        """Move a pending task to in-progress after rechecking ownership."""

        with self.database.transaction() as connection:
            actor = self._active_actor(connection, actor_user_id)
            task = self._accessible_task(connection, actor, task_id, write=True)
            if str(task["status"]) != "pending":
                raise InvalidVisitStateError("只有待上门任务可以开始")
            now = timestamp_to_db(utc_now())
            connection.execute(
                "UPDATE visit_tasks SET status = 'in_progress', updated_at = ? WHERE id = ?",
                (now, int(task["id"])),
            )
            self._audit(
                connection,
                int(actor["id"]),
                "visit_task.started",
                "visit_task",
                int(task["id"]),
            )

    def complete_visit_task(
        self,
        actor_user_id: int,
        task_id: int,
        *,
        summary: str,
        visited_at: datetime | str | None = None,
        details: Mapping[str, Any] | None = None,
        next_visit_at: datetime | str | None = None,
        next_visit_title: str = "定期复访",
        create_member_reminder: bool = True,
    ) -> dict[str, int | None]:
        """Complete one visit and optionally schedule its next visit atomically."""

        summary_value = self._required_text(summary, "上门总结", 20_000)
        visited = (
            timestamp_to_db(utc_now())
            if visited_at is None
            else self._datetime_to_db(visited_at, "实际上门时间")
        )
        details_json = self._mapping_json(details, "上门详情")
        next_scheduled = (
            None if next_visit_at is None else self._datetime_to_db(next_visit_at, "下次上门时间")
        )
        if next_scheduled is not None and next_scheduled <= visited:
            raise ManagementValidationError("下次上门时间必须晚于本次上门时间")
        if not isinstance(create_member_reminder, bool):
            raise ManagementValidationError("提醒开关格式无效")
        next_title = self._required_text(next_visit_title, "下次上门标题", 200)

        with self.database.transaction() as connection:
            actor = self._active_actor(connection, actor_user_id)
            task = self._accessible_task(connection, actor, task_id, write=True)
            if str(task["status"]) not in {"pending", "in_progress"}:
                raise InvalidVisitStateError("该上门任务已经结束，不能重复完成")
            task_id_value = int(task["id"])
            member_id = int(task["member_user_id"])
            assigned_advisor_id = (
                None if task["advisor_user_id"] is None else int(task["advisor_user_id"])
            )
            now = timestamp_to_db(utc_now())
            visit_cursor = connection.execute(
                """
                INSERT INTO visit_records (
                    task_id, member_user_id, advisor_user_id, visited_at,
                    summary, details_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id_value,
                    member_id,
                    assigned_advisor_id,
                    visited,
                    summary_value,
                    details_json,
                    now,
                    now,
                ),
            )
            visit_record_id = int(visit_cursor.lastrowid)
            updated = connection.execute(
                """
                UPDATE visit_tasks
                SET status = 'completed', updated_at = ?
                WHERE id = ? AND status IN ('pending', 'in_progress')
                """,
                (now, task_id_value),
            )
            if updated.rowcount != 1:  # pragma: no cover - write transaction owns current state
                raise InvalidVisitStateError("上门任务状态已变化，请刷新后重试")

            membership = connection.execute(
                """
                SELECT id, benefits_json FROM memberships
                WHERE user_id = ? AND status = 'active'
                ORDER BY starts_at DESC, id DESC LIMIT 1
                """,
                (member_id,),
            ).fetchone()
            details_values = dict(details) if isinstance(details, Mapping) else {}
            if membership is not None and (
                details_values.get("uses_membership_benefit")
                or details_values.get("first_filing_completed")
            ):
                try:
                    benefits = (
                        {}
                        if membership["benefits_json"] is None
                        else json.loads(str(membership["benefits_json"]))
                    )
                except (TypeError, ValueError):
                    benefits = {}
                if not isinstance(benefits, dict):
                    benefits = {}
                if details_values.get("uses_membership_benefit"):
                    total_value = benefits.get("visit_total", benefits.get("annual_visits"))
                    used_key = "visit_used"
                    used_value = benefits.get(used_key, benefits.get("used_visits", 0))
                    try:
                        total = max(0, int(total_value))
                        used = max(0, int(used_value))
                    except (TypeError, ValueError):
                        total = None
                        used = 0
                    if total is not None:
                        if used >= total:
                            raise ManagementValidationError(
                                "会员本周期上门次数已用尽；请取消“计入会员权益”后再保存"
                            )
                        benefits[used_key] = used + 1
                if details_values.get("first_filing_completed"):
                    benefits["first_filing"] = True
                connection.execute(
                    "UPDATE memberships SET benefits_json = ?, updated_at = ? WHERE id = ?",
                    (
                        json.dumps(
                            benefits,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        now,
                        int(membership["id"]),
                    ),
                )
                self._audit(
                    connection,
                    int(actor["id"]),
                    "membership.benefits_used",
                    "membership",
                    int(membership["id"]),
                    {"visit_record_id": visit_record_id},
                )

            next_task_id: int | None = None
            reminder_id: int | None = None
            if next_scheduled is not None:
                next_advisor_id = self._active_binding_advisor(connection, member_id)
                next_cursor = connection.execute(
                    """
                    INSERT INTO visit_tasks (
                        member_user_id, advisor_user_id, title, scheduled_at,
                        status, notes, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'pending', NULL, ?, ?)
                    """,
                    (member_id, next_advisor_id, next_title, next_scheduled, now, now),
                )
                next_task_id = int(next_cursor.lastrowid)
                if create_member_reminder:
                    reminder_cursor = connection.execute(
                        """
                        INSERT INTO reminders (
                            user_id, title, reminder_type, scheduled_at,
                            status, created_at, updated_at
                        ) VALUES (?, ?, 'visit', ?, 'active', ?, ?)
                        """,
                        (member_id, f"上门服务：{next_title}", next_scheduled, now, now),
                    )
                    reminder_id = int(reminder_cursor.lastrowid)

            actor_id = int(actor["id"])
            self._audit(
                connection,
                actor_id,
                "visit_task.completed",
                "visit_task",
                task_id_value,
                {"visit_record_id": visit_record_id},
            )
            if next_task_id is not None:
                self._audit(
                    connection,
                    actor_id,
                    "visit_task.next_created",
                    "visit_task",
                    next_task_id,
                    {"previous_task_id": task_id_value, "reminder_id": reminder_id},
                )
        return {
            "task_id": task_id_value,
            "visit_record_id": visit_record_id,
            "next_task_id": next_task_id,
            "reminder_id": reminder_id,
        }

    def list_visit_tasks(
        self,
        actor_user_id: int,
        *,
        member_user_id: int | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """List tasks inside the actor's role and binding scope."""

        limit_value = self._validate_limit(limit)
        if status is not None and status not in VISIT_TASK_STATUSES:
            raise ManagementValidationError("上门任务状态无效")
        with self.database.connect() as connection:
            actor = self._active_actor(connection, actor_user_id)
            actor_id = int(actor["id"])
            role = str(actor["role_code"])
            clauses: list[str] = []
            parameters: list[object] = []
            if role == ROLE_ADVISOR:
                clauses.append("t.advisor_user_id = ?")
                clauses.append(
                    "EXISTS (SELECT 1 FROM advisor_bindings b "
                    "WHERE b.member_user_id = t.member_user_id "
                    "AND b.advisor_user_id = ? AND b.status = 'active')"
                )
                parameters.extend((actor_id, actor_id))
            elif role == ROLE_MEMBER:
                clauses.append("t.member_user_id = ?")
                parameters.append(actor_id)
            if member_user_id is not None:
                requested_member = self._accessible_member_id(connection, actor, member_user_id)
                clauses.append("t.member_user_id = ?")
                parameters.append(requested_member)
            if status is not None:
                clauses.append("t.status = ?")
                parameters.append(status)
            where = " AND ".join(clauses) if clauses else "1 = 1"
            parameters.append(limit_value)
            rows = connection.execute(
                f"""
                SELECT t.id, t.member_user_id, t.advisor_user_id, t.title,
                       t.scheduled_at, t.status, t.notes, t.created_at, t.updated_at,
                       COALESCE(mp.display_name, member.username) AS member_name,
                       COALESCE(ap.display_name, advisor.username) AS advisor_name
                FROM visit_tasks t
                JOIN users member ON member.id = t.member_user_id
                LEFT JOIN member_profiles mp ON mp.user_id = t.member_user_id
                LEFT JOIN users advisor ON advisor.id = t.advisor_user_id
                LEFT JOIN advisor_profiles ap ON ap.user_id = t.advisor_user_id
                WHERE {where}
                ORDER BY CASE t.status WHEN 'in_progress' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
                         CASE WHEN t.scheduled_at IS NULL THEN 1 ELSE 0 END,
                         t.scheduled_at, t.id
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [self._visit_task_from_row(row) for row in rows]

    def list_visit_records(
        self,
        actor_user_id: int,
        *,
        member_user_id: int | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """List visit history within operator, active advisor or self scope."""

        limit_value = self._validate_limit(limit)
        with self.database.connect() as connection:
            actor = self._active_actor(connection, actor_user_id)
            actor_id = int(actor["id"])
            role = str(actor["role_code"])
            clauses: list[str] = []
            parameters: list[object] = []
            if role == ROLE_ADVISOR:
                clauses.append("r.advisor_user_id = ?")
                parameters.append(actor_id)
            elif role == ROLE_MEMBER:
                clauses.append("r.member_user_id = ?")
                parameters.append(actor_id)
            if member_user_id is not None:
                requested_member = self._accessible_member_id(connection, actor, member_user_id)
                clauses.append("r.member_user_id = ?")
                parameters.append(requested_member)
            where = " AND ".join(clauses) if clauses else "1 = 1"
            parameters.append(limit_value)
            rows = connection.execute(
                f"""
                SELECT r.id, r.task_id, r.member_user_id, r.advisor_user_id,
                       r.visited_at, r.summary, r.details_json,
                       r.created_at, r.updated_at,
                       COALESCE(mp.display_name, member.username) AS member_name,
                       COALESCE(ap.display_name, advisor.username) AS advisor_name
                FROM visit_records r
                JOIN users member ON member.id = r.member_user_id
                LEFT JOIN member_profiles mp ON mp.user_id = r.member_user_id
                LEFT JOIN users advisor ON advisor.id = r.advisor_user_id
                LEFT JOIN advisor_profiles ap ON ap.user_id = r.advisor_user_id
                WHERE {where}
                ORDER BY r.visited_at DESC, r.id DESC LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [self._visit_record_from_row(row) for row in rows]

    def set_account_enabled(
        self, actor_user_id: int, target_user_id: int, *, enabled: bool
    ) -> None:
        """Enable or disable an account without deleting its service history."""

        if not isinstance(enabled, bool):
            raise ManagementValidationError("账号开关格式无效")
        with self.database.transaction() as connection:
            operator = self._require_operator(connection, actor_user_id)
            operator_id = int(operator["id"])
            target_id = self._positive_id(target_user_id, "目标用户编号")
            if operator_id == target_id and not enabled:
                raise ManagementValidationError("不能停用当前正在操作的运营账号")
            target = connection.execute(
                "SELECT id, role_code, account_status FROM users WHERE id = ?",
                (target_id,),
            ).fetchone()
            if target is None:
                raise ManagementRecordNotFoundError("目标账号不存在")
            if not enabled and str(target["role_code"]) == ROLE_ADVISOR:
                active_binding_count = self._scalar_count(
                    connection,
                    "SELECT COUNT(*) FROM advisor_bindings "
                    "WHERE advisor_user_id = ? AND status = 'active'",
                    (target_id,),
                )
                open_task_count = self._scalar_count(
                    connection,
                    "SELECT COUNT(*) FROM visit_tasks WHERE advisor_user_id = ? "
                    "AND status IN ('pending', 'in_progress')",
                    (target_id,),
                )
                if active_binding_count or open_task_count:
                    raise ManagementValidationError(
                        "该顾问仍有绑定会员或未完成任务，请先把会员换绑给其他顾问"
                    )
            status = "active" if enabled else "disabled"
            now = timestamp_to_db(utc_now())
            connection.execute(
                "UPDATE users SET account_status = ?, updated_at = ? WHERE id = ?",
                (status, now, target_id),
            )
            self._audit(
                connection,
                operator_id,
                "account.enabled" if enabled else "account.disabled",
                "user",
                target_id,
                {"role_code": str(target["role_code"])},
            )

    @staticmethod
    def _active_actor(connection: sqlite3.Connection, actor_user_id: int) -> sqlite3.Row:
        actor_id = ServiceManagementService._positive_id(actor_user_id, "操作者编号")
        row = connection.execute(
            """
            SELECT id, role_code, account_status FROM users
            WHERE id = ?
            """,
            (actor_id,),
        ).fetchone()
        if row is None or str(row["account_status"]) != ACCOUNT_ACTIVE:
            raise PermissionDeniedError
        return row

    @staticmethod
    def _scalar_count(
        connection: sqlite3.Connection,
        query: str,
        parameters: tuple[object, ...] = (),
    ) -> int:
        return int(connection.execute(query, parameters).fetchone()[0])

    @classmethod
    def _require_operator(cls, connection: sqlite3.Connection, actor_user_id: int) -> sqlite3.Row:
        actor = cls._active_actor(connection, actor_user_id)
        if str(actor["role_code"]) != ROLE_OPERATOR:
            raise PermissionDeniedError
        return actor

    @staticmethod
    def _require_target_role(
        connection: sqlite3.Connection,
        user_id: int,
        expected_role: str,
        *,
        require_active: bool = True,
    ) -> int:
        target_id = ServiceManagementService._positive_id(user_id, "目标用户编号")
        row = connection.execute(
            "SELECT role_code, account_status FROM users WHERE id = ?",
            (target_id,),
        ).fetchone()
        if row is None:
            raise ManagementRecordNotFoundError("目标账号不存在")
        if str(row["role_code"]) != expected_role:
            raise ManagementValidationError("目标账号角色不符合要求")
        if require_active and str(row["account_status"]) != ACCOUNT_ACTIVE:
            raise ManagementValidationError("目标账号已停用")
        return target_id

    @classmethod
    def _accessible_member_id(
        cls,
        connection: sqlite3.Connection,
        actor: sqlite3.Row,
        member_user_id: int,
    ) -> int:
        member_id = cls._positive_id(member_user_id, "会员编号")
        member = connection.execute(
            "SELECT id, role_code FROM users WHERE id = ?",
            (member_id,),
        ).fetchone()
        role = str(actor["role_code"])
        actor_id = int(actor["id"])
        if member is None or str(member["role_code"]) != ROLE_MEMBER:
            if role == ROLE_OPERATOR:
                raise ManagementRecordNotFoundError("会员不存在")
            raise PermissionDeniedError
        if role == ROLE_OPERATOR or (role == ROLE_MEMBER and actor_id == member_id):
            return member_id
        if role == ROLE_ADVISOR:
            cls._require_active_binding(connection, member_id, actor_id)
            return member_id
        raise PermissionDeniedError

    @staticmethod
    def _require_active_binding(
        connection: sqlite3.Connection, member_user_id: int, advisor_user_id: int
    ) -> None:
        row = connection.execute(
            """
            SELECT 1 FROM advisor_bindings
            WHERE member_user_id = ? AND advisor_user_id = ? AND status = 'active'
            """,
            (member_user_id, advisor_user_id),
        ).fetchone()
        if row is None:
            raise PermissionDeniedError

    @staticmethod
    def _active_binding_advisor(connection: sqlite3.Connection, member_user_id: int) -> int | None:
        row = connection.execute(
            """
            SELECT advisor_user_id FROM advisor_bindings
            WHERE member_user_id = ? AND status = 'active'
            """,
            (member_user_id,),
        ).fetchone()
        return None if row is None else int(row["advisor_user_id"])

    @classmethod
    def _accessible_task(
        cls,
        connection: sqlite3.Connection,
        actor: sqlite3.Row,
        task_id: int,
        *,
        write: bool,
    ) -> sqlite3.Row:
        task_id_value = cls._positive_id(task_id, "上门任务编号")
        task = connection.execute(
            """
            SELECT id, member_user_id, advisor_user_id, status
            FROM visit_tasks WHERE id = ?
            """,
            (task_id_value,),
        ).fetchone()
        role = str(actor["role_code"])
        if task is None:
            if role == ROLE_OPERATOR:
                raise ManagementRecordNotFoundError("上门任务不存在")
            raise PermissionDeniedError
        if role == ROLE_OPERATOR:
            return task
        if role == ROLE_ADVISOR:
            actor_id = int(actor["id"])
            if task["advisor_user_id"] is None or int(task["advisor_user_id"]) != actor_id:
                raise PermissionDeniedError
            cls._require_active_binding(connection, int(task["member_user_id"]), actor_id)
            return task
        if role == ROLE_MEMBER and not write and int(task["member_user_id"]) == int(actor["id"]):
            return task
        raise PermissionDeniedError

    @staticmethod
    def _positive_id(value: Any, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ManagementValidationError(f"{label}无效")
        return value

    @staticmethod
    def _validate_limit(limit: int) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ManagementValidationError("读取数量必须在 1 到 500 之间")
        return limit

    @staticmethod
    def _required_text(value: Any, label: str, maximum: int) -> str:
        if not isinstance(value, str):
            raise ManagementValidationError(f"{label}格式无效")
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ManagementValidationError(f"{label}不能为空")
        if len(cleaned) > maximum:
            raise ManagementValidationError(f"{label}不能超过 {maximum} 个字符")
        return cleaned

    @classmethod
    def _optional_text(cls, value: Any, label: str, maximum: int) -> str | None:
        if value is None or value == "":
            return None
        return cls._required_text(value, label, maximum)

    @staticmethod
    def _datetime_to_db(value: datetime | str, label: str) -> str:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            cleaned = value.strip()
            if not cleaned:
                raise ManagementValidationError(f"{label}不能为空")
            normalized = cleaned[:-1] + "+00:00" if cleaned.endswith(("Z", "z")) else cleaned
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError as error:
                raise ManagementValidationError(f"{label}必须是有效的 ISO 8601 日期时间") from error
        else:
            raise ManagementValidationError(f"{label}格式无效")
        return timestamp_to_db(as_beijing(parsed).astimezone(UTC))

    @staticmethod
    def _membership_status(value: str) -> str:
        if not isinstance(value, str) or value not in MEMBERSHIP_STATUSES:
            raise ManagementValidationError("会员状态无效")
        return value

    @staticmethod
    def _mapping_json(value: Mapping[str, Any] | None, label: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise ManagementValidationError(f"{label}必须是键值对象")
        try:
            return json.dumps(
                dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError) as error:
            raise ManagementValidationError(f"{label}包含无法保存的内容") from error

    @staticmethod
    def _recorded_visit_minutes(details_json: Any) -> int | None:
        """Read a trustworthy, explicitly recorded visit duration or ignore it."""

        if details_json is None:
            return None
        try:
            details = json.loads(str(details_json))
        except (TypeError, ValueError):
            return None
        if not isinstance(details, dict):
            return None
        value = details.get("duration_minutes")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        numeric = float(value)
        if not math.isfinite(numeric) or not numeric.is_integer() or not 1 <= numeric <= 1_440:
            return None
        return int(numeric)

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        actor_user_id: int,
        action: str,
        entity_type: str,
        entity_id: int,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        details_json = (
            None
            if details is None
            else json.dumps(
                dict(details), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        )
        connection.execute(
            """
            INSERT INTO audit_logs (
                actor_user_id, action, entity_type, entity_id, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                actor_user_id,
                action,
                entity_type,
                entity_id,
                details_json,
                timestamp_to_db(utc_now()),
            ),
        )

    @staticmethod
    def _member_summary(row: sqlite3.Row) -> dict[str, Any]:
        result = {
            **dict(row),
            "membership_starts_at": timestamp_from_db(row["membership_starts_at"]),
            "membership_ends_at": timestamp_from_db(row["membership_ends_at"]),
            "next_visit_at": timestamp_from_db(row["next_visit_at"]),
        }
        raw_benefits = result.pop("membership_benefits_json", None)
        try:
            parsed_benefits = None if raw_benefits is None else json.loads(str(raw_benefits))
        except (TypeError, ValueError):
            parsed_benefits = None
        result["membership_benefits"] = parsed_benefits if isinstance(parsed_benefits, dict) else {}
        return result

    @staticmethod
    def _fact_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["value"] = json.loads(str(row["value_json"]))
        result.pop("value_json")
        result["effective_at"] = timestamp_from_db(row["effective_at"])
        result["created_at"] = timestamp_from_db(row["created_at"])
        result["updated_at"] = timestamp_from_db(row["updated_at"])
        return result

    @staticmethod
    def _life_record_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["occurred_at"] = timestamp_from_db(row["occurred_at"])
        result["details"] = (
            None if row["details_json"] is None else json.loads(str(row["details_json"]))
        )
        result.pop("details_json")
        return result

    @staticmethod
    def _visit_task_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for field in ("scheduled_at", "created_at", "updated_at"):
            result[field] = timestamp_from_db(row[field])
        return result

    @staticmethod
    def _visit_record_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for field in ("visited_at", "created_at", "updated_at"):
            result[field] = timestamp_from_db(row[field])
        result["details"] = (
            None if row["details_json"] is None else json.loads(str(row["details_json"]))
        )
        result.pop("details_json")
        return result


__all__ = [
    "InvalidVisitStateError",
    "ManagementRecordNotFoundError",
    "ManagementValidationError",
    "PermissionDeniedError",
    "ServiceManagementError",
    "ServiceManagementService",
]
