from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget


class WorkChart(QWidget):
    """Render the same recorded counts as the adjacent accessible table."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(340)
        self.mode = "bar"
        self.values: list[tuple[str, int]] = []
        self.setAccessibleName("已完成上门记录图表")

    def paintEvent(self, event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#fffdf6"))
        painter.setPen(QColor("#355342"))
        if not self.values or sum(value for _, value in self.values) == 0:
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "所选范围暂无上门记录")
            return
        colors = [QColor(code) for code in ("#7d9c69", "#a7b98b", "#c4aa75", "#6e9385", "#b0bdae")]
        area = QRectF(54, 35, max(100, self.width() - 100), self.height() - 100)
        if self.mode == "pie":
            diameter = min(area.height(), area.width() * 0.58)
            circle = QRectF(area.left(), area.top(), diameter, diameter)
            total = sum(value for _, value in self.values)
            angle = 0
            for index, (name, value) in enumerate(self.values):
                span = round(value / total * 5760)
                painter.setBrush(colors[index % len(colors)])
                painter.drawPie(circle, angle, span)
                angle += span
                painter.drawText(
                    int(circle.right() + 20),
                    50 + index * 29,
                    f"{name}  {value} 次（{value / total:.0%}）",
                )
            return
        painter.setPen(QPen(QColor("#819477"), 1))
        painter.drawLine(area.bottomLeft(), area.bottomRight())
        painter.drawLine(area.bottomLeft(), area.topLeft())
        maximum = max(value for _, value in self.values) or 1
        step = area.width() / max(1, len(self.values))
        points = []
        for index, (name, value) in enumerate(self.values):
            x = area.left() + step * (index + 0.5)
            y = area.bottom() - value / maximum * (area.height() - 24)
            painter.setPen(QColor("#355342"))
            painter.drawText(
                QRectF(x - step / 2, area.bottom() + 8, step, 48),
                Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap,
                name,
            )
            painter.drawText(
                QRectF(x - 30, y - 25, 60, 22), Qt.AlignmentFlag.AlignCenter, str(value)
            )
            if self.mode == "bar":
                painter.fillRect(
                    QRectF(x - step * 0.28, y, step * 0.56, area.bottom() - y),
                    colors[index % len(colors)],
                )
            points.append(QPointF(x, y))
        if self.mode == "line":
            painter.setPen(QPen(colors[0], 3))
            for left, right in zip(points, points[1:], strict=False):
                painter.drawLine(left, right)
            painter.setBrush(colors[0])
            for point in points:
                painter.drawEllipse(point, 5, 5)
