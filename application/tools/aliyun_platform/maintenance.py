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
    for folder in ("postgres", "api-aliyun", "api-supabase"):
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
    if {row[0] for row in rows} != set(PLATFORM_TABLES) or any(
        len(row) != 3 or row[1:] != ["t", "t"] for row in rows
    ):
        raise DeploymentError(
            "Restored structure failed; archive and verification database retained."
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
