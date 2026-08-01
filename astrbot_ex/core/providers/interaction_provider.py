from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class STTProvider(ABC):
    @abstractmethod
    async def get_text(self, audio_url: str) -> str:
        raise NotImplementedError


class TTSProvider(ABC):
    @abstractmethod
    async def get_audio(self, text: str) -> str:
        raise NotImplementedError

    def support_stream(self) -> bool:
        return False

    async def get_audio_stream(self, text_queue: Any, audio_queue: Any) -> None:
        raise NotImplementedError
