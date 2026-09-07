"""Add the private 27-table full platform without adopting or changing pilot rows.

Revision ID: platform_0001
Revises: pilot_0001

The platform has integer identities and legacy UTC ISO text timestamps, unlike
the independently retained UUID/TIMESTAMPTZ pilot. No desktop data is imported by
this revision. No database runtime credentials or public role access are created.
"""

from alembic import op

from server.platform_schema import (
    INDEX_DDL,
    PLATFORM_MARKER,
    PLATFORM_SCHEMA,
    PLATFORM_TABLES,
    TABLE_DDL,
    compatibility_constraints,
    policy_ddl,
    revoke_ddl,
)

revision = "platform_0001"
down_revision = "pilot_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Transactional CREATE SCHEMA and a marker make interrupted migration safe:
    # PostgreSQL rolls everything back; an unrelated existing schema is refused.
    op.execute(f"""
DO $platform_new_schema$
BEGIN
    IF pg_catalog.to_regnamespace('{PLATFORM_SCHEMA}') IS NOT NULL THEN
        RAISE EXCEPTION 'Refusing to adopt an existing platform schema';
    END IF;
END;
$platform_new_schema$;
""")
    op.execute(f"CREATE SCHEMA {PLATFORM_SCHEMA}")
    op.execute(f"COMMENT ON SCHEMA {PLATFORM_SCHEMA} IS '{PLATFORM_MARKER}'")
    op.execute("SET LOCAL standard_conforming_strings = on")
    op.execute("SET LOCAL TIME ZONE 'UTC'")
    for sql in TABLE_DDL:
        op.execute(sql)
    for table in PLATFORM_TABLES:
        op.execute(f"COMMENT ON TABLE {PLATFORM_SCHEMA}.{table} IS '{PLATFORM_MARKER}'")
    for sql in (*INDEX_DDL, *compatibility_constraints(), *policy_ddl()):
        op.execute(sql)
    op.execute(revoke_ddl())


def downgrade() -> None:
    # Do not pretend a generic downgrade can safely delete health records,
    # attachments, identities or platform sessions. A reviewed export/restore and
    # empty-object ownership audit must precede any explicitly requested removal.
    raise RuntimeError(
        "Automatic platform downgrade is intentionally blocked; "
        "export and verify the data before an explicitly reviewed removal migration"
    )
