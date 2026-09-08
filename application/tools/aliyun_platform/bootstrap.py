"""Install reviewed v2 DDL only into the new dedicated, independently marked database."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import psycopg
from psycopg import sql

from server import platform_experience_schema as v2
from server import platform_schema as v1

from .guard import ADMIN, DATABASE, MARKER, RUNTIME, DeploymentError, container_gate, secret


def connect(user: str, password: str):
    container_gate()
    if user not in {ADMIN, RUNTIME}:
        raise DeploymentError("Unknown database identity.")
    return psycopg.connect(
        host="postgres",
        port=5432,
        dbname=DATABASE,
        user=user,
        password=password,
        sslmode="verify-full",
        sslrootcert="/run/api-secrets/ca.crt",
        connect_timeout=10,
        application_name="medical-app-aliyun-bootstrap",
    )


def check_target(connection, user: str, deployment: dict) -> None:
    row = connection.execute(
        "SELECT current_database(),current_user,session_user,"
        "current_setting('medical_app.deployment_id',true),"
        "shobj_description(d.oid,'pg_database'),pg_get_userbyid(d.datdba),"
        "current_setting('password_encryption') FROM pg_database d "
        "WHERE d.datname=current_database()"
    ).fetchone()
    if not connection.pgconn.ssl_in_use or tuple(row or ()) != (
        DATABASE,
        user,
        user,
        deployment["id"],
        f"{MARKER}:{deployment['id']}",
        ADMIN,
        "scram-sha-256",
    ):
        raise DeploymentError("TLS, database ownership, or deployment identity differs.")


def ddl() -> tuple[str, ...]:
    return (
        *v1.TABLE_DDL,
        *v1.INDEX_DDL,
        *v1.compatibility_constraints(),
        *v1.policy_ddl(),
        *v2.NEW_TABLE_DDL,
        *v2.NEW_INDEX_DDL,
        *v2.compatibility_constraints(),
        *v2.incremental_policy_ddl(),
        v1.revoke_ddl(),
        *v2.runtime_grant_ddl(),
    )


def ddl_digest() -> str:
    return hashlib.sha256("\n".join(ddl()).encode()).hexdigest()


def catalog_digest(connection) -> str:
    # Data values and sequence counters deliberately excluded. Include effective
    # policy expressions, owners, ACLs, types, defaults, indexes and constraints.
    queries = (
        "SELECT c.relname,c.relkind,pg_get_userbyid(c.relowner),c.relrowsecurity,"
        "c.relforcerowsecurity,c.relacl::text FROM pg_class c JOIN pg_namespace n "
        "ON n.oid=c.relnamespace WHERE n.nspname=%s ORDER BY c.relname,c.relkind",
        "SELECT tablename,policyname,permissive,roles::text,cmd,qual,with_check "
        "FROM pg_policies WHERE schemaname=%s ORDER BY tablename,policyname",
        "SELECT table_name,column_name,data_type,is_nullable,column_default,"
        "identity_generation FROM information_schema.columns WHERE table_schema=%s "
        "ORDER BY table_name,ordinal_position",
        "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=%s ORDER BY indexname",
        "SELECT c.relname,k.conname,pg_get_constraintdef(k.oid) FROM pg_constraint k "
        "JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s ORDER BY c.relname,k.conname",
        "SELECT nspacl::text FROM pg_namespace WHERE nspname=%s",
        "SELECT pg_get_userbyid(a.defaclrole),a.defaclobjtype,a.defaclacl::text "
        "FROM pg_default_acl a JOIN pg_namespace n ON n.oid=a.defaclnamespace "
        "WHERE n.nspname=%s ORDER BY 1,2",
    )
    rows = [connection.execute(query, (v2.PLATFORM_SCHEMA,)).fetchall() for query in queries]
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def verify_installation(connection, deployment: dict) -> None:
    role = connection.execute(
        "SELECT shobj_description(oid,'pg_authid'),rolcanlogin,rolsuper,rolcreatedb,"
        "rolcreaterole,rolreplication,rolbypassrls,rolinherit,rolconnlimit,"
        "EXISTS(SELECT 1 FROM pg_auth_members m WHERE m.member=r.oid OR m.roleid=r.oid) "
        "FROM pg_roles r WHERE rolname=%s",
        (RUNTIME,),
    ).fetchone()
    expected = f"{MARKER}:{deployment['id']}:{ddl_digest()}:{catalog_digest(connection)}"
    if role != (expected, True, False, False, False, False, False, False, 5, False):
        raise DeploymentError("Runtime privileges, membership or schema fingerprint differs.")
    tables = connection.execute(
        "SELECT c.relname,pg_get_userbyid(c.relowner),c.relrowsecurity,c.relforcerowsecurity,"
        "obj_description(c.oid,'pg_class') FROM pg_class c JOIN pg_namespace n "
        "ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relkind='r'",
        (v2.PLATFORM_SCHEMA,),
    ).fetchall()
    if {row[0] for row in tables} != set(v2.PLATFORM_TABLES) or any(
        tuple(row[1:]) != (ADMIN, True, True, v2.PLATFORM_MARKER) for row in tables
    ):
        raise DeploymentError("Business tables are not the reviewed forced-RLS set.")
    scope = connection.execute(
        "SELECT has_database_privilege(%s,current_database(),'CONNECT'),"
        "has_database_privilege(%s,current_database(),'CREATE'),"
        "has_database_privilege(%s,current_database(),'TEMP'),"
        "has_schema_privilege(%s,'public','CREATE'),"
        "has_schema_privilege(%s,%s,'CREATE')",
        (RUNTIME, RUNTIME, RUNTIME, RUNTIME, RUNTIME, v2.PLATFORM_SCHEMA),
    ).fetchone()
    if scope != (True, False, False, False, False):
        raise DeploymentError("Runtime database scope is broader than allowed.")


def install_empty(connection, deployment: dict, password: str) -> None:
    occupied = connection.execute(
        "SELECT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
        "AND n.nspname NOT LIKE 'pg_toast%%' AND n.nspname NOT LIKE 'pg_temp%%') "
        "OR EXISTS(SELECT 1 FROM pg_namespace WHERE nspname NOT IN "
        "('public','pg_catalog','information_schema') AND nspname NOT LIKE 'pg_toast%%' "
        "AND nspname NOT LIKE 'pg_temp%%') "
        "OR EXISTS(SELECT 1 FROM pg_roles WHERE rolname=%s)",
        (RUNTIME,),
    ).fetchone()[0]
    if occupied:
        raise DeploymentError("Initial installation requires a genuinely empty owned database.")
    connection.execute(
        sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(DATABASE))
    )
    connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(v2.PLATFORM_SCHEMA)))
    connection.execute(
        sql.SQL("COMMENT ON SCHEMA {} IS {}").format(
            sql.Identifier(v2.PLATFORM_SCHEMA), sql.Literal(v2.PLATFORM_MARKER)
        )
    )
    verifier = connection.pgconn.encrypt_password(
        password.encode(), RUNTIME.encode(), b"scram-sha-256"
    ).decode()
    if not verifier.startswith("SCRAM-SHA-256$"):
        raise DeploymentError("Password verifier generation failed.")
    connection.execute(
        sql.SQL(
            "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOREPLICATION NOBYPASSRLS NOINHERIT CONNECTION LIMIT 5 PASSWORD {}"
        ).format(sql.Identifier(RUNTIME), sql.Literal(verifier))
    )
    for statement in ddl():
        connection.execute(statement)
    for table in v2.PLATFORM_TABLES:
        connection.execute(
            sql.SQL("COMMENT ON TABLE {} IS {}").format(
                sql.Identifier(v2.PLATFORM_SCHEMA, table), sql.Literal(v2.PLATFORM_MARKER)
            )
        )
    connection.execute(
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(DATABASE), sql.Identifier(RUNTIME)
        )
    )
    role_marker = f"{MARKER}:{deployment['id']}:{ddl_digest()}:{catalog_digest(connection)}"
    connection.execute(
        sql.SQL("COMMENT ON ROLE {} IS {}").format(
            sql.Identifier(RUNTIME), sql.Literal(role_marker)
        )
    )


def bootstrap() -> dict:
    deployment = container_gate()
    admin_password = secret(Path("/run/db-secrets/bootstrap-password"))
    runtime_password = secret(Path("/run/api-secrets/database-password"))
    if admin_password == runtime_password:
        raise DeploymentError("Administrator and runtime credentials must differ.")
    with connect(ADMIN, admin_password) as connection:
        check_target(connection, ADMIN, deployment)
        connection.execute("SET LOCAL statement_timeout='30s'")
        connection.execute("SET LOCAL lock_timeout='5s'")
        connection.execute("SET LOCAL TIME ZONE 'UTC'")
        connection.execute("SELECT pg_advisory_xact_lock(717380026, 2)")
        exists = connection.execute("SELECT to_regnamespace(%s)", (v2.PLATFORM_SCHEMA,)).fetchone()[
            0
        ]
        if exists is None:
            install_empty(connection, deployment, runtime_password)
        else:
            owner = connection.execute(
                "SELECT pg_get_userbyid(nspowner),obj_description(oid,'pg_namespace') "
                "FROM pg_namespace WHERE nspname=%s",
                (v2.PLATFORM_SCHEMA,),
            ).fetchone()
            if owner != (ADMIN, v2.PLATFORM_MARKER):
                raise DeploymentError("Existing schema ownership differs; nothing adopted.")
        verify_installation(connection, deployment)
    with connect(RUNTIME, runtime_password) as connection:
        check_target(connection, RUNTIME, deployment)
        if connection.execute(f"SELECT count(*) FROM {v2.PLATFORM_SCHEMA}.users").fetchone() != (
            0,
        ):
            raise DeploymentError("Anonymous runtime access was not denied by RLS.")
    return dict(
        status="owned_database_ready",
        created=exists is None,
        revision=v2.PLATFORM_REVISION,
        tables=len(v2.PLATFORM_TABLES),
        historical_data_imported=False,
    )


if __name__ == "__main__":
    try:
        print(json.dumps(bootstrap()))
    except Exception:
        raise SystemExit("Bootstrap refused or failed; credential details hidden.") from None
