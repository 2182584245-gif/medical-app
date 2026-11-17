"""Additive desktop schema v6; existing records and secrets are not replaced."""
from __future__ import annotations


def migrate_experience(connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS user_preferences (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            preferences_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(preferences_json)),
            updated_at TEXT NOT NULL
        )
    """)
    from .member_schema import initialize_member_schema
    from .staff_schema import migrate_staff
    initialize_member_schema(connection)
    migrate_staff(connection)
