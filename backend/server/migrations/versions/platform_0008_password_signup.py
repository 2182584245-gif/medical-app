"""Add the membership marker for ordinary accounts registered without a phone."""

from pathlib import Path

from alembic import op


revision = "platform_0008_password_signup"
down_revision = "platform_0007_mobile_11864"
branch_labels = None
depends_on = None


def upgrade() -> None:
    source = Path(__file__).resolve().parents[1] / "sql/password-signup-members-11868.sql"
    for statement in source.read_text(encoding="utf-8").split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Removing registration history would invalidate existing member logins.")
