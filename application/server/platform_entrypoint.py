"""Cloud entrypoint. Secrets come from the service environment, never the repository."""

from __future__ import annotations

import os
import ssl
from pathlib import Path

import uvicorn


def main():
    try:
        pem = os.environ.get("PLATFORM_DATABASE_CA_PEM", "")
        if not pem or len(pem) > 32_000:
            raise ValueError
        ssl.create_default_context(cadata=pem)
        target = Path("/tmp/medical-app-prod-ca.crt")
        if target.is_symlink():
            raise ValueError
        descriptor = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(pem)
        port = int(os.environ.get("PORT", "10000"))
        if not 1024 <= port <= 65535:
            raise ValueError
    except Exception:
        raise SystemExit(
            "Database certificate or listen configuration invalid; details hidden."
        ) from None
    uvicorn.run(
        "server.platform_app:create_app",
        factory=True,
        host="0.0.0.0",
        port=port,
        proxy_headers=False,
        access_log=False,
        log_level="warning",
        workers=1,
        limit_concurrency=12,
        timeout_keep_alive=5,
        timeout_graceful_shutdown=20,
    )


if __name__ == "__main__":
    main()
