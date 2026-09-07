from __future__ import annotations

import hashlib
import io
import sqlite3
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_EXCHANGE_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENTS = 4
MAX_USER_ATTACHMENT_BYTES = 100 * 1024 * 1024
MAX_EXTRACTED_CHARS = 50_000
MAX_EXPANDED_BYTES = 20 * 1024 * 1024
IMAGE_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
DOCUMENT_TYPES = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


class AttachmentValidationError(ValueError):
    """An attachment could not be read within the documented local limits."""


@dataclass(frozen=True, slots=True)
class ChatAttachment:
    original_name: str
    media_type: str
    content: bytes
    extracted_text: str
    extraction_method: str
    sha256: str

    @property
    def is_image(self) -> bool:
        return self.media_type.startswith("image/")


def prepare_attachment(source: str | Path) -> ChatAttachment:
    """Read and parse locally. Call from a worker; this never uploads anything."""

    path = Path(source).expanduser()
    if path.is_symlink() or not path.is_file():
        raise AttachmentValidationError("请选择普通文件")
    if path.suffix.casefold() not in IMAGE_TYPES | DOCUMENT_TYPES:
        raise AttachmentValidationError("只支持 JPG、PNG、WEBP、PDF、TXT、Markdown、CSV、DOCX")
    size = path.stat().st_size
    if not 0 < size <= MAX_ATTACHMENT_BYTES:
        raise AttachmentValidationError("单个附件必须非空且不能超过 10 MB")
    with path.open("rb") as handle:
        payload = handle.read(MAX_ATTACHMENT_BYTES + 1)
    if len(payload) != size:
        raise AttachmentValidationError("读取时文件发生变化或超过 10 MB，请重新选择")
    return prepare_attachment_bytes(path.name, payload)


def prepare_attachment_bytes(name: str, payload: bytes) -> ChatAttachment:
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name) > 255
        or any(character in name for character in ("/", "\\", ":"))
        or any(ord(character) < 32 for character in name)
    ):
        raise AttachmentValidationError("附件文件名无效")
    if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_ATTACHMENT_BYTES:
        raise AttachmentValidationError("单个附件必须非空且不能超过 10 MB")
    suffix = Path(name).suffix.casefold()
    media_type = (IMAGE_TYPES | DOCUMENT_TYPES).get(suffix)
    if media_type is None:
        raise AttachmentValidationError("只支持 JPG、PNG、WEBP、PDF、TXT、Markdown、CSV、DOCX")
    if suffix in IMAGE_TYPES:
        _validate_image(payload, media_type)
        text, method = "", "native_image"
    elif suffix == ".pdf":
        text, method = _pdf_text(payload), "pdf_text_layer"
    elif suffix == ".docx":
        text, method = _docx_text(payload), "docx_text"
    else:
        text, method = _decode_text(payload), "local_text"
    if suffix not in IMAGE_TYPES and not text.strip():
        raise AttachmentValidationError(
            "文件没有可读取的文字。扫描 PDF 请改为附加原始图片，使用视觉模型理解。"
        )
    if len(text) > MAX_EXTRACTED_CHARS:
        raise AttachmentValidationError("附件文字超过 50000 字符，请拆分文件后再附加")
    return ChatAttachment(
        name, media_type, payload, text, method, hashlib.sha256(payload).hexdigest()
    )


def validate_attachments(attachments: tuple[ChatAttachment, ...]) -> None:
    if len(attachments) > MAX_ATTACHMENTS:
        raise AttachmentValidationError("一条消息最多附加 4 个文件或图片")
    if any(not isinstance(item, ChatAttachment) for item in attachments):
        raise AttachmentValidationError("附件格式无效")
    for item in attachments:
        name = item.original_name
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 255
            or any(character in name for character in ("/", "\\", ":"))
            or any(ord(character) < 32 for character in name)
        ):
            raise AttachmentValidationError("附件文件名无效")
        suffix = Path(name).suffix.casefold()
        expected_method = (
            "native_image"
            if suffix in IMAGE_TYPES
            else "pdf_text_layer"
            if suffix == ".pdf"
            else "docx_text"
            if suffix == ".docx"
            else "local_text"
        )
        if (
            not isinstance(item.content, bytes)
            or not 0 < len(item.content) <= MAX_ATTACHMENT_BYTES
            or not isinstance(item.extracted_text, str)
            or len(item.extracted_text) > MAX_EXTRACTED_CHARS
            or hashlib.sha256(item.content).hexdigest() != item.sha256
            or item.media_type != (IMAGE_TYPES | DOCUMENT_TYPES).get(suffix)
            or item.extraction_method != expected_method
            or (item.is_image and item.extracted_text != "")
            or (not item.is_image and not item.extracted_text.strip())
        ):
            raise AttachmentValidationError("附件完整性检查失败，请重新选择")
    if sum(len(item.content) for item in attachments) > MAX_EXCHANGE_BYTES:
        raise AttachmentValidationError("一条消息的附件合计不能超过 20 MB")


def attachment_from_row(row: sqlite3.Row) -> ChatAttachment:
    attachment = ChatAttachment(
        str(row["original_name"]),
        str(row["media_type"]),
        bytes(row["content"]),
        str(row["extracted_text"]),
        str(row["extraction_method"]),
        str(row["sha256"]),
    )
    validate_attachments((attachment,))
    return attachment


def _validate_image(payload: bytes, expected_type: str) -> None:
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QImageReader

    buffer = QBuffer()
    buffer.setData(QByteArray(payload))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    reader.setDecideFormatFromContent(True)
    detected = bytes(reader.format()).decode("ascii", errors="ignore").lower()
    actual_type = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
    }.get(detected)
    size = reader.size()
    if actual_type != expected_type or not size.isValid():
        raise AttachmentValidationError("图片扩展名与内容不一致，或图片已损坏")
    if size.width() * size.height() > 20_000_000:
        raise AttachmentValidationError("图片不能超过 2000 万像素，请缩小后重试")
    if reader.read().isNull():
        raise AttachmentValidationError("图片无法完整解码，请重新导出图片")


def _decode_text(payload: bytes) -> str:
    encodings = (
        ("utf-16",) if payload.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "gb18030")
    )
    for encoding in encodings:
        try:
            value = payload.decode(encoding)
        except UnicodeError:
            continue
        if any(ord(character) < 32 and character not in "\n\r\t" for character in value):
            raise AttachmentValidationError("文件包含二进制内容，请使用 UTF-8 文本文件")
        return value.strip()
    raise AttachmentValidationError("文字编码无法识别，请另存为 UTF-8 文本文件")


def _pdf_text(payload: bytes) -> str:
    from pypdf import PdfReader

    if not payload.startswith(b"%PDF-"):
        raise AttachmentValidationError("文件扩展名与 PDF 内容不一致")
    try:
        reader = PdfReader(io.BytesIO(payload), strict=True)
        if reader.is_encrypted:
            raise AttachmentValidationError("暂不支持加密 PDF，请先解密后附加")
        if len(reader.pages) > 100:
            raise AttachmentValidationError("PDF 不能超过 100 页，请拆分后附加")
        parts: list[str] = []
        total = 0
        expanded_total = 0
        visited_streams: set[int] = set()

        def check_form_streams(resources, depth: int = 0) -> None:
            nonlocal expanded_total
            if depth > 20:
                raise AttachmentValidationError("PDF 嵌套层级过深，请简化后附加")
            if not resources:
                return
            resources = resources.get_object()
            objects = resources.get("/XObject")
            if not objects:
                return
            for reference in objects.get_object().values():
                stream = reference.get_object()
                if stream.get("/Subtype") != "/Form" or id(stream) in visited_streams:
                    continue
                visited_streams.add(id(stream))
                expanded_total += len(stream.get_data())
                if expanded_total > MAX_EXPANDED_BYTES:
                    raise AttachmentValidationError("PDF 全文展开后超过 20 MB，请拆分后附加")
                check_form_streams(stream.get("/Resources"), depth + 1)

        for page in reader.pages:
            contents = page.get_contents()
            if contents is not None:
                expanded_total += len(contents.get_data())
            if expanded_total > MAX_EXPANDED_BYTES:
                raise AttachmentValidationError("PDF 全文展开后超过 20 MB，请拆分后附加")
            check_form_streams(page.get("/Resources"))
            value = page.extract_text() or ""
            total += len(value) + 2
            if total > MAX_EXTRACTED_CHARS:
                raise AttachmentValidationError("PDF 文字超过 50000 字符，请拆分后附加")
            parts.append(value)
        return "\n\n".join(parts).strip()
    except AttachmentValidationError:
        raise
    except Exception as error:
        raise AttachmentValidationError("PDF 解析失败，请检查文件是否损坏") from error


def _docx_text(payload: bytes) -> str:
    class NoDtdTreeBuilder(ElementTree.TreeBuilder):
        def doctype(self, name, pubid, system) -> None:
            raise AttachmentValidationError("DOCX 包含不支持的 XML 声明")

    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = archive.infolist()
            if len(members) > 2000 or sum(item.file_size for item in members) > MAX_EXPANDED_BYTES:
                raise AttachmentValidationError("DOCX 展开后超过 20 MB，请简化后附加")
            if any(item.flag_bits & 1 for item in members):
                raise AttachmentValidationError("暂不支持加密 DOCX")
            if len({item.filename for item in members}) != len(members):
                raise AttachmentValidationError("DOCX 包含重复文件，无法安全读取")
            if "[Content_Types].xml" not in archive.namelist():
                raise AttachmentValidationError("所选文件不是有效的 DOCX")
            xml = archive.read("word/document.xml")
            if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
                raise AttachmentValidationError("DOCX 包含不支持的 XML 声明")
            # The parser callback rejects DTDs even in UTF-16/32 XML, where a
            # raw-byte search for an ASCII <!DOCTYPE token would miss them.
            document = ElementTree.fromstring(
                xml, parser=ElementTree.XMLParser(target=NoDtdTreeBuilder())
            )
            namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            paragraphs = []
            for paragraph in document.iter(namespace + "p"):
                paragraphs.append(
                    "".join(
                        (node.text or "")
                        if node.tag == namespace + "t"
                        else "\t"
                        if node.tag == namespace + "tab"
                        else "\n"
                        if node.tag == namespace + "br"
                        else ""
                        for node in paragraph.iter()
                    )
                )
            return "\n".join(paragraphs).strip()
    except AttachmentValidationError:
        raise
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, RuntimeError) as error:
        raise AttachmentValidationError("DOCX 解析失败，请检查文件是否损坏") from error
