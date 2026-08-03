from __future__ import annotations

import base64
import json
import urllib.request
import uuid
from pathlib import Path
from tempfile import gettempdir
from typing import Any

from astrbot_ex.core.providers.interaction_provider import STTProvider, TTSProvider


class AstrBotSTTProvider(STTProvider):
    def __init__(
        self,
        astrbot_base_url: str,
        timeout_sec: float = 10.0,
        event_bus: Any | None = None,
    ) -> None:
        self._url = astrbot_base_url.rstrip("/") + "/api/v1/ex/interaction/stt"
        self._timeout = timeout_sec
        self._event_bus = event_bus

    async def get_text(self, audio_url: str) -> str:
        payload_data: dict[str, Any]
        if audio_url.startswith("http://") or audio_url.startswith("https://"):
            payload_data = {"audio_url": audio_url}
        else:
            audio_path = Path(audio_url)
            if not audio_path.is_file():
                raise FileNotFoundError(f"audio file not found: {audio_url}")
            audio_bytes = audio_path.read_bytes()
            if len(audio_bytes) > 25 * 1024 * 1024:
                raise ValueError("audio file exceeds 25 MiB limit")
            payload_data = {
                "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
                "filename": audio_path.name,
            }
        payload = json.dumps(payload_data).encode("utf-8")
        req = urllib.request.Request(
            self._url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                result: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            if self._event_bus is not None:
                self._event_bus.emit("interaction", "astrbot stt request failed", error=str(exc))
            raise
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "stt failed"))
        return str(result.get("text", ""))


class AstrBotTTSProvider(TTSProvider):
    def __init__(
        self,
        astrbot_base_url: str,
        timeout_sec: float = 30.0,
        event_bus: Any | None = None,
    ) -> None:
        self._url = astrbot_base_url.rstrip("/") + "/api/v1/ex/interaction/tts"
        self._timeout = timeout_sec
        self._event_bus = event_bus

    async def get_audio(self, text: str) -> str:
        payload = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            self._url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                result: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            if self._event_bus is not None:
                self._event_bus.emit("interaction", "astrbot tts request failed", error=str(exc))
            raise
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "tts failed"))
        encoded_audio = result.get("audio_base64")
        if encoded_audio:
            try:
                audio_bytes = base64.b64decode(str(encoded_audio), validate=True)
            except ValueError as exc:
                raise RuntimeError("tts returned invalid base64 audio") from exc
            if len(audio_bytes) > 25 * 1024 * 1024:
                raise RuntimeError("tts audio exceeds 25 MiB limit")
            audio_format = str(result.get("audio_format", "wav")).lower().lstrip(".")
            if not audio_format.isalnum() or len(audio_format) > 8:
                audio_format = "wav"
            dest = Path(gettempdir()) / f"astrbotex_tts_{uuid.uuid4().hex}.{audio_format}"
            dest.write_bytes(audio_bytes)
            return str(dest)

        audio_url = str(result.get("audio_url", ""))
        if not audio_url:
            raise RuntimeError("tts returned no audio data")
        return self._resolve_audio(audio_url)

    def _resolve_audio(self, audio_url: str) -> str:
        if audio_url.startswith("http://") or audio_url.startswith("https://"):
            suffix = Path(urllib.request.url2pathname(audio_url.split("?", 1)[0])).suffix
            if not suffix or len(suffix) > 9:
                suffix = ".wav"
            dest = Path(gettempdir()) / f"astrbotex_tts_{uuid.uuid4().hex}{suffix}"
            try:
                urllib.request.urlretrieve(audio_url, str(dest))
            except Exception as exc:
                if self._event_bus is not None:
                    self._event_bus.emit(
                        "interaction",
                        "tts audio download failed",
                        url=audio_url,
                        error=str(exc),
                    )
                raise
            return str(dest)
        return audio_url
