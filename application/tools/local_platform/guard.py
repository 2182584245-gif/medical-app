"""Fixed local-only configuration: no user database URLs or cloud secret loaders."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

MARKER = "medical-app:local-container:v1"
API_SECRETS = Path("/run/api-secrets")
DB_SECRETS = Path("/run/db-secrets")
CA_PRIVATE = Path("/run/ca-private")
SMOKE_STATE = Path("/run/smoke-state")
FORBIDDEN_PREFIXES = ("PLATFORM_", "SERVER_", "SUPABASE_", "RAILWAY_", "RENDER_", "PG")


def require_local_container() -> None:
    if (
        os.environ.get("LOCAL_CONTAINER_ONLY") != "1"
        or sys.platform != "linux"
        or not Path("/.dockerenv").is_file()
        or any(name.startswith(FORBIDDEN_PREFIXES) for name in os.environ)
    ):
        raise RuntimeError("Only the isolated local Docker laboratory is permitted.")


def read_secret(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 128:
        raise RuntimeError("Local test secret is missing or invalid.")
    value = path.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise RuntimeError("Local test secret is missing or invalid.")
    return value


def runtime_url():
    from sqlalchemy.engine import URL

    require_local_container()
    return URL.create(
        "postgresql+psycopg",
        username="medical_app_platform_runtime",
        password=read_secret(API_SECRETS / "database-password"),
        host="postgres",
        port=5432,
        database="medical_app_local",
        query={"sslmode": "verify-full", "sslrootcert": str(API_SECRETS / "ca.crt")},
    )
