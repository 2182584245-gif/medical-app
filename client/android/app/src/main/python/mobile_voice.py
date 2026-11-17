"""Bounded, ephemeral voice upload; credentials never leave the Python facade."""
from __future__ import annotations

import base64
import asyncio
import io
import json
import os
from pathlib import Path
import stat
import struct
import wave

import httpx

MAX_WAV_BYTES = 2 * 1024 * 1024
MAX_FRAMES = 16000 * 60
ASR_CONTRACT = '2'
ASR_ENGINE = 'qwen-asr'
ASR_PROVIDER = 'qwen'
ASR_MODEL = 'qwen-audio-3.1-asr-flash-message'


class VoiceError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def validate_recognized_text(value):
    if (not isinstance(value, str) or len(value) > 4000
            or any(ord(char) < 32 and char not in '\n\t' for char in value)):
        raise ValueError
    value.encode('utf8')
    return value.strip()


def validate_cloud_result(result):
    """Contract 2 never relabels another provider/model as Qwen."""
    if (type(result) is not dict or result.get('engine') != ASR_ENGINE
            or result.get('provider') != ASR_PROVIDER
            or result.get('provider_model') != ASR_MODEL
            or result.get('offline') is not False):
        raise ValueError
    text = validate_recognized_text(result.get('text'))
    if not text:
        raise ValueError
    return {'text': text, 'engine': ASR_ENGINE, 'provider': ASR_PROVIDER,
            'provider_model': ASR_MODEL, 'offline': False}


def read_capture(private_root, capture_path):
    """Only accept a completed native recording in this application's directory."""
    if not isinstance(capture_path, str) or len(capture_path) > 4096:
        raise VoiceError('validation', '录音文件无效，请重新录入。')
    directory = Path(private_root) / 'voice-capture'
    candidate = Path(capture_path)
    if (not candidate.is_absolute() or directory.is_symlink() or candidate.is_symlink()
            or candidate.parent.is_symlink() or candidate.suffix != '.wav'):
        raise VoiceError('validation', '录音位置无效，请重新录入。')
    try:
        # Android may expose the same app-private files through /data/user/0
        # and /data/data. initialize() canonicalizes the root, while Java's
        # getFilesDir() can retain an ancestor alias. Compare actual parents,
        # retaining the ban on a redirected capture directory or leaf file.
        directory = directory.resolve(strict=True)
        if candidate.parent.resolve(strict=True) != directory:
            raise ValueError
        candidate = directory / candidate.name
    except (OSError, RuntimeError, ValueError):
        raise VoiceError('validation', '录音位置无效，请重新录入。') from None
    try:
        fd = os.open(candidate, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or not 44 < info.st_size <= MAX_WAV_BYTES:
                raise ValueError
            data = source.read(MAX_WAV_BYTES + 1)
        if len(data) != info.st_size or len(data) > MAX_WAV_BYTES:
            raise ValueError
        if (data[:4] != b'RIFF' or data[8:12] != b'WAVE'
                or struct.unpack_from('<I', data, 4)[0] + 8 != len(data)):
            raise ValueError
        with wave.open(io.BytesIO(data), 'rb') as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 16000, 'NONE'):
                raise ValueError
            frames = audio.getnframes()
            if not 1 <= frames <= MAX_FRAMES or len(audio.readframes(frames + 1)) != frames * 2:
                raise ValueError
        return data
    except (OSError, EOFError, ValueError, wave.Error, struct.error):
        raise VoiceError('validation', '录音格式或长度无效，请重新录入。') from None


def upload_capture(endpoint, token, data, *, transport=None, cancelled=None, on_partial=None):
    """No redirects, proxy inheritance, retries, disk writes, or body logging."""
    if not isinstance(endpoint, str) or not endpoint.startswith('https://'):
        raise VoiceError('cloud_unavailable', '语音服务暂时不可用，请稍后重新录入。')
    if cancelled is not None and cancelled():
        raise VoiceError('cancelled', '本次语音已取消。')
    async def transfer():
        async with httpx.AsyncClient(verify=True, trust_env=False, follow_redirects=False,
                                     timeout=httpx.Timeout(connect=8, read=90, write=20, pool=5),
                                     transport=transport) as client:
            async with client.stream('POST', endpoint.rstrip('/') + '/v1/member/voice/transcribe',
                               headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/x-ndjson',
                                        'X-Fawnsee-ASR-Contract': ASR_CONTRACT},
                               json={'wav_base64': base64.b64encode(data).decode('ascii')}) as response:
                if response.status_code in (401, 403):
                    raise VoiceError('not_authenticated', '登录状态已失效，请重新登录后录入。')
                if not 200 <= response.status_code < 300:
                    raise VoiceError('cloud_unavailable', '语音服务暂时不可用，请稍后重新录入。')
                media = response.headers.get('content-type', '').split(';')[0]
                if media == 'application/x-ndjson':
                    pending, total, events, final = bytearray(), 0, 0, None
                    def consume(line):
                        nonlocal events, final
                        if not line.strip():
                            return
                        events += 1
                        if events > 2048 or len(line) > 32768 or final is not None:
                            raise ValueError
                        item = json.loads(line)
                        if type(item) is not dict:
                            raise ValueError
                        if item.get('type') == 'status' and item.get('status') == 'recognizing':
                            return
                        if item.get('type') == 'partial':
                            partial = validate_recognized_text(item.get('text'))
                            if cancelled is not None and cancelled():
                                raise VoiceError('cancelled', '本次语音已取消。')
                            if partial and on_partial is not None:
                                on_partial(partial)
                        elif item.get('type') == 'final':
                            final = validate_cloud_result(item)
                        else:
                            raise ValueError
                    # A fixed chunk size buffers short NDJSON events until enough
                    # bytes arrive; consume transport chunks so partials stay live.
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > 4 * 1024 * 1024:
                            raise ValueError
                        pending.extend(chunk)
                        while b'\n' in pending:
                            line, _, rest = pending.partition(b'\n')
                            pending = bytearray(rest)
                            consume(line)
                        if len(pending) > 32768:
                            raise ValueError
                    if pending:
                        consume(pending)
                    if final is None:
                        raise ValueError
                    return final
                if media != 'application/json':
                    raise ValueError
                body = bytearray()
                async for chunk in response.aiter_bytes(4096):
                    if len(body) + len(chunk) > 32768:
                        raise ValueError
                    body.extend(chunk)
                result = json.loads(body)
                return validate_cloud_result(result)

    async def run():
        async def watch_cancel():
            while True:
                if cancelled is not None and cancelled():
                    raise VoiceError('cancelled', '本次语音已取消。')
                await asyncio.sleep(0.1)
        transfer_task = asyncio.create_task(transfer())
        cancel_task = asyncio.create_task(watch_cancel())
        try:
            done, _ = await asyncio.wait((transfer_task, cancel_task), timeout=90,
                                         return_when=asyncio.FIRST_COMPLETED)
            if cancel_task in done:
                return cancel_task.result()
            if transfer_task in done:
                if cancelled is not None and cancelled():
                    raise VoiceError('cancelled', '本次语音已取消。')
                return transfer_task.result()
            raise VoiceError('cloud_unavailable', '语音服务暂时不可用，请稍后重新录入。')
        finally:
            transfer_task.cancel()
            cancel_task.cancel()
            await asyncio.gather(transfer_task, cancel_task, return_exceptions=True)
    try:
        return asyncio.run(run())
    except VoiceError:
        raise
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        raise VoiceError('cloud_unavailable', '语音服务暂时不可用，请稍后重新录入。') from None
