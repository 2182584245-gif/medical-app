"""Original vector-like QPainter artwork; no downloaded assets or remote calls."""

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QWidget


def rounded_icon(name: str, size=32, color="#68785a", background="#f3ecdf") -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size / 32, size / 32)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(background))
    painter.drawRoundedRect(QRectF(1, 1, 30, 30), 10, 10)
    painter.setPen(
        QPen(
            QColor(color),
            2,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
    )
    painter.setBrush(Qt.BrushStyle.NoBrush)
    if name in {"today", "home"}:
        # A small house and rising sun make the landing page recognisable
        # without relying on emoji glyphs that vary between Windows fonts.
        painter.drawPolyline([QPointF(6, 16), QPointF(16, 7), QPointF(26, 16)])
        painter.drawLine(9, 15, 9, 26)
        painter.drawLine(23, 15, 23, 26)
        painter.drawLine(9, 26, 23, 26)
        painter.drawLine(14, 26, 14, 19)
        painter.drawLine(14, 19, 18, 19)
        painter.drawLine(18, 19, 18, 26)
    elif name == "diet":
        painter.drawArc(QRectF(6, 8, 20, 17), 180 * 16, 180 * 16)
        painter.drawLine(6, 16, 26, 16)
        painter.drawLine(12, 26, 20, 26)
        painter.drawLine(13, 8, 14, 12)
        painter.drawLine(19, 6, 20, 11)
    elif name == "water":
        path = QPainterPath(QPointF(16, 5))
        path.cubicTo(11, 12, 7, 15, 8, 21)
        path.cubicTo(10, 29, 23, 29, 24, 20)
        path.cubicTo(25, 15, 20, 10, 16, 5)
        painter.drawPath(path)
        painter.drawArc(QRectF(12, 17, 8, 7), 190 * 16, 85 * 16)
    elif name == "activity":
        painter.drawEllipse(QRectF(17, 4, 5, 5))
        painter.drawLine(17, 12, 13, 19)
        painter.drawLine(16, 12, 10, 13)
        painter.drawLine(10, 13, 7, 18)
        painter.drawLine(17, 12, 22, 17)
        painter.drawLine(22, 17, 26, 15)
        painter.drawLine(13, 19, 20, 22)
        painter.drawLine(20, 22, 21, 28)
        painter.drawLine(13, 19, 9, 26)
        painter.drawLine(9, 26, 5, 26)
    elif name == "sleep":
        path = QPainterPath(QPointF(19, 5))
        path.cubicTo(4, 4, 3, 24, 16, 27)
        path.cubicTo(22, 28, 27, 23, 27, 19)
        path.cubicTo(16, 24, 10, 12, 19, 5)
        painter.drawPath(path)
        painter.drawLine(25, 5, 25, 11)
        painter.drawLine(22, 8, 28, 8)
    elif name == "environment":
        path = QPainterPath(QPointF(9, 24))
        path.cubicTo(1, 11, 16, 5, 26, 7)
        path.cubicTo(27, 22, 21, 28, 9, 24)
        painter.drawPath(path)
        painter.drawLine(7, 27, 22, 12)
        painter.drawLine(14, 20, 14, 13)
        painter.drawLine(18, 16, 23, 16)
    elif name == "medical":
        painter.drawRoundedRect(QRectF(6, 8, 20, 18), 5, 5)
        painter.drawRoundedRect(QRectF(12, 5, 8, 5), 2, 2)
        painter.drawLine(11, 17, 21, 17)
        painter.drawLine(16, 12, 16, 22)
    elif name == "mic":
        painter.drawRoundedRect(QRectF(12, 5, 8, 14), 4, 4)
        painter.drawArc(QRectF(8, 8, 16, 16), 180 * 16, 180 * 16)
        painter.drawLine(16, 24, 16, 28)
        painter.drawLine(12, 28, 20, 28)
    elif name == "profile":
        painter.drawEllipse(QRectF(12, 7, 8, 8))
        painter.drawArc(QRectF(7, 16, 18, 17), 15 * 16, 150 * 16)
    elif name == "chat":
        painter.drawRoundedRect(QRectF(7, 7, 19, 15), 5, 5)
        painter.drawLine(11, 22, 10, 26)
        painter.drawLine(10, 26, 17, 22)
        for x in (12, 17, 22):
            painter.drawPoint(x, 15)
    elif name == "service":
        painter.drawRoundedRect(QRectF(7, 10, 18, 15), 4, 4)
        painter.drawRoundedRect(QRectF(12, 6, 8, 6), 2, 2)
        painter.drawLine(12, 17, 20, 17)
        painter.drawLine(16, 13, 16, 21)
    elif name == "commerce":
        painter.drawRoundedRect(QRectF(7, 10, 18, 15), 3, 3)
        painter.drawLine(10, 10, 12, 6)
        painter.drawLine(12, 6, 20, 6)
        painter.drawLine(20, 6, 23, 10)
        painter.drawEllipse(QRectF(11, 21, 2, 2))
        painter.drawEllipse(QRectF(20, 21, 2, 2))
    elif name == "forum":
        painter.drawRoundedRect(QRectF(6, 7, 20, 14), 5, 5)
        painter.drawLine(11, 21, 10, 27)
        painter.drawLine(10, 27, 16, 22)
        painter.drawLine(11, 13, 21, 13)
        painter.drawLine(11, 17, 18, 17)
    elif name == "statistics":
        painter.drawLine(7, 25, 25, 25)
        for x, y in ((10, 17), (16, 10), (22, 6)):
            painter.drawLine(x, y, x, 22)
    elif name in {"settings", "gear"}:
        painter.drawEllipse(QRectF(10, 10, 12, 12))
        painter.drawEllipse(QRectF(14, 14, 4, 4))
        for x1, y1, x2, y2 in (
            (16, 4, 16, 9), (16, 23, 16, 28), (4, 16, 9, 16),
            (23, 16, 28, 16), (7, 7, 11, 11), (21, 21, 25, 25),
            (21, 11, 25, 7), (7, 25, 11, 21),
        ):
            painter.drawLine(x1, y1, x2, y2)
    elif name in {"weather", "sun"}:
        painter.setBrush(QColor("#edbd63"))
        painter.drawEllipse(QRectF(10, 10, 12, 12))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for x1, y1, x2, y2 in (
            (16, 4, 16, 8), (16, 24, 16, 28), (4, 16, 8, 16),
            (24, 16, 28, 16), (7, 7, 10, 10), (22, 22, 25, 25),
            (22, 10, 25, 7), (7, 25, 10, 22),
        ):
            painter.drawLine(x1, y1, x2, y2)
    elif name in {"reminder", "bell"}:
        painter.drawArc(QRectF(9, 7, 14, 16), 0, 180 * 16)
        painter.drawLine(9, 15, 9, 22)
        painter.drawLine(23, 15, 23, 22)
        painter.drawLine(7, 23, 25, 23)
        painter.drawEllipse(QRectF(14, 24, 4, 4))
    elif name in {"reading", "book"}:
        painter.drawRoundedRect(QRectF(6, 7, 9, 19), 2, 2)
        painter.drawRoundedRect(QRectF(17, 7, 9, 19), 2, 2)
        painter.drawLine(16, 9, 16, 27)
        painter.drawLine(9, 12, 13, 12)
        painter.drawLine(19, 12, 23, 12)
    elif name in {"favorite", "heart"}:
        path = QPainterPath(QPointF(16, 26))
        path.cubicTo(14, 24, 6, 19, 6, 13)
        path.cubicTo(6, 7, 13, 5, 16, 11)
        path.cubicTo(19, 5, 26, 7, 26, 13)
        path.cubicTo(26, 19, 18, 24, 16, 26)
        painter.drawPath(path)
    elif name in {"network", "wifi"}:
        painter.drawArc(QRectF(5, 8, 22, 16), 30 * 16, 120 * 16)
        painter.drawArc(QRectF(9, 12, 14, 10), 30 * 16, 120 * 16)
        painter.drawEllipse(QRectF(14.5, 22, 3, 3))
    else:
        painter.drawRoundedRect(QRectF(7, 8, 18, 18), 4, 4)
        painter.drawLine(7, 14, 25, 14)
        painter.drawLine(12, 5, 12, 10)
        painter.drawLine(20, 5, 20, 10)
        painter.drawLine(11, 20, 14, 23)
        painter.drawLine(14, 23, 21, 17)
    painter.end()
    return QIcon(pixmap)


class GardenMark(QWidget):
    """Small decorative sun and leaves, deliberately containing no health claims."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(92, 54)
        self.setAccessibleName("草木与阳光装饰")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def sizeHint(self):
        return QSize(92, 54)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#edc773"))
        painter.drawEllipse(QRectF(56, 5, 25, 25))
        painter.setPen(
            QPen(
                QColor("#496449"),
                2.5,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        painter.drawLine(27, 46, 43, 9)
        painter.setBrush(QColor("#b0c3a1"))
        path = QPainterPath(QPointF(34, 29))
        path.cubicTo(12, 27, 10, 12, 14, 8)
        path.cubicTo(35, 11, 37, 19, 34, 29)
        painter.drawPath(path)
        painter.setBrush(QColor("#d1bbd2"))
        path = QPainterPath(QPointF(29, 39))
        path.cubicTo(38, 19, 55, 22, 56, 29)
        path.cubicTo(47, 42, 37, 43, 29, 39)
        painter.drawPath(path)
        painter.end()
