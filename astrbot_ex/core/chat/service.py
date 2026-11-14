"""Shared domain operations for native clients and OneBot actions."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from uuid import UUID, uuid4

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

from . import PROTOCOL
from .models import ChatError
from .media import MediaStore, media_uuid
from .repository import ChatRepository
from .peer_content import MAX_ATTACHMENT_BYTES


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def entity_id(value) -> int:
    try:
        number = int(value)
        if str(number) != str(value) or not 0 < number <= 2147483647:
            raise ValueError()
        return number
    except (ValueError, TypeError):
        raise ChatError("invalid_id", "Invalid account or message ID") from None


class ChatService:
    def __init__(self, repository: ChatRepository):
        self.repository = repository
        self.media = MediaStore(self)
        self.passwords = PasswordHasher()
        self._dummy_hash = self.passwords.hash(secrets.token_urlsafe(24))
        with repository.transaction() as db:
            db.execute("INSERT OR IGNORE INTO settings VALUES ('cursor_secret',?)", (secrets.token_hex(32),))
            self._cursor_secret = bytes.fromhex(db.execute("SELECT value FROM settings WHERE key='cursor_secret'").fetchone()[0])

    @staticmethod
    def _user(row) -> dict:
        return {"user_id": str(row["id"]), "username": row["username"], "display_name": row["display_name"], "bio": row["bio"], "kind": row["kind"]}

    @staticmethod
    def _member(db, user_id: int, conversation_id: str):
        row = db.execute("SELECT role FROM members WHERE conversation_id=? AND user_id=?", (conversation_id, user_id)).fetchone()
        if row is None:
            raise ChatError("forbidden", "You are not a member of this conversation", 403)
        return row

    @staticmethod
    def _message(row) -> dict:
        return {"protocol": PROTOCOL, "message_id": str(row["id"]), "conversation_id": row["conversation_id"],
                "sender_id": str(row["sender_id"]), "client_message_id": row["client_message_id"],
                "created_at": row["created_at"], "recalled": bool(row["recalled"]),
                "segments": [] if row["recalled"] else json.loads(row["segments"])}

    def agent_media_url(self, user_id, media_id, *, expires=None):
        self.media.download(user_id, media_id)
        expires = expires or int(time.time()) + 600
        signature = hmac.new(self._cursor_secret, f"agent-media:{user_id}:{media_id}:{expires}".encode(), hashlib.sha256).hexdigest()
        root = os.environ.get("ASTREX_CHAT_MEDIA_BASE_URL", "http://127.0.0.1:8780").rstrip("/")
        return f"{root}/api/v1/chat/agent-media/{media_id}?user_id={user_id}&expires={expires}&signature={signature}"

    def agent_media(self, user_id, media_id, expires, signature):
        expected = hmac.new(self._cursor_secret, f"agent-media:{user_id}:{media_id}:{expires}".encode(), hashlib.sha256).hexdigest()
        if expires < time.time() or expires > time.time() + 601 or not hmac.compare_digest(expected, signature):
            raise ChatError("forbidden", "Media link expired or invalid", 403)
        return self.media.download(user_id, media_id)

    @staticmethod
    def _emit(db, user_ids, kind: str, payload: dict) -> None:
        event_id, timestamp = str(uuid4()), now()
        db.executemany("INSERT INTO events(user_id,event_id,type,occurred_at,payload) VALUES (?,?,?,?,?)",
                       [(int(uid), event_id, kind, timestamp, json.dumps(payload, ensure_ascii=False)) for uid in set(user_ids)])

    def create_user(self, username: str, password: str, display_name: str, kind: str = "human") -> dict:
        username, display_name = username.strip(), display_name.strip()
        if not username or len(username) > 64 or not 8 <= len(password) <= 256 or not display_name or len(display_name) > 64 or kind not in {"human", "bot"}:
            raise ChatError("invalid_account", "Provide a name and a password of at least 8 characters")
        encoded = self.passwords.hash(password)
        try:
            with self.repository.transaction() as db:
                cursor = db.execute("INSERT INTO users(username,password_hash,display_name,kind) VALUES (?,?,?,?)", (username, encoded, display_name, kind))
                return self._user(db.execute("SELECT * FROM users WHERE id=?", (cursor.lastrowid,)).fetchone())
        except sqlite3.IntegrityError:
            raise ChatError("account_conflict", "Account already exists or ID limit reached", 409) from None

    def login(self, username: str, password: str, device_id: str) -> dict:
        with self.repository.transaction() as db:
            row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        try:
            self.passwords.verify(row["password_hash"] if row else self._dummy_hash, password)
        except VerificationError:
            raise ChatError("invalid_credentials", "Incorrect account or password", 401) from None
        if row is None or row["kind"] != "human":
            raise ChatError("invalid_credentials", "Incorrect account or password", 401)
        token = secrets.token_urlsafe(32)
        with self.repository.transaction() as db:
            db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), row["id"], device_id, time.time() + 3600))
        return {"access_token": token, "token_type": "bearer", "expires_in": 3600, "user": self._user(row), "device_id": device_id}

    def authenticate(self, token: str) -> int:
        with self.repository.transaction() as db:
            row = db.execute("SELECT user_id FROM sessions WHERE token_hash=? AND expires_at>?", (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        if row is None:
            raise ChatError("unauthorized", "Please sign in again", 401)
        return row["user_id"]

    def logout(self, token: str) -> None:
        with self.repository.transaction() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))

    def user(self, user_id: int) -> dict:
        with self.repository.transaction() as db:
            row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if row is None:
                raise ChatError("not_found", "Account not found", 404)
            return self._user(row)

    def update_profile(self, user_id: int, changes: dict) -> dict:
        if set(changes) - {"display_name", "bio"}:
            raise ChatError("invalid_profile", "Unsupported profile field")
        with self.repository.transaction() as db:
            for key, value in changes.items():
                if value is None:
                    continue
                value = value.strip()
                if (key == "display_name" and not 1 <= len(value) <= 64) or (key == "bio" and len(value) > 280):
                    raise ChatError("invalid_profile", "Invalid profile length")
                db.execute(f"UPDATE users SET {key}=? WHERE id=?", (value, user_id))
            profile = self._user(db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())
            self._emit(db, [user_id], "profile.updated", {"user": profile})
            return profile

    def create_group(self, owner_id: int, title: str, member_ids: list[str]) -> dict:
        if not title.strip() or len(title) > 80:
            raise ChatError("invalid_title", "Group name is required")
        ids = {owner_id, *(entity_id(x) for x in member_ids)}
        if len(ids) > 100:
            raise ChatError("too_many_members", "Group size exceeds 100")
        with self.repository.transaction() as db:
            for uid in ids:
                if not db.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
                    raise ChatError("not_found", "Group member not found", 404)
            group_id = db.execute("SELECT COALESCE(MAX(group_id),0)+1 FROM conversations").fetchone()[0]
            if group_id > 2147483647:
                raise ChatError("id_exhausted", "Group ID capacity reached", 503)
            cid = str(uuid4())
            db.execute("INSERT INTO conversations VALUES (?,?,?,?,NULL)", (cid, "group", title.strip(), group_id))
            db.executemany("INSERT INTO members VALUES (?,?,?)", [(cid, uid, "owner" if uid == owner_id else "member") for uid in ids])
            self._emit(db, ids, "conversation.updated", {"conversation_id": cid})
        return self.conversation(owner_id, cid)

    def direct(self, user_id: int, target_id: str) -> dict:
        target = entity_id(target_id)
        if user_id == target:
            raise ChatError("invalid_target", "Choose another account")
        with self.repository.transaction() as db:
            if not db.execute("SELECT 1 FROM users WHERE id=?", (target,)).fetchone():
                raise ChatError("not_found", "Account not found", 404)
            key = ":".join(str(x) for x in sorted((user_id, target)))
            row = db.execute("SELECT id FROM conversations WHERE direct_key=?", (key,)).fetchone()
            if row:
                cid = row[0]
            else:
                cid = str(uuid4())
                db.execute("INSERT INTO conversations VALUES (?,'direct','',NULL,?)", (cid, key))
                db.executemany("INSERT INTO members VALUES (?,?,'member')", [(cid, user_id), (cid, target)])
                self._emit(db, (user_id, target), "conversation.updated", {"conversation_id": cid})
        return self.conversation(user_id, cid)

    def conversation(self, user_id: int, cid: str) -> dict:
        with self.repository.transaction() as db:
            self._member(db, user_id, cid)
            row = db.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
            people = db.execute("SELECT u.*,m.role FROM users u JOIN members m ON m.user_id=u.id WHERE m.conversation_id=? ORDER BY u.id", (cid,)).fetchall()
            last = db.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
            title = row["title"] if row["kind"] == "group" else next(x["display_name"] for x in people if x["id"] != user_id)
            return {"conversation_id": cid, "kind": row["kind"], "title": title,
                    "group_id": str(row["group_id"]) if row["group_id"] else None,
                    "members": [{**self._user(x), "role": x["role"]} for x in people],
                    "last_message": self._message(last) if last else None, "unread_count": None}

    def conversations(self, user_id: int) -> list[dict]:
        with self.repository.transaction() as db:
            ids = [r[0] for r in db.execute("SELECT conversation_id FROM members WHERE user_id=? ORDER BY conversation_id", (user_id,))]
        return [self.conversation(user_id, cid) for cid in ids]

    def messages(self, user_id: int, cid: str, limit: int = 50, before: int | None = None) -> list[dict]:
        with self.repository.transaction() as db:
            self._member(db, user_id, cid)
            rows = db.execute("SELECT * FROM messages WHERE conversation_id=? AND id<? ORDER BY id DESC LIMIT ?", (cid, before or 2147483648, min(max(limit, 1), 100))).fetchall()
            return [self._message(r) for r in reversed(rows)]

    def send(self, user_id: int, cid: str, client_id: str, segments: list[dict], *, sent_at: str | None = None) -> dict:
        try:
            client_id = str(UUID(client_id))
        except (ValueError, TypeError):
            raise ChatError("invalid_message_id", "client_message_id must be a UUID") from None
        if not isinstance(segments, list) or not 1 <= len(segments) <= 16:
            raise ChatError("invalid_message", "Message must contain 1 to 16 segments")
        text_length, content = 0, False
        for index, segment in enumerate(segments):
            if not isinstance(segment, dict) or set(segment) != {"type", "data"} or not isinstance(segment["data"], dict):
                raise ChatError("invalid_message", "Invalid message segment")
            kind, data = segment["type"], segment["data"]
            field = {"text": "text", "mention": "user_id", "reply": "message_id", "image": "media_id", "file": "media_id", "sticker": "media_id"}.get(kind) if isinstance(kind, str) else None
            if field is None:
                raise ChatError("unsupported_segment", "Supported segments: text, mention, reply, image, file, sticker")
            if set(data) != {field} or not isinstance(data[field], str):
                raise ChatError("invalid_message", "Invalid segment data")
            value = data[field]
            if kind == "text":
                if not value or len(value) > 4000:
                    raise ChatError("invalid_message", "Text length must be between 1 and 4000")
                text_length += len(value)
                content = True
            elif kind in {"image", "file", "sticker"}:
                media_uuid(value)
                content = True
            elif kind == "reply":
                entity_id(value)
                if index != 0:
                    raise ChatError("invalid_message", "A single reply must be the first segment")
            else:
                if value != "all":
                    entity_id(value)
                content = True
        if not content or text_length > 4000:
            raise ChatError("invalid_message", "Provide text, an attachment or a mention; total text cannot exceed 4000 characters")
        encoded = json.dumps(segments, ensure_ascii=False, sort_keys=True)
        with self.repository.transaction() as db:
            member = self._member(db, user_id, cid)
            existing = db.execute("SELECT * FROM messages WHERE sender_id=? AND client_message_id=?", (user_id, client_id)).fetchone()
            if existing:
                if existing["conversation_id"] != cid or existing["segments"] != encoded:
                    raise ChatError("idempotency_conflict", "Message ID is already used for different content", 409)
                return self._message(existing)
            # Check dynamic permissions only for new messages: an accepted retry
            # remains idempotent even if its quoted message has since been recalled.
            kind = db.execute("SELECT kind FROM conversations WHERE id=?", (cid,)).fetchone()["kind"]
            attachment_count = attachment_size = 0
            for segment in segments:
                if segment["type"] == "mention":
                    target = segment["data"]["user_id"]
                    if kind != "group":
                        raise ChatError("invalid_mention", "Mentions are only supported in groups")
                    if target == "all":
                        if member["role"] not in {"owner", "admin"}:
                            raise ChatError("forbidden", "Only group administrators can mention all members", 403)
                    elif db.execute("SELECT 1 FROM members WHERE conversation_id=? AND user_id=?", (cid, entity_id(target))).fetchone() is None:
                        raise ChatError("invalid_mention", "Mention target is not a member of this group")
                elif segment["type"] in {"image", "file", "sticker"}:
                    media = db.execute("SELECT owner_id,conversation_id,kind,size_bytes FROM media WHERE id=?", (segment["data"]["media_id"],)).fetchone()
                    if media is None or media["owner_id"] != user_id or media["conversation_id"] != cid:
                        raise ChatError("invalid_media", "Attach media uploaded by this account for this conversation")
                    if media["kind"] != segment["type"]:
                        raise ChatError("invalid_media", "Attachment kind does not match its segment")
                    attachment_count += 1
                    attachment_size += media["size_bytes"]
                elif segment["type"] == "reply":
                    target = db.execute("SELECT conversation_id,recalled FROM messages WHERE id=?", (entity_id(segment["data"]["message_id"]),)).fetchone()
                    if target is None or target["conversation_id"] != cid or target["recalled"]:
                        raise ChatError("invalid_reply", "Reply target must be an available message in this conversation")
            if attachment_count > 4 or attachment_size > MAX_ATTACHMENT_BYTES:
                raise ChatError("media_too_large", "At most four attachments totaling 8 MiB", 413)
            if sent_at is not None:
                try:
                    timestamp = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
                    if timestamp.tzinfo is None:
                        raise ValueError()
                except (ValueError, TypeError, AttributeError):
                    raise ChatError("invalid_timestamp", "Provide a timezone-aware timestamp") from None
            row = db.execute("INSERT INTO messages(conversation_id,sender_id,client_message_id,segments,created_at) VALUES (?,?,?,?,?)", (cid, user_id, client_id, encoded, sent_at or now()))
            message = self._message(db.execute("SELECT * FROM messages WHERE id=?", (row.lastrowid,)).fetchone())
            db.executemany("INSERT OR IGNORE INTO media_uses(media_id,message_id) VALUES (?,?)",
                           [(s["data"]["media_id"], row.lastrowid) for s in segments if s["type"] in {"image", "file", "sticker"}])
            people = [r[0] for r in db.execute("SELECT user_id FROM members WHERE conversation_id=?", (cid,))]
            self._emit(db, people, "message.created", {"message": message})
            return message

    def get_message(self, user_id: int, message_id: str) -> dict:
        with self.repository.transaction() as db:
            row = db.execute("SELECT * FROM messages WHERE id=?", (entity_id(message_id),)).fetchone()
            if row is None:
                raise ChatError("not_found", "Message not found", 404)
            self._member(db, user_id, row["conversation_id"])
            return self._message(row)

    def recall(self, user_id: int, message_id: str) -> None:
        with self.repository.transaction() as db:
            row = db.execute("SELECT * FROM messages WHERE id=?", (entity_id(message_id),)).fetchone()
            if row is None:
                raise ChatError("not_found", "Message not found", 404)
            message = self._message(row)
            role = self._member(db, user_id, message["conversation_id"])["role"]
            if int(message["sender_id"]) != user_id and role not in {"owner", "admin"}:
                raise ChatError("forbidden", "You cannot recall this message", 403)
            if message["recalled"]:
                return
            db.execute("UPDATE messages SET recalled=1 WHERE id=?", (entity_id(message_id),))
            people = [r[0] for r in db.execute("SELECT user_id FROM members WHERE conversation_id=?", (message["conversation_id"],))]
            self._emit(db, people, "message.recalled", {"message_id": message_id, "conversation_id": message["conversation_id"]})

    def _cursor(self, uid: int, seq: int) -> str:
        value = f"{uid}:{seq}".encode()
        signature = hmac.new(self._cursor_secret, value, hashlib.sha256).hexdigest()
        return base64.urlsafe_b64encode(value + b":" + signature.encode()).decode().rstrip("=")

    def _sequence(self, uid: int, cursor: str | None) -> int:
        if not cursor:
            return 0
        try:
            account, number, signature = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode().split(":")
            seq = int(number)
            expected = hmac.new(self._cursor_secret, f"{account}:{number}".encode(), hashlib.sha256).hexdigest()
            if int(account) != uid or seq < 0 or not hmac.compare_digest(signature, expected):
                raise ValueError()
            return seq
        except (ValueError, UnicodeError, TypeError):
            raise ChatError("invalid_cursor", "Cursor does not belong to this account") from None

    def sync(self, user_id: int, after: str | None = None, limit: int = 100) -> dict:
        seq = self._sequence(user_id, after)
        limit = min(max(limit, 1), 100)
        with self.repository.transaction() as db:
            rows = db.execute("SELECT * FROM events WHERE user_id=? AND seq>? ORDER BY seq LIMIT ?", (user_id, seq, limit + 1)).fetchall()
            events = [{"protocol": PROTOCOL, "event_id": r["event_id"], "seq": str(r["seq"]), "cursor": self._cursor(user_id, r["seq"]),
                       "type": r["type"], "occurred_at": r["occurred_at"], "payload": json.loads(r["payload"])} for r in rows[:limit]]
        return {"events": events, "next_cursor": events[-1]["cursor"] if events else self._cursor(user_id, seq), "has_more": len(rows) > limit}

    def bot_offset(self, bot_id: int) -> str:
        with self.repository.transaction() as db:
            row = db.execute("SELECT seq FROM bot_offsets WHERE user_id=?", (bot_id,)).fetchone()
        return self._cursor(bot_id, row[0] if row else 0)

    def acknowledge_bot_event(self, bot_id: int, seq: int) -> None:
        # This is a transport send offset, not an AstrBot consumption receipt.
        with self.repository.transaction() as db:
            db.execute("INSERT INTO bot_offsets VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET seq=MAX(seq,excluded.seq)", (bot_id, seq))

    def seed_demo(self) -> dict:
        with self.repository.transaction() as db:
            if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                return {}
        alice = self.create_user("alice", "astrc-demo", "Alice")
        bob = self.create_user("bob", "astrc-demo", "Bob")
        bot = self.create_user("astrbot", secrets.token_urlsafe(32), "AstrBot", "bot")
        group = self.create_group(int(alice["user_id"]), "一起聊聊", [bob["user_id"], bot["user_id"]])
        self.send(int(alice["user_id"]), group["conversation_id"], str(uuid4()), [{"type": "text", "data": {"text": "欢迎来到 astrC，这是一条演示环境的真实入库消息。"}}])
        return {"alice": alice["user_id"], "bob": bob["user_id"], "bot": bot["user_id"], "conversation_id": group["conversation_id"]}
