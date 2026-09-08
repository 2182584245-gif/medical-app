"""Fixed application operations, never an arbitrary Python/SQL execution API."""

from __future__ import annotations

import hashlib
import inspect
import json
from uuid import UUID

from ollama_chat_app.data.database import timestamp_to_db, utc_now
from ollama_chat_app.services.cloud_rpc_codec import decode_rpc, encode_rpc

from .platform_accounts import advisor_term_valid

# These are application contracts, not names supplied to a generic getattr API.
METHODS = {
    "auth": {"create_staff", "get_user"},
    "chat": {
        "begin_message",
        "complete_message",
        "fail_message",
        "create_conversation",
        "rename_conversation",
        "get_context",
        "context_for_provider",
        "context_attachment_kinds",
        "get_conversation",
        "get_default_conversation",
        "get_message",
        "list_conversations",
        "list_messages",
        "list_message_attachments",
    },
    "health": {
        "add_life_record",
        "add_reminder",
        "delete_life_record",
        "delete_reminder",
        "get_dashboard",
        "get_profile",
        "get_record_statistics",
        "get_service_summary",
        "list_life_records",
        "list_reminders",
        "pause_reminder",
        "resume_reminder",
        "save_profile",
        "toggle_reminder",
        "update_reminder",
    },
    "service_management": {
        "set_advisor_validity",
        "reset_account_password",
        "request_appointment",
        "list_appointments",
        "update_appointment",
        "get_filtered_work_statistics",
        "bind_advisor",
        "complete_visit_task",
        "create_advisor",
        "create_visit_task",
        "get_advisor_work_statistics",
        "get_dashboard",
        "get_member_overview",
        "list_advisors",
        "list_members",
        "list_visit_records",
        "list_visit_tasks",
        "save_advisor_profile",
        "save_membership",
        "set_account_enabled",
        "start_visit_task",
    },
    "commerce": {
        "list_cart",
        "set_cart_quantity",
        "add_to_cart",
        "list_favorites",
        "set_favorite",
        "checkout_cart",
        "create_order",
        "create_product",
        "delete_product",
        "list_orders",
        "list_products",
        "list_recommendations",
        "mark_interested",
        "mark_order_delivered",
        "mark_product_interested",
        "mark_recommendation_interested",
        "recommend_product",
        "record_product_view",
        "reorder",
        "reorder_order",
        "set_product_active",
        "simulate_purchase",
        "update_product",
    },
    "files": {
        "upload_bytes",
        "get_file",
        "list_files",
        "delete_file",
        "storage_usage",
        "add_report_item_draft",
        "archive_report",
        "confirm_report",
        "confirm_report_item",
        "create_report_draft",
        "delete_report",
        "get_report",
        "list_reports",
    },
    "ai": {
        "build_member_context",
        "confirm_proposal",
        "dismiss_proposal",
        "get_ai_config",
        "get_draft",
        "list_drafts",
        "update_ai_config",
        "prepare_member_draft",
        "finish_member_draft",
        "prepare_advisor_summary",
        "finish_advisor_summary",
    },
    "preferences": {"get_preferences", "update_preferences"},
    "sync": {"get_snapshot", "import_member", "apply_queued", "get_sync_manifest",
             "get_sync_page", "get_file_chunk"},
}
ACTOR_ARGUMENTS = {"user_id", "actor_user_id", "advisor_user_id", "operator_user_id"}
MAX_CACHED_RESPONSE_BYTES = 128 * 1024  # matches rpc_requests database CHECK


class RpcError(RuntimeError):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def is_mutation(method: str) -> bool:
    return not method.startswith(("get_", "list_", "context_", "build_", "prepare_", "storage_"))


class RpcDispatcher:
    def __init__(self, database, services: dict) -> None:
        self.database = database
        self.operations = {}
        for service_name, instance in services.items():
            for method_name in METHODS.get(service_name, ()):
                function = getattr(instance, method_name, None)
                if callable(function):
                    signature = inspect.signature(function)
                    first = next(iter(signature.parameters), None)
                    if first not in ACTOR_ARGUMENTS:
                        raise RuntimeError("RPC operation has no explicit acting user")
                    self.operations[(service_name, method_name)] = (function, signature, first)

    def invoke(self, actor_id: int, service: str, method: str, payload: dict):
        operation = self.operations.get((service, method))
        if operation is None:
            raise RpcError(404, "operation_not_found")
        if not isinstance(payload, dict) or set(payload) != {"args", "kwargs", "request_id"}:
            raise RpcError(422, "invalid_request")
        try:
            if type(payload["request_id"]) is not str or len(payload["request_id"]) != 36:
                raise ValueError
            request_id = str(UUID(payload["request_id"]))
            args, kwargs = decode_rpc(payload["args"]), decode_rpc(payload["kwargs"])
            if not isinstance(args, list) or not isinstance(kwargs, dict):
                raise ValueError
            function, signature, actor_field = operation
            bound = signature.bind(*args, **kwargs)
            supplied_actor = bound.arguments.get(actor_field)
            if supplied_actor is not None and (
                type(supplied_actor) is not int or supplied_actor != actor_id
            ):
                raise RpcError(403, "identity_mismatch")
            # The authenticated identity is authoritative, including optional actor arguments.
            bound.arguments[actor_field] = actor_id
        except RpcError:
            raise
        except (TypeError, ValueError, KeyError):
            raise RpcError(422, "invalid_request") from None

        mutation = is_mutation(method)
        # A single operation is one database transaction, including legacy service substeps.
        # PlatformDatabase serializes its write transactions for this small test deployment.
        with self.database.transaction() as connection:
            # Reads and cached mutation responses both require current account
            # state; a valid token resolved earlier is insufficient after expiry.
            actor = connection.execute(
                "SELECT role_code, account_status FROM users WHERE id = ?", (actor_id,)
            ).fetchone()
            if (
                actor is None
                or actor["account_status"] != "active"
                or not advisor_term_valid(connection, actor_id, str(actor["role_code"]))
            ):
                raise RpcError(403, "identity_unavailable")
            if mutation:
                # Cache hits must not bypass a subsequent role change or revoked
                # advisor binding. Read current scope inside the write lock, not
                # from caller input or the earlier token-resolution snapshot.
                bindings = connection.execute(
                    "SELECT id, member_user_id, advisor_user_id FROM advisor_bindings "
                    "WHERE status = 'active' AND (member_user_id = ? OR advisor_user_id = ?) "
                    "ORDER BY id",
                    (actor_id, actor_id),
                ).fetchall()
                scope = {
                    "role": actor["role_code"],
                    "bindings": [
                        [row["id"], row["member_user_id"], row["advisor_user_id"]]
                        for row in bindings
                    ],
                }
                digest = hashlib.sha256(
                    json.dumps(
                        {
                            "service": service,
                            "method": method,
                            "args": payload["args"],
                            "kwargs": payload["kwargs"],
                            "authorization_scope": scope,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                ).hexdigest()
                cached = connection.execute(
                    "SELECT request_digest, response_json FROM rpc_requests "
                    "WHERE user_id = ? AND request_id = ?",
                    (actor_id, request_id),
                ).fetchone()
                if cached is not None:
                    if cached["request_digest"] != digest:
                        raise RpcError(409, "request_conflict")
                    return json.loads(cached["response_json"])
            result = {"result": encode_rpc(function(*bound.args, **bound.kwargs))}
            if service == "service_management" and method == "reset_account_password":
                connection.execute(
                    "UPDATE platform_sessions SET revoked_at = ? WHERE user_id = ? "
                    "AND revoked_at IS NULL",
                    (timestamp_to_db(utc_now()), bound.arguments["target_user_id"]),
                )
            if mutation:
                encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                if len(encoded.encode("utf-8")) > MAX_CACHED_RESPONSE_BYTES:
                    raise RpcError(413, "mutation_result_too_large")
                connection.execute(
                    "INSERT INTO rpc_requests "
                    "(user_id, request_id, request_digest, response_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (actor_id, request_id, digest, encoded, timestamp_to_db(utc_now())),
                )
            return result
