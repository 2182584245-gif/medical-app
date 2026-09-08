"""Desktop RPC facades never serialize provider credentials or invoke local databases."""

from __future__ import annotations

import json

import pytest

from ollama_chat_app.services.cloud_client import CloudAPIError
from ollama_chat_app.services.cloud_rpc_codec import decode_rpc, encode_rpc
from ollama_chat_app.services.remote_services import (
    RemoteAiAssistantService,
    RemoteAppointmentService,
    RemotePreferencesService,
)


class Client:
    def __init__(self):
        self.calls = []
        self.identity = 7

    def rpc(self, service, method, args, kwargs, *, request_id=None):
        self.calls.append((service, method, decode_rpc(args), decode_rpc(kwargs), request_id))
        if method == "list_visit_tasks":
            return encode_rpc([])
        return encode_rpc({"enabled": True, "member_assistant_enabled": True})

    def me(self):
        return {
            "id": self.identity,
            "username": "synthetic",
            "username_normalized": "synthetic",
            "created_at": "2026-09-08T00:00:00Z",
            "last_login_at": None,
            "role_code": "member",
            "account_status": "active",
            "updated_at": None,
        }


def test_remote_preferences_use_explicit_rpc_aliases():
    client = Client()
    preferences = RemotePreferencesService(client)
    preferences.get(7)
    preferences.update(7, {"theme_color": "blue"})
    assert [call[:3] for call in client.calls] == [
        ("preferences", "get_preferences", [7]),
        ("preferences", "update_preferences", [7, {"theme_color": "blue"}]),
    ]
    assert not hasattr(preferences, "database")


def test_remote_appointments_bind_actor_before_target_and_share_management_contract():
    client = Client()
    appointments = RemoteAppointmentService(client)
    appointments.create_request(7, "合成上门", notes="用户待确认预约", address="合成地址")
    appointments.list_for_member(7)
    appointments.list_all(9)
    appointments.update_request(42, 9, status="disabled")
    assert [call[1] for call in client.calls] == [
        "request_appointment",
        "list_visit_tasks",
        "list_visit_tasks",
        "update_appointment",
    ]
    assert client.calls[0][2] == [7]
    assert client.calls[0][3]["service_type"] == "合成上门"
    assert client.calls[-1][2:4] == ([9, 42], {"status": "disabled"})


def test_remote_life_extraction_is_no_write_and_no_historical_context_or_provider_on_rpc(
    monkeypatch,
):
    from ollama_chat_app.workers import cloud_bridge

    monkeypatch.setattr(cloud_bridge, "on_gui_thread", lambda: False)
    client = Client()

    class Provider:
        api_key = "synthetic-private-key-do-not-serialize"
        calls = 0

        def chat(self, model, messages):
            self.calls += 1
            assert model == "synthetic-model"
            context = json.loads(messages[0]["content"].split("稳定数据快照：")[1].split("\n")[0])
            assert set(context) == {"member_user_id", "generated_at"}
            assert messages[-1]["content"] == "早餐吃了鸡蛋"
            return json.dumps(
                {
                    "answer": "请确认",
                    "proposals": [
                        {
                            "type": "life_record",
                            "category": "diet",
                            "content": "早餐吃了鸡蛋",
                            "occurred_at": "2026-09-08T08:00:00+08:00",
                            "details": {},
                        }
                    ],
                },
                ensure_ascii=False,
            )

    provider = Provider()
    service = RemoteAiAssistantService(client)
    proposals = service.propose_life_records(
        7, provider, "synthetic", "synthetic-model", "早餐吃了鸡蛋"
    )
    assert len(proposals) == 1 and provider.calls == 1
    assert [call[1] for call in client.calls] == ["get_ai_config"]
    assert "synthetic-private-key" not in repr(client.calls)
    client.identity = 8
    with pytest.raises(CloudAPIError):
        service.propose_life_records(7, provider, "synthetic", "synthetic-model", "早餐吃了鸡蛋")
    assert provider.calls == 1
