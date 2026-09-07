"""Small Android HTTP adapters: no desktop SDK and no persisted credentials."""

from __future__ import annotations

import base64
import ipaddress
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx

from ollama_chat_app.config import DEEPSEEK_DEFAULT_MODEL
from ollama_chat_app.providers.base import (
    ChatMessage,
    ChatProvider,
    ProviderCapabilities,
    ProviderError,
    extract_chat_content,
    extract_model_names,
)

MODEL_NOTICE = "优先使用账号可用模型；Ollama 免费额度及模型可用性以官网为准，不保证永久免费。"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def validate_local_url(value: str) -> str:
    """Avoid DNS rebinding, public endpoints and cloud-key forwarding."""
    if not isinstance(value, str) or len(value) > 300:
        raise ProviderError("本地 Ollama 地址格式无效")
    try:
        parts = urlsplit(value.strip())
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.path not in {"", "/"}
        ):
            raise ValueError
        host = "127.0.0.1" if parts.hostname.lower() == "localhost" else parts.hostname
        address = ipaddress.ip_address(host)
        private_ranges = (
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
            ipaddress.ip_network("fc00::/7"),
        )
        if not (address.is_loopback or any(address in net for net in private_ranges)):
            raise ValueError
        if address.is_unspecified or address.is_multicast or address.is_link_local:
            raise ValueError
        port = parts.port or 11434
        if not 1 <= port <= 65535:
            raise ValueError
        host_text = f"[{address}]" if address.version == 6 else str(address)
        return f"{parts.scheme}://{host_text}:{port}"
    except (ValueError, TypeError):
        raise ProviderError(
            "请填写回环或局域网 IP 地址，例如 http://192.168.1.10:11434；不能填写公网地址。"
        ) from None


def _model(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 160:
        raise ProviderError("请先选择有效模型")
    clean = value.strip()
    if any(char.isspace() or ord(char) < 32 for char in clean):
        raise ProviderError("模型名称不能含空格或控制字符")
    return clean


class MobileProvider(ChatProvider):
    def __init__(
        self,
        provider: str,
        api_key: str = "",
        local_url: str = "http://127.0.0.1:11434",
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if provider not in {"deepseek", "ollama_cloud", "local"}:
            raise ProviderError("不支持的 AI 服务")
        if not isinstance(api_key, str) or len(api_key) > 4096:
            raise ProviderError("API Key 格式无效")
        clean_key = api_key.strip()
        if clean_key and (not clean_key.isascii() or any(ord(c) < 33 for c in clean_key)):
            raise ProviderError("API Key 不应含空格、换行或中文字符")
        if provider != "local" and not clean_key:
            raise ProviderError("请先在 AI 设置中输入自己的 API Key")
        self.provider = provider
        self._api_key = "" if provider == "local" else clean_key
        self._transport = transport
        self._base_url = {
            "deepseek": "https://api.deepseek.com",
            "ollama_cloud": "https://ollama.com",
        }.get(provider) or validate_local_url(local_url)

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_images=self.provider != "deepseek")

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            with (
                httpx.Client(
                    base_url=self._base_url,
                    headers=headers,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=httpx.Timeout(120, connect=15),
                    transport=self._transport,
                ) as client,
                client.stream(method, path, json=payload) as response,
            ):
                if not 200 <= response.status_code < 300:
                    raise _http_error(response.status_code)
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE_BYTES:
                        raise ProviderError("AI 返回内容过大，已停止接收")
                import json

                return json.loads(content)
        except ProviderError:
            raise
        except httpx.TimeoutException:
            raise ProviderError("等待 AI 响应超时，请检查网络后重试") from None
        except httpx.HTTPError:
            raise ProviderError("无法连接 AI 服务，请检查网络及服务地址") from None
        except (ValueError, TypeError):
            raise ProviderError("AI 服务返回格式无效，请稍后重试") from None

    def list_models(self) -> list[str]:
        if self.provider == "deepseek":
            return [DEEPSEEK_DEFAULT_MODEL]
        data = self._request("GET", "/api/tags")
        names = extract_model_names(data)
        return [_model(name) for name in names[:500]]

    def chat(self, model: str, messages: Sequence[ChatMessage]) -> str:
        model = _model(model)
        if self.provider == "deepseek" and model != DEEPSEEK_DEFAULT_MODEL:
            raise ProviderError("DeepSeek 固定使用 deepseek-v4-flash")
        normalised: list[dict[str, Any]] = []
        for message in messages:
            role, content = message.get("role"), message.get("content")
            if (
                role not in {"user", "assistant", "system"}
                or not isinstance(content, str)
                or not content.strip()
            ):
                raise ProviderError("对话消息格式无效")
            value: dict[str, Any] = {"role": role, "content": content}
            images = message.get("images")
            if images:
                if not self.supports_images:
                    raise ProviderError(
                        "DeepSeek 当前只支持文字，请取消图片或选择支持视觉的 Ollama 模型"
                    )
                value["images"] = [
                    base64.b64encode(item).decode("ascii") if isinstance(item, bytes) else item
                    for item in images
                ]
            normalised.append(value)
        if not normalised:
            raise ProviderError("没有可发送的消息")
        payload: dict[str, Any] = {"model": model, "messages": normalised, "stream": False}
        if self.provider == "deepseek":
            payload["thinking"] = {"type": "disabled"}
            payload["max_tokens"] = 4096
            data = self._request("POST", "/chat/completions", payload)
            try:
                result = data["choices"][0]["message"]["content"]
                if not isinstance(result, str) or not result.strip():
                    raise ValueError
                return result
            except (KeyError, IndexError, TypeError, ValueError):
                raise ProviderError("DeepSeek 返回了无法识别的空响应") from None
        return extract_chat_content(self._request("POST", "/api/chat", payload))


def _http_error(status: int) -> ProviderError:
    messages = {
        400: "AI 请求格式未被接受",
        401: "API Key 无效或已失效，请重新输入",
        402: "云端余额或额度不足，请前往官网检查",
        403: "当前账号无权使用此服务或模型",
        404: "模型或接口不存在，请重新获取模型列表",
        408: "AI 服务响应超时",
        429: "请求过于频繁或免费额度已用完，请稍后重试",
    }
    message = messages.get(
        status, "AI 服务暂时不可用，请稍后重试" if status >= 500 else "AI 服务未接受本次请求"
    )
    return ProviderError(message, status_code=status)
