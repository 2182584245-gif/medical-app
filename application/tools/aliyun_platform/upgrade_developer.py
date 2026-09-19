"""Explicit v3 -> v4 owned Aliyun upgrade; never replaces users or passwords."""

import argparse
import json
import os
from pathlib import Path

from psycopg import sql
from psycopg.pq import TransactionStatus

from server import platform_developer_schema as v4
from server.platform_developer_catalog import require_reviewed_v4
from server.platform_experience_schema import PLATFORM_MARKER, PLATFORM_SCHEMA, PLATFORM_TABLES

from . import bootstrap
from .guard import ADMIN, MARKER, RUNTIME, container_gate, secret
from .upgrade_medical import (
    data_fingerprint,
    digest,
    read_report,
    require,
    safe_file,
    write_report,
)


def reviewed_plan(connection, deployment):
    bootstrap.verify_installation(connection, deployment, revision="platform_0003")
    require(
        connection.execute(
            "SELECT count(*) FROM pg_event_trigger WHERE evtenabled<>'D'"
        ).fetchone()[0]
        == 0,
        "Unreviewed DDL event triggers prohibit this upgrade.",
    )
    require(
        connection.execute(
            "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=%s AND NOT t.tgisinternal",
            (PLATFORM_SCHEMA,),
        ).fetchone()[0]
        == 0,
        "Unreviewed table triggers prohibit this upgrade.",
    )
    plan = {
        "version": 1,
        "operation": "aliyun-developer-schema-upgrade",
        "status": "review_required",
        "deployment_id": deployment["id"],
        "from_revision": "platform_0003",
        "to_revision": "platform_0004",
        "old_catalog_sha256": bootstrap.catalog_digest(connection),
        "new_ddl_sha256": bootstrap.ddl_digest(revision="platform_0004"),
        "data": data_fingerprint(connection),
        "new_tables": sorted(v4.NEW_TABLES),
        "credentials_exported": False,
    }
    plan["plan_sha256"] = digest(plan)
    return plan


def plan_upgrade(connection, deployment):
    require(
        connection.info.transaction_status == TransactionStatus.IDLE, "Idle connection required."
    )
    with connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        connection.execute("SET LOCAL statement_timeout='60s'")
        return reviewed_plan(connection, deployment)


def apply_upgrade(connection, deployment, plan, confirmation):
    require(
        connection.info.transaction_status == TransactionStatus.IDLE, "Idle connection required."
    )
    require(
        type(confirmation) is str
        and len(confirmation) == 64
        and confirmation == plan.get("plan_sha256"),
        "The complete reviewed plan SHA256 is required.",
    )
    with connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        connection.execute("SET LOCAL lock_timeout='5s'")
        connection.execute("SET LOCAL statement_timeout='60s'")
        connection.execute("SELECT pg_advisory_xact_lock(717380026,4)")
        connection.execute(
            sql.SQL("LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE").format(
                sql.SQL(",").join(
                    sql.Identifier(PLATFORM_SCHEMA, table) for table in sorted(PLATFORM_TABLES)
                )
            )
        )
        require(
            reviewed_plan(connection, deployment) == plan,
            "Reviewed target or rows changed; new plan required.",
        )
        for statement in (*v4.TABLE_DDL, *v4.policy_ddl(), *v4.grant_ddl()):
            connection.execute(statement)
        for table in v4.NEW_TABLES:
            connection.execute(
                sql.SQL("COMMENT ON TABLE {} IS {}").format(
                    sql.Identifier(PLATFORM_SCHEMA, table), sql.Literal(PLATFORM_MARKER)
                )
            )
        require_reviewed_v4(connection)
        require(
            bootstrap.catalog_digest(connection, exclude_developer=True)
            == plan["old_catalog_sha256"],
            "Unrelated catalog changed; the entire upgrade is rolled back.",
        )
        require(
            data_fingerprint(connection) == plan["data"],
            "Existing rows changed; the entire upgrade is rolled back.",
        )
        for table in v4.NEW_TABLES:
            require(
                connection.execute(
                    sql.SQL("SELECT count(*) FROM {}").format(
                        sql.Identifier(PLATFORM_SCHEMA, table)
                    )
                ).fetchone()[0]
                == 0,
                "New developer tables must be empty; grants are separately provisioned.",
            )
        role_marker = (
            f"{MARKER}:{deployment['id']}:"
            f"{bootstrap.ddl_digest(revision='platform_0004')}:"
            f"{bootstrap.catalog_digest(connection)}"
        )
        connection.execute(
            sql.SQL("COMMENT ON ROLE {} IS {}").format(
                sql.Identifier(RUNTIME), sql.Literal(role_marker)
            )
        )
        bootstrap.verify_installation(connection, deployment, revision="platform_0004")
    return {
        "status": "developer_schema_upgrade_committed",
        "plan_sha256": confirmation,
        "revision": "platform_0004",
        "all_existing_rows_identical": True,
        "runtime_password_rotated": False,
        "developer_grant_created": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply", "status"))
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--plan-file", type=Path)
    parser.add_argument("--confirm-plan")
    parser.add_argument("--result-file", type=Path)
    args = parser.parse_args(argv)
    try:
        deployment = container_gate()
        require(
            os.geteuid() == 0 and args.deployment_id == deployment["id"],
            "Explicit root deployment identity required.",
        )
        if args.action in {"plan", "apply"}:
            require(args.plan_file is not None, "An explicit plan file is required.")
            safe_file(args.plan_file, existing=args.action == "apply")
        if args.action == "apply":
            require(args.result_file is not None, "A new result file is required.")
            safe_file(args.result_file, existing=False)
        with bootstrap.connect(ADMIN, secret(Path("/run/db-secrets/bootstrap-password"))) as conn:
            bootstrap.check_target(conn, ADMIN, deployment)
            conn.rollback()
            if args.action == "plan":
                result = plan_upgrade(conn, deployment)
                write_report(args.plan_file, result)
            elif args.action == "apply":
                result = apply_upgrade(
                    conn, deployment, read_report(args.plan_file), args.confirm_plan
                )
                write_report(args.result_file, result)
            else:
                bootstrap.verify_installation(conn, deployment, revision="platform_0004")
                result = {
                    "status": "developer_schema_v4_verified",
                    "deployment_id": deployment["id"],
                }
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception:
        print('{"status":"developer_upgrade_not_verified","automatic_retry":false}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
