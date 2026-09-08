"""Admin-only, read-only cloud backup; no cloud restore or migration entry point.

Real data travels through anonymous pipes and process memory, then current-user
DPAPI ciphertext outside the repository. A restore is accepted only in an owned,
network-isolated disposable Docker container with tmpfs storage and no logs.
Never log exceptions from libpq, Docker, pg_dump or pg_restore: they can contain
credentials, SQL or row contents. This module intentionally reports fixed errors.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from ctypes import wintypes
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg import sql

from .cloud_connection import ca_file
from .local_secret_store import _validate_path
from .migrations.guards import SCHEMA_MARKER
from .platform_admin import _inspect, management_url
from .platform_schema import PLATFORM_SCHEMA
from .schema import PRIVATE_SCHEMA

IMAGE = (
    "postgres:17-bookworm@sha256:"
    "051f7b7b3abdd564d5d1bd1e8c4b9c1b6e77087d1dd22020ede611c096a272e0"
)
LABEL = "healthlife.private-backup-verification"
CONFIRM = "readonly-platform-backup-and-isolated-restore"
MAX_BYTES = 128 * 1024 * 1024
MAGIC = b"HEALTHLIFE-PG-DPAPI-1\n"
ENTROPY = b"HealthLife/private-postgres-backup/current-user/v1"
SAFE_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")
SAFE_ERROR = "备份或隔离验证未完成；未执行云端迁移，未输出凭据或数据内容。"


class BackupError(RuntimeError):
    """Only prewritten, non-sensitive text is allowed in these errors."""


def _environment() -> dict[str, str]:
    # Do not inherit PG*, DOCKER_HOST/context overrides, tracing, or user secrets.
    allowed = ("SystemRoot", "WINDIR", "COMSPEC", "TEMP", "TMP", "PATH", "USERPROFILE")
    return {key: os.environ[key] for key in allowed if key in os.environ}


def _docker(arguments: list[str], *, data: bytes | None = None, timeout: int = 180) -> bytes:
    executable = shutil.which("docker")
    if not executable:
        raise BackupError("本机 Docker 不可用；未下载工具或开始备份。")
    try:
        result = subprocess.run(
            [executable, "--context", "desktop-linux", *arguments],
            input=data, capture_output=True, timeout=timeout, check=False,
            env=_environment(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # stderr is never propagated, even when pg_restore includes a bad row.
        if result.returncode or len(result.stdout) > MAX_BYTES:
            raise BackupError(SAFE_ERROR)
        return result.stdout
    except BackupError:
        raise
    except Exception:
        raise BackupError(SAFE_ERROR) from None


def require_local_daemon() -> None:
    contexts = json.loads(_docker(["context", "inspect", "desktop-linux"]))
    endpoint = contexts[0].get("Endpoints", {}).get("docker", {}) if len(contexts) == 1 else {}
    if endpoint.get("Host") != "npipe:////./pipe/dockerDesktopLinuxEngine":
        raise BackupError("恢复验证只能使用本机 Docker Desktop 命名管道，禁止远程 Docker。")


def require_image() -> None:
    require_local_daemon()
    info = json.loads(_docker(["image", "inspect", IMAGE]))
    if len(info) != 1 or IMAGE not in info[0].get("RepoDigests", []):
        # Docker can retain a digest under the untagged official repository name.
        expected = "postgres@" + IMAGE.split("@", 1)[1]
        if len(info) != 1 or expected not in info[0].get("RepoDigests", []):
            raise BackupError("缺少已核实的本地 PostgreSQL 17 官方镜像；未自动下载。")
    version = _docker([
        "run", "--rm", "--pull=never", "--network=none", "--log-driver=none",
        "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", IMAGE,
        "pg_dump", "--version",
    ])
    if not re.fullmatch(rb"pg_dump \(PostgreSQL\) 17\.[^\r\n]+\r?\n", version):
        raise BackupError("备份客户端不是经过核实的 PostgreSQL 17。")


def dump_arguments(uri, snapshot: str, *, metadata: bool = False) -> list[str]:
    if not re.fullmatch(r"[0-9A-Fa-f]+-[0-9A-Fa-f]+-[0-9]+", snapshot):
        raise BackupError("数据库快照标识无效。")
    if (
        not uri.password or any(ord(char) < 32 for char in uri.password)
        or uri.port != 5432 or uri.database != "postgres"
        or uri.query.get("sslmode") != "verify-full"
    ):
        raise BackupError("备份要求严格证书校验和可安全通过匿名管道传递的凭据。")
    certificate = ca_file(uri.query.get("sslrootcert", ""))
    if "," in certificate:
        raise BackupError("证书路径不能包含 Docker 挂载分隔符。")
    # No password, URI, inherited env, shell expansion, or secret-bearing file.
    command = [
        "run", "--rm", "-i", "--pull=never", "--log-driver=none", "--read-only",
        "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user=postgres",
        "--mount", f"type=bind,src={certificate},dst=/run/healthlife-ca.pem,readonly",
        IMAGE, "sh", "-c",
        'IFS= read -r PGPASSWORD || exit 70; export PGPASSWORD; '
        'export PGSSLMODE=verify-full PGSSLROOTCERT=/run/healthlife-ca.pem '
        'PGCONNECT_TIMEOUT=15 PGAPPNAME=healthlife-readonly-backup '
        'PGOPTIONS="-c default_transaction_read_only=on -c statement_timeout=120000"; '
        'exec pg_dump "$@"',
        "healthlife-dump", "--host", uri.host, "--port=5432", "--username", uri.username,
        "--dbname=postgres", "--no-password", "--format=custom", "--strict-names",
        "--no-publications", "--no-subscriptions", "--lock-wait-timeout=5000",
        "--snapshot", snapshot,
    ]
    command += (
        ["--table", PRIVATE_SCHEMA + ".alembic_version"] if metadata
        else ["--schema", PLATFORM_SCHEMA]
    )
    return command


# These queries return catalog definitions and counts, never business rows. Keep
# expressions independent of search_path, and map role OIDs back to role names.
SCOPE = (
    "(n.nspname='medical_app_platform' OR "
    "(n.nspname='medical_app_private' AND c.relname='alembic_version'))"
)
CATALOG_QUERIES = {
    "relations": f"""SELECT n.nspname,c.relname,c.relkind,c.relrowsecurity,
        c.relforcerowsecurity,pg_get_userbyid(c.relowner),obj_description(c.oid,'pg_class')
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE {SCOPE}
        ORDER BY 1,2""",
    "columns": f"""SELECT n.nspname,c.relname,a.attname,a.attnum,
        format_type(a.atttypid,a.atttypmod),a.attnotnull,a.attidentity,a.attgenerated,
        pg_get_expr(d.adbin,d.adrelid,false)
        FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
        WHERE {SCOPE} AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped
        ORDER BY 1,2,4""",
    "constraints": f"""SELECT n.nspname,c.relname,k.conname,k.contype,k.convalidated,
        pg_get_constraintdef(k.oid,false) FROM pg_constraint k
        JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE {SCOPE} ORDER BY 1,2,3""",
    "indexes": f"""SELECT n.nspname,c.relname,i.relname,x.indisvalid,x.indisready,
        pg_get_indexdef(x.indexrelid,0,false) FROM pg_index x
        JOIN pg_class c ON c.oid=x.indrelid JOIN pg_class i ON i.oid=x.indexrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace WHERE {SCOPE} ORDER BY 1,2,3""",
    "policies": f"""SELECT n.nspname,c.relname,p.polname,p.polpermissive,p.polcmd,
        ARRAY(SELECT CASE WHEN roleid=0 THEN 'public' ELSE pg_get_userbyid(roleid) END
        FROM unnest(p.polroles) roleid ORDER BY 1),pg_get_expr(p.polqual,p.polrelid,false),
        pg_get_expr(p.polwithcheck,p.polrelid,false) FROM pg_policy p
        JOIN pg_class c ON c.oid=p.polrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE {SCOPE} ORDER BY 1,2,3""",
    "table_acl": f"""SELECT n.nspname,c.relname,pg_get_userbyid(a.grantor),
        CASE WHEN a.grantee=0 THEN 'public' ELSE pg_get_userbyid(a.grantee) END,
        a.privilege_type,a.is_grantable FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        CROSS JOIN LATERAL aclexplode(coalesce(c.relacl,
            acldefault(CASE WHEN c.relkind='S' THEN 's'::"char" ELSE 'r'::"char" END,
                       c.relowner))) a
        WHERE {SCOPE} AND c.relkind IN ('r','S') ORDER BY 1,2,3,4,5,6""",
    "schema_acl": """SELECT n.nspname,pg_get_userbyid(a.grantor),
        CASE WHEN a.grantee=0 THEN 'public' ELSE pg_get_userbyid(a.grantee) END,
        a.privilege_type,a.is_grantable FROM pg_namespace n
        CROSS JOIN LATERAL aclexplode(n.nspacl) a
        WHERE n.nspname='medical_app_platform' ORDER BY 1,2,3,4,5""",
    "default_acl": """SELECT pg_get_userbyid(d.defaclrole),d.defaclobjtype,
        pg_get_userbyid(a.grantor),
        CASE WHEN a.grantee=0 THEN 'public' ELSE pg_get_userbyid(a.grantee) END,
        a.privilege_type,a.is_grantable FROM pg_default_acl d
        JOIN pg_namespace n ON n.oid=d.defaclnamespace
        CROSS JOIN LATERAL aclexplode(d.defaclacl) a
        WHERE n.nspname='medical_app_platform' ORDER BY 1,2,3,4,5,6""",
}


def _canonical(rows) -> list:
    return json.loads(json.dumps(rows, ensure_ascii=False))


def source_inventory(connection) -> dict:
    connection.execute("SET LOCAL search_path = pg_catalog")
    connection.execute("SET LOCAL row_security = off")
    require_supported_catalog(connection)
    catalog = {
        name: _canonical(connection.execute(query).fetchall())
        for name, query in CATALOG_QUERIES.items()
    }
    tables = [(row[0], row[1]) for row in catalog["relations"] if row[2] == "r"]
    counts = {}
    for schema, table in tables:
        counts[schema + "." + table] = connection.execute(
            sql.SQL("SELECT count(*) FROM {}.{}").format(
                sql.Identifier(schema), sql.Identifier(table)
            )
        ).fetchone()[0]
    revisions = connection.execute(
        sql.SQL("SELECT version_num FROM {}.alembic_version ORDER BY version_num").format(
            sql.Identifier(PRIVATE_SCHEMA)
        )
    ).fetchall()
    roles = {row[5] for row in catalog["relations"]}
    for row in catalog["policies"]:
        roles.update(row[5])
    for row in catalog["table_acl"]:
        roles.update(row[2:4])
    for row in catalog["schema_acl"]:
        roles.update(row[1:3])
    for row in catalog["default_acl"]:
        roles.update((row[0], row[2], row[3]))
    roles -= {"public", "postgres"}
    if any(not SAFE_NAME.fullmatch(role) for role in roles):
        raise BackupError("备份依赖了本工具不支持的角色标识；未开始本地恢复。")
    if any(row[4] is not True for row in catalog["constraints"]):
        raise BackupError("源库存在尚未验证的约束，不能宣称完整性验证通过。")
    return {"catalog": catalog, "counts": counts, "revisions": _canonical(revisions),
            "roles": sorted(roles)}


def require_supported_catalog(connection) -> None:
    # The frozen platform is table-only, with table/schema grants. Do not silently
    # claim a full privilege comparison if future/unexpected objects add a scope
    # this verifier does not implement. Never inspect global role password fields.
    unsupported = connection.execute(f"""SELECT
        EXISTS (SELECT 1 FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE {SCOPE} AND a.attacl IS NOT NULL AND cardinality(a.attacl)>0)
        OR EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='medical_app_platform')
        OR EXISTS (SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE {SCOPE} AND NOT t.tgisinternal)
    """).fetchone()[0]
    if unsupported:
        raise BackupError("源平台有本工具未覆盖的列级授权、函数或自定义触发器；停止备份验证。")


def export_cloud(profile: Path) -> dict:
    uri = management_url(profile)  # DPAPI load; never render URL or catch raw error text.
    with psycopg.connect(
        host=uri.host, port=5432, dbname="postgres", user=uri.username,
        password=uri.password, sslmode="verify-full",
        sslrootcert=ca_file(uri.query["sslrootcert"]), connect_timeout=15,
        application_name="healthlife-readonly-backup-inventory",
    ) as connection:
        connection.read_only = True
        connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        if not connection.pgconn.ssl_in_use:
            raise BackupError("源数据库未建立严格 TLS 连接。")
        connection.execute("SET LOCAL statement_timeout = '120000ms'")
        connection.execute("SET LOCAL lock_timeout = '5000ms'")
        connection.execute("SET LOCAL idle_in_transaction_session_timeout = '300000ms'")
        report = _inspect(connection)
        gates = ("pilot_owned", "schema_owned", "expected_table_set", "expected_columns",
                 "all_rls_forced", "expected_policy_set", "data_api_roles_no_access")
        if not all(report.get(key) for key in gates) or report.get("revisions") not in (
            ["platform_0001"], ["platform_0002"]
        ):
            raise BackupError("源库归属、版本或权限边界检查未通过；未执行导出。")
        snapshot = connection.execute("SELECT pg_export_snapshot()").fetchone()[0]
        inventory = source_inventory(connection)
        archives = {}
        for name, metadata in (("platform", False), ("alembic", True)):
            payload = _docker(
                dump_arguments(uri, snapshot, metadata=metadata),
                data=uri.password.encode("utf-8") + b"\n",
            )
            if not payload.startswith(b"PGDMP"):
                raise BackupError("导出没有生成有效的 PostgreSQL 自定义格式。")
            archives[name] = base64.b64encode(payload).decode("ascii")
        connection.rollback()
    return {"version": 1, "created_at": datetime.now(UTC).isoformat(),
            "image": IMAGE, "source_read_only": True, "certificate_mode": "verify-full",
            "scope": [PLATFORM_SCHEMA, PRIVATE_SCHEMA + ".alembic_version"],
            "inventory": inventory, "archives": archives}


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _protect(value: bytes, *, decrypt: bool = False) -> bytes:
    if sys.platform != "win32" or not 0 < len(value) <= MAX_BYTES:
        raise BackupError("备份需要当前 Windows 用户 DPAPI；不提供明文后备。")
    buffer = ctypes.create_string_buffer(value)
    entropy_buffer = ctypes.create_string_buffer(ENTROPY)
    source = _Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    entropy = _Blob(len(ENTROPY), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = _Blob()
    kernel = None
    try:
        crypt = ctypes.WinDLL("crypt32.dll", use_last_error=True, winmode=0x800)
        kernel = ctypes.WinDLL("kernel32.dll", use_last_error=True, winmode=0x800)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(_Blob),
                             ctypes.c_void_p if decrypt else wintypes.LPCWSTR,
                             ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                             wintypes.DWORD, ctypes.POINTER(_Blob)]
        function.restype = wintypes.BOOL
        if not function(ctypes.byref(source), None if decrypt else "HealthLife private PG backup",
                        ctypes.byref(entropy), None, None, 1, ctypes.byref(output)):
            raise BackupError("Windows 备份加解密失败；未使用明文后备。")
        if not output.data or not 0 < output.size <= MAX_BYTES:
            raise BackupError("加密备份大小无效。")
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.memset(buffer, 0, ctypes.sizeof(buffer))
        if output.data and kernel is not None:
            if decrypt:
                ctypes.memset(output.data, 0, output.size)
            kernel.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))


def save_encrypted(bundle: dict, destination: Path) -> None:
    target, exists = _validate_path(destination, must_exist=False)
    if exists:
        raise BackupError("备份文件已存在；禁止覆盖。")
    payload = json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ciphertext = MAGIC + _protect(payload)
    # Exclusive creation guarantees no existing backup is overwritten. Even a
    # partial write contains only ciphertext and is rejected by decrypt/JSON.
    with target.open("xb") as stream:
        stream.write(ciphertext)
        stream.flush()
        os.fsync(stream.fileno())


def load_encrypted(source: Path) -> dict:
    path, _ = _validate_path(source, must_exist=True)
    before = path.stat()
    if not len(MAGIC) < before.st_size <= MAX_BYTES + len(MAGIC):
        raise BackupError("加密备份长度无效。")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise BackupError("备份文件在读取期间发生变化。")
        ciphertext = stream.read(MAX_BYTES + len(MAGIC) + 1)
        after = os.fstat(stream.fileno())
    _validate_path(path, must_exist=True)
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise BackupError("备份文件在读取期间发生变化。")
    if not ciphertext.startswith(MAGIC):
        raise BackupError("文件不是受支持的加密备份。")
    bundle = json.loads(_protect(ciphertext[len(MAGIC):], decrypt=True))
    if bundle.get("version") != 1 or bundle.get("image") != IMAGE or bundle.get("scope") != [
        PLATFORM_SCHEMA, PRIVATE_SCHEMA + ".alembic_version"
    ]:
        raise BackupError("加密备份版本、镜像或数据边界不匹配。")
    return bundle


def _owned(container: str, token: str) -> bool:
    if not re.fullmatch(r"[a-f0-9]{64}", container) or not re.fullmatch(r"[a-f0-9]{32}", token):
        return False
    info = json.loads(_docker(["inspect", container]))
    if len(info) != 1:
        return False
    item = info[0]
    host = item.get("HostConfig", {})
    return (
        item.get("Id") == container and item.get("Config", {}).get("Labels", {}).get(LABEL) == token
        and item.get("Config", {}).get("Image") == IMAGE
        and host.get("NetworkMode") == "none" and not host.get("PortBindings")
        and host.get("LogConfig", {}).get("Type") == "none"
        and "/var/lib/postgresql/data" in host.get("Tmpfs", {})
        and not [mount for mount in item.get("Mounts", []) if mount.get("Type") != "tmpfs"]
    )


@contextmanager
def disposable_restore():
    require_local_daemon()
    token = uuid.uuid4().hex
    container = _docker([
        "run", "-d", "--pull=never", "--name", "healthlife-backup-check-" + token,
        "--label", LABEL + "=" + token, "--network=none", "--log-driver=none",
        "--memory=768m", "--memory-swap=768m", "--cpus=1", "--pids-limit=128",
        "--tmpfs", "/var/lib/postgresql/data:rw,noexec,nosuid,size=536870912",
        "--tmpfs", "/var/run/postgresql:rw,noexec,nosuid,size=16777216",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=16777216",
        "-e", "POSTGRES_HOST_AUTH_METHOD=trust", IMAGE, "postgres",
        "-c", "listen_addresses=", "-c", "log_statement=none",
        "-c", "log_min_error_statement=panic", "-c", "log_error_verbosity=terse",
        "-c", "logging_collector=off", "-c", "log_min_messages=panic",
    ], timeout=45).decode("ascii").strip()
    try:
        if not _owned(container, token):
            raise BackupError("一次性恢复容器的隔离或所有权检查未通过。")
        for _attempt in range(50):
            try:
                _docker(["exec", container, "pg_isready", "-U", "postgres", "-d", "postgres"],
                        timeout=5)
                break
            except BackupError:
                time.sleep(0.2)
        else:
            raise BackupError("一次性 PostgreSQL 验证库未就绪。")
        yield container
    finally:
        # Only the exact newly returned ID with our unique label is ever removed.
        if _owned(container, token):
            _docker(["rm", "--force", container], timeout=30)
        else:
            raise BackupError("无法确认临时容器归属，未删除任何容器；需要人工检查。")


def local_sql(container: str, query: str) -> bytes:
    return _docker(["exec", "-i", container, "psql", "-X", "-q", "-A", "-t",
                    "--set", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "postgres"],
                   data=query.encode("utf-8"))


def _local_json(container: str, query: str):
    output = local_sql(container, "SET search_path=pg_catalog; SELECT coalesce(json_agg(t), "
                       "'[]'::json) FROM (" + query + ") t;")
    # pg_get_* expressions can have duplicate field names. Retain JSON object
    # pairs as ordered values, rather than silently discarding duplicate keys.
    return json.loads(output, object_pairs_hook=lambda pairs: [value for _key, value in pairs])


@contextmanager
def restored_scope(bundle: dict):
    """Restore a bundle only into the guarded disposable target, never a URL."""
    inventory = bundle["inventory"]
    with disposable_restore() as container:
        roles = inventory["roles"]
        if any(not SAFE_NAME.fullmatch(role) for role in roles):
            raise BackupError("备份包含不受支持的角色标识。")
        setup = "\n".join(f'CREATE ROLE "{role}" NOLOGIN NOINHERIT NOBYPASSRLS;'
                          for role in roles)
        setup += f"\nCREATE SCHEMA {PRIVATE_SCHEMA};\n"
        setup += f"COMMENT ON SCHEMA {PRIVATE_SCHEMA} IS '{SCHEMA_MARKER}';\n"
        local_sql(container, setup)
        for name in ("platform", "alembic"):
            archive = base64.b64decode(bundle["archives"][name], validate=True)
            if not archive.startswith(b"PGDMP"):
                raise BackupError("备份中的 PostgreSQL 归档无效。")
            _docker(["exec", "-i", container, "pg_restore", "--single-transaction",
                     "--exit-on-error", "--no-password", "-U", "postgres", "-d", "postgres"],
                    data=archive)
        yield container


def verify_local(bundle: dict) -> dict:
    inventory = bundle["inventory"]
    with restored_scope(bundle) as container:
        for name, query in CATALOG_QUERIES.items():
            if _local_json(container, query) != inventory["catalog"][name]:
                raise BackupError("恢复后的目录定义或权限边界与源快照不一致（" + name + "）。")
        for qualified, count in inventory["counts"].items():
            schema, table = qualified.split(".")
            if schema not in (PRIVATE_SCHEMA, PLATFORM_SCHEMA) or not SAFE_NAME.fullmatch(table):
                raise BackupError("备份表边界不正确。")
            actual = local_sql(container, f'SELECT count(*) FROM "{schema}"."{table}";')
            if int(actual.strip()) != count:
                raise BackupError("恢复行数与一致性源快照不一致。")
        versions = _local_json(container, f"SELECT version_num FROM {PRIVATE_SCHEMA}."
                               "alembic_version ORDER BY version_num")
        if versions != inventory["revisions"]:
            raise BackupError("恢复的迁移版本元数据与源快照不一致。")
    return {
        "verified": True, "source_read_only": True, "cloud_changes": False,
        "tables_verified": len(inventory["counts"]),
        "platform_tables": len(inventory["counts"]) - 1,
        "source_rows": sum(inventory["counts"].values()),
        "constraints_verified": len(inventory["catalog"]["constraints"]),
        "indexes_verified": len(inventory["catalog"]["indexes"]),
        "policies_verified": len(inventory["catalog"]["policies"]),
        "catalog_and_acl_match": True, "consistent_snapshot_counts_match": True,
        "disposable_container_removed": True,
        "certificate_mode": "verify-full", "content_or_hashes_reported": False,
    }


def backup_and_verify(profile: Path, *, confirm: str) -> dict:
    if confirm != CONFIRM:
        raise BackupError("必须明确确认只读备份与隔离恢复验证。")
    require_image()
    folder = Path(os.environ["LOCALAPPDATA"]) / "HealthLife" / "private-backups"
    if folder.absolute().is_relative_to(Path(__file__).resolve().parents[1]):
        raise BackupError("备份必须保存在仓库外的当前 Windows 用户私有目录。")
    # Validate lexical parent chains before creating directories; never follow links.
    for directory in (folder.parent, folder):
        if not directory.exists():
            _validate_path(directory, must_exist=False)
            directory.mkdir()
        _validate_path(directory / ".path-check", must_exist=False)
    destination = folder / (datetime.now(UTC).strftime("platform-%Y%m%dT%H%M%SZ-")
                            + uuid.uuid4().hex + ".hlpgbackup")
    bundle = export_cloud(profile)
    save_encrypted(bundle, destination)
    return verify_encrypted(destination, confirm=confirm)


def verify_encrypted(destination: Path, *, confirm: str) -> dict:
    """Recheck an existing ciphertext locally; no profile loading/cloud connection."""
    if confirm != CONFIRM:
        raise BackupError("必须明确确认只读备份与隔离恢复验证。")
    require_image()
    # Verify what was actually encrypted and persisted, not the original memory only.
    persisted = load_encrypted(destination)
    result = verify_local(persisted)
    result["backup_file"] = str(destination)
    result["backup_bytes"] = destination.stat().st_size
    result["row_counts"] = persisted["inventory"]["counts"]
    result["revisions"] = persisted["inventory"]["revisions"]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--profile", type=Path)
    sources.add_argument("--verify-file", type=Path)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    try:
        result = (
            verify_encrypted(args.verify_file, confirm=args.confirm) if args.verify_file
            else backup_and_verify(args.profile, confirm=args.confirm)
        )
    except BackupError as error:
        print(json.dumps({"verified": False, "message": str(error)}, ensure_ascii=False))
        return 1
    except Exception:
        print(json.dumps({"verified": False, "message": SAFE_ERROR}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
