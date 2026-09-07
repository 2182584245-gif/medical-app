from __future__ import annotations

import sqlite3
from uuid import uuid4

import pytest
from platform_test_support import SyntheticDatabase

from ollama_chat_app.services.chat import ChatService
from ollama_chat_app.services.cloud_rpc_codec import decode_rpc, encode_rpc
from ollama_chat_app.services.health import HealthService
from server.platform_rpc import RpcDispatcher, RpcError


def payload(*args, request_id=None, **kwargs):
    return {
        "args": encode_rpc(list(args)),
        "kwargs": encode_rpc(kwargs),
        "request_id": request_id or str(uuid4()),
    }


@pytest.fixture
def context():
    database = SyntheticDatabase()
    actor = database.account("rpc-owner")
    other = database.account("rpc-other")
    dispatcher = RpcDispatcher(
        database,
        {
            "chat": ChatService(database),
            "health": HealthService(database),
        },
    )
    yield database, actor, other, dispatcher
    database.close()


@pytest.mark.parametrize(
    "service,method",
    [
        ("database", "execute"),
        ("chat", "_owned_conversation"),
        ("auth", "bootstrap_operator"),
        ("health", "__class__"),
        ("os", "system"),
    ],
)
def test_fixed_allowlist_refuses_arbitrary_operations(context, service, method):
    _, actor, _, dispatcher = context
    with pytest.raises(RpcError) as error:
        dispatcher.invoke(actor, service, method, payload(actor))
    assert error.value.status == 404


@pytest.mark.parametrize("supplied", [True, "owner", 99999])
def test_actor_forgery_rejected_before_service_work(context, supplied):
    database, actor, _, dispatcher = context
    before = database.scalar("SELECT COUNT(*) FROM conversations")
    with pytest.raises(RpcError) as error:
        dispatcher.invoke(actor, "chat", "create_conversation", payload(supplied, "forged"))
    assert error.value.status == 403
    assert database.scalar("SELECT COUNT(*) FROM conversations") == before


def test_keyword_actor_forgery_and_cross_user_resource_are_rejected(context):
    database, actor, other, dispatcher = context
    with pytest.raises(RpcError) as error:
        dispatcher.invoke(actor, "health", "get_profile", payload(user_id=other))
    assert error.value.status == 403
    other_conversation = database.scalar("SELECT id FROM conversations WHERE user_id=?", (other,))
    with pytest.raises(RuntimeError):
        dispatcher.invoke(actor, "chat", "get_conversation", payload(actor, other_conversation))


@pytest.mark.parametrize("request_id", [3, None, {}, [], True, "not-a-uuid", "0" * 36])
def test_bad_request_id_always_safe_422(context, request_id):
    _, actor, _, dispatcher = context
    request = payload(actor)
    request["request_id"] = request_id
    with pytest.raises(RpcError) as error:
        dispatcher.invoke(actor, "chat", "list_conversations", request)
    assert error.value.status == 422


def test_unknown_kwargs_or_codec_classes_do_not_execute(context):
    _, actor, _, dispatcher = context
    request = payload(actor, injected_sql="DROP TABLE users")
    with pytest.raises(RpcError) as error:
        dispatcher.invoke(actor, "chat", "list_conversations", request)
    assert error.value.status == 422
    request = payload(actor)
    request["kwargs"] = {"$__type": "SystemCommand", "fields": {"cmd": "not run"}}
    with pytest.raises(RpcError):
        dispatcher.invoke(actor, "chat", "list_conversations", request)


def test_retry_returns_same_result_and_different_payload_conflicts(context):
    database, actor, _, dispatcher = context
    request = payload(actor, "one conversation")
    first = dispatcher.invoke(actor, "chat", "create_conversation", request)
    second = dispatcher.invoke(actor, "chat", "create_conversation", request)
    assert first == second
    assert decode_rpc(first["result"]).title == "one conversation"
    assert database.scalar("SELECT COUNT(*) FROM conversations WHERE user_id=?", (actor,)) == 2
    changed = payload(actor, "changed", request_id=request["request_id"])
    with pytest.raises(RpcError) as error:
        dispatcher.invoke(actor, "chat", "create_conversation", changed)
    assert error.value.status == 409
    assert database.scalar("SELECT COUNT(*) FROM rpc_requests") == 1


def test_request_ids_are_scoped_to_authenticated_account(context):
    database, actor, other, dispatcher = context
    request_id = str(uuid4())
    first = dispatcher.invoke(
        actor, "chat", "create_conversation", payload(actor, "own", request_id=request_id)
    )
    second = dispatcher.invoke(
        other, "chat", "create_conversation", payload(other, "other", request_id=request_id)
    )
    assert decode_rpc(first["result"]).user_id == actor
    assert decode_rpc(second["result"]).user_id == other
    assert database.scalar("SELECT COUNT(*) FROM rpc_requests") == 2


def test_business_mutation_and_cache_insert_are_one_transaction(context):
    database, actor, _, dispatcher = context
    database.connection.execute(
        "CREATE TRIGGER fail_cache BEFORE INSERT ON rpc_requests "
        "BEGIN SELECT RAISE(ABORT,'synthetic cache failure'); END"
    )
    before = database.scalar("SELECT COUNT(*) FROM conversations")
    with pytest.raises(sqlite3.IntegrityError):
        dispatcher.invoke(actor, "chat", "create_conversation", payload(actor, "rolled back"))
    assert database.scalar("SELECT COUNT(*) FROM conversations") == before


def test_result_above_128k_rolls_back_all_service_substeps(context):
    database, actor, _, _ = context

    class OversizedService:
        def create_conversation(self, user_id, title):
            ChatService(database).create_conversation(user_id, title)
            return "中" * 45000  # 135 KB UTF-8, below old erroneous 256 KB threshold

    dispatcher = RpcDispatcher(database, {"chat": OversizedService()})
    before = database.scalar("SELECT COUNT(*) FROM conversations")
    with pytest.raises(RpcError) as error:
        dispatcher.invoke(actor, "chat", "create_conversation", payload(actor, "rollback"))
    assert error.value.status == 413
    assert database.scalar("SELECT COUNT(*) FROM conversations") == before
    assert database.scalar("SELECT COUNT(*) FROM rpc_requests") == 0


def test_optional_actor_none_is_replaced_with_authenticated_actor(context):
    database, actor, _, _ = context
    seen = []

    class AI:
        def get_ai_config(self, actor_user_id=None):
            seen.append(actor_user_id)
            return {"enabled": True}

    dispatcher = RpcDispatcher(database, {"ai": AI()})
    dispatcher.invoke(actor, "ai", "get_ai_config", payload(None))
    assert seen == [actor]


def test_allowed_operation_must_have_explicit_actor_argument(context):
    database, _, _, _ = context

    class Bad:
        def get_profile(self, filename):
            return filename

    with pytest.raises(RuntimeError, match="explicit acting user"):
        RpcDispatcher(database, {"health": Bad()})


@pytest.mark.parametrize("change", ["role", "binding", "disabled"])
def test_cached_result_is_not_replayed_after_current_authorization_changes(context, change):
    database, actor, other, dispatcher = context
    request = payload(actor, "cache under original scope")
    dispatcher.invoke(actor, "chat", "create_conversation", request)
    if change == "role":
        database.connection.execute("UPDATE users SET role_code='advisor' WHERE id=?", (actor,))
    elif change == "binding":
        database.connection.execute(
            "INSERT INTO advisor_bindings (member_user_id, advisor_user_id, "
            "status, created_at, updated_at) "
            "VALUES (?, ?, 'active', '2026-09-01T00:00:00.000000Z', '2026-09-01T00:00:00.000000Z')",
            (actor, other),
        )
    else:
        database.connection.execute(
            "UPDATE users SET account_status='disabled' WHERE id=?", (actor,)
        )
    with pytest.raises(RpcError) as error:
        dispatcher.invoke(actor, "chat", "create_conversation", request)
    assert error.value.status == (403 if change == "disabled" else 409)
    assert database.scalar("SELECT COUNT(*) FROM conversations WHERE user_id=?", (actor,)) == 2


def test_advisor_cache_is_invalid_after_binding_ends(context):
    database, actor, other, dispatcher = context
    database.connection.execute("UPDATE users SET role_code='advisor' WHERE id=?", (actor,))
    database.connection.execute(
        "INSERT INTO advisor_bindings (member_user_id, advisor_user_id, "
        "status, created_at, updated_at) "
        "VALUES (?, ?, 'active', '2026-09-01T00:00:00.000000Z', '2026-09-01T00:00:00.000000Z')",
        (other, actor),
    )
    request = payload(actor, "advisor cached response")
    dispatcher.invoke(actor, "chat", "create_conversation", request)
    database.connection.execute("UPDATE advisor_bindings SET status='ended'")
    with pytest.raises(RpcError) as error:
        dispatcher.invoke(actor, "chat", "create_conversation", request)
    assert error.value.status == 409
