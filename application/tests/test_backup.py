from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

import ollama_chat_app.services.backup as backup_module
from ollama_chat_app.data.database import SCHEMA_VERSION, Database
from ollama_chat_app.security.passwords import hash_password
from ollama_chat_app.services.auth import AuthenticationError, AuthService
from ollama_chat_app.services.backup import (
    DATA_DATABASE_PATH,
    DATA_MANIFEST_PATH,
    DATA_README_PATH,
    BackupError,
    BackupValidationError,
    PortableBackupService,
    migrate_legacy_database,
    snapshot_database,
)
from ollama_chat_app.services.chat import ChatService


def _create_populated_database(
    path: Path,
    *,
    username: str = "alice",
    password: str = "Plaintext-password-must-not-leak-987!",
    question: str = "portable question",
    answer: str = "portable answer",
) -> tuple[Database, str]:
    database = Database(path)
    database.initialize()
    auth = AuthService(database)
    chat = ChatService(database)
    user = auth.register(username, password)
    exchange = chat.begin_message(
        user.id,
        question,
        provider="local",
        model="qwen3:4b",
    )
    chat.complete_message(user.id, exchange.assistant_message.id, answer)
    return database, password


def _archive_payloads(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _write_payload_archive(path: Path, payloads: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in payloads.items():
            archive.writestr(name, payload)


def _database_from_archive(archive_path: Path, output: Path) -> Path:
    with zipfile.ZipFile(archive_path) as archive:
        output.write_bytes(archive.read(DATA_DATABASE_PATH.as_posix()))
    return output


def test_v2_database_snapshot_is_validated_and_migrated_to_current_schema(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy-v2.db"
    with sqlite3.connect(source) as connection:
        Database._create_schema_v2(connection)
        connection.execute("PRAGMA user_version = 2")
        connection.commit()

    destination = tmp_path / "migrated.db"
    snapshot_database(source, destination)

    with sqlite3.connect(destination) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        commerce_tables = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_schema
                WHERE type = 'table' AND name IN (
                    'products', 'product_recommendations', 'orders'
                )
                """
            )
        }
    assert commerce_tables == {"products", "product_recommendations", "orders"}


def test_v2_backup_archive_is_verified_imported_and_migrated_to_v3(tmp_path: Path) -> None:
    legacy_path = tmp_path / "legacy-v2.db"
    created_at = "2026-01-02T03:04:05.000000Z"
    password = "legacy-v2-password-123!"
    with sqlite3.connect(legacy_path) as connection:
        Database._create_schema_v2(connection)
        connection.execute(
            """
            INSERT INTO users (
                id, username, username_normalized, password_hash, created_at,
                last_login_at, role_code, account_status, updated_at
            ) VALUES (7, '旧版会员', '旧版会员', ?, ?, NULL, 'member', 'active', ?)
            """,
            (hash_password(password), created_at, created_at),
        )
        connection.commit()

    database_payload = legacy_path.read_bytes()
    archive_path = tmp_path / "legacy-v2.zip"
    manifest_payload = backup_module._build_manifest(
        created_at=datetime.now(UTC),
        bundle_id=str(uuid.uuid4()),
        backup_kind="manual_export",
        schema_version=2,
        database_sha256=hashlib.sha256(database_payload).hexdigest(),
        database_size=len(database_payload),
    )
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(DATA_DATABASE_PATH.as_posix(), database_payload)
        archive.writestr(DATA_MANIFEST_PATH.as_posix(), manifest_payload)
        archive.writestr(DATA_README_PATH.as_posix(), backup_module._readme_text())

    destination = Database(tmp_path / "current" / "app.db")
    destination.initialize()
    result = PortableBackupService(destination).import_archive(archive_path)

    assert result.user_count == 1
    assert AuthService(destination).authenticate("旧版会员", password).id == 7
    with destination.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
        }
    assert {"products", "product_recommendations", "orders"} <= tables


def test_export_contains_only_data_manifest_and_readme_with_wal_snapshot(
    tmp_path: Path,
) -> None:
    application_dir = tmp_path / "我的 Ollama 应用"
    (application_dir / "_internal").mkdir(parents=True)
    executable_marker = b"mock-executable-must-never-be-exported"
    runtime_marker = b"mock-runtime-must-never-be-exported"
    (application_dir / "Ollama 双模聊天.exe").write_bytes(executable_marker)
    (application_dir / "_internal" / "runtime.dll").write_bytes(runtime_marker)
    database, plaintext_password = _create_populated_database(application_dir / "data" / "app.db")
    api_key = b"ollama-api-key-must-never-be-exported"
    (application_dir / "api-key.txt").write_bytes(api_key)

    writer = sqlite3.connect(database.path)
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    writer.execute(
        "UPDATE users SET last_login_at = '2035-01-02T03:04:05.000000Z' WHERE username = 'alice'"
    )
    writer.commit()
    assert Path(f"{database.path}-wal").exists()

    target = tmp_path / "exports" / "data-backup.zip"
    target.parent.mkdir()
    target.write_bytes(b"old archive")
    service = PortableBackupService(database, application_dir)
    try:
        result = service.create_archive(target)
    finally:
        writer.close()

    expected_names = {
        DATA_DATABASE_PATH.as_posix(),
        DATA_MANIFEST_PATH.as_posix(),
        DATA_README_PATH.as_posix(),
    }
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        assert set(names) == expected_names
        assert len(names) == len(set(names)) == 3
        database_bytes = archive.read(DATA_DATABASE_PATH.as_posix())
        manifest = json.loads(archive.read(DATA_MANIFEST_PATH.as_posix()))
        readme = archive.read(DATA_README_PATH.as_posix()).decode("utf-8")
        combined_payload = b"".join(archive.read(name) for name in names)

    assert executable_marker not in combined_payload
    assert runtime_marker not in combined_payload
    assert api_key not in combined_payload
    assert plaintext_password.encode() not in database_bytes
    assert "只包含本地数据" in readme
    assert "未加密" in readme
    assert "不包含 EXE" in readme
    assert manifest["format_version"] == 2
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["backup_kind"] == "manual_export"
    assert manifest["included_files"] == sorted(expected_names)
    assert manifest["bundle_id"] == result.bundle_id
    assert manifest["database"]["sha256"] == hashlib.sha256(database_bytes).hexdigest()
    assert manifest["database"]["size_bytes"] == len(database_bytes)
    assert manifest["security"] == {
        "encrypted": False,
        "api_key_included": False,
        "application_files_included": False,
    }

    archived_database = _database_from_archive(target, tmp_path / "archived.db")
    with sqlite3.connect(archived_database) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
        assert connection.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
        assert connection.execute(
            "SELECT last_login_at FROM users WHERE username = 'alice'"
        ).fetchone() == ("2035-01-02T03:04:05.000000Z",)
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone() == (2,)

    assert result.path == target.resolve()
    assert result.file_count == 3
    assert result.size_bytes == target.stat().st_size
    assert result.database_member == DATA_DATABASE_PATH.as_posix()


def test_import_replaces_data_only_after_creating_verified_safety_backup(
    tmp_path: Path,
) -> None:
    source_database, alice_password = _create_populated_database(
        tmp_path / "source" / "app.db",
        username="alice",
        question="alice question",
        answer="alice answer",
    )
    archive = tmp_path / "alice-data.zip"
    PortableBackupService(source_database).create_archive(archive)

    current_database, bob_password = _create_populated_database(
        tmp_path / "current" / "app.db",
        username="bob",
        password="Bob-password-must-not-leak-456!",
        question="bob question",
        answer="bob answer",
    )
    service = PortableBackupService(current_database)
    result = service.import_archive(archive)

    assert result.path == archive.resolve()
    assert result.user_count == 1
    assert result.message_count == 2
    assert result.safety_backup_path.is_file()
    assert result.safety_backup_path.parent == current_database.path.parent / "backups"
    assert AuthService(current_database).authenticate("alice", alice_password).username == "alice"
    with pytest.raises(AuthenticationError):
        AuthService(current_database).authenticate("bob", bob_password)

    with zipfile.ZipFile(result.safety_backup_path) as safety_archive:
        assert set(safety_archive.namelist()) == {
            DATA_DATABASE_PATH.as_posix(),
            DATA_MANIFEST_PATH.as_posix(),
            DATA_README_PATH.as_posix(),
        }
        safety_manifest = json.loads(safety_archive.read(DATA_MANIFEST_PATH.as_posix()))
        assert safety_manifest["backup_kind"] == "pre_import_safety"

    old_database = _database_from_archive(result.safety_backup_path, tmp_path / "old.db")
    assert AuthService(Database(old_database)).authenticate("bob", bob_password).username == "bob"


def test_import_rejects_traversal_duplicate_symlink_and_tampering_before_backup(
    tmp_path: Path,
) -> None:
    exported_database, _ = _create_populated_database(tmp_path / "exported" / "app.db")
    valid_archive = tmp_path / "valid.zip"
    PortableBackupService(exported_database).create_archive(valid_archive)
    payloads = _archive_payloads(valid_archive)

    current_database, current_password = _create_populated_database(
        tmp_path / "current" / "app.db",
        username="current-user",
        password="Current-password-must-not-leak-123!",
    )
    before_hash = hashlib.sha256(current_database.path.read_bytes()).hexdigest()
    service = PortableBackupService(current_database)

    traversal = tmp_path / "traversal.zip"
    traversal_payloads = dict(payloads)
    readme = traversal_payloads.pop(DATA_README_PATH.as_posix())
    traversal_payloads["../README.txt"] = readme
    _write_payload_archive(traversal, traversal_payloads)

    duplicate = tmp_path / "duplicate.zip"
    with (
        pytest.warns(UserWarning, match="Duplicate name"),
        zipfile.ZipFile(duplicate, "w", compression=zipfile.ZIP_DEFLATED) as archive,
    ):
        archive.writestr(DATA_DATABASE_PATH.as_posix(), payloads[DATA_DATABASE_PATH.as_posix()])
        archive.writestr(DATA_DATABASE_PATH.as_posix(), payloads[DATA_DATABASE_PATH.as_posix()])
        archive.writestr(DATA_MANIFEST_PATH.as_posix(), payloads[DATA_MANIFEST_PATH.as_posix()])

    symlink = tmp_path / "symlink.zip"
    with zipfile.ZipFile(symlink, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        link_info = zipfile.ZipInfo(DATA_DATABASE_PATH.as_posix())
        link_info.create_system = 3
        link_info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link_info, b"../../outside.db")
        archive.writestr(DATA_MANIFEST_PATH.as_posix(), payloads[DATA_MANIFEST_PATH.as_posix()])
        archive.writestr(DATA_README_PATH.as_posix(), payloads[DATA_README_PATH.as_posix()])

    tampered = tmp_path / "tampered.zip"
    tampered_payloads = dict(payloads)
    tampered_payloads[DATA_DATABASE_PATH.as_posix()] += b"modified"
    _write_payload_archive(tampered, tampered_payloads)

    for invalid_archive in (traversal, duplicate, symlink, tampered):
        with pytest.raises(BackupValidationError):
            service.import_archive(invalid_archive)
        assert hashlib.sha256(current_database.path.read_bytes()).hexdigest() == before_hash

    assert not (current_database.path.parent / "backups").exists()
    assert not (tmp_path / "outside.db").exists()
    assert (
        AuthService(current_database).authenticate("current-user", current_password).username
        == "current-user"
    )


def test_import_rejects_database_with_extra_schema_even_with_matching_hash(
    tmp_path: Path,
) -> None:
    exported_database, _ = _create_populated_database(tmp_path / "exported" / "app.db")
    valid_archive = tmp_path / "valid.zip"
    PortableBackupService(exported_database).create_archive(valid_archive)
    payloads = _archive_payloads(valid_archive)

    modified_database = tmp_path / "modified.db"
    modified_database.write_bytes(payloads[DATA_DATABASE_PATH.as_posix()])
    with sqlite3.connect(modified_database) as connection:
        connection.execute("CREATE TABLE unexpected_payload (value BLOB)")
        connection.commit()
    modified_bytes = modified_database.read_bytes()
    manifest = json.loads(payloads[DATA_MANIFEST_PATH.as_posix()])
    manifest["database"]["sha256"] = hashlib.sha256(modified_bytes).hexdigest()
    manifest["database"]["size_bytes"] = len(modified_bytes)
    payloads[DATA_DATABASE_PATH.as_posix()] = modified_bytes
    payloads[DATA_MANIFEST_PATH.as_posix()] = (
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    ).encode()
    malicious_archive = tmp_path / "matching-hash-but-wrong-schema.zip"
    _write_payload_archive(malicious_archive, payloads)

    current_database, current_password = _create_populated_database(
        tmp_path / "current" / "app.db",
        username="current-user",
        password="Current-password-must-not-leak-123!",
    )
    before = current_database.path.read_bytes()
    with pytest.raises(BackupValidationError, match="数据库结构"):
        PortableBackupService(current_database).import_archive(malicious_archive)
    assert current_database.path.read_bytes() == before
    assert (
        AuthService(current_database).authenticate("current-user", current_password).username
        == "current-user"
    )
    assert not (current_database.path.parent / "backups").exists()


def test_import_rejects_reserved_prefix_schema_entry_even_with_matching_hash(
    tmp_path: Path,
) -> None:
    exported_database, _ = _create_populated_database(tmp_path / "exported" / "app.db")
    valid_archive = tmp_path / "valid.zip"
    PortableBackupService(exported_database).create_archive(valid_archive)
    payloads = _archive_payloads(valid_archive)

    modified_database = tmp_path / "modified.db"
    modified_database.write_bytes(payloads[DATA_DATABASE_PATH.as_posix()])
    with sqlite3.connect(modified_database) as connection:
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            """
            INSERT INTO sqlite_schema(type, name, tbl_name, rootpage, sql)
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                "trigger",
                "sqlite_hidden_trigger",
                "users",
                0,
                "CREATE TRIGGER sqlite_hidden_trigger AFTER INSERT ON users "
                "BEGIN DELETE FROM users; END",
            ),
        )
        connection.commit()
    modified_bytes = modified_database.read_bytes()
    manifest = json.loads(payloads[DATA_MANIFEST_PATH.as_posix()])
    manifest["database"]["sha256"] = hashlib.sha256(modified_bytes).hexdigest()
    manifest["database"]["size_bytes"] = len(modified_bytes)
    payloads[DATA_DATABASE_PATH.as_posix()] = modified_bytes
    payloads[DATA_MANIFEST_PATH.as_posix()] = (
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    ).encode()
    malicious_archive = tmp_path / "reserved-prefix-schema.zip"
    _write_payload_archive(malicious_archive, payloads)

    current_database, current_password = _create_populated_database(
        tmp_path / "current" / "app.db",
        username="current-user",
        password="Current-password-must-not-leak-123!",
    )
    before = current_database.path.read_bytes()
    with pytest.raises(BackupValidationError, match="数据库结构"):
        PortableBackupService(current_database).import_archive(malicious_archive)
    assert current_database.path.read_bytes() == before
    assert (
        AuthService(current_database).authenticate("current-user", current_password).username
        == "current-user"
    )
    assert not (current_database.path.parent / "backups").exists()


def test_failed_replacement_preserves_current_data_and_keeps_safety_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_database, _ = _create_populated_database(tmp_path / "source" / "app.db")
    archive = tmp_path / "valid.zip"
    PortableBackupService(source_database).create_archive(archive)
    current_database, password = _create_populated_database(
        tmp_path / "current" / "app.db",
        username="before-import",
        password="Before-import-password-123!",
    )
    before = current_database.path.read_bytes()

    def fail_before_replace(_path: Path) -> None:
        raise BackupError("injected lock failure")

    monkeypatch.setattr(backup_module, "_prepare_database_replacement", fail_before_replace)
    with pytest.raises(BackupError, match="自动备份已保留"):
        PortableBackupService(current_database).import_archive(archive)

    assert current_database.path.read_bytes() == before
    assert AuthService(current_database).authenticate("before-import", password).username == (
        "before-import"
    )
    safety_backups = list((current_database.path.parent / "backups").glob("*.zip"))
    assert len(safety_backups) == 1
    restored = _database_from_archive(safety_backups[0], tmp_path / "restored.db")
    assert AuthService(Database(restored)).authenticate("before-import", password).username == (
        "before-import"
    )


def test_snapshot_and_one_time_legacy_migration(tmp_path: Path) -> None:
    legacy_database, password = _create_populated_database(tmp_path / "legacy" / "app.db")
    legacy_bytes = legacy_database.path.read_bytes()
    portable_database = tmp_path / "portable" / "data" / "app.db"

    assert migrate_legacy_database(legacy_database.path, portable_database) is True
    assert legacy_database.path.read_bytes() == legacy_bytes
    assert AuthService(Database(portable_database)).authenticate("alice", password).username == (
        "alice"
    )

    portable_bytes = portable_database.read_bytes()
    assert migrate_legacy_database(legacy_database.path, portable_database) is False
    assert portable_database.read_bytes() == portable_bytes
    assert migrate_legacy_database(tmp_path / "missing.db", tmp_path / "unused.db") is False

    empty_seed = Database(tmp_path / "seeded-portable" / "data" / "app.db")
    empty_seed.initialize()
    assert migrate_legacy_database(legacy_database.path, empty_seed.path) is True
    assert AuthService(Database(empty_seed.path)).authenticate("alice", password).username == (
        "alice"
    )

    replacement = tmp_path / "replacement.db"
    replacement.write_bytes(b"old destination")
    assert snapshot_database(legacy_database.path, replacement) is None
    with sqlite3.connect(replacement) as connection:
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone() == (1,)


def test_snapshot_failure_preserves_source_and_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "broken.db"
    destination = tmp_path / "existing.db"
    source.write_bytes(b"not a SQLite database")
    destination.write_bytes(b"existing destination")
    before = set(tmp_path.iterdir())

    with pytest.raises(BackupError):
        snapshot_database(source, destination)

    assert source.read_bytes() == b"not a SQLite database"
    assert destination.read_bytes() == b"existing destination"
    assert set(tmp_path.iterdir()) == before

    missing_source = tmp_path / "missing.db"
    with pytest.raises(BackupValidationError):
        snapshot_database(missing_source, tmp_path / "must-not-exist.db")
    assert not (tmp_path / "must-not-exist.db").exists()


def test_export_failure_preserves_old_target_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _ = _create_populated_database(tmp_path / "data" / "app.db")
    target_dir = tmp_path / "backups"
    target_dir.mkdir()
    target = target_dir / "data.zip"
    target.write_bytes(b"known good old archive")
    before = set(target_dir.iterdir())

    def fail_after_partial_archive(archive_path: Path, **_kwargs: object) -> None:
        archive_path.write_bytes(b"incomplete archive")
        raise OSError("injected archive failure")

    monkeypatch.setattr(backup_module, "_write_data_archive", fail_after_partial_archive)
    with pytest.raises(BackupError, match="创建数据备份失败"):
        PortableBackupService(database).create_archive(target)

    assert target.read_bytes() == b"known good old archive"
    assert set(target_dir.iterdir()) == before
