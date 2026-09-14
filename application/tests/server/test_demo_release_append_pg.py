"""Explicit local PG17 acceptance of the actual, strictly synthetic v1.5 demo.

No endpoint/credential input is accepted. The shared fixture owns a new tmpfs
container on the exact local Windows Docker named pipe. Production connection
validation is NOT relaxed: only this test replaces the CLI connector with that
already owned loopback connection. Passwords/tokens are never report fields.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image
from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from server import platform_transfer as transfer
from server.platform_auth import PlatformAuth
from server.platform_database import PlatformDatabase
from server.platform_experience_schema import runtime_grant_ddl
from server.platform_security import PlatformAuthSettings
from server.platform_transfer_package import generate_key
from tests.server import test_platform_transfer_pg as fixture_module
from tests.server.test_medical_records_pg import migrate_v3
from tests.server.test_platform_transfer import synthetic_source
from tools.aliyun_platform import bootstrap
from tools.aliyun_platform import transfer as cli
from tools.upgrade_synthetic_demo_v7 import contract

pg_cluster = fixture_module.pg_cluster
target_db = fixture_module.target_db
PROJECT = Path(__file__).resolve().parents[2]
SOURCE = PROJECT / "outputs" / "synthetic-demo-20260909"
SOURCE_DATABASE_SHA256 = "e051bdcb2872d408f27f8dcdf0e5d062dd7f82bd43301676b00ece5e66da6c6e"
GATE = "MEDICAL_APP_RUN_DEMO_APPEND_PG_TESTS"
REPORT_ENV = "MEDICAL_APP_DEMO_APPEND_REPORT_DIR"


def require(condition: bool, safe_message: str):
    # Do not let assertion introspection print business rows, hashes, or tokens.
    if not condition:
        raise RuntimeError(safe_message)


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_rows_preserved(before, after):
    for table, rows in before["rows"].items():
        require(all(row in after["rows"][table] for row in rows), "Existing target row changed.")


def security_counts(connection):
    with connection.transaction():
        return {
            table: connection.execute(
                sql.SQL("SELECT count(*) FROM {}.{}").format(
                    sql.Identifier(transfer.PLATFORM_SCHEMA), sql.Identifier(table)
                )
            ).fetchone()[0]
            for table in transfer.EXCLUDED[:3]
        }


def check_foreign_keys(connection):
    checked = 0
    with connection.transaction():
        for table, columns in transfer.FK.items():
            for column, parent in columns.items():
                count = connection.execute(
                    sql.SQL(
                        "SELECT count(*) FROM {}.{} child LEFT JOIN {}.{} parent "
                        "ON child.{}=parent.id WHERE child.{} IS NOT NULL AND parent.id IS NULL"
                    ).format(
                        sql.Identifier(transfer.PLATFORM_SCHEMA),
                        sql.Identifier(table),
                        sql.Identifier(transfer.PLATFORM_SCHEMA),
                        sql.Identifier(parent),
                        sql.Identifier(column),
                        sql.Identifier(column),
                    )
                ).fetchone()[0]
                require(count == 0, "Foreign-key orphan found.")
                checked += 1
        require(
            connection.execute(
                "SELECT count(*) FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=%s AND k.contype='f' AND NOT k.convalidated",
                (transfer.PLATFORM_SCHEMA,),
            ).fetchone()[0]
            == 0,
            "An unvalidated foreign key exists.",
        )
    return checked


def install_owned_runtime(connection, deployment, monkeypatch):
    """Same v3 catalog/marker/grants, with a fresh local-only bootstrap owner."""
    monkeypatch.setattr(bootstrap, "ADMIN", "postgres")
    runtime_password = secrets.token_hex(32)
    with connection.transaction():
        connection.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS NOINHERIT CONNECTION LIMIT 5 PASSWORD {}"
            ).format(sql.Identifier(bootstrap.RUNTIME), sql.Literal(runtime_password))
        )
        for statement in runtime_grant_ddl():
            connection.execute(statement)
        connection.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(connection.info.dbname)
            )
        )
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(connection.info.dbname), sql.Identifier(bootstrap.RUNTIME)
            )
        )
        connection.execute(
            sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                sql.Identifier(connection.info.dbname),
                sql.Literal("medical-app:aliyun:v1:" + deployment["id"]),
            )
        )
        marker = (
            f"{bootstrap.MARKER}:{deployment['id']}:"
            f"{bootstrap.ddl_digest()}:{bootstrap.catalog_digest(connection)}"
        )
        connection.execute(
            sql.SQL("COMMENT ON ROLE {} IS {}").format(
                sql.Identifier(bootstrap.RUNTIME), sql.Literal(marker)
            )
        )
        bootstrap.verify_installation(connection, deployment)
    return runtime_password


def write_report(report, plan, original_conflicts, choices, common):
    """Only public counts/identities/mappings/hashes; exclusive ignored output."""
    requested = os.environ.get(REPORT_ENV)
    if not requested:
        return
    destination = Path(requested).absolute()
    expected_parent = (PROJECT / "outputs").resolve()
    require(destination.parent == expected_parent, "Report must be a new direct outputs child.")
    require(not destination.exists(), "Report output already exists; no overwrite allowed.")
    require(
        not any(p.is_symlink() or p.is_junction() for p in (destination, *destination.parents)),
        "Report path cannot be a link or junction.",
    )
    payload = {
        "report.json": report,
        "reviewed-plan.json": plan,
        "blocked-conflicts.json": original_conflicts,
        "reviewed-choices.json": choices,
        "cloud-choices-template.json": {"entity_types": {"member_profile": "users"}},
    }
    # Prove plaintext credentials cannot enter any persisted JSON/plan/report.
    encoded = b"\n".join(transfer.serialize(value) for value in payload.values())
    require(
        all(value.encode() not in encoded for value in common.credentials(SOURCE).values()),
        "A credential was accidentally included in the report.",
    )
    require(b"$argon2" not in encoded and b"access_token" not in encoded, "Unsafe report field.")
    destination.mkdir(mode=0o700)
    for name, value in payload.items():
        cli.write_json(value, destination / name)
    cli.write_json(
        {name: checksum(destination / name) for name in sorted(payload)},
        destination / "SHA256SUMS.json",
    )


@pytest.mark.skipif(
    os.environ.get(GATE) != "synthetic-local-only",
    reason="explicit actual synthetic-demo acceptance gate required",
)
def test_actual_demo_append_to_conflicting_nonempty_pg17(
    target_db, pg_cluster, tmp_path, monkeypatch
):
    common = contract()
    common.verify_payload(SOURCE)
    initial_hashes = {name: checksum(SOURCE / name) for name in common.PAYLOAD_FILES}
    require(initial_hashes["data/app.db"] == SOURCE_DATABASE_SHA256, "Frozen demo DB changed.")
    source = transfer.sqlite_snapshot(
        SOURCE / "data/app.db", label="actual-synthetic-demo-v150", asset_root=SOURCE
    )
    require(len(source["assets"]) == 6, "All six real demo illustrations must be attached.")
    migrate_v3(target_db)

    # The pre-existing target has all 29 tables populated, a real binary original,
    # and three independent synthetic accounts. Deliberately collide IDs AND names.
    existing = synthetic_source(tmp_path / "existing-only-synthetic.db", "existing-local")
    existing["rows"]["users"][0]["username"] = "demo-operator"
    existing["rows"]["users"][0]["username_normalized"] = "demo-operator"
    existing["rows"]["products"][0]["sku"] = source["rows"]["products"][0]["sku"]
    existing["rows"]["orders"][0]["order_no"] = source["rows"]["orders"][0]["order_no"]
    fixture_module.copy_to(existing, target_db)
    deployment = {"id": uuid4().hex}
    runtime_password = install_owned_runtime(target_db, deployment, monkeypatch)
    target_assets = tmp_path / "target-images"
    target_assets.mkdir()
    target_label = "owned-local-aliyun-style-v3"

    @contextmanager
    def owned_local_target(_path):
        require(target_db.info.host == "127.0.0.1", "Only the owned local container is allowed.")
        require(target_db.info.port == pg_cluster["port"], "Unexpected local target port.")
        yield target_db, target_label

    monkeypatch.setattr(cli, "connect", owned_local_target)
    before = transfer.postgres_snapshot(target_db, label=target_label, asset_root=target_assets)
    before_security = security_counts(target_db)
    require(all(count == 0 for count in before_security.values()), "Unexpected security rows.")
    key = tmp_path / "independent-test-only-transfer.key"
    generate_key(key)
    args = SimpleNamespace(
        key_file=key,
        dpapi=False,
        choices=None,
        source_sqlite=SOURCE / "data/app.db",
        source_export=None,
        source_label="actual-synthetic-demo-v150",
        source_assets=SOURCE,
        target_assets=target_assets,
        target_profile=tmp_path / "never-read-cloud-profile.json",
        mode="empty-only",
        directory=tmp_path / "default-blocked",
    )
    blocked = cli.prepare(args)
    require(blocked["status"] == "blocked", "Nonempty target must refuse default import.")
    require(
        any(item["code"] == "target_not_empty" for item in blocked["conflicts"]),
        "Default refusal reason missing.",
    )
    args.confirm_plan = blocked["plan_sha256"]
    with pytest.raises(transfer.TransferError):
        cli.apply(args)
    args.mode, args.directory = "append-only", tmp_path / "append-blocked"
    blocked_append = cli.prepare(args)
    conflicts = blocked_append["conflicts"]
    require(blocked_append["status"] == "blocked", "Name collisions must not auto-merge.")
    require(
        {item.get("table") for item in conflicts if item["code"] == "unique_value_conflict"}
        == {"users", "products", "orders"},
        "Expected independent username/SKU/order collision gates missing.",
    )
    # These two real demo audits refer to member_profiles.user_id, not a separate
    # profile identity sequence. This semantic choice is explicit, never inferred.
    for item in conflicts:
        if item["code"] == "unknown_audit_entity":
            row = next(r for r in source["rows"]["audit_logs"] if r["id"] == item["source_pk"][0])
            require(row["entity_type"] == "member_profile", "Unexpected audit review needed.")
            require(
                row["entity_id"] in {r["user_id"] for r in source["rows"]["member_profiles"]},
                "Profile audit does not reference the source user PK.",
            )
    choices = {
        "renames": {
            "users": {"1": "demo-operator-imported-local"},
            "products": {"1": "demo-product-imported-local"},
            "orders": {"1": "demo-order-imported-local"},
        },
        "entity_types": {"member_profile": "users"},
    }
    choice_file = tmp_path / "explicit-choices.json"
    cli.write_json(choices, choice_file)
    args.choices, args.directory = choice_file, tmp_path / "reviewed-append"
    reviewed = cli.prepare(args)
    require(reviewed["status"] == "review_required", "Reviewed append still blocked.")
    require(reviewed["backups_authenticated_readback"], "Authenticated backups missing.")
    require(
        cli.status(args)["status"] == "not_applied_target_unchanged", "Preflight changed target."
    )
    plan, packaged_source, backup, transformed = cli.load_plan(args)
    require(packaged_source == source and backup == before, "Encrypted package readback differs.")
    require(plan["source_counts"] == common.COUNTS, "Actual demo exact counts differ.")
    require(plan["target"]["record_schema"] == 7, "Target medical schema missing.")
    require(
        plan["target"]["deployment_marker"] == "medical-app:aliyun:v1:" + deployment["id"],
        "Owned deployment identity missing.",
    )
    args.confirm_plan = "0" * 64
    with pytest.raises(transfer.TransferError):
        cli.apply(args)
    require(
        transfer.postgres_snapshot(target_db, label=target_label, asset_root=target_assets)
        == before,
        "Preflight or rejected apply changed the target.",
    )
    args.confirm_plan = plan["plan_sha256"]
    result = cli.apply(args)
    require(result["status"] == "committed", "Append did not commit.")
    require(cli.status(args)["status"] == "all_planned_rows_present", "Post-commit status differs.")
    after = transfer.postgres_snapshot(target_db, label=target_label, asset_root=target_assets)
    require_rows_preserved(before, after)
    for table, imported in transformed.items():
        require(
            len(after["rows"][table]) == len(before["rows"][table]) + common.COUNTS[table],
            "Post-import per-table count differs.",
        )
        require(
            all(row in after["rows"][table] for row in imported), "Remapped imported row differs."
        )
    require(security_counts(target_db) == before_security, "Transfer copied security state.")
    foreign_keys = check_foreign_keys(target_db)
    for product in transformed["products"]:
        image_path = target_assets / product["image_path"]
        require(image_path.resolve().is_relative_to(target_assets), "Image escaped its owned root.")
        asset = next(
            a for a in plan["external_assets"] if a["destination"] == product["image_path"]
        )
        require(checksum(image_path) == asset["sha256"], "Transferred illustration hash differs.")
        with Image.open(image_path) as image:
            image.verify()
    with pytest.raises(transfer.TransferError):
        cli.apply(args)
    require_rows_preserved(
        after, transfer.postgres_snapshot(target_db, label=target_label, asset_root=target_assets)
    )

    # Real application auth under the least-privilege runtime role (not superuser).
    # Only imported users are logged in, so every pre-existing target row must
    # remain exact even after the normal imported-user last_login_at updates.
    url = URL.create(
        "postgresql+psycopg",
        username=bootstrap.RUNTIME,
        password=runtime_password,
        host="127.0.0.1",
        port=pg_cluster["port"],
        database=target_db.info.dbname,
    )
    database = PlatformDatabase(engine=create_engine(url, hide_parameters=True))
    login_report = []
    try:
        database.initialize()
        auth = PlatformAuth(database, PlatformAuthSettings(token_pepper=secrets.token_hex(32)))
        readiness = auth.check_ready()
        require(readiness["production_verified"], "Runtime readiness failed.")
        for username, password in common.credentials(SOURCE).items():
            original = next(row for row in source["rows"]["users"] if row["username"] == username)
            new_id = plan["id_mapping"]["users"][str(original["id"])]
            imported = next(row for row in transformed["users"] if row["id"] == new_id)
            login = auth.login(imported["username"], password, client_ip="synthetic-local-only")
            require(
                login["user"]["id"] == new_id, "Login resolved the pre-existing conflicting user."
            )
            require(login["user"]["role_code"] == original["role_code"], "Imported role changed.")
            principal = auth.resolve_token(login["access_token"])
            require(principal.user.id == new_id, "Token did not resolve the imported identity.")
            auth.logout(principal)
            login_report.append(
                {
                    "source_username": username,
                    "target_username": imported["username"],
                    "source_id": original["id"],
                    "target_id": new_id,
                    "role": original["role_code"],
                    "original_password_login": "passed",
                    "token_resolution_and_logout": "passed",
                }
            )
            del password, login, principal
    finally:
        database.close()
    final = transfer.postgres_snapshot(target_db, label=target_label, asset_root=target_assets)
    require_rows_preserved(before, final)
    require(check_foreign_keys(target_db) == foreign_keys, "FK contract changed during auth.")
    require(
        {name: checksum(SOURCE / name) for name in common.PAYLOAD_FILES} == initial_hashes,
        "Original frozen demo payload changed.",
    )
    common.verify_payload(SOURCE)
    report = {
        "status": "passed",
        "verified_at": datetime.now(UTC).isoformat(),
        "scope": "new owned loopback PG17 tmpfs container only; no cloud/AI/network API",
        "source_database_sha256": initial_hashes["data/app.db"],
        "source_schema": 7,
        "target_revision": "platform_0003",
        "business_tables": 29,
        "target_nonempty": True,
        "default_import_refused": True,
        "unreviewed_name_conflicts_refused": True,
        "wrong_plan_sha_refused": True,
        "duplicate_apply_refused": True,
        "no_identity_merge": True,
        "explicit_profile_audit_entity_mapping": "member_profile -> users",
        "source_counts": plan["source_counts"],
        "source_record_categories": dict(
            Counter(row["category"] for row in source["rows"]["life_records"])
        ),
        "existing_counts": plan["target_counts"],
        "post_import_counts": {table: len(rows) for table, rows in after["rows"].items()},
        "all_imported_rows_exact_after_declared_remapping": True,
        "all_existing_29_table_rows_unchanged_after_transfer_and_login": True,
        "source_payload_unchanged": True,
        "independent_aead_packages_readback": True,
        "security_tables_not_copied": True,
        "fk_relations_checked": foreign_keys,
        "all_six_product_images_resolve_and_verify": True,
        "product_assets": plan["external_assets"],
        "existing_original_binary_preserved": True,
        "five_original_demo_passwords_verified_by_runtime_auth": login_report,
        "runtime_readiness": readiness,
        "plan_sha256": plan["plan_sha256"],
        "transport_note": (
            "Local fixture uses loopback, not TLS; production TLS connector unchanged."
        ),
        "cleanup": "Fixture removes only its labeled container and network; no cloud resources.",
    }
    write_report(report, plan, conflicts, choices, common)
