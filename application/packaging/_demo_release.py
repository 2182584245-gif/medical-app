"""Strict synthetic-data contract, separate from the unchanged empty-release verifier."""

from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from PIL import Image

from ollama_chat_app.config import APP_NAME
from ollama_chat_app.config import APP_VERSION as APP_VERSION
from ollama_chat_app.data.database import SCHEMA_VERSION
from ollama_chat_app.security.passwords import verify_password
from ollama_chat_app.services.preferences import DEFAULT_PREFERENCES

ACCOUNTS = {
    "demo-operator": ("operator", "示例运营员"),
    "demo-advisor-1": ("advisor", "示例林顾问"),
    "demo-advisor-2": ("advisor", "示例周顾问"),
    "demo-member-1": ("member", "示例陈奶奶"),
    "demo-member-2": ("member", "示例吴爷爷"),
}
COUNTS = {
    "users": 5,
    "messages": 0,
    "member_profiles": 2,
    "profile_facts": 0,
    "life_records": 140,
    "reminders": 4,
    "advisor_profiles": 2,
    "advisor_bindings": 2,
    "memberships": 2,
    "visit_tasks": 16,
    "visit_records": 8,
    "user_files": 0,
    "medical_reports": 0,
    "report_items": 0,
    "ai_insights": 0,
    "advisor_summaries": 0,
    "app_settings": 0,
    "audit_logs": 193,
    "products": 6,
    "product_recommendations": 2,
    "orders": 2,
    "user_file_contents": 0,
    "conversations": 5,
    "chat_attachments": 0,
    "user_preferences": 2,
    "member_cart": 4,
    "member_favorites": 4,
    "staff_account_terms": 2,
    "visit_task_details": 16,
}
MANIFEST_COUNTS = {
    key: COUNTS[key]
    for key in ("users", "life_records", "visit_tasks", "visit_records", "products")
}
IMAGES = {f"assets/products/demo-{index}.png" for index in range(1, 7)}
PAYLOAD_FILES = IMAGES | {"data/app.db", "demo_manifest.json", "DEMO_ACCOUNTS.md"}
SECRET_PATTERN = re.compile(
    r"(?i)(sk-[a-z0-9_-]{16,}|postgres(?:ql)?(?:\+psycopg)?://|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|bearer\s+[a-z0-9._-]{20,}|"
    r'"(?:api_?key|password|access_token|refresh_token|token_pepper|private_key)"\s*:)'
)
FORBIDDEN_PARTS = {".local", "secrets", "credentials", "backups", "logs", "__pycache__"}
FORBIDDEN_SUFFIXES = (".dpapi", ".env", ".pem", ".key", ".pfx", ".p12", "-wal", "-shm", "-journal")
REQUIRED_MODULES = {
    "ollama_chat_app.config",
    "ollama_chat_app.services.preferences",
    "ollama_chat_app.services.appointment_service",
    "ollama_chat_app.services.cloud_sync",
    "ollama_chat_app.services.offline_access",
    "ollama_chat_app.services.offline_outbox",
    "ollama_chat_app.services.sync_files",
    "ollama_chat_app.services.sync_protocol",
    "ollama_chat_app.endpoint_settings",
    "ollama_chat_app.ui.endpoint_settings_dialog",
    "ollama_chat_app.ui.outbox_dialog",
    "ollama_chat_app.data.experience_schema",
    "ollama_chat_app.data.staff_schema",
    "ollama_chat_app.ui.operator_workspace",
    "ollama_chat_app.ui.advisor_workspace",
    "ollama_chat_app.ui.health_workspace",
    "ollama_chat_app.ui.member_today",
    "ollama_chat_app.ui.member_statistics",
    "ollama_chat_app.ui.member_commerce",
    "ollama_chat_app.ui.theme",
    "ollama_chat_app.ui.snapshot_refresh",
    "ollama_chat_app.security.secret_store",
    "ollama_chat_app.security.windows_dpapi",
}


def sibling(name: str):
    spec = importlib.util.spec_from_file_location(
        "medical_packaging_" + name, Path(__file__).with_name(name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ordinary_tree(root: Path) -> set[str]:
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("必须使用普通目录，不允许符号链接。")
    names = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError("发行内容不允许符号链接。")
        if path.is_file():
            name = path.relative_to(root).as_posix()
            if ":" in name or any(
                part.casefold() in FORBIDDEN_PARTS for part in path.relative_to(root).parts
            ):
                raise RuntimeError("发行内容包含敏感或运行时目录。")
            public_ca = name == "_internal/certifi/cacert.pem"
            if public_ca and b"PRIVATE KEY" in path.read_bytes():
                raise RuntimeError("公开 CA 文件中不得含有私钥。")
            if name.casefold().endswith(FORBIDDEN_SUFFIXES) and not public_ca:
                raise RuntimeError("发行内容包含凭据或数据库运行时文件。")
            if path.suffix.casefold() in {".db", ".sqlite", ".sqlite3"} and name != "data/app.db":
                raise RuntimeError("不允许附带其他数据库。")
            names.add(name)
    if len({name.casefold() for name in names}) != len(names):
        raise RuntimeError("发行内容存在 Windows 大小写路径冲突。")
    return names


def verify_fresh_build(source: Path) -> dict:
    sibling("assemble_release")._validate_clean_source(source)
    ordinary_tree(source)
    result = sibling("verify_embedded_source").verify(source / f"{APP_NAME}.exe")
    if (
        result["status"] != "passed"
        or not set(result.get("checked_modules", [])) >= REQUIRED_MODULES
    ):
        raise RuntimeError("必须使用与当前源码一致且包含新版模块的最终 EXE，拒绝旧 EXE。")
    return result


def credentials(root: Path) -> dict[str, str]:
    path = root / "DEMO_ACCOUNTS.md"
    if path.stat().st_size > 20000:
        raise RuntimeError("示例账号说明异常。")
    text = path.read_text(encoding="utf-8")
    if "虚构" not in text or "独立随机生成" not in text or SECRET_PATTERN.search(text):
        raise RuntimeError("示例账号说明缺少安全声明或包含疑似密钥。")
    result = {}
    for line in text.splitlines():
        if not line.startswith("| "):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) == 4 and cells[2].startswith("demo-"):
            username, password = cells[2:]
            if username not in ACCOUNTS or cells[1] != ACCOUNTS[username][1] or username in result:
                raise RuntimeError("示例账号名单不匹配。")
            if not re.fullmatch(r"[A-Za-z0-9_-]{20}", password):
                raise RuntimeError("初始密码不是生成器规定的独立随机密码格式。")
            result[username] = password
    if set(result) != set(ACCOUNTS) or len(set(result.values())) != 5:
        raise RuntimeError("示例账号必须精确匹配五个独立虚构账号。")
    return result


def verify_payload(root: Path) -> dict:
    files = ordinary_tree(root)
    if not files >= PAYLOAD_FILES or {p for p in files if p.startswith("data/")} != {"data/app.db"}:
        raise RuntimeError("示例资料文件不完整，或 data 中含有额外资料。")
    if {p for p in files if p.startswith("assets/")} != IMAGES:
        raise RuntimeError("仅允许六幅生成器输出的本地商品示意图。")
    manifest = json.loads((root / "demo_manifest.json").read_text(encoding="utf-8"))
    if (
        manifest.get("synthetic_demo") is not True
        or manifest.get("database") != "data/app.db"
        or manifest.get("counts") != MANIFEST_COUNTS
        or manifest.get("ai_calls") != 0
        or manifest.get("record_days") != 14
        or manifest.get("record_categories") != 5
        or manifest.get("integrity_check") != "ok"
        or manifest.get("foreign_key_violations") != 0
    ):
        raise RuntimeError("只接受通过固定虚构示例契约的 demo_manifest。")
    passwords = credentials(root)
    for name in IMAGES:
        path = root / name
        if path.stat().st_size > 2 * 1024 * 1024:
            raise RuntimeError("示意图体积异常。")
        with Image.open(path) as picture:
            if picture.format != "PNG" or max(picture.size) > 1024:
                raise RuntimeError("示意图不是受限 PNG。")
            picture.verify()
    database = root / "data/app.db"
    if database.stat().st_size > 16 * 1024 * 1024:
        raise RuntimeError("固定示例数据库体积异常。")
    with closing(
        sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        if (
            connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION
            or SCHEMA_VERSION != 6
        ):
            raise RuntimeError("示例发布要求完整 schema 6。")
        if (
            connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            or connection.execute("PRAGMA foreign_key_check").fetchall()
        ):
            raise RuntimeError("示例数据库完整性或外键检查失败。")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if tables != set(COUNTS):
            raise RuntimeError("示例数据库表集合不符合 schema 6 固定契约。")
        actual = {
            table: connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            for table in COUNTS
        }
        if actual != COUNTS:
            raise RuntimeError("示例数据库记录数偏离固定合成数据，拒绝使用。")
        users = connection.execute("SELECT * FROM users").fetchall()
        if {row["username"] for row in users} != set(ACCOUNTS):
            raise RuntimeError("数据库含有非示例账号。")
        for user in users:
            if (
                user["role_code"] != ACCOUNTS[user["username"]][0]
                or user["username_normalized"] != user["username"]
                or user["account_status"] != "active"
                or not user["password_hash"].startswith("$argon2id$")
                or not verify_password(user["password_hash"], passwords[user["username"]])
            ):
                raise RuntimeError("示例账号角色、状态或单向密码校验失败。")
        for table in COUNTS:
            for row in connection.execute(f'SELECT * FROM "{table}"'):
                for column in row.keys():  # noqa: SIM118 - sqlite3.Row iterates values, not keys.
                    value = row[column]
                    if table == "users" and column == "password_hash":
                        continue
                    if isinstance(value, bytes):
                        raise RuntimeError("示例数据库不应包含二进制附件或凭据。")
                    if isinstance(value, str) and (
                        SECRET_PATTERN.search(value) or any(p in value for p in passwords.values())
                    ):
                        raise RuntimeError("示例数据库含有疑似密钥或明文初始密码。")
        for row in connection.execute("SELECT preferences_json FROM user_preferences"):
            prefs = json.loads(row[0])
            if (
                set(prefs) - set(DEFAULT_PREFERENCES)
                or prefs.get("weather_consent") is not False
                or prefs.get("ai_context_consent") is not False
                or prefs.get("avatar_data")
                or prefs.get("avatar_path")
                or not prefs.get("nickname", "").startswith("示例")
            ):
                raise RuntimeError("示例偏好中存在未授权内容、头像或自动联网同意。")
        for row in connection.execute(
            "SELECT display_name,phone,emergency_contact_phone,medical_notes FROM member_profiles"
        ):
            if (
                not row[0].startswith("示例")
                or row[1] != "00000000000"
                or row[2] != "00000000000"
                or "虚构" not in row[3]
            ):
                raise RuntimeError("会员资料不满足虚构资料限制。")
        for table, text_column in (("life_records", "content"), ("visit_records", "summary")):
            for row in connection.execute(f"SELECT {text_column},details_json FROM {table}"):
                if "虚构演示" not in row[0] or json.loads(row[1]).get("synthetic_demo") is not True:
                    raise RuntimeError("每条生活与工作记录都必须声明为虚构。")
        groups = connection.execute(
            "SELECT user_id,category,count(*),count(DISTINCT local_date) "
            "FROM life_records GROUP BY user_id,category"
        ).fetchall()
        if len(groups) != 10 or any(row[2] != 14 or row[3] != 14 for row in groups):
            raise RuntimeError("生活记录必须为两会员、五类别、各十四天。")
        products = connection.execute("SELECT sku,name,image_path FROM products").fetchall()
        if (
            {row[0] for row in products} != {f"DEMO-{n:03d}" for n in range(1, 7)}
            or {row[2] for row in products} != IMAGES
            or any(not row[1].startswith("示例") for row in products)
        ):
            raise RuntimeError("商品必须精确匹配六个示例商品与本地图片。")
    return {
        "synthetic": True,
        "schema_version": SCHEMA_VERSION,
        "counts": actual,
        "account_count": 5,
        "integrity_check": "ok",
        "foreign_key_issues": 0,
        "credentials_verified_without_display": True,
    }
