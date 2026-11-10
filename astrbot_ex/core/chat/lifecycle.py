"""Start and close the optional chat listener without starting robot motion."""
from __future__ import annotations

import os
from pathlib import Path
import socket
import threading
import time

from .repository import ChatRepository
from .service import ChatService


class ChatLifecycle:
    def __init__(self, data_root: Path, *, host: str = "127.0.0.1", port: int = 8780, demo: bool = False,
                 invite_token: str | None = None, bot_id: int | None = None, reverse_url: str | None = None, bot_token: str | None = None):
        if demo and host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Demo mode only supports loopback binding")
        self.host, self.port, self.demo = host, port, demo
        self.repository = ChatRepository(data_root / "chat" / "chat.sqlite3")
        self.service = ChatService(self.repository)
        self.connector = None
        self._server = None
        self._thread = None
        self._socket = None
        self._failure = None
        try:
            if demo:
                self.service.seed_demo()
            if reverse_url:
                if bot_id is None or not bot_token:
                    raise ValueError("OneBot reverse URL requires bot ID and access token")
                from .onebot11.transport import ReverseConnector
                self.connector = ReverseConnector(self.service, bot_id, reverse_url, bot_token)
            from .native_api import create_app
            self.app = create_app(self.service, demo=demo, invite_token=invite_token, connector=self.connector)
        except BaseException:
            self.repository.close()
            raise

    @classmethod
    def from_environment(cls, data_root: Path):
        return cls(data_root, host=os.environ.get("ASTREX_CHAT_HOST", "127.0.0.1"), port=int(os.environ.get("ASTREX_CHAT_PORT", "8780")),
                   demo=os.environ.get("ASTREX_CHAT_DEMO", "").lower() in {"1", "true"},
                   invite_token=os.environ.get("ASTREX_CHAT_INVITE_TOKEN"),
                   bot_id=int(os.environ["ASTREX_CHAT_BOT_ID"]) if os.environ.get("ASTREX_CHAT_BOT_ID") else None,
                   reverse_url=os.environ.get("ASTREX_CHAT_ONEBOT_REVERSE_URL"), bot_token=os.environ.get("ASTREX_CHAT_ONEBOT_TOKEN"))

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
