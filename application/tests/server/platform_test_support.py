"""Synthetic-only integration fixture; no PostgreSQL/RLS claims are made here."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from threading import RLock

from ollama_chat_app.data.database import Database, timestamp_to_db, utc_now
from ollama_chat_app.services.auth import AuthService

PASSWORD = "synthetic-only-password-1234"
PEPPER = bytes(range(32)).hex()  # deterministic synthetic value, never production


class SyntheticDatabase:
    dialect = "sqlite"

    def __init__(self):
        self.connection = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        Database._create_schema_v5(self.connection)
        from ollama_chat_app.data.experience_schema import migrate_experience
        migrate_experience(self.connection)
        self.connection.execute(
            "CREATE TABLE platform_sessions (token_digest TEXT PRIMARY KEY, "
            "user_id INTEGER REFERENCES users(id), created_at TEXT, "
            "expires_at TEXT, revoked_at TEXT)"
        )
        self.connection.execute(
            "CREATE TABLE auth_rate_buckets (bucket_id INTEGER PRIMARY KEY, "
            "window_start INTEGER NOT NULL, attempts INTEGER NOT NULL)"
        )
        self.connection.execute(
            "CREATE TABLE rpc_requests (user_id INTEGER REFERENCES users(id), "
            "request_id TEXT, request_digest TEXT, response_json TEXT, "
            "created_at TEXT, PRIMARY KEY(user_id,request_id))"
        )
        self.lock = RLock()
        self.depth = 0
        self.initialize_calls = []

    def initialize(self, *, force=False):
        self.initialize_calls.append(force)

    @contextmanager
    def connect(self):
        with self.lock:
            yield self.connection

    @contextmanager
    def transaction(self, *, immediate=True):
        with self.lock:
            depth = self.depth
            self.depth += 1
            self.connection.execute(
                "BEGIN IMMEDIATE" if depth == 0 else f"SAVEPOINT synthetic_{depth}"
            )
            try:
                yield self.connection
            except BaseException:
                self.connection.execute(
                    "ROLLBACK" if depth == 0 else f"ROLLBACK TO synthetic_{depth}"
                )
                if depth:
                    self.connection.execute(f"RELEASE synthetic_{depth}")
                raise
            else:
                self.connection.execute("COMMIT" if depth == 0 else f"RELEASE synthetic_{depth}")
            finally:
                self.depth -= 1

    @contextmanager
    def identity(self, **_values):
        # Explicitly no RLS simulation: real enforcement needs the live PG suite.
        yield self

    def scalar(self, sql, parameters=()):
        return self.connection.execute(sql, parameters).fetchone()[0]

    def account(self, name, role="member"):
        display, normalized, password_hash = AuthService._prepare_new_account(name, PASSWORD)
        with self.transaction() as connection:
            return int(
                AuthService._insert_account(
                    connection,
                    display_username=display,
                    normalized_username=normalized,
                    password_hash=password_hash,
                    role_code=role,
                    now=timestamp_to_db(utc_now()),
                )["id"]
            )

    def dispose(self):
        pass  # caller retains the synthetic connection to assert post-request state

    def close(self):
        self.connection.close()
