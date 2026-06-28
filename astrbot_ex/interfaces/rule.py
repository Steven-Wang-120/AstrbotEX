from __future__ import annotations

from typing import Protocol

from astrbot_ex.core.models import Intent, RuleDecision, WorldState
from astrbot_ex.interfaces.base import EXPlugin


class RulePlugin(EXPlugin, Protocol):
    def evaluate_world(self, world: WorldState) -> list[RuleDecision]: ...

    def evaluate_intent(self, world: WorldState, intent: Intent) -> RuleDecision: ...
