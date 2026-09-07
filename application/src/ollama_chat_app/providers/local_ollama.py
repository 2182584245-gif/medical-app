from __future__ import annotations

from collections.abc import Sequence

import ollama

from ollama_chat_app import config
from ollama_chat_app.providers.base import (
    ChatMessage,
    ChatProvider,
    ProviderCapabilities,
    ProviderError,
    exception_status_code,
    extract_chat_content,
    is_timeout_error,
)


class LocalOllamaProvider(ChatProvider):
    """Chat with the user's local Ollama service.

    Deliberately, construction performs no network work. The provider neither
    probes service health nor reads the local model list; its sole connection
    attempt happens when ``chat`` is explicitly called.
    """

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_images=True)

    def chat(self, model: str, messages: Sequence[ChatMessage]) -> str:
        model_name = model.strip()
        if not model_name:
            raise ProviderError("请选择一个本地模型。", code="invalid_model")

        try:
            client = ollama.Client(
                host=config.LOCAL_OLLAMA_HOST,
                timeout=config.REQUEST_TIMEOUT_SECONDS,
            )
            response = client.chat(
                model=model_name,
                messages=[dict(message) for message in messages],
                stream=False,
            )
        except ProviderError:
            raise
        except Exception as exc:
            raise _translate_local_error(exc, model_name) from exc

        return extract_chat_content(response)


def _translate_local_error(exc: Exception, model: str) -> ProviderError:
    status = exception_status_code(exc)
    if status == 404:
        return ProviderError(
            f"本地模型“{model}”尚未下载或名称不正确。",
            code="model_not_found",
            status_code=status,
        )
    if status == 429:
        return ProviderError(
            "本机 Ollama 当前过于繁忙，请稍后再试。",
            code="rate_limited",
            status_code=status,
        )
    if status is not None and status >= 500:
        return ProviderError(
            "本机 Ollama 暂时无法完成请求，请稍后再试。",
            code="service_unavailable",
            status_code=status,
        )
    if is_timeout_error(exc):
        return ProviderError(
            "等待本机 Ollama 响应超时；模型可能仍在加载，请稍后重试。",
            code="timeout",
            status_code=status,
        )
    return ProviderError(
        "无法连接本机 Ollama，请确认 Ollama 已安装并正在运行。",
        code="connection_error",
        status_code=status,
    )


# Short alias kept convenient for service-layer imports.
LocalProvider = LocalOllamaProvider
