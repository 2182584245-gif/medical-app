from __future__ import annotations

import inspect
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from ollama_chat_app.data.database import Conversation, Message, User
from ollama_chat_app.services.chat import ChatTurn, PendingExchange
from ollama_chat_app.services.chat_attachments import ChatAttachment
from ollama_chat_app.services.cloud_client import CloudAPIError
from ollama_chat_app.services.cloud_rpc_codec import CloudCodecError, decode_rpc, encode_rpc
from ollama_chat_app.services.remote_services import (
    SERVICE_METHODS,
    RemoteAiAssistantService,
    RemoteAuthService,
    RemoteChatService,
    RemoteCommerceService,
    RemoteFileManagementService,
    RemoteHealthService,
    RemoteServiceManagementService,
    user_from_cloud,
)


class FakeClient:
    def __init__(self, result=None):
        self.result, self.calls = result, []

    def rpc(self, service, method, args, kwargs, *, request_id=None):
        self.calls.append((service, method, args, kwargs, request_id))
        return encode_rpc(self.result)


def test_codec_all_static_value_types_roundtrip():
    now = datetime.now(UTC)
    user = User(1, "member", "member", now, None)
    conversation = Conversation(1, 1, "合成", None, None, now, now)
    message = Message(1, 1, 1, "user", "合成", "complete", None, None, None, now, now)
    attachment = ChatAttachment("synthetic.txt", "text/plain", b"abc", "abc", "local_text", "x")
    values = [user, conversation, message, ChatTurn("user", "x", (attachment,)),
              PendingExchange(conversation, message, message), date(2026, 9, 7),
              {"kinds": {"image", "document"}}, (b"binary", None, True, 1, 1.5)]
    assert decode_rpc(encode_rpc(values)) == values


@pytest.mark.parametrize("value", [
    {"$__type": "os.system", "value": "not executable"},
    {"$__type": "bytes", "value": "invalid base64 !"},
    {"$__type": "datetime", "value": "invalid"},
    {"$__type": "User", "fields": {"password_hash": "secret"}},
    {"$__type": "date", "value": "2026-09-07", "extra": True},
])
def test_codec_rejects_unknown_types_fields_and_invalid_shapes(value):
    with pytest.raises(CloudCodecError) as error:
        decode_rpc(value)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("value", [Path("private-path"), object(), float("nan"),
                                 {"$__type": "reserved"}, {1: "bad key"}])
def test_codec_rejects_local_objects_paths_and_reserved_tags(value):
    with pytest.raises(CloudCodecError):
        encode_rpc(value)


def test_codec_depth_limit():
    value = None
    for _ in range(40):
        value = [value]
    with pytest.raises(CloudCodecError):
        encode_rpc(value)


@pytest.mark.parametrize("facade", [RemoteAuthService, RemoteChatService, RemoteHealthService,
                                    RemoteServiceManagementService, RemoteCommerceService])
def test_facade_public_signature_matches_local_whitelist_without_opening_database(facade):
    remote = facade(FakeClient())
    local_class, methods = SERVICE_METHODS[remote.service_name]
    assert not hasattr(remote, "database")
    for method in methods:
        expected = inspect.signature(local_class.__dict__[method])
        expected = expected.replace(parameters=list(expected.parameters.values())[1:])
        assert inspect.signature(getattr(remote, method)) == expected
    with pytest.raises(AttributeError):
        remote._require_user(1)


def test_facade_codec_preserves_timestamp_bytes_and_rejects_unknown_method():
    client = FakeClient({"saved": True})
    service = RemoteHealthService(client)
    result = service.add_life_record(3, "diet", content="合成记录", occurred_at=datetime.now(UTC))
    assert result == {"saved": True}
    assert client.calls[0][0:2] == ("health", "add_life_record")
    assert isinstance(decode_rpc(client.calls[0][3])["occurred_at"], datetime)
    with pytest.raises(CloudAPIError):
        service.call("_require_user", 3)
    with pytest.raises(CloudAPIError):
        service.add_life_record(3, unknown="secret")


def test_cloud_operator_bootstrap_never_calls_server():
    client = FakeClient()
    service = RemoteAuthService(client)
    assert service.has_operator()
    with pytest.raises(CloudAPIError):
        service.bootstrap_operator("ops", "private-password")
    assert client.calls == []


def test_plain_auth_safe_view_to_dataclass():
    view = {"id": 1, "username": "member", "username_normalized": "member",
            "created_at": "2026-09-07T00:00:00Z", "last_login_at": None,
            "role_code": "member", "account_status": "active", "updated_at": None}
    assert isinstance(user_from_cloud(view), User)
    with pytest.raises(CloudAPIError):
        user_from_cloud({**view, "password_hash": "must not pass"})


def test_file_source_path_never_leaves_device(tmp_path):
    source = tmp_path / "synthetic.pdf"
    source.write_bytes(b"%PDF synthetic-not-parsed-by-client")
    client = FakeClient(5)
    service = RemoteFileManagementService(client)
    assert service.upload_file(3, source) == 5
    assert client.calls[0][:2] == ("files", "upload_bytes")
    arguments = decode_rpc(client.calls[0][2])
    assert arguments == [3, source.name, source.read_bytes()]
    assert str(source) not in repr(client.calls)


def test_structured_ai_keeps_provider_keys_and_images_off_rpc(monkeypatch):
    from ollama_chat_app.workers import cloud_bridge

    monkeypatch.setattr(cloud_bridge, "on_gui_thread", lambda: False)

    class Provider:
        supports_images = True
        api_key = "private-api-key-never-upload"

        def chat(self, model, messages):
            assert model == "synthetic-model"
            assert messages[-1]["images"] == [b"synthetic-image"]
            return "synthetic-response"

    class Stages(FakeClient):
        def rpc(self, service, method, args, kwargs, *, request_id=None):
            self.calls.append((service, method, args, kwargs, request_id))
            if method == "prepare_member_draft":
                return encode_rpc({"ticket": "signed-synthetic-ticket", "model": "synthetic-model",
                                   "messages": [{"role": "user", "content": "synthetic"}]})
            return encode_rpc({"id": 9, "status": "draft"})

    client = Stages()
    service = RemoteAiAssistantService(client)
    result = service.create_member_draft(3, Provider(), "synthetic", "synthetic-model", "hello",
                                          images=[b"synthetic-image"])
    assert result["status"] == "draft"
    assert "private-api-key" not in repr(client.calls)
    assert "synthetic-image" not in repr(client.calls)
    assert client.calls[0][3]["had_images"] is True
