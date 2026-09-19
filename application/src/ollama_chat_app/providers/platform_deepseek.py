"""Authenticated default DeepSeek access without distributing the server key."""

import json

from ..services.cloud_client import CloudAPIError
from .base import ChatProvider, ProviderCapabilities, ProviderError
from .deepseek_cloud import DeepSeekCloudProvider, _completion_request_body


class PlatformDeepSeekProvider(ChatProvider):
    """Use the existing authenticated chat/stream routes, without native tools.

    ConversationAgentProvider uses its single-response JSON compatibility path
    with this provider. Adding a chat_tools method would incorrectly opt into
    multi-round requests unsupported by already-deployed v3 servers.
    """

    def __init__(self, client):
        self.client = client
        self._last_used_model = None
        self._last_response_model = None

    capabilities = property(lambda self: ProviderCapabilities(supports_images=True))
    last_used_model = property(lambda self: self._last_used_model)
    last_response_model = property(lambda self: self._last_response_model)
    resolve_model = DeepSeekCloudProvider.resolve_model

    def _payload(self, model, messages, max_tokens=2048):
        chosen = self.resolve_model(model, messages)
        raw = _completion_request_body(
            chosen,
            messages,
            thinking={"type": "disabled"},
            max_tokens=min(max_tokens or 2048, 2048),
        )
        value = json.loads(raw)
        payload = {
            "model": chosen,
            "messages": value["messages"],
            "max_tokens": value["max_tokens"],
        }
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 4 * 1024 * 1024:
            raise ProviderError(
                "默认 AI 单次附件与文字合计不能超过 4 MiB，请缩小后重试。", code="request_too_large"
            )
        return payload

    def validate_request(self, model, messages):
        return self._payload(model, messages)["model"]

    def chat(self, model, messages, *, max_tokens=None):
        self._last_used_model = self._last_response_model = None
        payload = self._payload(model, messages, max_tokens)
        try:
            result = self.client.request("POST", "/v1/ai/chat", json=payload)
        except CloudAPIError as error:
            raise _failure(error) from None
        if (
            not isinstance(result, dict)
            or result.get("model") != payload["model"]
            or not isinstance(result.get("content"), str)
            or not result["content"].strip()
        ):
            raise ProviderError("默认 AI 返回了无效内容。", code="invalid_response")
        self._last_used_model = self._last_response_model = result["model"]
        return result["content"]

    def chat_stream(self, model, messages, *, cancel_event=None, max_tokens=None):
        self._last_used_model = self._last_response_model = None
        payload = self._payload(model, messages, max_tokens)
        done = False
        try:
            for event in self.client.ai_stream(payload, cancel_event=cancel_event):
                if event.get("error"):
                    raise ProviderError(
                        "默认 AI 暂未完成回答，请稍后手动重试。", code="incomplete_response"
                    )
                if event.get("done") is True:
                    if event.get("model") != payload["model"]:
                        raise ProviderError("默认 AI 模型校验失败。", code="invalid_response")
                    done = True
                    self._last_used_model = self._last_response_model = event["model"]
                elif isinstance(event.get("delta"), str):
                    yield event["delta"]
        except CloudAPIError as error:
            raise _failure(error) from None
        if cancel_event is not None and cancel_event.is_set():
            raise ProviderError("已停止回答。", code="cancelled")
        if not done:
            raise ProviderError("默认 AI 连接在完成前中断。", code="incomplete_response")


def _failure(error):
    if error.code == "limited":
        return ProviderError(
            "默认 AI 已达到本分钟或今日额度，请稍后再试；不会自动重试或收费。", code="rate_limited"
        )
    if error.code == "unavailable":
        return ProviderError(
            "默认 AI 暂不可用，请稍后再试，或在更换 APIKEY 中使用自己的密钥。",
            code="service_unavailable",
        )
    return ProviderError(str(error), code=error.code)
