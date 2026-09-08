"""Initialize only the isolated, disposable local Compose PostgreSQL database.

This module never imports cloud administration, DPAPI, application configuration,
or local user data. The fixed endpoint, private CA, independent server/database
markers and explicit gate must all pass before the first modifying statement.
Re-running verifies an owned installation; it does not repair or adopt objects.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
from pathlib import Path

import psycopg
from psycopg import sql

from server.platform_schema import (
    INDEX_DDL,
    PLATFORM_COLUMNS,
    PLATFORM_MARKER,
    PLATFORM_RUNTIME_ROLE,
    PLATFORM_SCHEMA,
    TABLE_DDL,
    compatibility_constraints,
    policy_ddl,
    policy_rules,
    revoke_ddl,
    runtime_grant_ddl,
)

from .guard import require_local_container

DATABASE = "medical_app_local"
ADMIN = "local_bootstrap"
SERVER_MARKER = "medical-app-local-v1"
DATABASE_MARKER = "medical-app:local-container:v1"
ROLE_MARKER = "medical-app:local-runtime:v1"
ADMIN_PASSWORD = Path("/run/db-secrets/postgres-password")
RUNTIME_PASSWORD = Path("/run/api-secrets/database-password")
CA_CERTIFICATE = Path("/run/api-secrets/ca.crt")
PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


class LocalBootstrapError(RuntimeError):
    """A safe diagnostic that never includes database errors or credentials."""


def _gate() -> None:
    if os.environ.get("LOCAL_CONTAINER_ONLY") != "1":
        raise LocalBootstrapError("Local-container confirmation is missing; nothing connected.")
    # libpq also reads PGHOSTADDR/PGSERVICE/PGOPTIONS even with explicit host/user.
    # Reject that entire source rather than accidentally inheriting cloud routing.
    if any(name.upper().startswith("PG") for name in os.environ):
        raise LocalBootstrapError("Inherited PostgreSQL environment settings are forbidden.")
    try:
        require_local_container()
    except RuntimeError:
        raise LocalBootstrapError(
            "Only isolated Linux Docker without cloud environment settings is permitted."
        ) from None


def _read_password(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 128:
        raise LocalBootstrapError("A fixed local secret file is missing or invalid.")
    value = path.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[a-f0-9]{64}", value):
        raise LocalBootstrapError("Local passwords must be generated 32-byte hex secrets.")
    return value


def _connect(user: str, password: str):
    _gate()
    if user not in {ADMIN, PLATFORM_RUNTIME_ROLE}:
        raise LocalBootstrapError("Unexpected local database role.")
    if CA_CERTIFICATE.is_symlink() or not CA_CERTIFICATE.is_file():
        raise LocalBootstrapError("The explicit local database CA file is missing.")
    return psycopg.connect(
        host="postgres",
        port=5432,
        dbname=DATABASE,
        user=user,
        password=password,
        sslmode="verify-full",
        sslrootcert=str(CA_CERTIFICATE),
        connect_timeout=10,
        application_name="medical-app-local-bootstrap",
    )


def _check_target(connection, user: str) -> None:
    if not connection.pgconn.ssl_in_use:
        raise LocalBootstrapError("The isolated local database must use verified TLS.")
    row = connection.execute(
        "SELECT current_database(), current_user, session_user, "
        "current_setting('medical_app.local_container', true), "
        "pg_catalog.shobj_description(d.oid,'pg_database'), "
        "pg_catalog.pg_get_userbyid(d.datdba), pg_catalog.host(inet_server_addr()), "
        "current_setting('password_encryption') "
        "FROM pg_catalog.pg_database d WHERE d.datname=current_database()"
    ).fetchone()
    if (
        row is None
        or tuple(row[:6]) != (DATABASE, user, user, SERVER_MARKER, DATABASE_MARKER, ADMIN)
        or row[7] != "scram-sha-256"
    ):
        raise LocalBootstrapError("Local server/database identity markers do not match.")
    try:
        address = ipaddress.ip_address(row[6])
    except ValueError:
        raise LocalBootstrapError(
            "The database address is not an isolated network address."
        ) from None
    if not address.is_private or address.is_loopback or address.is_unspecified:
        raise LocalBootstrapError("The database address is not an isolated network address.")


def _schema_row(connection):
    return connection.execute(
        "SELECT pg_catalog.obj_description(oid,'pg_namespace'), "
        "pg_catalog.pg_get_userbyid(nspowner) FROM pg_catalog.pg_namespace WHERE nspname=%s",
        (PLATFORM_SCHEMA,),
    ).fetchone()


def _role_row(connection):
    return connection.execute(
        "SELECT pg_catalog.shobj_description(r.oid,'pg_authid'), r.rolcanlogin, "
        "r.rolsuper,r.rolcreatedb,r.rolcreaterole,r.rolreplication,r.rolbypassrls, "
        "r.rolinherit,r.rolconnlimit, EXISTS(SELECT 1 FROM pg_catalog.pg_auth_members m "
        "WHERE m.member=r.oid OR m.roleid=r.oid) "
        "FROM pg_catalog.pg_roles r WHERE r.rolname=%s",
        (PLATFORM_RUNTIME_ROLE,),
    ).fetchone()


def _ddl_digest() -> str:
    statements = (
        *TABLE_DDL,
        *INDEX_DDL,
        *compatibility_constraints(),
        *policy_ddl(),
        revoke_ddl(),
        *runtime_grant_ddl(),
    )
    return hashlib.sha256("\n".join(statements).encode()).hexdigest()


def _catalog_digest(connection) -> str:
    """Fingerprint metadata only, including exact RLS expressions and grants.

    Save this first-install fingerprint in the owned role comment. This detects
    later drift without rewriting policies or querying any user/chat/file rows.
    OIDs, sequence counters and passwords are deliberately absent.
    """
    queries = (
        "SELECT c.relname,c.relkind,pg_catalog.pg_get_userbyid(c.relowner), "
        "c.relrowsecurity,c.relforcerowsecurity, "
        "ARRAY(SELECT a::text FROM unnest(c.relacl) a ORDER BY a::text), "
        "pg_catalog.obj_description(c.oid,'pg_class') "
        "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s ORDER BY c.relname,c.relkind",
        "SELECT c.relname,a.attname,pg_catalog.format_type(a.atttypid,a.atttypmod), "
        "a.attnotnull,a.attidentity,a.attgenerated,pg_catalog.pg_get_expr(d.adbin,d.adrelid) "
        "FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid=a.attrelid "
        "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
        "WHERE n.nspname=%s AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped "
        "ORDER BY c.relname,a.attnum",
        "SELECT c.relname,k.conname,k.contype,k.convalidated, "
        "pg_catalog.pg_get_constraintdef(k.oid) FROM pg_catalog.pg_constraint k "
        "JOIN pg_catalog.pg_class c ON c.oid=k.conrelid "
        "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s ORDER BY c.relname,k.conname",
        "SELECT c.relname,p.polname,p.polcmd,p.polpermissive, "
        "ARRAY(SELECT CASE WHEN r=0 THEN 'PUBLIC' ELSE pg_catalog.pg_get_userbyid(r) END "
        "FROM unnest(p.polroles) r ORDER BY 1), "
        "pg_catalog.pg_get_expr(p.polqual,p.polrelid), "
        "pg_catalog.pg_get_expr(p.polwithcheck,p.polrelid) "
        "FROM pg_catalog.pg_policy p JOIN pg_catalog.pg_class c ON c.oid=p.polrelid "
        "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s ORDER BY c.relname,p.polname",
        "SELECT indexname,indexdef FROM pg_catalog.pg_indexes "
        "WHERE schemaname=%s ORDER BY indexname",
        "SELECT pg_catalog.pg_get_userbyid(a.defaclrole),a.defaclobjtype, "
        "ARRAY(SELECT v::text FROM unnest(a.defaclacl) v ORDER BY v::text) "
        "FROM pg_catalog.pg_default_acl a JOIN pg_catalog.pg_namespace n "
        "ON n.oid=a.defaclnamespace WHERE n.nspname=%s ORDER BY 1,2",
        "SELECT ARRAY(SELECT a::text FROM unnest(n.nspacl) a ORDER BY a::text) "
        "FROM pg_catalog.pg_namespace n WHERE n.nspname=%s",
    )
    metadata = [connection.execute(query, (PLATFORM_SCHEMA,)).fetchall() for query in queries]
    return hashlib.sha256(json.dumps(metadata, separators=(",", ":")).encode()).hexdigest()


def _verify_installation(connection) -> None:
    if _schema_row(connection) != (PLATFORM_MARKER, ADMIN):
        raise LocalBootstrapError("The existing schema is not owned by this local bootstrap.")
    role = _role_row(connection)
    if role is None or tuple(role[1:]) != (
        True,
        False,
        False,
        False,
        False,
        False,
        False,
        5,
        False,
    ):
        raise LocalBootstrapError(
            "The local runtime role has unexpected privileges or memberships."
        )
    tables = connection.execute(
        "SELECT c.relname,pg_catalog.obj_description(c.oid,'pg_class'), "
        "pg_catalog.pg_get_userbyid(c.relowner),c.relrowsecurity,c.relforcerowsecurity, "
        "ARRAY(SELECT a.attname::text FROM pg_catalog.pg_attribute a "
        "WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum) "
        "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s AND c.relkind='r' ORDER BY c.relname",
        (PLATFORM_SCHEMA,),
    ).fetchall()
    if {row[0] for row in tables} != set(PLATFORM_COLUMNS) or any(
        tuple(row[1:5]) != (PLATFORM_MARKER, ADMIN, True, True)
        or tuple(row[5]) != PLATFORM_COLUMNS[row[0]]
        for row in tables
    ):
        raise LocalBootstrapError("The local table set, columns, ownership or forced RLS differs.")
    matrix = connection.execute(
        "SELECT c.relname,p.privilege,has_table_privilege(%s,c.oid,p.privilege), "
        "has_table_privilege(%s,c.oid,p.privilege || ' WITH GRANT OPTION') "
        "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "CROSS JOIN unnest(%s::text[]) AS p(privilege) WHERE n.nspname=%s AND c.relkind='r'",
        (PLATFORM_RUNTIME_ROLE, PLATFORM_RUNTIME_ROLE, list(PRIVILEGES), PLATFORM_SCHEMA),
    ).fetchall()
    expected = {
        (table, privilege): privilege in rules
        for table, rules in policy_rules().items()
        for privilege in PRIVILEGES
    }
    if {(r[0], r[1]): bool(r[2]) for r in matrix} != expected or any(r[3] for r in matrix):
        raise LocalBootstrapError("The local per-table runtime grants differ.")
    scope = connection.execute(
        "SELECT has_schema_privilege(%s,%s,'USAGE'),has_schema_privilege(%s,%s,'CREATE'), "
        "has_database_privilege(%s,current_database(),'CONNECT'), "
        "has_database_privilege(%s,current_database(),'CREATE'), "
        "has_database_privilege(%s,current_database(),'TEMP'), "
        "has_schema_privilege(%s,'public','CREATE')",
        (
            PLATFORM_RUNTIME_ROLE,
            PLATFORM_SCHEMA,
            PLATFORM_RUNTIME_ROLE,
            PLATFORM_SCHEMA,
            PLATFORM_RUNTIME_ROLE,
            PLATFORM_RUNTIME_ROLE,
            PLATFORM_RUNTIME_ROLE,
            PLATFORM_RUNTIME_ROLE,
        ),
    ).fetchone()
    if scope != (True, False, True, False, False, False):
        raise LocalBootstrapError("The local runtime database/schema scope is too broad.")
    expected_marker = f"{ROLE_MARKER}:{_ddl_digest()}:{_catalog_digest(connection)}"
    if role[0] != expected_marker:
        raise LocalBootstrapError("Local schema metadata or the reviewed DDL fingerprint changed.")


def _create_installation(connection, password: str) -> None:
    connection.execute(
        sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(DATABASE))
    )
    connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(PLATFORM_SCHEMA)))
    connection.execute(
        sql.SQL("COMMENT ON SCHEMA {} IS {}").format(
            sql.Identifier(PLATFORM_SCHEMA), sql.Literal(PLATFORM_MARKER)
        )
    )
    for statement in TABLE_DDL:
        connection.execute(statement)
    for table in PLATFORM_COLUMNS:
        connection.execute(
            sql.SQL("COMMENT ON TABLE {} IS {}").format(
                sql.Identifier(PLATFORM_SCHEMA, table), sql.Literal(PLATFORM_MARKER)
            )
        )
    for statement in (*INDEX_DDL, *compatibility_constraints(), *policy_ddl()):
        connection.execute(statement)
    connection.execute(revoke_ddl())
    verifier = connection.pgconn.encrypt_password(
        password.encode("ascii"), PLATFORM_RUNTIME_ROLE.encode("ascii"), b"scram-sha-256"
    ).decode("ascii")
    if not verifier.startswith("SCRAM-SHA-256$"):
        raise LocalBootstrapError("The local runtime password verifier could not be generated.")
    connection.execute(
        sql.SQL(
            "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOREPLICATION NOBYPASSRLS NOINHERIT CONNECTION LIMIT 5 PASSWORD {}"
        ).format(sql.Identifier(PLATFORM_RUNTIME_ROLE), sql.Literal(verifier))
    )
    connection.execute(
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(DATABASE), sql.Identifier(PLATFORM_RUNTIME_ROLE)
        )
    )
    for statement in runtime_grant_ddl():
        connection.execute(statement)
    marker = f"{ROLE_MARKER}:{_ddl_digest()}:{_catalog_digest(connection)}"
    connection.execute(
        sql.SQL("COMMENT ON ROLE {} IS {}").format(
            sql.Identifier(PLATFORM_RUNTIME_ROLE), sql.Literal(marker)
        )
    )


def bootstrap() -> dict:
    """Create the empty local schema once, or verify it without overwriting anything."""
    _gate()
    try:
        admin_password = _read_password(ADMIN_PASSWORD)
        runtime_password = _read_password(RUNTIME_PASSWORD)
        if admin_password == runtime_password:
            raise LocalBootstrapError("Administrator and runtime passwords must be independent.")
        created = False
        with _connect(ADMIN, admin_password) as connection:
            _check_target(connection, ADMIN)
            connection.execute("SET LOCAL statement_timeout = '30000ms'")
            connection.execute("SET LOCAL lock_timeout = '5000ms'")
            connection.execute("SET LOCAL standard_conforming_strings = on")
            connection.execute("SET LOCAL TIME ZONE 'UTC'")
            connection.execute("SELECT pg_catalog.pg_advisory_xact_lock(717380026, 1)")
            schema, role = _schema_row(connection), _role_row(connection)
            if schema is None and role is None:
                _create_installation(connection, runtime_password)
                created = True
            elif schema is None or role is None:
                raise LocalBootstrapError("Incomplete or unknown same-name local resources exist.")
            _verify_installation(connection)
        # A retained volume and accidentally regenerated password must fail, not
        # silently replace an existing database login or make up a new password.
        with _connect(PLATFORM_RUNTIME_ROLE, runtime_password) as connection:
            _check_target(connection, PLATFORM_RUNTIME_ROLE)
            row = connection.execute(f"SELECT count(*) FROM {PLATFORM_SCHEMA}.users").fetchone()
            if row != (0,):
                raise LocalBootstrapError("Anonymous runtime access is not denied by local RLS.")
        return {
            "status": "local_database_ready",
            "created": created,
            "table_count": len(PLATFORM_COLUMNS),
            "client_tls": "verify-full",
            "cloud_connected": False,
            "historical_data_imported": False,
        }
    except LocalBootstrapError:
        raise
    except Exception:
        raise LocalBootstrapError(
            "Local database bootstrap failed; original errors and credentials are hidden."
        ) from None


def main() -> int:
    try:
        print(json.dumps(bootstrap(), sort_keys=True))
    except LocalBootstrapError as error:
        print(str(error))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
