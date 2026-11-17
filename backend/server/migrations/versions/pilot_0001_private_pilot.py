"""Initial private four-table pilot; NOT a full desktop-data migration.

Revision ID: pilot_0001
Revises: None
"""

import sqlalchemy as sa
from alembic import op

from server.migrations.guards import REVISION_MARKER, objects_guard_sql, ownership_sql
from server.schema import PRIVATE_SCHEMA

revision = "pilot_0001"
down_revision = None
branch_labels = None
depends_on = None

TABLE_COLUMNS = {
    "alembic_version": ("version_num",),
    "pilot_users": (
        "id",
        "username",
        "username_normalized",
        "password_hash",
        "active",
        "created_at",
    ),
    "pilot_login_sessions": (
        "id",
        "user_id",
        "token_digest",
        "created_at",
        "expires_at",
        "revoked_at",
    ),
    "pilot_conversations": (
        "id",
        "user_id",
        "request_id",
        "request_digest",
        "title",
        "next_sequence",
        "created_at",
        "updated_at",
    ),
    "pilot_messages": (
        "id",
        "conversation_id",
        "request_id",
        "request_digest",
        "sequence_no",
        "role",
        "content",
        "provider",
        "model",
        "created_at",
    ),
}
INDEXES = (
    "alembic_version_pkc",
    "pk_pilot_users",
    "uq_pilot_users_username_normalized",
    "pk_pilot_login_sessions",
    "uq_pilot_login_sessions_token_digest",
    "ix_pilot_sessions_user_expiry",
    "pk_pilot_conversations",
    "uq_pilot_conversation_request",
    "ix_pilot_conversations_user_date",
    "pk_pilot_messages",
    "uq_pilot_message_request",
    "uq_pilot_message_sequence",
)
CONSTRAINTS = (
    "alembic_version_pkc",
    "pk_pilot_users",
    "uq_pilot_users_username_normalized",
    "ck_pilot_users_username",
    "pk_pilot_login_sessions",
    "uq_pilot_login_sessions_token_digest",
    "fk_pilot_login_sessions_user_id_pilot_users",
    "pk_pilot_conversations",
    "uq_pilot_conversation_request",
    "fk_pilot_conversations_user_id_pilot_users",
    "ck_pilot_conversations_title",
    "ck_pilot_conversations_next_sequence",
    "pk_pilot_messages",
    "uq_pilot_message_request",
    "uq_pilot_message_sequence",
    "fk_pilot_messages_conversation_id_pilot_conversations",
    "ck_pilot_messages_sequence",
    "ck_pilot_messages_role",
    "ck_pilot_messages_content",
)


def upgrade() -> None:
    # Alembic creates its version table before this revision. No existing business
    # tables, extra schema objects or existing data may be adopted here.
    op.execute(
        objects_guard_sql(
            tables={"alembic_version": ("version_num",)},
            indexes=("alembic_version_pkc",),
            constraints=("alembic_version_pkc",),
        )
    )
    op.execute(f"COMMENT ON TABLE {PRIVATE_SCHEMA}.alembic_version IS '{REVISION_MARKER}'")
    op.create_table(
        "pilot_users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(50), nullable=False),
        sa.Column("username_normalized", sa.String(100), nullable=False),
        sa.Column("password_hash", sa.String(512), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_pilot_users"),
        sa.UniqueConstraint("username_normalized", name="uq_pilot_users_username_normalized"),
        sa.CheckConstraint(
            "length(username_normalized) BETWEEN 2 AND 100", name="ck_pilot_users_username"
        ),
        schema=PRIVATE_SCHEMA,
    )
    op.create_table(
        "pilot_login_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_pilot_login_sessions"),
        sa.UniqueConstraint("token_digest", name="uq_pilot_login_sessions_token_digest"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [f"{PRIVATE_SCHEMA}.pilot_users.id"],
            name="fk_pilot_login_sessions_user_id_pilot_users",
            ondelete="CASCADE",
        ),
        schema=PRIVATE_SCHEMA,
    )
    op.create_index(
        "ix_pilot_sessions_user_expiry",
        "pilot_login_sessions",
        ["user_id", "expires_at"],
        schema=PRIVATE_SCHEMA,
    )
    op.create_table(
        "pilot_conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("title", sa.String(100), nullable=False),
        sa.Column("next_sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_pilot_conversations"),
        sa.UniqueConstraint("user_id", "request_id", name="uq_pilot_conversation_request"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [f"{PRIVATE_SCHEMA}.pilot_users.id"],
            name="fk_pilot_conversations_user_id_pilot_users",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("length(title) BETWEEN 1 AND 100", name="ck_pilot_conversations_title"),
        sa.CheckConstraint("next_sequence > 0", name="ck_pilot_conversations_next_sequence"),
        schema=PRIVATE_SCHEMA,
    )
    op.create_index(
        "ix_pilot_conversations_user_date",
        "pilot_conversations",
        ["user_id", "created_at", "id"],
        schema=PRIVATE_SCHEMA,
    )
    op.create_table(
        "pilot_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("provider", sa.String(100), nullable=True),
        sa.Column("model", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_pilot_messages"),
        sa.UniqueConstraint("conversation_id", "request_id", name="uq_pilot_message_request"),
        sa.UniqueConstraint("conversation_id", "sequence_no", name="uq_pilot_message_sequence"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            [f"{PRIVATE_SCHEMA}.pilot_conversations.id"],
            name="fk_pilot_messages_conversation_id_pilot_conversations",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("sequence_no > 0", name="ck_pilot_messages_sequence"),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="ck_pilot_messages_role"),
        sa.CheckConstraint("length(content) BETWEEN 1 AND 50000", name="ck_pilot_messages_content"),
        schema=PRIVATE_SCHEMA,
    )
    for name in TABLE_COLUMNS:
        if name != "alembic_version":
            op.execute(f"COMMENT ON TABLE {PRIVATE_SCHEMA}.{name} IS '{REVISION_MARKER}'")


def downgrade() -> None:
    op.execute(ownership_sql())
    # Never mistake rows hidden by an unexpected RLS configuration for emptiness.
    op.execute("SET LOCAL row_security = off")
    business_tables = tuple(name for name in TABLE_COLUMNS if name != "alembic_version")
    # Prevent writes between the emptiness check and DROP; never CASCADE DDL.
    op.execute(
        "LOCK TABLE "
        + ", ".join(f"{PRIVATE_SCHEMA}.{name}" for name in TABLE_COLUMNS)
        + " IN ACCESS EXCLUSIVE MODE NOWAIT"
    )
    op.execute(objects_guard_sql(tables=TABLE_COLUMNS, indexes=INDEXES, constraints=CONSTRAINTS))
    for name in business_tables:
        op.execute(f"""
DO $medical_empty$
BEGIN
    IF pg_catalog.obj_description('{PRIVATE_SCHEMA}.{name}'::regclass, 'pg_class')
       IS DISTINCT FROM '{REVISION_MARKER}' THEN
        RAISE EXCEPTION 'Refusing unowned pilot table {name}';
    END IF;
    IF EXISTS (SELECT 1 FROM {PRIVATE_SCHEMA}.{name} LIMIT 1) THEN
        RAISE EXCEPTION 'Refusing downgrade: business table {name} is not empty';
    END IF;
END;
$medical_empty$;
""")
    for name in ("pilot_messages", "pilot_login_sessions", "pilot_conversations", "pilot_users"):
        op.execute(f"DROP TABLE {PRIVATE_SCHEMA}.{name} RESTRICT")
    # Intentionally retain owned schema + empty version table. Never drop the
    # schema or use CASCADE; Alembic removes only this revision's version row.
