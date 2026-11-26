"""Adapters for the existing InteractionCore speech-provider interfaces."""
from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from astrbot_ex.core.providers import mimo_client
from astrbot_ex.core.providers.interaction_provider import STTProvider, TTSProvider


class MiMoSTTProvider(STTProvider):
    """Transcribe a PCM WAV file supplied by any capture adapter."""

    def __init__(self, settings: Mapping[str, Any] | None = None) -> None:
        self.settings = dict(settings or {})

    async def get_text(self, audio_url: str) -> str:
        return await asyncio.to_thread(mimo_client.transcribe, self.settings, audio_url)


class MiMoTTSProvider(TTSProvider):
    """Produce WAV files or PCM chunks without opening a playback device.

    get_audio returns a temporary WAV path, with the same ownership contract as
    AstrBotTTSProvider. InteractionCore hands cleanup to its playback consumer.
    iter_pcm yields 24 kHz mono PCM16LE; the calling adapter owns playback.
    """

    def __init__(self, settings: Mapping[str, Any] | None = None) -> None:
        self.settings = dict(settings or {})

    async def get_audio(self, text: str) -> str:
        def generate() -> str:
            with NamedTemporaryFile(prefix="astrbotex_tts_", suffix=".wav", delete=False) as output:
                path = Path(output.name)
            try:
                mimo_client.synthesize(self.settings, text, path)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            return str(path)

        return await asyncio.to_thread(generate)

    def iter_pcm(self, text: str, *, cancel_event: Any = None) -> Iterator[bytes]:
        return mimo_client.stream_pcm(self.settings, text, cancel_event=cancel_event)
