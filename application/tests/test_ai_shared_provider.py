import json
import os
from types import SimpleNamespace

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import httpx
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from ollama_chat_app import config
from ollama_chat_app.data.database import Database, utc_now
from ollama_chat_app.providers.base import ProviderError
from ollama_chat_app.providers.deepseek_cloud import DeepSeekCloudProvider
from ollama_chat_app.providers.platform_deepseek import PlatformDeepSeekProvider
from ollama_chat_app.security.secret_store import SecretStore
from ollama_chat_app.services.ai_assistant import AiAssistantService, UnsafeMedicalRequestError
from ollama_chat_app.services.ai_provider_resolver import resolve_deepseek
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.chat import (
    ChatService,
    ConversationNotFoundError,
    MessageStateError,
    MessageValidationError,
)
from ollama_chat_app.services.chat_attachments import prepare_attachment_bytes
from ollama_chat_app.services.cloud_client import CloudAPIClient
from ollama_chat_app.services.health import HealthService
from ollama_chat_app.ui.chat_page import ChatPage


def test_personal_key_priority_rotation_removal_and_cloud_default_never_reads_default_key(tmp_path):
    store = SecretStore(namespace="cloud:https://synthetic.invalid", storage_dir=tmp_path)
    client = object()
    store.set_default_api_key("synthetic-local-default")
    assert isinstance(
        resolve_deepseek(store, "account", cloud_client=client), PlatformDeepSeekProvider
    )
    store.save_persistent_api_key("account", "synthetic-personal-one")
    first = resolve_deepseek(store, "account", cloud_client=client)
    assert isinstance(first, DeepSeekCloudProvider) and first._api_key == "synthetic-personal-one"
    store.save_persistent_api_key("account", "synthetic-personal-two")
    assert (
        resolve_deepseek(store, "account", cloud_client=client)._api_key == "synthetic-personal-two"
    )
    assert isinstance(
        resolve_deepseek(store, "another", cloud_client=client), PlatformDeepSeekProvider
    )
    store.delete_persistent_api_key("account")
    assert isinstance(
        resolve_deepseek(store, "account", cloud_client=client), PlatformDeepSeekProvider
    )
    assert resolve_deepseek(store, "account")._api_key == "synthetic-local-default"


def test_corrupt_personal_key_does_not_switch_to_shared_payer(monkeypatch, tmp_path):
    store = SecretStore(storage_dir=tmp_path)

    def corrupt(*_):
        raise RuntimeError("synthetic-private-key-unavailable")

    monkeypatch.setattr(store, "get_persistent_api_key", corrupt)
    with pytest.raises(RuntimeError):
        resolve_deepseek(store, "account", cloud_client=object())


def test_default_provider_fixed_json_route_and_model_validation():
    calls = []

    def request(method, path, *, json):
        calls.append((method, path, json))
        return {"model": json["model"], "content": "模拟提议"}

    provider = PlatformDeepSeekProvider(SimpleNamespace(request=request))
    assert (
        provider.chat(config.DEEPSEEK_DEFAULT_MODEL, [{"role": "user", "content": "喝水"}])
        == "模拟提议"
    )
    assert calls[0][:2] == ("POST", "/v1/ai/chat")
    assert set(calls[0][2]) == {"model", "messages", "max_tokens"}
    assert provider.last_used_model == config.DEEPSEEK_DEFAULT_MODEL
    with pytest.raises(ProviderError):
        provider.chat("unknown", [{"role": "user", "content": "喝水"}])
    assert len(calls) == 1


def test_default_stream_uses_existing_authenticated_client_without_secret_or_retry(monkeypatch):
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=('data: {"delta":"模拟"}\n\ndata: {"done":true,"model":"deepseek-v4-flash"}\n\n'),
        )

    client = CloudAPIClient("https://synthetic.example", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(client, "_on_gui_thread", lambda: False)
    client._token = "A" * 43
    provider = PlatformDeepSeekProvider(client)
    assert list(
        provider.chat_stream(config.DEEPSEEK_DEFAULT_MODEL, [{"role": "user", "content": "提问"}])
    ) == ["模拟"]
    assert len(captured) == 1 and str(captured[0].url).endswith("/v1/ai/chat/stream")
    assert captured[0].headers["Authorization"] == "Bearer " + "A" * 43
    assert set(json.loads(captured[0].content)) == {"model", "messages", "max_tokens"}
    client.close()


@pytest.fixture
def accounts(tmp_path):
    db = Database(tmp_path / "synthetic.db")
    auth = AuthService(db)
    return (
        db,
        auth.register("alice-test", "synthetic-password-123"),
        auth.register("bob-test", "synthetic-password-456"),
    )


def test_delete_conversations_atomic_scope_pending_and_attachment_cascade(accounts):
    db, user, other = accounts
    service = ChatService(db)
    first = service.get_default_conversation(user.id)
    second = service.create_conversation(user.id)
    foreign = service.get_default_conversation(other.id)
    with pytest.raises(ConversationNotFoundError):
        service.delete_conversations(user.id, [first.id, foreign.id])
    assert len(service.list_conversations(user.id)) == 2
    exchange = service.begin_message(
        user.id,
        "附件",
        conversation_id=first.id,
        attachments=[prepare_attachment_bytes("synthetic.txt", b"synthetic")],
    )
    with pytest.raises(MessageStateError):
        service.delete_conversations(user.id, [first.id, second.id])
    service.complete_message(user.id, exchange.assistant_message.id, "答复")
    assert service.delete_conversations(user.id, [first.id, second.id]) == 2
    assert len(service.list_conversations(user.id)) == 1
    assert service.list_messages(user.id) == []
    assert service.get_conversation(other.id, foreign.id).id == foreign.id
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM chat_attachments").fetchone()[0] == 0


@pytest.mark.parametrize("ids", [[], [True], [0], [1, 1], list(range(1, 102))])
def test_delete_selection_must_be_bounded_exact_ids(accounts, ids):
    db, user, _ = accounts
    with pytest.raises(MessageValidationError):
        ChatService(db).delete_conversations(user.id, ids)


def test_chat_checkbox_delete_confirmation_and_same_selection_for_voice(
    qtbot, accounts, tmp_path, monkeypatch
):
    db, user, _ = accounts
    chat = ChatService(db)
    chat.create_conversation(user.id)
    store = SecretStore(storage_dir=tmp_path / "secrets")
    page = ChatPage(chat, store, cloud_client=object())
    qtbot.addWidget(page)
    page.start_session(user)
    page._open_key_dialog = lambda *_: pytest.fail("default cloud use must not ask for a key")
    first = page._selected_provider()
    assert isinstance(first[0], PlatformDeepSeekProvider)
    store.save_persistent_api_key(page._credential_identity(), "synthetic-personal")
    second = page._selected_provider()  # This is exactly Today's existing callback.
    assert isinstance(second[0], DeepSeekCloudProvider)
    assert first[1:] == second[1:]
    for index in range(page.conversation_list.count()):
        page.conversation_list.item(index).setCheckState(Qt.CheckState.Checked)
    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_k: QMessageBox.StandardButton.No)
    page._delete_conversations()
    assert len(chat.list_conversations(user.id)) == 2
    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_k: QMessageBox.StandardButton.Yes)
    page._delete_conversations()
    qtbot.waitUntil(lambda: not page._request_running)
    assert len(chat.list_conversations(user.id)) == 1


def test_medical_and_environment_facts_are_context_not_medication_decisions(accounts):
    db, user, other = accounts
    health = HealthService(db)
    health.add_life_record(
        user.id,
        "medical",
        utc_now(),
        "今天按既有医嘱服药",
        details={"event_type": "medication", "name": "用户说出的药名"},
    )
    health.add_life_record(user.id, "environment", utc_now(), "房间完成通风")
    health.add_life_record(other.id, "medical", utc_now(), "其他会员的私密记录")
    service = AiAssistantService(db, health)
    context = service.build_member_context(user.id)
    assert {row["category"] for row in context["recent_life_records"]} == {"medical", "environment"}
    assert "其他会员" not in json.dumps(context, ensure_ascii=False)
    with pytest.raises(UnsafeMedicalRequestError):
        service._reject_medical_request("请帮我调整药量")
    service._reject_medical_request("今天已经服用药物，记录一下")
