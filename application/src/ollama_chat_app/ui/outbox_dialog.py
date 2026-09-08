"""Account-bound review of queued edits, with explicit conflict decisions."""

from __future__ import annotations

import json
from contextlib import suppress

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
)

from ..time_utils import format_beijing
from ..workers.task import FunctionTask
from .offline_feedback import show_queued_result

METHOD_LABELS = {
    "add_life_record": "新增生活记录", "add_reminder": "新增提醒",
    "save_profile": "修改个人档案", "update_reminder": "修改提醒",
    "pause_reminder": "暂停提醒", "resume_reminder": "恢复提醒",
    "request_appointment": "申请上门预约", "update_preferences": "修改个人设置",
}
STATUS_LABELS = {
    "queued": "待同步", "uncertain": "结果待确认", "conflict": "与云端冲突",
    "rejected": "云端未接受",
}
ERROR_LABELS = {
    "conflict": "云端内容已有变化，请核对后决定保留哪一版。",
    "validation": "云端校验未通过，重新提交仍可能失败；可放弃本项后重新填写。",
    "permission": "当前权限不允许提交，请联系运营人员。",
}


class OutboxDialog(QDialog):
    def __init__(self, sync_service, parent=None):
        super().__init__(parent)
        self.sync_service = sync_service
        self._identity = sync_service.identity
        self._revoked = False
        self._task = None
        self._connections = []
        self.setWindowTitle("待同步事项与冲突")
        self.resize(840, 650)
        root = QVBoxLayout(self)
        notice = QLabel(
            "这里的内容仅暂存在本机，尚未确认写入云端。结果待确认的事项必须沿用原请求继续重放，"
            "不能丢弃或重复新建；冲突项由您决定，程序不会自动覆盖云端。"
        )
        notice.setWordWrap(True)
        root.addWidget(notice)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["暂存时间", "事项", "状态"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._update_actions)
        root.addWidget(self.table, 1)
        buttons = QGridLayout()
        self.view_button = QPushButton("查看本项内容")
        self.continue_button = QPushButton("继续重放原请求")
        self.rebase_button = QPushButton("按当前版本重新提交")
        self.keep_cloud_button = QPushButton("保留云端版本")
        self.discard_button = QPushButton("放弃本机待提交项")
        self.refresh_button = QPushButton("刷新列表")
        for index, button in enumerate((
            self.view_button, self.continue_button, self.rebase_button,
            self.keep_cloud_button, self.discard_button, self.refresh_button,
        )):
            buttons.addWidget(button, index // 2, index % 2)
        self.view_button.clicked.connect(self._view_selected)
        self.continue_button.clicked.connect(self._continue)
        self.rebase_button.clicked.connect(self._rebase)
        self.keep_cloud_button.clicked.connect(lambda: self._discard(keep_cloud=True))
        self.discard_button.clicked.connect(lambda: self._discard(keep_cloud=False))
        self.refresh_button.clicked.connect(self.refresh)
        root.addLayout(buttons)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("选中一项后点击查看；不会自动展示全部个人资料。")
        self.details.setMaximumHeight(155)
        self.details.hide()
        root.addWidget(self.details)
        self.status = QLabel()
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        close.rejected.connect(self.reject)
        root.addWidget(close)
        for name, callback in (
            ("outbox_changed", self.refresh), ("status_changed", self.refresh),
            ("authorization_lost", self._revoke), ("reauthentication_required", self._revoke),
        ):
            signal = getattr(sync_service, name, None)
            if signal is not None:
                signal.connect(callback)
                self._connections.append((signal, callback))
        self.finished.connect(self._disconnect)
        self.refresh()

    def _disconnect(self, *_args):
        for signal, callback in self._connections:
            with suppress(RuntimeError, TypeError):
                signal.disconnect(callback)
        self._connections.clear()

    def _same_account(self):
        return (
            not self._revoked and self._identity is not None
            and self.sync_service.identity == self._identity
            and self.sync_service.state().get("authorized", False)
        )

    def _revoke(self, *_args):
        self._revoked = True
        self.table.setRowCount(0)
        self.details.clear()
        self.details.hide()
        self.status.setText("账户授权已变化，已隐藏本机事项；请重新在线登录后再打开。")
        self._update_actions()

    def _selected(self):
        item = self.table.item(self.table.currentRow(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def refresh(self, *_args):
        if not self._same_account():
            self._revoke()
            return
        previous = self._selected()
        selected_id = previous.get("operation_id") if previous else None
        try:
            rows = self.sync_service.pending_operations()
        except Exception:
            self.table.setRowCount(0)
            self.details.clear()
            self.details.hide()
            self.status.setText("暂时无法安全读取待同步事项，请确认当前账户授权后重试。")
            self._update_actions()
            return
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(len(rows))
            self.table.clearSelection()
            selected_row = None
            for index, row in enumerate(rows):
                values = (
                    format_beijing(row["created_at"]),
                    METHOD_LABELS.get(row["method"], "待同步修改"),
                    STATUS_LABELS.get(row["status"], "待检查"),
                )
                for column, text in enumerate(values):
                    item = QTableWidgetItem(text)
                    if column == 0:
                        item.setData(Qt.ItemDataRole.UserRole, dict(row))
                    self.table.setItem(index, column, item)
                if row["operation_id"] == selected_id:
                    selected_row = index
            self.table.setCurrentCell(-1, -1)
            if selected_row is not None:
                self.table.selectRow(selected_row)
            else:
                self.details.clear()
                self.details.hide()
        finally:
            self.table.blockSignals(False)
        self.status.setText(f"共 {len(rows)} 项本机待同步修改；列表不是云端已保存记录。")
        self._update_actions()

    def _update_actions(self):
        row = self._selected()
        allowed = self._same_account() and self._task is None
        state = self.sync_service.state()
        online = allowed and state.get("state") == "online"
        status = row.get("status") if row else None
        self.view_button.setEnabled(allowed and row is not None)
        self.continue_button.setEnabled(online and status in {"queued", "uncertain"})
        self.rebase_button.setEnabled(
            online and state.get("fresh", False) and status in {"conflict", "rejected"}
        )
        self.keep_cloud_button.setEnabled(allowed and status == "conflict")
        self.discard_button.setEnabled(allowed and status in {"queued", "conflict", "rejected"})
        self.refresh_button.setEnabled(allowed)

    def _view_selected(self):
        row = self._selected()
        if not row or not self._same_account():
            self._revoke()
            return
        try:
            value = self.sync_service.get_pending(row["operation_id"])
            if value is None:
                self.refresh()
                return
            content = {"填写内容": value.get("kwargs", {}),
                       "其他参数": value.get("args", [])[1:]}
            reason = ERROR_LABELS.get(value.get("error_code"), "")
            self.details.setPlainText(
                "本机待提交内容（不是云端记录）\n" + reason + "\n"
                + json.dumps(content, ensure_ascii=False, indent=2, default=str)
            )
            self.details.show()
        except Exception:
            self.details.clear()
            self.status.setText("无法安全读取这项内容，请刷新后重试。")

    def _continue(self):
        row = self._selected()
        if row and row["status"] in {"queued", "uncertain"} and self._same_account():
            # CloudSync owns the worker and reuses every original request UUID.
            self.sync_service.sync_now()
            if self._same_account():
                self.status.setText("已请求继续重放，将沿用原请求编号确认云端结果。")

    def _rebase(self):
        row = self._selected()
        if not row or row["status"] not in {"conflict", "rejected"} or not self._same_account():
            return
        self._view_selected()
        answer = QMessageBox.question(
            self, "确认重新提交", "这会按当前云端版本重新提交所选本机修改，并可能改变云端内容。"
            "请先核对上方本机内容；确认继续吗？",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._run(self.sync_service.rebase_pending, row["operation_id"])

    def _discard(self, *, keep_cloud):
        row = self._selected()
        if not row or row["status"] not in {"queued", "conflict", "rejected"}:
            return
        if not self._same_account():
            self._revoke()
            return
        title = "保留云端版本" if keep_cloud else "放弃本机待提交项"
        answer = QMessageBox.question(
            self, title, "只删除所选本机待提交修改，云端数据保持不变。"
            "本机这项修改删除后无法恢复，确定继续吗？",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._run(self.sync_service.discard_pending, row["operation_id"])

    def _run(self, method, identifier):
        if self._task is not None or not self._same_account():
            return
        self._task = FunctionTask(method, identifier)
        self._task.signals.result.connect(self._completed)
        self._task.signals.error.connect(self._failed)
        self._task.signals.finished.connect(self._finished)
        self._update_actions()
        QThreadPool.globalInstance().start(self._task)

    def _completed(self, _result):
        if self._same_account():
            self.refresh()
            if not show_queued_result(_result, self, label=self.status):
                self.status.setText("已放弃所选本机待提交修改；云端数据没有改变。")

    def _failed(self, _error):
        if self._same_account():
            self.status.setText("本次操作未完成，请刷新确认状态；请勿重新新建同一事项。")

    def _finished(self):
        self._task = None
        self._update_actions()

    def reject(self):
        if self._task is not None:
            self.status.setText("正在处理所选事项，请等待完成后关闭。")
            return
        super().reject()
