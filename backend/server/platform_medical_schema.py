"""Current v3 semantics; immutable v1/v2 DDL and ownership markers are unchanged."""

from __future__ import annotations

PLATFORM_REVISION = "platform_0003"
LEGACY_REVISION = "platform_0002"
OLD_CATEGORY_CHECK = (
    "CHECK ((category = ANY (ARRAY['diet'::text, 'water'::text, 'activity'::text, "
    "'sleep'::text, 'environment'::text])))"
)
CATEGORY_CHECK = (
    "CHECK ((category = ANY (ARRAY['diet'::text, 'water'::text, 'activity'::text, "
    "'sleep'::text, 'environment'::text, 'medical'::text])))"
)
ALTER_CATEGORY_SQL = (
    "ALTER TABLE medical_app_platform.life_records "
    "DROP CONSTRAINT life_records_category_check, "
    "ADD CONSTRAINT life_records_category_check CHECK "
    "(category IN ('diet','water','activity','sleep','environment','medical'))"
)


def category_check(connection, *, placeholder="%s"):
    """Read-only, identity-independent catalog check; no public data is read."""
    rows = connection.execute(
        "SELECT pg_catalog.pg_get_constraintdef(k.oid),k.convalidated "
        "FROM pg_catalog.pg_constraint k JOIN pg_catalog.pg_class c ON c.oid=k.conrelid "
        "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=" + placeholder + " AND c.relname='life_records' "
        "AND k.conname='life_records_category_check' AND k.contype='c'",
        ("medical_app_platform",),
    ).fetchall()
    return (str(rows[0][0]), bool(rows[0][1])) if len(rows) == 1 else (None, False)
