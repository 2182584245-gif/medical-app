from __future__ import annotations

from datetime import timedelta

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QProgressBar,
    QGroupBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..services.member_statistics import PARAMETERS, daily_series
from ..time_utils import beijing_today
from .health_records_panel import CATEGORIES, LifeRecordDialog
from .offline_feedback import show_queued_result
from ..workers.task import FunctionTask


class MemberStatisticsDialog(QDialog):
    """Cloud-backed member statistics with an optional DeepSeek explanation.

    Charts are computed only from confirmed records returned by
    ``health_service``. The model receives aggregate values and must not fill
    missing observations, diagnose, or prescribe. Network work is run in the
    Qt thread pool so the member window remains responsive.
    """

    def __init__(self, health_service, ai_service=None, provider_selector=None,
                 user_id=None, parent=None):
        super().__init__(parent)
        self.health_service = health_service
        self.ai_service = ai_service
        self.provider_selector = provider_selector
        self.user_id = user_id
        self._task = None
        self._series = []
        self._unit = ""
        self.setWindowTitle("我的统计")
        self.resize(920, 700)
        self.setObjectName("MemberStatisticsDialog")
        root = QVBoxLayout(self)

        heading = QHBoxLayout()
        title = QLabel("我的统计")
        title.setObjectName("PageTitle")
        heading.addWidget(title)
        subtitle = QLabel("把每天的小记录，慢慢变成看得见的变化")
        subtitle.setObjectName("StatisticsSubtitle")
        heading.addWidget(subtitle)
        heading.addStretch()
        self.period_combo = QComboBox()
        self.period_combo.addItem("近 7 天", 7)
        self.period_combo.addItem("近 30 天", 30)
        heading.addWidget(QLabel("时间"))
        heading.addWidget(self.period_combo)
        root.addLayout(heading)

        filters = QHBoxLayout()
        self.category_combo = QComboBox()
        # 饮水与饮食在用户界面合并，但保留水量参数以免丢失事实。
        for category, label in (("diet", "饮食"), ("water", "饮水"),
                                ("activity", "运动"), ("sleep", "睡眠")):
            self.category_combo.addItem(label, category)
        self.parameter_combo = QComboBox()
        self.view_combo = QComboBox()
        for label, code in (("表格", "table"), ("条形图", "bar"),
                            ("折线图", "line"), ("饼图", "pie")):
            self.view_combo.addItem(label, code)
        for label, widget in (("类别", self.category_combo),
                              ("参数", self.parameter_combo),
                              ("展示", self.view_combo)):
            filters.addWidget(QLabel(label))
            filters.addWidget(widget)
        filters.addStretch()
        root.addLayout(filters)

        self.progress = QProgressBar()
        self.progress.setObjectName("StatisticsProgress")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("等待生成统计报告")
        root.addWidget(self.progress)

        self.chart = RecordChart()
        self.chart.setObjectName("StatisticsChart")
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["日期", "数值", "样本数"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setObjectName("StatisticsTable")
        self.views = QStackedWidget()
        self.views.addWidget(self.table)
        self.views.addWidget(self.chart)
        root.addWidget(self.views, 1)

        report_group = QGroupBox("AI 统计说明")
        report_group.setObjectName("StatisticsReportCard")
        report_layout = QVBoxLayout(report_group)
        self.report_label = QLabel("点击“生成统计报告”后，AI 会根据已填写的数据进行说明。")
        self.report_label.setObjectName("StatisticsReportText")
        self.report_label.setWordWrap(True)
        report_layout.addWidget(self.report_label)
        root.addWidget(report_group)

        self.status_label = QLabel()
        self.status_label.setObjectName("StatisticsStatus")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)
        actions = QHBoxLayout()
        self.generate_button = QPushButton("生成统计报告")
        self.generate_button.setObjectName("PrimaryButton")
        self.generate_button.clicked.connect(self.generate)
        actions.addWidget(self.generate_button)
        actions.addStretch()
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        root.addLayout(actions)

        self.category_combo.currentIndexChanged.connect(self._parameters)
        self.parameter_combo.currentIndexChanged.connect(self._render_current)
        self.period_combo.currentIndexChanged.connect(self.generate)
        self.view_combo.currentIndexChanged.connect(self._render_current)
        self._parameters()

    def set_user(self, user_id):
        self.user_id = user_id

    def _parameters(self):
        category = self.category_combo.currentData()
        self.parameter_combo.blockSignals(True)
        self.parameter_combo.clear()
        for key, label, unit, _method in PARAMETERS.get(category, []):
            self.parameter_combo.addItem(f"{label}（{unit}）", key)
        self.parameter_combo.blockSignals(False)
        self._render_current()

    def _render_current(self):
        if not self._series:
            return
        self.table.setRowCount(len(self._series))
        for row, point in enumerate(self._series):
            value = "未填写" if point["value"] is None else f"{point['value']:g} {self._unit}"
            for col, text in enumerate((point["date"], value, str(point["samples"]))):
                self.table.setItem(row, col, QTableWidgetItem(text))
        kind = self.view_combo.currentData()
        self.views.setCurrentIndex(0 if kind == "table" else 1)
        self.chart.set_data(self._series, kind, self._unit)

    @staticmethod
    def _aggregate(records, days, start_date=None):
        totals = {}
        numeric = 0
        for record in records:
            category = str(record.get("category", ""))
            totals[category] = totals.get(category, 0) + 1
            for value in (record.get("details") or {}).values():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    numeric += 1
        payload = {"period_days": days, "record_count": len(records),
                   "category_counts": totals, "numeric_observation_count": numeric}
        # Include only measured daily series.  Missing dates remain null so the
        # model cannot mistake an unfilled day for a zero-value measurement.
        if start_date is not None:
            daily_metrics = {}
            for category, parameter in (
                ("diet", "calories_kcal"), ("water", "amount_ml"),
                ("activity", "duration_minutes"), ("activity", "energy_kcal"),
                ("sleep", "duration_hours"),
            ):
                try:
                    daily_metrics[f"{category}.{parameter}"] = daily_series(
                        records, category, parameter, start_date, days
                    )
                except (KeyError, ValueError):
                    # A schema without a parameter is not evidence that it is
                    # zero; leave it out of the AI context.
                    continue
            payload["daily_metrics"] = daily_metrics
        return payload

    def _ai_explanation(self, aggregate, selection=None):
        # ``provider_selector`` usually reads Qt controls. Resolve it on the
        # GUI thread in ``generate`` and pass its immutable tuple to the worker.
        # This keeps all cloud/network work out of the UI thread without
        # touching widgets from a worker thread.
        if not selection:
            return "统计已完成，但当前没有可用的云端 AI 服务，暂不生成建议。"
        provider, _provider_name, model = selection
        prompt = (
            "请用温和、适合中老年人的中文解释下面已记录的生活数据。"
            "只能描述确实存在的事实；不能补写缺失数值、诊断疾病或开药。"
            "先说观察到的趋势，再给不超过三条低风险生活建议，并提醒必要时就医。\n"
            f"数据：{aggregate}"
        )
        if self.ai_service is not None and callable(
                getattr(self.ai_service, "statistics_report", None)):
            return str(self.ai_service.statistics_report(
                self.user_id, provider, model, aggregate))
        return str(provider.chat(model, [{"role": "user", "content": prompt}]))

    def _load(self, selection=None):
        days = int(self.period_combo.currentData())
        end = beijing_today()
        start = end - timedelta(days=days - 1)
        records = self.health_service.list_life_records(
            self.user_id, start_date=start, end_date=end, limit=10_000)
        category = self.category_combo.currentData()
        parameter = self.parameter_combo.currentData()
        series = daily_series(records, category, parameter, start, days)
        definition = next(item for item in PARAMETERS[category] if item[0] == parameter)
        report = self._ai_explanation(
            self._aggregate(records, days, start_date=start), selection
        )
        return series, definition[2], report, len(records)

    def generate(self):
        if self.user_id is None:
            self.status_label.setText("请先登录后再查看统计。")
            return
        if self._task is not None:
            return
        self.generate_button.setEnabled(False)
        self.progress.setValue(5)
        self.progress.setFormat("正在读取云端记录…")
        self.status_label.setText("正在读取已确认的饮食、运动和睡眠记录。")
        selection = None
        if callable(self.provider_selector):
            try:
                selection = self.provider_selector()
            except Exception as error:
                self.status_label.setText(f"云端 AI 暂时不可用，将只显示图表：{error}")
        task = FunctionTask(self._load, selection)
        self._task = task
        task.signals.result.connect(self._loaded)
        task.signals.error.connect(self._failed)
        task.signals.finished.connect(self._finished)
        from PySide6.QtCore import QThreadPool
        QThreadPool.globalInstance().start(task)

    def _loaded(self, result):
        series, unit, report, count = result
        self._series, self._unit = series, unit
        self.progress.setValue(65)
        self.progress.setFormat("正在生成 AI 统计说明…")
        self._render_current()
        self.report_label.setText(report)
        self.progress.setValue(100)
        self.progress.setFormat("统计报告已生成")
        self.status_label.setText(
            f"已读取 {count} 条云端记录；空白日期表示未填写，未用零值替代。")

    def _failed(self, error):
        self.progress.setValue(0)
        self.progress.setFormat("统计生成失败")
        self.status_label.setText(f"统计暂时无法生成：{error}")

    def _finished(self):
        self._task = None
        self.generate_button.setEnabled(True)


class RecordChart(QWidget):
    """A local painter chart, including explicit gaps for unrecorded values."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(290)
        self.series = []
        self.kind = "bar"
        self.unit = ""

    def set_data(self, series, kind, unit):
        self.series, self.kind, self.unit = series, kind, unit
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#fffcf5"))
        values = [point["value"] for point in self.series if point["value"] is not None]
        area = QRectF(74, 34, max(40, self.width() - 110), max(40, self.height() - 92))
        if not values:
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter, "尚无该参数数值，填写记录后查看图表"
            )
            return
        if self.kind == "pie":
            if any(value < 0 for value in values) or sum(values) <= 0:
                painter.drawText(
                    self.rect(),
                    Qt.AlignmentFlag.AlignCenter,
                    "该参数含负值或总量为零，请选择表格、条形图或折线图",
                )
                return
            size = min(area.height(), area.width() * 0.6)
            pie = QRectF(area.x(), area.y(), size, size)
            start = 0
            colors = ["#477a54", "#6c9258", "#a3b47a", "#cfb872", "#9f7752", "#668b8c"]
            positive = [
                point for point in self.series if point["value"] is not None and point["value"] > 0
            ]
            for index, point in enumerate(positive):
                span = round(point["value"] / sum(values) * 5760)
                painter.setBrush(QColor(colors[index % len(colors)]))
                painter.setPen(QColor("#fffcf5"))
                painter.drawPie(pie, start, span)
                if index < 10:
                    painter.fillRect(
                        QRectF(pie.right() + 18, area.top() + index * 24, 12, 12), painter.brush()
                    )
                    painter.setPen(QColor("#334b37"))
                    painter.drawText(
                        QPointF(pie.right() + 38, area.top() + 12 + index * 24),
                        f"{point['date'][5:]}  {point['value']:g} {self.unit}",
                    )
                start += span
            if len(positive) > 10:
                painter.drawText(QPointF(pie.right() + 18, area.top() + 262), "完整数值见表格")
            return
        low, high = min(0, min(values)), max(0, max(values))
        if low == high:
            high = low + 1

        def y(value):
            return area.bottom() - (value - low) / (high - low) * area.height()

        painter.setPen(QPen(QColor("#cbd3be"), 1))
        for step in range(5):
            value = low + (high - low) * step / 4
            painter.setPen(QPen(QColor("#cbd3be"), 1))
            painter.drawLine(QPointF(area.left(), y(value)), QPointF(area.right(), y(value)))
            painter.setPen(QColor("#435d47"))
            painter.drawText(
                QRectF(2, y(value) - 10, 62, 22), Qt.AlignmentFlag.AlignRight, f"{value:g}"
            )
        painter.setPen(QColor("#435d47"))
        painter.drawText(QPointF(8, 20), self.unit)
        width = area.width() / len(self.series)
        previous = None
        for index, point in enumerate(self.series):
            x = area.left() + width * (index + 0.5)
            value = point["value"]
            if index % max(1, len(self.series) // 7) == 0 or index == len(self.series) - 1:
                painter.setPen(QColor("#435d47"))
                painter.drawText(
                    QRectF(x - 32, area.bottom() + 12, 64, 28),
                    Qt.AlignmentFlag.AlignCenter,
                    point["date"][5:],
                )
            if value is None:
                previous = None
                continue
            painter.setPen(QPen(QColor("#537c51"), 3))
            painter.setBrush(QColor("#7e9e6f"))
            current = QPointF(x, y(value))
            if self.kind == "bar":
                painter.drawRoundedRect(
                    QRectF(
                        x - width * 0.3,
                        min(y(value), y(0)),
                        width * 0.6,
                        max(2, abs(y(value) - y(0))),
                    ),
                    3,
                    3,
                )
            else:
                if previous is not None:
                    painter.drawLine(previous, current)
                painter.drawEllipse(current, 4, 4)
            previous = current


class ProfileStatisticsPage(QWidget):
    record_changed = Signal()
    profile_changed = Signal()

    def __init__(self, health_service, profile_editor, parent=None):
        super().__init__(parent)
        self.health_service, self.profile_editor = health_service, profile_editor
        self.user_id = None
        root = QVBoxLayout(self)
        heading = QHBoxLayout()
        title = QLabel("记录统计")
        title.setObjectName("PageTitle")
        heading.addWidget(title)
        heading.addStretch()
        root.addLayout(heading)
        self.profile_summary = QLabel("六类记录平级统计；个人资料请在“我的平台”查看。")
        root.addWidget(self.profile_summary)
        filters = QHBoxLayout()
        self.category_combo = QComboBox()
        for code, label in CATEGORIES:
            self.category_combo.addItem(label, code)
        self.parameter_combo = QComboBox()
        self.period_combo = QComboBox()
        self.period_combo.addItem("近 7 天", 7)
        self.period_combo.addItem("近 30 天", 30)
        self.view_combo = QComboBox()
        for label, code in [
            ("表格", "table"),
            ("条形图", "bar"),
            ("折线图", "line"),
            ("饼图", "pie"),
        ]:
            self.view_combo.addItem(label, code)
        for label, widget in [
            ("类别", self.category_combo),
            ("参数", self.parameter_combo),
            ("周期", self.period_combo),
            ("展示", self.view_combo),
        ]:
            filters.addWidget(QLabel(label))
            filters.addWidget(widget)
        filters.addStretch()
        add = QPushButton("新增记录")
        add.clicked.connect(self.add_record)
        filters.addWidget(add)
        root.addLayout(filters)
        self.notice = QLabel("空白表示尚未填写数值；每日睡眠、温度和湿度取均值，其他数值按日合计。")
        self.notice.setWordWrap(True)
        root.addWidget(self.notice)
        self.views = QStackedWidget()
        self.table = QTableWidget(0, 3)
        self.table.verticalHeader().hide()
        self.table.setHorizontalHeaderLabels(["日期", "数值", "已填该参数的记录数"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.chart = RecordChart()
        self.views.addWidget(self.table)
        self.views.addWidget(self.chart)
        root.addWidget(self.views, 1)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)
        self.category_combo.currentIndexChanged.connect(self._parameters)
        self.parameter_combo.currentIndexChanged.connect(self.refresh)
        self.period_combo.currentIndexChanged.connect(self.refresh)
        self.view_combo.currentIndexChanged.connect(self.refresh)
        self.profile_editor.profile_changed.connect(self.profile_changed)
        self._parameters()

    def _parameters(self):
        self.parameter_combo.blockSignals(True)
        self.parameter_combo.clear()
        for key, label, unit, _aggregation in PARAMETERS[self.category_combo.currentData()]:
            self.parameter_combo.addItem(f"{label}（{unit}）", key)
        self.parameter_combo.blockSignals(False)
        self.refresh()

    def set_user(self, user_id):
        self.user_id = user_id
        self.profile_editor.set_user(user_id)
        self.refresh()

    def refresh(self, *_args):
        self.table.setRowCount(0)
        self.chart.set_data([], "bar", "")
        if self.user_id is None:
            return
        try:
            days = self.period_combo.currentData()
            start = beijing_today() - timedelta(days=days - 1)
            category, parameter = (
                self.category_combo.currentData(),
                self.parameter_combo.currentData(),
            )
            records = self.health_service.list_life_records(
                self.user_id,
                category=category,
                start_date=start - timedelta(days=1),
                end_date=beijing_today() + timedelta(days=1),
                limit=10_000,
            )
            series = daily_series(records, category, parameter, start, days)
            self.table.setRowCount(len(series))
            definition = next(item for item in PARAMETERS[category] if item[0] == parameter)
            unit = definition[2]
            for row, point in enumerate(series):
                value = "未填写" if point["value"] is None else f"{point['value']:g} {unit}"
                for col, text in enumerate([point["date"], value, str(point["samples"])]):
                    self.table.setItem(row, col, QTableWidgetItem(text))
            kind = self.view_combo.currentData()
            self.views.setCurrentIndex(0 if kind == "table" else 1)
            self.chart.set_data(series, kind, unit)
            self.status_label.setText("仅汇总已确认的记录，不据此判断健康状况。")
        except Exception as error:
            self.status_label.setText(f"读取统计失败：{error}")

    def edit_profile(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("个人档案")
        dialog.resize(760, 760)
        layout = QVBoxLayout(dialog)
        layout.addWidget(self.profile_editor)
        self.profile_editor.show()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        self.profile_editor.refresh()
        dialog.exec()
        self.profile_editor.setParent(self)
        self.profile_editor.hide()
        self.refresh()

    def refresh_snapshot(self, resources):
        """Refresh confirmed statistics, preserving filters and the profile form."""
        if self.user_id is None:
            return ()
        from .snapshot_refresh import preserve_views

        category, parameter = self.category_combo.currentData(), self.parameter_combo.currentData()
        days = self.period_combo.currentData()
        series = daily_series(
            resources["life_records"],
            category,
            parameter,
            beijing_today() - timedelta(days=days - 1),
            days,
        )
        definition = next(item for item in PARAMETERS[category] if item[0] == parameter)
        unit, kind = definition[2], self.view_combo.currentData()
        with preserve_views(self.table):
            self.table.setRowCount(len(series))
            for row, point in enumerate(series):
                value = "未填写" if point["value"] is None else f"{point['value']:g} {unit}"
                for col, text in enumerate([point["date"], value, str(point["samples"])]):
                    self.table.setItem(row, col, QTableWidgetItem(text))
        self.views.setCurrentIndex(0 if kind == "table" else 1)
        self.chart.set_data(series, kind, unit)
        self.status_label.setText("已从云端完整镜像更新；仅汇总已确认记录，不据此判断健康状况。")
        return ("记录统计",)

    def add_record(self):
        if self.user_id is None:
            return
        dialog = LifeRecordDialog(self.category_combo.currentData(), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                result = self.health_service.add_life_record(self.user_id, **dialog.values)
                if show_queued_result(result, self, label=self.status_label):
                    return
                self.refresh()
                self.record_changed.emit()
            except Exception as error:
                self.status_label.setText(f"保存失败：{error}")
