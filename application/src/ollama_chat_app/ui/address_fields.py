"""Offline province/city suggestions with an unrestricted, explicitly confirmed detail."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QGridLayout, QLabel, QLineEdit, QWidget

from .china_regions import REGIONS, SOURCE_NOTICE

SERVICE_TYPES = ("首次建档", "居家环境查看", "生活记录回访", "日常生活指导", "综合上门服务")


class ServiceTypeCombo(QComboBox):
    def __init__(self, value="", parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.addItems(SERVICE_TYPES)
        self.lineEdit().setMaxLength(120)
        self.lineEdit().setPlaceholderText("选择常用服务，或填写您的需要")
        if value:
            self.setEditText(str(value))

    def text(self):
        return self.currentText().strip()

    def setText(self, value):
        self.setEditText(str(value or ""))


class AddressFields(QWidget):
    """QLineEdit-like text API; never silently rewrite a loaded legacy address.

    New submissions call validate(). Editing an old task can pass
    allow_unchanged_legacy=True so changing its status does not change its address.
    """

    textChanged = Signal(str)

    def __init__(self, value="", parent=None):
        super().__init__(parent)
        self._loading = False
        self._original = ""
        self._dirty = False
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(12)
        self.province = QComboBox()
        self.province.addItem("请选择省 / 自治区 / 直辖市", "")
        for name in REGIONS:
            self.province.addItem(name, name)
        self.city = QComboBox()
        self.city.addItem("请先选择省份", "")
        for combo in (self.province, self.city):
            combo.setMinimumContentsLength(8)
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            combo.setMaxVisibleItems(10)
            combo.setMinimumWidth(0)
        self.province.setAccessibleName("省份，必填")
        self.city.setAccessibleName("城市或地区，必填")
        self.detail = QLineEdit()
        self.detail.setMaxLength(400)
        self.detail.setPlaceholderText("区县、街道、小区、门牌或约定地点，自由填写")
        self.detail.setAccessibleName("详细地点，必填")
        layout.addWidget(QLabel("省份 *"), 0, 0)
        layout.addWidget(QLabel("市 / 地区 *"), 0, 1)
        layout.addWidget(self.province, 1, 0)
        layout.addWidget(self.city, 1, 1)
        layout.addWidget(QLabel("详细地点 *"), 2, 0, 1, 2)
        layout.addWidget(self.detail, 3, 0, 1, 2)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)
        self.notice = QLabel(SOURCE_NOTICE)
        self.notice.setObjectName("HealthHint")
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice, 4, 0, 1, 2)
        self.province.currentIndexChanged.connect(self._province_changed)
        self.city.currentIndexChanged.connect(self._changed)
        self.detail.textChanged.connect(self._changed)
        self.setText(value)

    def _province_changed(self):
        self.city.blockSignals(True)
        self.city.clear()
        self.city.addItem("请选择城市 / 地区", "")
        options = REGIONS.get(self.province.currentData(), ())
        for city in options:
            self.city.addItem(city, city)
        if len(options) == 1:
            self.city.setCurrentIndex(1)
        self.city.blockSignals(False)
        self.province.setToolTip(self.province.currentText())
        self._changed()

    def _changed(self, *_args):
        if self._loading:
            return
        self._dirty = True
        self.city.setToolTip(self.city.currentText())
        self.textChanged.emit(self.text())

    def setText(self, value):
        previous = self.text()
        value = str(value or "").strip()
        self._loading = True
        self.province.setCurrentIndex(0)
        self._province_changed()
        self.detail.setText(value)
        remainder = value
        province = next((name for name in REGIONS if value.startswith(name)), None)
        city = None
        if province:
            remainder = value[len(province) :].lstrip()
            city = next((name for name in REGIONS[province] if remainder.startswith(name)), None)
            if len(REGIONS[province]) == 1:
                city = REGIONS[province][0]
        else:
            candidates = [
                (p, c) for p, cities in REGIONS.items() for c in cities if value.startswith(c)
            ]
            if len(candidates) == 1:
                province, city = candidates[0]
        if province:
            self.province.setCurrentIndex(self.province.findData(province))
            self._province_changed()
            if city:
                self.city.setCurrentIndex(self.city.findData(city))
                if remainder.startswith(city):
                    remainder = remainder[len(city) :].lstrip()
            self.detail.setText(remainder)
        self._original, self._dirty, self._loading = value, False, False
        self.notice.setText(
            SOURCE_NOTICE
            if province
            else SOURCE_NOTICE + ("\n原有地点已保留；修改时请补选省市。" if value else "")
        )
        if value != previous:
            self.textChanged.emit(value)

    def text(self):
        if not self._dirty:
            return self._original
        province, city = self.province.currentData() or "", self.city.currentData() or ""
        pieces = [province]
        if city != province:
            pieces.append(city)
        pieces.append(self.detail.text().strip())
        return " ".join(part for part in pieces if part)

    def validate(self, *, allow_unchanged_legacy=False):
        if allow_unchanged_legacy and not self._dirty:
            return self.text()
        if not self.province.currentData():
            raise ValueError("请选择省份、自治区、直辖市或特别行政区。")
        if not self.city.currentData():
            raise ValueError("请选择对应城市或地区。")
        if not self.detail.text().strip():
            raise ValueError("请填写详细地点；可以是门牌、小区、门口或双方约定的地点。")
        # No postcode, street-number or 'real address' heuristics.
        return self.text()

    def setPlaceholderText(self, value):
        self.detail.setPlaceholderText(value)

    def clear(self):
        self.setText("")
