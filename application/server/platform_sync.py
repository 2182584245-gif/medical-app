"""Bounded, identity-scoped desktop mirrors and explicit first-account migration.

Never exports credentials or accepts database IDs as cloud ownership. Operations
run inside RpcDispatcher's authenticated transaction and idempotency journal.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
from datetime import datetime
from uuid import UUID

from ollama_chat_app.data.database import (
    conversation_from_row,
    timestamp_to_db,
)
from ollama_chat_app.services.cloud_client import CloudAPIError
from ollama_chat_app.services.cloud_rpc_codec import encode_rpc
from ollama_chat_app.services.health import PROFILE_FIELDS
from ollama_chat_app.services.offline_outbox import (
    OFFLINE_METHODS,
    content_version,
    validate_intent,
    version_resource,
)
from ollama_chat_app.services.preferences import PreferencesService

from .platform_rpc import RpcError

SNAPSHOT_LIMIT = 8 * 1024 * 1024
MANIFEST_LIMIT = 900 * 1024  # leave room for the authenticated RPC envelope
MAX_MIRRORED_LIFE_RECORDS = 1000
MAX_MIRRORED_CONVERSATIONS = 200
MAX_MIRRORED_MESSAGES = 5000
PREFERENCE_FIELDS = frozenset(
    {
        "nickname",
        "preferred_name",
        "font_size",
        "brightness",
        "theme_color",
        "timezone",
        "city",
        "latitude",
        "longitude",
    }
)
EXCLUDED_FIELDS = frozenset(
    {
        "chat_attachments",
        "user_files",
        "pending_messages",
        "system_messages",
        "orders",
        "service_assignments",
    }
)


def _json_bytes(value):
    return json.dumps(
        encode_rpc(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _keys(value, expected):
    if type(value) is not dict or set(value) != set(expected):
        raise ValueError("Migration shape invalid")


def _source_id(value):
    if type(value) is not str or not value.isascii() or not value.isdigit():
        raise ValueError("Source identifier invalid")
    if not 0 < int(value) < 2**63:
        raise ValueError("Source identifier invalid")
    return value


def _timestamp(value):
    if type(value) is not str or len(value) > 64:
        raise ValueError("Timestamp invalid")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Timestamp must have timezone")
    return timestamp_to_db(result)


def _check_mirror_capacity(manifest, existing_conversations=0):
    existing_conversations = max(existing_conversations, int(bool(manifest["conversations"])))
    if (
        len(manifest["life_records"]) > MAX_MIRRORED_LIFE_RECORDS
        or len(manifest["conversations"]) + existing_conversations > MAX_MIRRORED_CONVERSATIONS
        or sum(len(item["messages"]) for item in manifest["conversations"]) > MAX_MIRRORED_MESSAGES
    ):
        raise RpcError(413, "migration_exceeds_snapshot_capacity")


class PlatformSyncService:
    def __init__(self, database, services, pepper: str):
        self.database = database
        self.services = services
        digest = hmac.new(
            bytes.fromhex(pepper), b"medical-app-sync-instance-v1", hashlib.sha256
        ).hexdigest()
        self.instance_id = str(UUID(digest[:32]))

    def get_sync_manifest(self, actor_user_id: int):
        from .platform_sync_pages import SyncPages

        return SyncPages(self).manifest(actor_user_id)

    def get_sync_page(self, actor_user_id: int, resource: str, version: str, offset: int):
        from .platform_sync_pages import SyncPages

        return SyncPages(self).page(actor_user_id, resource, version, offset)

    def get_file_chunk(
        self, actor_user_id: int, kind: str, identifier: int, digest: str, offset: int
    ):
        from .platform_sync_pages import SyncPages

        return SyncPages(self).chunk(actor_user_id, kind, identifier, digest, offset)

    def apply_queued(self, actor_user_id: int, intent: dict):
        """One permission-checked, optimistic, idempotent application intent.

        RpcDispatcher owns the outer transaction and fixed UUID journal. Normal
        writers share PlatformDatabase's write lock, so the base check and
        service mutation cannot race another API writer.
        """
        _keys(intent, {"service", "method", "args", "kwargs", "base_version"})
        service, method = intent["service"], intent["method"]
        if service not in OFFLINE_METHODS or method not in OFFLINE_METHODS[service]:
            raise RpcError(403, "offline_operation_not_permitted")
        try:
            validate_intent(service, method, intent["args"], intent["kwargs"],
                            actor_user_id, intent["base_version"])
        except CloudAPIError as error:
            raise RpcError(403 if error.code == "permission" else 422,
                           "invalid_offline_intent") from None
        with self.database.transaction() as connection:
            actor = self.services["service_management"]._active_actor(connection, actor_user_id)
            if service != "preferences" and actor["role_code"] != "member":
                raise RpcError(403, "offline_member_operations_only")
            resource = version_resource(service, method)
            if resource is not None:
                if resource == "profile":
                    current = self.services["health"].get_profile(actor_user_id)
                elif resource == "reminders":
                    current = self.services["health"].list_reminders(actor_user_id)
                else:
                    current = {key: value for key, value in self.services["preferences"].get(
                        actor_user_id).items() if key != "avatar_path"}
                if not hmac.compare_digest(content_version(current), intent["base_version"]):
                    raise RpcError(409, "offline_base_version_changed")
            target = getattr(self.services[service], method)
            bound = inspect.signature(target).bind(*intent["args"], **intent["kwargs"])
            return target(*bound.args, **bound.kwargs)

    def get_snapshot(self, actor_user_id: int) -> dict:
        health = self.services["health"]
        management = self.services["service_management"]
        with self.database.connect() as connection:
            actor = management._active_actor(connection, actor_user_id)
            if (
                connection.execute(
                    "SELECT COUNT(*) FROM life_records WHERE user_id = ?", (actor_user_id,)
                ).fetchone()[0]
                > MAX_MIRRORED_LIFE_RECORDS
            ):
                raise RpcError(413, "snapshot_requires_pagination")
            rows = connection.execute(
                "SELECT * FROM conversations WHERE user_id = ? ORDER BY updated_at DESC, id DESC",
                (actor_user_id,),
            ).fetchall()
            if (
                len(rows) > MAX_MIRRORED_CONVERSATIONS
                or connection.execute(
                    "SELECT COUNT(*) FROM messages m "
                    "JOIN conversations c ON c.id=m.conversation_id "
                    "WHERE c.user_id = ?",
                    (actor_user_id,),
                ).fetchone()[0]
                > MAX_MIRRORED_MESSAGES
            ):
                raise RpcError(413, "snapshot_requires_pagination")
            conversations = [conversation_from_row(row) for row in rows]
            excluded = {
                "chat_attachments": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM chat_attachments a "
                        "JOIN messages m ON m.id=a.message_id "
                        "JOIN conversations c ON c.id=m.conversation_id WHERE c.user_id = ?",
                        (actor_user_id,),
                    ).fetchone()[0]
                ),
                "user_files": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM user_files WHERE user_id = ?",
                        (actor_user_id,),
                    ).fetchone()[0]
                ),
                "report_files": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM medical_reports WHERE user_id = ?",
                        (actor_user_id,),
                    ).fetchone()[0]
                ),
            }
        role = actor["role_code"]
        appointments = management.list_visit_tasks(actor_user_id, limit=500)
        if len(appointments) >= 500:
            raise RpcError(413, "snapshot_requires_pagination")
        resources = {
            "profile": health.get_profile(actor_user_id) if role == "member" else {},
            "preferences": {
                key: value
                for key, value in self.services["preferences"].get(actor_user_id).items()
                if key != "avatar_path"
            },
            "life_records": (
                health.list_life_records(actor_user_id, limit=MAX_MIRRORED_LIFE_RECORDS)
                if role == "member"
                else []
            ),
            "reminders": health.list_reminders(actor_user_id) if role == "member" else [],
            "conversations": conversations,
            "messages": {
                str(item.id): self.services["chat"].list_messages(
                    actor_user_id, conversation_id=item.id
                )
                for item in conversations
            },
            "service_summary": (
                health.get_service_summary(actor_user_id) if role == "member" else {}
            ),
            "appointments": appointments,
            "members": management.list_members(actor_user_id),
            "advisors": management.list_advisors(actor_user_id) if role == "operator" else [],
            "work_statistics": (
                management.get_advisor_work_statistics(actor_user_id) if role == "operator" else {}
            ),
        }
        content = _json_bytes({"resources": resources, "excluded": excluded})
        if len(content) > SNAPSHOT_LIMIT:
            raise RpcError(413, "snapshot_requires_pagination")
        return {
            "schema_version": 1,
            "server_instance_id": self.instance_id,
            "actor_id": actor_user_id,
            "revision": hashlib.sha256(content).hexdigest(),
            "complete": True,
            "resources": resources,
            "excluded": excluded,
        }

    def import_member(self, actor_user_id: int, manifest: dict) -> dict:
        """First migration only. Source data stays untouched; no server-side merge."""
        self._validate_manifest(manifest)
        health = self.services["health"]
        preferences = self.services["preferences"]
        with self.database.transaction() as connection:
            actor = self.services["service_management"]._active_actor(connection, actor_user_id)
            if actor["role_code"] != "member":
                raise RpcError(403, "member_migration_only")
            _check_mirror_capacity(
                manifest,
                connection.execute(
                    "SELECT COUNT(*) FROM conversations WHERE user_id = ?", (actor_user_id,)
                ).fetchone()[0],
            )
            for statement in (
                "SELECT 1 FROM life_records WHERE user_id = ? LIMIT 1",
                "SELECT 1 FROM reminders WHERE user_id = ? LIMIT 1",
                "SELECT 1 FROM messages m JOIN conversations c ON c.id=m.conversation_id "
                "WHERE c.user_id = ? LIMIT 1",
                "SELECT 1 FROM audit_logs WHERE actor_user_id = ? "
                "AND action = 'member.migrated' LIMIT 1",
            ):
                if connection.execute(statement, (actor_user_id,)).fetchone():
                    raise RpcError(409, "migration_requires_empty_account")
            profile = manifest["profile"]
            if profile is not None:
                current = health.get_profile(actor_user_id)
                if any(current.get(key) not in (None, "", value) for key, value in profile.items()):
                    raise RpcError(409, "profile_conflict")
                health.save_profile(actor_user_id, profile)
            patch = manifest["preferences"]
            if patch is not None:
                saved = connection.execute(
                    "SELECT preferences_json FROM user_preferences WHERE user_id = ?",
                    (actor_user_id,),
                ).fetchone()
                previous = json.loads(saved["preferences_json"]) if saved else {}
                if any(key in previous and previous[key] != value for key, value in patch.items()):
                    raise RpcError(409, "preferences_conflict")
                preferences.update(actor_user_id, patch)
            mapping = {"life_records": {}, "reminders": {}, "conversations": {}, "messages": {}}
            for item in manifest["life_records"]:
                mapping["life_records"][item["source_id"]] = health.add_life_record(
                    actor_user_id, **item["values"]
                )
            for item in manifest["reminders"]:
                values = dict(item["values"])
                enabled = values.pop("enabled")
                target = health.add_reminder(actor_user_id, **values)
                if not enabled:
                    health.pause_reminder(actor_user_id, target)
                mapping["reminders"][item["source_id"]] = target
            for item in manifest["conversations"]:
                conversation = self.services["chat"].create_conversation(
                    actor_user_id, item["title"]
                )
                mapping["conversations"][item["source_id"]] = conversation.id
                for sequence, message in enumerate(item["messages"], 1):
                    created = _timestamp(message["created_at"])
                    cursor = connection.execute(
                        "INSERT INTO messages "
                        "(conversation_id, sequence_no, role, content, status, "
                        "provider, model, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            conversation.id,
                            sequence,
                            message["role"],
                            message["content"],
                            message["status"],
                            message["provider"],
                            message["model"],
                            created,
                            created,
                        ),
                    )
                    mapping["messages"][message["source_id"]] = int(cursor.lastrowid)
            self.services["service_management"]._audit(
                connection,
                actor_user_id,
                "member.migrated",
                "user",
                actor_user_id,
                {
                    "source_id": manifest["source_id"],
                    "counts": {k: len(v) for k, v in mapping.items()},
                },
            )
            result = {
                "status": "imported",
                "actor_id": actor_user_id,
                "server_instance_id": self.instance_id,
                "id_mapping": mapping,
                "excluded": manifest["excluded"],
                "source_preserved": True,
            }
            # RpcDispatcher stores the result atomically; refuse before committing
            # if the bounded idempotency journal cannot retain the full outcome.
            if len(_json_bytes(result)) > 120 * 1024:
                raise RpcError(413, "migration_requires_smaller_batch")
            return result

    @staticmethod
    def _validate_manifest(manifest):
        _keys(
            manifest,
            {
                "schema_version",
                "source_id",
                "source_actor_id",
                "profile",
                "preferences",
                "life_records",
                "reminders",
                "conversations",
                "excluded",
            },
        )
        if len(_json_bytes(manifest)) > MANIFEST_LIMIT:
            raise RpcError(413, "migration_too_large")
        if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
            raise ValueError("Manifest version invalid")
        if (
            type(manifest["source_id"]) is not str
            or str(UUID(manifest["source_id"])) != manifest["source_id"]
        ):
            raise ValueError("Source identity invalid")
        if type(manifest["source_actor_id"]) is not int or manifest["source_actor_id"] <= 0:
            raise ValueError("Source account invalid")
        profile = manifest["profile"]
        if profile is not None and (type(profile) is not dict or set(profile) - PROFILE_FIELDS):
            raise ValueError("Profile fields invalid")
        prefs = manifest["preferences"]
        if prefs is not None:
            if type(prefs) is not dict or set(prefs) - PREFERENCE_FIELDS:
                raise ValueError("Preferences fields invalid")
            PreferencesService.validate(prefs)
        _keys(manifest["excluded"], EXCLUDED_FIELDS)
        if any(type(v) is not int or not 0 <= v < 2**31 for v in manifest["excluded"].values()):
            raise ValueError("Excluded count invalid")
        all_message_ids = set()
        for resource in ("life_records", "reminders", "conversations"):
            if type(manifest[resource]) is not list or len(manifest[resource]) > 2000:
                raise ValueError("Too many import rows")
            ids = set()
            for item in manifest[resource]:
                _keys(
                    item,
                    {"source_id", "title", "messages"}
                    if resource == "conversations"
                    else {"source_id", "values"},
                )
                source = _source_id(item["source_id"])
                if source in ids:
                    raise ValueError("Repeated source identity")
                ids.add(source)
                if resource == "life_records":
                    _keys(
                        item["values"], {"category", "occurred_at", "content", "details", "source"}
                    )
                    if item["values"]["source"] != "import":
                        raise ValueError("Migration provenance invalid")
                    _timestamp(item["values"]["occurred_at"])
                elif resource == "reminders":
                    _keys(item["values"], {"title", "reminder_type", "scheduled_at", "enabled"})
                    if type(item["values"]["enabled"]) is not bool:
                        raise ValueError("Reminder state invalid")
                    _timestamp(item["values"]["scheduled_at"])
                else:
                    if type(item["messages"]) is not list:
                        raise ValueError("Invalid messages")
                    if len(item["messages"]) > MAX_MIRRORED_MESSAGES:
                        raise RpcError(413, "migration_exceeds_snapshot_capacity")
                    for message in item["messages"]:
                        _keys(
                            message,
                            {
                                "source_id",
                                "role",
                                "content",
                                "status",
                                "created_at",
                                "provider",
                                "model",
                            },
                        )
                        identifier = _source_id(message["source_id"])
                        if identifier in all_message_ids:
                            raise ValueError("Repeated message identity")
                        all_message_ids.add(identifier)
                        if message["role"] not in {"user", "assistant"} or message[
                            "status"
                        ] not in {"complete", "error"}:
                            raise ValueError("Message role/state invalid")
                        if type(message["content"]) is not str or len(message["content"]) > 100000:
                            raise ValueError("Message content invalid")
                        for field in ("provider", "model"):
                            if message[field] is not None and (
                                type(message[field]) is not str or len(message[field]) > 120
                            ):
                                raise ValueError("Message model invalid")
                        _timestamp(message["created_at"])
        _check_mirror_capacity(manifest)
