"""The independent FAWNSEE member application's only public Python API.

The retained v3 services perform the business work. This facade narrows their
desktop bridge to member features and keeps staff and generic RPC entry points
out of the webview's exported object.
"""
from __future__ import annotations

from threading import RLock
from typing import Any, Mapping

from ..config import APP_NAME, APP_VERSION
from ..providers.base import ProviderError
from ..services.cloud_client import CloudAPIError
from ..services.member_weather import MemberWeatherService
from ..services.network_probe import probe_backend
from .bridge import BridgeRequestError, WebBridgeAPI, _safe_envelope
from .local_state import LOCAL_MEMBER_ACTIONS, LocalStateError, MemberLocalState
from .expansion import EXPANSION_ACTIONS, dispatch as expansion_dispatch


MEMBER_ACTIONS = frozenset({
    "auth.me", "auth.change-password",
    "health.dashboard", "health.records.list", "health.records.add", "health.records.delete",
    "health.statistics", "health.service-summary", "health.membership.activate",
    "health.profile.get", "health.profile.save", "health.reminders.list", "health.reminders.add",
    "health.reminders.update", "health.reminders.toggle", "health.reminders.delete",
    "chat.conversations.list", "chat.conversations.create", "chat.conversations.rename",
    "chat.conversations.delete", "chat.messages.list", "chat.send",
    "ai.statistics.explain", "ai.member.draft", "ai.life-records.propose", "ai.drafts.list",
    "ai.draft.get", "ai.proposal.confirm", "ai.proposal.dismiss", "ai.config", "ai.context",
    "voice.transcribe", "voice.smart-record", "voice.polish", "library.catalog", "library.search", "library.card", "library.source",
    "forum.topics", "forum.posts", "forum.posts.create", "forum.posts.like", "forum.posts.comment", "forum.posts.comments",
    "preferences.get", "preferences.update", "service.appointments",
    "service.appointments.request", "service.history", "service.tasks",
    "shopping.products", "shopping.cart", "shopping.cart.add", "shopping.cart.set",
    "shopping.favorites", "shopping.favorite.set", "shopping.checkout", "shopping.orders",
    "shopping.recommendations", "shopping.recommendation.view", "shopping.recommendation.interested",
    "shopping.product.interested", "files.upload", "files.list", "files.get", "files.delete",
    "files.storage", "files.reports.list", "files.report.get", "files.report.create",
    "files.report.delete", "files.report.archive", "files.report.confirm",
    "files.report.item.add", "files.report.item.confirm", "network.probe", "weather.current",
    "member.sync", "member.capabilities", "member.appointments.update",
})


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


_PROVIDER_MESSAGES = {
    "rate_limited": "DeepSeek 已达到当前额度，请稍后手动重试。",
    "service_unavailable": "DeepSeek 服务暂不可用，请稍后重试或联系平台管理员。",
    "incomplete_response": "DeepSeek 回答未完整生成，请先查看当前对话，再手动重试。",
    "invalid_response": "DeepSeek 返回内容或模型校验失败，本次未作为成功回答保存。",
    "authentication": "云端登录已失效，请重新登录会员账号。",
    "not_authenticated": "请先登录会员账号。",
    "permission": "当前会员账号没有执行此 AI 操作的权限。",
    "feature_disabled": "平台暂未开放此 AI 功能，请稍后再试。",
    "timeout": "等待 DeepSeek 超时，请先查看当前对话，不要重复提交同一内容。",
    "network": "暂时无法安全连接 DeepSeek，请检查网络后手动重试。",
    "cancelled": "已停止回答，本次未保存完整 AI 回答。",
    "invalid_model": "当前模型不受平台支持，请重新打开 AI 页面后再试。",
    "request_too_large": "本次 AI 文字与附件合计过大，请缩小图片或减少内容。",
    "input_too_large": "本次输入超出 AI 可完整处理的范围，请缩短文字或缩小回顾日期范围后重试；本次未生成完整结果。",
    "images_not_supported": "当前 AI 模型不支持图片，请改用文字或选择支持的模型。",
    "invalid_image": "图片内容无效或损坏，请重新选择完整图片。",
    "image_too_large": "图片大小超出 AI 接口限制，请缩小后重试。",
    "images_too_large": "图片总大小超出 AI 接口限制，请减少图片。",
    "image_dimensions_exceeded": "图片尺寸超出 AI 接口限制，请缩小后重试。",
}


class _MemberAPI(WebBridgeAPI):
    def request(self, action, payload=None):
        if action in {"member.sync", "member.capabilities", "member.appointments.update"}:
            try:
                self._ensure_client()
                self._require_session(action)
                if (self._user or {}).get("role_code") != "member":
                    raise BridgeRequestError("请使用会员账号登录。", "permission")
                body = dict(self._validate_payload(payload))
                if action == "member.sync":
                    if body:
                        raise BridgeRequestError("同步请求不接受参数。", "validation")
                    return _safe_envelope(self._client.request("GET", "/v1/member/sync"))
                if action == "member.capabilities":
                    if body:
                        raise BridgeRequestError("能力请求不接受参数。", "validation")
                    return _safe_envelope(self._client.request("GET", "/v1/member/capabilities"))
                appointment_id = body.pop("appointment_id", None)
                if type(appointment_id) is not int or appointment_id <= 0:
                    raise BridgeRequestError("预约编号无效。", "validation")
                if "expected_updated_at" not in body or type(body["expected_updated_at"]) is not str:
                    raise BridgeRequestError("缺少预约版本。", "validation")
                if not body or set(body) - {"expected_updated_at", "service_type", "scheduled_at", "address", "notes"}:
                    raise BridgeRequestError("预约修改字段不受支持。", "validation")
                return _safe_envelope(self._client.request("PATCH", f"/v1/member/appointments/{appointment_id}", json=body))
            except Exception as error:
                return self._failure(error)
        if action not in EXPANSION_ACTIONS:
            return super().request(action, payload)
        try:
            self._ensure_client()
            self._require_session(action)
            if (self._user or {}).get('role_code') != 'member':
                raise BridgeRequestError('请使用会员账号登录。', 'permission')
            body = self._validate_payload(payload)
            return _safe_envelope(expansion_dispatch(self, action, body))
        except Exception as error:
            return self._failure(error)

    @staticmethod
    def _failure(error: Exception) -> dict[str, Any]:
        if isinstance(error, (CloudAPIError, BridgeRequestError)):
            return _error(error.code, str(error))
        if isinstance(error, ProviderError):
            # Provider codes remain actionable; never echo an arbitrary upstream
            # body, URL or credentials that could appear in exception text.
            code = error.code if error.code in _PROVIDER_MESSAGES else "provider_error"
            return _error(code, _PROVIDER_MESSAGES.get(code, "DeepSeek 暂未完成本次请求，请稍后手动重试。"))
        # Only known domain validation errors contain messages reviewed for UI
        # display. Arbitrary Python/network exception text can contain secrets.
        module = type(error).__module__
        if module.startswith("ollama_chat_app.services.") and isinstance(error, (ValueError, LookupError, RuntimeError)):
            return _error("request_failed", str(error)[:300])
        if isinstance(error, ValueError):
            return _error("validation", "提交内容格式不正确，请检查后重试。")
        return _error("request_failed", "操作未完成，请稍后重试。")


class MemberBridge:
    """Five fixed browser-callable methods, one authenticated actor per instance."""

    def __init__(self, *, base_url: str | None = None, client=None, client_factory=None, services: Mapping[str, Any] | None = None, local_state: MemberLocalState | None = None):
        self._api = _MemberAPI(base_url=base_url, client=client, client_factory=client_factory, services=services)
        self._lock = RLock()
        self._generation = 0
        self._local_state = local_state

    def bootstrap(self, options: Mapping[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            self._discard_expired_identity()
            if options is not None and not isinstance(options, Mapping):
                return _error("validation", "启动参数格式无效。")
            result = self._api.bootstrap(options)
            if result.get("ok"):
                result["data"]["app"] = {"name": APP_NAME, "version": APP_VERSION, "transport": "member-bridge"}
                user = result["data"].get("user")
                if user and user.get("role_code") != "member":
                    self._clear_nonmember()
                    return _error("permission", "FAWNSEE 会员端仅支持会员账号，请使用会员账号登录。")
            return result

    def login(self, username: str, password: str) -> dict[str, Any]:
        with self._lock:
            self._generation += 1
            result = self._api.login(username, password)
            self._discard_expired_identity()
            if result.get("ok") and result["data"].get("role_code") != "member":
                self._clear_nonmember()
                return _error("permission", "FAWNSEE 会员端仅支持会员账号，请使用会员账号登录。")
            return result

    def register(self, username: str, password: str) -> dict[str, Any]:
        with self._lock:
            return self._api.register(username, password)

    def logout(self) -> dict[str, Any]:
        with self._lock:
            self._generation += 1
            return self._api.logout()

    def request(self, action: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        generation = self._generation
        if isinstance(action, str) and action in {"weather.current", "network.probe"}:
            return self._public_status_request(action, payload, generation)
        with self._lock:
            try:
                payload = self._scoped_payload(payload, generation)
            except BridgeRequestError as error:
                return self._api._failure(error)
            if not isinstance(action, str) or action not in MEMBER_ACTIONS | LOCAL_MEMBER_ACTIONS | EXPANSION_ACTIONS:
                return _error("permission", "会员端未开放此操作。")
            if self._api._user and self._api._user.get("role_code") != "member":
                self._clear_nonmember()
                return _error("permission", "请使用会员账号登录。")
            if action in LOCAL_MEMBER_ACTIONS:
                result = self._local_request(action, payload)
            else:
                result = self._api.request(action, payload)
            self._discard_expired_identity(force=not result.get("ok") and result.get("error", {}).get("code") in {"authentication", "not_authenticated"})
            if action == "auth.me" and result.get("ok") and result["data"].get("role_code") != "member":
                self._clear_nonmember()
                return _error("permission", "请使用会员账号登录。")
            return result

    def _scoped_payload(self, payload, generation):
        if generation != self._generation:
            raise BridgeRequestError("账号已变更，本次操作未提交，请重新打开当前页面。", "account_changed")
        if isinstance(payload, Mapping) and "_fawnsee_actor_id" in payload:
            payload = dict(payload)
            expected = payload.pop("_fawnsee_actor_id")
            user = self._api._user or {}
            if type(expected) is not int or expected <= 0 or expected != user.get("id") or user.get("role_code") != "member":
                raise BridgeRequestError("账号已变更，本次操作未提交，请重新打开当前页面。", "account_changed")
        return payload

    def _public_status_request(self, action, payload, generation):
        # Authenticate and capture only public inputs while holding the member
        # lock. Public HTTPS timeouts must never serialize account operations.
        with self._lock:
            try:
                payload = self._scoped_payload(payload, generation)
                self._discard_expired_identity()
                self._api._ensure_client()
                self._api._require_session(action)
                if (self._api._user or {}).get("role_code") != "member":
                    return _error("permission", "请使用会员账号登录。")
                body = dict(self._api._validate_payload(payload))
                endpoint = self._api._base_url
                allowed = {"city", "latitude", "longitude"} if action == "weather.current" else set()
                if set(body) - allowed:
                    return _error("validation", "公开状态请求参数格式不正确。")
                city = body.get("city")
                if city is not None and (not isinstance(city, str) or len(city) > 160):
                    return _error("validation", "天气地区格式不正确。")
                lat, lon = body.get("latitude"), body.get("longitude")
                if (lat is None) != (lon is None):
                    return _error("validation", "天气地区坐标不完整。")
                if lat is not None and (type(lat) not in {int, float} or type(lon) not in {int, float} or not -90 <= lat <= 90 or not -180 <= lon <= 180):
                    return _error("validation", "天气地区坐标格式不正确。")
            except Exception as error:
                return self._api._failure(error)
        # Nothing below this boundary uses the CloudAPIClient, user record,
        # bearer token, services cache, filesystem path or mutable auth state.
        try:
            if action == "network.probe":
                result = probe_backend(endpoint)
                data = {**result, "online": result.get("state") == "online", "endpoint": endpoint, "ip_location": "中国·北京（阿里云服务节点）"}
            else:
                service = MemberWeatherService()
                if not body.get("city") and body.get("latitude") is None:
                    data = service.current_from_ip()
                else:
                    try:
                        data = service.current(body)
                    except Exception:
                        data = service.fallback()
            return {"ok": True, "data": data}
        except Exception as error:
            return self._api._failure(error)

    def _local_request(self, action, payload):
        try:
            self._api._ensure_client()
            self._api._require_session(action)
            if (self._api._user or {}).get("role_code") != "member":
                return _error("permission", "请使用会员账号登录。")
            body = self._api._validate_payload(payload)
            if self._local_state is None:
                from .runtime import data_root
                self._local_state = MemberLocalState(data_root() / "member-local")
            data = self._local_state.request(self._api._base_url, self._api._actor_id(), action, body)
            return {"ok": True, "data": data}
        except LocalStateError as error:
            return _error(error.code, str(error))
        except Exception as error:
            return self._api._failure(error)

    def _discard_expired_identity(self, *, force: bool = False) -> None:
        client = self._api._client
        if client is not None and (force or not client.is_authenticated):
            if self._api._user is not None:
                self._generation += 1
            if force:
                client.clear_session()
            self._api._user = None
            self._api._reset_services()

    def _clear_nonmember(self) -> None:
        self._generation += 1
        if self._api._client is not None:
            self._api._client.clear_session()
        self._api._user = None
        self._api._reset_services()

    def _close(self) -> None:
        with self._lock:
            if self._api._client is not None:
                self._api._client.close()
