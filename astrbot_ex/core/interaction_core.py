from __future__ import annotations

import asyncio
import base64
import json
import queue
import re
import threading
import time
import unicodedata
import urllib.request
import uuid
import wave
from pathlib import Path
from tempfile import gettempdir
from typing import Any

from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.plugin_registry import PluginRegistry
from astrbot_ex.core.providers.interaction_provider import STTProvider, TTSProvider
from astrbot_ex.core.topic_bus import TopicBus, TopicMessage


class InteractionCore:
    _FILLER_ONLY = frozenset({"嗯", "嗯嗯", "呃", "额", "唔", "啊啊"})
    _CONFIRMATION_ACKS = frozenset({"嗯", "嗯嗯", "好", "可以", "对"})
    _CONFIRMATION_HINTS = (
        "是否",
        "要不要",
        "需不需要",
        "可以吗",
        "行吗",
        "继续吗",
        "执行吗",
        "确认",
        "确定",
        "同意",
    )
    _BACKCHANNEL_TOPIC = "interaction_core.message.backchannel"
    _CONFIRMATION_TOPIC = "interaction_core.message.confirmation"
    _CAPTURE_TOPIC = "interaction_core.audio.capture"

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
        business_connections: Any | None = None,
        capture_tail_sec: float = 0.5,
        capture_fallback_playback_sec: float = 2.0,
    ) -> None:
        self.registry = registry
        self.topic_bus = topic_bus
        self.event_bus = event_bus
        self.astrbot_base_url = astrbot_base_url.rstrip("/")
        self.session_id = session_id
        self.timeout_sec = timeout_sec
        self.stt_provider = stt_provider
        self.tts_provider = tts_provider
        self.business_connections = business_connections
        self.capture_tail_sec = max(0.0, float(capture_tail_sec))
        self.capture_fallback_playback_sec = max(
            0.0, float(capture_fallback_playback_sec)
        )
        self._unsub_callbacks: list[Any] = []
        self._audio_stop_unsub: Any | None = None
        self._pending_mic_messages: list[TopicMessage] = []
        self._work_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=32)
        self._lock = threading.RLock()
        self._runtime_active = False
        self._worker_stop = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._generation = 0
        self._active_turn_id: str | None = None
        self._last_request_generation: int | None = None
        self._capture_block_until = 0.0
        self._capture_gate_token = 0
        self._capture_resume_timer: threading.Timer | None = None
        self._capture_paused = False
        self._capture_resuming = False
        self._paused_mic_slots: list[tuple[Any, str]] = []
        self._active_capture_utterance_id: str | None = None
        self._suppressed_utterances: dict[str, float] = {}
        self._suppress_unidentified_until = 0.0
        self._awaiting_confirmation_until = 0.0
        self.last_incoming_text_at: float | None = None
        self.last_stt_at: float | None = None
        self.last_astrbot_reply_at: float | None = None
        self.last_tts_audio_at: float | None = None
        self.last_backchannel_at: float | None = None
        self.last_confirmation_at: float | None = None
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
        if self._audio_stop_unsub is None:
            self._audio_stop_unsub = self.topic_bus.subscribe(
                "interaction_core.audio.stop", self._handle_audio_stop
            )
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
        if self._mic_message_is_suppressed(message):
            return
        with self._lock:
            generation = self._generation
            turn_id = self._active_turn_id or uuid.uuid4().hex
        text = self._accept_mic_text(
            text,
            message=message,
            turn_id=turn_id,
            generation=generation,
        )
        if text is None:
            return
        with self._lock:
            self.last_incoming_text_at = time.time()
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
        if self._mic_message_is_suppressed(message):
            self._cleanup_mic_audio(message)
            return
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
        utterance_id = str(message.payload.get("utterance_id") or "").strip() or None
        if state in {"end", "speech_end", "inactive", "stop"}:
            with self._lock:
                if utterance_id == self._active_capture_utterance_id:
                    self._active_capture_utterance_id = None
            return
        if state not in {"start", "speech_start", "active"}:
            return
        with self._lock:
            self._prune_suppressed_utterances_locked()
            self._active_capture_utterance_id = utterance_id
            if self._capture_blocked_locked():
                self._remember_suppressed_utterance_locked(utterance_id)
                suppressed = True
            else:
                suppressed = False
        if suppressed:
            self.event_bus.emit_throttled(
                "interaction",
                "voice activity suppressed during audio playback",
                interval_sec=1.0,
                key="interaction:voice_activity_suppressed",
            )
            return
        with self._lock:
            self._generation += 1
            self._active_turn_id = utterance_id or uuid.uuid4().hex
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
        if self._mic_message_is_suppressed(message):
            self._cleanup_mic_audio(message)
            return
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
            if generation != self._generation or self._capture_blocked_locked():
                return
            self._active_turn_id = turn_id
        text = self._accept_mic_text(
            text,
            message=message,
            turn_id=turn_id,
            generation=generation,
        )
        if text is None:
            return
        with self._lock:
            self.last_incoming_text_at = time.time()
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

    @staticmethod
    def _normalize_mic_text(text: str) -> str:
        value = str(text).strip()
        return re.sub(r"\s+", " ", value)

    @staticmethod
    def _compact_mic_text(text: str) -> str:
        chars: list[str] = []
        for char in text:
            if char.isspace():
                continue
            category = unicodedata.category(char)
            if category and category[0] in {"P", "S"}:
                continue
            chars.append(char)
        return "".join(chars)

    def _accept_mic_text(
        self,
        text: str,
        *,
        message: TopicMessage,
        turn_id: str,
        generation: int,
    ) -> str | None:
        normalized = self._normalize_mic_text(text)
        compact = self._compact_mic_text(unicodedata.normalize("NFKC", normalized))
        if not compact:
            self.event_bus.emit_throttled(
                "interaction",
                "empty microphone transcript ignored",
                interval_sec=1.0,
                key="interaction:empty_transcript",
            )
            return None
        if compact in self._FILLER_ONLY:
            if self._confirmation_is_pending():
                self._publish_confirmation(
                    text=normalized,
                    message=message,
                    turn_id=turn_id,
                    generation=generation,
                )
            else:
                self._publish_backchannel(
                    text=normalized,
                    message=message,
                    turn_id=turn_id,
                    generation=generation,
                )
            return None
        if compact in self._CONFIRMATION_ACKS and self._confirmation_is_pending():
            self._publish_confirmation(
                text=normalized,
                message=message,
                turn_id=turn_id,
                generation=generation,
            )
            return None
        with self._lock:
            self._awaiting_confirmation_until = 0.0
        return normalized

    def _publish_backchannel(
        self,
        *,
        text: str,
        message: TopicMessage,
        turn_id: str,
        generation: int,
    ) -> None:
        with self._lock:
            now = time.time()
            self.last_incoming_text_at = now
            self.last_backchannel_at = now
        self.topic_bus.publish_payload(
            self._BACKCHANNEL_TOPIC,
            timestamp=message.timestamp,
            source=message.source,
            payload={
                "text": text,
                "original_topic": message.topic,
                "turn_id": turn_id,
                "generation": generation,
            },
        )
        self.event_bus.emit_throttled(
            "interaction",
            "filler-only microphone transcript treated as backchannel",
            interval_sec=1.0,
            key="interaction:backchannel",
        )

    def _publish_confirmation(
        self,
        *,
        text: str,
        message: TopicMessage,
        turn_id: str,
        generation: int,
    ) -> None:
        with self._lock:
            now = time.time()
            self.last_incoming_text_at = now
            self.last_confirmation_at = now
            self._awaiting_confirmation_until = 0.0
        self.topic_bus.publish_payload(
            self._CONFIRMATION_TOPIC,
            timestamp=message.timestamp,
            source=message.source,
            payload={
                "confirmed": True,
                "text": text,
                "original_topic": message.topic,
                "turn_id": turn_id,
                "generation": generation,
            },
        )
        self.event_bus.emit(
            "interaction",
            "microphone confirmation recognized",
            generation=generation,
            turn_id=turn_id,
        )

    def _confirmation_is_pending(self) -> bool:
        with self._lock:
            if self._awaiting_confirmation_until > time.monotonic():
                return True
            self._awaiting_confirmation_until = 0.0
            return False

    def _update_confirmation_state(self, text: str, reply: dict[str, Any]) -> None:
        metadata = reply.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        explicit = reply.get("awaiting_confirmation")
        if explicit is None:
            explicit = metadata.get("awaiting_confirmation")
        pending = (
            bool(explicit)
            if explicit is not None
            else self._looks_like_confirmation_prompt(text)
        )
        with self._lock:
            self._awaiting_confirmation_until = (
                time.monotonic() + 15.0 if pending else 0.0
            )

    def _looks_like_confirmation_prompt(self, text: str) -> bool:
        compact = self._compact_mic_text(
            unicodedata.normalize("NFKC", self._normalize_mic_text(text))
        )
        if not compact or not any(hint in compact for hint in self._CONFIRMATION_HINTS):
            return False
        return any(mark in text for mark in ("?", "？")) or any(
            hint in compact
            for hint in ("是否", "要不要", "需不需要", "确认", "确定", "同意")
        )

    def _capture_blocked_locked(self) -> bool:
        return bool(
            self._capture_paused
            or self._capture_resuming
            or self._capture_block_until > time.monotonic()
        )

    def _prune_suppressed_utterances_locked(self) -> None:
        now = time.monotonic()
        expired = [
            utterance_id
            for utterance_id, deadline in self._suppressed_utterances.items()
            if deadline <= now
        ]
        for utterance_id in expired:
            self._suppressed_utterances.pop(utterance_id, None)
        if self._suppress_unidentified_until <= now:
            self._suppress_unidentified_until = 0.0

    def _remember_suppressed_utterance_locked(self, utterance_id: str | None) -> None:
        deadline = max(time.monotonic(), self._capture_block_until) + 2.0
        if utterance_id:
            self._suppressed_utterances[utterance_id] = deadline
        else:
            self._suppress_unidentified_until = max(
                self._suppress_unidentified_until, deadline
            )

    def _mic_message_is_suppressed(self, message: TopicMessage) -> bool:
        utterance_id = str(message.payload.get("utterance_id") or "").strip() or None
        with self._lock:
            self._prune_suppressed_utterances_locked()
            if utterance_id and utterance_id in self._suppressed_utterances:
                return True
            if self._capture_blocked_locked():
                self._remember_suppressed_utterance_locked(utterance_id)
                return True
            return self._suppress_unidentified_until > time.monotonic()

    def _cleanup_mic_audio(self, message: TopicMessage) -> None:
        if not bool(message.payload.get("delete_after_read", False)):
            return
        audio_url = self._resolve_audio_url(message.payload)
        if not audio_url or audio_url.startswith(("http://", "https://")):
            return
        try:
            Path(audio_url).unlink(missing_ok=True)
        except OSError:
            pass

    def _audio_duration_sec(
        self, audio_url: str, metadata: dict[str, Any] | None = None
    ) -> float:
        if metadata is not None:
            try:
                explicit = float(metadata.get("duration_sec", 0.0))
            except (TypeError, ValueError):
                explicit = 0.0
            if explicit > 0.0:
                return explicit
        path_text = str(audio_url)
        if path_text.startswith("file://"):
            path_text = path_text[7:]
        path = Path(path_text)
        if path.is_file() and path.suffix.lower() == ".wav":
            try:
                with wave.open(str(path), "rb") as wav_file:
                    rate = wav_file.getframerate()
                    if rate > 0:
                        return max(0.0, wav_file.getnframes() / rate)
            except (OSError, EOFError, wave.Error):
                pass
        return self.capture_fallback_playback_sec

    def _begin_capture_pause(self, playback_duration_sec: float, reason: str) -> None:
        now = time.monotonic()
        deadline = now + max(0.0, playback_duration_sec) + self.capture_tail_sec
        with self._lock:
            was_blocked = self._capture_blocked_locked()
            self._capture_block_until = max(self._capture_block_until, deadline)
            self._capture_gate_token += 1
            token = self._capture_gate_token
            self._capture_paused = True
            self._prune_suppressed_utterances_locked()
            self._remember_suppressed_utterance_locked(self._active_capture_utterance_id)
        if not was_blocked:
            paused_slots = self._pause_mic_plugins(reason)
            with self._lock:
                if token == self._capture_gate_token:
                    self._paused_mic_slots = paused_slots
        self._publish_capture_state("paused", reason, deadline)
        self._schedule_capture_resume(token, deadline)

    def _pause_mic_plugins(self, reason: str) -> list[tuple[Any, str]]:
        with self._lock:
            runtime_active = self._runtime_active
        if not runtime_active:
            return []
        paused: list[tuple[Any, str]] = []
        for slot in self.registry.list():
            if slot.kind != "mic" or not slot.enabled:
                continue
            try:
                if slot.has_method("pause_capture"):
                    queued = slot.cast("pause_capture", reason)
                    resume_method = "resume_capture" if slot.has_method("resume_capture") else "on_runtime_start"
                elif slot.has_method("on_runtime_stop"):
                    queued = slot.cast("on_runtime_stop", reason)
                    resume_method = "on_runtime_start"
                else:
                    continue
                if queued:
                    paused.append((slot, resume_method))
            except Exception as exc:
                self.event_bus.emit(
                    "plugin_fault",
                    "microphone capture pause failed",
                    plugin=slot.id,
                    error=str(exc),
                )
        return paused

    def _schedule_capture_resume(self, token: int, deadline: float) -> None:
        delay = max(0.01, deadline - time.monotonic())
        timer = threading.Timer(delay, self._resume_capture_if_due, args=(token,))
        timer.daemon = True
        with self._lock:
            old_timer = self._capture_resume_timer
            self._capture_resume_timer = timer
        if old_timer is not None:
            old_timer.cancel()
        timer.start()

    def _resume_capture_if_due(self, token: int) -> None:
        with self._lock:
            if token != self._capture_gate_token:
                return
            remaining = self._capture_block_until - time.monotonic()
            if remaining > 0.0:
                deadline = self._capture_block_until
                resuming = False
            elif self._capture_resuming:
                return
            else:
                self._capture_resuming = True
                slots = list(self._paused_mic_slots)
                self._paused_mic_slots = []
                deadline = 0.0
                resuming = True
        if not resuming:
            self._schedule_capture_resume(token, deadline)
            return
        for slot, method in slots:
            with self._lock:
                if not self._runtime_active:
                    break
            if not slot.enabled:
                continue
            try:
                if not slot.cast(method):
                    raise RuntimeError("plugin actor is not running")
            except Exception as exc:
                self.event_bus.emit(
                    "plugin_fault",
                    "microphone capture resume failed",
                    plugin=slot.id,
                    error=str(exc),
                )
        with self._lock:
            if token != self._capture_gate_token:
                if self._runtime_active:
                    self._paused_mic_slots = slots + self._paused_mic_slots
                self._capture_resuming = False
                return
            self._capture_paused = False
            self._capture_resuming = False
            self._capture_block_until = 0.0
            self._capture_resume_timer = None
        self._publish_capture_state("resumed", "playback_finished", 0.0)

    def _handle_audio_stop(self, message: TopicMessage) -> None:
        del message
        with self._lock:
            if not self._capture_blocked_locked():
                return
            deadline = min(
                self._capture_block_until,
                time.monotonic() + self.capture_tail_sec,
            )
            self._capture_block_until = deadline
            self._capture_gate_token += 1
            token = self._capture_gate_token
        self._schedule_capture_resume(token, deadline)

    def _publish_capture_state(self, state: str, reason: str, deadline: float) -> None:
        remaining = max(0.0, deadline - time.monotonic()) if deadline else 0.0
        self.topic_bus.publish_payload(
            self._CAPTURE_TOPIC,
            timestamp=time.time(),
            source="interaction_core",
            payload={
                "state": state,
                "reason": reason,
                "resume_in_sec": remaining,
            },
        )


    def on_runtime_stop(self, reason: str) -> None:
        self._runtime_active = False
        with self._lock:
            callbacks = self._unsub_callbacks
            self._unsub_callbacks = []
        for unsub in callbacks:
            unsub()
        if self._audio_stop_unsub is not None:
            self._audio_stop_unsub()
            self._audio_stop_unsub = None
        with self._lock:
            timer = self._capture_resume_timer
            self._capture_resume_timer = None
            self._capture_gate_token += 1
            self._capture_block_until = 0.0
            self._capture_paused = False
            self._capture_resuming = False
            self._paused_mic_slots = []
            self._active_capture_utterance_id = None
            self._suppressed_utterances.clear()
            self._suppress_unidentified_until = 0.0
        if timer is not None:
            timer.cancel()
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
        if self.business_connections is not None:
            try:
                result, _ = self.business_connections.request_feature(
                    "text", "interaction.message", payload, timeout_sec=self.timeout_sec
                )
                self.event_bus.emit("interaction", "text sent to astrbot over ZeroMQ", text=text, source=source)
                return result
            except Exception as exc:
                self.event_bus.emit("interaction", "ZeroMQ text send failed", text=text, source=source, error=str(exc))
                return {"ok": False, "error": str(exc)}

        url = f"{self.astrbot_base_url}/api/v1/ex/interaction/message"
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
        audio_metadata = dict(metadata or {})
        duration_sec = self._audio_duration_sec(audio_url, audio_metadata)
        self._begin_capture_pause(duration_sec, "tts_playback")
        self.topic_bus.publish_payload(
            "interaction_core.audio.play",
            timestamp=time.time(),
            source="interaction_core",
            payload={
                "format": audio_format,
                "data": audio_url,
                "delete_after_play": delete_after_play,
                "duration_sec": duration_sec,
                **audio_metadata,
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
            capture_paused = self._capture_blocked_locked()
            capture_resume_in_sec = (
                max(0.0, self._capture_block_until - time.monotonic())
                if capture_paused
                else 0.0
            )
            awaiting_confirmation = (
                self._awaiting_confirmation_until > time.monotonic()
            )
            return {
                "last_incoming_text_at": self.last_incoming_text_at,
                "last_stt_at": self.last_stt_at,
                "last_astrbot_reply_at": self.last_astrbot_reply_at,
                "last_tts_audio_at": self.last_tts_audio_at,
                "last_backchannel_at": self.last_backchannel_at,
                "last_confirmation_at": self.last_confirmation_at,
                "capture_paused": capture_paused,
                "capture_resume_in_sec": capture_resume_in_sec,
                "awaiting_confirmation": awaiting_confirmation,
                "last_error": dict(self.last_error) if self.last_error is not None else None,
            }

    def _handle_mic_text(self, message: TopicMessage) -> None:
        text = str(message.payload.get("text", ""))
        if not text:
            return
        if self._mic_message_is_suppressed(message):
            return
        with self._lock:
            turn_id = self._active_turn_id or uuid.uuid4().hex
            generation = self._generation
        text = self._accept_mic_text(
            text,
            message=message,
            turn_id=turn_id,
            generation=generation,
        )
        if text is None:
            return
        with self._lock:
            self.last_incoming_text_at = time.time()
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

    def _handle_mic_raw(self, message: TopicMessage) -> None:
        if self._mic_message_is_suppressed(message):
            self._cleanup_mic_audio(message)
            return
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
                payload={
                    "text": text,
                    "utterance_id": message.payload.get("utterance_id"),
                },
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
            self._update_confirmation_state(text, reply)
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
