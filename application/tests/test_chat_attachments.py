from __future__ import annotations

import io
import zipfile
from dataclasses import replace

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage

from ollama_chat_app.services.chat_attachments import (
    MAX_ATTACHMENT_BYTES,
    AttachmentValidationError,
    prepare_attachment,
    prepare_attachment_bytes,
    validate_attachments,
)


def png_bytes() -> bytes:
    picture = QImage(8, 8, QImage.Format.Format_RGB32)
    picture.fill(0xABCDEF)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert picture.save(buffer, "PNG")
    return bytes(buffer.data())


def docx_bytes(text: str, *, extra: bytes = b"") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "word/document.xml",
            extra
            + (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
            ).encode(),
        )
    return output.getvalue()


@pytest.mark.parametrize("name", ["notes.txt", "notes.md", "notes.markdown", "notes.csv"])
def test_text_formats_preserve_original_bytes_and_extract_locally(name: str) -> None:
    raw = "资料标题\n字段,数值\n饮水,1000\n".encode()
    attachment = prepare_attachment_bytes(name, raw)
    assert attachment.content == raw
    assert attachment.extracted_text == raw.decode().strip()
    assert attachment.extraction_method == "local_text"
    assert not attachment.is_image


@pytest.mark.parametrize("encoding", ["utf-16", "utf-8-sig", "gb18030"])
def test_common_local_text_encodings(encoding: str) -> None:
    attachment = prepare_attachment_bytes("文字.txt", "合成资料".encode(encoding))
    assert attachment.extracted_text == "合成资料"


def test_docx_extracts_text_and_pdf_extracts_only_embedded_text() -> None:
    attachment = prepare_attachment_bytes("报告.docx", docx_bytes("人工合成的文档文字"))
    assert attachment.extracted_text == "人工合成的文档文字"
    assert attachment.extraction_method == "docx_text"
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 80 Td (Synthetic PDF text) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    pdf = prepare_attachment_bytes("报告.pdf", output.getvalue())
    assert "Synthetic PDF text" in pdf.extracted_text
    assert pdf.extraction_method == "pdf_text_layer"


def test_native_image_keeps_bytes_without_ocr() -> None:
    raw = png_bytes()
    attachment = prepare_attachment_bytes("示意图.png", raw)
    assert attachment.content == raw
    assert attachment.is_image
    assert attachment.extraction_method == "native_image"
    assert attachment.extracted_text == ""
    with pytest.raises(AttachmentValidationError, match="不一致"):
        prepare_attachment_bytes("伪造.jpg", raw)


@pytest.mark.parametrize(
    "name,content",
    [
        ("program.exe", b"executable"),
        ("archive.zip", b"PK"),
        ("empty.txt", b""),
        ("binary.txt", b"\x00\x01\x02"),
        ("../private.txt", b"text"),
        ("broken.docx", b"PKfake"),
        ("broken.pdf", b"%PDF-invalid"),
        ("oversized.txt", b"x" * (MAX_ATTACHMENT_BYTES + 1)),
        ("long.txt", b"x" * 50001),
    ],
    ids=["exe", "zip", "empty", "binary", "path", "broken-docx", "broken-pdf", "size", "text"],
)
def test_invalid_unsupported_binary_empty_and_oversized_attachments(name, content) -> None:
    with pytest.raises(AttachmentValidationError):
        prepare_attachment_bytes(name, content)


def test_scanned_empty_encrypted_and_over_page_limit_pdf_are_explicitly_rejected() -> None:
    for pages, password, expected in [
        (1, None, "没有可读取"),
        (1, "secret", "加密"),
        (101, None, "100 页"),
    ]:
        writer = PdfWriter()
        for _ in range(pages):
            writer.add_blank_page(200, 200)
        if password:
            writer.encrypt(password)
        output = io.BytesIO()
        writer.write(output)
        with pytest.raises(AttachmentValidationError, match=expected):
            prepare_attachment_bytes("scan.pdf", output.getvalue())


def test_docx_entities_and_zip_expansion_are_rejected() -> None:
    with pytest.raises(AttachmentValidationError, match="XML"):
        prepare_attachment_bytes("entity.docx", docx_bytes("text", extra=b"<!DOCTYPE x>"))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("oversized.bin", b"x" * (21 * 1024 * 1024))
    with pytest.raises(AttachmentValidationError, match="20 MB"):
        prepare_attachment_bytes("expanded.docx", output.getvalue())


def test_batch_limits_hash_and_local_path_validation(tmp_path) -> None:
    attachment = prepare_attachment_bytes("valid.txt", b"valid")
    with pytest.raises(AttachmentValidationError, match="4"):
        validate_attachments((attachment,) * 5)
    with pytest.raises(AttachmentValidationError, match="完整性"):
        validate_attachments((replace(attachment, content=b"tampered"),))
    source = tmp_path / "valid.txt"
    source.write_bytes(b"valid")
    assert prepare_attachment(source) == attachment
    with pytest.raises(AttachmentValidationError, match="普通文件"):
        prepare_attachment(tmp_path)
