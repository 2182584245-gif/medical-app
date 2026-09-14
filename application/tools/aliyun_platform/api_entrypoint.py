"""Aliyun-only production HTTPS; retired targets never read credentials."""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from sqlalchemy.engine import URL

from .guard import DATABASE, RUNTIME, DeploymentError, container_gate, secret

SECRETS = Path("/run/api-secrets")


def runtime_url(target: str):
    if target != "aliyun":
        raise DeploymentError("Only the explicit Aliyun runtime target is supported.")
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
