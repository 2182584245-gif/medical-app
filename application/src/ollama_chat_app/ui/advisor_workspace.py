from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..time_utils import display_timezone_label, format_beijing
from .advisor_ai_panel import AdvisorAiPanel
from .staff_style import build_staff_style
from .time_fields import BeijingDateTimeEdit, beijing_qdatetime, datetime_iso

TASK_LABELS = {
    "pending": "待上门",
    "in_progress": "进行中",
    "completed": "已完成",
    "cancelled": "已取消",
}
CATEGORY_LABELS = {
    "diet": "饮食",
    "water": "饮水",
    "activity": "活动",
    "sleep": "作息",
    "environment": "环境",
}
GENDER_LABELS = {"female": "女", "male": "男", "other": "其他 / 不便说明"}
PRODUCT_CATEGORY_LABELS = {
    "food": "食品与饮品",
    "daily": "日常生活用品",
    "home": "居家用品",
    "other": "其他生活用品",
}
RECOMMENDATION_LABELS = {
    "new": "会员尚未查看",
    "viewed": "会员已查看",
    "interested": "会员感兴趣",
}


def _display_time(value: object) -> str:
    return format_beijing(value, empty="未安排")


def _error_text(action: str, error: Exception) -> str:
    return f"{action}失败：{error}\n请先刷新数据后重试；如果仍然失败，请导出备份并联系运营人员。"


def _plain_value(value: object) -> str:
    if value in (None, "", [], {}):
        return "未填写"
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _display_money(value: object) -> str:
    try:
        return f"¥{int(value or 0) / 100:.2f}"
    except (TypeError, ValueError):
        return "¥0.00"


class AdvisorVisitCompletionDialog(QDialog):
    """Collect a factual visit record without making health judgments."""

    def __init__(self, task: Mapping[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("完成上门任务")
        self.resize(680, 760)
        root = QVBoxLayout(self)

        heading = QLabel(
            f"会员：{task.get('member_name') or '未填写'}　任务：{task.get('title') or '上门服务'}"
        )
        heading.setObjectName("DialogHeading")
        heading.setWordWrap(True)
        root.addWidget(heading)
        hint = QLabel("请记录本次服务中实际发生的事情；档案修改说明不会自动改写会员档案。")
        hint.setObjectName("AdvisorHint")
        hint.setWordWrap(True)
        root.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        form = QFormLayout(body)
        form.setContentsMargins(14, 12, 22, 12)
        form.setVerticalSpacing(12)

        self.summary_input = self._text_input(
            "例如：已完成首次上门，核对了生活档案和近期生活记录。", 110
        )
        form.addRow("上门总结 *", self.summary_input)
        self.duration_input = QSpinBox()
        self.duration_input.setRange(1, 1_440)
        self.duration_input.setValue(60)
        self.duration_input.setSuffix(" 分钟")
        form.addRow("服务时长 *", self.duration_input)

        self.inspected_areas_input = self._text_input("本次查看了哪些内容", 74)
        form.addRow("本次查看内容", self.inspected_areas_input)
        self.user_questions_input = self._text_input("会员提出了哪些问题", 74)
        form.addRow("会员提出的问题", self.user_questions_input)
        self.environment_changes_input = self._text_input("记录观察到的家庭环境变化", 74)
        form.addRow("家庭环境变化", self.environment_changes_input)
        self.profile_changes_input = self._text_input("仅记录建议修改的内容，之后另行确认", 74)
        form.addRow("档案修改说明", self.profile_changes_input)
        self.follow_ups_input = self._text_input("下次需要跟进的事项", 74)
        form.addRow("后续待办", self.follow_ups_input)
        self.gifts_input = self._text_input("本次实际交付的礼品，没有可留空", 74)
        form.addRow("礼品记录", self.gifts_input)
        self.notes_input = self._text_input("顾问内部备注", 74)
        form.addRow("顾问备注", self.notes_input)

        self.membership_benefit_input = QCheckBox("计入一次会员上门服务")
        self.membership_benefit_input.setChecked(True)
        form.addRow("会员权益", self.membership_benefit_input)
        self.first_filing_input = QCheckBox("本次已完成首次建档")
        self.first_filing_input.setChecked("首次" in str(task.get("title") or ""))
        form.addRow("建档状态", self.first_filing_input)

        self.create_next_visit_input = QCheckBox("安排下次上门")
        self.create_next_visit_input.toggled.connect(self._toggle_next_visit)
        form.addRow("下次上门", self.create_next_visit_input)
        self.next_visit_input = BeijingDateTimeEdit(beijing_qdatetime().addDays(30))
        self.next_visit_input.setEnabled(False)
        form.addRow(f"下次上门时间（{display_timezone_label()}）", self.next_visit_input)
        self.member_reminder_input = QCheckBox("同时为会员创建上门提醒")
        self.member_reminder_input.setChecked(True)
        self.member_reminder_input.setEnabled(False)
        form.addRow("会员提醒", self.member_reminder_input)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("确认完成任务")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @staticmethod
    def _text_input(placeholder: str, height: int) -> QTextEdit:
        editor = QTextEdit()
        editor.setPlaceholderText(placeholder)
        editor.setFixedHeight(height)
        return editor

    def _toggle_next_visit(self, checked: bool) -> None:
        self.next_visit_input.setEnabled(checked)
        self.member_reminder_input.setEnabled(checked)
        if not checked:
            self.member_reminder_input.setChecked(False)
        elif not self.member_reminder_input.isChecked():
            self.member_reminder_input.setChecked(True)

    def _validate(self) -> None:
        if not self.summary_input.toPlainText().strip():
            QMessageBox.warning(self, "上门总结未填写", "请先填写上门总结，再完成任务。")
            self.summary_input.setFocus()
            return
        if (
            self.create_next_visit_input.isChecked()
            and self.next_visit_input.dateTime() <= beijing_qdatetime()
        ):
            QMessageBox.warning(self, "下次时间无效", "下次上门时间必须晚于当前时间，请重新选择。")
            self.next_visit_input.setFocus()
            return
        self.accept()

    @property
    def values(self) -> dict[str, object]:
        details = {
            "duration_minutes": self.duration_input.value(),
            "inspected_areas": self.inspected_areas_input.toPlainText().strip(),
            "user_questions": self.user_questions_input.toPlainText().strip(),
            "environment_changes": self.environment_changes_input.toPlainText().strip(),
            "profile_changes": self.profile_changes_input.toPlainText().strip(),
            "follow_ups": self.follow_ups_input.toPlainText().strip(),
            "gifts": self.gifts_input.toPlainText().strip(),
            "notes": self.notes_input.toPlainText().strip(),
            "uses_membership_benefit": self.membership_benefit_input.isChecked(),
            "first_filing_completed": self.first_filing_input.isChecked(),
        }
        has_next = self.create_next_visit_input.isChecked()
        return {
            "summary": self.summary_input.toPlainText().strip(),
            "details": details,
            "next_visit_at": (datetime_iso(self.next_visit_input.dateTime()) if has_next else None),
            "next_visit_title": "定期复访",
            "create_member_reminder": has_next and self.member_reminder_input.isChecked(),
        }


class AdvisorWorkspace(QWidget):
    """Role-scoped advisor UI; all authorization remains in the service layer."""

    logout_requested = Signal()
    export_requested = Signal()
    import_requested = Signal()

    def __init__(
        self,
        service_management: object,
        backup_service: object | None = None,
        commerce_service: object | None = None,
        ai_assistant_service: object | None = None,
        secret_store: object | None = None,
    ) -> None:
        super().__init__()
        self.setObjectName("AdvisorWorkspace")
        self.service = service_management
        self.backup_service = backup_service
        self.commerce = commerce_service
        self.ai_panel = AdvisorAiPanel(ai_assistant_service, secret_store)
        self.current_user: Any | None = None
        self.actor_user_id: int | None = None
        self.members: list[dict[str, Any]] = []
        self.tasks: list[dict[str, Any]] = []
        self.records: list[dict[str, Any]] = []
        self.products: list[dict[str, Any]] = []
        self.recommendations: list[dict[str, Any]] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)
        header = QFrame()
        header.setObjectName("AdvisorHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 11, 16, 11)
        title = QLabel("家庭生活顾问工作台")
        title.setObjectName("AdvisorTitle")
        header_layout.addWidget(title)
        header_layout.addStretch(1)
        self.user_label = QLabel("未登录")
        header_layout.addWidget(self.user_label)
        self.export_button = QPushButton("导出数据")
        self.export_button.setEnabled(backup_service is not None)
        self.export_button.clicked.connect(self.export_requested)
        header_layout.addWidget(self.export_button)
        self.import_button = QPushButton("导入数据")
        self.import_button.setEnabled(backup_service is not None)
        self.import_button.clicked.connect(self.import_requested)
        header_layout.addWidget(self.import_button)
        logout_button = QPushButton("退出登录")
        logout_button.clicked.connect(self.logout_requested)
        header_layout.addWidget(logout_button)
        root.addWidget(header)
        timezone_hint = QLabel("日期和时间按右上角所选时区显示，默认北京时间。")
        timezone_hint.setObjectName("AdvisorHint")
        root.addWidget(timezone_hint)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        root.addWidget(self.status_label)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._build_overview_tab(), "概览")
        self.tabs.addTab(self._build_members_tab(), "我的会员")
        self.tabs.addTab(self._build_tasks_tab(), "上门任务")
        self.tabs.addTab(self._build_history_tab(), "历史上门")
        self.tabs.addTab(self._build_products_tab(), "商品推荐")
        self.tabs.addTab(self.ai_panel, "AI 工作摘要")
        root.addWidget(self.tabs, 1)
        self.apply_preferences({})

    def apply_preferences(self, preferences: dict) -> None:
        self.setStyleSheet(build_staff_style(preferences))

    def _build_overview_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        intro = QLabel("这里仅汇总当前顾问已绑定的会员和自己的上门服务数据。")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        row = QHBoxLayout()
        self.metric_labels: dict[str, QLabel] = {}
        for code, label in (
            ("active_member_count", "已绑定会员"),
            ("pending_visit_count", "待办上门"),
            ("completed_visit_count", "已完成上门"),
        ):
            frame = QFrame()
            frame.setObjectName("AdvisorCard")
            card = QVBoxLayout(frame)
            value = QLabel("0")
            value.setObjectName("AdvisorMetricValue")
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            card.addWidget(value)
            name = QLabel(label)
            name.setAlignment(Qt.AlignmentFlag.AlignCenter)
            card.addWidget(name)
            row.addWidget(frame)
            self.metric_labels[code] = value
        layout.addLayout(row)
        guide = QLabel("建议顺序：选择会员核对资料 → 开始上门任务 → 记录事实并完成任务。")
        guide.setWordWrap(True)
        layout.addWidget(guide)
        refresh_button = QPushButton("刷新工作台")
        refresh_button.clicked.connect(self.refresh)
        layout.addWidget(refresh_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        return page

    def _build_members_tab(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        left = QVBoxLayout()
        left.addWidget(QLabel("已绑定会员"))
        self.member_list = QListWidget()
        self.member_list.currentItemChanged.connect(self._show_member)
        left.addWidget(self.member_list, 1)
        layout.addLayout(left, 2)

        right = QVBoxLayout()
        self.member_name_label = QLabel("请从左侧选择会员")
        self.member_name_label.setObjectName("AdvisorSectionTitle")
        right.addWidget(self.member_name_label)
        self.member_tabs = QTabWidget()
        self.profile_text = self._read_only_text()
        self.facts_text = self._read_only_text()
        self.life_records_text = self._read_only_text()
        self.member_tabs.addTab(self.profile_text, "生活档案")
        self.member_tabs.addTab(self.facts_text, "已确认事实")
        self.member_tabs.addTab(self.life_records_text, "近期生活记录")
        right.addWidget(self.member_tabs, 1)
        disclaimer = QLabel("这里只展示会员已保存的事实；顾问备注不会直接改写正式档案。")
        disclaimer.setObjectName("AdvisorHint")
        disclaimer.setWordWrap(True)
        right.addWidget(disclaimer)
        layout.addLayout(right, 4)
        return page

    @staticmethod
    def _read_only_text() -> QTextEdit:
        widget = QTextEdit()
        widget.setReadOnly(True)
        return widget

    def _build_tasks_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("这里只显示分配给当前顾问且会员绑定仍然有效的上门任务。"))
        self.task_list = QListWidget()
        self.task_list.currentItemChanged.connect(self._task_selection_changed)
        layout.addWidget(self.task_list, 1)
        actions = QHBoxLayout()
        self.start_button = QPushButton("开始选中任务")
        self.start_button.clicked.connect(self._start_selected_task)
        actions.addWidget(self.start_button)
        self.complete_button = QPushButton("完成并填写上门记录")
        self.complete_button.setObjectName("PrimaryButton")
        self.complete_button.clicked.connect(self._complete_selected_task)
        actions.addWidget(self.complete_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self._task_selection_changed(None, None)
        return page

    def _build_history_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("历史上门记录（双击查看完整记录）"))
        self.history_list = QListWidget()
        self.history_list.itemDoubleClicked.connect(self._show_history_detail)
        layout.addWidget(self.history_list, 1)
        return page

    def _build_products_tab(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)

        products_box = QVBoxLayout()
        title = QLabel("可推荐的生活用品")
        title.setObjectName("AdvisorSectionTitle")
        products_box.addWidget(title)
        product_hint = QLabel("这里只显示运营人员已经上架的商品。")
        product_hint.setObjectName("AdvisorHint")
        product_hint.setWordWrap(True)
        products_box.addWidget(product_hint)
        self.product_list = QListWidget()
        self.product_list.currentItemChanged.connect(self._show_product)
        products_box.addWidget(self.product_list, 1)
        layout.addLayout(products_box, 3)

        recommendation_box = QVBoxLayout()
        recommend_title = QLabel("向已绑定会员推荐")
        recommend_title.setObjectName("AdvisorSectionTitle")
        recommendation_box.addWidget(recommend_title)
        self.product_detail = QLabel("请先从左侧选择一件商品。")
        self.product_detail.setWordWrap(True)
        self.product_detail.setMinimumHeight(90)
        self.product_detail.setAlignment(Qt.AlignmentFlag.AlignTop)
        recommendation_box.addWidget(self.product_detail)
        recommendation_box.addWidget(QLabel("选择会员"))
        self.recommend_member_combo = QComboBox()
        recommendation_box.addWidget(self.recommend_member_combo)
        recommendation_box.addWidget(QLabel("非医疗的生活用途推荐理由 *"))
        self.recommend_reason_input = QTextEdit()
        self.recommend_reason_input.setMaximumHeight(110)
        self.recommend_reason_input.setPlaceholderText(
            "例如：便于在客厅随手记录每日饮水量。不得填写治疗、诊断或疗效承诺。"
        )
        recommendation_box.addWidget(self.recommend_reason_input)
        disclaimer = QLabel("推荐仅用于日常生活服务，不构成医疗建议；推荐不会自动创建订单。")
        disclaimer.setObjectName("AdvisorHint")
        disclaimer.setWordWrap(True)
        recommendation_box.addWidget(disclaimer)
        self.recommend_button = QPushButton("保存生活用品推荐")
        self.recommend_button.setObjectName("PrimaryButton")
        self.recommend_button.setEnabled(self.commerce is not None)
        self.recommend_button.clicked.connect(self._recommend_product)
        recommendation_box.addWidget(self.recommend_button)
        recommendation_box.addWidget(QLabel("我的推荐与当前状态"))
        self.recommendation_list = QListWidget()
        recommendation_box.addWidget(self.recommendation_list, 1)
        layout.addLayout(recommendation_box, 4)
        return page

    def start_session(self, user: Any) -> None:
        self.current_user = user
        self.actor_user_id = int(user.id)
        self.user_label.setText(f"当前顾问：{getattr(user, 'username', '')}")
        self.refresh()
        self.ai_panel.start_session(self.actor_user_id, self.members)

    def end_session(self) -> None:
        self.current_user = None
        self.actor_user_id = None
        self.members.clear()
        self.tasks.clear()
        self.records.clear()
        self.products.clear()
        self.recommendations.clear()
        self.user_label.setText("未登录")
        for widget in (
            self.member_list,
            self.task_list,
            self.history_list,
            self.product_list,
            self.recommendation_list,
        ):
            widget.clear()
        self.recommend_member_combo.clear()
        self.recommend_reason_input.clear()
        self.product_detail.setText("请先从左侧选择一件商品。")
        self.member_name_label.setText("请从左侧选择会员")
        self.profile_text.clear()
        self.facts_text.clear()
        self.life_records_text.clear()
        for label in self.metric_labels.values():
            label.setText("0")
        self.status_label.hide()
        self.ai_panel.end_session()

    def refresh(self) -> None:
        if self.actor_user_id is None:
            return
        selected_member_id = self._selected_member_id()
        selected_task_id = self._selected_task_id()
        try:
            dashboard = self.service.get_dashboard(self.actor_user_id)
            for key, label in self.metric_labels.items():
                label.setText(str(dashboard.get(key, 0)))
            self.members = list(self.service.list_members(self.actor_user_id))
            all_tasks = list(self.service.list_visit_tasks(self.actor_user_id))
            self.tasks = [
                task for task in all_tasks if str(task.get("status")) in {"pending", "in_progress"}
            ]
            self.records = list(self.service.list_visit_records(self.actor_user_id))
            self._fill_members(selected_member_id)
            self._fill_tasks(selected_task_id)
            self._fill_history()
            self._fill_recommend_members()
            self.ai_panel.set_members(self.members)
            self.ai_panel.refresh_drafts()
            if self._refresh_commerce():
                self.status_label.hide()
        except Exception as exc:
            self._set_status(_error_text("读取顾问工作台", exc), error=True)

    def _refresh_commerce(self) -> bool:
        if self.actor_user_id is None:
            return False
        if self.commerce is None:
            self.products = []
            self.recommendations = []
            self._fill_products()
            self._fill_recommendations()
            return True
        try:
            self.products = list(
                self.commerce.list_products(self.actor_user_id, include_inactive=False)
            )
            self.recommendations = list(self.commerce.list_recommendations(self.actor_user_id))
            self._fill_products()
            self._fill_recommendations()
            return True
        except Exception as exc:
            self._set_status(_error_text("读取生活用品推荐", exc), error=True)
            return False

    def refresh_snapshot(self, resources):
        """Render only currently visible, mirrored lists without remote callbacks."""
        if self.actor_user_id is None:
            return ()
        from .snapshot_refresh import preserve_views

        tab = self.tabs.currentIndex()
        if tab == 1:
            selected = self._selected_member_id()
            self.members = list(resources["members"])
            with preserve_views(self.member_list):
                self._fill_members(selected)
            if self.member_list.currentRow() < 0:
                self._show_member(None, None)  # Clears details, performs no read.
            else:
                member = self.member_list.currentItem().data(Qt.ItemDataRole.UserRole)
                self.member_name_label.setText(
                    str(
                        member.get("nickname")
                        or member.get("display_name")
                        or member.get("username")
                    )
                )
            self._set_status(
                "会员列表已同步；右侧完整档案不是镜像内容，需主动重新选择会员在线读取。",
                error=False,
            )
            return ("已绑定会员列表",)
        if tab == 2:
            selected = self._selected_task_id()
            self.tasks = [
                item
                for item in resources["appointments"]
                if item.get("status") in {"pending", "in_progress"}
            ]
            with preserve_views(self.task_list):
                self._fill_tasks(selected)
            self._task_selection_changed(self.task_list.currentItem(), None)
            return ("顾问上门任务",)
        if tab in {0, 3}:
            # Visit-task count is not completed-visit-record count. Do not invent
            # a dashboard/history aggregate from a different resource.
            self._set_status(
                "镜像已同步；概览及历史上门明细需点击刷新工作台在线读取。", error=False
            )
        return ()

    def _fill_members(self, selected_id: int = -1) -> None:
        self.member_list.clear()
        selected_row = -1
        for index, member in enumerate(self.members):
            name = str(
                member.get("nickname")
                or member.get("display_name")
                or member.get("username")
                or "会员"
            )
            next_visit = _display_time(member.get("next_visit_at"))
            phone = str(member.get("phone") or "电话未填写")
            item = QListWidgetItem(
                f"{name}｜{phone}\n下次上门（{display_timezone_label()}）：{next_visit}"
            )
            item.setData(Qt.ItemDataRole.UserRole, member)
            self.member_list.addItem(item)
            if int(member["id"]) == selected_id:
                selected_row = index
        if self.members:
            self.member_list.setCurrentRow(max(0, selected_row))
        else:
            item = QListWidgetItem("当前没有已绑定会员，请联系运营人员处理绑定。")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.member_list.addItem(item)

    def _fill_recommend_members(self) -> None:
        selected_id = self.recommend_member_combo.currentData()
        self.recommend_member_combo.clear()
        selected_index = -1
        for index, member in enumerate(self.members):
            member_id = int(member["id"])
            name = str(member.get("display_name") or member.get("username") or "会员")
            self.recommend_member_combo.addItem(name, member_id)
            if selected_id is not None and member_id == int(selected_id):
                selected_index = index
        if selected_index >= 0:
            self.recommend_member_combo.setCurrentIndex(selected_index)

    def _fill_products(self) -> None:
        selected_id = self._selected_product_id()
        self.product_list.clear()
        selected_row = -1
        for index, product in enumerate(self.products):
            category = PRODUCT_CATEGORY_LABELS.get(
                str(product.get("category")), str(product.get("category") or "未分类")
            )
            item = QListWidgetItem(
                f"{product.get('name') or '未命名商品'}｜{category}\n"
                f"{_display_money(product.get('price_cents'))}/{product.get('unit') or '件'}"
            )
            item.setData(Qt.ItemDataRole.UserRole, product)
            self.product_list.addItem(item)
            if int(product["id"]) == selected_id:
                selected_row = index
        if self.products:
            self.product_list.setCurrentRow(max(0, selected_row))
        else:
            text = "商品推荐功能未启用。" if self.commerce is None else "当前没有已上架的生活用品。"
            item = QListWidgetItem(text)
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.product_list.addItem(item)
            self._show_product(None, None)

    def _fill_recommendations(self) -> None:
        self.recommendation_list.clear()
        member_names = {
            int(member["id"]): str(member.get("display_name") or member.get("username") or "会员")
            for member in self.members
            if member.get("id") is not None
        }
        for recommendation in self.recommendations:
            if int(recommendation.get("order_count") or 0) > 0:
                status = (
                    "模拟订单已交付"
                    if recommendation.get("latest_order_status") == "delivered"
                    else "会员已创建模拟订单"
                )
            else:
                status = RECOMMENDATION_LABELS.get(str(recommendation.get("status")), "状态未知")
            member_name = (
                recommendation.get("member_name")
                or recommendation.get("member_username")
                or member_names.get(int(recommendation.get("member_user_id") or -1))
                or f"会员编号 {recommendation.get('member_user_id') or '未知'}"
            )
            item = QListWidgetItem(
                f"[{status}] {member_name}｜"
                f"{recommendation.get('product_name') or '商品未填写'}\n"
                f"{recommendation.get('reason') or '未填写生活用途理由'}｜"
                f"{_display_time(recommendation.get('created_at'))}"
            )
            item.setData(Qt.ItemDataRole.UserRole, recommendation)
            self.recommendation_list.addItem(item)
        if not self.recommendations:
            text = "商品推荐功能未启用。" if self.commerce is None else "当前还没有保存过商品推荐。"
            item = QListWidgetItem(text)
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.recommendation_list.addItem(item)

    def _show_product(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        product = current.data(Qt.ItemDataRole.UserRole) if current else None
        if not isinstance(product, Mapping):
            self.product_detail.setText("请先从左侧选择一件商品。")
            self.recommend_button.setEnabled(False)
            return
        category = PRODUCT_CATEGORY_LABELS.get(
            str(product.get("category")), str(product.get("category") or "未分类")
        )
        self.product_detail.setText(
            f"商品：{product.get('name') or '未命名'}\n\n"
            f"分类：{category}\n\n"
            f"品牌 / 规格：{product.get('brand') or '未填写'} / "
            f"{product.get('specification') or '未填写'}\n\n"
            f"展示价格：{_display_money(product.get('price_cents'))}/"
            f"{product.get('unit') or '件'}\n\n"
            f"生活用途：{product.get('description') or '未填写'}"
        )
        self.recommend_button.setEnabled(
            self.commerce is not None and self.recommend_member_combo.count() > 0
        )

    def _selected_product_id(self) -> int:
        item = self.product_list.currentItem() if hasattr(self, "product_list") else None
        product = item.data(Qt.ItemDataRole.UserRole) if item else None
        return (
            int(product["id"])
            if isinstance(product, Mapping) and product.get("id") is not None
            else -1
        )

    def _recommend_product(self) -> None:
        if self.actor_user_id is None or self.commerce is None:
            return
        product_id = self._selected_product_id()
        member_id = self.recommend_member_combo.currentData()
        reason = self.recommend_reason_input.toPlainText().strip()
        if product_id < 0:
            QMessageBox.information(self, "请先选择", "请先选择一件已上架的商品。")
            return
        if member_id is None:
            QMessageBox.information(self, "暂无会员", "当前没有可以推荐商品的已绑定会员。")
            return
        if not reason:
            QMessageBox.warning(self, "理由未填写", "请填写非医疗的日常生活用途推荐理由。")
            self.recommend_reason_input.setFocus()
            return
        try:
            self.commerce.recommend_product(self.actor_user_id, int(member_id), product_id, reason)
            self.recommend_reason_input.clear()
            self._refresh_commerce()
            self._set_status("生活用品推荐已保存；没有自动创建订单，也没有发生支付。", error=False)
        except Exception as exc:
            QMessageBox.warning(self, "推荐失败", _error_text("保存商品推荐", exc))

    def _show_member(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        member = current.data(Qt.ItemDataRole.UserRole) if current else None
        if not isinstance(member, Mapping) or self.actor_user_id is None:
            self.member_name_label.setText("请从左侧选择会员")
            self.profile_text.clear()
            self.facts_text.clear()
            self.life_records_text.clear()
            return
        name = str(
            member.get("nickname") or member.get("display_name") or member.get("username") or "会员"
        )
        self.member_name_label.setText(name)
        try:
            overview = self.service.get_member_overview(
                self.actor_user_id, int(member["id"]), recent_record_limit=20
            )
            self._fill_member_overview(overview)
        except Exception as exc:
            self.profile_text.setPlainText(_error_text("读取会员资料", exc))
            self.facts_text.clear()
            self.life_records_text.clear()
            self._set_status(_error_text("读取会员资料", exc), error=True)

    def _fill_member_overview(self, overview: Mapping[str, Any]) -> None:
        profile = overview.get("profile")
        profile_data = profile if isinstance(profile, Mapping) else {}
        gender = GENDER_LABELS.get(str(profile_data.get("gender") or ""), "未填写")
        profile_lines = (
            ("姓名", profile_data.get("display_name") or profile_data.get("username")),
            ("出生日期", profile_data.get("birth_date")),
            ("性别", gender),
            ("联系电话", profile_data.get("phone")),
            ("居住情况", profile_data.get("living_situation")),
            ("紧急联系人", profile_data.get("emergency_contact_name")),
            ("紧急联系人电话", profile_data.get("emergency_contact_phone")),
            ("希望 AI 如何称呼", profile_data.get("ai_preferred_name")),
            ("身高（厘米）", profile_data.get("height_cm")),
            ("生活目标", profile_data.get("health_goals")),
            ("饮食偏好", profile_data.get("dietary_preferences")),
            ("会员自填备注", profile_data.get("medical_notes")),
        )
        self.profile_text.setPlainText(
            "\n\n".join(f"{label}：{_plain_value(value)}" for label, value in profile_lines)
        )

        facts = overview.get("confirmed_facts")
        fact_values = facts if isinstance(facts, list) else []
        if fact_values:
            self.facts_text.setPlainText(
                "\n\n".join(
                    f"{fact.get('fact_key') or '事实'}：{_plain_value(fact.get('value'))}\n"
                    f"来源：{fact.get('source') or '未填写'}｜生效（{display_timezone_label()}）："
                    f"{_display_time(fact.get('effective_at'))}"
                    for fact in fact_values
                    if isinstance(fact, Mapping)
                )
            )
        else:
            self.facts_text.setPlainText("暂无已确认事实。")

        records = overview.get("recent_life_records")
        record_values = records if isinstance(records, list) else []
        if record_values:
            self.life_records_text.setPlainText(
                "\n\n".join(
                    f"{_display_time(record.get('occurred_at'))}｜"
                    f"{CATEGORY_LABELS.get(str(record.get('category')), '生活记录')}\n"
                    f"{record.get('content') or '未填写内容'}\n"
                    f"来源：{record.get('source') or '未填写'}"
                    for record in record_values
                    if isinstance(record, Mapping)
                )
            )
        else:
            self.life_records_text.setPlainText("暂无近期生活记录。")

    def _fill_tasks(self, selected_id: int = -1) -> None:
        self.task_list.clear()
        selected_row = -1
        for index, task in enumerate(self.tasks):
            status = TASK_LABELS.get(str(task.get("status")), "状态未知")
            item = QListWidgetItem(
                f"[{status}] {_display_time(task.get('scheduled_at'))}｜"
                f"{task.get('title') or '上门服务'}\n"
                f"会员：{task.get('member_name') or '未填写'}｜"
                f"备注：{task.get('notes') or '无'}"
            )
            item.setData(Qt.ItemDataRole.UserRole, task)
            self.task_list.addItem(item)
            if int(task["id"]) == selected_id:
                selected_row = index
        if self.tasks:
            self.task_list.setCurrentRow(max(0, selected_row))
        else:
            item = QListWidgetItem("当前没有待处理的上门任务。")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.task_list.addItem(item)
            self._task_selection_changed(None, None)

    def _fill_history(self) -> None:
        self.history_list.clear()
        for record in self.records:
            item = QListWidgetItem(
                f"{_display_time(record.get('visited_at'))}｜"
                f"{record.get('member_name') or '会员未填写'}\n"
                f"{record.get('summary') or '无上门总结'}"
            )
            item.setData(Qt.ItemDataRole.UserRole, record)
            self.history_list.addItem(item)
        if not self.records:
            item = QListWidgetItem("暂无历史上门记录。")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.history_list.addItem(item)

    def _task_selection_changed(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        task = current.data(Qt.ItemDataRole.UserRole) if current else None
        status = str(task.get("status")) if isinstance(task, Mapping) else ""
        self.start_button.setEnabled(status == "pending")
        self.complete_button.setEnabled(status in {"pending", "in_progress"})

    def _start_selected_task(self) -> None:
        if self.actor_user_id is None or (task := self._selected_task()) is None:
            return
        try:
            self.service.start_visit_task(self.actor_user_id, int(task["id"]))
            self.refresh()
            self._set_status("上门任务已开始，可在服务结束后填写上门记录。", error=False)
        except Exception as exc:
            QMessageBox.warning(self, "开始失败", _error_text("开始上门任务", exc))

    def _complete_selected_task(self) -> None:
        if self.actor_user_id is None or (task := self._selected_task()) is None:
            return
        dialog = AdvisorVisitCompletionDialog(task, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.start_button.setEnabled(False)
        self.complete_button.setEnabled(False)
        try:
            result = self.service.complete_visit_task(
                self.actor_user_id, int(task["id"]), **dialog.values
            )
            message = "上门记录已保存，任务已完成。"
            if result.get("next_task_id") is not None:
                message += "下次上门任务已创建。"
            if result.get("reminder_id") is not None:
                message += "会员提醒已创建。"
            self.refresh()
            self._set_status(message, error=False)
            self.tabs.setCurrentIndex(3)
        except Exception as exc:
            QMessageBox.warning(self, "完成失败", _error_text("完成上门任务", exc))
            self._task_selection_changed(self.task_list.currentItem(), None)

    def _selected_task(self) -> dict[str, Any] | None:
        item = self.task_list.currentItem()
        value = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not isinstance(value, Mapping):
            QMessageBox.information(self, "请先选择", "请先选择一条自己的上门任务。")
            return None
        return dict(value)

    def _selected_member_id(self) -> int:
        item = self.member_list.currentItem() if hasattr(self, "member_list") else None
        member = item.data(Qt.ItemDataRole.UserRole) if item else None
        return int(member["id"]) if isinstance(member, Mapping) and member.get("id") else -1

    def _selected_task_id(self) -> int:
        item = self.task_list.currentItem() if hasattr(self, "task_list") else None
        task = item.data(Qt.ItemDataRole.UserRole) if item else None
        return int(task["id"]) if isinstance(task, Mapping) and task.get("id") else -1

    def _show_history_detail(self, item: QListWidgetItem) -> None:
        record = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(record, Mapping):
            return
        details = record.get("details")
        detail_values = details if isinstance(details, Mapping) else {}
        labels = (
            ("服务时长（分钟）", "duration_minutes"),
            ("本次查看内容", "inspected_areas"),
            ("会员提出的问题", "user_questions"),
            ("家庭环境变化", "environment_changes"),
            ("档案修改说明", "profile_changes"),
            ("后续待办", "follow_ups"),
            ("礼品记录", "gifts"),
            ("顾问备注", "notes"),
        )
        lines = [
            f"会员：{record.get('member_name') or '未填写'}",
            f"上门时间（{display_timezone_label()}）：{_display_time(record.get('visited_at'))}",
            f"上门总结：{record.get('summary') or '未填写'}",
        ]
        for label, key in labels:
            lines.append(f"{label}：{_plain_value(detail_values.get(key))}")
        QMessageBox.information(self, "上门记录详情", "\n\n".join(lines))

    def _set_status(self, text: str, *, error: bool) -> None:
        self.status_label.setObjectName("AdvisorStatusError" if error else "AdvisorStatusSuccess")
        self.status_label.setText(text)
        self.status_label.setVisible(True)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)


__all__ = ["AdvisorVisitCompletionDialog", "AdvisorWorkspace"]
