from __future__ import annotations

import json
import threading

import httpx
import pytest

from ollama_chat_app.data.database import Database
from ollama_chat_app.providers.base import ProviderError
from ollama_chat_app.providers.deepseek_cloud import DeepSeekCloudProvider
from ollama_chat_app.services.ai_assistant import AiAssistantService
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.health import HealthService
from ollama_chat_app.services.life_agent import ConversationAgentProvider


def call(name, args, identifier="call-1"):
    return {
        "id": identifier,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args),
        },
    }


def fixture(tmp_path, handler):
    db = Database(tmp_path / "tool-loop.db")
    member = AuthService(db).register("member", "synthetic-password-123")
    provider = DeepSeekCloudProvider("synthetic-only", transport=httpx.MockTransport(handler))
    agent = ConversationAgentProvider(
        provider, [], {"role": "user", "content": "我不吃香菜"}, threading.Event()
    )
    return db, member, AiAssistantService(db), agent


def response(content=None, calls=None):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "tool_calls" if calls else "stop",
                    "message": {"content": content, "tool_calls": calls},
                }
            ]
        },
    )


@pytest.mark.parametrize("natural_final", [False, True])
def test_real_adapter_read_propose_observe_finish_and_confirm(tmp_path, natural_final):
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["model"] == "deepseek-v4-flash"
        assert body["thinking"] == {"type": "disabled"}
        assert body["max_tokens"] == 4096
        assert {t["function"]["name"] for t in body["tools"]} == {
            "read_life_data",
            "propose_action",
        }
        if len(requests) == 1:
            assert "稳定数据快照" not in body["messages"][0]["content"]
            assert "只输出一个 JSON 对象" not in body["messages"][0]["content"]
            assert "不要在最终回复里输出草稿 JSON" in body["messages"][0]["content"]
            return response(calls=[call("read_life_data", {"section": "profile"})])
        if len(requests) == 2:
            assert body["messages"][-1]["role"] == "tool"
            assert body["messages"][-1]["tool_call_id"] == "call-1"
            return response(
                calls=[
                    call(
                        "propose_action",
                        {
                            "proposal": {
                                "type": "profile_fact",
                                "fact_key": "dietary_preferences",
                                "value": "不吃香菜",
                            }
                        },
                        "call-2",
                    )
                ]
            )
        assert json.loads(body["messages"][-1]["content"])["status"] == "pending_confirmation"
        return response(
            "请核对偏好草稿后保存。"
            if natural_final
            else json.dumps({"answer": "请核对偏好草稿后保存。", "proposals": []})
        )

    db, member, ai, agent = fixture(tmp_path, handle)
    result = ai.create_member_draft(
        member.id, agent, "deepseek_cloud", "deepseek-v4-flash", "我不吃香菜"
    )
    assert len(requests) == 3
    assert agent.trace == ["read_life_data:profile", "propose_action:profile_fact"]
    assert ai.build_member_context(member.id)["confirmed_facts"] == {}
    ai.confirm_proposal(member.id, result["id"], 1)
    assert (
        ai.build_member_context(member.id)["confirmed_facts"]["dietary_preferences"] == "不吃香菜"
    )
    assert HealthService(db).list_life_records(member.id) == []


@pytest.mark.parametrize(
    "tool",
    [
        call("execute_sql", {"sql": "DELETE FROM users"}),
        call("read_life_data", {"section": "profile", "user_id": 2}),
        call("read_life_data", {"section": "credentials"}),
        {
            "id": "bad",
            "type": "function",
            "function": {"name": "propose_action", "arguments": "oops"},
        },
    ],
)
def test_untrusted_tool_request_cannot_expand_scope_or_write(tmp_path, tool):
    db, member, ai, agent = fixture(tmp_path, lambda _: response(calls=[tool]))
    with pytest.raises(ProviderError):
        ai.create_member_draft(member.id, agent, "deepseek_cloud", "deepseek-v4-flash", "测试")
    assert ai.list_drafts(member.id) == []
    assert HealthService(db).list_life_records(member.id) == []


def test_endless_model_tool_loop_stops_with_no_drafts(tmp_path):
    count = 0

    def handler(_):
        nonlocal count
        count += 1
        return response(calls=[call("read_life_data", {"section": "profile"}, f"call-{count}")])

    _db, member, ai, agent = fixture(tmp_path, handler)
    with pytest.raises(ProviderError, match="上限"):
        ai.create_member_draft(member.id, agent, "deepseek_cloud", "deepseek-v4-flash", "测试")
    assert count == 4
    assert ai.list_drafts(member.id) == []


def test_one_malformed_json_can_be_corrected_without_executing_it(tmp_path):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            broken = call("read_life_data", {})
            broken["function"]["arguments"] = '{"section": "profile"'
            return response(calls=[broken])
        if len(requests) == 2:
            assert json.loads(body["messages"][-1]["content"])["error"] == "invalid_json"
            return response(calls=[call("read_life_data", {"section": "profile"}, "fixed")])
        return response("目前没有已确认的饮食偏好。")

    _db, member, ai, agent = fixture(tmp_path, handler)
    result = ai.create_member_draft(
        member.id, agent, "deepseek_cloud", "deepseek-v4-flash", "我的偏好"
    )
    assert agent.trace == ["read_life_data:profile"]
    assert len(requests) == 3
    assert result["proposals"] == []
