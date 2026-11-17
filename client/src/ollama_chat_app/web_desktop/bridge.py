"""Small, allow-listed Python API exposed to the HTML desktop UI.

The object in this module is intended to be passed to ``pywebview.create_window``
as ``js_api``.  It is intentionally *not* a generic RPC adapter: JavaScript can
only invoke the fixed action names in ``ACTION_HANDLERS`` and it can never read
the bearer token, an API key, an arbitrary cloud path, or a Python object.

All cloud authorization is still performed by the server.  The checks here are
a second, local boundary that prevents an accidental UI control from invoking
an operation outside the current user's role.  Every public method returns a
JSON-safe envelope: ``{"ok": True, "data": ...}`` or
``{"ok": False, "error": {"code": ..., "message": ...}}``.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from typing import Any, Callable, Mapping

from ..cloud_config import configured_cloud_base_url
from ..config import APP_NAME, APP_VERSION, DEEPSEEK_DEFAULT_MODEL, DEEPSEEK_VISION_MODEL
from ..data.database import User
from ..services.ai_assistant import AiAssistantService
from ..services.chat import ChatService, ChatTurn
from ..services.chat_attachments import prepare_attachment_bytes
from ..services.cloud_client import CloudAPIClient, CloudAPIError
from ..services.cloud_library import CloudKnowledgeLibrary
from ..services.commerce import CommerceService
from ..services.experience_forum import ExperienceForumService
from ..services.health import HealthService
from ..services.member_weather import MemberWeatherService
from ..services.network_probe import probe_backend
from ..services.preferences import PreferencesService
from ..services.remote_services import (
    RemoteAiAssistantService,
    RemoteAppointmentService,
    RemoteAuthService,
    RemoteChatService,
    RemoteCommerceService,
    RemoteExperienceForumService,
    RemoteFileManagementService,
    RemoteHealthService,
    RemotePreferencesService,
    RemoteServiceManagementService,
)
from ..services.service_management import ServiceManagementService


_MAX_ACTION_BYTES = 8 * 1024 * 1024
_MAX_TEXT = 32_000
_ID_KEYS = frozenset({"id", "user_id", "member_id", "advisor_id", "conversation_id", "post_id", "product_id", "record_id", "reminder_id", "appointment_id", "task_id", "order_id", "insight_id", "proposal_id", "message_id"})
_ROLE_ORDER = {"member": 0, "advisor": 1, "operator": 2}


class BridgeRequestError(ValueError):
    """A safe, client-facing validation error."""

    def __init__(self, message: str, code: str = "validation") -> None:
        self.code = code
        super().__init__(message)


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    """Convert service return values to bounded JSON primitives.

    The RPC codec already constrains server values.  This final conversion is
    still required because dataclass returns are common in chat/auth methods,
    while pywebview serializes the bridge return value directly.
    """

    if depth > 32:
        raise BridgeRequestError("返回数据层级过深", "protocol")
    if value is None or type(value) in {str, bool, int, float}:
        if isinstance(value, str) and len(value) > _MAX_TEXT:
            return value[:_MAX_TEXT] + "…"
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        # FileManagementService accepts files up to 15 MiB.  Keep the bridge
        # response just above that bound so a user can reopen an uploaded
        # image/PDF; the HTTP/RPC 32 MiB response limit still applies.
        if len(value) > 16 * 1024 * 1024:
            raise BridgeRequestError("返回附件过大", "limit")
        return {"encoding": "base64", "value": base64.b64encode(value).decode("ascii")}
    if is_dataclass(value):
        return _json_safe(asdict(value), depth=depth + 1)
    if isinstance(value, Mapping):
        if len(value) > 10_000:
            raise BridgeRequestError("返回项目过多", "limit")
        return {str(key): _json_safe(item, depth=depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        if len(value) > 10_000:
            raise BridgeRequestError("返回项目过多", "limit")
        return [_json_safe(item, depth=depth + 1) for item in value]
    # Avoid leaking arbitrary service objects or reprs into the browser.
    raise BridgeRequestError("云端返回了不受支持的数据", "protocol")


def _safe_envelope(value: Any) -> dict[str, Any]:
    return {"ok": True, "data": _json_safe(value)}


def _member_retrieval_receipt(trace: Any) -> dict[str, Any]:
    """Expose bounded reference metadata, never a trace or filesystem path."""
    trace = trace if isinstance(trace, Mapping) else {}
    status = trace.get("status")
    if not isinstance(status, str):
        status = "unavailable"
    sources = []
    raw_sources = trace.get("sources")
    if isinstance(raw_sources, list):
        for raw in raw_sources[:8]:
            if not isinstance(raw, Mapping):
                continue
            document_id, title = raw.get("document_id"), raw.get("title")
            if not isinstance(document_id, str) or len(document_id) > 100 or not re.fullmatch(r"D-[A-Za-z0-9-]+", document_id):
                continue
            if not isinstance(title, str) or not title.strip() or len(title) > 500 or any(ord(char) < 32 for char in title):
                continue
            if "\\" in title or title.startswith(("/", "file:", "http:", "https:")) or re.search(r"[A-Za-z]:/", title):
                continue
            source = {"document_id": document_id, "title": title.strip()[:200], "kind": "knowledge"}
            sections = raw.get("section_ids")
            if isinstance(sections, list):
                valid = [part for part in sections[:12] if isinstance(part, str) and len(part) <= 80 and re.fullmatch(r"[A-Za-z]\d+(?:-[A-Za-z]+\d+)*", part)]
                if valid:
                    source["section_ids"] = list(dict.fromkeys(valid))
            sources.append(source)
    if status != "hit":
        sources = []
    if status not in {"hit", "no_hit", "unavailable"} or (status == "hit" and not sources):
        status = "unavailable"
    return {"status": status, "sources": sources, "external_search": False}


class WebBridgeAPI:
    """Allow-listed pywebview API for the new HTML desktop shell.

    ``client`` and ``client_factory`` are optional test seams.  Production code
    normally provides neither; the default, validated Aliyun endpoint is used
    and the client is created lazily on the first ``bootstrap``/login call.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        client: CloudAPIClient | None = None,
        client_factory: Callable[[str], CloudAPIClient] | None = None,
        services: Mapping[str, Any] | None = None,
    ) -> None:
        self._base_url = base_url or configured_cloud_base_url()
        self._client = client
        self._client_factory = client_factory or CloudAPIClient
        self._services_override = dict(services or {})
        self._services: dict[str, Any] = {}
        self._user: dict[str, Any] | None = None
        self._bootstrapped = client is not None

    # ------------------------------------------------------------------
    # Lifecycle and authentication.  These are the only methods called by
    # the web shell before it starts drawing authenticated pages.
    # ------------------------------------------------------------------
    def bootstrap(self, options: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Initialize the cloud client without performing a network request."""

        if options is not None and not isinstance(options, Mapping):
            raise BridgeRequestError("启动参数格式无效")
        if not self._base_url:
            return {"ok": False, "error": {"code": "configuration", "message": "云端地址未配置"}}
        try:
            if self._client is None:
                self._client = self._client_factory(self._base_url)
            self._bootstrapped = True
            if self._client.is_authenticated and self._user is None:
                self._user = self._client.me()
            return _safe_envelope(self._bootstrap_data())
        except Exception as error:
            return self._failure(error)

    def login(self, username: str, password: str) -> dict[str, Any]:
        try:
            self._ensure_client()
            self._validate_text(username, "用户名", 64)
            self._validate_text(password, "密码", 256)
            self._user = self._client.login(username, password)  # token stays in client
            self._reset_services()
            return _safe_envelope(self._user_view())
        except Exception as error:
            return self._failure(error)

    def register(self, username: str, password: str) -> dict[str, Any]:
        try:
            self._ensure_client()
            self._validate_text(username, "用户名", 64)
            self._validate_text(password, "密码", 256)
            # Registration does not create an authenticated session.
            return _safe_envelope(self._client.register(username, password))
        except Exception as error:
            return self._failure(error)

    def developer_login(self, username: str, password: str) -> dict[str, Any]:
        """Perform the server's separate operator step-up login."""

        try:
            self._ensure_client()
            self._validate_text(username, "用户名", 64)
            self._validate_text(password, "密码", 256)
            self._user = self._client.developer_login(username, password)
            if self._user.get("role_code") != "operator":
                self._client.clear_session()
                self._user = None
                raise CloudAPIError("permission")
            self._reset_services()
            return _safe_envelope(self._user_view())
        except Exception as error:
            return self._failure(error)

    def logout(self) -> dict[str, Any]:
        try:
            if self._client is not None:
                self._client.logout()
            self._user = None
            self._reset_services()
            return _safe_envelope({"signed_out": True})
        except Exception as error:
            # CloudAPIClient clears its local token even when remote revocation
            # is uncertain; report the failure but never retain browser state.
            self._user = None
            self._reset_services()
            return self._failure(error)

    # ------------------------------------------------------------------
    # Single allow-listed request entry point.  It deliberately has no path,
    # service, method, eval, or generic rpc argument.
    # ------------------------------------------------------------------
    def request(self, action: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        try:
            self._ensure_client()
            action = self._validate_action(action)
            # A base64-encoded 15 MB image is larger than the ordinary RPC
            # envelope.  Only the fixed upload action receives that larger
            # bounded envelope; all other browser actions stay at 8 MB.
            payload_limit = 24 * 1024 * 1024 if action == "files.upload" else _MAX_ACTION_BYTES
            body = self._validate_payload(payload, max_bytes=payload_limit)
            handler = ACTION_HANDLERS.get(action)
            if handler is None:
                raise BridgeRequestError("此操作未开放", "permission")
            if action != "auth.reset-password":
                self._require_session(action)
                self._require_role(action)
            result = handler(self, body)
            return _safe_envelope(result)
        except Exception as error:
            return self._failure(error)

    # ------------------------------------------------------------------
    # Authenticated handlers.  Values that identify the current actor are
    # taken from the in-memory session; JavaScript cannot supply another one.
    # ------------------------------------------------------------------
    def _auth_me(self, _body):
        self._user = self._client.me()
        return self._user_view()

    def _auth_change_password(self, body):
        current = self._validate_text(body.get("current_password"), "当前密码", 256)
        new = self._validate_text(body.get("new_password"), "新密码", 256)
        result = self._client.change_password(current, new)
        return result if result is not None else {"changed": True}

    def _auth_reset_password(self, body):
        username = self._validate_text(body.get("username"), "账号", 64)
        new = self._validate_text(body.get("new_password"), "新密码", 256)
        result = self._client.reset_password(username, new)
        # Every remote session for the account is revoked by the server; do
        # not leave a stale local bearer token in the desktop bridge.
        self._client.clear_session()
        self._user = None
        self._reset_services()
        return result

    def _health_dashboard(self, _body):
        return self._service("health").get_dashboard(self._actor_id())

    def _health_records(self, body):
        return self._service("health").list_life_records(self._actor_id(), **body)

    def _health_add_record(self, body):
        return self._service("health").add_life_record(self._actor_id(), **body)

    def _health_delete_records(self, body):
        if "record_ids" not in body:
            raise BridgeRequestError("缺少 record_ids")
        return self._service("health").delete_life_records(self._actor_id(), body["record_ids"])

    def _health_statistics(self, body):
        return self._service("health").get_record_statistics(self._actor_id(), **body)

    def _ai_statistics_explain(self, body):
        """Explain factual health aggregates without changing any records."""

        values = self._only(body, {"days"})
        days = values.get("days", 7)
        if type(days) is not int or days not in {7, 30}:
            raise BridgeRequestError("统计周期只能选择近 7 天或近 30 天")
        preferences = self._service("preferences").get(self._actor_id())
        if preferences.get("ai_context_consent") is not True:
            raise BridgeRequestError("生成统计说明前，请先在 AI 设置中同意读取已保存的生活资料", "consent_required")
        statistics = self._service("health").get_record_statistics(self._actor_id(), days=days)
        prompt = (
            "请用温和、适合中老年人的中文解释下面已确认的生活统计。"
            "只能描述统计中确实存在的事实，不得补写缺失数值、诊断疾病、开药或修改剂量。"
            "先说明观察到的趋势，再给不超过三条低风险生活建议；必要时提醒联系医生。"
            "这是只读说明，不要提出任何需要写入档案的行动。\n"
            f"统计周期：近 {days} 天\n统计数据：{json.dumps(statistics, ensure_ascii=False, default=str)}"
        )
        from ..providers.platform_deepseek import PlatformDeepSeekProvider

        provider = PlatformDeepSeekProvider(self._client)
        answer = provider.chat(
            DEEPSEEK_DEFAULT_MODEL,
            [{"role": "user", "content": prompt}],
        )
        return {
            "answer": answer,
            "statistics": statistics,
            "model": provider.last_response_model or DEEPSEEK_DEFAULT_MODEL,
            "read_only": True,
        }

    def _health_service_summary(self, _body):
        return self._service("health").get_service_summary(self._actor_id())

    def _health_membership_activate(self, body):
        plan = self._validate_text(body.get("plan_code"), "会员方案", 32)
        return self._service("health").activate_membership(self._actor_id(), plan)

    def _health_profile_get(self, _body):
        return self._service("health").get_profile(self._actor_id())

    def _health_profile_save(self, body):
        return self._service("health").save_profile(self._actor_id(), body)

    def _health_reminders(self, _body):
        return self._service("health").list_reminders(self._actor_id())

    def _health_reminder_add(self, body):
        return self._service("health").add_reminder(self._actor_id(), **body)

    def _health_reminder_update(self, body):
        reminder_id = self._take_id(body, "reminder_id")
        values = {key: value for key, value in body.items() if key != "reminder_id"}
        return self._service("health").update_reminder(self._actor_id(), reminder_id, **values)

    def _health_reminder_toggle(self, body):
        reminder_id = self._take_id(body, "reminder_id")
        return self._service("health").toggle_reminder(self._actor_id(), reminder_id, body.get("enabled"))

    def _health_reminders_delete(self, body):
        return self._service("health").delete_reminders(self._actor_id(), body["reminder_ids"])

    def _chat_conversations(self, _body):
        return self._service("chat").list_conversations(self._actor_id())

    def _chat_create(self, body):
        return self._service("chat").create_conversation(self._actor_id(), body.get("title", "新对话"))

    def _chat_rename(self, body):
        return self._service("chat").rename_conversation(
            self._actor_id(), self._take_id(body, "conversation_id"), body["title"]
        )

    def _chat_delete(self, body):
        return self._service("chat").delete_conversations(self._actor_id(), body["conversation_ids"])

    def _chat_messages(self, body):
        return self._service("chat").list_messages(self._actor_id(), **body)

    def _chat_send(self, body):
        """Persist one turn and call the authenticated platform DeepSeek route.

        This mirrors the ordinary cloud chat path without exposing a provider
        key.  The full retrieval/trace path remains server-authorized; the web
        bridge merely supplies the bounded user content and conversation id.
        """

        content = self._validate_text(body.get("content"), "消息", 4_000)
        conversation_id = body.get("conversation_id")
        model = self._validate_text(body.get("model", ""), "模型", 160, required=False) or DEEPSEEK_DEFAULT_MODEL
        requested_share_context = body.get("share_context")
        if requested_share_context is not None and type(requested_share_context) is not bool:
            raise BridgeRequestError("生活资料授权开关格式无效")
        raw_attachment_ids = body.get("attachments", [])
        if raw_attachment_ids is None:
            raw_attachment_ids = []
        if type(raw_attachment_ids) is not list or len(raw_attachment_ids) > 4:
            raise BridgeRequestError("一条消息最多附加 4 个文件或图片")
        attachment_ids: list[int] = []
        for value in raw_attachment_ids:
            if type(value) is not int or value <= 0:
                raise BridgeRequestError("附件编号格式无效")
            if value in attachment_ids:
                raise BridgeRequestError("附件不能重复")
            attachment_ids.append(value)
        attachments = []
        files = self._service("files")
        for file_id in attachment_ids:
            stored = files.get_file(self._actor_id(), file_id)
            if not isinstance(stored, dict) or not isinstance(stored.get("content"), bytes):
                raise BridgeRequestError("附件读取失败", "not_found")
            attachments.append(
                prepare_attachment_bytes(
                    str(stored.get("original_name", "附件")), stored["content"]
                )
            )
        attachments = tuple(attachments)
        chat = self._service("chat")
        history = chat.context_for_provider(
            self._actor_id(), limit=30, conversation_id=conversation_id
        )
        # Build the same identity-bound system prompt used by the native chat:
        # preferences (preferred name/tone/time zone) and, for a health-related
        # question, the cloud service's bounded member snapshot are fetched
        # for the authenticated actor only.  No browser-provided user_id can
        # replace this actor id.
        from ..services.chat_personalization import personalized_system_message

        preferences = self._service("preferences").get(self._actor_id())
        stored_consent = preferences.get("ai_context_consent") is True
        share_context = stored_consent if requested_share_context is None else requested_share_context
        if share_context and not stored_consent:
            raise BridgeRequestError("请先在 AI 设置中同意读取已保存的生活资料", "consent_required")
        system_message = personalized_system_message(
            self._actor_id(), content, preferences, self._service("ai"), share_context=share_context
        )
        messages = [system_message, *(self._message_dict(item) for item in history)]
        # Scene instructions never create a separate provider or grant access.
        # Memory is read afresh only when both stored and per-turn consent hold.
        from .expansion import SCENES, memory_system_message
        scene = body.get('scene')
        if scene is not None:
            if scene not in SCENES:
                raise BridgeRequestError('请选择有效的饮食场景。')
            messages.insert(1, {'role': 'system', 'content': SCENES[scene]})
        share_memory = body.get('share_memory', False)
        if type(share_memory) is not bool:
            raise BridgeRequestError('记忆授权开关格式无效。')
        if share_memory:
            memory = memory_system_message(self, share_context=True)
            if memory:
                messages.insert(1, memory)
        # ChatTurn.as_dict() preserves image bytes and safely adds extracted
        # text for documents.  It is deliberately built server-side from
        # user-owned file IDs; the browser never supplies arbitrary bytes to
        # the model RPC.
        messages.append(ChatTurn("user", content, attachments).as_dict())
        exchange = chat.begin_message(
            self._actor_id(), content, provider="deepseek_cloud", model=model,
            conversation_id=conversation_id,
            attachments=attachments,
        )
        from ..providers.platform_deepseek import PlatformDeepSeekProvider
        from ..services.knowledge_chat import KnowledgeAwareProvider

        # Keep the same catalog -> card -> source -> language polish chain as
        # the legacy cloud chat page.  The cloud adapter supplies bounded
        # read-only library material; DeepSeek still performs selection and
        # reasoning, while the browser only sees the final answer.
        provider = KnowledgeAwareProvider(
            PlatformDeepSeekProvider(self._client),
            library=CloudKnowledgeLibrary(self._client),
        )
        try:
            chunks: list[str] = []
            for chunk in provider.chat_stream(model, messages):
                chunks.append(chunk)
            answer = "".join(chunks)
            chat.complete_message(
                self._actor_id(), exchange.assistant_message.id, answer,
                provider="deepseek_cloud", model=provider.provider.last_response_model or model,
            )
            # Retrieval receipts are bounded and contain only document IDs and
            # section references.  Failure to save a receipt must not discard
            # an answer that was already completed.
            try:
                chat.record_retrieval(
                    self._actor_id(), exchange.conversation.id,
                    exchange.user_message.id, exchange.assistant_message.id,
                    content, provider.last_trace,
                    provider="deepseek_cloud", model=provider.provider.last_response_model or model,
                )
            except Exception:
                pass
        except Exception as error:
            chat.fail_message(self._actor_id(), exchange.assistant_message.id, str(error), provider="deepseek_cloud", model=model)
            raise
        retrieval = _member_retrieval_receipt(provider.last_trace)
        return {
            "conversation": exchange.conversation,
            "user_message": exchange.user_message,
            "assistant_message": {"id": exchange.assistant_message.id, "content": answer, "role": "assistant", "status": "complete", "retrieval": retrieval},
            "retrieval": retrieval,
            "model": provider.provider.last_response_model or model,
        }

    def _ai_member_draft(self, body):
        """Generate a 2.2-style reviewable member draft and keep it in chat history.

        The browser supplies only the prompt and already-uploaded file IDs. The
        current Python session resolves those files, invokes the platform model,
        and lets the signed server ticket validate and persist the draft. No
        business row is written until a later proposal confirmation action.
        """

        values = self._only(body, {"content", "conversation_id", "attachments", "context_days", "input_source"})
        content = self._validate_text(values.get("content"), "问题", 4_000)
        conversation_id = self._optional_id(values.get("conversation_id"), "conversation_id")
        raw_ids = values.get("attachments", [])
        if raw_ids is None:
            raw_ids = []
        if type(raw_ids) is not list or len(raw_ids) > 4:
            raise BridgeRequestError("生活草稿最多附加 4 张图片")
        attachment_ids: list[int] = []
        image_bytes: list[bytes] = []
        chat_attachments = []
        files = self._service("files")
        for value in raw_ids:
            if type(value) is not int or value <= 0 or value in attachment_ids:
                raise BridgeRequestError("附件编号格式无效")
            attachment_ids.append(value)
            stored = files.get_file(self._actor_id(), value)
            if not isinstance(stored, dict) or not isinstance(stored.get("content"), bytes):
                raise BridgeRequestError("附件读取失败", "not_found")
            attachment = prepare_attachment_bytes(
                str(stored.get("original_name", "图片附件")), stored["content"]
            )
            if not attachment.is_image:
                raise BridgeRequestError("生活草稿目前只支持图片附件；文件请使用普通发送")
            if len(attachment.content) > 2 * 1024 * 1024:
                raise BridgeRequestError("单张图片不能超过 2 MiB")
            image_bytes.append(attachment.content)
            chat_attachments.append(attachment)
        if len(image_bytes) > 2:
            raise BridgeRequestError("平台视觉模型一次最多处理 2 张图片")
        context_days = values.get("context_days")
        if context_days is not None:
            context_days = self._nonnegative_int(context_days, "context_days", 90)
            if context_days < 1:
                raise BridgeRequestError("context_days 格式无效")
        preferences = self._service("preferences").get(self._actor_id())
        if preferences.get("ai_context_consent") is not True:
            raise BridgeRequestError("生成生活草稿前，请先同意读取已保存的生活资料", "consent_required")
        model = DEEPSEEK_VISION_MODEL if image_bytes else DEEPSEEK_DEFAULT_MODEL
        chat = self._service("chat")
        history_for_agent = chat.context_for_provider(
            self._actor_id(), limit=12, conversation_id=conversation_id
        )
        exchange = chat.begin_message(
            self._actor_id(),
            content,
            provider="deepseek_cloud",
            model=model,
            conversation_id=conversation_id,
            attachments=tuple(chat_attachments),
        )
        try:
            from ..providers.platform_deepseek import PlatformDeepSeekProvider
            from .life_chat import build_agent_provider, normalize_input_source

            raw_provider = PlatformDeepSeekProvider(self._client)
            current_for_agent = {"role": "user", "content": content}
            if image_bytes:
                current_for_agent["images"] = image_bytes
            provider = build_agent_provider(
                raw_provider,
                history_for_agent,
                current_for_agent,
                input_source=normalize_input_source(
                    has_images=bool(image_bytes), requested=values.get("input_source")
                ),
                answer_settings=preferences,
            )
            draft = self._service("ai").create_member_draft(
                self._actor_id(), provider,
                "deepseek_cloud",
                model,
                content,
                images=image_bytes or None,
                context_days=context_days,
            )
            if not isinstance(draft, Mapping) or not str(draft.get("answer", "")).strip():
                raise BridgeRequestError("AI 草稿返回格式无效", "protocol")
            answer = str(draft["answer"]).strip()
            chat.complete_message(
                self._actor_id(),
                exchange.assistant_message.id,
                answer,
                provider="deepseek_cloud",
                model=str(draft.get("model") or model),
            )
        except Exception as error:
            try:
                chat.fail_message(
                    self._actor_id(),
                    exchange.assistant_message.id,
                    str(error),
                    provider="deepseek_cloud",
                    model=model,
                )
            except Exception:
                pass
            raise
        return {
            "draft": draft,
            "conversation": exchange.conversation,
            "user_message": exchange.user_message,
            "assistant_message": {
                "id": exchange.assistant_message.id,
                "content": answer,
                "role": "assistant",
                "status": "complete",
            },
            "model": str(draft.get("model") or model),
        }

    def _ai_life_records_propose(self, body):
        values = self._only(body, {"content"})
        content = self._validate_text(values.get("content"), "生活描述", 4_000)
        from ..providers.platform_deepseek import PlatformDeepSeekProvider

        return self._service("ai").propose_life_records(
            self._actor_id(),
            PlatformDeepSeekProvider(self._client),
            "deepseek_cloud",
            DEEPSEEK_DEFAULT_MODEL,
            content,
        )

    def _ai_drafts_list(self, body):
        values = self._only(body, {"include_resolved"})
        include_resolved = values.get("include_resolved", False)
        if type(include_resolved) is not bool:
            raise BridgeRequestError("草稿状态开关格式无效")
        return self._service("ai").list_drafts(
            self._actor_id(), include_resolved=include_resolved
        )

    def _ai_draft_get(self, body):
        values = self._only(body, {"insight_id"})
        return self._service("ai").get_draft(self._actor_id(), self._take_id(values, "insight_id"))

    def _ai_proposal_confirm(self, body):
        values = self._only(body, {"insight_id", "proposal_id"})
        return self._service("ai").confirm_proposal(
            self._actor_id(), self._take_id(values, "insight_id"), self._take_id(values, "proposal_id")
        )

    def _ai_proposal_dismiss(self, body):
        values = self._only(body, {"insight_id", "proposal_id"})
        return self._service("ai").dismiss_proposal(
            self._actor_id(), self._take_id(values, "insight_id"), self._take_id(values, "proposal_id")
        )

    def _ai_config_update(self, body):
        values = self._only(
            body, {"enabled", "member_assistant_enabled", "advisor_summary_enabled", "context_days"}
        )
        return self._service("ai").update_ai_config(self._actor_id(), values)

    def _ai_advisor_summary(self, body):
        """Create a reviewable advisor summary for a bound member.

        The member id is deliberately supplied to the server together with the
        authenticated advisor actor.  We never accept an ``advisor_user_id``
        from the browser, and the cloud service rechecks the active binding
        before issuing its signed preparation ticket.
        """

        values = self._only(body, {"member_user_id", "context_days"})
        member_user_id = self._take_id(values, "member_user_id")
        context_days = values.get("context_days")
        if context_days is not None:
            context_days = self._nonnegative_int(context_days, "context_days", 90)
            if context_days < 1:
                raise BridgeRequestError("context_days 格式无效")
        from ..providers.platform_deepseek import PlatformDeepSeekProvider

        return self._service("ai").create_advisor_summary_draft(
            self._actor_id(),
            member_user_id,
            PlatformDeepSeekProvider(self._client),
            "deepseek_cloud",
            DEEPSEEK_DEFAULT_MODEL,
            context_days=context_days,
        )

    def _voice_transcribe(self, body):
        """Recognize bounded 16 kHz mono PCM locally with the bundled Vosk model.

        The browser sends raw PCM as base64.  Audio is decoded and passed to
        ``OfflineSpeechRecognizer`` in this process; no audio bytes are sent to
        the cloud client.  The explicit format fields prevent silently treating
        compressed browser recordings as PCM.
        """

        values = self._only(body, {"audio_base64", "sample_rate", "channels", "sample_width"})
        encoded = values.get("audio_base64")
        if not isinstance(encoded, str) or not encoded or len(encoded) > 2_560_000:
            raise BridgeRequestError("语音数据格式无效")
        sample_rate = values.get("sample_rate")
        channels = values.get("channels")
        sample_width = values.get("sample_width")
        if sample_rate != 16_000 or channels != 1 or sample_width != 2:
            raise BridgeRequestError("语音必须是 16 kHz、单声道、16 位 PCM")
        try:
            # validate=True rejects whitespace, data URLs and malformed padding.
            audio = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            raise BridgeRequestError("语音数据不是有效的 base64") from None
        if not audio or len(audio) > 1_920_000 or len(audio) % 2:
            raise BridgeRequestError("语音长度必须不超过 60 秒且为完整 PCM 样本")
        from ..services.voice import OfflineSpeechRecognizer

        text = OfflineSpeechRecognizer().transcribe_pcm(audio, sample_rate=sample_rate)
        return {
            "text": text,
            "sample_rate": sample_rate,
            "channels": channels,
            "sample_width": sample_width,
            "duration_seconds": round(len(audio) / (sample_rate * channels * sample_width), 3),
            "offline": True,
        }

    def _voice_polish(self, body):
        """Tidy one voice transcript so it can be written straight into a field.

        Audio never leaves the device (``voice.transcribe`` is local). Only the
        transcript text goes to the authenticated DeepSeek chat route, and the
        answer is bounded to one clean line. Callers fall back to the raw
        transcript when this raises, so a slow or failed model never blocks input.
        """

        text = self._validate_text(body.get("text"), "语音内容", 4_000)
        config = self._service("ai").get_ai_config(self._actor_id())
        AiAssistantService._require_ai_feature(config, "member_assistant_enabled", "会员 AI 助手")
        from ..providers.platform_deepseek import PlatformDeepSeekProvider

        prompt = (
            "你是中文语音转写的整理助手。把下面这段口语转写整理成可以直接填进表单的一句话："
            "补齐标点、删掉口头语和重复、保留全部事实与数字，不增加、不删减、不解释、不扩写。"
            "只输出整理后的文字本身，不要引号、不要前缀、不要 Markdown，最多 300 字。\n原文：\n"
            + text
        )
        answer = PlatformDeepSeekProvider(self._client).chat(
            DEEPSEEK_DEFAULT_MODEL,
            [{"role": "user", "content": prompt}],
        )
        raw = str(answer or "").strip()
        raw = re.sub(r"^```(?:text|markdown)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
        cleaned = re.sub(r"\s+", " ", raw).strip().strip('"').strip("“”").strip()
        if not cleaned:
            raise BridgeRequestError("没有整理出可用文字，请保留原话重试。", "invalid_response")
        return {"text": cleaned[:400]}
    def _voice_smart_record(self, body):
        """Turn one natural-language voice transcript into saved life records.

        Transcription itself stays local (``voice.transcribe``).  This second,
        explicit action sends only the transcript to the authenticated
        DeepSeek provider, validates the bounded JSON response, and writes one
        row for every event the member described.  The write is intentionally
        append-only, so a sentence containing several events (for example
        breakfast, water and a walk) produces several records.
        """

        text = self._validate_text(body.get("text"), "语音内容", 4_000)
        from ..services.ai_assistant import AiAssistantService
        config = self._service("ai").get_ai_config(self._actor_id())
        AiAssistantService._require_ai_feature(config, "member_assistant_enabled", "会员 AI 助手")
        from ..providers.platform_deepseek import PlatformDeepSeekProvider
        from ..time_utils import beijing_now

        now = beijing_now().isoformat(timespec="seconds")
        prompt = (
            "你是 FAWNSEE 的语音生活记录整理助手，不是医生。"
            "把用户的一段自然语言拆成所有明确发生的生活事项；一句话中有多个事项必须全部输出。"
            "只返回一个 JSON 对象，禁止 Markdown 和额外文字，格式为 "
            '{"records":[{"category":"diet|water|activity|sleep|environment|medical|metric",'
            '"occurred_at":"带时区 ISO 8601 时间", "content":"简短中文记录", "details":{}}]}。'
            f"当前北京时间是 {now}；未说明具体时刻时使用这个时间。"
            "六类生活记录详情字段只能是：diet 的 calories_kcal（数字，允许在食物明确但热量未提供时做保守估算）和 amount_g；"
            "water 的 amount_ml；activity 的 duration_minutes、steps、energy_kcal；"
            "sleep 的 duration_hours、quality（1-5）；environment 的 temperature_c、humidity_percent、"
            "ventilation_minutes、quality_score；medical 的 event_type（consultation/medication/other）、"
            "name、description、location。"
            "第七类 metric 只能记录体重或血糖，details 必须包含 kind（weight/glucose）、value（数字）、"
            "unit（体重用 kg，血糖用 mmol/L 或 mg/dL），可选 context（fasting/before_meal/after_meal/random/unspecified）"
            "和 notes。不要诊断，不修改药物，不推测用户没有提到的事实；食品热量只能给出大致估算，"
            "并在 content 中明确写“AI估算”。时间、数量不确定时在 content 中注明待核对。"
            "records 只保留明确生活事实，最多 8 条。用户原话如下：\n" + text
        )
        answer = PlatformDeepSeekProvider(self._client).chat(
            DEEPSEEK_DEFAULT_MODEL,
            [{"role": "user", "content": prompt}],
        )
        raw = str(answer or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"\s*```$", "", raw)
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            raise BridgeRequestError("DeepSeek 没有返回可保存的记录格式，请重试。", "invalid_response") from None
        rows = parsed.get("records") if isinstance(parsed, dict) else None
        if not isinstance(rows, list) or not rows or len(rows) > 8:
            raise BridgeRequestError("DeepSeek 没有返回可保存的生活记录，请重试。", "invalid_response")

        categories = {"diet", "water", "activity", "sleep", "environment", "medical", "metric"}
        saved: list[dict[str, Any]] = []
        from datetime import datetime
        from .expansion import dispatch

        # Validate the complete response before the first write.  A malformed
        # fifth item must never leave the first four items partially saved.
        for item in rows:
            if not isinstance(item, dict) or set(item) - {"category", "occurred_at", "content", "details"}:
                raise BridgeRequestError("DeepSeek 返回了无法核对的记录字段，请重试。", "invalid_response")
            category = item.get("category")
            if category not in categories:
                raise BridgeRequestError("DeepSeek 返回了无效记录分类，请重试。", "invalid_response")
            occurred_at, content, details = item.get("occurred_at"), item.get("content"), item.get("details") or {}
            if not isinstance(occurred_at, str) or not isinstance(content, str) or not content.strip() or len(content) > 10_000 or not isinstance(details, dict):
                raise BridgeRequestError("DeepSeek 返回了不完整的记录，请重试。", "invalid_response")
            try:
                parsed_time = datetime.fromisoformat(occurred_at)
            except (TypeError, ValueError):
                raise BridgeRequestError("DeepSeek 返回的记录时间无效，请重试。", "invalid_response") from None
            if parsed_time.tzinfo is None:
                raise BridgeRequestError("DeepSeek 返回的记录时间缺少时区，请重试。", "invalid_response")
            if category == "metric":
                allowed = {"kind", "value", "unit", "context", "notes"}
                if set(details) - allowed or details.get("kind") not in {"weight", "glucose"}:
                    raise BridgeRequestError("身体指标字段无效，请重试。", "invalid_response")
                try:
                    value = float(details.get("value"))
                except (TypeError, ValueError):
                    raise BridgeRequestError("身体指标数值无效，请重试。", "invalid_response") from None
                import math
                if not math.isfinite(value):
                    raise BridgeRequestError("身体指标数值无效，请重试。", "invalid_response")
                valid_units = {"kg"} if details["kind"] == "weight" else {"mmol/L", "mg/dL"}
                if not value > 0 or details.get("unit") not in valid_units:
                    raise BridgeRequestError("身体指标单位或数值无效，请重试。", "invalid_response")
                if details.get("context", "unspecified") not in {"fasting", "before_meal", "after_meal", "random", "unspecified"}:
                    raise BridgeRequestError("身体指标场景无效，请重试。", "invalid_response")
                if not isinstance(details.get("notes", ""), str) or len(details.get("notes", "")) > 4_000:
                    raise BridgeRequestError("身体指标备注无效，请重试。", "invalid_response")
                continue
            allowed_details = {
                "diet": {"calories_kcal", "amount_g"},
                "water": {"amount_ml"},
                "activity": {"duration_minutes", "steps", "energy_kcal"},
                "sleep": {"duration_hours", "quality"},
                "environment": {"temperature_c", "humidity_percent", "ventilation_minutes", "quality_score"},
                "medical": {"event_type", "name", "description", "location"},
            }[category]
            if set(details) - allowed_details:
                raise BridgeRequestError("生活记录详情字段无效，请重试。", "invalid_response")
            try:
                from ..services.health import HealthService
                HealthService._validate_record_details(category, details)
            except (ValueError, LookupError) as exc:
                raise BridgeRequestError(f"DeepSeek 返回的{category}记录未通过校验：{exc}", "invalid_response") from None

        for item in rows:
            if not isinstance(item, dict) or set(item) - {"category", "occurred_at", "content", "details"}:
                raise BridgeRequestError("DeepSeek 返回了无法核对的记录字段，请重试。", "invalid_response")
            category = item.get("category")
            if category not in categories:
                raise BridgeRequestError("DeepSeek 返回了无效记录分类，请重试。", "invalid_response")
            occurred_at = item.get("occurred_at")
            content = item.get("content")
            details = item.get("details") or {}
            if not isinstance(occurred_at, str) or not isinstance(content, str) or not content.strip() or not isinstance(details, dict):
                raise BridgeRequestError("DeepSeek 返回了不完整的记录，请重试。", "invalid_response")
            try:
                parsed_time = datetime.fromisoformat(occurred_at)
            except (TypeError, ValueError):
                raise BridgeRequestError("DeepSeek 返回的记录时间无效，请重试。", "invalid_response") from None
            if parsed_time.tzinfo is None:
                raise BridgeRequestError("DeepSeek 返回的记录时间缺少时区，请重试。", "invalid_response")
            clean_content = content.strip()[:10_000]
            if category == "metric":
                allowed = {"kind", "value", "unit", "context", "notes"}
                if set(details) - allowed or details.get("kind") not in {"weight", "glucose"}:
                    raise BridgeRequestError("身体指标字段无效，请重试。", "invalid_response")
                try:
                    value = float(details.get("value"))
                except (TypeError, ValueError):
                    raise BridgeRequestError("身体指标数值无效，请重试。", "invalid_response") from None
                unit = details.get("unit")
                kind = details["kind"]
                valid_units = {"kg"} if kind == "weight" else {"mmol/L", "mg/dL"}
                if not value > 0 or unit not in valid_units:
                    raise BridgeRequestError("身体指标单位或数值无效，请重试。", "invalid_response")
                context = details.get("context", "unspecified")
                if context not in {"fasting", "before_meal", "after_meal", "random", "unspecified"}:
                    raise BridgeRequestError("身体指标场景无效，请重试。", "invalid_response")
                notes = details.get("notes", "")
                if not isinstance(notes, str) or len(notes) > 4_000:
                    raise BridgeRequestError("身体指标备注无效，请重试。", "invalid_response")
                result = dispatch(self, "expansion.observations.create", {
                    "kind": kind, "value": value, "unit": unit,
                    "measured_at": occurred_at, "context": context, "notes": notes,
                })
                saved.append({"category": category, "occurred_at": occurred_at, "content": clean_content, "details": details, "id": result.get("id") if isinstance(result, dict) else result})
                continue

            allowed_details = {
                "diet": {"calories_kcal", "amount_g"},
                "water": {"amount_ml"},
                "activity": {"duration_minutes", "steps", "energy_kcal"},
                "sleep": {"duration_hours", "quality"},
                "environment": {"temperature_c", "humidity_percent", "ventilation_minutes", "quality_score"},
                "medical": {"event_type", "name", "description", "location"},
            }[category]
            if set(details) - allowed_details:
                raise BridgeRequestError("生活记录详情字段无效，请重试。", "invalid_response")
            try:
                validated = self._service("health").add_life_record(
                    self._actor_id(), category, occurred_at, clean_content,
                    source="ai_confirmed", details=details,
                )
            except (ValueError, LookupError) as exc:
                raise BridgeRequestError(f"DeepSeek 返回的{category}记录未通过校验：{exc}", "invalid_response") from None
            saved.append({"category": category, "occurred_at": occurred_at, "content": clean_content, "details": details, "id": validated})
        return {"text": text, "records": saved, "count": len(saved), "model": DEEPSEEK_DEFAULT_MODEL}

    def _library_catalog(self, body):
        return CloudKnowledgeLibrary(self._client).catalog(body.get("kind", "knowledge"), limit=body.get("limit", 200), offset=body.get("offset", 0))

    def _library_search(self, body):
        return CloudKnowledgeLibrary(self._client).search(body.get("kind", "knowledge"), body.get("query", ""), limit=body.get("limit", 5))

    def _library_card(self, body):
        lib = CloudKnowledgeLibrary(self._client)
        kind = body.get("kind", "knowledge")
        if kind == "language":
            return lib.open_language_card(body["document_id"], body.get("section_id"))
        return lib.open_knowledge_card(body["document_id"], body.get("section_id"))

    def _library_source(self, body):
        return CloudKnowledgeLibrary(self._client).open_source(body["document_id"], anchors=body.get("anchors", []))

    def _forum_topics(self, body):
        return self._service("forum").list_topics(self._actor_id(), body.get("query", ""))

    def _forum_posts(self, body):
        return self._service("forum").list_posts(self._actor_id(), body["topic"], body.get("limit", 50))

    def _forum_create(self, body):
        # Keep the legacy cloud RPC contract for text-only posts.  The v12/v13
        # platform endpoint accepts exactly three positional arguments; sending
        # an explicit ``None`` as a fourth argument makes its signature binder
        # reject an otherwise valid text-only post with a generic validation
        # error.  Only send the optional fourth argument when the member has
        # actually selected attachments.
        attachment_ids = body.get("attachment_ids")
        forum = self._service("forum")
        if attachment_ids:
            return forum.create_post(self._actor_id(), body["topic"], body["content"], attachment_ids)
        return forum.create_post(self._actor_id(), body["topic"], body["content"])

    def _forum_like(self, body):
        return self._service("forum").toggle_like(self._actor_id(), self._take_id(body, "post_id"))

    def _forum_comment(self, body):
        attachment_ids = body.get("attachment_ids")
        forum = self._service("forum")
        if attachment_ids:
            return forum.add_comment(self._actor_id(), self._take_id(body, "post_id"), body.get("content", ""), attachment_ids)
        return forum.add_comment(self._actor_id(), self._take_id(body, "post_id"), body.get("content", ""))

    def _forum_comments(self, body):
        return self._service("forum").list_comments(self._actor_id(), self._take_id(body, "post_id"), body.get("limit", 100))

    def _preferences_get(self, _body):
        return self._service("preferences").get(self._actor_id())

    def _preferences_update(self, body):
        return self._service("preferences").update(self._actor_id(), body)

    def _service_dashboard(self, _body):
        return self._service("management").get_dashboard(self._actor_id())

    def _service_members(self, _body):
        return self._service("management").list_members(self._actor_id())

    def _service_advisors(self, _body):
        return self._service("management").list_advisors(self._actor_id())

    def _service_appointments(self, _body):
        return self._service("management").list_appointments(self._actor_id())

    def _service_request_appointment(self, body):
        return self._service("management").request_appointment(self._actor_id(), **body)

    def _service_update_appointment(self, body):
        appointment_id = self._take_id(body, "appointment_id")
        values = {key: value for key, value in body.items() if key != "appointment_id"}
        return self._service("management").update_appointment(self._actor_id(), appointment_id, **values)

    # Service history, advisor workbench and operator task actions.  These
    # wrappers deliberately pick a finite set of fields before calling the
    # remote facade; the browser can never supply a method name or SQL/path.
    def _service_history(self, body):
        values = self._only(body, {"member_user_id", "limit"})
        member_id = self._optional_id(values.get("member_user_id"), "member_user_id")
        return self._service("management").list_visit_records(
            self._actor_id(), member_user_id=member_id, limit=self._limit(values.get("limit", 200))
        )

    def _service_member_overview(self, body):
        """Return an advisor/operator-scoped member profile and recent facts."""

        values = self._only(body, {"member_user_id", "recent_record_limit"})
        member_id = self._take_id(values, "member_user_id")
        limit = self._limit(values.get("recent_record_limit", 20))
        return self._service("management").get_member_overview(
            self._actor_id(), member_id, recent_record_limit=limit
        )

    def _service_tasks(self, body):
        values = self._only(body, {"member_user_id", "status", "limit"})
        member_id = self._optional_id(values.get("member_user_id"), "member_user_id")
        status = values.get("status")
        if status is not None:
            status = self._validate_text(status, "任务状态", 32)
        return self._service("management").list_visit_tasks(
            self._actor_id(), member_user_id=member_id, status=status,
            limit=self._limit(values.get("limit", 200)),
        )

    def _service_task_create(self, body):
        values = self._only(
            body, {"member_user_id", "title", "scheduled_at", "advisor_user_id", "notes"}
        )
        member_id = self._take_id(values, "member_user_id")
        title = self._validate_text(values.get("title"), "任务标题", 200)
        scheduled = self._optional_text(values.get("scheduled_at"), "计划时间", 64)
        advisor_id = self._optional_id(values.get("advisor_user_id"), "advisor_user_id")
        notes = self._optional_text(values.get("notes"), "任务备注", 10_000)
        return self._service("management").create_visit_task(
            self._actor_id(), member_id, title=title, scheduled_at=scheduled,
            advisor_user_id=advisor_id, notes=notes,
        )

    def _service_task_start(self, body):
        values = self._only(body, {"task_id"})
        return self._service("management").start_visit_task(
            self._actor_id(), self._take_id(values, "task_id")
        )

    def _service_task_complete(self, body):
        values = self._only(
            body,
            {"task_id", "summary", "visited_at", "details", "next_visit_at", "next_visit_title", "create_member_reminder"},
        )
        task_id = self._take_id(values, "task_id")
        summary = self._validate_text(values.get("summary"), "上门总结", 20_000)
        visited_at = self._optional_text(values.get("visited_at"), "实际时间", 64)
        next_visit_at = self._optional_text(values.get("next_visit_at"), "下次时间", 64)
        next_title = self._validate_text(values.get("next_visit_title", "定期复访"), "下次任务标题", 200)
        details = values.get("details")
        if details is not None and not isinstance(details, Mapping):
            raise BridgeRequestError("上门详情格式无效")
        create_reminder = values.get("create_member_reminder", True)
        if not isinstance(create_reminder, bool):
            raise BridgeRequestError("提醒开关格式无效")
        return self._service("management").complete_visit_task(
            self._actor_id(), task_id, summary=summary, visited_at=visited_at,
            details=dict(details) if details is not None else None,
            next_visit_at=next_visit_at, next_visit_title=next_title,
            create_member_reminder=create_reminder,
        )

    def _service_advisor_statistics(self, _body):
        return self._service("management").get_advisor_work_statistics(self._actor_id())

    def _service_work_statistics(self, body):
        values = self._only(body, {"advisor_user_id", "member_user_id", "starts_at", "ends_at"})
        advisor_id = self._optional_id(values.get("advisor_user_id"), "advisor_user_id")
        member_id = self._optional_id(values.get("member_user_id"), "member_user_id")
        starts = self._optional_text(values.get("starts_at"), "开始时间", 64)
        ends = self._optional_text(values.get("ends_at"), "结束时间", 64)
        return self._service("management").get_filtered_work_statistics(
            self._actor_id(), advisor_user_id=advisor_id, member_user_id=member_id,
            starts_at=starts, ends_at=ends,
        )

    def _service_advisor_create(self, body):
        values = self._only(
            body,
            {"username", "password", "display_name", "organization", "specialty", "bio", "valid_until"},
        )
        username = self._validate_text(values.get("username"), "顾问账号", 64)
        password = self._validate_text(values.get("password"), "顾问密码", 1024)
        display = self._validate_text(values.get("display_name"), "顾问姓名", 100)
        kwargs = {
            "display_name": display,
            "organization": self._optional_text(values.get("organization"), "机构", 200),
            "specialty": self._optional_text(values.get("specialty"), "专长", 200),
            "bio": self._optional_text(values.get("bio"), "简介", 4_000),
            "valid_until": self._optional_text(values.get("valid_until"), "有效期", 64),
        }
        return self._service("management").create_advisor(
            self._actor_id(), username, password, **kwargs
        )

    def _service_advisor_profile(self, body):
        values = self._only(body, {"advisor_user_id", "display_name", "organization", "specialty", "bio"})
        advisor_id = self._take_id(values, "advisor_user_id")
        display = self._validate_text(values.get("display_name"), "顾问姓名", 100)
        return self._service("management").save_advisor_profile(
            self._actor_id(), advisor_id, display_name=display,
            organization=self._optional_text(values.get("organization"), "机构", 200),
            specialty=self._optional_text(values.get("specialty"), "专长", 200),
            bio=self._optional_text(values.get("bio"), "简介", 4_000),
        )

    def _service_advisor_validity(self, body):
        values = self._only(body, {"advisor_user_id", "valid_until"})
        return self._service("management").set_advisor_validity(
            self._actor_id(), self._take_id(values, "advisor_user_id"),
            valid_until=self._validate_text(values.get("valid_until"), "有效期限", 64),
        )

    def _service_account_enabled(self, body):
        values = self._only(body, {"target_user_id", "enabled"})
        enabled = values.get("enabled")
        if not isinstance(enabled, bool):
            raise BridgeRequestError("账号开关格式无效")
        return self._service("management").set_account_enabled(
            self._actor_id(), self._take_id(values, "target_user_id"), enabled=enabled
        )

    def _service_reset_password(self, body):
        values = self._only(body, {"target_user_id", "new_password"})
        return self._service("management").reset_account_password(
            self._actor_id(), self._take_id(values, "target_user_id"),
            self._validate_text(values.get("new_password"), "新密码", 1024),
        )

    def _shopping_products(self, body):
        return self._service("commerce").list_products(self._actor_id(), **body)

    def _shopping_cart(self, _body):
        return self._service("commerce").list_cart(self._actor_id())

    def _shopping_cart_add(self, body):
        return self._service("commerce").add_to_cart(self._actor_id(), self._take_id(body, "product_id"), body.get("quantity", 1))

    def _shopping_cart_set(self, body):
        return self._service("commerce").set_cart_quantity(self._actor_id(), self._take_id(body, "product_id"), body["quantity"])

    def _shopping_favorites(self, _body):
        return self._service("commerce").list_favorites(self._actor_id())

    def _shopping_favorite(self, body):
        return self._service("commerce").set_favorite(self._actor_id(), self._take_id(body, "product_id"), body["favorite"])

    def _shopping_checkout(self, _body):
        return self._service("commerce").checkout_cart(self._actor_id())

    def _shopping_orders(self, _body):
        return self._service("commerce").list_orders(self._actor_id())

    def _shopping_recommendations(self, body):
        values = self._only(body, {"member_user_id"})
        member_id = self._optional_id(values.get("member_user_id"), "member_user_id")
        return self._service("commerce").list_recommendations(self._actor_id(), member_user_id=member_id)

    def _shopping_recommendation_view(self, body):
        values = self._only(body, {"recommendation_id"})
        return self._service("commerce").record_product_view(
            self._actor_id(), self._take_id(values, "recommendation_id")
        )

    def _shopping_recommendation_interest(self, body):
        values = self._only(body, {"recommendation_id"})
        return self._service("commerce").mark_recommendation_interested(
            self._actor_id(), self._take_id(values, "recommendation_id")
        )

    def _shopping_product_interest(self, body):
        values = self._only(body, {"product_id"})
        return self._service("commerce").mark_product_interested(
            self._actor_id(), self._take_id(values, "product_id")
        )

    def _shopping_product_create(self, body):
        allowed = {"sku", "name", "category", "brand", "specification", "price_cents", "unit_price_cents", "unit", "image_path", "description", "source_type", "is_active"}
        values = self._only(body, allowed)
        for key, limit in (("sku", 100), ("name", 200), ("category", 100), ("brand", 200), ("specification", 500), ("unit", 50), ("image_path", 1_000), ("description", 4_000), ("source_type", 100)):
            if key in values and values[key] is not None:
                values[key] = self._validate_text(values[key], key, limit)
        if "is_active" in values and not isinstance(values["is_active"], bool):
            raise BridgeRequestError("上架状态格式无效")
        return self._service("commerce").create_product(self._actor_id(), **values)

    def _shopping_product_update(self, body):
        values = self._only(body, {"product_id", "sku", "name", "category", "brand", "specification", "price_cents", "unit_price_cents", "unit", "image_path", "description", "source_type", "is_active"})
        product_id = self._take_id(values, "product_id")
        values.pop("product_id", None)
        for key, limit in (("sku", 100), ("name", 200), ("category", 100), ("brand", 200), ("specification", 500), ("unit", 50), ("image_path", 1_000), ("description", 4_000), ("source_type", 100)):
            if key in values and values[key] is not None:
                values[key] = self._validate_text(values[key], key, limit)
        if "is_active" in values and not isinstance(values["is_active"], bool):
            raise BridgeRequestError("上架状态格式无效")
        return self._service("commerce").update_product(self._actor_id(), product_id, **values)

    def _shopping_product_active(self, body):
        values = self._only(body, {"product_id", "is_active"})
        if not isinstance(values.get("is_active"), bool):
            raise BridgeRequestError("上架状态格式无效")
        return self._service("commerce").set_product_active(
            self._actor_id(), self._take_id(values, "product_id"), values["is_active"]
        )

    def _shopping_product_delete(self, body):
        values = self._only(body, {"product_id"})
        return self._service("commerce").delete_product(
            self._actor_id(), self._take_id(values, "product_id")
        )

    def _shopping_product_recommend(self, body):
        values = self._only(body, {"member_user_id", "product_id", "reason"})
        return self._service("commerce").recommend_product(
            self._actor_id(), self._take_id(values, "member_user_id"),
            self._take_id(values, "product_id"),
            self._validate_text(values.get("reason"), "推荐理由", 2_000),
        )

    def _ai_config(self, _body):
        return self._service("ai").get_ai_config(self._actor_id())

    def _ai_context(self, body):
        member_id = body.get("member_id")
        if member_id is not None and self._take_id({"member_id": member_id}, "member_id") != self._actor_id():
            raise BridgeRequestError("只能读取当前登录会员的生活资料", "permission")
        return self._service("ai").build_member_context(
            self._actor_id(), member_id, context_days=body.get("context_days", 14)
        )

    def _network_probe(self, _body):
        # Use the same bounded HTTPS probe as the native shell so the web UI
        # receives real latency and indicative application-path throughput.
        # The probe only requests /health/ready and never sends account data.
        result = probe_backend(self._base_url)
        return {
            **result,
            "online": result.get("state") == "online",
            "endpoint": self._base_url,
            "ip_location": "中国·北京（阿里云服务节点）",
        }

    def _weather_current(self, body):
        # The public weather endpoint receives only the member-entered city or
        # coordinates.  It never receives an auth token or database path.
        values = dict(body or {})
        # A fresh account has no saved city yet; use the bounded local default
        # while the profile editor is still empty.  This keeps the UI useful
        # without sending an IP address to a third-party geolocation vendor.
        service = MemberWeatherService()
        if not values.get("city") and values.get("latitude") is None:
            return service.current_from_ip()
        try:
            return service.current(values)
        except Exception:
            # The weather card must not blank the member shell when a public
            # provider is temporarily unreachable.
            return service.fallback()

    # Files are transferred as bounded base64 bytes.  The bridge intentionally
    # has no "source path" or filesystem export action, so a web page cannot
    # make the desktop process read an arbitrary local path.
    def _files_upload(self, body):
        values = self._only(body, {"original_name", "content"})
        name = self._validate_text(values.get("original_name"), "文件名", 255)
        encoded = values.get("content")
        if not isinstance(encoded, str) or not encoded or len(encoded) > 24 * 1024 * 1024:
            raise BridgeRequestError("文件内容格式无效", "validation")
        # Browser FileReader commonly emits a data URL.  Accept only its
        # bounded media/base64 header; never treat the value as a filesystem
        # path or URL to fetch.
        if encoded.startswith("data:"):
            header, separator, encoded_body = encoded.partition(",")
            if not separator or not re.fullmatch(r"data:[^,;]{1,100}(?:;[^,;]{1,40})*;base64", header, re.I):
                raise BridgeRequestError("文件内容格式无效", "validation")
            encoded = encoded_body
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            raise BridgeRequestError("文件内容不是有效的 base64", "validation") from None
        if not content or len(content) > 15 * 1024 * 1024:
            raise BridgeRequestError("文件大小超出限制", "limit")
        return self._service("files").upload_bytes(self._actor_id(), name, content)

    def _files_list(self, _body):
        return self._service("files").list_files(self._actor_id())

    def _files_get(self, body):
        values = self._only(body, {"file_id"})
        return self._service("files").get_file(self._actor_id(), self._take_id(values, "file_id"))

    def _files_delete(self, body):
        values = self._only(body, {"file_id"})
        return self._service("files").delete_file(self._actor_id(), self._take_id(values, "file_id"))

    def _files_storage(self, _body):
        return self._service("files").storage_usage(self._actor_id())

    def _files_reports_list(self, body):
        values = self._only(body, {"include_archived"})
        include_archived = values.get("include_archived", False)
        if not isinstance(include_archived, bool):
            raise BridgeRequestError("归档开关格式无效")
        return self._service("files").list_reports(self._actor_id(), include_archived=include_archived)

    def _files_report_get(self, body):
        values = self._only(body, {"report_id"})
        return self._service("files").get_report(self._actor_id(), self._take_id(values, "report_id"))

    def _files_report_create(self, body):
        values = self._only(body, {"file_id", "report_type", "report_date", "institution", "summary"})
        return self._service("files").create_report_draft(
            self._actor_id(), self._take_id(values, "file_id"),
            report_type=self._validate_text(values.get("report_type", "other"), "报告类型", 100),
            report_date=self._optional_text(values.get("report_date"), "报告日期", 32),
            institution=self._optional_text(values.get("institution"), "机构名称", 500),
            summary=self._optional_text(values.get("summary"), "报告摘要", 20_000),
        )

    def _files_report_delete(self, body):
        values = self._only(body, {"report_id"})
        return self._service("files").delete_report(self._actor_id(), self._take_id(values, "report_id"))

    def _files_report_archive(self, body):
        values = self._only(body, {"report_id"})
        return self._service("files").archive_report(self._actor_id(), self._take_id(values, "report_id"))

    def _files_report_confirm(self, body):
        values = self._only(body, {"report_id"})
        return self._service("files").confirm_report(self._actor_id(), self._take_id(values, "report_id"))

    def _files_report_item_add(self, body):
        values = self._only(body, {"report_id", "item_name", "result_value", "unit", "reference_range", "flag"})
        return self._service("files").add_report_item_draft(
            self._actor_id(), self._take_id(values, "report_id"),
            self._validate_text(values.get("item_name"), "项目名称", 500),
            result_value=self._optional_text(values.get("result_value"), "结果", 500),
            unit=self._optional_text(values.get("unit"), "单位", 100),
            reference_range=self._optional_text(values.get("reference_range"), "参考范围", 500),
            flag=self._optional_text(values.get("flag"), "项目标记", 32),
        )

    def _files_report_item_confirm(self, body):
        values = self._only(body, {"report_id", "item_id"})
        return self._service("files").confirm_report_item(
            self._actor_id(), self._take_id(values, "report_id"), self._take_id(values, "item_id")
        )

    # Developer operations are intentionally separate and require operator.
    def _developer_users(self, body):
        role = body.get("role", "member")
        if role not in {"operator", "advisor", "member"}:
            raise BridgeRequestError("用户类别不正确")
        return self._client.request("GET", "/v1/developer/users", params={"role_code": role, "offset": body.get("offset", 0), "limit": body.get("limit", 100)})

    def _developer_user(self, body):
        return self._client.request("GET", f"/v1/developer/users/{self._take_id(body, 'user_id')}")

    def _developer_settings(self, _body):
        return self._client.request("GET", "/v1/developer/settings")

    def _developer_status(self, _body):
        return self._client.request("GET", "/v1/developer/status")

    def _developer_client_settings(self, _body):
        return self._client.request("GET", "/v1/client-settings")

    def _developer_client_start(self, _body):
        return self._client.request("POST", "/v1/client-start", json={})

    def _developer_user_patch(self, body):
        values = self._only(body, {"user_id", "username", "password", "account_status"})
        user_id = self._take_id(values, "user_id")
        patch: dict[str, Any] = {}
        if "username" in values:
            patch["username"] = self._validate_text(values["username"], "用户名", 64)
        if "password" in values:
            patch["password"] = self._validate_text(values["password"], "密码", 1024)
        if "account_status" in values:
            status = self._validate_text(values["account_status"], "账号状态", 16)
            if status not in {"active", "disabled", "locked"}:
                raise BridgeRequestError("账号状态格式无效")
            patch["account_status"] = status
        if not patch:
            raise BridgeRequestError("至少需要修改一个账号字段")
        return self._client.request("PATCH", f"/v1/developer/users/{user_id}", json=patch)

    def _developer_user_records(self, body):
        values = self._only(body, {"user_id", "table", "offset", "limit"})
        table = self._developer_table(values.get("table"))
        user_id = self._take_id(values, "user_id")
        offset = self._nonnegative_int(values.get("offset", 0), "offset", 100_000)
        limit = self._limit(values.get("limit", 100), maximum=200)
        return self._client.request(
            "GET", f"/v1/developer/users/{user_id}/records/{table}",
            params={"offset": offset, "limit": limit},
        )

    def _developer_user_record_patch(self, body):
        values = self._only(body, {"user_id", "table", "row_id", "changes"})
        table = self._developer_table(values.get("table"))
        user_id = self._take_id(values, "user_id")
        row_id = self._take_id(values, "row_id")
        changes = values.get("changes")
        if not isinstance(changes, Mapping) or not changes or len(changes) > 40:
            raise BridgeRequestError("记录修改格式无效")
        # Keys are field names, not SQL.  Keep them bounded and reject obvious
        # credential fields before the request reaches the server.
        safe_changes: dict[str, Any] = {}
        for key, value in changes.items():
            if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key):
                raise BridgeRequestError("记录字段名格式无效")
            if re.search(r"password|token|secret|api.?key|credential", key, re.I):
                raise BridgeRequestError("不允许修改敏感凭据字段", "permission")
            safe_changes[key] = value
        return self._client.request(
            "PATCH", f"/v1/developer/users/{user_id}/records/{table}/{row_id}",
            json={"changes": safe_changes},
        )

    def _developer_settings_publish(self, body):
        values = self._only(body, {"expected_revision", "settings", "api_key"})
        revision = values.get("expected_revision")
        if type(revision) is not int or revision < 0:
            raise BridgeRequestError("设置版本号格式无效")
        settings = values.get("settings")
        if not isinstance(settings, Mapping):
            raise BridgeRequestError("应用设置格式无效")
        api_key = values.get("api_key")
        if api_key is not None and (not isinstance(api_key, str) or len(api_key) > 512):
            raise BridgeRequestError("API Key 格式无效")
        request_body = {"expected_revision": revision, "settings": dict(settings)}
        if api_key is not None:
            request_body["api_key"] = api_key
        return self._client.request("PUT", "/v1/developer/settings", json=request_body)

    # ------------------------------------------------------------------
    # Boundary helpers
    # ------------------------------------------------------------------
    def _service(self, name: str):
        if name in self._services:
            return self._services[name]
        if name in self._services_override:
            service = self._services_override[name]
        else:
            classes = {
                "auth": RemoteAuthService,
                "chat": RemoteChatService,
                "health": RemoteHealthService,
                "management": RemoteServiceManagementService,
                "commerce": RemoteCommerceService,
                "forum": RemoteExperienceForumService,
                "preferences": RemotePreferencesService,
                "ai": RemoteAiAssistantService,
                "appointments": RemoteAppointmentService,
                "files": RemoteFileManagementService,
            }
            try:
                service = classes[name](self._client)
            except KeyError:
                raise BridgeRequestError("服务未开放", "permission") from None
        self._services[name] = service
        return service

    def _ensure_client(self):
        if self._client is None:
            if not self._base_url:
                raise BridgeRequestError("云端地址未配置", "configuration")
            self._client = self._client_factory(self._base_url)
            self._bootstrapped = True

    def _require_session(self, action: str):
        if self._client is None or not self._client.is_authenticated or not self._user:
            raise CloudAPIError("not_authenticated")

    def _require_role(self, action: str):
        minimum = ACTION_ROLES.get(action, "member")
        actual = str((self._user or {}).get("role_code", ""))
        if _ROLE_ORDER.get(actual, -1) < _ROLE_ORDER[minimum]:
            raise CloudAPIError("permission")

    def _actor_id(self) -> int:
        identifier = (self._user or {}).get("id")
        if type(identifier) is not int or identifier <= 0:
            raise CloudAPIError("not_authenticated")
        return identifier

    def _user_view(self) -> dict[str, Any]:
        return dict(self._user or {})

    def _bootstrap_data(self):
        return {
            "app": {"name": APP_NAME, "version": APP_VERSION, "transport": "pywebview"},
            "cloud": {"configured": bool(self._base_url), "authenticated": bool(self._user), "endpoint": self._base_url},
            "user": self._user_view() if self._user else None,
        }

    def _reset_services(self):
        self._services.clear()

    @staticmethod
    def _validate_action(action: Any) -> str:
        if not isinstance(action, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{1,80}", action):
            raise BridgeRequestError("操作名称无效")
        return action

    @staticmethod
    def _validate_payload(
        payload: Mapping[str, Any] | None, *, max_bytes: int = _MAX_ACTION_BYTES
    ) -> dict[str, Any]:
        if payload is None:
            return {}
        if not isinstance(payload, Mapping):
            raise BridgeRequestError("参数格式无效")
        try:
            raw = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError):
            raise BridgeRequestError("参数包含不受支持的数据") from None
        if len(raw.encode("utf-8")) > max_bytes:
            raise BridgeRequestError("请求数据过大", "limit")
        return dict(payload)

    @staticmethod
    def _validate_text(value: Any, label: str, limit: int, *, required: bool = True) -> str:
        if value is None and not required:
            return ""
        if not isinstance(value, str) or (required and not value.strip()) or len(value) > limit:
            raise BridgeRequestError(f"{label}格式无效")
        return value.strip()

    @staticmethod
    def _take_id(body: Mapping[str, Any], name: str) -> int:
        value = body.get(name)
        if type(value) is not int or value <= 0:
            raise BridgeRequestError(f"{name}格式无效")
        return value

    @staticmethod
    def _optional_id(value: Any, name: str) -> int | None:
        if value is None:
            return None
        if type(value) is not int or value <= 0:
            raise BridgeRequestError(f"{name}格式无效")
        return value

    @staticmethod
    def _optional_text(value: Any, label: str, limit: int) -> str | None:
        if value is None:
            return None
        return WebBridgeAPI._validate_text(value, label, limit)

    @staticmethod
    def _limit(value: Any, *, maximum: int = 200) -> int:
        if type(value) is not int or not 1 <= value <= maximum:
            raise BridgeRequestError("数量上限格式无效")
        return value

    @staticmethod
    def _nonnegative_int(value: Any, label: str, maximum: int) -> int:
        if type(value) is not int or not 0 <= value <= maximum:
            raise BridgeRequestError(f"{label}格式无效")
        return value

    @staticmethod
    def _only(body: Mapping[str, Any], allowed: set[str]) -> dict[str, Any]:
        unknown = set(body) - allowed
        if unknown:
            raise BridgeRequestError("包含未开放的参数", "permission")
        return dict(body)

    @staticmethod
    def _developer_table(value: Any) -> str:
        # Mirrors the server's ownership-scoped TABLES list.  This is a fixed
        # identifier allowlist; it is never interpreted as SQL by the client.
        allowed = {
            "member_profiles", "profile_facts", "life_records", "reminders",
            "advisor_profiles", "advisor_bindings", "memberships", "visit_tasks",
            "visit_records", "user_files", "medical_reports", "report_items",
            "ai_insights", "advisor_summaries", "audit_logs", "product_recommendations",
            "orders", "conversations", "messages", "chat_attachments", "user_preferences",
            "member_cart", "member_favorites", "staff_account_terms", "visit_task_details",
        }
        if not isinstance(value, str) or value not in allowed:
            raise BridgeRequestError("记录表未开放", "permission")
        return value

    @staticmethod
    def _message_dict(message: Any) -> dict[str, Any]:
        if not isinstance(message, Mapping):
            raise BridgeRequestError("消息记录格式无效", "protocol")
        role, content = message.get("role"), message.get("content")
        if role not in {"system", "user", "assistant"} or not isinstance(content, str):
            raise BridgeRequestError("消息记录格式无效", "protocol")
        result: dict[str, Any] = {"role": role, "content": content}
        images = message.get("images", ())
        if images:
            if not isinstance(images, (list, tuple)) or any(
                not isinstance(item, (bytes, bytearray, str)) for item in images
            ):
                raise BridgeRequestError("图片附件格式无效", "protocol")
            result["images"] = list(images)
        return result

    @staticmethod
    def _failure(error: Exception) -> dict[str, Any]:
        if isinstance(error, CloudAPIError):
            return {"ok": False, "error": {"code": error.code, "message": str(error)}}
        if isinstance(error, BridgeRequestError):
            return {"ok": False, "error": {"code": error.code, "message": str(error)}}
        # Domain services expose safe Chinese messages.  Never include a
        # traceback, request body, URL, token, or provider key in the browser.
        if isinstance(error, (ValueError, LookupError, RuntimeError)) and str(error):
            return {"ok": False, "error": {"code": "request_failed", "message": str(error)[:300]}}
        return {"ok": False, "error": {"code": "request_failed", "message": "操作未完成，请稍后重试"}}


# Handlers are fixed at module load time; user input is never used as an
# attribute name or URL path.  Keep this table easy to audit when a new page is
# added to the HTML UI.
ACTION_HANDLERS: dict[str, Callable[[WebBridgeAPI, dict[str, Any]], Any]] = {
    "auth.me": WebBridgeAPI._auth_me,
    "auth.change-password": WebBridgeAPI._auth_change_password,
    "auth.reset-password": WebBridgeAPI._auth_reset_password,
    "health.dashboard": WebBridgeAPI._health_dashboard,
    "health.records.list": WebBridgeAPI._health_records,
    "health.records.add": WebBridgeAPI._health_add_record,
    "health.records.delete": WebBridgeAPI._health_delete_records,
    "health.statistics": WebBridgeAPI._health_statistics,
    "ai.statistics.explain": WebBridgeAPI._ai_statistics_explain,
    "health.service-summary": WebBridgeAPI._health_service_summary,
    "health.membership.activate": WebBridgeAPI._health_membership_activate,
    "health.profile.get": WebBridgeAPI._health_profile_get,
    "health.profile.save": WebBridgeAPI._health_profile_save,
    "health.reminders.list": WebBridgeAPI._health_reminders,
    "health.reminders.add": WebBridgeAPI._health_reminder_add,
    "health.reminders.update": WebBridgeAPI._health_reminder_update,
    "health.reminders.toggle": WebBridgeAPI._health_reminder_toggle,
    "health.reminders.delete": WebBridgeAPI._health_reminders_delete,
    "chat.conversations.list": WebBridgeAPI._chat_conversations,
    "chat.conversations.create": WebBridgeAPI._chat_create,
    "chat.conversations.rename": WebBridgeAPI._chat_rename,
    "chat.conversations.delete": WebBridgeAPI._chat_delete,
    "chat.messages.list": WebBridgeAPI._chat_messages,
    "chat.send": WebBridgeAPI._chat_send,
    "ai.member.draft": WebBridgeAPI._ai_member_draft,
    "ai.life-records.propose": WebBridgeAPI._ai_life_records_propose,
    "ai.drafts.list": WebBridgeAPI._ai_drafts_list,
    "ai.draft.get": WebBridgeAPI._ai_draft_get,
    "ai.proposal.confirm": WebBridgeAPI._ai_proposal_confirm,
    "ai.proposal.dismiss": WebBridgeAPI._ai_proposal_dismiss,
    "ai.advisor.summary": WebBridgeAPI._ai_advisor_summary,
    "voice.transcribe": WebBridgeAPI._voice_transcribe,
    "voice.smart-record": WebBridgeAPI._voice_smart_record,
    "voice.polish": WebBridgeAPI._voice_polish,
    "library.catalog": WebBridgeAPI._library_catalog,
    "library.search": WebBridgeAPI._library_search,
    "library.card": WebBridgeAPI._library_card,
    "library.source": WebBridgeAPI._library_source,
    "forum.topics": WebBridgeAPI._forum_topics,
    "forum.posts": WebBridgeAPI._forum_posts,
    "forum.posts.create": WebBridgeAPI._forum_create,
    "forum.posts.like": WebBridgeAPI._forum_like,
    "forum.posts.comment": WebBridgeAPI._forum_comment,
    "forum.posts.comments": WebBridgeAPI._forum_comments,
    "preferences.get": WebBridgeAPI._preferences_get,
    "preferences.update": WebBridgeAPI._preferences_update,
    "service.dashboard": WebBridgeAPI._service_dashboard,
    "service.members": WebBridgeAPI._service_members,
    "service.advisors": WebBridgeAPI._service_advisors,
    "service.appointments": WebBridgeAPI._service_appointments,
    "service.appointments.request": WebBridgeAPI._service_request_appointment,
    "service.appointments.update": WebBridgeAPI._service_update_appointment,
    "service.history": WebBridgeAPI._service_history,
    "service.member.overview": WebBridgeAPI._service_member_overview,
    "service.tasks": WebBridgeAPI._service_tasks,
    "service.task.create": WebBridgeAPI._service_task_create,
    "service.task.start": WebBridgeAPI._service_task_start,
    "service.task.complete": WebBridgeAPI._service_task_complete,
    "service.advisors.statistics": WebBridgeAPI._service_advisor_statistics,
    "service.work.statistics": WebBridgeAPI._service_work_statistics,
    "service.advisor.create": WebBridgeAPI._service_advisor_create,
    "service.advisor.profile.save": WebBridgeAPI._service_advisor_profile,
    "service.advisor.validity": WebBridgeAPI._service_advisor_validity,
    "service.account.enabled": WebBridgeAPI._service_account_enabled,
    "service.account.reset-password": WebBridgeAPI._service_reset_password,
    "shopping.products": WebBridgeAPI._shopping_products,
    "shopping.cart": WebBridgeAPI._shopping_cart,
    "shopping.cart.add": WebBridgeAPI._shopping_cart_add,
    "shopping.cart.set": WebBridgeAPI._shopping_cart_set,
    "shopping.favorites": WebBridgeAPI._shopping_favorites,
    "shopping.favorite.set": WebBridgeAPI._shopping_favorite,
    "shopping.checkout": WebBridgeAPI._shopping_checkout,
    "shopping.orders": WebBridgeAPI._shopping_orders,
    "shopping.recommendations": WebBridgeAPI._shopping_recommendations,
    "shopping.recommendation.view": WebBridgeAPI._shopping_recommendation_view,
    "shopping.recommendation.interested": WebBridgeAPI._shopping_recommendation_interest,
    "shopping.product.interested": WebBridgeAPI._shopping_product_interest,
    "shopping.product.create": WebBridgeAPI._shopping_product_create,
    "shopping.product.update": WebBridgeAPI._shopping_product_update,
    "shopping.product.active": WebBridgeAPI._shopping_product_active,
    "shopping.product.delete": WebBridgeAPI._shopping_product_delete,
    "shopping.product.recommend": WebBridgeAPI._shopping_product_recommend,
    "files.upload": WebBridgeAPI._files_upload,
    "files.list": WebBridgeAPI._files_list,
    "files.get": WebBridgeAPI._files_get,
    "files.delete": WebBridgeAPI._files_delete,
    "files.storage": WebBridgeAPI._files_storage,
    "files.reports.list": WebBridgeAPI._files_reports_list,
    "files.report.get": WebBridgeAPI._files_report_get,
    "files.report.create": WebBridgeAPI._files_report_create,
    "files.report.delete": WebBridgeAPI._files_report_delete,
    "files.report.archive": WebBridgeAPI._files_report_archive,
    "files.report.confirm": WebBridgeAPI._files_report_confirm,
    "files.report.item.add": WebBridgeAPI._files_report_item_add,
    "files.report.item.confirm": WebBridgeAPI._files_report_item_confirm,
    "ai.config": WebBridgeAPI._ai_config,
    "ai.context": WebBridgeAPI._ai_context,
    "ai.config.update": WebBridgeAPI._ai_config_update,
    "network.probe": WebBridgeAPI._network_probe,
    "weather.current": WebBridgeAPI._weather_current,
    "developer.users": WebBridgeAPI._developer_users,
    "developer.user": WebBridgeAPI._developer_user,
    "developer.settings": WebBridgeAPI._developer_settings,
    "developer.status": WebBridgeAPI._developer_status,
    "developer.client-settings": WebBridgeAPI._developer_client_settings,
    "developer.client-start": WebBridgeAPI._developer_client_start,
    "developer.user.patch": WebBridgeAPI._developer_user_patch,
    "developer.user.records": WebBridgeAPI._developer_user_records,
    "developer.user.record.patch": WebBridgeAPI._developer_user_record_patch,
    "developer.settings.publish": WebBridgeAPI._developer_settings_publish,
}


ACTION_ROLES: dict[str, str] = {
    **{name: "member" for name in ACTION_HANDLERS},
    "ai.config.update": "operator",
    "ai.advisor.summary": "advisor",
    "service.dashboard": "operator",
    "service.members": "operator",
    "service.advisors": "operator",
    "service.task.create": "advisor",
    "service.task.start": "advisor",
    "service.task.complete": "advisor",
    "service.member.overview": "advisor",
    "service.advisors.statistics": "operator",
    "service.work.statistics": "operator",
    "service.advisor.create": "operator",
    "service.advisor.profile.save": "operator",
    "service.advisor.validity": "operator",
    "service.account.enabled": "operator",
    "service.account.reset-password": "operator",
    "shopping.product.create": "operator",
    "shopping.product.update": "operator",
    "shopping.product.active": "operator",
    "shopping.product.delete": "operator",
    "shopping.product.recommend": "advisor",
    "developer.users": "operator",
    "developer.user": "operator",
    "developer.settings": "operator",
    "developer.status": "operator",
    "developer.client-settings": "operator",
    "developer.client-start": "operator",
    "developer.user.patch": "operator",
    "developer.user.records": "operator",
    "developer.user.record.patch": "operator",
    "developer.settings.publish": "operator",
}


__all__ = ["ACTION_HANDLERS", "ACTION_ROLES", "BridgeRequestError", "WebBridgeAPI"]
