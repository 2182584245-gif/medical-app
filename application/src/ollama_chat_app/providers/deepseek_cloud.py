from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from ollama_chat_app import config
from ollama_chat_app.providers.base import (
    ChatMessage,
    ChatProvider,
    ProviderCapabilities,
    ProviderError,
)


class DeepSeekCloudProvider(ChatProvider):
    """OpenAI-compatible adapter for the official DeepSeek API."""

    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        clean_key = api_key.strip()
        if not clean_key:
            raise ProviderError(
                "请先保存 DeepSeek API Key。",
                code="missing_api_key",
            )
        self._api_key = clean_key
        self._transport = transport
        self._last_used_model: str | None = None
        self._last_response_model: str | None = None

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_images=True)

    @property
    def last_used_model(self) -> str | None:
        """Model sent by the latest successful call; reset before each attempt."""

        return self._last_used_model

    @property
    def last_response_model(self) -> str | None:
        """Server-reported model, when present in a successful API response."""

        return self._last_response_model

    def resolve_model(self, model: str, messages: Sequence[ChatMessage]) -> str:
        """Return the request model locally, including images in earlier turns.

        Callers can persist this value with a pending request for audit purposes.
        This selects the model only; ``chat`` also validates the image bytes.
        """

        model_name = model.strip() if isinstance(model, str) else ""
        if model_name not in config.DEEPSEEK_SUPPORTED_MODELS:
            supported = "、".join(config.DEEPSEEK_SUPPORTED_MODELS)
            raise ProviderError(f"当前版本仅支持 DeepSeek 模型 {supported}。", code="invalid_model")
        has_images = False
        for message in messages:
            if not isinstance(message, Mapping):
                raise ProviderError("对话中的消息格式无效。", code="invalid_message")
            if _message_images(message):
                has_images = True
        return config.DEEPSEEK_VISION_MODEL if has_images else model_name

    def validate_request(self, model: str, messages: Sequence[ChatMessage]) -> str:
        """Fully validate locally before a caller persists a new chat exchange.

        Includes history images and the exact encoded request size. No network
        request is sent and successful-call audit metadata remains unchanged.
        """

        model_name = self.resolve_model(model, messages)
        _completion_request_body(model_name, messages, thinking={"type": "disabled"})
        return model_name

    def chat(
        self,
        model: str,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int | None = None,
    ) -> str:
        self._last_used_model = None
        self._last_response_model = None
        model_name = self.resolve_model(model, messages)
        return self._create_completion(
            model=model_name,
            messages=messages,
            thinking={"type": "disabled"},
            max_tokens=max_tokens,
        )

    def verify_key(self) -> str:
        """Verify a key with one deliberately tiny non-thinking completion."""

        return self._create_completion(
            model=config.DEEPSEEK_DEFAULT_MODEL,
            messages=[{"role": "user", "content": "仅回复 OK"}],
            thinking={"type": "disabled"},
            max_tokens=2,
        )

    def _create_completion(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        thinking: Mapping[str, str],
        max_tokens: int | None = None,
    ) -> str:
        self._last_used_model = None
        self._last_response_model = None
        request_body = _completion_request_body(
            model, messages, thinking=thinking, max_tokens=max_tokens
        )

        try:
            with httpx.Client(
                base_url=config.DEEPSEEK_API_BASE_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                timeout=config.REQUEST_TIMEOUT_SECONDS,
                transport=self._transport,
            ) as client:
                response = client.post("/chat/completions", content=request_body)
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "等待 DeepSeek 响应超时，请检查网络后重试。",
                code="timeout",
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                "无法连接 DeepSeek，请检查网络后重试。",
                code="connection_error",
            ) from exc

        if response.status_code >= 400:
            raise _translate_http_error(
                response.status_code, vision=model == config.DEEPSEEK_VISION_MODEL
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(
                "DeepSeek 返回了无法识别的响应。",
                code="invalid_response",
            ) from exc
        content = _extract_completion_content(data)
        self._last_used_model = model
        reported_model = data.get("model")
        self._last_response_model = (
            reported_model.strip()
            if isinstance(reported_model, str) and reported_model.strip()
            else None
        )
        return content


def _completion_request_body(
    model: str,
    messages: Sequence[ChatMessage],
    *,
    thinking: Mapping[str, str],
    max_tokens: int | None = None,
) -> bytes:
    if max_tokens is not None and (
        isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1
    ):
        raise ProviderError("输出长度预算必须是正整数。", code="invalid_parameters")
    payload: dict[str, Any] = {
        "model": model,
        "messages": _normalise_messages(messages),
        "stream": False,
        "thinking": dict(thinking),
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    request_body = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(request_body) > config.DEEPSEEK_MAX_REQUEST_BYTES:
        raise ProviderError(
            "本次图片、历史附件和对话编码后超过 DeepSeek 的 48 MiB 请求上限。"
            "请减少本次附件，或新建对话后仅附加需要的图片。",
            code="request_too_large",
        )
    return request_body


def _message_images(message: ChatMessage) -> Sequence[str | bytes | bytearray]:
    images = message.get("images")
    if images is None:
        return ()
    if isinstance(images, (str, bytes, bytearray)) or not isinstance(images, Sequence):
        raise ProviderError("图片附件格式无效，请重新选择图片。", code="invalid_image")
    if images and message.get("role") != "user":
        raise ProviderError("DeepSeek 只接受用户消息中的图片。", code="invalid_message")
    return images


def _normalise_messages(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    normalised: list[dict[str, Any]] = []
    image_count = sum(len(_message_images(message)) for message in messages)
    if image_count > config.DEEPSEEK_MAX_IMAGES:
        raise ProviderError("DeepSeek 单次请求最多支持 600 张图片。", code="too_many_images")
    max_edge = (
        config.DEEPSEEK_MANY_IMAGES_MAX_EDGE
        if image_count >= config.DEEPSEEK_MANY_IMAGES_THRESHOLD
        else config.DEEPSEEK_MAX_IMAGE_EDGE
    )
    total_image_bytes = 0
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        images = _message_images(message)
        if not isinstance(role, str) or role not in {"system", "user", "assistant"}:
            raise ProviderError("对话中包含 DeepSeek 不支持的消息角色。", code="invalid_message")
        if not isinstance(content, str) or (not content.strip() and not images):
            raise ProviderError("对话中包含空消息，无法发送给 DeepSeek。", code="invalid_message")
        if not images:
            normalised.append({"role": role, "content": content})
            continue
        parts: list[dict[str, Any]] = []
        if content.strip():
            parts.append({"type": "text", "text": content})
        for encoded_image in images:
            data_url, image_bytes = _image_data_url(encoded_image, max_edge=max_edge)
            total_image_bytes += image_bytes
            if total_image_bytes > config.DEEPSEEK_MAX_TOTAL_IMAGE_BYTES:
                raise ProviderError(
                    "DeepSeek 单次请求的图片原始总大小不能超过 64 MiB，请减少图片。",
                    code="images_too_large",
                )
            parts.append({"type": "image_url", "image_url": {"url": data_url}})
        normalised.append({"role": role, "content": parts})
    if not normalised:
        raise ProviderError("没有可发送给 DeepSeek 的消息。", code="invalid_message")
    return normalised


def _image_data_url(encoded: Any, *, max_edge: int) -> tuple[str, int]:
    """Validate local image bytes and preserve them without OCR or recompression."""

    if isinstance(encoded, (bytes, bytearray)):
        if len(encoded) > config.DEEPSEEK_MAX_IMAGE_BYTES:
            raise ProviderError("DeepSeek 单张图片不能超过 32 MiB。", code="image_too_large")
        raw = bytes(encoded)
        mime = _validate_image(raw, max_edge=max_edge)
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}", len(raw)
    if not isinstance(encoded, str) or not encoded:
        raise ProviderError("图片内容为空或格式无效，请重新选择图片。", code="invalid_image")
    maximum_encoded = 4 * ((config.DEEPSEEK_MAX_IMAGE_BYTES + 2) // 3)
    if len(encoded) > maximum_encoded + 64:
        raise ProviderError("DeepSeek 单张图片不能超过 32 MiB。", code="image_too_large")
    declared_mime: str | None = None
    if encoded.startswith("data:"):
        header, separator, encoded = encoded.partition(",")
        if not separator or header not in {
            "data:image/jpeg;base64",
            "data:image/png;base64",
            "data:image/gif;base64",
            "data:image/webp;base64",
        }:
            raise ProviderError("图片编码格式无效，请重新选择图片。", code="invalid_image")
        declared_mime = header[5:].split(";", 1)[0]
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProviderError("图片编码损坏，请重新选择图片。", code="invalid_image") from exc
    if len(raw) > config.DEEPSEEK_MAX_IMAGE_BYTES:
        raise ProviderError("DeepSeek 单张图片不能超过 32 MiB。", code="image_too_large")
    mime = _validate_image(raw, max_edge=max_edge)
    if declared_mime is not None and declared_mime != mime:
        raise ProviderError("图片声明的格式与实际内容不一致。", code="invalid_image")
    return f"data:{mime};base64,{encoded}", len(raw)


def _validate_image(raw: bytes, *, max_edge: int) -> str:
    # Delayed import keeps existing text-only clients independent of Qt.
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QImageReader

    buffer = QBuffer()
    buffer.setData(QByteArray(raw))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    try:
        reader = QImageReader(buffer)
        format_name = bytes(reader.format()).lower()
        mime = {
            b"jpeg": "image/jpeg",
            b"jpg": "image/jpeg",
            b"png": "image/png",
            b"gif": "image/gif",
            b"webp": "image/webp",
        }.get(format_name)
        if mime is None:
            raise ProviderError(
                "DeepSeek 识图仅支持真实的 JPG、PNG、GIF、WEBP 图片，请检查文件内容。",
                code="invalid_image",
            )
        size = reader.size()
        if not size.isValid() or size.isEmpty():
            raise ProviderError("图片损坏，无法读取尺寸，请重新选择图片。", code="invalid_image")
        if max(size.width(), size.height()) > max_edge:
            raise ProviderError(
                f"DeepSeek 本次请求的图片单边不能超过 {max_edge} 像素，请缩小图片后重试。",
                code="image_dimensions_exceeded",
            )
        if reader.read().isNull():
            raise ProviderError("图片无法完整读取，请重新选择或缩小图片。", code="invalid_image")
        return mime
    finally:
        buffer.close()


def _extract_completion_content(data: Any) -> str:
    if not isinstance(data, Mapping):
        raise ProviderError("DeepSeek 返回了无法识别的响应。", code="invalid_response")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderError("DeepSeek 返回了空响应，请稍后重试。", code="invalid_response")
    first = choices[0]
    message = first.get("message") if isinstance(first, Mapping) else None
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, str) or not content.strip():
        raise ProviderError("DeepSeek 返回了空响应，请稍后重试。", code="invalid_response")
    return content


def _translate_http_error(status_code: int, *, vision: bool = False) -> ProviderError:
    messages = {
        400: ("DeepSeek 未接受请求格式，请稍后重试。", "bad_request"),
        401: ("DeepSeek API Key 无效或已失效，请重新输入。", "authentication_error"),
        402: ("DeepSeek 账户余额不足，请先在官方平台充值或检查额度。", "insufficient_balance"),
        403: ("当前 DeepSeek 账户无权执行此操作，请检查账号状态。", "permission_denied"),
        408: ("等待 DeepSeek 响应超时，请检查网络后重试。", "timeout"),
        413: ("图片和对话超过 DeepSeek 请求大小限制，请减少图片后重试。", "request_too_large"),
        422: ("DeepSeek 未接受本次参数，请稍后重试。", "invalid_parameters"),
        429: ("DeepSeek 请求过于频繁，请稍后再试。", "rate_limited"),
    }
    if vision:
        messages.update(
            {
                400: ("DeepSeek 未接受图片或参数，请检查图片内容并重试。", "bad_request"),
                403: (
                    "当前账号无法使用 DeepSeek 视觉实验模型，请检查官方账号权限后重试。",
                    "permission_denied",
                ),
                404: (
                    "DeepSeek 视觉实验模型暂不可用，请稍后重试或查看官方模型说明。",
                    "model_unavailable",
                ),
                422: (
                    "DeepSeek 未接受图片或参数，请减少图片或缩小图片后重试。",
                    "invalid_parameters",
                ),
            }
        )
    if status_code in messages:
        message, code = messages[status_code]
    elif status_code >= 500:
        message, code = "DeepSeek 服务暂时不可用，请稍后再试。", "service_unavailable"
    else:
        message, code = "DeepSeek 未接受本次请求，请检查账号状态。", "request_rejected"
    return ProviderError(message, code=code, status_code=status_code)


DeepSeekProvider = DeepSeekCloudProvider
