from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from math import isfinite
from time import time
from typing import Any, Literal


class RuntimeState(str, Enum):
    IDLE = "idle"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    FAULT = "fault"
    FINISHED = "finished"


@dataclass(slots=True)
class Entity:
    id: str
    type: str
    confidence: float = 1.0
    semantic: str | None = None
    position: tuple[float, float] | None = None
    bbox_px: tuple[int, int, int, int] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    bearing_deg: float | None = None
    range_m: float | None = None
    range_quality: float | None = None


@dataclass(slots=True)
class Zone:
    id: str
    role: str
    polygon: list[tuple[float, float]]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RobotState:
    pose: tuple[float, float, float] | None = None
    battery_voltage: float | None = None
    link_ok: bool = False
    estop: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class VisionResult:
    frame_id: int
    timestamp: float
    entities: list[Entity] = field(default_factory=list)
    zones: list[Zone] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ScanResult:
    frame_id: int
    timestamp: float
    angle_min_rad: float
    angle_max_rad: float
    angle_increment_rad: float
    ranges: list[float]
    range_min_m: float = 0.0
    range_max_m: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def iter_points(self) -> Iterator[tuple[float, float]]:
        for index, range_m in enumerate(self.ranges):
            if not isfinite(range_m):
                continue
            if range_m < self.range_min_m:
                continue
            if self.range_max_m > 0.0 and range_m > self.range_max_m:
                continue
            yield self.angle_min_rad + index * self.angle_increment_rad, range_m


@dataclass(slots=True)
class ScanCluster:
    id: str
    bearing_deg: float
    range_m: float
    width_deg: float
    point_count: int
    metadata: dict[str, Any] = field(default_factory=dict)
    quality: float = 1.0


@dataclass(slots=True)
class FusedScene:
    timestamp: float
    entities: list[Entity]
    obstacles: list[ScanCluster]
    degraded: bool
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class WorldState:
    timestamp: float = field(default_factory=time)
    entities: list[Entity] = field(default_factory=list)
    zones: list[Zone] = field(default_factory=list)
    robot: RobotState = field(default_factory=RobotState)
    task_state: dict[str, Any] = field(default_factory=dict)
    obstacles: list[ScanCluster] = field(default_factory=list)
    perception_degraded: bool = False


@dataclass(slots=True)
class Goal:
    type: str
    target_entity_id: str | None = None
    target_zone_id: str | None = None
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MotionIntent:
    vx: float = 0.0
    vy: float = 0.0
    wz: float = 0.0
    duration_ms: int = 100
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ActuatorIntent:
    name: str
    value: Any
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Intent:
    motion: MotionIntent | None = None
    actuators: list[ActuatorIntent] = field(default_factory=list)
    note: str = ""


@dataclass(slots=True)
class RuleDecision:
    allowed: bool
    reason: str = ""
    severity: Literal["info", "warning", "error"] = "info"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SkillResult:
    status: Literal["running", "done", "failed"]
    intent: Intent = field(default_factory=Intent)
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RuntimeEvent:
    type: str
    message: str
    timestamp: float = field(default_factory=time)
    data: dict[str, Any] = field(default_factory=dict)
