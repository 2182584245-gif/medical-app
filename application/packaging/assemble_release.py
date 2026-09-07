from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from ollama_chat_app.config import APP_NAME, APP_VERSION
from ollama_chat_app.data.database import SCHEMA_VERSION, Database

EXECUTABLE_NAME = f"{APP_NAME}.exe"
EXPECTED_SOURCE_ENTRIES = {EXECUTABLE_NAME, "_internal"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _validate_clean_source(source: Path) -> None:
    if not source.is_dir() or source.is_symlink():
        raise RuntimeError("干净构建目录不存在或不是普通文件夹")
    entries = {path.name for path in source.iterdir()}
    if entries != EXPECTED_SOURCE_ENTRIES:
        raise RuntimeError(f"干净构建目录根级内容不符合预期：{sorted(entries)}")
    for path in source.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"干净构建目录包含符号链接：{path}")


def _create_empty_database(path: Path) -> None:
    database = Database(path)
    database.initialize()
    with sqlite3.connect(path) as connection:
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
        raise RuntimeError(f"发布数据库校验失败：{result}")


def _instructions() -> str:
    return f"""{APP_NAME} v{APP_VERSION} 便携版使用说明

1. 请先完整解压 ZIP 到桌面、文档或其他当前用户可写的文件夹，再双击“{EXECUTABLE_NAME}”；
   不要放入 Program Files 或只读 U 盘，不要只复制 EXE，也不要直接在 ZIP 内运行。
2. 本包已包含空数据库 data\\app.db。本地模式的账户、密码哈希、聊天、档案、生活记录、提醒、
   图片/PDF 原件、报告、服务、商品、推荐和模拟订单保存在这里；云端模式的数据保存在
   Supabase，不会因复制本地文件夹而下载或迁移云库。云端账号可在另一台电脑登录同步查看。
3. 全部界面默认北京时间（UTC+08:00）；日期时间可直接输入或用日历选择，
   保存提醒、生活记录、会员方案和上门安排时无需手动设置时区。
   本版本没有“忘记密码”功能，请牢记本机账户密码。
4. 本地模式不会在启动时检测 Ollama；发送消息时才会连接本机 Ollama。Ollama 和本地模型需另行安装。
5. Ollama Cloud 与 DeepSeek Cloud 均使用用户自己的 API Key。Key 只保留在当前运行
   内存中，关闭应用后自动清除。
6. DeepSeek 纯文字使用 deepseek-v4-flash；本轮或历史上下文含图片时自动使用
   deepseek-v4-flash-vision-exp（视觉实验模型）。实验模型可能调整或下线，图片与文字
   均可能消耗付费额度；用户主动验证 Key 时也会产生一次极小用量。
   聊天可附加 JPG/JPEG、PNG、WEBP 图片，以及 PDF、TXT、MD、CSV、DOCX 文件。
   文件先在本机提取文字；扫描 PDF 没有文字层时，请改为附加图片。
   单个附件最多 10 MiB，每次最多 4 个、合计 20 MiB；每个账户聊天附件共 100 MiB。
   点击发送并确认后，图片原文/文件提取文字及当前对话上下文才会发往 DeepSeek。
   左侧可新建、切换和重命名多个对话；各对话上下文互相独立，发送中暂不允许切换。
7. 语音转文字使用包内离线中文模型，OCR 使用包内模型；两者不会把音频或文件上传到云端。
   AI 生成的生活记录、档案事实、提醒和顾问摘要先保存为草稿，必须逐项确认才会生效。
8. 本地数据换电脑：完全关闭应用再复制整个文件夹，或在本地模式“导出数据”，
   新电脑登录页“从备份导入数据”。云端数据换电脑：选择同一云端服务并登录同一账号。
   云端模式不提供本地整库备份导入，旧数据不会自动上传；两种模式的账号相互独立。
9. 数据备份会包含账户、聊天、附件原件和报告，但未额外加密；请只放在自己的可信磁盘、
   U 盘或私有网盘中。
10. 商品功能是流程模拟：不会发生真实付款、扣款、发货、物流或向商家发送信息；
   “已模拟交付”也不代表真实履约。
11. 本程序为个人非商业项目，没有购买代码签名证书，因此 Windows 可能显示“未知发布者”。
   这不等同于病毒检测；如 Defender 明确报告威胁，请不要关闭防护或添加白名单，
   应停止运行该文件。
12. 登录与注册密码框旁可显示/隐藏密码，初始和离开页面后均恢复隐藏。
    程序启动默认为本地模式。登录页可以明确切换云端模式，数据将发送至你选择的后端。
    只使用可信的服务地址，不要把数据库连接串或 API Key 填入服务地址。
    本地和云端不会自动合并。云端登录令牌只存在内存，关闭应用后需重新登录。
13. 云端模式需要网络；网络失败不会偷偷保存到本地。写操作遇到超时可能已在服务端完成，
    请刷新核对后再决定是否重试。Render 免费服务可能休眠，首次连接请等待约一分钟。
    云端能力需先部署并验证可用的服务；有云端入口不代表服务已经开通。
14. 聊天附件、报告原件在云端模式保存到云库，OCR和语音识别仍在本机处理。
    AI生成依然由本机携带用户自己的Key调用所选服务，后端不保存AI Key。

安全边界：AI 功能连接本机 http://localhost:11434、Ollama 官方 https://ollama.com
和 DeepSeek 官方 https://api.deepseek.com；云端数据同步连接登录页显示的HTTPS后端。
获取 Key、查看用量和文档按钮打开相应官方网页。免费云服务适合小量测试，不保证永久免费、
不断线或无限存储；请保留可靠备份，不将此原型作为医疗服务的唯一数据系统。
"""


def assemble(source: Path, destination: Path) -> dict[str, object]:
    source = source.resolve()
    destination = destination.resolve()
    _validate_clean_source(source)
    if destination.exists():
        raise RuntimeError(f"发布目标已经存在，为防止覆盖已停止：{destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    # Build directly into a new, previously non-existent destination. On
    # Windows, antivirus scanners may briefly keep handles open in a freshly
    # copied PyInstaller tree and prevent an otherwise atomic directory rename.
    # Refusing any pre-existing destination still prevents accidental overwrite;
    # the subsequent release verifier rejects every partial or altered result.
    shutil.copytree(source, destination)
    data_dir = destination / "data"
    data_dir.mkdir()
    _create_empty_database(data_dir / "app.db")

    executable_hash = _sha256(destination / EXECUTABLE_NAME)
    metadata = {
        "application": APP_NAME,
        "version": APP_VERSION,
        "build_kind": "windows-x64-portable-onedir",
        "database_included": True,
        "database_state": "empty",
        "database_schema_version": SCHEMA_VERSION,
        "api_key_persisted": False,
        "executable_sha256": executable_hash,
        "assembled_at_utc": datetime.now(UTC).isoformat(),
    }
    (destination / "portable.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (destination / "使用说明.txt").write_text(_instructions(), encoding="utf-8-sig")

    files_for_manifest = sorted(
        (path for path in destination.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(destination).as_posix().casefold(),
    )
    manifest_lines = [
        f"{_sha256(path)}  {path.relative_to(destination).as_posix()}"
        for path in files_for_manifest
    ]
    (destination / "SHA256SUMS.txt").write_text(
        "\n".join(manifest_lines) + "\n",
        encoding="utf-8",
    )

    final_files = [path for path in destination.rglob("*") if path.is_file()]
    return {
        "path": str(destination),
        "version": APP_VERSION,
        "file_count": len(final_files),
        "size_bytes": sum(path.stat().st_size for path in final_files),
        "executable_sha256": executable_hash,
        "database_sha256": _sha256(destination / "data" / "app.db"),
    }


def main() -> int:
    if len(sys.argv) != 3:
        print("用法：assemble_release.py <干净 dist 目录> <发布目录>")
        return 2
    result = assemble(Path(sys.argv[1]), Path(sys.argv[2]))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
