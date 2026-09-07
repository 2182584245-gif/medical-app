from __future__ import annotations

import json

import httpx
import pytest

from ollama_chat_app import config
from ollama_chat_app.providers.base import ProviderError
from ollama_chat_app.providers.deepseek_cloud import DeepSeekCloudProvider
from ollama_chat_app.security.secret_store import SecretStore


def test_chat_sends_official_openai_compatible_request_and_parses_answer() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["Authorization"]
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "test-only",
                "choices": [{"message": {"role": "assistant", "content": "模拟回答"}}],
            },
        )

    provider = DeepSeekCloudProvider(
        "  user-owned-secret  ",
        transport=httpx.MockTransport(handler),
    )
    answer = provider.chat(
        config.DEEPSEEK_DEFAULT_MODEL,
        [{"role": "user", "content": "你好"}],
    )

    assert answer == "模拟回答"
    assert provider.last_used_model == config.DEEPSEEK_DEFAULT_MODEL
    assert provider.supports_images is True
    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["authorization"] == "Bearer user-owned-secret"
    assert captured["payload"] == {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "你好"}],
        "stream": False,
        "thinking": {"type": "disabled"},
    }
    assert "user-owned-secret" not in json.dumps(captured["payload"])


def test_verify_key_uses_a_tiny_non_thinking_completion() -> None:
    captured_payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "OK"}}]},
        )

    answer = DeepSeekCloudProvider(
        "secret",
        transport=httpx.MockTransport(handler),
    ).verify_key()

    assert answer == "OK"
    assert captured_payloads == [
        {
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": "仅回复 OK"}],
            "stream": False,
            "thinking": {"type": "disabled"},
            "max_tokens": 2,
        }
    ]


@pytest.mark.parametrize(
    ("status_code", "expected_code", "message_fragment"),
    [
        (401, "authentication_error", "API Key"),
        (402, "insufficient_balance", "余额不足"),
        (429, "rate_limited", "过于频繁"),
        (500, "service_unavailable", "暂时不可用"),
        (503, "service_unavailable", "暂时不可用"),
    ],
)
def test_http_failures_have_safe_user_facing_messages(
    status_code: int,
    expected_code: str,
    message_fragment: str,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": {"message": "upstream detail"}})

    provider = DeepSeekCloudProvider("never-show-this", transport=httpx.MockTransport(handler))

    with pytest.raises(ProviderError) as caught:
        provider.chat("deepseek-v4-flash", [{"role": "user", "content": "你好"}])

    assert caught.value.code == expected_code
    assert caught.value.status_code == status_code
    assert message_fragment in str(caught.value)
    assert "never-show-this" not in str(caught.value)
    assert "upstream detail" not in str(caught.value)
    assert provider.last_used_model is None


def test_timeout_is_translated_without_network_access() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("offline simulated timeout", request=request)

    provider = DeepSeekCloudProvider("secret", transport=httpx.MockTransport(handler))

    with pytest.raises(ProviderError) as caught:
        provider.chat("deepseek-v4-flash", [{"role": "user", "content": "你好"}])

    assert caught.value.code == "timeout"
    assert "超时" in str(caught.value)


def test_invalid_model_and_empty_response_are_rejected_locally() -> None:
    provider = DeepSeekCloudProvider(
        "secret",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"choices": []})),
    )

    with pytest.raises(ProviderError, match="仅支持") as invalid_model:
        provider.chat("another-model", [{"role": "user", "content": "你好"}])
    assert invalid_model.value.code == "invalid_model"

    with pytest.raises(ProviderError, match="空响应") as empty_response:
        provider.chat("deepseek-v4-flash", [{"role": "user", "content": "你好"}])
    assert empty_response.value.code == "invalid_response"


def test_secret_store_isolates_keys_by_user_and_provider() -> None:
    store = SecretStore()
    store.set_api_key(1, "ollama-user-1", "ollama_cloud")
    store.set_api_key(1, "deepseek-user-1", "deepseek_cloud")
    store.set_api_key(2, "deepseek-user-2", "deepseek_cloud")

    assert store.get_api_key(1, "ollama_cloud") == "ollama-user-1"
    assert store.get_api_key(1, "deepseek_cloud") == "deepseek-user-1"
    assert store.get_api_key(2, "deepseek_cloud") == "deepseek-user-2"

    store.delete_api_key(1, "deepseek_cloud")
    assert store.get_api_key(1, "deepseek_cloud") is None
    assert store.get_api_key(1, "ollama_cloud") == "ollama-user-1"
    assert SecretStore().get_api_key(2, "deepseek_cloud") is None
