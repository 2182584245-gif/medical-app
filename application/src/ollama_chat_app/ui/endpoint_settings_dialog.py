"""Explicit, cancellable editing of public application API addresses."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..endpoint_settings import EndpointSettings, EndpointSettingsError, EndpointSettingsStore


class EndpointSettingsDialog(QDialog):
    def __init__(self, settings: EndpointSettings, parent=None, *,
                 store: EndpointSettingsStore | None = None,
                 saved_settings: EndpointSettings | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("更换服务地址")
        self.setMinimumWidth(360)
        self.resize(690, 560)
        self.settings = settings
        self._original = settings
        self._store = store
        self._saved_settings = saved_settings if saved_settings is not None else settings
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setSpacing(12)
        notice = QLabel(
            "请选择可信的服务路线。更换地址后，登录信息和云端数据请求将发送到新地址。"
            "这里只保存公开地址，不保存密码，也不会迁移数据。"
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        self.target_group = QButtonGroup(self)
        self.aliyun_button = QRadioButton("使用阿里云")
        self.supabase_button = QRadioButton("使用 Supabase")
        self.target_group.addButton(self.aliyun_button, 0)
        self.target_group.addButton(self.supabase_button, 1)
        self.aliyun_url_input = QLineEdit(settings.aliyun_url)
        self.supabase_url_input = QLineEdit(settings.supabase_url)
        for radio, field, name in (
            (self.aliyun_button, self.aliyun_url_input, "阿里云 HTTPS 服务地址"),
            (self.supabase_button, self.supabase_url_input, "Supabase HTTPS 服务地址"),
        ):
            layout.addWidget(radio)
            field.setMaxLength(2048)
            field.setAccessibleName(name)
            field.setPlaceholderText("https://可信服务器/服务路线")
            layout.addWidget(field)
        selected = 1 if settings.selected_target == "supabase" else 0
        self.target_group.button(selected).setChecked(True)
        self.change_notice = QLabel()
        self.change_notice.setWordWrap(True)
        self.change_notice.setText(
            "当前正在使用的地址与已保存设置不同；下面显示当前地址，只有点击保存才会更新设置。"
            if settings != self._saved_settings else "更改和恢复默认都需要点击保存才会生效。"
        )
        layout.addWidget(self.change_notice)
        self.restore_button = QPushButton("恢复默认地址")
        self.restore_button.clicked.connect(self._restore_defaults)
        layout.addWidget(self.restore_button)
        self.error_label = QLabel()
        self.error_label.setObjectName("Error")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)
        layout.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        buttons = QHBoxLayout()
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.reject)
        self.save_button = QPushButton("保存")
        self.save_button.setObjectName("PrimaryButton")
        self.save_button.clicked.connect(self._save)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.save_button)
        outer.addLayout(buttons)
        screen = self.screen()
        if screen is not None:
            available = screen.availableGeometry()
            self.resize(min(690, available.width() - 32), min(560, available.height() - 64))

    def _restore_defaults(self) -> None:
        defaults = EndpointSettings()
        self.aliyun_url_input.setText(defaults.aliyun_url)
        self.supabase_url_input.setText(defaults.supabase_url)
        self.aliyun_button.setChecked(True)
        self.error_label.hide()
        self.change_notice.setText("已填入默认路线和地址，尚未保存；点击取消可保留原来的自定义设置。")

    def _save(self) -> None:
        try:
            settings = replace(
                self._original,
                selected_target="supabase" if self.supabase_button.isChecked() else "aliyun",
                aliyun_url=self.aliyun_url_input.text().strip(),
                supabase_url=self.supabase_url_input.text().strip(),
            ).validated()
            if self._store is not None:
                self._store.save(settings, expected=self._saved_settings)
        except EndpointSettingsError as error:
            self.error_label.setText(str(error))
            self.error_label.show()
            return
        self.settings = settings
        self.accept()
