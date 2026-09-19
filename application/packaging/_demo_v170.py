"""Strict, versioned release gate for the reviewed 210-day fictional corpus.

Unlabelled UI fixtures must not silently become real-user material. In addition
to provenance and counts, pin every business value to a reviewed golden digest.
Only generated password hashes, order identifiers and bookkeeping timestamps
are excluded from that digest and independently constrained below.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sqlite3
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from PIL import Image

from ollama_chat_app.data.database import SCHEMA_VERSION, timestamp_from_db
from ollama_chat_app.security.passwords import verify_password
from ollama_chat_app.services.preferences import DEFAULT_PREFERENCES, PreferencesService

PROFILE_ID = "expanded-v170"
ACCOUNTS = {
    "demo-operator": ("operator", "许静"),
    "demo-advisor-1": ("advisor", "林文静"),
    "demo-advisor-2": ("advisor", "周明远"),
    "demo-member-1": ("member", "陈淑兰"),
    "demo-member-2": ("member", "吴建国"),
}
COUNTS = {
    "users": 5,
    "messages": 72,
    "member_profiles": 2,
    "profile_facts": 0,
    "reminders": 16,
    "advisor_profiles": 2,
    "advisor_bindings": 2,
    "memberships": 2,
    "visit_tasks": 22,
    "visit_records": 14,
    "user_files": 0,
    "medical_reports": 0,
    "report_items": 0,
    "ai_insights": 0,
    "advisor_summaries": 0,
    "app_settings": 0,
    "audit_logs": 79,
    "products": 6,
    "product_recommendations": 2,
    "orders": 2,
    "user_file_contents": 0,
    "conversations": 17,
    "chat_attachments": 0,
    "user_preferences": 5,
    "member_cart": 4,
    "member_favorites": 4,
    "staff_account_terms": 2,
    "visit_task_details": 22,
    "life_records": 4632,
}
BUSINESS_SHA256 = "8acb784c4a2e9239f28aae3e2d2695311b49559b9ed34e7b13658cf8268f272c"
LIFE_CONTENT_SHA256 = "a6a2568e89beeb5790d2e09ad1f18fa3675838f68112d29156fff91a1ec552db"
PROVENANCE = {
    "synthetic_demo": True,
    "synthetic_profile": PROFILE_ID,
    "measured": False,
    "generated_by_ai": False,
}


@lru_cache(maxsize=1)
def _common():
    path = Path(__file__).with_name("_demo_release.py")
    spec = importlib.util.spec_from_file_location("medical_v170_seed_common", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def credentials(root: Path) -> dict[str, str]:
    common = _common()
    result = {}
    texts = []
    for filename in ("DEMO_ACCOUNTS.md", "DEMO_ACCOUNTS.txt"):
        path = root / filename
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 20000:
            raise RuntimeError("v1.7 体验账号说明缺失或异常。")
        text = path.read_text(encoding="utf-8-sig")
        if (
            "虚构" not in text
            or "独立随机生成" not in text
            or "不自动上传到云端" not in text
            or "不是现场调用 DeepSeek" not in text
            or common.SECRET_PATTERN.search(text)
        ):
            raise RuntimeError("v1.7 账号说明必须保留虚构来源和云端边界，且不能含真实密钥。")
        texts.append(text)
    if texts[0] != texts[1]:
        raise RuntimeError("v1.7 TXT 与 Markdown 账号信息不一致。")
    for line in texts[0].splitlines():
        if not line.startswith("| "):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 4 or not cells[2].startswith("demo-"):
            continue
        username, password = cells[2:]
        if (
            username not in ACCOUNTS
            or username in result
            or cells[1] != ACCOUNTS[username][1]
            or not re.fullmatch(r"[A-Za-z0-9_-]{20}", password)
        ):
            raise RuntimeError("v1.7 固定虚构账号名单或随机密码格式不匹配。")
        result[username] = password
    if set(result) != set(ACCOUNTS) or len(set(result.values())) != 5:
        raise RuntimeError("v1.7 只允许五个独立虚构账号。")
    return result


def business_fingerprint(connection: sqlite3.Connection) -> str:
    ignored = {"created_at", "updated_at", "last_login_at", "password_hash", "order_no"}
    body = {
        table: [
            {key: row[key] for key in row.keys() if key not in ignored}  # noqa: SIM118
            for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
        ]
        for table in sorted(COUNTS)
    }
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def verify_payload(root: Path) -> dict:
    root = Path(root).absolute()
    common = _common()
    if any(part.is_symlink() or part.is_junction() for part in (root, *root.parents)):
        raise RuntimeError("v1.7 资料路径不允许符号链接或 junction。")
    files = common.ordinary_tree(root)
    if (
        not files >= common.PAYLOAD_FILES
        or {name for name in files if name.startswith("data/")} != {"data/app.db"}
        or {name for name in files if name.startswith("assets/")} != common.IMAGES
    ):
        raise RuntimeError("v1.7 只允许完整的单一资料库和六张本地商品图。")
    manifest_path = root / "demo_manifest.json"
    if manifest_path.stat().st_size > 65536:
        raise RuntimeError("v1.7 来源清单体积异常。")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    required = {
        "synthetic_demo": True,
        "profile": PROFILE_ID,
        "schema_version": 7,
        "generated_at": "2026-09-19T20:00:00+08:00",
        "experience_day": "2026-09-19",
        "first_record_day": "2026-02-22",
        "record_days": 210,
        "record_categories": 6,
        "database": "data/app.db",
        "counts": COUNTS,
        "integrity_check": "ok",
        "foreign_key_violations": 0,
        "ai_calls": 0,
        "cloud_imported": False,
        "deterministic_content_seed": 170019,
        "content_sha256": LIFE_CONTENT_SHA256,
        "account_provenance": {name: "fictional-authored-persona" for name in ACCOUNTS},
        "message_provenance": "authored-dialogue-not-live-model-output",
    }
    if (
        type(manifest) is not dict
        or any(manifest.get(key) != value for key, value in required.items())
        or manifest.get("synthetic_demo") is not True
        or manifest.get("cloud_imported") is not False
        or type(manifest.get("passwords_preserved")) is not bool
        or "虚构测试资料" not in str(manifest.get("notice", ""))
        or common.SECRET_PATTERN.search(json.dumps(manifest))
    ):
        raise RuntimeError("v1.7 来源清单不符合审核过的 210 天虚构资料契约。")
    passwords = credentials(root)
    for image_path in common.IMAGES:
        path = root / image_path
        if path.stat().st_size > 2 * 1024 * 1024:
            raise RuntimeError("v1.7 商品图超过受审体积。")
        with Image.open(path) as image:
            if image.format != "PNG" or max(image.size) > 1024:
                raise RuntimeError("v1.7 商品图必须是受限 PNG。")
            image.verify()
    database = root / "data/app.db"
    if database.stat().st_size > 16 * 1024 * 1024:
        raise RuntimeError("v1.7 固定资料库体积异常。")
    with closing(
        sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        _verify_database(connection, passwords)
    return {
        "synthetic": True,
        "profile": PROFILE_ID,
        "schema_version": 7,
        "counts": dict(COUNTS),
        "account_count": 5,
        "integrity_check": "ok",
        "foreign_key_issues": 0,
        "credentials_verified_without_display": True,
        "authored_chat_not_live_ai": True,
        "business_fingerprint": BUSINESS_SHA256,
        "record_days": 210,
        "cloud_imported": False,
    }


def _verify_database(connection, passwords):
    from ollama_chat_app.services.backup import _expected_schema_signature, _schema_signature

    common = _common()
    if connection.execute("PRAGMA user_version").fetchone()[0] != 7 or SCHEMA_VERSION != 7:
        raise RuntimeError("v1.7 虚构资料要求已审核的 schema 7。")
    if _schema_signature(connection) != _expected_schema_signature(7):
        raise RuntimeError("v1.7 完整数据库结构签名不匹配。")
    if connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type IN ('trigger','view')"
    ).fetchone():
        raise RuntimeError("v1.7 资料不得带入触发器或视图。")
    if (
        connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
        or connection.execute("PRAGMA foreign_key_check").fetchall()
    ):
        raise RuntimeError("v1.7 资料完整性或外键检查失败。")
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if tables != set(COUNTS) or any(
        connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] != count
        for table, count in COUNTS.items()
    ):
        raise RuntimeError("v1.7 资料表或计数不符合固定集合。")
    users = connection.execute("SELECT * FROM users").fetchall()
    if {row["username"] for row in users} != set(ACCOUNTS):
        raise RuntimeError("v1.7 数据库含有未经审核的账号。")
    for user in users:
        if (
            user["role_code"] != ACCOUNTS[user["username"]][0]
            or user["username_normalized"] != user["username"]
            or user["account_status"] != "active"
            or user["last_login_at"] is not None
            or not user["password_hash"].startswith("$argon2id$")
            or not verify_password(user["password_hash"], passwords[user["username"]])
        ):
            raise RuntimeError("v1.7 账号角色、状态或单向密码校验失败。")
    for table in COUNTS:
        for row in connection.execute(f'SELECT * FROM "{table}"'):
            for key in row.keys():  # noqa: SIM118 - sqlite3.Row iterates values, not keys.
                value = row[key]
                if table == "users" and key == "password_hash":
                    continue
                if isinstance(value, bytes):
                    raise RuntimeError("v1.7 固定资料不得包含二进制个人附件。")
                if isinstance(value, str) and (
                    common.SECRET_PATTERN.search(value)
                    or any(p in value for p in passwords.values())
                ):
                    raise RuntimeError("v1.7 资料发现疑似密钥或明文密码。")
                if key in {"created_at", "updated_at"} and value is not None:
                    try:
                        parsed = timestamp_from_db(value)
                        if not 2026 <= parsed.year <= 2027:
                            raise ValueError
                    except (TypeError, ValueError, AttributeError):
                        raise RuntimeError("v1.7 记账时间不符合虚构资料范围。") from None
    for row in connection.execute("SELECT order_no FROM orders"):
        if not re.fullmatch(r"SIM-\d{8}-[A-F0-9]{12}", row[0]):
            raise RuntimeError("v1.7 订单只能是虚拟流程编号。")
    for row in connection.execute("SELECT preferences_json FROM user_preferences"):
        prefs = json.loads(row[0])
        PreferencesService.validate(prefs)
        if (
            set(prefs) != set(DEFAULT_PREFERENCES)
            or prefs.get("weather_consent") is not False
            or prefs.get("ai_context_consent") is not False
            or prefs.get("avatar_data")
            or prefs.get("avatar_path")
        ):
            raise RuntimeError("v1.7 资料不能带自动联网同意或真实头像。")
    for table in ("life_records", "visit_records"):
        for row in connection.execute(f'SELECT details_json FROM "{table}"'):
            details = json.loads(row[0])
            if any(details.get(key) != value for key, value in PROVENANCE.items()):
                raise RuntimeError("v1.7 生活/工作记录必须保留内部虚构来源。")
    if (
        connection.execute(
            "SELECT COUNT(*) FROM messages WHERE provider='fixture' "
            "AND model='synthetic-authored' AND status='complete'"
        ).fetchone()[0]
        != 72
    ):
        raise RuntimeError("v1.7 聊天必须是审核过的人工编写对话，不能冒称实时 AI 回答。")
    life_digest = hashlib.sha256(
        json.dumps(
            [
                tuple(row)
                for row in connection.execute(
                    "SELECT user_id,category,occurred_at,content,details_json "
                    "FROM life_records ORDER BY id"
                )
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if life_digest != LIFE_CONTENT_SHA256 or business_fingerprint(connection) != BUSINESS_SHA256:
        raise RuntimeError("v1.7 资料偏离已审核虚构正文指纹；不得用真实数据替换同数量记录。")
