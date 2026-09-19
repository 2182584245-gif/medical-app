from __future__ import annotations

import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QLineEdit

from ollama_chat_app.services.developer_settings import default_snapshot
from ollama_chat_app.ui.developer_workspace import (
    DeveloperLoginDialog,
    DeveloperWorkspaceDialog,
    EntryListEditor,
    RecordEditDialog,
    UserDataDialog,
)


class FakeDeveloperService:
    closed = False

    def close_session(self):
        self.closed = True


def test_developer_dialog_has_four_sections_and_three_role_tables(qtbot):
    service = FakeDeveloperService()
    dialog = DeveloperWorkspaceDialog(service)
    qtbot.addWidget(dialog)
    value = default_snapshot()
    value["api_key_configured"] = True
    dialog._loaded(
        (
            value,
            {"backend": "本地模式"},
            {
                "operator": {
                    "users": [{"id": 1, "username": "管理者", "account_status": "active"}]
                },
                "advisor": {"users": []},
                "member": {"users": []},
            },
        )
    )
    assert dialog.tabs.count() == 4
    assert dialog.role_tabs.count() == 3
    assert dialog.user_tables["operator"].item(0, 1).text() == "管理者"
    assert dialog.key.line_edit.text() == ""
    assert "已安全配置" in dialog.key_state.text()
    assert dialog.model.currentData() == "deepseek-v4-flash"
    assert dialog.tokens.value() == 2048
    dialog.reject()
    assert service.closed


def test_developer_login_not_prefilled_or_plain_password(qtbot):
    dialog = DeveloperLoginDialog(FakeDeveloperService())
    qtbot.addWidget(dialog)
    assert dialog.username_input.text() == ""
    assert dialog.password_field.line_edit.text() == ""
    assert dialog.password_field.line_edit.echoMode() == QLineEdit.EchoMode.Password


def test_record_editor_protects_foreign_keys_and_captures_only_changes(qtbot):
    row = {"id": 10, "user_id": 2, "content": "今天散步", "created_at": "2026-09-19"}
    dialog = RecordEditDialog(row, "life_records")
    qtbot.addWidget(dialog)
    assert set(dialog.inputs) == {"content"}
    assert dialog.changes() == {}
    dialog.inputs["content"].setPlainText("今天散步半小时")
    assert dialog.changes() == {"content": "今天散步半小时"}


def test_trade_and_attachment_tables_read_only(qtbot):
    for table in ("orders", "chat_attachments", "user_files"):
        dialog = RecordEditDialog({"id": 1, "content": "元信息"}, table)
        qtbot.addWidget(dialog)
        assert not dialog.inputs


def test_knowledge_review_status_visible_and_list_copy_isolated(qtbot):
    editor = EntryListEditor("knowledge")
    qtbot.addWidget(editor)
    rows = [
        {
            "title": "睡眠资料",
            "content": "待核验文字",
            "source": "资料出处",
            "keywords": ["睡眠"],
            "enabled": True,
            "reviewed": False,
        }
    ]
    editor.set_entries(rows)
    assert "未审核，不参与回答" in editor.list.item(0).text()
    rows[0]["reviewed"] = True
    assert editor.entries[0]["reviewed"] is False


def test_user_details_display_pending_changes_separately(qtbot):
    details = {
        "user": {"id": 7, "username": "test_member", "account_status": "active"},
        "tables": {"messages": [{"id": 1, "content": "原消息"}]},
        "pending_changes": [{"state": "pending"}],
    }
    dialog = UserDataDialog(FakeDeveloperService(), details)
    qtbot.addWidget(dialog)
    assert "1 条" in dialog.pending_label.text()
    assert "原消息" in dialog.records.item(0, 1).text()
    assert dialog.password.line_edit.echoMode() == QLineEdit.EchoMode.Password


def test_cloud_bridge_parent_deletion_does_not_poison_next_request(qtbot, qapp):
    import time

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QWidget

    from ollama_chat_app.workers.cloud_bridge import run_cloud_operation

    parent = QWidget()
    parent.show()
    parent.activateWindow()
    qapp.processEvents()
    QTimer.singleShot(10, parent.deleteLater)

    def operation():
        time.sleep(0.08)
        return "complete"

    assert run_cloud_operation(operation) == "complete"
    assert run_cloud_operation(lambda: "next") == "next"
