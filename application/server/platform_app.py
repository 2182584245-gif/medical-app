"""Full-platform HTTPS API; no desktop GUI, OCR, voice model or cloud AI key required."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import sqlite3
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ollama_chat_app.services.auth import AuthService
from ollama_chat_app.services.chat import ChatService
from ollama_chat_app.services.commerce import CommerceService
from ollama_chat_app.services.health import HealthService
from ollama_chat_app.services.service_management import ServiceManagementService

from .app import RequestBoundary
from .platform_ai import PlatformAIService
from .platform_config import PlatformSettings
from .platform_rpc import RpcDispatcher, RpcError
from .platform_uploads import PlatformFileService, validate_chat_uploads


class PlatformChatService(ChatService):
    """Keep a bounded recent multimodal context for the 32 MiB transport envelope."""

    def begin_message(
        self, user_id, content, provider=None, model=None, *, conversation_id=None, attachments=()
    ):
        validate_chat_uploads(attachments)
        return super().begin_message(
            user_id,
            content,
            provider=provider,
            model=model,
            conversation_id=conversation_id,
            attachments=attachments,
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


class PlatformBoundary:
    """Bound in-flight request memory and trust only the selected Render ingress."""

    def __init__(self, app, *, settings, auth=None):
        self.app, self.settings = app, settings
        self.auth = auth
        self._large = asyncio.Semaphore(1)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        sensitive_headers = {
            b"authorization",
            b"content-length",
            b"x-forwarded-proto",
            b"cf-connecting-ip",
        }
        seen = set()
        for name, _value in scope.get("headers", []):
            if name in sensitive_headers:
                if name in seen:
                    return await JSONResponse({"detail": "Ambiguous request headers"}, 400)(
                        scope, receive, send
                    )
                seen.add(name)
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
        large = scope["path"] in {
            "/v1/rpc/files/upload_bytes",
            "/v1/rpc/chat/begin_message",
        }
        if (
            self.settings.require_https
            and scope["scheme"] != "https"
            and not scope["path"].startswith("/health/")
        ):
            return await JSONResponse({"detail": "HTTPS required"}, 426)(scope, receive, send)
        if large:
            authorization = headers.get(b"authorization", b"")
            if not re.fullmatch(rb"Bearer [A-Za-z0-9_-]{43}", authorization) or self.auth is None:
                return await JSONResponse({"detail": "Authentication required"}, 401)(
                    scope, receive, send
                )
            try:
                await asyncio.wait_for(self._large.acquire(), timeout=0.05)
            except TimeoutError:
                return await JSONResponse({"detail": "Upload capacity limited"}, 429)(
                    scope, receive, send
                )
        try:
            if large:
                # Bound both authentication work and possible 32 MiB buffering.
                # Validate again in the route after upload in case state changed.
                try:
                    await asyncio.to_thread(
                        self.auth.resolve_token, authorization[7:].decode("ascii")
                    )
                except Exception as error:
                    from .platform_auth import PlatformAuthError

                    status = error.status_code if isinstance(error, PlatformAuthError) else 503
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


def create_app(settings=None, *, database=None):
    from .platform_auth import PlatformAuth, PlatformAuthError, create_auth_router
    from .platform_database import PlatformDatabase

    settings = settings or PlatformSettings()
    database = database or PlatformDatabase(settings.database_url.get_secret_value())
    auth = PlatformAuth(database, settings)
    database.initialize()
    health = HealthService(database)
    services = {
        "auth": AuthService(database),
        "chat": PlatformChatService(database),
        "health": health,
        "service_management": ServiceManagementService(database),
        "commerce": CommerceService(database),
        "files": PlatformFileService(database),
        "ai": PlatformAIService(database, health, settings.token_pepper.get_secret_value()),
    }
    dispatcher = RpcDispatcher(database, services)

    @asynccontextmanager
    async def lifespan(_application):
        if settings.production:
            # Refuse public startup unless shared limits and database security really exist.
            auth.check_ready()
        yield
        if hasattr(database, "dispose"):
            database.dispose()

    application = FastAPI(
        title="健康生活服务平台 API",
        version="1.2.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.state.database = database
    application.state.platform_auth = auth
    application.state.settings = settings
    application.state.services = services
    application.include_router(create_auth_router(auth))
    application.add_middleware(PlatformBoundary, settings=settings, auth=auth)
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    @application.exception_handler(RequestValidationError)
    async def validation_error(_request, _error):
        return JSONResponse({"detail": "Invalid input"}, status_code=422)

    @application.exception_handler(sqlite3.Error)
    async def database_error(_request, _error):
        return JSONResponse({"detail": "Database unavailable"}, status_code=503)

    @application.get("/health/live")
    def live():
        return {"status": "alive", "version": "1.2.0", "mode": "cloud-platform"}

    @application.get("/health/ready")
    def ready():
        try:
            database.initialize(force=True)
            auth.check_ready()
            return {"status": "ready", "version": "1.2.0"}
        except Exception:
            return JSONResponse({"status": "not_ready"}, status_code=503)

    @application.post("/v1/rpc/{service}/{method}")
    def rpc(service: str, method: str, payload: dict, request: Request):
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        try:
            principal = auth.resolve_token(header[7:])
            with auth.identity(principal):
                result = dispatcher.invoke(principal.user.id, service, method, payload)
            if not isinstance(result, Mapping):
                raise RpcError(500, "invalid_result")
            if len(json.dumps(result, ensure_ascii=False).encode()) > 32 * 1024 * 1024:
                raise RpcError(413, "result_too_large")
            return result
        except RpcError as error:
            return JSONResponse({"detail": error.code}, status_code=error.status)
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
