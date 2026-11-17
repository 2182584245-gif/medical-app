"""Additive member shopping state; called by the central versioned migration."""

from __future__ import annotations

import sqlite3


def initialize_member_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS member_cart (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
            quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 999),
            PRIMARY KEY(user_id, product_id)
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS member_favorites (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
            PRIMARY KEY(user_id, product_id)
        )
    """)
