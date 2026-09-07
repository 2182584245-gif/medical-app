from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

type ChatMessage = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Features callers may safely use without probing a provider over the network."""

    supports_images: bool = False


class ProviderError(RuntimeError):
    """A user-facing failure raised by an AI provider adapter.

    ``code`` is stable enough for the UI to choose an action while ``message``
    remains suitable for showing directly to a non-technical user.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_error",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class ChatProvider(ABC):
    """Small common surface shared by local and cloud chat providers."""

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Return conservative, local metadata about supported input types."""

        return ProviderCapabilities()

    @property
    def supports_images(self) -> bool:
        """Compatibility-friendly shorthand used by the UI and service layer."""

        return self.capabilities.supports_images

    @abstractmethod
    def chat(self, model: str, messages: Sequence[ChatMessage]) -> str:
        """Return the assistant's text for a conversation."""


# A descriptive alias for callers that prefer the longer name.
BaseProvider = ChatProvider


def extract_chat_content(response: Any) -> str:
    """Read chat content from both SDK model objects and plain dictionaries."""

    message = _get_value(response, "message")
    content = _get_value(message, "content") if message is not None else None

    # A couple of lightweight test doubles and compatibility wrappers expose
    # content directly rather than nesting it under ``message``.
    if content is None:
        content = _get_value(response, "content")

    if not isinstance(content, str) or not content.strip():
        raise ProviderError(
            "模型返回了无法识别的空响应，请稍后重试。",
            code="invalid_response",
        )
    return content


def extract_model_names(response: Any) -> list[str]:
    """Read and de-duplicate model names from an Ollama list response."""

    models = _get_value(response, "models")
    if models is None:
        raise ProviderError(
            "Ollama Cloud 返回了无法识别的模型列表。",
            code="invalid_response",
        )
    if isinstance(models, (str, bytes)) or not isinstance(models, Sequence):
        try:
            models = list(models)
        except TypeError as exc:
            raise ProviderError(
                "Ollama Cloud 返回了无法识别的模型列表。",
                code="invalid_response",
            ) from exc

    names: list[str] = []
    seen: set[str] = set()
    for item in models:
        if isinstance(item, str):
            name = item
        else:
            name = _get_value(item, "model") or _get_value(item, "name")
        if not isinstance(name, str):
            continue
        name = name.strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def exception_status_code(exc: BaseException) -> int | None:
    """Best-effort HTTP status extraction without depending on one SDK shape."""

    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        status = getattr(current, "status_code", None) or getattr(current, "status", None)
        if status is None:
            response = getattr(current, "response", None)
            status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status
        if isinstance(status, str) and status.isdigit():
            return int(status)
        cause = current.__cause__ or current.__context__
        current = cause if isinstance(cause, BaseException) else None
    return None


def is_timeout_error(exc: BaseException) -> bool:
    """Recognise built-in and HTTP-client timeout exceptions, including causes."""

    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        description = f"{type(current).__name__} {current}".lower()
        if (
            isinstance(current, TimeoutError)
            or "timeout" in description
            or "timed out" in description
        ):
            return True
        cause = current.__cause__ or current.__context__
        current = cause if isinstance(cause, BaseException) else None
    return False


def _get_value(value: Any, key: str) -> Any:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)
