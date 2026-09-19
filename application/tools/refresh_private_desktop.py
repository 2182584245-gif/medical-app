"""Copy a verified public runtime into a NEW, private-only desktop successor.

Never overwrites/moves the old installation, creates archives, calls the cloud,
or resets accounts. SQLite's online backup API includes committed WAL contents.
Only a sibling destination outside Git is accepted. A source changed during the
snapshot fails closed; changes made AFTER completion still belong to the old
installation, so the owner must stop using it before manually switching folders.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import stat
import time
from contextlib import ExitStack, closing
from datetime import UTC, datetime
from pathlib import Path

APP_NAME = "健康生活服务平台"
EXECUTABLE = APP_NAME + ".exe"
PRIVATE_README = "请先阅读-私有开发者副本.txt"
INCOMPLETE = "PRIVATE_REFRESH_INCOMPLETE.txt"
RECEIPT = "PRIVATE_REFRESH_RECEIPT.json"
NOTICE = "升级说明-仅本机私有副本.txt"
PENDING_EXE = "private-runtime.pending"
SIDECARS = ("-wal", "-shm", "-journal")


def _digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


def _ordinary(path: Path) -> None:
    """Refuse links/junctions, including all existing parents, before resolving."""
    for current in (path, *path.parents):
        if not os.path.lexists(current):
            continue
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RuntimeError("不允许符号链接、目录联接或其他重解析路径。")
        if not stat.S_ISREG(info.st_mode) and not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("只允许普通文件与文件夹。")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise RuntimeError("不允许硬链接文件。")


def _explicit(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or str(path).startswith("\\\\"):
        raise RuntimeError("必须提供本机明确绝对路径，不允许网络路径或父目录跳转。")
    _ordinary(path)
    return path.resolve()


def _outside_git(path: Path) -> None:
    if any(os.path.lexists(parent / ".git") for parent in (path, *path.parents)):
        raise RuntimeError("私有数据库源与目标必须位于 Git 仓库之外。")


def _tree(root: Path, *, ignore_runtime: bool = False) -> dict[str, Path]:
    files = {}
    for directory, names, filenames in os.walk(root, followlinks=False):
        here = Path(directory)
        _ordinary(here)
        for name in names:
            _ordinary(here / name)
            if name.casefold() == ".git":
                raise RuntimeError("私有副本中不允许嵌套 Git 仓库。")
        if ignore_runtime and here == root:
            names[:] = [name for name in names if name != "_internal"]
        for name in filenames:
            path = here / name
            _ordinary(path)
            relative = path.relative_to(root).as_posix()
            if not (ignore_runtime and relative == EXECUTABLE):
                files[relative] = path
    if len(files) != len({name.casefold() for name in files}):
        raise RuntimeError("不允许大小写冲突的文件路径。")
    return files


def _verify_public(root: Path, plan: Path | None) -> dict:
    script = Path(__file__).resolve().parents[1] / "packaging/verify_demo_release.py"
    spec = importlib.util.spec_from_file_location("private_refresh_public_gate", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.verify_directory(root, cloud_assets_plan=plan)


def _private_metadata(root: Path) -> dict:
    path = root / "portable.json"
    if not path.is_file() or path.stat().st_size > 65536:
        raise RuntimeError("缺少私有版本标识。")
    metadata = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(metadata, dict)
        or metadata.get("application") != APP_NAME
        or metadata.get("database_state") != "private_owner_copy"
        or metadata.get("synthetic") is not False
        or metadata.get("database_included") is not True
        or not (root / PRIVATE_README).is_file()
        or not (root / "data/app.db").is_file()
        or not (root / EXECUTABLE).is_file()
        or not (root / "_internal").is_dir()
        or (root / INCOMPLETE).exists()
    ):
        raise RuntimeError("源必须是完整且明确标识的私有桌面副本。")
    return metadata


def _databases(files: dict[str, Path]) -> set[str]:
    databases = set()
    for name, path in files.items():
        if name.endswith(SIDECARS):
            continue
        with path.open("rb") as stream:
            sqlite_header = stream.read(16) == b"SQLite format 3\x00"
        named_database = path.suffix.casefold() in {".db", ".sqlite", ".sqlite3"}
        if sqlite_header or named_database:
            if not name.startswith("data/") or not sqlite_header:
                raise RuntimeError("数据库位置或格式异常；保留原目录并停止。")
            databases.add(name)
    if "data/app.db" not in databases:
        raise RuntimeError("缺少有效的本地业务数据库。")
    for name in files:
        if name.endswith(SIDECARS) and name.rsplit("-", 1)[0] not in databases:
            raise RuntimeError("发现不属于已识别数据库的 SQLite 附属文件。")
    return databases


def _stable_files(files: dict[str, Path], databases: set[str]) -> dict[str, str]:
    return {
        name: _digest(path)
        for name, path in files.items()
        if name not in databases and not name.endswith(SIDECARS)
    }


def _backup(source: sqlite3.Connection, destination: Path, deadline: float) -> dict:
    def progress(_status, _remaining, _total):
        if time.monotonic() > deadline:
            raise RuntimeError("数据库繁忙或备份超时，请停止录入后重新复制到另一全新目录。")

    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(destination)) as target:
        source.backup(target, pages=256, progress=progress, sleep=0.02)
        if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("目标数据库完整性检查失败，原库未改动。")
        if target.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("目标数据库关联检查失败，原库未改动。")
        schema = target.execute("PRAGMA user_version").fetchone()[0]
        target.execute("PRAGMA journal_mode=DELETE")
    return {"schema_version": schema, "sha256": _digest(destination)}


def refresh(
    runtime: Path,
    private: Path,
    destination: Path,
    *,
    cloud_assets_plan: Path | None = None,
    timeout_seconds: float = 30.0,
) -> dict:
    """Produce only a private sibling copy; never activate it or mutate inputs."""
    runtime, private, destination = map(_explicit, (runtime, private, destination))
    if not 0 < timeout_seconds <= 120:
        raise RuntimeError("备份超时必须在 0 至 120 秒之间。")
    for path in (private, destination):
        _outside_git(path)
    if (
        not runtime.is_dir()
        or not private.is_dir()
        or destination.exists()
        or destination.parent != private.parent
        or not destination.name.startswith(private.name + "-")
        or destination.suffix.casefold() == ".zip"
    ):
        raise RuntimeError("目标必须是私有源同层、名称以源名加短横线开头的全新文件夹。")
    paths = (runtime, private, destination)
    if any(
        first == second or first.is_relative_to(second) or second.is_relative_to(first)
        for index, first in enumerate(paths)
        for second in paths[index + 1:]
    ):
        raise RuntimeError("公共来源、私有来源、目标路径不得重叠或互相包含。")
    plan = _explicit(cloud_assets_plan) if cloud_assets_plan is not None else None
    _tree(runtime)
    public_manifest = (runtime / "SHA256SUMS.txt").read_bytes()
    public_report = _verify_public(runtime, plan)
    metadata = _private_metadata(private)
    files = _tree(private, ignore_runtime=True)
    databases = _databases(files)
    if any(name in files for name in (INCOMPLETE, PENDING_EXE)):
        raise RuntimeError("私有来源有未完成的升级，请人工复核。")
    public_metadata = json.loads((runtime / "portable.json").read_text(encoding="utf-8"))
    if (
        metadata.get("version") != public_metadata.get("version")
        or metadata.get("database_schema_version") != public_metadata.get("database_schema_version")
    ):
        raise RuntimeError("本工具只刷新同版本运行文件，不执行版本或数据库结构迁移。")
    runtime_files = {
        name: path for name, path in _tree(runtime).items()
        if name == EXECUTABLE or name.startswith("_internal/")
    }
    manifest_hashes = {
        line.partition("  ")[2]: line.partition("  ")[0].lower()
        for line in public_manifest.decode("utf-8").splitlines()
    }
    runtime_hashes = {name: manifest_hashes[name] for name in runtime_files}
    if runtime_hashes[EXECUTABLE] != public_report["executable_sha256"].lower():
        raise RuntimeError("公共运行文件清单与验收结果不一致。")
    destination.mkdir(mode=0o700)
    (destination / INCOMPLETE).write_text(
        "私有升级尚未验收：不要启动、打包或上传。原目录未改动。\n", encoding="utf-8"
    )
    # Runtime copy can take minutes; capture user data only AFTER it completes.
    for name, path in runtime_files.items():
        target = destination / (PENDING_EXE if name == EXECUTABLE else name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        if _digest(target) != runtime_hashes[name] or _digest(path) != runtime_hashes[name]:
            raise RuntimeError("公共运行文件在复制中变化，未完成升级。")
    if (runtime / "SHA256SUMS.txt").read_bytes() != public_manifest:
        raise RuntimeError("公共来源清单在复制期间变化。")
    files = _tree(private, ignore_runtime=True)
    if _databases(files) != databases:
        raise RuntimeError("私有数据库集合在升级过程中变化。")
    before = _stable_files(files, databases)
    if json.loads((private / "portable.json").read_text(encoding="utf-8")) != metadata:
        raise RuntimeError("私有版本标识在升级过程中变化。")
    snapshots, connections = {}, []
    with ExitStack() as stack:
        deadline = time.monotonic() + timeout_seconds
        for name in sorted(databases):
            source = stack.enter_context(closing(sqlite3.connect(
                files[name].as_uri() + "?mode=ro", uri=True, timeout=min(timeout_seconds, 5)
            )))
            source.execute("PRAGMA query_only=ON")
            version = source.execute("PRAGMA data_version").fetchone()[0]
            source.execute("BEGIN")
            source.execute("SELECT COUNT(*) FROM sqlite_schema").fetchone()
            snapshots[name] = _backup(source, destination / name, deadline)
            source.rollback()
            connections.append((source, version))
        if snapshots["data/app.db"]["schema_version"] != metadata["database_schema_version"]:
            raise RuntimeError("私有数据库实际结构版本与标识不一致。")
        for name, expected in before.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(files[name], target)
            if _digest(target) != expected:
                raise RuntimeError("附件或私有说明在复制中变化，请停止录入后重试。")
        after_files = _tree(private, ignore_runtime=True)
        if _databases(after_files) != databases or _stable_files(after_files, databases) != before:
            raise RuntimeError("私有附件或配置在复制中变化，请停止录入后重试。")
        if any(conn.execute("PRAGMA data_version").fetchone()[0] != v for conn, v in connections):
            raise RuntimeError("旧应用在复制期间新增了记录，请停止录入后复制到另一新目录。")
    report = {
        "status": "private_copy_created_not_activated",
        "destination": str(destination),
        "private_only": True,
        "public_distribution_allowed": False,
        "cloud_connected": False,
        "source_overwritten": False,
        "automatic_switch": False,
        "snapshot_completed_at_utc": datetime.now(UTC).isoformat(),
        "runtime_executable_sha256": public_report["executable_sha256"],
        "source_public_manifest_sha256": hashlib.sha256(public_manifest).hexdigest(),
        "database_snapshots": snapshots,
        "readme_preserved_sha256": before[PRIVATE_README],
        "warning": "复制完成后的旧目录新增资料不会自动合并；切换前请停止使用旧版。禁止公开打包。",
    }
    updated = dict(metadata)
    updated.update(
        executable_sha256=public_report["executable_sha256"],
        source_executable_sha256=public_report["executable_sha256"],
        private_refresh={
            "receipt": RECEIPT,
            "previous_executable_sha256": metadata.get("executable_sha256"),
            "public_distribution_allowed": False,
        },
    )
    (destination / "portable.json").write_text(
        json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (destination / RECEIPT).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (destination / NOTICE).write_text(
        "这是仅供本机使用的私有升级副本，不得压缩到公开发布包或上传 Git。\n"
        "原目录没有被替换、移动或删除；本工具不会结束正在使用的应用。\n"
        "此处数据只包含复制完成时的快照。旧目录后续新增内容不会自动合并到这里。\n"
        "请停止使用旧版，核对新目录资料后，再由本人决定切换目录。\n"
        "原私有说明已完整保留；外部 Windows 用户加密设置未被移动或改写。\n"
        "没有连接云端，没有上传任何本地历史。\n",
        encoding="utf-8-sig",
    )
    (destination / PENDING_EXE).rename(destination / EXECUTABLE)
    (destination / INCOMPLETE).unlink()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--private", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--cloud-assets-plan", type=Path)
    args = parser.parse_args()
    try:
        result = refresh(
            args.runtime, args.private, args.destination, cloud_assets_plan=args.cloud_assets_plan
        )
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        print("私有升级未完成；原目录未覆盖。请检查路径、私有标识、完整性或录入活动后重试。")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
