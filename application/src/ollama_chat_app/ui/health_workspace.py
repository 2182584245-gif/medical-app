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
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..time_utils import as_beijing, beijing_today, display_timezone_label, format_beijing
from .member_appointment import AppointmentDialog
from .member_commerce import CommercePanel
from .member_statistics import ProfileStatisticsPage
from .member_today import TodayPage
from .offline_feedback import show_queued_result
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
    return f"{action}失败：{error}\n请检查填写内容后重试。"


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
        form.addRow(f"发生时间（{display_timezone_label()}）", self.time_input)
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
        layout.addRow(f"提醒时间（{display_timezone_label()}）", self.time_input)
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
            result = self.health_service.save_profile(self.user_id, values)
            if show_queued_result(result, self, label=self.status_label):
                return
            target = "云端" if getattr(self.health_service, "uses_network", False) else "本机"
            self._set_status(f"档案已保存到{target}。", False)
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


class ServicePage(QWidget):
    reminder_changed = Signal()

    def __init__(
        self,
        health_service: object,
        commerce_service: object | None = None,
        file_management_service: object | None = None,
        *,
        appointment_service: object | None = None,
        preferences_service: object | None = None,
    ) -> None:
        super().__init__()
        self.health_service = health_service
        self.appointment_service = appointment_service
        self.preferences_service = preferences_service
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
        summaries.addWidget(_card("会员顾问资料", self.advisor_label), 1)
        visit_card = QWidget()
        visit_layout = QVBoxLayout(visit_card)
        visit_layout.addWidget(self.visit_label)
        self.appointment_button = QPushButton("预约上门服务")
        self.appointment_button.setObjectName("PrimaryButton")
        self.appointment_button.clicked.connect(self._request_appointment)
        visit_layout.addWidget(self.appointment_button)
        summaries.addWidget(_card("下次上门", visit_card), 1)
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

        appointment_title = QLabel("我的预约")
        appointment_title.setObjectName("SectionTitle")
        care_layout.addWidget(appointment_title)
        self.appointment_list = QListWidget()
        care_layout.addWidget(self.appointment_list, 1)
        self.status_label = QLabel()
        self.status_label.setObjectName("HealthError")
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        care_layout.addWidget(self.status_label)

        self.commerce_panel = CommercePanel(commerce_service)
        self.section_tabs.addTab(care_tab, "会员服务")
        self.section_tabs.addTab(self.commerce_panel, "商品")
        root.addWidget(self.section_tabs, 1)

    def set_user(self, user_id: int | None) -> None:
        self.user_id = user_id
        self.commerce_panel.user_id = user_id
        self.refresh()

    def refresh(self) -> None:
        self.appointment_list.clear()
        self.visit_history_list.clear()
        if self.user_id is None:
            self.membership_label.setText("暂无会员信息")
            self.advisor_label.setText("暂未绑定顾问")
            self.visit_label.setText("暂无上门安排")
            self.visit_count_label.setText("已完成 0 次")
            self.status_label.hide()
            self.commerce_panel.refresh()
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
            self.appointment_button.setVisible(not bool(_value(summary, "next_visit", None)))
            self.appointment_button.setEnabled(self.appointment_service is not None)
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
            appointments = (
                self.appointment_service.list_for_member(self.user_id)
                if self.appointment_service
                else []
            )
            labels = {
                "pending": "预约中",
                "in_progress": "进行中",
                "completed": "已完成",
                "incomplete": "未完成",
                "disabled": "已停用",
            }
            for appointment in appointments:
                status = str(_value(appointment, "status", "pending"))
                service_type = _value(appointment, "service_type", "上门服务")
                text = (
                    f"{_display_time(_value(appointment, 'scheduled_at', ''))}  "
                    f"{labels.get(status, status)}  {service_type}\n"
                    f"{_value(appointment, 'address', '')}"
                )
                self.appointment_list.addItem(text)
            if not appointments:
                self.appointment_list.addItem("暂无预约请求，可选择期望时间与地址预约上门。")
            if any(_value(item, "status") in ("pending", "in_progress") for item in appointments):
                self.appointment_button.setVisible(False)
        except Exception as exc:
            errors.append(_service_error("读取预约", exc))
        self.status_label.setText("\n".join(errors))
        self.status_label.setVisible(bool(errors))
        self.commerce_panel.refresh()

    def refresh_snapshot(self, resources):
        """The care tab is mirrored; commerce deliberately remains independent."""
        if self.user_id is None or self.section_tabs.currentIndex() != 0:
            return ()
        from .snapshot_refresh import preserve_views

        summary, appointments = resources["service_summary"], resources["appointments"]
        self.membership_label.setText(self._membership_text(summary.get("membership")))
        self.advisor_label.setText(self._advisor_text(summary.get("advisor")))
        self.visit_label.setText(
            self._next_visit_text(summary.get("next_visit", summary.get("visit")))
        )
        pending = any(item.get("status") in {"pending", "in_progress"} for item in appointments)
        self.appointment_button.setVisible(not summary.get("next_visit") and not pending)
        self.appointment_button.setEnabled(self.appointment_service is not None)
        self.visit_count_label.setText(
            f"已完成 {int(summary.get('completed_visit_count') or 0)} 次"
        )
        labels = {
            "pending": "预约中",
            "in_progress": "进行中",
            "completed": "已完成",
            "incomplete": "未完成",
            "disabled": "已停用",
            "cancelled": "已停用",
        }
        with preserve_views(self.appointment_list, self.visit_history_list):
            self.appointment_list.clear()
            self.visit_history_list.clear()
            for appointment in appointments:
                status = str(appointment.get("status", "pending"))
                title = appointment.get("service_type") or appointment.get("title") or "上门服务"
                item = QListWidgetItem(
                    f"{_display_time(appointment.get('scheduled_at'))}  "
                    f"{labels.get(status, status)}  {title}\n"
                    f"{appointment.get('address') or ''}"
                )
                item.setData(Qt.ItemDataRole.UserRole, appointment)
                self.appointment_list.addItem(item)
            if not appointments:
                self.appointment_list.addItem("暂无预约请求，可选择期望时间与地址预约上门。")
            visits = _items(summary.get("visit_records", []))
            for visit in visits:
                item = QListWidgetItem(
                    f"{_display_time(visit.get('visited_at'))}　"
                    f"{visit.get('advisor_name') or '顾问未填写'}\n"
                    f"{visit.get('summary') or '上门服务'}"
                )
                item.setData(Qt.ItemDataRole.UserRole, visit)
                self.visit_history_list.addItem(item)
            if not visits:
                self.visit_history_list.addItem("尚无已完成的上门服务记录。")
        return ("会员服务", "上门预约")

    def _request_appointment(self) -> None:
        if self.user_id is None or self.appointment_service is None:
            return
        try:
            preferences = (
                self.preferences_service.get(self.user_id) if self.preferences_service else {}
            )
            dialog = AppointmentDialog(preferences, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            result = self.appointment_service.create_request(self.user_id, **dialog.values)
            if show_queued_result(result, self):
                return
            self.refresh()
            QMessageBox.information(
                self, "预约已提交", "运营端已收到预约请求，安排结果会显示在这里。"
            )
        except Exception as error:
            QMessageBox.warning(self, "预约失败", str(error))

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
        *,
        preferences_service: object | None = None,
        voice_service: object | None = None,
        ai_service: object | None = None,
        appointment_service: object | None = None,
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
        header_layout.addStretch(1)
        self.user_label = QLabel("未登录")
        header_layout.addWidget(self.user_label)
        self.import_button = QPushButton("迁移数据")
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
        navigation.setFixedWidth(196)
        nav_layout = QVBoxLayout(navigation)
        nav_layout.setContentsMargins(10, 14, 10, 14)
        nav_layout.setSpacing(9)

        self.stack = QStackedWidget()
        self.today_page = TodayPage(
            health_service,
            preferences_service=preferences_service,
            ai_service=ai_service,
            provider_selector=getattr(chat_page, "_selected_provider", None),
        )
        self.profile_editor = ProfilePage(health_service)
        self.profile_editor.hide()
        self.profile_page = ProfileStatisticsPage(health_service, self.profile_editor)
        from .my_platform import MyPlatformPanel

        self.my_platform_page = MyPlatformPanel(
            health_service, preferences_service=preferences_service
        )
        self.service_page = ServicePage(
            health_service,
            commerce_service,
            file_management_service,
            appointment_service=appointment_service,
            preferences_service=preferences_service,
        )
        pages = (
            ("今日记录", self.today_page),
            ("档案与统计", self.profile_page),
            ("服务", self.service_page),
            ("AI 助手", chat_page),
            ("我的平台", self.my_platform_page),
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

        self.today_page.record_changed.connect(self.profile_page.refresh)
        self.profile_page.record_changed.connect(self.today_page.refresh)
        self.profile_page.profile_changed.connect(self.today_page.refresh)
        self.service_page.reminder_changed.connect(self.today_page.refresh)
        self.my_platform_page.preferences_changed.connect(self._preferences_changed)
        if hasattr(chat_page, "logout_requested"):
            chat_page.logout_requested.connect(self.logout_requested)
        if hasattr(chat_page, "data_changed"):
            chat_page.data_changed.connect(self._refresh_member_data)
        self.show_page(0)
        self.setStyleSheet("""
            QFrame#HealthNavigation QPushButton {
                min-height: 48px; text-align: left; padding-left: 20px;
            }
            QLabel#CommerceNotice { padding: 12px; font-weight: 600; }
            QListWidget::item { padding: 10px; }
        """)

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
        self.profile_page.refresh()
        self.service_page.refresh()

    def refresh_snapshot(self, resources):
        """Only the visible read-only page may receive a periodic snapshot."""
        if self.current_user is None:
            return ()
        page = self.stack.currentWidget()
        if page not in (self.today_page, self.profile_page, self.service_page):
            return ()  # Never reset chat drafts, profile editors or My Platform.
        return page.refresh_snapshot(resources)

    def _preferences_changed(self, preferences: dict) -> None:
        self.user_label.setText(
            str(preferences.get("nickname") or getattr(self.current_user, "username", ""))
        )
        self.today_page._clock_tick()
        self.today_page.refresh()

    def start_session(self, user: Any) -> None:
        self.current_user = user
        self.user_label.setText(f"当前用户：{getattr(user, 'username', '')}")
        user_id = int(user.id)
        for page in (self.today_page, self.profile_page, self.service_page, self.my_platform_page):
            page.set_user(user_id)
        if hasattr(self.chat_page, "start_session"):
            self.chat_page.start_session(user)
        self.show_page(0)

    def end_session(self) -> None:
        self.current_user = None
        self.user_label.setText("未登录")
        for page in (self.today_page, self.profile_page, self.service_page, self.my_platform_page):
            page.set_user(None)
        if hasattr(self.chat_page, "end_session"):
            self.chat_page.end_session()
        self.show_page(0)


__all__ = [
    "HealthWorkspace",
    "TodayPage",
    "ProfilePage",
    "ServicePage",
    "CommercePanel",
]
