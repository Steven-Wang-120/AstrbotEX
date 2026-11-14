"""astrc-mailbox/1 identities shared by the EX bot endpoint and astrC."""
from __future__ import annotations
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .peer_relay import canonical, unbase, validate_card, verify
from .peer_content import MAX_PLAIN_BYTES, validate_content


def b64(value):
    return base64.b64encode(value).decode("ascii")


class PeerIdentity:
    def __init__(self, seed: bytes, box_seed: bytes, name: str):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40:
            raise ValueError("Nickname must contain 1 to 40 characters")
        self.signing = Ed25519PrivateKey.from_private_bytes(seed)
        self.exchange = X25519PrivateKey.from_private_bytes(box_seed)
        self.name = name.strip()
        self.id = hashlib.sha256(self.signing.public_key().public_bytes_raw()).hexdigest()

    @classmethod
    def load_or_create(cls, path: Path, name: str):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise ValueError("Unsafe identity path")
        if not path.exists():
            data = {"seed": b64(os.urandom(32)), "box_seed": b64(os.urandom(32))}
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as stream:
                stream.write(json.dumps(data))
                stream.flush()
                os.fsync(stream.fileno())
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(unbase(data["seed"], 32), unbase(data["box_seed"], 32), name)

    def sign(self, data):
        return b64(self.signing.sign(data))

    def card(self, relay):
        card = {"v": 1, "id": self.id, "name": self.name, "relay": relay,
                "sign_key": b64(self.signing.public_key().public_bytes_raw()),
                "box_key": b64(self.exchange.public_key().public_bytes_raw())}
        card["signature"] = self.sign(canonical(card))
        return card

    def contact_code(self, relay):
        return "astrc:contact:" + base64.urlsafe_b64encode(canonical(self.card(relay))).decode("ascii")

    def friend_request(self, relay, recipient, comment=""):
        request = {"v": 1, "id": str(uuid4()), "recipient": recipient, "card": self.card(relay),
                   "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "comment": comment}
        request["signature"] = self.sign(canonical(request))
        return request

    def seal(self, recipient, text, message_id=None):
        return self.seal_content(recipient, {"text": text}, message_id)

    def seal_content(self, recipient, content, message_id=None, *, created_at=None):
        validate_content(content)
        ephemeral = X25519PrivateKey.generate()
        header = {"v": 1, "id": message_id or str(uuid4()), "sender": self.id, "recipient": recipient["id"],
                  "created_at": created_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                  "ephemeral": b64(ephemeral.public_key().public_bytes_raw())}
        shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(unbase(recipient["box_key"], 32)))
        key = HKDF(SHA256(), 32, canonical(header), b"astrc-message-v1").derive(shared)
        nonce = os.urandom(12)
        encrypted = AESGCM(key).encrypt(nonce, canonical(content), canonical(header))
        packet = {**header, "nonce": b64(nonce), "ciphertext": b64(encrypted[:-16]), "mac": b64(encrypted[-16:])}
        packet["signature"] = self.sign(canonical(packet))
        return packet

    def open(self, sender, packet):
        return self.open_content(sender, packet)["text"]

    def open_content(self, sender, packet):
        fields = {"v", "id", "sender", "recipient", "created_at", "ephemeral", "nonce", "ciphertext", "mac", "signature"}
        if not isinstance(packet, dict) or set(packet) != fields or type(packet["v"]) is not int or packet["v"] != 1 or packet["sender"] != sender["id"] or packet["recipient"] != self.id:
            raise ValueError("Invalid message identity")
        try:
            if str(UUID(packet["id"])) != packet["id"] or not isinstance(packet["created_at"], str) or not 1 <= len(packet["created_at"]) <= 40:
                raise ValueError("Invalid message header")
            if datetime.fromisoformat(packet["created_at"].replace("Z", "+00:00")).tzinfo is None:
                raise ValueError("Invalid message timestamp")
        except (TypeError, AttributeError):
            raise ValueError("Invalid message header") from None
        if not isinstance(packet["ciphertext"], str) or len(packet["ciphertext"]) > 4 * ((MAX_PLAIN_BYTES + 2) // 3):
            raise ValueError("Invalid message size")
        ciphertext = unbase(packet["ciphertext"])
        if not 1 <= len(ciphertext) <= MAX_PLAIN_BYTES:
            raise ValueError("Invalid message size")
        verify(packet, sender["sign_key"])
        header = {k: packet[k] for k in ("v", "id", "sender", "recipient", "created_at", "ephemeral")}
        shared = self.exchange.exchange(X25519PublicKey.from_public_bytes(unbase(packet["ephemeral"], 32)))
        key = HKDF(SHA256(), 32, canonical(header), b"astrc-message-v1").derive(shared)
        try:
            plain = AESGCM(key).decrypt(unbase(packet["nonce"], 12), ciphertext + unbase(packet["mac"], 16), canonical(header))
        except InvalidTag:
            raise ValueError("Invalid encrypted message") from None
        data = json.loads(plain)
        return validate_content(data)


def parse_contact(code, *, allow_loopback=False):
    if not isinstance(code, str) or len(code) > 4096:
        raise ValueError("Invalid contact code")
    code = code.strip()
    if code.startswith("astrc:contact:"):
        code = code[14:]
        code = base64.b64decode(code + "=" * (-len(code) % 4), altchars=b"-_", validate=True).decode("utf-8")
    return validate_card(json.loads(code), allow_loopback=allow_loopback)
