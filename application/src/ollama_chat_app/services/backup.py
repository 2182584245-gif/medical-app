from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import stat
import tempfile
import threading
import uuid
import zipfile
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from ..config import APP_NAME, APP_VERSION
from ..data.database import SCHEMA_VERSION, Database
from ..time_utils import beijing_now

DATA_DATABASE_PATH = PurePosixPath("data/app.db")
DATA_MANIFEST_PATH = PurePosixPath("backup.json")
DATA_README_PATH = PurePosixPath("README.txt")

# Backward-compatible names used by the rest of the application. Their values
# deliberately point to the new data-only archive layout.
PORTABLE_DATABASE_PATH = DATA_DATABASE_PATH
PORTABLE_MANIFEST_PATH = DATA_MANIFEST_PATH
PORTABLE_README_PATH = DATA_README_PATH

_FORMAT_VERSION = 2
_WORK_DIRECTORY_PREFIX = ".ollama-chat-data-backup-"
_IMPORT_DIRECTORY_PREFIX = ".ollama-chat-data-import-"
_SNAPSHOT_PREFIX = ".ollama-chat-snapshot-"
_COPY_BUFFER_SIZE = 1024 * 1024
# A user may keep up to 100 MiB of documents.  Keep an absolute archive guard
# well above one account's quota while still bounding decompression and checks.
_MAX_DATABASE_SIZE = 2 * 1024 * 1024 * 1024
_MAX_SMALL_MEMBER_SIZE = 64 * 1024
_ALLOWED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
_EXPECTED_MEMBERS = {
    DATA_DATABASE_PATH.as_posix(),
    DATA_MANIFEST_PATH.as_posix(),
    DATA_README_PATH.as_posix(),
}


class BackupError(RuntimeError):
    """Base error safe for the UI to handle as a failed data operation."""


class BackupValidationError(BackupError):
    """The selected backup, database, or destination is not safe to use."""


class BackupInProgressError(BackupError):
    """The same service instance is already exporting or importing data."""


class BackupDestinationExistsError(BackupError):
    """A non-overwriting snapshot destination appeared during migration."""


@dataclass(frozen=True, slots=True)
class PortableBackupResult:
    """Result of a data-only export.

    The historical class name is retained so existing application wiring keeps
    working. The archive never contains an executable or ``_internal``.
    """

    path: Path
    file_count: int
    size_bytes: int
    created_at: datetime
    database_member: str
    bundle_id: str


DataBackupResult = PortableBackupResult


@dataclass(frozen=True, slots=True)
class DataImportResult:
    path: Path
    safety_backup_path: Path
    imported_at: datetime
    bundle_id: str
    user_count: int
    message_count: int


@dataclass(frozen=True, slots=True)
class _DatabaseInspection:
    schema_version: int
    user_count: int
    message_count: int


@dataclass(frozen=True, slots=True)
class _ArchiveInspection:
    bundle_id: str
    created_at: datetime
    schema_version: int
    database_sha256: str
    database_size: int


def snapshot_database(source: Path, destination: Path) -> None:
    """Atomically replace *destination* with a consistent SQLite snapshot.

    SQLite's online backup API includes committed WAL pages and avoids a torn
    copy while normal application writes are taking place.
    """

    _snapshot_database(Path(source), Path(destination), overwrite=True)


def migrate_legacy_database(source: Path, destination: Path) -> bool:
    """Snapshot legacy data once, without replacing populated current data."""

    source_path = Path(source).expanduser()
    destination_path = Path(destination).expanduser()
    if not source_path.exists():
        return False
    if destination_path.exists():
        if not destination_path.is_file() or _is_link_or_junction(destination_path):
            raise BackupValidationError("便携数据库目标不是普通文件")
        if _database_user_count(destination_path) > 0:
            return False
        if _database_user_count(source_path) == 0:
            return False
        _snapshot_database(source_path, destination_path, overwrite=True)
        return True
    _snapshot_database(source_path, destination_path, overwrite=False)
    return True


class PortableBackupService:
    """Export and import a strictly limited, data-only backup.

    ``application_dir`` remains an accepted constructor argument for source and
    packaged-app compatibility, but is intentionally never read. In particular,
    this service cannot locate, copy, or archive the running executable.
    """

    def __init__(self, database: Database, application_dir: Path | None = None) -> None:
        self.database = database
        # Explicitly discard the legacy parameter. Keeping it out of instance
        # state makes accidental application-directory traversal harder to add.
        del application_dir
        self._operation_lock = threading.Lock()

    def create_archive(self, destination: str | Path) -> PortableBackupResult:
        """Create and atomically publish one data-only ZIP archive."""

        if not self._operation_lock.acquire(blocking=False):
            raise BackupInProgressError("数据导入或导出正在进行，请勿重复操作")
        try:
            return self._create_archive(Path(destination), backup_kind="manual_export")
        finally:
            self._operation_lock.release()

    def import_archive(self, source: str | Path) -> DataImportResult:
        """Validate and atomically import a data backup.

        A verified data-only backup of the current database is published before
        replacement. If validation, backup, locking, or replacement fails, the
        current database is left in place.
        """

        if not self._operation_lock.acquire(blocking=False):
            raise BackupInProgressError("数据导入或导出正在进行，请勿重复操作")
        try:
            return self._import_archive(Path(source))
        finally:
            self._operation_lock.release()

    def _create_archive(
        self,
        destination: Path,
        *,
        backup_kind: str,
    ) -> PortableBackupResult:
        source_database = self.database.path.expanduser()
        if _is_link_or_junction(source_database):
            raise BackupValidationError("当前数据库不能是符号链接或联接文件")
        source_database = source_database.resolve()
        target = _safe_destination_path(destination)
        _validate_export_paths(source_database, target)

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise BackupError("无法创建备份目标目录") from error

        created_at = datetime.now(UTC)
        bundle_id = str(uuid.uuid4())
        try:
            with tempfile.TemporaryDirectory(
                prefix=_WORK_DIRECTORY_PREFIX,
                dir=target.parent,
                ignore_cleanup_errors=True,
            ) as working_directory_text:
                working_directory = Path(working_directory_text)
                snapshot_path = working_directory / "snapshot.db"
                temporary_archive = working_directory / "data-backup.zip.partial"

                snapshot_database(source_database, snapshot_path)
                database_details = _validate_database(snapshot_path)
                database_sha256 = _sha256_file(snapshot_path)
                database_size = snapshot_path.stat().st_size
                readme = _readme_text().encode("utf-8")
                manifest = _build_manifest(
                    created_at=created_at,
                    bundle_id=bundle_id,
                    backup_kind=backup_kind,
                    schema_version=database_details.schema_version,
                    database_sha256=database_sha256,
                    database_size=database_size,
                )
                _write_data_archive(
                    temporary_archive,
                    snapshot_path=snapshot_path,
                    manifest=manifest,
                    readme=readme,
                )
                _inspect_archive(temporary_archive)
                _fsync_file(temporary_archive)
                size_bytes = temporary_archive.stat().st_size
                os.replace(temporary_archive, target)
        except BackupError:
            raise
        except (
            OSError,
            sqlite3.Error,
            UnicodeError,
            ValueError,
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
        ) as error:
            raise BackupError("创建数据备份失败") from error

        return PortableBackupResult(
            path=target,
            file_count=len(_EXPECTED_MEMBERS),
            size_bytes=size_bytes,
            created_at=created_at,
            database_member=DATA_DATABASE_PATH.as_posix(),
            bundle_id=bundle_id,
        )

    def _import_archive(self, source: Path) -> DataImportResult:
        archive_path = source.expanduser()
        if _is_link_or_junction(archive_path):
            raise BackupValidationError("所选备份不能是符号链接或联接文件")
        if not archive_path.is_file():
            raise BackupValidationError("所选数据备份不存在")
        archive_path = archive_path.resolve()
        if archive_path.suffix.casefold() != ".zip":
            raise BackupValidationError("数据备份必须使用 .zip 扩展名")

        current_database = self.database.path.expanduser()
        if _is_link_or_junction(current_database):
            raise BackupValidationError("当前数据库不能是符号链接或联接文件")
        if not current_database.is_file():
            raise BackupValidationError("当前数据库不存在，无法安全导入")
        current_database = current_database.resolve()
        if _same_path(archive_path, current_database):
            raise BackupValidationError("数据备份不能与当前数据库是同一个文件")

        try:
            with tempfile.TemporaryDirectory(
                prefix=_IMPORT_DIRECTORY_PREFIX,
                dir=current_database.parent,
                ignore_cleanup_errors=True,
            ) as working_directory_text:
                staged_database = Path(working_directory_text) / "imported.db"
                archive_details, database_details = _stage_import_database(
                    archive_path,
                    staged_database,
                )

                safety_backup = _new_safety_backup_path(current_database.parent)
                self._create_archive(
                    safety_backup,
                    backup_kind="pre_import_safety",
                )

                try:
                    _prepare_database_replacement(current_database)
                    os.replace(staged_database, current_database)
                except (OSError, sqlite3.Error, BackupError) as error:
                    raise BackupError(
                        f"数据没有被替换；导入前自动备份已保留在：{safety_backup}"
                    ) from error
        except BackupError:
            raise
        except (zipfile.BadZipFile, zipfile.LargeZipFile) as error:
            raise BackupValidationError("所选文件不是有效的数据备份 ZIP") from error
        except (
            OSError,
            sqlite3.Error,
            UnicodeError,
            ValueError,
        ) as error:
            raise BackupError("导入数据备份失败，当前数据没有被替换") from error

        return DataImportResult(
            path=archive_path,
            safety_backup_path=safety_backup,
            imported_at=datetime.now(UTC),
            bundle_id=archive_details.bundle_id,
            user_count=database_details.user_count,
            message_count=database_details.message_count,
        )


def _validate_export_paths(source_database: Path, target: Path) -> None:
    if not source_database.is_file():
        raise BackupValidationError("当前数据库不存在")
    if target.suffix.casefold() != ".zip":
        raise BackupValidationError("备份文件必须使用 .zip 扩展名")
    if target.exists() and (not target.is_file() or _is_link_or_junction(target)):
        raise BackupValidationError("备份目标不是普通文件")
    if _same_path(source_database, target):
        raise BackupValidationError("备份目标不能覆盖正在使用的数据库")


def _safe_destination_path(destination: Path) -> Path:
    expanded = destination.expanduser()
    if expanded.exists() and _is_link_or_junction(expanded):
        raise BackupValidationError("备份目标不能是符号链接或联接文件")
    # Resolve existing parent components without following a target-file link.
    return expanded.parent.resolve() / expanded.name


def _write_data_archive(
    archive_path: Path,
    *,
    snapshot_path: Path,
    manifest: bytes,
    readme: bytes,
) -> None:
    with zipfile.ZipFile(
        archive_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        allowZip64=True,
        strict_timestamps=False,
    ) as archive:
        archive.write(snapshot_path, DATA_DATABASE_PATH.as_posix())
        archive.writestr(DATA_MANIFEST_PATH.as_posix(), manifest)
        archive.writestr(DATA_README_PATH.as_posix(), readme)


def _stage_import_database(
    archive_path: Path,
    destination: Path,
) -> tuple[_ArchiveInspection, _DatabaseInspection]:
    with zipfile.ZipFile(archive_path, mode="r") as archive:
        archive_details = _inspect_open_archive(archive)
        database_info = archive.getinfo(DATA_DATABASE_PATH.as_posix())
        digest = hashlib.sha256()
        copied = 0
        try:
            with archive.open(database_info, mode="r") as source, destination.open("xb") as target:
                while chunk := source.read(_COPY_BUFFER_SIZE):
                    copied += len(chunk)
                    if copied > _MAX_DATABASE_SIZE:
                        raise BackupValidationError("备份中的数据库过大，已停止导入")
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
        except RuntimeError as error:
            raise BackupValidationError("无法安全读取备份中的数据库") from error

    if copied != archive_details.database_size:
        raise BackupValidationError("备份中的数据库大小与清单不一致")
    if not hmac.compare_digest(digest.hexdigest(), archive_details.database_sha256):
        raise BackupValidationError("数据库校验值不一致，备份可能已损坏或被修改")
    database_details = _validate_database(
        destination,
        expected_schema_version=archive_details.schema_version,
    )
    if archive_details.schema_version < SCHEMA_VERSION:
        Database(destination).initialize()
        database_details = _validate_database(destination)
    return archive_details, database_details


def _inspect_archive(archive_path: Path) -> _ArchiveInspection:
    with zipfile.ZipFile(archive_path, mode="r") as archive:
        return _inspect_open_archive(archive)


def _inspect_open_archive(archive: zipfile.ZipFile) -> _ArchiveInspection:
    infos = archive.infolist()
    if len(infos) != len(_EXPECTED_MEMBERS):
        raise BackupValidationError("数据备份文件清单不正确")

    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        raise BackupValidationError("数据备份包含重复文件名")
    for info in infos:
        _validate_zip_member(info)
    if set(names) != _EXPECTED_MEMBERS:
        raise BackupValidationError("数据备份包含多余文件或缺少必要文件")

    database_info = archive.getinfo(DATA_DATABASE_PATH.as_posix())
    manifest_info = archive.getinfo(DATA_MANIFEST_PATH.as_posix())
    readme_info = archive.getinfo(DATA_README_PATH.as_posix())
    if database_info.file_size <= 0 or database_info.file_size > _MAX_DATABASE_SIZE:
        raise BackupValidationError("备份中的数据库大小无效")
    if manifest_info.file_size <= 0 or manifest_info.file_size > _MAX_SMALL_MEMBER_SIZE:
        raise BackupValidationError("备份清单大小无效")
    if readme_info.file_size <= 0 or readme_info.file_size > _MAX_SMALL_MEMBER_SIZE:
        raise BackupValidationError("备份说明大小无效")

    try:
        manifest_bytes = archive.read(manifest_info)
        readme = archive.read(readme_info).decode("utf-8", errors="strict")
    except (RuntimeError, UnicodeError, zipfile.BadZipFile) as error:
        raise BackupValidationError("备份清单或说明无法读取") from error
    if "只包含本地数据" not in readme or "未加密" not in readme:
        raise BackupValidationError("备份说明不符合当前安全格式")

    manifest = _decode_manifest(manifest_bytes)
    details = _validate_manifest(manifest, database_info)
    broken_member = archive.testzip()
    if broken_member is not None:
        raise BackupValidationError(f"ZIP 完整性检查失败：{broken_member}")
    return details


def _validate_zip_member(info: zipfile.ZipInfo) -> None:
    name = info.filename
    if not name or "\\" in name or "\x00" in name or info.is_dir():
        raise BackupValidationError("数据备份包含无效文件路径")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise BackupValidationError("数据备份包含不安全文件路径")
    if any(":" in part for part in path.parts):
        raise BackupValidationError("数据备份包含不安全文件路径")
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    if unix_mode and stat.S_ISLNK(unix_mode):
        raise BackupValidationError("数据备份不能包含符号链接")
    if info.flag_bits & 0x1:
        raise BackupValidationError("不支持加密 ZIP，请使用应用导出的数据备份")
    if info.compress_type not in _ALLOWED_COMPRESSION:
        raise BackupValidationError("数据备份使用了不支持的压缩方式")


def _decode_manifest(payload: bytes) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise BackupValidationError("备份清单包含重复字段")
            result[key] = value
        return result

    try:
        parsed = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except BackupValidationError:
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise BackupValidationError("备份清单格式无效") from error
    if not isinstance(parsed, dict):
        raise BackupValidationError("备份清单必须是一个对象")
    return parsed


def _validate_manifest(
    manifest: dict[str, Any],
    database_info: zipfile.ZipInfo,
) -> _ArchiveInspection:
    _require_exact_keys(
        manifest,
        {
            "format_version",
            "application_name",
            "application_version",
            "schema_version",
            "created_at_utc",
            "bundle_id",
            "backup_kind",
            "included_files",
            "database",
            "security",
        },
        "备份清单",
    )
    if type(manifest["format_version"]) is not int or (
        manifest["format_version"] != _FORMAT_VERSION
    ):
        raise BackupValidationError("备份格式版本不受支持")
    if manifest["application_name"] not in {APP_NAME, "Ollama 双模聊天"}:
        raise BackupValidationError("这不是本应用生成的数据备份")
    application_version = manifest["application_version"]
    if not isinstance(application_version, str) or not application_version.strip():
        raise BackupValidationError("备份中的应用版本无效")
    schema_version = manifest["schema_version"]
    if type(schema_version) is not int or schema_version < 1 or schema_version > SCHEMA_VERSION:
        raise BackupValidationError(
            f"数据库版本不兼容：支持 1 到 {SCHEMA_VERSION}，实际为 {schema_version}"
        )
    if not isinstance(manifest["backup_kind"], str) or manifest["backup_kind"] not in {
        "manual_export",
        "pre_import_safety",
    }:
        raise BackupValidationError("备份类型无效")
    if manifest["included_files"] != sorted(_EXPECTED_MEMBERS):
        raise BackupValidationError("备份清单中的文件列表不正确")

    database = manifest["database"]
    if not isinstance(database, dict):
        raise BackupValidationError("备份清单中的数据库信息无效")
    _require_exact_keys(database, {"path", "sha256", "size_bytes"}, "数据库清单")
    if database["path"] != DATA_DATABASE_PATH.as_posix():
        raise BackupValidationError("备份清单中的数据库路径无效")
    database_sha256 = database["sha256"]
    if (
        not isinstance(database_sha256, str)
        or len(database_sha256) != 64
        or any(character not in "0123456789abcdef" for character in database_sha256)
    ):
        raise BackupValidationError("备份清单中的数据库校验值无效")
    database_size = database["size_bytes"]
    if type(database_size) is not int or database_size != database_info.file_size:
        raise BackupValidationError("备份清单中的数据库大小不一致")

    security = manifest["security"]
    if not isinstance(security, dict):
        raise BackupValidationError("备份清单中的安全信息无效")
    _require_exact_keys(
        security,
        {"encrypted", "api_key_included", "application_files_included"},
        "安全清单",
    )
    if any(type(security[key]) is not bool for key in security):
        raise BackupValidationError("备份清单中的安全声明类型无效")
    if security != {
        "encrypted": False,
        "api_key_included": False,
        "application_files_included": False,
    }:
        raise BackupValidationError("备份清单中的安全声明无效")

    bundle_id = manifest["bundle_id"]
    if not isinstance(bundle_id, str):
        raise BackupValidationError("备份编号无效")
    try:
        bundle_id = str(uuid.UUID(bundle_id))
    except ValueError as error:
        raise BackupValidationError("备份编号无效") from error

    created_at_text = manifest["created_at_utc"]
    if not isinstance(created_at_text, str) or not created_at_text.endswith("Z"):
        raise BackupValidationError("备份时间无效")
    try:
        created_at = datetime.fromisoformat(created_at_text[:-1] + "+00:00")
    except ValueError as error:
        raise BackupValidationError("备份时间无效") from error
    if created_at.tzinfo is None:
        raise BackupValidationError("备份时间无效")

    return _ArchiveInspection(
        bundle_id=bundle_id,
        created_at=created_at.astimezone(UTC),
        schema_version=schema_version,
        database_sha256=database_sha256,
        database_size=database_size,
    )


def _require_exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise BackupValidationError(f"{label}字段不完整或包含未知字段")


def _build_manifest(
    *,
    created_at: datetime,
    bundle_id: str,
    backup_kind: str,
    schema_version: int,
    database_sha256: str,
    database_size: int,
) -> bytes:
    payload = {
        "format_version": _FORMAT_VERSION,
        "application_name": APP_NAME,
        "application_version": APP_VERSION,
        "schema_version": schema_version,
        "created_at_utc": created_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "bundle_id": bundle_id,
        "backup_kind": backup_kind,
        "included_files": sorted(_EXPECTED_MEMBERS),
        "database": {
            "path": DATA_DATABASE_PATH.as_posix(),
            "sha256": database_sha256,
            "size_bytes": database_size,
        },
        "security": {
            "encrypted": False,
            "api_key_included": False,
            "application_files_included": False,
        },
    }
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _readme_text() -> str:
    return (
        f"{APP_NAME}——数据备份\n"
        "========================\n\n"
        "此 ZIP 只包含本地数据：账户、密码哈希、聊天、生活记录、档案、服务、"
        "商品、推荐、模拟订单，以及已上传的图片、PDF 与健康报告原件。\n"
        "它不包含 EXE、_internal、Ollama 程序、模型或云端 API Key。\n\n"
        f"请在{APP_NAME}中使用“导入数据”选择本 ZIP，"
        "不要手动替换正在使用的数据库。导入前，应用会自动另存一份"
        "当前数据备份；导入后需要重新登录。\n\n"
        "安全提醒：此 ZIP 未加密，聊天正文属于敏感数据，请妥善保管，"
        "不要发送给不信任的人。\n"
    )


def _snapshot_database(source: Path, destination: Path, *, overwrite: bool) -> None:
    source_path = source.expanduser()
    destination_path = destination.expanduser()
    if _is_link_or_junction(source_path):
        raise BackupValidationError("源数据库不能是符号链接或联接文件")
    if not source_path.is_file():
        raise BackupValidationError("源数据库不存在")
    source_path = source_path.resolve()
    destination_path = _safe_snapshot_destination(destination_path)
    if _same_path(source_path, destination_path):
        raise BackupValidationError("数据库快照目标不能与源文件相同")
    if destination_path.exists() and not overwrite:
        raise BackupDestinationExistsError("数据库快照目标已存在")
    if destination_path.exists() and (
        not destination_path.is_file() or _is_link_or_junction(destination_path)
    ):
        raise BackupValidationError("数据库快照目标不是普通文件")

    try:
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=_SNAPSHOT_PREFIX,
            suffix=".db.partial",
            dir=destination_path.parent,
        )
        os.close(descriptor)
    except OSError as error:
        raise BackupError("无法创建数据库快照临时文件") from error

    temporary_path = Path(temporary_name)
    published = False
    try:
        read_only_uri = f"{source_path.as_uri()}?mode=ro"
        with closing(sqlite3.connect(read_only_uri, uri=True, timeout=5.0)) as source_connection:
            source_connection.execute("PRAGMA query_only = ON")
            with closing(sqlite3.connect(temporary_path, timeout=5.0)) as target_connection:
                source_connection.backup(target_connection, pages=256, sleep=0.01)
                target_connection.commit()

        source_schema_version = _database_schema_version(source_path)
        _validate_database(
            temporary_path,
            expected_schema_version=source_schema_version,
        )
        if source_schema_version < SCHEMA_VERSION:
            Database(temporary_path).initialize()
            _validate_database(temporary_path)
        _fsync_file(temporary_path)
        if overwrite:
            os.replace(temporary_path, destination_path)
        else:
            if destination_path.exists():
                raise BackupDestinationExistsError("数据库快照目标已存在")
            try:
                os.rename(temporary_path, destination_path)
            except FileExistsError as error:
                raise BackupDestinationExistsError("数据库快照目标已存在") from error
        published = True
    except BackupError:
        raise
    except (OSError, sqlite3.Error) as error:
        raise BackupError("创建数据库一致性快照失败") from error
    finally:
        if not published:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)


def _safe_snapshot_destination(destination: Path) -> Path:
    if destination.exists() and _is_link_or_junction(destination):
        raise BackupValidationError("数据库快照目标不能是符号链接或联接文件")
    return destination.parent.resolve() / destination.name


def _validate_database(
    database_path: Path,
    *,
    expected_schema_version: int = SCHEMA_VERSION,
) -> _DatabaseInspection:
    path = database_path.resolve()
    try:
        if path.stat().st_size <= 0 or path.stat().st_size > _MAX_DATABASE_SIZE:
            raise BackupValidationError("数据库文件大小无效")
        with path.open("rb") as source:
            if source.read(16) != b"SQLite format 3\x00":
                raise BackupValidationError("文件不是有效的 SQLite 数据库")

        read_only_uri = f"{path.as_uri()}?mode=ro"
        with closing(sqlite3.connect(read_only_uri, uri=True, timeout=5.0)) as connection:
            connection.execute("PRAGMA query_only = ON")
            with suppress(sqlite3.DatabaseError):
                connection.execute("PRAGMA trusted_schema = OFF")
            quick_check = connection.execute("PRAGMA quick_check").fetchall()
            if quick_check != [("ok",)]:
                raise BackupValidationError("数据库完整性检查失败")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise BackupValidationError("数据库外键检查失败")
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if schema_version != expected_schema_version:
                raise BackupValidationError(
                    f"数据库版本不兼容：需要 {expected_schema_version}，实际为 {schema_version}"
                )
            if _schema_signature(connection) != _expected_schema_signature(expected_schema_version):
                raise BackupValidationError("数据库结构与当前应用不一致")
            user_count = int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])
            message_count = int(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
    except BackupError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as error:
        raise BackupValidationError("数据库无法通过安全检查") from error
    return _DatabaseInspection(schema_version, user_count, message_count)


def _schema_signature(connection: sqlite3.Connection) -> tuple[tuple[str, str, str, str], ...]:
    rows = connection.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_schema
        ORDER BY type, name, tbl_name
        """
    ).fetchall()
    return tuple(
        (
            str(row[0]),
            str(row[1]),
            str(row[2]),
            " ".join(str(row[3] or "").split()),
        )
        for row in rows
    )


def _expected_schema_signature(
    schema_version: int = SCHEMA_VERSION,
) -> tuple[tuple[str, str, str, str], ...]:
    with closing(sqlite3.connect(":memory:")) as connection:
        if schema_version == 1:
            Database._create_schema_v1(connection)
        elif schema_version == 2:
            Database._create_schema_v2(connection)
        elif schema_version == 3:
            Database._create_schema_v3(connection)
        elif schema_version == 4:
            Database._create_schema_v4(connection)
        elif schema_version == 5:
            Database._create_schema_v5(connection)
        elif schema_version == 6:
            from ..data.experience_schema import migrate_experience

            Database._create_schema_v5(connection)
            migrate_experience(connection)
        else:  # pragma: no cover - all callers validate the supported range
            raise BackupValidationError("数据库版本不受支持")
        return _schema_signature(connection)


def _database_user_count(database_path: Path) -> int:
    version = _database_schema_version(database_path)
    return _validate_database(
        database_path,
        expected_schema_version=version,
    ).user_count


def _database_schema_version(database_path: Path) -> int:
    path = database_path.expanduser().resolve()
    try:
        read_only_uri = f"{path.as_uri()}?mode=ro"
        with closing(sqlite3.connect(read_only_uri, uri=True, timeout=5.0)) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    except (OSError, sqlite3.Error, TypeError, ValueError) as error:
        raise BackupValidationError("数据库版本无法读取") from error
    if version < 1 or version > SCHEMA_VERSION:
        raise BackupValidationError(
            f"数据库版本不兼容：支持 1 到 {SCHEMA_VERSION}，实际为 {version}"
        )
    return version


def _new_safety_backup_path(database_directory: Path) -> Path:
    backup_directory = database_directory / "backups"
    if backup_directory.exists() and (
        not backup_directory.is_dir() or _is_link_or_junction(backup_directory)
    ):
        raise BackupValidationError("自动备份目录不是安全的普通文件夹")
    backup_directory.mkdir(parents=True, exist_ok=True)
    timestamp = beijing_now().strftime("%Y%m%d-%H%M%S")
    unique_suffix = uuid.uuid4().hex[:8]
    return backup_directory / f"导入前自动备份-{timestamp}-{unique_suffix}.zip"


def _prepare_database_replacement(database_path: Path) -> None:
    """Ensure no WAL or concurrent connection can alter the imported file."""

    try:
        with closing(sqlite3.connect(database_path, timeout=5.0)) as connection:
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
            if journal_mode == "wal":
                checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint is None or int(checkpoint[0]) != 0:
                    raise BackupError("当前数据库仍被其他操作占用，请稍后重试")
                changed_mode = str(
                    connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
                ).casefold()
                if changed_mode != "delete":
                    raise BackupError("无法安全结束当前数据库写入，请重启应用后重试")
            connection.execute("BEGIN EXCLUSIVE")
            connection.commit()
    except BackupError:
        raise
    except sqlite3.Error as error:
        raise BackupError("当前数据库正在使用，无法安全导入") from error

    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{database_path}{suffix}")
        if sidecar.exists():
            raise BackupError("检测到尚未结束的数据库写入，请重启应用后再导入")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_COPY_BUFFER_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as handle:
        handle.flush()
        os.fsync(handle.fileno())


def _same_path(first: Path, second: Path) -> bool:
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


__all__ = [
    "BackupDestinationExistsError",
    "BackupError",
    "BackupInProgressError",
    "BackupValidationError",
    "DATA_DATABASE_PATH",
    "DATA_MANIFEST_PATH",
    "DATA_README_PATH",
    "DataBackupResult",
    "DataImportResult",
    "PORTABLE_DATABASE_PATH",
    "PORTABLE_MANIFEST_PATH",
    "PORTABLE_README_PATH",
    "PortableBackupResult",
    "PortableBackupService",
    "migrate_legacy_database",
    "snapshot_database",
]
