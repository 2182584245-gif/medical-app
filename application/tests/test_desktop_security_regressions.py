from __future__ import annotations

import io
import json
import os
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from ollama_chat_app import config
from ollama_chat_app.data.database import Database
from ollama_chat_app.providers.base import ProviderError
from ollama_chat_app.providers.deepseek_cloud import DeepSeekCloudProvider
from ollama_chat_app.security.secret_store import SecretStore
from ollama_chat_app.services.ai_assistant import AiAssistantService
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.chat import ChatService
from ollama_chat_app.services.chat_attachments import (
    AttachmentValidationError,
    prepare_attachment_bytes,
)
from ollama_chat_app.ui.chat_page import ChatPage


@pytest.fixture
def desktop_page(qtbot, tmp_path):
    database = Database(tmp_path / "security-test.db")
    user = AuthService(database).register("original-test-user", "only-test-password-123")
    page = ChatPage(
        ChatService(database), SecretStore(), ai_assistant_service=AiAssistantService(database)
    )
    qtbot.addWidget(page)
    page.start_session(user)
    return page


def test_late_structured_success_is_saved_for_original_user_without_new_session_display(
    desktop_page,
    qtbot,
    monkeypatch,
) -> None:
    page = desktop_page
    original_user = page.current_user
    original_conversation = page.current_conversation_id
    started, release = threading.Event(), threading.Event()

    class Provider:
        supports_images = True

        def chat(self, *_args):
            started.set()
            assert release.wait(10)
            return json.dumps({"answer": "Original user's synthetic answer.", "proposals": []})

    monkeypatch.setattr(page, "_selected_provider", lambda: (Provider(), "ollama_local", "test"))
    page.message_input.setPlainText("生成一般性生活草稿")
    page.send_structured_message()
    try:
        qtbot.waitUntil(started.is_set)
        page.end_session()
        new_user = AuthService(page.chat_service.database).register(
            "new-test-user", "different-test-password-123"
        )
        page.start_session(new_user)
        page.message_input.setPlainText("New session's unsent draft.")
    finally:
        release.set()
        qtbot.waitUntil(lambda: not page._request_running)

    assert page.current_user.id == new_user.id
    assert page.message_input.toPlainText() == "New session's unsent draft."
    assert page.message_list._bubbles == {}
    original_messages = page.chat_service.list_messages(
        original_user.id, conversation_id=original_conversation
    )
    assert original_messages[-1].status == "complete"
    assert original_messages[-1].content == "Original user's synthetic answer."
    assert page.chat_service.list_messages(new_user.id) == []


def test_pdf_expansion_limit_is_cumulative_across_pages(monkeypatch) -> None:
    # Scale the limit down: this exercises the same resource boundary without
    # creating a large or dangerous input or allocating a large decoded stream.
    monkeypatch.setattr("ollama_chat_app.services.chat_attachments.MAX_EXPANDED_BYTES", 1024)
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    for index in range(3):
        page = writer.add_blank_page(width=100, height=100)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        stream = DecodedStreamObject()
        stream.set_data(b" " * 700 + f"BT /F1 12 Tf 10 50 Td (Page {index}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream.flate_encode())
    output = io.BytesIO()
    writer.write(output)
    with pytest.raises(AttachmentValidationError, match="展开|累计|过大"):
        prepare_attachment_bytes("cumulative.pdf", output.getvalue())


def test_utf16_docx_cannot_bypass_doctype_entity_rejection() -> None:
    xml = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<!DOCTYPE w:document [<!ENTITY injected "Expanded synthetic entity">]>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>&injected;</w:t></w:r></w:p></w:body></w:document>"
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", xml.encode("utf-16"))
    with pytest.raises(AttachmentValidationError):
        prepare_attachment_bytes("entity.docx", output.getvalue())


def test_parallel_attachment_writes_cannot_exceed_per_user_quota(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "quota.db")
    auth = AuthService(database)
    user = auth.register("quota-test-user", "only-test-password-123")
    chat = ChatService(database)
    first = chat.get_default_conversation(user.id)
    second = chat.create_conversation(user.id)
    attachment = prepare_attachment_bytes("quota.txt", b"1234567890")
    monkeypatch.setattr("ollama_chat_app.services.chat.MAX_USER_ATTACHMENT_BYTES", 10)
    barrier = threading.Barrier(2)

    def attempt(conversation_id):
        barrier.wait(timeout=5)
        try:
            chat.begin_message(
                user.id,
                "Synthetic question",
                conversation_id=conversation_id,
                attachments=(attachment,),
            )
            return "accepted"
        except AttachmentValidationError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [first.id, second.id]))
    assert sorted(results) == ["accepted", "rejected"]
    with database.connect() as connection:
        assert (
            connection.execute("SELECT SUM(length(content)) FROM chat_attachments").fetchone()[0]
            == 10
        )
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2


def test_deepseek_preflight_is_offline_and_applies_complete_request_limits(monkeypatch) -> None:
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Synthetic answer"}}]})

    provider = DeepSeekCloudProvider("mock-only", transport=httpx.MockTransport(handler))
    provider.chat(config.DEEPSEEK_DEFAULT_MODEL, [{"role": "user", "content": "Earlier question"}])
    previous_model = provider.last_used_model
    captured.clear()
    assert (
        provider.validate_request(
            config.DEEPSEEK_DEFAULT_MODEL, [{"role": "user", "content": "Question"}]
        )
        == config.DEEPSEEK_DEFAULT_MODEL
    )
    monkeypatch.setattr(config, "DEEPSEEK_MAX_REQUEST_BYTES", 100)
    with pytest.raises(ProviderError) as error:
        provider.validate_request(
            config.DEEPSEEK_DEFAULT_MODEL,
            [
                {"role": "user", "content": "Long history" * 100},
                {"role": "user", "content": "Follow-up question"},
            ],
        )
    assert error.value.code == "request_too_large"
    assert "新建对话" in str(error.value)
    assert provider.last_used_model == previous_model
    assert captured == []


def test_ui_oversized_history_preflight_does_not_append_a_failed_exchange(
    desktop_page,
    qtbot,
    monkeypatch,
) -> None:
    page = desktop_page
    # Scale down the request-size limit to check history + current payload
    # together without allocating a large attachment.
    attachment = prepare_attachment_bytes("source.txt", b"Synthetic historical text" * 10)
    previous = page.chat_service.begin_message(
        page.current_user.id,
        "Earlier question",
        conversation_id=page.current_conversation_id,
        attachments=(attachment,),
    )
    page.chat_service.complete_message(
        page.current_user.id, previous.assistant_message.id, "Answer"
    )
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Answer"}}]})

    provider = DeepSeekCloudProvider("mock-only", transport=httpx.MockTransport(handler))
    provider.chat(
        config.DEEPSEEK_DEFAULT_MODEL,
        page.chat_service.context_for_provider(
            page.current_user.id,
            conversation_id=page.current_conversation_id,
        ),
    )
    prior_size = len(captured[0].content)
    captured.clear()
    monkeypatch.setattr(config, "DEEPSEEK_MAX_REQUEST_BYTES", prior_size + 1)
    monkeypatch.setattr(
        page,
        "_selected_provider",
        lambda: (provider, "deepseek_cloud", config.DEEPSEEK_DEFAULT_MODEL),
    )
    monkeypatch.setattr(page, "_confirm_deepseek_attachments", lambda **_kwargs: True)
    page.message_input.setPlainText("New follow-up question that exceeds the small test budget")
    page.send_message()
    qtbot.waitUntil(lambda: not page._request_running)
    messages = page.chat_service.list_messages(
        page.current_user.id, conversation_id=page.current_conversation_id
    )
    assert len(messages) == 2
    assert messages[-1].content == "Answer"
    assert page.message_input.toPlainText().startswith("New follow-up")
    assert captured == []
