"""Full-platform HTTPS API; no desktop GUI, OCR, voice model or cloud AI key required."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import sqlite3
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.chat import ChatService
from ollama_chat_app.services.commerce import CommerceService
from ollama_chat_app.data.database import timestamp_to_db, utc_now
from ollama_chat_app.services.health import (
    HealthService, HealthValidationError, LIFE_RECORD_SOURCES, MAX_CONTENT_LENGTH,
)
from ollama_chat_app.services.service_management import ServiceManagementService

from .app import RequestBoundary
from .platform_ai import PlatformAIService
from .platform_config import RAILWAY_HEALTHCHECK_HOST, PlatformSettings
from .platform_rpc import RpcDispatcher, RpcError
from .platform_uploads import PlatformFileService, validate_chat_uploads, validate_image
from expansion.common import ExpansionError
from expansion.service import ExpansionService
from .member_state import MemberStateService, create_member_router
from .member_voice import ASR_PATH, MAX_ASR_JSON_BYTES, QwenASRGateway, create_voice_router


class PlatformChatService(ChatService):
    """Keep a bounded recent multimodal context for the 32 MiB transport envelope."""

    def begin_message(
        self, user_id, content, provider=None, model=None, *, conversation_id=None, attachments=(),
        welcome_metadata=False,
    ):
        validate_chat_uploads(attachments)
        return super().begin_message(
            user_id,
            content,
            provider=provider,
            model=model,
            conversation_id=conversation_id,
            attachments=attachments,
            welcome_metadata=welcome_metadata,
        )

    def get_context(self, user_id, *, limit=20, conversation_id=None):
        turns = super().get_context(user_id, limit=limit, conversation_id=conversation_id)
        total, selected = 0, []
        for turn in reversed(turns):
            size = sum(len(item.content) for item in turn.attachments)
            if total + size > 20 * 1024 * 1024:
                # Retain the message text without exceeding the binary safety budget.
                turn = replace(turn, attachments=())
            else:
                total += size
            selected.append(turn)
        return list(reversed(selected))


class PlatformHealthService(HealthService):
    """Save record photos with the cloud decoder, without desktop Qt imports."""

    @staticmethod
    def _validate_owned_photos(connection, actor, details):
        from ollama_chat_app.services.record_photos11839 import (
            MAX_PHOTO_BYTES, PHOTO_TYPES, photo_ids,
        )
        if not details or 'photo_file_ids' not in details:
            return
        for identifier in photo_ids(details['photo_file_ids']):
            row = connection.execute(
                'SELECT f.media_type,f.size_bytes,f.sha256,c.content,c.mime_type,'
                'c.sha256 AS content_sha256 FROM user_files f JOIN user_file_contents c '
                'ON c.file_id=f.id WHERE f.id=? AND f.user_id=?',
                (identifier, actor),
            ).fetchone()
            if row is None or row['media_type'] not in PHOTO_TYPES:
                raise ValueError('照片不存在或不属于当前账号，请重新选择。')
            data = bytes(row['content'])
            if (not 0 < len(data) <= MAX_PHOTO_BYTES or len(data) != row['size_bytes']
                    or row['mime_type'] != row['media_type'] or row['content_sha256'] != row['sha256']
                    or not hmac.compare_digest(hashlib.sha256(data).hexdigest(), str(row['sha256']))):
                raise ValueError('照片校验未通过，请重新选择不超过2 MB的照片。')
            validate_image(data, str(row['media_type']))

    def add_life_record(
        self,
        user_id: int,
        category: str,
        occurred_at: datetime | str,
        content: str,
        source: str = "manual",
        details: Mapping[str, Any] | None = None,
    ) -> int:
        # Keep the shared service's normalization, one transaction and audit
        # contract; only the image decoder differs on the server platform.
        user_id = self._require_user(user_id)
        category_value = self._validate_category(category)
        occurred_at_utc, local_date, offset_minutes = self._normalize_local_timestamp(occurred_at)
        content_value = self._clean_required_text(content, "记录内容", maximum=MAX_CONTENT_LENGTH)
        source_value = self._clean_required_text(source, "记录来源", maximum=50)
        if source_value not in LIFE_RECORD_SOURCES:
            raise HealthValidationError("记录来源无效；AI 内容必须经用户确认后再保存")
        normalized_details = self._validate_record_details(category_value, details)
        details_json = self._mapping_to_json(normalized_details, "记录详情")
        now = timestamp_to_db(utc_now())
        with self.database.transaction() as connection:
            try:
                self._validate_owned_photos(connection, user_id, normalized_details)
            except ValueError as error:
                raise HealthValidationError(str(error)) from None
            cursor = connection.execute(
                """
                INSERT INTO life_records (
                    user_id, category, occurred_at, local_date,
                    timezone_offset_minutes, content, details_json, source,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    category_value,
                    occurred_at_utc,
                    local_date,
                    offset_minutes,
                    content_value,
                    details_json,
                    source_value,
                    now,
                    now,
                ),
            )
            record_id = int(cursor.lastrowid)
            self._add_audit_log(
                connection, user_id, "life_record.created", "life_record", record_id
            )
        return record_id


class PlatformExpansionService(ExpansionService):
    """Keep record and linked-meal edits on the same cloud photo decoder."""

    def _update_record(self, c, actor, current, changes, reason):
        from expansion.common import body, text, dumps, now
        body(changes, ("category", "occurred_at", "content", "details"))
        merged = {**current, **changes}
        category = self.health._validate_category(merged["category"])
        occurred, local, offset = self.health._normalize_local_timestamp(merged["occurred_at"])
        content = text(merged["content"], 10000)
        details = self.health._validate_record_details(category, merged["details"])
        self.health._validate_owned_photos(c, actor, details)
        c.execute("UPDATE life_records SET category=?,occurred_at=?,local_date=?,timezone_offset_minutes=?,content=?,details_json=?,updated_at=? WHERE id=? AND user_id=?", (category, occurred, local, offset, content, None if details is None else dumps(details), now(), current["id"], actor))
        after = self._record(actor, current["id"])
        self._revision(c, actor, "life_record", current["id"], "update", current, after, reason)
        return after


class PlatformBoundary:
    """Bound requests and honor only the explicitly configured deployment ingress.

    Railway edge-only mode relies on verified deployment entrance isolation,
    not on source CIDRs or environment variables authenticating a request.
    It restores only the HTTPS scheme and NEVER rewrites a client's socket IP.
    """

    def __init__(self, app, *, settings, auth=None, guest_store=None):
        self.app, self.settings = app, settings
        self.auth = auth
        self.guest_store = guest_store
        self._large = asyncio.Semaphore(4)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        raw_headers = [(name.lower(), value) for name, value in scope.get("headers", [])]
        headers = dict(raw_headers)
        sensitive_headers = {
            b"host",
            b"authorization",
            b"content-length",
            b"x-forwarded-proto",
            b"x-real-ip",
            b"cf-connecting-ip",
        }
        seen = set()
        for name, _value in raw_headers:
            if name in sensitive_headers:
                if name in seen:
                    return await JSONResponse({"detail": "Ambiguous request headers"}, 400)(
                        scope, receive, send
                    )
                seen.add(name)
        railway_mode = bool(
            getattr(self.settings, "railway_proxy", False)
            and getattr(self.settings, "railway_edge_only", False)
            and not self.settings.render_proxy
        )
        health_request = scope.get("method") == "GET" and scope["path"] in {
            "/health/live",
            "/health/ready",
        }
        try:
            raw_host = headers.get(b"host", b"").decode("ascii")
            if not raw_host or any(ord(char) < 33 or ord(char) > 126 for char in raw_host):
                raise ValueError
            hostname, *port = raw_host.lower().split(":")
            if port and (len(port) != 1 or not port[0].isdigit() or not 1 <= int(port[0]) <= 65535):
                raise ValueError
        except (ValueError, UnicodeError):
            hostname = ""
        if hostname == RAILWAY_HEALTHCHECK_HOST:
            if not railway_mode or not health_request:
                return await JSONResponse({"detail": "Host not permitted"}, 400)(
                    scope, receive, send
                )
        elif railway_mode and hostname not in self.settings.allowed_hosts:
            return await JSONResponse({"detail": "Host not permitted"}, 400)(scope, receive, send)
        if railway_mode and headers.get(b"x-forwarded-proto") == b"https":
            # Enabling both flags is the operator's explicit confirmation that
            # this service has no public TCP/direct ingress and the project
            # environment contains only trusted services. No CIDR is invented.
            scope = dict(scope)
            scope["scheme"] = "https"
            # X-Real-IP, XFF, CF-Connecting-IP, and Forwarded remain unused.
        if self.settings.render_proxy:
            scope = dict(scope)
            peer = (scope.get("client") or ("", 0))[0]
            try:
                peer_address = ipaddress.ip_address(peer)
                trusted = any(
                    peer_address in network
                    for network in (
                        ipaddress.ip_network("10.0.0.0/8"),
                        ipaddress.ip_network("172.16.0.0/12"),
                        ipaddress.ip_network("192.168.0.0/16"),
                        ipaddress.ip_network("127.0.0.0/8"),
                        ipaddress.ip_network("fc00::/7"),
                        ipaddress.ip_network("::1/128"),
                    )
                )
                if trusted and headers.get(b"x-forwarded-proto") == b"https":
                    scope["scheme"] = "https"
                    # Render terminates TLS at its edge. Restore scheme separately
                    # from client-IP metadata; a missing optional CF header must
                    # not make all legitimate HTTPS requests fail. Do not guess
                    # the first XFF entry, which may be supplied by a caller.
                    raw_client = headers.get(b"cf-connecting-ip", b"")
                    if raw_client:
                        client = ipaddress.ip_address(raw_client.decode("ascii"))
                        scope["client"] = (str(client), 0)
            except (ValueError, UnicodeError):
                pass
        guest_large = scope["path"] in {"/v1/guest/request", "/v1/guest/chat/stream", "/v1/guest/voice/transcribe"}
        large = guest_large or scope["path"] in {
            "/v1/rpc/files/upload_bytes",
            "/v1/rpc/chat/begin_message",
            "/v1/rpc/chat/complete_message",
            "/v1/ai/chat",
            "/v1/ai/chat/stream",
            ASR_PATH,
        }
        if self.settings.require_https and scope["scheme"] != "https" and not health_request:
            return await JSONResponse({"detail": "HTTPS required"}, 426)(scope, receive, send)
        if large:
            authorization = headers.get(b"authorization", b"")
            pattern = rb"Guest gs1_[A-Za-z0-9_-]{43}" if guest_large else rb"Bearer [A-Za-z0-9_-]{43}"
            if not re.fullmatch(pattern, authorization) or (self.guest_store is None if guest_large else self.auth is None):
                return await JSONResponse({"detail": "Authentication required"}, 401)(
                    scope, receive, send
                )
            try:
                await asyncio.wait_for(self._large.acquire(), timeout=30)
            except TimeoutError:
                return await JSONResponse({"detail": "Upload capacity limited"}, 429)(
                    scope, receive, send
                )
        try:
            if large:
                # Bound both authentication work and possible 32 MiB buffering.
                # Validate again in the route after upload in case state changed.
                try:
                    if guest_large:
                        await asyncio.to_thread(self.guest_store.active, authorization[6:].decode("ascii"))
                    else:
                        await asyncio.to_thread(self.auth.resolve_token, authorization[7:].decode("ascii"))
                except Exception as error:
                    from .platform_auth import PlatformAuthError

                    from .guest_ai_store import GuestError
                    status = error.status_code if isinstance(error, PlatformAuthError) else error.status if isinstance(error,GuestError) else 503
                    return await JSONResponse(
                        {"detail": "Authentication unavailable or required"}, status
                    )(scope, receive, send)
            limit = (
                32 * 1024 * 1024
                if large
                else (
                    64 * 1024
                    if scope["path"].startswith("/v1/auth/")
                    else self.settings.max_body_bytes
                )
            )
            if scope["path"] in {ASR_PATH, "/v1/guest/voice/transcribe"}:
                limit = min(limit, MAX_ASR_JSON_BYTES)
            if scope["path"].startswith("/v1/rpc/sync/"):
                limit = min(limit, 1024 * 1024)
            if scope["path"].startswith("/v1/ai/"):
                from .platform_ai_proxy import MAX_BODY as MAX_AI_BODY
                limit = min(limit, MAX_AI_BODY)
            boundary = RequestBoundary(
                self.app,
                settings=SimpleNamespace(
                    require_https=self.settings.require_https,
                    max_body_bytes=limit,
                    body_timeout_seconds=self.settings.body_timeout_seconds,
                ),
            )
            await boundary(scope, receive, send)
        finally:
            if large:
                self._large.release()


def create_app(settings=None, *, database=None, ai_proxy_settings=None, ai_transport=None):
    from .platform_auth import PlatformAuth, PlatformAuthError, create_auth_router
    from .platform_database import PlatformDatabase

    settings = settings or PlatformSettings()
    database = database or PlatformDatabase(settings.database_url.get_secret_value())
    auth = PlatformAuth(database, settings)
    database.initialize()
    from .phone_auth import PhoneAuth, create_phone_auth_router
    phone_auth = PhoneAuth(auth)
    from .profile_try1_schema import verify_shape as verify_try1_shape
    verify_try1_shape(database)
    if getattr(database,'dialect','sqlite')=='postgresql':
        from .multimodal_schema11842 import verify_shape as verify_multimodal_shape
        verify_multimodal_shape(database)
        from .conversation_schema11844 import verify_shape as verify_conversation_shape
        verify_conversation_shape(database)
    from .account_metadata11841 import AccountMetadataService,create_account_metadata_router
    account_metadata=AccountMetadataService(auth,database)
    auth.account_login_recorder=account_metadata.record_login
    health = PlatformHealthService(database)
    from ollama_chat_app.services.preferences import PreferencesService
    from ollama_chat_app.services.experience_forum import ExperienceForumService

    services = {
        "auth": AuthService(database),
        "chat": PlatformChatService(database),
        "health": health,
        "preferences": PreferencesService(database),
        "service_management": ServiceManagementService(database),
        "commerce": CommerceService(database),
        "files": PlatformFileService(database),
        "ai": PlatformAIService(database, health, settings.token_pepper.get_secret_value()),
        "forum": ExperienceForumService(database),
    }
    services["expansion"] = PlatformExpansionService(database,health=health,files=services["files"])
    from .platform_sync import PlatformSyncService

    services["sync"] = PlatformSyncService(
        database, services, settings.token_pepper.get_secret_value()
    )
    dispatcher = RpcDispatcher(database, services)

    @asynccontextmanager
    async def lifespan(_application):
        if settings.production:
            # Refuse public startup unless shared limits and database security really exist.
            auth.check_ready()
            if not application.state.profile_vectors.embedder.ready():
                raise RuntimeError('Qwen semantic API configuration is not ready')
        yield
        application.state.memory_projection.close()
        await application.state.memory_summarizer.close()
        await application.state.category_summarizer.close()
        await application.state.conversation_memory.close()
        if hasattr(database, "dispose"):
            database.dispose()

    from ollama_chat_app.config import APP_VERSION

    application = FastAPI(
        title="健康生活服务平台 API",
        version=APP_VERSION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.state.database = database
    application.state.platform_auth = auth
    application.state.phone_auth = phone_auth
    application.state.account_metadata=account_metadata
    application.state.settings = settings
    application.state.services = services
    from ollama_chat_app.services.knowledge_library import KnowledgeLibrary

    application.state.library = KnowledgeLibrary(getattr(settings, "library_root", None))
    application.state.member_state = MemberStateService(
        database, services, application.state.library
    )
    application.include_router(create_auth_router(auth))
    application.include_router(create_phone_auth_router(phone_auth))
    application.include_router(create_account_metadata_router(account_metadata))
    application.include_router(create_member_router(auth, application.state.member_state))
    application.state.voice_gateway = QwenASRGateway()
    application.include_router(create_voice_router(auth, gateway=application.state.voice_gateway))
    from .member_forum_images import create_forum_image_router
    application.include_router(create_forum_image_router(auth, database))
    from .member_public_profile import create_public_profile_router
    application.include_router(create_public_profile_router(auth, database))
    from .member_forum_discovery import create_discovery_router
    application.include_router(create_discovery_router(auth, database))
    from .member_forum_detail import create_detail_router
    application.include_router(create_detail_router(auth, database))
    from .library_api import create_library_router

    application.include_router(create_library_router(auth, application.state.library))
    from pathlib import Path
    @application.api_route('/v1/public/privacy/harmony-invite', methods=['GET', 'HEAD'], include_in_schema=False)
    def harmony_invite_privacy():
        document = Path(__file__).resolve().parent / 'public_documents' / 'fawnsee-privacy.html'
        return FileResponse(document, media_type='text/html', headers={'X-Content-Type-Options': 'nosniff'})

    from .reading_service import create_reading_router
    application.include_router(create_reading_router(auth, Path("/app/reading-presentation")))
    from .reading_service import ReadingPresentation
    from .reading_social import ReadingSocial, create_reading_social_router
    services["reading_social"] = ReadingSocial(database, ReadingPresentation(Path("/app/reading-presentation")))
    application.include_router(create_reading_social_router(auth, services["reading_social"]))
    from .platform_ai_proxy import PlatformAIProxy, create_ai_router

    application.state.ai_proxy = PlatformAIProxy(
        auth, settings=ai_proxy_settings, transport=ai_transport
    )
    from .platform_developer import DeveloperService, create_developer_router

    application.state.developer_service = DeveloperService(
        auth,
        key_path=application.state.ai_proxy.settings.key_path,
        legacy_key_configured=bool(
            application.state.ai_proxy.settings.envelope_path
            and application.state.ai_proxy.settings.key_path
        ),
    )
    application.state.ai_proxy.developer_service = application.state.developer_service
    application.include_router(create_developer_router(application.state.developer_service))
    application.include_router(create_ai_router(application.state.ai_proxy))
    from .member_onboarding11830 import OnboardingService, create_onboarding_router
    application.state.onboarding11830 = OnboardingService(auth, database, health, application.state.ai_proxy)
    application.include_router(create_onboarding_router(application.state.onboarding11830))
    from .member_memory import MemberMemoryService, create_memory_router
    from .member_memory_core import MemberMemoryCore
    from .member_memory_summary import MemoryPreferenceSummarizer
    from .member_memory_projection import DailyProjection, MUTATION_DOMAINS
    memory_content = {}
    for item in services["expansion"]._catalog()[0]:
        metadata = {key:item[key] for key in ("title","category","tags") if key in item}
        for identifier in (item.get("id"),item.get("document_id")):
            if isinstance(identifier,str):
                memory_content[identifier] = metadata
    application.state.member_memory = MemberMemoryService(database, services["preferences"],
        core=MemberMemoryCore(database, content_metadata=memory_content))
    from .profile_vectors11841 import ProfileVectorService
    application.state.profile_vectors=ProfileVectorService(database)
    application.state.member_memory.core.profile_introduction_retriever=application.state.profile_vectors.retrieve
    application.state.member_memory.profile_vector_invalidator=application.state.profile_vectors.invalidate
    from .member_multimodal11842 import MemberMultimodalRetrieval
    application.state.member_multimodal=MemberMultimodalRetrieval(
        database,core=application.state.member_memory.core,
        settings_reader=application.state.member_memory.settings)
    application.state.member_memory.core.multimodal_retriever=application.state.member_multimodal.retrieve
    application.state.member_memory.core.multimodal_source_invalidator=application.state.member_multimodal.invalidate_sources
    application.state.member_memory.multimodal_invalidator=application.state.member_multimodal.invalidate
    application.state.member_memory.multimodal_mutation_invalidator=application.state.member_multimodal.invalidate_target_mutation
    application.state.member_memory.multimodal_lock_owner=application.state.member_multimodal.lock_owner
    application.state.member_memory.multimodal_validator=application.state.member_multimodal.validate_proof
    application.state.ai_proxy.multimodal_retrieval=application.state.member_multimodal
    from .conversation_memory11844 import ConversationMemory11844
    application.state.conversation_memory = ConversationMemory11844(
        auth, application.state.member_memory, application.state.ai_proxy)
    application.state.conversation_memory.warm_index = application.state.member_multimodal.index_original_messages
    from .conversation_progress11846 import ConversationProgress11846
    application.state.conversation_history = ConversationProgress11846(
        application.state.conversation_memory, application.state.member_multimodal)
    application.state.conversation_memory.history_progress = application.state.conversation_history
    application.state.conversation_memory.warm_index = application.state.conversation_history.warm_selected
    application.state.ai_proxy.conversation_memory = application.state.conversation_memory
    application.state.member_memory.conversation_invalidator = application.state.conversation_memory.invalidate
    # The journal and stream validator accept older Qwen proofs unchanged and
    # revalidate the new conversation sources at the same boundaries.
    application.state.member_memory.multimodal_validator = application.state.ai_proxy.validate_personal_proof

    def invalidate_multimodal_mutation(connection,actor,service,method,role,arguments=None):
        if role!='member':return
        if arguments is not None and application.state.member_multimodal.invalidate_target_mutation(
                actor,service,method,arguments,connection):return
        # New facts are indexed lazily without throwing away prior vectors.
        # Edits/deletions remove stale derived excerpts before source commit.
        if service!='sync'and not method.startswith(('delete_','update_','save_','reset_','clear_','remove_',
                                  'close_','uncheckin_','unlike_','unfollow_','toggle_',
                                  'archive_','confirm_')):
            return
        if service=='preferences':domains=None
        elif service=='files':domains=('life_records','ai_records','forum')
        else:
            domains=MUTATION_DOMAINS.get(service)
            if not domains:return
            # Ordinary reminder/goal/reading changes do not modify retained
            # life/chat originals. Their own legacy domains still invalidate.
            if service in {'health','expansion'}:
                domains=tuple(domain for domain in domains if domain not in {'life_records','ai_records'})
        application.state.member_multimodal.invalidate(actor,domains=domains,connection=connection)
        if domains is None or 'ai_records' in domains:
            application.state.conversation_memory.invalidate(actor, connection=connection)
    dispatcher.mutation_source_observer=invalidate_multimodal_mutation
    services["ai"].profile_memory=application.state.member_memory
    health.profile_change_hook=application.state.profile_vectors.invalidate
    services["expansion"].goal_memory_invalidator=application.state.member_memory.core.invalidate_goal_memory
    application.state.memory_summarizer = MemoryPreferenceSummarizer(
        auth, application.state.member_memory, application.state.ai_proxy)
    application.state.memory_projection = DailyProjection(auth, application.state.member_memory)
    from .member_ai_categories import create_category_router
    category_router = create_category_router(auth, application.state.member_memory, application.state.ai_proxy)
    application.state.category_summarizer = category_router.category_summarizer
    application.state.category_preferences = category_router.category_service
    application.include_router(category_router)
    application.include_router(create_memory_router(
        auth, database, services["preferences"], service=application.state.member_memory,
        summarizer=application.state.memory_summarizer, projector=application.state.memory_projection,
        category_summarizer=application.state.category_summarizer))
    from .ai139_tools import AI139Tools
    application.state.ai_proxy.tool_service = AI139Tools(
        auth, services, memory_service=application.state.member_memory)
    from .mobile_ai_recovery149 import AIRecovery149
    application.state.ai_proxy.operation_ledger = AIRecovery149(
        auth, chat_service=services["chat"],
        tool_service=application.state.ai_proxy.tool_service,
        memory_service=application.state.member_memory)
    from .mobile_sync149 import MobileSync149, create_mobile_sync149_router
    application.state.mobile_sync149 = MobileSync149(
        auth, services, memory_service=application.state.member_memory,
        category_service=application.state.category_preferences)
    application.include_router(create_mobile_sync149_router(auth, application.state.mobile_sync149))
    application.state.onboarding11830.local_sync = application.state.mobile_sync149
    from .guest_ai_store import GuestStore
    from .guest_ai import GuestAIService, create_guest_router
    application.state.guest_store = GuestStore(database, settings.token_pepper.get_secret_value())
    application.state.guest_ai = GuestAIService(application.state.guest_store, application.state.ai_proxy, application.state.library, gateway=application.state.voice_gateway)
    application.include_router(create_guest_router(application.state.guest_ai))
    application.add_middleware(PlatformBoundary, settings=settings, auth=auth, guest_store=application.state.guest_store)
    allowed_hosts = list(settings.allowed_hosts)
    if settings.railway_proxy and settings.railway_edge_only:
        allowed_hosts.append(RAILWAY_HEALTHCHECK_HOST)
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    @application.exception_handler(RequestValidationError)
    async def validation_error(_request, _error):
        return JSONResponse({"detail": "Invalid input"}, status_code=422)

    @application.exception_handler(sqlite3.Error)
    async def database_error(_request, _error):
        return JSONResponse({"detail": "Database unavailable"}, status_code=503)

    @application.get("/health/live")
    def live():
        from ollama_chat_app.config import APP_VERSION

        return {"status": "alive", "version": APP_VERSION, "mode": "cloud-platform"}

    @application.get("/health/ready")
    def ready():
        from ollama_chat_app.config import APP_VERSION

        try:
            database.initialize(force=True)
            auth.check_ready()
            return {"status": "ready", "version": APP_VERSION}
        except Exception:
            return JSONResponse({"status": "not_ready"}, status_code=503)

    @application.post("/v1/rpc/{service}/{method}")
    def rpc(service: str, method: str, payload: dict, request: Request, background: BackgroundTasks):
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        try:
            principal = auth.resolve_token(header[7:])
            with auth.identity(principal):
                result = dispatcher.invoke(
                    principal.user.id,
                    service,
                    method,
                    payload,
                    operation_guard=lambda target_service, target_method, arguments: (
                        application.state.developer_service.enforce_operation(
                            principal, target_service, target_method, arguments
                        )
                    ),
                )
            if not isinstance(result, Mapping):
                raise RpcError(500, "invalid_result")
            maximum_result = (8 if service == "sync" else 32) * 1024 * 1024
            if len(json.dumps(result, ensure_ascii=False).encode()) > maximum_result:
                raise RpcError(413, "result_too_large")
            from .platform_rpc import is_mutation
            if is_mutation(method):
                application.state.memory_projection.schedule(
                    background, principal, MUTATION_DOMAINS.get(service, ()))
                if ((service == 'chat' and method in {'complete_message', 'fail_message'}) or service == 'sync'
                        or service in {'health', 'expansion'}):
                    background.add_task(application.state.conversation_memory.start, request, principal)
            return result
        except RpcError as error:
            payload = {"detail": error.code}
            if error.public_message is not None:
                payload["message"] = error.public_message
            return JSONResponse(payload, status_code=error.status)
        except ExpansionError as error:
            status=409 if error.code in {"conflict","invalid_state","invitation_already_bound"} else 403 if error.code in {"permission_denied","consent_required"} else 404 if error.code=="not_found" else 422
            return JSONResponse({"detail":error.code},status_code=status)
        except PlatformAuthError as error:
            headers = {"WWW-Authenticate": "Bearer"} if error.status_code == 401 else None
            if error.status_code == 429:
                headers = {"Retry-After": "60"}
            return JSONResponse(
                {"detail": str(error)}, status_code=error.status_code, headers=headers
            )
        except sqlite3.Error:
            return JSONResponse({"detail": "Database unavailable"}, status_code=503)
        except (ValueError, RuntimeError, LookupError):
            # Public errors never include SQL, inputs, tokens, local paths or driver messages.
            return JSONResponse(
                {"detail": "Operation not permitted or input invalid"}, status_code=422
            )

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return application
