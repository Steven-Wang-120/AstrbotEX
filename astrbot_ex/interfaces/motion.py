from __future__ import annotations

from typing import Protocol

from astrbot_ex.core.models import Intent, RobotState
from astrbot_ex.interfaces.base import EXPlugin


class MotionBridge(EXPlugin, Protocol):
    def send(self, intent: Intent) -> None: ...

    def stop(self, reason: str) -> None: ...

    def read_state(self) -> RobotState: ...
