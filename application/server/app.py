from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings
from .database import Database
from .models import Conversation, LoginSession, Message, User, utc_now
from .schemas import (
    ConversationCreate,
    ConversationTitle,
    ConversationView,
    Credentials,
    MessageCreate,
    MessageView,
    TokenView,
    UserView,
)
from .security import AuthCapacityError, AuthRateLimiter, Security, request_digest

_bearer = HTTPBearer(auto_error=False)


class RequestBoundary:
    """Bound actual streamed bytes before JSON parsing, including chunked requests."""

    def __init__(self, app, *, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if (
            self.settings.require_https
            and scope["scheme"] != "https"
            and not scope["path"].startswith("/health/")
        ):
            await JSONResponse({"detail": "HTTPS required"}, 426)(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            await JSONResponse({"detail": "Invalid Content-Length"}, 400)(scope, receive, send)
            return
        if declared < 0 or declared > self.settings.max_body_bytes:
            await JSONResponse({"detail": "Request body too large"}, 413)(scope, receive, send)
            return
        chunks = []
        size = 0
        try:
            async with asyncio.timeout(self.settings.body_timeout_seconds):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    size += len(message.get("body", b""))
                    if size > self.settings.max_body_bytes:
                        await JSONResponse({"detail": "Request body too large"}, 413)(
                            scope, receive, send
                        )
                        return
                    chunks.append(message)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await JSONResponse({"detail": "Request body timeout"}, 408)(scope, receive, send)
            return
        iterator = iter(chunks)

        async def bounded_receive():
            return next(iterator, {"type": "http.request", "body": b"", "more_body": False})

        async def private_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                ]
            await send(message)

        await self.app(scope, bounded_receive, private_send)


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.database.sessions() as session:
        yield session


Db = Annotated[Session, Depends(get_db)]


def unauthorized() -> HTTPException:
    return HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})


@dataclass(frozen=True)
class Principal:
    user: User
    session: LoginSession


def get_principal(
    request: Request,
    db: Db,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    if credentials is None or not re.fullmatch(r"[A-Za-z0-9_-]{43}", credentials.credentials):
        raise unauthorized()
    digest = request.app.state.security.token_digest(credentials.credentials)
    row = db.execute(
        select(User, LoginSession)
        .join(LoginSession, LoginSession.user_id == User.id)
        .where(
            LoginSession.token_digest == digest,
            LoginSession.revoked_at.is_(None),
            LoginSession.expires_at > utc_now(),
            User.active.is_(True),
        )
    ).first()
    if row is None:
        raise unauthorized()
    return Principal(row[0], row[1])


Auth = Annotated[Principal, Depends(get_principal)]
PageLimit = Annotated[int, Query(ge=1, le=100)]
PageOffset = Annotated[int, Query(ge=0, le=10000)]


def owned_conversation(db: Session, user_id: UUID, conversation_id: UUID) -> Conversation:
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
        )
    )
    if conversation is None:
        raise HTTPException(404, "Conversation not found")
    return conversation


def matching_retry(record, digest: str):
    if record is None or record.request_digest != digest:
        raise HTTPException(409, "request_id already used with a different payload")
    return record


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    # Validation and engine construction never initialize tables or contact a cloud service.
    database = Database(settings)

    @asynccontextmanager
    async def lifespan(_app):
        yield
        database.engine.dispose()

    app = FastAPI(
        title="Health Life Server Pilot",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if settings.env == "production" else "/docs",
        redoc_url=None,
        openapi_url=None if settings.env == "production" else "/openapi.json",
    )
    app.state.settings = settings
    app.state.database = database
    app.state.security = Security(
        settings.token_pepper.get_secret_value(),
        concurrency=settings.argon2_concurrency,
    )
    app.state.auth_limiter = AuthRateLimiter(
        settings.auth_attempts_per_minute,
        settings.auth_global_attempts_per_minute,
    )
    app.add_middleware(RequestBoundary, settings=settings)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, error: RequestValidationError):
        # Pydantic's default errors include submitted input, potentially including a password/key.
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {"loc": item["loc"], "msg": item["msg"], "type": item["type"]}
                    for item in error.errors()
                ]
            },
        )

    @app.exception_handler(SQLAlchemyError)
    async def unavailable_database(_request: Request, _error: SQLAlchemyError):
        return JSONResponse({"detail": "Database unavailable"}, status_code=503)

    @app.exception_handler(AuthCapacityError)
    async def auth_busy(_request: Request, _error: AuthCapacityError):
        return JSONResponse(
            {"detail": "Authentication temporarily limited"},
            status_code=429,
            headers={"Retry-After": "60"},
        )

    @app.get("/health/live")
    def live():
        return {"status": "alive", "stage": "pilot"}

    @app.get("/health/ready")
    def ready():
        try:
            database.check_ready()
        except SQLAlchemyError:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return {"status": "ready", "stage": "pilot"}

    @app.post("/auth/register", response_model=UserView, status_code=201)
    def register(payload: Credentials, request: Request, db: Db):
        request.app.state.auth_limiter.check(request.client.host if request.client else "unknown")
        password_hash = request.app.state.security.hash_password(
            payload.password.get_secret_value()
        )
        user = User(
            username=payload.username,
            username_normalized=payload.username.casefold(),
            password_hash=password_hash,
        )
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "Registration could not be completed") from None
        return user

    @app.post("/auth/login", response_model=TokenView)
    def login(payload: Credentials, request: Request, db: Db):
        request.app.state.auth_limiter.check(request.client.host if request.client else "unknown")
        security = request.app.state.security
        user = db.scalar(
            select(User).where(User.username_normalized == payload.username.casefold())
        )
        stored_hash = security.dummy_hash if user is None else user.password_hash
        valid = security.verify_password(payload.password.get_secret_value(), stored_hash)
        if not valid or user is None or not user.active:
            raise unauthorized()
        if security.hasher.check_needs_rehash(user.password_hash):
            user.password_hash = security.hash_password(payload.password.get_secret_value())
        token, digest = security.issue_token()
        expires_at = utc_now() + timedelta(seconds=settings.token_ttl_seconds)
        db.add(LoginSession(user_id=user.id, token_digest=digest, expires_at=expires_at))
        db.commit()
        return TokenView(access_token=token, expires_at=expires_at)

    @app.post("/auth/logout", status_code=204)
    def logout(auth: Auth, db: Db):
        auth.session.revoked_at = utc_now()
        db.commit()
        return Response(status_code=204)

    @app.get("/auth/me", response_model=UserView)
    def me(auth: Auth):
        return auth.user

    @app.post("/conversations", response_model=ConversationView, status_code=201)
    def create_conversation(payload: ConversationCreate, auth: Auth, db: Db, response: Response):
        digest = request_digest(payload.model_dump(mode="json"))
        query = select(Conversation).where(
            Conversation.user_id == auth.user.id,
            Conversation.request_id == payload.request_id,
        )
        existing = db.scalar(query)
        if existing is not None:
            response.status_code = 200
            return matching_retry(existing, digest)
        conversation = Conversation(
            user_id=auth.user.id,
            title=payload.title,
            request_id=payload.request_id,
            request_digest=digest,
        )
        db.add(conversation)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            response.status_code = 200
            return matching_retry(db.scalar(query), digest)
        return conversation

    @app.get("/conversations", response_model=list[ConversationView])
    def list_conversations(auth: Auth, db: Db, limit: PageLimit = 50, offset: PageOffset = 0):
        return db.scalars(
            select(Conversation)
            .where(Conversation.user_id == auth.user.id)
            .order_by(
                Conversation.created_at.desc(),
                Conversation.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        ).all()

    @app.patch("/conversations/{conversation_id}", response_model=ConversationView)
    def rename_conversation(conversation_id: UUID, payload: ConversationTitle, auth: Auth, db: Db):
        conversation = owned_conversation(db, auth.user.id, conversation_id)
        conversation.title = payload.title
        conversation.updated_at = utc_now()
        db.commit()
        return conversation

    @app.get("/conversations/{conversation_id}/messages", response_model=list[MessageView])
    def list_messages(
        conversation_id: UUID,
        auth: Auth,
        db: Db,
        limit: PageLimit = 50,
        after_sequence: Annotated[int, Query(ge=0, le=2147483647)] = 0,
    ):
        owned_conversation(db, auth.user.id, conversation_id)
        return db.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.sequence_no > after_sequence,
            )
            .order_by(Message.sequence_no)
            .limit(limit)
        ).all()

    @app.post(
        "/conversations/{conversation_id}/messages", response_model=MessageView, status_code=201
    )
    def write_message(
        conversation_id: UUID,
        payload: MessageCreate,
        auth: Auth,
        db: Db,
        response: Response,
    ):
        owned_conversation(db, auth.user.id, conversation_id)
        digest = request_digest(payload.model_dump(mode="json"))
        query = select(Message).where(
            Message.conversation_id == conversation_id,
            Message.request_id == payload.request_id,
        )
        existing = db.scalar(query)
        if existing is not None:
            response.status_code = 200
            return matching_retry(existing, digest)
        # Atomic counter update locks this conversation on PostgreSQL. The transaction
        # includes the message insert, so a duplicate/failed insert rolls back allocation.
        next_sequence = db.scalar(
            update(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.user_id == auth.user.id,
            )
            .values(
                next_sequence=Conversation.next_sequence + 1,
                updated_at=utc_now(),
            )
            .returning(Conversation.next_sequence)
        )
        if next_sequence is None:
            raise HTTPException(404, "Conversation not found")
        message = Message(
            conversation_id=conversation_id,
            request_id=payload.request_id,
            request_digest=digest,
            sequence_no=next_sequence - 1,
            role=payload.role,
            content=payload.content,
            provider=payload.provider,
            model=payload.model,
        )
        db.add(message)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            response.status_code = 200
            return matching_retry(db.scalar(query), digest)
        return message

    return app
