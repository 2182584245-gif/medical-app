"""Explicit administrator operations for the full platform, never the pilot login.

Inspect is read-only. Migrate requires two independent schema confirmations.
Provision journals fresh secrets with current-user DPAPI before transactional DDL.
Never distribute the .local profile, print returned secret payloads, or expose
these functions through the application HTTP/RPC surface.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import URL

from . import platform_schema as schema_v1
from .cloud_connection import (
    DEFAULT_PROFILE,
    ca_file,
    project_reference,
    project_url,
    validated_url,
)
from .local_secret_store import load_secret_payload, save_secret_payload
from .migrations.guards import REVISION_MARKER, SCHEMA_MARKER
from .platform_config import PlatformSettings
from .platform_experience_schema import (
    EXPIRY_TABLES,
    IDENTITY_TABLES,
    NEW_COLUMNS,
    PLATFORM_COLUMNS,
    PLATFORM_MARKER,
    PLATFORM_REVISION,
    PLATFORM_RUNTIME_ROLE,
    PLATFORM_SCHEMA,
    policy_rules,
    runtime_grant_ddl,
)
from .schema import PRIVATE_SCHEMA

DEFAULT_RUNTIME_PROFILE = DEFAULT_PROFILE.with_name("supabase-platform-runtime.json")
PURPOSE = "full-platform-runtime-v1"
ROLE_MARKER = "medical-app:platform-runtime:v1"
PROFILE_KEYS = {
    "project_url",
    "purpose",
    "runtime_database_url",
    "token_pepper",
    "provision_id",
    "state",
}
PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
    "MAINTAIN",
)


class PlatformAdminError(RuntimeError):
    """Fixed safe messages only; never attach driver/URL/credential text."""


def _safe(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except PlatformAdminError:
            raise
        except Exception:
            raise PlatformAdminError(
                "平台管理操作未完成，原始异常与凭据已隐藏；请保留本机加密记录并先核实提交状态。"
            ) from None

    return call


def _strict_url(value: str) -> URL:
    parsed = validated_url(value, project_url())
    if (
        not parsed.password
        or parsed.query.get("sslmode") != "verify-full"
        or not parsed.query.get("sslrootcert")
    ):
        raise PlatformAdminError("平台连接必须有密码、verify-full 和明确可信根证书。")
    ca_file(parsed.query["sslrootcert"])
    return parsed


@_safe
def management_url(profile: Path = DEFAULT_PROFILE) -> URL:
    payload = load_secret_payload(profile)
    if (
        set(payload) != {"project_url", "purpose", "migration_database_url"}
        or payload["project_url"] != project_url()
        or payload["purpose"] != "migration-readonly-preflight"
    ):
        raise PlatformAdminError("平台管理配置归属或用途不正确。")
    parsed = _strict_url(payload["migration_database_url"])
    if (parsed.username or "").split(".")[0] != "postgres":
        raise PlatformAdminError("平台管理操作需要独立 postgres 管理配置，不能使用运行账号。")
    return parsed


def _username(uri: URL) -> str:
    suffix = (
        f".{project_reference(project_url())}"
        if (uri.host or "").endswith(".pooler.supabase.com")
        else ""
    )
    return PLATFORM_RUNTIME_ROLE + suffix


def _validate_runtime(payload: dict, *, allow_pending: bool = False) -> dict:
    if (
        not isinstance(payload, dict)
        or set(payload) != PROFILE_KEYS
        or payload["project_url"] != project_url()
        or payload["purpose"] != PURPOSE
        or not isinstance(payload["state"], str)
        or payload["state"] not in ({"ready", "pending"} if allow_pending else {"ready"})
        or not isinstance(payload["provision_id"], str)
        or not re.fullmatch(r"[a-f0-9]{32}", payload["provision_id"])
        or not isinstance(payload["token_pepper"], str)
        or not re.fullmatch(r"[a-f0-9]{64}", payload["token_pepper"])
        or len(set(bytes.fromhex(payload["token_pepper"]))) < 16
    ):
        raise PlatformAdminError("平台运行配置尚未就绪、归属不符或格式不正确。")
    uri = _strict_url(payload["runtime_database_url"])
    if uri.username != _username(uri) or not re.fullmatch(r"[a-f0-9]{64}", uri.password):
        raise PlatformAdminError("平台运行账号不匹配；禁止复用管理员或旧试点账号。")
    return payload


@_safe
def load_runtime_payload(
    profile: Path = DEFAULT_RUNTIME_PROFILE, *, allow_pending: bool = False
) -> dict:
    """Secrets in memory only: caller MUST NOT print or log this return value."""
    return _validate_runtime(load_secret_payload(profile), allow_pending=allow_pending)


@_safe
def load_runtime_url(profile: Path = DEFAULT_RUNTIME_PROFILE) -> URL:
    return _strict_url(load_runtime_payload(profile)["runtime_database_url"])


@_safe
def load_runtime_settings(
    profile: Path = DEFAULT_RUNTIME_PROFILE,
    *,
    test_client: bool = False,
    allowed_hosts: list[str] | None = None,
    render_proxy: bool = False,
    railway_proxy: bool = False,
    railway_edge_only: bool = False,
) -> PlatformSettings:
    payload = load_runtime_payload(profile)
    return PlatformSettings(
        env="test" if test_client else "production",
        database_url=payload["runtime_database_url"],
        token_pepper=payload["token_pepper"],
        allowed_hosts=["localhost", "127.0.0.1", "testserver"]
        if test_client
        else (allowed_hosts or []),
        require_https=not test_client,
        render_proxy=render_proxy and not test_client,
        railway_proxy=railway_proxy and not test_client,
        railway_edge_only=railway_edge_only and not test_client,
    )


def _connect(uri: URL):
    return psycopg.connect(
        host=uri.host,
        port=5432,
        dbname="postgres",
        user=uri.username,
        password=uri.password,
        sslmode="verify-full",
        sslrootcert=ca_file(uri.query["sslrootcert"]),
        connect_timeout=10,
        application_name="medical-app-platform-admin",
    )


@contextmanager
def _connection(uri: URL, *, readonly: bool):
    with _connect(uri) as connection:
        connection.read_only = readonly
        try:
            if not connection.pgconn.ssl_in_use:
                raise PlatformAdminError("未建立严格 TLS 加密连接，已停止。")
            connection.execute("SET LOCAL statement_timeout = '10000ms'")
            connection.execute("SET LOCAL lock_timeout = '5000ms'")
            if not readonly:
                connection.execute("SELECT pg_catalog.pg_advisory_xact_lock(717380023, 1)")
            yield connection
        finally:
            if readonly:
                connection.rollback()


def _inspect(connection) -> dict:
    pilot = connection.execute(
        "SELECT pg_catalog.obj_description(n.oid,'pg_namespace'), "
        "pg_catalog.obj_description(c.oid,'pg_class'), "
        "pg_catalog.pg_get_userbyid(n.nspowner)=current_user, "
        "pg_catalog.pg_get_userbyid(c.relowner)=current_user "
        "FROM pg_catalog.pg_namespace n JOIN pg_catalog.pg_class c ON c.relnamespace=n.oid "
        "WHERE n.nspname=%s AND c.relname='alembic_version' AND c.relkind='r'",
        (PRIVATE_SCHEMA,),
    ).fetchone()
    pilot_owned = pilot == (SCHEMA_MARKER, REVISION_MARKER, True, True)
    revision = []
    if pilot_owned:
        revision = [
            row[0]
            for row in connection.execute(
                sql.SQL("SELECT version_num FROM {}.alembic_version").format(
                    sql.Identifier(PRIVATE_SCHEMA)
                )
            ).fetchall()
        ]
    schema = connection.execute(
        "SELECT pg_catalog.obj_description(oid,'pg_namespace'), "
        "pg_catalog.pg_get_userbyid(nspowner)=current_user "
        "FROM pg_catalog.pg_namespace WHERE nspname=%s",
        (PLATFORM_SCHEMA,),
    ).fetchone()
    result = {
        "status": "platform_catalog_readonly",
        "project_url": project_url(),
        "schema_exists": schema is not None,
        "pilot_owned": pilot_owned,
        "revisions": revision,
        "client_tls": True,
        "certificate_mode": "verify-full",
        "tables_changed": False,
        "health_records_read": 0,
        "historical_data_imported": False,
    }
    if schema is None:
        return result
    legacy = revision == [schema_v1.PLATFORM_REVISION]
    expected_columns = schema_v1.PLATFORM_COLUMNS if legacy else PLATFORM_COLUMNS
    expected_rules = schema_v1.policy_rules() if legacy else policy_rules()
    tables = connection.execute(
        "SELECT c.relname, pg_catalog.obj_description(c.oid,'pg_class'), "
        "pg_catalog.pg_get_userbyid(c.relowner)=current_user, "
        "c.relrowsecurity,c.relforcerowsecurity, "
        "ARRAY(SELECT a.attname::text FROM pg_catalog.pg_attribute a WHERE a.attrelid=c.oid "
        "AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum) "
        "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s AND c.relkind='r' ORDER BY c.relname",
        (PLATFORM_SCHEMA,),
    ).fetchall()
    result.update(
        schema_owned=schema == (PLATFORM_MARKER, True)
        and all(row[1] == PLATFORM_MARKER and row[2] for row in tables),
        expected_table_set={row[0] for row in tables} == set(expected_columns),
        expected_columns=all(tuple(row[5]) == expected_columns.get(row[0]) for row in tables),
        all_rls_forced=bool(tables) and all(row[3] and row[4] for row in tables),
        table_count=len(tables),
    )
    policies = connection.execute(
        "SELECT c.relname,p.polname,p.polcmd,p.polpermissive FROM pg_catalog.pg_policy p "
        "JOIN pg_catalog.pg_class c ON c.oid=p.polrelid JOIN pg_catalog.pg_namespace n "
        "ON n.oid=c.relnamespace WHERE n.nspname=%s",
        (PLATFORM_SCHEMA,),
    ).fetchall()
    codes = {"SELECT": "r", "INSERT": "a", "UPDATE": "w", "DELETE": "d"}
    expected = {
        (table, "platform_" + action.lower(), codes[action], True)
        for table, rules in expected_rules.items()
        for action in rules
    }
    if not legacy:
        expected |= {(table, "platform_staff_term", "*", False) for table in EXPIRY_TABLES}
    result["expected_policy_set"] = set(policies) == expected
    public_access = connection.execute(
        "SELECT r.rolname,c.relname, has_schema_privilege(r.oid,n.oid,'USAGE'), "
        "has_table_privilege(r.oid,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE') "
        "FROM pg_catalog.pg_roles r CROSS JOIN pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n "
        "ON n.oid=c.relnamespace WHERE r.rolname IN ('anon','authenticated','service_role') "
        "AND n.nspname=%s AND c.relkind='r'",
        (PLATFORM_SCHEMA,),
    ).fetchall()
    result["data_api_roles_no_access"] = len(public_access) == 3 * len(expected_columns) and all(
        not row[2] and not row[3] for row in public_access
    )
    return result


def _verified(report: dict) -> bool:
    return all(
        report.get(key)
        for key in (
            "pilot_owned",
            "schema_owned",
            "expected_table_set",
            "expected_columns",
            "all_rls_forced",
            "expected_policy_set",
            "data_api_roles_no_access",
        )
    ) and report.get("revisions") == [PLATFORM_REVISION]


@_safe
def inspect_catalog(profile: Path = DEFAULT_PROFILE) -> dict:
    with _connection(management_url(profile), readonly=True) as connection:
        return _inspect(connection)


@contextmanager
def migration_environment(uri: URL):
    values = {
        "SERVER_MIGRATION_CONFIRM": PRIVATE_SCHEMA,
        "SERVER_PLATFORM_CONFIRM": PLATFORM_SCHEMA,
        "SERVER_MIGRATION_DATABASE_URL": uri.render_as_string(hide_password=False),
    }
    previous = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@_safe
def migrate(*, confirm: str, pilot_confirm: str, profile: Path = DEFAULT_PROFILE) -> dict:
    if confirm != PLATFORM_SCHEMA or pilot_confirm != PRIVATE_SCHEMA:
        raise PlatformAdminError("必须分别明确确认 medical_app_platform 与 medical_app_private。")
    before = inspect_catalog(profile)
    if _verified(before):
        return {**before, "status": "platform_migration_already_verified"}
    initial = (
        not before["schema_exists"]
        and before["pilot_owned"]
        and before["revisions"] == ["pilot_0001"]
    )
    existing_v1 = before.get("revisions") == [schema_v1.PLATFORM_REVISION] and _verified(
        {**before, "revisions": [PLATFORM_REVISION]}
    )
    if not initial and not existing_v1:
        raise PlatformAdminError("迁移前状态不符合预期；未接管、覆盖或修改已有对象。")
    try:
        with migration_environment(management_url(profile)):
            command.upgrade(Config(str(Path(__file__).with_name("alembic.ini"))), PLATFORM_REVISION)
    except Exception:
        raise PlatformAdminError(
            "迁移未报告成功，提交状态未知；请先 inspect 核实，不要盲目重复。"
        ) from None
    after = inspect_catalog(profile)
    if not _verified(after):
        raise PlatformAdminError("平台迁移后的结构、权限或 RLS 验收未通过，禁止公开服务。")
    return {
        **after,
        "status": "platform_migration_verified",
        "tables_changed": True,
        "created_tables": len(NEW_COLUMNS) if existing_v1 else len(PLATFORM_COLUMNS),
    }


def _marker(payload: dict) -> str:
    return f"{ROLE_MARKER}:{project_reference(project_url())}:{payload['provision_id']}"


def _role_row(connection):
    return connection.execute(
        "SELECT pg_catalog.shobj_description(r.oid,'pg_authid'),r.rolcanlogin,r.rolsuper, "
        "r.rolcreatedb,r.rolcreaterole,r.rolreplication,r.rolbypassrls, "
        "r.rolinherit,r.rolconnlimit, "
        "EXISTS(SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member=r.oid) "
        "FROM pg_catalog.pg_roles r WHERE r.rolname=%s",
        (PLATFORM_RUNTIME_ROLE,),
    ).fetchone()


def _verify_role(connection, payload: dict, *, allow_missing_experience: bool = False) -> None:
    if _role_row(connection) != (
        _marker(payload),
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
        raise PlatformAdminError("平台运行角色归属、属性、连接数或成员关系不符合要求。")
    scope = connection.execute(
        "SELECT has_schema_privilege(%s,%s,'USAGE'),has_schema_privilege(%s,%s,'CREATE'), "
        "has_schema_privilege(%s,%s,'USAGE WITH GRANT OPTION'), "
        "has_database_privilege(%s,current_database(),'CONNECT'), "
        "has_schema_privilege(%s,%s,'USAGE')",
        (
            PLATFORM_RUNTIME_ROLE,
            PLATFORM_SCHEMA,
            PLATFORM_RUNTIME_ROLE,
            PLATFORM_SCHEMA,
            PLATFORM_RUNTIME_ROLE,
            PLATFORM_SCHEMA,
            PLATFORM_RUNTIME_ROLE,
            PLATFORM_RUNTIME_ROLE,
            PRIVATE_SCHEMA,
        ),
    ).fetchone()
    if scope != (True, False, False, True, False):
        raise PlatformAdminError("平台运行角色的结构权限或试点隔离不符合要求。")
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
    observed = {(row[0], row[1]): bool(row[2]) for row in matrix}
    missing_only_new = (
        allow_missing_experience
        and set(observed) == set(expected)
        and all(
            observed[key] == value or (key[0] in NEW_COLUMNS and value and not observed[key])
            for key, value in expected.items()
        )
    )
    if (observed != expected and not missing_only_new) or any(row[3] for row in matrix):
        raise PlatformAdminError("平台逐表权限不符合审核的最小授权矩阵。")
    sequences = connection.execute(
        "SELECT c.relname,has_sequence_privilege(%s,c.oid,'USAGE'), "
        "has_sequence_privilege(%s,c.oid,'SELECT'),has_sequence_privilege(%s,c.oid,'UPDATE') "
        "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s AND c.relkind='S'",
        (PLATFORM_RUNTIME_ROLE, PLATFORM_RUNTIME_ROLE, PLATFORM_RUNTIME_ROLE, PLATFORM_SCHEMA),
    ).fetchall()
    if {row[0] for row in sequences} != {f"{table}_id_seq" for table in IDENTITY_TABLES} or any(
        tuple(row[1:]) != (True, True, False) for row in sequences
    ):
        raise PlatformAdminError("平台身份序列权限不符合要求。")


def _create_role(connection, payload: dict) -> None:
    uri = _strict_url(payload["runtime_database_url"])
    verifier = connection.pgconn.encrypt_password(
        uri.password.encode("ascii"), PLATFORM_RUNTIME_ROLE.encode("ascii"), b"scram-sha-256"
    ).decode("ascii")
    if not verifier.startswith("SCRAM-SHA-256$"):
        raise PlatformAdminError("SCRAM 密码验证值生成失败，已停止。")
    role = sql.Identifier(PLATFORM_RUNTIME_ROLE)
    connection.execute(
        sql.SQL(
            "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOREPLICATION NOBYPASSRLS NOINHERIT CONNECTION LIMIT 5 PASSWORD {}"
        ).format(role, sql.Literal(verifier))
    )
    connection.execute(
        sql.SQL("COMMENT ON ROLE {} IS {}").format(role, sql.Literal(_marker(payload)))
    )
    connection.execute(sql.SQL("GRANT CONNECT ON DATABASE postgres TO {}").format(role))
    for statement in runtime_grant_ddl():
        connection.execute(statement)


@_safe
def provision(
    *,
    confirm: str,
    admin_profile: Path = DEFAULT_PROFILE,
    runtime_profile: Path = DEFAULT_RUNTIME_PROFILE,
) -> dict:
    if confirm != PLATFORM_SCHEMA:
        raise PlatformAdminError("必须明确确认 medical_app_platform，尚未连接或修改。")
    forbidden = (admin_profile, DEFAULT_PROFILE.with_name("supabase-runtime.json"))
    if any(runtime_profile.absolute() == path.absolute() for path in forbidden):
        raise PlatformAdminError("平台运行配置不能覆盖管理配置或旧试点运行配置。")
    uri = management_url(admin_profile)
    created = False
    with _connection(uri, readonly=False) as connection:
        if not _verified(_inspect(connection)):
            raise PlatformAdminError("平台结构、迁移版本或权限未通过检查，未创建运行账号。")
        connection.execute(
            sql.SQL("LOCK TABLE {} IN ACCESS SHARE MODE").format(
                sql.SQL(", ").join(
                    sql.Identifier(PLATFORM_SCHEMA, table) for table in PLATFORM_COLUMNS
                )
            )
        )
        payload = (
            load_runtime_payload(runtime_profile, allow_pending=True)
            if runtime_profile.exists()
            else None
        )
        existing = _role_row(connection)
        if existing and payload is None:
            raise PlatformAdminError("同名角色已存在但没有本机归属记录，拒绝接管。")
        if payload is not None:
            previous = _strict_url(payload["runtime_database_url"])
            if (previous.host, previous.port, previous.database, previous.query) != (
                uri.host,
                uri.port,
                uri.database,
                uri.query,
            ):
                raise PlatformAdminError("已有平台运行配置与管理连接不一致，未覆盖。")
            if not existing and payload["state"] == "ready":
                raise PlatformAdminError("就绪配置对应角色缺失，未自动重建或旋转密码。")
        else:
            pepper = secrets.token_hex(32)
            while len(set(bytes.fromhex(pepper))) < 16:
                pepper = secrets.token_hex(32)
            runtime = uri.set(username=_username(uri), password=secrets.token_hex(32))
            payload = _validate_runtime(
                {
                    "project_url": project_url(),
                    "purpose": PURPOSE,
                    "state": "pending",
                    "runtime_database_url": runtime.render_as_string(hide_password=False),
                    "token_pepper": pepper,
                    "provision_id": secrets.token_hex(16),
                },
                allow_pending=True,
            )
            save_secret_payload(runtime_profile, payload)
        if not existing:
            _create_role(connection, payload)
            created = True
        else:
            _verify_role(connection, payload, allow_missing_experience=True)
            for table in NEW_COLUMNS:
                connection.execute(
                    f"GRANT {', '.join(policy_rules()[table])} ON TABLE "
                    f"{PLATFORM_SCHEMA}.{table} TO {PLATFORM_RUNTIME_ROLE}"
                )
        _verify_role(connection, payload)
    if payload["state"] != "ready":
        save_secret_payload(runtime_profile, {**payload, "state": "ready"}, replace=True)
    return {
        "status": "platform_runtime_created" if created else "platform_runtime_verified",
        "database_role": PLATFORM_RUNTIME_ROLE,
        "profile_ready": True,
        "connection_limit": 5,
        "business_rows_changed": 0,
        "runtime_login": "not_checked",
        "password_rotated": False,
    }


@_safe
def bootstrap_admin(
    username: str,
    password: str,
    *,
    confirm: str,
    admin_profile: Path = DEFAULT_PROFILE,
    runtime_profile: Path = DEFAULT_RUNTIME_PROFILE,
):
    """In-memory local administrator call only; intentionally absent from the CLI."""
    if confirm != PLATFORM_SCHEMA:
        raise PlatformAdminError("管理员初始化需明确确认平台结构。")
    from .platform_auth import bootstrap_operator
    from .platform_database import PlatformDatabase
    from .platform_security import PlatformAuthSettings

    payload = load_runtime_payload(runtime_profile)
    database = PlatformDatabase(management_url(admin_profile).render_as_string(hide_password=False))
    try:
        settings = PlatformAuthSettings(token_pepper=payload["token_pepper"], production=True)
        return bootstrap_operator(database, settings, username, password, confirm=confirm)
    finally:
        database.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inspect", "migrate", "provision"))
    parser.add_argument("--confirm", default="")
    parser.add_argument("--pilot-confirm", default="")
    parser.add_argument("--admin-profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--runtime-profile", type=Path, default=DEFAULT_RUNTIME_PROFILE)
    args = parser.parse_args()
    try:
        if args.command == "inspect":
            result = inspect_catalog(args.admin_profile)
        elif args.command == "migrate":
            result = migrate(
                confirm=args.confirm, pilot_confirm=args.pilot_confirm, profile=args.admin_profile
            )
        else:
            result = provision(
                confirm=args.confirm,
                admin_profile=args.admin_profile,
                runtime_profile=args.runtime_profile,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except PlatformAdminError as error:
        print(str(error))
        return 2
    except (Exception, KeyboardInterrupt):
        print("平台管理操作中断；请保留本机加密记录，原始异常已隐藏。")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
