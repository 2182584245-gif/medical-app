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
        title = QLabel("档案与统计")
        title.setObjectName("PageTitle")
        heading.addWidget(title)
        heading.addStretch()
        profile_button = QPushButton("个人档案")
        profile_button.setObjectName("PrimaryButton")
        profile_button.clicked.connect(self.edit_profile)
        heading.addWidget(profile_button)
        root.addLayout(heading)
        self.profile_summary = QLabel("点击个人档案查看或编辑资料")
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
            profile = self.health_service.get_profile(self.user_id)
            self.profile_summary.setText(
                f"{profile.get('display_name') or '我的个人档案'} · 点击右上方查看完整资料"
            )
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

        profile = resources["profile"]
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
        self.profile_summary.setText(
            f"{profile.get('display_name') or '我的个人档案'} · 点击右上方查看完整资料"
        )
        with preserve_views(self.table):
            self.table.setRowCount(len(series))
            for row, point in enumerate(series):
                value = "未填写" if point["value"] is None else f"{point['value']:g} {unit}"
                for col, text in enumerate([point["date"], value, str(point["samples"])]):
                    self.table.setItem(row, col, QTableWidgetItem(text))
        self.views.setCurrentIndex(0 if kind == "table" else 1)
        self.chart.set_data(series, kind, unit)
        self.status_label.setText("已从云端完整镜像更新；仅汇总已确认记录，不据此判断健康状况。")
        return ("档案摘要", "记录统计")

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
