from __future__ import annotations

import json
import threading

import pytest

from ollama_chat_app.data.database import Database
from ollama_chat_app.providers.base import ProviderError
from ollama_chat_app.providers.demo_life import DEMO_MODEL, DemoLifeProvider
from ollama_chat_app.services.ai_assistant import (
    AiAssistantService,
    AiDraftNotFoundError,
    AiPermissionError,
)
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.health import HealthService
from ollama_chat_app.services.life_agent import ConversationAgentProvider
from ollama_chat_app.services.service_management import ServiceManagementService


@pytest.fixture
def setup(tmp_path):
    db = Database(tmp_path / "agent.db")
    auth = AuthService(db)
    member = auth.register("member", "synthetic-password-123")
    return db, auth, member, HealthService(db), AiAssistantService(db)


def turn(ai, user_id, text, history=(), provider=None, cancel=None):
    agent = ConversationAgentProvider(
        provider or DemoLifeProvider(),
        history,
        {"role": "user", "content": text},
        cancel or threading.Event(),
    )
    return ai.create_member_draft(user_id, agent, "workflow_demo", DEMO_MODEL, text)


def test_records_memory_reminder_and_restart_are_real_confirmed_data(setup):
    db, _auth, member, health, ai = setup
    for text, kind in [
        ("刚才喝了300毫升水", "life_record"),
        ("今天中午吃了面和两个鸡蛋", "life_record"),
        ("我不吃香菜", "profile_fact"),
        ("明天18点提醒我散步", "reminder"),
    ]:
        draft = turn(ai, member.id, text)
        assert draft["proposals"][0]["type"] == kind
        assert draft["proposals"][0]["status"] == "pending"
        if kind == "profile_fact":
            assert ai.build_member_context(member.id)["confirmed_facts"] == {}
        if kind == "reminder":
            assert health.list_reminders(member.id) == []
        ai.confirm_proposal(member.id, draft["id"], 1)
    restarted = AiAssistantService(db)
    context = restarted.build_member_context(member.id)
    assert len(context["recent_life_records"]) == 2
    assert context["confirmed_facts"]["dietary_preferences"] == "我不吃香菜"
    assert len(context["active_reminders"]) == 1
    answer = turn(restarted, member.id, "总结最近记录")
    assert "300毫升" in answer["answer"] and "不吃香菜" in answer["answer"]
    assert answer["proposals"] == []
    assert turn(restarted, member.id, "我不吃香菜")["proposals"] == []


def test_clarification_uses_only_this_conversation(setup):
    _db, _auth, member, health, ai = setup
    first = turn(ai, member.id, "提醒我散步")
    assert first["proposals"] == []
    second = turn(
        ai,
        member.id,
        "明天18点",
        [
            {"role": "user", "content": "提醒我散步"},
            {"role": "assistant", "content": first["answer"]},
        ],
    )
    assert second["proposals"][0]["title"] == "散步"
    assert health.list_reminders(member.id) == []
    assert turn(ai, member.id, "明天18点")["proposals"] == []


@pytest.mark.parametrize(
    "text",
    [
        "如果我刚才喝了300毫升水呢？",
        "我没喝水",
        "明天25点提醒我散步",
        "明天18点提醒我吃药",
        "今天天气如何",
        "这张图是什么",
        "我胸痛喘不过气",
    ],
)
def test_demo_does_not_invent_actions(setup, text):
    _db, _auth, member, health, ai = setup
    assert turn(ai, member.id, text)["proposals"] == []
    assert health.list_life_records(member.id) == []


def test_cancel_and_failure_never_save_drafts(setup):
    _db, _auth, member, _health, ai = setup
    cancel = threading.Event()

    class CancellingProvider(DemoLifeProvider):
        def chat_tools(self, model, messages, tools):
            result = super().chat_tools(model, messages, tools)
            cancel.set()
            return result

    with pytest.raises(ProviderError, match="停止"):
        turn(ai, member.id, "刚才喝了300毫升水", provider=CancellingProvider(), cancel=cancel)
    assert ai.list_drafts(member.id) == []


def test_member_and_advisor_permissions_apply_in_demo(setup):
    db, auth, member, _health, ai = setup
    outsider = auth.register("outsider", "synthetic-password-123")
    draft = turn(ai, member.id, "我不吃香菜")
    with pytest.raises(AiPermissionError):
        ai.build_member_context(outsider.id, member.id)
    assert ai.list_drafts(outsider.id) == []
    management = ServiceManagementService(db)
    operator = auth.bootstrap_operator("operator", "synthetic-password-123")
    advisor = management.create_advisor(
        operator.id, "advisor", "synthetic-password-123", display_name="测试顾问"
    )
    management.bind_advisor(operator.id, member.id, advisor.id)
    summary = ai.create_advisor_summary_draft(
        advisor.id, member.id, DemoLifeProvider(), "workflow_demo", DEMO_MODEL
    )
    assert "离线流程演示" in summary["answer"]
    with pytest.raises(AiDraftNotFoundError):
        ai.confirm_proposal(member.id, summary["id"], 1)
    ai.confirm_proposal(advisor.id, summary["id"], 1)
    assert draft["proposals"][0]["status"] == "pending"


def test_deepseek_contract_offline_through_real_adapter(setup):
    import httpx

    from ollama_chat_app.providers.deepseek_cloud import DeepSeekCloudProvider

    _db, _auth, member, _health, ai = setup
    requests = []

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.url.host == "api.deepseek.com"
        assert payload["model"] == "deepseek-v4-flash"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "answer": "请确认记录草稿。",
                                    "proposals": [],
                                }
                            )
                        }
                    }
                ],
            },
        )

    provider = DeepSeekCloudProvider("synthetic-test-only", transport=httpx.MockTransport(handle))
    agent = ConversationAgentProvider(
        provider, [], {"role": "user", "content": "你好"}, threading.Event()
    )
    result = ai.create_member_draft(member.id, agent, "deepseek_cloud", "deepseek-v4-flash", "你好")
    assert result["answer"] == "请确认记录草稿。"
    assert len(requests) == 1
    assert "proposals" in requests[0]["messages"][0]["content"]
