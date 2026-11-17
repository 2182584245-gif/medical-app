"""Install the reviewed PostgreSQL structures required by mobile version 11864."""

from alembic import op

from server import (
    account_reset_schema11831,
    conversation_schema11844,
    forum_detail_schema,
    guest_ai_schema,
    multimodal_schema11842,
    phone_auth_schema,
    phone_credentials_schema11838,
    profile_try1_schema,
    reading_social_schema,
)
from server.migrations.member_memory_11864 import upgrade_member_memory
from server.platform_experience_schema import runtime_grant_ddl
from server.platform_schema import (
    PLATFORM_MARKER,
    PLATFORM_RUNTIME_ROLE,
    PLATFORM_SCHEMA,
)

revision = "platform_0007_mobile_11864"
down_revision = "platform_0006_fawnsee_expansion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.execute(
        f"""
        DO $mobile_11864_preflight$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_catalog.pg_namespace
                WHERE nspname = '{PLATFORM_SCHEMA}'
                  AND pg_catalog.obj_description(oid, 'pg_namespace') = '{PLATFORM_MARKER}'
                  AND pg_catalog.pg_get_userbyid(nspowner) = current_user
            ) OR NOT EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles
                WHERE rolname = '{PLATFORM_RUNTIME_ROLE}'
                  AND rolname <> current_user AND NOT rolsuper AND NOT rolbypassrls
            ) THEN
                RAISE EXCEPTION 'Platform owner and restricted runtime role required';
            END IF;
        END;
        $mobile_11864_preflight$;
        """
    )
    for statement in runtime_grant_ddl():
        op.execute(statement)

    op.execute(
        f"ALTER TABLE {PLATFORM_SCHEMA}.member_profiles "
        "ADD COLUMN IF NOT EXISTS age INTEGER CHECK (age IS NULL OR age BETWEEN 1 AND 120)"
    )
    upgrade_member_memory(op.execute)
    for build_statements in (
        phone_auth_schema.postgres_statements,
        phone_credentials_schema11838.postgres_statements,
        reading_social_schema.postgres_statements,
        forum_detail_schema.postgres_statements,
        guest_ai_schema.postgres_statements,
        account_reset_schema11831.postgres_statements,
        profile_try1_schema.postgres_statements,
        multimodal_schema11842.postgres_statements,
        conversation_schema11844.postgres_statements,
    ):
        for statement in build_statements():
            op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Automatic mobile schema downgrade is blocked to preserve member data.")
