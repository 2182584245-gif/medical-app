"""Fixed application operations, never an arbitrary Python/SQL execution API."""

from __future__ import annotations

import hashlib
import inspect
import json
from uuid import UUID

from ollama_chat_app.data.database import timestamp_to_db, utc_now
from ollama_chat_app.services.cloud_rpc_codec import decode_rpc, encode_rpc

from .platform_accounts import advisor_term_valid
from .memory_job_ids11846 import is_internal_memory_request

# These are application contracts, not names supplied to a generic getattr API.
METHODS = {
    "auth": {"create_staff", "get_user"},
    "chat": {
        "ensure_greeting",
        "begin_message",
        "complete_message",
        "fail_message",
        "record_retrieval",
        "record_activity",
        "list_activity",
        "create_conversation",
        "delete_conversations",
        "rename_conversation",
        "get_context",
        "context_for_provider",
        "context_attachment_kinds",
        "get_conversation",
        "get_default_conversation",
        "get_message",
        "list_conversations",
        "list_messages",
        "list_messages_page",
        "list_message_attachments",
    },
    "health": {
        "add_life_record",
        "add_reminder",
        "delete_life_record",
        "delete_life_records",
        "delete_reminder",
        "delete_reminders",
        "get_dashboard",
        "get_profile",
        "get_record_statistics",
        "get_service_summary",
        "activate_membership",
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
    "forum": {"list_topics", "list_posts", "create_post", "toggle_like", "add_comment", "list_comments"},
    "sync": {
        "get_snapshot",
        "import_member",
        "apply_queued",
        "get_sync_manifest",
        "get_sync_page",
        "get_file_chunk",
    },
}
from expansion.contracts import RPC_METHODS as EXPANSION_RPC_METHODS
METHODS["expansion"] = EXPANSION_RPC_METHODS
ACTOR_ARGUMENTS = {"user_id", "actor_user_id", "advisor_user_id", "operator_user_id"}
MAX_CACHED_RESPONSE_BYTES = 128 * 1024  # matches rpc_requests database CHECK
# Audited SELECT-only methods; names alone cannot grant a read-only shortcut.
# In particular chat.list_conversations/get_conversation/get_default_conversation
# create a default conversation and MUST retain the existing writer transaction.
PURE_READ_OPERATIONS = frozenset({
    ('health','get_profile'), ('health','list_life_records'),
    ('health','get_record_statistics'), ('health','list_reminders'),
    ('chat','get_message'), ('chat','list_message_attachments'), ('chat','list_activity'),
    ('forum','list_topics'), ('forum','list_posts'), ('forum','list_comments'),
    *{('expansion',name) for name in (
        'get_settings','get_record','list_records','list_record_revisions',
        'get_observation','list_observations','get_goal','list_goals',
        'list_memories','list_points','get_growth','get_invite_code',
        'list_content','get_content','get_content_progress','list_ai_scenes',
        'list_foods','get_food','list_food_favorites','list_meals','get_meal',
    )},
})
# These methods can call the external semantic provider and can persist derived
# vectors internally. They are neither one long writer nor a SELECT-only read.
EXTERNAL_CONTEXT_OPERATIONS = frozenset({
    ('ai','build_member_context'), ('ai','prepare_member_draft'),
})


class RpcError(RuntimeError):
    def __init__(self, status: int, code: str, *, public_message: str | None = None) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.public_message = public_message


def is_mutation(method: str) -> bool:
    return not method.startswith(("get_", "list_", "context_", "build_", "prepare_", "storage_"))


class RpcDispatcher:
    def __init__(self, database, services: dict) -> None:
        self.database = database
        self.mutation_observer = None
        self.mutation_source_observer = None
        self._chat_result_reader = getattr(services.get('chat'), 'get_message', None)
        self._ai_result_validator = getattr(services.get('ai'), '_validate_legacy_rpc_result', None)
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

    def _context_actor(self, connection, actor_id):
        actor = connection.execute(
            'SELECT role_code, account_status FROM users WHERE id = ?', (actor_id,)
        ).fetchone()
        if (actor is None or actor['account_status'] != 'active'
                or not advisor_term_valid(connection,actor_id,str(actor['role_code']))):
            raise RpcError(403,'identity_unavailable')
        return str(actor['role_code'])

    def _external_context(self, actor_id, service, method, function, bound, operation_guard):
        active = getattr(self.database,'_active',None)
        if active is not None and active.get() is not None:
            raise RpcError(409,'context_transaction_already_active')
        # A short writer retains the normal current-session authorization gate;
        # release its connection and advisory lock before any provider request.
        with self.database.transaction() as connection:
            role = self._context_actor(connection,actor_id)
        value = function(*bound.args, **bound.kwargs)
        with self.database.transaction() as connection:
            if self._context_actor(connection,actor_id) != role:
                raise RpcError(403,'identity_unavailable')
            if operation_guard is not None:
                operation_guard(service,method,bound.arguments)
            if callable(self._ai_result_validator):
                self._ai_result_validator(actor_id,method,value)
        return {'result':encode_rpc(value)}

    def invoke(
        self, actor_id: int, service: str, method: str, payload: dict, *, operation_guard=None
    ):
        operation = self.operations.get((service, method))
        if operation is None:
            raise RpcError(404, "operation_not_found")
        if not isinstance(payload, dict) or set(payload) != {"args", "kwargs", "request_id"}:
            raise RpcError(422, "invalid_request")
        try:
            if type(payload["request_id"]) is not str or len(payload["request_id"]) != 36:
                raise ValueError
            request_id = str(UUID(payload["request_id"]))
            if is_internal_memory_request(request_id):
                raise ValueError('reserved_internal_receipt')
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

        if operation_guard is not None:
            operation_guard(service, method, bound.arguments)
        if (service,method) in EXTERNAL_CONTEXT_OPERATIONS:
            return self._external_context(actor_id,service,method,function,bound,operation_guard)
        mutation = is_mutation(method)
        # Only proven pure reads use MVCC, without queuing behind another
        # account's mutation. All writes/lazy initialization retain their existing
        # atomicity, global write lock and immutable idempotency receipts.
        scope=(getattr(self.database,'snapshot',self.database.connect)
               if (service,method) in PURE_READ_OPERATIONS else self.database.transaction)
        with scope() as connection:
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
            if (service, method) == ("health", "activate_membership"):
                # Membership grants stay staff-controlled. Do not expire or
                # create membership rows through the member RPC surface.
                raise RpcError(409, "feature_unavailable", public_message="暂未开放在线开通，请联系客服办理。")
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
                if cached is None and service=='expansion' and method=='update_meal':
                    from .record_assistant_try12 import draft_was_replaced
                    if draft_was_replaced(connection,actor_id,request_id):
                        raise RpcError(409,'record_draft_superseded')
                if cached is not None:
                    if cached["request_digest"] != digest:
                        raise RpcError(409, "request_conflict")
                    replay = json.loads(cached["response_json"])
                    if service == 'expansion' and method in {'create_goal','update_goal','close_goal','checkin_goal','uncheckin_goal'} and isinstance(replay,dict) and 'result' in replay:
                        saved_goal = decode_rpc(replay['result'])
                        if isinstance(saved_goal,dict):
                            saved_goal = saved_goal.get('goal',saved_goal)
                        goal_id = saved_goal.get('id') if isinstance(saved_goal,dict) else None
                        if type(goal_id)is int and connection.execute('SELECT id FROM expansion_goals WHERE user_id=? AND id=?',(actor_id,goal_id)).fetchone()is None:
                            return {'result':encode_rpc({'deleted':True,'goal_id':goal_id,'deleted_count':0,'reminder_ids':[]})}
                    if isinstance(replay, dict) and set(replay) == {'__large_chat_result_v1__'}:
                        saved = replay['__large_chat_result_v1__']
                        if (service != 'chat' or method != 'complete_message'
                                or not callable(self._chat_result_reader)
                                or type(saved) is not dict or set(saved) != {'message_id','sha256'}
                                or type(saved['message_id']) is not int
                                or bound.arguments.get('assistant_message_id') != saved['message_id']):
                            raise RpcError(409, 'mutation_result_unavailable')
                        message = self._chat_result_reader(actor_id, saved['message_id'])
                        if message is None:
                            raise RpcError(409, 'mutation_result_unavailable')
                        replay = {'result': encode_rpc(message)}
                        original = json.dumps(replay, ensure_ascii=False, separators=(',', ':'))
                        if hashlib.sha256(original.encode('utf-8')).hexdigest() != saved['sha256']:
                            raise RpcError(409, 'mutation_result_unavailable')
                    return replay
            if mutation and callable(self.mutation_source_observer):
                # Optional precise-source boundary runs while rows still exist,
                # inside the original source transaction. Legacy observers keep
                # their five arguments and post-function timing below.
                self.mutation_source_observer(connection,actor_id,service,method,
                    str(actor['role_code']),dict(bound.arguments))
            result = {"result": encode_rpc(function(*bound.args, **bound.kwargs))}
            if mutation and callable(self.mutation_observer):
                # Derived personal search content is invalidated in the same
                # transaction as its source change, before publishing a receipt.
                self.mutation_observer(connection, actor_id, service, method, str(actor['role_code']))
            if service == "service_management" and method == "reset_account_password":
                connection.execute(
                    "UPDATE platform_sessions SET revoked_at = ? WHERE user_id = ? "
                    "AND revoked_at IS NULL",
                    (timestamp_to_db(utc_now()), bound.arguments["target_user_id"]),
                )
            if mutation:
                encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                if len(encoded.encode("utf-8")) > MAX_CACHED_RESPONSE_BYTES:
                    message_id = bound.arguments.get('assistant_message_id')
                    if (service != 'chat' or method != 'complete_message'
                            or type(message_id) is not int or message_id <= 0
                            or not callable(self._chat_result_reader)
                            or len(encoded.encode('utf-8')) > 32 * 1024 * 1024):
                        raise RpcError(413, "mutation_result_too_large")
                    # Keep the original reply once in messages, not a duplicate
                    # megabyte copy in every idempotency receipt. A replay is
                    # owner-bound and must exactly match the completed result.
                    encoded = json.dumps({'__large_chat_result_v1__': {
                        'message_id':message_id,
                        'sha256':hashlib.sha256(encoded.encode('utf-8')).hexdigest(),
                    }}, separators=(',', ':'))
                connection.execute(
                    "INSERT INTO rpc_requests "
                    "(user_id, request_id, request_digest, response_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (actor_id, request_id, digest, encoded, timestamp_to_db(utc_now())),
                )
            return result
