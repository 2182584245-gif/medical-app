"""Explicit, project-bound pilot migration and read-only catalog verification.

No desktop records are imported. No downgrade, role change or public-schema DDL
is exposed here. Administrative secrets remain in DPAPI or this process memory.
"""

from __future__ import annotations

import argparse
import os
from contextlib import contextmanager
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL

from .cloud_connection import DEFAULT_PROFILE, ca_file, project_url, validated_url
from .local_secret_store import load_secret_payload
from .migrations.guards import REVISION_MARKER, SCHEMA_MARKER
from .migrations.versions.pilot_0001_private_pilot import TABLE_COLUMNS
from .schema import PRIVATE_SCHEMA


class CloudAdminError(RuntimeError):
    """Only fixed, non-secret messages may leave the management boundary."""


def management_url(profile: Path = DEFAULT_PROFILE) -> URL:
    try:
        payload = load_secret_payload(profile)
        if (
            set(payload) != {"project_url", "purpose", "migration_database_url"}
            or payload["project_url"] != project_url()
            or payload["purpose"] != "migration-readonly-preflight"
        ):
            raise ValueError
        url = validated_url(payload["migration_database_url"], project_url())
        if not url.password or url.query.get("sslmode") != "verify-full":
            raise ValueError
        return url.update_query_dict({"sslrootcert": ca_file(url.query.get("sslrootcert"))})
    except Exception:
        raise CloudAdminError("管理配置校验失败；未输出连接串或密码。") from None


@contextmanager
def readonly_connection(url: URL):
    with psycopg.connect(
        host=url.host,
        port=url.port,
        dbname=url.database,
        user=url.username,
        password=url.password,
        sslmode="verify-full",
        sslrootcert=url.query["sslrootcert"],
        connect_timeout=8,
        application_name="medical-app-catalog-preflight",
    ) as connection:
        connection.read_only = True
        try:
            if not connection.pgconn.ssl_in_use:
                raise CloudAdminError("未建立严格 TLS 连接，检查已停止。")
            connection.execute("SET LOCAL statement_timeout = '5000ms'")
            yield connection
        finally:
            connection.rollback()


def inspect_catalog(profile: Path = DEFAULT_PROFILE) -> dict:
    """Inspect only this pilot's metadata, not application/user table contents."""
    try:
        with readonly_connection(management_url(profile)) as connection:
            current_role = connection.execute("SELECT current_user").fetchone()[0]
            schema = connection.execute(
                "SELECT pg_catalog.pg_get_userbyid(nspowner), "
                "pg_catalog.obj_description(oid, 'pg_namespace') "
                "FROM pg_catalog.pg_namespace WHERE nspname = %s",
                (PRIVATE_SCHEMA,),
            ).fetchone()
            report = {
                "status": "catalog_readonly",
                "project_url": project_url(),
                "client_tls": True,
                "certificate_mode": "verify-full",
                "schema_exists": schema is not None,
                "tables_changed": False,
                "health_records_read": 0,
                "desktop_sync_enabled": False,
            }
            if schema is None:
                return report
            tables = connection.execute(
                "SELECT c.relname, pg_catalog.pg_get_userbyid(c.relowner), "
                "pg_catalog.obj_description(c.oid, 'pg_class'), "
                "c.relrowsecurity, c.relforcerowsecurity "
                "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
                "ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relkind='r' "
                "ORDER BY c.relname",
                (PRIVATE_SCHEMA,),
            ).fetchall()
            owned = schema == (current_role, SCHEMA_MARKER) and all(
                row[1] == current_role and row[2] == REVISION_MARKER for row in tables
            )
            report.update(
                schema_owned=owned,
                table_names=[row[0] for row in tables],
                expected_table_set={row[0] for row in tables} == set(TABLE_COLUMNS),
                row_level_security_enabled=any(row[3] or row[4] for row in tables),
            )
            # Never read even the version row from an unowned table/schema.
            if not owned or {row[0] for row in tables} != set(TABLE_COLUMNS):
                return report
            version = connection.execute(
                f"SELECT version_num FROM {PRIVATE_SCHEMA}.alembic_version"
            ).fetchall()
            grants = connection.execute(
                "SELECT r.rolname,c.relname, "
                "pg_catalog.has_schema_privilege(r.oid,n.oid,'USAGE'), "
                "pg_catalog.has_table_privilege(r.oid,c.oid,"
                "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE') "
                "FROM pg_catalog.pg_roles r CROSS JOIN pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                "WHERE r.rolname IN ('anon','authenticated','service_role') "
                "AND n.nspname=%s AND c.relkind='r' ORDER BY r.rolname,c.relname",
                (PRIVATE_SCHEMA,),
            ).fetchall()
            report.update(
                revisions=[row[0] for row in version],
                data_api_roles_no_access=len(grants) == 3 * len(TABLE_COLUMNS)
                and all(not row[2] and not row[3] for row in grants),
                data_api_privilege_checks=len(grants),
            )
            return report
    except CloudAdminError:
        raise
    except Exception:
        raise CloudAdminError("云端目录检查失败；原始数据库异常与凭据已隐藏。") from None


@contextmanager
def migration_environment(url: URL):
    values = {
        "SERVER_MIGRATION_CONFIRM": PRIVATE_SCHEMA,
        "SERVER_MIGRATION_DATABASE_URL": url.render_as_string(hide_password=False),
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


def migrate(*, confirm: str, profile: Path = DEFAULT_PROFILE) -> dict:
    if confirm != PRIVATE_SCHEMA:
        raise CloudAdminError("必须明确确认 medical_app_private 才能执行试点迁移。")
    before = inspect_catalog(profile)
    if before["schema_exists"] and not (
        before.get("schema_owned")
        and before.get("expected_table_set")
        and before.get("revisions") == ["pilot_0001"]
    ):
        raise CloudAdminError("发现未确认的已有数据库对象，未接管或覆盖。")
    try:
        config = Config(str(Path(__file__).with_name("alembic.ini")))
        with migration_environment(management_url(profile)):
            command.upgrade(config, "pilot_0001")
    except Exception:
        raise CloudAdminError(
            "迁移未报告成功，提交状态需重新只读核实；不要直接重复执行。原始异常已隐藏。"
        ) from None
    after = inspect_catalog(profile)
    if not (
        after.get("schema_owned")
        and after.get("expected_table_set")
        and after.get("revisions") == ["pilot_0001"]
        and after.get("data_api_roles_no_access")
    ):
        raise CloudAdminError("迁移后的权限或结构验收未通过，禁止启动公开服务。")
    after.update(
        status="pilot_migration_verified",
        schema_created=not before["schema_exists"],
        business_tables_created=0 if before["schema_exists"] else 4,
        tables_changed=not before["schema_exists"],
        historical_data_imported=False,
    )
    return after


def main() -> int:
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inspect", "migrate"))
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    try:
        result = migrate(confirm=args.confirm) if args.command == "migrate" else inspect_catalog()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except CloudAdminError as error:
        print(str(error))
        return 2
    except Exception:
        print("管理操作未完成；原始异常与凭据已隐藏。")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
