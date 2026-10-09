import struct

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BASE64_CHARS = ((MAX_IMAGE_BYTES + 2) // 3) * 4


def validate_image_bytes(content: bytes, claimed_extension: str | None = None) -> str:
    detected = None
    if len(content) >= 24 and content.startswith(b"\x89PNG\r\n\x1a\n") and content[12:16] == b"IHDR":
        detected = "png"
    elif len(content) >= 4 and content.startswith(b"\xff\xd8") and content.endswith(b"\xff\xd9"):
        detected = "jpg"
    elif (len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP"
          and struct.unpack("<I", content[4:8])[0] + 8 <= len(content)):
        detected = "webp"
    if detected is None:
        raise ValueError("Image content is not a valid PNG, JPEG, or WebP file.")
    claimed = (claimed_extension or "").lower().lstrip(".")
    if claimed == "jpeg":
        claimed = "jpg"
    if claimed and claimed != detected:
        raise ValueError(f"Image content is {detected.upper()}, not {claimed_extension}.")
    if len(content) > MAX_IMAGE_BYTES:
        raise ValueError("Image must be no larger than 10 MiB.")
    return detected
