"""Precisely scoped reads from an authorized cloud mirror; all writes remain remote.

No local domain service is instantiated and no cache becomes an offline login.
Unknown operations or filters deliberately use the original remote implementation.
"""

from __future__ import annotations

import inspect
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from functools import wraps

from ..data.database import Conversation, Message, timestamp_from_db
from .cloud_client import CloudAPIError
from .cloud_sync import SyncError
from .health import HealthService
from .offline_outbox import OFFLINE_METHODS, canonical_method, queue_payload_allowed
from .service_management import VISIT_TASK_STATUSES, ServiceManagementService

_MISS = object()
_ACTOR_FIELDS = ("user_id", "actor_user_id", "advisor_user_id", "operator_user_id")
_READ_PREFIXES = ("get_", "list_", "context_", "build_", "prepare_", "storage_")
_CACHE_METHODS = {
    "health": {"get_profile", "list_life_records", "list_reminders", "get_service_summary"},
    "chat": {"list_conversations", "list_messages", "get_conversation", "get_default_conversation",
             "list_message_attachments"},
    "service_management": {"list_members", "list_advisors", "list_visit_tasks"},
    "preferences": {"get", "get_preferences"},
    "files": {"list_files", "get_file", "list_reports", "get_report"},
}


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Snapshot timestamp must be timezone-aware")
    return value.astimezone(UTC)


class SyncedRemoteService:
    """Wrap an existing remote service using the same client and sync coordinator."""

    uses_network = True

    def __init__(self, remote, sync):
        if remote.client is not sync.client:
            raise CloudAPIError("permission")
        self._remote = remote
        self._sync = sync
        self.client = remote.client
        self.service_name = remote.service_name
        self._endpoint = self.client.base_url.rstrip("/")

    def __getattr__(self, method):
        if method.startswith("_"):
            raise AttributeError(method)
        target = getattr(self._remote, method)
        if not callable(target):
            return target

        @wraps(target)
        def invoke(*args, **kwargs):
            return self._invoke(method, target, args, kwargs)

        invoke.__signature__ = inspect.signature(target)
        return invoke

    def call(self, method, *args, request_id=None, **kwargs):
        target = getattr(self._remote, method)
        return self._invoke(method, target, args, kwargs, request_id=request_id)

    def _guard(self, actor):
        if (
            type(actor) is not int
            or actor != self._sync.actor_id
            or self.client is not self._sync.client
            or self.client.base_url.rstrip("/") != self._endpoint
        ):
            raise CloudAPIError("permission")
        if not self.client.is_authenticated:
            error = CloudAPIError("authentication")
            self._sync.reject_authorization(error)
            raise error
        if self._sync.authorization_blocked:
            raise CloudAPIError("permission")

    def _invoke(self, method, target, args, kwargs, *, request_id=None):
        try:
            bound = inspect.signature(target).bind(*args, **kwargs)
        except TypeError:
            raise CloudAPIError("validation") from None
        bound.apply_defaults()
        # Match the server's actor binding: first identity parameter in the
        # method signature. Later advisor/member IDs are authorized targets,
        # not a second caller (e.g. an operator assigning a different advisor).
        actor_field = next((name for name in bound.arguments if name in _ACTOR_FIELDS), None)
        # Auth is intentionally not wrapped. Other no-actor local helper methods
        # are not cache eligible, but must still belong to the active cloud session.
        actor = bound.arguments[actor_field] if actor_field else self._sync.actor_id
        self._guard(actor)
        if (self.service_name == "health" and method == "toggle_reminder"
                and getattr(self._sync, "offline_enabled", False)):
            snapshot = self._sync.authorized_snapshot(actor, self._endpoint)
            row = next((item for item in snapshot["resources"]["reminders"]
                        if item["id"] == bound.arguments["reminder_id"]), None)
            if row is None:
                raise CloudAPIError("not_found")
            enabled = bound.arguments["enabled"]
            if enabled is None:
                enabled = row["status"] != "active"
            if type(enabled) is not bool:
                raise CloudAPIError("validation")
            return self._sync.submit_intent(
                "health", "resume_reminder" if enabled else "pause_reminder", [actor],
                {"reminder_id": row["id"]},
            )
        if method in _CACHE_METHODS.get(self.service_name, set()):
            try:
                generation = self._sync.cache_generation
                snapshot = self._sync.authorized_snapshot(actor, self._endpoint)
                result = self._read(method, bound.arguments, snapshot["resources"], actor)
            except SyncError:
                result = _MISS
            except (KeyError, TypeError, ValueError):
                # Do not reinterpret an unknown schema or unsupported value.
                result = _MISS
            except CloudAPIError as error:
                self._sync.reject_authorization(error)
                raise
            if result is not _MISS:
                self._guard(actor)
                copied = deepcopy(result)
                if self._sync.snapshot_generation_is_current(generation):
                    return copied
        mutation = not (method.startswith(_READ_PREFIXES) or method in {"get", "me"})
        queued_method = canonical_method(self.service_name, method)
        if (
            mutation and getattr(self._sync, "offline_enabled", False)
            and queued_method in OFFLINE_METHODS.get(self.service_name, ())
        ):
            intent_args = [actor]
            intent_kwargs = {name: value for name, value in bound.arguments.items()
                             if name != actor_field}
            if queue_payload_allowed(intent_args, intent_kwargs):
                return self._sync.submit_intent(
                    self.service_name, method, intent_args, intent_kwargs
                )
            if not self.client.is_online_authenticated:
                raise CloudAPIError("offline")
        if mutation and getattr(self._sync, "_state", None) == "offline":
            raise CloudAPIError("offline")
        try:
            result = (
                target(*args, **kwargs)
                if request_id is None
                else self._remote.call(method, *args, request_id=request_id, **kwargs)
            )
        except CloudAPIError as error:
            if actor == self._sync.actor_id and self.client.base_url.rstrip("/") == self._endpoint:
                if error.code in {"authentication", "not_authenticated", "permission"}:
                    self._sync.reject_authorization(error)
                elif mutation and error.outcome_uncertain:
                    self._sync.invalidate_after_write()
            raise
        self._guard(actor)
        # These legacy "get" methods may create the default conversation when
        # none exists. Never create that row locally while offline.
        may_create_default = self.service_name == "chat" and method in _CACHE_METHODS["chat"]
        if mutation or may_create_default:
            self._sync.invalidate_after_write()
        return result

    @staticmethod
    def _owned_rows(rows, actor, field="user_id"):
        if type(rows) is not list:
            raise ValueError("Unknown snapshot shape")
        if any(type(row) is not dict or row.get(field) != actor for row in rows):
            raise CloudAPIError("permission")
        return rows

    def _read(self, method, values, resources, actor):
        if self.service_name == "files":
            resource = "reports" if method in {"list_reports", "get_report"} else "user_files"
            if resource not in resources:
                return _MISS
            rows = self._owned_rows(resources[resource], actor)
            if method == "list_files":
                return rows
            if method == "list_reports":
                return [dict(item, items=None) for item in rows if values["include_archived"]
                        or item["status"] != "archived"]
            identifier = values["report_id"] if method == "get_report" else values["file_id"]
            item = next((row for row in rows if row["id"] == identifier), None)
            if item is None:
                return _MISS
            if method == "get_report":
                return item
            content = self._sync.files.read(self._sync.identity, "user_files", item)
            return dict(item, content=content) if content is not None else _MISS
        if self.service_name == "chat" and method == "list_message_attachments":
            if "chat_attachments" not in resources:
                return _MISS
            message_id = values["message_id"]
            messages = [item for rows in resources["messages"].values() for item in rows]
            if not any(item.id == message_id for item in messages):
                raise CloudAPIError("not_found")
            fields = {"id", "original_name", "media_type", "size_bytes", "extraction_method"}
            return [{key: value for key, value in item.items() if key in fields}
                    for item in resources["chat_attachments"] if item["message_id"] == message_id]
        if self.service_name == "preferences":
            if set(values) != {"user_id"}:
                return _MISS
            preferences = dict(resources["preferences"])
            if getattr(self._sync, "offline_enabled", False):
                for item in self._sync.outbox.list(self._sync.identity, include_payload=True):
                    if item["service"] == "preferences":
                        patch = item["args"][1] if len(item["args"]) > 1 else item["kwargs"].get(
                            "patch", {})
                        for key in ("ai_context_consent", "weather_consent"):
                            if patch.get(key) is False:
                                preferences[key] = False
            return preferences
        if self.service_name == "health":
            if method != "list_life_records" and set(values) != {"user_id"}:
                return _MISS
            if method == "get_profile":
                profile = resources["profile"]
                if profile.get("user_id") != actor:
                    raise CloudAPIError("permission")
                return profile
            if method == "get_service_summary":
                return resources["service_summary"]
            if method == "list_reminders":
                return self._owned_rows(resources["reminders"], actor)
            if set(values) != {"user_id", "category", "limit", "start_date", "end_date"}:
                return _MISS
            rows = self._owned_rows(resources["life_records"], actor)
            category = (
                None
                if values["category"] is None
                else HealthService._validate_category(values["category"])
            )
            limit = HealthService._validate_limit(values["limit"])
            start = (
                None
                if values["start_date"] is None
                else HealthService._parse_date_input(values["start_date"], "开始日期")
            )
            end = (
                None
                if values["end_date"] is None
                else HealthService._parse_date_input(values["end_date"], "结束日期")
            )
            if start is not None and end is not None and start > end:
                return _MISS
            minimum = (
                None
                if start is None
                else timestamp_from_db(HealthService._beijing_day_start(start))
            )
            maximum = (
                None
                if end is None or end == date.max
                else timestamp_from_db(HealthService._beijing_day_start(end + timedelta(days=1)))
            )
            selected = [
                row
                for row in rows
                if (category is None or row["category"] == category)
                and (minimum is None or _aware(row["occurred_at"]) >= minimum)
                and (maximum is None or _aware(row["occurred_at"]) < maximum)
            ]
            return sorted(
                selected, key=lambda row: (_aware(row["occurred_at"]), row["id"]), reverse=True
            )[:limit]
        if self.service_name == "chat":
            return self._chat_read(method, values, resources, actor)
        return self._management_read(method, values, resources, actor)

    def _chat_read(self, method, values, resources, actor):
        expected = {"user_id"}
        if method in {"get_conversation", "list_messages"}:
            expected.add("conversation_id")
        if method == "list_messages":
            expected.add("limit")
        if set(values) != expected:
            return _MISS
        conversations = resources["conversations"]
        if any(not isinstance(row, Conversation) or row.user_id != actor for row in conversations):
            raise CloudAPIError("permission")
        if not conversations:
            return _MISS  # Original methods create a default: online-only.
        if method == "list_conversations":
            return sorted(
                conversations, key=lambda row: (_aware(row.updated_at), row.id), reverse=True
            )
        identifier = values.get("conversation_id")
        if identifier is None:
            conversation = min(conversations, key=lambda row: row.id)
        else:
            if type(identifier) is not int or identifier <= 0:
                return _MISS
            conversation = next((row for row in conversations if row.id == identifier), None)
            if conversation is None:
                raise CloudAPIError("permission")
        if method != "list_messages":
            return conversation
        if set(values) != {"user_id", "limit", "conversation_id"}:
            return _MISS
        rows = resources["messages"].get(str(conversation.id))
        if rows is None:
            return _MISS
        if any(
            not isinstance(row, Message) or row.conversation_id != conversation.id for row in rows
        ):
            raise CloudAPIError("permission")
        limit = values["limit"]
        if limit is not None and type(limit) is not int:
            return _MISS
        ordered = sorted(rows, key=lambda row: row.sequence_no)
        return ordered if limit is None else ([] if limit <= 0 else ordered[-limit:])

    def _management_read(self, method, values, resources, actor):
        if method != "list_visit_tasks" and set(values) != {"actor_user_id"}:
            return _MISS
        role = self._sync.actor_role
        if role not in {"member", "advisor", "operator"}:
            return _MISS
        if method == "list_advisors":
            return resources["advisors"] if role == "operator" else _MISS
        if method == "list_members":
            rows = resources["members"]
            if role == "member":
                if not rows:
                    return _MISS
                self._owned_rows(rows, actor, "id")
            if role == "advisor":
                self._owned_rows(rows, actor, "advisor_user_id")
            return rows
        if set(values) != {"actor_user_id", "member_user_id", "status", "limit"}:
            return _MISS
        limit = ServiceManagementService._validate_limit(values["limit"])
        status, member = values["status"], values["member_user_id"]
        if status is not None and status not in VISIT_TASK_STATUSES:
            return _MISS
        if member is not None:
            # A missing member must not turn a forbidden target into a successful
            # empty response. The original server can decide authorization.
            allowed = {row["id"] for row in resources["members"]}
            if role == "member":
                allowed = {actor}
            if type(member) is not int or member not in allowed:
                return _MISS
        rows = resources["appointments"]
        if role != "operator":
            self._owned_rows(
                rows, actor, "member_user_id" if role == "member" else "advisor_user_id"
            )
        # Preserve the server's task ordering (it depends on the underlying task
        # state, not the displayed incomplete/disabled outcome override).
        return [
            row
            for row in rows
            if (status is None or row["status"] == status)
            and (member is None or row["member_user_id"] == member)
        ][:limit]


SnapshotAwareRemoteFacade = SyncedRemoteService
