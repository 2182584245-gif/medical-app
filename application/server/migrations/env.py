"""Offline SQL is the default deliverable; online changes require explicit opt-in."""

from __future__ import annotations

import os

import psycopg
from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError

from server.cloud_connection import CloudConfigurationError, ca_file, project_url, validated_url
from server.migrations.guards import bootstrap_sql, objects_guard_sql, ownership_sql, revoke_sql
from server.migrations.versions.pilot_0001_private_pilot import CONSTRAINTS, INDEXES, TABLE_COLUMNS
from server.models import Base
from server.schema import PRIVATE_SCHEMA


def migration_url() -> URL:
    """Never fall back to the backend's lower-privilege runtime credentials."""
    if os.environ.get("SERVER_MIGRATION_CONFIRM") != PRIVATE_SCHEMA:
        raise RuntimeError(
            "Online migration requires SERVER_MIGRATION_CONFIRM=medical_app_private; "
            "review the target and SQL first. No connection was attempted."
        )
    raw = os.environ.get("SERVER_MIGRATION_DATABASE_URL", "")
    try:
        url = validated_url(raw, project_url())
    except CloudConfigurationError:
        raise RuntimeError(
            "A separate migration URL for the configured project is required."
        ) from None
    if (
        url.drivername != "postgresql+psycopg"
        or not all((url.host, url.database, url.username, url.password))
        or url.query.get("sslmode") != "verify-full"
        or not url.query.get("sslrootcert")
    ):
        raise RuntimeError(
            "Migration requires PostgreSQL/psycopg, explicit credentials, "
            "sslmode=verify-full and sslrootcert. Connection details are not logged."
        )
    try:
        certificate = ca_file(url.query["sslrootcert"])
    except CloudConfigurationError:
        raise RuntimeError(
            "Migration root certificate is invalid; no connection attempted."
        ) from None
    return url.update_query_dict({"sslrootcert": certificate})


def configure(**kwargs) -> None:
    context.configure(
        target_metadata=Base.metadata,
        version_table_schema=PRIVATE_SCHEMA,
        include_schemas=True,
        transactional_ddl=True,
        **kwargs,
    )


def run() -> None:
    target = context.get_revision_argument() or "base"
    if target not in {"head", "pilot_0001", "platform_0001", "base"}:
        raise RuntimeError("This pilot environment supports reviewed upgrade/downgrade only.")
    command = getattr(context.config, "cmd_opts", None)
    command_tuple = getattr(command, "cmd", None)
    if command_tuple and command_tuple[0].__name__ not in {"upgrade", "downgrade"}:
        raise RuntimeError("Stamp/autogenerate cannot bypass the reviewed pilot migration.")

    def run_steps() -> None:
        context.execute("SET LOCAL lock_timeout = '5s'")
        context.execute("SET LOCAL statement_timeout = '30s'")
        context.execute("SET LOCAL search_path = pg_catalog")
        context.execute("SELECT pg_catalog.pg_advisory_xact_lock(73124605106327)")
        context.execute(ownership_sql() if target == "base" else bootstrap_sql())
        context.execute(revoke_sql(include_tables=False))
        context.run_migrations()
        context.execute(
            objects_guard_sql(
                tables={"alembic_version": ("version_num",)} if target == "base" else TABLE_COLUMNS,
                indexes=("alembic_version_pkc",) if target == "base" else INDEXES,
                constraints=("alembic_version_pkc",) if target == "base" else CONSTRAINTS,
            )
        )
        context.execute(revoke_sql(include_tables=True))

    if context.is_offline_mode():
        configure(
            dialect_name="postgresql", dialect_opts={"paramstyle": "named"}, literal_binds=True
        )
        with context.begin_transaction():
            run_steps()
    else:
        if (
            target in {"head", "platform_0001"}
            and os.environ.get("SERVER_PLATFORM_CONFIRM") != "medical_app_platform"
        ):
            raise RuntimeError(
                "Full platform migration requires separate medical_app_platform confirmation."
            )
        url = migration_url()
        engine = None
        try:
            engine = create_engine(
                url,
                poolclass=pool.NullPool,
                hide_parameters=True,
                connect_args={"connect_timeout": 10},
            )
            with engine.connect() as connection, connection.begin():
                configure(connection=connection)
                run_steps()
        except (SQLAlchemyError, psycopg.Error):
            raise RuntimeError(
                "Migration did not report success; final commit status may be unknown. "
                "Recheck schema and revision read-only before retrying. "
                "Connection details and database error text are hidden; "
                "verify target, TLS, privileges and pilot ownership with the administrator."
            ) from None
        finally:
            if engine is not None:
                try:
                    engine.dispose()
                except (SQLAlchemyError, psycopg.Error):
                    raise RuntimeError(
                        "Migration connection cleanup failed; details hidden."
                    ) from None


run()
