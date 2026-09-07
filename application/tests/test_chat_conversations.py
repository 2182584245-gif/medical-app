from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage

from ollama_chat_app.data.database import Database
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.chat import ChatService, ConversationNotFoundError
from ollama_chat_app.services.chat_attachments import (
    AttachmentValidationError,
    prepare_attachment_bytes,
)


@pytest.fixture
def chats(tmp_path):
    database = Database(tmp_path / "chats.db")
    auth = AuthService(database)
    user = auth.register("chat-owner", "synthetic-local-password")
    other = auth.register("other-owner", "another-synthetic-password")
    return ChatService(database), user, other


def test_conversations_keep_history_context_defaults_and_renames_isolated(chats) -> None:
    chat, user, other = chats
    first = chat.get_default_conversation(user.id)
    second = chat.create_conversation(user.id)
    old_turn = chat.begin_message(user.id, "default conversation question")
    chat.complete_message(user.id, old_turn.assistant_message.id, "default conversation answer")
    new_turn = chat.begin_message(user.id, "new conversation question", conversation_id=second.id)
    chat.complete_message(user.id, new_turn.assistant_message.id, "second answer")
    assert old_turn.conversation.id == first.id
    assert chat.get_default_conversation(user.id).id == first.id
    assert chat.get_conversation(user.id, second.id).title == "new conversation question"
    assert [turn.content for turn in chat.get_context(user.id)] == [
        "default conversation question",
        "default conversation answer",
    ]
    assert [turn.content for turn in chat.get_context(user.id, conversation_id=second.id)] == [
        "new conversation question",
        "second answer",
    ]
    renamed = chat.rename_conversation(user.id, second.id, "  资料讨论  ")
    assert renamed.title == "资料讨论"
    assert chat.list_conversations(user.id)[0].id == second.id
    with pytest.raises(ConversationNotFoundError):
        chat.rename_conversation(other.id, second.id, "不能改名")
    assert len(chat.list_conversations(other.id)) == 1


def test_second_attachment_insert_failure_rolls_back_whole_exchange(chats) -> None:
    chat, user, _ = chats
    first = prepare_attachment_bytes("first.txt", b"first")
    second = prepare_attachment_bytes("reject.txt", b"second")
    with chat.database.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER reject_test_attachment BEFORE INSERT ON chat_attachments "
            "WHEN NEW.original_name = 'reject.txt' BEGIN "
            "SELECT RAISE(ABORT, 'synthetic attachment failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic"):
        chat.begin_message(user.id, "must be atomic", attachments=(first, second))
    assert chat.list_messages(user.id) == []
    with chat.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM chat_attachments").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_attachment_tamper_fails_before_writing_any_turn(chats) -> None:
    chat, user, _ = chats
    attachment = prepare_attachment_bytes("notes.txt", b"original")
    for invalid in (
        replace(attachment, content=b"changed"),
        replace(attachment, original_name="../notes.txt"),
        replace(attachment, extraction_method="native_image"),
    ):
        with pytest.raises(AttachmentValidationError):
            chat.begin_message(user.id, "question", attachments=(invalid,))
    assert chat.list_messages(user.id) == []


def test_restart_restores_original_images_at_original_message_and_limits_context(chats) -> None:
    chat, user, _ = chats
    picture = QImage(4, 4, QImage.Format.Format_RGB32)
    picture.fill(0xCCABAA)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert picture.save(buffer, "PNG")
    raw = bytes(buffer.data())
    attachment = prepare_attachment_bytes("synthetic.png", raw)
    first = chat.begin_message(user.id, "describe this", attachments=(attachment,))
    chat.complete_message(user.id, first.assistant_message.id, "synthetic answer")
    followup = chat.begin_message(user.id, "what else?")
    chat.complete_message(user.id, followup.assistant_message.id, "followup answer")
    restored = ChatService(Database(chat.database.path))
    context = restored.context_for_provider(user.id)
    assert context[0]["images"] == [raw]
    assert "images" not in context[2]
    assert "synthetic.png" in context[0]["content"]
    assert restored.context_attachment_kinds(user.id) == {"image"}
    assert restored.context_attachment_kinds(user.id, limit=2) == set()
    assert all("images" not in turn for turn in restored.context_for_provider(user.id, limit=2))
    assert len(restored.list_message_attachments(user.id, first.user_message.id)) == 1
