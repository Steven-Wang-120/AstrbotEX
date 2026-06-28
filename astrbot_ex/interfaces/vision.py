from __future__ import annotations

from typing import Protocol

from astrbot_ex.core.models import VisionResult
from astrbot_ex.interfaces.base import EXPlugin


class VisionProvider(EXPlugin, Protocol):
    def get_result(self) -> VisionResult: ...
