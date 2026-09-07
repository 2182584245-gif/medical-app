"""Shared, fail-closed authentication budgets for the full platform backend.

PostgreSQL is the production coordinator; SQLite is an explicit offline test
backend. Buckets store no IP address or username. A keyed hash maps each axis
into 8,192 slots, so even hostile high-cardinality input cannot grow this table
beyond 16,385 rows. Hash collisions can only throttle more conservatively.
Windows are fixed UTC minutes, so the documented boundary burst is at most 2x.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .security import AuthCapacityError, Security

PLATFORM_SCHEMA = "medical_app_platform"
PLATFORM_RUNTIME_ROLE = "medical_app_platform_runtime"
BUCKETS_PER_AXIS = 8192


class PlatformAuthSettings(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True, extra="forbid")
    token_pepper: SecretStr
    token_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    auth_ip_limit: int = Field(default=20, ge=1, le=100)
    auth_username_limit: int = Field(default=10, ge=1, le=100)
    auth_global_limit: int = Field(default=120, ge=1, le=1000)
    argon2_concurrency: int = Field(default=2, ge=1, le=4)
    production: bool = True

    @field_validator("token_pepper")
    @classmethod
    def strong_pepper(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        try:
            decoded = bytes.fromhex(secret)
        except ValueError:
            raise ValueError(
                "token_pepper must be 32 random bytes encoded as 64 hex digits"
            ) from None
        if len(secret) != 64 or len(decoded) != 32 or len(set(decoded)) < 16:
            raise ValueError("token_pepper must contain 32 independently random bytes")
        return value


class PlatformAuthUnavailable(RuntimeError):
    """Safe message only; driver errors and credential input must never escape."""


class SharedAuthLimiter:
    def __init__(self, database, settings: PlatformAuthSettings, *, clock=time.time):
        self.database = database
        self.settings = settings
        self.clock = clock
        self._key = bytes.fromhex(settings.token_pepper.get_secret_value())
        self.postgres = getattr(database, "dialect", "sqlite") == "postgresql"
        if settings.production and not self.postgres:
            raise PlatformAuthUnavailable("生产认证需要 PostgreSQL 共享限流，禁止进程内替代。")

    def bucket_ids(self, client: str, username: str) -> tuple[int, int, int]:
        def slot(axis: str, value: str) -> int:
            digest = hmac.new(self._key, (axis + "\0" + value).encode(), hashlib.sha256).digest()
            return int.from_bytes(digest[:4], "big") % BUCKETS_PER_AXIS

        # Inputs are normalized/bounded by authentication before this helper.
        return (
            0,
            1 + slot("ip", client[:256]),
            1 + BUCKETS_PER_AXIS + slot("name", username[:256]),
        )

    def check(self, client: str, username: str) -> None:
        limits = (
            self.settings.auth_global_limit,
            self.settings.auth_ip_limit,
            self.settings.auth_username_limit,
        )
        counts = []
        try:
            with self.database.transaction() as connection:
                if self.postgres:
                    connection.execute("SET LOCAL statement_timeout = '5000ms'")
                    connection.execute("SET LOCAL lock_timeout = '3000ms'")
                    connection.execute("SELECT pg_catalog.pg_advisory_xact_lock(717380022, 1)")
                    now = int(
                        connection.execute(
                            "SELECT floor(extract(epoch FROM clock_timestamp()))::bigint"
                        ).fetchone()[0]
                    )
                else:
                    # SQLite's BEGIN IMMEDIATE serializes independent instances in tests.
                    now = int(self.clock())
                window = now // 60 * 60
                connection.execute(
                    "DELETE FROM auth_rate_buckets WHERE bucket_id IN "
                    "(SELECT bucket_id FROM auth_rate_buckets WHERE window_start < ? "
                    "ORDER BY window_start, bucket_id LIMIT 128)",
                    (window - 120,),
                )
                for bucket in self.bucket_ids(client, username):
                    row = connection.execute(
                        "INSERT INTO auth_rate_buckets (bucket_id, window_start, attempts) "
                        "VALUES (?, ?, 1) ON CONFLICT (bucket_id) DO UPDATE SET "
                        "attempts = CASE WHEN auth_rate_buckets.window_start "
                        "<> excluded.window_start "
                        "THEN 1 WHEN auth_rate_buckets.attempts < 1000000 "
                        "THEN auth_rate_buckets.attempts + 1 ELSE 1000000 END, "
                        "window_start = excluded.window_start RETURNING attempts",
                        (bucket, window),
                    ).fetchone()
                    if row is None:
                        raise PlatformAuthUnavailable("共享认证限流没有返回结果，已拒绝认证。")
                    counts.append(int(row[0]))
            # Commit rejected attempts too: rollback here would erase the throttle.
        except Exception:
            raise PlatformAuthUnavailable("共享认证限流暂不可用，已拒绝认证。") from None
        if any(count > limit for count, limit in zip(counts, limits, strict=True)):
            raise AuthCapacityError()


def make_security(settings: PlatformAuthSettings) -> Security:
    return Security(
        settings.token_pepper.get_secret_value(), concurrency=settings.argon2_concurrency
    )
