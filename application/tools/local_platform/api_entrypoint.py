"""Direct TLS for localhost testing only; production ingress remains unchanged."""

from __future__ import annotations

import ssl

import uvicorn

from .guard import API_SECRETS, MARKER, read_secret, require_local_container, runtime_url


def create_local_app():
    from server.platform_app import create_app
    from server.platform_config import PlatformSettings
    from server.platform_database import PlatformDatabase

    require_local_container()
    settings = PlatformSettings(
        env="development",
        database_url=runtime_url().render_as_string(hide_password=False),
        token_pepper=read_secret(API_SECRETS / "token-pepper"),
        allowed_hosts=["api", "localhost", "127.0.0.1"],
        require_https=True,
        render_proxy=False,
        railway_proxy=False,
        railway_edge_only=False,
    )
    database = PlatformDatabase(settings.database_url)
    try:
        with database.engine.connect() as connection:
            identity = connection.exec_driver_sql(
                "SELECT current_database(), current_setting('medical_app.local_container', true), "
                "shobj_description((SELECT oid FROM pg_database WHERE datname=current_database()), "
                "'pg_database')"
            ).one()
            if tuple(identity) != ("medical_app_local", "medical-app-local-v1", MARKER):
                raise RuntimeError("Non-laboratory database was refused.")
        app = create_app(settings, database=database)
        app.state.platform_auth.check_ready()
        app.state.local_container_only = True
        return app
    except Exception:
        database.dispose()
        raise RuntimeError("Local API startup checks failed; details hidden.") from None


def main():
    require_local_container()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(API_SECRETS / "server.crt", API_SECRETS / "server.key")
    app = create_local_app()
    uvicorn.run(
        app, host="0.0.0.0", port=8443, ssl_certfile=str(API_SECRETS / "server.crt"),
        ssl_keyfile=str(API_SECRETS / "server.key"), proxy_headers=False, access_log=False,
        log_level="warning", workers=1, limit_concurrency=12, timeout_keep_alive=5,
        timeout_graceful_shutdown=20,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Local API failed to start; credential details hidden.") from None
