"""Production HTTPS on both independent API targets; proxy headers never establish trust."""

from __future__ import annotations

import os
import re
from pathlib import Path

import uvicorn
from sqlalchemy.engine import URL, make_url

from .guard import DATABASE, RUNTIME, DeploymentError, container_gate, read_file, secret

SECRETS = Path("/run/api-secrets")


def validate_supabase_url(raw: str):
    try:
        url = make_url(raw)
        if (
            url.drivername != "postgresql+psycopg"
            or not url.password
            or len(url.password) < 32
            or not url.database
            or url.port not in (None, 5432)
            or not url.host
            or not re.fullmatch(
                r"medical_app_platform_runtime(?:\.[a-z0-9]{20})?", url.username or ""
            )
            or not (url.host.endswith(".supabase.co") or url.host.endswith(".pooler.supabase.com"))
            or url.query.get("sslmode") != "verify-full"
            or set(url.query) - {"sslmode", "sslrootcert", "connect_timeout"}
        ):
            raise ValueError
        # Explicit dedicated mounted CA; ignore arbitrary paths supplied with URL.
        return url.update_query_dict(
            {"sslrootcert": "/run/api-secrets/database-ca.crt", "connect_timeout": "10"}
        )
    except Exception:
        raise DeploymentError(
            "Supabase requires an existing restricted runtime URL and CA."
        ) from None


def runtime_url(target: str):
    if target == "aliyun":
        return URL.create(
            "postgresql+psycopg",
            username=RUNTIME,
            password=secret(SECRETS / "database-password"),
            host="postgres",
            port=5432,
            database=DATABASE,
            query={
                "sslmode": "verify-full",
                "sslrootcert": str(SECRETS / "ca.crt"),
                "connect_timeout": "10",
            },
        )
    if target == "supabase":
        read_file(SECRETS / "database-ca.crt")
        return validate_supabase_url(read_file(SECRETS / "database-url"))
    raise DeploymentError("API_TARGET must be explicitly aliyun or supabase.")


def create_cloud_app():
    from server.platform_app import create_app
    from server.platform_config import PlatformSettings
    from server.platform_database import PlatformDatabase

    deployment = container_gate()
    target = os.environ.get("API_TARGET", "")
    settings = PlatformSettings(
        env="production",
        database_url=runtime_url(target).render_as_string(hide_password=False),
        token_pepper=secret(SECRETS / "token-pepper"),
        allowed_hosts=[deployment["public_host"]],
        require_https=True,
        render_proxy=False,
        railway_proxy=False,
        railway_edge_only=False,
    )
    database = PlatformDatabase(settings.database_url)
    try:
        if target == "aliyun":
            with database.engine.connect() as connection:
                row = connection.exec_driver_sql(
                    "SELECT current_database(),current_setting('medical_app.deployment_id',true)"
                ).one()
                if tuple(row) != (DATABASE, deployment["id"]):
                    raise DeploymentError("API database deployment identity differs.")
        app = create_app(settings, database=database)
        app.state.platform_auth.check_ready()
        return app
    except Exception:
        database.dispose()
        raise DeploymentError("API readiness failed; database details hidden.") from None


def main() -> None:
    app = create_cloud_app()
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8443,
        ssl_certfile=str(SECRETS / "server.crt"),
        ssl_keyfile=str(SECRETS / "server.key"),
        proxy_headers=False,
        access_log=False,
        log_level="warning",
        workers=1,
        limit_concurrency=12,
        timeout_keep_alive=5,
        timeout_graceful_shutdown=20,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Cloud API failed to start; credential details hidden.") from None
