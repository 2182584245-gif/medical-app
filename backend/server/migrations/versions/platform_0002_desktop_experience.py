"""Add private preferences, staff terms, appointments and member shopping state.

Revision ID: platform_0002
Revises: platform_0001
Only new empty tables and reviewed policy changes; no historical data rewrite.
"""

from alembic import op

from server.platform_experience_schema import (
    NEW_COLUMNS,
    NEW_INDEX_DDL,
    NEW_TABLE_DDL,
    PLATFORM_MARKER,
    PLATFORM_SCHEMA,
    compatibility_constraints,
    incremental_policy_ddl,
)
from server.platform_schema import revoke_ddl

revision = "platform_0002"
down_revision = "platform_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(f"""
DO $platform_v2_guard$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_namespace
        WHERE nspname = '{PLATFORM_SCHEMA}'
        AND pg_catalog.obj_description(oid, 'pg_namespace') = '{PLATFORM_MARKER}'
        AND pg_catalog.pg_get_userbyid(nspowner) = current_user) THEN
        RAISE EXCEPTION 'Refusing to change an unowned platform schema';
    END IF;
END;
$platform_v2_guard$;
""")
    op.execute("SET LOCAL standard_conforming_strings = on")
    op.execute("SET LOCAL TIME ZONE 'UTC'")
    for statement in NEW_TABLE_DDL:
        op.execute(statement)
    for table in NEW_COLUMNS:
        op.execute(f"COMMENT ON TABLE {PLATFORM_SCHEMA}.{table} IS '{PLATFORM_MARKER}'")
    for statement in (*NEW_INDEX_DDL, *compatibility_constraints(), *incremental_policy_ddl()):
        op.execute(statement)
    op.execute(revoke_ddl())
    # Runtime grants remain an explicit, separately reviewed provisioning step.


def downgrade() -> None:
    raise RuntimeError(
        "Automatic v2 downgrade is blocked; preserve user records and review recovery."
    )
