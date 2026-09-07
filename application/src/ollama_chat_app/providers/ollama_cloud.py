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
    extract_model_names,
    is_timeout_error,
)


class OllamaCloudProvider(ChatProvider):
    """Authenticated adapter for Ollama's hosted API."""

    def __init__(self, api_key: str) -> None:
        clean_key = api_key.strip()
        if not clean_key:
            raise ProviderError(
                "请先保存 Ollama Cloud API Key。",
                code="missing_api_key",
            )
        self._api_key = clean_key

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_images=True)

    def list_models(self) -> list[str]:
        try:
            response = self._client().list()
            return extract_model_names(response)
        except ProviderError:
            raise
        except Exception as exc:
            raise _translate_cloud_error(exc, action="获取模型列表") from exc

    def chat(self, model: str, messages: Sequence[ChatMessage]) -> str:
        model_name = model.strip()
        if not model_name:
            raise ProviderError("请选择一个 Ollama Cloud 模型。", code="invalid_model")

        try:
            response = self._client().chat(
                model=model_name,
                messages=[dict(message) for message in messages],
                stream=False,
            )
        except ProviderError:
            raise
        except Exception as exc:
            raise _translate_cloud_error(exc, action="发送消息", model=model_name) from exc

        return extract_chat_content(response)

    def _client(self) -> ollama.Client:
        return ollama.Client(
            host=config.OLLAMA_CLOUD_HOST,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=config.REQUEST_TIMEOUT_SECONDS,
        )


def _translate_cloud_error(
    exc: Exception,
    *,
    action: str,
    model: str | None = None,
) -> ProviderError:
    status = exception_status_code(exc)
    if status == 401:
        return ProviderError(
            "Ollama Cloud API Key 无效或已失效，请重新保存密钥。",
            code="authentication_error",
            status_code=status,
        )
    if status == 403:
        return ProviderError(
            "当前 Ollama Cloud 账号无权执行此操作；请检查模型权限和账号状态。",
            code="permission_denied",
            status_code=status,
        )
    if status == 404:
        target = f"模型“{model}”" if model else "Ollama Cloud 接口"
        return ProviderError(
            f"未找到{target}，它可能已改名或下线。",
            code="model_not_found" if model else "not_found",
            status_code=status,
        )
    if status == 429:
        return ProviderError(
            "Ollama Cloud 免费额度可能已用完，或请求过于频繁；请稍后再试。",
            code="rate_limited",
            status_code=status,
        )
    if status == 408 or is_timeout_error(exc):
        return ProviderError(
            f"连接 Ollama Cloud {action}时超时，请检查网络后重试。",
            code="timeout",
            status_code=status,
        )
    if status is not None and status >= 500:
        return ProviderError(
            "Ollama Cloud 服务暂时不可用，请稍后再试。",
            code="service_unavailable",
            status_code=status,
        )
    if status is not None and 400 <= status < 500:
        return ProviderError(
            "Ollama Cloud 未接受本次请求，请检查所选模型和账号状态。",
            code="request_rejected",
            status_code=status,
        )
    return ProviderError(
        f"无法连接 Ollama Cloud 来{action}，请检查网络后重试。",
        code="connection_error",
        status_code=status,
    )


# Short alias kept convenient for service-layer imports.
CloudProvider = OllamaCloudProvider
