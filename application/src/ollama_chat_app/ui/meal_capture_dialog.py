"""Meal-first capture UI. Camera starts only after the user clicks its button."""

from __future__ import annotations

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtMultimedia import QCamera, QImageCapture, QMediaCaptureSession, QMediaDevices
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QDialog, QFileDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout


class MealCaptureDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("拍下这一餐")
        self.resize(680, 540)
        self.image_bytes: bytes | None = None
        self.file_path: str | None = None
        self.camera = None
        layout = QVBoxLayout(self)
        hint = QLabel("把这一餐拍清楚就好，不用逐个打字。识别结果会先请您确认。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.video = QVideoWidget()
        layout.addWidget(self.video, 1)
        self.status = QLabel("可以打开摄像头，或选择手机拍好的照片。照片不会自动发送。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        row = QHBoxLayout()
        self.start_button = QPushButton("打开摄像头")
        self.start_button.clicked.connect(self.start_camera)
        row.addWidget(self.start_button)
        self.capture_button = QPushButton("拍好了")
        self.capture_button.setEnabled(False)
        self.capture_button.clicked.connect(lambda: self.capture.capture())
        row.addWidget(self.capture_button)
        choose = QPushButton("选择餐食照片")
        choose.clicked.connect(self.choose_photo)
        row.addWidget(choose)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        layout.addLayout(row)
        self.session = QMediaCaptureSession(self)
        self.capture = QImageCapture(self)
        self.session.setImageCapture(self.capture)
        self.session.setVideoOutput(self.video)
        self.capture.readyForCaptureChanged.connect(self.capture_button.setEnabled)
        self.capture.imageCaptured.connect(self.image_captured)
        self.capture.errorOccurred.connect(lambda _id, _err, text: self.status.setText(text))
        self.finished.connect(lambda _: self.stop_camera())

    def start_camera(self):
        if self.camera is not None:
            return
        device = QMediaDevices.defaultVideoInput()
        if device.isNull():
            self.status.setText("未找到摄像头，请选择手机拍好的餐食照片。")
            return
        self.camera = QCamera(device, self)
        self.camera.errorOccurred.connect(
            lambda _err, text: self.status.setText("摄像头无法使用：" + text + "。可选择照片。")
        )
        self.session.setCamera(self.camera)
        self.camera.start()
        self.start_button.setEnabled(False)

    def stop_camera(self):
        if self.camera is not None:
            self.camera.stop()

    def choose_photo(self):
        selected, _ = QFileDialog.getOpenFileName(
            self, "选择餐食照片", "", "照片 (*.jpg *.jpeg *.png *.webp)"
        )
        if selected:
            self.file_path = selected
            self.accept()

    def image_captured(self, _identifier, image):
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, "JPEG", 90):
            self.status.setText("照片处理失败，请重新拍摄。")
            return
        self.image_bytes = bytes(buffer.data())
        self.accept()
