from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from .schema import PRIVATE_SCHEMA


def utc_now() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """PostgreSQL TIMESTAMPTZ; normalize SQLite's test-only naive round trips."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone aware")
        return value.astimezone(UTC)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(
        schema=PRIVATE_SCHEMA,
        naming_convention={
            "pk": "pk_%(table_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        },
    )


class User(Base):
    __tablename__ = "pilot_users"
    __table_args__ = (
        CheckConstraint(
            "length(username_normalized) BETWEEN 2 AND 100", name="ck_pilot_users_username"
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(50))
    username_normalized: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class LoginSession(Base):
    __tablename__ = "pilot_login_sessions"
    __table_args__ = (Index("ix_pilot_sessions_user_expiry", "user_id", "expires_at"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("pilot_users.id", ondelete="CASCADE"))
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Conversation(Base):
    __tablename__ = "pilot_conversations"
    __table_args__ = (
        UniqueConstraint("user_id", "request_id", name="uq_pilot_conversation_request"),
        CheckConstraint("length(title) BETWEEN 1 AND 100", name="ck_pilot_conversations_title"),
        CheckConstraint("next_sequence > 0", name="ck_pilot_conversations_next_sequence"),
        Index("ix_pilot_conversations_user_date", "user_id", "created_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("pilot_users.id", ondelete="CASCADE"))
    request_id: Mapped[UUID] = mapped_column(Uuid)
    request_digest: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(100))
    next_sequence: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class Message(Base):
    __tablename__ = "pilot_messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "request_id", name="uq_pilot_message_request"),
        UniqueConstraint("conversation_id", "sequence_no", name="uq_pilot_message_sequence"),
        CheckConstraint("sequence_no > 0", name="ck_pilot_messages_sequence"),
        CheckConstraint("role IN ('user', 'assistant')", name="ck_pilot_messages_role"),
        CheckConstraint("length(content) BETWEEN 1 AND 50000", name="ck_pilot_messages_content"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("pilot_conversations.id", ondelete="CASCADE")
    )
    request_id: Mapped[UUID] = mapped_column(Uuid)
    request_digest: Mapped[str] = mapped_column(String(64))
    sequence_no: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(10))
    content: Mapped[str] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(String(100))
    model: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
