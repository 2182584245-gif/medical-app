from __future__ import annotations

from urllib.parse import urlencode

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .address_fields import AddressFields, ServiceTypeCombo
from .dialog_layout import fit_dialog
from .segmented_time import SegmentedDateTimeEdit as BeijingDateTimeEdit
from .time_fields import beijing_qdatetime, datetime_iso


class AppointmentDialog(QDialog):
    """Address confirmation is the reliable fallback when embedded maps are unavailable."""

    def __init__(self, preferences=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("预约上门服务")
        self.resize(720, 660)
        root = QVBoxLayout(self)
        self.form_scroll = QScrollArea(self)
        self.form_scroll.setWidgetResizable(True)
        body = QWidget()
        self.form_scroll.setWidget(body)
        content = QVBoxLayout(body)
        root.addWidget(self.form_scroll, 1)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.service_type = ServiceTypeCombo()
        form.addRow("服务内容", self.service_type)
        self.scheduled_at = BeijingDateTimeEdit(beijing_qdatetime().addDays(1))
        form.addRow("期望上门时间", self.scheduled_at)
        self.address = AddressFields()
        self.address.setPlaceholderText("请填写城市、街道、小区及门牌号")
        self.address.setText(str((preferences or {}).get("city") or ""))
        form.addRow("上门地址", self.address)
        content.addLayout(form)
        self.map_status = QLabel(
            "点击浏览地图才会把上述地址发送至 OpenStreetMap；最终以您确认的地址为准。"
        )
        self.map_status.setWordWrap(True)
        content.addWidget(self.map_status)
        browse = QPushButton("浏览地图核对地址")
        browse.clicked.connect(self.browse_map)
        content.addWidget(browse)
        self.map_container = QVBoxLayout()
        content.addLayout(self.map_container)
        self.address_confirmed = QCheckBox("我已核对并确认上述完整上门地址")
        content.addWidget(self.address_confirmed)
        self.address.textChanged.connect(lambda: self.address_confirmed.setChecked(False))
        self.notes = QTextEdit()
        self.notes.setPlaceholderText("可补充门禁、联系方法、上门需求等；可不填")
        self.notes.setMaximumHeight(100)
        content.addWidget(self.notes)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("提交预约请求")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.validate)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.map_view = None
        fit_dialog(self, width=760, height=700)

    def browse_map(self):
        address = self.address.text().strip()
        if not address:
            self.map_status.setText("请先填写地区或街道，再浏览地图。")
            return
        url = QUrl("https://www.openstreetmap.org/search?" + urlencode({"query": address}))
        try:
            from PySide6.QtWebEngineWidgets import QWebEngineView

            if self.map_view is None:
                self.map_view = QWebEngineView(self)
                self.map_view.setMinimumHeight(260)
                self.map_view.loadFinished.connect(self._map_loaded)
                self.map_container.addWidget(self.map_view)
            self.map_view.setUrl(url)
            self.map_status.setText(
                "正在加载 OpenStreetMap。请核对后勾选地址确认；地图不自动更改地址。"
            )
        except ImportError:
            opened = QDesktopServices.openUrl(url)
            self.map_status.setText(
                "已打开浏览器地图，请回到这里确认完整地址。"
                if opened
                else "地图暂不可用，仍可核对填写完整地址后提交预约。"
            )

    def _map_loaded(self, ok):
        self.map_status.setText(
            "地图已加载，请核对并确认完整上门地址。"
            if ok
            else "地图暂不可用，可直接核对填写完整地址后提交预约。"
        )

    def validate(self):
        try:
            self.address.validate()
            scheduled = self.scheduled_at.dateTime()
            if not self.service_type.text():
                raise ValueError("请选择或填写服务内容。")
        except ValueError as error:
            QMessageBox.information(self, "请补充预约信息", str(error))
            return
        if not self.address_confirmed.isChecked():
            QMessageBox.information(self, "请确认地址", "请核对地址后勾选确认。")
            return
        if scheduled <= beijing_qdatetime():
            QMessageBox.information(self, "时间已过去", "请选择将来的期望上门时间。")
            return
        self.accept()

    @property
    def values(self):
        return {
            "service_type": self.service_type.currentText(),
            "scheduled_at": datetime_iso(self.scheduled_at.dateTime()),
            "address": self.address.text().strip(),
            "notes": self.notes.toPlainText().strip(),
            "latitude": None,
            "longitude": None,
        }
