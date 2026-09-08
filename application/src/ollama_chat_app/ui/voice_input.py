from __future__ import annotations

from PySide6.QtCore import QEvent, QIODevice, QObject, QThreadPool, QTimer, Signal
from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..services.voice import VOICE_MAX_SECONDS, VOICE_SAMPLE_RATE


class VoiceInputController(QObject):
    """Capture microphone audio in the exact PCM format used by offline Vosk."""

    recording_started = Signal()
    recording_stopped = Signal(bytes)
    recording_failed = Signal(str)
    remaining_seconds_changed = Signal(int)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._audio_source: QAudioSource | None = None
        self._io_device: QIODevice | None = None
        self._audio = bytearray()
        self._elapsed_seconds = 0
        self._timer = QTimer(self)
        self._timer.setInterval(1_000)
        self._timer.timeout.connect(self._tick)

    @property
    def is_recording(self) -> bool:
        return self._audio_source is not None

    def start(self) -> None:
        if self.is_recording:
            return
        device = QMediaDevices.defaultAudioInput()
        if device.isNull():
            self.recording_failed.emit("没有找到可用麦克风，请检查系统声音设置。")
            return

        audio_format = QAudioFormat()
        audio_format.setSampleRate(VOICE_SAMPLE_RATE)
        audio_format.setChannelCount(1)
        audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if not device.isFormatSupported(audio_format):
            self.recording_failed.emit(
                "当前麦克风不支持离线识别所需的 16 kHz 单声道格式，请改用文字输入。"
            )
            return

        self._audio.clear()
        self._elapsed_seconds = 0
        source = QAudioSource(device, audio_format, self)
        source.setBufferSize(VOICE_SAMPLE_RATE * 2)
        io_device = source.start()
        if io_device is None:
            source.deleteLater()
            self.recording_failed.emit("麦克风启动失败，请检查系统麦克风权限。")
            return
        self._audio_source = source
        self._io_device = io_device
        io_device.readyRead.connect(self._read_available)
        self._timer.start()
        self.remaining_seconds_changed.emit(VOICE_MAX_SECONDS)
        self.recording_started.emit()

    def stop(self) -> None:
        if not self.is_recording:
            return
        self._read_available()
        assert self._audio_source is not None
        self._audio_source.stop()
        self._audio_source.deleteLater()
        self._audio_source = None
        self._io_device = None
        self._timer.stop()
        audio = bytes(self._audio)
        self._audio.clear()
        self.recording_stopped.emit(audio)

    def cancel(self) -> None:
        if not self.is_recording:
            return
        assert self._audio_source is not None
        self._audio_source.stop()
        self._audio_source.deleteLater()
        self._audio_source = None
        self._io_device = None
        self._timer.stop()
        self._audio.clear()

    def _read_available(self) -> None:
        if self._io_device is None:
            return
        data = self._io_device.readAll()
        if data:
            self._audio.extend(bytes(data))

    def _tick(self) -> None:
        self._elapsed_seconds += 1
        remaining = max(0, VOICE_MAX_SECONDS - self._elapsed_seconds)
        self.remaining_seconds_changed.emit(remaining)
        if remaining == 0:
            self.stop()


class VoiceTextDialog(QDialog):
    """Explicit microphone use, local recognition, editable confirmation; no cloud upload."""

    def __init__(self, parent=None, prompt: str = "请说出想填写的内容，录音只在本机识别。"):
        super().__init__(parent)
        self.setWindowTitle("语音输入 · 先听，再确认")
        self.resize(620, 460)
        self._tasks = []
        self.controller = VoiceInputController(self)
        layout = QVBoxLayout(self)
        hint = QLabel(prompt)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.status = QLabel("点击开始说话；最长一分钟，不会自动发送或保存。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.record = QPushButton("🎙 开始说话")
        self.record.setMinimumHeight(52)
        self.record.clicked.connect(self._toggle)
        layout.addWidget(self.record)
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("识别结果会显示在这里，您也可以修改后再使用。")
        self.text.setProperty("voice_input_attached", True)
        layout.addWidget(self.text)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("使用这段文字")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.controller.recording_failed.connect(self._error)
        self.controller.remaining_seconds_changed.connect(
            lambda n: self.status.setText(f"正在听您说话，还可录音 {n} 秒；点击停止开始识别。")
        )
        self.controller.recording_stopped.connect(self._recognize)

    @property
    def transcript_text(self) -> str:
        return self.text.toPlainText().strip()

    def _toggle(self):
        if self.controller.is_recording:
            self.controller.stop()
        else:
            self.controller.start()
            if self.controller.is_recording:
                self.record.setText("停止并识别")

    def _recognize(self, audio: bytes):
        from ..services.voice import OfflineSpeechRecognizer
        from ..workers.task import FunctionTask

        self.record.setEnabled(False)
        self.status.setText("正在本机识别，请稍候…")
        task = FunctionTask(OfflineSpeechRecognizer().transcribe_pcm, audio)
        self._tasks.append(task)
        task.signals.result.connect(self.text.setPlainText)
        task.signals.result.connect(lambda _: self.status.setText("请核对文字，再点击使用。"))
        task.signals.error.connect(lambda _: self._error("没有识别成功，请重试或直接输入文字。"))
        task.signals.finished.connect(lambda: self._finished(task))
        QThreadPool.globalInstance().start(task)

    def _finished(self, task):
        if task in self._tasks:
            self._tasks.remove(task)
        self.record.setText("🎙 重新说话")
        self.record.setEnabled(True)

    def _error(self, message):
        self.status.setText(str(message))
        self.record.setText("🎙 开始说话")

    def done(self, result):
        self.controller.cancel()
        if self._tasks:
            self.status.setText("正在结束识别，请稍候再关闭。")
            return
        super().done(result)


class VoiceInputInstaller(QObject):
    """A visible microphone beside every ordinary editable text control.

    Password fields intentionally never record audio. The app-wide filter also
    covers dynamically opened record/profile dialogs without rewriting layouts.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._widgets = []
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Show, QEvent.Type.Polish) and isinstance(
            watched, (QLineEdit, QTextEdit, QPlainTextEdit)
        ):
            self._attach(watched)
        if event.type() == QEvent.Type.Resize and isinstance(watched, QWidget):
            button = watched.findChild(QToolButton, "InlineVoiceInput")
            if button is not None:
                button.move(max(0, watched.width() - 46), 2)
        return False

    def _attach(self, widget):
        if (
            widget.property("voice_input_attached")
            or widget.property("sensitive_input")
            or widget.isReadOnly()
        ):
            return
        if isinstance(widget, QLineEdit) and widget.echoMode() != QLineEdit.EchoMode.Normal:
            widget.setProperty("sensitive_input", True)
            return
        # Embedded spin/date editors have separate constrained input semantics.
        from PySide6.QtWidgets import QAbstractSpinBox

        if isinstance(widget.parentWidget(), QAbstractSpinBox):
            return
        widget.setProperty("voice_input_attached", True)
        button = QToolButton(widget)
        button.setObjectName("InlineVoiceInput")
        button.setText("🎙")
        button.setToolTip("语音输入（本机识别，确认后填写）")
        button.setAccessibleName("语音输入")
        button.setFixedSize(42, 38)
        button.move(max(0, widget.width() - 46), 2)
        button.clicked.connect(lambda: self._fill(widget))
        if isinstance(widget, QLineEdit):
            widget.setTextMargins(0, 0, 44, 0)
        else:
            widget.setViewportMargins(0, 0, 44, 0)
        button.show()

    def _fill(self, widget):
        # Echo mode can change after construction; never record credentials.
        if widget.property("sensitive_input"):
            return
        if isinstance(widget, QLineEdit) and widget.echoMode() != QLineEdit.EchoMode.Normal:
            return
        dialog = VoiceTextDialog(widget.window())
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.transcript_text:
            if isinstance(widget, QLineEdit):
                widget.insert(dialog.transcript_text)
            else:
                widget.insertPlainText(dialog.transcript_text)


__all__ = ["VoiceInputController", "VoiceTextDialog", "VoiceInputInstaller"]
