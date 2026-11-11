"""Start and close the optional chat listener without starting robot motion."""
from __future__ import annotations

import os
import json
import secrets
from pathlib import Path
import socket
import threading
import time

from .repository import ChatRepository
from .service import ChatService


class ChatLifecycle:
    def __init__(self, data_root: Path, *, host: str = "127.0.0.1", port: int = 8780, demo: bool = False,
                 invite_token: str | None = None, bot_id: int | None = None, reverse_url: str | None = None, bot_token: str | None = None, peer_relay: str | None = None, peer_name: str = "AstrBot", peer_auto_accept: bool = False):
        if demo and host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Demo mode only supports loopback binding")
        self.host, self.port, self.demo = host, port, demo
        self.repository = ChatRepository(data_root / "chat" / "chat.sqlite3")
        self.service = ChatService(self.repository)
        self.connector = None
        self.data_root, self.bot_id = data_root, bot_id
        self.peer_config = data_root / "chat/peer-account.json"
        self.peer_account = None
        self._server = None
        self._thread = None
        self._socket = None
        self._failure = None
        try:
            if demo:
                self.service.seed_demo()
            if peer_relay:
                self.configure_peer(peer_name, peer_relay, peer_auto_accept)
            elif self.peer_config.exists():
                config = json.loads(self.peer_config.read_text(encoding="utf-8"))
                if bot_id is not None and bot_id != config["bot_id"]:
                    raise ValueError("Configured OneBot account differs from the saved peer identity")
                self.bot_id = config["bot_id"]
                self.configure_peer(config["name"], config["relay"], config["auto_accept"])
            if reverse_url:
                if self.bot_id is None or not bot_token:
                    raise ValueError("OneBot reverse URL requires bot ID and access token")
                from .onebot11.transport import ReverseConnector
                self.connector = ReverseConnector(self.service, self.bot_id, reverse_url, bot_token)
            from .native_api import create_app
            self.app = create_app(self.service, demo=demo, invite_token=invite_token, connector=self.connector)
        except BaseException:
            self.repository.close()
            raise

    def configure_peer(self, name: str, relay: str, auto_accept: bool = False):
        from .peer_account import PeerAccount
        from .models import ChatError
        if self.peer_account is not None:
            if self.peer_account.relay != relay.rstrip("/"):
                raise ChatError("migration_required", "An existing identity requires explicit relay migration", 409)
            if type(auto_accept) is not bool:
                raise ChatError("invalid_request", "Friend policy must be boolean")
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40:
                raise ChatError("invalid_request", "Nickname must contain 1 to 40 characters")
            self.peer_account.identity.name = name.strip()
            self.peer_account.auto_accept = auto_accept
        else:
            if self.bot_id is None:
                with self.repository.transaction() as db:
                    row = db.execute("SELECT id FROM users WHERE username='astrc_bot' AND kind='bot'").fetchone()
                self.bot_id = row[0] if row else int(self.service.create_user("astrc_bot", secrets.token_urlsafe(32), name, "bot")["user_id"])
            self.peer_account = PeerAccount(self.service, self.bot_id, self.data_root, relay, name, auto_accept=auto_accept)
            self.service.peer_account = self.peer_account
        config = {"bot_id": self.bot_id, "name": self.peer_account.identity.name, "relay": self.peer_account.relay, "auto_accept": auto_accept}
        temporary = self.peer_config.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(config, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.peer_config)
        return self.peer_status()

    def peer_status(self):
        status = self.peer_account.status() if self.peer_account else {"enabled": False}
        return {**status, "onebot_connected": bool(self.connector and self.connector.connected)}


    @classmethod
    def from_environment(cls, data_root: Path):
        return cls(data_root, host=os.environ.get("ASTREX_CHAT_HOST", "127.0.0.1"), port=int(os.environ.get("ASTREX_CHAT_PORT", "8780")),
                   demo=os.environ.get("ASTREX_CHAT_DEMO", "").lower() in {"1", "true"},
                   invite_token=os.environ.get("ASTREX_CHAT_INVITE_TOKEN"),
                   bot_id=int(os.environ["ASTREX_CHAT_BOT_ID"]) if os.environ.get("ASTREX_CHAT_BOT_ID") else None,
                   reverse_url=os.environ.get("ASTREX_CHAT_ONEBOT_REVERSE_URL"), bot_token=os.environ.get("ASTREX_CHAT_ONEBOT_TOKEN"),
                   peer_relay=os.environ.get("ASTREX_CHAT_PEER_RELAY"), peer_name=os.environ.get("ASTREX_CHAT_PEER_NAME", "AstrBot"),
                   peer_auto_accept=os.environ.get("ASTREX_CHAT_PEER_AUTO_ACCEPT", "").lower() in {"1", "true"})

    def start(self) -> None:
        import uvicorn
        if self._thread is not None:
            raise RuntimeError("Chat listener has already been started")
        try:
            sock = socket.socket(socket.AF_INET6 if ":" in self.host else socket.AF_INET, socket.SOCK_STREAM)
            self._socket = sock
            sock.bind((self.host, self.port))
            self.port = sock.getsockname()[1]
            sock.listen(128)
            self._server = uvicorn.Server(uvicorn.Config(self.app, host=self.host, port=self.port, log_level="warning", access_log=False))
            def serve():
                try:
                    self._server.run(sockets=[sock])
                except BaseException as exc:
                    self._failure = exc
            self._thread = threading.Thread(target=serve, name="astrex-chat", daemon=True)
            self._thread.start()
            deadline = time.monotonic() + 10
            while not self._server.started:
                if self._failure is not None or not self._thread.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError("Chat listener did not start") from self._failure
                time.sleep(0.02)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                raise RuntimeError("Chat listener did not stop; database kept open")
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        self.repository.close()
