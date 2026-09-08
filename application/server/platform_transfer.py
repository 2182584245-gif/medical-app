"""Administrator-only full business-data transfer; never an HTTP/RPC service.

Copy, never move. Only reviewed business columns cross databases. Runtime roles,
sessions, token digests, RPC caches, API credentials and arbitrary SQL never do.
The caller owns the explicit source/target connection and confirmation workflow.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import sqlite3
from copy import deepcopy
from pathlib import Path

import psycopg
from argon2 import extract_parameters
from argon2.low_level import Type
from psycopg import sql

from .platform_experience_schema import (
    BUSINESS_COLUMNS,
    BUSINESS_TABLES,
    IDENTITY_TABLES,
    PLATFORM_COLUMNS,
    PLATFORM_MARKER,
    PLATFORM_SCHEMA,
)

VERSION = 1
MAX_BYTES = 512 * 1024 * 1024
MAX_ROWS = 1_000_000
EXCLUDED = ("platform_sessions", "auth_rate_buckets", "rpc_requests", "credentials")
SAFE_FAILURE = "迁移未确认完成；请复核计划与目标状态。未输出凭据或业务行内容。"

# This is deliberately fixed metadata, not a client-supplied relation/SQL graph.
FK = {
    "member_profiles": {"user_id": "users"},
    "profile_facts": {"user_id": "users"},
    "life_records": {"user_id": "users"},
    "reminders": {"user_id": "users"},
    "advisor_profiles": {"user_id": "users"},
    "advisor_bindings": {"member_user_id": "users", "advisor_user_id": "users"},
    "memberships": {"user_id": "users"},
    "visit_tasks": {"member_user_id": "users", "advisor_user_id": "users"},
    "visit_records": {
        "task_id": "visit_tasks",
        "member_user_id": "users",
        "advisor_user_id": "users",
    },
    "user_files": {"user_id": "users"},
    "medical_reports": {"user_id": "users", "file_id": "user_files"},
    "report_items": {"report_id": "medical_reports"},
    "ai_insights": {"user_id": "users"},
    "advisor_summaries": {"member_user_id": "users", "advisor_user_id": "users"},
    "app_settings": {"user_id": "users"},
    "audit_logs": {"actor_user_id": "users"},
    "products": {"created_by_user_id": "users"},
    "product_recommendations": {
        "product_id": "products",
        "member_user_id": "users",
        "advisor_user_id": "users",
    },
    "orders": {
        "member_user_id": "users",
        "product_id": "products",
        "recommendation_id": "product_recommendations",
        "reordered_from_order_id": "orders",
    },
    "user_file_contents": {"file_id": "user_files"},
    "conversations": {"user_id": "users"},
    "messages": {"conversation_id": "conversations"},
    "chat_attachments": {"message_id": "messages"},
    "user_preferences": {"user_id": "users"},
    "member_cart": {"user_id": "users", "product_id": "products"},
    "member_favorites": {"user_id": "users", "product_id": "products"},
    "staff_account_terms": {"user_id": "users"},
    "visit_task_details": {"task_id": "visit_tasks", "requested_by": "users"},
}
PK = {table: ("id",) for table in IDENTITY_TABLES}
PK.update({table: (columns[0],) for table, columns in BUSINESS_COLUMNS.items() if table not in PK})
PK.update({"member_cart": ("user_id", "product_id"), "member_favorites": ("user_id", "product_id")})
JSON_IDS = {key: table for columns in FK.values() for key, table in columns.items()}
JSON_IDS.update(
    {
        "member_id": "users",
        "advisor_id": "users",
        "previous_task_id": "visit_tasks",
        "next_task_id": "visit_tasks",
        "reminder_id": "reminders",
        "record_id": "life_records",
        "record_ids": "life_records",
        "life_record_id": "life_records",
        "insight_id": "ai_insights",
        "order_id": "orders",
        "fact_id": "profile_facts",
        "visit_record_id": "visit_records",
    }
)
ENTITY_TYPES = {table: table for table in BUSINESS_TABLES}
ENTITY_TYPES.update({table.removesuffix("s"): table for table in BUSINESS_TABLES})
ENTITY_TYPES.update({"account": "users", "member": "users", "advisor": "users"})
SECRETS = re.compile(
    r"(?i)(?:\bsk-[a-z0-9_-]{12,}|\bbearer\s+[a-z0-9._~+/=-]{12,}|"
    r"\bsb_(?:secret|publishable)_[a-z0-9_-]{12,}|\beyJ[a-z0-9_-]{12,}\.[a-z0-9_-]{12,}\.[a-z0-9_-]{12,}|"
    r"postgres(?:ql)?://[^\s]+|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"
)
SECRET_KEYS = frozenset(
    {
        "password",
        "passwordhash",
        "apikey",
        "accesstoken",
        "refreshtoken",
        "bearertoken",
        "tokenpepper",
        "runtimedatabaseurl",
        "managementdatabaseurl",
        "privatekey",
        "secret",
        "clientsecret",
        "databasepassword",
        "dbpassword",
        "pgpassword",
        "credentials",
        "credential",
        "dpapi",
        "encryptionkey",
        "servicekey",
    }
)


class TransferError(RuntimeError):
    """Fixed messages only; database exceptions may contain health data or hashes."""


def _encoded(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$binary": base64.b64encode(bytes(value)).decode("ascii")}
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, (tuple, list)):
        return [_encoded(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _encoded(item) for key, item in value.items()}
    raise TransferError("迁移数据包含未支持的字段类型。")


def serialize(value) -> bytes:
    result = json.dumps(
        _encoded(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(result) > MAX_BYTES:
        raise TransferError("迁移快照超过 512 MiB 序列化上限；未截断或部分迁移。")
    return result


def deserialize(raw: bytes):
    if len(raw) > MAX_BYTES:
        raise TransferError("迁移包超过安全读取上限。")

    def hook(value):
        if set(value) == {"$binary"}:
            return base64.b64decode(value["$binary"], validate=True)
        return value

    try:
        return json.loads(raw, object_hook=hook)
    except Exception:
        raise TransferError("迁移包编码无效。") from None


def digest(value) -> str:
    return hashlib.sha256(serialize(value)).hexdigest()


def _secret_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", value.lower())
    return (
        normalized in SECRET_KEYS
        or any(normalized.endswith(item) for item in SECRET_KEYS)
        or any(
            item in normalized
            for item in (
                "apikey",
                "password",
                "privatekey",
                "credential",
                "tokenpepper",
                "dpapi",
                "accesstoken",
                "refreshtoken",
                "bearertoken",
                "databaseurl",
            )
        )
    )


def _no_secrets(value):
    if isinstance(value, dict):
        if any(_secret_key(str(key)) for key in value):
            raise TransferError("检测到疑似凭据字段；禁止迁移，请在管理员预检中处理源数据。")
        for item in value.values():
            _no_secrets(item)
    elif isinstance(value, list):
        for item in value:
            _no_secrets(item)
    elif isinstance(value, str) and SECRETS.search(value):
        raise TransferError("检测到疑似密钥或连接凭据；禁止迁移，未输出其内容。")
    elif isinstance(value, bytes) and SECRETS.search(value.decode("latin1")):
        raise TransferError("文件含疑似密钥或连接凭据；禁止迁移，未输出其内容。")


def _key(table, row):
    return tuple(row[column] for column in PK[table])


def _sort(rows):
    return {
        table: sorted(rows[table], key=lambda row, t=table: _key(t, row))
        for table in BUSINESS_TABLES
    }


def validate_rows(rows: dict, *, require_files: bool = True) -> None:
    if type(rows) is not dict or set(rows) != set(BUSINESS_TABLES):
        raise TransferError("迁移必须且只能包含固定 29 张业务表。")
    if sum(len(items) for items in rows.values()) > MAX_ROWS:
        raise TransferError("迁移超过一百万行业务记录上限，未执行部分导入。")
    identities = {table: {row["id"] for row in rows[table]} for table in IDENTITY_TABLES}
    for table, items in rows.items():
        seen = set()
        for row in items:
            if type(row) is not dict or set(row) != set(BUSINESS_COLUMNS[table]):
                raise TransferError("业务表字段与已审核 schema 6 / platform v2 不一致。")
            key = _key(table, row)
            if key in seen or any(type(item) is not int or not 0 < item < 2**63 for item in key):
                raise TransferError("业务主键重复或超出允许范围。")
            seen.add(key)
            for column, target in FK.get(table, {}).items():
                value = row[column]
                if value is not None and (
                    type(value) is not int or value not in identities[target]
                ):
                    raise TransferError("源数据存在缺失的业务关系，禁止不完整迁移。")
            for column, value in row.items():
                if table == "users" and column == "password_hash":
                    try:
                        if extract_parameters(value).type is not Type.ID:
                            raise ValueError
                    except Exception:
                        raise TransferError(
                            "账号必须使用有效 Argon2id 哈希；不接收明文密码。"
                        ) from None
                    continue
                _no_secrets(value)
                if column.endswith("_json") and value is not None:
                    try:
                        _no_secrets(json.loads(value))
                    except (ValueError, TypeError):
                        raise TransferError("结构化字段不是有效 JSON。") from None
            if table == "app_settings" and _secret_key(str(row["setting_key"])):
                raise TransferError("app_settings 含疑似凭据配置，禁止导出任何形式的该密钥。")
            if table in {"user_file_contents", "chat_attachments"}:
                content = bytes(row["content"])
                limit = 15 * 1024 * 1024 if table == "user_file_contents" else 10 * 1024 * 1024
                if (
                    not 0 < len(content) <= limit
                    or hashlib.sha256(content).hexdigest() != row["sha256"]
                ):
                    raise TransferError("原始文件大小或 SHA256 校验失败。")
    if require_files:
        contents = {row["file_id"]: row for row in rows["user_file_contents"]}
        for row in rows["user_files"]:
            blob = contents.get(row["id"])
            if (
                blob is None
                or len(blob["content"]) != row["size_bytes"]
                or (row["sha256"] is not None and blob["sha256"] != row["sha256"])
            ):
                raise TransferError("原始文件缺失或与目录信息不一致；请提供已确认的源文件目录。")
    serialize(rows)


def _safe_file(root: Path, relative: str, limit: int) -> bytes:
    if not relative or "\\" in relative or ":" in relative:
        raise TransferError("源文件必须使用安全相对路径。")
    parts = Path(relative).parts
    if Path(relative).is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise TransferError("源文件路径越界。")
    current = root
    for part in parts:
        current = current / part
        stat = current.lstat()
        if current.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
            raise TransferError("源文件不允许符号链接或重解析点。")
    if (
        not current.is_file()
        or current.stat().st_size > limit
        or not current.resolve().is_relative_to(root)
    ):
        raise TransferError("源文件不存在、过大或越界。")
    before = current.stat()
    with current.open("rb") as stream:
        result = stream.read(limit + 1)
    after = current.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or len(result) > limit:
        raise TransferError("读取期间源文件发生变化。")
    return result


def _file_root(root: Path) -> Path:
    root = Path(root).absolute()
    for path in (root, *root.parents):
        stat = path.lstat()
        if path.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
            raise TransferError("源目录不允许符号链接或重解析点。")
    if not root.is_dir():
        raise TransferError("源资产目录不存在。")
    return root.resolve()


def _hydrate(rows, asset_root):
    found = {row["file_id"] for row in rows["user_file_contents"]}
    for row in rows["user_files"]:
        if row["id"] in found:
            continue
        if asset_root is None:
            raise TransferError("历史原件只在磁盘；需明确指定源资产目录，未读取默认用户目录。")
        content = _safe_file(asset_root, row["relative_path"], 15 * 1024 * 1024)
        rows["user_file_contents"].append(
            {
                "file_id": row["id"],
                "content": content,
                "mime_type": row["media_type"] or "application/octet-stream",
                "sha256": hashlib.sha256(content).hexdigest(),
                "created_at": row["created_at"],
            }
        )


def sqlite_snapshot(path: Path, *, label: str, asset_root: Path | None = None) -> dict:
    """Existing explicit SQLite source, read-only transaction; never initializes/migrates it."""
    path = Path(path).absolute()
    _file_root(path.parent)
    if path.is_symlink() or not path.is_file():
        raise TransferError("必须明确选择已存在的本地源库。")
    asset_root = _file_root(asset_root) if asset_root is not None else None
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("BEGIN")
            if connection.execute("PRAGMA user_version").fetchone()[0] != 6:
                raise TransferError("本地源库必须已是 schema 6；迁移工具不改写原始库。")
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or (
                connection.execute("PRAGMA foreign_key_check").fetchall()
            ):
                raise TransferError("本地源库完整性或外键检查未通过。")
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if tables != set(BUSINESS_TABLES):
                raise TransferError("本地表集合不匹配，禁止漏掉未知业务表。")
            rows = {}
            for table, columns in BUSINESS_COLUMNS.items():
                actual = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
                if actual != set(columns):
                    raise TransferError("本地业务列集合不匹配。")
                rows[table] = [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"')]
            connection.rollback()
        _hydrate(rows, asset_root)
        validate_rows(rows)
        result = snapshot(
            rows,
            {
                "kind": "sqlite",
                "label": label,
                "locator": hashlib.sha256(str(path.resolve()).encode()).hexdigest(),
            },
        )
        from .platform_transfer_assets import attach_assets

        return attach_assets(result, asset_root)
    except TransferError:
        raise
    except Exception:
        raise TransferError("本地源预检失败；未写入源库或输出内容。") from None


def snapshot(rows, identity, *, assets=None):
    _no_secrets(identity)
    if not isinstance(identity.get("label"), str) or not 1 <= len(identity["label"]) <= 100:
        raise TransferError("需要明确的源或目标名称。")
    return {"version": VERSION, "identity": identity, "rows": _sort(rows), "assets": assets or {}}


def catalog_guard(connection) -> str:
    """Existing owned v2 only. Requires administrator bypass; never elevates privileges."""
    owner = connection.execute(
        "SELECT pg_get_userbyid(nspowner),obj_description(oid,'pg_namespace') "
        "FROM pg_namespace WHERE nspname=%s",
        (PLATFORM_SCHEMA,),
    ).fetchone()
    role = connection.execute(
        "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user"
    ).fetchone()
    current_user = connection.execute("SELECT current_user").fetchone()[0]
    if not owner or owner != (current_user, PLATFORM_MARKER) or not role or role[0] is not True:
        raise TransferError("需要已确认归属的平台与独立管理员连接；不允许受限账号静默导出部分行。")
    tables = connection.execute(
        "SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,"
        "obj_description(c.oid,'pg_class') FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s AND c.relkind='r'",
        (PLATFORM_SCHEMA,),
    ).fetchall()
    if {row[0] for row in tables} != set(PLATFORM_COLUMNS) or any(
        tuple(row[1:]) != (True, True, PLATFORM_MARKER) for row in tables
    ):
        raise TransferError("平台不是已审核的 32 表强制 RLS 结构。")
    actual = connection.execute(
        "SELECT table_name,column_name FROM information_schema.columns "
        "WHERE table_schema=%s ORDER BY table_name,ordinal_position",
        (PLATFORM_SCHEMA,),
    ).fetchall()
    columns = {}
    for table, column in actual:
        columns.setdefault(table, []).append(column)
    if {table: tuple(items) for table, items in columns.items()} != PLATFORM_COLUMNS:
        raise TransferError("平台列定义不匹配，未迁移未知结构。")
    triggers = connection.execute(
        "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s AND NOT t.tgisinternal",
        (PLATFORM_SCHEMA,),
    ).fetchone()[0]
    if triggers:
        raise TransferError("平台存在未审核的用户触发器，禁止以管理员身份触发未知操作。")
    references = connection.execute(
        "SELECT c.relname,a.attname,r.relname,b.attname FROM pg_constraint k "
        "JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
        "JOIN pg_class r ON r.oid=k.confrelid "
        "CROSS JOIN LATERAL unnest(k.conkey,k.confkey) AS keys(src,dst) "
        "JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=keys.src "
        "JOIN pg_attribute b ON b.attrelid=r.oid AND b.attnum=keys.dst "
        "WHERE n.nspname=%s AND k.contype='f'",
        (PLATFORM_SCHEMA,),
    ).fetchall()
    expected_fk = {
        (table, column, destination, "id")
        for table, fields in FK.items()
        for column, destination in fields.items()
    }
    if {tuple(row) for row in references if row[0] in BUSINESS_TABLES} != expected_fk:
        raise TransferError("数据库关系定义与已审核的业务关系不一致。")
    constraints = connection.execute(
        "SELECT c.relname,k.conname,pg_get_constraintdef(k.oid) FROM pg_constraint k "
        "JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s ORDER BY c.relname,k.conname",
        (PLATFORM_SCHEMA,),
    ).fetchall()
    return digest({"owner": owner[0], "columns": columns, "constraints": constraints})


def _pg_rows(connection):
    result = {}
    for table, columns in BUSINESS_COLUMNS.items():
        cursor = connection.execute(
            sql.SQL("SELECT {} FROM {}.{}").format(
                sql.SQL(",").join(map(sql.Identifier, columns)),
                sql.Identifier(PLATFORM_SCHEMA),
                sql.Identifier(table),
            )
        )
        result[table] = [dict(zip(columns, row, strict=True)) for row in cursor]
    return _sort(result)


def _pg_identity(connection, label, catalog):
    marker = connection.execute(
        "SELECT shobj_description(oid,'pg_database') FROM pg_database "
        "WHERE datname=current_database()"
    ).fetchone()[0]
    # Never export arbitrary database comments: only our public ownership marker.
    if not isinstance(marker, str) or not re.fullmatch(
        r"medical-app:aliyun:v1:[a-f0-9]{32}", marker
    ):
        marker = None
    return {
        "kind": "postgresql",
        "label": label,
        "host": connection.info.host,
        "port": connection.info.port,
        "database": connection.info.dbname,
        "catalog": catalog,
        "deployment_marker": marker,
    }


def postgres_snapshot(connection, *, label: str, asset_root: Path | None = None) -> dict:
    if connection.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise TransferError("管理员连接必须空闲，禁止嵌套在未知事务中。")
    try:
        with connection.transaction():
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            connection.execute("SET LOCAL statement_timeout='120s'")
            connection.execute("SET LOCAL lock_timeout='5s'")
            catalog = catalog_guard(connection)
            rows = _pg_rows(connection)
            validate_rows(rows)
            from .platform_transfer_assets import attach_assets

            return attach_assets(
                snapshot(rows, _pg_identity(connection, label, catalog)), asset_root
            )
    except TransferError:
        raise
    except Exception:
        raise TransferError("云端只读快照失败；未输出凭据或行内容。") from None


def _import_order():
    remaining, ordered = set(BUSINESS_TABLES), []
    while remaining:
        ready = sorted(
            table
            for table in remaining
            if not (set(FK.get(table, {}).values()) - {table}) & remaining
        )
        if not ready:
            raise TransferError("业务关系存在未支持的循环。")
        ordered.extend(ready)
        remaining.difference_update(ready)
    return ordered


def plan_transfer(
    source, target, *, mode="empty-only", renames=None, json_id_fields=None, entity_types=None
):
    """Safe append is NOT identity merge. Conflict choices create distinct new entities."""
    from .platform_transfer_assets import plan_assets, rewrite_asset_paths

    for item in (source, target):
        if item.get("version") != VERSION:
            raise TransferError("快照版本无效。")
        validate_rows(item["rows"])
    source_location = {k: v for k, v in source["identity"].items() if k != "label"}
    target_location = {k: v for k, v in target["identity"].items() if k != "label"}
    if source_location == target_location:
        raise TransferError("源和目标相同，禁止向自身复制。")
    if mode not in {"empty-only", "append-only"}:
        raise TransferError("只允许空目标导入或显式仅追加；不支持覆盖既有身份。")
    choices = {
        "renames": renames or {},
        "json_id_fields": json_id_fields or {},
        "entity_types": entity_types or {},
    }
    _no_secrets(choices)
    for name in ("json_id_fields", "entity_types"):
        if any(value not in BUSINESS_TABLES for value in choices[name].values()):
            raise TransferError("编号引用审阅只能指定固定业务表。")
    if set(choices["renames"]) - {"users", "products", "orders"}:
        raise TransferError("只能显式重命名导入账号、SKU 或订单号；不能更改角色或业务内容。")
    conflicts = []
    occupied = any(target["rows"].values())
    if occupied and mode == "empty-only":
        conflicts.append({"code": "target_not_empty"})
    mappings = {}
    counters = {}
    for table in sorted(IDENTITY_TABLES):
        start = max([row["id"] for row in target["rows"][table]], default=0)
        mappings[table] = {
            str(row["id"]): start + index + 1 if occupied else row["id"]
            for index, row in enumerate(source["rows"][table])
        }
        counters[table] = max([start, *mappings[table].values()])

    def remap(table, value):
        if value is None:
            return None
        if type(value) is not int or value <= 0:
            raise TransferError("结构化编号引用不是合法正整数。")
        key = str(value)
        # Historical audit/evidence can refer to already-deleted objects. Allocate
        # reserved gaps to avoid falsely attributing them to an existing target ID.
        if key not in mappings[table]:
            counters[table] += 1
            mappings[table][key] = counters[table] if occupied else value
        return mappings[table][key]

    json_fields = {**JSON_IDS, **choices["json_id_fields"]}

    def json_refs(value, table, identifier):
        if isinstance(value, list):
            return [json_refs(item, table, identifier) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            numeric = type(item) is int or (
                isinstance(item, list) and item and all(type(v) is int for v in item)
            )
            if key in json_fields and numeric:
                result[key] = (
                    [remap(json_fields[key], v) for v in item]
                    if isinstance(item, list)
                    else remap(json_fields[key], item)
                )
            else:
                if occupied and numeric and (key == "id" or key.endswith(("_id", "_ids"))):
                    conflicts.append(
                        {
                            "code": "unknown_json_reference",
                            "table": table,
                            "source_pk": identifier,
                            "field": key,
                        }
                    )
                result[key] = json_refs(item, table, identifier)
        return result

    transformed = deepcopy(source["rows"])
    entities = {**ENTITY_TYPES, **choices["entity_types"]}
    for table, items in transformed.items():
        for row in items:
            identifier = list(_key(table, row))
            if table in IDENTITY_TABLES:
                row["id"] = remap(table, row["id"])
            for column, referenced in FK.get(table, {}).items():
                row[column] = remap(referenced, row[column])
            for column in BUSINESS_COLUMNS[table]:
                if column.endswith("_json") and row[column] is not None:
                    original = json.loads(row[column])
                    changed = json_refs(original, table, identifier)
                    if changed != original:
                        row[column] = json.dumps(changed, ensure_ascii=False, separators=(",", ":"))
            if table == "audit_logs" and row["entity_id"] is not None:
                entity = entities.get(row["entity_type"])
                if entity in IDENTITY_TABLES:
                    row["entity_id"] = remap(entity, row["entity_id"])
                elif occupied:
                    conflicts.append(
                        {"code": "unknown_audit_entity", "table": table, "source_pk": identifier}
                    )
    for table, column in (
        ("users", "username_normalized"),
        ("products", "sku"),
        ("orders", "order_no"),
    ):
        edits = choices["renames"].get(table, {})
        source_ids = {str(row["id"]) for row in source["rows"][table]}
        if set(edits) - source_ids:
            raise TransferError("重命名计划引用了源中不存在的记录。")
        seen = {row[column] for row in target["rows"][table]}
        for old, row in zip(source["rows"][table], transformed[table], strict=True):
            value = edits.get(str(old["id"]))
            if value is not None:
                if not isinstance(value, str) or not 1 <= len(value.strip()) <= 100:
                    raise TransferError("冲突重命名不符合长度限制。")
                if table == "users":
                    row["username"] = value.strip()
                    row[column] = value.strip().casefold()
                else:
                    row[column] = value.strip()
            if row[column] in seen:
                conflicts.append(
                    {
                        "code": "unique_value_conflict",
                        "table": table,
                        "source_pk": [old["id"]],
                        "field": column,
                    }
                )
            seen.add(row[column])
    global_keys = {
        row["setting_key"] for row in target["rows"]["app_settings"] if row["user_id"] is None
    }
    if any(
        row["setting_key"] in global_keys and row["user_id"] is None
        for row in transformed["app_settings"]
    ):
        conflicts.append({"code": "global_settings_conflict", "table": "app_settings"})
    assets = plan_assets(source)
    plan_assets(target)  # The target backup must also preserve its external files.
    rewrite_asset_paths(transformed, assets)
    plan = {
        "version": VERSION,
        "operation": "copy-platform-business-data",
        "mode": mode,
        "status": "blocked" if conflicts else "review_required",
        "source": source["identity"],
        "target": target["identity"],
        "source_sha256": digest(source),
        "target_sha256": digest(target),
        "source_counts": {table: len(rows) for table, rows in source["rows"].items()},
        "target_counts": {table: len(rows) for table, rows in target["rows"].items()},
        "choices": choices,
        "id_mapping": mappings,
        "conflicts": conflicts,
        "excluded": list(EXCLUDED),
        "external_assets": assets,
        "source_preserved": True,
        "existing_target_rows_preserved": True,
        "transformed_sha256": digest(_sort(transformed)),
    }
    plan["plan_sha256"] = digest(plan)
    return plan, _sort(transformed)


def apply_transfer(
    connection,
    source,
    target_backup,
    plan,
    *,
    confirm_sha256: str,
    target_asset_root: Path | None = None,
) -> dict:
    """Atomic append with a reviewed, independently preserved pre-write backup.

    No automatic retry: ambiguous COMMIT outcomes require an explicit status check.
    The CLI verifies encrypted source/target backups on disk before calling this.
    """
    if connection.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise TransferError("目标连接必须在事务外，禁止未知事务或自动提交模式混入。")
    rebuilt, transformed = plan_transfer(
        source, target_backup, mode=plan["mode"], **plan["choices"]
    )
    if (
        rebuilt != plan
        or confirm_sha256 != plan["plan_sha256"]
        or plan["status"] != "review_required"
    ):
        raise TransferError("计划未明确确认、存在冲突或已被修改；未执行写入。")
    from .platform_transfer_assets import attach_assets, verify_staged_assets

    verify_staged_assets(plan, target_asset_root)
    try:
        with connection.transaction():
            connection.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
            connection.execute("SET LOCAL lock_timeout='5s'")
            connection.execute("SET LOCAL statement_timeout='120s'")
            connection.execute("SET LOCAL idle_in_transaction_session_timeout='180s'")
            # Consistent order, before snapshot/data access; prevents target change
            # between content fingerprint validation and the final verification.
            connection.execute(
                sql.SQL("LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE").format(
                    sql.SQL(",").join(
                        sql.Identifier(PLATFORM_SCHEMA, table) for table in sorted(BUSINESS_TABLES)
                    )
                )
            )
            catalog = catalog_guard(connection)
            actual = snapshot(
                _pg_rows(connection), _pg_identity(connection, plan["target"]["label"], catalog)
            )
            attach_assets(actual, target_asset_root)
            if digest(actual) != plan["target_sha256"]:
                raise TransferError(
                    "目标身份、结构或数据自预检后已改变；重新生成计划，禁止盲目重试。"
                )
            for table in _import_order():
                columns = BUSINESS_COLUMNS[table]
                statement = sql.SQL("INSERT INTO {}.{} ({}) VALUES ({})").format(
                    sql.Identifier(PLATFORM_SCHEMA),
                    sql.Identifier(table),
                    sql.SQL(",").join(map(sql.Identifier, columns)),
                    sql.SQL(",").join(sql.Placeholder() for _ in columns),
                )
                values = []
                for row in transformed[table]:
                    values.append(
                        tuple(
                            None
                            if table == "orders" and column == "reordered_from_order_id"
                            else row[column]
                            for column in columns
                        )
                    )
                if values:
                    with connection.cursor() as cursor:
                        cursor.executemany(statement, values)
                if table == "orders":
                    for row in transformed[table]:
                        if row["reordered_from_order_id"] is not None:
                            connection.execute(
                                "UPDATE medical_app_platform.orders "
                                "SET reordered_from_order_id=%s WHERE id=%s",
                                (row["reordered_from_order_id"], row["id"]),
                            )
            expected = {
                table: target_backup["rows"][table] + transformed[table]
                for table in BUSINESS_TABLES
            }
            actual_rows = _pg_rows(connection)
            if digest(actual_rows) != digest(_sort(expected)):
                raise TransferError("事务内逐行内容或原始文件校验失败，整笔回滚。")
            # RESTART is transactional (unlike setval), and table locks protect the
            # application's default nextval/INSERT path. Never lower a sequence.
            for table in sorted(IDENTITY_TABLES):
                sequence = connection.execute(
                    "SELECT pg_get_serial_sequence(%s,'id')", (f"{PLATFORM_SCHEMA}.{table}",)
                ).fetchone()[0]
                if sequence != f"{PLATFORM_SCHEMA}.{table}_id_seq":
                    raise TransferError("编号序列归属不匹配。")
                sequence_id = sql.Identifier(PLATFORM_SCHEMA, table + "_id_seq")
                last = connection.execute(
                    sql.SQL("SELECT last_value FROM {}").format(sequence_id)
                ).fetchone()[0]
                next_value = (
                    max(
                        last,
                        max(plan["id_mapping"][table].values(), default=0),
                        max((row["id"] for row in actual_rows[table]), default=0),
                    )
                    + 1
                )
                connection.execute(
                    sql.SQL("ALTER SEQUENCE {} RESTART WITH {}").format(
                        sequence_id,
                        sql.Literal(next_value),
                    )
                )
        return {
            "status": "committed",
            "plan_sha256": plan["plan_sha256"],
            "source": plan["source"],
            "target": plan["target"],
            "inserted_counts": plan["source_counts"],
            "verified_row_content": True,
            "verified_file_sha256": True,
            "source_preserved": True,
            "existing_target_rows_preserved": True,
            "credentials_transferred": False,
            "id_mapping": plan["id_mapping"],
        }
    except TransferError:
        raise
    except Exception:
        raise TransferError(SAFE_FAILURE) from None
