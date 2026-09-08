"""Stateless scoped pages: recheck versions and permissions on every request."""
from __future__ import annotations

import hashlib
import hmac
from itertools import islice

from ollama_chat_app.data.database import conversation_from_row
from ollama_chat_app.services.cloud_rpc_codec import encode_rpc
from ollama_chat_app.services.sync_protocol import (
    PAGE_BYTES,
    PAGE_ITEMS,
    RESOURCE_KINDS,
    json_bytes,
    resource_digest,
    resource_records,
)

from .platform_rpc import RpcError

CHUNK_BYTES = 64 * 1024


class SyncPages:
    def __init__(self, sync):
        self.sync = sync
        self.database, self.services = sync.database, sync.services

    def _resource(self, actor_id, name):
        if name not in RESOURCE_KINDS:
            raise RpcError(422, "unsupported_sync_resource")
        health, management = self.services["health"], self.services["service_management"]
        with self.database.connect() as connection:
            actor = management._active_actor(connection, actor_id)
            role = actor["role_code"]
            if name == "preferences":
                return {key: value for key, value in self.services["preferences"].get(
                    actor_id).items() if key != "avatar_path"}
            if name in {"profile", "life_records", "reminders", "service_summary"}:
                if role != "member":
                    return {} if RESOURCE_KINDS[name] == "dict" else []
                if name == "life_records":
                    rows = connection.execute(
                        "SELECT * FROM life_records WHERE user_id=? "
                        "ORDER BY occurred_at DESC,id DESC", (actor_id,),
                    ).fetchall()
                    return [health._life_record_from_row(row) for row in rows]
                return getattr(health, {"profile": "get_profile", "reminders": "list_reminders",
                                        "service_summary": "get_service_summary"}[name])(actor_id)
            if name in {"conversations", "messages"}:
                rows = connection.execute(
                    "SELECT * FROM conversations WHERE user_id=? ORDER BY updated_at DESC,id DESC",
                    (actor_id,),
                ).fetchall()
                conversations = [conversation_from_row(row) for row in rows]
                if name == "conversations":
                    return conversations
                return {str(item.id): self.services["chat"].list_messages(
                    actor_id, conversation_id=item.id) for item in conversations}
            if name == "members":
                return management.list_members(actor_id)
            if name == "advisors":
                return management.list_advisors(actor_id) if role == "operator" else []
            if name == "work_statistics":
                return (management.get_advisor_work_statistics(actor_id)
                        if role == "operator" else {})
            if name in {"appointments", "visit_records"}:
                return self._visits(connection, actor_id, role, records=name == "visit_records")
            if name in {"user_files", "reports"}:
                if role != "member" or "files" not in self.services:
                    return []
                files = self.services["files"]
                if name == "user_files":
                    return files.list_files(actor_id)
                return [files.get_report(actor_id, item["id"]) for item in
                        files.list_reports(actor_id, include_archived=True)]
            rows = connection.execute(
                "SELECT a.id,a.message_id,a.original_name,a.media_type,length(a.content) "
                "AS size_bytes,a.sha256,a.extraction_method,a.created_at "
                "FROM chat_attachments a JOIN messages m ON m.id=a.message_id "
                "JOIN conversations c ON c.id=m.conversation_id "
                "WHERE c.user_id=? ORDER BY a.id", (actor_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def _visits(self, connection, actor_id, role, *, records):
        alias, table = ("r", "visit_records") if records else ("t", "visit_tasks")
        params, clauses = [], []
        if role == "member":
            clauses.append(f"{alias}.member_user_id=?")
            params.append(actor_id)
        elif role == "advisor":
            clauses.append(f"{alias}.advisor_user_id=?")
            params.append(actor_id)
            # Removed bindings must also remove historical private data from exports.
            clauses.append(f"EXISTS(SELECT 1 FROM advisor_bindings b WHERE "
                           f"b.member_user_id={alias}.member_user_id "
                           "AND b.advisor_user_id=? AND b.status='active')")
            params.append(actor_id)
        where = " AND ".join(clauses) if clauses else "1=1"
        fields = (
            "r.id,r.task_id,r.member_user_id,r.advisor_user_id,r.visited_at,r.summary,"
            "r.details_json,r.created_at,r.updated_at"
            if records else
            "t.id,t.member_user_id,t.advisor_user_id,t.title,t.scheduled_at,"
            "COALESCE(d.outcome_status,t.status) AS status,t.notes,t.created_at,t.updated_at,"
            "COALESCE(d.service_type,t.title) AS service_type,d.address,d.latitude,d.longitude,"
            "d.requested_by"
        )
        details = "" if records else "LEFT JOIN visit_task_details d ON d.task_id=t.id "
        order = "r.visited_at DESC,r.id DESC" if records else (
            "CASE t.status WHEN 'in_progress' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,"
            "CASE WHEN t.scheduled_at IS NULL THEN 1 ELSE 0 END,t.scheduled_at,t.id"
        )
        rows = connection.execute(
            f"SELECT {fields},COALESCE(mp.display_name,member.username) AS member_name,"
            "COALESCE(ap.display_name,advisor.username) AS advisor_name "
            f"FROM {table} {alias} {details}"
            f"JOIN users member ON member.id={alias}.member_user_id "
            f"LEFT JOIN member_profiles mp ON mp.user_id={alias}.member_user_id "
            f"LEFT JOIN users advisor ON advisor.id={alias}.advisor_user_id "
            f"LEFT JOIN advisor_profiles ap ON ap.user_id={alias}.advisor_user_id "
            f"WHERE {where} ORDER BY {order}", params,
        ).fetchall()
        management = self.services["service_management"]
        convert = management._visit_record_from_row if records else management._visit_task_from_row
        return [convert(row) for row in rows]

    def manifest(self, actor_id):
        descriptions = {name: resource_digest(name, self._resource(actor_id, name))
                        for name in RESOURCE_KINDS}
        return {"schema_version": 2, "server_instance_id": self.sync.instance_id,
                "actor_id": actor_id,
                "revision": hashlib.sha256(json_bytes(descriptions)).hexdigest(),
                "resources": descriptions}

    def page(self, actor_id, resource, version, offset):
        if type(offset) is not int or offset < 0 or type(version) is not str:
            raise RpcError(422, "invalid_page")
        value = self._resource(actor_id, resource)
        description = resource_digest(resource, value)
        if not hmac.compare_digest(version, description["version"]):
            raise RpcError(409, "sync_resource_changed")
        if offset >= description["count"]:
            raise RpcError(422, "page_out_of_range")
        items, used = [], 0
        for record in islice(resource_records(resource, value), offset, offset + PAGE_ITEMS):
            size = len(json_bytes(encode_rpc(record)))
            if used + size > PAGE_BYTES and items:
                break
            used += size
            items.append(record)
        return {"resource": resource, "version": version, "offset": offset,
                "next_offset": offset + len(items), "items": items}

    def chunk(self, actor_id, kind, identifier, digest, offset):
        if (kind not in {"user_files", "chat_attachments"} or type(identifier) is not int
                or identifier <= 0 or type(offset) is not int or offset < 0
                or offset % CHUNK_BYTES or type(digest) is not str or len(digest) != 64):
            raise RpcError(422, "invalid_file_chunk")
        with self.database.connect() as connection:
            actor = self.services["service_management"]._active_actor(connection, actor_id)
            if kind == "user_files":
                if actor["role_code"] != "member":
                    raise RpcError(403, "member_files_only")
                row = connection.execute(
                    "SELECT c.content,f.sha256,f.size_bytes FROM user_files f "
                    "JOIN user_file_contents c ON c.file_id=f.id WHERE f.user_id=? AND f.id=?",
                    (actor_id, identifier),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT a.content,a.sha256,length(a.content) AS size_bytes "
                    "FROM chat_attachments a JOIN messages m ON m.id=a.message_id "
                    "JOIN conversations c ON c.id=m.conversation_id WHERE c.user_id=? AND a.id=?",
                    (actor_id, identifier),
                ).fetchone()
        if row is None:
            raise RpcError(404, "file_not_found")
        content = bytes(row["content"])
        if (len(content) != row["size_bytes"] or not hmac.compare_digest(row["sha256"], digest)
                or not hmac.compare_digest(hashlib.sha256(content).hexdigest(), digest)):
            raise RpcError(409, "file_content_changed")
        if offset >= len(content):
            raise RpcError(422, "chunk_out_of_range")
        block = content[offset:offset + CHUNK_BYTES]
        return {"kind": kind, "id": identifier, "sha256": digest, "size_bytes": len(content),
                "offset": offset, "next_offset": offset + len(block), "content": block}
