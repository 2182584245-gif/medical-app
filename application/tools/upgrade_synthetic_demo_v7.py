"""Copy a strictly verified fictional seed, retain every old row, add six-category examples."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import sqlite3
from contextlib import closing
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ollama_chat_app.data.database import Database, timestamp_to_db, utc_now
from ollama_chat_app.services.health import HealthService


def contract():
    path = Path(__file__).resolve().parents[1] / "packaging" / "_demo_release.py"
    spec = importlib.util.spec_from_file_location("synthetic_v7_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def rows(connection, tables):
    return {
        table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
        for table in sorted(tables)
    }


def checked_path(path):
    path = Path(path).absolute()
    if any(part.is_symlink() or part.is_junction() for part in (path, *path.parents)):
        raise RuntimeError("不允许链接或 junction 输入输出。")
    return path


def upgrade_copy(source: Path, destination: Path) -> dict:
    source, destination = checked_path(source), checked_path(destination)
    if (
        destination.exists()
        or not destination.parent.is_dir()
        or destination.is_relative_to(source)
        or source.is_relative_to(destination)
    ):
        raise RuntimeError("必须使用不包含源目录、尚不存在的独立新输出目录。")
    common = contract()
    legacy = common.verify_legacy_seed(source)
    source_hashes = {name: sha256(source / name) for name in common.LEGACY_PAYLOAD_FILES}
    with closing(
        sqlite3.connect((source / "data/app.db").as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as old:
        before = rows(old, common.LEGACY_COUNTS)
        last_day = date.fromisoformat(
            old.execute("SELECT max(local_date) FROM life_records").fetchone()[0]
        )
    experience_day = last_day + timedelta(days=1)
    destination.mkdir(mode=0o700, exist_ok=False)
    for name in sorted(common.LEGACY_PAYLOAD_FILES):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, target)
    if any(sha256(destination / name) != value for name, value in source_hashes.items()):
        raise RuntimeError("复制后 hash 不一致；保留现场，未触及原目录。")
    database = Database(destination / "data/app.db")
    # Versioned transactional schema upgrade, never the real default database.
    database.initialize()
    with closing(sqlite3.connect(database.path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        if rows(connection, common.LEGACY_COUNTS) != before:
            raise RuntimeError("结构升级后旧行不一致。")
        stamp = timestamp_to_db(utc_now())
        members = connection.execute(
            "SELECT id FROM users WHERE role_code='member' ORDER BY username"
        ).fetchall()

        def insert(member, category, day, content, details):
            validated = HealthService._validate_record_details(category, details)
            # This private synthetic tool adds provenance only after normal fact validation.
            validated["synthetic_demo"] = True
            occurred = timestamp_to_db(
                datetime.combine(day, time(7, 30), ZoneInfo("Asia/Shanghai"))
            )
            cursor = connection.execute(
                "INSERT INTO life_records(user_id,category,occurred_at,local_date,"
                "timezone_offset_minutes,content,details_json,source,created_at,updated_at) "
                "VALUES(?,?,?,?,480,?,?,'import',?,?)",
                (
                    member,
                    category,
                    occurred,
                    day.isoformat(),
                    content,
                    json.dumps(validated, ensure_ascii=False, separators=(",", ":")),
                    stamp,
                    stamp,
                ),
            )
            connection.execute(
                "INSERT INTO audit_logs(actor_user_id,action,entity_type,entity_id,created_at) "
                "VALUES(?,'life_record.created','life_record',?,?)",
                (member, cursor.lastrowid, stamp),
            )

        for member in members:
            identifier = member[0]
            for category in ("diet", "water", "activity", "sleep", "environment"):
                previous = connection.execute(
                    "SELECT content,details_json FROM life_records WHERE user_id=? AND category=? "
                    "ORDER BY occurred_at DESC,id DESC LIMIT 1",
                    (identifier, category),
                ).fetchone()
                detail = json.loads(previous["details_json"])
                detail.pop("synthetic_demo", None)
                insert(
                    identifier,
                    category,
                    experience_day,
                    "【虚构演示】沿用前一日同类预设参数；非真实测量或 AI 结果。"
                    + previous["content"],
                    detail,
                )
            for offset, kind, name, description in (
                (2, "consultation", "示例社区门诊复查", "已发生的看病事项字段演示，无诊断结论。"),
                (1, "other", "示例检查资料整理", "已发生的医疗资料整理字段演示，不对应真实检查。"),
                (
                    0,
                    "medication",
                    "示例既有用药事项",
                    "仅演示已完成事项字段，不提供药品、剂量、处方或用药方案。",
                ),
            ):
                insert(
                    identifier,
                    "medical",
                    experience_day - timedelta(days=offset),
                    "【虚构演示】" + name + "；不是真实健康事实，也不是 AI 医疗建议。",
                    {"event_type": kind, "name": name, "description": description},
                )
        after = rows(connection, common.COUNTS)
        for table, original in before.items():
            if after[table][: len(original)] != original:
                raise RuntimeError("旧数据发生变化；整笔示例追加已回滚。")
            if table not in {"life_records", "audit_logs"} and after[table] != original:
                raise RuntimeError("非记录表不得改变；整笔示例追加已回滚。")
        if {table: len(value) for table, value in after.items()} != common.COUNTS:
            raise RuntimeError("新增数量不符合固定156/209契约。")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("示例外键校验失败。")
    manifest_path = destination / "demo_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        schema_version=7,
        upgraded_from_schema=legacy["schema_version"],
        counts=common.MANIFEST_COUNTS,
        record_days=15,
        record_categories=6,
        preserved_legacy_records=140,
        added_records=16,
        cloud_imported=False,
        experience_day=experience_day.isoformat(),
        source_database_sha256=source_hashes["data/app.db"],
        preserved_rows_sha256={
            table: hashlib.sha256(
                json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
            ).hexdigest()
            for table, value in before.items()
        },
        notice="所有资料均为虚构演示；保留原140记录，新增医疗事实与今日六类示例，不含用药方案；尚未云导入。",
    )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (destination / "DEMO_ACCOUNTS.md").open("a", encoding="utf-8") as stream:
        stream.write(
            "\n## 1.5.0 六类示例副本\n\n保留原五个虚构账号和独立随机密码，不重新生成密码。"
            "保留全部原资料，医疗只记录已发生事项的虚构字段，不给出药品、剂量或处方。"
            "物料生成时尚未云导入；后续云端开通以管理员实际授权与验收通知为准。\n"
        )
    passwords = common.credentials(destination)
    explanation = (
        "虚构演示账号（仅5个）：沿用原独立随机生成的密码，不是真实个人凭据。\n"
        "物料生成时尚未云导入；先选本地模式体验，后续云端开通以管理员确认通知为准。\n"
        "医疗仅为事实字段演示，不提供药品、剂量、处方或用药方案。\n\n"
    )
    (destination / "DEMO_ACCOUNTS.txt").write_text(
        explanation + "\n".join(f"{name}\t{passwords[name]}" for name in sorted(passwords)) + "\n",
        encoding="utf-8-sig",
    )
    result = common.verify_payload(destination)
    if any(sha256(source / name) != value for name, value in source_hashes.items()):
        raise RuntimeError("源文件在操作期间改变；不得交付此副本。")
    lines = [
        f"{sha256(destination / name).upper()}  {name}" for name in sorted(common.PAYLOAD_FILES)
    ]
    (destination / "PAYLOAD_SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "status": "synthetic_v7_copy_verified",
        "destination": str(destination),
        "schema_version": 7,
        "counts": result["counts"],
        "experience_day": experience_day.isoformat(),
        "all_original_rows_identical": True,
        "original_payload_unchanged": True,
        "passwords_preserved_without_display": True,
        "cloud_imported": False,
        "database_sha256": sha256(database.path),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    result = upgrade_copy(args.source, args.destination)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
