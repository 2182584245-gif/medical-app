"""Explicit v2 -> v3 owned Aliyun schema upgrade; no default/implicit data migration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from psycopg import sql
from psycopg.pq import TransactionStatus

from server.platform_experience_schema import PLATFORM_SCHEMA, PLATFORM_TABLES
from server.platform_medical_schema import (
    ALTER_CATEGORY_SQL,
    CATEGORY_CHECK,
    OLD_CATEGORY_CHECK,
    category_check,
)

from . import bootstrap
from .guard import ADMIN, MARKER, RUNTIME, DeploymentError, container_gate, secret


def require(condition, reason):
    if not condition:
        raise DeploymentError(reason)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def safe_file(path, *, existing):
    path = Path(path).absolute()
    require(not any(part.is_symlink() for part in (path, *path.parents)), "Links are not allowed.")
    require(
        path.parent.is_dir() and path.exists() == existing, "New output or existing input required."
    )
    require(not existing or path.is_file(), "A regular input file is required.")
    return path


def write_report(path, value):
    path = safe_file(path, existing=False)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encode(value))
        stream.flush()
        os.fsync(stream.fileno())


def read_report(path):
    path = safe_file(path, existing=True)
    require(path.stat().st_size <= 65536, "Plan size is invalid.")
    return json.loads(path.read_bytes())


def data_fingerprint(connection):
    counts, hashes, size, count = {}, {}, 0, 0
    for table in sorted(PLATFORM_TABLES):
        cursor = connection.execute(
            sql.SQL(
                "SELECT to_jsonb(t)::text FROM {}.{} t ORDER BY to_jsonb(t)::text LIMIT 100001"
            ).format(sql.Identifier(PLATFORM_SCHEMA), sql.Identifier(table))
        )
        table_hash = hashlib.sha256()
        counts[table] = 0
        for row in cursor:
            raw = row[0].encode()
            size += len(raw)
            count += 1
            require(
                count <= 100000 and size <= 128 * 1024 * 1024,
                "Verification exceeds the bounded maintenance limit; no partial upgrade.",
            )
            table_hash.update(len(raw).to_bytes(8, "big") + raw)
            counts[table] += 1
        hashes[table] = table_hash.hexdigest()
    return {"counts": counts, "row_sha256": hashes}


def reviewed_plan(connection, deployment):
    # Check the OLD deployed DDL/catalog marker, not a newly invented baseline.
    bootstrap.verify_installation(connection, deployment, revision="platform_0002")
    require(
        category_check(connection) == (OLD_CATEGORY_CHECK, True), "Old category constraint differs."
    )
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
        "operation": "aliyun-medical-schema-upgrade",
        "status": "review_required",
        "deployment_id": deployment["id"],
        "from_revision": "platform_0002",
        "to_revision": "platform_0003",
        "old_catalog_sha256": bootstrap.catalog_digest(connection),
        "unchanged_catalog_sha256": bootstrap.catalog_digest(
            connection, exclude_medical_category=True
        ),
        "new_ddl_sha256": bootstrap.ddl_digest(),
        "data": data_fingerprint(connection),
        "only_change": "life_records_category_check adds medical",
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
        connection.execute("SET LOCAL statement_timeout='30s'")
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
        connection.execute("SET LOCAL statement_timeout='30s'")
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
        connection.execute(ALTER_CATEGORY_SQL)
        require(
            category_check(connection) == (CATEGORY_CHECK, True),
            "New category verification failed.",
        )
        require(
            bootstrap.catalog_digest(connection, exclude_medical_category=True)
            == plan["unchanged_catalog_sha256"],
            "Unrelated catalog changed; the entire upgrade is rolled back.",
        )
        require(
            data_fingerprint(connection) == plan["data"],
            "Existing rows changed; the entire upgrade is rolled back.",
        )
        marker = (
            f"{MARKER}:{deployment['id']}:{bootstrap.ddl_digest()}:"
            f"{bootstrap.catalog_digest(connection)}"
        )
        connection.execute(
            sql.SQL("COMMENT ON ROLE {} IS {}").format(sql.Identifier(RUNTIME), sql.Literal(marker))
        )
        bootstrap.verify_installation(connection, deployment)
    return {
        "status": "medical_schema_upgrade_committed",
        "plan_sha256": confirmation,
        "revision": "platform_0003",
        "all_existing_rows_identical": True,
        "runtime_password_rotated": False,
        "unrelated_permissions_changed": False,
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
        password = secret(Path("/run/db-secrets/bootstrap-password"))
        with bootstrap.connect(ADMIN, password) as connection:
            bootstrap.check_target(connection, ADMIN, deployment)
            # Identity check is read-only; begin a fresh operation transaction.
            connection.rollback()
            if args.action == "plan":
                result = plan_upgrade(connection, deployment)
                write_report(args.plan_file, result)
            elif args.action == "apply":
                result = apply_upgrade(
                    connection, deployment, read_report(args.plan_file), args.confirm_plan
                )
                write_report(args.result_file, result)
            else:
                bootstrap.verify_installation(connection, deployment)
                result = {"status": "medical_schema_v3_verified", "deployment_id": deployment["id"]}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception:
        print('{"status":"medical_upgrade_not_verified","automatic_retry":false}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
