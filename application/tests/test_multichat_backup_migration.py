from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

import ollama_chat_app.services.backup as backup_module
from ollama_chat_app.data.database import SCHEMA_VERSION, Database, DatabaseError
from ollama_chat_app.security.passwords import hash_password
from ollama_chat_app.services.backup import (
    DATA_DATABASE_PATH,
    DATA_MANIFEST_PATH,
    DATA_README_PATH,
    BackupValidationError,
    PortableBackupService,
)
from ollama_chat_app.services.chat import ChatError, ChatService
from ollama_chat_app.services.chat_attachments import prepare_attachment_bytes

_CREATED_AT = "2026-01-02T03:04:05.000000Z"


@pytest.fixture
def legacy_database(tmp_path: Path) -> Database:
    """Build the real pre-upgrade v4 schema, including its one-chat constraint."""
    database = Database(tmp_path / "legacy-v4.db")
    with database.transaction() as connection:
        Database._create_schema_v4(connection)
        password_hash = hash_password("synthetic-legacy-password-123")
        for user_id, username in ((7, "旧版会员"), (8, "另一会员")):
            connection.execute(
                """INSERT INTO users (
                    id, username, username_normalized, password_hash,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, username, username, password_hash, _CREATED_AT, _CREATED_AT),
            )
            connection.execute(
                """INSERT INTO conversations (
                    id, user_id, title, provider, model, created_at, updated_at
                ) VALUES (?, ?, ?, 'local', 'synthetic-model', ?, ?)""",
                (user_id + 10, user_id, f"{username}的旧会话", _CREATED_AT, _CREATED_AT),
            )
        for message_id, conversation_id, sequence, role, content in (
            (27, 17, 1, "user", "保留旧问题"),
            (28, 17, 2, "assistant", "保留旧回答"),
            (29, 18, 1, "user", "另一会员的消息"),
        ):
            connection.execute(
                """INSERT INTO messages (
                    id, conversation_id, sequence_no, role, content, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'complete', ?, ?)""",
                (message_id, conversation_id, sequence, role, content, _CREATED_AT, _CREATED_AT),
            )
        connection.execute(
            """INSERT INTO member_profiles (user_id, display_name, created_at, updated_at)
            VALUES (7, '保留个人档案', ?, ?)""",
            (_CREATED_AT, _CREATED_AT),
        )
        file_content = "合成的历史健康报告原文\n".encode()
        file_hash = hashlib.sha256(file_content).hexdigest()
        connection.execute(
            """INSERT INTO user_files (
                id, user_id, relative_path, original_name, media_type,
                size_bytes, sha256, created_at
            ) VALUES (71, 7, 'documents/legacy.txt', '历史报告.txt', 'text/plain', ?, ?, ?)""",
            (len(file_content), file_hash, _CREATED_AT),
        )
        connection.execute(
            """INSERT INTO user_file_contents (file_id, content, mime_type, sha256, created_at)
            VALUES (71, ?, 'text/plain', ?, ?)""",
            (file_content, file_hash, _CREATED_AT),
        )
        connection.execute(
            """INSERT INTO medical_reports (
                id, user_id, file_id, summary, created_at, updated_at
            ) VALUES (81, 7, 71, '历史报告摘要', ?, ?)""",
            (_CREATED_AT, _CREATED_AT),
        )
        connection.execute(
            """INSERT INTO report_items (
                id, report_id, item_name, result_value, created_at, updated_at
            ) VALUES (91, 81, '合成指标', '100', ?, ?)""",
            (_CREATED_AT, _CREATED_AT),
        )
        connection.execute(
            """INSERT INTO products (
                id, sku, name, category, price_cents, created_by_user_id,
                created_at, updated_at
            ) VALUES (111, 'SYNTHETIC-111', '合成商品', '测试', 1200, 8, ?, ?)""",
            (_CREATED_AT, _CREATED_AT),
        )
        connection.execute(
            """INSERT INTO product_recommendations (
                id, product_id, member_user_id, reason, recommendation_source,
                created_at, updated_at
            ) VALUES (121, 111, 7, '保留历史推荐', 'catalogue', ?, ?)""",
            (_CREATED_AT, _CREATED_AT),
        )
        connection.execute(
            """INSERT INTO orders (
                id, order_no, member_user_id, product_id, recommendation_id,
                product_name_snapshot, quantity, unit_price_cents, total_amount_cents,
                created_at, updated_at
            ) VALUES (131, 'SYNTHETIC-ORDER', 7, 111, 121, '合成商品', 2, 1200, 2400, ?, ?)""",
            (_CREATED_AT, _CREATED_AT),
        )
        connection.execute(
            """INSERT INTO audit_logs (
                id, actor_user_id, action, entity_type, entity_id, details_json, created_at
            ) VALUES (141, 7, 'synthetic.audit', 'order', 131, '{"retained":true}', ?)""",
            (_CREATED_AT,),
        )
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    return database


def _all_rows(database: Database) -> dict[str, list[tuple]]:
    with database.connect() as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' ORDER BY name"
        ).fetchall()
        return {
            str(table[0]): [
                tuple(row)
                for row in connection.execute(f'SELECT * FROM "{table[0]}" ORDER BY rowid')
            ]
            for table in tables
        }


def _assert_rows_preserved(database: Database, expected: dict[str, list[tuple]]) -> None:
    actual = _all_rows(database)
    for table, rows in expected.items():
        assert actual[table] == rows, f"migration or import changed rows in {table}"
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 6
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA quick_check").fetchall()[0][0] == "ok"


def _write_database_archive(database: Database, destination: Path, version: int) -> None:
    payload = database.path.read_bytes()
    manifest = backup_module._build_manifest(
        created_at=datetime.now(UTC),
        bundle_id=str(uuid.uuid4()),
        backup_kind="manual_export",
        schema_version=version,
        database_sha256=hashlib.sha256(payload).hexdigest(),
        database_size=len(payload),
    )
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(DATA_DATABASE_PATH.as_posix(), payload)
        archive.writestr(DATA_MANIFEST_PATH.as_posix(), manifest)
        archive.writestr(DATA_README_PATH.as_posix(), backup_module._readme_text())


def test_real_v4_migration_preserves_all_rows_and_allows_multiple_chats(
    legacy_database: Database,
) -> None:
    with (
        pytest.raises(sqlite3.IntegrityError, match="UNIQUE"),
        legacy_database.transaction() as connection,
    ):
        connection.execute(
            """INSERT INTO conversations (user_id, title, created_at, updated_at)
            VALUES (7, '升级前不允许第二会话', ?, ?)""",
            (_CREATED_AT, _CREATED_AT),
        )
    expected = _all_rows(legacy_database)

    legacy_database.initialize()
    legacy_database.initialize()

    _assert_rows_preserved(legacy_database, expected)
    with legacy_database.transaction() as connection:
        connection.execute(
            """INSERT INTO conversations (id, user_id, title, created_at, updated_at)
            VALUES (19, 7, '升级后的第二会话', ?, ?)""",
            (_CREATED_AT, _CREATED_AT),
        )
        assert [row[0] for row in connection.execute(
            "SELECT id FROM conversations WHERE user_id = 7 ORDER BY id"
        )] == [17, 19]
        assert connection.execute("PRAGMA foreign_key_list(messages)").fetchone()[2] == (
            "conversations"
        )
    after_new_chat = _all_rows(legacy_database)
    legacy_database.initialize()
    _assert_rows_preserved(legacy_database, after_new_chat)


def test_failed_v4_migration_rolls_back_schema_and_preserves_original_rows(
    legacy_database: Database,
) -> None:
    with legacy_database.connect() as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("UPDATE messages SET conversation_id = 999 WHERE id = 27")
    expected = _all_rows(legacy_database)

    with pytest.raises(DatabaseError, match="外键"):
        legacy_database.initialize()

    assert _all_rows(legacy_database) == expected
    with legacy_database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute(
            "SELECT name FROM sqlite_schema WHERE name IN ('chat_attachments', 'conversations_v5')"
        ).fetchall() == []


@pytest.mark.parametrize("historical_archive", [True, False])
def test_v4_archives_import_as_v5_without_changing_the_legacy_source(
    legacy_database: Database, tmp_path: Path, historical_archive: bool
) -> None:
    expected = _all_rows(legacy_database)
    legacy_bytes = legacy_database.path.read_bytes()
    archive_path = tmp_path / "legacy-backup.zip"
    if historical_archive:
        _write_database_archive(legacy_database, archive_path, 4)
    else:
        PortableBackupService(legacy_database).create_archive(archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read(DATA_MANIFEST_PATH.as_posix()))
    assert manifest["schema_version"] == (4 if historical_archive else SCHEMA_VERSION)

    destination = Database(tmp_path / "current" / "app.db")
    destination.initialize()
    original_destination = _all_rows(destination)
    result = PortableBackupService(destination).import_archive(archive_path)

    assert result.user_count == 2
    assert result.message_count == 3
    assert legacy_database.path.read_bytes() == legacy_bytes
    _assert_rows_preserved(destination, expected)
    assert result.safety_backup_path.is_file()
    restored_safety = Database(tmp_path / "safety" / "app.db")
    restored_safety.initialize()
    PortableBackupService(restored_safety).import_archive(result.safety_backup_path)
    _assert_rows_preserved(restored_safety, original_destination)


def test_attachment_bytes_and_text_survive_backup_with_two_independent_conversations(
    legacy_database: Database, tmp_path: Path
) -> None:
    chat = ChatService(legacy_database)
    second = chat.create_conversation(7, "健康资料讨论")
    first_file = prepare_attachment_bytes("旧会话附件.txt", "第一份原文\r\n".encode())
    second_file = prepare_attachment_bytes("资料.md", "# 第二份资料\n独立会话的文字🙂\n".encode())
    first_exchange = chat.begin_message(
        7, "第一会话问题", conversation_id=17, attachments=(first_file,)
    )
    second_exchange = chat.begin_message(
        7, "第二会话问题", conversation_id=second.id, attachments=(second_file,)
    )
    chat.complete_message(7, first_exchange.assistant_message.id, "第一会话回答")
    chat.complete_message(7, second_exchange.assistant_message.id, "第二会话回答")
    expected = _all_rows(legacy_database)
    assert len(expected["chat_attachments"]) == 2
    archive_path = tmp_path / "with-attachments.zip"
    PortableBackupService(legacy_database).create_archive(archive_path)

    destination = Database(tmp_path / "restored" / "app.db")
    destination.initialize()
    PortableBackupService(destination).import_archive(archive_path)

    _assert_rows_preserved(destination, expected)
    restored = ChatService(destination)
    first_context = restored.context_for_provider(7, conversation_id=17)
    second_context = restored.context_for_provider(7, conversation_id=second.id)
    first_text = "\n".join(turn["content"] for turn in first_context)
    second_text = "\n".join(turn["content"] for turn in second_context)
    assert first_file.extracted_text in first_text
    assert second_file.extracted_text not in first_text
    assert second_file.extracted_text in second_text
    assert first_file.extracted_text not in second_text
    with destination.connect() as connection:
        attachment = connection.execute(
            "SELECT content, extracted_text, sha256 FROM chat_attachments WHERE message_id = ?",
            (second_exchange.user_message.id,),
        ).fetchone()
        assert bytes(attachment["content"]) == second_file.content
        assert attachment["extracted_text"] == second_file.extracted_text
        assert attachment["sha256"] == hashlib.sha256(second_file.content).hexdigest()
    with destination.transaction() as connection:
        connection.execute("DELETE FROM conversations WHERE id = ?", (second.id,))
        assert connection.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (second.id,)
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM chat_attachments WHERE message_id = ?",
            (second_exchange.user_message.id,),
        ).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM chat_attachments").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM user_file_contents").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_other_member_cannot_read_or_attach_to_an_owned_conversation(
    legacy_database: Database,
) -> None:
    chat = ChatService(legacy_database)
    attachment = prepare_attachment_bytes("私有资料.txt", "仅会话所有者可见".encode())
    expected = _all_rows(legacy_database)

    with pytest.raises(ChatError):
        chat.begin_message(8, "越权附加", conversation_id=17, attachments=(attachment,))
    with pytest.raises(ChatError):
        chat.list_messages(8, conversation_id=17)
    with pytest.raises(ChatError):
        chat.context_for_provider(8, conversation_id=17)

    assert _all_rows(legacy_database) == expected


@pytest.mark.parametrize("invalid_structure", ["extra-trigger", "orphan-attachment"])
def test_import_rejects_invalid_v5_structure_before_replacing_or_backing_up_current_data(
    legacy_database: Database, tmp_path: Path, monkeypatch, invalid_structure: str
) -> None:
    legacy_database.initialize()
    with legacy_database.connect() as connection:
        if invalid_structure == "extra-trigger":
            connection.execute(
                """CREATE TRIGGER unexpected_attachment_trigger
                AFTER INSERT ON chat_attachments BEGIN
                    UPDATE messages SET content = 'tampered' WHERE id = NEW.message_id;
                END"""
            )
        else:
            connection.execute("PRAGMA foreign_keys = OFF")
            content = b"orphan synthetic attachment"
            connection.execute(
                """INSERT INTO chat_attachments (
                    message_id, original_name, media_type, content, extracted_text,
                    extraction_method, sha256, created_at
                ) VALUES (999, 'orphan.txt', 'text/plain', ?, 'orphan', 'local_text', ?, ?)""",
                (content, hashlib.sha256(content).hexdigest(), _CREATED_AT),
            )
    malformed_archive = tmp_path / "malformed.zip"
    _write_database_archive(legacy_database, malformed_archive, 5)
    current = Database(tmp_path / "current" / "app.db")
    current.initialize()
    before_bytes = current.path.read_bytes()
    service = PortableBackupService(current)

    def unexpected_safety_backup(*_args, **_kwargs):
        raise AssertionError("invalid input must be rejected before creating a safety backup")

    monkeypatch.setattr(service, "_create_archive", unexpected_safety_backup)
    with pytest.raises(BackupValidationError):
        service.import_archive(malformed_archive)

    assert current.path.read_bytes() == before_bytes
    assert list(current.path.parent.iterdir()) == [current.path]
