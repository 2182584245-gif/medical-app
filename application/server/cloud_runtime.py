"""Explicit, administrator-side provisioning of the private pilot runtime role.

Never include this profile in the desktop package.  No cloud connection happens
at import or while loading settings.  Provisioning creates one backend login,
not end-user accounts, and never alters public/auth/storage permissions.

A DPAPI-encrypted pending journal is durable BEFORE CREATE ROLE.  A failed or
uncertain commit can be retried with the same password and pepper.  Unknown
existing roles are never adopted and passwords are never silently rotated.
PUBLIC-inherited PostgreSQL capabilities are not a per-role sandbox; the grants
verified here are for the owned private pilot namespace only.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
from pathlib import Path

import psycopg
from psycopg import sql
from sqlalchemy.engine import URL

from .cloud_connection import (
    DEFAULT_PROFILE,
    CloudConfigurationError,
    ca_file,
    project_reference,
    project_url,
    validated_url,
)
from .config import Settings
from .local_secret_store import LocalSecretStoreError, load_secret_payload, save_secret_payload
from .migrations.guards import REVISION_MARKER, SCHEMA_MARKER
from .schema import PRIVATE_SCHEMA

DEFAULT_RUNTIME_PROFILE = DEFAULT_PROFILE.with_name("supabase-runtime.json")
RUNTIME_ROLE = "medical_app_runtime"
PURPOSE = "private-pilot-runtime-v1"
ROLE_MARKER = "medical-app:private-pilot-runtime:v1"
TABLE_PRIVILEGES = {
    "pilot_users": ("SELECT", "INSERT", "UPDATE"),
    "pilot_login_sessions": ("SELECT", "INSERT", "UPDATE"),
    "pilot_conversations": ("SELECT", "INSERT", "UPDATE"),
    "pilot_messages": ("SELECT", "INSERT"),
}
ALL_PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
    "MAINTAIN",
)
_KEYS = {"project_url", "purpose", "runtime_database_url", "token_pepper", "provision_id", "state"}


def _strict_url(value: str) -> URL:
    uri = validated_url(value, project_url())
    if not uri.password or uri.query.get("sslmode") != "verify-full":
        raise CloudConfigurationError("运行配置缺少密码或严格证书验证，已停止。")
    certificate = uri.query.get("sslrootcert")
    if not certificate:
        raise CloudConfigurationError("运行配置必须显式指定受信任根证书。")
    ca_file(certificate)
    return uri


def _runtime_username(uri: URL) -> str:
    if (uri.host or "").endswith(".pooler.supabase.com"):
        return f"{RUNTIME_ROLE}.{project_reference(project_url())}"
    return RUNTIME_ROLE


def _validate_payload(payload: dict, *, allow_pending: bool = False) -> dict:
    if (
        not isinstance(payload, dict)
        or set(payload) != _KEYS
        or payload["project_url"] != project_url()
        or payload["purpose"] != PURPOSE
        or not isinstance(payload["state"], str)
        or payload["state"] not in ({"ready", "pending"} if allow_pending else {"ready"})
        or not isinstance(payload["provision_id"], str)
        or not re.fullmatch(r"[0-9a-f]{32}", payload["provision_id"])
        or not isinstance(payload["token_pepper"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", payload["token_pepper"])
        or len(set(bytes.fromhex(payload["token_pepper"]))) < 16
    ):
        raise CloudConfigurationError("运行配置不完整、用途不符或尚未完成，已停止。")
    uri = _strict_url(payload["runtime_database_url"])
    if uri.username != _runtime_username(uri) or not re.fullmatch(r"[0-9a-f]{64}", uri.password):
        raise CloudConfigurationError("运行配置不是本项目专用角色或密码格式不正确。")
    return payload


def load_runtime_payload(
    profile: Path = DEFAULT_RUNTIME_PROFILE, *, allow_pending: bool = False
) -> dict:
    """Return secrets for trusted backend callers only; never print the result."""
    return _validate_payload(load_secret_payload(profile), allow_pending=allow_pending)


def load_runtime_settings(
    profile: Path = DEFAULT_RUNTIME_PROFILE, *, test_client: bool = False
) -> Settings:
    """Local-only factory; not a public deployment or network listener."""
    payload = load_runtime_payload(profile)
    return Settings(
        env="test" if test_client else "development",
        database_url=payload["runtime_database_url"],
        token_pepper=payload["token_pepper"],
        allowed_hosts=["localhost", "127.0.0.1"] + (["testserver"] if test_client else []),
        require_https=False,  # Only loopback/TestClient; DB TLS is always verify-full.
        external_auth_limits_configured=False,
    )


def _admin_url(profile: Path) -> URL:
    payload = load_secret_payload(profile)
    if (
        set(payload) != {"project_url", "purpose", "migration_database_url"}
        or payload["project_url"] != project_url()
        or payload["purpose"] != "migration-readonly-preflight"
    ):
        raise CloudConfigurationError("管理配置不属于当前项目或用途不正确。")
    uri = _strict_url(payload["migration_database_url"])
    if (uri.username or "").split(".")[0] == RUNTIME_ROLE:
        raise CloudConfigurationError("不能将运行角色当作迁移管理员。")
    return uri


def _connect(uri: URL):
    return psycopg.connect(
        host=uri.host,
        port=5432,
        dbname="postgres",
        user=uri.username,
        password=uri.password,
        sslmode="verify-full",
        sslrootcert=ca_file(uri.query["sslrootcert"]),
        connect_timeout=8,
        application_name="medical-app-runtime-provision",
    )


def _marker(payload: dict) -> str:
    return f"{ROLE_MARKER}:{project_reference(project_url())}:{payload['provision_id']}"


def _check_schema(connection) -> None:
    row = connection.execute(
        "SELECT pg_catalog.obj_description(n.oid, 'pg_namespace'), "
        "n.nspowner = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = current_user) "
        "FROM pg_catalog.pg_namespace n WHERE n.nspname = %s",
        (PRIVATE_SCHEMA,),
    ).fetchone()
    if row != (SCHEMA_MARKER, True):
        raise CloudConfigurationError("尚未找到当前管理员拥有的应用专用结构，先完成迁移。")
    for table in (*TABLE_PRIVILEGES, "alembic_version"):
        row = connection.execute(
            "SELECT pg_catalog.obj_description(c.oid, 'pg_class'), c.relkind, "
            "c.relowner = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = current_user) "
            "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=%s AND c.relname=%s",
            (PRIVATE_SCHEMA, table),
        ).fetchone()
        if row != (REVISION_MARKER, "r", True):
            raise CloudConfigurationError("应用表或迁移记录的所有权标记不符合要求，已停止。")
    connection.execute(
        sql.SQL("LOCK TABLE {} IN ACCESS SHARE MODE").format(
            sql.SQL(", ").join(
                sql.Identifier(PRIVATE_SCHEMA, table)
                for table in (*TABLE_PRIVILEGES, "alembic_version")
            )
        )
    )
    rows = connection.execute(
        sql.SQL("SELECT version_num FROM {}.alembic_version").format(sql.Identifier(PRIVATE_SCHEMA))
    ).fetchall()
    if rows != [("pilot_0001",)]:
        raise CloudConfigurationError("云端迁移版本不是已审核的 pilot_0001，已停止。")


def _role_row(connection):
    return connection.execute(
        "SELECT pg_catalog.shobj_description(r.oid, 'pg_authid'), "
        "r.rolcanlogin, r.rolsuper, r.rolcreatedb, r.rolcreaterole, "
        "r.rolreplication, r.rolbypassrls, r.rolinherit, "
        "EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member=r.oid) "
        "FROM pg_catalog.pg_roles r WHERE r.rolname=%s",
        (RUNTIME_ROLE,),
    ).fetchone()


def _verify_role(connection, payload: dict) -> None:
    if _role_row(connection) != (
        _marker(payload),
        True,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
    ):
        raise CloudConfigurationError("运行角色的归属、属性或成员关系异常；未接管或修改。")
    row = connection.execute(
        "SELECT has_schema_privilege(%s, %s, 'USAGE'), "
        "has_schema_privilege(%s, %s, 'CREATE'), "
        "has_database_privilege(%s, current_database(), 'CONNECT')",
        (RUNTIME_ROLE, PRIVATE_SCHEMA, RUNTIME_ROLE, PRIVATE_SCHEMA, RUNTIME_ROLE),
    ).fetchone()
    if row != (True, False, True):
        raise CloudConfigurationError("运行角色的应用结构权限不符合最小权限要求。")
    for table in (*TABLE_PRIVILEGES, "alembic_version"):
        for privilege in ALL_PRIVILEGES:
            row = connection.execute(
                "SELECT has_table_privilege(%s, %s, %s)",
                (RUNTIME_ROLE, f"{PRIVATE_SCHEMA}.{table}", privilege),
            ).fetchone()
            if row != (privilege in TABLE_PRIVILEGES.get(table, ()),):
                raise CloudConfigurationError("运行角色的应用表权限不符合最小权限要求。")


def _create_role(connection, payload: dict) -> None:
    uri = _strict_url(payload["runtime_database_url"])
    # libpq computes the SCRAM verifier locally. Plaintext never enters DDL or SQL logs.
    verifier = connection.pgconn.encrypt_password(
        uri.password.encode("ascii"), RUNTIME_ROLE.encode("ascii"), b"scram-sha-256"
    ).decode("ascii")
    if not verifier.startswith("SCRAM-SHA-256$"):
        raise CloudConfigurationError("未能安全生成数据库密码验证值，已停止。")
    role = sql.Identifier(RUNTIME_ROLE)
    connection.execute(
        sql.SQL(
            "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOREPLICATION NOBYPASSRLS NOINHERIT CONNECTION LIMIT 10 PASSWORD {}"
        ).format(role, sql.Literal(verifier))
    )
    connection.execute(
        sql.SQL("COMMENT ON ROLE {} IS {}").format(role, sql.Literal(_marker(payload)))
    )
    connection.execute(sql.SQL("GRANT CONNECT ON DATABASE postgres TO {}").format(role))
    connection.execute(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(PRIVATE_SCHEMA), role)
    )
    for table, privileges in TABLE_PRIVILEGES.items():
        connection.execute(
            sql.SQL("GRANT {} ON TABLE {} TO {}").format(
                sql.SQL(", ").join(sql.SQL(item) for item in privileges),
                sql.Identifier(PRIVATE_SCHEMA, table),
                role,
            )
        )


def provision(
    *,
    confirm: str,
    admin_profile: Path = DEFAULT_PROFILE,
    runtime_profile: Path = DEFAULT_RUNTIME_PROFILE,
) -> dict:
    """Explicit cloud write; safe to retry after an interrupted/uncertain commit."""
    if confirm != PRIVATE_SCHEMA:
        raise CloudConfigurationError("必须显式确认 medical_app_private，未连接或修改云端。")
    if admin_profile.absolute() == runtime_profile.absolute():
        raise CloudConfigurationError("运行配置与管理配置必须使用不同文件。")
    admin = _admin_url(admin_profile)
    created = False
    with _connect(admin) as connection:
        if not connection.pgconn.ssl_in_use:
            raise CloudConfigurationError("未建立严格 TLS 连接，已停止。")
        connection.execute("SET LOCAL statement_timeout = '5000ms'")
        connection.execute("SET LOCAL lock_timeout = '3000ms'")
        connection.execute("SET LOCAL idle_in_transaction_session_timeout = '30000ms'")
        connection.execute("SELECT pg_catalog.pg_advisory_xact_lock(717380021, 1)")
        _check_schema(connection)
        # Load inside the cross-process database lock, never reuse stale file state.
        payload = (
            load_runtime_payload(runtime_profile, allow_pending=True)
            if runtime_profile.exists()
            else None
        )
        existing = _role_row(connection)
        if existing and payload is None:
            raise CloudConfigurationError("同名运行角色已存在，但没有本机归属记录；拒绝接管。")
        if payload:
            runtime = _strict_url(payload["runtime_database_url"])
            if (runtime.host, runtime.port, runtime.database, runtime.query) != (
                admin.host,
                admin.port,
                admin.database,
                admin.query,
            ):
                raise CloudConfigurationError("已有运行配置与当前管理连接不一致；未覆盖。")
            if not existing and payload["state"] == "ready":
                raise CloudConfigurationError("已就绪配置对应的云端角色缺失，需人工核对；未重建。")
        else:
            password = secrets.token_hex(32)
            pepper = secrets.token_hex(32)
            while len(set(bytes.fromhex(pepper))) < 16:
                pepper = secrets.token_hex(32)
            runtime = admin.set(username=_runtime_username(admin), password=password)
            payload = _validate_payload(
                {
                    "project_url": project_url(),
                    "purpose": PURPOSE,
                    "runtime_database_url": runtime.render_as_string(hide_password=False),
                    "token_pepper": pepper,
                    "provision_id": secrets.token_hex(16),
                    "state": "pending",
                },
                allow_pending=True,
            )
            # Caller must use an existing trusted private directory; DPAPI validates it.
            save_secret_payload(runtime_profile, payload)
        if not existing:
            _create_role(connection, payload)
            created = True
        _verify_role(connection, payload)
    # Context manager commits all role/GRANT changes together before ready is saved.
    # On any commit uncertainty, preserve pending credentials for a safe retry.
    if payload["state"] != "ready":
        save_secret_payload(runtime_profile, {**payload, "state": "ready"}, replace=True)
    return {
        "status": "runtime_role_created" if created else "runtime_role_verified",
        "database_role": RUNTIME_ROLE,
        "profile_ready": True,
        "schema": PRIVATE_SCHEMA,
        "application_tables": 4,
        "business_rows_changed": 0,
        "desktop_sync_enabled": False,
        "runtime_login": "not_checked",
        "password_rotated": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("provision",))
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--admin-profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--runtime-profile", type=Path, default=DEFAULT_RUNTIME_PROFILE)
    args = parser.parse_args()
    try:
        result = provision(
            confirm=args.confirm,
            admin_profile=args.admin_profile,
            runtime_profile=args.runtime_profile,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (CloudConfigurationError, LocalSecretStoreError) as error:
        print(str(error))
        return 2
    except psycopg.Error:
        print("运行角色配置未完成；原始数据库错误已隐藏。请保留本机加密记录以便安全重试。")
        return 3
    except (Exception, KeyboardInterrupt):
        print("配置中断或本机保存未完成；请保留加密记录，勿删除后重新生成密码。")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
