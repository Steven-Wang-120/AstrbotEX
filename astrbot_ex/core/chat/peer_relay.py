"""Identity-authenticated, opaque store-and-forward mailboxes for astrC."""
from __future__ import annotations

import argparse
import base64
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse

PREFIX = "/api/v1/peer"
TTL = 7 * 86400


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def unbase(value: str, size: int | None = None) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
        if size is not None and len(raw) != size:
            raise ValueError()
        return raw
    except (TypeError, ValueError):
        raise Fault(400, "invalid_encoding") from None


class Fault(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code


def fingerprint(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise Fault(400, "invalid_identity")
    return value


def verify(data: dict, key: str):
    try:
        payload = {k: v for k, v in data.items() if k != "signature"}
        Ed25519PublicKey.from_public_bytes(unbase(key, 32)).verify(unbase(data.get("signature"), 64), canonical(payload))
    except (InvalidSignature, KeyError, TypeError, ValueError):
        raise Fault(400, "invalid_signature") from None


def validate_card(card, *, allow_loopback=False):
    if not isinstance(card, dict) or set(card) != {"v", "id", "name", "sign_key", "box_key", "relay", "signature"}:
        raise Fault(400, "invalid_card")
    if card["v"] != 1 or not isinstance(card["name"], str) or not 1 <= len(card["name"]) <= 40:
        raise Fault(400, "invalid_card")
    key = unbase(card["sign_key"], 32)
    unbase(card["box_key"], 32)
    if hashlib.sha256(key).hexdigest() != fingerprint(card["id"]):
        raise Fault(400, "invalid_identity")
    if not all(isinstance(card[k], str) for k in ("id", "sign_key", "box_key", "relay", "signature")):
        raise Fault(400, "invalid_card")
    try:
        relay = urlsplit(card["relay"])
        relay.port
    except ValueError:
        raise Fault(400, "invalid_relay") from None
    if not relay.hostname or relay.username or relay.password or relay.query or relay.fragment or relay.path not in ("", "/"):
        raise Fault(400, "invalid_relay")
    if relay.scheme != "https" and not (allow_loopback and relay.scheme == "http" and relay.hostname in {"127.0.0.1", "localhost", "::1"}):
        raise Fault(400, "https_required")
    verify(card, card["sign_key"])
    return card


def validate_friend_request(packet, *, allow_loopback=False):
    fields = {"v", "id", "recipient", "card", "created_at", "comment", "signature"}
    if not isinstance(packet, dict) or set(packet) != fields or type(packet["v"]) is not int or packet["v"] != 1:
        raise Fault(400, "invalid_friend_request")
    validate_card(packet["card"], allow_loopback=allow_loopback)
    fingerprint(packet["recipient"])
    try:
        if str(UUID(packet["id"])) != packet["id"]:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise Fault(400, "invalid_friend_request") from None
    if not isinstance(packet["created_at"], str) or not 1 <= len(packet["created_at"]) <= 40 or not isinstance(packet["comment"], str) or len(packet["comment"]) > 200 or packet["recipient"] == packet["card"]["id"]:
        raise Fault(400, "invalid_friend_request")
    verify(packet, packet["card"]["sign_key"])
    return packet


class Mailbox:
    def __init__(self, path: Path, *, quota=16 * 1024 * 1024, clock=time.time):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.quota, self.clock = quota, clock
        self.challenges, self.sessions, self.rates = {}, {}, {}
        self.db.executescript("""
        PRAGMA journal_mode=DELETE;
        CREATE TABLE IF NOT EXISTS identities(id TEXT PRIMARY KEY, card TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS allowed(owner TEXT, peer TEXT, PRIMARY KEY(owner,peer));
        CREATE TABLE IF NOT EXISTS envelopes(sender TEXT, id TEXT, recipient TEXT, digest TEXT,
          packet TEXT NOT NULL, created REAL, expires REAL, acked INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(sender,id));
        CREATE INDEX IF NOT EXISTS mailbox_pending ON envelopes(recipient,acked,created);
        CREATE INDEX IF NOT EXISTS mailbox_expiry ON envelopes(expires);
        CREATE TABLE IF NOT EXISTS friend_requests(sender TEXT,id TEXT,recipient TEXT,digest TEXT,packet TEXT,state TEXT,created REAL,expires REAL,version INTEGER,
          PRIMARY KEY(sender,id));
        CREATE INDEX IF NOT EXISTS friend_requests_pending ON friend_requests(recipient,state,created);
        """)

    def close(self):
        self.db.close()

    def prune(self):
        now = self.clock()
        self.challenges = {k: v for k, v in self.challenges.items() if v[2] > now}
        self.sessions = {k: v for k, v in self.sessions.items() if v[1] > now}
        self.rates = {k: v for k, v in self.rates.items() if v[1] > now}
        self.db.execute("DELETE FROM envelopes WHERE expires<=?", (now,))
        self.db.execute("DELETE FROM friend_requests WHERE expires<=?", (now,))

    def limit(self, key, count):
        self.prune()
        hits, until = self.rates.get(key, (0, self.clock() + 60))
        if hits >= count:
            raise Fault(429, "rate_limited")
        if len(self.rates) > 10000:
            raise Fault(503, "busy")
        self.rates[key] = (hits + 1, until)


def create_peer_app(store: Mailbox, *, public_url="", allow_loopback=False):
    @asynccontextmanager
    async def lifespan(app):
        yield
        store.close()

    app = FastAPI(title="astrC encrypted mailbox", version="1.0", lifespan=lifespan)

    @app.exception_handler(Fault)
    async def fault_handler(request, exc):
        return JSONResponse({"error": exc.code}, status_code=exc.status)

    @app.middleware("http")
    async def bounded_body(request: Request, call_next):
        # Enforce streamed size, including requests without Content-Length.
        if request.method in {"POST", "PUT"}:
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > 32768:
                    return JSONResponse({"error": "body_too_large"}, status_code=413)
                body.extend(chunk)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    async def body(request: Request):
        try:
            result = await request.json()
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (ValueError, TypeError):
            raise Fault(400, "invalid_json") from None

    def user(authorization: str | None = Header(default=None)):
        with store.lock:
            store.prune()
            token = authorization.removeprefix("Bearer ") if authorization else ""
            session = store.sessions.get(hashlib.sha256(token.encode()).hexdigest())
            if not session:
                raise Fault(401, "unauthorized")
            return session[0]

    @app.get(PREFIX + "/health")
    def health():
        return {"protocol": "astrc-mailbox/1", "public_url": public_url, "retention_days": 7,
                "max_packet_bytes": 32768, "encrypted_content": True, "friend_requests": True}

    @app.post(PREFIX + "/challenge")
    async def challenge(request: Request):
        data = await body(request)
        identity = fingerprint(data.get("id"))
        with store.lock:
            store.limit(("challenge", request.client.host), 30)
            if len(store.challenges) >= 2048:
                raise Fault(503, "busy")
            cid, value = secrets.token_hex(16), secrets.token_hex(32)
            store.challenges[cid] = (identity, value, store.clock() + 60)
            return {"challenge_id": cid, "challenge": value}

    @app.post(PREFIX + "/session")
    async def session(request: Request):
        data = await body(request)
        card = validate_card(data.get("card"), allow_loopback=allow_loopback)
        with store.lock:
            store.limit(("session", request.client.host), 60)
            cid = data.get("challenge_id")
            if not isinstance(cid, str) or len(cid) != 32:
                raise Fault(400, "invalid_challenge")
            challenge = store.challenges.pop(cid, None)
            if challenge is None or challenge[0] != card["id"]:
                raise Fault(401, "invalid_challenge")
            proof = f"astrc-relay-auth-v1\n{data['challenge_id']}\n{challenge[1]}\n{card['id']}".encode()
            try:
                Ed25519PublicKey.from_public_bytes(unbase(card["sign_key"], 32)).verify(unbase(data.get("signature"), 64), proof)
            except InvalidSignature:
                raise Fault(401, "invalid_proof") from None
            previous = store.db.execute("SELECT card FROM identities WHERE id=?", (card["id"],)).fetchone()
            if previous and json.loads(previous[0])["box_key"] != card["box_key"]:
                raise Fault(409, "identity_key_changed")
            if previous is None and store.db.execute("SELECT COUNT(*) FROM identities").fetchone()[0] >= 10000:
                raise Fault(503, "identity_capacity")
            if len(store.sessions) >= 4096:
                raise Fault(503, "session_capacity")
            store.db.execute("INSERT OR REPLACE INTO identities VALUES (?,?)", (card["id"], canonical(card).decode()))
            token = secrets.token_urlsafe(32)
            store.sessions[hashlib.sha256(token.encode()).hexdigest()] = (card, store.clock() + 3600)
            return {"access_token": token, "expires_in": 3600}

    @app.put(PREFIX + "/allowed/{peer_id}")
    def allow(peer_id: str, owner=Depends(user)):
        fingerprint(peer_id)
        with store.lock:
            if store.db.execute("SELECT 1 FROM allowed WHERE owner=? AND peer=?", (owner["id"], peer_id)).fetchone():
                return {"ok": True}
            if store.db.execute("SELECT COUNT(*) FROM allowed WHERE owner=?", (owner["id"],)).fetchone()[0] >= 1000:
                raise Fault(413, "contacts_full")
            store.db.execute("INSERT OR IGNORE INTO allowed VALUES (?,?)", (owner["id"], peer_id))
        return {"ok": True}

    @app.delete(PREFIX + "/allowed/{peer_id}")
    def block(peer_id: str, owner=Depends(user)):
        with store.lock:
            store.db.execute("DELETE FROM allowed WHERE owner=? AND peer=?", (owner["id"], peer_id))
        return {"ok": True}

    @app.post(PREFIX + "/friend-requests", status_code=201)
    async def request_friend(request: Request, sender=Depends(user)):
        packet = validate_friend_request(await body(request), allow_loopback=allow_loopback)
        if packet["card"]["id"] != sender["id"]:
            raise Fault(403, "invalid_sender")
        encoded = canonical(packet).decode()
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with store.lock:
            store.limit(("friend_request", sender["id"]), 30)
            previous = store.db.execute("SELECT digest,state FROM friend_requests WHERE sender=? AND id=?", (sender["id"], packet["id"])).fetchone()
            if previous:
                if previous["digest"] != digest:
                    raise Fault(409, "idempotency_conflict")
                return {"id": packet["id"], "state": previous["state"]}
            if not store.db.execute("SELECT 1 FROM identities WHERE id=?", (packet["recipient"],)).fetchone():
                raise Fault(404, "recipient_not_registered")
            if store.db.execute("SELECT COUNT(*) FROM friend_requests WHERE recipient=? AND state='pending'", (packet["recipient"],)).fetchone()[0] >= 100:
                raise Fault(413, "requests_full")
            if store.db.execute("SELECT COUNT(*) FROM friend_requests").fetchone()[0] >= 100000:
                raise Fault(503, "relay_full")
            allowed = store.db.execute("SELECT 1 FROM allowed WHERE owner=? AND peer=?", (packet["recipient"], sender["id"])).fetchone()
            state = "approved" if allowed else "pending"
            store.db.execute("INSERT INTO friend_requests VALUES (?,?,?,?,?,?,?,?,?)", (sender["id"], packet["id"], packet["recipient"], digest, encoded, state, store.clock(), store.clock() + TTL, 1))
        return {"id": packet["id"], "state": state}

    @app.get(PREFIX + "/friend-requests")
    def friend_requests(owner=Depends(user)):
        with store.lock:
            rows = store.db.execute("SELECT packet FROM friend_requests WHERE recipient=? AND state='pending' ORDER BY created,sender,id LIMIT 100", (owner["id"],)).fetchall()
        return {"items": [json.loads(row[0]) for row in rows]}

    @app.post(PREFIX + "/friend-requests/{request_id}/resolve")
    async def resolve_friend(request_id: str, request: Request, owner=Depends(user)):
        data = await body(request)
        if set(data) != {"sender", "approve"} or type(data["approve"]) is not bool:
            raise Fault(400, "invalid_friend_resolution")
        fingerprint(data["sender"])
        with store.lock:
            previous = store.db.execute("SELECT state FROM friend_requests WHERE sender=? AND id=? AND recipient=?", (data["sender"], request_id, owner["id"])).fetchone()
            if previous is None:
                raise Fault(404, "request_not_found")
            state = "approved" if data["approve"] else "rejected"
            if previous["state"] != "pending" and previous["state"] != state:
                raise Fault(409, "request_resolved")
            if data["approve"]:
                exists = store.db.execute("SELECT 1 FROM allowed WHERE owner=? AND peer=?", (owner["id"], data["sender"])).fetchone()
                if not exists and store.db.execute("SELECT COUNT(*) FROM allowed WHERE owner=?", (owner["id"],)).fetchone()[0] >= 1000:
                    raise Fault(413, "contacts_full")
                store.db.execute("INSERT OR IGNORE INTO allowed VALUES (?,?)", (owner["id"], data["sender"]))
            store.db.execute("UPDATE friend_requests SET state=? WHERE sender=? AND id=? AND recipient=?", (state, data["sender"], request_id, owner["id"]))
        return {"state": state}


    @app.post(PREFIX + "/envelopes", status_code=201)
    async def send(request: Request, sender=Depends(user)):
        packet = await body(request)
        required = {"v", "id", "sender", "recipient", "created_at", "ephemeral", "nonce", "ciphertext", "mac", "signature"}
        if set(packet) != required or packet["v"] != 1 or packet["sender"] != sender["id"]:
            raise Fault(400, "invalid_packet")
        try:
            if str(UUID(packet["id"])) != packet["id"] or not isinstance(packet["created_at"], str) or len(packet["created_at"]) > 40:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise Fault(400, "invalid_packet") from None
        fingerprint(packet["recipient"])
        unbase(packet["ephemeral"], 32)
        unbase(packet["nonce"], 12)
        unbase(packet["mac"], 16)
        ciphertext = unbase(packet["ciphertext"])
        if not 1 <= len(ciphertext) <= 16384:
            raise Fault(413, "packet_too_large")
        verify(packet, sender["sign_key"])
        encoded = canonical(packet).decode()
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with store.lock:
            store.limit(("send", sender["id"]), 60)
            if not store.db.execute("SELECT 1 FROM allowed WHERE owner=? AND peer=?", (packet["recipient"], sender["id"])).fetchone():
                raise Fault(403, "recipient_has_not_added_you")
            previous = store.db.execute("SELECT digest FROM envelopes WHERE sender=? AND id=?", (sender["id"], packet["id"])).fetchone()
            if previous:
                if previous[0] != digest:
                    raise Fault(409, "idempotency_conflict")
                return {"id": packet["id"], "accepted": True}
            usage = store.db.execute("SELECT COUNT(*),COALESCE(SUM(LENGTH(packet)),0) FROM envelopes WHERE recipient=? AND acked=0", (packet["recipient"],)).fetchone()
            if usage[0] >= 1000 or usage[1] + len(encoded.encode()) > store.quota:
                raise Fault(413, "mailbox_full")
            if store.db.execute("SELECT COUNT(*) FROM envelopes").fetchone()[0] >= 100000:
                raise Fault(503, "relay_full")
            store.db.execute("INSERT INTO envelopes VALUES (?,?,?,?,?,?,?,0)",
                (sender["id"], packet["id"], packet["recipient"], digest, encoded, store.clock(), store.clock() + TTL))
        return {"id": packet["id"], "accepted": True}

    @app.get(PREFIX + "/envelopes")
    def inbox(owner=Depends(user)):
        with store.lock:
            rows = store.db.execute("SELECT packet FROM envelopes WHERE recipient=? AND acked=0 ORDER BY created,sender,id LIMIT 100", (owner["id"],)).fetchall()
            return {"items": [json.loads(r[0]) for r in rows]}

    @app.post(PREFIX + "/ack")
    async def ack(request: Request, owner=Depends(user)):
        data = await body(request)
        ids = data.get("items")
        if not isinstance(ids, list) or len(ids) > 100 or any(not isinstance(x, dict) or set(x) != {"sender", "id"} or not all(isinstance(v, str) for v in x.values()) for x in ids):
            raise Fault(400, "invalid_ack")
        with store.lock:
            for item in ids:
                store.db.execute("UPDATE envelopes SET acked=1,packet='' WHERE recipient=? AND sender=? AND id=?",
                                 (owner["id"], item["sender"], item["id"]))
        return {"ok": True}

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8781)
    parser.add_argument("--public-url", default="")
    parser.add_argument("--allow-loopback", action="store_true", help="Local tests only")
    args = parser.parse_args()
    if not args.public_url and not args.allow_loopback:
        parser.error("--public-url must identify the HTTPS deployment")
    import uvicorn
    store = Mailbox(args.data_root / "peer-mailboxes.sqlite3")
    uvicorn.run(create_peer_app(store, public_url=args.public_url, allow_loopback=args.allow_loopback), host=args.host, port=args.port,
                access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
