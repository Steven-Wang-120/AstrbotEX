from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import Any, Literal


@dataclass(slots=True)
class InteractionMessage:
    timestamp: float = field(default_factory=time)
    source: str = ""
    content_type: Literal["text", "audio", "command"] = "text"
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class InteractionSession:
    session_id: str = ""
    messages: list[InteractionMessage] = field(default_factory=list)
    last_activity: float = field(default_factory=time)


@dataclass(slots=True)
class STTRequest:
    audio_url: str = ""
    session_id: str = ""


@dataclass(slots=True)
class TTSRequest:
    text: str = ""
    session_id: str = ""
    voice_id: str | None = None


@dataclass(slots=True)
class AstrBotTextMessage:
    text: str = ""
    session_id: str = ""
    sender_role: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AstrBotReply:
    text: str = ""
    audio_url: str | None = None
    message_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
