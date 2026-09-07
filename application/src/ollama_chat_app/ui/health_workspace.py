from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..time_utils import as_beijing, beijing_today, format_beijing
from .files_reports_panel import FilesReportsPanel
from .health_records_panel import HealthRecordsPanel
from .time_fields import BeijingDateTimeEdit, OptionalDateEdit, beijing_qdatetime, datetime_iso

CATEGORIES = (
    ("diet", "饮食"),
    ("water", "饮水"),
    ("activity", "活动"),
    ("sleep", "睡眠"),
    ("environment", "居住环境"),
)
CATEGORY_LABELS = dict(CATEGORIES)


def _value(source: object, name: str, default: object = "") -> object:
    """Read the same field from a repository dataclass or a mapping."""
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _items(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return [value]
    if isinstance(value, Iterable):
        return list(value)
    return [value]


def _display_time(value: object, offset_minutes: object = None) -> str:
    return format_beijing(value, empty="时间未填写")


def _service_error(action: str, error: Exception) -> str:
    return f"{action}失败：{error}\n请检查填写内容后重试；如果仍然失败，请先导出数据备份。"


def _card(title: str, body: QLabel | QWidget) -> QFrame:
    frame = QFrame()
    frame.setObjectName("HealthCard")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(18, 16, 18, 16)
    layout.setSpacing(10)
    title_label = QLabel(title)
    title_label.setObjectName("SectionTitle")
    layout.addWidget(title_label)
    layout.addWidget(body)
    return frame


class RecordDialog(QDialog):
    def __init__(self, category: str = "diet", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("新增生活记录")
        self.setMinimumWidth(500)
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.category_combo = QComboBox()
        for code, label in CATEGORIES:
            self.category_combo.addItem(label, code)
        index = self.category_combo.findData(category)
        self.category_combo.setCurrentIndex(max(0, index))
        form.addRow("记录类别", self.category_combo)

        self.time_input = BeijingDateTimeEdit()
        form.addRow("发生时间（北京时间）", self.time_input)
        layout.addLayout(form)

        layout.addWidget(QLabel("记录内容（例如：午餐一碗米饭和蔬菜）"))
        self.content_input = QTextEdit()
        self.content_input.setPlaceholderText("用自己的话写下事实即可，不需要做健康判断。")
        self.content_input.setMinimumHeight(130)
        layout.addWidget(self.content_input)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存记录")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _validate(self) -> None:
        if not self.content_input.toPlainText().strip():
            QMessageBox.warning(self, "内容未填写", "请先填写记录内容，再点击保存。")
            return
        self.accept()

    @property
    def values(self) -> dict[str, str]:
        return {
            "category": str(self.category_combo.currentData()),
            "content": self.content_input.toPlainText().strip(),
            "occurred_at": datetime_iso(self.time_input.dateTime()),
        }


class ReminderDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("新增提醒")
        self.setMinimumWidth(480)
        layout = QFormLayout(self)
        self.title_input = QLineEdit()
        self.title_input.setPlaceholderText("例如：晚上 9 点记录睡眠准备情况")
        layout.addRow("提醒内容", self.title_input)
        self.time_input = BeijingDateTimeEdit(beijing_qdatetime().addSecs(3600))
        layout.addRow("提醒时间（北京时间）", self.time_input)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存提醒")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _validate(self) -> None:
        if not self.title_input.text().strip():
            QMessageBox.warning(self, "内容未填写", "请先填写提醒内容，再点击保存。")
            return
        self.accept()

    @property
    def values(self) -> dict[str, str]:
        return {
            "title": self.title_input.text().strip(),
            "scheduled_at": datetime_iso(self.time_input.dateTime()),
        }


class TodayPage(QWidget):
    record_changed = Signal()

    def __init__(self, health_service: object) -> None:
        super().__init__()
        self.health_service = health_service
        self.user_id: int | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 4, 8, 8)
        root.setSpacing(14)

        title = QLabel("今天，从一件小事开始")
        title.setObjectName("PageTitle")
        root.addWidget(title)
        subtitle = QLabel("这里只展示已记录的事实和提醒，不提供健康评分或诊断。")
        subtitle.setObjectName("HealthHint")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        self.status_label = QLabel()
        self.status_label.setObjectName("HealthError")
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        root.addWidget(self.status_label)

        quick_frame = QFrame()
        quick_frame.setObjectName("HealthCard")
        quick_layout = QVBoxLayout(quick_frame)
        quick_title = QLabel("快速记录")
        quick_title.setObjectName("SectionTitle")
        quick_layout.addWidget(quick_title)
        button_row = QHBoxLayout()
        for category, label in CATEGORIES:
            button = QPushButton(f"记录{label}")
            button.setProperty("category", category)
            button.clicked.connect(lambda _checked=False, code=category: self._add_record(code))
            button_row.addWidget(button)
        quick_layout.addLayout(button_row)
        root.addWidget(quick_frame)

        cards = QGridLayout()
        cards.setSpacing(14)
        self.facts_label = QLabel("尚未填写档案。")
        self.facts_label.setWordWrap(True)
        self.records_label = QLabel("今天还没有生活记录。")
        self.records_label.setWordWrap(True)
        self.reminders_label = QLabel("近期没有提醒。")
        self.reminders_label.setWordWrap(True)
        cards.addWidget(_card("个人事实", self.facts_label), 0, 0)
        cards.addWidget(_card("今日记录", self.records_label), 0, 1)
        cards.addWidget(_card("近期提醒", self.reminders_label), 1, 0, 1, 2)
        root.addLayout(cards)
        root.addStretch(1)

    def set_user(self, user_id: int | None) -> None:
        self.user_id = user_id
        self.refresh()

    def refresh(self) -> None:
        if self.user_id is None:
            return
        try:
            dashboard = self.health_service.get_dashboard(self.user_id) or {}
            facts = _items(_value(dashboard, "facts", _value(dashboard, "profile_facts", [])))
            records = _items(_value(dashboard, "today_records", _value(dashboard, "records", [])))
            reminders = _items(
                _value(dashboard, "reminders", _value(dashboard, "upcoming_reminders", []))
            )
            self.facts_label.setText(self._facts_text(facts))
            self.records_label.setText(self._records_text(records))
            self.reminders_label.setText(self._reminders_text(reminders))
            self.status_label.hide()
        except Exception as exc:
            self.status_label.setText(_service_error("读取今日信息", exc))
            self.status_label.show()

    def _facts_text(self, facts: list[object]) -> str:
        if not facts:
            return "尚未填写档案，可前往“档案”页面补充。"
        lines = []
        for fact in facts[:6]:
            if isinstance(fact, str):
                lines.append(f"• {fact}")
            else:
                label = _value(fact, "label", _value(fact, "name", "事实"))
                value = _value(fact, "value", _value(fact, "content", ""))
                lines.append(f"• {label}：{value}")
        return "\n".join(lines)

    def _records_text(self, records: list[object]) -> str:
        if not records:
            return "今天还没有生活记录。"
        lines = []
        for record in records[:8]:
            category = CATEGORY_LABELS.get(
                str(_value(record, "category", "")),
                str(_value(record, "category", "生活")),
            )
            content = _value(record, "content", "")
            time = _display_time(
                _value(record, "occurred_at", ""),
                _value(record, "timezone_offset_minutes", None),
            )
            lines.append(f"• {time}｜{category}｜{content}")
        return "\n".join(lines)

    def _reminders_text(self, reminders: list[object]) -> str:
        if not reminders:
            return "近期没有提醒，可前往“服务”页面添加。"
        return "\n".join(
            f"• {_display_time(_value(item, 'scheduled_at', ''))}｜{_value(item, 'title', '提醒')}"
            for item in reminders[:8]
        )

    def _add_record(self, category: str) -> None:
        if self.user_id is None:
            return
        dialog = RecordDialog(category, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values
        try:
            self.health_service.add_life_record(
                self.user_id,
                category=values["category"],
                content=values["content"],
                occurred_at=values["occurred_at"],
            )
            self.status_label.hide()
            self.refresh()
            self.record_changed.emit()
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", _service_error("保存生活记录", exc))


class RecordsPage(QWidget):
    record_changed = Signal()

    def __init__(self, health_service: object) -> None:
        super().__init__()
        self.health_service = health_service
        self.user_id: int | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 4, 8, 8)
        root.setSpacing(12)

        title_row = QHBoxLayout()
        title = QLabel("生活记录")
        title.setObjectName("PageTitle")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self.filter_combo = QComboBox()
        self.filter_combo.addItem("全部类别", None)
        for code, label in CATEGORIES:
            self.filter_combo.addItem(label, code)
        self.filter_combo.currentIndexChanged.connect(self.refresh)
        title_row.addWidget(QLabel("筛选"))
        title_row.addWidget(self.filter_combo)
        root.addLayout(title_row)

        hint = QLabel("记录客观发生的事情即可；这里不会自动判断健康状况。")
        hint.setObjectName("HealthHint")
        root.addWidget(hint)
        self.status_label = QLabel()
        self.status_label.setObjectName("HealthError")
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        root.addWidget(self.status_label)

        self.record_list = QListWidget()
        self.record_list.setAlternatingRowColors(True)
        root.addWidget(self.record_list, 1)

        actions = QHBoxLayout()
        self.add_button = QPushButton("新增记录")
        self.add_button.setObjectName("PrimaryButton")
        self.add_button.clicked.connect(self._add_record)
        actions.addWidget(self.add_button)
        self.delete_button = QPushButton("删除选中记录")
        self.delete_button.setObjectName("DangerButton")
        self.delete_button.clicked.connect(self._delete_selected)
        actions.addWidget(self.delete_button)
        actions.addStretch(1)
        root.addLayout(actions)

    def set_user(self, user_id: int | None) -> None:
        self.user_id = user_id
        self.refresh()

    def refresh(self, *_args: object) -> None:
        self.record_list.clear()
        if self.user_id is None:
            return
        try:
            records = self.health_service.list_life_records(
                self.user_id, category=self.filter_combo.currentData()
            )
            for record in _items(records):
                category_code = str(_value(record, "category", ""))
                category = CATEGORY_LABELS.get(category_code, category_code or "生活")
                time = _display_time(
                    _value(record, "occurred_at", ""),
                    _value(record, "timezone_offset_minutes", None),
                )
                content = str(_value(record, "content", ""))
                item = QListWidgetItem(f"{time}　{category}\n{content}")
                item.setData(Qt.ItemDataRole.UserRole, _value(record, "id", None))
                self.record_list.addItem(item)
            if self.record_list.count() == 0:
                empty = QListWidgetItem("当前筛选条件下还没有记录。")
                empty.setFlags(Qt.ItemFlag.NoItemFlags)
                self.record_list.addItem(empty)
            self.status_label.hide()
        except Exception as exc:
            self.status_label.setText(_service_error("读取生活记录", exc))
            self.status_label.show()

    def _add_record(self) -> None:
        if self.user_id is None:
            return
        category = str(self.filter_combo.currentData() or "diet")
        dialog = RecordDialog(category, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values
        try:
            self.health_service.add_life_record(
                self.user_id,
                category=values["category"],
                content=values["content"],
                occurred_at=values["occurred_at"],
            )
            self.refresh()
            self.record_changed.emit()
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", _service_error("保存生活记录", exc))

    def _delete_selected(self) -> None:
        if self.user_id is None:
            return
        item = self.record_list.currentItem()
        record_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        if record_id is None:
            QMessageBox.information(self, "请先选择", "请先在列表中选择要删除的记录。")
            return
        answer = QMessageBox.question(
            self,
            "确认删除",
            "这条生活记录删除后无法在应用内恢复。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.health_service.delete_life_record(self.user_id, int(record_id))
            self.refresh()
            self.record_changed.emit()
        except Exception as exc:
            QMessageBox.warning(self, "删除失败", _service_error("删除生活记录", exc))


class ProfilePage(QWidget):
    profile_changed = Signal()

    def __init__(self, health_service: object) -> None:
        super().__init__()
        self.health_service = health_service
        self.user_id: int | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 4, 8, 8)

        title = QLabel("个人档案")
        title.setObjectName("PageTitle")
        root.addWidget(title)
        hint = QLabel("档案用于在本机保存基本事实；AI 不会直接修改这些内容。")
        hint.setObjectName("HealthHint")
        hint.setWordWrap(True)
        root.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        form = QFormLayout(content)
        form.setContentsMargins(20, 18, 28, 18)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(14)

        self.full_name_input = QLineEdit()
        form.addRow("姓名", self.full_name_input)
        self.birth_date_input = OptionalDateEdit(past_only=True)
        form.addRow("出生日期", self.birth_date_input)
        self.gender_combo = QComboBox()
        self.gender_combo.addItem("未填写", "")
        self.gender_combo.addItem("女", "female")
        self.gender_combo.addItem("男", "male")
        self.gender_combo.addItem("其他 / 不便说明", "other")
        form.addRow("性别", self.gender_combo)
        self.phone_input = QLineEdit()
        form.addRow("电话", self.phone_input)
        self.living_situation_input = QLineEdit()
        self.living_situation_input.setPlaceholderText("例如：独居、与家人同住")
        form.addRow("居住情况", self.living_situation_input)
        self.emergency_name_input = QLineEdit()
        form.addRow("紧急联系人姓名", self.emergency_name_input)
        self.emergency_phone_input = QLineEdit()
        form.addRow("紧急联系人电话", self.emergency_phone_input)
        self.ai_preferred_name_input = QLineEdit()
        self.ai_preferred_name_input.setPlaceholderText("例如：王阿姨")
        form.addRow("希望 AI 如何称呼", self.ai_preferred_name_input)
        self.reminder_frequency_combo = QComboBox()
        self.reminder_frequency_combo.addItem("按每条提醒执行", "normal")
        self.reminder_frequency_combo.addItem("适当减少", "low")
        self.reminder_frequency_combo.addItem("暂不主动提醒", "off")
        form.addRow("提醒频率", self.reminder_frequency_combo)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        form.addRow("", self.status_label)
        self.save_button = QPushButton("保存档案")
        self.save_button.setObjectName("PrimaryButton")
        self.save_button.clicked.connect(self.save)
        form.addRow("", self.save_button)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

    def set_user(self, user_id: int | None) -> None:
        self.user_id = user_id
        self.refresh()

    def refresh(self) -> None:
        if self.user_id is None:
            return
        try:
            profile = self.health_service.get_profile(self.user_id) or {}
            self.full_name_input.setText(str(_value(profile, "display_name", "") or ""))
            birth_date = str(_value(profile, "birth_date", "") or "")[:10]
            self.birth_date_input.set_iso_value(birth_date)
            self._select(self.gender_combo, _value(profile, "gender", ""))
            self.phone_input.setText(str(_value(profile, "phone", "") or ""))
            self.living_situation_input.setText(str(_value(profile, "living_situation", "") or ""))
            self.emergency_name_input.setText(
                str(_value(profile, "emergency_contact_name", "") or "")
            )
            self.emergency_phone_input.setText(
                str(_value(profile, "emergency_contact_phone", "") or "")
            )
            self.ai_preferred_name_input.setText(
                str(_value(profile, "ai_preferred_name", "") or "")
            )
            self._select(
                self.reminder_frequency_combo,
                _value(profile, "reminder_frequency", "normal"),
            )
            self._set_status("档案已从本机载入。", False)
        except Exception as exc:
            self._set_status(_service_error("读取个人档案", exc), True)

    def save(self) -> None:
        if self.user_id is None:
            return
        birth_date = self.birth_date_input.iso_value() or ""
        values = {
            "display_name": self.full_name_input.text().strip(),
            "birth_date": birth_date,
            "gender": str(self.gender_combo.currentData()),
            "phone": self.phone_input.text().strip(),
            "living_situation": self.living_situation_input.text().strip(),
            "emergency_contact_name": self.emergency_name_input.text().strip(),
            "emergency_contact_phone": self.emergency_phone_input.text().strip(),
            "ai_preferred_name": self.ai_preferred_name_input.text().strip(),
            "reminder_frequency": str(self.reminder_frequency_combo.currentData()),
        }
        try:
            self.health_service.save_profile(self.user_id, values)
            self._set_status("档案已保存到本机。", False)
            self.profile_changed.emit()
        except Exception as exc:
            self._set_status(_service_error("保存个人档案", exc), True)

    def _set_status(self, text: str, error: bool) -> None:
        self.status_label.setObjectName("HealthError" if error else "HealthSuccess")
        self.status_label.setText(text)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    @staticmethod
    def _select(combo: QComboBox, value: object) -> None:
        index = combo.findData(str(value or ""))
        combo.setCurrentIndex(max(0, index))


class CommercePanel(QWidget):
    """Member-facing product recommendations and local simulated orders."""

    def __init__(self, commerce_service: object | None = None) -> None:
        super().__init__()
        self.commerce_service = commerce_service
        self.user_id: int | None = None
        self.selected_product: object | None = None
        self.selected_product_id: int | None = None
        self.selected_recommendation_id: int | None = None
        self._last_viewed_recommendation_id: int | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(12)

        self.notice_label = QLabel(
            "这里的商品和订单仅用于本地原型体验。点击“模拟购买”不会付款、扣款、发货，"
            "也不会向外部商家提交订单。"
        )
        self.notice_label.setObjectName("CommerceNotice")
        self.notice_label.setWordWrap(True)
        root.addWidget(self.notice_label)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        root.addWidget(self.status_label)

        selection_row = QHBoxLayout()
        selection_row.setSpacing(12)

        self.recommendation_list = QListWidget()
        self.recommendation_list.setMinimumHeight(190)
        self.recommendation_list.itemClicked.connect(self._select_item)
        self.recommendation_list.itemActivated.connect(self._select_item)
        selection_row.addWidget(_card("为我推荐", self.recommendation_list), 1)

        self.product_list = QListWidget()
        self.product_list.setMinimumHeight(190)
        self.product_list.itemClicked.connect(self._select_item)
        self.product_list.itemActivated.connect(self._select_item)
        selection_row.addWidget(_card("全部商品", self.product_list), 1)
        root.addLayout(selection_row)

        self.detail_label = QLabel("请从上方选择一件商品查看详情。")
        self.detail_label.setWordWrap(True)
        self.detail_label.setMinimumHeight(100)
        root.addWidget(_card("商品详情", self.detail_label))

        actions = QHBoxLayout()
        self.interested_button = QPushButton("我感兴趣")
        self.interested_button.setMinimumHeight(46)
        self.interested_button.clicked.connect(self._mark_interested)
        actions.addWidget(self.interested_button)
        actions.addStretch(1)
        actions.addWidget(QLabel("模拟数量"))
        self.quantity_input = QSpinBox()
        self.quantity_input.setRange(1, 99)
        self.quantity_input.setValue(1)
        self.quantity_input.setMinimumHeight(42)
        actions.addWidget(self.quantity_input)
        self.purchase_button = QPushButton("确认模拟购买（不付款）")
        self.purchase_button.setObjectName("PrimaryButton")
        self.purchase_button.setMinimumHeight(46)
        self.purchase_button.clicked.connect(self._simulate_purchase)
        actions.addWidget(self.purchase_button)
        root.addLayout(actions)

        order_header = QHBoxLayout()
        order_title = QLabel("我的模拟订单")
        order_title.setObjectName("SectionTitle")
        order_header.addWidget(order_title)
        order_header.addStretch(1)
        order_hint = QLabel("无真实支付 · 无真实物流")
        order_hint.setObjectName("HealthHint")
        order_header.addWidget(order_hint)
        root.addLayout(order_header)
        self.order_list = QListWidget()
        self.order_list.setMinimumHeight(130)
        self.order_list.itemSelectionChanged.connect(self._order_selection_changed)
        root.addWidget(self.order_list, 1)

        reorder_actions = QHBoxLayout()
        reorder_actions.addWidget(QLabel("再次模拟购买数量"))
        self.reorder_quantity_input = QSpinBox()
        self.reorder_quantity_input.setRange(1, 99)
        self.reorder_quantity_input.setValue(1)
        self.reorder_quantity_input.setMinimumHeight(42)
        reorder_actions.addWidget(self.reorder_quantity_input)
        self.reorder_button = QPushButton("再次模拟购买选中订单")
        self.reorder_button.setMinimumHeight(46)
        self.reorder_button.clicked.connect(self._reorder_selected)
        reorder_actions.addWidget(self.reorder_button)
        reorder_actions.addStretch(1)
        root.addLayout(reorder_actions)

        self._set_actions_enabled(False)
        self._set_order_actions_enabled(False)

    def set_user(self, user_id: int | None) -> None:
        self.user_id = user_id
        self.refresh()

    def refresh(self) -> None:
        self.recommendation_list.clear()
        self.product_list.clear()
        self.order_list.clear()
        self.selected_product = None
        self.selected_product_id = None
        self.selected_recommendation_id = None
        self._last_viewed_recommendation_id = None
        self.detail_label.setText("请从上方选择一件商品查看详情。")
        self.interested_button.setText("我感兴趣")
        self._set_actions_enabled(False)
        self._set_order_actions_enabled(False)

        if self.user_id is None:
            self.status_label.hide()
            return
        if self.commerce_service is None:
            self.status_label.setObjectName("HealthHint")
            self.status_label.setText("商品功能尚未接入本地数据服务，其他会员服务仍可正常使用。")
            self.status_label.show()
            self._add_empty_rows()
            return

        errors: list[str] = []
        try:
            self._refresh_recommendations()
        except Exception as exc:
            errors.append(_service_error("读取商品推荐", exc))

        try:
            products = _items(
                self.commerce_service.list_products(self.user_id, include_inactive=False)
            )
            for product in products:
                item = QListWidgetItem(
                    f"{self._product_name(product)}　{self._price_text(product)}\n"
                    f"{self._short_description(product)}"
                )
                item.setData(
                    Qt.ItemDataRole.UserRole,
                    {"product": product, "recommendation_id": None, "source": product},
                )
                self.product_list.addItem(item)
        except Exception as exc:
            errors.append(_service_error("读取商品列表", exc))

        try:
            self._refresh_orders()
        except Exception as exc:
            errors.append(_service_error("读取模拟订单", exc))

        self._add_empty_rows()
        if errors:
            self._set_status("\n".join(errors), error=True)
        else:
            self.status_label.hide()

    def _refresh_recommendations(self, selected_recommendation_id: int | None = None) -> None:
        self.recommendation_list.clear()
        recommendations = _items(self.commerce_service.list_recommendations(self.user_id))
        selected_item: QListWidgetItem | None = None
        for recommendation in recommendations:
            context = self._recommendation_context(recommendation)
            name = self._product_name(context["product"], recommendation)
            status = {
                "new": "新推荐",
                "viewed": "已查看",
                "interested": "已感兴趣",
            }.get(str(_value(recommendation, "status", "new")), "推荐")
            reason = str(
                _value(
                    recommendation,
                    "reason",
                    _value(recommendation, "recommendation_reason", "适合进一步了解"),
                )
                or "适合进一步了解"
            )
            item = QListWidgetItem(
                f"[{status}] {name}　{self._price_text(context['product'], recommendation)}\n"
                f"推荐说明：{reason}"
            )
            item.setData(Qt.ItemDataRole.UserRole, context)
            self.recommendation_list.addItem(item)
            recommendation_id = context.get("recommendation_id")
            if (
                selected_recommendation_id is not None
                and recommendation_id == selected_recommendation_id
            ):
                selected_item = item
        if selected_item is not None:
            self.recommendation_list.setCurrentItem(selected_item)

    def _add_empty_rows(self) -> None:
        if self.recommendation_list.count() == 0:
            item = QListWidgetItem("当前没有推荐商品。")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.recommendation_list.addItem(item)
        if self.product_list.count() == 0:
            item = QListWidgetItem("当前没有可查看的商品。")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.product_list.addItem(item)
        if self.order_list.count() == 0:
            item = QListWidgetItem("还没有模拟订单。")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.order_list.addItem(item)

    @staticmethod
    def _recommendation_context(recommendation: object) -> dict[str, object]:
        product = _value(recommendation, "product", None)
        if product in (None, "", {}):
            product = recommendation
        recommendation_id = _value(recommendation, "id", None)
        return {
            "product": product,
            "recommendation_id": recommendation_id,
            "source": recommendation,
        }

    @staticmethod
    def _product_id(product: object, source: object | None = None) -> int | None:
        value = _value(source, "product_id", None) if source is not None else None
        if value in (None, ""):
            value = _value(product, "id", None)
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _product_name(product: object, source: object | None = None) -> str:
        value = _value(
            product,
            "name",
            _value(product, "title", _value(product, "product_name", "")),
        )
        if not value and source is not None:
            value = _value(source, "product_name", "")
        return str(value or "未命名商品")

    @staticmethod
    def _short_description(product: object) -> str:
        description = str(
            _value(product, "description", _value(product, "summary", "暂无商品说明"))
            or "暂无商品说明"
        )
        return description if len(description) <= 72 else f"{description[:72]}…"

    @staticmethod
    def _price_text(product: object, source: object | None = None) -> str:
        display = _value(product, "price_display", "")
        if display:
            return str(display)
        cents = _value(product, "price_cents", None)
        if cents in (None, "") and source is not None:
            cents = _value(source, "price_cents", None)
        if cents not in (None, ""):
            try:
                return f"¥{int(cents) / 100:.2f}"
            except (TypeError, ValueError):
                pass
        price = _value(product, "price", None)
        if price in (None, "") and source is not None:
            price = _value(source, "price", None)
        if price not in (None, ""):
            try:
                return f"¥{float(price):.2f}"
            except (TypeError, ValueError):
                return str(price)
        return "价格待定"

    def _select_item(self, item: QListWidgetItem) -> None:
        context = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(context, Mapping):
            return
        product = context.get("product")
        source = context.get("source")
        product_id = self._product_id(product, source)
        if product is None or product_id is None:
            self._set_status("这件商品缺少有效编号，暂时不能操作。", error=True)
            return

        self.selected_product = product
        self.selected_product_id = product_id
        recommendation_id = context.get("recommendation_id")
        try:
            self.selected_recommendation_id = (
                int(recommendation_id) if recommendation_id not in (None, "") else None
            )
        except (TypeError, ValueError):
            self.selected_recommendation_id = None
        self.interested_button.setText("我感兴趣")
        self._set_actions_enabled(True)

        name = self._product_name(product, source)
        description = str(
            _value(
                product,
                "description",
                _value(source, "description", _value(source, "product_description", "暂无说明")),
            )
            or "暂无说明"
        )
        category = str(
            _value(product, "category", _value(source, "category", "未分类")) or "未分类"
        )
        brand = str(_value(product, "brand", _value(source, "brand", "")) or "")
        specification = str(
            _value(
                product,
                "specification",
                _value(source, "specification", ""),
            )
            or ""
        )
        unit = str(_value(product, "unit", _value(source, "unit", "")) or "")
        reason = str(_value(source, "reason", _value(source, "recommendation_reason", "")) or "")
        lines = [
            f"名称：{name}",
            f"参考价格：{self._price_text(product, source)}",
            f"类别：{category}",
            f"说明：{description}",
        ]
        if brand:
            lines.insert(3, f"品牌：{brand}")
        if specification:
            lines.insert(4, f"规格：{specification}{unit}")
        if reason:
            lines.append(f"推荐理由：{reason}")
        lines.append("提示：这是本地原型商品，不代表医疗建议或疗效承诺。")
        self.detail_label.setText("\n".join(lines))

        view_target_id = self.selected_recommendation_id
        if view_target_id is not None and self._last_viewed_recommendation_id != view_target_id:
            try:
                self.commerce_service.record_product_view(self.user_id, view_target_id)
                self._last_viewed_recommendation_id = view_target_id
                self._refresh_recommendations(view_target_id)
                self._add_empty_rows()
            except Exception as exc:
                self._set_status(_service_error("记录商品浏览", exc), error=True)

    def _mark_interested(self) -> None:
        if self.user_id is None or self.selected_product is None:
            return
        product_id = self.selected_product_id
        if product_id is None:
            return
        try:
            if self.selected_recommendation_id is not None:
                self.commerce_service.mark_interested(self.user_id, self.selected_recommendation_id)
            else:
                self.commerce_service.mark_product_interested(self.user_id, product_id)
            self._refresh_recommendations(self.selected_recommendation_id)
            self._add_empty_rows()
            self.interested_button.setText("已记录兴趣")
            self._set_status("已在本机记录“我感兴趣”，不会自动购买或联系商家。")
        except Exception as exc:
            self._set_status(_service_error("记录商品兴趣", exc), error=True)

    def _simulate_purchase(self) -> None:
        if self.user_id is None or self.selected_product is None:
            return
        product_id = self.selected_product_id
        if product_id is None:
            return
        quantity = self.quantity_input.value()
        name = self._product_name(self.selected_product)
        answer = QMessageBox.question(
            self,
            "确认模拟购买",
            f"准备模拟购买：{name} × {quantity}\n\n"
            "本操作只会在本机生成模拟订单，不会付款、扣款、发货，也不会把信息发送给商家。"
            "是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            created_order = self.commerce_service.simulate_purchase(
                self.user_id,
                product_id,
                quantity=quantity,
                recommendation_id=self.selected_recommendation_id,
            )
            created_order_id = self._safe_int(_value(created_order, "id", None))
            self._refresh_orders(created_order_id)
            self._refresh_recommendations(self.selected_recommendation_id)
            self._add_empty_rows()
            self._set_status("模拟购买已完成并保存到本机；没有发生真实付款或物流。")
        except Exception as exc:
            self._set_status(_service_error("创建模拟订单", exc), error=True)

    def _refresh_orders(self, selected_order_id: int | None = None) -> None:
        if selected_order_id is None:
            current = self.order_list.currentItem()
            current_order = current.data(Qt.ItemDataRole.UserRole) if current else None
            selected_order_id = self._safe_int(_value(current_order, "id", None))
        self.order_list.clear()
        orders = _items(self.commerce_service.list_orders(self.user_id))
        status_labels = {
            "created": "已创建",
            "delivered": "模拟已交付",
        }
        order_numbers = {
            order_id: str(_value(order, "order_no", order_id))
            for order in orders
            if (order_id := self._safe_int(_value(order, "id", None))) is not None
        }
        selected_item: QListWidgetItem | None = None
        for order in orders:
            product_name = str(
                _value(
                    order,
                    "product_name",
                    _value(order, "product_name_snapshot", _value(order, "name", "商品")),
                )
                or "商品"
            )
            quantity = _value(order, "quantity", 1)
            status_code = str(_value(order, "status", ""))
            status = status_labels.get(status_code, "状态未知")
            created_at = _display_time(_value(order, "created_at", _value(order, "ordered_at", "")))
            total = self._order_total_text(order)
            number = str(_value(order, "order_no", _value(order, "id", "")) or "")
            prefix = f"模拟单号 {number}｜" if number else ""
            reordered_from_id = self._safe_int(_value(order, "reordered_from_order_id", None))
            reorder_line = ""
            if reordered_from_id is not None:
                original_number = order_numbers.get(reordered_from_id, f"编号 {reordered_from_id}")
                reorder_line = f"\n再次购买自：{original_number}"
            item = QListWidgetItem(
                f"{prefix}{created_at}｜{status}\n"
                f"{product_name} × {quantity}｜{total}｜无真实物流{reorder_line}"
            )
            item.setData(Qt.ItemDataRole.UserRole, order)
            self.order_list.addItem(item)
            if self._safe_int(_value(order, "id", None)) == selected_order_id:
                selected_item = item
        if selected_item is not None:
            self.order_list.setCurrentItem(selected_item)

    def _order_selection_changed(self) -> None:
        item = self.order_list.currentItem()
        order = item.data(Qt.ItemDataRole.UserRole) if item else None
        order_id = self._safe_int(_value(order, "id", None))
        self._set_order_actions_enabled(order_id is not None)
        if order_id is not None:
            quantity = self._safe_int(_value(order, "quantity", 1)) or 1
            self.reorder_quantity_input.setValue(min(99, max(1, quantity)))

    def _reorder_selected(self) -> None:
        if self.user_id is None:
            return
        item = self.order_list.currentItem()
        order = item.data(Qt.ItemDataRole.UserRole) if item else None
        order_id = self._safe_int(_value(order, "id", None))
        if order_id is None:
            QMessageBox.information(self, "请先选择", "请先在模拟订单列表中选择一条订单。")
            return
        order_no = str(_value(order, "order_no", order_id))
        product_name = str(
            _value(
                order,
                "product_name",
                _value(order, "product_name_snapshot", "商品"),
            )
            or "商品"
        )
        quantity = self.reorder_quantity_input.value()
        answer = QMessageBox.question(
            self,
            "确认再次模拟购买",
            f"准备根据原模拟订单 {order_no} 再次模拟购买：\n"
            f"{product_name} × {quantity}\n\n"
            "本操作不会发生真实付款、扣款或发货，也不会向商家发送信息。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            created_order = self.commerce_service.reorder(self.user_id, order_id, quantity=quantity)
            created_order_id = self._safe_int(_value(created_order, "id", None))
            self._refresh_orders(created_order_id)
            self._refresh_recommendations(self.selected_recommendation_id)
            self._add_empty_rows()
            self._set_status("再次模拟购买已保存到本机；没有发生真实付款、扣款或发货。")
        except Exception as exc:
            self._set_status(_service_error("再次创建模拟订单", exc), error=True)

    @staticmethod
    def _order_total_text(order: object) -> str:
        cents = _value(
            order,
            "total_amount_cents",
            _value(order, "amount_cents", _value(order, "total_cents", None)),
        )
        if cents not in (None, ""):
            try:
                return f"模拟合计 ¥{int(cents) / 100:.2f}"
            except (TypeError, ValueError):
                pass
        total = _value(order, "total_amount", _value(order, "total", None))
        if total not in (None, ""):
            try:
                return f"模拟合计 ¥{float(total):.2f}"
            except (TypeError, ValueError):
                return f"模拟合计 {total}"
        return "模拟合计待定"

    def _set_actions_enabled(self, enabled: bool) -> None:
        self.interested_button.setEnabled(enabled)
        self.quantity_input.setEnabled(enabled)
        self.purchase_button.setEnabled(enabled)

    def _set_order_actions_enabled(self, enabled: bool) -> None:
        self.reorder_quantity_input.setEnabled(enabled)
        self.reorder_button.setEnabled(enabled)

    @staticmethod
    def _safe_int(value: object) -> int | None:
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def _set_status(self, text: str, error: bool = False) -> None:
        self.status_label.setObjectName("HealthError" if error else "HealthSuccess")
        self.status_label.setText(text)
        self.status_label.show()
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)


class ServicePage(QWidget):
    reminder_changed = Signal()

    def __init__(
        self,
        health_service: object,
        commerce_service: object | None = None,
        file_management_service: object | None = None,
    ) -> None:
        super().__init__()
        self.health_service = health_service
        self.user_id: int | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 4, 8, 8)
        root.setSpacing(12)
        title = QLabel("我的服务")
        title.setObjectName("PageTitle")
        root.addWidget(title)

        self.section_tabs = QTabWidget()
        self.section_tabs.setDocumentMode(True)
        care_tab = QWidget()
        care_layout = QVBoxLayout(care_tab)
        care_layout.setContentsMargins(8, 10, 8, 8)
        care_layout.setSpacing(12)

        summaries = QHBoxLayout()
        self.membership_label = QLabel("暂无会员信息")
        self.membership_label.setWordWrap(True)
        self.advisor_label = QLabel("暂未绑定顾问")
        self.advisor_label.setWordWrap(True)
        self.visit_label = QLabel("暂无上门安排")
        self.visit_label.setWordWrap(True)
        summaries.addWidget(_card("会员", self.membership_label), 1)
        summaries.addWidget(_card("顾问", self.advisor_label), 1)
        summaries.addWidget(_card("下次上门", self.visit_label), 1)
        care_layout.addLayout(summaries)

        visit_header = QHBoxLayout()
        visit_title = QLabel("历史上门服务")
        visit_title.setObjectName("SectionTitle")
        visit_header.addWidget(visit_title)
        visit_header.addStretch(1)
        self.visit_count_label = QLabel("已完成 0 次")
        self.visit_count_label.setObjectName("HealthHint")
        visit_header.addWidget(self.visit_count_label)
        care_layout.addLayout(visit_header)
        self.visit_history_list = QListWidget()
        self.visit_history_list.setMaximumHeight(150)
        self.visit_history_list.itemDoubleClicked.connect(self._show_visit_detail)
        care_layout.addWidget(self.visit_history_list)

        reminder_header = QHBoxLayout()
        reminder_title = QLabel("提醒")
        reminder_title.setObjectName("SectionTitle")
        reminder_header.addWidget(reminder_title)
        reminder_header.addStretch(1)
        add_button = QPushButton("新增提醒")
        add_button.setObjectName("PrimaryButton")
        add_button.clicked.connect(self._add_reminder)
        reminder_header.addWidget(add_button)
        self.toggle_button = QPushButton("启用 / 停用")
        self.toggle_button.clicked.connect(self._toggle_selected)
        reminder_header.addWidget(self.toggle_button)
        self.delete_button = QPushButton("删除")
        self.delete_button.setObjectName("DangerButton")
        self.delete_button.clicked.connect(self._delete_selected)
        reminder_header.addWidget(self.delete_button)
        care_layout.addLayout(reminder_header)

        self.reminder_list = QListWidget()
        care_layout.addWidget(self.reminder_list, 1)
        self.status_label = QLabel()
        self.status_label.setObjectName("HealthError")
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        care_layout.addWidget(self.status_label)

        self.commerce_panel = CommercePanel(commerce_service)
        self.files_reports_panel = FilesReportsPanel(file_management_service)
        self.section_tabs.addTab(care_tab, "会员服务与提醒")
        self.section_tabs.addTab(self.commerce_panel, "商品推荐与模拟购买")
        self.section_tabs.addTab(self.files_reports_panel, "文件与健康报告")
        root.addWidget(self.section_tabs, 1)

    def set_user(self, user_id: int | None) -> None:
        self.user_id = user_id
        self.commerce_panel.user_id = user_id
        self.files_reports_panel.user_id = user_id
        self.refresh()

    def refresh(self) -> None:
        self.reminder_list.clear()
        self.visit_history_list.clear()
        if self.user_id is None:
            self.membership_label.setText("暂无会员信息")
            self.advisor_label.setText("暂未绑定顾问")
            self.visit_label.setText("暂无上门安排")
            self.visit_count_label.setText("已完成 0 次")
            self.status_label.hide()
            self.commerce_panel.refresh()
            self.files_reports_panel.refresh()
            return
        errors: list[str] = []
        try:
            summary = self.health_service.get_service_summary(self.user_id) or {}
            self.membership_label.setText(
                self._membership_text(_value(summary, "membership", None))
            )
            self.advisor_label.setText(self._advisor_text(_value(summary, "advisor", None)))
            self.visit_label.setText(
                self._next_visit_text(_value(summary, "next_visit", _value(summary, "visit", None)))
            )
            completed_count = int(_value(summary, "completed_visit_count", 0) or 0)
            self.visit_count_label.setText(f"已完成 {completed_count} 次")
            visits = _items(_value(summary, "visit_records", []))
            for visit in visits:
                visited_at = _display_time(_value(visit, "visited_at", ""))
                advisor_name = str(_value(visit, "advisor_name", "顾问未填写") or "顾问未填写")
                visit_summary = str(_value(visit, "summary", "上门服务") or "上门服务")
                item = QListWidgetItem(f"{visited_at}　{advisor_name}\n{visit_summary}")
                item.setData(Qt.ItemDataRole.UserRole, visit)
                self.visit_history_list.addItem(item)
            if not visits:
                item = QListWidgetItem("尚无已完成的上门服务记录。")
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                self.visit_history_list.addItem(item)
        except Exception as exc:
            errors.append(_service_error("读取服务摘要", exc))
        try:
            reminders = _items(self.health_service.list_reminders(self.user_id))
            for reminder in reminders:
                enabled = bool(_value(reminder, "enabled", _value(reminder, "is_active", True)))
                marker = "已启用" if enabled else "已停用"
                title = _value(reminder, "title", "提醒")
                time = _display_time(_value(reminder, "scheduled_at", ""))
                item = QListWidgetItem(f"[{marker}] {time}　{title}")
                item.setData(Qt.ItemDataRole.UserRole, _value(reminder, "id", None))
                item.setData(Qt.ItemDataRole.UserRole + 1, enabled)
                self.reminder_list.addItem(item)
            if not reminders:
                empty = QListWidgetItem("还没有提醒，可点击“新增提醒”。")
                empty.setFlags(Qt.ItemFlag.NoItemFlags)
                self.reminder_list.addItem(empty)
        except Exception as exc:
            errors.append(_service_error("读取提醒", exc))
        self.status_label.setText("\n".join(errors))
        self.status_label.setVisible(bool(errors))
        self.commerce_panel.refresh()
        self.files_reports_panel.refresh()

    @staticmethod
    def _summary_text(value: object, empty: str) -> str:
        if value in (None, "", {}):
            return empty
        if isinstance(value, str):
            return value
        if isinstance(value, Mapping):
            useful = [str(item) for item in value.values() if item not in (None, "", False)]
            return "\n".join(useful) if useful else empty
        return str(value)

    @staticmethod
    def _membership_text(value: object) -> str:
        if isinstance(value, str):
            return value
        if not isinstance(value, Mapping):
            return "暂无会员信息\n可由运营人员在本地管理台设置。"
        status_labels = {
            "active": "有效",
            "pending": "待生效",
            "expired": "已到期",
            "cancelled": "已取消",
        }
        plan = str(_value(value, "plan_code", "未命名套餐"))
        status = status_labels.get(str(_value(value, "status", "")), "状态未填写")
        starts_at = _display_time(_value(value, "starts_at", ""))[:10]
        ends_value = _value(value, "ends_at", None)
        ends_at = _display_time(ends_value)[:10] if ends_value else "长期"
        remaining = ""
        if isinstance(ends_value, datetime):
            remaining_days = max(0, (as_beijing(ends_value).date() - beijing_today()).days)
            remaining = f"\n剩余天数：{remaining_days} 天"
        benefits = _value(value, "benefits", {})
        benefit_lines: list[str] = []
        if isinstance(benefits, Mapping):
            total = benefits.get("visit_total", benefits.get("annual_visits"))
            used = benefits.get("visit_used", benefits.get("used_visits", 0))
            if total not in (None, ""):
                benefit_lines.append(f"上门权益：已使用 {used} / 共 {total} 次")
            filing = benefits.get("first_filing")
            if filing not in (None, ""):
                benefit_lines.append(f"首次建档：{'已完成' if filing else '待完成'}")
            gift = benefits.get("gift")
            if gift:
                benefit_lines.append(f"会员礼品：{gift}")
        suffix = "" if not benefit_lines else "\n" + "\n".join(benefit_lines)
        return (
            f"套餐：{plan}\n状态：{status}\n开始：{starts_at}\n到期：{ends_at}{remaining}{suffix}"
        )

    @staticmethod
    def _advisor_text(value: object) -> str:
        if isinstance(value, str):
            return value
        if not isinstance(value, Mapping):
            return "暂未绑定家庭生活顾问\n请联系运营人员完成绑定。"
        name = str(_value(value, "display_name", "顾问") or "顾问")
        organization = str(_value(value, "organization", "") or "")
        specialty = str(_value(value, "specialty", "") or "")
        started_at = _value(value, "started_at", None)
        lines = [f"姓名：{name}"]
        if organization:
            lines.append(f"机构：{organization}")
        if specialty:
            lines.append(f"擅长：{specialty}")
        if started_at:
            lines.append(f"服务开始：{_display_time(started_at)[:10]}")
        return "\n".join(lines)

    @staticmethod
    def _next_visit_text(value: object) -> str:
        if isinstance(value, str):
            return value
        if not isinstance(value, Mapping):
            return "暂无上门安排"
        title = str(_value(value, "title", "上门服务") or "上门服务")
        scheduled_at = _display_time(_value(value, "scheduled_at", ""))
        advisor_name = str(_value(value, "advisor_name", "") or "")
        result = f"{title}\n时间：{scheduled_at}"
        return result if not advisor_name else f"{result}\n顾问：{advisor_name}"

    def _show_visit_detail(self, item: QListWidgetItem) -> None:
        visit = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(visit, Mapping):
            return
        details = _value(visit, "details", {})
        detail_labels = {
            "duration_minutes": "服务时长",
            "inspected_areas": "本次查看内容",
            "user_questions": "用户提出的问题",
            "environment_changes": "家庭环境变化",
            "profile_changes": "档案修改说明",
            "follow_ups": "后续待办",
            "gifts": "礼品记录",
            "notes": "顾问备注",
        }
        lines = [
            f"时间：{_display_time(_value(visit, 'visited_at', ''))}",
            f"顾问：{_value(visit, 'advisor_name', '未填写')}",
            f"摘要：{_value(visit, 'summary', '')}",
        ]
        if isinstance(details, Mapping):
            for key, label in detail_labels.items():
                detail = details.get(key)
                if detail not in (None, "", [], {}):
                    suffix = " 分钟" if key == "duration_minutes" else ""
                    lines.append(f"{label}：{detail}{suffix}")
        QMessageBox.information(self, "上门服务详情", "\n\n".join(lines))

    def _add_reminder(self) -> None:
        if self.user_id is None:
            return
        dialog = ReminderDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values
        try:
            self.health_service.add_reminder(
                self.user_id,
                values["title"],
                values["scheduled_at"],
                reminder_type="custom",
            )
            self.refresh()
            self.reminder_changed.emit()
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", _service_error("保存提醒", exc))

    def _selected(self) -> tuple[int, bool] | None:
        item = self.reminder_list.currentItem()
        reminder_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        if reminder_id is None:
            QMessageBox.information(self, "请先选择", "请先在列表中选择一条提醒。")
            return None
        return int(reminder_id), bool(item.data(Qt.ItemDataRole.UserRole + 1))

    def _toggle_selected(self) -> None:
        if self.user_id is None:
            return
        selected = self._selected()
        if selected is None:
            return
        reminder_id, enabled = selected
        try:
            self.health_service.toggle_reminder(self.user_id, reminder_id, not enabled)
            self.refresh()
            self.reminder_changed.emit()
        except Exception as exc:
            QMessageBox.warning(self, "修改失败", _service_error("修改提醒状态", exc))

    def _delete_selected(self) -> None:
        if self.user_id is None:
            return
        selected = self._selected()
        if selected is None:
            return
        answer = QMessageBox.question(
            self,
            "确认删除",
            "这条提醒删除后无法在应用内恢复。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.health_service.delete_reminder(self.user_id, selected[0])
            self.refresh()
            self.reminder_changed.emit()
        except Exception as exc:
            QMessageBox.warning(self, "删除失败", _service_error("删除提醒", exc))


class HealthWorkspace(QWidget):
    """Member-facing shell that keeps health data UI separate from chat logic."""

    logout_requested = Signal()
    export_requested = Signal()
    import_requested = Signal()

    def __init__(
        self,
        health_service: object,
        chat_page: QWidget,
        backup_service: object | None = None,
        commerce_service: object | None = None,
        file_management_service: object | None = None,
    ) -> None:
        super().__init__()
        self.setObjectName("HealthWorkspace")
        self.health_service = health_service
        self.chat_page = chat_page
        self.backup_service = backup_service
        self.current_user: Any | None = None

        # The platform shell already owns account and backup actions. Keep only
        # the provider/model controls in the embedded chat header.
        for attribute in ("user_label", "backup_button", "import_button", "logout_button"):
            if widget := getattr(chat_page, attribute, None):
                widget.hide()

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)

        header = QFrame()
        header.setObjectName("HealthCard")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(18, 12, 18, 12)
        title = QLabel("健康生活服务平台")
        title.setObjectName("AppTitle")
        header_layout.addWidget(title)
        header_layout.addWidget(QLabel("北京时间（UTC+08:00）"))
        header_layout.addStretch(1)
        self.user_label = QLabel("未登录")
        header_layout.addWidget(self.user_label)
        self.export_button = QPushButton("导出数据")
        self.export_button.setEnabled(backup_service is not None)
        self.export_button.setToolTip("导出本机账户、聊天和健康生活数据。")
        self.export_button.clicked.connect(self.export_requested)
        header_layout.addWidget(self.export_button)
        self.import_button = QPushButton("导入数据")
        self.import_button.setEnabled(backup_service is not None)
        self.import_button.setToolTip("导入本应用生成的数据备份。")
        self.import_button.clicked.connect(self.import_requested)
        header_layout.addWidget(self.import_button)
        logout_button = QPushButton("退出登录")
        logout_button.clicked.connect(self.logout_requested)
        header_layout.addWidget(logout_button)
        root.addWidget(header)

        body = QHBoxLayout()
        body.setSpacing(14)
        navigation = QFrame()
        navigation.setObjectName("HealthNavigation")
        navigation.setFixedWidth(174)
        nav_layout = QVBoxLayout(navigation)
        nav_layout.setContentsMargins(10, 14, 10, 14)
        nav_layout.setSpacing(9)

        self.stack = QStackedWidget()
        self.today_page = TodayPage(health_service)
        self.records_page = HealthRecordsPanel(health_service)
        self.profile_page = ProfilePage(health_service)
        self.service_page = ServicePage(
            health_service,
            commerce_service,
            file_management_service,
        )
        pages = (
            ("今日", self.today_page),
            ("记录与提醒", self.records_page),
            ("档案", self.profile_page),
            ("服务", self.service_page),
            ("AI 助手", chat_page),
        )
        self.navigation_buttons: list[QPushButton] = []
        for index, (label, page) in enumerate(pages):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setMinimumHeight(52)
            button.clicked.connect(lambda _checked=False, i=index: self.show_page(i))
            nav_layout.addWidget(button)
            self.navigation_buttons.append(button)
            self.stack.addWidget(page)
        nav_layout.addStretch(1)
        body.addWidget(navigation)
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)

        self.today_page.record_changed.connect(self.records_page.refresh)
        self.records_page.record_changed.connect(self.today_page.refresh)
        self.records_page.reminder_changed.connect(self.today_page.refresh)
        self.profile_page.profile_changed.connect(self.today_page.refresh)
        self.service_page.reminder_changed.connect(self.today_page.refresh)
        if hasattr(chat_page, "logout_requested"):
            chat_page.logout_requested.connect(self.logout_requested)
        if hasattr(chat_page, "data_changed"):
            chat_page.data_changed.connect(self._refresh_member_data)
        self.show_page(0)
        self.setStyleSheet(
            """
            QWidget#HealthWorkspace { font-size: 16px; }
            QFrame#HealthCard, QFrame#HealthNavigation {
                background: #ffffff;
                border: 1px solid #dce2eb;
                border-radius: 14px;
            }
            QLabel#AppTitle { font-size: 24px; font-weight: 700; color: #12372a; }
            QLabel#PageTitle { font-size: 26px; font-weight: 700; color: #153e2f; }
            QLabel#SectionTitle { font-size: 18px; font-weight: 700; color: #1f493a; }
            QLabel#HealthHint { color: #5f6f68; }
            QLabel#HealthError {
                color: #9c2f27; background: #fff1f0; border-radius: 8px; padding: 8px;
            }
            QLabel#HealthSuccess { color: #176b47; padding: 8px; }
            QLabel#CommerceNotice {
                color: #724c13; background: #fff8e8; border: 1px solid #e8c97a;
                border-radius: 10px; padding: 12px; font-weight: 600;
            }
            QPushButton { min-height: 32px; }
            QFrame#HealthNavigation QPushButton {
                min-height: 48px; text-align: left; padding-left: 20px;
            }
            QFrame#HealthNavigation QPushButton:checked {
                background: #dff4e8; color: #145a3d; border-color: #79b997; font-weight: 700;
            }
            QTabBar::tab { min-height: 42px; min-width: 190px; padding: 4px 14px; }
            QTabBar::tab:selected { color: #145a3d; font-weight: 700; }
            QListWidget {
                background: #ffffff; border: 1px solid #dce2eb;
                border-radius: 10px; padding: 8px;
            }
            QListWidget::item { padding: 10px; }
            """
        )

    def show_page(self, index: int) -> None:
        if not 0 <= index < self.stack.count():
            return
        self.stack.setCurrentIndex(index)
        for button_index, button in enumerate(self.navigation_buttons):
            button.setChecked(button_index == index)
        page = self.stack.currentWidget()
        if hasattr(page, "refresh"):
            page.refresh()

    def _refresh_member_data(self) -> None:
        self.today_page.refresh()
        self.records_page.refresh()
        self.profile_page.refresh()
        self.service_page.refresh()

    def start_session(self, user: Any) -> None:
        self.current_user = user
        self.user_label.setText(f"当前用户：{getattr(user, 'username', '')}")
        user_id = int(user.id)
        for page in (self.today_page, self.records_page, self.profile_page, self.service_page):
            page.set_user(user_id)
        if hasattr(self.chat_page, "start_session"):
            self.chat_page.start_session(user)
        self.show_page(0)

    def end_session(self) -> None:
        self.current_user = None
        self.user_label.setText("未登录")
        for page in (self.today_page, self.records_page, self.profile_page):
            page.user_id = None
        self.service_page.set_user(None)
        if hasattr(self.chat_page, "end_session"):
            self.chat_page.end_session()
        self.show_page(0)


__all__ = [
    "HealthWorkspace",
    "TodayPage",
    "RecordsPage",
    "ProfilePage",
    "ServicePage",
    "CommercePanel",
]
