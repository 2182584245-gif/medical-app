"""Prove control of a local member account before offering its data for migration."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from ..security.passwords import normalize_username, perform_dummy_verification, verify_password
from ..workers.task import FunctionTask
from .auth_pages import _PasswordField


def verify_local_source(path, username, password):
    """Read-only verification: never initializes/migrates/updates the source file."""
    source = Path(path).resolve()
    if not source.is_file() or not 1 <= len(password) <= 1024:
        raise ValueError("本机用户名或密码不正确")
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as connection:
        row = connection.execute(
            "SELECT id, password_hash, role_code, account_status FROM users "
            "WHERE username_normalized = ?",
            (normalize_username(username),),
        ).fetchone()
    if row is None:
        perform_dummy_verification(password)
        raise ValueError("本机用户名或密码不正确")
    if not verify_password(row[1], password) or row[2:] != ("member", "active"):
        raise ValueError("本机用户名或密码不正确，或该账号不是启用的会员账号")
    return int(row[0])


class LocalMigrationAuthDialog(QDialog):
    def __init__(self, source_path, parent=None):
        super().__init__(parent)
        self.source_path = source_path
        self.actor_id = None
        self._running = False
        self._tasks = []
        self.setWindowTitle("验证本机源账户")
        self.resize(610, 320)
        root = QVBoxLayout(self)
        notice = QLabel(
            "请验证要迁移的本机会员账号。密码只在本机核验，"
            "不发送到云端、不保存；云端目标为当前已登录账户。"
        )
        notice.setWordWrap(True)
        root.addWidget(notice)
        form = QFormLayout()
        self.username = QLineEdit()
        self.password = _PasswordField("输入本机账户密码")
        form.addRow("本机用户名", self.username)
        form.addRow("本机密码", self.password)
        root.addLayout(form)
        self.status = QLabel()
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("验证")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self.verify)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)

    def verify(self):
        if self._running:
            return
        self._running = True
        self.buttons.setEnabled(False)
        self.status.setText("正在本机核验，不会发送密码到云端…")
        task = FunctionTask(
            verify_local_source,
            self.source_path,
            self.username.text(),
            self.password.line_edit.text(),
        )
        self.password.line_edit.clear()
        self._tasks.append(task)
        task.signals.result.connect(self._verified)
        task.signals.error.connect(
            lambda _: self.status.setText(
                "本机账号验证失败，请检查本机用户名、密码与会员账号状态。"
            )
        )
        task.signals.finished.connect(lambda: self._finished(task))
        QThreadPool.globalInstance().start(task)

    def _verified(self, actor_id):
        self.actor_id = actor_id

    def _finished(self, task):
        self._tasks.remove(task)
        self._running = False
        self.buttons.setEnabled(True)
        if self.actor_id is not None:
            self.accept()

    def done(self, result):
        if self._running:
            return
        self.password.line_edit.clear()
        super().done(result)


def authenticate_local_migration(source_path, parent=None):
    dialog = LocalMigrationAuthDialog(source_path, parent)
    return dialog.actor_id if dialog.exec() == QDialog.DialogCode.Accepted else None
