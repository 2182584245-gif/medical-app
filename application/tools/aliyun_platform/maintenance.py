"""Owned PostgreSQL backups and disposable restore verification; never prune automatically."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import uuid
from datetime import UTC, datetime, timedelta

from server.platform_developer_schema import NEW_TABLES as DEVELOPER_TABLES
from server.platform_experience_schema import PLATFORM_TABLES

from .guard import ADMIN, DATABASE, MARKER, PROJECT, ROOT, DeploymentError, host_root, marker


def _compose(*args: str, stdin: str | None = None, timeout: int = 1800) -> str:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-directory",
            str(ROOT),
            "--project-name",
            PROJECT,
            "-f",
            str(ROOT / "compose.yaml"),
            *args,
        ],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    )
    return result.stdout.strip()


def _pg(database: str, command: str) -> str:
    return _compose(
        "exec",
        "-T",
        "-u",
        "0:0",
        "-e",
        "PGPASSFILE=/run/db-secrets/pgpass",
        "-e",
        "PGSSLMODE=verify-full",
        "-e",
        "PGSSLROOTCERT=/run/db-secrets/ca.crt",
        "postgres",
        "psql",
        "-X",
        "-w",
        "-h",
        "postgres",
        "-U",
        ADMIN,
        "-d",
        database,
        "-v",
        "ON_ERROR_STOP=1",
        "-At",
        stdin=command,
        timeout=120,
    )


def _identity(deployment: dict) -> None:
    expected = "|".join((DATABASE, ADMIN, deployment["id"], f"{MARKER}:{deployment['id']}"))
    result = _pg(
        DATABASE,
        "SELECT current_database(),current_user,"
        "current_setting('medical_app.deployment_id',true),"
        "shobj_description(oid,'pg_database') FROM pg_database "
        "WHERE datname=current_database();",
    )
    if result != expected:
        raise DeploymentError("Backup target ownership differs.")


# Definitions only, not business contents. Comparing source/dump-restored/source
# catches concurrent DDL and verifies types, defaults, constraints, expressions,
# privileges, indexes, comments and ownership rather than only table counts.
CATALOG_SQL = """
SELECT json_build_object(
 'relations',(SELECT json_agg(x ORDER BY relname) FROM (
   SELECT c.relname,c.relkind,pg_get_userbyid(c.relowner) AS owner,
          c.relrowsecurity,c.relforcerowsecurity,c.relacl::text,
          obj_description(c.oid,'pg_class') AS comment
   FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname='medical_app_platform') x),
 'columns',(SELECT json_agg(x ORDER BY relname,attnum) FROM (
   SELECT c.relname,a.attname,a.attnum,format_type(a.atttypid,a.atttypmod) AS type,
          a.attnotnull,a.attidentity,a.attgenerated,a.attacl::text,
          pg_get_expr(d.adbin,d.adrelid,false) AS default_expr
   FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
   JOIN pg_namespace n ON n.oid=c.relnamespace
   LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
   WHERE n.nspname='medical_app_platform' AND a.attnum>0 AND NOT a.attisdropped) x),
 'constraints',(SELECT json_agg(x ORDER BY relname,conname) FROM (
   SELECT c.relname,k.conname,k.convalidated,pg_get_constraintdef(k.oid,false) AS definition
   FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
   JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='medical_app_platform') x),
 'policies',(SELECT json_agg(x ORDER BY tablename,policyname) FROM (
   SELECT tablename,policyname,permissive,roles::text,cmd,qual,with_check
   FROM pg_policies WHERE schemaname='medical_app_platform') x),
 'indexes',(SELECT json_agg(x ORDER BY indexname) FROM (
   SELECT indexname,indexdef FROM pg_indexes WHERE schemaname='medical_app_platform') x),
 'schema',(SELECT json_agg(x) FROM (
   SELECT pg_get_userbyid(nspowner) AS owner,nspacl::text,
          obj_description(oid,'pg_namespace') AS comment
   FROM pg_namespace WHERE nspname='medical_app_platform') x),
 'default_acl',(SELECT json_agg(x ORDER BY owner,defaclobjtype) FROM (
   SELECT pg_get_userbyid(a.defaclrole) AS owner,a.defaclobjtype,a.defaclacl::text
   FROM pg_default_acl a JOIN pg_namespace n ON n.oid=a.defaclnamespace
   WHERE n.nspname='medical_app_platform') x),
 'triggers',(SELECT json_agg(x ORDER BY relname,tgname) FROM (
   SELECT c.relname,t.tgname,pg_get_triggerdef(t.oid,false) AS definition
   FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
   JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname='medical_app_platform' AND NOT t.tgisinternal) x),
 'functions',(SELECT json_agg(x ORDER BY name) FROM (
   SELECT p.oid::regprocedure::text AS name,pg_get_functiondef(p.oid) AS definition
   FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
   WHERE n.nspname='medical_app_platform') x)
);
"""


def _catalog(database):
    value = json.loads(_pg(database, CATALOG_SQL))
    tables = [row for row in value["relations"] if row["relkind"] == "r"]
    names = {row["relname"] for row in tables}
    if names not in (set(PLATFORM_TABLES), set(PLATFORM_TABLES) | set(DEVELOPER_TABLES)):
        raise DeploymentError("Backup table set is not a reviewed v3/v4 set.")
    if any(
        row["owner"] != ADMIN or not row["relrowsecurity"] or not row["relforcerowsecurity"]
        for row in tables
    ):
        raise DeploymentError("Backup ownership or forced RLS differs.")
    if value["triggers"] or value["functions"]:
        raise DeploymentError("Unreviewed functions or triggers prohibit backup verification.")
    return value


def capacity() -> dict:
    host_root()
    marker(ROOT / "deployment.json")
    usage = shutil.disk_usage(ROOT)
    percent = round(100 * usage.used / usage.total, 1)
    now = datetime.now(UTC)
    verified = []
    for path in (ROOT / "backups").glob("*.verified.json"):
        if path.is_symlink():
            continue
        try:
            record = json.loads(path.read_text())
            instant = datetime.fromisoformat(record["verified_at"])
            if instant.tzinfo is not None:
                verified.append(instant)
        except (ValueError, KeyError, TypeError):
            continue
    alerts = []
    if percent >= 85 or usage.free < 2 * 1024**3:
        alerts.append("disk_capacity_low")
    if not verified or now - max(verified) > timedelta(hours=36):
        alerts.append("no_recent_verified_backup")
    for folder in ("postgres", "api-aliyun"):
        certificate = ROOT / "secrets" / folder / "server.crt"
        result = subprocess.run(
            ["openssl", "x509", "-checkend", str(30 * 86400), "-noout", "-in", str(certificate)],
            capture_output=True,
            timeout=10,
        )
        if result.returncode:
            alerts.append(folder + "_internal_certificate_due")
    return dict(
        status="attention" if alerts else "ok",
        disk_used_percent=percent,
        free_bytes=usage.free,
        verified_backup_count=len(verified),
        alerts=alerts,
        automatic_deletion=False,
    )


def backup() -> dict:
    host_root()
    deployment = marker(ROOT / "deployment.json")
    backup_root = ROOT / "backups"
    if backup_root.is_symlink() or backup_root.resolve() != ROOT / "backups":
        raise DeploymentError("Backup directory must be the fixed deployment subdirectory.")
    _identity(deployment)
    source_catalog = _catalog(DATABASE)
    nonce = uuid.uuid4().hex
    stem = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + nonce
    archive = backup_root / (stem + ".dump")
    verification_db = "medical_app_verify_" + nonce
    ownership = f"{MARKER}:{deployment['id']}:verification:{nonce}"
    if archive.exists():
        raise DeploymentError("A backup filename already exists.")
    # Password is only in a root-only mounted pgpass file, never argv or logs.
    _compose(
        "exec",
        "-T",
        "-u",
        "0:0",
        "-e",
        "PGPASSFILE=/run/db-secrets/pgpass",
        "-e",
        "PGSSLMODE=verify-full",
        "-e",
        "PGSSLROOTCERT=/run/db-secrets/ca.crt",
        "postgres",
        "pg_dump",
        "-w",
        "-h",
        "postgres",
        "-U",
        ADMIN,
        "-d",
        DATABASE,
        "--format=custom",
        "--file=/backups/" + archive.name,
    )
    archive.chmod(0o600)
    _pg(
        DATABASE,
        f"CREATE DATABASE {verification_db} OWNER {ADMIN} TEMPLATE template0;\n"
        f"COMMENT ON DATABASE {verification_db} IS '{ownership}';\n"
        f"REVOKE ALL ON DATABASE {verification_db} FROM PUBLIC;",
    )
    _compose(
        "exec",
        "-T",
        "-u",
        "0:0",
        "-e",
        "PGPASSFILE=/run/db-secrets/pgpass",
        "-e",
        "PGSSLMODE=verify-full",
        "-e",
        "PGSSLROOTCERT=/run/db-secrets/ca.crt",
        "postgres",
        "pg_restore",
        "-w",
        "-h",
        "postgres",
        "-U",
        ADMIN,
        "--dbname=" + verification_db,
        "--exit-on-error",
        "--single-transaction",
        "/backups/" + archive.name,
    )
    tables = _pg(
        verification_db,
        "SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='medical_app_platform' AND c.relkind='r' ORDER BY c.relname;",
    )
    rows = [line.split("|") for line in tables.splitlines()]
    expected_tables = {
        row["relname"] for row in source_catalog["relations"] if row["relkind"] == "r"
    }
    if {row[0] for row in rows} != expected_tables or any(
        len(row) != 3 or row[1:] != ["t", "t"] for row in rows
    ):
        raise DeploymentError(
            "Restored structure failed; archive and verification database retained."
        )
    if _catalog(verification_db) != source_catalog or _catalog(DATABASE) != source_catalog:
        raise DeploymentError(
            "Restored catalog or live DDL changed; archive and verification database retained."
        )
    counts = {}
    for row in rows:
        table = row[0]
        if not re.fullmatch(r"[a-z_]+", table):
            raise DeploymentError("Unexpected restored table identifier.")
        counts[table] = int(
            _pg(verification_db, f"SELECT count(*) FROM medical_app_platform.{table};")
        )
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    # Delete only this run's successful disposable restore, after rechecking marker.
    actual = _pg(
        DATABASE,
        "SELECT shobj_description(oid,'pg_database') FROM pg_database "
        f"WHERE datname='{verification_db}';",
    )
    if actual != ownership:
        raise DeploymentError("Verification database ownership changed; no deletion performed.")
    _pg(DATABASE, f"DROP DATABASE {verification_db};")
    report = dict(
        verified_at=datetime.now(UTC).isoformat(),
        archive=archive.name,
        sha256=digest,
        restored_table_count=len(rows),
        restored_catalog_matches_source=True,
        restored_row_counts=counts,
        restore_database_removed=True,
        automatic_archive_deletion=False,
    )
    with (backup_root / (stem + ".verified.json")).open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["backup", "capacity"])
    args = parser.parse_args()
    try:
        report = backup() if args.action == "backup" else capacity()
        print(json.dumps(report))
        return 1 if report.get("alerts") else 0
    except Exception:
        print("Maintenance failed; existing backups and failed restore databases are retained.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
