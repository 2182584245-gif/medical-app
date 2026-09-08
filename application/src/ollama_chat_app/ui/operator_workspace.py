from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..time_utils import display_timezone_label, format_beijing
from .operator_performance_panel import OperatorPerformancePanel
from .staff_style import build_staff_style
from .time_fields import (
    BeijingDateTimeEdit,
    beijing_qdatetime,
    datetime_iso,
    set_editor_datetime,
)

ROLE_LABELS = {"member": "会员", "advisor": "顾问", "operator": "运营人员"}
ACCOUNT_LABELS = {"active": "已启用", "disabled": "已停用", "locked": "已锁定"}
MEMBERSHIP_LABELS = {
    "active": "有效",
    "pending": "待生效",
    "expired": "已到期",
    "cancelled": "已取消",
}
TASK_LABELS = {
    "pending": "预约中",
    "in_progress": "进行中",
    "completed": "已完成",
    "cancelled": "已停用",
    "incomplete": "未完成",
    "disabled": "已停用",
}
PRODUCT_CATEGORY_LABELS = {
    "food": "食品与饮品",
    "daily": "日常生活用品",
    "home": "居家用品",
    "other": "其他生活用品",
}
ORDER_LABELS = {
    "created": "待模拟交付",
    "delivered": "已模拟交付",
}


def _value(source: object, name: str, default: object = "") -> object:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _display_time(value: object) -> str:
    return format_beijing(value, empty="未安排")


def _iso_value(editor: QDateTimeEdit) -> str:
    return datetime_iso(editor.dateTime())


def _display_money(value: object) -> str:
    try:
        return f"¥{int(value or 0) / 100:.2f}"
    except (TypeError, ValueError):
        return "¥0.00"


def _error_text(action: str, error: Exception) -> str:
    return f"{action}失败：{error}\n请检查填写内容后重试；如果仍然失败，请先导出数据备份。"


def _dialog_buttons(dialog: QDialog, validate: Any) -> QDialogButtonBox:
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
    )
    buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
    buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
    buttons.accepted.connect(validate)
    buttons.rejected.connect(dialog.reject)
    return buttons


class CreateAdvisorDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("新增家庭生活顾问")
        self.setMinimumWidth(560)
        form = QFormLayout(self)
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("用于顾问登录，例如 wang-advisor")
        form.addRow("登录账号", self.username_input)
        self.password_input = QLineEdit()
        self.password_input.setProperty("sensitive_input", True)
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setPlaceholderText("至少 8 个字符")
        form.addRow("初始密码", self.password_input)
        self.name_input = QLineEdit()
        form.addRow("昵称", self.name_input)
        self.valid_until_input = BeijingDateTimeEdit(beijing_qdatetime().addYears(1))
        form.addRow(f"有效期限（{display_timezone_label()}）", self.valid_until_input)
        form.addRow(_dialog_buttons(self, self._validate))

    def _validate(self) -> None:
        if not self.username_input.text().strip() or not self.name_input.text().strip():
            QMessageBox.warning(self, "信息未填写", "请填写登录账号和顾问姓名。")
            return
        password = self.password_input.text()
        if len(password) < 8:
            QMessageBox.warning(self, "密码过短", "初始密码至少需要 8 个字符。")
            return
        if self.valid_until_input.dateTime() <= beijing_qdatetime():
            QMessageBox.warning(self, "期限无效", "有效期限必须晚于现在。")
            return
        self.accept()

    @property
    def values(self) -> dict[str, str]:
        return {
            "username": self.username_input.text().strip(),
            "password": self.password_input.text(),
            "display_name": self.name_input.text().strip(),
            "valid_until": _iso_value(self.valid_until_input),
        }


class MembershipDialog(QDialog):
    def __init__(
        self,
        member_name: str,
        membership: Mapping[str, Any] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._membership = dict(membership) if membership else None
        self.membership_id = (
            int(membership["membership_id"])
            if membership and membership.get("membership_id") is not None
            else None
        )
        self.setWindowTitle(f"设置会员方案｜{member_name}")
        self.setMinimumWidth(540)
        form = QFormLayout(self)
        self.plan_input = QLineEdit("家庭基础会员")
        form.addRow("方案名称", self.plan_input)
        self.status_combo = QComboBox()
        for code, label in MEMBERSHIP_LABELS.items():
            self.status_combo.addItem(label, code)
        form.addRow("会员状态", self.status_combo)
        self.start_input = BeijingDateTimeEdit()
        form.addRow(f"开始时间（{display_timezone_label()}）", self.start_input)
        self.end_input = BeijingDateTimeEdit(beijing_qdatetime().addYears(1))
        form.addRow(f"到期时间（{display_timezone_label()}）", self.end_input)
        self.visit_total_input = QSpinBox()
        self.visit_total_input.setRange(0, 100)
        self.visit_total_input.setValue(4)
        form.addRow("本周期上门总次数", self.visit_total_input)
        self.filing_input = QCheckBox("首次建档已经完成")
        form.addRow("首次建档", self.filing_input)
        self.gift_input = QLineEdit()
        self.gift_input.setPlaceholderText("例如：米面油礼品（未领取）")
        form.addRow("会员礼品", self.gift_input)
        form.addRow(_dialog_buttons(self, self._validate))
        if membership and self.membership_id is not None:
            self._load_membership(membership)

    def _load_membership(self, membership: Mapping[str, Any]) -> None:
        self.plan_input.setText(str(membership.get("plan_code") or "家庭基础会员"))
        status_index = self.status_combo.findData(str(membership.get("membership_status")))
        if status_index >= 0:
            self.status_combo.setCurrentIndex(status_index)
        for editor, key in (
            (self.start_input, "membership_starts_at"),
            (self.end_input, "membership_ends_at"),
        ):
            value = membership.get(key)
            if value:
                try:
                    set_editor_datetime(editor, value)
                except (TypeError, ValueError, OverflowError):
                    continue
        benefits = membership.get("membership_benefits")
        if not isinstance(benefits, Mapping):
            return
        try:
            self.visit_total_input.setValue(int(benefits.get("visit_total", 4)))
        except (TypeError, ValueError):
            self.visit_total_input.setValue(4)
        self.filing_input.setChecked(bool(benefits.get("first_filing", False)))
        self.gift_input.setText(str(benefits.get("gift") or ""))

    def _validate(self) -> None:
        if not self.plan_input.text().strip():
            QMessageBox.warning(self, "方案未填写", "请填写会员方案名称。")
            return
        if self.end_input.dateTime() <= self.start_input.dateTime():
            QMessageBox.warning(self, "日期无效", "到期时间必须晚于开始时间。")
            return
        self.accept()

    @property
    def values(self) -> dict[str, object]:
        return {
            "plan_code": self.plan_input.text().strip(),
            "status": str(self.status_combo.currentData()),
            "starts_at": _iso_value(self.start_input),
            "ends_at": _iso_value(self.end_input),
            "benefits": {
                "visit_total": self.visit_total_input.value(),
                "visit_used": self._existing_visit_used(),
                "first_filing": self.filing_input.isChecked(),
                "gift": self.gift_input.text().strip(),
            },
            "membership_id": self.membership_id,
        }

    def _existing_visit_used(self) -> int:
        membership = self._membership
        benefits = (
            membership.get("membership_benefits") if isinstance(membership, Mapping) else None
        )
        if not isinstance(benefits, Mapping):
            return 0
        try:
            return max(0, int(benefits.get("visit_used", 0)))
        except (TypeError, ValueError):
            return 0


class AdvisorProfileDialog(QDialog):
    def __init__(self, advisor: Mapping[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑顾问信息")
        self.setMinimumWidth(540)
        form = QFormLayout(self)
        account = QLabel(str(advisor.get("username") or ""))
        form.addRow("登录账号", account)
        self.name_input = QLineEdit(str(advisor.get("display_name") or ""))
        form.addRow("顾问姓名", self.name_input)
        self.organization_input = QLineEdit(str(advisor.get("organization") or ""))
        form.addRow("所属机构", self.organization_input)
        self.specialty_input = QLineEdit(str(advisor.get("specialty") or ""))
        form.addRow("擅长服务", self.specialty_input)
        self.bio_input = QTextEdit()
        self.bio_input.setPlainText(str(advisor.get("bio") or ""))
        self.bio_input.setMaximumHeight(130)
        form.addRow("顾问简介", self.bio_input)
        self.valid_until_input = BeijingDateTimeEdit(beijing_qdatetime().addYears(1))
        if advisor.get("valid_until"):
            set_editor_datetime(self.valid_until_input, advisor["valid_until"])
        form.addRow("账号有效期限", self.valid_until_input)
        form.addRow(_dialog_buttons(self, self._validate))

    def _validate(self) -> None:
        if not self.name_input.text().strip():
            QMessageBox.warning(self, "姓名未填写", "请填写顾问姓名。")
            return
        self.accept()

    @property
    def values(self) -> dict[str, str]:
        return {
            "display_name": self.name_input.text().strip(),
            "organization": self.organization_input.text().strip(),
            "specialty": self.specialty_input.text().strip(),
            "bio": self.bio_input.toPlainText().strip(),
        }


class BindAdvisorDialog(QDialog):
    def __init__(
        self, member_name: str, advisors: list[dict[str, Any]], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"绑定家庭生活顾问｜{member_name}")
        self.setMinimumWidth(520)
        form = QFormLayout(self)
        self.advisor_combo = QComboBox()
        for advisor in advisors:
            name = str(advisor.get("display_name") or advisor.get("username") or "顾问")
            status = ACCOUNT_LABELS.get(str(advisor.get("account_status")), "状态未知")
            self.advisor_combo.addItem(f"{name}｜{status}", int(advisor["id"]))
        form.addRow("选择顾问", self.advisor_combo)
        self.start_input = BeijingDateTimeEdit()
        form.addRow(f"服务开始时间（{display_timezone_label()}）", self.start_input)
        note = QLabel("换绑时，旧绑定会结束，但历史上门记录仍会保留。")
        note.setWordWrap(True)
        form.addRow(note)
        form.addRow(_dialog_buttons(self, self._validate))

    def _validate(self) -> None:
        if self.advisor_combo.currentData() is None:
            QMessageBox.warning(self, "暂无可用顾问", "请先新增并启用一位顾问。")
            return
        self.accept()

    @property
    def values(self) -> dict[str, object]:
        return {
            "advisor_user_id": int(self.advisor_combo.currentData()),
            "started_at": _iso_value(self.start_input),
        }


class VisitTaskDialog(QDialog):
    def __init__(self, member_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"创建上门任务｜{member_name}")
        self.setMinimumWidth(540)
        form = QFormLayout(self)
        self.title_input = QLineEdit("首次上门建档")
        form.addRow("任务名称", self.title_input)
        self.time_input = BeijingDateTimeEdit(beijing_qdatetime().addDays(1))
        form.addRow(f"计划上门时间（{display_timezone_label()}）", self.time_input)
        self.notes_input = QTextEdit()
        self.notes_input.setMaximumHeight(120)
        self.notes_input.setPlaceholderText("填写需要顾问提前了解的事项")
        form.addRow("任务备注", self.notes_input)
        form.addRow(_dialog_buttons(self, self._validate))

    def _validate(self) -> None:
        if not self.title_input.text().strip():
            QMessageBox.warning(self, "名称未填写", "请填写上门任务名称。")
            return
        if self.time_input.dateTime() <= beijing_qdatetime():
            QMessageBox.warning(self, "时间无效", "新建上门安排必须晚于当前时间。")
            return
        self.accept()

    @property
    def values(self) -> dict[str, object]:
        return {
            "title": self.title_input.text().strip(),
            "scheduled_at": _iso_value(self.time_input),
            "notes": self.notes_input.toPlainText().strip(),
        }


class ProductDialog(QDialog):
    """Create or edit a non-medical lifestyle product."""

    def __init__(
        self,
        product: Mapping[str, Any] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑商品" if product else "新增商品")
        self.setMinimumWidth(580)
        form = QFormLayout(self)
        self.sku_input = QLineEdit(str(product.get("sku") or "") if product else "")
        self.sku_input.setPlaceholderText("唯一编号，例如 CUP-001（字母、数字、点、横线、下划线）")
        form.addRow("商品 SKU *", self.sku_input)
        self.name_input = QLineEdit(str(product.get("name") or "") if product else "")
        self.name_input.setPlaceholderText("例如：家用保温杯")
        form.addRow("商品名称 *", self.name_input)
        self.brand_input = QLineEdit(str(product.get("brand") or "") if product else "")
        self.brand_input.setPlaceholderText("选填")
        form.addRow("品牌", self.brand_input)
        self.category_input = QComboBox()
        for code, label in PRODUCT_CATEGORY_LABELS.items():
            self.category_input.addItem(label, code)
        category = str(product.get("category") or "other") if product else "other"
        category_index = self.category_input.findData(category)
        if category_index < 0:
            self.category_input.addItem(category, category)
            category_index = self.category_input.count() - 1
        self.category_input.setCurrentIndex(max(0, category_index))
        form.addRow("商品分类", self.category_input)
        self.description_input = QTextEdit()
        self.description_input.setMaximumHeight(130)
        self.description_input.setPlaceholderText("只描述生活用途，不填写医疗功效或治疗承诺")
        self.description_input.setPlainText(
            str(product.get("description") or "") if product else ""
        )
        form.addRow("生活用途说明 *", self.description_input)
        self.specification_input = QLineEdit(
            str(product.get("specification") or "") if product else ""
        )
        self.specification_input.setPlaceholderText("例如：500 毫升")
        form.addRow("规格", self.specification_input)
        self.price_input = QDoubleSpinBox()
        self.price_input.setRange(0, 999_999.99)
        self.price_input.setDecimals(2)
        self.price_input.setPrefix("¥ ")
        self.price_input.setValue(float(product.get("price_cents") or 0) / 100 if product else 0)
        form.addRow("展示价格", self.price_input)
        self.unit_input = QLineEdit(str(product.get("unit") or "件") if product else "件")
        self.unit_input.setPlaceholderText("例如：件、盒、袋")
        form.addRow("计量单位", self.unit_input)
        self.source_type_input = QComboBox()
        self.source_type_input.addItem("平台自营", "self_operated")
        self.source_type_input.addItem("第三方商品", "third_party")
        source_type = (
            str(product.get("source_type") or "self_operated") if product else "self_operated"
        )
        source_index = self.source_type_input.findData(source_type)
        self.source_type_input.setCurrentIndex(max(0, source_index))
        form.addRow("商品来源", self.source_type_input)
        self.active_input = QCheckBox("保存后立即上架，顾问和会员可见")
        self.active_input.setChecked(bool(product.get("is_active", True)) if product else True)
        form.addRow("商品状态", self.active_input)
        warning = QLabel("商品仅用于生活服务展示；本版本订单为本地模拟，不接入付款与物流。")
        warning.setWordWrap(True)
        form.addRow(warning)
        form.addRow(_dialog_buttons(self, self._validate))

    def _validate(self) -> None:
        if not self.sku_input.text().strip():
            QMessageBox.warning(self, "SKU 未填写", "请填写唯一的商品 SKU。")
            return
        if not self.name_input.text().strip():
            QMessageBox.warning(self, "名称未填写", "请填写商品名称。")
            return
        if not self.description_input.toPlainText().strip():
            QMessageBox.warning(self, "说明未填写", "请填写商品的生活用途说明。")
            return
        if not self.unit_input.text().strip():
            QMessageBox.warning(self, "单位未填写", "请填写商品的计量单位。")
            return
        self.accept()

    @property
    def values(self) -> dict[str, object]:
        return {
            "sku": self.sku_input.text().strip().upper(),
            "name": self.name_input.text().strip(),
            "category": str(self.category_input.currentData()),
            "brand": self.brand_input.text().strip() or None,
            "specification": self.specification_input.text().strip() or None,
            "description": self.description_input.toPlainText().strip(),
            "price_cents": int(round(self.price_input.value() * 100)),
            "unit": self.unit_input.text().strip(),
            "source_type": str(self.source_type_input.currentData()),
            "is_active": self.active_input.isChecked(),
        }


class OperatorWorkspace(QWidget):
    """Local operator console for account, membership, binding and visit setup."""

    logout_requested = Signal()
    export_requested = Signal()
    import_requested = Signal()

    def __init__(
        self,
        service_management: object,
        backup_service: object | None = None,
        commerce_service: object | None = None,
        ai_assistant_service: object | None = None,
    ) -> None:
        super().__init__()
        self.setObjectName("OperatorWorkspace")
        self.service = service_management
        self.backup_service = backup_service
        self.commerce = commerce_service
        self.ai_assistant_service = ai_assistant_service
        self.current_user: Any | None = None
        self.actor_user_id: int | None = None
        self.members: list[dict[str, Any]] = []
        self.advisors: list[dict[str, Any]] = []
        self.products: list[dict[str, Any]] = []
        self.orders: list[dict[str, Any]] = []
        self.metric_labels: dict[str, QLabel] = {}
        self.tasks: list[dict[str, Any]] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)
        header = QFrame()
        header.setObjectName("StaffHeader")
        header_layout = QHBoxLayout(header)
        title = QLabel("运营管理台")
        title.setObjectName("StaffTitle")
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
        timezone_hint.setObjectName("BeijingTimeHint")
        root.addWidget(timezone_hint)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        root.addWidget(self.status_label)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._build_members_tab(), "会员服务")
        self.tabs.addTab(self._build_advisors_tab(), "顾问管理与上门服务")
        self.tabs.addTab(self._build_products_tab(), "商品管理")
        self.tabs.addTab(self._build_orders_tab(), "模拟订单")
        self.performance_panel = OperatorPerformancePanel(self.service)
        self.tabs.addTab(self.performance_panel, "工作统计")
        self.ai_config_panel = None
        for button in (
            self.create_product_button,
            self.edit_product_button,
            self.toggle_product_button,
            self.deliver_order_button,
        ):
            button.setEnabled(self.commerce is not None)
        self.tabs.currentChanged.connect(lambda _index: self.refresh())
        root.addWidget(self.tabs, 1)
        self.apply_preferences({})

    def apply_preferences(self, preferences: dict) -> None:
        self.setStyleSheet(build_staff_style(preferences))

    def _build_overview_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("这里汇总本机数据库中的会员、顾问和上门服务状态。"))
        row = QHBoxLayout()
        self.metric_labels: dict[str, QLabel] = {}
        for code, label in (
            ("member_count", "会员账户"),
            ("advisor_count", "顾问账户"),
            ("active_membership_count", "有效会员"),
            ("pending_visit_count", "待办上门"),
            ("completed_visit_count", "已完成上门"),
        ):
            frame = QFrame()
            frame.setObjectName("StaffCard")
            card = QVBoxLayout(frame)
            value_label = QLabel("0")
            value_label.setObjectName("MetricValue")
            value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            card.addWidget(value_label)
            name_label = QLabel(label)
            name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            card.addWidget(name_label)
            self.metric_labels[code] = value_label
            row.addWidget(frame)
        layout.addLayout(row)
        guide = QLabel(
            "推荐操作顺序：先创建顾问账号，再选择会员设置会员方案、绑定顾问，最后创建首次上门任务。"
        )
        guide.setWordWrap(True)
        layout.addWidget(guide)
        refresh_button = QPushButton("刷新全部数据")
        refresh_button.clicked.connect(self.refresh)
        layout.addWidget(refresh_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        return page

    def _metric_row(self, entries: tuple[tuple[str, str], ...]) -> QHBoxLayout:
        row = QHBoxLayout()
        for code, title in entries:
            frame = QFrame()
            frame.setObjectName("StaffCard")
            card = QHBoxLayout(frame)
            card.addWidget(QLabel(title))
            label = QLabel("0")
            label.setObjectName("MetricValue")
            card.addWidget(label)
            self.metric_labels[code] = label
            row.addWidget(frame)
        return row

    def _build_members_tab(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addLayout(
            self._metric_row(
                (("member_count", "会员账户"), ("active_membership_count", "有效会员"))
            )
        )
        split = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(split, 1)
        left_widget = QWidget()
        left = QVBoxLayout()
        left_widget.setLayout(left)
        actions = QHBoxLayout()
        actions.addWidget(QLabel("会员列表"))
        self.view_members_button = QPushButton("查看资料表")
        self.view_members_button.setCheckable(True)
        self.view_members_button.toggled.connect(self._toggle_member_table)
        actions.addWidget(self.view_members_button)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        actions.addWidget(refresh)
        left.addLayout(actions)
        self.member_list = QListWidget()
        self.member_list.currentItemChanged.connect(self._show_member)
        self.member_table = QTableWidget(0, 7)
        self.member_table.setHorizontalHeaderLabels(
            ["昵称", "真实姓名", "账号", "密码", "会员状态", "顾问绑定", "联系电话"]
        )
        self.member_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.member_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.member_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.member_table.setAlternatingRowColors(True)
        self.member_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.member_table.itemSelectionChanged.connect(self._select_table_member)
        self.member_views = QStackedWidget()
        self.member_views.addWidget(self.member_list)
        self.member_views.addWidget(self.member_table)
        left.addWidget(self.member_views, 1)
        split.addWidget(left_widget)
        right_widget = QWidget()
        right = QVBoxLayout()
        right_widget.setLayout(right)
        right.addWidget(QLabel("会员服务详情"))
        self.member_detail = QLabel("请从左侧选择会员。")
        self.member_detail.setWordWrap(True)
        self.member_detail.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.member_detail.setTextFormat(Qt.TextFormat.PlainText)
        self.member_detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detail_scroll = QScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setWidget(self.member_detail)
        right.addWidget(detail_scroll, 1)
        member_actions = QGridLayout()
        self.membership_button = QPushButton("设置会员方案")
        self.membership_button.clicked.connect(self._set_membership)
        member_actions.addWidget(self.membership_button, 0, 0)
        self.bind_button = QPushButton("绑定 / 更换顾问")
        self.bind_button.clicked.connect(self._bind_advisor)
        member_actions.addWidget(self.bind_button, 0, 1)
        self.task_button = QPushButton("创建上门任务")
        self.task_button.clicked.connect(self._create_visit_task)
        member_actions.addWidget(self.task_button, 1, 0)
        self.member_account_button = QPushButton("停用 / 启用账号")
        self.member_account_button.clicked.connect(self._toggle_member_account)
        member_actions.addWidget(self.member_account_button, 1, 1)
        reset_button = QPushButton("重置密码")
        reset_button.clicked.connect(lambda: self._reset_password(self.member_list))
        member_actions.addWidget(reset_button, 2, 0, 1, 2)
        right.addLayout(member_actions)
        split.addWidget(right_widget)
        split.setSizes([580, 470])
        return page

    def _build_advisors_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addLayout(
            self._metric_row(
                (
                    ("advisor_count", "顾问账户"),
                    ("pending_visit_count", "待办上门"),
                    ("completed_visit_count", "已完成上门"),
                )
            )
        )
        split = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(split, 1)
        advisor_page = QWidget()
        advisor_box = QVBoxLayout(advisor_page)
        action_row = QGridLayout()
        create_button = QPushButton("新增顾问")
        create_button.clicked.connect(self._create_advisor)
        action_row.addWidget(create_button, 0, 0)
        edit_button = QPushButton("编辑顾问")
        edit_button.clicked.connect(self._edit_advisor)
        action_row.addWidget(edit_button, 0, 1)
        account_button = QPushButton("停用 / 启用账号")
        account_button.clicked.connect(self._toggle_advisor_account)
        action_row.addWidget(account_button, 1, 0)
        reset_button = QPushButton("重置密码")
        reset_button.clicked.connect(lambda: self._reset_password(self.advisor_list))
        action_row.addWidget(reset_button, 1, 1)
        advisor_box.addLayout(action_row)
        self.advisor_list = QListWidget()
        self.advisor_list.itemDoubleClicked.connect(lambda _item: self._edit_advisor())
        advisor_box.addWidget(self.advisor_list, 1)
        split.addWidget(advisor_page)
        split.addWidget(self._build_visits_tab())
        split.setSizes([430, 680])
        return page

    def _build_visits_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        tasks_box = QVBoxLayout()
        task_header = QHBoxLayout()
        task_header.addWidget(QLabel("上门预约（双击编辑）"))
        self.task_status_filter = QComboBox()
        self.task_status_filter.addItem("全部状态", None)
        for code in ("pending", "in_progress", "completed", "incomplete", "disabled"):
            self.task_status_filter.addItem(TASK_LABELS[code], code)
        self.task_status_filter.currentIndexChanged.connect(
            lambda _index: self._fill_tasks(self.tasks)
        )
        task_header.addWidget(self.task_status_filter)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        task_header.addWidget(refresh)
        tasks_box.addLayout(task_header)
        self.task_list = QListWidget()
        self.task_list.itemDoubleClicked.connect(self._edit_task)
        tasks_box.addWidget(self.task_list, 1)
        layout.addLayout(tasks_box, 1)
        records_box = QVBoxLayout()
        records_box.addWidget(QLabel("已完成的上门记录（双击可查看详情）"))
        self.visit_record_list = QListWidget()
        self.visit_record_list.itemDoubleClicked.connect(self._show_visit_record)
        records_box.addWidget(self.visit_record_list, 1)
        layout.addLayout(records_box, 1)
        return page

    def _build_products_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        hint = QLabel("维护可供顾问推荐的生活用品。商品说明不得包含诊断、治疗或疗效承诺。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        actions = QHBoxLayout()
        self.create_product_button = QPushButton("新增商品")
        self.create_product_button.clicked.connect(self._create_product)
        actions.addWidget(self.create_product_button)
        self.edit_product_button = QPushButton("编辑选中商品")
        self.edit_product_button.clicked.connect(self._edit_product)
        actions.addWidget(self.edit_product_button)
        self.toggle_product_button = QPushButton("上架 / 下架选中商品")
        self.toggle_product_button.clicked.connect(self._toggle_product)
        actions.addWidget(self.toggle_product_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.product_list = QListWidget()
        self.product_list.itemDoubleClicked.connect(lambda _item: self._edit_product())
        layout.addWidget(self.product_list, 1)
        return page

    def _build_orders_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        hint = QLabel("这里记录会员在本机创建的模拟订单；没有真实支付、扣款、发货或物流。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.order_list = QListWidget()
        self.order_list.currentItemChanged.connect(self._order_selection_changed)
        layout.addWidget(self.order_list, 1)
        actions = QHBoxLayout()
        self.deliver_order_button = QPushButton("标记选中订单为已模拟交付")
        self.deliver_order_button.clicked.connect(self._mark_order_delivered)
        actions.addWidget(self.deliver_order_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self._order_selection_changed(None, None)
        return page

    def start_session(self, user: Any) -> None:
        self.current_user = user
        self.actor_user_id = int(user.id)
        self.user_label.setText(f"当前运营人员：{getattr(user, 'username', '')}")
        self.performance_panel.set_actor(self.actor_user_id)
        if self.ai_config_panel is not None:
            self.ai_config_panel.set_operator(self.actor_user_id)
        self.refresh()

    def end_session(self) -> None:
        self.current_user = None
        self.actor_user_id = None
        self.user_label.setText("未登录")
        self.performance_panel.set_actor(None)
        if self.ai_config_panel is not None:
            self.ai_config_panel.set_operator(None)
        for widget in (
            self.member_list,
            self.advisor_list,
            self.task_list,
            self.visit_record_list,
            self.product_list,
            self.order_list,
        ):
            widget.clear()
        self.member_detail.setText("请从左侧选择会员。")
        self.member_table.setRowCount(0)
        self.members = []
        self.advisors = []
        self.tasks = []

    def refresh(self) -> None:
        if self.actor_user_id is None:
            return
        try:
            dashboard = self.service.get_dashboard(self.actor_user_id)
            for key, label in self.metric_labels.items():
                label.setText(str(dashboard.get(key, 0)))
            self.members = list(self.service.list_members(self.actor_user_id))
            self.advisors = list(self.service.list_advisors(self.actor_user_id))
            tasks = list(self.service.list_visit_tasks(self.actor_user_id))
            self.tasks = tasks
            records = list(self.service.list_visit_records(self.actor_user_id))
            self._fill_members()
            self._fill_advisors()
            self._fill_tasks(tasks)
            self._fill_records(records)
            self.performance_panel.refresh()
            if self._refresh_commerce():
                self.status_label.hide()
        except Exception as exc:
            self._set_status(_error_text("读取运营数据", exc), error=True)

    def _refresh_commerce(self) -> bool:
        if self.actor_user_id is None:
            return False
        if self.commerce is None:
            self.products = []
            self.orders = []
            self._fill_products()
            self._fill_orders()
            return True
        try:
            self.products = list(
                self.commerce.list_products(self.actor_user_id, include_inactive=True)
            )
            self.orders = list(self.commerce.list_orders(self.actor_user_id))
            self._fill_products()
            self._fill_orders()
            return True
        except Exception as exc:
            self._set_status(_error_text("读取商品和模拟订单", exc), error=True)
            return False

    def refresh_snapshot(self, resources):
        """Periodic read-only refresh: no details, commerce, dialogs or RPC."""
        if self.actor_user_id is None:
            return ()
        from .snapshot_refresh import preserve_views

        tab = self.tabs.currentIndex()
        if tab == 0:
            self.members = list(resources["members"])
            with preserve_views(self.member_list, self.member_table):
                self._fill_members()
            if self.member_list.currentRow() < 0:
                self.member_detail.setText("请从左侧选择会员。")
            self._set_status(
                "会员列表已同步；右侧完整档案保留上次在线读取结果，需主动重新选择会员读取。",
                error=False,
            )
            return ("运营会员列表",)
        if tab == 1:
            self.advisors = list(resources["advisors"])
            self.tasks = list(resources["appointments"])
            with preserve_views(self.advisor_list, self.task_list):
                self._fill_advisors()
                self._fill_tasks(self.tasks)
            self._set_status(
                "顾问和任务列表已同步；历史上门明细与概览数字需主动点击刷新在线读取。",
                error=False,
            )
            return ("顾问列表", "运营上门任务")
        if tab == 4:
            return self.performance_panel.refresh_snapshot(resources)
        return ()  # Products, virtual orders and AI configuration are not mirrored.

    def _fill_members(self) -> None:
        selected_id = self._selected_id(self.member_list)
        self.member_list.clear()
        self.member_table.blockSignals(True)
        self.member_table.setRowCount(len(self.members))
        selected_row = -1
        for index, member in enumerate(self.members):
            name = str(member.get("display_name") or member.get("username") or "会员")
            account = ACCOUNT_LABELS.get(str(member.get("account_status")), "状态未知")
            plan = str(member.get("plan_code") or "未设置会员")
            advisor = str(member.get("advisor_name") or "未绑定顾问")
            nickname = str(member.get("nickname") or name)
            membership = MEMBERSHIP_LABELS.get(str(member.get("membership_status")), "未设置会员")
            password = "已设置 ••••••" if member.get("password_set", True) else "未设置"
            item = QListWidgetItem(
                f"昵称：{nickname}｜真实姓名：{member.get('display_name') or '未填写'}\n"
                f"账号：{member.get('username', '')}｜密码：{password}\n"
                f"会员：{membership} · {plan}｜顾问：{advisor}｜{account}"
            )
            item.setData(Qt.ItemDataRole.UserRole, member)
            self.member_list.addItem(item)
            values = (
                nickname,
                member.get("display_name") or "未填写",
                member.get("username"),
                password,
                membership,
                advisor,
                member.get("phone") or "未填写",
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setData(Qt.ItemDataRole.UserRole, int(member["id"]))
                self.member_table.setItem(index, column, cell)
            if int(member["id"]) == selected_id:
                selected_row = index
        if self.members:
            self.member_list.setCurrentRow(max(0, selected_row))
            self.member_table.selectRow(max(0, selected_row))
        else:
            item = QListWidgetItem("暂无会员账户；会员可在登录页自行注册。")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.member_list.addItem(item)
        self.member_table.blockSignals(False)

    def _toggle_member_table(self, checked: bool) -> None:
        self.member_views.setCurrentIndex(1 if checked else 0)
        self.view_members_button.setText("返回会员列表" if checked else "查看资料表")

    def _select_table_member(self) -> None:
        row = self.member_table.currentRow()
        if row >= 0 and row < len(self.members):
            self.member_list.setCurrentRow(row)

    def _fill_advisors(self) -> None:
        selected_id = self._selected_id(self.advisor_list)
        self.advisor_list.clear()
        selected_row = -1
        for index, advisor in enumerate(self.advisors):
            name = str(advisor.get("display_name") or advisor.get("username") or "顾问")
            account = ACCOUNT_LABELS.get(str(advisor.get("account_status")), "状态未知")
            organization = str(advisor.get("organization") or "机构未填写")
            workload = int(advisor.get("active_member_count") or 0)
            validity = (
                _display_time(advisor["valid_until"])
                if advisor.get("valid_until")
                else "原账号期限未设置"
            )
            item = QListWidgetItem(
                f"昵称：{name}｜{account}\n"
                f"账号：{advisor.get('username', '')}｜密码：已设置 ••••••\n"
                f"期限：{validity}\n"
                f"{organization}｜当前负责 {workload} 位会员"
            )
            item.setData(Qt.ItemDataRole.UserRole, advisor)
            self.advisor_list.addItem(item)
            if int(advisor["id"]) == selected_id:
                selected_row = index
        if self.advisors:
            self.advisor_list.setCurrentRow(max(0, selected_row))
        else:
            item = QListWidgetItem("暂无顾问，请点击“新增家庭生活顾问”。")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.advisor_list.addItem(item)

    def _fill_tasks(self, tasks: list[dict[str, Any]]) -> None:
        self.task_list.clear()
        selected_status = self.task_status_filter.currentData()
        if selected_status:
            tasks = [
                task
                for task in tasks
                if ("disabled" if task.get("status") == "cancelled" else task.get("status"))
                == selected_status
            ]
        for task in tasks:
            status = TASK_LABELS.get(str(task.get("status")), "状态未知")
            item = QListWidgetItem(
                f"[{status}] {_display_time(task.get('scheduled_at'))}｜{task.get('title', '')}\n"
                f"会员：{task.get('member_name', '')}｜"
                f"顾问：{task.get('advisor_name') or '未分配'}\n"
                f"地址：{task.get('address') or '未填写'}｜备注：{task.get('notes') or '无'}"
            )
            item.setData(Qt.ItemDataRole.UserRole, task)
            self.task_list.addItem(item)
        if not tasks:
            item = QListWidgetItem("暂无上门任务。")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.task_list.addItem(item)

    def _fill_records(self, records: list[dict[str, Any]]) -> None:
        self.visit_record_list.clear()
        for record in records:
            item = QListWidgetItem(
                f"{_display_time(record.get('visited_at'))}｜{record.get('member_name', '')}｜"
                f"{record.get('advisor_name') or '顾问未填写'}\n{record.get('summary', '')}"
            )
            item.setData(Qt.ItemDataRole.UserRole, record)
            self.visit_record_list.addItem(item)
        if not records:
            item = QListWidgetItem("暂无已完成的上门记录。")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.visit_record_list.addItem(item)

    def _fill_products(self) -> None:
        selected_id = self._selected_id(self.product_list)
        self.product_list.clear()
        selected_row = -1
        for index, product in enumerate(self.products):
            active = "已上架" if bool(product.get("is_active")) else "已下架"
            category = PRODUCT_CATEGORY_LABELS.get(
                str(product.get("category")), str(product.get("category") or "未分类")
            )
            item = QListWidgetItem(
                f"[{active}] {product.get('sku') or '无 SKU'}｜"
                f"{product.get('name') or '未命名商品'}｜{category}｜"
                f"{_display_money(product.get('price_cents'))}/{product.get('unit') or '件'}\n"
                f"{product.get('brand') or '品牌未填写'}｜"
                f"{product.get('specification') or '规格未填写'}｜"
                f"{product.get('description') or '暂无生活用途说明'}"
            )
            item.setData(Qt.ItemDataRole.UserRole, product)
            self.product_list.addItem(item)
            if int(product["id"]) == selected_id:
                selected_row = index
        if self.products:
            self.product_list.setCurrentRow(max(0, selected_row))
        else:
            text = (
                "商品功能未启用。"
                if self.commerce is None
                else "暂无商品，请点击“新增商品”开始维护。"
            )
            item = QListWidgetItem(text)
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.product_list.addItem(item)

    def _fill_orders(self) -> None:
        selected_id = self._selected_id(self.order_list)
        self.order_list.clear()
        selected_row = -1
        member_names = {
            int(member["id"]): str(member.get("display_name") or member.get("username") or "会员")
            for member in self.members
            if member.get("id") is not None
        }
        for index, order in enumerate(self.orders):
            status = ORDER_LABELS.get(str(order.get("status")), "状态未知")
            member_name = (
                order.get("member_name")
                or member_names.get(int(order.get("member_user_id") or -1))
                or order.get("member_username")
                or "会员未填写"
            )
            item = QListWidgetItem(
                f"[{status}] {_display_time(order.get('created_at'))}｜"
                f"{member_name}\n"
                f"{order.get('product_name') or order.get('product_name_snapshot') or '商品未填写'}"
                f" × {order.get('quantity') or 1}｜合计 "
                f"{_display_money(order.get('amount_cents') or order.get('total_amount_cents'))}"
            )
            item.setData(Qt.ItemDataRole.UserRole, order)
            self.order_list.addItem(item)
            if int(order["id"]) == selected_id:
                selected_row = index
        if self.orders:
            self.order_list.setCurrentRow(max(0, selected_row))
        else:
            text = "模拟订单功能未启用。" if self.commerce is None else "暂无模拟订单。"
            item = QListWidgetItem(text)
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.order_list.addItem(item)
            self._order_selection_changed(None, None)

    def _show_member(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        member = current.data(Qt.ItemDataRole.UserRole) if current else None
        if not isinstance(member, Mapping):
            self.member_detail.setText("请从左侧选择会员。")
            return
        account = ACCOUNT_LABELS.get(str(member.get("account_status")), "状态未知")
        membership = MEMBERSHIP_LABELS.get(str(member.get("membership_status")), "未设置会员")
        nickname = member.get("nickname") or member.get("display_name") or member.get("username")
        lines = [
            f"昵称：{nickname}",
            f"真实姓名：{member.get('display_name') or '未填写'}",
            f"登录账号：{member.get('username')}",
            "密码：已设置 ••••••（可使用下方入口重置）",
            f"账号状态：{account}",
            f"会员方案：{member.get('plan_code') or '未设置'}",
            f"会员状态：{membership}",
            f"到期时间（{display_timezone_label()}）：{_display_time(member.get('membership_ends_at'))}",
            f"家庭生活顾问：{member.get('advisor_name') or '未绑定'}",
            f"下次上门（{display_timezone_label()}）：{_display_time(member.get('next_visit_at'))}",
            f"联系电话：{member.get('phone') or '未填写'}",
            f"居住情况：{member.get('living_situation') or '未填写'}",
        ]
        method = getattr(self.service, "get_member_overview", None)
        if self.actor_user_id is not None and callable(method):
            try:
                overview = method(self.actor_user_id, int(member["id"]), recent_record_limit=20)
                profile = overview.get("profile", {})
                labels = (
                    ("出生日期", "birth_date"),
                    ("性别", "gender"),
                    ("紧急联系人", "emergency_contact_name"),
                    ("紧急联系电话", "emergency_contact_phone"),
                    ("称呼偏好", "ai_preferred_name"),
                    ("身高（厘米）", "height_cm"),
                    ("生活目标", "health_goals"),
                    ("饮食偏好", "dietary_preferences"),
                    ("会员自填备注", "medical_notes"),
                )
                lines.extend(f"{label}：{profile.get(key) or '未填写'}" for label, key in labels)
                lines.append("已确认生活事实")
                facts = overview.get("confirmed_facts", [])
                lines.extend(f"{fact.get('fact_key')}：{fact.get('value')}" for fact in facts)
                if not facts:
                    lines.append("暂无已确认事实")
                lines.append("近期生活记录")
                records = overview.get("recent_life_records", [])
                lines.extend(
                    f"{_display_time(record.get('occurred_at'))}｜{record.get('content')}"
                    for record in records
                )
                if not records:
                    lines.append("暂无近期记录")
            except Exception as exc:
                lines.append(f"完整档案暂时无法读取：{exc}")
        self.member_detail.setText("\n\n".join(lines))

    def _reset_password(self, widget: QListWidget) -> None:
        account = self._selected_data(widget)
        if self.actor_user_id is None or not account:
            return
        dialog = QInputDialog(self)
        dialog.setWindowTitle("重置账号密码")
        dialog.setLabelText(f"为 {account.get('username')} 输入新密码（至少 8 位）：")
        dialog.setTextEchoMode(QLineEdit.EchoMode.Password)
        for field in dialog.findChildren(QLineEdit):
            field.setProperty("sensitive_input", True)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        password = dialog.textValue()
        try:
            self.service.reset_account_password(self.actor_user_id, int(account["id"]), password)
            self._set_status("新密码已保存。请将新密码告知账号使用者。", error=False)
            self.refresh()
        except Exception as exc:
            QMessageBox.warning(self, "重置失败", str(exc))

    @staticmethod
    def _selected_data(widget: QListWidget) -> dict[str, Any] | None:
        item = widget.currentItem()
        data = item.data(Qt.ItemDataRole.UserRole) if item else None
        return dict(data) if isinstance(data, Mapping) else None

    @classmethod
    def _selected_id(cls, widget: QListWidget) -> int:
        data = cls._selected_data(widget)
        return int(data["id"]) if data and data.get("id") else -1

    def _required_member(self) -> dict[str, Any] | None:
        member = self._selected_data(self.member_list)
        if member is None:
            QMessageBox.information(self, "请先选择", "请先从会员列表中选择一位会员。")
        return member

    def _create_advisor(self) -> None:
        if self.actor_user_id is None:
            return
        dialog = CreateAdvisorDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.service.create_advisor(self.actor_user_id, **dialog.values)
            self.refresh()
            self._set_status("顾问账号已创建，可在设置的有效期限内登录。", error=False)
        except Exception as exc:
            QMessageBox.warning(self, "创建失败", _error_text("创建顾问", exc))

    def _set_membership(self) -> None:
        if self.actor_user_id is None or (member := self._required_member()) is None:
            return
        name = str(member.get("display_name") or member.get("username"))
        dialog = MembershipDialog(name, member, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.service.save_membership(self.actor_user_id, int(member["id"]), **dialog.values)
            self.refresh()
            self._set_status(f"已为 {name} 保存会员方案。", error=False)
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", _error_text("保存会员方案", exc))

    def _edit_advisor(self) -> None:
        if self.actor_user_id is None:
            return
        advisor = self._selected_data(self.advisor_list)
        if advisor is None:
            QMessageBox.information(self, "请先选择", "请先选择一位顾问。")
            return
        dialog = AdvisorProfileDialog(advisor, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.service.save_advisor_profile(
                self.actor_user_id, int(advisor["id"]), **dialog.values
            )
            self.service.set_advisor_validity(
                self.actor_user_id,
                int(advisor["id"]),
                valid_until=_iso_value(dialog.valid_until_input),
            )
            self.refresh()
            self._set_status("顾问信息已更新。", error=False)
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", _error_text("保存顾问信息", exc))

    def _bind_advisor(self) -> None:
        if self.actor_user_id is None or (member := self._required_member()) is None:
            return
        active_advisors = [
            advisor for advisor in self.advisors if advisor.get("account_status") == "active"
        ]
        name = str(member.get("display_name") or member.get("username"))
        dialog = BindAdvisorDialog(name, active_advisors, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.service.bind_advisor(self.actor_user_id, int(member["id"]), **dialog.values)
            self.refresh()
            self._set_status(f"已为 {name} 更新家庭生活顾问。", error=False)
        except Exception as exc:
            QMessageBox.warning(self, "绑定失败", _error_text("绑定顾问", exc))

    def _create_visit_task(self) -> None:
        if self.actor_user_id is None or (member := self._required_member()) is None:
            return
        if not member.get("advisor_user_id"):
            QMessageBox.information(self, "尚未绑定顾问", "请先为会员绑定顾问，再创建上门任务。")
            return
        name = str(member.get("display_name") or member.get("username"))
        dialog = VisitTaskDialog(name, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.service.create_visit_task(
                self.actor_user_id,
                int(member["id"]),
                advisor_user_id=int(member["advisor_user_id"]),
                **dialog.values,
            )
            self.refresh()
            self._set_status(f"已为 {name} 创建上门任务。", error=False)
        except Exception as exc:
            QMessageBox.warning(self, "创建失败", _error_text("创建上门任务", exc))

    def _create_product(self) -> None:
        if self.actor_user_id is None or self.commerce is None:
            return
        dialog = ProductDialog(parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.commerce.create_product(self.actor_user_id, **dialog.values)
            self._refresh_commerce()
            self._set_status("商品已保存；上架商品可供顾问推荐。", error=False)
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", _error_text("保存商品", exc))

    def _edit_product(self) -> None:
        if self.actor_user_id is None or self.commerce is None:
            return
        product = self._selected_data(self.product_list)
        if product is None:
            QMessageBox.information(self, "请先选择", "请先选择一个商品。")
            return
        dialog = ProductDialog(product, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.commerce.update_product(self.actor_user_id, int(product["id"]), **dialog.values)
            self._refresh_commerce()
            self._set_status("商品信息已更新。", error=False)
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", _error_text("更新商品", exc))

    def _toggle_product(self) -> None:
        if self.actor_user_id is None or self.commerce is None:
            return
        product = self._selected_data(self.product_list)
        if product is None:
            QMessageBox.information(self, "请先选择", "请先选择一个商品。")
            return
        is_active = not bool(product.get("is_active"))
        action = "上架" if is_active else "下架"
        try:
            self.commerce.set_product_active(self.actor_user_id, int(product["id"]), is_active)
            self._refresh_commerce()
            self._set_status(f"商品已{action}。", error=False)
        except Exception as exc:
            QMessageBox.warning(self, f"{action}失败", _error_text(f"{action}商品", exc))

    def _order_selection_changed(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        order = current.data(Qt.ItemDataRole.UserRole) if current else None
        status = str(order.get("status")) if isinstance(order, Mapping) else ""
        self.deliver_order_button.setEnabled(self.commerce is not None and status == "created")

    def _mark_order_delivered(self) -> None:
        if self.actor_user_id is None or self.commerce is None:
            return
        order = self._selected_data(self.order_list)
        if order is None:
            QMessageBox.information(self, "请先选择", "请先选择一条模拟订单。")
            return
        answer = QMessageBox.question(
            self,
            "确认模拟交付",
            "此操作只更新本机订单状态，不会发货，也不会产生物流信息。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.commerce.mark_order_delivered(self.actor_user_id, int(order["id"]))
            self._refresh_commerce()
            self._set_status("订单已标记为“已模拟交付”；没有发生真实物流。", error=False)
        except Exception as exc:
            QMessageBox.warning(self, "更新失败", _error_text("更新模拟订单", exc))

    def _toggle_member_account(self) -> None:
        self._toggle_account(self._selected_data(self.member_list), "会员")

    def _toggle_advisor_account(self) -> None:
        self._toggle_account(self._selected_data(self.advisor_list), "顾问")

    def _toggle_account(self, target: dict[str, Any] | None, role_label: str) -> None:
        if self.actor_user_id is None or target is None:
            QMessageBox.information(self, "请先选择", f"请先选择一位{role_label}。")
            return
        enable = target.get("account_status") != "active"
        action = "启用" if enable else "停用"
        name = str(target.get("display_name") or target.get("username"))
        answer = QMessageBox.question(
            self,
            f"确认{action}",
            f"是否{action} {name} 的登录账号？历史业务记录不会被删除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.service.set_account_enabled(self.actor_user_id, int(target["id"]), enabled=enable)
            self.refresh()
            self._set_status(f"账号已{action}。", error=False)
        except Exception as exc:
            QMessageBox.warning(self, f"{action}失败", _error_text(f"{action}账号", exc))

    def _edit_task(self, item: QListWidgetItem) -> None:
        task = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(task, Mapping) or self.actor_user_id is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"上门预约｜{task.get('member_name') or '会员'}")
        dialog.setMinimumWidth(580)
        form = QFormLayout(dialog)
        form.addRow("会员", QLabel(str(task.get("member_name") or "")))
        kind = QLineEdit(str(task.get("service_type") or task.get("title") or ""))
        form.addRow("服务类型", kind)
        scheduled = BeijingDateTimeEdit()
        if task.get("scheduled_at"):
            set_editor_datetime(scheduled, task["scheduled_at"])
        form.addRow(f"预约时间（{display_timezone_label()}）", scheduled)
        advisor = QComboBox()
        advisor.addItem("暂不分配", None)
        for entry in self.advisors:
            advisor.addItem(str(entry.get("display_name") or entry.get("username")), entry["id"])
        advisor.setCurrentIndex(max(0, advisor.findData(task.get("advisor_user_id"))))
        form.addRow("服务人员", advisor)
        form.addRow(QLabel("更换人员需先在会员服务中更换顾问绑定。"))
        state = QComboBox()
        for code in ("pending", "in_progress", "completed", "incomplete", "disabled"):
            state.addItem(TASK_LABELS[code], code)
        current = "disabled" if task.get("status") == "cancelled" else task.get("status")
        state.setCurrentIndex(max(0, state.findData(current)))
        form.addRow("服务状态", state)
        address = QLineEdit(str(task.get("address") or ""))
        form.addRow("上门地址", address)
        notes = QTextEdit(str(task.get("notes") or ""))
        notes.setMaximumHeight(130)
        form.addRow("预约备注", notes)
        form.addRow(QLabel("已完成状态由顾问提交工作记录后产生，保留完整服务审计。"))
        form.addRow(_dialog_buttons(dialog, dialog.accept))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.service.update_appointment(
                self.actor_user_id,
                int(task["id"]),
                service_type=kind.text(),
                scheduled_at=_iso_value(scheduled),
                advisor_id=advisor.currentData(),
                status=state.currentData(),
                notes=notes.toPlainText(),
                address=address.text(),
            )
            self.refresh()
            self._set_status("预约已更新，会员刷新服务页后即可看到最新安排。", error=False)
        except Exception as exc:
            QMessageBox.warning(self, "保存预约失败", str(exc))

    def _show_task_detail(self, item: QListWidgetItem) -> None:
        task = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(task, Mapping):
            return
        QMessageBox.information(
            self,
            "上门任务详情",
            "\n\n".join(
                (
                    f"任务：{task.get('title', '')}",
                    f"状态：{TASK_LABELS.get(str(task.get('status')), '未知')}",
                    f"会员：{task.get('member_name', '')}",
                    f"顾问：{task.get('advisor_name') or '未分配'}",
                    f"计划时间（{display_timezone_label()}）：{_display_time(task.get('scheduled_at'))}",
                    f"备注：{task.get('notes') or '无'}",
                )
            ),
        )

    def _show_visit_record(self, item: QListWidgetItem) -> None:
        record = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(record, Mapping):
            return
        details = record.get("details")
        lines = [
            f"会员：{record.get('member_name', '')}",
            f"顾问：{record.get('advisor_name') or '未填写'}",
            f"上门时间（{display_timezone_label()}）：{_display_time(record.get('visited_at'))}",
            f"总结：{record.get('summary', '')}",
        ]
        if isinstance(details, Mapping):
            labels = {
                "duration_minutes": "服务时长（分钟）",
                "inspected_areas": "本次查看内容",
                "user_questions": "用户提出的问题",
                "environment_changes": "家庭环境变化",
                "profile_changes": "档案修改说明",
                "follow_ups": "后续待办",
                "gifts": "礼品记录",
                "notes": "顾问备注",
            }
            for key, label in labels.items():
                if details.get(key) not in (None, "", [], {}):
                    lines.append(f"{label}：{details[key]}")
        QMessageBox.information(self, "上门记录详情", "\n\n".join(lines))

    def _set_status(self, text: str, *, error: bool) -> None:
        self.status_label.setObjectName(
            "WorkspaceStatusError" if error else "WorkspaceStatusSuccess"
        )
        self.status_label.setText(text)
        self.status_label.setVisible(True)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)


__all__ = [
    "AdvisorProfileDialog",
    "BindAdvisorDialog",
    "CreateAdvisorDialog",
    "MembershipDialog",
    "OperatorWorkspace",
    "ProductDialog",
    "VisitTaskDialog",
]
