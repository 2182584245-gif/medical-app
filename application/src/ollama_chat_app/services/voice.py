from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

VOICE_SAMPLE_RATE = 16_000
VOICE_MAX_SECONDS = 60
VOICE_MODEL_DIRNAME = "vosk-model-small-cn-0.22"
VOICE_CACHE_APP_DIR = "HealthLifeServicePlatform"
VOICE_REQUIRED_FILES = frozenset(
    {
        "am/final.mdl",
        "conf/mfcc.conf",
        "conf/model.conf",
        "graph/Gr.fst",
        "graph/HCLr.fst",
        "graph/disambig_tid.int",
        "graph/phones/word_boundary.int",
        "ivector/final.dubm",
        "ivector/final.ie",
        "ivector/final.mat",
        "ivector/global_cmvn.stats",
        "ivector/online_cmvn.conf",
        "ivector/splice.conf",
        "README",
    }
)


class VoiceRecognitionError(RuntimeError):
    """A user-facing failure while performing offline speech recognition."""


def bundled_voice_model_path() -> Path:
    """Locate the Chinese Vosk model in source and PyInstaller builds."""

    if bool(getattr(sys, "frozen", False)):
        bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return bundle_root / "assets" / VOICE_MODEL_DIRNAME
    return Path(__file__).resolve().parents[3] / "assets" / VOICE_MODEL_DIRNAME


class OfflineSpeechRecognizer:
    """Convert 16 kHz, 16-bit, mono PCM audio to Chinese text locally."""

    _model: object | None = None
    _model_path: Path | None = None

    def __init__(self, model_path: Path | None = None) -> None:
        self.model_path = (model_path or bundled_voice_model_path()).resolve()

    def transcribe_pcm(self, audio: bytes, sample_rate: int = VOICE_SAMPLE_RATE) -> str:
        if sample_rate != VOICE_SAMPLE_RATE:
            raise VoiceRecognitionError("麦克风不支持本应用所需的 16 kHz 录音格式。")
        if not isinstance(audio, bytes) or len(audio) < sample_rate // 2:
            raise VoiceRecognitionError("录音太短，请靠近麦克风并说一句完整的话。")
        if not self.model_path.is_dir():
            raise VoiceRecognitionError("离线中文语音模型缺失，请重新解压完整应用。")

        runtime_model_path = _vosk_compatible_model_path(self.model_path)

        try:
            from vosk import KaldiRecognizer, Model, SetLogLevel
        except Exception as exc:  # pragma: no cover - packaging failure
            raise VoiceRecognitionError("离线语音识别组件未正确安装。") from exc

        try:
            SetLogLevel(-1)
            if self.__class__._model is None or self.__class__._model_path != runtime_model_path:
                self.__class__._model = Model(str(runtime_model_path))
                self.__class__._model_path = runtime_model_path
            recognizer = KaldiRecognizer(self.__class__._model, float(sample_rate))
            for offset in range(0, len(audio), 8_000):
                recognizer.AcceptWaveform(audio[offset : offset + 8_000])
            payload = json.loads(recognizer.FinalResult())
        except VoiceRecognitionError:
            raise
        except Exception as exc:
            raise VoiceRecognitionError("语音识别失败，请稍后重试或改用文字输入。") from exc

        text = str(payload.get("text", "")).strip()
        text = self._join_cjk_spaces(text)
        if not text:
            raise VoiceRecognitionError("没有识别到清晰内容，请靠近麦克风后重试。")
        return text

    @staticmethod
    def _join_cjk_spaces(text: str) -> str:
        previous = None
        while previous != text:
            previous = text
            text = re.sub(r"([\u3400-\u9fff])\s+([\u3400-\u9fff])", r"\1\2", text)
        return re.sub(r"\s+", " ", text).strip()


def _vosk_compatible_model_path(source: Path) -> Path:
    """Return an ASCII path because the Windows Vosk DLL cannot open Unicode paths."""

    source = source.resolve()
    if str(source).isascii() or sys.platform != "win32":
        return source
    manifest = _model_manifest(source)
    fingerprint = hashlib.sha256(
        "\n".join(f"{name}:{digest}" for name, digest in manifest.items()).encode()
    ).hexdigest()[:16]
    program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")).resolve()
    cache_root = program_data / VOICE_CACHE_APP_DIR / "VoiceModels"
    target = cache_root / f"{VOICE_MODEL_DIRNAME}-{fingerprint}"
    if not str(target).isascii():
        raise VoiceRecognitionError("系统语音模型缓存路径不兼容，请改用纯英文 Windows 用户路径。")
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
        if cache_root.is_symlink() or not cache_root.resolve().is_relative_to(program_data):
            raise VoiceRecognitionError("离线语音模型缓存目录不安全。")
        if target.exists():
            if target.is_symlink() or _model_manifest(target) != manifest:
                raise VoiceRecognitionError("离线语音模型缓存已损坏，请联系应用维护者处理。")
            return target
        with tempfile.TemporaryDirectory(prefix=".voice-model-stage-", dir=cache_root) as stage:
            staged_model = Path(stage) / VOICE_MODEL_DIRNAME
            shutil.copytree(source, staged_model)
            if _model_manifest(staged_model) != manifest:
                raise VoiceRecognitionError("离线语音模型复制校验失败。")
            try:
                os.replace(staged_model, target)
            except FileExistsError:
                if target.is_symlink() or _model_manifest(target) != manifest:
                    raise VoiceRecognitionError("离线语音模型缓存发生冲突。") from None
        return target
    except VoiceRecognitionError:
        raise
    except OSError as exc:
        raise VoiceRecognitionError(
            "无法准备离线语音模型缓存，请确认系统公共数据目录可写。"
        ) from exc


def _model_manifest(root: Path) -> dict[str, str]:
    if not root.is_dir() or root.is_symlink():
        raise VoiceRecognitionError("离线中文语音模型目录无效。")
    files = sorted(path for path in root.rglob("*") if path.is_file())
    relative_names = {path.relative_to(root).as_posix() for path in files}
    if relative_names != VOICE_REQUIRED_FILES or any(path.is_symlink() for path in files):
        raise VoiceRecognitionError("离线中文语音模型文件不完整。")
    result: dict[str, str] = {}
    for path in files:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        result[path.relative_to(root).as_posix()] = digest.hexdigest()
    return result


__all__ = [
    "OfflineSpeechRecognizer",
    "VOICE_MAX_SECONDS",
    "VOICE_MODEL_DIRNAME",
    "VOICE_SAMPLE_RATE",
    "VoiceRecognitionError",
    "bundled_voice_model_path",
]
