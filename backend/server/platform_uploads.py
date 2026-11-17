"""Server upload checks; never import Qt, execute files, or perform OCR here."""

from __future__ import annotations

import io
import warnings
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from ollama_chat_app.services.chat_attachments import (
    IMAGE_TYPES,
    AttachmentValidationError,
    validate_attachments,
)
from ollama_chat_app.services.file_management import (
    MAX_FILE_SIZE,
    FileManagementService,
    FileValidationError,
)

MAX_IMAGE_PIXELS = 20_000_000
_MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


def validate_image(content: bytes, expected_mime: str) -> None:
    """Check format and pixel budget before decoding a single-frame image."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as picture:
                if (
                    _MIME.get(picture.format) != expected_mime
                    or picture.width <= 0
                    or picture.height <= 0
                    or picture.width * picture.height > MAX_IMAGE_PIXELS
                    or getattr(picture, "n_frames", 1) != 1
                ):
                    raise ValueError("Invalid image shape")
                picture.verify()
            with Image.open(io.BytesIO(content)) as picture:
                picture.load()
    except (
        ValueError,
        OSError,
        SyntaxError,
        UnidentifiedImageError,
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
    ):
        raise AttachmentValidationError(
            "图片损坏、格式不匹配、含动画，或超过 2000 万像素；请重新导出静态图片"
        ) from None


def validate_chat_uploads(attachments) -> None:
    validate_attachments(attachments)
    for item in attachments:
        if item.is_image:
            validate_image(item.content, item.media_type)
        elif item.media_type == "application/pdf" and not item.content.startswith(b"%PDF-"):
            raise AttachmentValidationError("PDF 附件格式不正确")
    # Text extraction remains untrusted client-supplied context. This server does
    # not parse office/PDF archives or treat extracted text as an instruction.


class PlatformFileService(FileManagementService):
    def upload_bytes(self, user_id: int, original_name: str, content: bytes) -> int:
        name = self._validate_original_name(original_name)
        if not isinstance(content, bytes) or not 0 < len(content) <= MAX_FILE_SIZE:
            raise FileValidationError("单个文件必须非空且不超过 15 MB")
        expected = IMAGE_TYPES.get(Path(name).suffix.casefold())
        if expected:
            validate_image(content, expected)
        return super().upload_bytes(user_id, name, content)
