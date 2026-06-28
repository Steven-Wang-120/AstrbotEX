from __future__ import annotations

from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.plugin_registry import PluginRegistry
from astrbot_ex.core.runtime import AstrBotEXRuntime
from astrbot_ex.plugins.mock import (
    ApproachEntitySkill,
    BasicRulePlugin,
    MockMotionBridge,
    MockVisionProvider,
    NearestEntityPolicy,
)


def build_demo_runtime() -> AstrBotEXRuntime:
    registry = PluginRegistry()
    registry.register("vision", MockVisionProvider())
    registry.register("motion", MockMotionBridge())
    registry.register("rule", BasicRulePlugin())
    registry.register("policy", NearestEntityPolicy())
    registry.register("skill", ApproachEntitySkill())
    return AstrBotEXRuntime(registry=registry, event_bus=EventBus())


def main() -> None:
    runtime = build_demo_runtime()
    runtime.start()
    for _ in range(5):
        runtime.tick()
    for event in runtime.event_bus.recent():
        print(f"{event.timestamp:.3f} [{event.type}] {event.message} {event.data}")


if __name__ == "__main__":
    main()
