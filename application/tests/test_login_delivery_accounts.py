"""Login each documented fictional account on an isolated SQLite backup, never its source.

LOGIN_DELIVERY_SOURCE optionally selects an existing public installation. Output
contains usernames, roles and status only; credentials are never parameter IDs,
logs, report fields, screenshots or assertion messages. Real network/secret-store
access is blocked. Without the opt-in path, a fresh fictional seed is generated.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QMessageBox

from ollama_chat_app.data.database import Database
from ollama_chat_app.main import build_desktop_window
from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.ui.theme import build_style
from tests.test_v170_developer_journey import JourneySecrets
from tools.generate_synthetic_demo import EXPANDED_PROFILE, build_demo
from tools.upgrade_synthetic_demo_v7 import contract

ACCOUNTS = (
    ("demo-operator", "operator", "OperatorWorkspace"),
    ("demo-advisor-1", "advisor", "AdvisorWorkspace"),
    ("demo-advisor-2", "advisor", "AdvisorWorkspace"),
    ("demo-member-1", "member", "HealthWorkspace"),
    ("demo-member-2", "member", "HealthWorkspace"),
)


class HiddenPassword(str):
    def __repr__(self):
        return "<documented password withheld>"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_digests(source):
    return {path.name: digest(path) for path in source.glob("data/app.db*") if path.is_file()}


@pytest.fixture(scope="module")
def delivery(tmp_path_factory):
    supplied = os.environ.get("LOGIN_DELIVERY_SOURCE")
    root = Path(supplied) if supplied else tmp_path_factory.mktemp("delivery-seed") / "seed"
    if not supplied:
        original = dict(os.environ)
        try:
            build_demo(root, profile=EXPANDED_PROFILE, screenshots=False)
        finally:
            os.environ.clear()
            os.environ.update(original)
    source = root / "data/app.db"
    assert root.is_absolute() and not source.is_symlink()
    credentials = {
        username: HiddenPassword(value)
        for username, value in contract().sibling("_demo_v170").credentials(root).items()
    }
    before = source_digests(root)
    yield root, credentials
    assert source_digests(root) == before, "Source DB/WAL changed during isolated acceptance"


@pytest.fixture
def isolated_delivery(delivery, tmp_path, monkeypatch, qapp, qtbot):
    root, credentials = delivery
    calls, windows = [], []

    def forbidden(*_args, **_kwargs):
        calls.append("forbidden network attempt")
        raise AssertionError("No network is permitted during delivery-account acceptance")

    monkeypatch.setattr("httpx.Client.send", forbidden)
    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "private"))
    target = tmp_path / "isolated-copy"
    (target / "data").mkdir(parents=True)
    with closing(sqlite3.connect(
        (root / "data/app.db").as_uri() + "?mode=ro", uri=True
    )) as original, closing(sqlite3.connect(target / "data/app.db")) as copied:
        original.execute("PRAGMA query_only=ON")
        original.backup(copied)
        assert copied.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert copied.execute("PRAGMA foreign_key_check").fetchone() is None
    for name in ("assets", "transfer-assets"):
        if (root / name).is_dir():
            shutil.copytree(root / name, target / name)
    monkeypatch.chdir(target)
    monkeypatch.setenv("OLLAMA_DUAL_CHAT_DATA_DIR", str(target / "data"))
    secrets = JourneySecrets()
    secrets.defaults.clear()
    monkeypatch.setattr("ollama_chat_app.main.SecretStore", lambda **_kw: secrets)
    monkeypatch.setattr(
        QMessageBox, "information", lambda *_a, **_kw: QMessageBox.StandardButton.Ok
    )
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *_a, **_kw: warnings.append("UI warning"))
    previous_font, previous_style = qapp.font(), qapp.styleSheet()
    font_directory = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for filename in ("msyh.ttc", "msyhbd.ttc"):
        if (font_directory / filename).is_file():
            QFontDatabase.addApplicationFont(str(font_directory / filename))
    qapp.setFont(QFont("Microsoft YaHei UI", 15))
    qapp.setStyleSheet(build_style(20))
    database = Database(target / "data/app.db")
    auth = AuthService(database)
    window = build_desktop_window("local", local_database=database)
    qtbot.addWidget(window)
    windows.append(window)
    window.resize(1500, 960)
    window.show()
    window.network_monitor.stop()
    yield database, auth, window, credentials
    for window in windows:
        window._logout()
        window.close()
    assert QThreadPool.globalInstance().waitForDone(3000)
    qapp.processEvents()
    qapp.setFont(previous_font)
    qapp.setStyleSheet(previous_style)
    assert not calls
    assert not warnings


@pytest.mark.parametrize("username,role,workspace_name", ACCOUNTS, ids=[a[0] for a in ACCOUNTS])
def test_documented_account_login_and_role_workspace(
    isolated_delivery, username, role, workspace_name, qtbot, qapp
):
    database, auth, window, credentials = isolated_delivery
    result = {
        "username": username, "expected_role": role, "mode": "local",
        "status": "fail", "reason": "not completed", "network_used": False,
    }
    output = os.environ.get("LOGIN_DELIVERY_EVIDENCE")
    directory = Path(output) if output else None
    if directory:
        directory.mkdir(parents=True, exist_ok=True)
    try:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT id,role_code,account_status FROM users WHERE username=?", (username,)
            ).fetchone()
            assert row is not None, "Documented account is absent from the local copied database"
            result.update(actual_role=row["role_code"], account_status=row["account_status"])
            term = connection.execute(
                "SELECT starts_at,ends_at FROM staff_account_terms WHERE user_id=?", (row["id"],)
            ).fetchone()
            if term:
                result["staff_valid_from"], result["staff_valid_until"] = tuple(term)
                now = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
                result["staff_currently_valid"] = term["starts_at"] <= now < term["ends_at"]
            membership = connection.execute(
                "SELECT status,starts_at,ends_at FROM memberships WHERE user_id=? "
                "ORDER BY id DESC LIMIT 1", (row["id"],)
            ).fetchone()
            if membership:
                result["membership"] = dict(membership)
        user = auth.login(username, credentials[username])
        assert user.role_code == role, "Authenticated account has an unexpected role"
        assert user.account_status == "active", "Authenticated account is not active"
        window._login_succeeded(user)
        qtbot.waitUntil(lambda: window._active_workspace is not None, timeout=10000)
        qtbot.wait(200)
        assert window._current_user.id == user.id
        assert window.pages.currentWidget() is window._active_workspace
        actual_workspace = type(window._active_workspace).__name__
        result["workspace"] = actual_workspace
        assert actual_workspace == workspace_name, "Role workspace did not open correctly"
        if directory:
            qapp.processEvents()
            assert window.grab().save(str(directory / (username + ".png")))
        result.update(status="pass", reason="Documented password accepted; correct role UI opened")
    except Exception as error:
        # Exception type is diagnostic without exposing credential values or record text.
        result["reason"] = type(error).__name__ + ": local account/UI verification failed"
        raise
    finally:
        if directory:
            (directory / (username + ".json")).write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
