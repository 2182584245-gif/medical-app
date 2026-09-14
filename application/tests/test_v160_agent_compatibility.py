"""Merged agent behavior against the unchanged v3 API, entirely synthetic."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta

import httpx
import pytest

from ollama_chat_app.data.database import Database
from ollama_chat_app.providers.base import ProviderError
from ollama_chat_app.providers.demo_life import DEMO_MODEL, DemoLifeProvider
from ollama_chat_app.providers.platform_deepseek import PlatformDeepSeekProvider
from ollama_chat_app.services.agent_tools import AGENT_TOOLS, SnapshotTools
from ollama_chat_app.services.ai_assistant import (
    AiAssistantService,
    AiValidationError,
    UnsafeMedicalRequestError,
)
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.cloud_client import CloudAPIClient
from ollama_chat_app.services.health import HealthService
from ollama_chat_app.services.life_agent import ConversationAgentProvider
from ollama_chat_app.time_utils import BEIJING_TIMEZONE


@pytest.fixture
def local_services(tmp_path):
    database = Database(tmp_path / "synthetic-v160.db")
    user = AuthService(database).register("synthetic-member", "synthetic-password-123")
    return user, AiAssistantService(database), HealthService(database)


def record(category="water", details=None):
    return {
        "type": "life_record",
        "category": category,
        "occurred_at": "2026-09-14T08:00:00+08:00",
        "content": "用户已确认发生的合成测试事实",
        "details": details if details is not None else {"amount_ml": 300},
    }


def proxy_agent(monkeypatch, handler, *, cancel=None):
    client = CloudAPIClient(
        "https://synthetic.example/aliyun", transport=httpx.MockTransport(handler)
    )
    monkeypatch.setattr(client, "_on_gui_thread", lambda: False)
    client._token = "X" * 43
    provider = PlatformDeepSeekProvider(client)
    assert not callable(getattr(provider, "chat_tools", None))
    agent = ConversationAgentProvider(
        provider,
        [{"role": "user", "content": "仅当前对话的历史"}],
        {"role": "user", "content": "我刚才喝了300毫升水"},
        cancel or threading.Event(),
        input_source="voice",
        answer_settings={"preferred_name": "合成称呼"},
    )
    return client, agent


def test_existing_proxy_one_request_draft_confirmation_and_trusted_source(
    local_services, monkeypatch
):
    user, ai, health = local_services
    requests = []
    proposal = record(details={"amount_ml": 300, "input_source": "wearable", "external_id": "fake"})

    def handler(request):
        requests.append(request)
        assert request.url.host == "synthetic.example"
        assert request.url.path == "/aliyun/v1/ai/chat"
        assert request.headers["Authorization"] == "Bearer " + "X" * 43
        body = json.loads(request.content)
        assert set(body) == {"model", "messages", "max_tokens"}
        assert body["max_tokens"] <= 2048
        assert "单次结构化兼容模式" in body["messages"][0]["content"]
        assert "合成称呼" in body["messages"][0]["content"]
        assert body["messages"][-2]["content"] == "仅当前对话的历史"
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "content": json.dumps(
                    {"answer": "请核对后保存。", "proposals": [proposal, proposal]}
                ),
            },
        )

    client, agent = proxy_agent(monkeypatch, handler)
    try:
        draft = ai.create_member_draft(
            user.id, agent, "deepseek_cloud", "deepseek-v4-flash", "喝水"
        )
        assert len(requests) == 1
        assert agent.trace == ["structured_single_response", "propose_action:life_record"]
        assert len(draft["proposals"]) == 1
        assert draft["proposals"][0]["details"]["input_source"] == "voice"
        assert "external_id" not in draft["proposals"][0]["details"]
        assert health.list_life_records(user.id) == []
        ai.confirm_proposal(user.id, draft["id"], 1)
        saved = health.list_life_records(user.id)
        assert len(saved) == 1 and saved[0]["details"]["amount_ml"] == 300
    finally:
        client.close()


@pytest.mark.parametrize("status,code", [(429, "rate_limited"), (503, "service_unavailable")])
def test_proxy_agent_preserves_limits_and_never_retries(local_services, monkeypatch, status, code):
    user, ai, health = local_services
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"detail": "synthetic"})

    client, agent = proxy_agent(monkeypatch, handler)
    try:
        with pytest.raises(ProviderError) as caught:
            ai.create_member_draft(user.id, agent, "deepseek_cloud", "deepseek-v4-flash", "喝水")
        assert caught.value.code == code
        assert len(requests) == 1
        assert ai.list_drafts(user.id) == health.list_life_records(user.id) == []
    finally:
        client.close()


@pytest.mark.parametrize(
    "invalid,expected_error",
    [
        ("不是 JSON", AiValidationError),
        ('{"answer":"请立即服用药物","proposals":[]}', UnsafeMedicalRequestError),
    ],
)
def test_proxy_agent_bad_or_unsafe_output_never_saves(
    local_services, monkeypatch, invalid, expected_error
):
    user, ai, health = local_services
    client, agent = proxy_agent(
        monkeypatch,
        lambda _: httpx.Response(
            200,
            json={
                "model": "deepseek-v4-flash",
                "content": invalid,
            },
        ),
    )
    try:
        with pytest.raises(expected_error):
            ai.create_member_draft(user.id, agent, "deepseek_cloud", "deepseek-v4-flash", "喝水")
        assert ai.list_drafts(user.id) == health.list_life_records(user.id) == []
    finally:
        client.close()


def test_proxy_agent_cancel_after_answer_does_not_persist(local_services, monkeypatch):
    user, ai, health = local_services
    cancel = threading.Event()

    def handler(_):
        cancel.set()
        return httpx.Response(
            200,
            json={
                "model": "deepseek-v4-flash",
                "content": json.dumps({"answer": "请核对。", "proposals": [record()]}),
            },
        )

    client, agent = proxy_agent(monkeypatch, handler, cancel=cancel)
    try:
        with pytest.raises(ProviderError, match="停止"):
            ai.create_member_draft(user.id, agent, "deepseek_cloud", "deepseek-v4-flash", "喝水")
        assert ai.list_drafts(user.id) == health.list_life_records(user.id) == []
    finally:
        client.close()


@pytest.mark.parametrize(
    "text,kind",
    [
        ("我刚才去医院看病了", "consultation"),
        ("我刚才按医嘱服药了", "medication"),
    ],
)
def test_native_tool_demo_preserves_medical_facts_and_confirmation(local_services, text, kind):
    user, ai, health = local_services
    agent = ConversationAgentProvider(
        DemoLifeProvider(),
        [],
        {"role": "user", "content": text},
        threading.Event(),
        input_source="voice",
    )
    draft = ai.create_member_draft(user.id, agent, "workflow_demo", DEMO_MODEL, text)
    proposal = draft["proposals"][0]
    assert proposal["category"] == "medical"
    assert proposal["details"] == {"event_type": kind}
    assert health.list_life_records(user.id) == []
    ai.confirm_proposal(user.id, draft["id"], 1)
    saved = health.list_life_records(user.id)
    assert len(saved) == 1 and saved[0]["source"] == "ai_confirmed"


def test_medical_native_schema_and_prescription_boundary():
    categories = AGENT_TOOLS[1]["function"]["parameters"]["properties"]["proposal"]["properties"]
    assert "medical" in categories["category"]["enum"]
    tools = SnapshotTools({})
    with pytest.raises(AiValidationError):
        tools.execute(
            "propose_action",
            json.dumps(
                {
                    "proposal": record(
                        "medical", {"event_type": "medication", "dose_recommendation": "synthetic"}
                    )
                }
            ),
        )
    assert tools.proposals == []


def test_demo_sleep_preserves_explicit_offsets_and_dst_elapsed_time(monkeypatch):
    monkeypatch.setattr(
        "ollama_chat_app.providers.demo_life.beijing_now",
        lambda: datetime(2026, 11, 3, 12, tzinfo=BEIJING_TIMEZONE),
    )
    provider = DemoLifeProvider()
    text = (
        "我自报的作息：入睡 2026-10-31T22:00:00-04:00，起床 2026-11-01T07:00:00-05:00"
        "（纽约时间；时区 America/New_York）。请整理为待确认的作息记录。"
    )
    _, proposals = provider._member(text, {}, [])
    assert proposals[0]["details"]["duration_hours"] == 10
    assert datetime.fromisoformat(proposals[0]["details"]["start_at"]).utcoffset() == timedelta(
        hours=-4
    )
    assert datetime.fromisoformat(proposals[0]["occurred_at"]).utcoffset() == timedelta(hours=-5)


def test_demo_sleep_mixed_naive_and_aware_times_do_not_create_fact(monkeypatch):
    monkeypatch.setattr(
        "ollama_chat_app.providers.demo_life.beijing_now",
        lambda: datetime(2026, 11, 3, 12, tzinfo=BEIJING_TIMEZONE),
    )
    text = "我自报的作息：入睡 2026-10-31T22:00:00，起床 2026-11-01T07:00:00-05:00"
    assert DemoLifeProvider()._member(text, {}, [])[1] == []
