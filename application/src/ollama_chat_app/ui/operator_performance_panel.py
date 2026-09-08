from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QGridLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .staff_work_chart import WorkChart


class OperatorPerformancePanel(QWidget):
    """Show recorded advisor workload facts without assigning a score or rank."""

    def __init__(self, service_management: object, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service_management
        self.actor_user_id: int | None = None
        self.result: dict[str, Any] = {}

        layout = QVBoxLayout(self)
        title = QLabel("顾问工作事实统计")
        title.setObjectName("SectionTitle")
        layout.addWidget(title)
        self.notice = QLabel(
            "仅汇总本机数据库中已记录的工作事实；不生成自动绩效分数、排名、医疗评价或健康结论。"
        )
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)

        filters = QGridLayout()
        self.advisor_filter = QComboBox()
        self.member_filter = QComboBox()
        self.date_filter = QCheckBox("限定日期")
        self.start_date = QDateEdit(QDate.currentDate().addMonths(-1))
        self.end_date = QDateEdit(QDate.currentDate())
        for editor in (self.start_date, self.end_date):
            editor.setCalendarPopup(True)
            editor.setDisplayFormat("yyyy-MM-dd")
        self.view_filter = QComboBox()
        for label, mode in (
            ("表格", "table"),
            ("柱状图", "bar"),
            ("折线图", "line"),
            ("饼图", "pie"),
        ):
            self.view_filter.addItem(label, mode)
        for column, (label, widget) in enumerate(
            (
                ("顾问", self.advisor_filter),
                ("会员", self.member_filter),
                ("显示", self.view_filter),
            )
        ):
            filters.addWidget(QLabel(label), 0, column * 2)
            filters.addWidget(widget, 0, column * 2 + 1)
        filters.addWidget(self.date_filter, 1, 0)
        filters.addWidget(self.start_date, 1, 1)
        filters.addWidget(QLabel("至（包含当天）"), 1, 2)
        filters.addWidget(self.end_date, 1, 3)
        refresh_button = QPushButton("应用筛选 / 刷新")
        refresh_button.clicked.connect(self.refresh)
        filters.addWidget(refresh_button, 1, 4, 1, 2)
        layout.addLayout(filters)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            [
                "顾问",
                "账号状态",
                "负责会员",
                "待处理任务",
                "已完成任务",
                "上门记录",
                "已记录分钟",
                "未填时长记录",
            ]
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, self.table.columnCount()):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.chart = WorkChart()
        self.chart_scroll = QScrollArea()
        self.chart_scroll.setWidgetResizable(True)
        self.chart_scroll.setWidget(self.chart)
        self.display = QStackedWidget()
        self.display.addWidget(self.table)
        self.display.addWidget(self.chart_scroll)
        layout.addWidget(self.display, 1)
        self.view_filter.currentIndexChanged.connect(self._show_view)

        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def set_actor(self, actor_user_id: int | None) -> None:
        self.actor_user_id = actor_user_id
        if actor_user_id is None:
            self.table.setRowCount(0)
            self.result = {}
            self.chart.values = []
            self.chart.update()
            self.advisor_filter.clear()
            self.member_filter.clear()
            self.status.clear()
            return
        self.refresh()

    def refresh(self) -> None:
        if self.actor_user_id is None:
            return
        method = getattr(self.service, "get_advisor_work_statistics", None)
        if not callable(method):
            self.table.setRowCount(0)
            self.status.setText("当前数据服务未启用顾问工作统计。")
            return
        try:
            for combo, method_name, label in (
                (self.advisor_filter, "list_advisors", "全部顾问"),
                (self.member_filter, "list_members", "全部会员"),
            ):
                list_method = getattr(self.service, method_name, None)
                if callable(list_method):
                    previous = combo.currentData()
                    combo.clear()
                    combo.addItem(label, None)
                    for item in list_method(self.actor_user_id):
                        combo.addItem(
                            str(
                                item.get("nickname")
                                or item.get("display_name")
                                or item.get("username")
                            ),
                            item["id"],
                        )
                    combo.setCurrentIndex(max(0, combo.findData(previous)))
            filtered = getattr(self.service, "get_filtered_work_statistics", None)
            if callable(filtered):
                filters: dict[str, Any] = {
                    "advisor_user_id": self.advisor_filter.currentData(),
                    "member_user_id": self.member_filter.currentData(),
                }
                if self.date_filter.isChecked():
                    zone = ZoneInfo("Asia/Shanghai")
                    filters["starts_at"] = datetime.combine(
                        self.start_date.date().toPython(), time.min, zone
                    ).isoformat()
                    filters["ends_at"] = datetime.combine(
                        self.end_date.date().toPython() + timedelta(days=1), time.min, zone
                    ).isoformat()
                result = filtered(self.actor_user_id, **filters)
            else:
                result = method(self.actor_user_id)
            self.result = result
            advisors = list(result.get("advisors", []))
            self.notice.setText(str(result.get("notice") or self.notice.text()))
            self._fill(advisors)
            self._show_view()
            self.status.setText(f"已读取 {len(advisors)} 位顾问；图表单位为已完成上门记录次数。")
            self.status.setStyleSheet("color: #176b47;")
        except Exception as exc:
            self.table.setRowCount(0)
            self.result = {}
            self.chart.values = []
            self.chart.update()
            self.status.setText(f"读取工作统计失败：{exc}")
            self.status.setStyleSheet("color: #9c2f27;")

    def _show_view(self, _index: int = 0) -> None:
        mode = self.view_filter.currentData()
        self.display.setCurrentIndex(0 if mode == "table" else 1)
        self.chart.mode = str(mode)
        if mode == "line":
            self.chart.values = [
                (str(row["date"]), int(row["count"])) for row in self.result.get("daily", [])
            ]
        else:
            self.chart.values = [
                (
                    str(row.get("display_name") or row.get("username")),
                    int(row.get("completed_visit_record_count", 0)),
                )
                for row in self.result.get("advisors", [])
            ]
        self.chart.setMinimumWidth(max(560, len(self.chart.values) * 110) if mode != "pie" else 660)
        self.chart.setMinimumHeight(
            max(340, len(self.chart.values) * 30 + 75) if mode == "pie" else 340
        )
        self.chart.update()

    def refresh_snapshot(self, resources):
        """Use the unfiltered aggregate only when it matches the visible filters."""
        if self.actor_user_id is None:
            return ()
        # The snapshot contains all-time advisor aggregates, not individual visit
        # records. Applying date/member filters or creating a daily line would
        # silently change the meaning of the requested statistic.
        if (
            self.date_filter.isChecked()
            or self.member_filter.currentData() is not None
            or self.view_filter.currentData() == "line"
        ):
            self.status.setText(
                "镜像已同步；当前日期/会员筛选或每日折线需点击“应用筛选 / 刷新”在线读取。"
            )
            return ()
        from copy import deepcopy

        from .snapshot_refresh import preserve_views

        result = deepcopy(resources["work_statistics"])
        if not isinstance(result.get("advisors"), list):
            return ()
        advisor_id = self.advisor_filter.currentData()
        if advisor_id is not None:
            result["advisors"] = [
                row for row in result["advisors"] if row.get("advisor_user_id") == advisor_id
            ]
        self.result = result
        self.notice.setText(str(result.get("notice") or self.notice.text()))
        with preserve_views(self.table):
            self._fill(result["advisors"])
        self._show_view()
        self.status.setText("已从完整镜像更新顾问累计工作统计；筛选条件保持不变。")
        return ("顾问累计工作统计",)

    def _fill(self, advisors: list[dict[str, Any]]) -> None:
        self.table.setRowCount(len(advisors))
        for row, advisor in enumerate(advisors):
            values = (
                advisor.get("display_name") or advisor.get("username") or "未命名顾问",
                "已启用" if advisor.get("account_status") == "active" else "未启用",
                advisor.get("active_member_count", 0),
                advisor.get("pending_task_count", 0),
                advisor.get("completed_task_count", 0),
                advisor.get("completed_visit_record_count", 0),
                advisor.get("recorded_visit_minutes", 0),
                advisor.get("records_without_duration_count", 0),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column > 0:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, column, item)


__all__ = ["OperatorPerformancePanel"]
