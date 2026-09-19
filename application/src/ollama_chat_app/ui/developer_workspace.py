"""Four-section developer console with explicit authentication and delayed publication."""

from __future__ import annotations

import copy
import json

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..services.developer import PROTECTED_COLUMNS, READ_ONLY_TABLES, TABLE_LABELS
from ..services.developer_settings import FEATURE_LABELS, DeveloperError, safe_snapshot
from ..workers.task import FunctionTask
from .auth_pages import _PasswordField
from .dialog_layout import fit_dialog, scroll_form_dialog

FIELD_LABELS = {
    "id": "记录编号",
    "user_id": "用户编号",
    "username": "账号",
    "role_code": "角色",
    "account_status": "账号状态",
    "created_at": "建立时间",
    "updated_at": "修改时间",
    "last_login_at": "上次登录",
    "display_name": "姓名",
    "birth_date": "生日",
    "gender": "性别",
    "phone": "电话",
    "living_situation": "居住情况",
    "emergency_contact_name": "紧急联系人",
    "emergency_contact_phone": "紧急联系电话",
    "ai_preferred_name": "AI 称呼",
    "height_cm": "身高（厘米）",
    "health_goals": "生活目标",
    "dietary_preferences": "饮食偏好",
    "medical_notes": "医疗备注",
    "category": "记录分类",
    "occurred_at": "发生时间",
    "local_date": "本地日期",
    "content": "内容",
    "details_json": "详细参数（JSON）",
    "title": "标题",
    "scheduled_at": "计划时间",
    "status": "状态",
    "starts_at": "开始时间",
    "ends_at": "结束时间",
    "notes": "备注",
    "summary": "总结",
    "preferences_json": "个人设置（JSON）",
    "role": "消息角色",
    "provider": "AI 提供方",
    "model": "模型",
    "source": "来源",
    "plan_code": "会员计划",
    "value_json": "事实值（JSON）",
    "fact_key": "事实名称",
    "service_type": "服务类型",
    "address": "地点",
    "organization": "机构",
    "specialty": "专长",
    "bio": "介绍",
}


def _plain_label(text):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _table(headers):
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.horizontalHeader().setStretchLastSection(True)
    return widget


class AsyncDialog(QDialog):
    """Keep network/password work off the GUI; do not close while a write is unresolved."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks = []

    def _run(self, function, success, *args):
        if self._tasks:
            return
        task = FunctionTask(function, *args)
        self._tasks.append(task)
        self.setEnabled(False)
        task.signals.result.connect(success)
        task.signals.error.connect(
            lambda error: QMessageBox.warning(
                self,
                "操作未完成",
                self._error_message(error),
            )
        )
        task.signals.finished.connect(lambda: self._finished(task))
        QThreadPool.globalInstance().start(task)

    def _error_message(self, error):
        return (str(error) if isinstance(error, (DeveloperError, RuntimeError))
                else "无法完成操作；原始错误已隐藏，请检查配置和连接。")

    def _finished(self, task):
        if task in self._tasks:
            self._tasks.remove(task)
        self.setEnabled(True)

    def reject(self):
        if not self._tasks:
            super().reject()

    def closeEvent(self, event):
        if self._tasks:
            event.ignore()
        else:
            super().closeEvent(event)


class DeveloperLoginDialog(AsyncDialog):
    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("开发者模式 · 身份验证")
        layout = QFormLayout(self)
        layout.addRow(
            _plain_label(
                "开发者可以查看私密资料和发布全局设置。请使用已开通开发权限的管理者账号重新验证。"
                "普通运营账号不会自动获得权限。"
            )
        )
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("开发者账号")
        self.username_input.setMaxLength(64)
        self.password_field = _PasswordField("请输入开发者密码")
        layout.addRow("账号", self.username_input)
        layout.addRow("密码", self.password_field)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("验证并进入")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self._submit)
        self.buttons.rejected.connect(self.reject)
        self.password_field.line_edit.returnPressed.connect(self._submit)
        layout.addRow(self.buttons)
        fit_dialog(self, width=650, height=360)

    def _error_message(self, error):
        from ..services.cloud_client import CloudAPIError

        if (getattr(self.service, "uses_network", False)
                and isinstance(error, CloudAPIError) and error.code == "not_found"):
            return ("云端开发者接口尚未升级（不是密码错误）。"
                    "请联系部署管理员完成阿里云后端升级及开发权限配置后重试；"
                    "本机开发者账号不会自动获得云端权限。")
        return super()._error_message(error)

    def _submit(self):
        username = self.username_input.text().strip()
        password = self.password_field.line_edit.text()
        self.password_field.line_edit.clear()
        if not username or not password:
            QMessageBox.information(self, "请填写", "账号和密码不能为空。")
            return
        self._run(self.service.authenticate, lambda _user: self.accept(), username, password)


class RecordEditDialog(QDialog):
    def __init__(self, row, table, parent=None, editable_fields=None):
        super().__init__(parent)
        self.setWindowTitle("调整记录 · " + TABLE_LABELS.get(table, table))
        self.original = copy.deepcopy(row)
        self.inputs = {}
        form = QFormLayout(self)
        form.addRow(
            _plain_label(
                (
                    "顾问有效期属于账号安全设置，保存后立即生效，顾问须重新登录。"
                    if table == "staff_account_terms"
                    else "修改将暂存，下次打开或云端重新登录后生效。"
                )
                + "编号、归属和系统时间不能更改。"
                "时间保持原格式（例如 2026-09-19T08:00:00Z）；空值使用 null。"
            )
        )
        for key, value in row.items():
            label = f"{FIELD_LABELS.get(key, key)}（{key}）"
            if (
                key in PROTECTED_COLUMNS
                or table in READ_ONLY_TABLES
                or editable_fields is not None
                and key not in editable_fields
            ):
                display = _plain_label(str(value) if value is not None else "未填写")
                display.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                form.addRow(label, display)
                continue
            if key.endswith("_json") or key in {"content", "summary", "medical_notes", "bio"}:
                editor = QPlainTextEdit(str(value) if value is not None else "null")
                editor.setMinimumHeight(90)
            else:
                editor = QLineEdit(str(value) if value is not None else "null")
            self.inputs[key] = editor
            form.addRow(label, editor)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText(
            "立即保存有效期" if table == "staff_account_terms" else "暂存修改"
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(bool(self.inputs))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        scroll_form_dialog(self, form)

    def changes(self):
        patch = {}
        for key, editor in self.inputs.items():
            text = editor.toPlainText() if isinstance(editor, QPlainTextEdit) else editor.text()
            old = self.original[key]
            value = None if text == "null" else text
            if value is not None and isinstance(old, (int, float)):
                value = type(old)(text)
            if old != value:
                patch[key] = value
        return patch


class UserDataDialog(AsyncDialog):
    def __init__(self, service, details, parent=None):
        super().__init__(parent)
        self.service, self.details = service, details
        self.user_id = int(details["user"]["id"])
        self.setWindowTitle("用户资料 · " + str(details["user"]["username"]))
        layout = QVBoxLayout(self)
        layout.addWidget(
            _plain_label(
                "业务资料在下次打开或云端重新登录时生效；账号名称、密码、停用和顾问有效期立即生效。"
                "不会读取原密码、密码哈希、登录令牌或用户 API Key。"
            )
        )
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        account = QWidget()
        form = QFormLayout(account)
        self.username = QLineEdit(str(details["user"]["username"]))
        self.password = _PasswordField("不修改请留空；原密码不可读取")
        self.status = QComboBox()
        self.status.addItem("启用", "active")
        self.status.addItem("停用", "disabled")
        self.status.setCurrentIndex(max(0, self.status.findData(details["user"]["account_status"])))
        form.addRow("账号名称", self.username)
        form.addRow("重置密码", self.password)
        form.addRow("账号状态", self.status)
        save_account = QPushButton("立即保存账号安全设置")
        save_account.clicked.connect(self._save_account)
        form.addRow(save_account)
        self.tabs.addTab(account, "账号与密码重置")
        records = QWidget()
        records_layout = QVBoxLayout(records)
        self.table_selector = QComboBox()
        records_layout.addWidget(self.table_selector)
        self.records = _table(["记录", "内容"])
        records_layout.addWidget(self.records, 1)
        self.records.cellDoubleClicked.connect(self._edit_record)
        edit_button = QPushButton("查看或调整选中记录（也可双击）")
        edit_button.clicked.connect(lambda: self._edit_record(self.records.currentRow(), 0))
        records_layout.addWidget(edit_button)
        paging = QHBoxLayout()
        previous = QPushButton("上一页记录")
        following = QPushButton("下一页记录")
        previous.clicked.connect(lambda: self._record_page(-1))
        following.clicked.connect(lambda: self._record_page(1))
        self.record_page_label = _plain_label("")
        paging.addWidget(previous)
        paging.addWidget(self.record_page_label, 1)
        paging.addWidget(following)
        records_layout.addLayout(paging)
        self.pending_label = _plain_label("")
        records_layout.addWidget(self.pending_label)
        self.tabs.addTab(records, "业务数据库与聊天记录")
        self.pending_details = QPlainTextEdit()
        self.pending_details.setReadOnly(True)
        self.tabs.addTab(self.pending_details, "待生效修改与冲突")
        self._record_pages = {}
        self.table_selector.currentIndexChanged.connect(self._show_table)
        self._set_details(details)
        close = QPushButton("关闭")
        close.clicked.connect(self.reject)
        layout.addWidget(close)
        fit_dialog(self, width=1150, height=820)

    def _set_details(self, details):
        self.details = details
        selected = self.table_selector.currentData()
        self.table_selector.blockSignals(True)
        self.table_selector.clear()
        for name, rows in details.get("tables", {}).items():
            self.table_selector.addItem(f"{TABLE_LABELS.get(name, name)} · {len(rows)} 条", name)
        if selected:
            self.table_selector.setCurrentIndex(max(0, self.table_selector.findData(selected)))
        self.table_selector.blockSignals(False)
        pending = details.get("pending_changes", [])
        self.pending_details.setPlainText(json.dumps(pending, ensure_ascii=False, indent=2))
        self.pending_label.setText(
            f"待下次打开或云端重新登录处理：{len(pending)} 条。"
            + ("如用户同时修改了同一条资料，将保留冲突供人工核对。" if pending else "")
        )
        self._show_table()

    def _show_table(self):
        name = self.table_selector.currentData()
        page = self._record_pages.get(name)
        self._rows = page["records"] if page else self.details.get("tables", {}).get(name, [])
        offset = page.get("offset", 0) if page else 0
        total = (
            str(page["total"])
            if page
            else (
                "更多" if name in self.details.get("truncated_tables", []) else str(len(self._rows))
            )
        )
        self.record_page_label.setText(
            f"已显示第 {offset + 1 if self._rows else 0} 至 "
            f"{offset + len(self._rows)} 条 / 共 {total} 条"
        )
        self.records.setRowCount(len(self._rows))
        for index, row in enumerate(self._rows):
            identity = row.get("id", row.get("user_id", row.get("task_id", index + 1)))
            text = " | ".join(
                f"{FIELD_LABELS.get(k, k)}: {v}"
                for k, v in row.items()
                if k not in PROTECTED_COLUMNS and v is not None
            )
            self.records.setItem(index, 0, QTableWidgetItem(str(identity)))
            self.records.setItem(index, 1, QTableWidgetItem(text[:1000]))

    def _record_page(self, direction):
        table = self.table_selector.currentData()
        current = self._record_pages.get(table)
        offset = current["offset"] if current else 0
        if direction > 0:
            target = offset + len(self._rows)
            if current and target >= current["total"]:
                return
            if not current and table not in self.details.get("truncated_tables", []):
                return
        else:
            target = max(0, offset - 100)
        if not callable(getattr(self.service, "table_page", None)):
            return

        def shown(page):
            self._record_pages[table] = page
            self._show_table()

        self._run(self.service.table_page, shown, self.user_id, table, target, 100)

    def _edit_record(self, index, _column):
        if not 0 <= index < len(self._rows):
            return
        table, row = self.table_selector.currentData(), self._rows[index]
        editable = self.details.get("editable_fields", {}).get(table)
        dialog = RecordEditDialog(row, table, self, editable_fields=editable)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            changes = dialog.changes()
        except (TypeError, ValueError):
            QMessageBox.warning(self, "数值格式", "请在数值字段填写有效数字。")
            return
        if not changes:
            return
        row_id = row.get(
            "id",
            row.get("product_id")
            if table == "member_cart"
            else row.get("task_id")
            if table == "visit_task_details"
            else row.get("user_id"),
        )

        def save():
            self.service.update_record(self.user_id, table, row_id, changes)
            return self.service.user_details(self.user_id)

        self._run(save, self._set_details)

    def _save_account(self):
        changes = {}
        if self.username.text() != self.details["user"]["username"]:
            changes["username"] = self.username.text()
        if self.status.currentData() != self.details["user"]["account_status"]:
            changes["account_status"] = self.status.currentData()
        password = self.password.line_edit.text()
        self.password.line_edit.clear()
        if password:
            changes["password"] = password
        if not changes:
            return
        if (
            QMessageBox.question(
                self, "立即生效的安全变更", "确认更改此账号？密码重置和停用可能使现有登录失效。"
            )
            != QMessageBox.StandardButton.Yes
        ):
            return

        def save():
            self.service.update_user(self.user_id, changes)
            return self.service.user_details(self.user_id)

        self._run(save, self._set_details)


class EntryListEditor(QWidget):
    """Bounded descriptive skills/knowledge, not executable Python or shell plugins."""

    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self.kind, self.entries = kind, []
        layout = QVBoxLayout(self)
        self.list = QListWidget()
        layout.addWidget(self.list)
        buttons = QHBoxLayout()
        add, edit, remove = QPushButton("新增"), QPushButton("调整"), QPushButton("删除")
        buttons.addWidget(add)
        buttons.addWidget(edit)
        buttons.addWidget(remove)
        layout.addLayout(buttons)
        add.clicked.connect(lambda: self._edit(-1))
        edit.clicked.connect(lambda: self._edit(self.list.currentRow()))
        remove.clicked.connect(self._remove)
        self.list.itemDoubleClicked.connect(lambda _: self._edit(self.list.currentRow()))

    def set_entries(self, entries):
        self.entries = copy.deepcopy(entries)
        self.list.clear()
        for row in self.entries:
            label = row.get("name", row.get("title", "未命名"))
            state = "启用" if row.get("enabled") else "停用"
            if self.kind == "knowledge":
                state += " · 已审核" if row.get("reviewed") else " · 未审核，不参与回答"
            self.list.addItem(f"{label}（{state}）")

    def _remove(self):
        index = self.list.currentRow()
        if (
            0 <= index < len(self.entries)
            and QMessageBox.question(self, "移除条目", "确认从待发布配置中移除此条目？")
            == QMessageBox.StandardButton.Yes
        ):
            self.entries.pop(index)
            self.set_entries(self.entries)

    def _edit(self, index):
        knowledge = self.kind == "knowledge"
        defaults = (
            {
                "title": "",
                "content": "",
                "source": "",
                "reviewed": False,
                "keywords": [],
                "enabled": True,
            }
            if knowledge
            else {"name": "", "instructions": "", "triggers": [], "enabled": True}
        )
        item = copy.deepcopy(self.entries[index] if 0 <= index < len(self.entries) else defaults)
        dialog = QDialog(self)
        dialog.setWindowTitle("专业知识条目" if knowledge else "任务技能（描述性规则）")
        form = QFormLayout(dialog)
        title = QLineEdit(item["title" if knowledge else "name"])
        content = QPlainTextEdit(item["content" if knowledge else "instructions"])
        content.setMinimumHeight(200)
        tags = QLineEdit("，".join(item["keywords" if knowledge else "triggers"]))
        enabled = QCheckBox("启用此条目")
        enabled.setChecked(item["enabled"])
        form.addRow("名称", title)
        form.addRow("正文" if knowledge else "工作规则（不支持执行代码）", content)
        form.addRow("匹配关键词（用逗号分开）", tags)
        source = QLineEdit(item.get("source", ""))
        reviewed = QCheckBox("已核对来源、适用条件和内容，允许参与回答")
        reviewed.setChecked(item.get("reviewed", False))
        if knowledge:
            form.addRow("原文来源 / 文献出处", source)
            form.addRow(reviewed)
        form.addRow(enabled)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        scroll_form_dialog(dialog, form)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        item["title" if knowledge else "name"] = title.text()
        item["content" if knowledge else "instructions"] = content.toPlainText()
        item["keywords" if knowledge else "triggers"] = [
            x.strip() for x in tags.text().replace("，", ",").split(",") if x.strip()
        ]
        item["enabled"] = enabled.isChecked()
        if knowledge:
            item.update(source=source.text(), reviewed=reviewed.isChecked())
        if 0 <= index < len(self.entries):
            self.entries[index] = item
        else:
            self.entries.append(item)
        self.set_entries(self.entries)


class DeveloperWorkspaceDialog(AsyncDialog):
    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service, self._snapshot = service, None
        self.setWindowTitle("开发者工作台")
        layout = QVBoxLayout(self)
        self.revision_label = _plain_label("正在读取已发布设置…")
        layout.addWidget(self.revision_label)
        layout.addWidget(
            _plain_label(
                "配置发布不改变当前会话；用户下次打开或云端重新登录后采用新版本和暂存的业务修改。"
                "密码与账号停用立即生效。关闭开发者界面即清除本次开发会话。"
            )
        )
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self._build_users()
        self._build_ai()
        self._build_features()
        self._build_runtime()
        footer = QHBoxLayout()
        reload_button = QPushButton("重新读取已发布设置")
        reload_button.clicked.connect(self.reload)
        self.publish_button = QPushButton("发布 AI / 开放功能 / 运行设置")
        self.publish_button.setObjectName("PrimaryButton")
        self.publish_button.clicked.connect(self._publish)
        close = QPushButton("退出开发者模式")
        close.clicked.connect(self.reject)
        footer.addWidget(reload_button)
        footer.addWidget(self.publish_button)
        footer.addWidget(close)
        layout.addLayout(footer)
        fit_dialog(self, width=1300, height=900)

    def _build_users(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.role_tabs = QTabWidget()
        self.user_tables = {}
        self.user_page_labels = {}
        self._user_offsets = {}
        self._user_totals = {}
        for role, name in (("operator", "管理者表"), ("advisor", "顾问表"), ("member", "会员表")):
            role_page = QWidget()
            role_layout = QVBoxLayout(role_page)
            table = _table(["编号", "账号", "状态", "建立时间"])
            table.cellDoubleClicked.connect(lambda row, _col, r=role: self._open_user(r, row))
            self.user_tables[role] = table
            role_layout.addWidget(table, 1)
            buttons = QHBoxLayout()
            previous, following = QPushButton("上一页"), QPushButton("下一页")
            previous.clicked.connect(lambda _=False, r=role: self._user_page(r, -1))
            following.clicked.connect(lambda _=False, r=role: self._user_page(r, 1))
            label = _plain_label("")
            self.user_page_labels[role] = label
            buttons.addWidget(previous)
            buttons.addWidget(label, 1)
            buttons.addWidget(following)
            role_layout.addLayout(buttons)
            self.role_tabs.addTab(role_page, name)
        layout.addWidget(self.role_tabs, 1)
        layout.addWidget(
            _plain_label(
                "双击一行查看完整业务资料，包括个人档案、AI 对话、记录和服务。"
                "附件只显示元信息；不暴露服务器文件路径、密钥或密码哈希。"
            )
        )
        refresh = QPushButton("刷新三类用户表")
        refresh.clicked.connect(self._load_users)
        layout.addWidget(refresh)
        self.tabs.addTab(page, "① 用户数据库")

    def _scroll_tab(self, name):
        body = QWidget()
        form = QFormLayout(body)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        self.tabs.addTab(scroll, name)
        return form

    def _build_ai(self):
        form = self._scroll_tab("② AI 与专业知识")
        self.ai_enabled = QCheckBox("启用 AI 服务")
        self.model = QComboBox()
        self.model.addItem("DeepSeek Flash", "deepseek-v4-flash")
        self.model.addItem("DeepSeek Flash · 图片理解", "deepseek-v4-flash-vision-exp")
        self.complexity = QComboBox()
        for label, value in (
            ("通俗简明", "simple"),
            ("标准说明", "standard"),
            ("深入细致", "detailed"),
        ):
            self.complexity.addItem(label, value)
        self.tone = QComboBox()
        for label, value in (("温和亲切", "warm"), ("简洁直接", "concise"), ("正式稳重", "formal")):
            self.tone.addItem(label, value)
        self.tokens = QSpinBox()
        self.tokens.setRange(256, 2048)
        self.key = _PasswordField("留空保留原 Key；仅写入，不显示当前密钥")
        self.key_state = _plain_label("密钥状态待读取")
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText("补充预提示词；不覆盖应用的医疗、安全、权限和事实边界。")
        self.prompt.setMinimumHeight(130)
        self.skills = EntryListEditor("skills")
        self.knowledge = EntryListEditor("knowledge")
        self.tips = QPlainTextEdit()
        self.tips.setPlaceholderText("每行一条每日提示，最多 20 条。")
        for label, widget in (
            ("", self.ai_enabled),
            ("默认模型", self.model),
            ("回答复杂度", self.complexity),
            ("表达语气", self.tone),
            ("最大输出长度（token）", self.tokens),
            ("替换 API Key", self.key),
            ("当前密钥", self.key_state),
            ("补充预提示词", self.prompt),
            ("技能 / Skill（描述性任务规则）", self.skills),
            ("专业知识（人工审核的小型知识条目）", self.knowledge),
            ("每日提示", self.tips),
        ):
            form.addRow(label, widget)
        form.addRow(
            _plain_label(
                "这里管理的是有来源的文本知识条目，不是模型训练或无限量视频数据库。"
                "知识内容视为参考资料，不能包含要求模型绕过安全和隐私限制的指令。"
            )
        )

    def _build_features(self):
        form = self._scroll_tab("③ 板块与服务开放")
        self.features = {}
        for name, label in FEATURE_LABELS.items():
            box = QCheckBox("开放 " + label)
            self.features[name] = box
            form.addRow(box)
        form.addRow(
            _plain_label(
                "取消开放只关闭入口和相关调用，不删除已有记录；下次打开或云端重新登录后生效。"
            )
        )

    def _build_runtime(self):
        form = self._scroll_tab("④ 云服务运行设置")
        self.server_status = QPlainTextEdit()
        self.server_status.setReadOnly(True)
        self.server_status.setMaximumHeight(170)
        form.addRow("当前服务状态（只读）", self.server_status)
        self.announcement = QPlainTextEdit()
        self.support = QLineEdit()
        self.maintenance = QCheckBox("启用维护提示")
        self.maintenance_message = QPlainTextEdit()
        self.timeout = QSpinBox()
        self.timeout.setRange(30, 120)
        form.addRow("公告", self.announcement)
        form.addRow("支持联系方式", self.support)
        form.addRow(self.maintenance)
        form.addRow("维护提示内容", self.maintenance_message)
        form.addRow("客户端等待时长（秒）", self.timeout)
        form.addRow(
            _plain_label(
                "此处调整应用级运行设置。操作系统、SSH、数据库连接串、云盘、HTTPS 证书"
                "和防火墙须通过云控制台或受控部署维护，不允许桌面端执行远程命令。"
            )
        )

    def reload(self):
        def fetch():
            settings = self.service.settings()
            return (
                settings,
                self.service.status(),
                {role: self.service.list_users(role) for role in self.user_tables},
            )

        self._run(fetch, self._loaded)

    def _loaded(self, result):
        value, status, users = result
        self._apply_settings(value)
        self.server_status.setPlainText(json.dumps(status, ensure_ascii=False, indent=2))
        self._set_users(users)

    def _apply_settings(self, value):
        self._snapshot = safe_snapshot(value)
        self.revision_label.setText(
            f"已发布版本：{self._snapshot['revision']} · 本次修改需点击发布"
        )
        settings = self._snapshot["settings"]
        ai = settings["ai"]
        for widget, field in (
            (self.model, "model"),
            (self.complexity, "complexity"),
            (self.tone, "tone"),
        ):
            widget.setCurrentIndex(max(0, widget.findData(ai[field])))
        self.ai_enabled.setChecked(ai["enabled"])
        self.tokens.setValue(ai["max_tokens"])
        self.prompt.setPlainText(ai["system_prompt"])
        self.skills.set_entries(ai["skills"])
        self.knowledge.set_entries(ai["knowledge"])
        self.tips.setPlainText("\n".join(ai["daily_tips"]))
        self.key.line_edit.clear()
        self.key_state.setText(
            "已安全配置（不显示原文）" if value.get("api_key_configured") else "尚未配置"
        )
        for name, box in self.features.items():
            box.setChecked(settings["features"][name])
        runtime = settings["runtime"]
        self.announcement.setPlainText(runtime["announcement"])
        self.support.setText(runtime["support_contact"])
        self.maintenance.setChecked(runtime["maintenance_enabled"])
        self.maintenance_message.setPlainText(runtime["maintenance_message"])
        self.timeout.setValue(runtime["client_timeout_seconds"])

    def _load_users(self):
        self._run(
            lambda: {role: self.service.list_users(role) for role in self.user_tables},
            self._set_users,
        )

    def _user_page(self, role, direction):
        offset = self._user_offsets.get(role, 0)
        target = max(0, offset + 100 * direction)
        if direction > 0 and target >= self._user_totals.get(role, 0):
            return
        self._run(lambda: {role: self.service.list_users(role, target, 100)}, self._set_users)

    def _set_users(self, values):
        if not hasattr(self, "_users"):
            self._users = {}
        for role, result in values.items():
            rows = result["users"]
            self._users[role] = rows
            offset = result.get("offset", 0)
            total = result.get("total", len(rows))
            self._user_offsets[role], self._user_totals[role] = offset, total
            self.user_page_labels[role].setText(
                f"第 {offset + 1 if rows else 0} 至 {offset + len(rows)} 位 / 共 {total} 位"
            )
            table = self.user_tables[role]
            table.setRowCount(len(rows))
            for index, user in enumerate(rows):
                for column, field in enumerate(("id", "username", "account_status", "created_at")):
                    table.setItem(index, column, QTableWidgetItem(str(user.get(field, ""))))
            table.resizeColumnsToContents()

    def _open_user(self, role, row):
        users = getattr(self, "_users", {}).get(role, [])
        if not 0 <= row < len(users):
            return

        def show(details):
            # The worker has completed before entering another dialog's loop.
            dialog = UserDataDialog(self.service, details, self)
            dialog.exec()

        self._run(self.service.user_details, show, users[row]["id"])

    def _publish(self):
        if self._snapshot is None:
            return
        settings = {
            "ai": {
                "enabled": self.ai_enabled.isChecked(),
                "model": self.model.currentData(),
                "complexity": self.complexity.currentData(),
                "tone": self.tone.currentData(),
                "max_tokens": self.tokens.value(),
                "system_prompt": self.prompt.toPlainText(),
                "skills": self.skills.entries,
                "knowledge": self.knowledge.entries,
                "daily_tips": [
                    s.strip() for s in self.tips.toPlainText().splitlines() if s.strip()
                ],
            },
            "features": {name: box.isChecked() for name, box in self.features.items()},
            "runtime": {
                "announcement": self.announcement.toPlainText(),
                "support_contact": self.support.text(),
                "maintenance_message": self.maintenance_message.toPlainText(),
                "maintenance_enabled": self.maintenance.isChecked(),
                "client_timeout_seconds": self.timeout.value(),
            },
        }
        api_key = self.key.line_edit.text().strip() or None
        self.key.line_edit.clear()
        if (
            QMessageBox.question(
                self, "发布下一次启动配置", "确认发布这些设置？当前已打开的应用保持原设置。"
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._run(
            self.service.publish_settings,
            self._published,
            self._snapshot["revision"],
            settings,
            api_key,
        )

    def _published(self, value):
        self._apply_settings(value)
        QMessageBox.information(self, "已发布", "新配置已保存，用户下次打开或云端重新登录后生效。")

    def done(self, result):
        if not self._tasks:
            self.key.line_edit.clear()
            self.service.close_session()
            super().done(result)
