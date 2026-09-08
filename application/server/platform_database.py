"""Server-only PostgreSQL adapter for the existing desktop business services.

Only trusted service source supplies SQL. No HTTP endpoint may accept SQL or a
database identity from a client. The application-specific write lock deliberately
serializes this small test deployment; it is not a high-throughput architecture.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.exc import DBAPIError, IntegrityError

from .platform_experience_schema import IDENTITY_TABLES, PLATFORM_COLUMNS, PLATFORM_SCHEMA


@dataclass(frozen=True)
class Identity:
    user_id: int | None = None
    role_code: str | None = None
    auth_username: str | None = None
    token_digest: str | None = None
    registering: bool = False


_ANONYMOUS = Identity()  # frozen and immutable; never mutated between requests


def _parts(sql: str) -> list[tuple[bool, str]]:
    """Split standard SQL into executable text and quoted/comment text.

    Reject ambiguous dialect constructs instead of guessing. Doubled quotes,
    quoted question marks and SQL comments are handled without substitution.
    """
    parts: list[tuple[bool, str]] = []
    start = i = 0
    while i < len(sql):
        quote = sql[i] if sql[i] in "'\"" else None
        comment = sql.startswith("--", i) or sql.startswith("/*", i)
        if not quote and not comment:
            if sql[i] == "$" or sql[i] == "\x00":
                raise ValueError("Dollar-quoted SQL and NUL are not supported")
            i += 1
            continue
        parts.append((True, sql[start:i]))
        begin = i
        if quote:
            i += 1
            while i < len(sql):
                if sql[i] == quote:
                    if i + 1 < len(sql) and sql[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            else:
                raise ValueError("Unterminated SQL literal")
            parts.append((False, sql[begin:i]))
        elif sql.startswith("--", i):
            end = sql.find("\n", i + 2)
            i = len(sql) if end < 0 else end + 1
            parts.append((False, "\n"))
        else:
            end = sql.find("*/", i + 2)
            if end < 0 or "/*" in sql[i + 2 : end]:
                raise ValueError("Unterminated or nested SQL comment")
            i = end + 2
            parts.append((False, " "))
        start = i
    parts.append((True, sql[start:]))
    return parts


def compile_sql(sql: str, parameters: Sequence[Any] = ()) -> tuple[str, tuple, bool]:
    """Qmark to psycopg conversion for one trusted, non-DDL statement.

    Return the SQL, bound parameters and whether an implicit RETURNING id was
    added. No string interpolation of a parameter occurs, including LIKE input.
    """
    if not isinstance(sql, str) or not sql.strip() or isinstance(parameters, Mapping):
        raise ValueError("Expected a SQL string and positional parameters")
    sql = sql.strip()
    if sql.endswith(";"):
        sql = sql[:-1].rstrip()
    pieces = _parts(sql)
    executable = "".join(value if plain else " " for plain, value in pieces)
    if ";" in executable:
        raise ValueError("Multiple statements are forbidden")
    verb_match = re.match(r"\s*([A-Za-z]+)", executable)
    verb = verb_match.group(1).upper() if verb_match else ""
    if verb not in {"SELECT", "INSERT", "UPDATE", "DELETE", "SET"}:
        raise ValueError("Only trusted SELECT/INSERT/UPDATE/DELETE SQL is supported")
    if verb == "SET" and not re.fullmatch(
        r"SET\s+LOCAL\s+(?:statement_timeout|lock_timeout)\s*(?:=|TO)\s*"
        r"(?:\d+|'\d+(?:ms|s)?')",
        sql,
        re.I,
    ):
        raise ValueError("Only bounded transaction timeout settings are supported")
    if re.search(r"\b(?:PRAGMA|AUTOINCREMENT|ATTACH|DETACH|VACUUM|COLLATE)\b", executable, re.I):
        raise ValueError("Unsupported SQLite-specific SQL")
    if re.search(
        r"\bINSERT\s+OR\b|\b(?:strftime|julianday|datetime|ifnull|group_concat)\s*\(",
        executable,
        re.I,
    ):
        raise ValueError("Unsupported SQLite-specific expression")
    implicit = False
    insert = re.match(r'\s*INSERT\s+INTO\s+(?:medical_app_platform\.)?"?(\w+)"?\s*\(', sql, re.I)
    if (
        verb == "INSERT"
        and insert
        and insert.group(1) in IDENTITY_TABLES
        and not re.search(r"\bRETURNING\b", executable, re.I)
    ):
        pieces.append((True, " RETURNING id"))
        implicit = True
    count = sum(value.count("?") for plain, value in pieces if plain)
    bound = tuple(parameters)
    if count != len(bound):
        raise ValueError("SQL parameter count does not match")
    compiled: list[str] = []
    for plain, value in pieces:
        # psycopg's percent lexer also reads SQL literals when parameters exist.
        value = value.replace("%", "%%")
        if plain:
            # The only SQLite function remappings supported by this adapter.
            value = re.sub(r"\binstr\s*\(", "strpos(", value, flags=re.I)
            value = value.replace("?", "%s")
        compiled.append(value)
    return "".join(compiled), bound, implicit


def _value(value: Any) -> Any:
    if isinstance(value, memoryview):
        return bytes(value)
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


class Row(Mapping[str, Any]):
    """Mapping/index row access expected by sqlite3.Row business code."""

    def __init__(self, keys: Sequence[str], values: Sequence[Any]):
        self._keys = tuple(keys)
        self._values = tuple(_value(value) for value in values)
        self._lookup = {}
        for key, value in zip(self._keys, self._values, strict=True):
            # sqlite3.Row resolves a duplicate result-column name to its first
            # occurrence. Do not silently switch a joined row to the last ID.
            self._lookup.setdefault(key, value)

    def __getitem__(self, key: str | int | slice) -> Any:
        if isinstance(key, (int, slice)):
            return self._values[key]
        return self._lookup[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)


class Cursor:
    def __init__(self, result: Any, *, implicit_id: bool = False):
        self.rowcount = result.rowcount
        self.lastrowid: int | None = None
        self._keys = tuple(result.keys()) if result.returns_rows else ()
        self._result = result
        if implicit_id:
            row = result.fetchone()
            if row is not None:
                self.lastrowid = int(row[0])

    def fetchone(self) -> Row | None:
        row = self._result.fetchone()
        return None if row is None else Row(self._keys, row)

    def fetchall(self) -> list[Row]:
        return [Row(self._keys, row) for row in self._result.fetchall()]

    def __iter__(self) -> Iterator[Row]:
        while (row := self.fetchone()) is not None:
            yield row


def _safe_error(error: DBAPIError) -> sqlite3.DatabaseError:
    # Neither SQL parameters nor connection strings/raw server errors leave here.
    cls = sqlite3.IntegrityError if isinstance(error, IntegrityError) else sqlite3.OperationalError
    result = cls(
        "Cloud database constraint rejected the operation"
        if cls is sqlite3.IntegrityError
        else "Cloud database operation could not be completed"
    )
    result.sqlstate = getattr(error.orig, "sqlstate", None)
    return result


class ServiceConnection:
    def __init__(self, connection: Connection, *, writable: bool):
        self._connection = connection
        self.writable = writable

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> Cursor:
        statement, bound, implicit = compile_sql(sql, parameters)
        if not self.writable and not re.match(r"\s*SELECT\b", statement, re.I):
            raise sqlite3.OperationalError("Writes require database.transaction()")
        try:
            result = self._connection.exec_driver_sql(statement, bound)
            return Cursor(result, implicit_id=implicit)
        except DBAPIError as error:
            raise _safe_error(error) from None

    def executemany(self, sql: str, parameters: Sequence[Sequence[Any]]) -> None:
        # Bounded business batches stay in the caller's one transaction.
        for row in parameters:
            self.execute(sql, row)


class PlatformDatabase:
    dialect = "postgresql"
    is_postgres = True
    is_cloud = True
    WRITE_LOCK = (717380021, 1)

    def __init__(self, settings_or_url: Any = None, *, engine: Engine | None = None):
        if engine is not None:
            if engine.dialect.name != "postgresql":
                raise ValueError("PlatformDatabase never falls back to SQLite")
            self.engine = engine
        else:
            value = getattr(settings_or_url, "database_url", settings_or_url)
            url = value.get_secret_value() if hasattr(value, "get_secret_value") else value
            try:
                parsed = make_url(url)
            except Exception:
                raise ValueError("A PostgreSQL connection configuration is required") from None
            if (
                parsed.drivername != "postgresql+psycopg"
                or parsed.query.get("sslmode") != "verify-full"
            ):
                raise ValueError("PlatformDatabase requires PostgreSQL psycopg and verified TLS")
            self.engine = create_engine(
                parsed,
                pool_pre_ping=True,
                pool_size=5,
                max_overflow=0,
                pool_timeout=5,
                hide_parameters=True,
                connect_args={"connect_timeout": 10, "options": "-c statement_timeout=10000"},
            )
        self._identity: ContextVar[Identity] = ContextVar(
            f"platform_identity_{id(self)}", default=_ANONYMOUS
        )
        self._active: ContextVar[ServiceConnection | None] = ContextVar(
            f"platform_connection_{id(self)}", default=None
        )
        self._initialized = False

    @property
    def path(self):
        raise RuntimeError("Cloud data has no local SQLite path; use the cloud export workflow")

    @contextmanager
    def identity(
        self,
        user_id: int | None = None,
        role_code: str | None = None,
        auth_username: str | None = None,
        token_digest: str | None = None,
        registering: bool = False,
    ):
        if user_id is not None and (
            isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0
        ):
            raise ValueError("Invalid server identity")
        if role_code not in {None, "member", "advisor", "operator"}:
            raise ValueError("Invalid server role")
        if token_digest is not None and not re.fullmatch(r"[0-9a-f]{64}", token_digest):
            raise ValueError("Invalid token digest")
        if auth_username is not None and (
            not isinstance(auth_username, str) or not 1 <= len(auth_username) <= 100
        ):
            raise ValueError("Invalid normalized username")
        token = self._identity.set(
            Identity(user_id, role_code, auth_username, token_digest, registering)
        )
        active = self._active.get()
        try:
            if active is not None:
                self._apply_identity(active._connection)
            yield self
        finally:
            self._identity.reset(token)
            # Do not obscure the original DB failure if its transaction aborted.
            if active is not None and active._connection.in_transaction():
                with suppress(DBAPIError):
                    self._apply_identity(active._connection)

    def _apply_identity(self, connection: Connection) -> None:
        ident = self._identity.get()
        settings = (
            ("medical_app.user_id", str(ident.user_id or "")),
            ("medical_app.role_code", ident.role_code or ""),
            ("medical_app.auth_username", ident.auth_username or ""),
            ("medical_app.token_digest", ident.token_digest or ""),
            ("medical_app.registering", "true" if ident.registering else "false"),
        )
        # One bound statement installs all five transaction-local values. This
        # preserves explicit clearing on every request without five network trips.
        connection.exec_driver_sql(
            "SELECT " + ", ".join("pg_catalog.set_config(%s, %s, true)" for _ in settings),
            tuple(part for pair in settings for part in pair),
        )

    def _prepare(self, connection: Connection, *, writable: bool) -> None:
        if not writable:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        connection.exec_driver_sql(f"SET LOCAL search_path = {PLATFORM_SCHEMA}, pg_catalog")
        connection.exec_driver_sql("SET LOCAL statement_timeout = '10000ms'")
        connection.exec_driver_sql("SET LOCAL lock_timeout = '5000ms'")
        self._apply_identity(connection)
        if writable:
            connection.exec_driver_sql(
                "SELECT pg_catalog.pg_advisory_xact_lock(%s, %s)", self.WRITE_LOCK
            )

    @contextmanager
    def connect(self):
        active = self._active.get()
        if active is not None:
            yield active
            return
        try:
            with self.engine.connect() as connection, connection.begin():
                self._prepare(connection, writable=False)
                wrapper = ServiceConnection(connection, writable=False)
                token = self._active.set(wrapper)
                try:
                    yield wrapper
                finally:
                    self._active.reset(token)
        except DBAPIError as error:
            raise _safe_error(error) from None

    @contextmanager
    def transaction(self, *, immediate: bool = True):
        # Both modes serialize writes; do not silently weaken the existing
        # BEGIN IMMEDIATE race guarantees when a caller supplies immediate=False.
        active = self._active.get()
        try:
            if active is not None:
                if not active.writable:
                    raise sqlite3.OperationalError("Cannot upgrade a read-only transaction")
                with active._connection.begin_nested():
                    self._apply_identity(active._connection)
                    yield active
                return
            with self.engine.connect() as connection, connection.begin():
                self._prepare(connection, writable=True)
                wrapper = ServiceConnection(connection, writable=True)
                token = self._active.set(wrapper)
                try:
                    yield wrapper
                finally:
                    self._active.reset(token)
        except DBAPIError as error:
            raise _safe_error(error) from None

    def initialize(self, *, force: bool = False) -> None:
        """Read-only shape verification; migrations run solely under admin control."""
        if self._initialized and not force:
            return
        with self.connect() as connection:
            for table, columns in PLATFORM_COLUMNS.items():
                column_sql = ", ".join(f'"{column}"' for column in columns)
                connection.execute(f'SELECT {column_sql} FROM {PLATFORM_SCHEMA}."{table}" LIMIT 0')
        self._initialized = True

    def close(self) -> None:
        self.engine.dispose()

    dispose = close
