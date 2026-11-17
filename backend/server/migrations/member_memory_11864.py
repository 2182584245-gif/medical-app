"""Install the reviewed member-memory tables in the platform owner's transaction."""

from collections.abc import Callable
from pathlib import Path

from server.platform_schema import PLATFORM_MARKER, PLATFORM_RUNTIME_ROLE, PLATFORM_SCHEMA

_SQL_DIR = Path(__file__).with_name("sql")
_SQL_FILES = (
    "member-memory-v1.sql",
    "member-ai-preferences-v1.sql",
    "member-ai-preference-categories-v1.sql",
)


def upgrade_member_memory(execute: Callable[[str], object]) -> None:
    """Require the platform schema owner and preserve member-only RLS."""
    execute("SELECT pg_catalog.pg_advisory_xact_lock(717380021, 1)")
    execute(
        f"""
        DO $member_memory_owner$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_catalog.pg_namespace
                WHERE nspname = '{PLATFORM_SCHEMA}'
                  AND pg_catalog.pg_get_userbyid(nspowner) = current_user
                  AND pg_catalog.obj_description(oid, 'pg_namespace') = '{PLATFORM_MARKER}'
            ) OR NOT EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles
                WHERE rolname = '{PLATFORM_RUNTIME_ROLE}'
                  AND rolname <> current_user AND NOT rolsuper AND NOT rolbypassrls
            ) THEN
                RAISE EXCEPTION 'Reviewed platform owner and restricted runtime role required';
            END IF;
        END;
        $member_memory_owner$;
        """
    )
    for filename in _SQL_FILES:
        sql = (_SQL_DIR / filename).read_text(encoding="utf-8")
        for statement in sql.split("\n-- statement boundary\n"):
            execute(statement)
