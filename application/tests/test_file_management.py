from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path

import pytest

from ollama_chat_app.data.database import SCHEMA_VERSION, Database
from ollama_chat_app.services import file_management as file_module
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.backup import DATA_DATABASE_PATH, PortableBackupService
from ollama_chat_app.services.file_management import (
    FileInUseError,
    FileManagementService,
    FileNotFoundError,
    FileValidationError,
    ReportStateError,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"portable-image-data"
PDF_BYTES = b"%PDF-1.4\n% portable report\n%%EOF\n"


def _member(database: Database, name: str = "alice"):
    return AuthService(database).register(name, "correct-horse-battery-staple")


def test_v3_to_current_migration_preserves_file_features_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "legacy-v3.db"
    with sqlite3.connect(path) as connection:
        Database._create_schema_v3(connection)
        connection.execute(
            """
            INSERT INTO users (
                username, username_normalized, password_hash, created_at,
                role_code, account_status, updated_at
            ) VALUES ('旧用户', '旧用户', 'hash', '2026-01-01T00:00:00.000000Z',
                      'member', 'active', '2026-01-01T00:00:00.000000Z')
            """
        )
        user_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.execute(
            """
            INSERT INTO reminders (
                user_id, title, reminder_type, scheduled_at, status, created_at, updated_at
            ) VALUES (?, '喝水', 'water', '2030-01-01T00:00:00.000000Z', 'active',
                      '2026-01-01T00:00:00.000000Z', '2026-01-01T00:00:00.000000Z')
            """,
            (user_id,),
        )
        connection.commit()

    database = Database(path)
    database.initialize()
    database.initialize()

    with database.connect() as connection:
        reminder = connection.execute(
            "SELECT title, repeat_rule, source, paused_until FROM reminders"
        ).fetchone()
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            ).fetchall()
        }
        item_columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(report_items)")
        }
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION

    assert SCHEMA_VERSION == 5
    assert tuple(reminder) == ("喝水", "none", "manual", None)
    assert "user_file_contents" in tables
    assert "chat_attachments" in tables
    assert {"status", "updated_at"} <= item_columns


def test_upload_validates_signature_permissions_and_exports_copy(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    alice = _member(database)
    bob = _member(database, "bob")
    service = FileManagementService(database)

    file_id = service.upload_bytes(alice.id, "饮食照片.png", PNG_BYTES)
    metadata = service.list_files(alice.id)
    assert [(item["id"], item["media_type"], item["size_bytes"]) for item in metadata] == [
        (file_id, "image/png", len(PNG_BYTES))
    ]
    assert service.get_file(alice.id, file_id)["content"] == PNG_BYTES
    assert service.list_files(bob.id) == []
    with pytest.raises(FileNotFoundError):
        service.get_file(bob.id, file_id)

    exported = service.export_file(alice.id, file_id, tmp_path / "copy.png")
    assert exported.read_bytes() == PNG_BYTES
    with pytest.raises(FileValidationError, match="已存在"):
        service.export_file(alice.id, file_id, exported)

    with pytest.raises(FileValidationError, match="扩展名与实际内容"):
        service.upload_bytes(alice.id, "伪装.pdf", PNG_BYTES)
    with pytest.raises(FileValidationError, match="只支持"):
        service.upload_bytes(alice.id, "程序.exe", b"MZ" + b"x" * 20)


def test_size_and_per_user_quota_are_enforced(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    alice = _member(database)
    service = FileManagementService(database)

    monkeypatch.setattr(file_module, "MAX_USER_STORAGE", len(PNG_BYTES) + 3)
    service.upload_bytes(alice.id, "one.png", PNG_BYTES)
    with pytest.raises(FileValidationError, match="100 MB"):
        service.upload_bytes(alice.id, "two.png", PNG_BYTES)

    monkeypatch.setattr(file_module, "MAX_FILE_SIZE", 4)
    with pytest.raises(FileValidationError, match="15 MB"):
        service.upload_bytes(alice.id, "large.png", PNG_BYTES)


def test_report_draft_confirmation_archive_and_original_preservation(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    alice = _member(database)
    service = FileManagementService(database)
    file_id = service.upload_bytes(alice.id, "体检报告.pdf", PDF_BYTES)
    report_id = service.create_report_draft(
        alice.id,
        file_id,
        report_type="体检报告",
        report_date="2026-09-01",
        institution="本地示例机构",
        summary="仅记录原报告已有信息，等待用户确认。",
    )
    item_id = service.add_report_item_draft(
        alice.id,
        report_id,
        "示例指标",
        result_value="5.0",
        unit="mmol/L",
        reference_range="3.0-6.0",
        flag="normal",
    )

    report = service.get_report(alice.id, report_id)
    assert report["status"] == "draft"
    assert report["items"][0]["status"] == "draft"
    with pytest.raises(FileInUseError, match="报告原件"):
        service.delete_file(alice.id, file_id)

    service.confirm_report_item(alice.id, report_id, item_id)
    assert service.get_report(alice.id, report_id)["items"][0]["status"] == "confirmed"
    service.confirm_report(alice.id, report_id)
    assert service.get_report(alice.id, report_id)["status"] == "confirmed"
    with pytest.raises(ReportStateError, match="已经确认"):
        service.add_report_item_draft(alice.id, report_id, "不能再写入")
    service.archive_report(alice.id, report_id)
    assert service.list_reports(alice.id) == []
    assert service.list_reports(alice.id, include_archived=True)[0]["status"] == "archived"

    service.delete_report(alice.id, report_id)
    service.delete_file(alice.id, file_id)
    assert service.list_files(alice.id) == []


def test_backup_round_trip_includes_embedded_file_bytes(tmp_path: Path) -> None:
    source = Database(tmp_path / "source" / "app.db")
    source.initialize()
    alice = _member(source)
    source_files = FileManagementService(source)
    file_id = source_files.upload_bytes(alice.id, "照片.png", PNG_BYTES)
    archive = tmp_path / "all-data.zip"
    PortableBackupService(source).create_archive(archive)

    with zipfile.ZipFile(archive) as backup:
        restored_path = tmp_path / "restored-direct.db"
        restored_path.write_bytes(backup.read(DATA_DATABASE_PATH.as_posix()))
    restored = Database(restored_path)
    restored.initialize()
    restored_file = FileManagementService(restored).get_file(alice.id, file_id)
    assert restored_file["content"] == PNG_BYTES

    current = Database(tmp_path / "current" / "app.db")
    current.initialize()
    _member(current, "current-user")
    PortableBackupService(current).import_archive(archive)
    assert FileManagementService(current).get_file(alice.id, file_id)["content"] == PNG_BYTES


def test_corrupt_blob_is_detected_before_export(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    alice = _member(database)
    service = FileManagementService(database)
    file_id = service.upload_bytes(alice.id, "照片.png", PNG_BYTES)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE user_file_contents SET content = ? WHERE file_id = ?",
            (sqlite3.Binary(PNG_BYTES + b"changed"), file_id),
        )
    with pytest.raises(Exception, match="完整性"):
        service.export_file(alice.id, file_id, tmp_path / "must-not-exist.png")
    assert not (tmp_path / "must-not-exist.png").exists()


def test_image_ocr_returns_unconfirmed_candidates_without_writing_report(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "app.db")
    database.initialize()
    alice = _member(database)

    def fake_ocr(_content: bytes):
        return (
            [
                ([(0, 0)], "示例指标 5.2 mmol/L 3.0-6.0", 0.99),
                ([(0, 0)], "这是原报告中的普通文字", 0.95),
            ],
            0.01,
        )

    service = FileManagementService(database, ocr_engine=fake_ocr)
    file_id = service.upload_bytes(alice.id, "报告照片.png", PNG_BYTES)
    extraction = service.extract_text(alice.id, file_id)

    assert extraction["text"] == "示例指标 5.2 mmol/L 3.0-6.0\n这是原报告中的普通文字"
    assert extraction["candidate_items"] == [
        {
            "item_name": "示例指标",
            "result_value": "5.2",
            "unit": "mmol/L",
            "reference_range": "3.0-6.0",
            "flag": "unknown",
            "source_line": "示例指标 5.2 mmol/L 3.0-6.0",
        }
    ]
    assert extraction["confirmed"] is False
    assert "不构成诊断" in extraction["notice"]
    assert service.list_reports(alice.id) == []
