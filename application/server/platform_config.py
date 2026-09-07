"""Configuration for the full platform, separate from the historical pilot."""

from __future__ import annotations

import os
import re
from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

RAILWAY_HEALTHCHECK_HOST = "healthcheck.railway.app"
RAILWAY_DOMAIN_PATTERN = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.up\.railway\.app"


class PlatformSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PLATFORM_", env_file=None, extra="forbid", hide_input_in_errors=True
    )
    env: Literal["test", "development", "production"] = "production"
    database_url: SecretStr
    token_pepper: SecretStr
    allowed_hosts: list[str] = Field(default_factory=list)
    token_ttl_seconds: int = Field(default=7200, ge=60, le=86400)
    auth_ip_limit: int = Field(default=20, ge=1, le=100)
    auth_username_limit: int = Field(default=10, ge=1, le=50)
    auth_global_limit: int = Field(default=120, ge=1, le=1000)
    argon2_concurrency: int = Field(default=2, ge=1, le=2)
    render_proxy: bool = False
    railway_proxy: bool = False
    # An operator assertion about verified deployment networking, NOT a proof
    # of proxy identity: HTTP edge only, no TCP proxy, trusted project services.
    railway_edge_only: bool = False
    # Used by the bounded request middleware, never disables production TLS.
    require_https: bool = True
    max_body_bytes: int = Field(default=1024 * 1024, ge=1024, le=1024 * 1024)
    body_timeout_seconds: float = Field(default=30.0, ge=0.05, le=30.0)

    @property
    def production(self) -> bool:
        return self.env == "production"

    @model_validator(mode="after")
    def secure(self):
        try:
            if self.railway_proxy != self.railway_edge_only or (
                self.render_proxy and self.railway_proxy
            ):
                raise ValueError
            pepper = self.token_pepper.get_secret_value()
            raw = bytes.fromhex(pepper)
            if len(pepper) != 64 or len(raw) != 32 or len(set(raw)) < 16:
                raise ValueError
            url = make_url(self.database_url.get_secret_value())
            if self.production:
                if (
                    url.drivername != "postgresql+psycopg"
                    or not url.host
                    or not url.database
                    or not url.password
                    or len(url.password) < 32
                    or url.query.get("sslmode") != "verify-full"
                    or not url.query.get("sslrootcert")
                    or not self.require_https
                ):
                    raise ValueError
                if not re.fullmatch(
                    r"medical_app_platform_runtime(?:\.[a-z0-9]{20})?", url.username or ""
                ):
                    raise ValueError
                if self.railway_proxy:
                    # These checks catch deployment mistakes. Environment values
                    # do not authenticate incoming requests or prove isolation.
                    for name in ("RAILWAY_SERVICE_ID", "RAILWAY_PROJECT_ID",
                                 "RAILWAY_ENVIRONMENT_ID"):
                        value = os.environ.get(name, "")
                        if str(UUID(value)) != value:
                            raise ValueError
                    railway_host = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
                    if not re.fullmatch(RAILWAY_DOMAIN_PATTERN, railway_host):
                        raise ValueError
                    if not self.allowed_hosts:
                        self.allowed_hosts = [railway_host]
                    if railway_host not in self.allowed_hosts:
                        raise ValueError
                elif not self.allowed_hosts:
                    host = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")
                    if host.endswith(".onrender.com"):
                        self.allowed_hosts = [host]
                if not self.allowed_hosts or any(
                    not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host)
                    or ".." in host
                    or "." not in host
                    or host in {"localhost", "127.0.0.1", "testserver"}
                    for host in self.allowed_hosts
                ):
                    raise ValueError
                if self.railway_proxy and any(
                    host not in {railway_host, RAILWAY_HEALTHCHECK_HOST}
                    for host in self.allowed_hosts
                ):
                    raise ValueError
                if self.render_proxy and not (
                    os.environ.get("RENDER_SERVICE_ID", "").startswith("srv-")
                    and os.environ.get("RENDER_EXTERNAL_HOSTNAME", "") in self.allowed_hosts
                ):
                    raise ValueError
            elif url.drivername not in {"postgresql+psycopg", "sqlite+pysqlite"}:
                raise ValueError
        except Exception:
            raise ValueError("Platform security configuration is incomplete or invalid") from None
        return self
