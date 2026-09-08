"""Explicit cloud mirror status and reviewable first migration. No source-switching controls."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..services.cloud_client import CloudAPIError
from ..time_utils import format_beijing
from ..workers.task import FunctionTask

RESOURCE_LABELS = {
    "profile": "个人档案",
    "preferences": "我的偏好",
    "life_records": "生活记录",
    "reminders": "生活提醒",
    "conversations": "聊天列表",
    "messages": "聊天文字",
    "service_summary": "会员服务摘要",
    "appointments": "预约记录",
    "members": "会员名单",
    "advisors": "顾问名单",
    "work_statistics": "工作统计",
}
FIELD_LABELS = {
    "display_name": "姓名",
    "nickname": "昵称",
    "preferred_name": "称呼",
    "birth_date": "出生日期",
    "gender": "性别",
    "phone": "联系电话",
    "living_situation": "居住情况",
    "city": "天气地区",
    "timezone": "时区",
    "font_size": "字体大小",
    "brightness": "界面亮度",
    "theme_color": "主题颜色",
    "emergency_contact_name": "紧急联系人",
    "emergency_contact_phone": "紧急联系电话",
    "ai_preferred_name": "AI称呼",
    "reminder_frequency": "提醒频率",
    "height_cm": "身高",
    "health_goals": "生活目标",
    "dietary_preferences": "饮食偏好",
    "medical_notes": "个人备注",
}
EXCLUDED_LABELS = {
    "chat_attachments": "聊天图片与附件",
    "user_files": "本地文件",
    "report_files": "报告文件",
    "pending_messages": "未完成消息",
    "system_messages": "系统消息",
    "orders": "本地虚拟订单",
    "service_assignments": "服务安排与权益",
}


def _plain(value):
    if is_dataclass(value):
        return asdict(value)
    return value


def _text(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return "未填写" if value is None else str(value)


class MirrorViewDialog(QDialog):
    def __init__(self, sync_service, parent=None):
        super().__init__(parent)
        self.sync_service = sync_service
        self.setWindowTitle("只读云端镜像")
        self.resize(920, 660)
        root = QVBoxLayout(self)
        notice = QLabel(
            "这是最近一次完整同步的只读副本。离线访问须有本机独立、未到期的许可；"
            "镜像本身不能用于验证登录。"
        )
        notice.setWordWrap(True)
        root.addWidget(notice)
        self.resources = QComboBox()
        for name, label in RESOURCE_LABELS.items():
            self.resources.addItem(label, name)
        root.addWidget(self.resources)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["时间 / 项目", "内容"])
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        root.addWidget(self.table, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        close.rejected.connect(self.reject)
        root.addWidget(close)
        self.resources.currentIndexChanged.connect(self.refresh)
        self.sync_service.authorization_lost.connect(self._revoke)
        self.sync_service.status_changed.connect(self._status_changed)
        self.sync_service.snapshot_changed.connect(self.refresh)
        self.finished.connect(self._disconnect)
        self.refresh()

    def _disconnect(self):
        self.sync_service.authorization_lost.disconnect(self._revoke)
        self.sync_service.status_changed.disconnect(self._status_changed)
        self.sync_service.snapshot_changed.disconnect(self.refresh)

    def _revoke(self):
        self.table.setRowCount(0)
        self.status.setText("云端登录或授权已失效，镜像已锁定，请重新在线登录。")

    def _status_changed(self, state):
        if not state["authorized"] or not state.get("fresh", True):
            self._revoke()

    def refresh(self, *_args):
        self.table.setRowCount(0)
        try:
            resource = self.resources.currentData()
            snapshot = self.sync_service.mirror.snapshot()
            value = snapshot["resources"][resource]
            if resource == "messages":
                value = [message for messages in value.values() for message in messages]
            if isinstance(value, dict):
                rows = [(FIELD_LABELS.get(key, key), _text(val)) for key, val in value.items()]
            else:
                rows = []
                for item in value:
                    record = _plain(item)
                    if isinstance(record, dict):
                        stamp = (
                            record.get("occurred_at")
                            or record.get("scheduled_at")
                            or record.get("created_at")
                        )
                        content = (
                            record.get("content")
                            or record.get("title")
                            or record.get("display_name")
                            or record.get("username")
                        )
                        label = format_beijing(stamp) if stamp else str(record.get("id", ""))
                        rows.append((label, str(content) if content is not None else _text(record)))
                    else:
                        rows.append(("", _text(item)))
            self.table.setRowCount(len(rows))
            for row, (label, content) in enumerate(rows):
                self.table.setItem(row, 0, QTableWidgetItem(label))
                self.table.setItem(row, 1, QTableWidgetItem(content))
            self.table.resizeRowsToContents()
            excluded = snapshot["excluded"]
            exclusions = "，".join(
                f"{EXCLUDED_LABELS[key]} {count} 项" for key, count in excluded.items()
            )
            self.status.setText(f"共 {len(rows)} 项。未镜像：{exclusions}。")
        except Exception as error:
            self.status.setText(str(error))


class DataSyncDialog(QDialog):
    def __init__(
        self,
        sync_service,
        parent=None,
        *,
        planner=None,
        migration_service=None,
        local_actor_id=None,
        source_authenticator=None,
    ):
        super().__init__(parent)
        self.sync_service = sync_service
        self.planner, self.migration_service, self.local_actor_id = (
            planner,
            migration_service,
            local_actor_id,
        )
        self.source_authenticator = source_authenticator
        self.plan = None
        self._task = None
        self._migration_target = None
        self.setWindowTitle("数据与同步")
        self.resize(780, 640)
        root = QVBoxLayout(self)
        destination = QLabel(f"云端目标：{sync_service.client.base_url}")
        destination.setWordWrap(True)
        root.addWidget(destination)
        self.status = QLabel()
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.outbox_button = QPushButton("待同步事项与冲突")
        self.outbox_button.clicked.connect(self._open_outbox)
        self.outbox_button.setVisible(callable(getattr(sync_service, "pending_operations", None)))
        root.addWidget(self.outbox_button)
        self.last_success = QLabel()
        root.addWidget(self.last_success)
        explanation = QLabel(
            "每 10 秒尝试同步一次完整快照。在线修改仍写入云端；断网时可从下方查看只读镜像。"
            "认证失效后镜像会锁定，重新登录不会自动恢复旧权限。"
        )
        explanation.setWordWrap(True)
        root.addWidget(explanation)
        actions = QHBoxLayout()
        refresh = QPushButton("立即同步")
        refresh.clicked.connect(sync_service.sync_now)
        actions.addWidget(refresh)
        self.view_button = QPushButton("查看只读镜像")
        self.view_button.clicked.connect(self.view_mirror)
        actions.addWidget(self.view_button)
        root.addLayout(actions)
        title = QLabel("将本机本人记录迁入空云账户")
        title.setObjectName("SectionTitle")
        root.addWidget(title)
        hint = QLabel(
            "首次迁移只支持当前本地会员本人的档案、生活记录、提醒、偏好和聊天文字。"
            "目标云账户必须尚无生活记录、提醒或聊天消息，已有档案不允许被覆盖。"
            "本地原始数据始终保留。图片、文件、订单和服务安排不随此操作迁入云端。"
        )
        hint.setWordWrap(True)
        root.addWidget(hint)
        self.preview_button = QPushButton("生成迁移预览")
        self.preview_button.setEnabled(
            planner is not None and (local_actor_id is not None or source_authenticator is not None)
        )
        self.preview_button.clicked.connect(self.preview)
        root.addWidget(self.preview_button)
        self.plan_summary = QLabel("先生成预览，核对来源账户、数量和排除项目后再确认迁移。")
        self.plan_summary.setWordWrap(True)
        root.addWidget(self.plan_summary, 1)
        self.confirm = QCheckBox("我已核对来源及目标账户，并理解这是迁入空云账户的首次迁移")
        self.confirm.toggled.connect(self._update_execute)
        root.addWidget(self.confirm)
        self.execute_button = QPushButton("确认首次迁移")
        self.execute_button.setObjectName("PrimaryButton")
        self.execute_button.setEnabled(False)
        self.execute_button.clicked.connect(self.execute)
        root.addWidget(self.execute_button)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        close.rejected.connect(self.reject)
        root.addWidget(close)
        sync_service.status_changed.connect(self._show_state)
        self.finished.connect(lambda: sync_service.status_changed.disconnect(self._show_state))
        if hasattr(sync_service, "outbox_changed"):
            sync_service.outbox_changed.connect(self._show_outbox_counts)
            self.finished.connect(self._disconnect_outbox)
        self._show_state(sync_service.state())

    def _show_state(self, state):
        self.status.setText(state["message"])
        self._show_outbox_counts(state)
        self.outbox_button.setEnabled(state.get("authorized", False))
        stamp = state["last_success"]
        self.last_success.setText(
            "最近完整同步：" + (format_beijing(stamp) if stamp else "尚未完成")
        )
        self.view_button.setEnabled(state["authorized"] and state.get("fresh", True))
        self._update_execute()

    def _show_outbox_counts(self, state):
        self.outbox_button.setText(
            f"待同步事项 {state.get('pending_count', 0)} 项 · "
            f"冲突 {state.get('conflict_count', 0)} 项"
        )

    def _disconnect_outbox(self, *_args):
        self.sync_service.outbox_changed.disconnect(self._show_outbox_counts)

    def _open_outbox(self):
        from .outbox_dialog import OutboxDialog

        if self.sync_service.state().get("authorized", False):
            OutboxDialog(self.sync_service, self).exec()

    def _update_execute(self):
        self.execute_button.setEnabled(
            bool(
                self.plan
                and not self.plan.blocked_reason
                and self.confirm.isChecked()
                and self.migration_service
                and self.sync_service.identity
                and self.sync_service.state()["authorized"]
                and self._task is None
            )
        )

    def view_mirror(self):
        MirrorViewDialog(self.sync_service, self).exec()

    def preview(self):
        if self._task is not None or self.planner is None:
            return
        if self.source_authenticator is not None:
            try:
                actor_id = self.source_authenticator()
            except Exception as error:
                self.plan_summary.setText(f"本机来源验证未完成：{error}")
                return
            if actor_id is None:
                return
            if type(actor_id) is not int or actor_id <= 0:
                self.plan_summary.setText("本机来源账户验证结果无效。")
                return
            self.local_actor_id = actor_id
        self.confirm.setChecked(False)
        self.plan = None
        task = FunctionTask(
            self.planner.prepare_for_target, self.local_actor_id, self.sync_service.identity
        )
        self._run(task, self._preview_ready)

    def _preview_ready(self, plan):
        self.plan = plan
        labels = {**RESOURCE_LABELS, "messages": "聊天文字"}
        counts = "，".join(
            f"{labels.get(key, key)} {count} 项" for key, count in plan.counts.items()
        )
        excluded = "，".join(
            f"{EXCLUDED_LABELS.get(key, key)} {count} 项" for key, count in plan.excluded.items()
        )
        actor = self.sync_service.actor_id
        self.plan_summary.setText(
            f"本机来源：{plan.source_username}（会员 {self.local_actor_id}）\n"
            f"目标云端会员：{actor}\n拟迁移：{counts}\n"
            f"仍留本地：{excluded}\n清单大小：{plan.payload_bytes / 1024:.1f} KiB。"
            + (f"\n{plan.blocked_reason}" if plan.blocked_reason else "")
            + (
                "\n已恢复上次结果未确认的原始清单；之后新增的本机记录没有追加到此次重试。"
                if plan.resumed
                else ""
            )
        )
        self._update_execute()

    def execute(self):
        if not self.execute_button.isEnabled():
            return
        self._migration_target = self.sync_service.identity
        task = FunctionTask(self.migration_service.execute, self.plan, self.sync_service.identity)
        self._run(task, self._migration_done)

    def _invalidate_migration(self):
        if (
            self._migration_target is not None
            and self._migration_target != self.sync_service.identity
        ):
            return
        # Normally the service invalidates on the worker before emitting its
        # result. Keep a fallback for callers that did not inject a coordinator.
        if getattr(self.migration_service, "sync_service", None) is not self.sync_service:
            self.sync_service.invalidate_after_write()

    def _migration_done(self, result):
        self._invalidate_migration()
        self.confirm.setChecked(False)
        self.plan_summary.setText(
            "首次迁移已完成。本机原始数据完整保留；未迁移的图片、文件、订单和服务安排仍在本机。"
        )
        self.plan = None
        self.sync_service.sync_now()

    def _operation_failed(self, error):
        if self._migration_target is not None and (
            not isinstance(error, CloudAPIError) or error.outcome_uncertain
        ):
            self._invalidate_migration()
        self.plan_summary.setText(
            f"本次操作未确认完成：{error}\n"
            "如果发送结果不确定，再次确认会复用原请求编号，不会创建新的重试编号。"
        )

    def _run(self, task, success):
        self._task = task
        self.preview_button.setEnabled(False)
        self.execute_button.setEnabled(False)
        task.signals.result.connect(success)
        task.signals.error.connect(self._operation_failed)
        task.signals.finished.connect(self._finished_task)
        QThreadPool.globalInstance().start(task)

    def _finished_task(self):
        self._task = None
        self._migration_target = None
        self.preview_button.setEnabled(
            self.planner is not None
            and (self.local_actor_id is not None or self.source_authenticator is not None)
        )
        self._update_execute()

    def done(self, result):
        if self._task is not None:
            QMessageBox.information(
                self, "操作进行中", "请等待当前同步预览或迁移请求结束后再关闭。"
            )
            return
        super().done(result)
