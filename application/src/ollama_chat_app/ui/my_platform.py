"""Personal, accessible settings with no hidden OS or network permission changes."""

from __future__ import annotations

import base64

from PySide6.QtCore import QBuffer, QIODevice, Qt, Signal
from PySide6.QtGui import QImageReader, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..services.preferences import DEFAULT_PREFERENCES
from .offline_feedback import show_queued_result

TIMEZONES = (
    ("北京时间", "Asia/Shanghai"),
    ("香港", "Asia/Hong_Kong"),
    ("东京", "Asia/Tokyo"),
    ("新加坡", "Asia/Singapore"),
    ("伦敦（自动夏令时）", "Europe/London"),
    ("纽约（自动夏令时）", "America/New_York"),
    ("洛杉矶（自动夏令时）", "America/Los_Angeles"),
    ("协调世界时 UTC", "UTC"),
)


class MyPlatformPanel(QWidget):
    preferences_changed = Signal(dict)

    def __init__(self, health_service, preferences_service=None, parent=None):
        super().__init__(parent)
        self.health_service = health_service
        self.preferences_service = preferences_service
        self.user_id = None
        self._avatar_data = ""
        outer = QVBoxLayout(self)
        title = QLabel("我的平台")
        title.setObjectName("Title")
        outer.addWidget(title)
        subtitle = QLabel("按您的习惯来，让每一天更从容。")
        subtitle.setObjectName("Subtitle")
        outer.addWidget(subtitle)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        layout = QVBoxLayout(body)
        card = QFrame()
        card.setObjectName("Card")
        form = QFormLayout(card)
        form.setContentsMargins(28, 24, 28, 24)
        form.setSpacing(16)
        self.avatar = QLabel("☺")
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.avatar.setFixedSize(100, 100)
        avatar_line = QHBoxLayout()
        avatar_line.addWidget(self.avatar)
        avatar_button = QPushButton("更换头像")
        avatar_button.clicked.connect(self._choose_avatar)
        avatar_line.addWidget(avatar_button)
        avatar_line.addStretch()
        form.addRow("个人头像", avatar_line)
        self.nickname = QLineEdit()
        self.nickname.setMaxLength(40)
        self.nickname.setPlaceholderText("例如：爱散步的阿姨")
        self.preferred_name = QLineEdit()
        self.preferred_name.setMaxLength(40)
        self.preferred_name.setPlaceholderText("例如：王阿姨，AI 会优先使用这个称呼")
        form.addRow("我的昵称", self.nickname)
        form.addRow("希望怎样称呼您", self.preferred_name)
        self.membership = QLabel("登录后查看会员情况")
        self.membership.setWordWrap(True)
        form.addRow("会员情况", self.membership)
        self.font_size = QSpinBox()
        self.font_size.setRange(16, 30)
        self.font_size.setSuffix(" 像素")
        form.addRow("文字大小", self.font_size)
        self.brightness = QSlider(Qt.Orientation.Horizontal)
        self.brightness.setRange(70, 120)
        self.brightness.setToolTip("调整本应用背景明暗，不改变电脑屏幕亮度。")
        form.addRow("界面亮度", self.brightness)
        self.theme = QComboBox()
        for label, code in (
            ("草木绿 · 米白", "sage"),
            ("晴空蓝 · 云白", "blue"),
            ("暖蔷薇 · 杏白", "rose"),
        ):
            self.theme.addItem(label, code)
        form.addRow("温和配色", self.theme)
        self.timezone = QComboBox()
        for label, code in TIMEZONES:
            self.timezone.addItem(label, code)
        form.addRow("时间显示", self.timezone)
        self.city = QLineEdit()
        self.city.setPlaceholderText("填写所在城市，例如：北京；不会自动定位")
        form.addRow("地区名称", self.city)
        self.weather_consent = QCheckBox("允许把所选城市/坐标发给天气服务查询天气")
        form.addRow(self.weather_consent)
        self.context_consent = QCheckBox("允许 AI 回答时参考我填写的相关生活与档案资料")
        self.context_consent.setToolTip("仅发送与问题相关的有限资料至所选 AI 服务；可随时关闭。")
        form.addRow(self.context_consent)
        self.storage = QLabel()
        self.storage.setWordWrap(True)
        form.addRow("存储空间", self.storage)
        save = QPushButton("保存我的设置")
        save.setObjectName("PrimaryButton")
        save.clicked.connect(self._save)
        form.addRow(save)
        self.status = QLabel()
        self.status.setWordWrap(True)
        form.addRow(self.status)
        layout.addWidget(card)
        for title in ("联系客服", "使用帮助与意见反馈"):
            button = QPushButton(title + "  ›")
            button.clicked.connect(
                lambda checked=False, name=title: QMessageBox.information(self, name, "敬请期待")
            )
            layout.addWidget(button)
        layout.addStretch()

    def set_user(self, user_id):
        self.user_id = int(getattr(user_id, "id", user_id)) if user_id is not None else None
        self.refresh()

    def refresh(self):
        if self.user_id is None:
            return
        try:
            prefs = (
                self.preferences_service.get(self.user_id)
                if self.preferences_service
                else dict(DEFAULT_PREFERENCES)
            )
            self.nickname.setText(prefs.get("nickname", ""))
            self.preferred_name.setText(prefs.get("preferred_name", ""))
            self.font_size.setValue(prefs["font_size"])
            self.brightness.setValue(prefs["brightness"])
            self.theme.setCurrentIndex(max(0, self.theme.findData(prefs["theme_color"])))
            self.timezone.setCurrentIndex(max(0, self.timezone.findData(prefs["timezone"])))
            self.city.setText(prefs.get("city", ""))
            self.weather_consent.setChecked(prefs.get("weather_consent", False))
            self.context_consent.setChecked(prefs.get("ai_context_consent", False))
            self._avatar_data = prefs.get("avatar_data", "")
            self._show_avatar()
            summary = self.health_service.get_service_summary(self.user_id)
            membership = summary.get("membership") or {}
            self.membership.setText(
                str(membership.get("plan_code", "普通用户"))
                + " · "
                + str(membership.get("status", "未开通会员"))
            )
            database = getattr(self.health_service, "database", None)
            if database is not None and hasattr(database, "path"):
                size = database.path.stat().st_size if database.path.exists() else 0
                self.storage.setText(
                    f"本机数据库 {size / 1024 / 1024:.2f} MB；附件另计。\n"
                    "云端数据与本机缓存的同步情况，请查看右上角“数据与同步”。"
                )
            else:
                self.storage.setText("当前连接云端；不读取本机其他账户数据库。")
        except Exception:
            self.status.setText("设置暂时无法读取，请检查连接后重试。")

    def _choose_avatar(self):
        selected, _ = QFileDialog.getOpenFileName(
            self, "选择头像", "", "图片 (*.png *.jpg *.jpeg *.webp)"
        )
        if not selected:
            return
        try:
            from pathlib import Path

            if Path(selected).stat().st_size > 8 * 1024 * 1024:
                raise ValueError
            reader = QImageReader(selected)
            size = reader.size()
            if not size.isValid() or size.width() * size.height() > 32_000_000:
                raise ValueError
            image = reader.read().scaled(
                256,
                256,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            if image.isNull():
                raise ValueError
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            image.save(buffer, "PNG")
            self._avatar_data = "data:image/png;base64," + base64.b64encode(
                bytes(buffer.data())
            ).decode("ascii")
            self._show_avatar()
        except Exception:
            QMessageBox.warning(
                self, "无法使用图片", "请使用不超过 8 MB 的正常 PNG/JPG/WebP 图片。"
            )

    def _show_avatar(self):
        pixmap = QPixmap()
        if self._avatar_data:
            try:
                pixmap.loadFromData(
                    base64.b64decode(self._avatar_data.split(",", 1)[1], validate=True)
                )
            except (ValueError, IndexError):
                pass
        if pixmap.isNull():
            self.avatar.setText("☺")
        else:
            self.avatar.setPixmap(
                pixmap.scaled(
                    96,
                    96,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

    def _save(self):
        if self.user_id is None or self.preferences_service is None:
            self.status.setText("请先登录后再设置。")
            return
        try:
            previous = self.preferences_service.get(self.user_id)
            patch = {
                "nickname": self.nickname.text(),
                "preferred_name": self.preferred_name.text(),
                "font_size": self.font_size.value(),
                "brightness": self.brightness.value(),
                "theme_color": self.theme.currentData(),
                "timezone": self.timezone.currentData(),
                "city": self.city.text(),
                "avatar_data": self._avatar_data,
                "weather_consent": self.weather_consent.isChecked(),
                "ai_context_consent": self.context_consent.isChecked(),
                "ai_context_consent_decided": True,
            }
            if patch["city"].strip() != previous.get("city", ""):
                patch.update(latitude=None, longitude=None)
            # Do not re-send an existing sharing opt-in as a new offline grant.
            # Only actual user changes become queued intents.
            patch = {key: value for key, value in patch.items() if previous.get(key) != value}
            if not patch:
                self.status.setText("设置没有变化，无需再次保存。")
                return
            prefs = self.preferences_service.update(self.user_id, patch)
            if show_queued_result(prefs, self, label=self.status):
                return
            self.preferences_changed.emit(prefs)
            self.status.setText("已保存，祝您今天也过得舒心。")
        except Exception:
            self.status.setText("设置没有保存成功，请检查内容和连接后重试。")
