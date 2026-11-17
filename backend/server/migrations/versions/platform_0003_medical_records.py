"""Widen only the fact category constraint, preserving all records and permissions.

Revision ID: platform_0003
Revises: platform_0002
"""

from alembic import op

from server.platform_medical_schema import ALTER_CATEGORY_SQL, OLD_CATEGORY_CHECK
from server.platform_schema import PLATFORM_MARKER, PLATFORM_SCHEMA

revision = "platform_0003"
down_revision = "platform_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    # Hold the same lock ALTER needs before reading the old definition, so even
    # another administrator cannot replace the reviewed CHECK between these steps.
    op.execute(f"LOCK TABLE {PLATFORM_SCHEMA}.life_records IN ACCESS EXCLUSIVE MODE")
    expected = OLD_CATEGORY_CHECK.replace("'", "''")
    op.execute(f"""
DO $medical_v3_guard$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_namespace
        WHERE nspname='{PLATFORM_SCHEMA}'
        AND pg_catalog.obj_description(oid,'pg_namespace')='{PLATFORM_MARKER}'
        AND pg_catalog.pg_get_userbyid(nspowner)=current_user) THEN
        RAISE EXCEPTION 'Refusing unowned platform category migration';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint k
        JOIN pg_catalog.pg_class c ON c.oid=k.conrelid
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='{PLATFORM_SCHEMA}' AND c.relname='life_records'
        AND c.relrowsecurity AND c.relforcerowsecurity
        AND pg_catalog.obj_description(c.oid,'pg_class')='{PLATFORM_MARKER}'
        AND pg_catalog.pg_get_userbyid(c.relowner)=current_user
        AND k.conname='life_records_category_check' AND k.contype='c' AND k.convalidated
        AND pg_catalog.pg_get_constraintdef(k.oid)='{expected}') THEN
        RAISE EXCEPTION 'Refusing unknown previous category constraint';
    END IF;
END;
$medical_v3_guard$;
""")
    op.execute(ALTER_CATEGORY_SQL)


def downgrade() -> None:
    raise RuntimeError(
        "Medical facts may already exist; destructive automatic downgrade is blocked."
    )
