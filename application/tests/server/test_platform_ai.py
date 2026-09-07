from __future__ import annotations

import json
from datetime import timedelta

import pytest
from platform_test_support import PEPPER, SyntheticDatabase
from test_platform_rpc import payload

from ollama_chat_app.data.database import timestamp_to_db, utc_now
from ollama_chat_app.services.ai_assistant import AiAssistantError, AiValidationError
from ollama_chat_app.services.cloud_rpc_codec import decode_rpc
from ollama_chat_app.services.health import HealthService
from ollama_chat_app.services.service_management import ServiceManagementService
from server.platform_ai import PlatformAIService
from server.platform_rpc import RpcDispatcher


@pytest.fixture
def context():
    database = SyntheticDatabase()
    member = database.account("ai-member")
    other = database.account("ai-other")
    operator = database.account("ai-operator", "operator")
    advisor = database.account("ai-advisor", "advisor")
    health = HealthService(database)
    service = PlatformAIService(database, health, PEPPER)
    manager = ServiceManagementService(database)
    manager.bind_advisor(operator, member, advisor)
    dispatcher = RpcDispatcher(database, {"ai": service})
    yield database, member, other, operator, advisor, service, manager, dispatcher
    database.close()


def prepare(service, member):
    return service.prepare_member_draft(member, "deepseek", "synthetic-model", "请整理近期生活记录")


def raw_life():
    return json.dumps(
        {
            "answer": "可以先保存为待确认的饮水记录。",
            "proposals": [
                {
                    "type": "life_record",
                    "category": "water",
                    "occurred_at": timestamp_to_db(utc_now()),
                    "content": "喝了一杯水",
                    "details": {"amount_ml": 200},
                }
            ],
        },
        ensure_ascii=False,
    )


def raw_summary():
    today = utc_now().date()
    return json.dumps(
        {
            "answer": "当前生活记录较少。",
            "proposals": [
                {
                    "type": "advisor_summary",
                    "period_start": (today - timedelta(days=7)).isoformat(),
                    "period_end": today.isoformat(),
                    "content": "暂无足够近期生活记录，后续可补充。",
                }
            ],
        },
        ensure_ascii=False,
    )


def test_prepare_only_returns_signed_context_no_generation_or_db_writes(context):
    database, member, _, _, _, service, _, _ = context
    result = prepare(service, member)
    assert set(result) == {"ticket", "model", "messages"}
    assert [message["role"] for message in result["messages"]] == ["system", "user"]
    assert "北京时间" in result["messages"][0]["content"]
    assert PEPPER not in str(result)
    assert database.scalar("SELECT COUNT(*) FROM ai_insights") == 0
    assert database.scalar("SELECT COUNT(*) FROM life_records") == 0


def test_finish_creates_draft_only_then_member_explicitly_confirms_once(context):
    database, member, _, _, _, service, _, dispatcher = context
    ticket = prepare(service, member)["ticket"]
    draft = service.finish_member_draft(member, ticket, raw_life())
    assert draft["status"] == "draft"
    assert database.scalar("SELECT COUNT(*) FROM life_records") == 0
    request = payload(member, draft["id"], 1)
    first = dispatcher.invoke(member, "ai", "confirm_proposal", request)
    second = dispatcher.invoke(member, "ai", "confirm_proposal", request)
    assert first == second
    assert database.scalar("SELECT COUNT(*) FROM life_records") == 1
    assert decode_rpc(first["result"])["evidence"]["proposals"][0]["status"] == "accepted"


@pytest.mark.parametrize("change", ["data", "signature", "unicode", "wrong-kind", "cross-user"])
def test_ticket_tampering_cross_user_or_wrong_flow_rejected(context, change):
    database, member, other, _, _, service, _, _ = context
    ticket = prepare(service, member)["ticket"]
    actor = member
    if change == "data":
        ticket = ("B" if ticket[0] != "B" else "A") + ticket[1:]
    elif change == "signature":
        ticket = ticket[:-1] + ("0" if ticket[-1] != "0" else "1")
    elif change == "unicode":
        ticket = ticket.rsplit(".", 1)[0] + ".无效"
    elif change == "cross-user":
        actor = other
    with pytest.raises(AiValidationError):
        if change == "wrong-kind":
            service.finish_advisor_summary(actor, ticket, raw_summary())
        else:
            service.finish_member_draft(actor, ticket, raw_life())
    assert database.scalar("SELECT COUNT(*) FROM ai_insights") == 0


def test_ticket_expires_at_exact_boundary(context, monkeypatch):
    _, member, _, _, _, service, _, _ = context
    monkeypatch.setattr("server.platform_ai.time.time", lambda: 1000)
    ticket = prepare(service, member)["ticket"]
    monkeypatch.setattr("server.platform_ai.time.time", lambda: 1600)
    with pytest.raises(AiValidationError, match="过期"):
        service.finish_member_draft(member, ticket, raw_life())


@pytest.mark.parametrize("state", ["feature-disabled", "account-disabled"])
def test_current_state_rechecked_after_external_generation(context, state):
    database, member, _, operator, _, service, manager, _ = context
    ticket = prepare(service, member)["ticket"]
    if state == "feature-disabled":
        service.update_ai_config(operator, {"member_assistant_enabled": False})
    else:
        manager.set_account_enabled(operator, member, enabled=False)
    with pytest.raises(AiAssistantError):
        service.finish_member_draft(member, ticket, raw_life())
    assert database.scalar("SELECT COUNT(*) FROM ai_insights") == 0


def test_advisor_binding_revocation_invalidates_prepared_ticket(context):
    database, member, _, operator, advisor, service, manager, _ = context
    prepared = service.prepare_advisor_summary(advisor, member, "deepseek", "synthetic-model")
    replacement = database.account("replacement-advisor", "advisor")
    manager.bind_advisor(operator, member, replacement)
    with pytest.raises(AiAssistantError):
        service.finish_advisor_summary(advisor, prepared["ticket"], raw_summary())
    assert database.scalar("SELECT COUNT(*) FROM advisor_summaries") == 0


def test_advisor_summary_stays_draft_until_bound_advisor_confirms(context):
    database, member, _, _, advisor, service, _, dispatcher = context
    ticket = service.prepare_advisor_summary(advisor, member, "deepseek", "synthetic-model")[
        "ticket"
    ]
    draft = service.finish_advisor_summary(advisor, ticket, raw_summary())
    assert database.scalar("SELECT source FROM advisor_summaries") == "ai_draft"
    with pytest.raises(AiAssistantError):
        service.confirm_proposal(member, draft["id"], 1)
    dispatcher.invoke(advisor, "ai", "confirm_proposal", payload(advisor, draft["id"], 1))
    assert database.scalar("SELECT source FROM advisor_summaries") == "ai_confirmed"


def test_confirmation_is_atomic_when_final_resolution_fails(context, monkeypatch):
    database, member, _, _, _, service, _, dispatcher = context
    draft = service.finish_member_draft(member, prepare(service, member)["ticket"], raw_life())

    def fail_resolution(*_args, **_kwargs):
        raise RuntimeError("synthetic final-substep failure")

    monkeypatch.setattr(service, "_resolve_proposal", fail_resolution)
    with pytest.raises(RuntimeError):
        dispatcher.invoke(member, "ai", "confirm_proposal", payload(member, draft["id"], 1))
    assert database.scalar("SELECT COUNT(*) FROM life_records") == 0
    assert database.scalar("SELECT COUNT(*) FROM rpc_requests") == 0
    assert service.get_draft(member, draft["id"])["evidence"]["proposals"][0]["status"] == "pending"


def test_advisor_summary_insert_rolls_back_if_insight_save_fails(context, monkeypatch):
    database, member, _, _, advisor, service, _, _ = context
    ticket = service.prepare_advisor_summary(advisor, member, "deepseek", "synthetic-model")[
        "ticket"
    ]

    def fail_save(**_kwargs):
        raise RuntimeError("synthetic insight failure")

    monkeypatch.setattr(service, "_save_insight", fail_save)
    with pytest.raises(RuntimeError):
        service.finish_advisor_summary(advisor, ticket, raw_summary())
    assert database.scalar("SELECT COUNT(*) FROM advisor_summaries") == 0


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '{"answer":"保证治愈", "proposals":[]}',
        '{"answer":"a", "proposals":[], "extra":"no"}',
    ],
)
def test_unvalidated_ai_response_cannot_be_persisted(context, raw):
    database, member, _, _, _, service, _, _ = context
    with pytest.raises(AiAssistantError):
        service.finish_member_draft(member, prepare(service, member)["ticket"], raw)
    assert database.scalar("SELECT COUNT(*) FROM ai_insights") == 0


def test_had_images_is_not_coerced_from_untrusted_strings(context):
    _, member, _, _, _, service, _, _ = context
    with pytest.raises(AiValidationError):
        service.prepare_member_draft(member, "deepseek", "model", "生活记录", had_images="false")
