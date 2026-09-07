from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Credentials(InputModel):
    username: str = Field(min_length=2, max_length=50)
    password: SecretStr = Field(min_length=8, max_length=256)

    @field_validator("username")
    @classmethod
    def clean_username(cls, value: str) -> str:
        value = unicodedata.normalize("NFKC", value).strip()
        if (
            not 2 <= len(value) <= 50
            or len(value.casefold()) > 100
            or any(unicodedata.category(c).startswith("C") for c in value)
        ):
            raise ValueError("username must contain 2 to 50 visible characters")
        return value


class ConversationTitle(InputModel):
    title: str = Field(min_length=1, max_length=100)

    @field_validator("title")
    @classmethod
    def nonblank_title(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("title must not be blank or contain a null character")
        return value.strip()


class ConversationCreate(ConversationTitle):
    request_id: UUID


class MessageCreate(InputModel):
    request_id: UUID
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=50000)
    provider: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=200)

    @field_validator("content")
    @classmethod
    def nonblank_content(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("content must not be blank or contain a null character")
        return value

    @field_validator("provider", "model")
    @classmethod
    def labels_without_nulls(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:
            raise ValueError("labels must not contain null characters")
        return value


class OutputModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class UserView(OutputModel):
    id: UUID
    username: str
    created_at: datetime


class TokenView(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_at: datetime


class ConversationView(OutputModel):
    id: UUID
    title: str
    request_id: UUID
    created_at: datetime
    updated_at: datetime


class MessageView(OutputModel):
    id: UUID
    conversation_id: UUID
    request_id: UUID
    sequence_no: int
    role: str
    content: str
    provider: str | None
    model: str | None
    created_at: datetime
