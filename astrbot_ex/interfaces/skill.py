from __future__ import annotations

from typing import Protocol

from astrbot_ex.core.models import Goal, SkillResult, WorldState
from astrbot_ex.interfaces.base import EXPlugin


class SkillPlugin(EXPlugin, Protocol):
    def can_run(self, world: WorldState, goal: Goal) -> bool: ...

    def start(self, world: WorldState, goal: Goal) -> None: ...

    def tick(self, world: WorldState) -> SkillResult: ...

    def cancel(self, reason: str) -> None: ...
