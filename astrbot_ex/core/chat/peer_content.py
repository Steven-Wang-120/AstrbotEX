"""Validated rich bodies inside signed, end-to-end encrypted peer envelopes."""
from __future__ import annotations

import base64
import hashlib
from io import BytesIO
import re
import warnings

from PIL import Image

from .peer_relay import MAX_ATTACHMENT_BYTES, MAX_PLAIN_BYTES, MAX_PACKET_BYTES
IMAGE_MIMES = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif", "WEBP": "image/webp"}


def attachment(kind, name, mime, content):
    value = {"kind": kind, "name": name, "mime": mime, "size": len(content),
             "sha256": hashlib.sha256(content).hexdigest(), "data": base64.b64encode(content).decode("ascii")}
    validate_attachment(value)
    return value


def validate_attachment(value):
    fields = {"kind", "name", "mime", "size", "sha256", "data"}
    if not isinstance(value, dict) or set(value) != fields or not isinstance(value["kind"], str) or value["kind"] not in {"image", "file", "sticker"}:
        raise ValueError("Invalid attachment")
    name, mime, size = value["name"], value["mime"], value["size"]
    if (not isinstance(name, str) or not 1 <= len(name) <= 180 or name in {".", ".."}
            or any(ord(c) < 32 or c in "/\\" for c in name)
            or not isinstance(mime, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,63}", mime)
            or type(size) is not int or not 1 <= size <= MAX_ATTACHMENT_BYTES
            or not isinstance(value["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"])
            or not isinstance(value["data"], str) or len(value["data"]) > 4 * ((MAX_ATTACHMENT_BYTES + 2) // 3)):
        raise ValueError("Invalid attachment metadata or size")
    try:
        content = base64.b64decode(value["data"], validate=True)
    except (ValueError, TypeError):
        raise ValueError("Invalid attachment encoding") from None
    if len(content) != size or hashlib.sha256(content).hexdigest() != value["sha256"]:
        raise ValueError("Attachment digest mismatch")
    if value["kind"] != "file":
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(content)) as image:
                    if IMAGE_MIMES.get(image.format) != mime:
                        raise ValueError("Unsupported image format")
                    w, h = image.size
                    frames = getattr(image, "n_frames", 1)
                    if min(w, h) < 1 or max(w, h) > 8192 or w * h > 16_000_000 or frames > 240 or w * h * frames > 64_000_000:
                        raise ValueError("Image dimensions or animation too large")
                    image.verify()
        except (OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise ValueError("Invalid image") from None
    return content


def validate_content(value):
    if not isinstance(value, dict) or set(value) not in ({"text"}, {"text", "attachments"}):
        raise ValueError("Invalid message body")
    text = value["text"]
    if not isinstance(text, str) or len(text) > 4000:
        raise ValueError("Invalid text")
    items = value.get("attachments", [])
    if not isinstance(items, list) or len(items) > 4 or (not text.strip() and not items):
        raise ValueError("Provide text or attachments")
    total = 0
    for item in items:
        total += len(validate_attachment(item))
    if total > MAX_ATTACHMENT_BYTES:
        raise ValueError("Attachments must total at most 8 MiB")
    return value
