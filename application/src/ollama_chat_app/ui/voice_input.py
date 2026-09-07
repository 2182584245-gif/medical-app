from __future__ import annotations

from PySide6.QtCore import QIODevice, QObject, QTimer, Signal
from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices

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


__all__ = ["VoiceInputController"]
