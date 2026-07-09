from __future__ import annotations


class Plugin:
    id = "demo_trace_plugin"
    name = "Demo Trace Plugin"

    def __init__(self, context) -> None:
        self.context = context
        self.enabled = False
        self.ticks = 0
        self.emit_every_ticks = int(context.config.get("emit_every_ticks", 20))

    def on_load(self) -> None:
        self.context.event_bus.emit("plugin", "demo trace plugin loaded", plugin=self.id)

    def on_enable(self) -> None:
        self.enabled = True
        self.context.event_bus.emit("plugin", "demo trace plugin enabled", plugin=self.id)

    def on_disable(self) -> None:
        self.enabled = False
        self.context.event_bus.emit("plugin", "demo trace plugin disabled", plugin=self.id)

    def on_unload(self) -> None:
        self.enabled = False

    def on_runtime_start(self) -> None:
        self.ticks = 0
        self.context.event_bus.emit("trace", "demo trace runtime start", plugin=self.id)

    def on_tick(self, world) -> None:
        if not self.enabled:
            return
        self.ticks += 1
        if self.ticks % self.emit_every_ticks == 0:
            self.context.event_bus.emit(
                "trace",
                "demo trace tick",
                plugin=self.id,
                ticks=self.ticks,
                entities=len(world.entities),
            )

    def on_runtime_stop(self, reason: str) -> None:
        self.context.event_bus.emit(
            "trace",
            "demo trace runtime stop",
            plugin=self.id,
            reason=reason,
            ticks=self.ticks,
        )
