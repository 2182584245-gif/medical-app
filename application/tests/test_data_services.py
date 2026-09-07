from __future__ import annotations

from pathlib import Path

import pytest

from ollama_chat_app.data.database import Database
from ollama_chat_app.services.auth import (
    LOGIN_ERROR_MESSAGE,
    AuthenticationError,
    AuthService,
    RegistrationError,
)
from ollama_chat_app.services.chat import ChatService, MessageNotFoundError


@pytest.fixture
def services(tmp_path: Path) -> tuple[Database, AuthService, ChatService]:
    database = Database(tmp_path / "app.sqlite3")
    database.initialize()
    return database, AuthService(database), ChatService(database)


def test_registration_normalization_uniform_login_errors_and_no_plaintext_password(
    services: tuple[Database, AuthService, ChatService],
) -> None:
    database, auth, _chat = services
    password = "Plaintext-Secret-987654!"

    user = auth.register("  ＡlＩcＥ  ", password)

    assert user.username == "AlIcE"
    assert user.username_normalized == "alice"
    assert auth.authenticate(" ａＬｉＣｅ ", password).id == user.id

    with pytest.raises(RegistrationError, match="用户名已存在"):
        auth.register("ALICE", "Another-safe-password-123!")

    with pytest.raises(AuthenticationError) as wrong_password:
        auth.authenticate("alice", "this-password-is-wrong")
    with pytest.raises(AuthenticationError) as missing_user:
        auth.authenticate("somebody-else", "this-password-is-wrong")

    assert str(wrong_password.value) == LOGIN_ERROR_MESSAGE
    assert str(missing_user.value) == LOGIN_ERROR_MESSAGE

    with database.connect() as connection:
        stored = connection.execute(
            "SELECT password_hash FROM users WHERE id = ?",
            (user.id,),
        ).fetchone()

    assert stored is not None
    password_hash = str(stored["password_hash"])
    assert password_hash.startswith("$argon2id$")
    assert password_hash != password
    assert password.encode("utf-8") not in database.path.read_bytes()


def test_begin_then_complete_persists_provider_model_and_context(
    services: tuple[Database, AuthService, ChatService],
) -> None:
    _database, auth, chat = services
    user = auth.register("alice", "correct-horse-battery-staple")

    exchange = chat.begin_message(
        user.id,
        "你好",
        provider="  local  ",
        model="  qwen2.5:3b  ",
    )

    assert exchange.conversation.provider == "local"
    assert exchange.conversation.model == "qwen2.5:3b"
    assert exchange.user_message.role == "user"
    assert exchange.user_message.status == "complete"
    assert exchange.assistant_message.role == "assistant"
    assert exchange.assistant_message.status == "pending"
    assert exchange.assistant_message.provider == "local"
    assert exchange.assistant_message.model == "qwen2.5:3b"

    completed = chat.complete_message(
        user.id,
        exchange.assistant_message.id,
        "你好！有什么可以帮你？",
    )

    assert completed.status == "complete"
    assert completed.error_message is None
    assert completed.provider == "local"
    assert completed.model == "qwen2.5:3b"
    assert [(message.role, message.status) for message in chat.list_messages(user.id)] == [
        ("user", "complete"),
        ("assistant", "complete"),
    ]
    assert chat.context_for_provider(user.id) == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好！有什么可以帮你？"},
    ]

    legacy_call = chat.begin_message(user.id, "继续")
    assert legacy_call.conversation.provider == "local"
    assert legacy_call.conversation.model == "qwen2.5:3b"
    assert legacy_call.assistant_message.provider == "local"
    assert legacy_call.assistant_message.model == "qwen2.5:3b"


def test_begin_then_error_records_failure_and_excludes_it_from_context(
    services: tuple[Database, AuthService, ChatService],
) -> None:
    _database, auth, chat = services
    user = auth.register("alice", "correct-horse-battery-staple")

    exchange = chat.begin_message(
        user.id,
        "请回答",
        provider="cloud",
        model="qwen3:8b-cloud",
    )
    failed = chat.fail_message(
        user.id,
        exchange.assistant_message.id,
        "  network\n   unavailable  ",
    )

    assert failed.status == "error"
    assert failed.content == ""
    assert failed.error_message == "network unavailable"
    assert failed.provider == "cloud"
    assert failed.model == "qwen3:8b-cloud"
    assert chat.context_for_provider(user.id) == [
        {"role": "user", "content": "请回答"},
    ]


def test_messages_are_isolated_between_users(
    services: tuple[Database, AuthService, ChatService],
) -> None:
    _database, auth, chat = services
    alice = auth.register("alice", "correct-horse-battery-staple")
    bob = auth.register("bob", "another-correct-password")
    exchange = chat.begin_message(
        alice.id,
        "Alice private message",
        provider="local",
        model="qwen2.5:3b",
    )

    assert chat.get_message(bob.id, exchange.user_message.id) is None
    assert chat.get_message(bob.id, exchange.assistant_message.id) is None
    assert chat.list_messages(bob.id) == []
    assert chat.context_for_provider(bob.id) == []
    assert chat.get_default_conversation(bob.id).id != exchange.conversation.id

    with pytest.raises(MessageNotFoundError):
        chat.complete_message(
            bob.id,
            exchange.assistant_message.id,
            "Bob must not be able to complete Alice's message",
        )
    with pytest.raises(MessageNotFoundError):
        chat.fail_message(
            bob.id,
            exchange.assistant_message.id,
            "Bob must not be able to fail Alice's message",
        )

    assert chat.get_message(alice.id, exchange.assistant_message.id) is not None
