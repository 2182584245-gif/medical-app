"""Member-authenticated, memory-only Qwen ASR gateway with legacy wire compatibility."""
from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import struct
import time
import wave
from collections import OrderedDict, deque
from contextlib import aclosing

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .platform_auth import authentication_dependency
from .qwen_asr import QwenASRAdapter, QwenASRError, MODEL as QWEN_MODEL, text as qwen_text


ASR_PATH = "/v1/member/voice/transcribe"
MAX_WAV_BYTES = 2 * 1024 * 1024
MAX_BASE64_CHARS = 4 * ((MAX_WAV_BYTES + 2) // 3)
MAX_ASR_JSON_BYTES = MAX_BASE64_CHARS + 1024
MAX_AUDIO_SECONDS = 60
MAX_WORKER_RESPONSE_BYTES = 32 * 1024
MAX_WORKER_STREAM_BYTES = 4 * 1024 * 1024
MAX_WORKER_STREAM_EVENTS = 2048
WORKER_TIMEOUT_SECONDS = 90
ASR_QUEUE_TIMEOUT_SECONDS = 30
ASR_MAX_WAITERS = 2


def _failure(status: int, message: str, retry_after: int | None = None):
    headers = {"Cache-Control": "no-store"}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return HTTPException(status, message, headers=headers)


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def decode_wav(body: bytes) -> bytes:
    """Accept a bounded standard WAV envelope; never log payloads or errors."""
    if len(body) > MAX_ASR_JSON_BYTES:
        raise _failure(413, "录音文件过大，请缩短后重试")
    try:
        payload = json.loads(body, object_pairs_hook=_unique_fields)
    except (ValueError, UnicodeError, RecursionError):
        raise _failure(422, "录音数据格式无效，请重新录音") from None
    if not isinstance(payload, dict) or set(payload) != {"wav_base64"}:
        raise _failure(422, "录音数据格式无效，请重新录音")
    encoded = payload["wav_base64"]
    if not isinstance(encoded, str) or not encoded:
        raise _failure(422, "录音数据格式无效，请重新录音")
    if len(encoded) > MAX_BASE64_CHARS:
        raise _failure(413, "录音文件过大，请缩短后重试")
    try:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (ValueError, UnicodeError, binascii.Error):
        raise _failure(422, "录音数据格式无效，请重新录音") from None
    if len(raw) > MAX_WAV_BYTES:
        raise _failure(413, "录音文件过大，请缩短后重试")
    try:
        if (len(raw) < 44 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE"
                or struct.unpack_from("<I", raw, 4)[0] + 8 != len(raw)):
            raise ValueError("invalid WAV envelope")
        with wave.open(io.BytesIO(raw), "rb") as audio:
            frames = audio.getnframes()
            if (audio.getframerate() != 16000 or audio.getnchannels() != 1
                    or audio.getsampwidth() != 2 or audio.getcomptype() != "NONE"
                    or not 0 < frames <= MAX_AUDIO_SECONDS * 16000):
                raise ValueError("unsupported audio")
            if len(audio.readframes(frames + 1)) != frames * 2:
                raise ValueError("truncated audio")
    except (ValueError, wave.Error, EOFError, struct.error):
        raise _failure(422, "请使用六十秒以内的十六千赫单声道录音") from None
    return raw


class RecognitionQuota:
    """Bounded per-process limits; the worker also enforces global concurrency."""

    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.recent: OrderedDict[int, deque[float]] = OrderedDict()

    def consume(self, actor_id: int):
        now = self.clock()
        cutoff = now - 60
        while self.recent:
            oldest = next(iter(self.recent))
            if self.recent[oldest][-1] > cutoff:
                break
            self.recent.popitem(last=False)
        history = self.recent.get(actor_id)
        if history is None:
            if len(self.recent) >= 4096:
                raise _failure(503, "语音识别暂时繁忙，请稍后重试", 10)
            history = deque()
            self.recent[actor_id] = history
        while history and history[0] <= cutoff:
            history.popleft()
        if len(history) >= 6:
            raise _failure(429, "操作较频繁，请稍后再试", max(1, int(history[0] + 60 - now) + 1))
        history.append(now)
        self.recent.move_to_end(actor_id)


class VoiceStreamingResponse(StreamingResponse):
    """Keep the actor context and recognition lease until completion/disconnect."""

    def __init__(self, iterator, gate, identity_context, release=None):
        super().__init__(iterator, media_type="application/x-ndjson",
                         headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        self.gate = gate
        self.identity_context = identity_context
        self.release = release or gate.release

    async def __call__(self, scope, receive, send):
        try:
            with self.identity_context():
                await super().__call__(scope, receive, send)
        finally:
            # __call__ owns release even if cancellation precedes the first yield.
            try:
                closer = getattr(self.body_iterator, 'aclose', None)
                if closer is not None:
                    await closer()
            finally:
                self.release()


class QwenASRGateway:
    """Four bounded network recognitions; no local model or paid retry."""
    def __init__(self, *, adapter=None, quota=None):
        self.adapter = adapter or QwenASRAdapter()
        self.quota = quota or RecognitionQuota()
        self.gate = asyncio.Semaphore(4)
        self.pending = 0

    def _reserve(self, actor_id=None):
        if self.pending >= 6:
            raise _failure(503, "语音识别暂时繁忙，请稍后重试", 2)
        if actor_id is not None:
            self.quota.consume(actor_id)
        self.pending += 1
        return {"acquired": False, "released": False, "waiting": self.pending > 4}

    def _release(self, ticket):
        if ticket["released"]:
            return
        ticket["released"] = True
        self.pending -= 1
        if ticket["acquired"]:
            self.gate.release()

    async def _acquire(self, actor_id, ticket):
        try:
            await asyncio.wait_for(self.gate.acquire(), ASR_QUEUE_TIMEOUT_SECONDS)
            ticket["acquired"] = True
        except asyncio.TimeoutError:
            self._release(ticket)
            raise _failure(504, "语音识别等待超时，请稍后重试") from None
        except BaseException:
            self._release(ticket)
            raise

    @staticmethod
    def result(text, contract):
        result = {"text": text, "engine": "qwen-asr" if contract == 2 else "fun-asr-nano",
                  "provider": "qwen", "provider_model": QWEN_MODEL, "offline": False}
        if contract != 2:
            result["engine_compat"] = True
        return result

    async def _events(self, wav, contract):
        final = None
        async with aclosing(self.adapter.events(wav)) as events:
            async for row in events:
                if final is not None:
                    raise QwenASRError("invalid_response")
                kind = row.get("type")
                if kind == "status" and row.get("status") == "recognizing":
                    yield {"type": "status", "status": "recognizing"}
                elif kind == "partial":
                    # Qwen revises current sentence text. Forward cumulative
                    # snapshots, not an append-only prefix assumption.
                    yield {"type": "partial", "text": qwen_text(row.get("text"))}
                elif kind == "final":
                    if row.get("provider") != "qwen" or row.get("provider_model") != QWEN_MODEL or row.get("offline") is not False:
                        raise QwenASRError("invalid_response")
                    final = {"type": "final", **self.result(qwen_text(row.get("text")), contract)}
                else:
                    raise QwenASRError("invalid_response")
        if final is None:
            raise QwenASRError("invalid_response")
        yield final

    async def transcribe(self, actor_id, wav, contract=1):
        ticket = self._reserve(actor_id)
        try:
            async with asyncio.timeout(WORKER_TIMEOUT_SECONDS):
                await self._acquire(actor_id, ticket)
                final = None
                async with aclosing(self._events(wav, contract)) as events:
                    async for event in events:
                        if event["type"] == "final":
                            final = {key: value for key, value in event.items() if key != "type"}
                if final is None:
                    raise QwenASRError("invalid_response")
                return final
        except TimeoutError:
            raise _failure(504, "语音识别超时，请稍后重试") from None
        except QwenASRError as error:
            raise _failure(504 if error.code == "timeout" else 503, "语音识别暂时不可用，请稍后重试") from None
        finally:
            self._release(ticket)

    async def _stream(self, wav, contract=1):
        try:
            async with aclosing(self._events(wav, contract)) as events:
                async for row in events:
                    yield (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf8")
        except QwenASRError as error:
            yield (json.dumps({"type": "error", "code": "timeout" if error.code == "timeout" else "unavailable"}) + "\n").encode()

    async def streaming_response(self, actor_id, wav, identity_context, contract=1):
        ticket = self._reserve(actor_id)
        async def admitted():
            if ticket["waiting"]:
                yield b'{"type":"status","status":"recognizing","queue_state":"waiting"}\n'
            try:
                async with asyncio.timeout(WORKER_TIMEOUT_SECONDS):
                    await self._acquire(actor_id, ticket)
                    async with aclosing(self._stream(wav, contract)) as events:
                        async for event in events:
                            yield event
            except TimeoutError:
                yield b'{"type":"error","code":"timeout"}\n'
            except HTTPException as error:
                yield (json.dumps({"type": "error", "code": "timeout" if error.status_code == 504 else "unavailable"}) + "\n").encode()
        return VoiceStreamingResponse(admitted(), self.gate, identity_context, release=lambda: self._release(ticket))


# Keep internal import compatibility only; this alias never invokes FunASR.
FreeASRGateway = QwenASRGateway


def asr_contract(request):
    header = request.headers.get("x-fawnsee-asr-contract")
    if header is None:
        return 1
    if header == "2":
        return 2
    raise _failure(422, "语音请求版本不支持，请更新应用")


def create_voice_router(auth, *, gateway=None) -> APIRouter:
    router = APIRouter(prefix="/v1/member/voice", tags=["member"])
    principal = Depends(authentication_dependency(auth))
    recognizer = gateway or QwenASRGateway()

    @router.post("/transcribe")
    async def transcribe(request: Request, identity=principal):
        if identity.user.role_code != "member":
            raise _failure(403, "当前账号无权使用此功能")
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
            raise _failure(415, "请使用录音数据格式上传")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_ASR_JSON_BYTES:
                raise _failure(413, "录音文件过大，请缩短后重试")
            body.extend(chunk)
        wav = await asyncio.to_thread(decode_wav, bytes(body))
        contract = asr_contract(request)
        accepted = [part.strip().lower().split(";", 1)[0] for part in request.headers.get("accept", "").split(",")]
        if "application/x-ndjson" in accepted:
            with auth.identity(identity):
                return await recognizer.streaming_response(identity.user.id, wav, lambda: auth.identity(identity), contract)
        with auth.identity(identity):
            result = await recognizer.transcribe(identity.user.id, wav, contract)
        return JSONResponse(result,
                            headers={"Cache-Control": "no-store"})

    return router
