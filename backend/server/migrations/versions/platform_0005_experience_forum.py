"""Add the shared member experience forum without changing existing records."""

from alembic import op

from server.platform_forum_schema import FORUM_TABLES, INDEX_DDL, TABLE_DDL, grant_ddl, policy_ddl
from server.platform_schema import PLATFORM_MARKER, PLATFORM_SCHEMA

revision = "platform_0005"
down_revision = "platform_0004"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute(f"""
DO $forum_v5_guard$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_namespace
        WHERE nspname='{PLATFORM_SCHEMA}'
        AND pg_catalog.obj_description(oid,'pg_namespace')='{PLATFORM_MARKER}'
        AND pg_catalog.pg_get_userbyid(nspowner)=current_user) THEN
        RAISE EXCEPTION 'Refusing unowned forum migration';
    END IF;
END;
$forum_v5_guard$;
""")
    for statement in TABLE_DDL:
        op.execute(statement)
    for table in FORUM_TABLES:
        op.execute(f"COMMENT ON TABLE {PLATFORM_SCHEMA}.{table} IS '{PLATFORM_MARKER}'")
    for statement in INDEX_DDL:
        op.execute(statement)
    for statement in policy_ddl():
        op.execute(statement)
    for topic in (("hypertension", "高血压专区"), ("cholesterol", "高胆固醇专区"), ("glucose", "高血糖专区")):
        slug, name = topic
        safe_slug = slug.replace("'", "''")
        safe_name = name.replace("'", "''")
        op.execute(
            f"INSERT INTO {PLATFORM_SCHEMA}.forum_topics(slug,name,created_at,updated_at) "
            f"VALUES ('{safe_slug}','{safe_name}',statement_timestamp()::text,statement_timestamp()::text) "
            "ON CONFLICT (slug) DO NOTHING"
        )
    for statement in grant_ddl():
        op.execute(
            "DO $forum_grants$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles "
            "WHERE rolname='medical_app_platform_runtime') THEN "
            + statement
            + "; END IF; END; $forum_grants$;"
        )


def downgrade():
    raise RuntimeError("Automatic forum downgrade is blocked; preserve member posts.")
