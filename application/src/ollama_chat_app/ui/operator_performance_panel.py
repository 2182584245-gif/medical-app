from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


class OperatorPerformancePanel(QWidget):
    """Show recorded advisor workload facts without assigning a score or rank."""

    def __init__(self, service_management: object, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service_management
        self.actor_user_id: int | None = None

        layout = QVBoxLayout(self)
        title = QLabel("顾问工作事实统计")
        title.setStyleSheet("font-size: 20px; font-weight: 700; color: #163c4a;")
        layout.addWidget(title)
        self.notice = QLabel(
            "仅汇总本机数据库中已记录的工作事实；不生成自动绩效分数、排名、医疗评价或健康结论。"
        )
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)

        refresh_button = QPushButton("刷新工作统计")
        refresh_button.clicked.connect(self.refresh)
        layout.addWidget(refresh_button, 0, Qt.AlignmentFlag.AlignLeft)

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
        layout.addWidget(self.table, 1)

        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def set_actor(self, actor_user_id: int | None) -> None:
        self.actor_user_id = actor_user_id
        if actor_user_id is None:
            self.table.setRowCount(0)
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
            result = method(self.actor_user_id)
            advisors = list(result.get("advisors", []))
            self.notice.setText(str(result.get("notice") or self.notice.text()))
            self._fill(advisors)
            self.status.setText(f"已读取 {len(advisors)} 位顾问的本地工作事实。")
            self.status.setStyleSheet("color: #176b47;")
        except Exception as exc:
            self.table.setRowCount(0)
            self.status.setText(f"读取工作统计失败：{exc}")
            self.status.setStyleSheet("color: #9c2f27;")

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
