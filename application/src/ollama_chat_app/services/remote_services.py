"""Drop-in synchronous service facades for explicitly selected cloud mode.

These facades do not construct or open a local Database. Existing public method
signatures are validated against a static whitelist of local service methods,
but their local implementations are NEVER invoked. Caller-supplied user/actor
IDs preserve UI compatibility; the server MUST bind those to the authenticated
identity rather than trusting them. Target member IDs still require server
authorization. All methods that use the network belong in a UI worker.
"""

from __future__ import annotations

import inspect
from datetime import datetime
from functools import wraps

from ..data.database import User
from .ai_assistant import AiAssistantService
from .auth import AuthService
from .chat import ChatService
from .cloud_client import CloudAPIClient, CloudAPIError
from .cloud_rpc_codec import CloudCodecError, decode_rpc, encode_rpc
from .commerce import CommerceService
from .file_management import FileManagementService
from .health import HealthService
from .service_management import ServiceManagementService

SERVICE_METHODS = {
    "auth": (AuthService, frozenset({"create_staff", "get_user"})),
    "chat": (ChatService, frozenset({
        "get_default_conversation", "get_conversation", "list_conversations",
        "create_conversation", "rename_conversation", "list_messages", "get_message",
        "get_context", "context_for_provider", "list_message_attachments",
        "context_attachment_kinds", "begin_message", "complete_message", "fail_message",
    })),
    "health": (HealthService, frozenset({
        "get_dashboard", "list_life_records", "add_life_record", "get_record_statistics",
        "delete_life_record", "get_profile", "save_profile", "list_reminders",
        "add_reminder", "update_reminder", "pause_reminder", "resume_reminder",
        "toggle_reminder", "delete_reminder", "get_service_summary",
    })),
    "service_management": (ServiceManagementService, frozenset({
        "get_dashboard", "create_advisor", "list_members", "get_member_overview",
        "list_advisors", "get_advisor_work_statistics", "save_advisor_profile",
        "save_membership", "bind_advisor", "create_visit_task", "start_visit_task",
        "complete_visit_task", "list_visit_tasks", "list_visit_records", "set_account_enabled",
    })),
    "commerce": (CommerceService, frozenset({
        "list_products", "create_product", "update_product", "set_product_active",
        "delete_product", "recommend_product", "list_recommendations", "record_product_view",
        "mark_interested", "mark_product_interested", "simulate_purchase", "reorder",
        "list_orders", "mark_order_delivered", "create_order",
        "mark_recommendation_interested", "reorder_order",
    })),
    "files": (FileManagementService, frozenset({
        "upload_bytes", "get_file", "list_files", "delete_file", "storage_usage",
        "add_report_item_draft", "archive_report", "confirm_report", "confirm_report_item",
        "create_report_draft", "delete_report", "get_report", "list_reports",
    })),
    "ai": (AiAssistantService, frozenset({
        "build_member_context", "confirm_proposal", "dismiss_proposal", "get_ai_config",
        "get_draft", "list_drafts", "update_ai_config",
    })),
}


def user_from_cloud(view: dict) -> User:
    """Decode the plain JSON safe-view used only by the authentication endpoints."""
    try:
        required = {"id", "username", "username_normalized", "created_at", "last_login_at",
                    "role_code", "account_status", "updated_at"}
        if type(view) is not dict or set(view) != required:
            raise ValueError
        if type(view["id"]) is not int or view["id"] <= 0:
            raise ValueError
        if any(type(view[key]) is not str for key in ("username", "username_normalized")):
            raise ValueError
        if view["role_code"] not in {"member", "advisor", "operator"}:
            raise ValueError
        if view["account_status"] not in {"active", "disabled"}:
            raise ValueError
        values = dict(view)
        for key in ("created_at", "last_login_at", "updated_at"):
            value = values[key]
            if value is None and key != "created_at":
                continue
            if type(value) is not str or len(value) > 64:
                raise ValueError
            timestamp = datetime.fromisoformat(value)
            if timestamp.tzinfo is None:
                raise ValueError
            values[key] = timestamp
        return User(**values)
    except Exception:
        raise CloudAPIError("protocol") from None


class _RemoteService:
    uses_network = True
    service_name: str

    def __init__(self, client: CloudAPIClient) -> None:
        self.client = client

    def call(self, method: str, *args, request_id: str | None = None, **kwargs):
        """Optional stable request_id permits explicit retry of the SAME operation.

        No retry is automatic. Store the ID with the in-flight UI operation if
        a user may retry after an uncertain network outcome. Do not generate a
        fresh ID to retry a payment-like or other non-repeatable mutation.
        """
        local_class, allowed = SERVICE_METHODS[self.service_name]
        if method not in allowed:
            raise CloudAPIError("permission")
        # Lookup is bounded by the explicit table above, never caller-supplied types.
        signature = inspect.signature(local_class.__dict__[method])
        try:
            signature.bind(None, *args, **kwargs)
            encoded_args, encoded_kwargs = encode_rpc(list(args)), encode_rpc(kwargs)
        except (TypeError, CloudCodecError):
            raise CloudAPIError("validation") from None
        result = self.client.rpc(self.service_name, method, encoded_args, encoded_kwargs,
                                 request_id=request_id)
        try:
            return decode_rpc(result)
        except CloudCodecError:
            raise CloudAPIError("protocol", outcome_uncertain=True) from None

    def __getattr__(self, method: str):
        local_class, allowed = SERVICE_METHODS[self.service_name]
        if method not in allowed:
            raise AttributeError("该云端服务方法不存在")
        descriptor = local_class.__dict__[method]

        @wraps(descriptor)
        def remote(*args, **kwargs):
            return self.call(method, *args, **kwargs)

        remote.__signature__ = inspect.signature(descriptor).replace(
            parameters=list(inspect.signature(descriptor).parameters.values())[1:]
        )
        return remote


class RemoteAuthService(_RemoteService):
    service_name = "auth"

    def register(self, username: str, password: str) -> User:
        return user_from_cloud(self.client.register(username, password))

    def authenticate(self, username: str, password: str) -> User:
        try:
            return user_from_cloud(self.client.login(username, password))
        except Exception:
            self.client.clear_session()
            raise

    def login(self, username: str, password: str) -> User:
        return self.authenticate(username, password)

    def me(self) -> User:
        return user_from_cloud(self.client.me())

    def has_operator(self) -> bool:
        # Never ask the server during login-page construction; cloud bootstrap
        # is administrator-only and is deliberately unavailable in public apps.
        return True

    def bootstrap_operator(self, username: str, password: str):
        raise CloudAPIError("permission")

    def change_password(self, current_password: str, new_password: str):
        return self.client.change_password(current_password, new_password)

    def logout(self) -> None:
        self.client.logout()

    def clear_session(self) -> None:
        self.client.clear_session()


class RemoteChatService(_RemoteService):
    service_name = "chat"


class RemoteHealthService(_RemoteService):
    service_name = "health"


class RemoteServiceManagementService(_RemoteService):
    service_name = "service_management"


class RemoteCommerceService(_RemoteService):
    service_name = "commerce"


class RemoteFileManagementService(_RemoteService):
    """Remote byte storage plus the unchanged local path/OCR/PDF operations."""

    service_name = "files"
    upload_file = FileManagementService.upload_file
    export_file = FileManagementService.export_file
    extract_pdf_text = FileManagementService.extract_pdf_text
    extract_text = FileManagementService.extract_text
    _run_ocr = FileManagementService._run_ocr

    def __init__(self, client: CloudAPIClient, *, ocr_engine=None) -> None:
        super().__init__(client)
        self._ocr_engine = ocr_engine


class RemoteAiAssistantService(_RemoteService):
    """Fetch authorized context, call the user's provider locally, then store a draft.

    Provider objects, API keys and image bytes NEVER enter our cloud RPC payload.
    Images go only to the user-selected AI provider after existing UI consent.
    The backend validates the signed preparation ticket and all proposals again.
    """

    service_name = "ai"

    def _stage(self, method, *args, **kwargs):
        allowed = {"prepare_member_draft", "finish_member_draft", "prepare_advisor_summary",
                   "finish_advisor_summary"}
        if method not in allowed:
            raise CloudAPIError("permission")
        return decode_rpc(self.client.rpc("ai", method, encode_rpc(list(args)), encode_rpc(kwargs)))

    @staticmethod
    def _prepared(value):
        if not isinstance(value, dict) or set(value) != {"ticket", "model", "messages"}:
            raise CloudAPIError("protocol")
        if (not isinstance(value["ticket"], str) or not isinstance(value["model"], str)
                or not isinstance(value["messages"], list) or not value["messages"]):
            raise CloudAPIError("protocol")
        return value

    def create_member_draft(self, actor_user_id, provider, provider_name, model, prompt, *,
                            member_user_id=None, images=None, context_days=None):
        from ..providers.base import ProviderError
        from ..workers.cloud_bridge import on_gui_thread, run_cloud_operation

        if on_gui_thread():
            return run_cloud_operation(self.create_member_draft, actor_user_id, provider,
                provider_name, model, prompt, member_user_id=member_user_id,
                images=images, context_days=context_days)
        image_list = AiAssistantService._normalise_images(images)
        if image_list and not provider.supports_images:
            raise ProviderError("当前 AI 模型不支持图片输入。", code="images_not_supported")
        prepared = self._prepared(self._stage("prepare_member_draft", actor_user_id,
            provider_name, model, prompt, member_user_id=member_user_id,
            had_images=bool(image_list), context_days=context_days))
        messages = [dict(message) for message in prepared["messages"]]
        if image_list:
            messages[-1]["images"] = image_list
        raw_response = provider.chat(prepared["model"], messages)
        return self._stage("finish_member_draft", actor_user_id, prepared["ticket"], raw_response)

    def create_advisor_summary_draft(self, advisor_user_id, member_user_id, provider,
                                     provider_name, model, *, context_days=None):
        from ..workers.cloud_bridge import on_gui_thread, run_cloud_operation

        if on_gui_thread():
            return run_cloud_operation(self.create_advisor_summary_draft, advisor_user_id,
                member_user_id, provider, provider_name, model, context_days=context_days)
        prepared = self._prepared(self._stage("prepare_advisor_summary", advisor_user_id,
            member_user_id, provider_name, model, context_days=context_days))
        raw_response = provider.chat(prepared["model"], prepared["messages"])
        return self._stage("finish_advisor_summary", advisor_user_id,
                           prepared["ticket"], raw_response)
