import hashlib
import io

import pytest
from PIL import Image

from ollama_chat_app.services.chat_attachments import AttachmentValidationError, ChatAttachment
from server.platform_uploads import validate_chat_uploads, validate_image


def picture(fmt="PNG"):
    result = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(result, format=fmt)
    return result.getvalue()


@pytest.mark.parametrize(
    "fmt,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")]
)
def test_valid_image(fmt, mime):
    validate_image(picture(fmt), mime)


@pytest.mark.parametrize(
    "content,mime",
    [
        (b"secret malicious payload", "image/png"),
        (picture(), "image/jpeg"),
        (picture()[:30], "image/png"),
    ],
)
def test_invalid_image_is_safe(content, mime):
    with pytest.raises(AttachmentValidationError) as error:
        validate_image(content, mime)
    assert "secret" not in str(error.value)


def test_pixel_limit(monkeypatch):
    monkeypatch.setattr("server.platform_uploads.MAX_IMAGE_PIXELS", 3)
    with pytest.raises(AttachmentValidationError):
        validate_image(picture(), "image/png")


def test_chat_validates_actual_payload_not_only_hash():
    content = b"not really an image"
    item = ChatAttachment(
        "x.png", "image/png", content, "", "native_image", hashlib.sha256(content).hexdigest()
    )
    with pytest.raises(AttachmentValidationError):
        validate_chat_uploads((item,))


def test_valid_chat_image():
    content = picture()
    item = ChatAttachment(
        "x.png", "image/png", content, "", "native_image", hashlib.sha256(content).hexdigest()
    )
    validate_chat_uploads((item,))
