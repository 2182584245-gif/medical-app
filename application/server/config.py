from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SERVER_",
        env_file=None,
        extra="forbid",
        hide_input_in_errors=True,
    )

    env: Literal["development", "test", "production"]
    database_url: SecretStr
    token_pepper: SecretStr
    token_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    allowed_hosts: list[str] = Field(default_factory=lambda: ["localhost", "127.0.0.1"])
    require_https: bool = True
    max_body_bytes: int = Field(default=262144, ge=1024, le=262144)
    body_timeout_seconds: float = Field(default=10, ge=0.05, le=30)
    auth_attempts_per_minute: int = Field(default=20, ge=1, le=100)
    auth_global_attempts_per_minute: int = Field(default=120, ge=1, le=1000)
    argon2_concurrency: int = Field(default=2, ge=1, le=4)
    external_auth_limits_configured: bool = False

    @model_validator(mode="after")
    def validate_security(self) -> Settings:
        pepper = self.token_pepper.get_secret_value()
        try:
            raw_pepper = bytes.fromhex(pepper)
        except ValueError:
            raise ValueError(
                "token_pepper must be a random 64-character hexadecimal secret"
            ) from None
        if len(pepper) != 64 or len(raw_pepper) != 32 or len(set(raw_pepper)) < 16:
            raise ValueError("token_pepper must contain 32 independently random bytes")
        try:
            url = make_url(self.database_url.get_secret_value())
        except Exception:
            raise ValueError("database_url is not a supported SQLAlchemy URL") from None
        if url.drivername not in {"postgresql+psycopg", "sqlite+pysqlite"}:
            raise ValueError(
                "use postgresql+psycopg; SQLite is reserved for local development/tests"
            )
        if not self.allowed_hosts or any(not host or "/" in host for host in self.allowed_hosts):
            raise ValueError("allowed_hosts must contain explicit host names")
        if self.env == "production":
            if not self.external_auth_limits_configured:
                raise ValueError(
                    "production is blocked until external multi-instance auth limits are verified"
                )
            if url.drivername != "postgresql+psycopg":
                raise ValueError("production requires PostgreSQL with psycopg")
            if (
                not url.host
                or not url.database
                or not url.username
                or not url.password
                or len(url.password) < 16
                or any(word in url.password.casefold() for word in ("changeme", "example"))
            ):
                raise ValueError(
                    "production requires explicit, non-placeholder database credentials"
                )
            if url.query.get("sslmode") != "verify-full":
                raise ValueError("production database connections require sslmode=verify-full")
            if not self.require_https:
                raise ValueError("production HTTPS enforcement cannot be disabled")
            if any(
                "*" in host or host in {"localhost", "127.0.0.1", "testserver"}
                for host in self.allowed_hosts
            ):
                raise ValueError("production requires explicit public allowed_hosts")
        return self
