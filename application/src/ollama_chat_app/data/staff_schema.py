"""Additive staff workflow schema; invoked by the central database migration."""

from __future__ import annotations

import sqlite3


def migrate_staff(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS staff_account_terms (
            user_id INTEGER PRIMARY KEY,
            starts_at TEXT NOT NULL,
            ends_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            CHECK (starts_at < ends_at)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS visit_task_details (
            task_id INTEGER PRIMARY KEY,
            service_type TEXT NOT NULL,
            address TEXT,
            latitude REAL,
            longitude REAL,
            outcome_status TEXT CHECK (outcome_status IN ('incomplete', 'disabled')),
            requested_by INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES visit_tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (requested_by) REFERENCES users(id) ON DELETE SET NULL,
            CHECK (latitude IS NULL OR (latitude >= -90 AND latitude <= 90)),
            CHECK (longitude IS NULL OR (longitude >= -180 AND longitude <= 180))
        )
        """
    )
