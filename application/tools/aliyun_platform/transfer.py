"""Explicit full-platform administrator migration, never a desktop or public endpoint."""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

import psycopg

from server.platform_transfer import (
    SAFE_FAILURE,
    TransferError,
    apply_transfer,
    digest,
    plan_transfer,
    postgres_snapshot,
    serialize,
    sqlite_snapshot,
)
from server.platform_transfer_assets import stage_assets
from server.platform_transfer_package import (
    generate_key,
    load_package,
    package_context,
    read_key,
    safe_path,
    save_package,
)


def read_json(path: Path, *, limit=64 * 1024 * 1024):
    path = safe_path(path, exists=True)
    if path.stat().st_size > limit:
        raise TransferError("管理员计划或配置文件过大。")

    def unique(items):
        result = {}
        for name, value in items:
            if name in result:
                raise TransferError("JSON 字段重复，拒绝不明确的管理员配置。")
            result[name] = value
        return result

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)


def _secret_file(path: Path, *, deployed_postgres_secret: bool = False) -> str:
    path = safe_path(path, exists=True)
    info = path.stat()
    # The reviewed Aliyun prepare tool creates this ONE container-mounted file
    # as postgres UID999/0400. Do not accept UID999 for arbitrary profile paths.
    owners = {0, 999} if deployed_postgres_secret else {0}
    if os.name == "posix" and (info.st_uid not in owners or stat.S_IMODE(info.st_mode) & 0o077):
        raise TransferError("Linux 密码文件需专用管理/部署所有者，且禁止组或其他用户访问。")
    if not 1 <= info.st_size <= 1024:
        raise TransferError("连接密码文件大小无效。")
    value = path.read_text(encoding="utf-8").removesuffix("\n").removesuffix("\r")
    if not value or any(ord(char) < 32 for char in value):
        raise TransferError("密码文件内容无效，未输出内容。")
    return value


def connection_options(profile: dict) -> dict:
    """Fixed libpq keyword allowlist; no DSN, PG environment, SQL or relaxed TLS."""
    if any(name.upper().startswith("PG") for name in os.environ):
        raise TransferError("禁止继承 libpq 环境选项，请在干净管理员容器执行。")
    if set(profile) != {
        "kind",
        "label",
        "host",
        "port",
        "database",
        "user",
        "password_file",
        "sslrootcert",
    }:
        raise TransferError("连接配置只能包含公开目标身份与密码/CA文件路径，不接受直接连接串。")
    if not isinstance(profile["label"], str) or not 1 <= len(profile["label"]) <= 100:
        raise TransferError("管理员连接必须使用明确名称。")
    if profile["port"] != 5432:
        raise TransferError("管理员迁移仅支持 5432 直接或会话连接，禁止事务池。")
    host, user = profile["host"], profile["user"]
    if profile["kind"] == "aliyun":
        if (
            host != "postgres"
            or profile["database"] != "medical_app_aliyun"
            or user != "aliyun_bootstrap"
        ):
            raise TransferError("阿里云迁移仅在维护容器使用内部 postgres 管理连接。")
    elif profile["kind"] == "supabase":
        direct = re.fullmatch(r"db\.[a-z0-9]{20}\.supabase\.co", str(host))
        pooler = re.fullmatch(r"aws-[a-z0-9-]+\.pooler\.supabase\.com", str(host))
        if (
            not (direct or pooler)
            or profile["database"] != "postgres"
            or not re.fullmatch(r"postgres(?:\.[a-z0-9]{20})?", str(user))
        ):
            raise TransferError("Supabase 管理端点、数据库或用户名格式不匹配。")
    else:
        raise TransferError("只支持明确选择的阿里云或 Supabase 管理目标。")
    certificate = safe_path(Path(profile["sslrootcert"]), exists=True)
    if certificate.stat().st_size > 128 * 1024:
        raise TransferError("CA 文件过大。")
    pem = certificate.read_text(encoding="utf-8")
    if "PRIVATE KEY" in pem:
        raise TransferError("CA 文件不得包含私钥。")
    ssl.create_default_context(cadata=pem)
    return {
        "host": host,
        "port": 5432,
        "dbname": profile["database"],
        "user": user,
        "password": _secret_file(
            Path(profile["password_file"]),
            deployed_postgres_secret=(
                profile["kind"] == "aliyun"
                and Path(profile["password_file"]) == Path("/run/db-secrets/bootstrap-password")
            ),
        ),
        "sslmode": "verify-full",
        "sslrootcert": str(certificate),
        "connect_timeout": 15,
        "application_name": "healthlife-explicit-admin-transfer",
    }


@contextmanager
def connect(profile_path: Path):
    profile = read_json(profile_path, limit=16384)
    options = connection_options(profile)
    try:
        with psycopg.connect(**options) as connection:
            if not connection.pgconn.ssl_in_use:
                raise TransferError("目标连接未建立严格 TLS。")
            yield connection, profile["label"]
    except TransferError:
        raise
    except Exception:
        raise TransferError("管理员数据库操作未完成；原始错误和凭据已隐藏。") from None


@contextmanager
def connect_windows_management(profile_path: Path):
    """The Supabase management credential stays in this Windows process only."""
    if os.name != "nt" or any(name.upper().startswith("PG") for name in os.environ):
        raise TransferError("DPAPI 管理源仅限原 Windows 账户且禁止 libpq 环境覆盖。")
    from server.cloud_connection import ca_file
    from server.platform_admin import management_url

    try:
        uri = management_url(profile_path)
        with psycopg.connect(
            host=uri.host,
            port=5432,
            dbname=uri.database,
            user=uri.username,
            password=uri.password,
            sslmode="verify-full",
            sslrootcert=ca_file(uri.query.get("sslrootcert")),
            connect_timeout=15,
            application_name="healthlife-readonly-admin-export",
        ) as connection:
            if not connection.pgconn.ssl_in_use:
                raise TransferError("源连接没有建立严格 TLS。")
            yield connection
    except TransferError:
        raise
    except Exception:
        raise TransferError("Windows 管理源读取未完成；凭据及原始错误已隐藏。") from None


def write_json(value, destination: Path):
    target = safe_path(destination, exists=False)
    with target.open("xb") as stream:
        if os.name == "posix":
            os.fchmod(stream.fileno(), 0o600)
        stream.write(serialize(value))
        stream.flush()
        os.fsync(stream.fileno())


def _bundle_key(args):
    if args.key_file is not None:
        return read_key(args.key_file)
    if not args.dpapi or os.name != "nt":
        raise TransferError("必须选择独立 AES-GCM key 文件；Windows 可显式选择 DPAPI。")
    return None


def _new_directory(path):
    path = Path(path).absolute()
    safe_path(path, exists=False)
    path.mkdir(mode=0o700)
    return path


def export_context(manifest):
    return {
        "purpose": "source",
        "plan_sha256": manifest["export_sha256"],
        "source_sha256": manifest["source_sha256"],
        "target_sha256": digest(manifest["expected_target"]),
    }


def export(args):
    """Read-only Windows DPAPI source -> independent-key authenticated envelope."""
    if not re.fullmatch(r"[a-f0-9]{32}", args.target_deployment_id):
        raise TransferError("请明确核对目标阿里云部署的 32 位身份标记。")
    key = read_key(args.key_file)
    with connect_windows_management(args.source_management_profile) as connection:
        source = postgres_snapshot(
            connection, label=args.source_label, asset_root=args.source_assets
        )
    manifest = {
        "version": 1,
        "operation": "export-platform-snapshot",
        "source": source["identity"],
        "source_counts": {table: len(rows) for table, rows in source["rows"].items()},
        "source_sha256": digest(source),
        "expected_target": {"kind": "aliyun", "deployment_id": args.target_deployment_id},
    }
    manifest["export_sha256"] = digest(manifest)
    directory = _new_directory(args.directory)
    save_package(source, directory / "source.hltransfer", context=export_context(manifest), key=key)
    write_json(manifest, directory / "export.json")
    return {
        "status": "source_exported_target_not_connected",
        **manifest,
        "credentials_exported": False,
        "source_changed": False,
        "encrypted_authenticated_readback": True,
    }


def load_export(directory, key):
    manifest = read_json(directory / "export.json", limit=1024 * 1024)
    if (
        not isinstance(manifest, dict)
        or set(manifest)
        != {
            "version",
            "operation",
            "source",
            "source_counts",
            "source_sha256",
            "expected_target",
            "export_sha256",
        }
        or manifest["version"] != 1
        or manifest["operation"] != "export-platform-snapshot"
        or manifest["export_sha256"]
        != digest({name: value for name, value in manifest.items() if name != "export_sha256"})
    ):
        raise TransferError("源导出清单格式或身份校验失败。")
    expected = manifest["expected_target"]
    if (
        not isinstance(expected, dict)
        or set(expected) != {"kind", "deployment_id"}
        or expected["kind"] != "aliyun"
        or not re.fullmatch(r"[a-f0-9]{32}", str(expected["deployment_id"]))
    ):
        raise TransferError("源包未绑定可核对的阿里云部署身份。")
    source = load_package(
        directory / "source.hltransfer", context=export_context(manifest), key=key
    )
    if (
        digest(source) != manifest["source_sha256"]
        or source["identity"] != manifest["source"]
        or {table: len(rows) for table, rows in source["rows"].items()} != manifest["source_counts"]
    ):
        raise TransferError("源包内容与已认证清单不一致。")
    return source, expected


def prepare(args):
    key = _bundle_key(args)
    expected_target = None
    choices = read_json(args.choices, limit=1024 * 1024) if args.choices else {}
    if set(choices) - {"renames", "json_id_fields", "entity_types"}:
        raise TransferError("审阅文件只能包含固定冲突选择，不能包含数据或 SQL。")
    if getattr(args, "source_export", None):
        if key is None:
            raise TransferError("跨系统源导出必须使用独立 AES-GCM key。")
        source, expected_target = load_export(args.source_export, key)
    elif args.source_sqlite:
        source = sqlite_snapshot(
            args.source_sqlite, label=args.source_label, asset_root=args.source_assets
        )
    else:
        with connect(args.source_profile) as (connection, label):
            source = postgres_snapshot(connection, label=label, asset_root=args.source_assets)
    with connect(args.target_profile) as (connection, label):
        target = postgres_snapshot(connection, label=label, asset_root=args.target_assets)
    if expected_target is not None and target["identity"].get("deployment_marker") != (
        "medical-app:aliyun:v1:" + expected_target["deployment_id"]
    ):
        raise TransferError("实际目标部署与 Windows 源包确认的身份不同；未写入任何目标数据。")
    plan, _rows = plan_transfer(source, target, mode=args.mode, **choices)
    directory = _new_directory(args.directory)
    save_package(
        source, directory / "source.hltransfer", context=package_context(plan, "source"), key=key
    )
    save_package(
        target,
        directory / "target-before.hltransfer",
        context=package_context(plan, "target-backup"),
        key=key,
    )
    write_json(plan, directory / "plan.json")
    return {
        "status": plan["status"],
        "plan_sha256": plan["plan_sha256"],
        "source": plan["source"],
        "target": plan["target"],
        "source_counts": plan["source_counts"],
        "target_counts": plan["target_counts"],
        "conflicts": plan["conflicts"],
        "external_assets": len(plan["external_assets"]),
        "backups_authenticated_readback": True,
        "target_changed": False,
        "next_step": "review plan.json; apply requires the complete exact plan SHA256",
    }


def load_plan(args):
    key = _bundle_key(args)
    plan = read_json(args.directory / "plan.json")
    source = load_package(
        args.directory / "source.hltransfer", context=package_context(plan, "source"), key=key
    )
    target = load_package(
        args.directory / "target-before.hltransfer",
        context=package_context(plan, "target-backup"),
        key=key,
    )
    rebuilt, transformed = plan_transfer(source, target, mode=plan["mode"], **plan["choices"])
    if rebuilt != plan:
        raise TransferError("计划、源数据或预先备份已改变；禁止继续。")
    return plan, source, target, transformed


def apply(args):
    plan, source, target, _rows = load_plan(args)
    if args.confirm_plan != plan["plan_sha256"] or plan["status"] != "review_required":
        raise TransferError("存在冲突或未明确确认完整计划 SHA256，未执行迁移。")
    with connect(args.target_profile) as (connection, label):
        current = postgres_snapshot(connection, label=label, asset_root=args.target_assets)
        if digest(current) != plan["target_sha256"]:
            raise TransferError("实际目标与计划不一致；未写入业务或图片。")
        staged = stage_assets(source, plan, args.target_assets)
        result = apply_transfer(
            connection,
            source,
            target,
            plan,
            confirm_sha256=args.confirm_plan,
            target_asset_root=args.target_assets,
        )
    result["assets"] = staged
    write_json(result, args.directory / ("committed-" + uuid.uuid4().hex + ".json"))
    return result


def status(args):
    plan, _source, target, transformed = load_plan(args)
    with connect(args.target_profile) as (connection, label):
        current = postgres_snapshot(connection, label=label, asset_root=args.target_assets)
    if current["identity"] != plan["target"]:
        raise TransferError("查询目标与确认的迁移计划身份不一致。")
    if digest(current) == plan["target_sha256"]:
        state = "not_applied_target_unchanged"
    else:
        expected = {table: target["rows"][table] + transformed[table] for table in transformed}
        unchanged = all(
            all(row in current["rows"][table] for row in rows) for table, rows in expected.items()
        )
        state = "all_planned_rows_present" if unchanged else "target_changed_requires_manual_review"
    return {
        "status": state,
        "plan_sha256": plan["plan_sha256"],
        "source": plan["source"],
        "target": plan["target"],
        "target_changed_by_status_check": False,
        "automatic_retry_performed": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    keygen = commands.add_parser("keygen", help="generate an independent 32-byte administrator key")
    keygen.add_argument("--key-file", type=Path, required=True)
    exporter = commands.add_parser(
        "export", help="Windows DPAPI source -> encrypted Linux source package"
    )
    exporter.add_argument("--source-management-profile", type=Path, required=True)
    exporter.add_argument("--source-label", required=True)
    exporter.add_argument("--source-assets", type=Path)
    exporter.add_argument("--target-deployment-id", required=True)
    exporter.add_argument("--directory", type=Path, required=True)
    exporter.add_argument("--key-file", type=Path, required=True)
    for name in ("prepare", "apply", "status"):
        command = commands.add_parser(name)
        command.add_argument("--directory", type=Path, required=True)
        command.add_argument("--target-profile", type=Path, required=True)
        command.add_argument("--target-assets", type=Path)
        cipher = command.add_mutually_exclusive_group(required=True)
        cipher.add_argument("--key-file", type=Path)
        cipher.add_argument("--dpapi", action="store_true")
        if name == "prepare":
            source = command.add_mutually_exclusive_group(required=True)
            source.add_argument("--source-sqlite", type=Path)
            source.add_argument("--source-profile", type=Path)
            source.add_argument("--source-export", type=Path)
            command.add_argument("--source-label", default="explicit-local-source")
            command.add_argument("--source-assets", type=Path)
            command.add_argument(
                "--mode", choices=("empty-only", "append-only"), default="empty-only"
            )
            command.add_argument("--choices", type=Path)
        elif name == "apply":
            command.add_argument("--confirm-plan", required=True)
    args = parser.parse_args(argv)
    try:
        result = (
            generate_key(args.key_file) if args.action == "keygen" else globals()[args.action](args)
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception:
        print(
            json.dumps(
                {
                    "status": "not_verified",
                    "message": SAFE_FAILURE,
                    "automatic_retry_performed": False,
                },
                ensure_ascii=False,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
