from __future__ import annotations

from typing import Protocol

from astrbot_ex.core.models import Goal, WorldState
from astrbot_ex.interfaces.base import EXPlugin


class PolicyPlugin(EXPlugin, Protocol):
    def select_goal(self, world: WorldState) -> Goal | None: ...
