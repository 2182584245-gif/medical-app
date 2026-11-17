"""One shared appointment workflow for members, operators and bound advisors."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from ..data.database import Database, timestamp_to_db, utc_now

APPOINTMENT_STATUSES = frozenset({"pending", "in_progress", "completed", "incomplete", "disabled"})
_UNCHANGED = object()


class AppointmentService:
    def __init__(self, database: Database | None = None) -> None:
        from .service_management import ServiceManagementService

        self.management = ServiceManagementService(database)
        self.database = self.management.database

    def create_request(
        self,
        member_id: int,
        service_type: str,
        scheduled_at: datetime | str | None = None,
        notes: str = "",
        address: str = "",
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> int:
        from .service_management import PermissionDeniedError

        service = self.management
        kind = service._required_text(service_type, "服务类型", 200)
        location = service._optional_text(address, "上门地址", 2000)
        note = service._optional_text(notes, "预约备注", 10000)
        scheduled = service._datetime_to_db(scheduled_at, "预约时间") if scheduled_at else None
        lat, lon = self._coordinates(latitude, longitude)
        with self.database.transaction() as connection:
            member = service._active_actor(connection, member_id)
            if member["role_code"] != "member":
                raise PermissionDeniedError
            advisor = service._active_binding_advisor(connection, member_id)
            now = timestamp_to_db(utc_now())
            cursor = connection.execute(
                "INSERT INTO visit_tasks (member_user_id, advisor_user_id, title, "
                "scheduled_at, status, notes, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
                (member_id, advisor, kind, scheduled, note, now, now),
            )
            task_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO visit_task_details (task_id, service_type, address, latitude, "
                "longitude, requested_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, kind, location, lat, lon, member_id, now, now),
            )
            service._audit(connection, member_id, "appointment.requested", "visit_task", task_id)
        return task_id

    def list_for_member(self, member_id: int) -> list[dict[str, Any]]:
        from .service_management import PermissionDeniedError

        with self.database.connect() as connection:
            actor = self.management._active_actor(connection, member_id)
            if actor["role_code"] != "member":
                raise PermissionDeniedError
        return self._list(member_id)

    def list_all(self, actor_id: int) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            self.management._require_operator(connection, actor_id)
        return self._list(actor_id)

    def _list(self, actor_id: int) -> list[dict[str, Any]]:
        return [
            {**task, "member_id": task["member_user_id"], "advisor_id": task["advisor_user_id"]}
            for task in self.management.list_visit_tasks(actor_id)
        ]

    def update_request(
        self,
        appointment_id: int,
        actor_id: int,
        *,
        status: str | None = None,
        scheduled_at: datetime | str | None = None,
        advisor_id: int | None | object = _UNCHANGED,
        service_type: str | None = None,
        notes: str | None = None,
        address: str | None = None,
    ) -> None:
        from .service_management import InvalidVisitStateError, ManagementValidationError

        service = self.management
        if status is not None and status not in APPOINTMENT_STATUSES:
            raise ManagementValidationError("预约状态无效")
        with self.database.transaction() as connection:
            service._require_operator(connection, actor_id)
            task = connection.execute(
                "SELECT * FROM visit_tasks WHERE id = ?", (appointment_id,)
            ).fetchone()
            if task is None:
                raise ManagementValidationError("预约不存在")
            if task["status"] == "completed" and status not in {None, "completed"}:
                raise InvalidVisitStateError("已完成的服务保留原始完成状态")
            if status == "completed" and task["status"] != "completed":
                raise InvalidVisitStateError("请由负责顾问填写工作记录后完成服务")
            advisor = task["advisor_user_id"] if advisor_id is _UNCHANGED else advisor_id
            if advisor is not None and advisor != task["advisor_user_id"]:
                service._require_target_role(connection, advisor, "advisor")
                service._require_active_binding(connection, int(task["member_user_id"]), advisor)
                service._active_actor(connection, advisor)
            detail = connection.execute(
                "SELECT * FROM visit_task_details WHERE task_id = ?", (appointment_id,)
            ).fetchone()
            kind = service._required_text(
                service_type if service_type is not None else task["title"], "服务类型", 200
            )
            scheduled = (
                service._datetime_to_db(scheduled_at, "预约时间")
                if scheduled_at is not None
                else task["scheduled_at"]
            )
            note = task["notes"] if notes is None else service._optional_text(notes, "备注", 10000)
            location = (
                (detail["address"] if detail else None)
                if address is None
                else (service._optional_text(address, "地址", 2000))
            )
            latitude = detail["latitude"] if detail and location == detail["address"] else None
            longitude = detail["longitude"] if detail and location == detail["address"] else None
            outcome = detail["outcome_status"] if detail else None
            if status is not None:
                outcome = status if status in {"incomplete", "disabled"} else None
            stored_status = (
                "cancelled" if outcome else (status if status is not None else task["status"])
            )
            now = timestamp_to_db(utc_now())
            connection.execute(
                "UPDATE visit_tasks SET title = ?, scheduled_at = ?, advisor_user_id = ?, "
                "status = ?, notes = ?, updated_at = ? WHERE id = ?",
                (kind, scheduled, advisor, stored_status, note, now, appointment_id),
            )
            connection.execute(
                "INSERT INTO visit_task_details (task_id, service_type, address, latitude, "
                "longitude, outcome_status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET service_type = excluded.service_type, "
                "address = excluded.address, outcome_status = excluded.outcome_status, "
                "latitude = excluded.latitude, longitude = excluded.longitude, "
                "updated_at = excluded.updated_at",
                (appointment_id, kind, location, latitude, longitude, outcome, now, now),
            )
            service._audit(
                connection,
                actor_id,
                "appointment.updated",
                "visit_task",
                appointment_id,
                {
                    "status": status or stored_status,
                    "advisor_user_id": advisor,
                    "scheduled_at": scheduled,
                    "service_type": kind,
                },
            )

    @staticmethod
    def _coordinates(latitude: float | None, longitude: float | None) -> tuple:
        from .service_management import ManagementValidationError

        if (latitude is None) != (longitude is None):
            raise ManagementValidationError("经纬度需要同时提供")
        for coordinate, limit in ((latitude, 90), (longitude, 180)):
            if coordinate is not None and (
                isinstance(coordinate, bool)
                or not isinstance(coordinate, (int, float))
                or not math.isfinite(coordinate)
                or abs(coordinate) > limit
            ):
                raise ManagementValidationError("地图位置无效")
        return latitude, longitude
