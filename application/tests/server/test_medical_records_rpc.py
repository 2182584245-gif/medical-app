"""Six-category RPC and atomic multi-delete, synthetic users only."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from platform_test_support import SyntheticDatabase

from ollama_chat_app.services.cloud_rpc_codec import decode_rpc, encode_rpc
from ollama_chat_app.services.health import (
    HealthRecordNotFoundError,
    HealthService,
    HealthValidationError,
    ReminderNotFoundError,
)
from server.platform_rpc import RpcDispatcher, RpcError


def payload(*args, **kwargs):
    return {
        "args": encode_rpc(list(args)),
        "kwargs": encode_rpc(kwargs),
        "request_id": str(uuid4()),
    }


@pytest.fixture
def context():
    database = SyntheticDatabase()
    actor = database.account("synthetic-medical-rpc-owner")
    other = database.account("synthetic-medical-rpc-other")
    service = HealthService(database)
    dispatcher = RpcDispatcher(database, {"health": service})
    yield database, actor, other, service, dispatcher
    database.close()


def test_medical_rpc_same_uuid_only_one_record_and_fact_details(context):
    database, actor, other, service, dispatcher = context
    request = payload(
        actor,
        "medical",
        datetime.now(UTC),
        "合成既有看病事实",
        details={"event_type": "consultation", "name": "示例门诊", "description": "已就诊"},
    )
    first = dispatcher.invoke(actor, "health", "add_life_record", request)
    assert dispatcher.invoke(actor, "health", "add_life_record", request) == first
    assert len(service.list_life_records(actor)) == 1
    assert service.list_life_records(other) == []
    assert database.scalar("SELECT count(*) FROM rpc_requests") == 1
    assert type(decode_rpc(first["result"])) is int
    assert service.list_life_records(actor)[0]["details"]["event_type"] == "consultation"


@pytest.mark.parametrize("kind", ["record", "reminder"])
def test_batch_delete_checks_all_owners_atomically_and_replay_is_safe(context, kind):
    database, actor, other, service, dispatcher = context
    if kind == "record":
        own = service.add_life_record(actor, "medical", datetime.now(UTC), "合成事实")
        foreign = service.add_life_record(other, "environment", datetime.now(UTC), "合成事实")
        method, table, error = "delete_life_records", "life_records", HealthRecordNotFoundError
    else:
        own = service.add_reminder(actor, "合成喝水", datetime.now(UTC))
        foreign = service.add_reminder(other, "合成喝水", datetime.now(UTC))
        method, table, error = "delete_reminders", "reminders", ReminderNotFoundError
    before_audits = database.scalar("SELECT count(*) FROM audit_logs")
    with pytest.raises(error):
        dispatcher.invoke(actor, "health", method, payload(actor, [own, foreign]))
    assert database.scalar("SELECT count(*) FROM " + table) == 2
    assert database.scalar("SELECT count(*) FROM audit_logs") == before_audits
    assert database.scalar("SELECT count(*) FROM rpc_requests") == 0
    request = payload(actor, [own])
    first = dispatcher.invoke(actor, "health", method, request)
    assert dispatcher.invoke(actor, "health", method, request) == first
    assert decode_rpc(first["result"])["count"] == 1
    assert database.scalar("SELECT count(*) FROM " + table) == 1
    assert database.scalar("SELECT user_id FROM " + table) == other
    assert database.scalar("SELECT count(*) FROM audit_logs") == before_audits + 1


@pytest.mark.parametrize("identifiers", [[], [True], [1, 1], list(range(1, 102))])
def test_batch_bounds_reject_before_mutation(context, identifiers):
    database, actor, _, _, dispatcher = context
    with pytest.raises(HealthValidationError):
        dispatcher.invoke(actor, "health", "delete_life_records", payload(actor, identifiers))
    assert database.scalar("SELECT count(*) FROM rpc_requests") == 0


def test_batch_actor_forgery_rejected(context):
    _, actor, other, _, dispatcher = context
    with pytest.raises(RpcError) as error:
        dispatcher.invoke(actor, "health", "delete_reminders", payload(other, [1]))
    assert error.value.status == 403
