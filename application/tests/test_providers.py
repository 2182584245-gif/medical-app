from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ollama_chat_app import config
from ollama_chat_app.providers import local_ollama, ollama_cloud
from ollama_chat_app.providers.base import ProviderError
from ollama_chat_app.providers.local_ollama import LocalOllamaProvider
from ollama_chat_app.providers.ollama_cloud import OllamaCloudProvider
from ollama_chat_app.security.secret_store import SecretStore


def test_local_provider_does_not_connect_until_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    client_calls: list[dict[str, Any]] = []
    chat_calls: list[dict[str, Any]] = []

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            client_calls.append(kwargs)

        def list(self) -> None:
            pytest.fail("The local provider must never read the local model list")

        def chat(self, **kwargs: Any) -> dict[str, Any]:
            chat_calls.append(kwargs)
            return {"message": {"content": "本地回答"}}

    monkeypatch.setattr(local_ollama.ollama, "Client", FakeClient)

    provider = LocalOllamaProvider()
    assert client_calls == []

    messages = [{"role": "user", "content": "你好"}]
    assert provider.chat("qwen3:4b", messages) == "本地回答"
    assert client_calls == [
        {
            "host": config.LOCAL_OLLAMA_HOST,
            "timeout": config.REQUEST_TIMEOUT_SECONDS,
        }
    ]
    assert chat_calls == [
        {
            "model": "qwen3:4b",
            "messages": messages,
            "stream": False,
        }
    ]


def test_local_provider_translates_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def chat(self, **_kwargs: Any) -> None:
            raise ConnectionError("connection refused")

    monkeypatch.setattr(local_ollama.ollama, "Client", FakeClient)

    with pytest.raises(ProviderError) as caught:
        LocalOllamaProvider().chat("qwen3:4b", [{"role": "user", "content": "你好"}])

    assert caught.value.code == "connection_error"
    assert "本机 Ollama" in str(caught.value)


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            SimpleNamespace(
                models=[
                    SimpleNamespace(model="first-cloud"),
                    SimpleNamespace(name="second-cloud"),
                    SimpleNamespace(model="first-cloud"),
                ]
            ),
            ["first-cloud", "second-cloud"],
        ),
        (
            {
                "models": [
                    {"model": "dictionary-cloud"},
                    {"name": "name-fallback"},
                ]
            },
            ["dictionary-cloud", "name-fallback"],
        ),
    ],
)
def test_cloud_list_models_parses_sdk_objects_and_dicts(
    monkeypatch: pytest.MonkeyPatch,
    response: Any,
    expected: list[str],
) -> None:
    client_calls: list[dict[str, Any]] = []

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            client_calls.append(kwargs)

        def list(self) -> Any:
            return response

    monkeypatch.setattr(ollama_cloud.ollama, "Client", FakeClient)

    assert OllamaCloudProvider("  secret-key  ").list_models() == expected
    assert client_calls == [
        {
            "host": config.OLLAMA_CLOUD_HOST,
            "headers": {"Authorization": "Bearer secret-key"},
            "timeout": config.REQUEST_TIMEOUT_SECONDS,
        }
    ]


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (SimpleNamespace(message=SimpleNamespace(content="对象回答")), "对象回答"),
        ({"message": {"content": "字典回答"}}, "字典回答"),
    ],
)
def test_cloud_chat_parses_sdk_objects_and_dicts(
    monkeypatch: pytest.MonkeyPatch,
    response: Any,
    expected: str,
) -> None:
    chat_calls: list[dict[str, Any]] = []

    class FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def chat(self, **kwargs: Any) -> Any:
            chat_calls.append(kwargs)
            return response

    monkeypatch.setattr(ollama_cloud.ollama, "Client", FakeClient)

    messages = [{"role": "user", "content": "你好"}]
    answer = OllamaCloudProvider("secret-key").chat("cloud-model", messages)

    assert answer == expected
    assert chat_calls == [
        {
            "model": "cloud-model",
            "messages": messages,
            "stream": False,
        }
    ]


@pytest.mark.parametrize(
    ("operation", "status_code", "expected_code", "message_fragment"),
    [
        ("list", 401, "authentication_error", "API Key"),
        ("chat", 429, "rate_limited", "免费额度"),
    ],
)
def test_cloud_translates_http_errors(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    status_code: int,
    expected_code: str,
    message_fragment: str,
) -> None:
    class HttpFailure(Exception):
        def __init__(self, status: int) -> None:
            super().__init__(f"HTTP {status}")
            self.status_code = status

    class FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def list(self) -> None:
            raise HttpFailure(status_code)

        def chat(self, **_kwargs: Any) -> None:
            raise HttpFailure(status_code)

    monkeypatch.setattr(ollama_cloud.ollama, "Client", FakeClient)
    provider = OllamaCloudProvider("secret-key")

    with pytest.raises(ProviderError) as caught:
        if operation == "list":
            provider.list_models()
        else:
            provider.chat("cloud-model", [{"role": "user", "content": "你好"}])

    assert caught.value.code == expected_code
    assert caught.value.status_code == status_code
    assert message_fragment in str(caught.value)


def test_secret_store_isolates_users_and_never_persists_keys() -> None:
    store = SecretStore()
    store.set_api_key(1, "first-user-key")
    store.set_api_key(2, "second-user-key")

    assert store.get_api_key(1) == "first-user-key"
    assert store.get_api_key(2) == "second-user-key"
    store.delete_api_key(1)
    assert store.get_api_key(1) is None
    assert store.get_api_key(2) == "second-user-key"

    store.clear_api_keys()
    assert store.get_api_key(1) is None
    assert store.get_api_key(2) is None

    # Deleting an already absent key remains safe.
    store.delete_api_key(1)

    # A fresh app session has no persisted keys.
    assert SecretStore().get_api_key(2) is None
