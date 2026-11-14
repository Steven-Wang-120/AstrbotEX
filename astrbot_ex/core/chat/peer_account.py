"""EX acts as an astrC network endpoint; OneBot remains an Agent adapter."""
from __future__ import annotations
import asyncio
import json
from pathlib import Path
import secrets
import time
from uuid import NAMESPACE_URL, uuid4, uuid5
from urllib.parse import urlsplit

import httpx

from .models import ChatError
from .peer_identity import PeerIdentity, parse_contact
from .peer_relay import canonical, validate_friend_request, Fault
from .peer_content import MAX_ATTACHMENT_BYTES, validate_content


class RelayError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)


class PeerRelayClient:
    def __init__(self, identity, relay, *, allow_loopback=False, client=None):
        self.identity, self.relay, self.allow_loopback = identity, relay, allow_loopback
        self.client = client or httpx.AsyncClient(timeout=60, follow_redirects=False, trust_env=False)
        self.tokens, self.allowed = {}, set()

    def origin(self, value):
        parsed = urlsplit(value)
        local = self.allow_loopback and parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (not local and parsed.scheme != "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ValueError("HTTPS relay origin required")
        parsed.port
        return value.rstrip("/")

    async def raw(self, origin, path, method="GET", data=None, token=None):
        url = self.origin(origin) + "/api/v1/peer" + path
        headers = {"Authorization": "Bearer " + token} if token else {}
        response = await self.client.request(method, url, json=data, headers=headers)
        if response.status_code >= 300:
            try:
                code = response.json().get("error", "relay_unavailable")
            except ValueError:
                code = "relay_unavailable"
            raise RelayError(response.status_code, code if isinstance(code, str) else "relay_unavailable")
        return response.json()

    async def authenticate(self, origin):
        challenge = await self.raw(origin, "/challenge", "POST", {"id": self.identity.id})
        proof = f"astrc-relay-auth-v1\n{challenge['challenge_id']}\n{challenge['challenge']}\n{self.identity.id}".encode()
        result = await self.raw(origin, "/session", "POST", {"card": self.identity.card(self.relay),
            "challenge_id": challenge["challenge_id"], "signature": self.identity.sign(proof)})
        self.tokens[origin] = result["access_token"]

    async def request(self, origin, path, method="GET", data=None):
        origin = self.origin(origin)
        if origin not in self.tokens:
            await self.authenticate(origin)
        try:
            return await self.raw(origin, path, method, data, self.tokens[origin])
        except RelayError as exc:
            if exc.status != 401:
                raise
        await self.authenticate(origin)
        return await self.raw(origin, path, method, data, self.tokens[origin])

    async def allow(self, peer):
        if peer not in self.allowed:
            await self.request(self.relay, "/allowed/" + peer, "PUT")
            self.allowed.add(peer)

    async def close(self):
        self.tokens.clear()
        self.allowed.clear()
        await self.client.aclose()


class PeerAccount:
    def __init__(self, service, bot_id, data_root: Path, relay, name, *, auto_accept=False, allow_loopback=False, client=None):
        if service.user(bot_id)["kind"] != "bot" or type(auto_accept) is not bool:
            raise ValueError("A bot account and boolean friend policy are required")
        self.service, self.bot_id, self.root = service, bot_id, data_root
        self.identity = PeerIdentity.load_or_create(data_root / "secrets" / f"chat-peer-{bot_id}.json", name)
        self.client = PeerRelayClient(self.identity, relay, allow_loopback=allow_loopback, client=client)
        self.relay = self.client.origin(relay)
        self.auto_accept, self.allow_loopback = auto_accept, allow_loopback
        self.online, self.last_error = False, ""
        with self.service.repository.transaction() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS peer_contacts(bot_id INTEGER,peer_id TEXT,user_id INTEGER,card TEXT,state TEXT,
              PRIMARY KEY(bot_id,peer_id),UNIQUE(bot_id,user_id));
            CREATE TABLE IF NOT EXISTS peer_requests(bot_id INTEGER,flag TEXT,packet TEXT,state TEXT,resolved INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(bot_id,flag));
            CREATE TABLE IF NOT EXISTS peer_outbox(bot_id INTEGER,message_id INTEGER,packet TEXT,accepted INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(bot_id,message_id));
            CREATE TABLE IF NOT EXISTS peer_outgoing_requests(bot_id INTEGER,peer_id TEXT,packet TEXT,sent INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(bot_id,peer_id));
            CREATE TABLE IF NOT EXISTS peer_rejected(bot_id INTEGER,sender TEXT,id TEXT,PRIMARY KEY(bot_id,sender,id));
            ''')

    def contacts(self):
        with self.service.repository.transaction() as db:
            return [dict(r) for r in db.execute("SELECT * FROM peer_contacts WHERE bot_id=?", (self.bot_id,))]

    def user_for_card(self, card):
        with self.service.repository.transaction() as db:
            previous = db.execute("SELECT * FROM peer_contacts WHERE bot_id=? AND peer_id=?", (self.bot_id, card["id"])).fetchone()
            if previous:
                if json.loads(previous["card"])["box_key"] != card["box_key"]:
                    raise ValueError("Contact encryption key changed")
                db.execute("UPDATE peer_contacts SET card=? WHERE bot_id=? AND peer_id=?", (canonical(card).decode(), self.bot_id, card["id"]))
                db.execute("UPDATE users SET display_name=? WHERE id=?", (card["name"], previous["user_id"]))
                return previous["user_id"]
            if db.execute("SELECT COUNT(*) FROM peer_contacts WHERE bot_id=?", (self.bot_id,)).fetchone()[0] >= 1000:
                raise ValueError("Contact capacity reached")
            # This number is only an adapter-local OneBot mapping, never a network account.
            username = "peer:" + card["id"][:59]
            user = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        uid = user[0] if user else int(self.service.create_user(username, secrets.token_urlsafe(32), card["name"])["user_id"])
        with self.service.repository.transaction() as db:
            db.execute("INSERT INTO peer_contacts VALUES (?,?,?,?,?)", (self.bot_id, card["id"], uid, canonical(card).decode(), "pending"))
        return uid

    def ingest_request(self, packet):
        validate_friend_request(packet, allow_loopback=self.allow_loopback)
        if packet["recipient"] != self.identity.id:
            raise ValueError("Request belongs to another account")
        sender = packet["card"]
        uid = self.user_for_card(sender)
        flag = sender["id"] + ":" + packet["id"]
        with self.service.repository.transaction() as db:
            old = db.execute("SELECT packet FROM peer_requests WHERE bot_id=? AND flag=?", (self.bot_id, flag)).fetchone()
            if old:
                if old[0] != canonical(packet).decode():
                    raise ValueError("Request ID conflict")
                return flag
            db.execute("INSERT INTO peer_requests VALUES (?,?,?,'pending',0)", (self.bot_id, flag, canonical(packet).decode()))
            if not self.auto_accept:
                self.service._emit(db, [self.bot_id], "friend.request", {"user_id": uid, "comment": packet["comment"], "flag": flag})
        if self.auto_accept:
            self.resolve(flag, True)
        return flag

    def resolve(self, flag, approve=True):
        if not isinstance(flag, str) or type(approve) is not bool:
            raise ChatError("invalid_request", "Provide a friend request flag and boolean approval")
        with self.service.repository.transaction() as db:
            row = db.execute("SELECT * FROM peer_requests WHERE bot_id=? AND flag=?", (self.bot_id, flag)).fetchone()
            if row is None:
                raise ChatError("not_found", "Friend request not found", 404)
            desired = "approved" if approve else "rejected"
            if row["state"] != "pending":
                if row["state"] != desired:
                    raise ChatError("request_resolved", "Friend request was already resolved", 409)
                return None
            packet = json.loads(row["packet"])
            uid = db.execute("SELECT user_id FROM peer_contacts WHERE bot_id=? AND peer_id=?", (self.bot_id, packet["card"]["id"])).fetchone()[0]
            db.execute("UPDATE peer_requests SET state=? WHERE bot_id=? AND flag=?", (desired, self.bot_id, flag))
            if approve:
                db.execute("UPDATE peer_contacts SET state='accepted',card=? WHERE bot_id=? AND peer_id=?", (canonical(packet["card"]).decode(), self.bot_id, packet["card"]["id"]))
                self.service._emit(db, [self.bot_id], "friend.add", {"user_id": uid})
        if approve:
            self.service.direct(self.bot_id, str(uid))
        return None

    def add_contact(self, code):
        card = parse_contact(code, allow_loopback=self.allow_loopback)
        if card["id"] == self.identity.id:
            raise ValueError("Cannot add self")
        uid = self.user_for_card(card)
        with self.service.repository.transaction() as db:
            db.execute("UPDATE peer_contacts SET state='accepted',card=? WHERE bot_id=? AND peer_id=?", (canonical(card).decode(), self.bot_id, card["id"]))
            request = self.identity.friend_request(self.relay, card["id"])
            db.execute("INSERT OR IGNORE INTO peer_outgoing_requests VALUES (?,?,?,0)", (self.bot_id, card["id"], canonical(request).decode()))
        self.service.direct(self.bot_id, str(uid))
        return {"user_id": str(uid), "id": card["id"], "name": card["name"]}

    def is_peer_user(self, uid):
        return any(r["user_id"] == int(uid) for r in self.contacts())

    def validate_send(self, uid, segments):
        if self.is_peer_user(uid):
            contact = next(r for r in self.contacts() if r["user_id"] == int(uid))
            if contact["state"] != "accepted":
                raise ChatError("forbidden", "Friend request has not been approved", 403)
            if any(s["type"] not in {"text", "reply", "image", "file", "sticker"} for s in segments):
                raise ChatError("unsupported_segment", "Unsupported private message segment")

    def status(self):
        with self.service.repository.transaction() as db:
            requests = [{"flag": r["flag"], "state": r["state"], "card": json.loads(r["packet"])["card"],
                         "comment": json.loads(r["packet"])["comment"]} for r in db.execute("SELECT * FROM peer_requests WHERE bot_id=? ORDER BY rowid DESC LIMIT 100", (self.bot_id,))]
        return {"enabled": True, "user_id": str(self.bot_id), "card": self.identity.card(self.relay),
                "contact_code": self.identity.contact_code(self.relay), "online": self.online, "error": self.last_error,
                "auto_accept": self.auto_accept, "requests": requests,
                "contacts": [{"id": r["peer_id"], "user_id": str(r["user_id"]), "name": json.loads(r["card"])["name"], "state": r["state"]} for r in self.contacts()]}

    async def sync_once(self):
        try:
            requests = await self.client.request(self.relay, "/friend-requests")
            for packet in requests["items"]:
                self.ingest_request(packet)
            with self.service.repository.transaction() as db:
                resolved = [dict(r) for r in db.execute("SELECT * FROM peer_requests WHERE bot_id=? AND state!='pending' AND resolved=0", (self.bot_id,))]
            for row in resolved:
                packet = json.loads(row["packet"])
                try:
                    await self.client.request(self.relay, "/friend-requests/" + packet["id"] + "/resolve", "POST",
                                              {"sender": packet["card"]["id"], "approve": row["state"] == "approved"})
                except RelayError as exc:
                    if exc.status != 404 or exc.code != "request_not_found":
                        raise
                with self.service.repository.transaction() as db:
                    db.execute("UPDATE peer_requests SET resolved=1 WHERE bot_id=? AND flag=?", (self.bot_id, row["flag"]))
            contacts = {r["peer_id"]: r for r in self.contacts() if r["state"] == "accepted"}
            with self.service.repository.transaction() as db:
                outgoing_requests = [dict(r) for r in db.execute("SELECT * FROM peer_outgoing_requests WHERE bot_id=? AND sent=0 LIMIT 20", (self.bot_id,))]
            for row in outgoing_requests:
                if row["peer_id"] not in contacts:
                    continue
                card = json.loads(contacts[row["peer_id"]]["card"])
                try:
                    await self.client.request(card["relay"], "/friend-requests", "POST", json.loads(row["packet"]))
                except RelayError:
                    continue
                with self.service.repository.transaction() as db:
                    db.execute("UPDATE peer_outgoing_requests SET sent=1 WHERE bot_id=? AND peer_id=?", (self.bot_id, row["peer_id"]))
            for peer in contacts:
                await self.client.allow(peer)
            inbox = await self.client.request(self.relay, "/envelopes")
            ack = []
            for packet in inbox["items"]:
                sender = packet["sender"]
                if sender not in contacts:
                    continue
                with self.service.repository.transaction() as db:
                    rejected = db.execute("SELECT 1 FROM peer_rejected WHERE bot_id=? AND sender=? AND id=?", (self.bot_id, sender, packet["id"])).fetchone()
                if not rejected:
                    try:
                        content = self.identity.open_content(json.loads(contacts[sender]["card"]), packet)
                    except Exception:
                        with self.service.repository.transaction() as db:
                            db.execute("INSERT OR IGNORE INTO peer_rejected VALUES (?,?,?)", (self.bot_id, sender, packet["id"]))
                    else:
                        uid = contacts[sender]["user_id"]
                        conversation = self.service.direct(self.bot_id, str(uid))
                        # ChatService commits before ACK; sender+UUID remains idempotent after restart.
                        segments = [{"type": "text", "data": {"text": content["text"]}}] if content["text"] else []
                        for index, item in enumerate(content.get("attachments", [])):
                            mid = str(uuid5(NAMESPACE_URL, f"astrc:{sender}:{packet['id']}:{index}"))
                            metadata = self.service.media.upload_attachment(uid, conversation["conversation_id"], mid, item)
                            segments.append({"type": item["kind"], "data": {"media_id": metadata["media_id"]}})
                        self.service.send(uid, conversation["conversation_id"], packet["id"], segments, sent_at=packet["created_at"])
                ack.append({"sender": sender, "id": packet["id"]})
            if ack:
                await self.client.request(self.relay, "/ack", "POST", {"items": ack})
            await self.flush_outbox(contacts)
            self.online, self.last_error = True, ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.online = False
            self.last_error = exc.code if isinstance(exc, RelayError) else "peer_network_unavailable"

    async def flush_outbox(self, contacts):
        for peer, contact in contacts.items():
            conversation = self.service.direct(self.bot_id, str(contact["user_id"]))
            with self.service.repository.transaction() as db:
                rows = [dict(r) for r in db.execute("SELECT m.*,o.packet FROM messages m LEFT JOIN peer_outbox o ON o.bot_id=? AND o.message_id=m.id WHERE m.sender_id=? AND m.conversation_id=? AND m.recalled=0 AND (o.accepted IS NULL OR o.accepted=0) ORDER BY m.id LIMIT 20", (self.bot_id, self.bot_id, conversation["conversation_id"]))]
            for row in rows:
                packet = json.loads(row["packet"]) if row["packet"] else None
                if packet is None:
                    segments = json.loads(row["segments"])
                    text = "".join(s["data"]["text"] for s in segments if s["type"] == "text")
                    items = [self.service.media.as_attachment(self.bot_id, s["data"]["media_id"]) for s in segments if s["type"] in {"image", "file", "sticker"}]
                    content = {"text": text, **({"attachments": items} if items else {})}
                    packet = self.identity.seal_content(json.loads(contact["card"]), content, row["client_message_id"], created_at=row["created_at"])
                    with self.service.repository.transaction() as db:
                        db.execute("INSERT INTO peer_outbox VALUES (?,?,?,0)", (self.bot_id, row["id"], canonical(packet).decode()))
                # Persist the complete fixed envelope before sending; retries never re-encrypt.
                await self.client.request(json.loads(contact["card"])["relay"], "/envelopes", "POST", packet)
                with self.service.repository.transaction() as db:
                    db.execute("UPDATE peer_outbox SET accepted=1 WHERE bot_id=? AND message_id=?", (self.bot_id, row["id"]))

    def history(self, peer_id, limit=50):
        contact = next((r for r in self.contacts() if r["peer_id"] == peer_id and r["state"] == "accepted"), None)
        if contact is None:
            raise ChatError("not_found", "Accepted contact not found", 404)
        conversation = self.service.direct(self.bot_id, str(contact["user_id"]))
        items = self.service.messages(self.bot_id, conversation["conversation_id"], min(100, max(1, limit)))
        with self.service.repository.transaction() as db:
            for message in items:
                if message["sender_id"] == str(self.bot_id):
                    outbox = db.execute("SELECT accepted FROM peer_outbox WHERE bot_id=? AND message_id=?", (self.bot_id, int(message["message_id"]))).fetchone()
                    message["delivery"] = "accepted" if outbox and outbox[0] else "queued"
                for segment in message["segments"]:
                    if segment["type"] in {"image", "file", "sticker"}:
                        row = db.execute("SELECT * FROM media WHERE id=?", (segment["data"]["media_id"],)).fetchone()
                        segment["data"] = {**segment["data"], **self.service.media.metadata(row)}
        return {"items": items, "conversation_id": conversation["conversation_id"], "self_id": str(self.bot_id)}

    def send_content(self, peer_id, content, message_id=None):
        try:
            validate_content(content)
        except ValueError as exc:
            raise ChatError("invalid_attachment", str(exc)) from None
        history = self.history(peer_id, 1)
        segments = [{"type": "text", "data": {"text": content["text"]}}] if content["text"] else []
        message_id = message_id or str(uuid4())
        for index, value in enumerate(content.get("attachments", [])):
            mid = str(uuid5(NAMESPACE_URL, f"astrc-management:{message_id}:{index}"))
            item = self.service.media.upload_attachment(self.bot_id, history["conversation_id"], mid, value)
            segments.append({"type": value["kind"], "data": {"media_id": item["media_id"]}})
        return self.service.send(self.bot_id, history["conversation_id"], message_id, segments)

    def favorite_sticker(self, value):
        value = {**value, "kind": "sticker"}
        try:
            validate_content({"text": "", "attachments": [value]})
        except ValueError as exc:
            raise ChatError("invalid_sticker", str(exc)) from None
        with self.service.repository.transaction() as db:
            existing = db.execute("SELECT 1 FROM sticker_favorites WHERE owner_id=? AND sha256=?", (self.bot_id, value["sha256"])).fetchone()
            count, size = db.execute("SELECT COUNT(*),COALESCE(SUM(LENGTH(attachment)),0) FROM sticker_favorites WHERE owner_id=?", (self.bot_id,)).fetchone()
            if not existing and (count >= 100 or size + len(canonical(value)) > 32 * 1024 * 1024):
                raise ChatError("sticker_quota", "Saved sticker capacity reached", 413)
            db.execute("INSERT OR IGNORE INTO sticker_favorites VALUES (?,?,?,?)", (self.bot_id, value["sha256"], canonical(value).decode(), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))

    def stickers(self):
        with self.service.repository.transaction() as db:
            return {"items": [json.loads(r[0]) for r in db.execute("SELECT attachment FROM sticker_favorites WHERE owner_id=? ORDER BY created_at", (self.bot_id,))]}

    def remove_sticker(self, digest):
        with self.service.repository.transaction() as db:
            db.execute("DELETE FROM sticker_favorites WHERE owner_id=? AND sha256=?", (self.bot_id, digest))


async def run_peer_endpoint(service):
    try:
        while True:
            account = getattr(service, "peer_account", None)
            if account is not None:
                await account.sync_once()
            await asyncio.sleep(2)
    finally:
        account = getattr(service, "peer_account", None)
        if account is not None:
            await account.client.close()
