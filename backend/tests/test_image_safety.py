import base64

import pytest
from fastapi import HTTPException

from app import schemas
from app.image_safety import MAX_IMAGE_BASE64_CHARS, validate_image_bytes
from app.routers.device_types import _decode_editor_image


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 12


def test_png_renamed_as_jpeg_is_rejected():
    with pytest.raises(ValueError, match="PNG"):
        validate_image_bytes(PNG, "jpg")


@pytest.mark.parametrize("content", [b"<svg></svg>", b"\x89PNG\r\n\x1a\n"])
def test_non_image_and_truncated_image_are_rejected(content):
    with pytest.raises(ValueError, match="valid PNG"):
        validate_image_bytes(content, "png")


def test_editor_rejects_oversize_base64_before_decoding():
    payload = schemas.ImageFileRequest(side="front", filename="image.png", content_type="image/png",
                                       content_base64="A" * (MAX_IMAGE_BASE64_CHARS + 1))
    with pytest.raises(HTTPException) as exc:
        _decode_editor_image(payload)
    assert exc.value.status_code == 422


def test_editor_derives_extension_from_detected_content():
    payload = schemas.ImageFileRequest(side="front", filename="image.png", content_type="image/png",
                                       content_base64=base64.b64encode(PNG).decode())
    assert _decode_editor_image(payload) == (PNG, "png")
