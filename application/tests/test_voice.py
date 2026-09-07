from __future__ import annotations

import json
import sys
import types

import pytest

from ollama_chat_app.services.voice import (
    VOICE_REQUIRED_FILES,
    OfflineSpeechRecognizer,
    VoiceRecognitionError,
    _vosk_compatible_model_path,
)


def test_voice_rejects_too_short_audio(tmp_path) -> None:
    model_path = tmp_path / "model"
    model_path.mkdir()
    recognizer = OfflineSpeechRecognizer(model_path)

    with pytest.raises(VoiceRecognitionError, match="录音太短"):
        recognizer.transcribe_pcm(b"\0" * 100)


def test_voice_rejects_missing_model(tmp_path) -> None:
    recognizer = OfflineSpeechRecognizer(tmp_path / "missing")

    with pytest.raises(VoiceRecognitionError, match="语音模型缺失"):
        recognizer.transcribe_pcm(b"\0" * 16_000)


def test_voice_transcribes_locally_and_joins_chinese_spaces(tmp_path, monkeypatch) -> None:
    model_path = tmp_path / "model"
    model_path.mkdir()

    class FakeModel:
        def __init__(self, path: str) -> None:
            self.path = path

    class FakeRecognizer:
        chunks: list[bytes] = []

        def __init__(self, model: object, sample_rate: float) -> None:
            assert isinstance(model, FakeModel)
            assert sample_rate == 16_000.0

        def AcceptWaveform(self, value: bytes) -> bool:
            self.chunks.append(value)
            return True

        def FinalResult(self) -> str:
            return json.dumps({"text": "今 天 走 路 三 千 步"}, ensure_ascii=False)

    fake_vosk = types.SimpleNamespace(
        KaldiRecognizer=FakeRecognizer,
        Model=FakeModel,
        SetLogLevel=lambda _level: None,
    )
    monkeypatch.setitem(sys.modules, "vosk", fake_vosk)
    OfflineSpeechRecognizer._model = None
    OfflineSpeechRecognizer._model_path = None

    result = OfflineSpeechRecognizer(model_path).transcribe_pcm(b"\0" * 16_000)

    assert result == "今天走路三千步"
    assert sum(len(chunk) for chunk in FakeRecognizer.chunks) == 16_000


def test_voice_rejects_unsupported_sample_rate(tmp_path) -> None:
    model_path = tmp_path / "model"
    model_path.mkdir()

    with pytest.raises(VoiceRecognitionError, match="16 kHz"):
        OfflineSpeechRecognizer(model_path).transcribe_pcm(b"\0" * 16_000, 44_100)


def test_unicode_windows_model_is_copied_to_verified_ascii_cache(tmp_path, monkeypatch) -> None:
    source = tmp_path / "中文模型"
    for index, relative_name in enumerate(sorted(VOICE_REQUIRED_FILES)):
        path = source / relative_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"model-file-{index}".encode())
    program_data = tmp_path / "ProgramData"
    monkeypatch.setenv("PROGRAMDATA", str(program_data))
    monkeypatch.setattr("ollama_chat_app.services.voice.sys.platform", "win32")

    cached = _vosk_compatible_model_path(source)

    assert str(cached).isascii()
    assert cached.is_dir()
    cached_files = {
        path.relative_to(cached).as_posix() for path in cached.rglob("*") if path.is_file()
    }
    assert cached_files == set(VOICE_REQUIRED_FILES)
    assert _vosk_compatible_model_path(source) == cached
