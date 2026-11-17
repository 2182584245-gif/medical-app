from __future__ import annotations

from datetime import timedelta
import random

from PySide6.QtCore import QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QProgressBar,
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
from ..time_utils import as_beijing, beijing_now, beijing_today, display_timezone, format_beijing
from ..workers.task import FunctionTask
from .art_icons import rounded_icon
from .health_records_panel import (
    CATEGORIES,
    CATEGORY_LABELS,
    LifeRecordDialog,
    ReminderEditorDialog,
)
from .offline_feedback import show_queued_result
from .selection_actions import deletion_prompt, selected_rows
from .theme import RECORD_TONES


def _timeline():
    table = QTableWidget(0, 2)
    table.setHorizontalHeaderLabels(["时间", "内容"])
    table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
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
            "说清楚时间、事项、数量与地点；饮食、饮水、运动、睡眠、环境和医疗都可记录。\n"
            "例如：中午喝水 300 毫升；室温 24 度；上午到社区门诊复查。医疗只记录已发生事实。"
        )
        guide.setWordWrap(True)
        root.addWidget(guide)
        self.text_input = QTextEdit()
        self.text_input.setPlaceholderText("可以先语音输入，也可以直接打字或修改识别文字。")
        root.addWidget(self.text_input)
        voice = QPushButton()
        voice.setIcon(rounded_icon("mic", size=30, color="#536546", background="#edf4ea"))
        voice.setIconSize(QSize(30, 30))
        voice.setToolTip("点击麦克风开始语音输入")
        voice.setAccessibleName("麦克风语音输入")
        voice.setFixedWidth(68)
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
            self,
            prompt="请说出生活、环境或既有医疗事实，包括时间、名称、数量和地点；不生成用药决策。",
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
        cloud_client=None,
        parent=None,
    ):
        super().__init__(parent)
        self.health_service = health_service
        self.preferences_service = preferences_service
        self.ai_service = ai_service
        self.provider_selector = provider_selector
        self.cloud_client = cloud_client
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
        heading.setSpacing(14)
        self.today_greeting = QLabel("今天也要好好照顾自己")
        self.today_greeting.setObjectName("TodayGreeting")
        heading.addWidget(self.today_greeting)
        self.date_label = QLabel()
        self.date_label.setObjectName("PageTitle")
        heading.addWidget(self.date_label)
        heading.addStretch()
        self.weather_label = QLabel("天气不可用 · 请在我的平台设置地区")
        self.weather_label.setObjectName("TodayWeather")
        self.weather_label.setWordWrap(True)
        self.weather_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        heading.addWidget(self.weather_label)
        self.weather_refresh = QPushButton("刷新天气")
        self.weather_refresh.clicked.connect(lambda: self.refresh_weather(force=True))
        self.weather_refresh.hide()
        root.addLayout(heading)

        self.reminder_banner = QFrame()
        self.reminder_banner.setObjectName("TodayReminderBanner")
        self.reminder_banner.setProperty("warmTone", "reminder")
        reminder_layout = QHBoxLayout(self.reminder_banner)
        self.reminder_banner_text = QLabel("正在检查今天还需要填写什么…")
        self.reminder_banner_text.setObjectName("WarmReminderText")
        self.reminder_banner_text.setWordWrap(True)
        reminder_layout.addWidget(self.reminder_banner_text, 1)
        self.reminder_fill_button = QPushButton("去填写")
        self.reminder_fill_button.setObjectName("WarmReminderAction")
        self.reminder_fill_button.clicked.connect(self._fill_next_reminder)
        reminder_layout.addWidget(self.reminder_fill_button)
        root.addWidget(self.reminder_banner)

        self.reading_card = QFrame()
        self.reading_card.setObjectName("DailyReadingCard")
        self.reading_card.setProperty("warmTone", "reading")
        reading_layout = QVBoxLayout(self.reading_card)
        reading_head = QHBoxLayout()
        self.reading_topic = QLabel("今日主题")
        self.reading_topic.setObjectName("WarmReadingTopic")
        reading_head.addWidget(self.reading_topic)
        reading_head.addStretch(1)
        self.reading_go_button = QPushButton("去阅读")
        self.reading_go_button.setObjectName("WarmSecondaryAction")
        self.reading_go_button.clicked.connect(self._open_reading)
        reading_head.addWidget(self.reading_go_button)
        reading_layout.addLayout(reading_head)
        self.reading_title = QLabel("每日阅读")
        self.reading_title.setObjectName("WarmReadingTitle")
        self.reading_title.setWordWrap(True)
        reading_layout.addWidget(self.reading_title)
        self.reading_summary = QLabel("每天读一点可靠资料，让生活调整更有依据。")
        self.reading_summary.setObjectName("WarmReadingSummary")
        self.reading_summary.setWordWrap(True)
        reading_layout.addWidget(self.reading_summary)
        root.addWidget(self.reading_card)
        quick = QFrame()
        quick.setObjectName("VoiceAndRecordsCard")
        quick.setProperty("warmTone", "voice")
        quick_layout = QVBoxLayout(quick)
        smart_row = QHBoxLayout()
        self.smart_button = QPushButton("语音智能输入")
        self.smart_button.setMinimumHeight(54)
        self.smart_button.setObjectName("WarmVoiceButton")
        self.smart_button.setIcon(rounded_icon("mic"))
        self.smart_button.setIconSize(QSize(32, 32))
        self.smart_button.clicked.connect(self.smart_input)
        smart_row.addWidget(self.smart_button)
        guide = QLabel("六类事实均可填写；说出时间、事项、数量和地点，逐项确认后保存。")
        guide.setObjectName("WarmVoiceSubtitle")
        guide.setWordWrap(True)
        smart_row.addWidget(guide, 1)
        quick_layout.addLayout(smart_row)
        quick_buttons = QGridLayout()
        display_categories = (
            ("diet", "饮食"), ("activity", "运动"), ("sleep", "睡眠"),
            ("medical", "医疗"), ("environment", "居住环境"), ("smart", "语音记录"),
        )
        for index, (code, label) in enumerate(display_categories):
            button = QPushButton(label + "记录")
            button.setObjectName("RecordCategory_" + code)
            button.setProperty("recordTone", code)
            tone = "diet" if code == "smart" else code
            surface, ink, _edge, _active = RECORD_TONES[tone]
            button.setIcon(rounded_icon(code, size=36, color=ink, background=surface))
            button.setIconSize(QSize(36, 36))
            button.setMinimumHeight(52)
            button.setProperty("warmStyle", "record")
            button.clicked.connect(
                lambda _checked=False, category=code: self.smart_input()
                if category == "smart" else self._add_record(category)
            )
            quick_buttons.addWidget(button, index // 3, index % 3)
        # Keep a non-visible compatibility entry for older automation and
        # accessibility tooling.  Water logging is intentionally merged into
        # the diet editor in the current product flow, so this alias never
        # appears in the member-facing grid.
        water_alias = QPushButton("饮水记录")
        water_alias.setObjectName("RecordCategory_water")
        water_alias.setProperty("recordTone", "water")
        surface, ink, _edge, _active = RECORD_TONES["water"]
        water_alias.setIcon(rounded_icon("water", size=36, color=ink, background=surface))
        water_alias.setIconSize(QSize(36, 36))
        water_alias.setAccessibleName("饮水记录（已合并到饮食记录）")
        water_alias.hide()
        quick_layout.addWidget(water_alias)
        quick_layout.addLayout(quick_buttons)
        root.addWidget(quick)
        self.quick_hint = guide
        self.quick_hint.setText("说出今天做过的事情，系统自动记录；也可以点下方图标手动填写。")
        self.stats_button = QPushButton("查看我的统计")
        self.stats_button.setObjectName("WarmStatsButton")
        self.stats_button.clicked.connect(self._show_statistics)
        root.addWidget(self.stats_button)

        # A compact, always-visible timeline is the visual anchor of the
        # reference design.  The legacy tables below remain available for
        # compatibility but are intentionally hidden.
        self.today_timeline_card = QFrame()
        self.today_timeline_card.setObjectName("TodayTimelineCard")
        timeline_layout = QVBoxLayout(self.today_timeline_card)
        timeline_heading = QHBoxLayout()
        timeline_title = QLabel("今日动态")
        timeline_title.setObjectName("SectionTitle")
        timeline_heading.addWidget(timeline_title)
        timeline_heading.addStretch(1)
        timeline_hint = QLabel("记录会按时间自动排列")
        timeline_hint.setObjectName("Hint")
        timeline_heading.addWidget(timeline_hint)
        timeline_layout.addLayout(timeline_heading)
        self.today_timeline_table = _timeline()
        self.today_timeline_table.setObjectName("TodayTimelineTable")
        self.today_timeline_table.setMinimumHeight(145)
        timeline_layout.addWidget(self.today_timeline_table)
        root.addWidget(self.today_timeline_card)
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
        # Legacy reminder controls remain available to the data service but are
        # kept out of the compact Today view.
        self.reminders_table = _timeline()
        root.addWidget(self.reminders_table, 1)
        reminder_row_widget = QWidget()
        reminder_row_widget.setLayout(reminder_row)
        root.addWidget(reminder_row_widget)
        reminder_row_widget.hide()
        self.reminders_table.hide()
        records_row = QHBoxLayout()
        records_title = QLabel("今日综合记录")
        records_title.setObjectName("SectionTitle")
        records_row.addWidget(records_title)
        records_row.addStretch()
        delete_record = QPushButton("删除所选记录")
        delete_record.clicked.connect(self.delete_record)
        records_row.addWidget(delete_record)
        # The full timeline is available from the statistics/history view.
        self.records_table = _timeline()
        root.addWidget(self.records_table, 2)
        records_row_widget = QWidget()
        records_row_widget.setLayout(records_row)
        root.addWidget(records_row_widget)
        records_row_widget.hide()
        self.records_table.hide()
        legacy_hint = QLabel("记录和提醒可按 Ctrl 多选、Shift 连选；删除前会显示已选条数。")
        legacy_hint.hide()
        root.addWidget(legacy_hint)
        self.timer = QTimer(self)
        self.timer.setInterval(60_000)
        self.timer.timeout.connect(self._clock_tick)
        self.timer.start()
        self.reading_timer = QTimer(self)
        self.reading_timer.setInterval(30 * 60 * 1000)
        self.reading_timer.timeout.connect(self._refresh_reading)
        self.reading_timer.start()
        self._clock_tick()

    def _clock_tick(self):
        today = beijing_today()
        now = beijing_now()
        self.date_label.setText(
            f"{today:%m 月 %d 日}  星期{'一二三四五六日'[today.weekday()]}  {now:%H:%M}"
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
            self._fill(self.today_timeline_table, records, record=True)
            self._fill(self.reminders_table, reminders, record=False)
            self.status_label.hide()
        except Exception as error:
            self._error(f"读取今日记录失败：{error}")
        self.refresh_weather()
        self._refresh_reading()
        self._update_reminder_banner(records)

    def _update_reminder_banner(self, records):
        done = {str(item.get("category")) for item in records}
        pending = [(code, label) for code, label in (
            ("diet", "饮食和饮水"), ("activity", "运动"), ("sleep", "睡眠"),
            ("medical", "医疗"), ("environment", "居住环境")) if code not in done]
        self._pending_record_categories = pending
        if pending:
            self.reminder_banner_text.setText(f"今天还可以记录：{pending[0][1]}，写下来更容易看见自己的变化。")
            self.reminder_fill_button.show()
        else:
            self.reminder_banner_text.setText("恭喜写完今天的记录，祝您天天健康、心情舒畅。")
            self.reminder_fill_button.hide()

    def _fill_next_reminder(self):
        pending = getattr(self, "_pending_record_categories", [])
        if pending:
            self._add_record(pending[0][0])

    def _refresh_reading(self):
        if self.user_id is None:
            return
        try:
            if self.cloud_client is not None:
                from ..services.cloud_library import CloudKnowledgeLibrary
                docs = CloudKnowledgeLibrary(self.cloud_client).catalog("knowledge", limit=50)
            else:
                docs = []
            if docs:
                item = random.choice(docs)
                self._reading_item = item
                self.reading_topic.setText(str(item.get("topic") or "健康生活"))
                self.reading_title.setText(str(item.get("title") or "每日阅读"))
                self.reading_summary.setText(str(item.get("summary") or "打开资料卡查看这篇内容。"))
            else:
                self._reading_item = None
                self.reading_topic.setText("健康生活")
                self.reading_title.setText("每日阅读")
                self.reading_summary.setText("每天读一点可靠资料，让生活调整更有依据。")
        except Exception:
            self._reading_item = None

    def _open_reading(self):
        item = getattr(self, "_reading_item", None)
        if not item:
            QMessageBox.information(self, "每日阅读", "今日资料正在准备，请稍后再试。")
            return
        title = str(item.get("title") or "每日阅读")
        summary = str(item.get("summary") or "")
        text = summary
        # The catalog is only the index.  When the member chooses “去阅读”,
        # open the bounded cloud card and (when available) its original source
        # so the reading action actually reaches the selected article.
        try:
            from ..services.cloud_library import CloudKnowledgeLibrary

            if self.cloud_client is not None:
                library = CloudKnowledgeLibrary(self.cloud_client)
                document_id = str(item.get("document_id") or "")
                card = library.open_knowledge_card(document_id)
                text = str(card.get("text") or text)
                if not text.strip():
                    source = library.open_source(document_id)
                    text = str(source.get("text") or text)
        except Exception:
            # A short catalog summary is still useful when the server is
            # temporarily unavailable; do not turn a reading click into an
            # opaque technical error.
            text = summary or "今日资料暂时无法打开，请稍后再试。"

        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(860, 640)
        layout = QVBoxLayout(dialog)
        heading = QLabel(summary)
        heading.setWordWrap(True)
        layout.addWidget(heading)
        body = QTextEdit()
        body.setReadOnly(True)
        body.setPlainText(text)
        layout.addWidget(body, 1)
        close = QPushButton("关闭")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec()

    def _show_statistics(self):
        if self.user_id is None:
            return
        from .member_statistics import MemberStatisticsDialog

        dialog = MemberStatisticsDialog(
            self.health_service,
            ai_service=self.ai_service,
            provider_selector=self.provider_selector,
            user_id=self.user_id,
            parent=self,
        )
        dialog.generate()
        dialog.exec()

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
                if item.get("category") == "medical":
                    suffix = " · ".join(
                        str(detail[key]) for key in ("name", "description") if detail.get(key)
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
        self._fill(self.today_timeline_table, records, record=True)
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
                    f"📍 {result['city']}  ☀ {result['temperature_c']} ℃\n{result['condition']}"
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
                self, "智能填写尚未连接", "请先在 AI 助手选择可用服务；也可使用下方六类记录按钮。"
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
        selected = selected_rows(self.reminders_table)
        if (
            selected
            and QMessageBox.question(self, "删除提醒", deletion_prompt(selected, "提醒"))
            == QMessageBox.StandardButton.Yes
        ):
            self._mutate(
                lambda: self.health_service.delete_reminders(
                    self.user_id, [item["id"] for item in selected]
                )
            )

    def delete_record(self):
        selected = selected_rows(self.records_table)
        if (
            selected
            and QMessageBox.question(self, "删除记录", deletion_prompt(selected, "记录"))
            == QMessageBox.StandardButton.Yes
        ):
            self._mutate(
                lambda: self.health_service.delete_life_records(
                    self.user_id, [item["id"] for item in selected]
                ),
                True,
            )

    def _error(self, text):
        self.status_label.setObjectName("HealthError")
        self.status_label.setText(text)
        self.status_label.show()
