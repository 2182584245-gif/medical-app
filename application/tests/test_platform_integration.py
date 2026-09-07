from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import zipfile
from datetime import UTC, datetime
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from ollama_chat_app.config import APP_NAME
from ollama_chat_app.data.database import SCHEMA_VERSION, Database
from ollama_chat_app.security.passwords import hash_password
from ollama_chat_app.security.secret_store import SecretStore
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.backup import (
    DATA_DATABASE_PATH,
    DATA_MANIFEST_PATH,
    DATA_README_PATH,
    PortableBackupService,
)
from ollama_chat_app.services.chat import ChatService
from ollama_chat_app.services.health import HealthService
from ollama_chat_app.ui.health_workspace import HealthWorkspace
from ollama_chat_app.ui.main_window import MainWindow


def test_main_window_opens_integrated_member_workspace(tmp_path: Path, qtbot) -> None:
    database = Database(tmp_path / "app.db")
    auth = AuthService(database)
    user = auth.register("alice", "correct-horse-battery-staple")
    window = MainWindow(
        auth,
        ChatService(database),
        SecretStore(),
        PortableBackupService(database),
        HealthService(database),
    )
    qtbot.addWidget(window)

    window._login_succeeded(user)

    assert window.windowTitle() == APP_NAME
    assert isinstance(window.health_workspace, HealthWorkspace)
    assert window.pages.currentWidget() is window.health_workspace
    assert window.health_workspace.current_user == user
    assert window.health_workspace.stack.count() == 5
    assert window.health_workspace.chat_page is window.chat_page


def test_v1_data_backup_is_verified_migrated_and_imported(tmp_path: Path) -> None:
    legacy_path = tmp_path / "legacy.db"
    created_at = "2026-01-02T03:04:05.000000Z"
    password = "legacy-password-123!"
    with sqlite3.connect(legacy_path) as connection:
        Database._create_schema_v1(connection)
        connection.execute(
            """
            INSERT INTO users (
                id, username, username_normalized, password_hash,
                created_at, last_login_at
            ) VALUES (7, '旧用户', '旧用户', ?, ?, NULL)
            """,
            (hash_password(password), created_at),
        )
        connection.execute(
            """
            INSERT INTO conversations (
                id, user_id, title, provider, model, created_at, updated_at
            ) VALUES (11, 7, '旧对话', 'ollama_local', 'qwen3:4b', ?, ?)
            """,
            (created_at, created_at),
        )
        connection.execute(
            """
            INSERT INTO messages (
                id, conversation_id, sequence_no, role, content, status,
                error_message, provider, model, created_at, updated_at
            ) VALUES (
                13, 11, 1, 'user', '旧消息要保留', 'complete',
                NULL, 'ollama_local', 'qwen3:4b', ?, ?
            )
            """,
            (created_at, created_at),
        )
        connection.execute("PRAGMA user_version = 1")
        connection.commit()

    payload = legacy_path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    manifest = {
        "format_version": 2,
        "application_name": "Ollama 双模聊天",
        "application_version": "0.2.1",
        "schema_version": 1,
        "created_at_utc": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "bundle_id": "ca412c7f-68ac-4d6e-8e5c-7fbb9bd71560",
        "backup_kind": "manual_export",
        "included_files": sorted(
            [
                DATA_DATABASE_PATH.as_posix(),
                DATA_MANIFEST_PATH.as_posix(),
                DATA_README_PATH.as_posix(),
            ]
        ),
        "database": {
            "path": DATA_DATABASE_PATH.as_posix(),
            "sha256": digest,
            "size_bytes": len(payload),
        },
        "security": {
            "encrypted": False,
            "api_key_included": False,
            "application_files_included": False,
        },
    }
    archive_path = tmp_path / "legacy-v1.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(DATA_DATABASE_PATH.as_posix(), payload)
        archive.writestr(
            DATA_MANIFEST_PATH.as_posix(),
            json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
        )
        archive.writestr(
            DATA_README_PATH.as_posix(),
            "此 ZIP 只包含本地数据，备份未加密。".encode(),
        )

    current = Database(tmp_path / "current" / "app.db")
    current.initialize()
    result = PortableBackupService(current).import_archive(archive_path)

    assert result.user_count == 1
    assert result.message_count == 1
    assert AuthService(current).authenticate("旧用户", password).id == 7
    with current.connect() as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION
        assert connection.execute("SELECT content FROM messages WHERE id = 13").fetchone()[0] == (
            "旧消息要保留"
        )
