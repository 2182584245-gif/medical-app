"""Explicit developer grants, step-up sessions, and immutable settings revisions."""

from alembic import op

from server.platform_developer_schema import NEW_TABLES, TABLE_DDL, grant_ddl, policy_ddl
from server.platform_schema import PLATFORM_MARKER, PLATFORM_SCHEMA

revision = "platform_0004"
down_revision = "platform_0003"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute(f"""
DO $developer_v4_guard$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_namespace
        WHERE nspname='{PLATFORM_SCHEMA}'
        AND pg_catalog.obj_description(oid,'pg_namespace')='{PLATFORM_MARKER}'
        AND pg_catalog.pg_get_userbyid(nspowner)=current_user) THEN
        RAISE EXCEPTION 'Refusing unowned developer migration';
    END IF;
END;
$developer_v4_guard$;
""")
    for statement in TABLE_DDL:
        op.execute(statement)
    for table in NEW_TABLES:
        op.execute(f"COMMENT ON TABLE {PLATFORM_SCHEMA}.{table} IS '{PLATFORM_MARKER}'")
    for statement in policy_ddl():
        op.execute(statement)
    # An already provisioned v3 runtime receives only the reviewed additions.
    # Fresh installations can migrate before their separate role provisioning.
    for statement in grant_ddl():
        op.execute(
            "DO $developer_grants$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles "
            "WHERE rolname='medical_app_platform_runtime') THEN "
            + statement
            + "; END IF; END; $developer_grants$;"
        )


def downgrade():
    raise RuntimeError("Automatic developer downgrade is blocked; preserve published settings.")
