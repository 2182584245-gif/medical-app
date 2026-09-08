from __future__ import annotations

import os
import threading

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import pytest
from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QFileDialog, QInputDialog

from ollama_chat_app.data.database import Database
from ollama_chat_app.security.secret_store import SecretStore
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.chat import ChatService
from ollama_chat_app.services.chat_attachments import prepare_attachment_bytes
from ollama_chat_app.ui.chat_page import ChatPage


@pytest.fixture
def page(qtbot, tmp_path):
    database = Database(tmp_path / "ui.db")
    auth = AuthService(database)
    user = auth.register("test-member", "synthetic-password-only")
    widget = ChatPage(ChatService(database), SecretStore())
    qtbot.addWidget(widget)
    widget.start_session(user)
    widget.show()
    return widget


def _select_conversation(page, identifier):
    for index in range(page.conversation_list.count()):
        item = page.conversation_list.item(index)
        if item.data(Qt.ItemDataRole.UserRole) == identifier:
            page.conversation_list.setCurrentItem(item)
            return
    raise AssertionError("conversation missing")


def test_create_switch_rename_and_keep_drafts_separate(page, monkeypatch) -> None:
    first = page.current_conversation_id
    attachment = prepare_attachment_bytes("first.txt", "第一对话附件".encode())
    page.message_input.setPlainText("第一对话草稿")
    page._pending_attachments = [attachment]
    page._create_conversation()
    second = page.current_conversation_id
    assert first != second
    assert page.conversation_list.count() == 2
    assert page.message_input.toPlainText() == ""
    assert page._pending_attachments == []
    page.message_input.setPlainText("第二对话草稿")
    monkeypatch.setattr(QInputDialog, "getText", lambda *_a, **_k: ("资料讨论", True))
    page._rename_conversation()
    assert page.conversation_list.currentItem().text() == "资料讨论"
    _select_conversation(page, first)
    assert page.message_input.toPlainText() == "第一对话草稿"
    assert page._pending_attachments == [attachment]
    _select_conversation(page, second)
    assert page.message_input.toPlainText() == "第二对话草稿"
    assert page._pending_attachments == []


def test_model_runs_in_worker_and_cannot_switch_or_double_send(page, qtbot, monkeypatch) -> None:
    started, release = threading.Event(), threading.Event()
    calls = []
    main_thread = threading.get_ident()

    class Provider:
        supports_images = True

        def chat(self, model, messages):
            calls.append((model, messages, threading.get_ident()))
            started.set()
            assert release.wait(10)
            return "合成模型回答"

    monkeypatch.setattr(page, "_selected_provider", lambda: (Provider(), "ollama_local", "fake"))
    page.message_input.setPlainText("测试问题")
    current = page.current_conversation_id
    page.send_message()
    try:
        qtbot.waitUntil(started.is_set)
        assert not page.conversation_list.isEnabled()
        assert not page.new_conversation_button.isEnabled()
        assert not page.logout_button.isEnabled()
        page._create_conversation()
        page.send_message()
        assert page.current_conversation_id == current
        assert len(calls) == 1
        assert calls[0][2] != main_thread
    finally:
        release.set()
        qtbot.waitUntil(lambda: not page._request_running)
    assert page.message_input.toPlainText() == ""
    assert len(page.message_list._bubbles) == 2
    assert page.conversation_list.isEnabled()


def test_failed_send_keeps_text_and_attachments_and_links_them_to_failed_turn(
    page, qtbot, monkeypatch
) -> None:
    attachment = prepare_attachment_bytes("notes.txt", "原始参考资料".encode())
    page._pending_attachments = [attachment]
    page.message_input.setPlainText("请解释资料")
    confirmations = []

    class Provider:
        supports_images = True

        def chat(self, *_args):
            raise RuntimeError("合成网络失败")

    monkeypatch.setattr(page, "_selected_provider", lambda: (Provider(), "deepseek_cloud", "fake"))
    monkeypatch.setattr(
        page, "_confirm_deepseek_attachments", lambda **kwargs: confirmations.append(kwargs) or True
    )
    page.send_message()
    qtbot.waitUntil(lambda: not page._request_running)
    assert page.message_input.toPlainText() == "请解释资料"
    assert page._pending_attachments == [attachment]
    assert confirmations == [{"has_images": False}]
    messages = page.chat_service.list_messages(
        page.current_user.id, conversation_id=page.current_conversation_id
    )
    assert messages[-1].status == "error"
    metadata = page.chat_service.list_message_attachments(page.current_user.id, messages[0].id)
    assert metadata[0]["original_name"] == "notes.txt"
    assert "notes.txt" in page.message_list._bubbles[messages[0].id].content_label.text()


def test_cancelled_upload_does_not_persist_or_send_and_other_provider_blocks_files(
    page, monkeypatch
) -> None:
    attachment = prepare_attachment_bytes("private.txt", b"synthetic private document")
    page._pending_attachments = [attachment]
    page.message_input.setPlainText("保留输入")

    class Provider:
        supports_images = True

        def chat(self, *_args):
            raise AssertionError("must not send")

    monkeypatch.setattr(page, "_selected_provider", lambda: (Provider(), "deepseek_cloud", "fake"))
    monkeypatch.setattr(page, "_confirm_deepseek_attachments", lambda **_kwargs: False)
    page.send_message()
    assert not page._tasks
    assert page.chat_service.list_messages(page.current_user.id) == []
    assert page.message_input.toPlainText() == "保留输入"
    assert page._pending_attachments == [attachment]
    monkeypatch.setattr(page, "_selected_provider", lambda: (Provider(), "ollama_cloud", "fake"))
    page.send_message()
    assert "切回 DeepSeek" in page.status_label.text()
    assert not page._tasks


def test_attachment_parse_is_off_ui_thread_and_failed_batch_keeps_previous(
    page, qtbot, monkeypatch, tmp_path
) -> None:
    source = tmp_path / "new.txt"
    source.write_text("new file", encoding="utf-8")
    existing = prepare_attachment_bytes("existing.txt", b"keep existing")
    page._pending_attachments = [existing]
    page.message_input.setPlainText("keep draft")
    started, release = threading.Event(), threading.Event()
    thread_ids = []

    def parse(_source):
        thread_ids.append(threading.get_ident())
        started.set()
        assert release.wait(10)
        raise ValueError("synthetic parse failure")

    monkeypatch.setattr("ollama_chat_app.ui.chat_page.prepare_attachment", parse)
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *_a: ([str(source)], ""))
    page._choose_attachments(images_only=False)
    try:
        qtbot.waitUntil(started.is_set)
        assert page._attachment_processing
        assert not page.conversation_list.isEnabled()
        assert thread_ids[0] != threading.get_ident()
    finally:
        release.set()
        qtbot.waitUntil(lambda: not page._attachment_processing)
    assert page._pending_attachments == [existing]
    assert page.message_input.toPlainText() == "keep draft"
    assert "synthetic parse failure" in page.status_label.text()


def test_request_callback_cannot_write_into_a_new_session(page, qtbot, monkeypatch) -> None:
    started, release = threading.Event(), threading.Event()
    original_user = page.current_user
    original_conversation = page.current_conversation_id

    class Provider:
        supports_images = True

        def chat(self, *_args):
            started.set()
            assert release.wait(10)
            return "original session answer"

    monkeypatch.setattr(page, "_selected_provider", lambda: (Provider(), "ollama_local", "fake"))
    page.message_input.setPlainText("original session question")
    page.send_message()
    try:
        qtbot.waitUntil(started.is_set)
        page.end_session()
        new_user = AuthService(page.chat_service.database).register(
            "second-member", "another-password"
        )
        page.start_session(new_user)
        page.message_input.setPlainText("new session draft")
    finally:
        release.set()
        qtbot.waitUntil(lambda: not page._request_running)
    assert page.current_user.id == new_user.id
    assert page.message_input.toPlainText() == "new session draft"
    assert page.message_list._bubbles == {}
    old_messages = page.chat_service.list_messages(
        original_user.id, conversation_id=original_conversation
    )
    assert old_messages[-1].content == "original session answer"


def test_followup_reuses_persisted_image_and_requests_fresh_upload_consent(
    page, qtbot, monkeypatch
) -> None:
    image = QImage(4, 4, QImage.Format.Format_RGB32)
    image.fill(0xABCDEF)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    raw = bytes(buffer.data())
    page._pending_attachments = [prepare_attachment_bytes("图.png", raw)]
    page.message_input.setPlainText("描述图片")
    calls, confirmations = [], []

    class Provider:
        supports_images = True

        def resolve_model(self, model, messages):
            return "vision" if any(message.get("images") for message in messages) else model

        def chat(self, model, messages):
            calls.append((model, messages))
            return "图中为合成色块"

    monkeypatch.setattr(page, "_selected_provider", lambda: (Provider(), "deepseek_cloud", "text"))
    monkeypatch.setattr(
        page, "_confirm_deepseek_attachments", lambda **kwargs: confirmations.append(kwargs) or True
    )
    page.send_message()
    qtbot.waitUntil(lambda: not page._request_running)
    assert page._pending_attachments == []
    page.message_input.setPlainText("刚才图片里还有什么？")
    page.send_message()
    qtbot.waitUntil(lambda: not page._request_running)
    assert [call[0] for call in calls] == ["vision", "vision"]
    # Personalization adds a system turn; the historical user image is still
    # included verbatim and still requires fresh upload consent.
    historical_user = next(message for message in calls[1][1] if message["role"] == "user")
    assert historical_user["images"] == [raw]
    assert "images" not in calls[1][1][-1]
    assert confirmations == [{"has_images": True}, {"has_images": True}]
    messages = page.chat_service.list_messages(page.current_user.id)
    assert [message.model for message in messages if message.role == "assistant"] == [
        "vision",
        "vision",
    ]


def test_provider_budget_validation_failure_does_not_persist_new_attachment(
    page, qtbot, monkeypatch
) -> None:
    page._pending_attachments = [prepare_attachment_bytes("资料.txt", b"synthetic text")]
    page.message_input.setPlainText("question")

    class Provider:
        supports_images = True

        def validate_request(self, *_args):
            raise ValueError("请求大小超限，请新建对话")

        def chat(self, *_args):
            raise AssertionError("must not send")

    monkeypatch.setattr(page, "_selected_provider", lambda: (Provider(), "deepseek_cloud", "fake"))
    monkeypatch.setattr(page, "_confirm_deepseek_attachments", lambda **_kwargs: True)
    page.send_message()
    qtbot.waitUntil(lambda: not page._request_running)
    assert page.chat_service.list_messages(page.current_user.id) == []
    assert len(page._pending_attachments) == 1
    assert "新建对话" in page.status_label.text()


def test_long_chinese_bubbles_wrap_with_sufficient_height_at_narrow_width(page, qtbot) -> None:
    page.resize(980, 680)
    content = "这是一条较长的中文测试消息，用于确认对话内容完整换行，不能被硬裁切。" * 10
    bubble = page.message_list.add_message(999, "assistant", content)
    qtbot.waitUntil(lambda: bubble.isVisible())
    page.message_list._resize_bubbles()
    assert bubble.content_label.text() == content
    assert bubble.content_label.minimumHeight() >= bubble.content_label.heightForWidth(
        bubble.width() - 32
    )
    assert bubble.content_label.height() >= bubble.content_label.fontMetrics().height() * 3
    assert bubble.width() <= page.message_list.viewport().width()
