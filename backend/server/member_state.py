"""Small, read-only member state manifest used by the simplified client.

The endpoint exposes only opaque SHA-256 digests. It intentionally omits the
legacy file/report resources so that the new client cannot accidentally depend
on the removed screens. The old v3 sync/RPC endpoints remain available to the
old client.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from .platform_auth import PlatformAuth, PlatformPrincipal, authentication_dependency
from .member_booking import MemberBookingError, MemberBookingService

RESOURCE_KEYS = (
    "profile", "records", "observations", "reminders", "appointments", "chat",
    "goals", "foods", "content", "ai_settings", "growth", "shopping",
    "community", "membership", "preferences",
)
CLIENT_CAPABILITIES = {
    "client_contract": "simplified-member-v1",
    "ui_spec_revision": "fawnsee-simplified-20261017",
    "preserved": ["records", "appointments", "chat", "goals", "food_library", "trends", "community", "membership"],
    "removed_screens": ["files_reports", "daily_reading", "recipes", "health_preferences", "drafts", "record_context"],
    "exercise_sleep_expansion": False,
}
MAX_HASH_BYTES = 16 * 1024 * 1024


class MemberStateError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"sha256": hashlib.sha256(bytes(value)).hexdigest(), "bytes": len(value)}
    return value


def _digest(value: Any) -> str:
    try:
        encoded = json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), default=str, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise MemberStateError("sync_state_unavailable") from None
    if len(encoded) > MAX_HASH_BYTES:
        raise MemberStateError("sync_state_too_large")
    return hashlib.sha256(encoded).hexdigest()


def _rows(connection, sql: str, parameters=()) -> list[dict[str, Any]]:
    rows = connection.execute(sql, parameters).fetchall()
    return [_row_dict(row) for row in rows]


def _row_dict(row) -> dict[str, Any]:
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    lookup = getattr(row, "_lookup", None)
    if isinstance(lookup, dict):
        return dict(lookup)
    return dict(row)


class MemberStateService:
    def __init__(self, database, services, library=None):
        self.database = database
        self.services = services
        self.library = library
        self.bookings = MemberBookingService(database, services.get("service_management"))
        self._content_manifest_cache: dict[str, tuple[int, int, str]] = {}

    def _actor(self, actor_id: int) -> None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT role_code,account_status FROM users WHERE id=?", (actor_id,)
            ).fetchone()
        if row is None or row["role_code"] != "member" or row["account_status"] != "active":
            raise MemberStateError("permission_denied")

    def _value(self, actor_id: int, key: str) -> Any:
        with self.database.connect() as connection:
            if key == "profile":
                return self.services["health"].get_profile(actor_id)
            if key == "records":
                return _rows(connection, "SELECT id,category,occurred_at,local_date,timezone_offset_minutes,content,details_json,source,created_at,updated_at FROM life_records WHERE user_id=? ORDER BY occurred_at DESC,id DESC", (actor_id,))
            if key == "observations":
                return _rows(connection, "SELECT id,kind,measured_at,payload_json,created_at,updated_at FROM expansion_observations WHERE user_id=? ORDER BY measured_at DESC,id DESC", (actor_id,))
            if key == "reminders":
                return self.services["health"].list_reminders(actor_id)
            if key == "appointments":
                return _rows(connection, "SELECT t.id,t.title,t.scheduled_at,t.status,t.notes,t.created_at,t.updated_at,d.service_type,d.address,d.requested_by,d.outcome_status FROM visit_tasks t LEFT JOIN visit_task_details d ON d.task_id=t.id WHERE t.member_user_id=? ORDER BY t.scheduled_at ASC NULLS LAST,t.id", (actor_id,))
            if key == "chat":
                conversations = _rows(connection, "SELECT id,title,provider,model,created_at,updated_at FROM conversations WHERE user_id=? ORDER BY updated_at DESC,id DESC", (actor_id,))
                messages = _rows(connection, "SELECT m.id,m.conversation_id,m.sequence_no,m.role,m.content,m.status,m.provider,m.model,m.created_at,m.updated_at FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE c.user_id=? ORDER BY m.conversation_id,m.sequence_no", (actor_id,))
                return {"conversations": conversations, "messages": messages}
            if key == "goals":
                return _rows(connection, "SELECT id,kind,status,payload_json,created_at,updated_at FROM expansion_goals WHERE user_id=? ORDER BY id", (actor_id,))
            if key == "foods":
                return _rows(connection, "SELECT id,owner_user_id,name,category,payload_json,created_at,updated_at FROM expansion_foods WHERE owner_user_id IS NULL OR owner_user_id=? ORDER BY id", (actor_id,))
            if key == "ai_settings":
                old = self.services["preferences"].get(actor_id)
                row = connection.execute("SELECT payload_json,updated_at FROM expansion_settings WHERE user_id=?", (actor_id,)).fetchone()
                return {"preferences": old, "expansion": None if row is None else {"payload_json": row["payload_json"], "updated_at": row["updated_at"]}}
            if key == "growth":
                return _rows(connection, "SELECT id,delta,event_key,reason,created_at FROM expansion_point_events WHERE user_id=? ORDER BY id", (actor_id,))
            if key == "shopping":
                return {
                    "cart": _rows(connection, "SELECT user_id,product_id,quantity FROM member_cart WHERE user_id=? ORDER BY product_id", (actor_id,)),
                    "favorites": _rows(connection, "SELECT user_id,product_id FROM member_favorites WHERE user_id=? ORDER BY product_id", (actor_id,)),
                    "orders": _rows(connection, "SELECT id,order_no,product_id,product_name_snapshot,quantity,total_amount_cents,currency,status,delivered_at,created_at,updated_at FROM orders WHERE member_user_id=? ORDER BY id DESC", (actor_id,)),
                }
            if key == "community":
                return {"topics": _rows(connection, "SELECT id,slug,name FROM forum_topics ORDER BY id"), "posts": _rows(connection, "SELECT id,topic_id,author_user_id,content,status FROM forum_posts WHERE status='active' ORDER BY id DESC"), "comments": _rows(connection, "SELECT id,post_id,author_user_id,content FROM forum_comments ORDER BY id DESC")}
            if key == "membership":
                return _rows(connection, "SELECT id,plan_code,status,starts_at,ends_at,benefits_json,created_at,updated_at FROM memberships WHERE user_id=? ORDER BY id DESC", (actor_id,))
            if key == "preferences":
                row = connection.execute("SELECT preferences_json,updated_at FROM user_preferences WHERE user_id=?", (actor_id,)).fetchone()
                return None if row is None else {"preferences_json": row["preferences_json"], "updated_at": row["updated_at"]}
        raise MemberStateError("unsupported_sync_resource")

    def _content_digest(self) -> str:
        roots = []
        if self.library is not None:
            root = getattr(self.library, "root", None)
            if root:
                roots.append(Path(root))
        expansion = self.services.get("expansion")
        catalog_root = getattr(expansion, "catalog_root", None)
        if catalog_root:
            roots.append(Path(catalog_root))
        all_files = []
        for root in roots:
            if not root.is_dir() or root.is_symlink():
                continue
            files = []
            for path in sorted(root.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    relative = path.relative_to(root).as_posix()
                    key = str(path)
                    stat = path.stat()
                    cached = self._content_manifest_cache.get(key)
                    if cached is not None and cached[:2] == (stat.st_size, stat.st_mtime_ns):
                        digest = cached[2]
                    else:
                        with path.open("rb") as stream:
                            digest = hashlib.file_digest(stream, "sha256").hexdigest()
                        self._content_manifest_cache[key] = (stat.st_size, stat.st_mtime_ns, digest)
                    files.append((relative, stat.st_size, digest))
            if files:
                # Keep the two public static roots distinct even when they
                # contain an equally named catalog or original file.
                all_files.extend((str(root), item) for item in files)
        return _digest(all_files)

    def manifest(self, actor_id: int) -> dict[str, Any]:
        self._actor(actor_id)
        hashes = {}
        for key in RESOURCE_KEYS:
            hashes[key] = self._content_digest() if key == "content" else _digest(self._value(actor_id, key))
        return {
            "schema_version": 1,
            "contract": "simplified-member-v1",
            "actor_id": actor_id,
            "revision": _digest(hashes),
            "resources": hashes,
            "poll_after_ms": 3000,
            "excluded_resources": ["user_files", "reports", "drafts", "record_context"],
        }

    def capabilities(self, actor_id: int) -> dict[str, Any]:
        self._actor(actor_id)
        return dict(CLIENT_CAPABILITIES)


def create_member_router(auth: PlatformAuth, state: MemberStateService) -> APIRouter:
    router = APIRouter(prefix="/v1/member", tags=["member"])
    principal = Depends(authentication_dependency(auth))

    @router.get("/capabilities")
    def capabilities(_principal: PlatformPrincipal = principal):
        try:
            with auth.identity(_principal):
                return state.capabilities(_principal.user.id)
        except MemberStateError as error:
            raise HTTPException(403, error.code) from None

    @router.get("/sync")
    def sync(_principal: PlatformPrincipal = principal):
        try:
            with auth.identity(_principal):
                result = state.manifest(_principal.user.id)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except MemberStateError as error:
            status = 413 if error.code == "sync_state_too_large" else 403 if error.code == "permission_denied" else 503
            raise HTTPException(status, error.code) from None

    @router.patch("/appointments/{appointment_id}")
    def update_appointment(appointment_id: int, payload: dict,
                           _principal: PlatformPrincipal = principal):
        if not isinstance(payload, dict) or set(payload) - {"expected_updated_at", "service_type", "scheduled_at", "address", "notes"}:
            raise HTTPException(422, "member_appointment_field_forbidden")
        expected = payload.pop("expected_updated_at", None)
        try:
            with auth.identity(_principal):
                result = state.bookings.update(_principal.user.id, appointment_id, expected_updated_at=expected, **payload)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except MemberBookingError as error:
            status = 409 if error.code in {"appointment_conflict", "appointment_locked"} else 404 if error.code == "not_found" else 403 if error.code == "permission_denied" else 422
            raise HTTPException(status, error.code) from None

    return router

