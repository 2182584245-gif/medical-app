from __future__ import annotations

import hashlib
import hmac
import io
import os
import re
import sqlite3
import tempfile
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import date
from pathlib import Path
from typing import Any

from ..data.database import Database, timestamp_from_db, timestamp_to_db, utc_now

MAX_FILE_SIZE = 15 * 1024 * 1024
MAX_USER_STORAGE = 100 * 1024 * 1024
MAX_ORIGINAL_NAME_LENGTH = 255
MAX_REPORT_TEXT_LENGTH = 10_000
MAX_REPORT_ITEM_TEXT_LENGTH = 1_000

_EXTENSION_TO_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}
_ALLOWED_FLAGS = frozenset({"low", "normal", "high", "unknown"})
_CANDIDATE_ITEM_PATTERN = re.compile(
    r"^(?P<name>[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9 ()/._-]{0,80}?)"
    r"\s*[:：]?\s+(?P<value>[<>≤≥]?\s*-?\d+(?:\.\d+)?)"
    r"\s*(?P<unit>[A-Za-z%/µμ·0-9^]{0,20})?"
    r"(?:\s+(?P<range>[<>≤≥]?\s*-?\d+(?:\.\d+)?"
    r"\s*[-~—至]\s*-?\d+(?:\.\d+)?))?$"
)

OcrEngine = Callable[[bytes], Any]


class FileManagementError(RuntimeError):
    """Base error for local, user-owned file and report operations."""


class FileValidationError(FileManagementError, ValueError):
    """Raised when a file or report draft is invalid."""


class FileNotFoundError(FileManagementError, LookupError):
    """Raised when a file does not exist or belongs to another user."""


class FileInUseError(FileManagementError):
    """Raised when deleting a file would remove a report's original evidence."""


class ReportNotFoundError(FileManagementError, LookupError):
    """Raised when a report does not exist or belongs to another user."""


class ReportStateError(FileManagementError):
    """Raised when a draft-only or confirmation-only operation is not allowed."""


class PdfTextExtractionError(FileManagementError):
    """Raised when embedded PDF text cannot be read safely."""


class FileManagementService:
    """Store user files as SQLite BLOBs so database backups are self-contained.

    Uploaded files are data, never executable application components.  The
    service validates both filename extension and file signature, scopes every
    query to one active member, and verifies size/hash again before returning or
    exporting bytes.  Report extraction only copies text/items already present
    in the source; it never diagnoses a disease or writes facts automatically.
    """

    def __init__(
        self,
        database: Database | None = None,
        *,
        ocr_engine: OcrEngine | None = None,
    ) -> None:
        self.database = database or Database()
        self.database.initialize()
        self._ocr_engine = ocr_engine

    def upload_file(self, user_id: int, source: str | Path) -> int:
        source_path = Path(source).expanduser()
        if source_path.is_symlink() or not source_path.is_file():
            raise FileValidationError("请选择普通的图片或 PDF 文件")
        try:
            size = source_path.stat().st_size
        except OSError as error:
            raise FileValidationError("无法读取所选文件") from error
        if size <= 0:
            raise FileValidationError("不能上传空文件")
        if size > MAX_FILE_SIZE:
            raise FileValidationError("单个文件不能超过 15 MB")
        try:
            content = source_path.read_bytes()
        except OSError as error:
            raise FileValidationError("无法读取所选文件") from error
        if len(content) != size:
            raise FileValidationError("读取文件时内容发生变化，请重新选择")
        return self.upload_bytes(user_id, source_path.name, content)

    def upload_bytes(self, user_id: int, original_name: str, content: bytes) -> int:
        user_id = self._require_member(user_id)
        name = self._validate_original_name(original_name)
        if not isinstance(content, bytes):
            raise FileValidationError("文件内容格式无效")
        size = len(content)
        if size <= 0:
            raise FileValidationError("不能上传空文件")
        if size > MAX_FILE_SIZE:
            raise FileValidationError("单个文件不能超过 15 MB")
        suffix = Path(name).suffix.casefold()
        expected_mime = _EXTENSION_TO_MIME.get(suffix)
        if expected_mime is None:
            raise FileValidationError("只支持 JPG、PNG、WEBP 图片和 PDF 文件")
        detected_mime = _detect_mime(content)
        if detected_mime != expected_mime:
            raise FileValidationError("文件扩展名与实际内容不一致")
        digest = hashlib.sha256(content).hexdigest()
        internal_path = f"embedded/{user_id}/{uuid.uuid4().hex}{suffix}"
        now = timestamp_to_db(utc_now())

        with self.database.transaction() as connection:
            current_total = int(
                connection.execute(
                    "SELECT COALESCE(SUM(size_bytes), 0) FROM user_files WHERE user_id = ?",
                    (user_id,),
                ).fetchone()[0]
            )
            if current_total + size > MAX_USER_STORAGE:
                raise FileValidationError("每个账号的文件总量不能超过 100 MB")
            cursor = connection.execute(
                """
                INSERT INTO user_files (
                    user_id, relative_path, original_name, media_type,
                    size_bytes, sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, internal_path, name, detected_mime, size, digest, now),
            )
            file_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO user_file_contents (file_id, content, mime_type, sha256, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (file_id, sqlite3.Binary(content), detected_mime, digest, now),
            )
            self._audit(connection, user_id, "user_file.uploaded", "user_file", file_id)
        return file_id

    def list_files(self, user_id: int) -> list[dict[str, Any]]:
        user_id = self._require_member(user_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT f.id, f.user_id, f.relative_path, f.original_name,
                       f.media_type, f.size_bytes, f.sha256, f.created_at,
                       EXISTS(
                           SELECT 1 FROM medical_reports r WHERE r.file_id = f.id
                       ) AS attached_to_report
                FROM user_files f
                WHERE f.user_id = ?
                ORDER BY f.created_at DESC, f.id DESC
                """,
                (user_id,),
            ).fetchall()
        return [self._file_metadata(row) for row in rows]

    def get_file(self, user_id: int, file_id: int) -> dict[str, Any]:
        user_id = self._require_member(user_id)
        file_id = self._positive_integer(file_id, "文件编号")
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT f.id, f.user_id, f.relative_path, f.original_name,
                       f.media_type, f.size_bytes, f.sha256, f.created_at,
                       c.content, c.mime_type AS content_mime_type,
                       c.sha256 AS content_sha256,
                       EXISTS(
                           SELECT 1 FROM medical_reports r WHERE r.file_id = f.id
                       ) AS attached_to_report
                FROM user_files f
                JOIN user_file_contents c ON c.file_id = f.id
                WHERE f.id = ? AND f.user_id = ?
                """,
                (file_id, user_id),
            ).fetchone()
        if row is None:
            raise FileNotFoundError("文件不存在或无权访问")
        content = bytes(row["content"])
        stored_digest = str(row["sha256"])
        actual_digest = hashlib.sha256(content).hexdigest()
        if (
            len(content) != int(row["size_bytes"])
            or str(row["content_sha256"]) != stored_digest
            or not hmac.compare_digest(actual_digest, stored_digest)
            or str(row["content_mime_type"]) != str(row["media_type"])
            or _detect_mime(content) != str(row["media_type"])
        ):
            raise FileManagementError("文件完整性检查失败")
        result = self._file_metadata(row)
        result["content"] = content
        return result

    def export_file(
        self,
        user_id: int,
        file_id: int,
        destination: str | Path,
        *,
        overwrite: bool = False,
    ) -> Path:
        stored = self.get_file(user_id, file_id)
        target = Path(destination).expanduser()
        if target.exists() and (target.is_symlink() or not target.is_file()):
            raise FileValidationError("导出目标不是普通文件")
        target = target.parent.resolve() / target.name
        if target.exists() and not overwrite:
            raise FileValidationError("导出目标已存在")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".health-file-export-", suffix=".partial", dir=target.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(stored["content"])
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, target)
        except OSError as error:
            with suppress(OSError):
                os.close(descriptor)
            Path(temporary_name).unlink(missing_ok=True)
            raise FileManagementError("导出文件失败") from error
        return target

    def delete_file(self, user_id: int, file_id: int) -> None:
        user_id = self._require_member(user_id)
        file_id = self._positive_integer(file_id, "文件编号")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT 1 FROM user_files WHERE id = ? AND user_id = ?",
                (file_id, user_id),
            ).fetchone()
            if row is None:
                raise FileNotFoundError("文件不存在或无权访问")
            if connection.execute(
                "SELECT 1 FROM medical_reports WHERE file_id = ? LIMIT 1", (file_id,)
            ).fetchone():
                raise FileInUseError("该文件是健康报告原件，请先删除对应报告")
            connection.execute(
                "DELETE FROM user_files WHERE id = ? AND user_id = ?", (file_id, user_id)
            )
            self._audit(connection, user_id, "user_file.deleted", "user_file", file_id)

    def extract_pdf_text(self, user_id: int, file_id: int, *, max_chars: int = 50_000) -> str:
        if (
            not isinstance(max_chars, int)
            or isinstance(max_chars, bool)
            or not 1 <= max_chars <= 200_000
        ):
            raise FileValidationError("PDF 文本长度上限无效")
        stored = self.get_file(user_id, file_id)
        if stored["media_type"] != "application/pdf":
            raise FileValidationError("所选文件不是 PDF")
        try:
            from pypdf import PdfReader
        except ImportError as error:  # pragma: no cover - packaging dependency
            raise PdfTextExtractionError("当前安装缺少 PDF 文本提取组件") from error
        try:
            reader = PdfReader(io.BytesIO(stored["content"]), strict=False)
            if reader.is_encrypted:
                raise PdfTextExtractionError("暂不支持读取加密 PDF")
            if len(reader.pages) > 200:
                raise PdfTextExtractionError("PDF 页数超过 200 页，已停止提取")
            parts: list[str] = []
            remaining = max_chars
            for page in reader.pages:
                text = page.extract_text() or ""
                if text:
                    parts.append(text[:remaining])
                    remaining -= len(parts[-1])
                if remaining <= 0:
                    break
        except PdfTextExtractionError:
            raise
        except Exception as error:
            raise PdfTextExtractionError("无法读取 PDF 中已有的文字") from error
        return "\n\n".join(parts).strip()[:max_chars]

    def extract_text(
        self,
        user_id: int,
        file_id: int,
        *,
        max_chars: int = 50_000,
    ) -> dict[str, Any]:
        """Return source text and unconfirmed item candidates without persisting them.

        Digital PDFs use ``pypdf``'s text layer.  Images use an injected OCR
        callable when supplied (useful for deterministic tests), otherwise the
        optional RapidOCR runtime.  Candidate rows are deliberately labelled
        ``unknown`` and remain ordinary return values until the user creates and
        confirms a report draft.
        """

        stored = self.get_file(user_id, file_id)
        if stored["media_type"] == "application/pdf":
            text = self.extract_pdf_text(user_id, file_id, max_chars=max_chars)
            extraction_method = "pdf_text_layer"
        elif str(stored["media_type"]).startswith("image/"):
            if (
                not isinstance(max_chars, int)
                or isinstance(max_chars, bool)
                or not 1 <= max_chars <= 200_000
            ):
                raise FileValidationError("OCR 文本长度上限无效")
            output = self._run_ocr(stored["content"])
            text = _ocr_output_to_text(output)[:max_chars]
            extraction_method = "image_ocr"
        else:  # pragma: no cover - upload validation limits stored media types
            raise FileValidationError("该文件类型不支持文字提取")
        return {
            "file_id": int(stored["id"]),
            "original_name": str(stored["original_name"]),
            "media_type": str(stored["media_type"]),
            "extraction_method": extraction_method,
            "text": text,
            "candidate_items": _candidate_items_from_text(text),
            "confirmed": False,
            "notice": "仅为原文和待确认候选项目，不构成诊断，也不会自动写入健康档案。",
        }

    def _run_ocr(self, content: bytes) -> Any:
        if self._ocr_engine is not None:
            try:
                return self._ocr_engine(content)
            except Exception as error:
                raise PdfTextExtractionError("图片文字识别失败") from error
        try:
            from rapidocr import RapidOCR
        except ImportError:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError as error:
                raise PdfTextExtractionError(
                    "当前安装缺少图片文字识别组件；仍可查看或导出原图"
                ) from error
        try:
            engine = RapidOCR()
            return engine(content)
        except Exception as error:
            raise PdfTextExtractionError("图片文字识别失败，请核对原图") from error

    def create_report_draft(
        self,
        user_id: int,
        file_id: int,
        *,
        report_type: str = "other",
        report_date: str | None = None,
        institution: str | None = None,
        summary: str | None = None,
    ) -> int:
        user_id = self._require_member(user_id)
        file_id = self._positive_integer(file_id, "文件编号")
        report_type_value = self._required_text(report_type, "报告类型", 100)
        date_value = self._optional_date(report_date)
        institution_value = self._optional_text(institution, "机构名称", 500)
        summary_value = self._optional_text(summary, "报告摘要", MAX_REPORT_TEXT_LENGTH)
        now = timestamp_to_db(utc_now())
        with self.database.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM user_files WHERE id = ? AND user_id = ?",
                (file_id, user_id),
            ).fetchone() is None:
                raise FileNotFoundError("报告原件不存在或无权访问")
            cursor = connection.execute(
                """
                INSERT INTO medical_reports (
                    user_id, file_id, report_type, report_date, institution,
                    summary, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?)
                """,
                (
                    user_id,
                    file_id,
                    report_type_value,
                    date_value,
                    institution_value,
                    summary_value,
                    now,
                    now,
                ),
            )
            report_id = int(cursor.lastrowid)
            self._audit(
                connection,
                user_id,
                "medical_report.draft_created",
                "medical_report",
                report_id,
            )
        return report_id

    def add_report_item_draft(
        self,
        user_id: int,
        report_id: int,
        item_name: str,
        *,
        result_value: str | None = None,
        unit: str | None = None,
        reference_range: str | None = None,
        flag: str | None = None,
    ) -> int:
        user_id = self._require_member(user_id)
        report_id = self._positive_integer(report_id, "报告编号")
        item_name_value = self._required_text(
            item_name, "项目名称", MAX_REPORT_ITEM_TEXT_LENGTH
        )
        result_value_text = self._optional_text(
            result_value, "结果", MAX_REPORT_ITEM_TEXT_LENGTH
        )
        unit_value = self._optional_text(unit, "单位", 100)
        range_value = self._optional_text(
            reference_range, "参考范围", MAX_REPORT_ITEM_TEXT_LENGTH
        )
        flag_value = None if flag is None else str(flag).strip().casefold()
        if flag_value not in {None, *_ALLOWED_FLAGS}:
            raise FileValidationError("报告项目标记无效")
        now = timestamp_to_db(utc_now())
        with self.database.transaction() as connection:
            self._require_draft_report(connection, user_id, report_id)
            cursor = connection.execute(
                """
                INSERT INTO report_items (
                    report_id, item_name, result_value, unit, reference_range,
                    flag, created_at, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?)
                """,
                (
                    report_id,
                    item_name_value,
                    result_value_text,
                    unit_value,
                    range_value,
                    flag_value,
                    now,
                    now,
                ),
            )
            item_id = int(cursor.lastrowid)
            self._audit(connection, user_id, "report_item.draft_created", "report_item", item_id)
        return item_id

    def confirm_report_item(self, user_id: int, report_id: int, item_id: int) -> None:
        user_id = self._require_member(user_id)
        report_id = self._positive_integer(report_id, "报告编号")
        item_id = self._positive_integer(item_id, "报告项目编号")
        now = timestamp_to_db(utc_now())
        with self.database.transaction() as connection:
            self._require_draft_report(connection, user_id, report_id)
            cursor = connection.execute(
                """
                UPDATE report_items SET status = 'confirmed', updated_at = ?
                WHERE id = ? AND report_id = ? AND status = 'draft'
                """,
                (now, item_id, report_id),
            )
            if cursor.rowcount != 1:
                raise ReportStateError("报告项目不存在、无权访问或已经确认")
            self._audit(connection, user_id, "report_item.confirmed", "report_item", item_id)

    def confirm_report(self, user_id: int, report_id: int) -> None:
        user_id = self._require_member(user_id)
        report_id = self._positive_integer(report_id, "报告编号")
        now = timestamp_to_db(utc_now())
        with self.database.transaction() as connection:
            self._require_draft_report(connection, user_id, report_id)
            connection.execute(
                "UPDATE report_items SET status = 'confirmed', updated_at = ? WHERE report_id = ?",
                (now, report_id),
            )
            connection.execute(
                """
                UPDATE medical_reports SET status = 'confirmed', updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (now, report_id, user_id),
            )
            self._audit(
                connection,
                user_id,
                "medical_report.confirmed",
                "medical_report",
                report_id,
            )

    def archive_report(self, user_id: int, report_id: int) -> None:
        user_id = self._require_member(user_id)
        report_id = self._positive_integer(report_id, "报告编号")
        now = timestamp_to_db(utc_now())
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE medical_reports SET status = 'archived', updated_at = ?
                WHERE id = ? AND user_id = ? AND status = 'confirmed'
                """,
                (now, report_id, user_id),
            )
            if cursor.rowcount != 1:
                raise ReportStateError("只有已确认且属于当前账号的报告可以归档")
            self._audit(connection, user_id, "medical_report.archived", "medical_report", report_id)

    def delete_report(self, user_id: int, report_id: int) -> None:
        user_id = self._require_member(user_id)
        report_id = self._positive_integer(report_id, "报告编号")
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM medical_reports WHERE id = ? AND user_id = ?",
                (report_id, user_id),
            )
            if cursor.rowcount != 1:
                raise ReportNotFoundError("报告不存在或无权访问")
            self._audit(connection, user_id, "medical_report.deleted", "medical_report", report_id)

    def list_reports(self, user_id: int, *, include_archived: bool = False) -> list[dict[str, Any]]:
        user_id = self._require_member(user_id)
        query = (
            "SELECT r.id, r.user_id, r.file_id, r.report_type, r.report_date, "
            "r.institution, r.summary, r.status, r.created_at, r.updated_at, "
            "f.original_name FROM medical_reports r "
            "LEFT JOIN user_files f ON f.id = r.file_id "
            "WHERE r.user_id = ?"
        )
        parameters: list[object] = [user_id]
        if not include_archived:
            query += " AND r.status != 'archived'"
        query += " ORDER BY COALESCE(r.report_date, r.created_at) DESC, r.id DESC"
        with self.database.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._report_from_row(row, items=None) for row in rows]

    def get_report(self, user_id: int, report_id: int) -> dict[str, Any]:
        user_id = self._require_member(user_id)
        report_id = self._positive_integer(report_id, "报告编号")
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT r.id, r.user_id, r.file_id, r.report_type, r.report_date,
                       r.institution, r.summary, r.status, r.created_at, r.updated_at,
                       f.original_name
                FROM medical_reports r
                LEFT JOIN user_files f ON f.id = r.file_id
                WHERE r.id = ? AND r.user_id = ?
                """,
                (report_id, user_id),
            ).fetchone()
            if row is None:
                raise ReportNotFoundError("报告不存在或无权访问")
            item_rows = connection.execute(
                """
                SELECT id, report_id, item_name, result_value, unit,
                       reference_range, flag, status, created_at, updated_at
                FROM report_items WHERE report_id = ? ORDER BY id
                """,
                (report_id,),
            ).fetchall()
        items = [
            {
                "id": int(item["id"]),
                "report_id": int(item["report_id"]),
                "item_name": str(item["item_name"]),
                "result_value": _optional_db_text(item["result_value"]),
                "unit": _optional_db_text(item["unit"]),
                "reference_range": _optional_db_text(item["reference_range"]),
                "flag": _optional_db_text(item["flag"]),
                "status": str(item["status"]),
                "created_at": timestamp_from_db(item["created_at"]),
                "updated_at": timestamp_from_db(item["updated_at"]),
            }
            for item in item_rows
        ]
        return self._report_from_row(row, items=items)

    def storage_usage(self, user_id: int) -> dict[str, int]:
        user_id = self._require_member(user_id)
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS file_count, COALESCE(SUM(size_bytes), 0) AS used_bytes
                FROM user_files WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        return {
            "file_count": int(row["file_count"]),
            "used_bytes": int(row["used_bytes"]),
            "quota_bytes": MAX_USER_STORAGE,
            "remaining_bytes": MAX_USER_STORAGE - int(row["used_bytes"]),
        }

    def _require_member(self, user_id: int) -> int:
        value = self._positive_integer(user_id, "用户编号")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT role_code, account_status FROM users WHERE id = ?", (value,)
            ).fetchone()
        if row is None:
            raise FileValidationError("用户不存在")
        if str(row["role_code"]) != "member":
            raise FileValidationError("文件与健康报告功能仅供会员账号使用")
        if str(row["account_status"]) != "active":
            raise FileValidationError("账号已停用，不能访问文件")
        return value

    @staticmethod
    def _require_draft_report(
        connection: sqlite3.Connection, user_id: int, report_id: int
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT status FROM medical_reports WHERE id = ? AND user_id = ?",
            (report_id, user_id),
        ).fetchone()
        if row is None:
            raise ReportNotFoundError("报告不存在或无权访问")
        if str(row["status"]) != "draft":
            raise ReportStateError("该报告已经确认或归档，不能修改提取项目")
        return row

    @staticmethod
    def _positive_integer(value: Any, label: str) -> int:
        if isinstance(value, bool):
            raise FileValidationError(f"{label}无效")
        try:
            result = int(value)
        except (TypeError, ValueError) as error:
            raise FileValidationError(f"{label}无效") from error
        if result <= 0:
            raise FileValidationError(f"{label}无效")
        return result

    @staticmethod
    def _validate_original_name(value: str) -> str:
        if not isinstance(value, str):
            raise FileValidationError("文件名无效")
        name = value.strip()
        if (
            not name
            or name in {".", ".."}
            or Path(name).name != name
            or len(name) > MAX_ORIGINAL_NAME_LENGTH
            or any(ord(character) < 32 for character in name)
        ):
            raise FileValidationError("文件名无效")
        return name

    @staticmethod
    def _required_text(value: Any, label: str, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise FileValidationError(f"{label}不能为空")
        result = value.strip()
        if len(result) > maximum:
            raise FileValidationError(f"{label}过长")
        return result

    @classmethod
    def _optional_text(cls, value: Any, label: str, maximum: int) -> str | None:
        if value is None or value == "":
            return None
        return cls._required_text(value, label, maximum)

    @staticmethod
    def _optional_date(value: Any) -> str | None:
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise FileValidationError("报告日期格式无效")
        try:
            return date.fromisoformat(value.strip()).isoformat()
        except ValueError as error:
            raise FileValidationError("报告日期必须使用 YYYY-MM-DD 格式") from error

    @staticmethod
    def _file_metadata(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "user_id": int(row["user_id"]),
            "relative_path": str(row["relative_path"]),
            "original_name": str(row["original_name"]),
            "media_type": str(row["media_type"]),
            "size_bytes": int(row["size_bytes"]),
            "sha256": str(row["sha256"]),
            "created_at": timestamp_from_db(row["created_at"]),
            "attached_to_report": bool(row["attached_to_report"]),
        }

    @staticmethod
    def _report_from_row(row: sqlite3.Row, *, items: list[dict[str, Any]] | None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": int(row["id"]),
            "user_id": int(row["user_id"]),
            "file_id": None if row["file_id"] is None else int(row["file_id"]),
            "original_name": _optional_db_text(row["original_name"]),
            "report_type": str(row["report_type"]),
            "report_date": _optional_db_text(row["report_date"]),
            "institution": _optional_db_text(row["institution"]),
            "summary": _optional_db_text(row["summary"]),
            "status": str(row["status"]),
            "created_at": timestamp_from_db(row["created_at"]),
            "updated_at": timestamp_from_db(row["updated_at"]),
        }
        if items is not None:
            result["items"] = items
        return result

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        actor_user_id: int,
        action: str,
        entity_type: str,
        entity_id: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_logs (
                actor_user_id, action, entity_type, entity_id, details_json, created_at
            ) VALUES (?, ?, ?, ?, NULL, ?)
            """,
            (actor_user_id, action, entity_type, entity_id, timestamp_to_db(utc_now())),
        )


def _detect_mime(content: bytes) -> str | None:
    if content.startswith(b"\xFF\xD8\xFF"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if content.startswith(b"%PDF-"):
        return "application/pdf"
    return None


def _ocr_output_to_text(output: Any) -> str:
    """Normalize the public result shapes used by current and legacy RapidOCR."""

    if output is None:
        return ""
    if isinstance(output, str):
        return output.strip()
    text_values = getattr(output, "txts", None)
    if text_values is not None:
        return "\n".join(str(value).strip() for value in text_values if str(value).strip())
    direct_text = getattr(output, "text", None)
    if isinstance(direct_text, str):
        return direct_text.strip()
    rows = output[0] if isinstance(output, tuple) and output else output
    if not isinstance(rows, (list, tuple)):
        raise PdfTextExtractionError("图片文字识别组件返回了不支持的结果格式")
    lines: list[str] = []
    for row in rows:
        if isinstance(row, str):
            candidate = row.strip()
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            candidate = str(row[1]).strip()
        else:
            continue
        if candidate:
            lines.append(candidate)
    return "\n".join(lines)


def _candidate_items_from_text(text: str) -> list[dict[str, str | None]]:
    candidates: list[dict[str, str | None]] = []
    for raw_line in text.splitlines():
        line = " ".join(raw_line.strip().split())
        if not line or len(line) > 300:
            continue
        match = _CANDIDATE_ITEM_PATTERN.fullmatch(line)
        if match is None:
            continue
        candidates.append(
            {
                "item_name": match.group("name").strip(),
                "result_value": match.group("value").replace(" ", ""),
                "unit": (match.group("unit") or "").strip() or None,
                "reference_range": (match.group("range") or "").strip() or None,
                "flag": "unknown",
                "source_line": line,
            }
        )
        if len(candidates) >= 200:
            break
    return candidates


def _optional_db_text(value: Any) -> str | None:
    return None if value is None else str(value)


__all__ = [
    "FileInUseError",
    "FileManagementError",
    "FileManagementService",
    "FileNotFoundError",
    "FileValidationError",
    "MAX_FILE_SIZE",
    "MAX_USER_STORAGE",
    "PdfTextExtractionError",
    "ReportNotFoundError",
    "ReportStateError",
]
