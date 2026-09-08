from __future__ import annotations

from datetime import timedelta

from PySide6.QtCore import Qt, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..services.member_weather import MemberWeatherService
from ..time_utils import as_beijing, beijing_today, display_timezone, format_beijing
from ..workers.task import FunctionTask
from .health_records_panel import CATEGORY_LABELS, LifeRecordDialog, ReminderEditorDialog
from .offline_feedback import show_queued_result


def _timeline():
    table = QTableWidget(0, 2)
    table.setHorizontalHeaderLabels(["时间", "内容"])
    table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setWordWrap(True)
    table.verticalHeader().hide()
    table.setMinimumHeight(180)
    return table


class SmartRecordDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("语音智能输入")
        self.resize(670, 430)
        root = QVBoxLayout(self)
        guide = QLabel(
            "说清楚吃了什么、吃多少，喝了多少水，运动或睡眠持续多久、在哪里。\n"
            "例如：今天中午在家吃了一碗米饭，喝水 300 毫升，下午在公园散步 30 分钟。"
        )
        guide.setWordWrap(True)
        root.addWidget(guide)
        self.text_input = QTextEdit()
        self.text_input.setPlaceholderText("可以先语音输入，也可以直接打字或修改识别文字。")
        root.addWidget(self.text_input)
        voice = QPushButton("开始语音输入")
        voice.clicked.connect(self.record_voice)
        root.addWidget(voice)
        notice = QLabel("点击智能填写会将这段文字发送给当前选择的 AI；生成结果需逐项确认才会保存。")
        notice.setWordWrap(True)
        root.addWidget(notice)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("智能填写")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._accept_text)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def record_voice(self):
        from .voice_input import VoiceTextDialog

        dialog = VoiceTextDialog(
            self, prompt="请说出饮食、饮水、运动或睡眠事实，包括数量、时长和地点。"
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            existing = self.text_input.toPlainText().strip()
            self.text_input.setPlainText((existing + "\n" + dialog.transcript_text).strip())

    def _accept_text(self):
        if not self.text_input.toPlainText().strip():
            QMessageBox.information(self, "请先输入", "请先说出或填写一段生活记录。")
            return
        self.accept()


class TodayPage(QWidget):
    record_changed = Signal()
    reminder_changed = Signal()

    def __init__(
        self,
        health_service,
        *,
        preferences_service=None,
        ai_service=None,
        provider_selector=None,
        weather_service=None,
        parent=None,
    ):
        super().__init__(parent)
        self.health_service = health_service
        self.preferences_service = preferences_service
        self.ai_service = ai_service
        self.provider_selector = provider_selector
        self.weather_service = weather_service or MemberWeatherService()
        self.user_id = None
        self._weather_key = None
        self._weather_serial = 0
        self._tasks = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.content_scroll = QScrollArea()
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        root = QVBoxLayout(content)
        root.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.content_scroll.setWidget(content)
        outer.addWidget(self.content_scroll)
        root.setSpacing(12)
        heading = QHBoxLayout()
        self.date_label = QLabel()
        self.date_label.setObjectName("PageTitle")
        heading.addWidget(self.date_label)
        heading.addStretch()
        self.weather_label = QLabel("天气不可用 · 请在我的平台设置地区")
        self.weather_label.setWordWrap(True)
        self.weather_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        heading.addWidget(self.weather_label)
        self.weather_refresh = QPushButton("刷新天气")
        self.weather_refresh.clicked.connect(lambda: self.refresh_weather(force=True))
        heading.addWidget(self.weather_refresh)
        root.addLayout(heading)
        quick = QFrame()
        quick.setObjectName("HealthCard")
        quick_layout = QVBoxLayout(quick)
        smart_row = QHBoxLayout()
        self.smart_button = QPushButton("语音智能输入")
        self.smart_button.setMinimumHeight(54)
        self.smart_button.setObjectName("PrimaryButton")
        self.smart_button.clicked.connect(self.smart_input)
        smart_row.addWidget(self.smart_button)
        guide = QLabel("说一说吃多少、喝多少、运动多久、在哪里，确认后记入今日。")
        guide.setWordWrap(True)
        smart_row.addWidget(guide, 1)
        quick_layout.addLayout(smart_row)
        quick_buttons = QHBoxLayout()
        for code, label in [
            ("diet", "饮食记录"),
            ("water", "饮水记录"),
            ("activity", "运动记录"),
            ("sleep", "睡眠记录"),
        ]:
            button = QPushButton(label)
            button.setMinimumHeight(52)
            button.clicked.connect(lambda _checked=False, category=code: self._add_record(category))
            quick_buttons.addWidget(button)
        quick_layout.addLayout(quick_buttons)
        root.addWidget(quick)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        root.addWidget(self.status_label)
        reminder_row = QHBoxLayout()
        title = QLabel("今日提醒")
        title.setObjectName("SectionTitle")
        reminder_row.addWidget(title)
        reminder_row.addStretch()
        for label, callback in [
            ("新增提醒", self.add_reminder),
            ("编辑", self.edit_reminder),
            ("暂停 / 恢复", self.toggle_reminder),
            ("删除", self.delete_reminder),
        ]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            reminder_row.addWidget(button)
        root.addLayout(reminder_row)
        self.reminders_table = _timeline()
        root.addWidget(self.reminders_table, 1)
        records_row = QHBoxLayout()
        records_title = QLabel("今日综合记录")
        records_title.setObjectName("SectionTitle")
        records_row.addWidget(records_title)
        records_row.addStretch()
        environment = QPushButton("记录环境")
        environment.clicked.connect(lambda: self._add_record("environment"))
        records_row.addWidget(environment)
        delete_record = QPushButton("删除所选记录")
        delete_record.clicked.connect(self.delete_record)
        records_row.addWidget(delete_record)
        root.addLayout(records_row)
        self.records_table = _timeline()
        root.addWidget(self.records_table, 2)
        self.timer = QTimer(self)
        self.timer.setInterval(60_000)
        self.timer.timeout.connect(self._clock_tick)
        self.timer.start()
        self._clock_tick()

    def _clock_tick(self):
        today = beijing_today()
        self.date_label.setText(
            f"{today:%Y 年 %m 月 %d 日}  星期{'一二三四五六日'[today.weekday()]}"
        )
        if getattr(self, "_today", None) != today:
            self._today = today
            self.refresh()

    def set_user(self, user_id):
        self.user_id = user_id
        self._weather_key = None
        self._weather_serial += 1
        self.refresh()

    def refresh(self, *_args):
        self.records_table.setRowCount(0)
        self.reminders_table.setRowCount(0)
        if self.user_id is None:
            self.weather_label.setText("天气不可用 · 请先登录并设置地区")
            return
        try:
            today = beijing_today()
            records = self.health_service.list_life_records(
                self.user_id,
                start_date=today - timedelta(days=1),
                end_date=today + timedelta(days=1),
                limit=10_000,
            )
            records = [
                item
                for item in records
                if as_beijing(item["occurred_at"]).astimezone(display_timezone()).date() == today
            ]
            reminders = [
                item
                for item in self.health_service.list_reminders(self.user_id)
                if as_beijing(item["scheduled_at"]).astimezone(display_timezone()).date() == today
            ]
            self._fill(self.records_table, records, record=True)
            self._fill(self.reminders_table, reminders, record=False)
            self.status_label.hide()
        except Exception as error:
            self._error(f"读取今日记录失败：{error}")
        self.refresh_weather()

    def _fill(self, table, items, *, record):
        table.clearContents()
        table.setRowCount(max(1, len(items)))
        if not items:
            table.setItem(0, 0, QTableWidgetItem("—"))
            table.setItem(
                0, 1, QTableWidgetItem("今天还没有生活记录" if record else "今天没有提醒")
            )
            return
        for row, item in enumerate(
            sorted(items, key=lambda x: str(x.get("occurred_at" if record else "scheduled_at")))
        ):
            stamp = item.get("occurred_at" if record else "scheduled_at")
            time_cell = QTableWidgetItem(format_beijing(stamp)[-5:])
            time_cell.setData(Qt.ItemDataRole.UserRole, dict(item))
            table.setItem(row, 0, time_cell)
            if record:
                detail = item.get("details") or {}
                labels = {
                    "amount_ml": "毫升",
                    "duration_minutes": "分钟",
                    "calories_kcal": "千卡",
                    "energy_kcal": "千卡耗能",
                    "steps": "步",
                    "duration_hours": "小时",
                    "amount_g": "克",
                    "temperature_c": "℃",
                    "humidity_percent": "%",
                }
                suffix = " · ".join(
                    f"{detail[key]} {unit}" for key, unit in labels.items() if key in detail
                )
                category = CATEGORY_LABELS.get(item.get("category"), "生活")
                text = f"{category}  {item.get('content', '')}"
                if suffix:
                    text += f"\n{suffix}"
            else:
                text = str(item.get("title", "")) + (
                    "  · 已暂停" if not item.get("enabled", True) else ""
                )
            table.setItem(row, 1, QTableWidgetItem(text))
        table.resizeRowsToContents()

    def refresh_snapshot(self, resources):
        """Update timelines only: no weather, settings lookup or editor changes."""
        if self.user_id is None:
            return ()
        from .snapshot_refresh import preserve_views

        today = beijing_today()
        records = [
            item
            for item in resources["life_records"]
            if as_beijing(item["occurred_at"]).astimezone(display_timezone()).date() == today
        ]
        reminders = [
            item
            for item in resources["reminders"]
            if as_beijing(item["scheduled_at"]).astimezone(display_timezone()).date() == today
        ]
        with preserve_views(self.records_table, self.reminders_table):
            self._fill(self.records_table, records, record=True)
            self._fill(self.reminders_table, reminders, record=False)
        return ("今日记录", "今日提醒")

    def refresh_weather(self, force=False):
        if self.user_id is None or self.preferences_service is None:
            return
        try:
            preferences = self.preferences_service.get(self.user_id)
            if not preferences.get("weather_consent", False):
                self._weather_key = None
                self._weather_serial += 1
                self.weather_label.setText("天气未开启 · 请在我的平台确认地区与天气联网")
                return
            location = {key: preferences.get(key) for key in ("city", "latitude", "longitude")}
        except Exception as error:
            self.weather_label.setText(f"天气不可用 · 地区设置读取失败：{error}")
            return
        key = tuple(location.values())
        if key == self._weather_key and not force:
            return
        self._weather_key = key
        self._weather_serial += 1
        serial, user = self._weather_serial, self.user_id
        if not location["city"] and (location["latitude"] is None or location["longitude"] is None):
            self.weather_label.setText("天气不可用 · 请在我的平台设置地区")
            return
        self.weather_label.setText(f"{location['city'] or '所选地区'} · 正在查询天气…")
        task = FunctionTask(self.weather_service.current, location)

        def done(result):
            if serial == self._weather_serial and user == self.user_id:
                self.weather_label.setText(
                    f"{result['city']}  {result['condition']}  {result['temperature_c']} ℃"
                )
                self.weather_label.setToolTip(
                    f"来源：Open-Meteo · 数据时间：{result['observed_at']}"
                )

        def failed(error):
            if serial == self._weather_serial and user == self.user_id:
                self.weather_label.setText("天气不可用 · 请稍后刷新")
                self.weather_label.setToolTip(str(error))

        self._run(task, done, failed)

    def _run(self, task, success, failure):
        self._tasks.append(task)
        task.signals.result.connect(success)
        task.signals.error.connect(failure)
        task.signals.finished.connect(
            lambda: self._tasks.remove(task) if task in self._tasks else None
        )
        QThreadPool.globalInstance().start(task)

    def smart_input(self):
        if self.user_id is None:
            return
        if self.ai_service is None or self.provider_selector is None:
            QMessageBox.information(
                self, "智能填写尚未连接", "请先在 AI 助手选择可用服务；也可使用下方四个记录按钮。"
            )
            return
        dialog = SmartRecordDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            selected_provider = self.provider_selector()
            if selected_provider is None:
                self._error("请先在 AI 助手配置并选择可用服务。")
                return
            provider, provider_name, model = selected_provider
            user_id = self.user_id
            task = FunctionTask(
                self.ai_service.propose_life_records,
                user_id,
                provider,
                provider_name,
                model,
                dialog.text_input.toPlainText().strip(),
            )
            self.smart_button.setEnabled(False)
            self.smart_button.setText("正在智能填写…")
            task.signals.finished.connect(lambda: self.smart_button.setEnabled(True))
            task.signals.finished.connect(lambda: self.smart_button.setText("语音智能输入"))
            self._run(
                task,
                lambda proposals: self._confirm_proposals(user_id, proposals),
                lambda error: self._error(f"智能填写失败：{error}。可以继续手动填写记录。"),
            )
        except Exception as error:
            self._error(str(error))

    def _confirm_proposals(self, user_id, proposals):
        if user_id != self.user_id:
            return
        if not proposals:
            self._error("没有识别到可填写的生活记录，请补充数量、时长或地点后重试。")
            return
        queued = False
        for proposal in proposals:
            dialog = LifeRecordDialog(proposal.get("category", "diet"), self)
            dialog.set_proposal(proposal)
            if dialog.exec() == QDialog.DialogCode.Accepted and self.user_id == user_id:
                try:
                    result = self.health_service.add_life_record(
                        user_id, **dialog.values, source="ai_confirmed"
                    )
                    if show_queued_result(result, self, label=self.status_label):
                        queued = True
                        continue
                    self.record_changed.emit()
                except Exception as error:
                    self._error(f"保存失败：{error}")
        if not queued:
            self.refresh()

    def _add_record(self, category):
        if self.user_id is None:
            return
        dialog = LifeRecordDialog(category, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._mutate(
                lambda: self.health_service.add_life_record(self.user_id, **dialog.values), True
            )

    @staticmethod
    def _selected(table):
        item = table.item(table.currentRow(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _mutate(self, action, record=False):
        try:
            result = action()
            if show_queued_result(result, self, label=self.status_label):
                return
            self.refresh()
            (self.record_changed if record else self.reminder_changed).emit()
        except Exception as error:
            self._error(f"保存失败：{error}")

    def add_reminder(self):
        if self.user_id is None:
            return
        dialog = ReminderEditorDialog(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._mutate(lambda: self.health_service.add_reminder(self.user_id, **dialog.values))

    def edit_reminder(self):
        if selected := self._selected(self.reminders_table):
            dialog = ReminderEditorDialog(selected, self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self._mutate(
                    lambda: self.health_service.update_reminder(
                        self.user_id, selected["id"], **dialog.values
                    )
                )

    def toggle_reminder(self):
        if selected := self._selected(self.reminders_table):
            self._mutate(
                lambda: self.health_service.toggle_reminder(
                    self.user_id, selected["id"], not selected["enabled"]
                )
            )

    def delete_reminder(self):
        selected = self._selected(self.reminders_table)
        if (
            selected
            and QMessageBox.question(self, "删除提醒", "确认删除选中的提醒吗？")
            == QMessageBox.StandardButton.Yes
        ):
            self._mutate(lambda: self.health_service.delete_reminder(self.user_id, selected["id"]))

    def delete_record(self):
        selected = self._selected(self.records_table)
        if (
            selected
            and QMessageBox.question(self, "删除记录", "确认删除选中的记录吗？")
            == QMessageBox.StandardButton.Yes
        ):
            self._mutate(
                lambda: self.health_service.delete_life_record(self.user_id, selected["id"]), True
            )

    def _error(self, text):
        self.status_label.setObjectName("HealthError")
        self.status_label.setText(text)
        self.status_label.show()
