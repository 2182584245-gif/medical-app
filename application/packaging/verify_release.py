from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import sys
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath

from ollama_chat_app.config import APP_NAME, APP_VERSION
from ollama_chat_app.data.database import SCHEMA_VERSION

EXECUTABLE_NAME = f"{APP_NAME}.exe"
EXPECTED_ROOT_ENTRIES = {
    EXECUTABLE_NAME,
    "_internal",
    "data",
    "portable.json",
    "SHA256SUMS.txt",
    "使用说明.txt",
}
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _verify_database(path: Path) -> dict[str, object]:
    if path.read_bytes()[:16] != b"SQLite format 3\x00":
        raise RuntimeError("发布数据库没有正确的 SQLite 文件头")
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as connection:
        required_columns = {
            "user_file_contents": {"file_id", "content"},
            "reminders": {"repeat_rule", "source", "paused_until"},
            "report_items": {"status", "updated_at"},
            "chat_attachments": {"message_id", "content", "extracted_text", "sha256"},
        }
        missing_columns = {
            table: sorted(
                columns
                - {
                    str(row[1])
                    for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
            )
            for table, columns in required_columns.items()
        }
        missing_columns = {table: columns for table, columns in missing_columns.items() if columns}
        if missing_columns:
            raise RuntimeError(f"发布数据库缺少 v{SCHEMA_VERSION} 必要字段：{missing_columns}")
        result = {
            "quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
            "foreign_key_issues": len(connection.execute("PRAGMA foreign_key_check").fetchall()),
            "schema_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
            "users": int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]),
            "messages": int(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]),
            "conversations": int(
                connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
            ),
            "chat_attachments": int(
                connection.execute("SELECT COUNT(*) FROM chat_attachments").fetchone()[0]
            ),
            "products": int(connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]),
            "recommendations": int(
                connection.execute("SELECT COUNT(*) FROM product_recommendations").fetchone()[0]
            ),
            "orders": int(connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]),
            "life_records": int(
                connection.execute("SELECT COUNT(*) FROM life_records").fetchone()[0]
            ),
            "reminders": int(connection.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]),
            "ai_insights": int(
                connection.execute("SELECT COUNT(*) FROM ai_insights").fetchone()[0]
            ),
            "user_files": int(connection.execute("SELECT COUNT(*) FROM user_files").fetchone()[0]),
            "user_file_contents": int(
                connection.execute("SELECT COUNT(*) FROM user_file_contents").fetchone()[0]
            ),
            "medical_reports": int(
                connection.execute("SELECT COUNT(*) FROM medical_reports").fetchone()[0]
            ),
            "report_items": int(
                connection.execute("SELECT COUNT(*) FROM report_items").fetchone()[0]
            ),
        }
    expected = {
        "quick_check": "ok",
        "foreign_key_issues": 0,
        "schema_version": SCHEMA_VERSION,
        "users": 0,
        "messages": 0,
        "conversations": 0,
        "chat_attachments": 0,
        "products": 0,
        "recommendations": 0,
        "orders": 0,
        "life_records": 0,
        "reminders": 0,
        "ai_insights": 0,
        "user_files": 0,
        "user_file_contents": 0,
        "medical_reports": 0,
        "report_items": 0,
    }
    if result != expected:
        raise RuntimeError(f"发布数据库不是经过验证的空数据库：{result}")
    return result


def verify_directory(root: Path) -> dict[str, object]:
    root = root.resolve()
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError("发布目录不存在或不是普通文件夹")
    entries = {path.name for path in root.iterdir()}
    if entries != EXPECTED_ROOT_ENTRIES:
        raise RuntimeError(f"发布目录根级内容不符合预期：{sorted(entries)}")

    all_paths = list(root.rglob("*"))
    links = [str(path.relative_to(root)) for path in all_paths if path.is_symlink()]
    if links:
        raise RuntimeError(f"发布目录包含符号链接：{links}")
    files = [path for path in all_paths if path.is_file()]
    relative_names = {path.relative_to(root).as_posix() for path in files}
    prohibited = sorted(
        name
        for name in relative_names
        if name.startswith("data/logs/")
        or name.startswith("data/backups/")
        or name.endswith(("-wal", "-shm", "-journal"))
        or name.endswith(".app-instance.lock")
    )
    if prohibited:
        raise RuntimeError(f"发布目录包含运行时或敏感数据：{prohibited}")

    manifest_path = root / "SHA256SUMS.txt"
    manifest: dict[str, str] = {}
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        digest, separator, name = line.partition("  ")
        if separator != "  " or len(digest) != 64 or not name:
            raise RuntimeError(f"哈希清单行格式无效：{line!r}")
        if name in manifest:
            raise RuntimeError(f"哈希清单包含重复路径：{name}")
        manifest[name] = digest
    expected_manifest_names = relative_names - {"SHA256SUMS.txt"}
    if set(manifest) != expected_manifest_names:
        raise RuntimeError("哈希清单与发布目录文件不完全一致")
    bad_hashes = [
        name for name, expected_hash in manifest.items() if _sha256(root / name) != expected_hash
    ]
    if bad_hashes:
        raise RuntimeError(f"文件哈希校验失败：{bad_hashes}")

    metadata = json.loads((root / "portable.json").read_text(encoding="utf-8"))
    if metadata.get("application") != APP_NAME or metadata.get("version") != APP_VERSION:
        raise RuntimeError("便携版元数据中的应用名称或版本不正确")
    if (
        metadata.get("database_state") != "empty"
        or metadata.get("database_included") is not True
        or metadata.get("database_schema_version") != SCHEMA_VERSION
        or metadata.get("api_key_persisted") is not False
    ):
        raise RuntimeError("便携版元数据没有声明空数据库和不保存 API Key")
    executable_hash = _sha256(root / EXECUTABLE_NAME)
    if metadata.get("executable_sha256") != executable_hash:
        raise RuntimeError("便携版元数据中的 EXE 哈希不正确")

    database_result = _verify_database(root / "data" / "app.db")
    instructions = (root / "使用说明.txt").read_text(encoding="utf-8-sig")
    for required_text in (
        "完整解压",
        "忘记密码",
        "API Key",
        "图片/PDF 原件",
        "离线中文模型",
        "逐项确认",
        "模拟订单",
        "不会发生真实付款",
        "未知发布者",
        "视觉实验模型",
        "多个对话",
        "没有开启 Supabase/Render 云端同步",
    ):
        if required_text not in instructions:
            raise RuntimeError(f"使用说明缺少必要提示：{required_text}")

    return {
        "kind": "directory",
        "path": str(root),
        "file_count": len(files),
        "size_bytes": sum(path.stat().st_size for path in files),
        "executable_sha256": executable_hash,
        "database_sha256": _sha256(root / "data" / "app.db"),
        "database": database_result,
    }


def _validate_member(info: zipfile.ZipInfo, root_name: str) -> PurePosixPath:
    name = info.filename
    if not name or "\\" in name or "\x00" in name:
        raise RuntimeError(f"ZIP 包含无效路径：{name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or path.parts[0] != root_name:
        raise RuntimeError(f"ZIP 包含越界路径：{name}")
    if info.flag_bits & 0x1:
        raise RuntimeError(f"ZIP 包含加密成员：{name}")
    mode = (info.external_attr >> 16) & 0xFFFF
    if stat.S_ISLNK(mode):
        raise RuntimeError(f"ZIP 包含符号链接：{name}")
    if info.file_size > MAX_ARCHIVE_BYTES:
        raise RuntimeError(f"ZIP 成员过大：{name}")
    return path


def verify_archive(path: Path) -> dict[str, object]:
    path = path.resolve()
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("发布 ZIP 不存在或不是普通文件")
    with zipfile.ZipFile(path, "r") as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise RuntimeError("发布 ZIP 包含重复成员")
        for info in infos:
            _validate_member(info, APP_NAME)
        if sum(info.file_size for info in infos) > MAX_ARCHIVE_BYTES:
            raise RuntimeError("发布 ZIP 解压后总体积异常")
        broken = archive.testzip()
        if broken is not None:
            raise RuntimeError(f"ZIP CRC 校验失败：{broken}")

        with tempfile.TemporaryDirectory(prefix="ollama-chat-release-verify-") as temporary:
            temporary_root = Path(temporary).resolve()
            for info in infos:
                member = PurePosixPath(info.filename)
                destination = temporary_root.joinpath(*member.parts)
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info, "r") as source, destination.open("xb") as target:
                    while chunk := source.read(1024 * 1024):
                        target.write(chunk)
            directory_result = verify_directory(temporary_root / APP_NAME)

    return {
        "kind": "zip",
        "path": str(path),
        "sha256": _sha256(path),
        "compressed_size_bytes": path.stat().st_size,
        "members": len([info for info in infos if not info.is_dir()]),
        "contents": directory_result,
    }


def main() -> int:
    if len(sys.argv) != 2:
        print("用法：verify_release.py <发布目录或 ZIP>")
        return 2
    target = Path(sys.argv[1])
    result = (
        verify_archive(target) if target.suffix.casefold() == ".zip" else verify_directory(target)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
