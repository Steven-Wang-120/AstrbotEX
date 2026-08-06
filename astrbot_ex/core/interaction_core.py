from __future__ import annotations

import asyncio
import base64
import json
import queue
import threading
import time
import urllib.request
import uuid
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
        self._work_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=32)
        self._lock = threading.RLock()
        self._runtime_active = False
        self._worker_stop = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._generation = 0
        self._active_turn_id: str | None = None
        self._last_request_generation: int | None = None
        self.last_incoming_text_at: float | None = None
        self.last_stt_at: float | None = None
        self.last_astrbot_reply_at: float | None = None
        self.last_tts_audio_at: float | None = None
        self.last_error: dict[str, Any] | None = None

    def on_runtime_start(self) -> None:
        self._runtime_active = True
        self._worker_stop.clear()
        if self._worker_thread is None or not self._worker_thread.is_alive():
            self._worker_thread = threading.Thread(
                target=self._worker_main,
                name="astrbotex-interaction-worker",
                daemon=True,
            )
            self._worker_thread.start()
        self.refresh_mic_subscriptions()

    def refresh_mic_subscriptions(self) -> None:
        with self._lock:
            callbacks = self._unsub_callbacks
            self._unsub_callbacks = []
        for unsub in callbacks:
            unsub()
        if not self._runtime_active:
            return
        for slot in self.registry.list():
            if slot.kind == "mic" and slot.enabled:
                unsub_text = self.topic_bus.subscribe(
                    f"{slot.id}.audio.text", self._queue_mic_text
                )
                unsub_raw = self.topic_bus.subscribe(
                    f"{slot.id}.audio.raw", self._queue_mic_raw
                )
                unsub_activity = self.topic_bus.subscribe(
                    f"{slot.id}.voice_activity", self._handle_voice_activity
                )
                self._unsub_callbacks.extend([unsub_text, unsub_raw, unsub_activity])

    def _queue_mic_text(self, message: TopicMessage) -> None:
        text = str(message.payload.get("text", ""))
        if not text:
            return
        with self._lock:
            self.last_incoming_text_at = time.time()
            generation = self._generation
            turn_id = self._active_turn_id or uuid.uuid4().hex
        self.topic_bus.publish_payload(
            "interaction_core.message.incoming",
            timestamp=message.timestamp,
            source=message.source,
            payload={
                "text": text,
                "original_topic": message.topic,
                "turn_id": turn_id,
                "generation": generation,
            },
        )
        try:
            self._work_queue.put_nowait(("mic_text", (text, message.source, turn_id, generation)))
        except queue.Full:
            self.event_bus.emit_throttled(
                "interaction",
                "interaction worker queue full",
                interval_sec=1.0,
                key="interaction:queue_full",
                kind="mic_text",
            )

    def _queue_mic_raw(self, message: TopicMessage) -> None:
        try:
            self._work_queue.put_nowait(("mic_raw", message))
        except queue.Full:
            self.event_bus.emit_throttled(
                "interaction",
                "interaction worker queue full",
                interval_sec=1.0,
                key="interaction:queue_full",
                kind="mic_raw",
            )

    def _handle_voice_activity(self, message: TopicMessage) -> None:
        state = str(message.payload.get("state", "")).strip().lower()
        if state not in {"start", "speech_start", "active"}:
            return
        with self._lock:
            self._generation += 1
            self._active_turn_id = str(message.payload.get("utterance_id") or uuid.uuid4().hex)
            generation = self._generation
        self.topic_bus.publish_payload(
            "interaction_core.audio.stop",
            timestamp=time.time(),
            source="interaction_core",
            payload={"reason": "user_speech", "generation": generation},
        )
        self.event_bus.emit(
            "interaction",
            "user speech interrupted playback",
            generation=generation,
            turn_id=self._active_turn_id,
        )

    def _worker_main(self) -> None:
        while not self._worker_stop.is_set():
            try:
                kind, payload = self._work_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if kind == "stop":
                return
            try:
                if kind == "mic_text" and isinstance(payload, tuple):
                    text, source, turn_id, generation = payload
                    self.send_text(
                        text,
                        source=source,
                        turn_id=turn_id,
                        generation=generation,
                    )
                elif kind == "mic_raw" and isinstance(payload, TopicMessage):
                    self._process_mic_raw(payload)
                elif kind == "astrbot_reply" and isinstance(payload, dict):
                    self._handle_astrbot_reply_sync(payload)
            except Exception as exc:
                self.event_bus.emit(
                    "interaction",
                    "interaction worker task failed",
                    kind=kind,
                    error=str(exc),
                )

    def _process_mic_raw(self, message: TopicMessage) -> None:
        with self._lock:
            generation = self._generation
            turn_id = str(
                message.payload.get("utterance_id")
                or self._active_turn_id
                or uuid.uuid4().hex
            )
        audio_url = self._resolve_audio_url(message.payload)
        if audio_url is None:
            self.event_bus.emit(
                "interaction",
                "audio url missing in mic raw payload",
                source=message.source,
            )
            return
        result = self.transcribe_audio(
            audio_url,
            source=message.source,
            delete_after_read=bool(message.payload.get("delete_after_read", False)),
        )
        if not result.get("ok"):
            return
        text = str(result.get("text", ""))
        with self._lock:
            if generation != self._generation:
                return
            self._active_turn_id = turn_id
            self.last_incoming_text_at = time.time()
        if not text:
            return
        self.topic_bus.publish_payload(
            "interaction_core.message.incoming",
            timestamp=message.timestamp,
            source=message.source,
            payload={
                "text": text,
                "original_topic": message.topic,
                "turn_id": turn_id,
                "generation": generation,
            },
        )
        self.send_text(
            text,
            source=message.source,
            turn_id=turn_id,
            generation=generation,
        )


    def on_runtime_stop(self, reason: str) -> None:
        self._runtime_active = False
        with self._lock:
            callbacks = self._unsub_callbacks
            self._unsub_callbacks = []
        for unsub in callbacks:
            unsub()
        self._worker_stop.set()
        try:
            self._work_queue.put_nowait(("stop", None))
        except queue.Full:
            pass
        worker = self._worker_thread
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=max(1.0, self.timeout_sec + 1.0))
        self._worker_thread = None
        while True:
            try:
                self._work_queue.get_nowait()
            except queue.Empty:
                break

    def tick(self) -> None:
        with self._lock:
            pending = self._pending_mic_messages
            self._pending_mic_messages = []
        for message in pending:
            self._handle_mic_text(message)

    def send_text(
        self,
        text: str,
        source: str = "interaction_core",
        *,
        turn_id: str | None = None,
        generation: int | None = None,
    ) -> dict[str, Any]:
        url = f"{self.astrbot_base_url}/api/v1/ex/interaction/message"
        payload = {
            "text": text,
            "session_id": self.session_id,
            "sender_role": "robot",
            "metadata": {
                "source": source,
                **({"turn_id": turn_id} if turn_id is not None else {}),
                **({"generation": generation} if generation is not None else {}),
            },
        }
        if generation is not None:
            with self._lock:
                self._last_request_generation = int(generation)
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

    def publish_audio(
        self,
        audio_url: str,
        format: str | None = None,
        *,
        delete_after_play: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        audio_format = format or Path(audio_url).suffix.lstrip(".") or "wav"
        self.topic_bus.publish_payload(
            "interaction_core.audio.play",
            timestamp=time.time(),
            source="interaction_core",
            payload={
                "format": audio_format,
                "data": audio_url,
                "delete_after_play": delete_after_play,
                **(metadata or {}),
            },
        )

    def transcribe_audio(
        self,
        audio_url: str,
        *,
        source: str = "interaction_core",
        delete_after_read: bool = False,
    ) -> dict[str, Any]:
        if self.stt_provider is None:
            error = "stt provider unavailable"
            self.event_bus.emit(
                "interaction",
                "stt provider unavailable for raw audio",
                source=source,
            )
            self._record_error("stt", error, source=source)
            if delete_after_read:
                try:
                    Path(audio_url).unlink(missing_ok=True)
                except OSError:
                    pass
            return {"ok": False, "error": error}
        try:
            text = asyncio.run(self.stt_provider.get_text(audio_url))
        except Exception as exc:
            self.event_bus.emit(
                "interaction",
                "stt transcription failed",
                source=source,
                error=str(exc),
            )
            self._record_error("stt", str(exc), source=source)
            return {"ok": False, "error": str(exc)}
        finally:
            if delete_after_read:
                try:
                    Path(audio_url).unlink(missing_ok=True)
                except OSError as exc:
                    self.event_bus.emit(
                        "interaction",
                        "temporary mic audio cleanup failed",
                        source=source,
                        error=str(exc),
                    )

        with self._lock:
            self.last_stt_at = time.time()
            self.last_error = None
        if not text:
            self.event_bus.emit(
                "interaction",
                "stt returned empty text",
                source=source,
            )
        return {"ok": True, "text": text}

    def synthesize_text(
        self,
        text: str,
        *,
        source: str = "interaction_core",
    ) -> dict[str, Any]:
        if self.tts_provider is None:
            error = "tts provider unavailable"
            self._record_error("tts", error, source=source)
            return {"ok": False, "error": error}
        try:
            audio_url = asyncio.run(self.tts_provider.get_audio(text))
            self.publish_audio(audio_url, delete_after_play=True)
        except Exception as exc:
            self.event_bus.emit(
                "interaction",
                "tts synthesis failed",
                text=text,
                source=source,
                error=str(exc),
            )
            self._record_error("tts", str(exc), source=source)
            return {"ok": False, "error": str(exc)}
        with self._lock:
            self.last_tts_audio_at = time.time()
            self.last_error = None
        return {"ok": True, "audio_url": audio_url}

    def status_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "last_incoming_text_at": self.last_incoming_text_at,
                "last_stt_at": self.last_stt_at,
                "last_astrbot_reply_at": self.last_astrbot_reply_at,
                "last_tts_audio_at": self.last_tts_audio_at,
                "last_error": dict(self.last_error) if self.last_error is not None else None,
            }

    def _handle_mic_text(self, message: TopicMessage) -> None:
        text = message.payload.get("text", "")
        if not text:
            return
        with self._lock:
            self.last_incoming_text_at = time.time()
        self.topic_bus.publish_payload(
            "interaction_core.message.incoming",
            timestamp=message.timestamp,
            source=message.source,
            payload={"text": text, "original_topic": message.topic},
        )
        self.send_text(text, source=message.source)

    def _handle_mic_raw(self, message: TopicMessage) -> None:
        audio_url = self._resolve_audio_url(message.payload)
        if audio_url is None:
            self.event_bus.emit(
                "interaction",
                "audio url missing in mic raw payload",
                source=message.source,
            )
            return
        result = self.transcribe_audio(
            audio_url,
            source=message.source,
            delete_after_read=bool(message.payload.get("delete_after_read", False)),
        )
        if not result.get("ok"):
            return
        text = str(result.get("text", ""))
        if text:
            synthetic = TopicMessage(
                topic=message.topic.replace(".raw", ".text"),
                timestamp=message.timestamp,
                source=message.source,
                payload={"text": text},
            )
            self._handle_mic_text(synthetic)

    def handle_astrbot_reply(self, reply: dict[str, Any]) -> None:
        if self._worker_thread is not None and self._worker_thread.is_alive():
            try:
                self._work_queue.put_nowait(("astrbot_reply", dict(reply)))
            except queue.Full:
                self.event_bus.emit_throttled(
                    "interaction",
                    "interaction worker queue full",
                    interval_sec=1.0,
                    key="interaction:queue_full",
                    kind="astrbot_reply",
                )
            return
        self._handle_astrbot_reply_sync(reply)

    def _handle_astrbot_reply_sync(self, reply: dict[str, Any]) -> None:
        text = reply.get("text", "")
        if text:
            with self._lock:
                self.last_astrbot_reply_at = time.time()
                current_generation = self._generation
            reply_generation = reply.get("generation")
            if reply_generation is None:
                with self._lock:
                    reply_generation = self._last_request_generation
            if reply_generation is not None and int(reply_generation) != current_generation:
                self.event_bus.emit(
                    "interaction",
                    "stale astrbot reply discarded",
                    generation=reply_generation,
                    current_generation=current_generation,
                )
                return
            self.publish_text(text)
        if self.tts_provider is not None and text:
            turn_id = reply.get("turn_id")
            generation = reply.get("generation")
            if generation is None:
                with self._lock:
                    generation = self._last_request_generation
            if generation is None and turn_id is None:
                self.synthesize_text(text, source="astrbot_reply")
                return
            try:
                audio_url = asyncio.run(self.tts_provider.get_audio(text))
            except Exception as exc:
                self.event_bus.emit(
                    "interaction",
                    "tts synthesis failed",
                    text=text,
                    source="astrbot_reply",
                    error=str(exc),
                )
                self._record_error("tts", str(exc), source="astrbot_reply")
                return
            with self._lock:
                current_generation = self._generation
            if generation is not None and int(generation) != current_generation:
                try:
                    Path(audio_url).unlink(missing_ok=True)
                except OSError:
                    pass
                self.event_bus.emit(
                    "interaction",
                    "stale tts audio discarded",
                    generation=generation,
                    current_generation=current_generation,
                )
                return
            self.publish_audio(
                audio_url,
                delete_after_play=True,
                metadata={
                    **({"turn_id": str(turn_id)} if turn_id is not None else {}),
                    **({"generation": int(generation)} if generation is not None else {}),
                },
            )
            with self._lock:
                self.last_tts_audio_at = time.time()

    def _handle_astrbot_reply(self, reply: dict[str, Any]) -> None:
        self.handle_astrbot_reply(reply)

    def _record_error(self, operation: str, error: str, **details: Any) -> None:
        with self._lock:
            self.last_error = {
                "operation": operation,
                "error": error,
                "timestamp": time.time(),
                **details,
            }

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
