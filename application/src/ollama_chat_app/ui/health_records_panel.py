from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from PySide6.QtCore import QDateTime, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..services.health import HealthService, HealthValidationError
from ..time_utils import beijing_today, format_beijing
from .time_fields import BeijingDateTimeEdit, beijing_qdatetime, datetime_iso

CATEGORIES = (
    ("diet", "饮食"),
    ("water", "饮水"),
    ("activity", "活动"),
    ("sleep", "睡眠"),
    ("environment", "居住环境"),
)
CATEGORY_LABELS = dict(CATEGORIES)
REMINDER_TYPES = (
    ("water", "饮水"),
    ("diet", "饮食"),
    ("sleep", "睡眠"),
    ("activity", "活动"),
    ("environment", "环境"),
    ("visit", "顾问回访"),
    ("membership", "会员服务"),
    ("custom", "自定义生活事项"),
)
REMINDER_LABELS = dict(REMINDER_TYPES)


def _local_iso(value: QDateTime) -> str:
    return datetime_iso(value)


def _display_time(value: object) -> str:
    return format_beijing(value)


def _message(parent: QWidget, title: str, error: Exception) -> None:
    QMessageBox.warning(parent, title, f"{error}\n请检查填写内容后重试。")


class LifeRecordDialog(QDialog):
    """Five-category fact entry form; it deliberately contains no diagnosis fields."""

    def __init__(self, category: str = "diet", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("新增生活记录")
        self.setMinimumWidth(560)
        root = QVBoxLayout(self)
        form = QFormLayout()

        self.category_combo = QComboBox()
        for code, label in CATEGORIES:
            self.category_combo.addItem(label, code)
        self.category_combo.setCurrentIndex(max(0, self.category_combo.findData(category)))
        form.addRow("记录类别", self.category_combo)
        self.time_input = BeijingDateTimeEdit()
        form.addRow("发生时间（北京时间）", self.time_input)
        root.addLayout(form)

        self.detail_stack = QStackedWidget()
        self._detail_pages: dict[str, QWidget] = {}
        for code, _label in CATEGORIES:
            page = QWidget()
            self._detail_pages[code] = page
            self.detail_stack.addWidget(page)
        self._build_detail_pages()
        root.addWidget(self.detail_stack)

        root.addWidget(QLabel("事实描述（例如：午餐吃了一碗米饭和蔬菜）"))
        self.content_input = QTextEdit()
        self.content_input.setPlaceholderText("只记录实际发生的内容，不填写疾病诊断或治疗结论。")
        self.content_input.setMinimumHeight(110)
        root.addWidget(self.content_input)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存事实")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.category_combo.currentIndexChanged.connect(self._show_category)
        self._show_category()

    def _build_detail_pages(self) -> None:
        diet = QFormLayout(self._detail_pages["diet"])
        self.meal_type = QComboBox()
        for code, label in (
            ("breakfast", "早餐"),
            ("lunch", "午餐"),
            ("dinner", "晚餐"),
            ("snack", "加餐"),
        ):
            self.meal_type.addItem(label, code)
        self.calories = QSpinBox()
        self.calories.setRange(0, 20_000)
        self.calories.setSpecialValueText("未填写")
        diet.addRow("餐次", self.meal_type)
        diet.addRow("估算热量（千卡，可不填）", self.calories)

        water = QFormLayout(self._detail_pages["water"])
        self.water_amount = QSpinBox()
        self.water_amount.setRange(0, 10_000)
        self.water_amount.setSuffix(" 毫升")
        self.water_amount.setSpecialValueText("未填写")
        water.addRow("饮水量", self.water_amount)

        activity = QFormLayout(self._detail_pages["activity"])
        self.activity_type = QLineEdit()
        self.activity_type.setPlaceholderText("例如：散步、健身操")
        self.activity_minutes = QSpinBox()
        self.activity_minutes.setRange(0, 1_440)
        self.activity_minutes.setSuffix(" 分钟")
        self.activity_minutes.setSpecialValueText("未填写")
        self.steps = QSpinBox()
        self.steps.setRange(0, 200_000)
        self.steps.setSuffix(" 步")
        self.steps.setSpecialValueText("未填写")
        activity.addRow("活动方式", self.activity_type)
        activity.addRow("活动时长", self.activity_minutes)
        activity.addRow("步数", self.steps)

        sleep = QFormLayout(self._detail_pages["sleep"])
        self.sleep_hours = QDoubleSpinBox()
        self.sleep_hours.setRange(0, 24)
        self.sleep_hours.setDecimals(1)
        self.sleep_hours.setSingleStep(0.5)
        self.sleep_hours.setSuffix(" 小时")
        self.sleep_hours.setSpecialValueText("未填写")
        self.sleep_quality = QSpinBox()
        self.sleep_quality.setRange(0, 5)
        self.sleep_quality.setSpecialValueText("未填写")
        sleep.addRow("睡眠时长", self.sleep_hours)
        sleep.addRow("主观质量（1 至 5）", self.sleep_quality)

        environment = QFormLayout(self._detail_pages["environment"])
        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(-80, 80)
        self.temperature.setDecimals(1)
        self.temperature.setSuffix(" ℃")
        self.temperature.setSpecialValueText("未填写")
        self.temperature.setValue(0)
        self.humidity = QDoubleSpinBox()
        self.humidity.setRange(0, 100)
        self.humidity.setDecimals(1)
        self.humidity.setSuffix(" %")
        self.humidity.setSpecialValueText("未填写")
        self.air_quality = QLineEdit()
        self.air_quality.setPlaceholderText("例如：通风良好、略有异味")
        environment.addRow("室内温度（可不填）", self.temperature)
        environment.addRow("室内湿度（可不填）", self.humidity)
        environment.addRow("环境描述", self.air_quality)

    def _show_category(self) -> None:
        category = str(self.category_combo.currentData())
        self.detail_stack.setCurrentWidget(self._detail_pages[category])

    def _validate(self) -> None:
        if not self.content_input.toPlainText().strip():
            QMessageBox.warning(self, "内容未填写", "请先填写发生过的事实。")
            return
        self.accept()

    @property
    def values(self) -> dict[str, Any]:
        category = str(self.category_combo.currentData())
        details: dict[str, Any]
        if category == "diet":
            details = {"meal_type": str(self.meal_type.currentData())}
            if self.calories.value():
                details["calories_kcal"] = self.calories.value()
        elif category == "water":
            details = {}
            if self.water_amount.value():
                details["amount_ml"] = self.water_amount.value()
        elif category == "activity":
            details = {}
            if self.activity_type.text().strip():
                details["activity_type"] = self.activity_type.text().strip()
            if self.activity_minutes.value():
                details["duration_minutes"] = self.activity_minutes.value()
            if self.steps.value():
                details["steps"] = self.steps.value()
        elif category == "sleep":
            details = {}
            if self.sleep_hours.value():
                details["duration_hours"] = self.sleep_hours.value()
            if self.sleep_quality.value():
                details["quality"] = self.sleep_quality.value()
        else:
            details = {}
            if self.temperature.value():
                details["temperature_c"] = self.temperature.value()
            if self.humidity.value():
                details["humidity_percent"] = self.humidity.value()
            if self.air_quality.text().strip():
                details["air_quality"] = self.air_quality.text().strip()
        return {
            "category": category,
            "occurred_at": _local_iso(self.time_input.dateTime()),
            "content": self.content_input.toPlainText().strip(),
            "details": details,
        }


class ReminderEditorDialog(QDialog):
    def __init__(
        self,
        reminder: Mapping[str, Any] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑提醒" if reminder else "新增提醒")
        self.setMinimumWidth(500)
        form = QFormLayout(self)
        self.title_input = QLineEdit(str((reminder or {}).get("title", "")))
        self.title_input.setPlaceholderText("例如：晚上九点记录睡眠准备情况")
        form.addRow("提醒内容", self.title_input)
        self.type_combo = QComboBox()
        for code, label in REMINDER_TYPES:
            self.type_combo.addItem(label, code)
        current_type = str((reminder or {}).get("reminder_type", "custom"))
        self.type_combo.setCurrentIndex(max(0, self.type_combo.findData(current_type)))
        form.addRow("提醒类型", self.type_combo)
        initial = beijing_qdatetime().addSecs(3600)
        scheduled_at = (reminder or {}).get("scheduled_at")
        if isinstance(scheduled_at, (datetime, str)) and scheduled_at:
            initial = beijing_qdatetime(scheduled_at)
        self.time_input = BeijingDateTimeEdit(initial)
        form.addRow("提醒时间（北京时间）", self.time_input)
        hint = QLabel("提醒只用于饮水、饮食、睡眠、活动、环境、回访等生活事项，不提供药物提醒。")
        hint.setWordWrap(True)
        hint.setObjectName("HealthHint")
        form.addRow(hint)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存提醒")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _validate(self) -> None:
        if not self.title_input.text().strip():
            QMessageBox.warning(self, "内容未填写", "请先填写提醒内容。")
            return
        self.accept()

    @property
    def values(self) -> dict[str, str]:
        return {
            "title": self.title_input.text().strip(),
            "reminder_type": str(self.type_combo.currentData()),
            "scheduled_at": _local_iso(self.time_input.dateTime()),
        }


class HealthRecordsPanel(QWidget):
    """Reusable member panel for life facts, 7/30-day statistics, and reminders."""

    record_changed = Signal()
    reminder_changed = Signal()

    def __init__(self, health_service: HealthService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.health_service = health_service
        self.user_id: int | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 4, 8, 8)
        title = QLabel("生活记录与提醒")
        title.setObjectName("PageTitle")
        root.addWidget(title)
        notice = QLabel(
            "这里保存的是生活事实；统计仅做数量汇总，不提供健康评分、疾病诊断或治疗建议。"
        )
        notice.setObjectName("HealthHint")
        notice.setWordWrap(True)
        root.addWidget(notice)
        self.status_label = QLabel()
        self.status_label.setObjectName("HealthError")
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        root.addWidget(self.status_label)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_records_tab(), "生活记录")
        self.tabs.addTab(self._build_statistics_tab(), "7 / 30 天统计")
        self.tabs.addTab(self._build_reminders_tab(), "生活提醒")
        root.addWidget(self.tabs, 1)

    def _build_records_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        filters = QHBoxLayout()
        self.category_filter = QComboBox()
        self.category_filter.addItem("全部类别", None)
        for code, label in CATEGORIES:
            self.category_filter.addItem(label, code)
        self.record_period = QComboBox()
        self.record_period.addItem("全部日期", None)
        self.record_period.addItem("近 7 天", 7)
        self.record_period.addItem("近 30 天", 30)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh_records)
        add = QPushButton("新增记录")
        add.setObjectName("PrimaryButton")
        add.clicked.connect(self.add_record)
        delete = QPushButton("删除所选")
        delete.setObjectName("DangerButton")
        delete.clicked.connect(self.delete_selected_record)
        filters.addWidget(self.category_filter)
        filters.addWidget(self.record_period)
        filters.addStretch(1)
        filters.addWidget(refresh)
        filters.addWidget(add)
        filters.addWidget(delete)
        layout.addLayout(filters)
        self.record_table = QTableWidget(0, 4)
        self.record_table.setHorizontalHeaderLabels(["时间", "类别", "事实描述", "详情"])
        self.record_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.record_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self.record_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.record_table, 1)
        return page

    def _build_statistics_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        row = QHBoxLayout()
        self.statistics_period = QComboBox()
        self.statistics_period.addItem("近 7 天", 7)
        self.statistics_period.addItem("近 30 天", 30)
        refresh = QPushButton("更新统计")
        refresh.clicked.connect(self.refresh_statistics)
        row.addWidget(QLabel("统计周期"))
        row.addWidget(self.statistics_period)
        row.addStretch(1)
        row.addWidget(refresh)
        layout.addLayout(row)
        self.statistics_summary = QLabel("尚未读取统计。")
        self.statistics_summary.setWordWrap(True)
        card = QFrame()
        card.setObjectName("HealthCard")
        card_layout = QVBoxLayout(card)
        card_layout.addWidget(self.statistics_summary)
        layout.addWidget(card)
        self.daily_table = QTableWidget(0, 2)
        self.daily_table.setHorizontalHeaderLabels(["日期", "记录条数"])
        self.daily_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.daily_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.daily_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        layout.addWidget(self.daily_table, 1)
        return page

    def _build_reminders_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        buttons = QHBoxLayout()
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh_reminders)
        add = QPushButton("新增提醒")
        add.setObjectName("PrimaryButton")
        add.clicked.connect(self.add_reminder)
        edit = QPushButton("编辑所选")
        edit.clicked.connect(self.edit_selected_reminder)
        pause = QPushButton("暂停 / 恢复")
        pause.clicked.connect(self.toggle_selected_reminder)
        delete = QPushButton("删除所选")
        delete.setObjectName("DangerButton")
        delete.clicked.connect(self.delete_selected_reminder)
        buttons.addStretch(1)
        buttons.addWidget(refresh)
        buttons.addWidget(add)
        buttons.addWidget(edit)
        buttons.addWidget(pause)
        buttons.addWidget(delete)
        layout.addLayout(buttons)
        self.reminder_table = QTableWidget(0, 4)
        self.reminder_table.setHorizontalHeaderLabels(["提醒内容", "类型", "时间", "状态"])
        self.reminder_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.reminder_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self.reminder_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.reminder_table, 1)
        return page

    def set_user(self, user_id: int | None) -> None:
        self.user_id = user_id
        self.refresh()

    def refresh(self) -> None:
        if self.user_id is None:
            self.record_table.setRowCount(0)
            self.reminder_table.setRowCount(0)
            self.daily_table.setRowCount(0)
            return
        self.refresh_records()
        self.refresh_statistics()
        self.refresh_reminders()

    def refresh_records(self) -> None:
        if self.user_id is None:
            return
        try:
            days = self.record_period.currentData()
            start_date = (
                None
                if days is None
                else (beijing_today() - timedelta(days=int(days) - 1))
            )
            records = self.health_service.list_life_records(
                self.user_id,
                category=self.category_filter.currentData(),
                limit=10_000,
                start_date=start_date,
            )
            self.record_table.setRowCount(len(records))
            for row_index, record in enumerate(records):
                time_item = QTableWidgetItem(_display_time(record.get("occurred_at")))
                time_item.setData(Qt.ItemDataRole.UserRole, dict(record))
                self.record_table.setItem(row_index, 0, time_item)
                self.record_table.setItem(
                    row_index,
                    1,
                    QTableWidgetItem(CATEGORY_LABELS.get(str(record.get("category")), "其他")),
                )
                self.record_table.setItem(
                    row_index, 2, QTableWidgetItem(str(record.get("content", "")))
                )
                details = record.get("details") or {}
                self.record_table.setItem(
                    row_index,
                    3,
                    QTableWidgetItem(
                        json.dumps(details, ensure_ascii=False) if details else "未填写详细数值"
                    ),
                )
            self.status_label.hide()
        except Exception as error:
            self._show_error("读取生活记录失败", error, modal=False)

    def refresh_statistics(self) -> None:
        if self.user_id is None:
            return
        try:
            statistics = self.health_service.get_record_statistics(
                self.user_id, int(self.statistics_period.currentData())
            )
            counts = statistics["by_category"]
            metrics = statistics["metrics"]
            sleep = metrics["sleep_average_hours"]
            period_text = (
                f"{statistics['start_date']} 至 {statistics['end_date']}，"
                f"共 {statistics['total_records']} 条事实记录。"
            )
            water_activity_text = (
                f"饮水合计 {metrics['water_amount_ml']} 毫升；"
                f"活动 {metrics['activity_minutes']} 分钟；"
            )
            lines = [
                period_text,
                "；".join(f"{label} {counts[code]} 条" for code, label in CATEGORIES),
                (
                    water_activity_text + f"步数 {metrics['activity_steps']} 步；平均睡眠 "
                    f"{'未填写' if sleep is None else f'{sleep} 小时'}。"
                ),
                str(statistics["notice"]),
            ]
            self.statistics_summary.setText("\n".join(lines))
            daily = statistics["by_date"]
            self.daily_table.setRowCount(len(daily))
            for row_index, (day, count) in enumerate(daily.items()):
                self.daily_table.setItem(row_index, 0, QTableWidgetItem(day))
                self.daily_table.setItem(row_index, 1, QTableWidgetItem(str(count)))
            self.status_label.hide()
        except Exception as error:
            self._show_error("读取统计失败", error, modal=False)

    def refresh_reminders(self) -> None:
        if self.user_id is None:
            return
        try:
            reminders = self.health_service.list_reminders(self.user_id)
            self.reminder_table.setRowCount(len(reminders))
            for row_index, reminder in enumerate(reminders):
                title_item = QTableWidgetItem(str(reminder["title"]))
                title_item.setData(Qt.ItemDataRole.UserRole, dict(reminder))
                self.reminder_table.setItem(row_index, 0, title_item)
                self.reminder_table.setItem(
                    row_index,
                    1,
                    QTableWidgetItem(REMINDER_LABELS.get(str(reminder["reminder_type"]), "自定义")),
                )
                self.reminder_table.setItem(
                    row_index, 2, QTableWidgetItem(_display_time(reminder["scheduled_at"]))
                )
                self.reminder_table.setItem(
                    row_index,
                    3,
                    QTableWidgetItem("启用" if reminder["enabled"] else "已暂停"),
                )
            self.status_label.hide()
        except Exception as error:
            self._show_error("读取提醒失败", error, modal=False)

    def add_record(self) -> None:
        if self.user_id is None:
            return
        dialog = LifeRecordDialog(parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.health_service.add_life_record(self.user_id, **dialog.values)
            self.refresh_records()
            self.refresh_statistics()
            self.record_changed.emit()
        except Exception as error:
            self._show_error("保存生活记录失败", error)

    def delete_selected_record(self) -> None:
        record = self._selected_data(self.record_table)
        if self.user_id is None or record is None:
            QMessageBox.information(self, "尚未选择", "请先选择要删除的生活记录。")
            return
        if (
            QMessageBox.question(self, "确认删除", "确定删除所选生活事实吗？")
            != QMessageBox.StandardButton.Yes
        ):
            return
        try:
            self.health_service.delete_life_record(self.user_id, int(record["id"]))
            self.refresh_records()
            self.refresh_statistics()
            self.record_changed.emit()
        except Exception as error:
            self._show_error("删除生活记录失败", error)

    def add_reminder(self) -> None:
        if self.user_id is None:
            return
        dialog = ReminderEditorDialog(parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.health_service.add_reminder(self.user_id, **dialog.values)
            self.refresh_reminders()
            self.reminder_changed.emit()
        except Exception as error:
            self._show_error("保存提醒失败", error)

    def edit_selected_reminder(self) -> None:
        reminder = self._selected_data(self.reminder_table)
        if self.user_id is None or reminder is None:
            QMessageBox.information(self, "尚未选择", "请先选择要编辑的提醒。")
            return
        dialog = ReminderEditorDialog(reminder, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.health_service.update_reminder(self.user_id, int(reminder["id"]), **dialog.values)
            self.refresh_reminders()
            self.reminder_changed.emit()
        except Exception as error:
            self._show_error("编辑提醒失败", error)

    def toggle_selected_reminder(self) -> None:
        reminder = self._selected_data(self.reminder_table)
        if self.user_id is None or reminder is None:
            QMessageBox.information(self, "尚未选择", "请先选择要暂停或恢复的提醒。")
            return
        try:
            if bool(reminder["enabled"]):
                self.health_service.pause_reminder(self.user_id, int(reminder["id"]))
            else:
                self.health_service.resume_reminder(self.user_id, int(reminder["id"]))
            self.refresh_reminders()
            self.reminder_changed.emit()
        except Exception as error:
            self._show_error("更新提醒状态失败", error)

    def delete_selected_reminder(self) -> None:
        reminder = self._selected_data(self.reminder_table)
        if self.user_id is None or reminder is None:
            QMessageBox.information(self, "尚未选择", "请先选择要删除的提醒。")
            return
        if (
            QMessageBox.question(self, "确认删除", "确定删除所选提醒吗？")
            != QMessageBox.StandardButton.Yes
        ):
            return
        try:
            self.health_service.delete_reminder(self.user_id, int(reminder["id"]))
            self.refresh_reminders()
            self.reminder_changed.emit()
        except Exception as error:
            self._show_error("删除提醒失败", error)

    @staticmethod
    def _selected_data(table: QTableWidget) -> dict[str, Any] | None:
        row = table.currentRow()
        if row < 0:
            return None
        item = table.item(row, 0)
        if item is None:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        return dict(value) if isinstance(value, Mapping) else None

    def _show_error(self, title: str, error: Exception, *, modal: bool = True) -> None:
        if isinstance(error, HealthValidationError):
            self.status_label.setText(str(error))
        else:
            self.status_label.setText(f"{title}：{error}")
        self.status_label.show()
        if modal:
            _message(self, title, error)


__all__ = ["HealthRecordsPanel", "LifeRecordDialog", "ReminderEditorDialog"]
