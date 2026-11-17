"""Member-owned appointment edits for the simplified FAWNSEE client.

This service deliberately does not reuse the operator/advisor appointment
mutation. A member may edit only a request they created while it is still
pending. Assignment, status and ownership remain server-controlled.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ollama_chat_app.data.database import timestamp_to_db, utc_now


class MemberBookingError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


_MISSING = object()
_FIELDS = frozenset({"service_type", "scheduled_at", "address", "notes"})


def _text(value: Any, maximum: int, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if type(value) is not str or len(value) > maximum:
        raise MemberBookingError("invalid_appointment_input")
    return value.strip()


def _scheduled(value: Any, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if type(value) is not str or len(value) > 80:
        raise MemberBookingError("invalid_appointment_input")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise MemberBookingError("invalid_appointment_input") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.year < 1900:
        raise MemberBookingError("invalid_appointment_input")
    return timestamp_to_db(parsed)


class MemberBookingService:
    """Strict member appointment read/update service.

    The PostgreSQL migration supplies the matching row-level UPDATE policies.
    The service remains strict on SQLite fixtures so the same boundary is
    exercised before a cloud rollout.
    """

    def __init__(self, database, management=None):
        if management is None:
            from ollama_chat_app.services.service_management import ServiceManagementService

            management = ServiceManagementService(database)
        self.database = database
        self.management = management

    @staticmethod
    def _row(row) -> dict[str, Any]:
        if hasattr(row, "keys"):
            return {key: row[key] for key in row.keys()}
        lookup = getattr(row, "_lookup", None)
        if isinstance(lookup, dict):
            return dict(lookup)
        return dict(row)

    def get(self, actor_id: int, appointment_id: int) -> dict[str, Any]:
        with self.database.connect() as connection:
            self.management._active_actor(connection, actor_id)
            row = connection.execute(
                "SELECT t.id,t.member_user_id,t.advisor_user_id,t.title,t.scheduled_at,"
                "t.status,t.notes,t.created_at,t.updated_at,"
                "d.service_type,d.address,d.latitude,d.longitude,d.requested_by "
                "FROM visit_tasks t LEFT JOIN visit_task_details d ON d.task_id=t.id "
                "WHERE t.id=? AND t.member_user_id=?",
                (appointment_id, actor_id),
            ).fetchone()
        if row is None:
            raise MemberBookingError("not_found")
        value = self._row(row)
        # Assignment and internal requester identifiers are not client-editable
        # and should not be copied into a simplified member form.
        value.pop("member_user_id", None)
        value.pop("advisor_user_id", None)
        value.pop("requested_by", None)
        value["expected_updated_at"] = value["updated_at"]
        return value

    def update(self, actor_id: int, appointment_id: int, *, expected_updated_at: str, **changes: Any) -> dict[str, Any]:
        if type(expected_updated_at) is not str or len(expected_updated_at) > 80:
            raise MemberBookingError("invalid_appointment_version")
        if set(changes) - _FIELDS:
            raise MemberBookingError("member_appointment_field_forbidden")
        if not changes:
            raise MemberBookingError("no_appointment_changes")

        service_type = changes.get("service_type", _MISSING)
        scheduled_at = changes.get("scheduled_at", _MISSING)
        address = changes.get("address", _MISSING)
        notes = changes.get("notes", _MISSING)
        if service_type is not _MISSING:
            service_type = _text(service_type, 200)
            if not service_type:
                raise MemberBookingError("invalid_appointment_input")
        if scheduled_at is not _MISSING:
            scheduled_at = _scheduled(scheduled_at, optional=True)
        if address is not _MISSING:
            address = _text(address, 2000, optional=True)
        if notes is not _MISSING:
            notes = _text(notes, 10000)

        with self.database.transaction() as connection:
            actor = self.management._active_actor(connection, actor_id)
            if actor["role_code"] != "member":
                raise MemberBookingError("permission_denied")
            row = connection.execute(
                "SELECT t.id,t.member_user_id,t.advisor_user_id,t.title,t.scheduled_at,"
                "t.status,t.notes,t.created_at,t.updated_at,d.service_type,d.address,"
                "d.requested_by,d.outcome_status FROM visit_tasks t LEFT JOIN visit_task_details d ON d.task_id=t.id "
                "WHERE t.id=? AND t.member_user_id=?",
                (appointment_id, actor_id),
            ).fetchone()
            if row is None:
                raise MemberBookingError("not_found")
            if row["status"] != "pending" or row["requested_by"] != actor_id or row["outcome_status"] is not None:
                raise MemberBookingError("appointment_locked")
            if row["updated_at"] != expected_updated_at:
                raise MemberBookingError("appointment_conflict")
            # mobile_booking_capability_0001
            # PlatformDatabase serializes every write with its transaction-wide
            # advisory lock. Owner, pending, requester, NULL outcome and CAS were
            # all checked above under that same transaction before minting this
            # exact-id capability. Clients cannot submit GUCs or arbitrary SQL.
            postgres = getattr(self.database, "is_postgres", False)
            if postgres:
                connection.execute(
                    "SELECT pg_catalog.set_config('medical_app.member_booking_task_id', ?, true)",
                    (str(appointment_id),),
                )
            now = timestamp_to_db(utc_now())
            task_title = row["title"] if service_type is _MISSING else service_type
            task_time = row["scheduled_at"] if scheduled_at is _MISSING else scheduled_at
            task_notes = row["notes"] if notes is _MISSING else notes
            task_write = connection.execute(
                "UPDATE visit_tasks SET title=?,scheduled_at=?,notes=?,updated_at=? "
                "WHERE id=? AND member_user_id=? AND status='pending' AND updated_at=?",
                (task_title, task_time, task_notes, now, appointment_id, actor_id, expected_updated_at),
            )
            if task_write.rowcount != 1:
                raise MemberBookingError("appointment_conflict")
            detail_type = row["service_type"] if service_type is _MISSING else service_type
            detail_address = row["address"] if address is _MISSING else address
            detail_write = connection.execute(
                "UPDATE visit_task_details SET service_type=?,address=?,updated_at=? "
                "WHERE task_id=? AND requested_by=? AND outcome_status IS NULL",
                (detail_type, detail_address, now, appointment_id, actor_id),
            )
            if detail_write.rowcount != 1:
                # Raising within transaction() rolls back the task write too.
                raise MemberBookingError("appointment_locked")
            if postgres:
                connection.execute(
                    "SELECT pg_catalog.set_config('medical_app.member_booking_task_id', '', true)"
                )
            # On errors the enclosing transaction rolls back both writes and
            # set_config; on success the capability is cleared before commit.
        return self.get(actor_id, appointment_id)

