from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
import urllib.request
from pathlib import Path
from tempfile import gettempdir
from typing import Any

from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.plugin_registry import PluginRegistry
from astrbot_ex.core.providers.interaction_provider import STTProvider, TTSProvider
from astrbot_ex.core.topic_bus import TopicBus, TopicMessage


class InteractionCore:
    def __init__(
        self,
        *,
        registry: PluginRegistry,
        topic_bus: TopicBus,
        event_bus: EventBus,
        astrbot_base_url: str = "http://127.0.0.1:8766",
        session_id: str = "astrbotex_default",
        timeout_sec: float = 5.0,
        stt_provider: STTProvider | None = None,
        tts_provider: TTSProvider | None = None,
    ) -> None:
        self.registry = registry
        self.topic_bus = topic_bus
        self.event_bus = event_bus
        self.astrbot_base_url = astrbot_base_url.rstrip("/")
        self.session_id = session_id
        self.timeout_sec = timeout_sec
        self.stt_provider = stt_provider
        self.tts_provider = tts_provider
        self._unsub_callbacks: list[Any] = []
        self._pending_mic_messages: list[TopicMessage] = []
        self._lock = threading.RLock()

    def on_runtime_start(self) -> None:
        for slot in self.registry.list():
            if slot.kind == "mic":
                unsub_text = self.topic_bus.subscribe(
                    f"{slot.id}.audio.text", self._handle_mic_text
                )
                unsub_raw = self.topic_bus.subscribe(
                    f"{slot.id}.audio.raw", self._handle_mic_raw
                )
                self._unsub_callbacks.extend([unsub_text, unsub_raw])

    def on_runtime_stop(self, reason: str) -> None:
        for unsub in self._unsub_callbacks:
            unsub()
        self._unsub_callbacks.clear()

    def tick(self) -> None:
        with self._lock:
            pending = self._pending_mic_messages
            self._pending_mic_messages = []
        for message in pending:
            self._handle_mic_text(message)

    def send_text(self, text: str, source: str = "interaction_core") -> dict[str, Any]:
        url = f"{self.astrbot_base_url}/api/v1/ex/interaction/message"
        payload = {
            "text": text,
            "session_id": self.session_id,
            "sender_role": "robot",
            "metadata": {"source": source},
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            self.event_bus.emit(
                "interaction",
                "text sent to astrbot",
                text=text,
                source=source,
            )
            return result
        except Exception as exc:
            self.event_bus.emit(
                "interaction",
                "text send failed",
                text=text,
                source=source,
                error=str(exc),
            )
            return {"ok": False, "error": str(exc)}

    def publish_text(self, text: str, source: str = "interaction_core") -> None:
        self.topic_bus.publish_payload(
            "interaction_core.message.outgoing",
            timestamp=time.time(),
            source=source,
            payload={"text": text},
        )

    def publish_audio(self, audio_url: str, format: str = "wav") -> None:
        self.topic_bus.publish_payload(
            "interaction_core.audio.play",
            timestamp=time.time(),
            source="interaction_core",
            payload={"format": format, "data": audio_url},
        )

    def _handle_mic_text(self, message: TopicMessage) -> None:
        text = message.payload.get("text", "")
        if not text:
            return
        self.topic_bus.publish_payload(
            "interaction_core.message.incoming",
            timestamp=message.timestamp,
            source=message.source,
            payload={"text": text, "original_topic": message.topic},
        )
        self.send_text(text, source=message.source)

    def _handle_mic_raw(self, message: TopicMessage) -> None:
        if self.stt_provider is None:
            self.event_bus.emit(
                "interaction",
                "stt provider unavailable for raw audio",
                source=message.source,
            )
            return
        audio_url = self._resolve_audio_url(message.payload)
        if audio_url is None:
            self.event_bus.emit(
                "interaction",
                "audio url missing in mic raw payload",
                source=message.source,
            )
            return
        try:
            text = asyncio.run(self.stt_provider.get_text(audio_url))
        except Exception as exc:
            self.event_bus.emit(
                "interaction",
                "stt transcription failed",
                source=message.source,
                error=str(exc),
            )
            return
        if text:
            synthetic = TopicMessage(
                topic=message.topic.replace(".raw", ".text"),
                timestamp=message.timestamp,
                source=message.source,
                payload={"text": text},
            )
            self._handle_mic_text(synthetic)

    def _handle_astrbot_reply(self, reply: dict[str, Any]) -> None:
        text = reply.get("text", "")
        if text:
            self.publish_text(text)
        if self.tts_provider is not None and text:
            try:
                audio_url = asyncio.run(self.tts_provider.get_audio(text))
                self.publish_audio(audio_url)
            except Exception as exc:
                self.event_bus.emit(
                    "interaction",
                    "tts synthesis failed",
                    text=text,
                    error=str(exc),
                )

    def _resolve_audio_url(self, payload: dict[str, Any]) -> str | None:
        raw = payload.get("audio_url") or payload.get("data") or payload.get("audio")
        if not raw:
            return None
        audio_url = str(raw)
        if audio_url.startswith("data:"):
            return self._write_base64_to_temp(audio_url)
        if audio_url.startswith("file://"):
            return Path(audio_url[7:]).as_posix()
        return audio_url

    def _write_base64_to_temp(self, data_uri: str) -> str | None:
        try:
            header, encoded = data_uri.split(",", 1)
            ext = "wav"
            if ";" in header:
                mime = header.split(";")[0].replace("data:", "")
                if mime == "audio/wav":
                    ext = "wav"
                elif mime == "audio/mpeg":
                    ext = "mp3"
                elif mime == "audio/ogg":
                    ext = "ogg"
            decoded = base64.b64decode(encoded)
            temp_path = Path(gettempdir()) / f"astrbotex_audio_{int(time.time() * 1000)}.{ext}"
            temp_path.write_bytes(decoded)
            return temp_path.as_posix()
        except Exception:
            return None
