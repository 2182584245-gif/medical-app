from __future__ import annotations

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import Settings
from .models import Base
from .schema import PRIVATE_SCHEMA


class Database:
    def __init__(self, settings: Settings) -> None:
        url = settings.database_url.get_secret_value()
        options: dict = {"pool_pre_ping": True, "hide_parameters": True}
        if url.startswith("sqlite+pysqlite:"):
            options["execution_options"] = {"schema_translate_map": {PRIVATE_SCHEMA: None}}
            options["connect_args"] = {"check_same_thread": False, "timeout": 5}
            if url.endswith(":memory:"):
                options["poolclass"] = StaticPool
        else:
            options["connect_args"] = {
                # The verified Windows -> Session pooler TLS/login handshake can
                # take about 7 seconds. Keep a bounded margin, without relaxing
                # certificate verification or the separate 5-second SQL limit.
                "connect_timeout": 10,
                "options": "-c statement_timeout=5000",
            }
            options["pool_size"] = 5
            options["max_overflow"] = 5
            options["pool_timeout"] = 5
        self.engine = create_engine(url, **options)
        if self.engine.dialect.name == "sqlite":

            @event.listens_for(self.engine, "connect")
            def enable_foreign_keys(connection, _record):
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys = ON")
                cursor.close()

        self.sessions = sessionmaker(self.engine, class_=Session, expire_on_commit=False)

    def check_ready(self) -> None:
        with self.engine.connect() as connection:
            # Every expected column must exist; SELECT 1 alone misses absent tables.
            for table in Base.metadata.sorted_tables:
                connection.execute(select(table).limit(0))


def initialize_development_database(settings: Settings) -> None:
    if settings.env != "development":
        raise RuntimeError("schema initialization is allowed only in explicit development mode")
    if not settings.database_url.get_secret_value().startswith("sqlite+pysqlite:"):
        raise RuntimeError(
            "init-db is SQLite-development-only; PostgreSQL requires reviewed Alembic migrations"
        )
    database = Database(settings)
    try:
        Base.metadata.create_all(database.engine)
    finally:
        database.engine.dispose()
