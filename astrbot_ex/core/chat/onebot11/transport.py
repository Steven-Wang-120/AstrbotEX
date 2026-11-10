"""Reverse Universal WS with bounded reconnects and a persisted send offset."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging
import time
from urllib.parse import urlsplit

from websockets.asyncio.client import connect
from .actions import ActionDispatcher

logger = logging.getLogger(__name__)


class ReverseConnector:
    def __init__(self, service, bot_id: int, url: str, access_token: str):
        parsed = urlsplit(url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname or parsed.username or parsed.password or not access_token:
            raise ValueError("Invalid OneBot reverse connection")
        self.service, self.bot_id, self.url, self._token = service, bot_id, url, access_token
        self.dispatcher = ActionDispatcher(service, bot_id)
        self.connected = False

    async def run(self) -> None:
        delay = 0.5
        while True:
            try:
                async with connect(self.url, additional_headers={"X-Self-ID": str(self.bot_id), "X-Client-Role": "Universal", "Authorization": "Bearer " + self._token},
                                   open_timeout=10, max_size=1024 * 1024, proxy=None) as socket:
                    self.connected = True
                    delay = 0.5
                    lock = asyncio.Lock()
                    async def send(value):
                        async with lock:
                            await asyncio.wait_for(socket.send(json.dumps(value, ensure_ascii=False)), 5)
                    await send({"time": int(time.time()), "self_id": self.bot_id, "post_type": "meta_event", "meta_event_type": "lifecycle", "sub_type": "connect"})
                    async def receive():
                        async for raw in socket:
                            try:
                                request = json.loads(raw)
                            except (ValueError, UnicodeError):
                                await send({"status": "failed", "retcode": 1400, "data": None, "message": "Invalid JSON"})
                                continue
                            await send(await asyncio.to_thread(self.dispatcher.dispatch, request))
                    async def publish():
                        after = await asyncio.to_thread(self.service.bot_offset, self.bot_id)
                        while True:
                            batch = await asyncio.to_thread(self.service.sync, self.bot_id, after)
                            for event in batch["events"]:
                                outgoing = await asyncio.to_thread(self.dispatcher.event, event)
                                if outgoing:
                                    await send(outgoing)
                                await asyncio.to_thread(self.service.acknowledge_bot_event, self.bot_id, int(event["seq"]))
                            after = batch["next_cursor"]
                            await asyncio.sleep(0.05 if batch["has_more"] else 0.25)
                    async def heartbeat():
                        while True:
                            await asyncio.sleep(15)
                            await send({"time": int(time.time()), "self_id": self.bot_id, "post_type": "meta_event", "meta_event_type": "heartbeat", "status": {"online": True, "good": True}, "interval": 15000})
                    tasks = [asyncio.create_task(f()) for f in (receive, publish, heartbeat)]
                    try:
                        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            task.result()
                    finally:
                        for task in tasks:
                            task.cancel()
                        for task in tasks:
                            with suppress(asyncio.CancelledError, Exception):
                                await task
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("OneBot reverse connection unavailable; retrying")
            finally:
                self.connected = False
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15)
