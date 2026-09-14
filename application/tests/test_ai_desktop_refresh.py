from __future__ import annotations

import json
import os
import threading
from types import SimpleNamespace

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import httpx
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox, QPushButton

from ollama_chat_app import config
from ollama_chat_app.data.database import Database
from ollama_chat_app.providers.base import ProviderError
from ollama_chat_app.providers.deepseek_cloud import DeepSeekCloudProvider
from ollama_chat_app.security.secret_store import SecretStore
from ollama_chat_app.security.windows_dpapi import SecretProtectionError
from ollama_chat_app.services.ai_assistant import AiAssistantService
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.chat import ChatService
from ollama_chat_app.services.chat_attachments import prepare_attachment_bytes
from ollama_chat_app.services.chat_personalization import personalized_system_message
from ollama_chat_app.ui.chat_page import ChatPage
from ollama_chat_app.ui.chat_settings_dialog import ChatSettingsDialog


def test_dpapi_restart_default_user_override_and_namespace_isolation(tmp_path):
    first = SecretStore(namespace="local:test-a", storage_dir=tmp_path)
    first.set_default_api_key("synthetic-default-do-not-use")
    first.save_persistent_api_key("alice-stable-id", "synthetic-alice-do-not-use")
    restarted = SecretStore(namespace="local:test-a", storage_dir=tmp_path)
    assert restarted.get_api_key("alice-stable-id", "deepseek_cloud") is None
    assert restarted.get_effective_api_key("alice-stable-id") == "synthetic-alice-do-not-use"
    assert restarted.get_effective_api_key("bob-stable-id") == "synthetic-default-do-not-use"
    cloud = SecretStore(namespace="cloud:https://test.invalid", storage_dir=tmp_path)
    assert cloud.get_effective_api_key("alice-stable-id") == "synthetic-default-do-not-use"
    assert not any(b"synthetic" in path.read_bytes() for path in tmp_path.iterdir())
    restarted.delete_persistent_api_key("alice-stable-id")
    assert first.get_effective_api_key("alice-stable-id") == "synthetic-default-do-not-use"


def test_dpapi_account_binding_rejects_swapped_ciphertext(tmp_path):
    store = SecretStore(storage_dir=tmp_path)
    store.save_persistent_api_key("alice", "synthetic-only")
    original = store._secret_path(store._persistent_account("alice", "deepseek_cloud"))
    target = store._secret_path(store._persistent_account("bob", "deepseek_cloud"))
    target.write_bytes(original.read_bytes())
    with pytest.raises(SecretProtectionError, match="无法读取"):
        store.get_effective_api_key("bob")


def test_dpapi_failure_never_writes_plaintext(tmp_path, monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise SecretProtectionError("synthetic failure")

    monkeypatch.setattr("ollama_chat_app.security.secret_store.protect_secret", unavailable)
    with pytest.raises(SecretProtectionError):
        SecretStore(storage_dir=tmp_path).set_default_api_key("synthetic-only")
    assert list(tmp_path.iterdir()) == []


def test_personalization_uses_only_own_context_and_selected_preferences():
    calls = []

    class Context:
        def build_member_context(self, user_id, **kwargs):
            calls.append((user_id, kwargs))
            return {
                "member_user_id": user_id,
                "recent_life_records": [{"content": "本人合成睡眠记录"}],
                "api_key": "never-share-this",
            }

    result = personalized_system_message(
        17, "最近睡得怎么样", {"preferred_name": "李阿姨", "api_key": "do-not-share"}, Context()
    )
    assert calls == [(17, {"context_days": 7})]
    assert "李阿姨" in result["content"]
    assert "本人合成睡眠记录" in result["content"]
    assert "never-share-this" not in result["content"]
    assert "do-not-share" not in result["content"]
    assert "没有实时天气查询工具" in result["content"]


def test_personalization_rejects_cross_account_snapshot():
    service = SimpleNamespace(build_member_context=lambda *_a, **_k: {"member_user_id": 999})
    with pytest.raises(ValueError, match="身份校验"):
        personalized_system_message(17, "饮食建议", {}, service)


@pytest.mark.parametrize("question,share", [("讲个笑话", True), ("睡眠建议", False)])
def test_context_not_read_for_unrelated_or_opted_out_question(question, share):
    class Context:
        def build_member_context(self, *_args, **_kwargs):
            raise AssertionError("must not read")

    result = personalized_system_message(17, question, {}, Context(), share_context=share)
    assert "本用户资料开始" in result["content"]


def _stream_transport(lines, captured):
    def handle(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text="\n\n".join(lines)
        )

    return httpx.MockTransport(handle)


def test_deepseek_stream_returns_actual_deltas_and_marks_completion():
    captured = []
    provider = DeepSeekCloudProvider(
        "synthetic-only",
        transport=_stream_transport(
            [
                'data: {"choices":[{"delta":{"content":"您好"}}]}',
                'data: {"choices":[{"delta":{"content":"，慢慢来"}}]}',
                "data: [DONE]",
            ],
            captured,
        ),
    )
    assert list(
        provider.chat_stream(config.DEEPSEEK_DEFAULT_MODEL, [{"role": "user", "content": "hi"}])
    ) == ["您好", "，慢慢来"]
    assert captured[0]["stream"] is True
    assert provider.last_used_model == config.DEEPSEEK_DEFAULT_MODEL


def test_deepseek_stream_truncation_is_not_success():
    provider = DeepSeekCloudProvider(
        "synthetic-only",
        transport=_stream_transport(
            [
                'data: {"choices":[{"delta":{"content":"不完整"}}]}',
            ],
            [],
        ),
    )
    with pytest.raises(ProviderError) as caught:
        list(
            provider.chat_stream(config.DEEPSEEK_DEFAULT_MODEL, [{"role": "user", "content": "hi"}])
        )
    assert caught.value.code == "incomplete_response"
    assert provider.last_used_model is None


def test_deepseek_stream_cancel_does_not_report_complete():
    provider = DeepSeekCloudProvider(
        "synthetic-only",
        transport=_stream_transport(
            [
                'data: {"choices":[{"delta":{"content":"片段"}}]}',
                "data: [DONE]",
            ],
            [],
        ),
    )
    stop = threading.Event()
    chunks = provider.chat_stream(
        config.DEEPSEEK_DEFAULT_MODEL, [{"role": "user", "content": "hi"}], cancel_event=stop
    )
    assert next(chunks) == "片段"
    stop.set()
    with pytest.raises(ProviderError) as caught:
        next(chunks)
    assert caught.value.code == "cancelled"
    assert provider.last_used_model is None


@pytest.fixture
def refreshed_page(qtbot, tmp_path):
    database = Database(tmp_path / "refresh.db")
    user = AuthService(database).register("synthetic-member", "synthetic-password-only")
    service = AiAssistantService(database)
    page = ChatPage(
        ChatService(database),
        SecretStore(storage_dir=tmp_path / "secrets"),
        ai_assistant_service=service,
    )
    qtbot.addWidget(page)
    page.start_session(user)
    page.context_checkbox.setChecked(True)
    page.resize(1100, 800)
    page.show()
    return page


def test_attachment_cards_remove_only_selected_and_unified_controls(
    refreshed_page, qtbot, monkeypatch
):
    page = refreshed_page
    page._pending_attachments = [
        prepare_attachment_bytes("one.txt", b"one"),
        prepare_attachment_bytes("two.txt", b"two"),
    ]
    page._refresh_attachment_label()
    assert not page.file_button.isVisible()
    assert not page.clear_attachments_button.isVisible()
    removes = page.attachment_cards.findChildren(QPushButton)
    assert len(removes) == 2
    qtbot.mouseClick(removes[0], Qt.MouseButton.LeftButton)
    assert [item.original_name for item in page._pending_attachments] == ["two.txt"]
    actions = page.life_actions_button.menu().actions()
    assert [action.text() for action in actions] == ["看看近期生活", "设置生活提醒"]
    requests = []
    monkeypatch.setattr(page, "_send_suggestion", requests.append)
    for action in actions:
        action.trigger()
    assert requests == ["请总结最近的生活记录", "我想设置一个生活提醒"]


def test_suggestions_send_once_and_hide_after_first_exchange(refreshed_page, qtbot, monkeypatch):
    page = refreshed_page
    captured = []

    class Provider:
        supports_images = True

        def chat(self, _model, messages):
            captured.append(messages)
            return "合成测试回复"

    monkeypatch.setattr(
        page, "_selected_provider", lambda: (Provider(), "ollama_local", "synthetic")
    )
    qtbot.mouseClick(page.message_list.suggestion_buttons[0], Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not page._request_running)
    assert len(captured) == 1
    assert captured[0][0]["role"] == "system"
    assert "近期" in captured[0][0]["content"] or "资料快照" in captured[0][0]["content"]
    assert not page.message_list.welcome.isVisible()


def test_settings_exposes_all_requested_controls(qtbot):
    dialog = ChatSettingsDialog(
        {"preferred_name": "合成称呼", "ai_tone": "concise", "font_size": 22}
    )
    qtbot.addWidget(dialog)
    assert set(dialog.values()) == {
        "ai_provider",
        "font_size",
        "ai_tone",
        "ai_complexity",
        "theme_color",
        "ai_model",
    }
    assert dialog.values()["ai_tone"] == "concise"
    assert dialog.values()["font_size"] == 22


def test_life_record_proposals_do_not_write_records_or_drafts(refreshed_page):
    page = refreshed_page

    class Provider:
        def chat(self, *_args):
            return json.dumps(
                {
                    "answer": "请确认",
                    "proposals": [
                        {
                            "type": "life_record",
                            "category": "diet",
                            "occurred_at": "2026-09-08T08:00:00+08:00",
                            "content": "早餐吃了一个鸡蛋",
                            "details": {},
                        }
                    ],
                },
                ensure_ascii=False,
            )

    result = page.ai_assistant_service.propose_life_records(
        page.current_user.id, Provider(), "synthetic", "synthetic", "早餐吃了一个鸡蛋"
    )
    assert result[0]["category"] == "diet"
    with page.chat_service.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM life_records").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM ai_insights").fetchone()[0] == 0


def test_declined_context_consent_is_saved_and_not_repeated(refreshed_page, monkeypatch):
    page = refreshed_page
    page._preferences = {"ai_context_consent": False, "ai_context_consent_decided": False}
    page.context_checkbox.blockSignals(True)
    page.context_checkbox.setChecked(False)
    page.context_checkbox.blockSignals(False)
    prompts = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_a, **_kw: prompts.append(True) or QMessageBox.StandardButton.No,
    )
    assert not page._ensure_context_consent()
    assert not page._ensure_context_consent()
    assert prompts == [True]
    assert page._preferences["ai_context_consent_decided"] is True
    assert page._preferences["ai_context_consent"] is False
