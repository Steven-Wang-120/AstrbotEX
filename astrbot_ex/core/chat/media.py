"""Bounded image decoding and conversation-scoped native attachments."""
from __future__ import annotations

import hashlib
from io import BytesIO
import warnings
from uuid import UUID, uuid4

from PIL import Image, ImageOps, UnidentifiedImageError

from .models import ChatError

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_ACCOUNT_BYTES = 64 * 1024 * 1024


def media_uuid(value: str) -> str:
    try:
        parsed = str(UUID(value))
        if parsed != value:
            raise ValueError()
        return parsed
    except (ValueError, TypeError, AttributeError):
        raise ChatError("invalid_media_id", "Media IDs must be canonical UUID strings") from None


def normalize_image(content: bytes) -> tuple[bytes, str, int, int]:
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise ChatError("media_too_large", "Images must contain at most 8 MiB", 413)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content), formats=("PNG", "JPEG", "WEBP")) as image:
                width, height = image.size
                if min(width, height) < 1 or max(width, height) > 8192 or width * height > MAX_IMAGE_PIXELS:
                    raise ChatError("image_dimensions", "Images must fit within 8192 pixels per side and 16 megapixels", 413)
                if getattr(image, "is_animated", False):
                    raise ChatError("unsupported_image", "Animated images are not supported")
                image.verify()
            with Image.open(BytesIO(content), formats=("PNG", "JPEG", "WEBP")) as image:
                image.load()
                oriented = ImageOps.exif_transpose(image)
                has_alpha = "A" in oriented.getbands() or "transparency" in oriented.info
                mode = "RGBA" if has_alpha else "RGB"
                # A new pixel-only image discards EXIF, location, profiles and text.
                clean = Image.new(mode, oriented.size)
                clean.paste(oriented.convert(mode))
                output = BytesIO()
                if has_alpha or image.format == "PNG":
                    clean.save(output, format="PNG")
                    mime = "image/png"
                else:
                    clean.save(output, format="JPEG", quality=90)
                    mime = "image/jpeg"
                normalized = output.getvalue()
                if len(normalized) > MAX_IMAGE_BYTES:
                    raise ChatError("media_too_large", "Normalized image exceeds 8 MiB", 413)
                return normalized, mime, *clean.size
    except ChatError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ChatError("unsupported_image", "Provide a valid static PNG, JPEG or WebP image") from None


class MediaStore:
    def __init__(self, service):
        self.service = service
        self.repository = service.repository

    @staticmethod
    def metadata(row) -> dict:
        return {"media_id": row["id"], "conversation_id": row["conversation_id"], "content_type": row["content_type"],
                "size_bytes": row["size_bytes"], "width": row["width"], "height": row["height"], "sha256": row["sha256"]}

    @staticmethod
    def _existing(db, user_id: int, cid: str, client_id: str, digest: str):
        row = db.execute("SELECT * FROM media WHERE owner_id=? AND client_media_id=?", (user_id, client_id)).fetchone()
        if row is not None and (row["conversation_id"] != cid or row["source_sha256"] != digest):
            raise ChatError("idempotency_conflict", "Upload ID is already used for different content", 409)
        return row

    def upload(self, user_id: int, cid: str, client_id: str, content: bytes) -> dict:
        client_id = media_uuid(client_id)
        if not content or len(content) > MAX_IMAGE_BYTES:
            raise ChatError("media_too_large", "Images must contain at most 8 MiB", 413)
        digest = hashlib.sha256(content).hexdigest()
        with self.repository.transaction() as db:
            self.service._member(db, user_id, cid)
            existing = self._existing(db, user_id, cid, client_id, digest)
            if existing is not None:
                return self.metadata(existing)
        normalized, mime, width, height = normalize_image(content)
        with self.repository.transaction() as db:
            self.service._member(db, user_id, cid)
            existing = self._existing(db, user_id, cid, client_id, digest)
            if existing is not None:
                return self.metadata(existing)
            used = db.execute("SELECT COALESCE(SUM(size_bytes),0) FROM media WHERE owner_id=?", (user_id,)).fetchone()[0]
            if used + len(normalized) > MAX_ACCOUNT_BYTES:
                raise ChatError("media_quota", "Account image storage limit reached", 413)
            mid = str(uuid4())
            db.execute("INSERT INTO media(id,owner_id,conversation_id,client_media_id,source_sha256,sha256,content_type,size_bytes,width,height,content) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (mid, user_id, cid, client_id, digest, hashlib.sha256(normalized).hexdigest(), mime, len(normalized), width, height, normalized))
            return self.metadata(db.execute("SELECT * FROM media WHERE id=?", (mid,)).fetchone())

    def download(self, user_id: int, media_id: str) -> tuple[bytes, str]:
        with self.repository.transaction() as db:
            row = db.execute("SELECT * FROM media WHERE id=?", (media_uuid(media_id),)).fetchone()
            if row is None:
                raise ChatError("not_found", "Image not found", 404)
            self.service._member(db, user_id, row["conversation_id"])
            visible = row["owner_id"] == user_id or db.execute(
                "SELECT 1 FROM media_uses u JOIN messages m ON m.id=u.message_id WHERE u.media_id=? AND m.recalled=0 LIMIT 1", (media_id,)).fetchone()
            if not visible:
                raise ChatError("forbidden", "Image is not visible in this conversation", 403)
            return bytes(row["content"]), row["content_type"]
